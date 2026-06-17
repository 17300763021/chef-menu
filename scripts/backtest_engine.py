"""Run a lightweight Backtrader validation for recent strong-pick strategy data."""

from __future__ import annotations

import argparse
import json
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


def recent_picks(client: SupabaseRest) -> list[dict[str, Any]]:
    start = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    rows = client.request(
        "GET",
        "stock_strong_picks"
        f"?scan_date=gte.{start}&select=*&order=scan_date.desc,score.desc&limit={PICK_LIMIT}",
    )
    return rows or []


def bought_keys(client: SupabaseRest) -> set[tuple[str, str]]:
    rows = client.request("GET", "stock_auto_trade_orders?side=eq.buy&select=code,order_date") or []
    keys: set[tuple[str, str]] = set()
    for row in rows:
        keys.add((str(row.get("code", "")).zfill(6), str(row.get("order_date", ""))))
    return keys


def stock_history(code: str, start_date: str, end_date: str) -> Any:
    ak, _, pd = load_backtest_dependencies()
    raw = ak.stock_zh_a_hist(
        symbol=str(code).zfill(6),
        period="daily",
        start_date=start_date.replace("-", ""),
        end_date=end_date.replace("-", ""),
        adjust="qfq",
    )
    if raw.empty:
        return raw
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
        result = {
            "entry_date": pick_date.isoformat(),
            "exit_date": last_date.isoformat(),
            "entry_price": entry_price,
            "exit_price": last_price,
            "shares": shares,
            "pnl_amount": (last_price - entry_price) * shares,
            "pnl_rate": (last_price - entry_price) / entry_price * 100 if entry_price else 0,
            "holding_days": max(0, (last_date - pick_date).days),
            "exit_reason": "end",
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
            "max_drawdown_rate": 0,
            "win_rate": 0,
            "profit_loss_ratio": 0,
            "avg_holding_days": 0,
        }
    pnl_values = [number(item.get("pnl_amount")) for item in trades]
    equity = INITIAL_CASH
    peak = INITIAL_CASH
    max_drawdown = 0.0
    for pnl in pnl_values:
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak * 100)
    wins = [value for value in pnl_values if value > 0]
    losses = [abs(value) for value in pnl_values if value < 0]
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    return {
        "final_value": equity,
        "total_return_rate": (equity - INITIAL_CASH) / INITIAL_CASH * 100 if INITIAL_CASH else 0,
        "max_drawdown_rate": max_drawdown,
        "win_rate": len(wins) / len(trades) * 100,
        "profit_loss_ratio": avg_win / avg_loss if avg_loss else (avg_win if avg_win else 0),
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


def insert_results(
    client: SupabaseRest,
    picks: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    missed: list[dict[str, Any]],
    curve: list[dict[str, Any]],
    benchmark_rate: float,
    dry_run: bool,
) -> dict[str, Any]:
    start_date = min(str(item.get("scan_date")) for item in picks)
    end_date = date.today().isoformat()
    metrics = summarize(trades)
    excess_return = metrics["total_return_rate"] - benchmark_rate
    run_payload = {
        "strategy_name": "strong_pick_v1_backtrader",
        "benchmark_name": "pick_equal_weight",
        "start_date": start_date,
        "end_date": end_date,
        "initial_cash": INITIAL_CASH,
        "final_value": metrics["final_value"],
        "total_return_rate": metrics["total_return_rate"],
        "benchmark_return_rate": benchmark_rate,
        "excess_return_rate": excess_return,
        "max_drawdown_rate": metrics["max_drawdown_rate"],
        "win_rate": metrics["win_rate"],
        "profit_loss_ratio": metrics["profit_loss_ratio"],
        "trade_count": len(trades),
        "avg_holding_days": metrics["avg_holding_days"],
        "missed_runner_count": len(missed),
        "note": f"Backtrader lightweight validation, picks={len(picks)}",
    }
    if dry_run:
        return {"run": run_payload, "trades": len(trades), "missed": len(missed), "curve": len(curve)}
    run_rows = client.request("POST", "stock_backtest_runs", run_payload, prefer="return=representation")
    run_id = run_rows[0]["id"]
    if trades:
        client.insert("stock_backtest_trades", [{**item, "run_id": run_id} for item in trades])
    if missed:
        client.insert("stock_missed_runners", [{**item, "run_id": run_id} for item in missed])
    if curve:
        client.insert("stock_backtest_equity_curve", [{**item, "run_id": run_id} for item in curve])
    return {"run_id": run_id, "trades": len(trades), "missed": len(missed), **run_payload}


def run(dry_run: bool = False) -> dict[str, Any]:
    client = get_client()
    picks = recent_picks(client)
    if not picks:
        return {"picks": 0, "trades": 0, "missed": 0}
    end = date.today()
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
    curve = equity_curve(trades, benchmark_rate)
    return insert_results(client, picks, trades, missed, curve, benchmark_rate, dry_run)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(dry_run=args.dry_run), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
