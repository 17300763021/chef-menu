"""Classify the current A-share market regime for simulation risk controls."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pandas as pd

from sync_stock_data import SupabaseRest, env_value, read_env_file


ROOT = Path(__file__).resolve().parents[1]
STOCK_ENGINE = ROOT / "scripts" / "stock_engine"
if str(STOCK_ENGINE) not in sys.path:
    sys.path.insert(0, str(STOCK_ENGINE))

from a_stock_trade_common_v7 import _eastmoney_spot_direct, get_hist  # noqa: E402


POSITION_CAP_BY_REGIME = {
    "强牛市": 80,
    "弱牛市": 60,
    "震荡市": 40,
    "熊市": 20,
    "防御": 10,
}

REGIME_NOTES = {
    "强牛市": "趋势和市场宽度共振，允许较高模拟仓位至80%",
    "弱牛市": "指数偏多但强度未满，模拟仓位上限60%",
    "震荡市": "大盘震荡，方向不明，降低模拟仓位至40%",
    "熊市": "指数弱于中期均线，模拟仓位上限20%，谨慎管理风险",
    "防御": "市场进入防御状态，模拟仓位上限10%，停止新增买入优先保护本金",
}


def get_client() -> SupabaseRest:
    env = read_env_file()
    url = env_value("VITE_SUPABASE_URL", env)
    key = env_value("SUPABASE_SERVICE_ROLE_KEY", env)
    return SupabaseRest(url, key, max_attempts=3, retry_delay_seconds=2)


def safe_float(value: Any, fallback: float = 0) -> float:
    try:
        if value is None or value == "":
            return fallback
        number = float(value)
        if pd.isna(number):
            return fallback
        return number
    except (TypeError, ValueError):
        return fallback


def safe_int(value: Any, fallback: int = 0) -> int:
    return int(round(safe_float(value, fallback)))


def retry_call(func, attempts: int = 3, delay_seconds: float = 2):
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as exc:  # pragma: no cover - exercised through live data paths
            last_error = exc
            if attempt < attempts:
                time.sleep(delay_seconds)
    if last_error:
        raise last_error
    raise RuntimeError("retry_call failed without an exception")


def csi300_metrics_from_history(history: pd.DataFrame) -> dict[str, float]:
    if history is None or history.empty or "close" not in history:
        raise ValueError("CSI300 history is empty")
    close = pd.to_numeric(history["close"], errors="coerce").dropna()
    if len(close) < 20:
        raise ValueError("CSI300 history needs at least 20 closes")
    return {
        "csi300_close": safe_float(close.iloc[-1]),
        "csi300_ma20": safe_float(close.tail(20).mean()),
        "csi300_ma60": safe_float(close.tail(60).mean()) if len(close) >= 60 else safe_float(close.mean()),
    }


def fetch_csi300_history_from_supabase(client: SupabaseRest) -> pd.DataFrame:
    codes = ("000300", "sh000300")
    for code in codes:
        rows = client.request(
            "GET",
            f"stock_daily_history?code=eq.{quote(code)}&select=trade_date,close&order=trade_date.desc&limit=90",
        ) or []
        if rows:
            frame = pd.DataFrame(rows)
            frame = frame.rename(columns={"trade_date": "date"})
            return frame.sort_values("date").reset_index(drop=True)
    return pd.DataFrame()


def fetch_csi300_history(client: SupabaseRest) -> pd.DataFrame:
    try:
        frame = retry_call(lambda: fetch_csi300_history_from_supabase(client))
        if not frame.empty:
            return frame
    except Exception as exc:
        print(f"[MarketRegime] CSI300 Supabase cache unavailable: {exc}", flush=True)

    try:
        import akshare as ak

        frame = retry_call(lambda: ak.stock_zh_index_daily(symbol="sh000300"))
        if frame is not None and not frame.empty:
            print("[MarketRegime] CSI300 fallback source: AkShare sh000300", flush=True)
            return frame.tail(120).reset_index(drop=True)
    except Exception as exc:
        print(f"[MarketRegime] CSI300 AkShare fallback unavailable: {exc}", flush=True)

    frame, source = retry_call(lambda: get_hist("000300", days=120))
    print(f"[MarketRegime] CSI300 fallback source: {source}", flush=True)
    return frame


def market_breadth_from_spot(spot: pd.DataFrame) -> dict[str, float]:
    if spot is None or spot.empty:
        raise ValueError("East Money spot data is empty")

    amount = pd.to_numeric(spot.get("amount"), errors="coerce").fillna(0)
    pct = pd.to_numeric(spot.get("pct"), errors="coerce")
    high = pd.to_numeric(spot.get("high"), errors="coerce")
    pre_close = pd.to_numeric(spot.get("pre_close"), errors="coerce")
    price = pd.to_numeric(spot.get("price"), errors="coerce")

    limit_up = pct >= 9.8
    limit_down = pct <= -9.8
    rising = pct > 0
    falling = pct < 0

    touched_limit_up = (pre_close > 0) & (high >= pre_close * 1.098)
    touched_count = int(touched_limit_up.sum())
    sealed_count = int(limit_up.sum())
    broken_count = max(0, touched_count - sealed_count)
    break_rate = broken_count / touched_count * 100 if touched_count > 0 else 50
    falling_count = int(falling.sum())

    return {
        "market_turnover_yi": round(safe_float(amount.sum()) / 100000000, 2),
        "limit_up_count": sealed_count,
        "limit_down_count": int(limit_down.sum()),
        "break_rate_pct": round(break_rate, 2),
        "advance_decline_ratio": round(int(rising.sum()) / falling_count, 4) if falling_count > 0 else float(int(rising.sum())),
        "valid_price_count": int((price > 0).sum()),
        "breadth_source": "eastmoney_spot",
    }


def market_breadth_from_history_rows(rows: list[dict[str, Any]]) -> dict[str, float]:
    if not rows:
        raise ValueError("stock_daily_history cache has no market breadth rows")
    frame = pd.DataFrame(rows)
    amount = pd.to_numeric(frame.get("amount"), errors="coerce").fillna(0)
    change_rate = pd.to_numeric(frame.get("change_rate"), errors="coerce")
    limit_up = change_rate >= 9.8
    limit_down = change_rate <= -9.8
    rising = change_rate > 0
    falling = change_rate < 0
    falling_count = int(falling.sum())
    return {
        "market_turnover_yi": round(safe_float(amount.sum()) / 100000000, 2),
        "limit_up_count": int(limit_up.sum()),
        "limit_down_count": int(limit_down.sum()),
        "break_rate_pct": 50,
        "advance_decline_ratio": round(int(rising.sum()) / falling_count, 4) if falling_count > 0 else float(int(rising.sum())),
        "valid_price_count": int(change_rate.notna().sum()),
        "breadth_source": "stock_daily_history_cache",
    }


def fetch_market_breadth_from_history(client: SupabaseRest) -> dict[str, float]:
    latest = client.request(
        "GET",
        "stock_daily_history?select=trade_date&order=trade_date.desc&limit=1",
    ) or []
    if not latest:
        raise ValueError("stock_daily_history has no rows")
    trade_date = str(latest[0].get("trade_date") or "")
    rows = client.request(
        "GET",
        f"stock_daily_history?trade_date=eq.{quote(trade_date)}&select=amount,change_rate&limit=5000",
    ) or []
    breadth = market_breadth_from_history_rows(rows)
    breadth["breadth_trade_date"] = trade_date
    return breadth


def fetch_market_breadth(client: SupabaseRest) -> dict[str, float]:
    try:
        spot = retry_call(_eastmoney_spot_direct)
        return market_breadth_from_spot(spot)
    except Exception as exc:
        print(f"[MarketRegime] East Money breadth unavailable: {exc}", flush=True)
        return fetch_market_breadth_from_history(client)


def classify_from_metrics(
    *,
    csi300_close: float,
    csi300_ma20: float,
    csi300_ma60: float,
    market_turnover_yi: float,
    limit_up_count: int,
    limit_down_count: int,
    break_rate_pct: float,
    advance_decline_ratio: float,
    valid_price_count: int = 0,
    breadth_source: str = "",
    breadth_trade_date: str = "",
) -> dict[str, Any]:
    if csi300_close > csi300_ma20 and csi300_close > csi300_ma60 and market_turnover_yi > 12000 and limit_up_count > 80 and break_rate_pct < 25:
        regime = "强牛市"
    elif csi300_close > csi300_ma20 and market_turnover_yi > 8000:
        regime = "弱牛市"
    elif csi300_close < csi300_ma20 and csi300_close < csi300_ma60 and (market_turnover_yi < 6000 or limit_down_count > 50):
        regime = "防御"
    elif csi300_close < csi300_ma60 and market_turnover_yi < 10000:
        regime = "熊市"
    elif csi300_ma20 > 0 and csi300_ma20 * 0.95 <= csi300_close <= csi300_ma20 * 1.05 and market_turnover_yi > 6000:
        regime = "震荡市"
    else:
        regime = "震荡市"

    return {
        "regime": regime,
        "csi300_close": round(safe_float(csi300_close), 4),
        "csi300_ma20": round(safe_float(csi300_ma20), 4),
        "csi300_ma60": round(safe_float(csi300_ma60), 4),
        "market_turnover_yi": round(safe_float(market_turnover_yi), 2),
        "limit_up_count": safe_int(limit_up_count),
        "limit_down_count": safe_int(limit_down_count),
        "break_rate_pct": round(safe_float(break_rate_pct, 50), 2),
        "advance_decline_ratio": round(safe_float(advance_decline_ratio), 4),
        "position_cap_pct": POSITION_CAP_BY_REGIME[regime],
        "regime_note": REGIME_NOTES[regime],
        "breadth_source": breadth_source,
        "breadth_trade_date": breadth_trade_date,
    }


def latest_saved_regime(client: SupabaseRest) -> dict[str, Any]:
    rows = client.request(
        "GET",
        "stock_market_regime?select=*&order=regime_date.desc&limit=1",
    ) or []
    if not rows:
        return {
            "regime": "震荡市",
            "position_cap_pct": POSITION_CAP_BY_REGIME["震荡市"],
            "regime_note": "无法判断（非交易日），且没有历史市场状态，使用震荡市默认值",
        }
    row = rows[0]
    details = row.get("details") or {}
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except json.JSONDecodeError:
            details = {}
    return {
        "regime_date": row.get("regime_date"),
        "regime": row.get("regime", "震荡市"),
        "csi300_close": safe_float(row.get("csi300_close")),
        "market_turnover_yi": safe_float(row.get("market_turnover_yi")),
        "limit_up_count": safe_int(row.get("limit_up_count")),
        "limit_down_count": safe_int(row.get("limit_down_count")),
        "break_rate_pct": safe_float(row.get("break_rate_pct"), 50),
        "advance_decline_ratio": safe_float(row.get("advance_decline_ratio")),
        "position_cap_pct": safe_float(row.get("position_cap_pct"), POSITION_CAP_BY_REGIME["震荡市"]),
        "regime_note": details.get("regime_note") or "无法判断（非交易日），返回最近交易日市场状态",
        "details": details,
    }


def upsert_market_regime(client: SupabaseRest, regime_info: dict[str, Any]) -> None:
    payload = {
        "regime_date": regime_info.get("regime_date") or date.today().isoformat(),
        "regime": regime_info.get("regime", "震荡市"),
        "csi300_close": safe_float(regime_info.get("csi300_close")),
        "market_turnover_yi": safe_float(regime_info.get("market_turnover_yi")),
        "limit_up_count": safe_int(regime_info.get("limit_up_count")),
        "limit_down_count": safe_int(regime_info.get("limit_down_count")),
        "break_rate_pct": safe_float(regime_info.get("break_rate_pct"), 50),
        "advance_decline_ratio": safe_float(regime_info.get("advance_decline_ratio")),
        "position_cap_pct": safe_float(regime_info.get("position_cap_pct"), POSITION_CAP_BY_REGIME["震荡市"]),
        "details": {
            "csi300_ma20": safe_float(regime_info.get("csi300_ma20")),
            "csi300_ma60": safe_float(regime_info.get("csi300_ma60")),
            "regime_note": regime_info.get("regime_note", ""),
            "breadth_source": regime_info.get("breadth_source", ""),
            "breadth_trade_date": regime_info.get("breadth_trade_date", ""),
        },
    }
    client.request(
        "POST",
        f"stock_market_regime?on_conflict={quote('regime_date')}",
        [payload],
        prefer="resolution=merge-duplicates,return=minimal",
    )


def classify_market_regime(client: SupabaseRest) -> dict[str, Any]:
    try:
        history = fetch_csi300_history(client)
        csi_metrics = csi300_metrics_from_history(history)
        breadth = fetch_market_breadth(client)
        if safe_int(breadth.get("valid_price_count")) == 0:
            raise ValueError("market breadth has no valid prices")
        result = classify_from_metrics(**csi_metrics, **breadth)
        result["regime_date"] = date.today().isoformat()
        upsert_market_regime(client, result)
        return result
    except Exception as exc:
        print(f"[MarketRegime] 无法判断（非交易日）：{exc}", flush=True)
        return latest_saved_regime(client)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    client = get_client()
    result = classify_market_regime(client)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
