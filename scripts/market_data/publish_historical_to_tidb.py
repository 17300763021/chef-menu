"""Publish M2.3 historical-market evidence into TiDB checkpoint tables."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from scripts.market_data.tidb_checkpoint_store import (
    TiDBConfig,
    connect,
    default_dataset_id,
    ensure_schema,
    load_historical_evidence,
    publish_historical_evidence,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish non-authoritative M2.3 historical evidence to TiDB")
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--dataset-id")
    parser.add_argument("--init-schema", action="store_true")
    parser.add_argument("--allow-unaccepted-checkpoint", action="store_true")
    parser.add_argument("--missing-input-ok", action="store_true")
    parser.add_argument("--publish-attempts", type=int, default=3)
    args = parser.parse_args()
    if args.publish_attempts < 1:
        raise ValueError("--publish-attempts must be at least 1")

    if not (args.input_dir / "manifest.json").exists():
        if args.missing_input_ok:
            print(json.dumps({
                "event": "tidb_publish_skipped",
                "reason": "missing_manifest",
                "input_dir": str(args.input_dir),
            }, ensure_ascii=False, sort_keys=True), flush=True)
            return 0
        raise FileNotFoundError(f"missing manifest under {args.input_dir}")

    evidence = load_historical_evidence(args.input_dir)
    dataset_id = args.dataset_id or default_dataset_id(evidence.manifest)
    config = TiDBConfig.from_env()
    result = None
    last_error: Exception | None = None
    for attempt in range(1, args.publish_attempts + 1):
        connection = None
        try:
            connection = connect(config)
            if args.init_schema:
                ensure_schema(connection)
            result = publish_historical_evidence(
                connection,
                evidence,
                dataset_id=dataset_id,
                allow_unaccepted_checkpoint=args.allow_unaccepted_checkpoint,
            )
            break
        except Exception as error:
            last_error = error
            if connection is not None:
                connection.rollback()
            print(json.dumps({
                "event": "tidb_publish_retry",
                "attempt": attempt,
                "remaining_attempts": args.publish_attempts - attempt,
                "error_type": type(error).__name__,
                "error": str(error)[:300],
            }, ensure_ascii=False, sort_keys=True), flush=True)
            if attempt >= args.publish_attempts:
                raise
            time.sleep(min(2 ** (attempt - 1), 8))
        finally:
            if connection is not None:
                connection.close()
    if result is None:
        assert last_error is not None
        raise last_error

    print(json.dumps({
        "event": "tidb_publish_completed",
        "host": config.host,
        "database": config.database,
        **result,
    }, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
