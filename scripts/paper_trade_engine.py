"""Run a conservative virtual stock trading pass from latest live decisions."""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sync_stock_data import SupabaseRest, env_value, read_env_file


INITIAL_CAPITAL = float(os.environ.get("STOCK_PAPER_INITIAL_CAPITAL", "1000000"))
MAX_HOLDINGS = int(os.environ.get("STOCK_PAPER_MAX_HOLDINGS", "6"))
INITIAL_POSITION_RATE = float(os.environ.get("STOCK_PAPER_INITIAL_RATE", "0.08"))
MAX_SINGLE_POSITION_RATE = float(os.environ.get("STOCK_PAPER_MAX_SINGLE_RATE", "0.15"))
CASH_RESERVE_RATE = float(os.environ.get("STOCK_PAPER_CASH_RESERVE_RATE", "0.25"))
RISK_RATE = float(os.environ.get("STOCK_PAPER_RISK_RATE", "0.01"))


ROOT = Path(__file__).resolve().parents[1]


def get_client() -> SupabaseRest:
    env = read_env_file()
    url = env_value("VITE_SUPABASE_URL", env)
    key = env_value("SUPABASE_SERVICE_ROLE_KEY", env)
    return SupabaseRest(url, key)


def number(value: Any, fallback: float = 0) -> float:
    try:
        if value is None or value == "":
            return fallback
        return float(value)
    except (TypeError, ValueError):
        return fallback


def integer(value: Any, fallback: int = 0) -> int:
    return int(number(value, fallback))


def round_lot(shares: float) -> int:
    return max(0, int(shares // 100) * 100)


def initial_position_rate(decision: dict[str, Any]) -> float:
    if "3%试错仓" in str(decision.get("final_action", "")):
        return 0.03
    return INITIAL_POSITION_RATE


def latest_decision_date(client: SupabaseRest) -> str:
    rows = client.request(
        "GET",
        "stock_live_decisions?select=decision_date&order=decision_date.desc&limit=1",
    )
    return str(rows[0]["decision_date"]) if rows else ""


def latest_live_decisions(client: SupabaseRest) -> list[dict[str, Any]]:
    decision_date = latest_decision_date(client)
    if not decision_date:
        return []
    rows = client.request(
        "GET",
        f"stock_live_decisions?decision_date=eq.{decision_date}&select=*&order=updated_at.desc",
    )
    return rows or []


def open_positions(client: SupabaseRest) -> list[dict[str, Any]]:
    rows = client.request(
        "GET",
        "stock_positions?status=eq.open&select=*",
    )
    return rows or []


def trade_history(client: SupabaseRest) -> list[dict[str, Any]]:
    rows = client.request("GET", "stock_trade_history?select=*")
    return rows or []


def today_orders(client: SupabaseRest) -> list[dict[str, Any]]:
    rows = client.request(
        "GET",
        f"stock_auto_trade_orders?order_date=eq.{date.today().isoformat()}&select=*",
    )
    return rows or []


def realized_pnl(trades: list[dict[str, Any]]) -> float:
    return sum(number(item.get("pnl_amount")) for item in trades)


def floating_pnl(positions: list[dict[str, Any]]) -> float:
    return sum(number(item.get("floating_pnl")) for item in positions)


def market_value(positions: list[dict[str, Any]]) -> float:
    return sum(number(item.get("market_value")) for item in positions)


def cost_basis(positions: list[dict[str, Any]]) -> float:
    return sum(number(item.get("cost_price")) * integer(item.get("shares")) for item in positions)


def cash_balance(positions: list[dict[str, Any]], trades: list[dict[str, Any]]) -> float:
    return INITIAL_CAPITAL + realized_pnl(trades) - cost_basis(positions)


def latest_by_code(decisions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in decisions:
        code = str(item.get("code", "")).zfill(6)
        if code and code not in result:
            result[code] = item
    return result


def update_position_price(client: SupabaseRest, position: dict[str, Any], price: float, suggestion: str) -> dict[str, Any]:
    shares = integer(position.get("shares"))
    cost_price = number(position.get("cost_price"))
    market = price * shares
    pnl = (price - cost_price) * shares
    pnl_rate = (price - cost_price) / cost_price * 100 if cost_price > 0 else 0
    payload = {
        "current_price": price,
        "market_value": market,
        "floating_pnl": pnl,
        "pnl_rate": pnl_rate,
        "current_suggestion": suggestion,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    client.request(
        "PATCH",
        f"stock_positions?id=eq.{position['id']}",
        payload,
        prefer="return=minimal",
    )
    return {**position, **payload}


def insert_order(
    client: SupabaseRest,
    decision: dict[str, Any],
    side: str,
    reason: str,
    price: float,
    shares: int,
    cash_before: float,
    cash_after: float,
    before_shares: int,
    after_shares: int,
    pnl: float = 0,
) -> None:
    client.insert("stock_auto_trade_orders", [{
        "order_date": date.today().isoformat(),
        "code": str(decision.get("code", "")).zfill(6),
        "name": decision.get("name", ""),
        "side": side,
        "reason": reason,
        "price": price,
        "shares": shares,
        "amount": price * shares,
        "cash_before": cash_before,
        "cash_after": cash_after,
        "position_shares_before": before_shares,
        "position_shares_after": after_shares,
        "realized_pnl": pnl,
        "status": "filled",
        "source_decision_date": decision.get("decision_date"),
        "source_update_time": decision.get("update_time", ""),
    }])


def buy_position(
    client: SupabaseRest,
    decision: dict[str, Any],
    positions: list[dict[str, Any]],
    trades: list[dict[str, Any]],
) -> dict[str, Any] | None:
    price = number(decision.get("suggest_buy_price")) or number(decision.get("current_price"))
    stop_loss = number(decision.get("stop_loss"))
    if price <= 0 or len(positions) >= MAX_HOLDINGS:
        return None

    cash = cash_balance(positions, trades)
    total_assets = INITIAL_CAPITAL + realized_pnl(trades) + floating_pnl(positions)
    reserve_cash = INITIAL_CAPITAL * CASH_RESERVE_RATE
    available_cash = max(0, cash - reserve_cash)
    target_amount = INITIAL_CAPITAL * initial_position_rate(decision)
    max_single_amount = total_assets * MAX_SINGLE_POSITION_RATE
    risk_per_share = price - stop_loss if stop_loss > 0 and price > stop_loss else price * 0.06
    risk_amount_cap = INITIAL_CAPITAL * RISK_RATE / risk_per_share * price
    allowed_amount = max(0, min(available_cash, target_amount, max_single_amount, risk_amount_cap))
    shares = round_lot(allowed_amount / price)
    if shares <= 0:
        return None

    market = price * shares
    payload = {
        "code": str(decision.get("code", "")).zfill(6),
        "name": decision.get("name", ""),
        "cost_price": price,
        "shares": shares,
        "current_price": price,
        "market_value": market,
        "floating_pnl": 0,
        "pnl_rate": 0,
        "buy_date": date.today().isoformat(),
        "holding_days": 0,
        "current_suggestion": f"自动模拟买入：{decision.get('final_action', '')}",
        "buy_memo": "自动模拟交易引擎",
        "status": "open",
    }
    rows = client.request("POST", "stock_positions", payload, prefer="return=representation")
    cash_after = cash - market
    insert_order(client, decision, "buy", "can_buy signal", price, shares, cash, cash_after, 0, shares)
    return rows[0] if rows else payload


def sell_position(
    client: SupabaseRest,
    decision: dict[str, Any],
    position: dict[str, Any],
    positions: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    reason: str,
    shares_to_sell: int,
) -> dict[str, Any] | None:
    price = number(decision.get("suggest_sell_price")) or number(decision.get("current_price"))
    if price <= 0 or shares_to_sell <= 0:
        return position

    shares = integer(position.get("shares"))
    shares_to_sell = min(shares, round_lot(shares_to_sell))
    if shares_to_sell <= 0:
        return position

    cost_price = number(position.get("cost_price"))
    pnl = (price - cost_price) * shares_to_sell
    cash = cash_balance(positions, trades)
    cash_after = cash + price * shares_to_sell
    next_shares = shares - shares_to_sell
    is_cleared = next_shares <= 0

    client.insert("stock_trade_history", [{
        "code": position.get("code"),
        "name": position.get("name"),
        "buy_date": position.get("buy_date") or date.today().isoformat(),
        "sell_date": date.today().isoformat(),
        "cost_price": cost_price,
        "sell_price": price,
        "shares": shares_to_sell,
        "pnl_amount": pnl,
        "pnl_rate": (price - cost_price) / cost_price * 100 if cost_price > 0 else 0,
        "buy_memo": position.get("buy_memo", ""),
        "sell_memo": f"自动模拟卖出：{reason}",
        "is_cleared": is_cleared,
    }])

    if is_cleared:
        client.request(
            "PATCH",
            f"stock_positions?id=eq.{position['id']}",
            {
                "shares": 0,
                "current_price": price,
                "market_value": 0,
                "floating_pnl": pnl,
                "pnl_rate": (price - cost_price) / cost_price * 100 if cost_price > 0 else 0,
                "current_suggestion": f"自动模拟清仓：{reason}",
                "status": "closed",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            prefer="return=minimal",
        )
        next_position = None
    else:
        next_position = update_position_price(
            client,
            {**position, "shares": next_shares},
            price,
            f"自动模拟减仓：{reason}",
        )
        client.request(
            "PATCH",
            f"stock_positions?id=eq.{position['id']}",
            {"shares": next_shares},
            prefer="return=minimal",
        )

    insert_order(
        client,
        decision,
        "sell",
        reason,
        price,
        shares_to_sell,
        cash,
        cash_after,
        shares,
        next_shares,
        pnl,
    )
    return next_position


def sell_reason(decision: dict[str, Any], position: dict[str, Any]) -> tuple[str, int]:
    price = number(decision.get("current_price"))
    stop_loss = number(decision.get("stop_loss"))
    target = number(decision.get("target_price_1"))
    shares = integer(position.get("shares"))
    if price > 0 and stop_loss > 0 and price <= stop_loss:
        return "触发止损", shares
    if price > 0 and target > 0 and price >= target:
        return "触发第一止盈位", max(100, round_lot(shares / 2))
    status = str(decision.get("status", "")).lower()
    action = str(decision.get("final_action", "")).lower()
    if "sell" in status or "risk" in status or "sell" in action:
        return "live decision sell/risk", shares
    return "", 0


def insert_snapshot(client: SupabaseRest, positions: list[dict[str, Any]], trades: list[dict[str, Any]], trade_count: int) -> None:
    hold_value = market_value(positions)
    realized = realized_pnl(trades)
    floating = floating_pnl(positions)
    total_pnl = realized + floating
    total_assets = INITIAL_CAPITAL + total_pnl
    client.insert("stock_portfolio_snapshots", [{
        "snapshot_date": date.today().isoformat(),
        "cash": total_assets - hold_value,
        "holding_market_value": hold_value,
        "total_assets": total_assets,
        "realized_pnl": realized,
        "floating_pnl": floating,
        "total_pnl": total_pnl,
        "total_return_rate": total_pnl / INITIAL_CAPITAL * 100 if INITIAL_CAPITAL > 0 else 0,
        "position_count": len(positions),
        "trade_count": trade_count,
        "note": "auto paper trading snapshot",
    }])


def run(dry_run: bool = False) -> dict[str, int]:
    client = get_client()
    decisions = latest_live_decisions(client)
    positions = open_positions(client)
    trades = trade_history(client)
    try:
        existing_orders = today_orders(client)
    except RuntimeError as reason:
        if "stock_auto_trade_orders" in str(reason):
            print("Paper trading tables are not ready; apply the Supabase migration first.", flush=True)
            return {"decisions": len(decisions), "positions": len(positions), "orders": 0, "snapshots": 0}
        raise
    ordered_keys = {
        (str(item.get("code", "")).zfill(6), str(item.get("side", "")))
        for item in existing_orders
    }
    decisions_by_code = latest_by_code(decisions)
    position_by_code = {str(item.get("code", "")).zfill(6): item for item in positions}
    trade_count = 0

    for position in list(positions):
        code = str(position.get("code", "")).zfill(6)
        decision = decisions_by_code.get(code)
        if not decision:
            continue
        updated = update_position_price(
            client,
            position,
            number(decision.get("current_price"), number(position.get("current_price"))),
            str(decision.get("final_action") or position.get("current_suggestion") or ""),
        )
        position_by_code[code] = updated

    positions = [item for item in position_by_code.values() if integer(item.get("shares")) > 0]

    for position in list(positions):
        code = str(position.get("code", "")).zfill(6)
        decision = decisions_by_code.get(code)
        if not decision or (code, "sell") in ordered_keys:
            continue
        reason, shares = sell_reason(decision, position)
        if not reason:
            continue
        if not dry_run:
            next_position = sell_position(client, decision, position, positions, trades, reason, shares)
            trade_count += 1
            trades = trade_history(client)
            if next_position:
                position_by_code[code] = next_position
            else:
                position_by_code.pop(code, None)

    positions = [item for item in position_by_code.values() if integer(item.get("shares")) > 0]

    buy_candidates = [
        item for item in decisions
        if bool(item.get("can_buy")) and str(item.get("code", "")).zfill(6) not in position_by_code
    ]
    for decision in buy_candidates:
        code = str(decision.get("code", "")).zfill(6)
        if len(position_by_code) >= MAX_HOLDINGS or (code, "buy") in ordered_keys:
            continue
        if not dry_run:
            bought = buy_position(client, decision, list(position_by_code.values()), trades)
            if bought:
                position_by_code[code] = bought
                trade_count += 1

    final_positions = [item for item in position_by_code.values() if integer(item.get("shares")) > 0]
    final_trades = trade_history(client)
    if not dry_run:
        insert_snapshot(client, final_positions, final_trades, trade_count)
    return {
        "decisions": len(decisions),
        "positions": len(final_positions),
        "orders": trade_count,
        "snapshots": 0 if dry_run else 1,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(dry_run=args.dry_run), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
