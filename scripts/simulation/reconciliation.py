"""Independent reconciliation of an immutable RQAlpha simulation result."""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal
from typing import Any

from .contracts import DataState, Side, SimulationPackage, canonicalize


ZERO = Decimal("0.0000")


def _difference(left: Decimal, right: Decimal) -> Decimal:
    return (left - right).quantize(Decimal("0.0001"))


def reconcile(package: SimulationPackage) -> dict[str, Any]:
    errors: list[str] = []
    instruction_ids = [row.instruction_id for row in package.instructions]
    order_ids = [row.order_id for row in package.orders]
    fill_ids = [row.fill_id for row in package.fills]
    entry_ids = [row.entry_id for row in package.cash_entries]
    for label, values in (("instruction", instruction_ids), ("order", order_ids), ("fill", fill_ids), ("cash entry", entry_ids)):
        duplicates = sorted(key for key, count in Counter(values).items() if count > 1)
        if duplicates:
            errors.append(f"duplicate {label} ids: {duplicates}")

    instructions = {row.instruction_id: row for row in package.instructions}
    orders = {row.order_id: row for row in package.orders}
    instruction_order_counts: Counter[str] = Counter()
    fills_by_order: dict[str, int] = defaultdict(int)
    for order in package.orders:
        source = instructions.get(order.instruction_id)
        instruction_order_counts[order.instruction_id] += 1
        if source is None:
            errors.append(f"order {order.order_id} references unknown instruction")
        elif (
            order.symbol != source.symbol or order.side is not source.side
            or order.requested_quantity != source.quantity or order.price_type is not source.price_type
            or order.limit_price != source.limit_price
        ):
            errors.append(f"order {order.order_id} differs from its structured instruction")
        if order.status.value == "rejected" and (order.filled_quantity != 0 or not order.reject_reason):
            errors.append(f"rejected order {order.order_id} has a fill or no rejection reason")
        if order.status.value == "filled" and order.filled_quantity != order.requested_quantity:
            errors.append(f"filled order {order.order_id} is not completely filled")
        if order.status.value == "partially_filled" and not 0 < order.filled_quantity < order.requested_quantity:
            errors.append(f"partially filled order {order.order_id} has invalid quantity")
    for instruction_id in instruction_ids:
        if instruction_order_counts[instruction_id] != 1:
            errors.append(f"instruction {instruction_id} must produce exactly one accepted or rejected order")
    for fill in package.fills:
        order = orders.get(fill.order_id)
        if order is None:
            errors.append(f"fill {fill.fill_id} references unknown order")
        elif fill.symbol != order.symbol or fill.side is not order.side:
            errors.append(f"fill {fill.fill_id} differs from its order")
        fills_by_order[fill.order_id] += fill.quantity
        if fill.quantity <= 0 or fill.price <= 0 or min(fill.commission, fill.tax, fill.slippage) < 0:
            errors.append(f"fill {fill.fill_id} has invalid quantity, price or fee")
    for order in package.orders:
        if order.filled_quantity != fills_by_order[order.order_id]:
            errors.append(f"order {order.order_id} filled quantity mismatch")
        if not 0 <= order.filled_quantity <= order.requested_quantity:
            errors.append(f"order {order.order_id} overfilled")

    entries_by_fill = {row.fill_id: row for row in package.cash_entries}
    known_fill_ids = set(fill_ids)
    for entry in package.cash_entries:
        if entry.fill_id not in known_fill_ids:
            errors.append(f"cash entry {entry.entry_id} references unknown fill")
    sequences = [row.sequence_no for row in package.cash_entries]
    if sorted(sequences) != list(range(1, len(sequences) + 1)):
        errors.append("cash entry sequence must be contiguous from one")
    cash_balance = package.snapshot.opening_cash
    for fill in package.fills:
        entry = entries_by_fill.get(fill.fill_id)
        if entry is None:
            errors.append(f"fill {fill.fill_id} has no cash entry")
            continue
        if _difference(entry.amount, fill.cash_effect) != ZERO:
            errors.append(f"fill {fill.fill_id} cash effect mismatch")
    if len(entries_by_fill) != len(package.cash_entries):
        errors.append("multiple cash entries reference the same fill")
    for entry in sorted(package.cash_entries, key=lambda row: row.sequence_no):
        cash_balance = (cash_balance + entry.amount).quantize(Decimal("0.0001"))
        if _difference(entry.balance_after, cash_balance) != ZERO:
            errors.append(f"cash entry {entry.entry_id} running balance mismatch")

    expected_quantities: dict[str, int] = defaultdict(int)
    for position in package.opening_positions:
        expected_quantities[position.symbol] += position.total_shares
    for fill in package.fills:
        expected_quantities[fill.symbol] += fill.quantity if fill.side is Side.BUY else -fill.quantity
    closing = {row.symbol: row for row in package.closing_positions}
    if len(closing) != len(package.closing_positions):
        errors.append("duplicate closing-position symbols")
    for symbol in sorted(set(expected_quantities) | set(closing)):
        actual = closing[symbol].total_shares if symbol in closing else 0
        if actual != expected_quantities[symbol]:
            errors.append(f"position {symbol} quantity mismatch")

    for position in package.closing_positions:
        if (
            position.total_shares < 0 or position.sellable_shares < 0
            or position.sellable_shares > position.total_shares
            or position.average_cost < 0 or position.mark_price < 0
        ):
            errors.append(f"position {position.symbol} has invalid quantity or price")
        expected_value = (Decimal(position.total_shares) * position.mark_price).quantize(Decimal("0.0001"))
        expected_floating = (
            Decimal(position.total_shares) * (position.mark_price - position.average_cost)
        ).quantize(Decimal("0.0001"))
        if _difference(position.market_value, expected_value) != ZERO:
            errors.append(f"position {position.symbol} market value mismatch")
        if _difference(position.floating_pnl, expected_floating) != ZERO:
            errors.append(f"position {position.symbol} floating PnL mismatch")
    market_value = sum((row.market_value for row in package.closing_positions), ZERO)
    floating_pnl = sum((row.floating_pnl for row in package.closing_positions), ZERO)
    total_fees = sum((row.fee_total for row in package.fills), ZERO)
    realized_pnl = package.snapshot.opening_realized_pnl + sum(
        (row.realized_pnl for row in package.fills), ZERO,
    )
    for fill in package.fills:
        if fill.side is Side.BUY and fill.realized_pnl != ZERO:
            errors.append(f"buy fill {fill.fill_id} cannot realize PnL")
    equity = (package.snapshot.cash + package.snapshot.market_value).quantize(Decimal("0.0001"))
    differences = {
        "cash": _difference(package.snapshot.cash, cash_balance),
        "market_value": _difference(package.snapshot.market_value, market_value),
        "equity": _difference(package.snapshot.total_equity, equity),
        "floating_pnl": _difference(package.snapshot.floating_pnl, floating_pnl),
        "fees": _difference(package.snapshot.total_fees, total_fees),
        "realized_pnl": _difference(package.snapshot.realized_pnl, realized_pnl),
        "total_pnl": _difference(
            package.snapshot.total_equity,
            package.snapshot.initial_capital + package.snapshot.realized_pnl + package.snapshot.floating_pnl,
        ),
    }
    for name, value in differences.items():
        if value != ZERO:
            errors.append(f"{name} reconciliation difference is {value}")

    evaluation_counts = Counter(row.symbol for row in package.evaluations if row.evaluated)
    open_symbols = {row.symbol for row in package.closing_positions if row.total_shares > 0}
    if set(evaluation_counts) != open_symbols or any(count != 1 for count in evaluation_counts.values()):
        errors.append("every open position must have exactly one daily evaluation")
    for position in package.closing_positions:
        evaluation = next((row for row in package.evaluations if row.symbol == position.symbol), None)
        if position.data_state is not DataState.FRESH:
            if evaluation is None or evaluation.data_state is not position.data_state or not evaluation.blocked_reason:
                errors.append(f"position {position.symbol} missing-data state is not exposed and blocked")

    if errors:
        raise ValueError("simulation reconciliation failed: " + "; ".join(errors))
    return {
        "accepted": True,
        "differences": canonicalize(differences),
        "open_position_count": len(open_symbols),
        "evaluated_position_count": len(evaluation_counts),
        "order_count": len(package.orders),
        "fill_count": len(package.fills),
        "cash_entry_count": len(package.cash_entries),
    }
