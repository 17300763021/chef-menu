"""Run a conservative virtual stock trading pass from latest live decisions."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from market_regime import classify_market_regime
from sync_stock_data import SupabaseRest, env_value, read_env_file


INITIAL_CAPITAL = float(os.environ.get("STOCK_PAPER_INITIAL_CAPITAL", "1000000"))
MAX_HOLDINGS = int(os.environ.get("STOCK_PAPER_MAX_HOLDINGS", "6"))
MAX_HOLD_DAYS = int(os.environ.get("STOCK_PAPER_MAX_HOLD_DAYS", "10"))
INITIAL_POSITION_RATE = float(os.environ.get("STOCK_PAPER_INITIAL_RATE", "0.08"))
MAX_SINGLE_POSITION_RATE = float(os.environ.get("STOCK_PAPER_MAX_SINGLE_RATE", "0.15"))
CASH_RESERVE_RATE = float(os.environ.get("STOCK_PAPER_CASH_RESERVE_RATE", "0.25"))
RISK_RATE = float(os.environ.get("STOCK_PAPER_RISK_RATE", "0.01"))
TRAILING_STOP_RATE = float(os.environ.get("STOCK_PAPER_TRAILING_STOP_RATE", "0.93"))
LEGACY_ENTRY_STOP_RATE = float(os.environ.get("STOCK_PAPER_LEGACY_ENTRY_STOP_RATE", "0.94"))
PRESSURE_PROFIT_RATE = float(os.environ.get("STOCK_PAPER_PRESSURE_PROFIT_RATE", "10"))
STAGNATION_PROFIT_RATE = float(os.environ.get("STOCK_PAPER_STAGNATION_PROFIT_RATE", "15"))
HIGH_PROFIT_PROTECTION_RATE = float(os.environ.get("STOCK_PAPER_HIGH_PROFIT_RATE", "25"))
DEFAULT_SLIPPAGE_RATE = float(os.environ.get("STOCK_PAPER_SLIPPAGE_RATE", "0.001"))
COMMISSION_RATE = float(os.environ.get("STOCK_PAPER_COMMISSION_RATE", "0.0003"))
MIN_COMMISSION = float(os.environ.get("STOCK_PAPER_MIN_COMMISSION", "5"))
STAMP_DUTY_RATE = float(os.environ.get("STOCK_PAPER_STAMP_DUTY_RATE", "0.0005"))
TRANSFER_FEE_RATE = float(os.environ.get("STOCK_PAPER_TRANSFER_FEE_RATE", "0.00001"))
REGIME_MAX_HOLDINGS = {
    "强牛市": 8,
    "弱牛市": 6,
    "震荡市": 4,
    "熊市": 2,
    "防御": 1,
}


ROOT = Path(__file__).resolve().parents[1]
STOCK_ENGINE = ROOT / "scripts" / "stock_engine"
if str(STOCK_ENGINE) not in sys.path:
    sys.path.insert(0, str(STOCK_ENGINE))

from a_stock_trade_common_v7 import sector_momentum_ranking  # noqa: E402


@dataclass(frozen=True)
class SellDecision:
    reason: str
    shares: int
    next_sell_stage: str
    trailing_stop_price: float | None = None
    last_profit_taking_price: float | None = None
    execution_status: str = "auto_executed"


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


def position_sell_stage(position: dict[str, Any]) -> str:
    stage = str(position.get("sell_stage") or "none").strip()
    return stage if stage else "none"


def target_2r(position: dict[str, Any], target_1r: float) -> float:
    cost_price = number(position.get("cost_price"))
    if cost_price <= 0 or target_1r <= cost_price:
        return 0
    return cost_price + 2 * (target_1r - cost_price)


def entry_stop_loss(position: dict[str, Any]) -> float:
    explicit_stop = number(position.get("entry_stop_loss"))
    if explicit_stop > 0:
        return explicit_stop
    cost_price = number(position.get("cost_price"))
    return round(cost_price * LEGACY_ENTRY_STOP_RATE, 3) if cost_price > 0 else 0


def is_strong_limit_up(decision: dict[str, Any]) -> bool:
    """Only use numeric change rate to identify limit-up state."""
    return number(decision.get("change_rate")) >= 9.7


def is_limit_down(decision: dict[str, Any]) -> bool:
    """Only use numeric change rate to identify limit-down state."""
    return number(decision.get("change_rate")) <= -9.7


def is_suspended(decision: dict[str, Any]) -> bool:
    """Only inspect operation_type for explicit suspension markers."""
    op = str(decision.get("operation_type", ""))
    return "停牌" in op or "suspended" in op.lower()


def execution_price(side: str, price: float) -> float:
    if price <= 0:
        return price
    direction = 1 if side == "buy" else -1
    return round(price * (1 + direction * DEFAULT_SLIPPAGE_RATE), 4)


def trading_fee(side: str, gross_amount: float) -> float:
    if gross_amount <= 0:
        return 0
    commission = max(MIN_COMMISSION, gross_amount * COMMISSION_RATE)
    stamp_duty = gross_amount * STAMP_DUTY_RATE if side == "sell" else 0
    transfer_fee = gross_amount * TRANSFER_FEE_RATE
    return round(commission + stamp_duty + transfer_fee, 2)


def slippage_amount(raw_price: float, executed_price: float, shares: int) -> float:
    return round(abs(executed_price - raw_price) * shares, 2)


def next_trailing_stop(decision: dict[str, Any], position: dict[str, Any]) -> float:
    price = number(decision.get("current_price"))
    existing = number(position.get("trailing_stop_price"))
    raised = round(price * TRAILING_STOP_RATE, 2) if price > 0 else 0
    return max(existing, raised)


def initial_position_rate(decision: dict[str, Any]) -> float:
    if "3%试错仓" in str(decision.get("final_action", "")):
        return 0.03
    return INITIAL_POSITION_RATE


def profit_rate(price: float, position: dict[str, Any]) -> float:
    cost_price = number(position.get("cost_price"))
    if price <= 0 or cost_price <= 0:
        return 0
    return (price - cost_price) / cost_price * 100


def holding_days(position: dict[str, Any], today: date | None = None) -> int:
    buy_date_text = str(position.get("buy_date") or "")
    if not buy_date_text:
        return 0
    try:
        buy_date = date.fromisoformat(buy_date_text)
    except ValueError:
        return 0
    return max(0, ((today or date.today()) - buy_date).days)


def decision_text(decision: dict[str, Any]) -> str:
    keys = ("status", "final_action", "reason", "risk", "sell_reason", "buy_reason")
    return " ".join(str(decision.get(key, "")) for key in keys)


def has_any_text(text_value: str, patterns: tuple[str, ...]) -> bool:
    lowered = text_value.lower()
    return any(pattern in text_value or pattern.lower() in lowered for pattern in patterns)


def is_near_pressure(decision: dict[str, Any]) -> bool:
    return has_any_text(
        decision_text(decision),
        ("临近压力", "接近压力", "压力", "上方抛压", "冲高回落", "上影线"),
    )


def is_heavy_volume_stagnation(decision: dict[str, Any]) -> bool:
    return has_any_text(
        decision_text(decision),
        ("放量滞涨", "量价背离", "放量不涨", "冲高回落", "上影线", "封板松动", "炸板"),
    )


def is_consecutive_limit_up(decision: dict[str, Any]) -> bool:
    limit_up_days = integer(
        decision.get("limit_up_days")
        or decision.get("consecutive_limit_up_days")
        or decision.get("board_count")
        or decision.get("limit_up_count")
    )
    return limit_up_days >= 2 or has_any_text(
        decision_text(decision),
        ("连续涨停", "连板", "二连板", "三连板", "多连板", "封单强"),
    )


def is_heavy_volume_board_break(decision: dict[str, Any]) -> bool:
    return has_any_text(
        decision_text(decision),
        ("放量炸板", "爆量炸板", "炸板", "封板松动", "打开涨停", "涨停打开"),
    )


def is_failed_reseal(decision: dict[str, Any]) -> bool:
    return has_any_text(
        decision_text(decision),
        ("回封失败", "未能回封", "封板失败", "炸板后回封失败", "资金承接转弱"),
    )


def partial_sell_shares(shares: int, rate: float) -> int:
    return min(shares, max(100, round_lot(shares * rate)))


def next_profit_stage(stage: str) -> str:
    if stage == "none":
        return "sold_1r"
    if stage == "sold_1r":
        return "sold_2r"
    return stage


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


def latest_model_predictions(client: SupabaseRest) -> dict[str, dict[str, Any]]:
    latest = client.request(
        "GET",
        "stock_model_predictions?select=prediction_date&order=prediction_date.desc&limit=1",
    ) or []
    if not latest:
        return {}
    prediction_date = str(latest[0].get("prediction_date") or "")
    rows = client.request(
        "GET",
        f"stock_model_predictions?prediction_date=eq.{quote(prediction_date)}&select=code,score,rank,predicted_return,confidence",
    ) or []
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = str(row.get("code", "")).zfill(6)
        if code:
            out[code] = row
    return out


def normalized_model_rank(rank: float) -> float:
    if rank <= 0:
        return 0
    return max(0, min(100, 101 - rank))


def enrich_decisions_with_model_predictions(
    decisions: list[dict[str, Any]],
    predictions: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    enriched = []
    for decision in decisions:
        item = dict(decision)
        code = str(item.get("code", "")).zfill(6)
        prediction = predictions.get(code, {})
        model_score = number(prediction.get("score"))
        model_rank = integer(prediction.get("rank"))
        multi_factor_score = number(
            item.get("multi_factor_score")
            or item.get("score")
            or item.get("ranking_score")
            or item.get("factor_score")
        )
        item["model_score"] = model_score
        item["model_rank"] = model_rank
        item["multi_factor_score"] = multi_factor_score
        item["combined_score"] = normalized_model_rank(model_rank) * 0.4 + multi_factor_score * 0.6
        enriched.append(item)
    return enriched


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


def portfolio_snapshots(client: SupabaseRest) -> list[dict[str, Any]]:
    rows = client.request(
        "GET",
        "stock_portfolio_snapshots?select=total_assets,snapshot_date,snapshot_time&order=snapshot_date.desc&limit=120",
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


def account_drawdown_pct(snapshots: list[dict[str, Any]], current_total_assets: float) -> float:
    values = [number(item.get("total_assets")) for item in snapshots]
    values = [value for value in values if value > 0]
    if current_total_assets <= 0 or not values:
        return 0
    peak = max(max(values), current_total_assets)
    return max(0, (peak - current_total_assets) / peak * 100) if peak > 0 else 0


def consecutive_loss_count(trades: list[dict[str, Any]]) -> int:
    ordered = sorted(
        trades,
        key=lambda item: str(item.get("sell_date") or item.get("trade_date") or item.get("created_at") or ""),
        reverse=True,
    )
    losses = 0
    for trade in ordered:
        pnl = number(trade.get("pnl_amount") or trade.get("realized_pnl"))
        if pnl < 0:
            losses += 1
            continue
        if pnl > 0:
            break
    return losses


def decision_signal_strength(decision: dict[str, Any]) -> float:
    score = number(
        decision.get("multi_factor_score")
        or decision.get("combined_score")
        or decision.get("score"),
        100,
    )
    return max(0, min(1, score / 100))


def adaptive_position_size(
    base_amount: float,
    signal_strength: float,
    market_regime: str,
    account_drawdown_pct: float,
    consecutive_losses: int,
    stock_volatility_pct: float,
) -> float:
    amount = max(0, base_amount)
    if signal_strength > 0:
        amount *= max(0.5, min(signal_strength, 1.0))

    regime_position_cap = {
        "强牛市": 0.80,
        "弱牛市": 0.60,
        "震荡市": 0.40,
        "熊市": 0.20,
        "防御": 0.10,
    }
    cap = regime_position_cap.get(market_regime, 0.40)
    amount = min(amount, base_amount * cap)

    if account_drawdown_pct > 10:
        amount *= 0.5
    elif account_drawdown_pct > 5:
        amount *= 0.7

    if consecutive_losses >= 5:
        amount *= 0.25
    elif consecutive_losses >= 3:
        amount *= 0.5

    if stock_volatility_pct > 60:
        amount *= 0.7

    return round(max(amount, base_amount * 0.25), 2)


def latest_by_code(decisions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in decisions:
        code = str(item.get("code", "")).zfill(6)
        if code and code not in result:
            result[code] = item
    return result


def decision_momentum_score(decision: dict[str, Any]) -> float:
    factor_scores = decision.get("factor_scores")
    if isinstance(factor_scores, dict):
        value = number(factor_scores.get("momentum"), -1)
        if value >= 0:
            return value
    return number(
        decision.get("factor_momentum")
        or decision.get("因子动量")
        or decision.get("momentum_score"),
        0,
    )


def extract_sector_name(item: dict[str, Any]) -> str:
    for key in (
        "shenwan_industry_l1",
        "industry",
        "sector",
        "所属板块",
        "行业",
        "板块",
    ):
        value = str(item.get(key) or "").strip()
        if value and value.lower() not in {"none", "nan"}:
            return value
    return ""


def fetch_sector_name(client: SupabaseRest, code: str) -> str:
    rows = client.request(
        "GET",
        f"stock_sector_mapping?code=eq.{quote(code)}&select=shenwan_industry_l1&limit=1",
    ) or []
    if not rows:
        return ""
    return str(rows[0].get("shenwan_industry_l1") or "").strip()


def sector_name_for_item(client: SupabaseRest, item: dict[str, Any]) -> str:
    sector = extract_sector_name(item)
    if sector:
        return sector
    code = str(item.get("code", "")).zfill(6)
    return fetch_sector_name(client, code) if code else ""


def sector_rank_for_name(sector_name: str, sector_ranks: dict[str, dict[str, Any]]) -> int:
    if not sector_name or not sector_ranks:
        return 0
    ranking = sector_ranks.get(sector_name)
    if not ranking:
        for name, value in sector_ranks.items():
            if sector_name in name or name in sector_name:
                ranking = value
                break
    return integer((ranking or {}).get("rank"))


def sector_skip_reason(decision: dict[str, Any], sector_rank: int) -> str:
    if sector_rank <= 0:
        return ""
    if sector_rank > 15:
        return "板块排名靠后"
    if sector_rank > 10 and decision_momentum_score(decision) < 60:
        return "板块排名靠后，个股动量不足"
    return ""


def mark_weak_sector_observations(
    client: SupabaseRest,
    positions: list[dict[str, Any]],
    sector_ranks: dict[str, dict[str, Any]],
    dry_run: bool,
) -> None:
    if not sector_ranks:
        return
    for position in positions:
        sector = sector_name_for_item(client, position)
        rank = sector_rank_for_name(sector, sector_ranks)
        if rank <= 20:
            continue
        message = f"减仓观察：板块排名靠后（{sector} #{rank}）"
        print(f"[SectorRank] {position.get('code', '')} {message}", flush=True)
        if dry_run:
            continue
        position_id = position.get("id")
        if not position_id:
            continue
        client.request(
            "PATCH",
            f"stock_positions?id=eq.{quote(str(position_id))}",
            {
                "current_suggestion": message,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            prefer="return=minimal",
        )


def signal_event_id(client: SupabaseRest, decision: dict[str, Any]) -> str:
    existing = str(decision.get("source_signal_id") or decision.get("signal_event_id") or "")
    if existing:
        return existing

    code = quote(str(decision.get("code", "")).zfill(6))
    signal_date = str(decision.get("decision_date") or date.today().isoformat())
    rows = client.request(
        "GET",
        f"stock_signal_events?code=eq.{code}&signal_date=eq.{quote(signal_date)}&select=id&order=signal_time.desc&limit=1",
    ) or []
    return str(rows[0].get("id", "")) if rows else ""


def record_signal_execution(
    client: SupabaseRest,
    decision: dict[str, Any],
    status: str,
    reason: str,
    order_id: str = "",
    signal_id: str = "",
) -> None:
    target_id = signal_id or signal_event_id(client, decision)
    if not target_id:
        return

    client.request(
        "PATCH",
        f"stock_signal_events?id=eq.{quote(target_id)}",
        {
            "execution_status": status,
            "execution_order_id": order_id or None,
            "execution_reason": reason,
            "execution_handled_at": datetime.now(timezone.utc).isoformat(),
        },
        prefer="return=minimal",
    )


def sell_state_payload(decision_result: SellDecision) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sell_stage": decision_result.next_sell_stage,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if decision_result.trailing_stop_price is not None:
        payload["trailing_stop_price"] = decision_result.trailing_stop_price
    if decision_result.last_profit_taking_price is not None:
        payload["last_profit_taking_price"] = decision_result.last_profit_taking_price
    return payload


def record_skipped_sell_decision(
    client: SupabaseRest,
    decision: dict[str, Any],
    position: dict[str, Any],
    decision_result: SellDecision,
) -> None:
    if not decision_result.reason:
        return
    client.request(
        "PATCH",
        f"stock_positions?id=eq.{position['id']}",
        {
            "current_suggestion": decision_result.reason,
            **sell_state_payload(decision_result),
        },
        prefer="return=minimal",
    )
    record_signal_execution(client, decision, decision_result.execution_status, decision_result.reason)


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
    failure_reason: str = "",
    status: str = "filled",
    fee_amount: float = 0,
    order_slippage_amount: float = 0,
) -> str:
    source_signal_id = signal_event_id(client, decision)
    payload = {
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
        "status": status,
        "source_signal_id": source_signal_id or None,
        "failure_reason": failure_reason,
        "fee_amount": fee_amount,
        "slippage_amount": order_slippage_amount,
        "model_score": number(decision.get("model_score")),
        "model_rank": integer(decision.get("model_rank")),
        "multi_factor_score": number(decision.get("multi_factor_score")),
        "source_decision_date": decision.get("decision_date"),
        "source_update_time": decision.get("update_time", ""),
    }
    try:
        rows = client.request("POST", "stock_auto_trade_orders", [payload], prefer="return=representation")
    except RuntimeError as error:
        optional_fields = ("fee_amount", "slippage_amount")
        if not any(field in str(error) for field in optional_fields):
            raise
        fallback_payload = dict(payload)
        for field in optional_fields:
            fallback_payload.pop(field, None)
        rows = client.request("POST", "stock_auto_trade_orders", [fallback_payload], prefer="return=representation")
    return str(rows[0].get("id", "")) if rows else ""


def record_unfilled_order(
    client: SupabaseRest,
    decision: dict[str, Any],
    side: str,
    reason: str,
    status: str,
    failure_reason: str,
    price: float = 0,
    before_shares: int = 0,
) -> str:
    return insert_order(
        client,
        decision,
        side,
        reason,
        price,
        0,
        cash_before=0,
        cash_after=0,
        before_shares=before_shares,
        after_shares=before_shares,
        pnl=0,
        failure_reason=failure_reason,
        status=status,
    )


def buy_position(
    client: SupabaseRest,
    decision: dict[str, Any],
    positions: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    max_holdings: int = MAX_HOLDINGS,
    market_regime: str = "震荡市",
    account_drawdown: float = 0,
    consecutive_losses: int = 0,
    stock_volatility_pct: float = 40,
) -> dict[str, Any] | None:
    price = number(decision.get("suggest_buy_price")) or number(decision.get("current_price"))
    stop_loss = number(decision.get("stop_loss"))
    if is_suspended(decision):
        reason = "自动模拟买入受阻：股票停牌，不能交易"
        order_id = record_unfilled_order(client, decision, "buy", "blocked buy", "blocked", "suspended stock", price)
        record_signal_execution(client, decision, "blocked", reason, order_id)
        return None
    if is_strong_limit_up(decision):
        reason = "自动模拟买入受阻：涨停买入成交概率低，未生成虚拟订单"
        order_id = record_unfilled_order(client, decision, "buy", "blocked buy", "blocked", "limit-up buy blocked", price)
        record_signal_execution(client, decision, "blocked", reason, order_id)
        return None
    if price <= 0:
        record_signal_execution(client, decision, "failed", "自动模拟买入失败：价格无效，未生成虚拟订单")
        return None
    if len(positions) >= max_holdings:
        record_signal_execution(client, decision, "blocked", f"自动模拟买入受阻：已达到持仓数量上限 {max_holdings} 只")
        return None

    cash = cash_balance(positions, trades)
    total_assets = INITIAL_CAPITAL + realized_pnl(trades) + floating_pnl(positions)
    reserve_cash = INITIAL_CAPITAL * CASH_RESERVE_RATE
    available_cash = max(0, cash - reserve_cash)
    base_amount = INITIAL_CAPITAL * initial_position_rate(decision)
    target_amount = adaptive_position_size(
        base_amount,
        decision_signal_strength(decision),
        market_regime,
        account_drawdown,
        consecutive_losses,
        stock_volatility_pct,
    )
    max_single_amount = total_assets * MAX_SINGLE_POSITION_RATE
    risk_per_share = price - stop_loss if stop_loss > 0 and price > stop_loss else price * 0.06
    risk_amount_cap = INITIAL_CAPITAL * RISK_RATE / risk_per_share * price
    allowed_amount = max(0, min(available_cash, target_amount, max_single_amount, risk_amount_cap))
    executed_price = execution_price("buy", price)
    shares = round_lot(allowed_amount / executed_price)
    if shares <= 0:
        record_signal_execution(client, decision, "blocked", "自动模拟买入受阻：可用现金或风险预算不足 100 股")
        return None

    market = executed_price * shares
    fee = trading_fee("buy", market)
    payload = {
        "code": str(decision.get("code", "")).zfill(6),
        "name": decision.get("name", ""),
        "cost_price": executed_price,
        "shares": shares,
        "current_price": executed_price,
        "market_value": market,
        "floating_pnl": 0,
        "pnl_rate": 0,
        "buy_date": date.today().isoformat(),
        "holding_days": 0,
        "current_suggestion": f"自动模拟买入：{decision.get('final_action', '')}",
        "buy_memo": "自动模拟交易引擎",
        "status": "open",
        "entry_stop_loss": stop_loss,
    }
    rows = client.request("POST", "stock_positions", payload, prefer="return=representation")
    cash_after = cash - market - fee
    order_id = insert_order(
        client,
        decision,
        "buy",
        "can_buy signal",
        executed_price,
        shares,
        cash,
        cash_after,
        0,
        shares,
        fee_amount=fee,
        order_slippage_amount=slippage_amount(price, executed_price, shares),
    )
    record_signal_execution(client, decision, "auto_executed", "自动模拟买入已执行", order_id)
    return rows[0] if rows else payload


def sell_position(
    client: SupabaseRest,
    decision: dict[str, Any],
    position: dict[str, Any],
    positions: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    reason: str,
    shares_to_sell: int,
    decision_result: SellDecision | None = None,
) -> dict[str, Any] | None:
    price = number(decision.get("suggest_sell_price")) or number(decision.get("current_price"))
    shares = integer(position.get("shares"))
    if price <= 0:
        record_signal_execution(client, decision, "failed", "自动模拟卖出失败：价格无效，未生成虚拟订单")
        return position
    if str(position.get("buy_date") or "") == date.today().isoformat():
        reason_text = "\u81ea\u52a8\u6a21\u62df\u5356\u51fa\u53d7\u963b\uff1aA\u80a1 T+1 \u7ea6\u675f\uff0c\u5f53\u65e5\u4e70\u5165\u4e0d\u80fd\u5f53\u65e5\u5356\u51fa"
        order_id = record_unfilled_order(client, decision, "sell", reason, "blocked", "T+1 same-day sell blocked", price, shares)
        record_signal_execution(client, decision, "blocked", reason_text, order_id)
        return position
    if is_suspended(decision):
        reason_text = "自动模拟卖出受阻：股票停牌，不能交易"
        order_id = record_unfilled_order(client, decision, "sell", reason, "blocked", "suspended stock", price, shares)
        record_signal_execution(client, decision, "blocked", reason_text, order_id)
        return position
    if is_limit_down(decision):
        reason_text = "自动模拟卖出受阻：跌停卖出成交概率低，未生成成交订单"
        order_id = record_unfilled_order(client, decision, "sell", reason, "blocked", "limit-down sell blocked", price, shares)
        record_signal_execution(client, decision, "blocked", reason_text, order_id)
        return position
    if shares_to_sell <= 0:
        record_signal_execution(client, decision, "blocked", "自动模拟卖出受阻：策略未给出可卖股数")
        return position

    shares_to_sell = min(shares, round_lot(shares_to_sell))
    if shares_to_sell <= 0:
        record_signal_execution(client, decision, "blocked", "自动模拟卖出受阻：可卖股数不足 100 股")
        return position

    executed_price = execution_price("sell", price)
    gross_amount = executed_price * shares_to_sell
    fee = trading_fee("sell", gross_amount)
    cost_price = number(position.get("cost_price"))
    pnl = (executed_price - cost_price) * shares_to_sell - fee
    cash = cash_balance(positions, trades)
    cash_after = cash + gross_amount - fee
    next_shares = shares - shares_to_sell
    is_cleared = next_shares <= 0
    sell_state = decision_result or SellDecision(reason, shares_to_sell, "closed" if is_cleared else position_sell_stage(position))

    client.insert("stock_trade_history", [{
        "code": position.get("code"),
        "name": position.get("name"),
        "buy_date": position.get("buy_date") or date.today().isoformat(),
        "sell_date": date.today().isoformat(),
        "cost_price": cost_price,
        "sell_price": executed_price,
        "shares": shares_to_sell,
        "pnl_amount": pnl,
        "pnl_rate": pnl / (cost_price * shares_to_sell) * 100 if cost_price > 0 and shares_to_sell > 0 else 0,
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
                "current_price": executed_price,
                "market_value": 0,
                "floating_pnl": pnl,
                "pnl_rate": pnl / (cost_price * shares_to_sell) * 100 if cost_price > 0 and shares_to_sell > 0 else 0,
                "current_suggestion": f"自动模拟清仓：{reason}",
                "status": "closed",
                **sell_state_payload(sell_state),
            },
            prefer="return=minimal",
        )
        next_position = None
    else:
        next_position = update_position_price(
            client,
            {**position, "shares": next_shares},
            executed_price,
            f"自动模拟减仓：{reason}",
        )
        client.request(
            "PATCH",
            f"stock_positions?id=eq.{position['id']}",
            {
                "shares": next_shares,
                **sell_state_payload(sell_state),
            },
            prefer="return=minimal",
        )

    order_id = insert_order(
        client,
        decision,
        "sell",
        reason,
        executed_price,
        shares_to_sell,
        cash,
        cash_after,
        shares,
        next_shares,
        pnl,
        fee_amount=fee,
        order_slippage_amount=slippage_amount(price, executed_price, shares_to_sell),
    )
    record_signal_execution(client, decision, "auto_executed", f"自动模拟卖出已执行：{reason}", order_id)
    return next_position


def sell_decision(decision: dict[str, Any], position: dict[str, Any]) -> SellDecision:
    price = number(decision.get("current_price"))
    stop_loss = number(decision.get("stop_loss"))
    entry_stop = entry_stop_loss(position)
    target_1r = number(decision.get("target_price_1"))
    target_2 = target_2r(position, target_1r)
    shares = integer(position.get("shares"))
    stage = position_sell_stage(position)
    trailing_stop = number(position.get("trailing_stop_price"))
    pnl_rate = profit_rate(price, position)
    days_held = holding_days(position)

    if days_held >= MAX_HOLD_DAYS and pnl_rate < 0:
        return SellDecision(f"触发时间止损（持有{days_held}天，浮亏{pnl_rate:.1f}%）", shares, "closed")
    if price > 0 and entry_stop > 0 and price <= entry_stop:
        return SellDecision("触发原始止损（买入时设定）", shares, "closed")
    if price > 0 and stop_loss > 0 and price <= stop_loss and (entry_stop <= 0 or stop_loss > entry_stop):
        if entry_stop > 0:
            return SellDecision("触发日内紧急止损", shares, "closed")
        return SellDecision("触发止损", shares, "closed")
    if price > 0 and trailing_stop > 0 and price <= trailing_stop:
        return SellDecision("跌破移动止损", shares, "closed")
    if price > 0 and is_failed_reseal(decision):
        return SellDecision("炸板后回封失败，清仓保护利润", shares, "closed", last_profit_taking_price=price)
    if price > 0 and is_heavy_volume_board_break(decision):
        sell_shares = partial_sell_shares(shares, 0.5)
        next_stage = "closed" if sell_shares >= shares else "sold_2r"
        return SellDecision("放量炸板，减仓保护利润", sell_shares, next_stage, last_profit_taking_price=price)
    if price > 0 and is_consecutive_limit_up(decision) and is_strong_limit_up(decision):
        return SellDecision(
            "连续涨停且封板强，暂不卖出，继续抬高移动止损",
            0,
            "trailing_stop",
            trailing_stop_price=next_trailing_stop(decision, position),
            execution_status="blocked",
        )
    if price > 0 and pnl_rate >= HIGH_PROFIT_PROTECTION_RATE and is_strong_limit_up(decision):
        return SellDecision(
            "浮盈超过25%且强势涨停，暂不卖出，抬高移动止损保护利润",
            0,
            "trailing_stop",
            trailing_stop_price=next_trailing_stop(decision, position),
            execution_status="blocked",
        )
    if price > 0 and pnl_rate >= STAGNATION_PROFIT_RATE and is_heavy_volume_stagnation(decision):
        return SellDecision("浮盈超过15%且放量滞涨，清仓保护利润", shares, "closed", last_profit_taking_price=price)
    if price > 0 and pnl_rate >= HIGH_PROFIT_PROTECTION_RATE:
        if stage == "none":
            sell_shares = partial_sell_shares(shares, 0.5)
            next_stage = "closed" if sell_shares >= shares else "sold_1r"
            return SellDecision("浮盈超过25%，普通持仓强制减仓保护", sell_shares, next_stage, last_profit_taking_price=price)
        return SellDecision("浮盈超过25%，普通持仓强制清仓保护", shares, "closed", last_profit_taking_price=price)
    if price > 0 and pnl_rate >= PRESSURE_PROFIT_RATE and is_near_pressure(decision):
        sell_shares = partial_sell_shares(shares, 0.3)
        next_stage = "closed" if sell_shares >= shares else next_profit_stage(stage)
        return SellDecision("浮盈超过10%且临近压力，减仓保护", sell_shares, next_stage, last_profit_taking_price=price)
    if price > 0 and is_strong_limit_up(decision):
        return SellDecision(
            "强势涨停，暂不机械止盈，抬高移动止损",
            0,
            "trailing_stop",
            trailing_stop_price=next_trailing_stop(decision, position),
            execution_status="blocked",
        )
    if price > 0 and target_2 > 0 and price >= target_2 and stage in {"sold_1r", "sold_2r", "trailing_stop"}:
        return SellDecision("触发第二止盈位", shares, "closed", last_profit_taking_price=price)
    if price > 0 and target_1r > 0 and price >= target_1r and stage == "none":
        sell_shares = min(shares, max(100, round_lot(shares * 0.5)))
        next_stage = "closed" if sell_shares >= shares else "sold_1r"
        return SellDecision("触发第一止盈位", sell_shares, next_stage, last_profit_taking_price=price)

    status = str(decision.get("status", "")).lower()
    action = str(decision.get("final_action", "")).lower()
    if "sell" in status or "risk" in status or "sell" in action:
        return SellDecision("盘中策略提示卖出或风险控制", shares, "closed")
    return SellDecision("", 0, stage)


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


def insert_snapshot(
    client: SupabaseRest,
    positions: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    trade_count: int,
    regime: str = "震荡市",
) -> None:
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
        "note": f"[{regime}] auto paper trading snapshot",
    }])


def run(dry_run: bool = False) -> dict[str, int]:
    client = get_client()
    regime_info = classify_market_regime(client)
    regime = str(regime_info.get("regime") or "震荡市")
    position_cap_pct = number(regime_info.get("position_cap_pct"), 40)
    max_holdings = REGIME_MAX_HOLDINGS.get(regime, 4)
    buy_blocked_by_regime = regime in {"熊市", "防御"}
    print(f"[MarketRegime] {regime} | 仓位上限 {position_cap_pct}% | 最大持仓 {max_holdings}", flush=True)
    if os.environ.get("A_STOCK_SKIP_SECTOR_RANKING", "").strip().lower() in {"1", "true", "yes"}:
        print("[SectorRank] 已按环境变量跳过行业过滤", flush=True)
        sector_ranks = {}
    else:
        try:
            sector_ranks = sector_momentum_ranking(client, lookback_days=20)
        except Exception as exc:
            print(f"[SectorRank] 行业排名获取失败，跳过行业过滤: {exc}", flush=True)
            sector_ranks = {}
    decisions = latest_live_decisions(client)
    predictions = latest_model_predictions(client)
    decisions = enrich_decisions_with_model_predictions(decisions, predictions)
    positions = open_positions(client)
    trades = trade_history(client)
    snapshots = portfolio_snapshots(client)
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
    total_assets_now = INITIAL_CAPITAL + realized_pnl(trades) + floating_pnl(positions)
    account_drawdown = account_drawdown_pct(snapshots, total_assets_now)
    consecutive_losses = consecutive_loss_count(trades)

    for position in list(positions):
        code = str(position.get("code", "")).zfill(6)
        decision = decisions_by_code.get(code)
        if not decision:
            orphan_stop = entry_stop_loss(position)
            orphan_price = number(position.get("current_price"))
            orphan_shares = integer(position.get("shares"))
            if orphan_stop > 0 and orphan_price > 0 and orphan_price <= orphan_stop and orphan_shares > 0:
                if not dry_run:
                    reason_text = "僵尸仓触发原始止损（无实时决策，自救清仓）"
                    sell_position(
                        client,
                        {
                            "code": code,
                            "name": position.get("name", ""),
                            "current_price": orphan_price,
                            "suggest_sell_price": orphan_price,
                            "decision_date": date.today().isoformat(),
                        },
                        position,
                        positions,
                        trades,
                        reason_text,
                        orphan_shares,
                    )
                    trade_count += 1
                    trades = trade_history(client)
                    position_by_code.pop(code, None)
            continue
        updated = update_position_price(
            client,
            position,
            number(decision.get("current_price"), number(position.get("current_price"))),
            str(decision.get("final_action") or position.get("current_suggestion") or ""),
        )
        position_by_code[code] = updated

    positions = [item for item in position_by_code.values() if integer(item.get("shares")) > 0]
    mark_weak_sector_observations(client, positions, sector_ranks, dry_run)

    for position in list(positions):
        code = str(position.get("code", "")).zfill(6)
        decision = decisions_by_code.get(code)
        if not decision or (code, "sell") in ordered_keys:
            continue
        decision_result = sell_decision(decision, position)
        if not decision_result.reason:
            continue
        if not dry_run:
            if decision_result.shares <= 0:
                record_skipped_sell_decision(client, decision, position, decision_result)
                continue
            next_position = sell_position(
                client,
                decision,
                position,
                positions,
                trades,
                decision_result.reason,
                decision_result.shares,
                decision_result,
            )
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
        if buy_blocked_by_regime:
            if not dry_run:
                record_signal_execution(client, decision, "blocked", f"市场状态为{regime}，自动模拟停止新增买入")
            continue
        sector = sector_name_for_item(client, decision)
        sector_rank = sector_rank_for_name(sector, sector_ranks)
        skip_reason = sector_skip_reason(decision, sector_rank)
        if skip_reason:
            full_reason = f"{skip_reason}（{sector or '未知行业'} #{sector_rank}）"
            print(f"[SectorRank] {code} {full_reason}", flush=True)
            if not dry_run:
                record_signal_execution(client, decision, "blocked", full_reason)
            continue
        if len(position_by_code) >= max_holdings or (code, "buy") in ordered_keys:
            continue
        if not dry_run:
            bought = buy_position(
                client,
                decision,
                list(position_by_code.values()),
                trades,
                max_holdings=max_holdings,
                market_regime=regime,
                account_drawdown=account_drawdown,
                consecutive_losses=consecutive_losses,
                stock_volatility_pct=number(decision.get("stock_volatility_pct") or decision.get("volatility_pct"), 40),
            )
            if bought:
                position_by_code[code] = bought
                trade_count += 1

    final_positions = [item for item in position_by_code.values() if integer(item.get("shares")) > 0]
    final_trades = trade_history(client)
    if not dry_run:
        insert_snapshot(client, final_positions, final_trades, trade_count, regime=regime)
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
