"""Train the P4 real-model prediction stack for the virtual model account."""

from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from catboost import CatBoostRegressor
from lightgbm import LGBMRegressor
from scipy.stats import spearmanr
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from xgboost import XGBRegressor

from sync_stock_data import SupabaseRest, env_value, read_env_file


RANDOM_STATE = 42
LABEL_HORIZON_DAYS = 5
DEFAULT_MODEL_STORE = Path(__file__).resolve().parent / "model_store"
A_SHARE_EXTERNAL_FEATURES = [
    "limit_board_count",
    "first_limit_time_rank",
    "yesterday_limit_up_stocks_avg_ret",
    "main_net_inflow_ratio",
    "big_order_buy_ratio",
    "north_bound_holding_change",
    "market_limit_up_count",
    "market_limit_break_rate",
    "market_advance_decline_ratio",
    "turnover_rate_5d",
]
FEATURE_COLUMNS = [
    "ret_1d",
    "ret_5d",
    "ret_10d",
    "ret_20d",
    "ret_60d",
    "vol_5d",
    "vol_10d",
    "vol_20d",
    "max_drawdown_20d",
    "max_drawdown_60d",
    "volume_ratio_5v20",
    "volume_ratio_10v60",
    "close_position",
    "upper_shadow_ratio",
    "lower_shadow_ratio",
    "close_over_ma5",
    "close_over_ma10",
    "close_over_ma20",
    "close_over_ma60",
    "ma5_over_ma10",
    "ma10_over_ma20",
    "ma20_slope_5d",
    "ma60_slope_10d",
    "rsi_6",
    "rsi_14",
    "rsi_28",
    "macd_hist",
    "macd_signal_line_diff",
    *A_SHARE_EXTERNAL_FEATURES,
    "pressure_room_pct",
    "support_room_pct",
    "pressure_strength",
    "support_strength",
    "box_range_pct",
]


@dataclass(frozen=True)
class TrainedModelBundle:
    lgb: Any
    cat: Any
    xgb: Any
    meta: Any
    feature_columns: list[str]
    metrics: dict[str, dict[str, float]]
    feature_importance: list[dict[str, Any]]
    split_dates: dict[str, str]
    random_state: int = RANDOM_STATE


def get_client() -> SupabaseRest:
    env = read_env_file()
    return SupabaseRest(env_value("VITE_SUPABASE_URL", env), env_value("SUPABASE_SERVICE_ROLE_KEY", env))


def number(value: Any, fallback: float = np.nan) -> float:
    try:
        if value is None or value == "":
            return fallback
        return float(value)
    except (TypeError, ValueError):
        return fallback


def safe_divide(numerator: float, denominator: float, fallback: float = np.nan) -> float:
    return numerator / denominator if denominator and not math.isnan(denominator) else fallback


def load_training_data(client: SupabaseRest, lookback_days: int = 500, page_size: int = 1000) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = client.request(
            "GET",
            "stock_daily_history?adjustment=eq.qfq"
            "&select=code,trade_date,open,high,low,close,volume"
            "&order=code.asc,trade_date.asc"
            f"&limit={page_size}"
            f"&offset={offset}",
        ) or []
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    if lookback_days <= 0 or not rows:
        return rows
    ordered_dates = sorted({str(row.get("trade_date")) for row in rows if row.get("trade_date")})
    keep_dates = set(ordered_dates[-lookback_days:])
    return [row for row in rows if str(row.get("trade_date")) in keep_dates]


def group_by_code(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        code = str(row.get("code", "")).zfill(6)
        if code:
            grouped.setdefault(code, []).append(row)
    return grouped


def fetch_a_share_feature_map() -> dict[tuple[str, str], dict[str, float]]:
    """Best-effort current A-share sentiment/fund map; failures intentionally become NaN."""
    try:
        import akshare as ak  # type: ignore

        today = date.today().strftime("%Y%m%d")
        limit_rows = ak.stock_zt_pool_em(date=today)
        feature_map: dict[tuple[str, str], dict[str, float]] = {}
        if hasattr(limit_rows, "iterrows"):
            limit_count = len(limit_rows)
            for rank, row in limit_rows.reset_index(drop=True).iterrows():
                code = str(row.get("代码", "")).zfill(6)
                if not code:
                    continue
                feature_map[(code, date.today().isoformat())] = {
                    "limit_board_count": number(row.get("连板数")),
                    "first_limit_time_rank": float(rank + 1),
                    "market_limit_up_count": float(limit_count),
                }
        return feature_map
    except Exception:
        return {}


def pct_change(closes: list[float], days: int) -> float:
    if len(closes) <= days:
        return np.nan
    base = closes[-days - 1]
    latest = closes[-1]
    return safe_divide(latest - base, base)


def rolling_volatility(closes: list[float], days: int) -> float:
    if len(closes) <= 2:
        return np.nan
    window = np.array(closes[-min(days + 1, len(closes)):], dtype=float)
    returns = np.diff(window) / window[:-1]
    return float(np.nanstd(returns)) if len(returns) else np.nan


def rolling_max_drawdown(closes: list[float], days: int) -> float:
    window = np.array(closes[-min(days, len(closes)):], dtype=float)
    if len(window) == 0:
        return np.nan
    peaks = np.maximum.accumulate(window)
    drawdowns = np.where(peaks > 0, (peaks - window) / peaks, np.nan)
    return float(np.nanmax(drawdowns)) if len(drawdowns) else np.nan


def moving_average(values: list[float], days: int) -> float:
    if len(values) < days:
        return np.nan
    return float(np.nanmean(values[-days:]))


def rsi(closes: list[float], days: int) -> float:
    if len(closes) <= days:
        return np.nan
    diff = np.diff(np.array(closes[-(days + 1):], dtype=float))
    gains = np.where(diff > 0, diff, 0.0)
    losses = np.where(diff < 0, -diff, 0.0)
    avg_loss = np.nanmean(losses)
    if avg_loss == 0:
        return 100.0
    rs = np.nanmean(gains) / avg_loss
    return float(100 - 100 / (1 + rs))


def ema(values: list[float], span: int) -> float:
    if not values:
        return np.nan
    return float(pd.Series(values, dtype="float64").ewm(span=span, adjust=False).mean().iloc[-1])


def extract_feature_dict(
    code: str,
    history: list[dict[str, Any]],
    external_feature_map: dict[tuple[str, str], dict[str, float]] | None = None,
) -> dict[str, Any]:
    usable = sorted(history, key=lambda row: str(row.get("trade_date", "")))
    closes = [number(row.get("close")) for row in usable]
    highs = [number(row.get("high")) for row in usable]
    lows = [number(row.get("low")) for row in usable]
    opens = [number(row.get("open")) for row in usable]
    volumes = [number(row.get("volume")) for row in usable]
    latest = usable[-1]
    latest_close = closes[-1]
    latest_high = highs[-1]
    latest_low = lows[-1]
    latest_open = opens[-1]
    trade_date = str(latest.get("trade_date"))
    ma5 = moving_average(closes, 5)
    ma10 = moving_average(closes, 10)
    ma20 = moving_average(closes, 20)
    ma60 = moving_average(closes, 60)
    high20 = float(np.nanmax(highs[-20:])) if len(highs) >= 20 else np.nan
    low20 = float(np.nanmin(lows[-20:])) if len(lows) >= 20 else np.nan
    high60 = float(np.nanmax(highs[-60:])) if len(highs) >= 60 else np.nan
    low60 = float(np.nanmin(lows[-60:])) if len(lows) >= 60 else np.nan
    volume5 = float(np.nanmean(volumes[-5:])) if len(volumes) >= 5 else np.nan
    volume10 = float(np.nanmean(volumes[-10:])) if len(volumes) >= 10 else np.nan
    volume20 = float(np.nanmean(volumes[-20:])) if len(volumes) >= 20 else np.nan
    volume60 = float(np.nanmean(volumes[-60:])) if len(volumes) >= 60 else np.nan
    macd_fast = ema(closes, 12)
    macd_slow = ema(closes, 26)
    macd_line = macd_fast - macd_slow
    signal_line = float(pd.Series(closes, dtype="float64").ewm(span=12, adjust=False).mean().sub(
        pd.Series(closes, dtype="float64").ewm(span=26, adjust=False).mean()
    ).ewm(span=9, adjust=False).mean().iloc[-1]) if closes else np.nan

    features: dict[str, Any] = {
        "code": str(code).zfill(6),
        "name": str(latest.get("name") or ""),
        "trade_date": trade_date,
        "close_price": latest_close,
        "ret_1d": pct_change(closes, 1),
        "ret_5d": pct_change(closes, 5),
        "ret_10d": pct_change(closes, 10),
        "ret_20d": pct_change(closes, 20),
        "ret_60d": pct_change(closes, 60),
        "vol_5d": rolling_volatility(closes, 5),
        "vol_10d": rolling_volatility(closes, 10),
        "vol_20d": rolling_volatility(closes, 20),
        "max_drawdown_20d": rolling_max_drawdown(closes, 20),
        "max_drawdown_60d": rolling_max_drawdown(closes, 60),
        "volume_ratio_5v20": safe_divide(volume5, volume20),
        "volume_ratio_10v60": safe_divide(volume10, volume60),
        "close_position": safe_divide(latest_close - latest_low, latest_high - latest_low),
        "upper_shadow_ratio": safe_divide(latest_high - max(latest_open, latest_close), latest_close),
        "lower_shadow_ratio": safe_divide(min(latest_open, latest_close) - latest_low, latest_close),
        "close_over_ma5": safe_divide(latest_close, ma5) - 1,
        "close_over_ma10": safe_divide(latest_close, ma10) - 1,
        "close_over_ma20": safe_divide(latest_close, ma20) - 1,
        "close_over_ma60": safe_divide(latest_close, ma60) - 1,
        "ma5_over_ma10": safe_divide(ma5, ma10) - 1,
        "ma10_over_ma20": safe_divide(ma10, ma20) - 1,
        "ma20_slope_5d": safe_divide(ma20 - moving_average(closes[:-5], 20), moving_average(closes[:-5], 20)),
        "ma60_slope_10d": safe_divide(ma60 - moving_average(closes[:-10], 60), moving_average(closes[:-10], 60)),
        "rsi_6": rsi(closes, 6),
        "rsi_14": rsi(closes, 14),
        "rsi_28": rsi(closes, 28),
        "macd_hist": macd_line - signal_line,
        "macd_signal_line_diff": safe_divide(macd_line - signal_line, abs(signal_line), 0.0),
        "pressure_room_pct": safe_divide(high20 - latest_close, latest_close),
        "support_room_pct": safe_divide(latest_close - low20, latest_close),
        "pressure_strength": safe_divide(high60 - latest_close, high60 - low60),
        "support_strength": safe_divide(latest_close - low60, high60 - low60),
        "box_range_pct": safe_divide(high60 - low60, latest_close),
    }
    for name in A_SHARE_EXTERNAL_FEATURES:
        features[name] = np.nan
    if external_feature_map:
        features.update(external_feature_map.get((str(code).zfill(6), trade_date), {}))
    return features


def extract_features_for_code(
    code: str,
    history: list[dict[str, Any]],
    external_feature_map: dict[tuple[str, str], dict[str, float]] | None = None,
) -> pd.DataFrame:
    if not history:
        return pd.DataFrame(columns=FEATURE_COLUMNS)
    features = extract_feature_dict(code, history, external_feature_map)
    return pd.DataFrame([{column: features.get(column, np.nan) for column in FEATURE_COLUMNS}])


def build_features(grouped_history: dict[str, list[dict[str, Any]]]) -> pd.DataFrame:
    external_feature_map = fetch_a_share_feature_map()
    rows: list[dict[str, Any]] = []
    for code, history in grouped_history.items():
        usable = sorted(history, key=lambda row: str(row.get("trade_date", "")))
        for index in range(60, len(usable) - LABEL_HORIZON_DAYS):
            prefix = usable[: index + 1]
            feature_row = extract_feature_dict(code, prefix, external_feature_map)
            close_now = number(usable[index].get("close"))
            close_future = number(usable[index + LABEL_HORIZON_DAYS].get("close"))
            feature_row["label"] = safe_divide(close_future - close_now, close_now)
            feature_row["label_binary"] = 1 if feature_row["label"] > 0.03 else 0
            rows.append(feature_row)
    return pd.DataFrame(rows)


def build_labels(grouped_history: dict[str, list[dict[str, Any]]]) -> pd.DataFrame:
    return build_features(grouped_history)[["code", "trade_date", "label", "label_binary"]]


def split_by_date(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, str]]:
    ordered = frame.sort_values("trade_date").copy()
    train = ordered[(ordered["trade_date"] >= "2024-01-01") & (ordered["trade_date"] <= "2026-02-28")]
    validation = ordered[(ordered["trade_date"] >= "2026-03-01") & (ordered["trade_date"] <= "2026-04-30")]
    test = ordered[ordered["trade_date"] >= "2026-05-01"]
    if train.empty or validation.empty or test.empty:
        dates = sorted(ordered["trade_date"].astype(str).unique())
        train_end = max(1, int(len(dates) * 0.6))
        validation_end = max(train_end + 1, int(len(dates) * 0.8))
        train_dates = set(dates[:train_end])
        validation_dates = set(dates[train_end:validation_end])
        test_dates = set(dates[validation_end:])
        train = ordered[ordered["trade_date"].isin(train_dates)]
        validation = ordered[ordered["trade_date"].isin(validation_dates)]
        test = ordered[ordered["trade_date"].isin(test_dates)]
    split_dates = {
        "train_start_date": str(train["trade_date"].min()),
        "train_end_date": str(train["trade_date"].max()),
        "validation_start_date": str(validation["trade_date"].min()),
        "validation_end_date": str(validation["trade_date"].max()),
        "test_start_date": str(test["trade_date"].min()),
        "test_end_date": str(test["trade_date"].max()),
    }
    return train, validation, test, split_dates


def rank_ic(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    if len(y_true) < 3 or np.nanstd(y_pred) == 0:
        return 0.0
    value = spearmanr(y_true, y_pred, nan_policy="omit").correlation
    return float(value) if not np.isnan(value) else 0.0


def icir_by_date(frame: pd.DataFrame, predictions: np.ndarray) -> float:
    values: list[float] = []
    scored = frame[["trade_date", "label"]].copy()
    scored["prediction"] = predictions
    for _, group in scored.groupby("trade_date"):
        if len(group) >= 3:
            values.append(rank_ic(group["label"].to_numpy(), group["prediction"].to_numpy()))
    if not values or np.nanstd(values) == 0:
        return 0.0
    return float(np.nanmean(values) / np.nanstd(values))


def train_stacking_model(features_df: pd.DataFrame) -> TrainedModelBundle:
    frame = features_df.dropna(subset=["label", "trade_date"]).copy()
    train, validation, test, split_dates = split_by_date(frame)
    if train.empty or validation.empty or test.empty:
        raise ValueError("not enough dated rows to build train/validation/test splits")
    feature_columns = [column for column in FEATURE_COLUMNS if column in frame.columns]
    x_train = train[feature_columns]
    y_train = train["label"]
    x_validation = validation[feature_columns]
    y_validation = validation["label"]
    x_test = test[feature_columns]
    y_test = test["label"].to_numpy()

    n_estimators = int(os.environ.get("STOCK_MODEL_N_ESTIMATORS", "500"))
    lgb = LGBMRegressor(
        num_leaves=63,
        min_data_in_leaf=50,
        feature_fraction=0.8,
        bagging_fraction=0.8,
        bagging_freq=1,
        n_estimators=n_estimators,
        random_state=RANDOM_STATE,
        verbosity=-1,
    )
    cat = CatBoostRegressor(
        depth=6,
        learning_rate=0.03,
        iterations=n_estimators,
        random_seed=RANDOM_STATE,
        verbose=0,
        allow_writing_files=False,
    )
    xgb = XGBRegressor(
        max_depth=5,
        subsample=0.8,
        colsample_bytree=0.8,
        n_estimators=n_estimators,
        random_state=RANDOM_STATE,
        objective="reg:squarederror",
    )
    lgb.fit(x_train, y_train)
    cat.fit(x_train, y_train)
    xgb.fit(x_train, y_train)
    validation_stack = np.column_stack([
        lgb.predict(x_validation),
        cat.predict(x_validation),
        xgb.predict(x_validation),
    ])
    meta = make_pipeline(StandardScaler(), Ridge(alpha=1.0))
    meta.fit(validation_stack, y_validation)

    test_lgb = lgb.predict(x_test)
    test_cat = cat.predict(x_test)
    test_xgb = xgb.predict(x_test)
    test_stack = np.column_stack([test_lgb, test_cat, test_xgb])
    test_meta = meta.predict(test_stack)
    val_meta = meta.predict(validation_stack)
    metrics = {
        "lgb": {"rank_ic": rank_ic(y_test, test_lgb), "icir": icir_by_date(test, test_lgb)},
        "cat": {"rank_ic": rank_ic(y_test, test_cat), "icir": icir_by_date(test, test_cat)},
        "xgb": {"rank_ic": rank_ic(y_test, test_xgb), "icir": icir_by_date(test, test_xgb)},
        "stacking": {"rank_ic": rank_ic(y_test, test_meta), "icir": icir_by_date(test, test_meta)},
        "validation": {"rank_ic": rank_ic(y_validation.to_numpy(), val_meta), "icir": icir_by_date(validation, val_meta)},
    }
    importances = getattr(lgb, "feature_importances_", np.zeros(len(feature_columns)))
    feature_importance = sorted(
        [
            {"feature": column, "importance": float(importances[index])}
            for index, column in enumerate(feature_columns)
        ],
        key=lambda row: row["importance"],
        reverse=True,
    )[:20]
    return TrainedModelBundle(
        lgb=lgb,
        cat=cat,
        xgb=xgb,
        meta=meta,
        feature_columns=feature_columns,
        metrics=metrics,
        feature_importance=feature_importance,
        split_dates=split_dates,
    )


def save_model_bundle(bundle: TrainedModelBundle, model_store_dir: Path = DEFAULT_MODEL_STORE, version: str | None = None) -> dict[str, Any]:
    model_store_dir.mkdir(parents=True, exist_ok=True)
    model_version = version or date.today().strftime("%Y%m%d")
    paths = {
        "lgb": model_store_dir / f"model_lgb_{model_version}.pkl",
        "cat": model_store_dir / f"model_cat_{model_version}.pkl",
        "xgb": model_store_dir / f"model_xgb_{model_version}.pkl",
        "meta": model_store_dir / f"model_meta_{model_version}.pkl",
        "config": model_store_dir / f"model_config_{model_version}.json",
    }
    joblib.dump(bundle.lgb, paths["lgb"])
    joblib.dump(bundle.cat, paths["cat"])
    joblib.dump(bundle.xgb, paths["xgb"])
    joblib.dump(bundle.meta, paths["meta"])
    config = {
        "model_version": model_version,
        "random_state": bundle.random_state,
        "feature_columns": bundle.feature_columns,
        "metrics": bundle.metrics,
        "feature_importance": bundle.feature_importance,
        "split_dates": bundle.split_dates,
        "model_files": {key: path.name for key, path in paths.items() if key != "config"},
    }
    paths["config"].write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return config


def synthetic_training_frame() -> pd.DataFrame:
    rng = np.random.default_rng(RANDOM_STATE)
    rows: list[dict[str, Any]] = []
    dates = pd.date_range("2024-01-01", periods=260, freq="B")
    for code_index in range(8):
        code = f"{code_index + 1:06d}"
        latent = rng.normal(0, 1, len(dates))
        for index, trade_date in enumerate(dates):
            row = {column: float(rng.normal(0, 0.5)) for column in FEATURE_COLUMNS}
            signal_linear = 0.035 * row["ret_5d"] - 0.025 * row["vol_20d"]
            signal_tree = 0.045 if row["rsi_14"] > 0 and row["volume_ratio_5v20"] > 0 else -0.025
            signal_boost = 0.03 * row["macd_hist"] * row["support_strength"] + 0.015 * latent[index]
            row.update({
                "code": code,
                "trade_date": trade_date.date().isoformat(),
                "close_price": 10 + code_index + index * 0.01,
                "label": signal_linear + signal_tree + signal_boost + float(rng.normal(0, 0.01)),
                "label_binary": 1,
            })
            rows.append(row)
    return pd.DataFrame(rows)


def train_from_supabase(report_only: bool = False) -> dict[str, Any]:
    client = get_client()
    rows = load_training_data(client)
    frame = build_features(group_by_code(rows))
    bundle = train_stacking_model(frame)
    config = {
        "random_state": bundle.random_state,
        "metrics": bundle.metrics,
        "feature_importance": bundle.feature_importance,
        "split_dates": bundle.split_dates,
    }
    if not report_only:
        config = save_model_bundle(bundle, DEFAULT_MODEL_STORE)
    return config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()
    config = train_from_supabase(report_only=args.report)
    print(json.dumps(config, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
