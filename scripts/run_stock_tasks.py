"""Run stock tasks in GitHub Actions and sync generated CSV to Supabase."""

from __future__ import annotations

import argparse
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


def sync_generated(stock_dir: Path, include_holdings: bool = False) -> None:
    args = [sys.executable, str(ROOT / "scripts" / "sync_stock_data.py"), "--stock-dir", str(stock_dir)]
    if include_holdings:
        args.append("--sync-holdings")
    run_command(args, ROOT)


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
    command = [
        sys.executable,
        "-B",
        "a_stock_live_decision_v8.py",
        "--watchlist",
        "watchlists/latest_strong_watchlist.csv",
        "--show-checks",
        "--minute-period",
        "5",
    ]
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
        return 1
    if job_type == "sync_latest":
        sync_generated(ENGINE_DIR)
        return 1
    if job_type == "full":
        run_night_scan()
        run_live_decision()
        sync_generated(ENGINE_DIR)
        return 2
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
    parser.add_argument("--mode", choices=["auto", "full", "night_scan", "live_decision", "sync_latest", "pending"], default="pending")
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
