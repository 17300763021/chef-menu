"""Run the P4 model-driven virtual trading account."""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import quote

from legacy_account_freeze import LEGACY_ACCOUNT_FREEZE_REASON, LEGACY_ACCOUNT_FROZEN
from model_prediction_engine import MODEL_NAME, MODEL_VERSION
from sync_stock_data import SupabaseRest, env_value, read_env_file


STRATEGY_ACCOUNT = os.environ.get("STOCK_MODEL_ACCOUNT", "model_qlib_lgbm_v1")
INITIAL_CAPITAL = float(os.environ.get("STOCK_MODEL_INITIAL_CAPITAL", "1000000"))
MAX_HOLDINGS = int(os.environ.get("STOCK_MODEL_MAX_HOLDINGS", "6"))
MAX_SINGLE_POSITION_RATE = float(os.environ.get("STOCK_MODEL_MAX_SINGLE_RATE", "0.15"))
POSITION_RATE = float(os.environ.get("STOCK_MODEL_POSITION_RATE", "0.08"))
CASH_RESERVE_RATE = float(os.environ.get("STOCK_MODEL_CASH_RESERVE_RATE", "0.25"))
BUY_RANK_LIMIT = int(os.environ.get("STOCK_MODEL_BUY_RANK_LIMIT", "10"))
SELL_RANK_LIMIT = int(os.environ.get("STOCK_MODEL_SELL_RANK_LIMIT", "35"))
MIN_CONFIDENCE = float(os.environ.get("STOCK_MODEL_MIN_CONFIDENCE", "0.35"))
STOP_LOSS_RATE = float(os.environ.get("STOCK_MODEL_STOP_LOSS_RATE", "0.06"))
DEFAULT_SLIPPAGE_RATE = float(os.environ.get("STOCK_PAPER_SLIPPAGE_RATE", "0.001"))
COMMISSION_RATE = float(os.environ.get("STOCK_PAPER_COMMISSION_RATE", "0.0003"))
MIN_COMMISSION = float(os.environ.get("STOCK_PAPER_MIN_COMMISSION", "5"))
STAMP_DUTY_RATE = float(os.environ.get("STOCK_PAPER_STAMP_DUTY_RATE", "0.0005"))
TRANSFER_FEE_RATE = float(os.environ.get("STOCK_PAPER_TRANSFER_FEE_RATE", "0.00001"))


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


def integer(value: Any, fallback: int = 0) -> int:
    return int(number(value, fallback))


def round_lot(shares: float) -> int:
    return max(0, int(shares // 100) * 100)


def execution_price(side: str, price: float) -> float:
    direction = 1 if side == "buy" else -1
    return round(price * (1 + direction * DEFAULT_SLIPPAGE_RATE), 4) if price > 0 else 0


def trading_fee(side: str, gross_amount: float) -> float:
    if gross_amount <= 0:
        return 0
    commission = max(MIN_COMMISSION, gross_amount * COMMISSION_RATE)
    stamp_duty = gross_amount * STAMP_DUTY_RATE if side == "sell" else 0
    transfer_fee = gross_amount * TRANSFER_FEE_RATE
    return round(commission + stamp_duty + transfer_fee, 2)


def slippage_amount(raw_price: float, executed_price: float, shares: int) -> float:
    return round(abs(executed_price - raw_price) * shares, 2)


def latest_prediction_date(client: SupabaseRest) -> str:
    rows = client.request(
        "GET",
        "stock_model_predictions"
        f"?model_name=eq.{quote(MODEL_NAME)}"
        f"&model_version=eq.{quote(MODEL_VERSION)}"
        "&select=prediction_date"
        "&order=prediction_date.desc"
        "&limit=1",
    ) or []
    return str(rows[0]["prediction_date"]) if rows else ""


def latest_predictions(client: SupabaseRest) -> list[dict[str, Any]]:
    prediction_date = latest_prediction_date(client)
    if not prediction_date:
        return []
    rows = client.request(
        "GET",
        "stock_model_predictions"
        f"?prediction_date=eq.{quote(prediction_date)}"
        f"&model_name=eq.{quote(MODEL_NAME)}"
        f"&model_version=eq.{quote(MODEL_VERSION)}"
        "&select=*"
        "&order=rank.asc",
    ) or []
    return rows


def open_positions(client: SupabaseRest) -> list[dict[str, Any]]:
    return client.request(
        "GET",
        "stock_model_positions"
        f"?strategy_account=eq.{quote(STRATEGY_ACCOUNT)}"
        "&status=eq.open"
        "&select=*",
    ) or []


def trade_history(client: SupabaseRest) -> list[dict[str, Any]]:
    return client.request(
        "GET",
        "stock_model_trade_history"
        f"?strategy_account=eq.{quote(STRATEGY_ACCOUNT)}"
        "&select=*",
    ) or []


def today_orders(client: SupabaseRest, today: str) -> list[dict[str, Any]]:
    return client.request(
        "GET",
        "stock_model_orders"
        f"?strategy_account=eq.{quote(STRATEGY_ACCOUNT)}"
        f"&order_date=eq.{quote(today)}"
        "&select=*",
    ) or []


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


def is_limit_up(prediction: dict[str, Any]) -> bool:
    payload = prediction.get("feature_payload") if isinstance(prediction.get("feature_payload"), dict) else {}
    return number(payload.get("return_5d")) >= 35 or number(prediction.get("predicted_return")) > 6


def is_limit_down(prediction: dict[str, Any]) -> bool:
    payload = prediction.get("feature_payload") if isinstance(prediction.get("feature_payload"), dict) else {}
    return number(payload.get("return_5d")) <= -35 or number(prediction.get("predicted_return")) < -6


def update_position_mark(client: SupabaseRest, position: dict[str, Any], prediction: dict[str, Any], suggestion: str) -> dict[str, Any]:
    price = number(prediction.get("close_price"), number(position.get("current_price")))
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
    client.request("PATCH", f"stock_model_positions?id=eq.{quote(str(position['id']))}", payload, prefer="return=minimal")
    return {**position, **payload}


def make_decision(prediction: dict[str, Any], position: dict[str, Any] | None) -> dict[str, Any]:
    rank = integer(prediction.get("rank"))
    confidence = number(prediction.get("confidence"))
    predicted_return = number(prediction.get("predicted_return"))
    price = number(prediction.get("close_price"))
    if confidence < MIN_CONFIDENCE:
        return {
            "action": "blocked",
            "reason": "model confidence below threshold",
            "risk_gate_status": "blocked",
            "risk_gate_reason": f"confidence {confidence:.2f} < {MIN_CONFIDENCE:.2f}",
            "target_weight": 0,
            "planned_shares": 0,
        }
    if position:
        cost_price = number(position.get("cost_price"))
        pnl_rate = (price - cost_price) / cost_price if price > 0 and cost_price > 0 else 0
        if pnl_rate <= -STOP_LOSS_RATE:
            return {
                "action": "sell",
                "reason": "model account stop loss",
                "risk_gate_status": "passed",
                "risk_gate_reason": "",
                "target_weight": 0,
                "planned_shares": integer(position.get("shares")),
            }
        if rank > SELL_RANK_LIMIT or predicted_return < 0:
            return {
                "action": "sell",
                "reason": "model rank deteriorated",
                "risk_gate_status": "passed",
                "risk_gate_reason": "",
                "target_weight": 0,
                "planned_shares": integer(position.get("shares")),
            }
        return {
            "action": "hold",
            "reason": "model remains constructive",
            "risk_gate_status": "passed",
            "risk_gate_reason": "",
            "target_weight": POSITION_RATE,
            "planned_shares": 0,
        }
    if rank <= BUY_RANK_LIMIT and predicted_return > 0:
        return {
            "action": "buy",
            "reason": "top-ranked positive model prediction",
            "risk_gate_status": "passed",
            "risk_gate_reason": "",
            "target_weight": POSITION_RATE,
            "planned_shares": 0,
        }
    return {
        "action": "watch",
        "reason": "not in model buy range",
        "risk_gate_status": "passed",
        "risk_gate_reason": "",
        "target_weight": 0,
        "planned_shares": 0,
    }


def insert_decision(client: SupabaseRest, prediction: dict[str, Any], decision: dict[str, Any], status: str = "new") -> str:
    payload = {
        "decision_date": str(prediction.get("prediction_date") or date.today().isoformat()),
        "prediction_id": prediction.get("id"),
        "strategy_account": STRATEGY_ACCOUNT,
        "code": str(prediction.get("code", "")).zfill(6),
        "name": prediction.get("name", ""),
        "model_name": prediction.get("model_name", MODEL_NAME),
        "model_version": prediction.get("model_version", MODEL_VERSION),
        "action": decision["action"],
        "reason": decision["reason"],
        "risk_gate_status": decision["risk_gate_status"],
        "risk_gate_reason": decision["risk_gate_reason"],
        "target_weight": decision["target_weight"],
        "planned_shares": decision["planned_shares"],
        "status": status,
    }
    rows = client.request("POST", "stock_model_decisions", payload, prefer="return=representation") or []
    return str(rows[0].get("id", "")) if rows else ""


def insert_order(
    client: SupabaseRest,
    prediction: dict[str, Any],
    decision_id: str,
    side: str,
    reason: str,
    price: float,
    shares: int,
    cash_before: float,
    cash_after: float,
    before_shares: int,
    after_shares: int,
    realized: float = 0,
    status: str = "filled",
    failure_reason: str = "",
) -> str:
    amount = price * shares
    payload = {
        "order_date": str(prediction.get("prediction_date") or date.today().isoformat()),
        "strategy_account": STRATEGY_ACCOUNT,
        "decision_id": decision_id or None,
        "prediction_id": prediction.get("id"),
        "code": str(prediction.get("code", "")).zfill(6),
        "name": prediction.get("name", ""),
        "side": side,
        "reason": reason,
        "price": price,
        "shares": shares,
        "amount": amount,
        "fee_amount": trading_fee(side, amount) if shares > 0 else 0,
        "slippage_amount": slippage_amount(number(prediction.get("close_price")), price, shares) if shares > 0 else 0,
        "cash_before": cash_before,
        "cash_after": cash_after,
        "position_shares_before": before_shares,
        "position_shares_after": after_shares,
        "realized_pnl": realized,
        "status": status,
        "failure_reason": failure_reason,
        "model_name": MODEL_NAME,
        "model_version": MODEL_VERSION,
    }
    rows = client.request("POST", "stock_model_orders", payload, prefer="return=representation") or []
    return str(rows[0].get("id", "")) if rows else ""


def buy_position(
    client: SupabaseRest,
    prediction: dict[str, Any],
    positions: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    decision_id: str,
) -> dict[str, Any] | None:
    if len(positions) >= MAX_HOLDINGS:
        insert_order(client, prediction, decision_id, "buy", "blocked buy", 0, 0, 0, 0, 0, 0, status="blocked", failure_reason="max holdings reached")
        return None
    if is_limit_up(prediction):
        insert_order(client, prediction, decision_id, "buy", "blocked buy", 0, 0, 0, 0, 0, 0, status="blocked", failure_reason="limit-up buy blocked")
        return None
    cash = cash_balance(positions, trades)
    total_assets = INITIAL_CAPITAL + realized_pnl(trades) + floating_pnl(positions)
    available_cash = max(0, cash - INITIAL_CAPITAL * CASH_RESERVE_RATE)
    target_amount = min(INITIAL_CAPITAL * POSITION_RATE, total_assets * MAX_SINGLE_POSITION_RATE, available_cash)
    raw_price = number(prediction.get("close_price"))
    price = execution_price("buy", raw_price)
    shares = round_lot(target_amount / price) if price > 0 else 0
    if shares <= 0:
        insert_order(client, prediction, decision_id, "buy", "blocked buy", price, 0, cash, cash, 0, 0, status="blocked", failure_reason="insufficient cash for 100-share lot")
        return None
    amount = price * shares
    fee = trading_fee("buy", amount)
    payload = {
        "strategy_account": STRATEGY_ACCOUNT,
        "code": str(prediction.get("code", "")).zfill(6),
        "name": prediction.get("name", ""),
        "cost_price": price,
        "shares": shares,
        "current_price": price,
        "market_value": amount,
        "floating_pnl": 0,
        "pnl_rate": 0,
        "buy_date": str(prediction.get("prediction_date") or date.today().isoformat()),
        "current_suggestion": "model virtual buy",
        "status": "open",
        "model_name": MODEL_NAME,
        "model_version": MODEL_VERSION,
    }
    rows = client.request("POST", "stock_model_positions", payload, prefer="return=representation") or []
    insert_order(client, prediction, decision_id, "buy", "top-ranked positive model prediction", price, shares, cash, cash - amount - fee, 0, shares)
    return rows[0] if rows else payload


def sell_position(
    client: SupabaseRest,
    prediction: dict[str, Any],
    position: dict[str, Any],
    positions: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    decision_id: str,
    reason: str,
) -> None:
    shares = integer(position.get("shares"))
    raw_price = number(prediction.get("close_price"))
    if str(position.get("buy_date")) == str(prediction.get("prediction_date")):
        insert_order(client, prediction, decision_id, "sell", reason, raw_price, 0, 0, 0, shares, shares, status="blocked", failure_reason="T+1 same-day sell blocked")
        return
    if is_limit_down(prediction):
        insert_order(client, prediction, decision_id, "sell", reason, raw_price, 0, 0, 0, shares, shares, status="blocked", failure_reason="limit-down sell blocked")
        return
    shares_to_sell = round_lot(shares)
    if shares_to_sell <= 0:
        insert_order(client, prediction, decision_id, "sell", reason, raw_price, 0, 0, 0, shares, shares, status="blocked", failure_reason="less than 100 shares sellable")
        return
    price = execution_price("sell", raw_price)
    gross = price * shares_to_sell
    fee = trading_fee("sell", gross)
    cost_price = number(position.get("cost_price"))
    realized = (price - cost_price) * shares_to_sell - fee
    cash = cash_balance(positions, trades)
    client.request("PATCH", f"stock_model_positions?id=eq.{quote(str(position['id']))}", {
        "shares": 0,
        "current_price": price,
        "market_value": 0,
        "floating_pnl": realized,
        "pnl_rate": realized / (cost_price * shares_to_sell) * 100 if cost_price > 0 else 0,
        "current_suggestion": f"model virtual sell: {reason}",
        "status": "closed",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, prefer="return=minimal")
    client.request("POST", "stock_model_trade_history", {
        "strategy_account": STRATEGY_ACCOUNT,
        "code": position.get("code"),
        "name": position.get("name", ""),
        "buy_date": position.get("buy_date"),
        "sell_date": str(prediction.get("prediction_date") or date.today().isoformat()),
        "cost_price": cost_price,
        "sell_price": price,
        "shares": shares_to_sell,
        "pnl_amount": realized,
        "pnl_rate": realized / (cost_price * shares_to_sell) * 100 if cost_price > 0 else 0,
        "fee_amount": fee,
        "slippage_amount": slippage_amount(raw_price, price, shares_to_sell),
        "buy_memo": "model virtual account",
        "sell_memo": reason,
        "is_cleared": True,
        "model_name": MODEL_NAME,
        "model_version": MODEL_VERSION,
    }, prefer="return=minimal")
    insert_order(client, prediction, decision_id, "sell", reason, price, shares_to_sell, cash, cash + gross - fee, shares, 0, realized)


def insert_snapshot(client: SupabaseRest, positions: list[dict[str, Any]], trades: list[dict[str, Any]], trade_count: int) -> None:
    hold_value = market_value(positions)
    realized = realized_pnl(trades)
    floating = floating_pnl(positions)
    total_pnl = realized + floating
    losses = [number(item.get("pnl_amount")) for item in trades if number(item.get("pnl_amount")) < 0]
    client.request("POST", "stock_model_portfolio_snapshots", {
        "snapshot_date": date.today().isoformat(),
        "strategy_account": STRATEGY_ACCOUNT,
        "cash": INITIAL_CAPITAL + realized - cost_basis(positions),
        "holding_market_value": hold_value,
        "total_assets": INITIAL_CAPITAL + total_pnl,
        "realized_pnl": realized,
        "floating_pnl": floating,
        "total_pnl": total_pnl,
        "total_return_rate": total_pnl / INITIAL_CAPITAL * 100 if INITIAL_CAPITAL else 0,
        "max_drawdown_rate": 0,
        "consecutive_losses": len(losses),
        "position_count": len(positions),
        "trade_count": trade_count,
        "model_name": MODEL_NAME,
        "model_version": MODEL_VERSION,
        "note": "model virtual account snapshot; simulation only",
    }, prefer="return=minimal")


def run(dry_run: bool = False) -> dict[str, int]:
    if LEGACY_ACCOUNT_FROZEN and not dry_run:
        print(f"[LegacyAccountFrozen] {LEGACY_ACCOUNT_FREEZE_REASON}", flush=True)
        return {"predictions": 0, "decisions": 0, "orders": 0, "snapshots": 0}
    client = get_client()
    predictions = latest_predictions(client)
    if not predictions:
        return {"predictions": 0, "decisions": 0, "orders": 0, "snapshots": 0}
    prediction_date = str(predictions[0].get("prediction_date") or date.today().isoformat())
    positions = open_positions(client)
    trades = trade_history(client)
    existing_orders = today_orders(client, prediction_date)
    ordered = {(str(item.get("code", "")).zfill(6), str(item.get("side"))) for item in existing_orders}
    positions_by_code = {str(item.get("code", "")).zfill(6): item for item in positions}
    decisions = 0
    orders = 0

    for position in list(positions):
        prediction = next((item for item in predictions if str(item.get("code", "")).zfill(6) == str(position.get("code", "")).zfill(6)), None)
        if prediction:
            positions_by_code[str(position.get("code", "")).zfill(6)] = update_position_mark(client, position, prediction, "model mark-to-market")

    positions = list(positions_by_code.values())
    for prediction in predictions:
        code = str(prediction.get("code", "")).zfill(6)
        position = positions_by_code.get(code)
        decision = make_decision(prediction, position)
        if decision["action"] not in {"buy", "sell", "blocked", "hold", "watch"}:
            continue
        if dry_run:
            decisions += 1
            continue
        decision_id = insert_decision(client, prediction, decision, "handled")
        decisions += 1
        if decision["action"] == "buy" and not position and (code, "buy") not in ordered:
            bought = buy_position(client, prediction, list(positions_by_code.values()), trades, decision_id)
            if bought:
                positions_by_code[code] = bought
                orders += 1
        elif decision["action"] == "sell" and position and (code, "sell") not in ordered:
            sell_position(client, prediction, position, list(positions_by_code.values()), trades, decision_id, decision["reason"])
            positions_by_code.pop(code, None)
            trades = trade_history(client)
            orders += 1
        elif decision["action"] == "blocked":
            insert_order(client, prediction, decision_id, "buy", decision["reason"], 0, 0, 0, 0, 0, 0, status="blocked", failure_reason=decision["risk_gate_reason"])

    final_positions = open_positions(client)
    final_trades = trade_history(client)
    if not dry_run:
        insert_snapshot(client, final_positions, final_trades, orders)
    return {"predictions": len(predictions), "decisions": decisions, "orders": orders, "snapshots": 0 if dry_run else 1}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(dry_run=args.dry_run), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
