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
    storage_percent: str,
    checked_at: str,
    critical_work: bool = False,
    now: datetime,
) -> dict[str, object]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("quota observation time must be timezone-aware")
    scheduled = event_name == "schedule"
    if scheduled and schedule_enabled.strip().lower() != "true":
        raise RuntimeError("scheduled M2 daily ingestion is disabled")
    try:
        storage = DecimalPercent(storage_percent)
    except ValueError as error:
        raise RuntimeError("a numeric MySQL storage-capacity percentage is required") from error
    if storage >= 100:
        raise RuntimeError("MySQL storage capacity reached 100%; affected work is stopped")
    if storage >= MAX_USAGE_PERCENT and not critical_work:
        raise RuntimeError(
            f"MySQL storage capacity {storage:g}% reached the {MAX_USAGE_PERCENT}% nonessential-work stop"
        )
    try:
        observed_date = date.fromisoformat(checked_at)
    except ValueError as error:
        raise RuntimeError("MySQL storage-capacity checked-at date must use YYYY-MM-DD") from error
    age = now.astimezone(timezone.utc).date() - observed_date
    if age < timedelta(0):
        raise RuntimeError("MySQL storage-capacity attestation date cannot be in the future")
    if age > timedelta(days=MAX_ATTESTATION_AGE_DAYS):
        raise RuntimeError(
            f"MySQL storage-capacity attestation is {age.days} days old; maximum is {MAX_ATTESTATION_AGE_DAYS}"
        )
    return {
        "allowed": True,
        "event_name": event_name,
        "storage_capacity_percent": storage,
        "critical_work": critical_work,
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
    parser = argparse.ArgumentParser(description="M2 daily MySQL storage-capacity guard")
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--schedule-enabled", default="false")
    parser.add_argument("--storage-percent", required=True)
    parser.add_argument("--checked-at", required=True)
    parser.add_argument(
        "--critical-work",
        action="store_true",
        help="allow critical daily work between 80% and 100%; 100% still fails closed",
    )
    args = parser.parse_args()
    result = evaluate_daily_quota(
        event_name=args.event_name,
        schedule_enabled=args.schedule_enabled,
        storage_percent=args.storage_percent,
        checked_at=args.checked_at,
        critical_work=args.critical_work,
        now=datetime.now(timezone.utc),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
