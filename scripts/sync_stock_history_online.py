"""Upload cached daily stock history to the online Supabase database."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from sync_stock_data import SupabaseRest, env_value, read_env_file


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_DIR = ROOT / "scripts" / "stock_engine" / "history_cache"


def history_rows(path: Path) -> list[dict[str, Any]]:
    code, adjustment = path.stem.split("_", 1)
    frame = pd.read_csv(path, encoding="utf-8-sig")
    rows = []
    for item in frame.to_dict("records"):
        rows.append({
            "code": code,
            "trade_date": str(item["date"])[:10],
            "adjustment": adjustment,
            "open": float(item["open"]),
            "close": float(item["close"]),
            "high": float(item["high"]),
            "low": float(item["low"]),
            "volume": float(item.get("volume") or 0),
            "amount": None if pd.isna(item.get("amount")) else float(item["amount"]),
            "change_rate": None if pd.isna(item.get("pct")) else float(item["pct"]),
            "source": "sina_qfq_cache_import",
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()

    env = read_env_file()
    client = SupabaseRest(
        env_value("VITE_SUPABASE_URL", env),
        env_value("SUPABASE_SERVICE_ROLE_KEY", env),
    )
    files = sorted(args.cache_dir.glob("*_qfq.csv"))
    imported = 0
    by_code: dict[str, int] = {}
    for index, path in enumerate(files, start=1):
        rows = history_rows(path)
        for start in range(0, len(rows), args.batch_size):
            batch = rows[start:start + args.batch_size]
            client.upsert(
                "stock_daily_history",
                batch,
                "code,trade_date,adjustment",
            )
            imported += len(batch)
        by_code[path.stem[:6]] = len(rows)
        if index % 10 == 0 or index == len(files):
            print(f"线上导入 {index}/{len(files)}，累计 {imported} 行", flush=True)

    print(json.dumps({
        "symbols": len(files),
        "rows": imported,
        "minimum_rows_per_symbol": min(by_code.values(), default=0),
        "maximum_rows_per_symbol": max(by_code.values(), default=0),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

