"""M3.4 disabled development-ledger publication and online acceptance.

The accepted M3.3 RQAlpha strategy remains the sole execution engine.  This
module extracts its transaction-day accounting evidence, builds the existing
typed V2 ledger package, publishes it through one atomic RPC, and verifies the
online immutable boundary.  It does not activate a shadow or main account.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from scripts.cloud_runtime import CloudRuntimeClient, env_value, read_env_file
from scripts.simulation.contracts import (
    ENGINE_NAME,
    ENGINE_VERSION,
    AccountSnapshot,
    CashEntry,
    ClosingPosition,
    DataState,
    EngineFill,
    EngineOrder,
    OrderInstruction,
    OrderStatus,
    PositionEvaluation,
    PriceType,
    Side,
    SimulationPackage,
    canonical_json,
    simulation_identity,
)
from scripts.simulation.m2_history_source import ACCEPTANCE_SESSIONS, PINNED_DAILY_RELEASE_IDS
from scripts.simulation.reconciliation import reconcile
from scripts.simulation.rqalpha_backtest_runner import (
    INITIAL_CAPITAL,
    STRATEGY_VERSION,
    execute_bounded_backtest,
)
from scripts.simulation.run_store import SimulationRunStore, publication_payload


TARGET_BUSINESS_DATE = ACCEPTANCE_SESSIONS[1]
MONEY = Decimal("0.0001")
LEDGER_TABLES: tuple[tuple[str, str, str, tuple[str, ...], str], ...] = (
    (
        "opening_positions", "v2_simulation_opening_positions", "p_opening_positions",
        ("symbol", "total_shares", "sellable_shares", "average_cost"), "symbol",
    ),
    (
        "instructions", "v2_simulation_decisions", "p_instructions",
        (
            "instruction_id", "symbol", "side", "quantity", "price_type", "limit_price",
            "business_date", "valid_until", "strategy_version", "data_release_id",
        ),
        "instruction_id",
    ),
    (
        "orders", "v2_simulation_orders", "p_orders",
        (
            "order_id", "instruction_id", "symbol", "side", "requested_quantity",
            "filled_quantity", "price_type", "limit_price", "status", "reject_reason",
        ),
        "order_id",
    ),
    (
        "fills", "v2_simulation_fills", "p_fills",
        (
            "fill_id", "order_id", "symbol", "side", "quantity", "price", "commission",
            "tax", "slippage", "realized_pnl",
        ),
        "fill_id",
    ),
    (
        "cash_entries", "v2_simulation_cash_entries", "p_cash_entries",
        ("entry_id", "fill_id", "sequence_no", "amount", "balance_after"), "sequence_no",
    ),
    (
        "positions", "v2_simulation_positions", "p_positions",
        (
            "symbol", "total_shares", "sellable_shares", "average_cost", "mark_price",
            "market_value", "floating_pnl", "data_state",
        ),
        "symbol",
    ),
    (
        "evaluations", "v2_simulation_position_evaluations", "p_evaluations",
        ("symbol", "data_state", "evaluated", "blocked_reason"), "symbol",
    ),
)
NUMERIC_FIELDS = {
    "average_cost", "limit_price", "price", "commission", "tax", "slippage",
    "realized_pnl", "amount", "balance_after", "mark_price", "market_value", "floating_pnl",
}
INTEGER_FIELDS = {
    "total_shares", "sellable_shares", "quantity", "requested_quantity", "filled_quantity",
    "sequence_no",
}


class LedgerClient(Protocol):
    def rpc(self, name: str, payload: dict[str, Any]) -> dict[str, Any]: ...
    def rows(self, table: str, query: str) -> list[dict[str, Any]]: ...
    def request(self, method: str, path: str, body: Any | None = None) -> Any: ...


class SupabaseLedgerClient:
    """Small acceptance adapter over the already accepted M1 REST client."""

    def __init__(self, url: str, key: str) -> None:
        self._client = CloudRuntimeClient(url, key)

    def rpc(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._client.rpc(name, payload)

    def rows(self, table: str, query: str) -> list[dict[str, Any]]:
        return self._client.rows(table, query)

    def request(self, method: str, path: str, body: Any | None = None) -> Any:
        return self._client._request(method, path, body)


def money(value: Any) -> Decimal:
    with localcontext() as context:
        context.prec = 34
        return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)


def engine_identifier(prefix: str, value: Any) -> str:
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if not text:
        raise RuntimeError(f"RQAlpha {prefix} identity is missing")
    return f"rqalpha-{prefix}-{text}"


def _date_text(value: Any) -> str:
    return str(value)[:10]


def extract_transaction_day_evidence(result: dict[str, Any]) -> dict[str, Any]:
    analyser = result.get("sys_analyser")
    if not isinstance(analyser, dict):
        raise RuntimeError("RQAlpha analyser output is missing")
    trades = analyser.get("trades")
    portfolio = analyser.get("portfolio")
    positions = analyser.get("stock_positions")
    account = analyser.get("stock_account")
    if any(value is None for value in (trades, portfolio, positions, account)):
        raise RuntimeError("RQAlpha transaction-day accounting tables are incomplete")

    trade_rows = [
        dict(row)
        for _, row in trades.iterrows()
        if _date_text(row["trading_datetime"]) == TARGET_BUSINESS_DATE.isoformat()
    ]
    portfolio_rows = [
        dict(row) for index, row in portfolio.iterrows()
        if _date_text(index) == TARGET_BUSINESS_DATE.isoformat()
    ]
    position_rows = [
        dict(row) for index, row in positions.iterrows()
        if _date_text(index) == TARGET_BUSINESS_DATE.isoformat()
    ]
    account_rows = [
        dict(row) for index, row in account.iterrows()
        if _date_text(index) == TARGET_BUSINESS_DATE.isoformat()
    ]
    if not (len(trade_rows) == len(portfolio_rows) == len(position_rows) == len(account_rows) == 1):
        raise RuntimeError(
            "M3.4 requires exactly one accepted fill, portfolio row, position row and account row "
            "on the bounded transaction day"
        )
    return {
        "business_date": TARGET_BUSINESS_DATE.isoformat(),
        "trade": trade_rows[0],
        "portfolio": portfolio_rows[0],
        "position": position_rows[0],
        "account": account_rows[0],
    }


def _build_disabled_package(
    evidence: dict[str, Any],
    *,
    input_sha256: str,
    source_commit: str,
) -> SimulationPackage:
    if not re.fullmatch(r"[0-9a-f]{64}", input_sha256):
        raise ValueError("M3.4 input_sha256 must be a canonical SHA-256")
    if not source_commit.strip():
        raise ValueError("M3.4 source commit is required")
    business_date = date.fromisoformat(str(evidence["business_date"]))
    if business_date != TARGET_BUSINESS_DATE:
        raise ValueError("M3.4 may publish only the accepted bounded transaction date")

    trade = evidence["trade"]
    position_row = evidence["position"]
    portfolio = evidence["portfolio"]
    account = evidence["account"]
    symbol = str(trade["symbol"])
    order_book_id = str(trade["order_book_id"])
    if symbol != "600519" or order_book_id != "600519.XSHG" or str(trade["side"]) != "BUY":
        raise ValueError("M3.4 bounded ledger evidence differs from the accepted RQAlpha fill")
    quantity = int(trade["last_quantity"])
    if quantity != 100:
        raise ValueError("M3.4 bounded fill quantity differs from the accepted 100-share lot")

    price = money(trade["last_price"])
    commission = money(trade["commission"])
    tax = money(trade["tax"])
    transaction_cost = money(trade["transaction_cost"])
    if transaction_cost != commission + tax or money(account["transaction_cost"]) != transaction_cost:
        raise ValueError("RQAlpha transaction cost does not reconcile to commission and tax")

    data_release_id = (
        f"{PINNED_DAILY_RELEASE_IDS[0]}@input-sha256-{input_sha256}"
    )
    instruction_id = f"m3.3-{business_date.isoformat()}-{symbol}-buy"
    instruction = OrderInstruction.from_mapping({
        "instruction_id": instruction_id,
        "symbol": symbol,
        "side": "buy",
        "quantity": quantity,
        "price_type": "market",
        "limit_price": None,
        "business_date": business_date.isoformat(),
        "valid_until": business_date.isoformat(),
        "strategy_version": STRATEGY_VERSION,
        "data_release_id": data_release_id,
    })
    order_id = engine_identifier("order", trade["order_id"])
    fill_id = engine_identifier("fill", trade["exec_id"])
    order = EngineOrder(
        order_id=order_id,
        instruction_id=instruction_id,
        symbol=symbol,
        side=Side.BUY,
        requested_quantity=quantity,
        filled_quantity=quantity,
        price_type=PriceType.MARKET,
        limit_price=None,
        status=OrderStatus.FILLED,
    )
    fill = EngineFill(
        fill_id=fill_id,
        order_id=order_id,
        symbol=symbol,
        side=Side.BUY,
        quantity=quantity,
        price=price,
        commission=commission,
        tax=tax,
        slippage=Decimal("0"),
        realized_pnl=Decimal("0"),
    )

    cash = money(portfolio["cash"])
    expected_cash = money(INITIAL_CAPITAL + fill.cash_effect)
    if cash != expected_cash:
        raise ValueError("RQAlpha closing cash does not reconcile to the accepted fill")
    mark_price = money(position_row["last_price"])
    market_value = money(position_row["market_value"])
    if int(position_row["quantity"]) != quantity or market_value != money(mark_price * quantity):
        raise ValueError("RQAlpha closing position quantity or market value is inconsistent")
    if money(portfolio["market_value"]) != market_value:
        raise ValueError("RQAlpha portfolio and position market values differ")

    # The V2 ledger capitalizes acquisition fees in book cost.  RQAlpha keeps
    # its raw average execution price separately, while total account value
    # already includes the same fee.  This representation avoids deducting the
    # fee twice and preserves the accepted equity identity.
    total_book_cost = money(price * quantity + transaction_cost)
    average_cost = money(total_book_cost / quantity)
    floating_pnl = money(market_value - total_book_cost)
    total_equity = money(cash + market_value)
    if total_equity != money(portfolio["total_value"]):
        raise ValueError("RQAlpha total equity does not reconcile to cash plus market value")
    if total_equity != money(INITIAL_CAPITAL + floating_pnl):
        raise ValueError("fee-capitalized V2 book cost does not reconcile to RQAlpha equity")

    run_id, idempotency_key = simulation_identity(
        "development", business_date, data_release_id, STRATEGY_VERSION, source_commit,
    )
    package = SimulationPackage(
        run_id=run_id,
        idempotency_key=idempotency_key,
        environment="development",
        business_date=business_date,
        data_release_id=data_release_id,
        strategy_version=STRATEGY_VERSION,
        source_commit=source_commit,
        engine_name=ENGINE_NAME,
        engine_version=ENGINE_VERSION,
        predecessor_run_id=None,
        opening_positions=(),
        instructions=(instruction,),
        orders=(order,),
        fills=(fill,),
        cash_entries=(CashEntry(
            entry_id=f"cash-{fill_id}",
            fill_id=fill_id,
            sequence_no=1,
            amount=fill.cash_effect,
            balance_after=cash,
        ),),
        closing_positions=(ClosingPosition(
            symbol=symbol,
            total_shares=quantity,
            sellable_shares=0,
            average_cost=average_cost,
            mark_price=mark_price,
            market_value=market_value,
            floating_pnl=floating_pnl,
            data_state=DataState.FRESH,
        ),),
        evaluations=(PositionEvaluation(
            symbol=symbol,
            data_state=DataState.FRESH,
            evaluated=True,
            blocked_reason="",
        ),),
        snapshot=AccountSnapshot(
            initial_capital=INITIAL_CAPITAL,
            opening_cash=INITIAL_CAPITAL,
            opening_realized_pnl=Decimal("0"),
            cash=cash,
            market_value=market_value,
            total_equity=total_equity,
            realized_pnl=Decimal("0"),
            floating_pnl=floating_pnl,
            total_fees=transaction_cost,
        ),
    )
    reconcile(package)
    return package


def build_disabled_package(
    evidence: dict[str, Any],
    *,
    input_sha256: str,
    source_commit: str,
) -> SimulationPackage:
    # RQAlpha narrows the ambient Decimal context during a run.  Keep that
    # framework-local side effect outside V2 money construction and replay.
    with localcontext() as context:
        context.prec = 34
        return _build_disabled_package(
            evidence,
            input_sha256=input_sha256,
            source_commit=source_commit,
        )


def _normalize_readback(rows: list[dict[str, Any]], fields: tuple[str, ...], sort_field: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {}
        for field in fields:
            value = row.get(field)
            if value is not None and field in NUMERIC_FIELDS:
                value = format(money(value), "f")
            elif value is not None and field in INTEGER_FIELDS:
                value = int(value)
            item[field] = value
        normalized.append(item)
    return sorted(normalized, key=lambda row: row[sort_field])


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _expect_denied(action: Any, label: str, *, allow_not_found: bool = False) -> str:
    try:
        action()
    except RuntimeError as error:
        message = str(error)
        accepted_codes = ("Supabase 400:", "Supabase 401:", "Supabase 403:")
        if allow_not_found:
            accepted_codes += ("Supabase 404:",)
        if not any(code in message for code in accepted_codes):
            raise AssertionError(f"{label} failed for an unexpected reason") from error
        return "rejected"
    raise AssertionError(f"{label} unexpectedly succeeded")


def _run_online_acceptance(
    service: LedgerClient,
    public: LedgerClient,
    package: SimulationPackage,
) -> dict[str, Any]:
    payload = publication_payload(package)
    store = SimulationRunStore(service)
    publications = [store.publish(package) for _ in range(10)]
    if {row["run_id"] for row in publications} != {package.run_id}:
        raise AssertionError("ten publication retries did not retain one run identity")
    if sum(not bool(row.get("idempotent_replay")) for row in publications) > 1:
        raise AssertionError("more than one retry claimed a fresh business publication")

    encoded_run_id = quote(package.run_id, safe="")
    run_rows = service.rows(
        "v2_simulation_runs",
        "select=run_id,idempotency_key,manifest_sha256,payload_sha256,manifest_json,"
        "simulation_only,activation_state,authoritative_account_write&"
        f"run_id=eq.{encoded_run_id}",
    )
    if len(run_rows) != 1:
        raise AssertionError(f"online ledger contains {len(run_rows)} run rows instead of one")
    stored_run = run_rows[0]
    if (
        stored_run["run_id"] != package.run_id
        or stored_run["idempotency_key"] != package.idempotency_key
        or stored_run["manifest_sha256"] != payload["p_manifest"]["manifest_sha256"]
        or not re.fullmatch(r"[0-9a-f]{64}", str(stored_run["payload_sha256"]))
        or stored_run["simulation_only"] is not True
        or stored_run["activation_state"] != "disabled_acceptance"
        or stored_run["authoritative_account_write"] is not False
    ):
        raise AssertionError("online run identity, hashes, or simulation boundary differs from publication")

    component_report: dict[str, Any] = {}
    for component, table, payload_key, fields, sort_field in LEDGER_TABLES:
        field_list = ",".join(fields)
        rows = service.rows(
            table,
            f"select={field_list}&run_id=eq.{encoded_run_id}&order={sort_field}.asc",
        )
        normalized = _normalize_readback(rows, fields, sort_field)
        expected = _normalize_readback(payload[payload_key], fields, sort_field)
        if normalized != expected:
            raise AssertionError(f"online {component} rows differ from the atomic publication payload")
        # PostgreSQL returns NUMERIC values without preserving the client's
        # insignificant trailing-zero representation.  Semantic row equality
        # is checked above after fixed-money normalization; the manifest hash
        # is then reproduced from the exact canonical publication component.
        content_sha256 = _sha256(payload[payload_key])
        if content_sha256 != payload["p_manifest"]["hashes"][component]:
            raise AssertionError(f"online {component} content hash differs from the manifest")
        component_report[component] = {"count": len(normalized), "sha256": content_sha256}

    tampered = copy.deepcopy(payload)
    tampered["p_cash_entries"][0]["amount"] = "-1.0000"
    tamper_status = _expect_denied(
        lambda: service.rpc("publish_v2_simulation_run", tampered),
        "same-key changed-content publication",
    )
    update_status = _expect_denied(
        lambda: service.request(
            "PATCH",
            f"v2_simulation_runs?run_id=eq.{encoded_run_id}",
            {"activation_state": "disabled_acceptance"},
        ),
        "service-role ledger update",
    )
    delete_status = _expect_denied(
        lambda: service.request("DELETE", f"v2_simulation_runs?run_id=eq.{encoded_run_id}"),
        "service-role ledger delete",
    )
    public_select_status = _expect_denied(
        lambda: public.rows("v2_simulation_runs", "select=run_id&limit=1"),
        "public ledger read",
        allow_not_found=True,
    )
    public_rpc_status = _expect_denied(
        lambda: public.rpc("publish_v2_simulation_run", {}),
        "public ledger publication",
        allow_not_found=True,
    )

    final_rows = service.rows(
        "v2_simulation_runs", f"select=run_id,manifest_sha256&run_id=eq.{encoded_run_id}",
    )
    if len(final_rows) != 1 or final_rows[0]["manifest_sha256"] != payload["p_manifest"]["manifest_sha256"]:
        raise AssertionError("rejection probes changed or removed the immutable run")
    return {
        "schema_version": "m3.4-online-ledger-acceptance-v1",
        "accepted": True,
        "simulation_only": True,
        "authoritative_account_write": False,
        "activation_state": "disabled_acceptance",
        "run_id": package.run_id,
        "business_date": package.business_date.isoformat(),
        "data_release_id": package.data_release_id,
        "strategy_version": package.strategy_version,
        "source_commit": package.source_commit,
        "engine": {"name": package.engine_name, "version": package.engine_version},
        "publication_attempts": 10,
        "database_run_rows": 1,
        "component_readback": component_report,
        "reconciliation": reconcile(package),
        "rejection_probes": {
            "changed_content": tamper_status,
            "service_update": update_status,
            "service_delete": delete_status,
            "public_select": public_select_status,
            "public_rpc": public_rpc_status,
        },
        "manifest_sha256": payload["p_manifest"]["manifest_sha256"],
    }


def run_online_acceptance(
    service: LedgerClient,
    public: LedgerClient,
    package: SimulationPackage,
) -> dict[str, Any]:
    with localcontext() as context:
        context.prec = 34
        return _run_online_acceptance(service, public, package)


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish and verify the disabled M3.4 development ledger")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--report-out", type=Path, required=True)
    args = parser.parse_args()
    source_commit = os.environ.get("GITHUB_SHA", "").strip() or os.environ.get("SOURCE_COMMIT", "").strip()
    if not source_commit:
        raise RuntimeError("GITHUB_SHA or SOURCE_COMMIT is required for online ledger lineage")

    result, input_sha256 = execute_bounded_backtest(args.input)
    evidence = extract_transaction_day_evidence(result)
    package = build_disabled_package(
        evidence,
        input_sha256=input_sha256,
        source_commit=source_commit,
    )
    values = read_env_file()
    url = env_value("VITE_SUPABASE_URL", values)
    service_key = env_value("SUPABASE_SERVICE_ROLE_KEY", values)
    public_key = env_value("VITE_SUPABASE_PUBLISHABLE_KEY", values)
    if not public_key:
        raise RuntimeError("VITE_SUPABASE_PUBLISHABLE_KEY is required for the public-access rejection probe")
    report = run_online_acceptance(
        SupabaseLedgerClient(url, service_key),
        SupabaseLedgerClient(url, public_key),
        package,
    )
    args.report_out.parent.mkdir(parents=True, exist_ok=True)
    args.report_out.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
