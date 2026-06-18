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
INITIAL_CAPITAL = 1_000_000.0
MAX_HOLDINGS = 6
NORMAL_POSITION_RATE = 0.08
WEAK_POSITION_RATE = 0.03
CASH_RESERVE_RATE = 0.25
LOT_SIZE = 100
BUY_COMMISSION_RATE = 0.0003
SELL_COMMISSION_RATE = 0.0003
STAMP_DUTY_RATE = 0.0005
MIN_COMMISSION = 5.0
SLIPPAGE_RATE = 0.001


def is_bad_name(name: str) -> bool:
    text = str(name).upper()
    return any(marker in text for marker in ["ST", "退", "N", "C"])


def number(value: Any, fallback: float = 0) -> float:
    try:
        value = float(value)
        return fallback if math.isnan(value) else value
    except (TypeError, ValueError):
        return fallback


def round_lot(shares: float) -> int:
    return max(0, int(shares // LOT_SIZE) * LOT_SIZE)


def commission(amount: float, rate: float) -> float:
    return max(MIN_COMMISSION, amount * rate) if amount > 0 else 0


def daily_limit_rate(code: str) -> float:
    if str(code).startswith(("300", "301", "688")):
        return 0.20
    if str(code).startswith(("4", "8", "9")):
        return 0.30
    return 0.10


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


def simulate_trade(
    frame: pd.DataFrame,
    index: int,
    stop_price: float,
    code: str = "",
) -> dict[str, Any] | None:
    if index + 1 >= len(frame):
        return None
    entry_index = index + 1
    entry_price = float(frame.iloc[entry_index]["open"])
    if entry_price <= 0:
        return None
    previous_close = float(frame.iloc[index]["close"])
    entry_row = frame.iloc[entry_index]
    limit_rate = daily_limit_rate(code)
    if (
        previous_close > 0
        and entry_price >= previous_close * (1 + limit_rate) * 0.995
        and float(entry_row["high"]) == float(entry_row["low"])
    ):
        return None
    stop = stop_price if 0 < stop_price < entry_price else entry_price * 0.94
    target = entry_price * 1.12
    exit_index = min(entry_index + 10, len(frame) - 1)
    exit_price = float(frame.iloc[exit_index]["close"])
    exit_reason = "10d"
    # A股 T+1：买入当日不能卖出，从下一交易日开始检查止损止盈。
    for cursor in range(entry_index + 1, exit_index + 1):
        row = frame.iloc[cursor]
        open_price = float(row["open"])
        previous_close = float(frame.iloc[cursor - 1]["close"])
        one_price_limit_down = (
            previous_close > 0
            and open_price <= previous_close * (1 - limit_rate) * 1.005
            and float(row["high"]) == float(row["low"])
        )
        if open_price <= stop:
            if one_price_limit_down:
                continue
            exit_index, exit_price, exit_reason = cursor, open_price, "gap_stop"
            break
        if open_price >= target:
            exit_index, exit_price, exit_reason = cursor, open_price, "gap_target"
            break
        if float(row["low"]) <= stop:
            exit_index, exit_price, exit_reason = cursor, stop, "stop"
            break
        if float(row["high"]) >= target:
            exit_index, exit_price, exit_reason = cursor, target, "target"
            break
    return {
        "entry_date": str(frame.iloc[entry_index]["date"].date()),
        "exit_date": str(frame.iloc[exit_index]["date"].date()),
        "entry_price": entry_price,
        "exit_price": exit_price,
        "stop_price": stop,
        "target_price": target,
        "pnl_rate": (exit_price - entry_price) / entry_price * 100 - 0.2,
        "holding_days": exit_index - entry_index + 1,
        "exit_reason": exit_reason,
        "price_path": {
            str(frame.iloc[cursor]["date"].date()): float(frame.iloc[cursor]["close"])
            for cursor in range(entry_index, exit_index + 1)
        },
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
            trade = simulate_trade(frame, index, number(result.get("stop")), item["code"])
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


def portfolio_position_rate(trade: dict[str, Any]) -> float:
    return WEAK_POSITION_RATE if trade.get("market_regime") == "weak" else NORMAL_POSITION_RATE


def portfolio_backtest(
    candidate_trades: list[dict[str, Any]],
    initial_capital: float = INITIAL_CAPITAL,
    max_holdings: int = MAX_HOLDINGS,
) -> dict[str, Any]:
    """Convert independent signal trades into an executable cash/position portfolio."""
    entries: dict[str, list[dict[str, Any]]] = {}
    exits: dict[str, list[dict[str, Any]]] = {}
    valuation_dates: set[str] = set()
    for trade in candidate_trades:
        entries.setdefault(str(trade["entry_date"]), []).append(trade)
        exits.setdefault(str(trade["exit_date"]), []).append(trade)
        valuation_dates.update(str(day) for day in trade.get("price_path", {}))

    dates = sorted(set(entries) | set(exits) | valuation_dates)
    cash = initial_capital
    positions: dict[str, dict[str, Any]] = {}
    executed: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    peak = initial_capital

    for day in dates:
        # 先处理当天应退出的真实持仓，释放资金后再选新机会。
        due_codes = [
            code for code, position in positions.items()
            if str(position["candidate"]["exit_date"]) == day
        ]
        for code in due_codes:
            position = positions.pop(code)
            candidate = position["candidate"]
            raw_exit = number(candidate.get("exit_price"))
            sell_price = raw_exit * (1 - SLIPPAGE_RATE)
            sell_amount = sell_price * position["shares"]
            sell_fee = commission(sell_amount, SELL_COMMISSION_RATE)
            stamp_duty = sell_amount * STAMP_DUTY_RATE
            cash += sell_amount - sell_fee - stamp_duty
            pnl_amount = sell_amount - sell_fee - stamp_duty - position["cost_amount"]
            cost_amount = position["cost_amount"]
            executed.append({
                **candidate,
                "shares": position["shares"],
                "buy_price": position["buy_price"],
                "sell_price": sell_price,
                "buy_fee": position["buy_fee"],
                "sell_fee": sell_fee,
                "stamp_duty": stamp_duty,
                "pnl_amount": pnl_amount,
                "portfolio_pnl_rate": pnl_amount / cost_amount * 100 if cost_amount else 0,
                "position_rate": position["position_rate"],
            })

        candidates = sorted(
            entries.get(day, []),
            key=lambda item: (-number(item.get("score")), str(item.get("code"))),
        )
        seen_today: set[str] = set()
        for candidate in candidates:
            code = str(candidate.get("code", "")).zfill(6)
            if code in seen_today:
                rejected.append({**candidate, "reject_reason": "same_stock_same_day_lower_priority"})
                continue
            seen_today.add(code)
            if code in positions:
                rejected.append({**candidate, "reject_reason": "already_holding"})
                continue
            if len(positions) >= max_holdings:
                rejected.append({**candidate, "reject_reason": "max_holdings"})
                continue

            raw_entry = number(candidate.get("entry_price"))
            if raw_entry <= 0:
                rejected.append({**candidate, "reject_reason": "invalid_entry_price"})
                continue
            buy_price = raw_entry * (1 + SLIPPAGE_RATE)
            position_rate = portfolio_position_rate(candidate)
            target_amount = initial_capital * position_rate
            reserve_cash = initial_capital * CASH_RESERVE_RATE
            allowed_amount = min(target_amount, max(0, cash - reserve_cash))
            shares = round_lot(allowed_amount / buy_price)
            if shares <= 0:
                rejected.append({**candidate, "reject_reason": "insufficient_cash"})
                continue
            buy_amount = buy_price * shares
            buy_fee = commission(buy_amount, BUY_COMMISSION_RATE)
            total_cost = buy_amount + buy_fee
            while shares > 0 and total_cost > cash - reserve_cash:
                shares -= LOT_SIZE
                buy_amount = buy_price * shares
                buy_fee = commission(buy_amount, BUY_COMMISSION_RATE)
                total_cost = buy_amount + buy_fee
            if shares <= 0:
                rejected.append({**candidate, "reject_reason": "insufficient_cash"})
                continue
            cash -= total_cost
            positions[code] = {
                "candidate": candidate,
                "shares": shares,
                "buy_price": buy_price,
                "buy_fee": buy_fee,
                "cost_amount": total_cost,
                "position_rate": position_rate,
            }

        market_value = 0.0
        for position in positions.values():
            price_path = position["candidate"].get("price_path", {})
            mark_price = number(price_path.get(day), position["buy_price"])
            market_value += mark_price * position["shares"]
        equity = cash + market_value
        peak = max(peak, equity)
        equity_curve.append({
            "date": day,
            "cash": round(cash, 2),
            "market_value": round(market_value, 2),
            "total_assets": round(equity, 2),
            "position_count": len(positions),
            "drawdown_rate": round((peak - equity) / peak * 100 if peak else 0, 4),
        })

    final_assets = cash + sum(position["buy_price"] * position["shares"] for position in positions.values())
    pnl_rates = [number(item.get("portfolio_pnl_rate")) for item in executed]
    wins = [value for value in pnl_rates if value > 0]
    losses = [abs(value) for value in pnl_rates if value < 0]
    max_drawdown = max((number(item.get("drawdown_rate")) for item in equity_curve), default=0)
    return {
        "initial_capital": initial_capital,
        "final_assets": round(final_assets, 2),
        "total_return_rate": round((final_assets - initial_capital) / initial_capital * 100, 2),
        "max_drawdown_rate": round(max_drawdown, 2),
        "trade_count": len(executed),
        "rejected_count": len(rejected),
        "win_rate": round(len(wins) / len(pnl_rates) * 100, 2) if pnl_rates else 0,
        "profit_loss_ratio": round(mean(wins) / mean(losses), 2) if wins and losses else 0,
        "avg_holding_days": round(mean(number(item.get("holding_days")) for item in executed), 2) if executed else 0,
        "executed_trades": executed,
        "rejected_signals": rejected,
        "equity_curve": equity_curve,
    }


def compare_portfolios(trades: list[dict]) -> tuple[list[dict[str, Any]], list[dict], list[dict], list[dict]]:
    summaries: list[dict[str, Any]] = []
    executed: list[dict] = []
    rejected: list[dict] = []
    curves: list[dict] = []
    for variant in VARIANTS:
        result = portfolio_backtest([item for item in trades if item["variant"] == variant])
        summaries.append({
            key: value for key, value in result.items()
            if key not in {"executed_trades", "rejected_signals", "equity_curve"}
        } | {"variant": variant})
        executed.extend([{**item, "variant": variant} for item in result["executed_trades"]])
        rejected.extend([{**item, "variant": variant} for item in result["rejected_signals"]])
        curves.extend([{**item, "variant": variant} for item in result["equity_curve"]])
    summaries.sort(key=lambda item: (item["total_return_rate"], -item["max_drawdown_rate"]), reverse=True)
    return summaries, executed, rejected, curves


def write_reports(outdir: Path, observations: list[dict], trades: list[dict], failures: list[dict]) -> dict[str, Any]:
    outdir.mkdir(parents=True, exist_ok=True)
    reasons = indicator_diagnostics(observations, "reasons")
    risks = indicator_diagnostics(observations, "risks")
    signal_variants = compare_variants(trades)
    portfolio_variants, executed, rejected, curves = compare_portfolios(trades)
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "observation_count": len(observations),
        "trade_count": len(trades),
        "failed_symbols": failures,
        "variants": portfolio_variants,
        "portfolio_variants": portfolio_variants,
        "signal_variants": signal_variants,
        "portfolio_assumptions": {
            "initial_capital": INITIAL_CAPITAL,
            "max_holdings": MAX_HOLDINGS,
            "normal_position_rate": NORMAL_POSITION_RATE,
            "weak_position_rate": WEAK_POSITION_RATE,
            "cash_reserve_rate": CASH_RESERVE_RATE,
            "lot_size": LOT_SIZE,
            "buy_commission_rate": BUY_COMMISSION_RATE,
            "sell_commission_rate": SELL_COMMISSION_RATE,
            "stamp_duty_rate": STAMP_DUTY_RATE,
            "minimum_commission": MIN_COMMISSION,
            "slippage_rate_each_side": SLIPPAGE_RATE,
            "t_plus_one": True,
            "duplicate_entry_while_holding": False,
        },
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
    pd.DataFrame(executed).to_csv(outdir / "strategy_portfolio_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(rejected).to_csv(outdir / "strategy_portfolio_rejections.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(curves).to_csv(outdir / "strategy_portfolio_equity.csv", index=False, encoding="utf-8-sig")
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
