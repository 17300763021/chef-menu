"""Fail-closed MySQL storage-capacity guard for nonessential M2.5 backfills."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta, timezone


MAX_USAGE_PERCENT = 80.0
MAX_ATTESTATION_AGE_DAYS = 7


def _percent(value: str, label: str) -> float:
    try:
        parsed = float(value.strip().removesuffix("%").strip())
    except ValueError as error:
        raise RuntimeError(f"a numeric MySQL {label} percentage is required") from error
    if not 0 <= parsed <= 100:
        raise RuntimeError(f"MySQL {label} percentage must be between 0 and 100")
    return parsed


def evaluate_industry_quota(
    *,
    storage_percent: str,
    checked_at: str,
    now: datetime,
) -> dict[str, object]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("quota observation time must be timezone-aware")
    storage = _percent(storage_percent, "storage-capacity")
    if storage >= MAX_USAGE_PERCENT:
        raise RuntimeError(
            f"MySQL storage capacity {storage:g}% reached the {MAX_USAGE_PERCENT:g}% nonessential-work stop"
        )
    try:
        observed = date.fromisoformat(checked_at)
    except ValueError as error:
        raise RuntimeError("MySQL quota checked-at date must use YYYY-MM-DD") from error
    age = now.astimezone(timezone.utc).date() - observed
    if age < timedelta(0):
        raise RuntimeError("MySQL quota attestation date cannot be in the future")
    if age > timedelta(days=MAX_ATTESTATION_AGE_DAYS):
        raise RuntimeError(
            f"MySQL quota attestation is {age.days} days old; maximum is {MAX_ATTESTATION_AGE_DAYS}"
        )
    return {
        "allowed": True,
        "storage_capacity_percent": storage,
        "checked_at": observed.isoformat(),
        "attestation_age_days": age.days,
        "threshold_percent": MAX_USAGE_PERCENT,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="M2.5 MySQL storage-capacity guard")
    parser.add_argument("--storage-percent", required=True)
    parser.add_argument("--checked-at", required=True)
    args = parser.parse_args()
    result = evaluate_industry_quota(
        storage_percent=args.storage_percent,
        checked_at=args.checked_at,
        now=datetime.now(timezone.utc),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
