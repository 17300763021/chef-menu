"""Run stock tasks in GitHub Actions and sync generated CSV to Supabase."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sync_stock_data import SupabaseRest, env_value, read_env_file


ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "scripts" / "stock_engine"


def run_command(args: list[str], cwd: Path) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, cwd=cwd, check=True)


def get_client() -> SupabaseRest:
    env = read_env_file()
    url = env_value("VITE_SUPABASE_URL", env)
    key = env_value("SUPABASE_SERVICE_ROLE_KEY", env)
    if not key:
        print("Missing SUPABASE_SERVICE_ROLE_KEY", file=sys.stderr)
        raise SystemExit(1)
    return SupabaseRest(url, key)


def patch_request(client: SupabaseRest, request_id: str, payload: dict[str, Any]) -> None:
    client.request("PATCH", f"stock_job_requests?id=eq.{request_id}", payload, prefer="return=minimal")


def pending_requests(client: SupabaseRest) -> list[dict[str, Any]]:
    rows = client.request(
        "GET",
        "stock_job_requests?status=eq.pending&order=requested_at.asc&limit=5",
    )
    return rows or []


def open_positions(client: SupabaseRest) -> list[dict[str, Any]]:
    rows = client.request(
        "GET",
        "stock_positions?status=eq.open&select=code,name,cost_price,shares",
    )
    return rows or []


def write_holdings_csv(client: SupabaseRest) -> Path | None:
    rows = open_positions(client)
    if not rows:
        return None
    path = ENGINE_DIR / "holdings_from_supabase.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["代码", "名称", "成本", "股数"])
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "代码": str(row.get("code", "")).zfill(6),
                "名称": row.get("name", ""),
                "成本": row.get("cost_price", 0),
                "股数": row.get("shares", 0),
            })
    return path


def latest_table_date(client: SupabaseRest, table: str, date_column: str) -> str:
    rows = client.request(
        "GET",
        f"{table}?select={date_column}&order={date_column}.desc&limit=1",
    )
    if not rows:
        return ""
    return str(rows[0].get(date_column) or "")


def latest_watch_rows(client: SupabaseRest, table: str, date_column: str, limit: int) -> list[dict[str, Any]]:
    latest_date = latest_table_date(client, table, date_column)
    if not latest_date:
        return []
    rows = client.request(
        "GET",
        f"{table}?{date_column}=eq.{latest_date}&select=*&order=score.desc&limit={limit}",
    )
    return rows or []


def write_watchlist_csv(rows: list[dict[str, Any]], filename: str) -> Path | None:
    if not rows:
        return None
    path = ENGINE_DIR / "watchlists" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "生成日期",
        "代码",
        "名称",
        "排名分",
        "昨收",
        "信号",
        "动作",
        "支撑1",
        "压力1",
        "建议止损",
        "入选理由",
        "主要风险",
        "策略等级",
        "策略复核",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "生成日期": row.get("scan_date", ""),
                "代码": str(row.get("code", "")).zfill(6),
                "名称": row.get("name", ""),
                "排名分": row.get("score", 0),
                "昨收": row.get("prev_close", 0),
                "信号": row.get("signal", ""),
                "动作": row.get("action", ""),
                "支撑1": row.get("support_level", 0),
                "压力1": row.get("resistance_level", 0),
                "建议止损": row.get("stop_loss", 0),
                "入选理由": row.get("reason", ""),
                "主要风险": row.get("risk", ""),
                "策略等级": row.get("strategy_level", ""),
                "策略复核": row.get("review_status", ""),
            })
    return path


def resolve_watchlist(client: SupabaseRest) -> str | None:
    live_limit = int(os.environ.get("STOCK_LIVE_TOP", "10"))
    strong_watchlist = ENGINE_DIR / "watchlists" / "latest_strong_watchlist.csv"
    if has_csv_rows(strong_watchlist):
        return "watchlists/latest_strong_watchlist.csv"
    regular_watchlist = ENGINE_DIR / "watchlists" / "latest_watchlist.csv"
    if has_csv_rows(regular_watchlist):
        return "watchlists/latest_watchlist.csv"

    strong_rows = latest_watch_rows(client, "stock_strong_picks", "scan_date", live_limit)
    rebuilt_strong = write_watchlist_csv(strong_rows, "latest_strong_watchlist.csv")
    if rebuilt_strong and has_csv_rows(rebuilt_strong):
        return "watchlists/latest_strong_watchlist.csv"

    scan_rows = latest_watch_rows(client, "stock_scan_results", "scan_date", live_limit)
    rebuilt_regular = write_watchlist_csv(scan_rows, "latest_watchlist.csv")
    if rebuilt_regular and has_csv_rows(rebuilt_regular):
        return "watchlists/latest_watchlist.csv"

    return None


def sync_generated(stock_dir: Path, include_holdings: bool = False) -> None:
    args = [sys.executable, str(ROOT / "scripts" / "sync_stock_data.py"), "--stock-dir", str(stock_dir)]
    if include_holdings:
        args.append("--sync-holdings")
    run_command(args, ROOT)


def run_paper_trade() -> None:
    run_command([sys.executable, str(ROOT / "scripts" / "paper_trade_engine.py")], ROOT)


def has_csv_rows(path: Path) -> bool:
    if not path.exists():
        return False
    with path.open("r", encoding="utf-8-sig") as handle:
        return len([line for line in handle if line.strip()]) > 1


def run_night_scan() -> None:
    env = os.environ.copy()
    env["A_STOCK_SPOT_SOURCE"] = env.get("A_STOCK_SPOT_SOURCE", "tencent")
    env["A_STOCK_HIST_SOURCE"] = env.get("A_STOCK_HIST_SOURCE", "sina_fast")
    command = [
        sys.executable,
        "-B",
        "a_stock_night_scanner_v7.py",
        "--limit",
        os.environ.get("STOCK_SCAN_LIMIT", "300"),
        "--top",
        os.environ.get("STOCK_SCAN_TOP", "50"),
        "--strong-top",
        os.environ.get("STOCK_STRONG_TOP", "30"),
        "--min-score",
        os.environ.get("STOCK_MIN_SCORE", "70"),
        "--workers",
        os.environ.get("STOCK_SCAN_WORKERS", "4"),
        "--sleep",
        os.environ.get("STOCK_SCAN_SLEEP", "0.03"),
    ]
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ENGINE_DIR, env=env, check=True)


def run_live_decision() -> None:
    env = os.environ.copy()
    env["A_STOCK_SPOT_SOURCE"] = env.get("A_STOCK_SPOT_SOURCE", "tencent")
    client = get_client()
    watchlist = resolve_watchlist(client)
    holdings_path = write_holdings_csv(client)
    if not watchlist and not holdings_path:
        print("No watchlist or open positions available for live decision; skipping.", flush=True)
        return
    command = [
        sys.executable,
        "-B",
        "a_stock_live_decision_v8.py",
        "--show-checks",
        "--minute-period",
        "5",
    ]
    if watchlist:
        command.extend(["--watchlist", watchlist])
    if holdings_path:
        command.extend(["--holdings", holdings_path.name])
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ENGINE_DIR, env=env, check=True)


def execute_job(job_type: str) -> int:
    if job_type == "night_scan":
        run_night_scan()
        sync_generated(ENGINE_DIR)
        return 1
    if job_type == "live_decision":
        run_live_decision()
        sync_generated(ENGINE_DIR)
        run_paper_trade()
        return 2
    if job_type == "paper_trade":
        run_paper_trade()
        return 1
    if job_type == "sync_latest":
        sync_generated(ENGINE_DIR)
        return 1
    if job_type == "full":
        run_night_scan()
        run_live_decision()
        sync_generated(ENGINE_DIR)
        run_paper_trade()
        return 3
    if job_type == "auto":
        hour = datetime.now(timezone.utc).hour
        if hour >= 7:
            return execute_job("full")
        return execute_job("live_decision")
    raise ValueError(f"Unknown job type: {job_type}")


def process_pending() -> None:
    client = get_client()
    rows = pending_requests(client)
    print(json.dumps({"pending": len(rows)}, ensure_ascii=False), flush=True)
    for row in rows:
        request_id = str(row["id"])
        job_type = str(row["job_type"])
        patch_request(client, request_id, {"status": "running", "started_at": datetime.now(timezone.utc).isoformat()})
        try:
            imported = execute_job(job_type)
            patch_request(client, request_id, {
                "status": "success",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error_message": "",
            })
            client.insert("stock_job_runs", [{
                "job_type": f"Web request: {job_type}",
                "status": "success",
                "imported_count": imported,
                "error_message": "",
            }])
        except Exception as reason:
            patch_request(client, request_id, {
                "status": "failed",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error_message": str(reason),
            })
            client.insert("stock_job_runs", [{
                "job_type": f"Web request: {job_type}",
                "status": "failed",
                "imported_count": 0,
                "error_message": str(reason),
            }])
            raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["auto", "full", "night_scan", "live_decision", "paper_trade", "sync_latest", "pending"], default="pending")
    args = parser.parse_args()
    if args.mode == "pending":
        process_pending()
    else:
        imported = execute_job(args.mode)
        client = get_client()
        client.insert("stock_job_runs", [{
            "job_type": f"GitHub Actions: {args.mode}",
            "status": "success",
            "imported_count": imported,
            "error_message": "",
        }])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
