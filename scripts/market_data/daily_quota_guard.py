"""Fail-closed cost gate for the scheduled M2 daily workflow."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone


MAX_USAGE_PERCENT = 80
MAX_ATTESTATION_AGE_DAYS = 7


def evaluate_daily_quota(
    *,
    event_name: str,
    schedule_enabled: str,
    reported_percent: str,
    storage_percent: str,
    checked_at: str,
    now: datetime,
) -> dict[str, object]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("quota observation time must be timezone-aware")
    scheduled = event_name == "schedule"
    if scheduled and schedule_enabled.strip().lower() != "true":
        raise RuntimeError("scheduled M2 daily ingestion is disabled")
    try:
        percent = DecimalPercent(reported_percent)
        storage = DecimalPercent(storage_percent)
    except ValueError as error:
        raise RuntimeError("numeric TiDB monthly RU and storage percentages are required") from error
    if percent >= MAX_USAGE_PERCENT:
        raise RuntimeError(
            f"TiDB monthly RU usage {percent:g}% reached the {MAX_USAGE_PERCENT}% nonessential-work stop"
        )
    if storage >= MAX_USAGE_PERCENT:
        raise RuntimeError(
            f"TiDB row-storage usage {storage:g}% reached the {MAX_USAGE_PERCENT}% nonessential-work stop"
        )
    try:
        observed_date = date.fromisoformat(checked_at)
    except ValueError as error:
        raise RuntimeError("TiDB RU checked-at date must use YYYY-MM-DD") from error
    age = now.astimezone(timezone.utc).date() - observed_date
    if age < timedelta(0):
        raise RuntimeError("TiDB RU attestation date cannot be in the future")
    if age > timedelta(days=MAX_ATTESTATION_AGE_DAYS):
        raise RuntimeError(
            f"TiDB RU attestation is {age.days} days old; maximum is {MAX_ATTESTATION_AGE_DAYS}"
        )
    return {
        "allowed": True,
        "event_name": event_name,
        "reported_percent": percent,
        "storage_percent": storage,
        "checked_at": observed_date.isoformat(),
        "attestation_age_days": age.days,
        "threshold_percent": MAX_USAGE_PERCENT,
    }


def DecimalPercent(value: str) -> float:
    text = value.strip().removesuffix("%").strip()
    try:
        result = float(text)
    except ValueError as error:
        raise ValueError("invalid percentage") from error
    if not 0 <= result <= 100:
        raise ValueError("percentage outside 0..100")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="M2 daily TiDB cost guard")
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--schedule-enabled", default="false")
    parser.add_argument("--reported-percent", required=True)
    parser.add_argument("--storage-percent", required=True)
    parser.add_argument("--checked-at", required=True)
    args = parser.parse_args()
    result = evaluate_daily_quota(
        event_name=args.event_name,
        schedule_enabled=args.schedule_enabled,
        reported_percent=args.reported_percent,
        storage_percent=args.storage_percent,
        checked_at=args.checked_at,
        now=datetime.now(timezone.utc),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
