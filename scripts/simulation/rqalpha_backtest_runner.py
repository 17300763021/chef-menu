"""Execute and independently reconcile the bounded M3.3 RQAlpha backtest."""

from __future__ import annotations

import argparse
import json
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import Any

from scripts.market_data.manifest import sha256
from scripts.simulation.contracts import DataState, MarketState, OrderInstruction, PositionState
from scripts.simulation.m2_history_source import (
    ACCEPTANCE_SESSIONS,
    PINNED_DAILY_RELEASE_IDS,
    PINNED_HISTORY_DATASET_ID,
    load_bounded_input,
)
from scripts.simulation.rqalpha_adapter import RQAlphaAdapter


INITIAL_CAPITAL = Decimal("1000000.0000")
STRATEGY_VERSION = "m3.3-engine-acceptance-fixture-v1"
_Q = Decimal("0.0001")


def _money(value: Any) -> Decimal:
    with localcontext() as context:
        context.prec = 34
        return Decimal(str(value)).quantize(_Q, rounding=ROUND_HALF_UP)


def _release_for(session: date) -> str:
    if session == ACCEPTANCE_SESSIONS[0]:
        return PINNED_HISTORY_DATASET_ID
    return PINNED_DAILY_RELEASE_IDS[ACCEPTANCE_SESSIONS.index(session) - 1]


def _normalized_results(result: dict[str, Any], input_sha256: str) -> dict[str, Any]:
    analyser = result.get("sys_analyser")
    if not isinstance(analyser, dict):
        raise RuntimeError("RQAlpha analyser output is missing")
    trades = analyser.get("trades")
    portfolio = analyser.get("portfolio")
    positions = analyser.get("stock_positions")
    account = analyser.get("stock_account")
    if any(value is None for value in (trades, portfolio, positions, account)):
        raise RuntimeError("RQAlpha accounting tables are incomplete")
    if len(trades) < 1:
        raise RuntimeError("M3.3 expected at least one real framework fill")

    normalized_trades = []
    cash = INITIAL_CAPITAL
    quantities: dict[str, int] = {}
    total_cost = Decimal("0")
    for _, row in trades.sort_values(["trading_datetime", "order_book_id", "side"]).iterrows():
        symbol = str(row["order_book_id"])
        quantity = int(row["last_quantity"])
        price = _money(row["last_price"])
        transaction_cost = _money(row["transaction_cost"])
        side = str(row["side"])
        gross = _money(price * quantity)
        if side == "BUY":
            cash = _money(cash - gross - transaction_cost)
            quantities[symbol] = quantities.get(symbol, 0) + quantity
        elif side == "SELL":
            cash = _money(cash + gross - transaction_cost)
            quantities[symbol] = quantities.get(symbol, 0) - quantity
        else:
            raise RuntimeError(f"unsupported RQAlpha side: {side}")
        total_cost = _money(total_cost + transaction_cost)
        normalized_trades.append({
            "trading_datetime": str(row["trading_datetime"]), "order_book_id": symbol,
            "side": side, "quantity": quantity, "price": format(price, "f"),
            "commission": format(_money(row["commission"]), "f"),
            "tax": format(_money(row["tax"]), "f"),
            "transaction_cost": format(transaction_cost, "f"),
        })

    final_portfolio = portfolio.iloc[-1]
    final_account = account.iloc[-1]
    final_positions = positions.loc[positions.index == positions.index.max()]
    rq_quantities = {
        str(row["order_book_id"]): int(row["quantity"])
        for _, row in final_positions.iterrows() if int(row["quantity"]) != 0
    }
    expected_quantities = {symbol: quantity for symbol, quantity in quantities.items() if quantity != 0}
    if rq_quantities != expected_quantities:
        raise RuntimeError(f"position quantity reconciliation failed: {rq_quantities} != {expected_quantities}")
    rq_market_value = _money(final_portfolio["market_value"])
    independent_market_value = sum(
        (_money(row["market_value"]) for _, row in final_positions.iterrows()), Decimal("0")
    ).quantize(_Q)
    differences = {
        "cash": _money(_money(final_portfolio["cash"]) - cash),
        "market_value": _money(rq_market_value - independent_market_value),
        "equity": _money(_money(final_portfolio["total_value"]) - cash - independent_market_value),
        "transaction_cost": _money(
            sum((_money(value) for value in account["transaction_cost"]), Decimal("0")) - total_cost
        ),
    }
    if any(value != 0 for value in differences.values()):
        raise RuntimeError(f"independent RQAlpha reconciliation failed: {differences}")

    normalized = {
        "schema_version": "m3-rqalpha-backtest-result-v1",
        "accepted": True,
        "authoritative": False,
        "simulation_orders_allowed": False,
        "strategy_purpose": "engine_acceptance_fixture_not_performance_evidence",
        "input_sha256": input_sha256,
        "engine": {"name": "rqalpha", "version": "6.2.1", "run_type": "backtest"},
        "trades": normalized_trades,
        "closing_quantities": expected_quantities,
        "closing_cash": format(cash, "f"),
        "closing_market_value": format(independent_market_value, "f"),
        "closing_equity": format(_money(cash + independent_market_value), "f"),
        "transaction_cost": format(total_cost, "f"),
        "reconciliation_differences": {key: format(value, "f") for key, value in differences.items()},
    }
    normalized["result_sha256"] = sha256(normalized)
    return normalized


def _execute_bounded_backtest(
    input_path: Path,
) -> tuple[dict[str, Any], str, tuple[dict[str, Any], ...]]:
    """Run the accepted strategy once and retain end-of-day engine state."""

    value = load_bounded_input(input_path)
    bars = {
        (str(row["symbol"]), date.fromisoformat(str(row["business_date"]))): row
        for row in value.bars
    }
    facts = {
        (str(row["symbol"]), date.fromisoformat(str(row["business_date"]))): row
        for row in value.tradeability
    }
    adapter = RQAlphaAdapter()
    daily_trace: list[dict[str, Any]] = []

    def init(context: Any) -> None:
        context.m3_orders = set()

    def submit(context: Any, symbol: str, side: str, quantity: int) -> None:
        from rqalpha.api import order_shares

        session = context.now.date()
        row = bars[symbol, session]
        fact = facts[symbol, session]
        instruction = OrderInstruction.from_mapping({
            "instruction_id": f"m3.3-{session.isoformat()}-{symbol}-{side}",
            "symbol": symbol, "side": side, "quantity": quantity,
            "price_type": "market", "limit_price": None, "business_date": session.isoformat(),
            "valid_until": session.isoformat(), "strategy_version": STRATEGY_VERSION,
            "data_release_id": _release_for(session),
        })
        position = None
        if side == "sell":
            from rqalpha.const import POSITION_DIRECTION

            rq_position = context.portfolio.get_position(f"{symbol}.XSHG", POSITION_DIRECTION.LONG)
            position = PositionState(
                symbol, int(rq_position.quantity), int(rq_position.closable), _money(rq_position.avg_price)
            )
        market = MarketState(
            symbol=symbol, business_date=session, data_state=DataState.FRESH,
            data_release_id=_release_for(session), last_price=_money(row["close"]),
            previous_close=_money(row["previous_close"]), upper_limit=_money(fact["limit_up"]),
            lower_limit=_money(fact["limit_down"]), suspended=bool(fact["is_suspended"]),
            one_price_limit_up=bool(fact["one_price_limit_up"]),
            one_price_limit_down=bool(fact["one_price_limit_down"]),
        )
        adapter.submit(instruction, market, position, order_function=order_shares)
        context.m3_orders.add(instruction.instruction_id)

    def handle_bar(context: Any, _bars: Any) -> None:
        session = context.now.date()
        if session == ACCEPTANCE_SESSIONS[1]:
            submit(context, "600519", "buy", 100)

    def after_trading(context: Any) -> None:
        from rqalpha.const import POSITION_DIRECTION

        session = context.now.date()
        position = context.portfolio.get_position("600519.XSHG", POSITION_DIRECTION.LONG)
        daily_trace.append({
            "business_date": session.isoformat(),
            "symbol": "600519",
            "total_shares": int(position.quantity),
            "sellable_shares": int(position.closable),
            "average_execution_price": format(_money(position.avg_price), "f"),
        })

    config = {
        "base": {
            "start_date": ACCEPTANCE_SESSIONS[0].isoformat(),
            "end_date": ACCEPTANCE_SESSIONS[-1].isoformat(),
            "frequency": "1d", "run_type": "b", "accounts": {"stock": float(INITIAL_CAPITAL)},
            "auto_update_bundle": False, "capital_gain_tax_rate": 0,
        },
        "extra": {"log_level": "error"},
        "mod": {
            "m2_data": {
                "enabled": True, "lib": "scripts.simulation.rqalpha_m2_mod",
                "input_path": str(input_path.resolve()), "priority": 1,
            },
            "sys_analyser": {"enabled": True, "record": True, "benchmark": None},
            "sys_progress": {"enabled": False},
            "sys_simulation": {
                "enabled": True, "matching_type": "current_bar", "slippage": 0,
                "price_limit": True, "volume_limit": True, "inactive_limit": True,
            },
        },
    }
    result = adapter.run_strategy(
        config=config,
        init=init,
        handle_bar=handle_bar,
        after_trading=after_trading,
    )
    return result, value.input_sha256, tuple(daily_trace)


def execute_bounded_backtest(input_path: Path) -> tuple[dict[str, Any], str]:
    """Run the accepted bounded strategy and retain RQAlpha's in-memory result.

    M3.4 reuses this public project boundary to build the disabled online-ledger
    acceptance package.  The normalized M3.3 output remains produced by
    ``run_bounded_backtest`` so its accepted schema and hash do not change.
    """
    result, input_sha256, _ = _execute_bounded_backtest(input_path)
    return result, input_sha256


def execute_continuity_backtest(
    input_path: Path,
) -> tuple[dict[str, Any], str, tuple[dict[str, Any], ...]]:
    """Expose the same run's daily T+1 state for disabled continuity acceptance."""

    return _execute_bounded_backtest(input_path)


def run_bounded_backtest(input_path: Path) -> dict[str, Any]:
    result, input_sha256 = execute_bounded_backtest(input_path)
    return _normalized_results(result, input_sha256)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run_bounded_backtest(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    print(result)


if __name__ == "__main__":
    main()
