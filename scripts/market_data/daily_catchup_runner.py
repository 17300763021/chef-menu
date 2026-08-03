"""Bounded sequential wrapper for atomically publishing missing daily sessions."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

from scripts.market_data.daily_incremental_runner import DEFAULT_BASE_HISTORY_DATASET_ID, SHANGHAI, run


def catch_up(*, max_sessions: int, base_history_dataset_id: str, output_dir: Path, symbol_attempts: int) -> dict:
    if not 1 <= max_sessions <= 5:
        raise ValueError("daily catch-up must process between 1 and 5 sessions")
    results = []
    for position in range(1, max_sessions + 1):
        result = run(
            observed_at=datetime.now(SHANGHAI),
            base_history_dataset_id=base_history_dataset_id,
            output_dir=output_dir / f"session-{position}",
            requested_target=None,
            initialize_schema=position == 1,
            symbol_attempts=symbol_attempts,
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
    args = parser.parse_args()
    catch_up(max_sessions=args.max_sessions, base_history_dataset_id=args.base_history_dataset_id,
             output_dir=args.output_dir, symbol_attempts=args.symbol_attempts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
