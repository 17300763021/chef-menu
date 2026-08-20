"""Bounded sequential wrapper for atomically publishing missing daily sessions."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

from scripts.market_data.daily_incremental_runner import DEFAULT_BASE_HISTORY_DATASET_ID, SHANGHAI, run
from scripts.market_data.tidb_daily_store import TiDBConfig, connect, ensure_daily_schema


def _run_capture_shard(
    *, observed_at: datetime, base_history_dataset_id: str, output_dir: Path,
    requested_target: date | None, symbol_attempts: int, shard_index: int, shard_count: int,
) -> int:
    command = [
        sys.executable, "-u", "-m", "scripts.market_data.daily_incremental_runner",
        "--observed-at", observed_at.isoformat(),
        "--base-history-dataset-id", base_history_dataset_id,
        "--output-dir", str(output_dir / f"shard-{shard_index}"),
        "--symbol-attempts", str(symbol_attempts),
        "--shard-index", str(shard_index), "--shard-count", str(shard_count),
        "--defer-finalize",
    ]
    if requested_target is not None:
        command.extend(("--target-session", requested_target.isoformat()))
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        print(f"[daily-shard-{shard_index}] {line.rstrip()}", flush=True)
    return process.wait()


def _capture_parallel(
    *, observed_at: datetime, base_history_dataset_id: str, output_dir: Path,
    requested_target: date | None, symbol_attempts: int, shard_count: int,
) -> None:
    with concurrent.futures.ThreadPoolExecutor(max_workers=shard_count) as executor:
        futures = [
            executor.submit(
                _run_capture_shard, observed_at=observed_at,
                base_history_dataset_id=base_history_dataset_id, output_dir=output_dir,
                requested_target=requested_target, symbol_attempts=symbol_attempts,
                shard_index=shard_index, shard_count=shard_count,
            )
            for shard_index in range(shard_count)
        ]
        codes = [future.result() for future in futures]
    failed = [index for index, code in enumerate(codes) if code != 0]
    if failed:
        raise RuntimeError(f"daily capture shards failed: {failed}")


def catch_up(
    *, max_sessions: int, base_history_dataset_id: str, output_dir: Path,
    symbol_attempts: int, parallel_shards: int = 1,
    requested_target: date | None = None,
    supersedes_dataset_id: str | None = None,
) -> dict:
    if not 1 <= max_sessions <= 5:
        raise ValueError("daily catch-up must process between 1 and 5 sessions")
    if not 1 <= parallel_shards <= 4:
        raise ValueError("daily catch-up parallelism must be between 1 and 4 shards")
    if requested_target is not None and max_sessions != 1:
        raise ValueError("a requested daily target requires max_sessions=1")
    if supersedes_dataset_id is not None and (
        requested_target is None or max_sessions != 1 or parallel_shards != 1
    ):
        raise ValueError("a daily correction requires one explicit target and one shard")
    if parallel_shards > 1:
        connection = connect(TiDBConfig.from_env())
        try:
            ensure_daily_schema(connection)
        finally:
            connection.close()
    results = []
    for position in range(1, max_sessions + 1):
        observed_at = datetime.now(SHANGHAI)
        session_output = output_dir / f"session-{position}"
        if parallel_shards > 1:
            _capture_parallel(
                observed_at=observed_at, base_history_dataset_id=base_history_dataset_id,
                output_dir=session_output, requested_target=requested_target,
                symbol_attempts=symbol_attempts, shard_count=parallel_shards,
            )
            result = run(
                observed_at=observed_at, base_history_dataset_id=base_history_dataset_id,
                output_dir=session_output, requested_target=requested_target,
                initialize_schema=False, symbol_attempts=symbol_attempts,
                finalize_only=True,
            )
        else:
            result = run(
                observed_at=observed_at,
                base_history_dataset_id=base_history_dataset_id,
                output_dir=session_output,
                requested_target=requested_target,
                initialize_schema=position == 1,
                symbol_attempts=symbol_attempts,
                supersedes_dataset_id=supersedes_dataset_id,
            )
        results.append({key: value for key, value in result.items() if key != "manifest"})
        if result.get("event") == "daily_noop":
            break
        if not result.get("accepted"):
            raise RuntimeError(f"daily catch-up stopped at atomic session {position}: {result.get('dataset_id')}")
    summary = {"event": "daily_catchup_completed", "requested_max_sessions": max_sessions, "results": results}
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "catchup-summary.json").write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-sessions", type=int, default=1)
    parser.add_argument("--base-history-dataset-id", default=os.environ.get("M2_BASE_HISTORY_DATASET_ID", "").strip() or DEFAULT_BASE_HISTORY_DATASET_ID)
    parser.add_argument("--output-dir", type=Path, default=Path("daily-market-increment"))
    parser.add_argument("--symbol-attempts", type=int, default=2)
    parser.add_argument("--parallel-shards", type=int, default=1)
    parser.add_argument("--target-session", type=date.fromisoformat)
    parser.add_argument("--supersedes-dataset-id")
    args = parser.parse_args()
    catch_up(max_sessions=args.max_sessions, base_history_dataset_id=args.base_history_dataset_id,
             output_dir=args.output_dir, symbol_attempts=args.symbol_attempts,
             parallel_shards=args.parallel_shards, requested_target=args.target_session,
             supersedes_dataset_id=args.supersedes_dataset_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
