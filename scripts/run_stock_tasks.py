"""Run stock tasks in GitHub Actions and sync generated CSV to Supabase."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from legacy_account_freeze import LEGACY_ACCOUNT_FREEZE_REASON, LEGACY_ACCOUNT_FROZEN
from sync_stock_data import SupabaseRest, env_value, read_env_file


ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "scripts" / "stock_engine"
SHANGHAI = ZoneInfo("Asia/Shanghai")


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


def live_session_window(now: datetime) -> tuple[datetime, datetime] | None:
    local_now = now.astimezone(SHANGHAI)
    morning_start = datetime.combine(local_now.date(), clock_time(9, 30), SHANGHAI)
    morning_end = datetime.combine(local_now.date(), clock_time(11, 30), SHANGHAI)
    afternoon_start = datetime.combine(local_now.date(), clock_time(13, 0), SHANGHAI)
    afternoon_end = datetime.combine(local_now.date(), clock_time(15, 0), SHANGHAI)
    if local_now <= morning_end and local_now >= morning_start - timedelta(minutes=15):
        return morning_start, morning_end
    if local_now <= afternoon_end and local_now >= afternoon_start - timedelta(minutes=15):
        return afternoon_start, afternoon_end
    return None


def seconds_until_next_cycle(now: datetime) -> int:
    local_now = now.astimezone(SHANGHAI)
    elapsed = local_now.minute % 5 * 60 + local_now.second
    return 300 - elapsed if elapsed else 300


def record_job(client: SupabaseRest, job_type: str, status: str, imported_count: int, error_message: str = "") -> None:
    client.insert("stock_job_runs", [{
        "job_type": job_type,
        "status": status,
        "imported_count": imported_count,
        "error_message": error_message,
    }])


def today_start(now: datetime) -> datetime:
    local_now = now.astimezone(SHANGHAI)
    return datetime.combine(local_now.date(), clock_time(0, 0), SHANGHAI)


def has_todays_scheduled_stock_task(client: SupabaseRest, now: datetime) -> bool:
    started_at = quote(today_start(now).isoformat(), safe="")
    job_types = [
        "GitHub Actions: live_decision",
        "GitHub Actions: full",
        "GitHub Actions watchdog: live_decision",
        "GitHub Actions watchdog: full",
    ]
    for job_type in job_types:
        encoded_job_type = quote(job_type, safe="")
        rows = client.request(
            "GET",
            f"stock_job_runs?select=started_at&job_type=eq.{encoded_job_type}"
            f"&started_at=gte.{started_at}&limit=1",
        )
        if rows:
            return True
    return False


def watchdog_backfill_mode(now: datetime) -> str | None:
    local_now = now.astimezone(SHANGHAI)
    if local_now.weekday() >= 5:
        return None

    date = local_now.date()
    morning_backfill_start = datetime.combine(date, clock_time(9, 35), SHANGHAI)
    morning_end = datetime.combine(date, clock_time(11, 30), SHANGHAI)
    afternoon_backfill_start = datetime.combine(date, clock_time(13, 5), SHANGHAI)
    afternoon_end = datetime.combine(date, clock_time(15, 0), SHANGHAI)
    full_backfill_start = datetime.combine(date, clock_time(15, 50), SHANGHAI)
    full_backfill_end = datetime.combine(date, clock_time(21, 30), SHANGHAI)

    if morning_backfill_start <= local_now <= morning_end:
        return "live_decision"
    if afternoon_backfill_start <= local_now <= afternoon_end:
        return "live_decision"
    if full_backfill_start <= local_now <= full_backfill_end:
        return "full"
    return None


def run_stock_task_watchdog(client: SupabaseRest, now: datetime | None = None) -> str | None:
    if os.environ.get("STOCK_TASK_WATCHDOG", "1").lower() in {"0", "false", "off"}:
        return None

    current = now or datetime.now(SHANGHAI)
    mode = watchdog_backfill_mode(current)
    if not mode or has_todays_scheduled_stock_task(client, current):
        return None

    try:
        imported = execute_job(mode)
        record_job(
            client,
            f"GitHub Actions watchdog: {mode}",
            "success",
            imported,
            "Backfilled missing scheduled stock task.",
        )
        return mode
    except Exception as reason:
        record_job(
            client,
            f"GitHub Actions watchdog: {mode}",
            "failed",
            0,
            f"Backfill failed: {reason}",
        )
        raise


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


def run_paper_trade() -> bool:
    if LEGACY_ACCOUNT_FROZEN:
        print(f"[LegacyAccountFrozen] {LEGACY_ACCOUNT_FREEZE_REASON}", flush=True)
        return False
    run_command([sys.executable, str(ROOT / "scripts" / "paper_trade_engine.py")], ROOT)
    return True


def run_backtest() -> None:
    run_command([sys.executable, str(ROOT / "scripts" / "backtest_engine.py")], ROOT)


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
        os.environ.get("STOCK_SCAN_LIMIT", "1000"),
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
        "--workers",
        os.environ.get("STOCK_LIVE_WORKERS", "4"),
    ]
    if watchlist:
        command.extend(["--watchlist", watchlist])
    if holdings_path:
        command.extend(["--holdings", holdings_path.name])
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ENGINE_DIR, env=env, check=True)


def run_live_session() -> int:
    client = get_client()
    window = live_session_window(datetime.now(SHANGHAI))
    if not window:
        print("Outside A-share live session; skipping.", flush=True)
        return 0
    start, end = window
    now = datetime.now(SHANGHAI)
    if now < start:
        wait_seconds = max(0, int((start - now).total_seconds()))
        print(f"Waiting {wait_seconds}s for session open at {start:%H:%M}.", flush=True)
        time.sleep(wait_seconds)

    completed = 0
    while datetime.now(SHANGHAI) < end:
        cycle_started = datetime.now(SHANGHAI)
        try:
            imported = execute_job("live_decision")
            completed += 1
            record_job(client, "GitHub Actions: live_decision", "success", imported)
        except Exception as reason:
            record_job(client, "GitHub Actions: live_decision", "failed", 0, str(reason))
            print(f"Live decision cycle failed: {reason}", file=sys.stderr, flush=True)

        now = datetime.now(SHANGHAI)
        if now >= end:
            break
        delay = min(seconds_until_next_cycle(now), max(0, int((end - now).total_seconds())))
        print(
            f"Cycle started {cycle_started:%H:%M:%S}; next cycle in {delay}s.",
            flush=True,
        )
        if delay > 0:
            time.sleep(delay)
    return completed


def execute_job(job_type: str) -> int:
    if job_type == "night_scan":
        run_night_scan()
        sync_generated(ENGINE_DIR)
        return 1
    if job_type == "live_decision":
        run_live_decision()
        sync_generated(ENGINE_DIR)
        paper_trade_ran = run_paper_trade()
        return 2 if paper_trade_ran else 1
    if job_type == "live_session":
        return run_live_session()
    if job_type == "paper_trade":
        return 1 if run_paper_trade() else 0
    if job_type == "backtest":
        run_backtest()
        return 1
    if job_type == "sync_latest":
        sync_generated(ENGINE_DIR)
        return 1
    if job_type == "full":
        run_night_scan()
        run_live_decision()
        sync_generated(ENGINE_DIR)
        paper_trade_ran = run_paper_trade()
        run_backtest()
        return 4 if paper_trade_ran else 3
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
    if not rows:
        record_job(client, "GitHub Actions: pending", "success", 0, "No pending stock task requests.")
        run_stock_task_watchdog(client)
        return
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
    run_stock_task_watchdog(client)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["auto", "full", "night_scan", "live_decision", "live_session", "paper_trade", "backtest", "sync_latest", "pending"], default="pending")
    args = parser.parse_args()
    if args.mode == "pending":
        process_pending()
    elif args.mode == "live_session":
        execute_job(args.mode)
    else:
        imported = execute_job(args.mode)
        client = get_client()
        record_job(client, f"GitHub Actions: {args.mode}", "success", imported)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
