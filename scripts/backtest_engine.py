"""Run a lightweight Backtrader validation for recent strong-pick strategy data."""

from __future__ import annotations

import argparse
import json
import math
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sync_stock_data import SupabaseRest, env_value, read_env_file


ROOT = Path(__file__).resolve().parents[1]
INITIAL_CASH = float(os.environ.get("STOCK_BACKTEST_INITIAL_CASH", "1000000"))
POSITION_RATE = float(os.environ.get("STOCK_BACKTEST_POSITION_RATE", "0.08"))
TAKE_PROFIT_RATE = float(os.environ.get("STOCK_BACKTEST_TAKE_PROFIT_RATE", "0.12"))
DEFAULT_STOP_RATE = float(os.environ.get("STOCK_BACKTEST_STOP_RATE", "0.06"))
MAX_HOLD_DAYS = int(os.environ.get("STOCK_BACKTEST_MAX_HOLD_DAYS", "10"))
LOOKBACK_DAYS = int(os.environ.get("STOCK_BACKTEST_LOOKBACK_DAYS", "90"))
PICK_LIMIT = int(os.environ.get("STOCK_BACKTEST_PICK_LIMIT", "80"))
MISSED_RUNNER_RATE = float(os.environ.get("STOCK_BACKTEST_MISSED_RUNNER_RATE", "15"))
SLIPPAGE_RATE = float(os.environ.get("STOCK_BACKTEST_SLIPPAGE_RATE", os.environ.get("STOCK_PAPER_SLIPPAGE_RATE", "0.001")))
COMMISSION_RATE = float(os.environ.get("STOCK_BACKTEST_COMMISSION_RATE", os.environ.get("STOCK_PAPER_COMMISSION_RATE", "0.0003")))
MIN_COMMISSION = float(os.environ.get("STOCK_BACKTEST_MIN_COMMISSION", os.environ.get("STOCK_PAPER_MIN_COMMISSION", "5")))
STAMP_DUTY_RATE = float(os.environ.get("STOCK_BACKTEST_STAMP_DUTY_RATE", os.environ.get("STOCK_PAPER_STAMP_DUTY_RATE", "0.0005")))
TRANSFER_FEE_RATE = float(os.environ.get("STOCK_BACKTEST_TRANSFER_FEE_RATE", os.environ.get("STOCK_PAPER_TRANSFER_FEE_RATE", "0.00001")))
DEPS: tuple[Any, Any, Any] | None = None


def load_backtest_dependencies() -> tuple[Any, Any, Any]:
    global DEPS
    if DEPS:
        return DEPS
    import akshare as ak
    import backtrader as bt
    import pandas as pd

    DEPS = (ak, bt, pd)
    return DEPS


def make_pick_strategy(bt: Any) -> type:
    class PickStrategy(bt.Strategy):
        params = (
            ("entry_date", None),
            ("shares", 0),
            ("stop_price", 0.0),
            ("target_rate", TAKE_PROFIT_RATE),
            ("max_hold_days", MAX_HOLD_DAYS),
        )

        def __init__(self) -> None:
            self.entry_bar: int | None = None
            self.entry_price = 0.0
            self.exit_reason = "end"
            self.trade_result: dict[str, Any] | None = None

        def next(self) -> None:
            current_date = self.data.datetime.date(0)
            if not self.position and current_date >= self.p.entry_date:
                shares = int(self.p.shares)
                if shares > 0:
                    self.buy(size=shares)
                    self.entry_bar = len(self)
                    self.entry_price = float(self.data.close[0])
                return

            if not self.position or self.entry_bar is None:
                return

            holding_days = len(self) - self.entry_bar
            low = float(self.data.low[0])
            high = float(self.data.high[0])
            target = self.entry_price * (1 + float(self.p.target_rate))
            stop = float(self.p.stop_price) or self.entry_price * (1 - DEFAULT_STOP_RATE)
            if low <= stop:
                self.exit_reason = "stop_loss"
                self.close()
            elif high >= target:
                self.exit_reason = "take_profit"
                self.close()
            elif holding_days >= int(self.p.max_hold_days):
                self.exit_reason = "max_hold"
                self.close()

        def notify_trade(self, trade: Any) -> None:
            if not trade.isclosed:
                return
            entry_date = bt.num2date(trade.dtopen).date()
            exit_date = bt.num2date(trade.dtclose).date()
            self.trade_result = {
                "entry_date": entry_date.isoformat(),
                "exit_date": exit_date.isoformat(),
                "entry_price": float(trade.price),
                "exit_price": float(trade.price + trade.pnl / max(1, abs(trade.size))),
                "shares": abs(int(trade.size)),
                "pnl_amount": float(trade.pnl),
                "pnl_rate": float(trade.pnlcomm / (trade.price * abs(trade.size)) * 100) if trade.price and trade.size else 0,
                "holding_days": max(0, (exit_date - entry_date).days),
                "exit_reason": self.exit_reason,
            }

    return PickStrategy


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


def round_lot(shares: float) -> int:
    return max(0, int(shares // 100) * 100)


def execution_price(side: str, price: float) -> float:
    if price <= 0:
        return price
    direction = 1 if side == "buy" else -1
    return round(price * (1 + direction * SLIPPAGE_RATE), 4)


def trading_fee(side: str, gross_amount: float) -> float:
    if gross_amount <= 0:
        return 0
    commission = max(MIN_COMMISSION, gross_amount * COMMISSION_RATE)
    stamp_duty = gross_amount * STAMP_DUTY_RATE if side == "sell" else 0
    transfer_fee = gross_amount * TRANSFER_FEE_RATE
    return round(commission + stamp_duty + transfer_fee, 2)


def net_trade_result(entry_price: float, exit_price: float, shares: int) -> dict[str, float]:
    entry_executed = execution_price("buy", entry_price)
    exit_executed = execution_price("sell", exit_price)
    buy_amount = entry_executed * shares
    sell_amount = exit_executed * shares
    buy_fee = trading_fee("buy", buy_amount)
    sell_fee = trading_fee("sell", sell_amount)
    fee_amount = buy_fee + sell_fee
    slippage_cost = abs(entry_executed - entry_price) * shares + abs(exit_executed - exit_price) * shares
    pnl = sell_amount - sell_fee - buy_amount - buy_fee
    cost_basis = buy_amount + buy_fee
    return {
        "entry_price": entry_executed,
        "exit_price": exit_executed,
        "fee_amount": round(fee_amount, 2),
        "slippage_amount": round(slippage_cost, 2),
        "pnl_amount": round(pnl, 2),
        "pnl_rate": pnl / cost_basis * 100 if cost_basis > 0 else 0,
    }


def recent_picks(client: SupabaseRest, end_date: str | None = None) -> list[dict[str, Any]]:
    end = date.fromisoformat(end_date) if end_date else backtest_end_date()
    start = (end - timedelta(days=LOOKBACK_DAYS)).isoformat()
    rows = client.request(
        "GET",
        "stock_strong_picks"
        f"?scan_date=gte.{start}&scan_date=lte.{end.isoformat()}&select=*&order=scan_date.desc,score.desc&limit={PICK_LIMIT}",
    )
    return rows or []


def bought_keys(client: SupabaseRest) -> set[tuple[str, str]]:
    rows = client.request("GET", "stock_auto_trade_orders?side=eq.buy&select=code,order_date") or []
    keys: set[tuple[str, str]] = set()
    for row in rows:
        keys.add((str(row.get("code", "")).zfill(6), str(row.get("order_date", ""))))
    return keys


def backtest_end_date() -> date:
    configured = os.environ.get("STOCK_BACKTEST_END_DATE", "").strip()
    if configured:
        return date.fromisoformat(configured)
    return date.today()


def stock_history(code: str, start_date: str, end_date: str) -> Any:
    if os.environ.get("STOCK_BACKTEST_HISTORY_SOURCE", "").lower() == "cache":
        return cached_stock_history(code, start_date, end_date)
    ak, _, pd = load_backtest_dependencies()
    cache_frame = None
    try:
        raw = ak.stock_zh_a_hist(
            symbol=str(code).zfill(6),
            period="daily",
            start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
            adjust="qfq",
        )
    except Exception:
        cache_frame = cached_stock_history(code, start_date, end_date)
        raw = cache_frame
    if raw.empty:
        cache_frame = cached_stock_history(code, start_date, end_date)
        raw = cache_frame
    if raw.empty:
        return raw
    if cache_frame is not None and not cache_frame.empty:
        return cache_frame
    columns = list(raw.columns)
    frame = raw.rename(columns={
        columns[0]: "datetime",
        columns[1]: "open",
        columns[2]: "close",
        columns[3]: "high",
        columns[4]: "low",
        columns[5]: "volume",
    })
    frame["datetime"] = pd.to_datetime(frame["datetime"])
    frame = frame.set_index("datetime")
    return frame[["open", "high", "low", "close", "volume"]].astype(float)


def cached_stock_history(code: str, start_date: str, end_date: str) -> Any:
    _, _, pd = load_backtest_dependencies()
    client = get_client()
    rows = client.request(
        "GET",
        "stock_daily_history"
        f"?code=eq.{str(code).zfill(6)}"
        "&adjustment=eq.qfq"
        f"&trade_date=gte.{start_date}"
        f"&trade_date=lte.{end_date}"
        "&select=trade_date,open,high,low,close,volume"
        "&order=trade_date.asc",
    ) or []
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows).rename(columns={"trade_date": "datetime"})
    frame["datetime"] = pd.to_datetime(frame["datetime"])
    frame = frame.set_index("datetime")
    return frame[["open", "high", "low", "close", "volume"]].astype(float)


def run_single_pick(pick: dict[str, Any], end: date) -> dict[str, Any] | None:
    _, bt, _ = load_backtest_dependencies()
    pick_strategy = make_pick_strategy(bt)
    code = str(pick.get("code", "")).zfill(6)
    pick_date = date.fromisoformat(str(pick.get("scan_date")))
    history = stock_history(code, pick_date.isoformat(), end.isoformat())
    if len(history) < 2:
        return None
    entry_price = float(history.iloc[0]["close"])
    shares = max(0, int((INITIAL_CASH * POSITION_RATE / entry_price) // 100) * 100)
    if shares <= 0:
        return None

    cerebro = bt.Cerebro(stdstats=False)
    cerebro.broker.setcash(INITIAL_CASH)
    cerebro.adddata(bt.feeds.PandasData(dataname=history))
    cerebro.addstrategy(
        pick_strategy,
        entry_date=pick_date,
        shares=shares,
        stop_price=number(pick.get("stop_loss")),
    )
    strategies = cerebro.run()
    strategy = strategies[0]
    result = strategy.trade_result
    if not result:
        last_date = history.index[-1].date()
        last_price = float(history.iloc[-1]["close"])
        net_result = net_trade_result(entry_price, last_price, shares)
        result = {
            "entry_date": pick_date.isoformat(),
            "exit_date": last_date.isoformat(),
            "entry_price": net_result["entry_price"],
            "exit_price": net_result["exit_price"],
            "shares": shares,
            "pnl_amount": net_result["pnl_amount"],
            "pnl_rate": net_result["pnl_rate"],
            "fee_amount": net_result["fee_amount"],
            "slippage_amount": net_result["slippage_amount"],
            "holding_days": max(0, (last_date - pick_date).days),
            "exit_reason": "end",
        }
    else:
        net_result = net_trade_result(number(result.get("entry_price")), number(result.get("exit_price")), integer_shares := int(number(result.get("shares"))))
        result = {
            **result,
            "entry_price": net_result["entry_price"],
            "exit_price": net_result["exit_price"],
            "shares": integer_shares,
            "pnl_amount": net_result["pnl_amount"],
            "pnl_rate": net_result["pnl_rate"],
            "fee_amount": net_result["fee_amount"],
            "slippage_amount": net_result["slippage_amount"],
        }
    return {
        **result,
        "code": code,
        "name": pick.get("name", ""),
    }


def max_runner_after_pick(pick: dict[str, Any], end: date) -> dict[str, Any] | None:
    code = str(pick.get("code", "")).zfill(6)
    pick_date = date.fromisoformat(str(pick.get("scan_date")))
    history = stock_history(code, pick_date.isoformat(), min(end, pick_date + timedelta(days=30)).isoformat())
    if history.empty:
        return None
    pick_price = number(pick.get("prev_close")) or float(history.iloc[0]["close"])
    if pick_price <= 0:
        return None
    max_index = history["high"].idxmax()
    max_price = float(history.loc[max_index]["high"])
    max_return = (max_price - pick_price) / pick_price * 100
    if max_return < MISSED_RUNNER_RATE:
        return None
    return {
        "pick_date": pick_date.isoformat(),
        "code": code,
        "name": pick.get("name", ""),
        "pick_price": pick_price,
        "max_price": max_price,
        "max_return_rate": max_return,
        "days_to_high": max(0, (max_index.date() - pick_date).days),
        "reason": "strong pick was not bought before a large move",
    }


def summarize(trades: list[dict[str, Any]]) -> dict[str, float]:
    if not trades:
        return {
            "final_value": INITIAL_CASH,
            "total_return_rate": 0,
            "annual_return_rate": 0,
            "max_drawdown_rate": 0,
            "sharpe_ratio": 0,
            "calmar_ratio": 0,
            "win_rate": 0,
            "profit_loss_ratio": 0,
            "turnover_rate": 0,
            "consecutive_losses": 0,
            "largest_single_loss": 0,
            "avg_holding_days": 0,
        }
    pnl_values = [number(item.get("pnl_amount")) for item in trades]
    pnl_rates = [number(item.get("pnl_rate")) for item in trades]
    equity = INITIAL_CASH
    peak = INITIAL_CASH
    max_drawdown = 0.0
    current_loss_streak = 0
    max_loss_streak = 0
    for pnl in pnl_values:
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak * 100)
        if pnl < 0:
            current_loss_streak += 1
            max_loss_streak = max(max_loss_streak, current_loss_streak)
        else:
            current_loss_streak = 0
    wins = [value for value in pnl_values if value > 0]
    losses = [abs(value) for value in pnl_values if value < 0]
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    sorted_dates = sorted(str(item.get("exit_date", "")) for item in trades if item.get("exit_date"))
    period_days = 0
    if len(sorted_dates) >= 2:
        start = date.fromisoformat(sorted_dates[0])
        end = date.fromisoformat(sorted_dates[-1])
        period_days = max(1, (end - start).days)
    total_return = (equity - INITIAL_CASH) / INITIAL_CASH * 100 if INITIAL_CASH else 0
    annual_return = ((equity / INITIAL_CASH) ** (365 / period_days) - 1) * 100 if period_days and equity > 0 and INITIAL_CASH > 0 else total_return
    avg_rate = sum(pnl_rates) / len(pnl_rates) if pnl_rates else 0
    variance = sum((value - avg_rate) ** 2 for value in pnl_rates) / len(pnl_rates) if pnl_rates else 0
    volatility = math.sqrt(variance)
    sharpe = avg_rate / volatility * math.sqrt(252) if volatility > 0 else 0
    calmar = annual_return / max_drawdown if max_drawdown > 0 else 0
    traded_amount = sum(number(item.get("entry_price")) * number(item.get("shares")) for item in trades)
    return {
        "final_value": equity,
        "total_return_rate": total_return,
        "annual_return_rate": annual_return,
        "max_drawdown_rate": max_drawdown,
        "sharpe_ratio": sharpe,
        "calmar_ratio": calmar,
        "win_rate": len(wins) / len(trades) * 100,
        "profit_loss_ratio": avg_win / avg_loss if avg_loss else (avg_win if avg_win else 0),
        "turnover_rate": traded_amount / INITIAL_CASH * 100 if INITIAL_CASH else 0,
        "consecutive_losses": max_loss_streak,
        "largest_single_loss": min(pnl_values),
        "avg_holding_days": sum(number(item.get("holding_days")) for item in trades) / len(trades),
    }


def benchmark_return(picks: list[dict[str, Any]], end: date) -> float:
    returns: list[float] = []
    for pick in picks[: min(len(picks), 40)]:
        try:
            code = str(pick.get("code", "")).zfill(6)
            pick_date = date.fromisoformat(str(pick.get("scan_date")))
            history = stock_history(code, pick_date.isoformat(), end.isoformat())
            if len(history) < 2:
                continue
            entry = float(history.iloc[0]["close"])
            latest = float(history.iloc[-1]["close"])
            if entry > 0:
                returns.append((latest - entry) / entry * 100)
        except Exception as reason:
            print(json.dumps({"benchmark_skip": pick.get("code"), "reason": str(reason)}, ensure_ascii=False), flush=True)
    return sum(returns) / len(returns) if returns else 0


def benchmark_return_from_history(history: Any) -> float:
    if history is None:
        return 0
    if hasattr(history, "empty") and history.empty:
        return 0
    if not hasattr(history, "iloc") and len(history) == 0:
        return 0
    try:
        first = history.iloc[0]
        last = history.iloc[-1]
        start_close = number(first["close"])
        end_close = number(last["close"])
    except AttributeError:
        start_close = number(history[0].get("close"))
        end_close = number(history[-1].get("close"))
    if start_close <= 0:
        return 0
    return (end_close - start_close) / start_close * 100


def index_history(symbol: str, start_date: str, end_date: str) -> Any:
    ak, _, pd = load_backtest_dependencies()
    compact_start = start_date.replace("-", "")
    compact_end = end_date.replace("-", "")
    if hasattr(ak, "index_zh_a_hist"):
        raw = ak.index_zh_a_hist(symbol=symbol, period="daily", start_date=compact_start, end_date=compact_end)
    else:
        raw = ak.stock_zh_index_daily(symbol=symbol)
        if not raw.empty:
            date_column = "date" if "date" in raw.columns else raw.columns[0]
            raw[date_column] = pd.to_datetime(raw[date_column])
            start = pd.to_datetime(start_date)
            end = pd.to_datetime(end_date)
            raw = raw[(raw[date_column] >= start) & (raw[date_column] <= end)]
    if raw.empty:
        return raw
    columns = list(raw.columns)
    close_column = "close" if "close" in raw.columns else columns[2]
    return raw.rename(columns={close_column: "close"})


def index_benchmark_returns(start_date: str, end_date: str) -> dict[str, float]:
    benchmarks = {
        "csi300": "000300",
        "csi500": "000905",
    }
    results: dict[str, float] = {}
    for name, symbol in benchmarks.items():
        try:
            results[name] = benchmark_return_from_history(index_history(symbol, start_date, end_date))
        except Exception as reason:
            print(json.dumps({"benchmark_skip": name, "reason": str(reason)}, ensure_ascii=False), flush=True)
            results[name] = 0
    return results


def build_date_splits(start_date: str, end_date: str) -> list[dict[str, str]]:
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if end < start:
        raise ValueError("end_date must be on or after start_date")
    total_days = max(1, (end - start).days + 1)
    names = ["in_sample", "validation", "test", "out_of_sample"]
    if total_days < len(names):
        splits: list[dict[str, str]] = []
        for index, name in enumerate(names):
            if index < total_days:
                split_date = start + timedelta(days=index)
                splits.append({"name": name, "start_date": split_date.isoformat(), "end_date": split_date.isoformat()})
            else:
                splits.append({"name": name, "start_date": "", "end_date": ""})
        return splits
    weights = [0.6, 0.2, 0.1, 0.1]
    lengths = [max(1, int(total_days * weight)) for weight in weights]
    while sum(lengths) > total_days:
        index = max(range(len(lengths)), key=lambda item: lengths[item])
        lengths[index] -= 1
    while sum(lengths) < total_days:
        lengths[0] += 1

    cursor = start
    splits: list[dict[str, str]] = []
    for name, length in zip(names, lengths):
        split_end = min(end, cursor + timedelta(days=length - 1))
        splits.append({
            "name": name,
            "start_date": cursor.isoformat(),
            "end_date": split_end.isoformat(),
        })
        cursor = split_end + timedelta(days=1)
    return splits


def split_trade_metrics(trades: list[dict[str, Any]], splits: list[dict[str, str]]) -> dict[str, dict[str, float]]:
    metrics: dict[str, dict[str, float]] = {}
    for split in splits:
        if not split["start_date"] or not split["end_date"]:
            metrics[split["name"]] = {
                "start_date": split["start_date"],
                "end_date": split["end_date"],
                "trade_count": 0,
                "total_return_rate": 0,
                "max_drawdown_rate": 0,
                "win_rate": 0,
                "profit_loss_ratio": 0,
            }
            continue
        start = date.fromisoformat(split["start_date"])
        end = date.fromisoformat(split["end_date"])
        split_trades = [
            trade for trade in trades
            if trade.get("exit_date") and start <= date.fromisoformat(str(trade["exit_date"])) <= end
        ]
        split_summary = summarize(split_trades)
        metrics[split["name"]] = {
            "start_date": split["start_date"],
            "end_date": split["end_date"],
            "trade_count": len(split_trades),
            "total_return_rate": split_summary["total_return_rate"],
            "max_drawdown_rate": split_summary["max_drawdown_rate"],
            "win_rate": split_summary["win_rate"],
            "profit_loss_ratio": split_summary["profit_loss_ratio"],
        }
    return metrics


def parameter_sensitivity_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for take_profit in (0.08, 0.12, 0.16):
        for stop_rate in (0.04, 0.06, 0.08):
            max_hold = 5 if take_profit <= 0.08 else (10 if take_profit <= 0.12 else 15)
            cases.append({
                "case_name": f"tp_{take_profit:.2f}_sl_{stop_rate:.2f}_hold_{max_hold}",
                "take_profit_rate": take_profit,
                "stop_rate": stop_rate,
                "max_hold_days": max_hold,
            })
    return cases


def parameter_sensitivity_summary(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    base_metrics = summarize(trades)
    summary: list[dict[str, Any]] = []
    for case in parameter_sensitivity_cases():
        summary.append({
            **case,
            "baseline_trade_count": len(trades),
            "baseline_total_return_rate": base_metrics["total_return_rate"],
            "baseline_max_drawdown_rate": base_metrics["max_drawdown_rate"],
        })
    return summary


def equity_curve(trades: list[dict[str, Any]], benchmark_rate: float) -> list[dict[str, Any]]:
    sorted_trades = sorted(trades, key=lambda item: str(item.get("exit_date", "")))
    if not sorted_trades:
        return []
    equity = INITIAL_CASH
    previous_equity = INITIAL_CASH
    peak = INITIAL_CASH
    points: list[dict[str, Any]] = []
    total = max(1, len(sorted_trades))
    for index, trade in enumerate(sorted_trades, start=1):
        equity += number(trade.get("pnl_amount"))
        peak = max(peak, equity)
        daily_return = (equity - previous_equity) / previous_equity * 100 if previous_equity else 0
        drawdown = (peak - equity) / peak * 100 if peak else 0
        benchmark_progress = benchmark_rate * index / total
        points.append({
            "curve_date": trade.get("exit_date"),
            "equity_value": equity,
            "daily_return_rate": daily_return,
            "drawdown_rate": drawdown,
            "benchmark_value": INITIAL_CASH * (1 + benchmark_progress / 100),
            "benchmark_return_rate": benchmark_progress,
        })
        previous_equity = equity
    return points


def reconcile_equity_curve(trades: list[dict[str, Any]], curve: list[dict[str, Any]]) -> dict[str, Any]:
    sorted_trades = sorted(trades, key=lambda item: str(item.get("exit_date", "")))
    mismatches: list[dict[str, Any]] = []
    equity = INITIAL_CASH
    for index, trade in enumerate(sorted_trades):
        equity += number(trade.get("pnl_amount"))
        if index >= len(curve):
            mismatches.append({"index": index, "reason": "missing_curve_point", "expected_equity": round(equity, 2)})
            continue
        actual = number(curve[index].get("equity_value"))
        if abs(actual - equity) >= 0.01:
            mismatches.append({
                "index": index,
                "reason": "equity_mismatch",
                "expected_equity": round(equity, 2),
                "actual_equity": round(actual, 2),
            })
    if len(curve) > len(sorted_trades):
        for index in range(len(sorted_trades), len(curve)):
            mismatches.append({"index": index, "reason": "extra_curve_point"})
    actual_final = number(curve[-1].get("equity_value")) if curve else INITIAL_CASH
    return {
        "ok": not mismatches,
        "trade_count": len(sorted_trades),
        "curve_count": len(curve),
        "expected_final_value": round(equity, 2),
        "actual_final_value": round(actual_final, 2),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:10],
    }


def insert_results(
    client: SupabaseRest | None,
    picks: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    missed: list[dict[str, Any]],
    curve: list[dict[str, Any]],
    benchmark_rate: float,
    dry_run: bool,
    benchmark_details: dict[str, float] | None = None,
    report_end_date: str | None = None,
) -> dict[str, Any]:
    start_date = min(str(item.get("scan_date")) for item in picks)
    end_date = report_end_date or date.today().isoformat()
    metrics = summarize(trades)
    details = benchmark_details or {}
    reconciliation = reconcile_equity_curve(trades, curve)
    splits = build_date_splits(start_date, end_date)
    split_summary = split_trade_metrics(trades, splits)
    sensitivity_summary = parameter_sensitivity_summary(trades)
    excess_return = metrics["total_return_rate"] - benchmark_rate
    run_payload = {
        "strategy_name": "strong_pick_v1_backtrader",
        "benchmark_name": "pick_equal_weight",
        "start_date": start_date,
        "end_date": end_date,
        "initial_cash": INITIAL_CASH,
        "final_value": metrics["final_value"],
        "total_return_rate": metrics["total_return_rate"],
        "annual_return_rate": metrics["annual_return_rate"],
        "benchmark_return_rate": benchmark_rate,
        "benchmark_csi300_return_rate": details.get("csi300", 0),
        "benchmark_csi500_return_rate": details.get("csi500", 0),
        "excess_return_rate": excess_return,
        "equity_reconciled": reconciliation["ok"],
        "max_drawdown_rate": metrics["max_drawdown_rate"],
        "sharpe_ratio": metrics["sharpe_ratio"],
        "calmar_ratio": metrics["calmar_ratio"],
        "win_rate": metrics["win_rate"],
        "profit_loss_ratio": metrics["profit_loss_ratio"],
        "turnover_rate": metrics["turnover_rate"],
        "consecutive_losses": metrics["consecutive_losses"],
        "largest_single_loss": metrics["largest_single_loss"],
        "sample_split_summary": split_summary,
        "parameter_sensitivity_summary": sensitivity_summary,
        "trade_count": len(trades),
        "avg_holding_days": metrics["avg_holding_days"],
        "missed_runner_count": len(missed),
        "note": (
            f"Backtrader lightweight validation, picks={len(picks)}, "
            f"equity_reconciled={reconciliation['ok']}, mismatches={reconciliation['mismatch_count']}"
        ),
    }
    if dry_run:
        return {"run": run_payload, "trades": len(trades), "missed": len(missed), "curve": len(curve)}
    if client is None:
        raise ValueError("client is required when dry_run is false")
    try:
        run_rows = client.request("POST", "stock_backtest_runs", run_payload, prefer="return=representation")
    except RuntimeError as error:
        optional_columns = {
            "annual_return_rate",
            "sharpe_ratio",
            "calmar_ratio",
            "turnover_rate",
            "consecutive_losses",
            "largest_single_loss",
            "benchmark_csi300_return_rate",
            "benchmark_csi500_return_rate",
            "equity_reconciled",
            "sample_split_summary",
            "parameter_sensitivity_summary",
        }
        if not any(column in str(error) for column in optional_columns):
            raise
        fallback_payload = {key: value for key, value in run_payload.items() if key not in optional_columns}
        run_rows = client.request("POST", "stock_backtest_runs", fallback_payload, prefer="return=representation")
    run_id = run_rows[0]["id"]
    if trades:
        trade_rows = [{**item, "run_id": run_id} for item in trades]
        try:
            client.insert("stock_backtest_trades", trade_rows)
        except RuntimeError as error:
            if "fee_amount" not in str(error) and "slippage_amount" not in str(error):
                raise
            client.insert(
                "stock_backtest_trades",
                [
                    {key: value for key, value in item.items() if key not in {"fee_amount", "slippage_amount"}}
                    for item in trade_rows
                ],
            )
    if missed:
        client.insert("stock_missed_runners", [{**item, "run_id": run_id} for item in missed])
    if curve:
        client.insert("stock_backtest_equity_curve", [{**item, "run_id": run_id} for item in curve])
    return {"run_id": run_id, "trades": len(trades), "missed": len(missed), **run_payload}


def run(dry_run: bool = False) -> dict[str, Any]:
    client = get_client()
    end = backtest_end_date()
    picks = recent_picks(client, end.isoformat())
    if not picks:
        return {"picks": 0, "trades": 0, "missed": 0}
    orders = bought_keys(client)
    trades: list[dict[str, Any]] = []
    missed: list[dict[str, Any]] = []
    for pick in picks:
        try:
            trade = run_single_pick(pick, end)
            if trade:
                trades.append(trade)
            code = str(pick.get("code", "")).zfill(6)
            pick_date = str(pick.get("scan_date"))
            was_bought = any(code == bought_code and order_date >= pick_date for bought_code, order_date in orders)
            if not was_bought:
                runner = max_runner_after_pick(pick, end)
                if runner:
                    missed.append(runner)
        except Exception as reason:
            print(json.dumps({"skip": pick.get("code"), "reason": str(reason)}, ensure_ascii=False), flush=True)
    benchmark_rate = benchmark_return(picks, end)
    start_date = min(str(item.get("scan_date")) for item in picks)
    benchmark_details = index_benchmark_returns(start_date, end.isoformat())
    curve = equity_curve(trades, benchmark_rate)
    return insert_results(client, picks, trades, missed, curve, benchmark_rate, dry_run, benchmark_details, end.isoformat())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(dry_run=args.dry_run), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
