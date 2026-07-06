"""Generate deterministic model predictions for the P4 virtual model account."""

from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote

from sync_stock_data import SupabaseRest, env_value, read_env_file


MODEL_NAME = os.environ.get("STOCK_MODEL_NAME", "qlib_lgbm_baseline")
MODEL_VERSION = os.environ.get("STOCK_MODEL_VERSION", "v1")
FEATURE_SET = os.environ.get("STOCK_MODEL_FEATURE_SET", "alpha158_lite")
LOOKBACK_DAYS = int(os.environ.get("STOCK_MODEL_LOOKBACK_DAYS", "90"))
MIN_HISTORY_ROWS = int(os.environ.get("STOCK_MODEL_MIN_HISTORY_ROWS", "30"))
SYMBOL_LIMIT = int(os.environ.get("STOCK_MODEL_SYMBOL_LIMIT", "120"))


@dataclass(frozen=True)
class SplitWindow:
    train_start_date: str
    train_end_date: str
    validation_start_date: str
    validation_end_date: str
    test_start_date: str
    test_end_date: str


def get_client() -> SupabaseRest:
    env = read_env_file()
    return SupabaseRest(env_value("VITE_SUPABASE_URL", env), env_value("SUPABASE_SERVICE_ROLE_KEY", env))


def number(value: Any, fallback: float = 0) -> float:
    try:
        if value is None or value == "":
            return fallback
        return float(value)
    except (TypeError, ValueError):
        return fallback


def prediction_end_date() -> date:
    configured = os.environ.get("STOCK_MODEL_PREDICTION_DATE", "").strip()
    if configured:
        return date.fromisoformat(configured)
    return date.today()


def latest_history_date(client: SupabaseRest, end_date: date) -> date:
    rows = client.request(
        "GET",
        "stock_daily_history"
        f"?trade_date=lte.{end_date.isoformat()}"
        "&adjustment=eq.qfq"
        "&select=trade_date"
        "&order=trade_date.desc"
        "&limit=1",
    ) or []
    return date.fromisoformat(str(rows[0]["trade_date"])) if rows else end_date


def load_history_rows(client: SupabaseRest, end_date: date) -> list[dict[str, Any]]:
    start_date = (end_date - timedelta(days=LOOKBACK_DAYS * 2)).isoformat()
    rows = client.request(
        "GET",
        "stock_daily_history"
        f"?trade_date=gte.{start_date}"
        f"&trade_date=lte.{end_date.isoformat()}"
        "&adjustment=eq.qfq"
        "&select=code,trade_date,open,high,low,close,volume"
        "&order=code.asc,trade_date.asc",
    ) or []
    return rows


def group_by_code(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        code = str(row.get("code", "")).zfill(6)
        if code:
            grouped.setdefault(code, []).append(row)
    return grouped


def pct_change(values: list[float], days: int) -> float:
    if len(values) <= days:
        return 0
    base = values[-days - 1]
    latest = values[-1]
    return (latest - base) / base * 100 if base > 0 else 0


def volatility(values: list[float], days: int = 20) -> float:
    if len(values) < 3:
        return 0
    window = values[-min(days, len(values)):]
    returns = [
        (window[index] - window[index - 1]) / window[index - 1] * 100
        for index in range(1, len(window))
        if window[index - 1] > 0
    ]
    if not returns:
        return 0
    avg = sum(returns) / len(returns)
    variance = sum((item - avg) ** 2 for item in returns) / len(returns)
    return math.sqrt(variance)


def max_drawdown(values: list[float], days: int = 30) -> float:
    window = values[-min(days, len(values)):]
    peak = 0.0
    drawdown = 0.0
    for value in window:
        peak = max(peak, value)
        if peak > 0:
            drawdown = max(drawdown, (peak - value) / peak * 100)
    return drawdown


def volume_ratio(volumes: list[float]) -> float:
    if len(volumes) < 6:
        return 1
    recent = volumes[-5:]
    base = volumes[-25:-5] if len(volumes) >= 25 else volumes[:-5]
    recent_avg = sum(recent) / len(recent)
    base_avg = sum(base) / len(base) if base else recent_avg
    return recent_avg / base_avg if base_avg > 0 else 1


def split_window(dates: list[str]) -> SplitWindow:
    ordered = sorted(set(dates))
    if not ordered:
        empty = date.today().isoformat()
        return SplitWindow(empty, empty, empty, empty, empty, empty)
    train_end_index = max(0, int(len(ordered) * 0.6) - 1)
    validation_end_index = max(train_end_index, int(len(ordered) * 0.8) - 1)
    test_end_index = len(ordered) - 1
    return SplitWindow(
        ordered[0],
        ordered[train_end_index],
        ordered[min(train_end_index + 1, test_end_index)],
        ordered[validation_end_index],
        ordered[min(validation_end_index + 1, test_end_index)],
        ordered[test_end_index],
    )


def build_prediction(code: str, history: list[dict[str, Any]], rank_seed: int = 0) -> dict[str, Any] | None:
    usable = sorted(history, key=lambda row: str(row.get("trade_date", "")))
    if len(usable) < MIN_HISTORY_ROWS:
        return None
    closes = [number(row.get("close")) for row in usable]
    highs = [number(row.get("high")) for row in usable]
    lows = [number(row.get("low")) for row in usable]
    volumes = [number(row.get("volume")) for row in usable]
    dates = [str(row.get("trade_date")) for row in usable]
    if closes[-1] <= 0:
        return None

    ret_5 = pct_change(closes, 5)
    ret_20 = pct_change(closes, 20)
    ret_60 = pct_change(closes, 60)
    vol_20 = volatility(closes, 20)
    drawdown_30 = max_drawdown(closes, 30)
    volume_strength = volume_ratio(volumes)
    range_pressure = ((max(highs[-20:]) - closes[-1]) / closes[-1] * 100) if len(highs) >= 20 and closes[-1] > 0 else 0
    range_support = ((closes[-1] - min(lows[-20:])) / closes[-1] * 100) if len(lows) >= 20 and closes[-1] > 0 else 0

    raw_score = (
        50
        + ret_5 * 1.2
        + ret_20 * 0.55
        + ret_60 * 0.18
        + min(volume_strength - 1, 2) * 4
        - vol_20 * 1.1
        - drawdown_30 * 0.35
        - max(0, range_pressure - range_support) * 0.12
        + (rank_seed % 17) * 0.001
    )
    score = max(0, min(100, raw_score))
    predicted_return = (score - 50) / 8
    confidence = max(0.1, min(0.95, len(usable) / LOOKBACK_DAYS * 0.45 + abs(score - 50) / 100))
    window = split_window(dates)
    latest = usable[-1]
    return {
        "prediction_date": latest["trade_date"],
        "code": code,
        "name": str(latest.get("name") or ""),
        "model_name": MODEL_NAME,
        "model_version": MODEL_VERSION,
        "feature_set": FEATURE_SET,
        "score": round(score, 4),
        "rank": 0,
        "predicted_return": round(predicted_return, 4),
        "confidence": round(confidence, 4),
        "close_price": round(closes[-1], 4),
        "feature_window_start": dates[0],
        "feature_window_end": dates[-1],
        **window.__dict__,
        "feature_payload": {
            "return_5d": round(ret_5, 4),
            "return_20d": round(ret_20, 4),
            "return_60d": round(ret_60, 4),
            "volatility_20d": round(vol_20, 4),
            "max_drawdown_30d": round(drawdown_30, 4),
            "volume_ratio_5v20": round(volume_strength, 4),
            "range_pressure_20d": round(range_pressure, 4),
            "range_support_20d": round(range_support, 4),
            "history_rows": len(usable),
        },
    }


def build_predictions(rows: list[dict[str, Any]], limit: int = SYMBOL_LIMIT) -> list[dict[str, Any]]:
    predictions: list[dict[str, Any]] = []
    for index, (code, history) in enumerate(group_by_code(rows).items()):
        prediction = build_prediction(code, history, index)
        if prediction:
            predictions.append(prediction)
    predictions = sorted(predictions, key=lambda row: (-number(row["score"]), row["code"]))[:limit]
    for rank, row in enumerate(predictions, start=1):
        row["rank"] = rank
    return predictions


def delete_existing_predictions(client: SupabaseRest, prediction_date: str) -> None:
    client.request(
        "DELETE",
        "stock_model_predictions"
        f"?prediction_date=eq.{quote(prediction_date)}"
        f"&model_name=eq.{quote(MODEL_NAME)}"
        f"&model_version=eq.{quote(MODEL_VERSION)}"
        f"&feature_set=eq.{quote(FEATURE_SET)}",
        prefer="return=minimal",
    )


def insert_predictions(client: SupabaseRest, predictions: list[dict[str, Any]], dry_run: bool = False) -> dict[str, Any]:
    if not predictions:
        return {"prediction_date": "", "inserted": 0, "top": []}
    prediction_date = str(predictions[0]["prediction_date"])
    if dry_run:
        return {"prediction_date": prediction_date, "inserted": 0, "top": predictions[:5]}
    delete_existing_predictions(client, prediction_date)
    rows = client.request("POST", "stock_model_predictions", predictions, prefer="return=representation") or []
    return {"prediction_date": prediction_date, "inserted": len(rows), "top": rows[:5]}


def run(dry_run: bool = False) -> dict[str, Any]:
    client = get_client()
    end = latest_history_date(client, prediction_end_date())
    rows = load_history_rows(client, end)
    predictions = build_predictions(rows)
    return insert_predictions(client, predictions, dry_run)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(dry_run=args.dry_run), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
