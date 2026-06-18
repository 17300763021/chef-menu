"""Replay the current stock-selection rules without using future data."""

from __future__ import annotations

import argparse
import json
import math
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "scripts" / "stock_engine"
sys.path.insert(0, str(ENGINE_DIR))

from a_stock_trade_common_v7 import get_hist, get_spot_all, score_stock


STANDARD_SIGNALS = {
    "回踩20日线低吸买点",
    "放量突破买点",
    "站回20日线修复买点",
}
VARIANTS = {
    "current_proxy": {
        "min_score": 76,
        "allow_trend": False,
        "block_weak_market": True,
        "restore_risks": [],
        "max_risks": 1,
        "blocked_risks": ["短线涨幅过大，追高风险", "上影线较长，上方抛压大", "疑似放量假突破"],
    },
    "allow_two_risks": {
        "min_score": 76,
        "allow_trend": False,
        "block_weak_market": True,
        "restore_risks": [],
        "max_risks": 2,
        "blocked_risks": ["短线涨幅过大，追高风险", "疑似放量假突破"],
    },
    "remove_risk_count_gate": {
        "min_score": 76,
        "allow_trend": False,
        "block_weak_market": True,
        "restore_risks": [],
        "max_risks": None,
        "blocked_risks": ["短线涨幅过大，追高风险", "疑似放量假突破"],
    },
    "allow_weak_market": {
        "min_score": 76,
        "allow_trend": False,
        "block_weak_market": False,
        "restore_risks": [],
        "max_risks": 1,
        "blocked_risks": ["短线涨幅过大，追高风险", "上影线较长，上方抛压大", "疑似放量假突破"],
    },
    "balanced": {
        "min_score": 72,
        "allow_trend": True,
        "block_weak_market": False,
        "restore_risks": [],
        "max_risks": 2,
        "blocked_risks": ["短线涨幅过大，追高风险", "疑似放量假突破"],
    },
}
RISK_SCORE_PENALTIES = {
    "RSI过热": 10,
    "上影线较长，上方抛压大": 10,
}
DAILY_ALLOCATION_RATE = 0.08


def is_bad_name(name: str) -> bool:
    text = str(name).upper()
    return any(marker in text for marker in ["ST", "退", "N", "C"])


def number(value: Any, fallback: float = 0) -> float:
    try:
        value = float(value)
        return fallback if math.isnan(value) else value
    except (TypeError, ValueError):
        return fallback


def fetch_history(code: str, days: int) -> tuple[str, pd.DataFrame | None, str]:
    try:
        frame, source = get_hist(code, days=days)
        return code, frame, source
    except Exception as reason:
        return code, None, str(reason)


def select_universe(limit: int) -> list[dict[str, str]]:
    spot = get_spot_all()
    spot = spot[~spot["name"].astype(str).apply(is_bad_name)].copy()
    spot["amount"] = pd.to_numeric(spot.get("amount"), errors="coerce").fillna(0)
    spot["price"] = pd.to_numeric(spot.get("price"), errors="coerce").fillna(0)
    spot = spot[(spot["price"] >= 2) & (spot["amount"] >= 1e8)]
    spot = spot.sort_values("amount", ascending=False).head(limit)
    return [
        {"code": str(row["code"]).zfill(6), "name": str(row.get("name", ""))}
        for _, row in spot.iterrows()
    ]


def market_regimes(histories: dict[str, pd.DataFrame]) -> dict[str, str]:
    breadth: dict[str, list[str]] = {}
    for frame in histories.values():
        closes = frame["close"].astype(float)
        ma20 = closes.rolling(20).mean()
        previous_ma20 = ma20.shift(5)
        for index in range(24, len(frame)):
            state = "sideways"
            if closes.iloc[index] < ma20.iloc[index] and ma20.iloc[index] < previous_ma20.iloc[index]:
                state = "weak"
            elif closes.iloc[index] > ma20.iloc[index] and ma20.iloc[index] > previous_ma20.iloc[index]:
                state = "strong"
            day = str(frame.iloc[index]["date"].date())
            breadth.setdefault(day, []).append(state)
    result = {}
    for day, states in breadth.items():
        weak_rate = states.count("weak") / len(states)
        strong_rate = states.count("strong") / len(states)
        if weak_rate >= 0.55:
            result[day] = "weak"
        elif strong_rate >= 0.55:
            result[day] = "strong"
        else:
            result[day] = "sideways"
    return result


def future_metrics(frame: pd.DataFrame, index: int) -> dict[str, float]:
    close = float(frame.iloc[index]["close"])
    result: dict[str, float] = {}
    for horizon in [1, 3, 5, 10]:
        target = min(index + horizon, len(frame) - 1)
        result[f"return_{horizon}d"] = (float(frame.iloc[target]["close"]) - close) / close * 100
    future = frame.iloc[index + 1:min(index + 11, len(frame))]
    result["max_return_10d"] = (
        (float(future["high"].max()) - close) / close * 100 if not future.empty else 0
    )
    return result


def simulate_trade(frame: pd.DataFrame, index: int, stop_price: float) -> dict[str, Any] | None:
    if index + 1 >= len(frame):
        return None
    entry_index = index + 1
    entry_price = float(frame.iloc[entry_index]["open"])
    if entry_price <= 0:
        return None
    stop = stop_price if 0 < stop_price < entry_price else entry_price * 0.94
    target = entry_price * 1.12
    exit_index = min(entry_index + 10, len(frame) - 1)
    exit_price = float(frame.iloc[exit_index]["close"])
    exit_reason = "10d"
    for cursor in range(entry_index, exit_index + 1):
        row = frame.iloc[cursor]
        if float(row["low"]) <= stop:
            exit_index, exit_price, exit_reason = cursor, stop, "stop"
            break
        if float(row["high"]) >= target:
            exit_index, exit_price, exit_reason = cursor, target, "target"
            break
    return {
        "entry_date": str(frame.iloc[entry_index]["date"].date()),
        "exit_date": str(frame.iloc[exit_index]["date"].date()),
        "pnl_rate": (exit_price - entry_price) / entry_price * 100 - 0.2,
        "holding_days": exit_index - entry_index + 1,
        "exit_reason": exit_reason,
    }


def variant_selected(result: dict[str, Any], regime: str, config: dict[str, Any]) -> bool:
    adjusted_score = result["score"] + sum(
        RISK_SCORE_PENALTIES.get(risk, 0)
        for risk in config.get("restore_risks", [])
        if risk in result["risks"]
    )
    standard = result["signal"] in STANDARD_SIGNALS
    trend = (
        result["score"] >= 82
        and "股价在向上的20日线上方" in result["reasons"]
        and "MA5>MA10>MA20，多头排列" in result["reasons"]
        and "股价在60日线上方" in result["reasons"]
    )
    if adjusted_score < config["min_score"]:
        return False
    if not standard and not (config["allow_trend"] and trend):
        return False
    if config["block_weak_market"] and regime == "weak":
        return False
    max_risks = config.get("max_risks")
    if max_risks is not None and len(result["risks"]) > max_risks:
        return False
    if any(risk in result["risks"] for risk in config.get("blocked_risks", [])):
        return False
    return True


def replay_stock(
    item: dict[str, str],
    frame: pd.DataFrame,
    replay_days: int,
    regimes: dict[str, str],
) -> tuple[list[dict], list[dict]]:
    observations: list[dict] = []
    trades: list[dict] = []
    start_index = max(80, len(frame) - replay_days - 11)
    end_index = max(start_index, len(frame) - 11)
    for index in range(start_index, end_index):
        history = frame.iloc[: index + 1].copy()
        try:
            result = score_stock(history)
        except Exception:
            continue
        signal_date = str(frame.iloc[index]["date"].date())
        regime = regimes.get(signal_date, "unknown")
        future = future_metrics(frame, index)
        observation = {
            "date": signal_date,
            "code": item["code"],
            "name": item["name"],
            "score": result["score"],
            "signal": result["signal"],
            "market_regime": regime,
            "reasons": result["reasons"],
            "risks": result["risks"] + (["大盘弱势"] if regime == "weak" else []),
            **future,
        }
        observations.append(observation)
        for variant, config in VARIANTS.items():
            if not variant_selected(result, regime, config):
                continue
            trade = simulate_trade(frame, index, number(result.get("stop")))
            if trade:
                trades.append({
                    "variant": variant,
                    "code": item["code"],
                    "name": item["name"],
                    "signal_date": observation["date"],
                    "score": result["score"],
                    "signal": result["signal"],
                    "market_regime": regime,
                    **trade,
                })
    return observations, trades


def indicator_diagnostics(rows: list[dict], field: str) -> list[dict[str, Any]]:
    indicators = sorted({indicator for row in rows for indicator in row.get(field, [])})
    output = []
    for indicator in indicators:
        selected = [row for row in rows if indicator in row.get(field, [])]
        rejected = [row for row in rows if indicator not in row.get(field, [])]
        returns = [number(row.get("return_5d")) for row in selected]
        rejected_returns = [number(row.get("return_5d")) for row in rejected]
        avg_return = mean(returns) if returns else 0
        avg_without = mean(rejected_returns) if rejected_returns else 0
        runner_count = sum(number(row.get("max_return_10d")) >= 8 for row in selected)
        runner_rate = runner_count / len(selected) * 100 if selected else 0
        rejected_runner_rate = (
            sum(number(row.get("max_return_10d")) >= 8 for row in rejected) / len(rejected) * 100
            if rejected else 0
        )
        output.append({
            "type": field,
            "indicator": indicator,
            "sample_count": len(selected),
            "avg_return_5d": round(avg_return, 2),
            "avg_return_without_5d": round(avg_without, 2),
            "return_lift_5d": round(avg_return - avg_without, 2),
            "win_rate_5d": round(sum(value > 0 for value in returns) / len(returns) * 100, 2) if returns else 0,
            "missed_runner_count": runner_count,
            "runner_rate_10d": round(runner_rate, 2),
            "runner_rate_without_10d": round(rejected_runner_rate, 2),
            "runner_rate_lift_10d": round(runner_rate - rejected_runner_rate, 2),
            "avg_max_return_10d": round(mean(number(row.get("max_return_10d")) for row in selected), 2) if selected else 0,
        })
    return sorted(output, key=lambda item: (-item["sample_count"], item["indicator"]))


def compare_variants(trades: list[dict]) -> list[dict[str, Any]]:
    output = []
    for variant in VARIANTS:
        selected = [trade for trade in trades if trade["variant"] == variant]
        pnl = [number(trade.get("pnl_rate")) for trade in selected]
        daily_batches: dict[str, list[float]] = {}
        for trade in selected:
            daily_batches.setdefault(str(trade.get("signal_date")), []).append(number(trade.get("pnl_rate")))
        batch_returns = [
            mean(values) * DAILY_ALLOCATION_RATE
            for _, values in sorted(daily_batches.items())
        ]
        equity = peak = 100.0
        max_drawdown = 0.0
        for value in batch_returns:
            equity += value
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, (peak - equity) / peak * 100)
        wins = [value for value in pnl if value > 0]
        losses = [abs(value) for value in pnl if value < 0]
        output.append({
            "variant": variant,
            "trade_count": len(selected),
            "active_days": len(batch_returns),
            "batch_return_rate": round(equity - 100, 2),
            "avg_trade_return": round(mean(pnl), 2) if pnl else 0,
            "max_drawdown_rate": round(max_drawdown, 2),
            "win_rate": round(len(wins) / len(pnl) * 100, 2) if pnl else 0,
            "profit_loss_ratio": round((mean(wins) / mean(losses)), 2) if wins and losses else 0,
            "avg_holding_days": round(mean(number(item.get("holding_days")) for item in selected), 2) if selected else 0,
        })
    return sorted(output, key=lambda item: (item["batch_return_rate"], -item["max_drawdown_rate"]), reverse=True)


def write_reports(outdir: Path, observations: list[dict], trades: list[dict], failures: list[dict]) -> dict[str, Any]:
    outdir.mkdir(parents=True, exist_ok=True)
    reasons = indicator_diagnostics(observations, "reasons")
    risks = indicator_diagnostics(observations, "risks")
    variants = compare_variants(trades)
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "observation_count": len(observations),
        "trade_count": len(trades),
        "failed_symbols": failures,
        "variants": variants,
        "reason_diagnostics": reasons,
        "risk_diagnostics": risks,
    }
    (outdir / "strategy_replay_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    pd.DataFrame([{**row, "reasons": "；".join(row["reasons"]), "risks": "；".join(row["risks"])} for row in observations]).to_csv(
        outdir / "strategy_replay_observations.csv", index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(trades).to_csv(outdir / "strategy_replay_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(reasons + risks).to_csv(outdir / "strategy_indicator_diagnostics.csv", index=False, encoding="utf-8-sig")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--universe", type=int, default=80)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--outdir", type=Path, default=ENGINE_DIR / "replay_reports")
    args = parser.parse_args()

    universe = select_universe(args.universe)
    histories: dict[str, pd.DataFrame] = {}
    failures: list[dict] = []
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(fetch_history, item["code"], max(130, args.days + 100)): item
            for item in universe
        }
        for index, future in enumerate(as_completed(futures), start=1):
            item = futures[future]
            code, frame, source = future.result()
            if frame is None or len(frame) < 100:
                failures.append({"code": code, "reason": source})
            else:
                histories[code] = frame
            if index % 10 == 0 or index == len(futures):
                print(f"历史行情 {index}/{len(futures)}，成功 {len(histories)}", flush=True)

    observations: list[dict] = []
    trades: list[dict] = []
    regimes = market_regimes(histories)
    for index, item in enumerate(universe, start=1):
        frame = histories.get(item["code"])
        if frame is None:
            continue
        stock_observations, stock_trades = replay_stock(item, frame, args.days, regimes)
        observations.extend(stock_observations)
        trades.extend(stock_trades)
        if index % 10 == 0 or index == len(universe):
            print(f"策略重放 {index}/{len(universe)}", flush=True)

    summary = write_reports(args.outdir, observations, trades, failures)
    print(json.dumps({
        "observations": summary["observation_count"],
        "trades": summary["trade_count"],
        "variants": summary["variants"],
        "report": str(args.outdir / "strategy_replay_summary.json"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
