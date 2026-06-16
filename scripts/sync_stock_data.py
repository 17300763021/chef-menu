"""Sync local stock strategy CSV files into Supabase.

Usage:
  set SUPABASE_SERVICE_ROLE_KEY=...
  python scripts/sync_stock_data.py

The script reads the existing Python strategy outputs under
C:\\Users\\middol\\Desktop\\数据备份\\pythonData by default.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STOCK_DIR = Path(r"C:\Users\middol\Desktop\数据备份\pythonData")


def read_env_file() -> dict[str, str]:
    env_path = ROOT / ".env.local"
    values: dict[str, str] = {}
    if not env_path.exists():
      return values
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def env_value(name: str, fallback: dict[str, str]) -> str:
    return os.environ.get(name) or fallback.get(name) or ""


def parse_float(value: Any) -> float | None:
    text = str(value or "").strip().replace("%", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value: Any) -> int | None:
    number = parse_float(value)
    return None if number is None else int(number)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def date_from_timestamp(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return datetime.now().date().isoformat()
    return text.split(" ")[0]


class SupabaseRest:
    def __init__(self, url: str, key: str) -> None:
        if not url or not key:
            raise SystemExit("缺少 SUPABASE_SERVICE_ROLE_KEY 或 VITE_SUPABASE_URL。")
        self.base_url = url.rstrip("/") + "/rest/v1"
        self.key = key

    def request(self, method: str, path: str, body: Any | None = None, prefer: str | None = None) -> Any:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        request = Request(f"{self.base_url}/{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                payload = response.read().decode("utf-8")
                return json.loads(payload) if payload else None
        except HTTPError as error:
            message = error.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Supabase {method} {path} failed: {error.code} {message}") from error
        except URLError as error:
            raise RuntimeError(f"Supabase network failed: {error}") from error

    def delete_equals(self, table: str, column: str, value: str) -> None:
        self.request("DELETE", f"{table}?{column}=eq.{quote(value)}", prefer="return=minimal")

    def insert(self, table: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        self.request("POST", table, rows, prefer="return=minimal")
        return len(rows)

    def upsert(self, table: str, rows: list[dict[str, Any]], conflict: str) -> int:
        if not rows:
            return 0
        self.request(
            "POST",
            f"{table}?on_conflict={quote(conflict)}",
            rows,
            prefer="resolution=merge-duplicates,return=minimal",
        )
        return len(rows)


def scan_row(row: dict[str, str]) -> dict[str, Any]:
    return {
        "scan_date": date_from_timestamp(row.get("生成日期", "")),
        "code": row.get("代码", "").zfill(6),
        "name": row.get("名称", ""),
        "score": parse_float(row.get("排名分")) or 0,
        "prev_close": parse_float(row.get("昨收")) or 0,
        "signal": row.get("信号", ""),
        "action": row.get("动作", ""),
        "support_level": parse_float(row.get("支撑1")) or 0,
        "resistance_level": parse_float(row.get("压力1")) or 0,
        "stop_loss": parse_float(row.get("建议止损")) or 0,
        "reason": row.get("入选理由", ""),
        "risk": row.get("主要风险", ""),
    }


def strong_row(row: dict[str, str]) -> dict[str, Any]:
    mapped = scan_row(row)
    mapped["strategy_level"] = row.get("策略等级", "")
    mapped["review_status"] = row.get("策略复核", "")
    return mapped


def fallback_strong_rows(watch_rows: list[dict[str, str]], limit: int = 10) -> list[dict[str, Any]]:
    ranked = sorted(watch_rows, key=lambda row: parse_float(row.get("排名分")) or 0, reverse=True)
    rows = []
    for row in ranked[:limit]:
        mapped = scan_row(row)
        mapped["strategy_level"] = row.get("策略等级") or row.get("池分类") or "海选精选候选"
        mapped["review_status"] = row.get("策略复核") or "重点池为空，按海选排名展示"
        rows.append(mapped)
    return rows


def live_row(row: dict[str, str]) -> dict[str, Any]:
    time_text = row.get("时间", "")
    return {
        "decision_date": date_from_timestamp(time_text),
        "update_time": time_text.split(" ")[1] if " " in time_text else time_text,
        "code": row.get("代码", "").zfill(6),
        "name": row.get("名称", ""),
        "operation_type": row.get("操作类型", ""),
        "current_price": parse_float(row.get("当前价")) or 0,
        "change_rate": parse_float(row.get("涨跌幅")) or 0,
        "can_buy": row.get("买入判断") == "可以买小仓" or row.get("操作类型") == "可买入",
        "suggest_buy_price": parse_float(row.get("建议买入价")),
        "suggest_sell_price": parse_float(row.get("建议卖出价")),
        "stop_loss": parse_float(row.get("止损位")) or 0,
        "target_price_1": parse_float(row.get("第一止盈价")),
        "final_action": row.get("最终动作", ""),
        "no_buy_reason": row.get("不买原因", ""),
        "sell_reason": row.get("卖出理由", ""),
        "status": row.get("操作类型") or "不买/无动作",
    }


def holding_row(row: dict[str, str]) -> dict[str, Any]:
    cost = parse_float(row.get("成本")) or 0
    shares = parse_int(row.get("股数")) or 0
    market_value = cost * shares
    return {
        "code": row.get("代码", "").zfill(6),
        "name": row.get("名称", ""),
        "cost_price": cost,
        "shares": shares,
        "current_price": cost,
        "market_value": market_value,
        "floating_pnl": 0,
        "pnl_rate": 0,
        "buy_date": datetime.now().date().isoformat(),
        "holding_days": 0,
        "current_suggestion": "从 holdings.csv 同步，等待盘中策略更新",
        "buy_memo": "holdings.csv 初始导入",
        "status": "open",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stock-dir", type=Path, default=DEFAULT_STOCK_DIR)
    parser.add_argument("--sync-holdings", action="store_true", help="同步 holdings.csv 到 stock_positions")
    args = parser.parse_args()

    env = read_env_file()
    url = env_value("VITE_SUPABASE_URL", env)
    key = env_value("SUPABASE_SERVICE_ROLE_KEY", env)
    if not key:
        print("请先设置 SUPABASE_SERVICE_ROLE_KEY。不要用 publishable key 同步个人持仓数据。", file=sys.stderr)
        return 1

    client = SupabaseRest(url, key)
    stock_dir = args.stock_dir

    raw_watch_rows = read_csv(stock_dir / "watchlists" / "latest_watchlist.csv")
    raw_strong_rows = read_csv(stock_dir / "watchlists" / "latest_strong_watchlist.csv")
    watch_rows = [scan_row(row) for row in raw_watch_rows]
    strong_rows = [strong_row(row) for row in raw_strong_rows]
    if not strong_rows and raw_watch_rows:
        strong_rows = fallback_strong_rows(raw_watch_rows)
    live_rows = [live_row(row) for row in read_csv(stock_dir / "live_reports" / "latest_live_decision.csv")]

    counts: dict[str, int] = {}
    if watch_rows:
        scan_date = watch_rows[0]["scan_date"]
        client.delete_equals("stock_scan_results", "scan_date", scan_date)
        counts["stock_scan_results"] = client.insert("stock_scan_results", watch_rows)
    if strong_rows:
        scan_date = strong_rows[0]["scan_date"]
        client.delete_equals("stock_strong_picks", "scan_date", scan_date)
        counts["stock_strong_picks"] = client.insert("stock_strong_picks", strong_rows)
    if live_rows:
        decision_date = live_rows[0]["decision_date"]
        client.delete_equals("stock_live_decisions", "decision_date", decision_date)
        counts["stock_live_decisions"] = client.insert("stock_live_decisions", live_rows)
    if args.sync_holdings:
        holding_rows = [holding_row(row) for row in read_csv(stock_dir / "holdings.csv")]
        counts["stock_positions"] = client.upsert("stock_positions", holding_rows, "code,status")

    client.insert("stock_job_runs", [{
        "job_type": "本地CSV同步",
        "status": "成功",
        "imported_count": sum(counts.values()),
        "error_message": "",
    }])
    print(json.dumps(counts, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
