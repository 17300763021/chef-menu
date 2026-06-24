"""Warm the local daily-history cache for the current liquid-stock universe."""

from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ProcessPoolExecutor, as_completed

from strategy_replay import fetch_history, select_universe


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=int, default=3)
    parser.add_argument("--universe", type=int, default=80)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    os.environ["A_STOCK_HIST_STORAGE"] = "local"

    trading_days = max(120, args.years * 250)
    universe = select_universe(args.universe)
    failures: list[dict[str, str]] = []
    sources: dict[str, int] = {}
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(fetch_history, item["code"], trading_days): item
            for item in universe
        }
        for index, future in enumerate(as_completed(futures), start=1):
            item = futures[future]
            code, frame, source = future.result()
            if frame is None or len(frame) < min(120, trading_days):
                failures.append({"code": code, "name": item["name"], "reason": source})
            else:
                sources[source] = sources.get(source, 0) + 1
            if index % 10 == 0 or index == len(futures):
                print(f"缓存进度 {index}/{len(futures)}，失败 {len(failures)}", flush=True)

    print(json.dumps({
        "requested_symbols": len(universe),
        "cached_symbols": len(universe) - len(failures),
        "trading_days": trading_days,
        "sources": sources,
        "failures": failures,
    }, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
