"""Sync East Money capital-flow data into Supabase.

This script is part of the simulation-only quant platform. It records market
data for research and paper trading only; it does not place real orders.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

import requests

from sync_stock_data import SupabaseRest, env_value, read_env_file


ROOT = Path(__file__).resolve().parents[1]
STOCK_ENGINE_DIR = ROOT / "scripts" / "stock_engine"
if str(STOCK_ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(STOCK_ENGINE_DIR))

from a_stock_trade_common_v7 import eastmoney_secid, kill_proxy_env  # noqa: E402


kill_proxy_env()


EASTMONEY_CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
EASTMONEY_ULIST_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
EASTMONEY_FFLOW_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
EASTMONEY_REFERER = "https://quote.eastmoney.com/center/gridlist.html"
DEFAULT_PAGE_SIZE = 500
DEFAULT_FALLBACK_CODES = [
    "600519",
    "000001",
    "000002",
    "300750",
    "601318",
    "600036",
    "601398",
    "600030",
    "000858",
    "002594",
]


def parse_number(value: Any, default: float = 0.0) -> float:
    text = str(value if value is not None else "").replace(",", "").replace("%", "").strip()
    if text in {"", "-", "--", "None", "nan"}:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def normalize_code(value: Any) -> str:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[-6:].zfill(6) if digits else ""


def eastmoney_get_json(
    url: str,
    params: dict[str, Any],
    attempts: int = 3,
    delay_seconds: float = 2.0,
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            session = requests.Session()
            session.trust_env = False
            response = session.request(
                "GET",
                url,
                params=params,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Referer": EASTMONEY_REFERER,
                    "Accept": "application/json,text/plain,*/*",
                },
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            return response.json()
        except Exception as error:  # noqa: BLE001 - network errors are retried uniformly here.
            last_error = error
            if attempt < attempts:
                time.sleep(delay_seconds)
    try:
        query = urlencode(params)
        completed = subprocess.run(
            [
                "curl.exe",
                "-L",
                "--silent",
                "--show-error",
                "--max-time",
                str(int(timeout_seconds)),
                "-A",
                "Mozilla/5.0",
                "-e",
                EASTMONEY_REFERER,
                f"{url}?{query}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds + 3,
        )
    except FileNotFoundError:
        completed = subprocess.run(
            [
                "curl",
                "-L",
                "--silent",
                "--show-error",
                "--max-time",
                str(int(timeout_seconds)),
                "-A",
                "Mozilla/5.0",
                "-e",
                EASTMONEY_REFERER,
                f"{url}?{query}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds + 3,
        )
    if completed.returncode == 0 and completed.stdout.strip():
        return json.loads(completed.stdout)
    raise RuntimeError(
        f"East Money request failed after {attempts} attempts: {last_error}; "
        f"curl fallback rc={completed.returncode} stderr={completed.stderr.strip()}"
    ) from last_error


def extract_diff(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    data = payload.get("data") or {}
    diff = data.get("diff") or []
    total = int(parse_number(data.get("total"), len(diff)))
    return list(diff), total


def fetch_eastmoney_pages(
    params: dict[str, Any],
    http_get_json: Callable[[str, dict[str, Any]], dict[str, Any]] = eastmoney_get_json,
    max_pages: int | None = None,
) -> list[dict[str, Any]]:
    params = params.copy()
    params.setdefault("pn", "1")
    params.setdefault("pz", str(DEFAULT_PAGE_SIZE))
    rows: list[dict[str, Any]] = []

    first_payload = http_get_json(EASTMONEY_CLIST_URL, params)
    first_rows, total = extract_diff(first_payload)
    rows.extend(first_rows)
    page_size = int(params.get("pz") or DEFAULT_PAGE_SIZE)
    total_pages = max(1, (total + page_size - 1) // page_size)
    if max_pages is not None:
        total_pages = min(total_pages, max_pages)

    for page in range(2, total_pages + 1):
        params["pn"] = str(page)
        payload = http_get_json(EASTMONEY_CLIST_URL, params)
        page_rows, _ = extract_diff(payload)
        rows.extend(page_rows)
        time.sleep(0.2)
    return rows


def fetch_north_bound_rows(
    http_get_json: Callable[[str, dict[str, Any]], dict[str, Any]] = eastmoney_get_json,
    max_pages: int | None = None,
) -> list[dict[str, Any]]:
    params = {
        "pn": "1",
        "pz": str(DEFAULT_PAGE_SIZE),
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f62",
        "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23 f:!50",
        "fields": "f12,f14,f62,f184,f3",
    }
    return fetch_eastmoney_pages(params, http_get_json=http_get_json, max_pages=max_pages)


def fetch_moneyflow_rows(
    http_get_json: Callable[[str, dict[str, Any]], dict[str, Any]] = eastmoney_get_json,
    max_pages: int | None = None,
) -> list[dict[str, Any]]:
    params = {
        "pn": "1",
        "pz": str(DEFAULT_PAGE_SIZE),
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f62",
        "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23",
        "fields": "f12,f14,f62,f64,f66,f70,f72,f74,f76,f78,f184",
    }
    return fetch_eastmoney_pages(params, http_get_json=http_get_json, max_pages=max_pages)


def chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def recent_history_codes(client: SupabaseRest, limit: int = 80) -> list[str]:
    rows = client.request(
        "GET",
        f"stock_daily_history?select=code&order=trade_date.desc&limit={max(5, limit * 3)}",
    ) or []
    codes: list[str] = []
    seen: set[str] = set()
    for row in rows:
        code = normalize_code(row.get("code"))
        if code and code not in seen:
            seen.add(code)
            codes.append(code)
        if len(codes) >= limit:
            break
    return codes


def fetch_stock_fflow_row(
    code: str,
    flow_date: str,
    http_get_json: Callable[[str, dict[str, Any]], dict[str, Any]] = eastmoney_get_json,
) -> dict[str, Any] | None:
    payload = http_get_json(
        EASTMONEY_FFLOW_URL,
        {
            "lmt": "20",
            "klt": "101",
            "secid": eastmoney_secid(code),
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55",
        },
    )
    data = payload.get("data") or {}
    klines = data.get("klines") or []
    selected = ""
    for line in klines:
        if str(line).startswith(f"{flow_date},"):
            selected = str(line)
            break
    if not selected and klines:
        selected = str(klines[-1])
    if not selected:
        return None
    parts = selected.split(",")
    if len(parts) < 5:
        return None
    return {
        "f12": normalize_code(data.get("code") or code),
        "f14": data.get("name") or "",
        "kline_date": parts[0],
        "f62": parse_number(parts[1]),
        "f64": parse_number(parts[4]),
        "f66": 0,
        "f184": 0,
    }


def fetch_ulist_moneyflow_rows(
    codes: list[str],
    http_get_json: Callable[[str, dict[str, Any]], dict[str, Any]] = eastmoney_get_json,
    batch_size: int = 50,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for batch in chunks(codes, batch_size):
        if not batch:
            continue
        payload = http_get_json(
            EASTMONEY_ULIST_URL,
            {
                "fltt": "2",
                "secids": ",".join(eastmoney_secid(code) for code in batch),
                "fields": "f12,f14,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87",
            },
        )
        data = payload.get("data") or {}
        rows.extend(data.get("diff") or [])
        time.sleep(0.1)
    return rows


def fetch_fflow_fallback_rows(
    client: SupabaseRest,
    flow_date: str,
    http_get_json: Callable[[str, dict[str, Any]], dict[str, Any]] = eastmoney_get_json,
    limit: int = 80,
) -> list[dict[str, Any]]:
    codes = recent_history_codes(client, limit=limit)
    try:
        rows = fetch_ulist_moneyflow_rows(codes, http_get_json=http_get_json)
        if rows:
            return rows[:limit]
    except Exception as error:  # noqa: BLE001 - fflow below is the slower fallback.
        print(f"East Money ulist fallback failed: {error}", file=sys.stderr)

    try:
        rows = fetch_ulist_moneyflow_rows(DEFAULT_FALLBACK_CODES[:limit], http_get_json=http_get_json)
        if rows:
            return rows[:limit]
    except Exception as error:  # noqa: BLE001 - fflow below is the slower fallback.
        print(f"East Money default-code fallback failed: {error}", file=sys.stderr)

    rows: list[dict[str, Any]] = []
    for code in codes:
        try:
            row = fetch_stock_fflow_row(code, flow_date, http_get_json=http_get_json)
        except Exception as error:  # noqa: BLE001 - one bad stock should not stop the pool.
            print(f"East Money fflow fallback failed for {code}: {error}", file=sys.stderr)
            continue
        if row:
            rows.append(row)
        if len(rows) >= limit:
            break
        time.sleep(0.1)
    return rows


def moneyflow_by_code(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    mapped: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = normalize_code(row.get("f12"))
        if code:
            mapped[code] = row
    return mapped


def row_has_nonzero_flow(row: dict[str, Any]) -> bool:
    return any(
        parse_number(row.get(key)) != 0
        for key in [
            "north_bound_net_inflow",
            "north_bound_holding_change",
            "big_order_net_inflow",
            "big_order_buy_ratio",
            "main_net_inflow",
            "main_net_inflow_ratio",
            "margin_balance_change",
        ]
    )


def build_capital_flow_rows(
    flow_date: str,
    north_bound_rows: list[dict[str, Any]],
    moneyflow_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    flow_map = moneyflow_by_code(moneyflow_rows)
    source_rows = north_bound_rows or moneyflow_rows
    has_north_bound_source = bool(north_bound_rows)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for north_row in source_rows:
        code = normalize_code(north_row.get("f12"))
        if not code or code in seen:
            continue
        seen.add(code)
        flow_row = flow_map.get(code, {})
        name = str(north_row.get("f14") or flow_row.get("f14") or "")
        # Keep the secid conversion visible for downstream debugging and to
        # validate code normalization against the common engine helper.
        eastmoney_secid(code)
        main_net = parse_number(flow_row.get("f62"))
        big_order_net = parse_number(flow_row.get("f72"), main_net) or main_net
        big_order_ratio = parse_number(flow_row.get("f75"), parse_number(flow_row.get("f184")))
        north_net = parse_number(north_row.get("f62")) if has_north_bound_source else 0
        north_change = parse_number(north_row.get("f184")) if has_north_bound_source else 0
        row = {
            "code": code,
            "name": name,
            "flow_date": flow_date,
            "north_bound_net_inflow": north_net,
            "north_bound_holding_pct": 0,
            "north_bound_holding_change": north_change,
            "big_order_net_inflow": big_order_net,
            "big_order_buy_ratio": big_order_ratio,
            "main_net_inflow": main_net,
            "main_net_inflow_ratio": parse_number(flow_row.get("f184")),
            "margin_balance_change": 0,
        }
        rows.append(row)
    return rows


def sync_capital_flow(
    client: SupabaseRest,
    flow_date: str,
    http_get_json: Callable[[str, dict[str, Any]], dict[str, Any]] = eastmoney_get_json,
    max_pages: int | None = None,
    fallback_limit: int = 20,
) -> dict[str, Any]:
    source = "eastmoney_clist"
    try:
        north_rows = fetch_north_bound_rows(http_get_json=http_get_json, max_pages=max_pages)
        money_rows = fetch_moneyflow_rows(http_get_json=http_get_json, max_pages=max_pages)
    except Exception as error:
        print(f"East Money clist unavailable, trying ulist/fflow fallback: {error}", file=sys.stderr)
        source = "eastmoney_ulist_fallback"
        money_rows = fetch_fflow_fallback_rows(
            client,
            flow_date,
            http_get_json=http_get_json,
            limit=fallback_limit,
        )
        north_rows = []
    rows = build_capital_flow_rows(flow_date, north_rows, money_rows)
    upserted = client.upsert("stock_capital_flow", rows, "code,flow_date")
    nonzero_rows = sum(1 for row in rows if row_has_nonzero_flow(row))
    return {
        "source": source,
        "flow_date": flow_date,
        "north_bound_rows": len(north_rows),
        "moneyflow_rows": len(money_rows),
        "rows": len(rows),
        "upserted": upserted,
        "nonzero_rows": nonzero_rows,
    }


def date_range_for_mode(target_date: date, mode: str) -> list[date]:
    if mode == "latest":
        return [target_date]
    if mode == "backfill":
        start = target_date - timedelta(days=59)
        return [start + timedelta(days=offset) for offset in range(60)]
    raise ValueError(f"Unsupported mode: {mode}")


def get_client() -> SupabaseRest:
    env = read_env_file()
    return SupabaseRest(
        env_value("VITE_SUPABASE_URL", env),
        env_value("SUPABASE_SERVICE_ROLE_KEY", env),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().isoformat(), help="Flow date to write, YYYY-MM-DD")
    parser.add_argument("--mode", choices=["latest", "backfill"], default="latest")
    parser.add_argument("--max-pages", type=int, default=None, help="Optional debug limit for East Money pages")
    parser.add_argument("--fallback-limit", type=int, default=20, help="Fallback stock count when clist is unavailable")
    args = parser.parse_args()

    try:
        target_date = date.fromisoformat(args.date)
    except ValueError:
        print("--date must be in YYYY-MM-DD format", file=sys.stderr)
        return 2

    client = get_client()
    summaries = []
    for flow_day in date_range_for_mode(target_date, args.mode):
        print(f"Syncing capital flow for {flow_day.isoformat()}...", flush=True)
        summary = sync_capital_flow(
            client,
            flow_day.isoformat(),
            max_pages=args.max_pages,
            fallback_limit=args.fallback_limit,
        )
        summaries.append(summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)

    latest_summary = summaries[-1] if summaries else {}
    if args.mode == "latest" and (
        int(latest_summary.get("upserted") or 0) < 5 or int(latest_summary.get("nonzero_rows") or 0) < 1
    ):
        print(
            "Capital-flow sync did not meet acceptance: need >=5 rows and at least one nonzero flow row.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps({"mode": args.mode, "runs": summaries}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
