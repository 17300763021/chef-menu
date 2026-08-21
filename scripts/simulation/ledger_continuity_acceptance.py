"""M3.5 disabled two-day account-continuity publication and acceptance.

The module reuses the accepted M3.3 RQAlpha run and the M3.4 atomic ledger.
It proves that a position bought on one session is carried, becomes sellable
under T+1, and is valued exactly once on the next session even without a new
strategy instruction.  It does not authorize trading or activate an account.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any
from urllib.parse import quote

from scripts.simulation.contracts import (
    ENGINE_NAME,
    ENGINE_VERSION,
    AccountSnapshot,
    ClosingPosition,
    DataState,
    PositionEvaluation,
    PositionState,
    SimulationPackage,
    canonical_json,
    simulation_identity,
)
from scripts.simulation.ledger_acceptance import (
    INTEGER_FIELDS,
    LEDGER_TABLES,
    NUMERIC_FIELDS,
    LedgerClient,
    SupabaseLedgerClient,
    build_disabled_package,
    extract_transaction_day_evidence,
    money,
)
from scripts.simulation.m2_history_source import (
    ACCEPTANCE_SESSIONS,
    PINNED_DAILY_RELEASE_IDS,
    M2BoundedResearchInput,
    load_bounded_input,
)
from scripts.simulation.reconciliation import reconcile
from scripts.simulation.rqalpha_backtest_runner import (
    INITIAL_CAPITAL,
    STRATEGY_VERSION,
    execute_continuity_backtest,
)
from scripts.simulation.run_store import SimulationRunStore, publication_payload
from scripts.cloud_runtime import env_value, read_env_file


TRANSACTION_DATE = ACCEPTANCE_SESSIONS[1]
HOLDING_DATE = ACCEPTANCE_SESSIONS[2]
SYMBOL = "600519"


def _date_text(value: Any) -> str:
    return str(value)[:10]


def _one_row(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    if len(rows) != 1:
        raise RuntimeError(f"M3.5 requires exactly one {label}; found {len(rows)}")
    return rows[0]


def extract_holding_day_evidence(
    result: dict[str, Any],
    daily_trace: tuple[dict[str, Any], ...],
    input_release: M2BoundedResearchInput,
) -> dict[str, Any]:
    """Extract one no-order holding day and its admitted valuation lineage."""

    analyser = result.get("sys_analyser")
    if not isinstance(analyser, dict):
        raise RuntimeError("RQAlpha analyser output is missing")
    trades = analyser.get("trades")
    portfolio = analyser.get("portfolio")
    positions = analyser.get("stock_positions")
    account = analyser.get("stock_account")
    if any(value is None for value in (trades, portfolio, positions, account)):
        raise RuntimeError("RQAlpha holding-day accounting tables are incomplete")

    day = HOLDING_DATE.isoformat()
    trade_rows = [
        dict(row) for _, row in trades.iterrows()
        if _date_text(row["trading_datetime"]) == day
    ]
    if trade_rows:
        raise RuntimeError("M3.5 holding day must not contain an order fill")
    portfolio_row = _one_row(
        [dict(row) for index, row in portfolio.iterrows() if _date_text(index) == day],
        "holding-day portfolio row",
    )
    account_row = _one_row(
        [dict(row) for index, row in account.iterrows() if _date_text(index) == day],
        "holding-day stock-account row",
    )
    position_row = _one_row(
        [
            dict(row) for index, row in positions.iterrows()
            if _date_text(index) == day and str(row["order_book_id"]) == f"{SYMBOL}.XSHG"
        ],
        "holding-day 600519 position row",
    )
    trace_row = _one_row(
        [
            dict(row) for row in daily_trace
            if row.get("business_date") == day and row.get("symbol") == SYMBOL
        ],
        "holding-day RQAlpha T+1 trace row",
    )
    transaction_trace = _one_row(
        [
            dict(row) for row in daily_trace
            if row.get("business_date") == TRANSACTION_DATE.isoformat() and row.get("symbol") == SYMBOL
        ],
        "transaction-day RQAlpha T+1 trace row",
    )
    bar = _one_row(
        [
            dict(row) for row in input_release.bars
            if str(row.get("symbol")) == SYMBOL and str(row.get("business_date")) == day
        ],
        "holding-day admitted raw bar",
    )
    tradeability = _one_row(
        [
            dict(row) for row in input_release.tradeability
            if str(row.get("symbol")) == SYMBOL and str(row.get("business_date")) == day
        ],
        "holding-day tradeability fact",
    )
    if str(bar.get("adjustment")) != "none" or not bool(tradeability.get("has_primary_bar")):
        raise RuntimeError("holding-day valuation requires one admitted raw primary bar")
    if bool(tradeability.get("can_buy")) or bool(tradeability.get("can_sell")):
        raise RuntimeError("M3.5 expected holding-day decisions to remain fail-closed")

    return {
        "business_date": day,
        "portfolio": portfolio_row,
        "account": account_row,
        "position": position_row,
        "trace": trace_row,
        "transaction_trace": transaction_trace,
        "bar": bar,
        "tradeability": tradeability,
    }


def _build_holding_package(
    first: SimulationPackage,
    evidence: dict[str, Any],
    *,
    input_sha256: str,
    source_commit: str,
) -> SimulationPackage:
    if first.business_date != TRANSACTION_DATE or len(first.closing_positions) != 1:
        raise ValueError("M3.5 predecessor must be the accepted one-position transaction day")
    if date.fromisoformat(str(evidence["business_date"])) != HOLDING_DATE:
        raise ValueError("M3.5 holding evidence has the wrong business date")

    predecessor = first.closing_positions[0]
    if predecessor.symbol != SYMBOL or predecessor.total_shares != 100 or predecessor.sellable_shares != 0:
        raise ValueError("M3.5 predecessor position differs from the accepted T+0 close")
    transaction_trace = evidence["transaction_trace"]
    trace = evidence["trace"]
    if (
        int(transaction_trace["total_shares"]) != 100
        or int(transaction_trace["sellable_shares"]) != 0
        or int(trace["total_shares"]) != 100
        or int(trace["sellable_shares"]) != 100
    ):
        raise ValueError("RQAlpha T+1 state did not move from 0 to 100 sellable shares")

    position_row = evidence["position"]
    portfolio = evidence["portfolio"]
    account = evidence["account"]
    bar = evidence["bar"]
    if str(position_row["order_book_id"]) != f"{SYMBOL}.XSHG" or int(position_row["quantity"]) != 100:
        raise ValueError("RQAlpha holding-day position identity or quantity is inconsistent")
    if money(position_row["avg_price"]) != money(trace["average_execution_price"]):
        raise ValueError("RQAlpha holding-day average execution price is inconsistent")
    mark_price = money(position_row["last_price"])
    if mark_price != money(bar["close"]):
        raise ValueError("RQAlpha holding-day mark differs from the admitted raw close")
    market_value = money(position_row["market_value"])
    if market_value != money(mark_price * predecessor.total_shares):
        raise ValueError("RQAlpha holding-day market value is inconsistent")
    if market_value != money(portfolio["market_value"]) or market_value != money(account["market_value"]):
        raise ValueError("RQAlpha holding-day position, portfolio, and account values differ")

    cash = money(portfolio["cash"])
    if cash != first.snapshot.cash or cash != money(account["cash"]):
        raise ValueError("holding-day opening cash does not equal the predecessor close")
    if money(account["transaction_cost"]) != 0:
        raise ValueError("the no-trade holding day unexpectedly contains transaction cost")
    floating_pnl = money(market_value - predecessor.average_cost * predecessor.total_shares)
    total_equity = money(cash + market_value)
    if total_equity != money(portfolio["total_value"]) or total_equity != money(account["total_value"]):
        raise ValueError("RQAlpha holding-day equity does not reconcile")
    if total_equity != money(INITIAL_CAPITAL + floating_pnl):
        raise ValueError("holding-day fee-capitalized PnL does not reconcile to initial capital")

    data_release_id = f"{PINNED_DAILY_RELEASE_IDS[1]}@input-sha256-{input_sha256}"
    run_id, idempotency_key = simulation_identity(
        "development",
        HOLDING_DATE,
        data_release_id,
        STRATEGY_VERSION,
        source_commit,
        first.run_id,
    )
    package = SimulationPackage(
        run_id=run_id,
        idempotency_key=idempotency_key,
        environment="development",
        business_date=HOLDING_DATE,
        data_release_id=data_release_id,
        strategy_version=STRATEGY_VERSION,
        source_commit=source_commit,
        engine_name=ENGINE_NAME,
        engine_version=ENGINE_VERSION,
        predecessor_run_id=first.run_id,
        opening_positions=(PositionState(
            symbol=predecessor.symbol,
            total_shares=predecessor.total_shares,
            sellable_shares=100,
            average_cost=predecessor.average_cost,
        ),),
        instructions=(),
        orders=(),
        fills=(),
        cash_entries=(),
        closing_positions=(ClosingPosition(
            symbol=SYMBOL,
            total_shares=100,
            sellable_shares=100,
            average_cost=predecessor.average_cost,
            mark_price=mark_price,
            market_value=market_value,
            floating_pnl=floating_pnl,
            data_state=DataState.FRESH,
        ),),
        evaluations=(PositionEvaluation(
            symbol=SYMBOL,
            data_state=DataState.FRESH,
            evaluated=True,
            blocked_reason="",
        ),),
        snapshot=AccountSnapshot(
            initial_capital=INITIAL_CAPITAL,
            opening_cash=first.snapshot.cash,
            opening_realized_pnl=first.snapshot.realized_pnl,
            cash=cash,
            market_value=market_value,
            total_equity=total_equity,
            realized_pnl=first.snapshot.realized_pnl,
            floating_pnl=floating_pnl,
            total_fees=Decimal("0"),
        ),
    )
    reconcile(package)
    return package


def assert_continuity(first: SimulationPackage, second: SimulationPackage) -> None:
    if second.predecessor_run_id != first.run_id:
        raise ValueError("holding-day predecessor_run_id does not reference the transaction day")
    if second.snapshot.opening_cash != first.snapshot.cash:
        raise ValueError("holding-day opening cash differs from the predecessor close")
    if second.snapshot.opening_realized_pnl != first.snapshot.realized_pnl:
        raise ValueError("holding-day opening realized PnL differs from the predecessor close")
    if len(first.closing_positions) != 1 or len(second.opening_positions) != 1:
        raise ValueError("continuity acceptance requires exactly one carried position")
    first_position = first.closing_positions[0]
    second_position = second.opening_positions[0]
    if (
        second_position.symbol != first_position.symbol
        or second_position.total_shares != first_position.total_shares
        or second_position.average_cost != first_position.average_cost
        or second_position.sellable_shares != first_position.total_shares
    ):
        raise ValueError("holding-day opening position does not carry the predecessor book state")
    if second.instructions or second.orders or second.fills or second.cash_entries:
        raise ValueError("holding-day continuity acceptance must not create trading activity")
    if len(second.closing_positions) != 1 or len(second.evaluations) != 1:
        raise ValueError("carried position must receive exactly one holding-day evaluation")


def build_continuity_packages(
    transaction_evidence: dict[str, Any],
    holding_evidence: dict[str, Any],
    *,
    input_sha256: str,
    source_commit: str,
) -> tuple[SimulationPackage, SimulationPackage]:
    with localcontext() as context:
        context.prec = 34
        first = build_disabled_package(
            transaction_evidence,
            input_sha256=input_sha256,
            source_commit=source_commit,
        )
        second = _build_holding_package(
            first,
            holding_evidence,
            input_sha256=input_sha256,
            source_commit=source_commit,
        )
        assert_continuity(first, second)
        return first, second


def _normalize_rows(
    rows: list[dict[str, Any]], fields: tuple[str, ...], sort_field: str,
) -> list[dict[str, Any]]:
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


def _expect_denied(action: Any, label: str) -> str:
    try:
        action()
    except RuntimeError as error:
        if not any(code in str(error) for code in ("Supabase 400:", "Supabase 401:", "Supabase 403:")):
            raise AssertionError(f"{label} failed for an unexpected reason") from error
        return "rejected"
    raise AssertionError(f"{label} unexpectedly succeeded")


def _publish_and_read_back(service: LedgerClient, package: SimulationPackage) -> dict[str, Any]:
    payload = publication_payload(package)
    publications = [SimulationRunStore(service).publish(package) for _ in range(10)]
    if {row["run_id"] for row in publications} != {package.run_id}:
        raise AssertionError("holding-day retries did not retain one run identity")
    if sum(not bool(row.get("idempotent_replay")) for row in publications) > 1:
        raise AssertionError("holding-day retries created more than one fresh publication")

    encoded = quote(package.run_id, safe="")
    run_rows = service.rows(
        "v2_simulation_runs",
        "select=run_id,predecessor_run_id,manifest_sha256,simulation_only,activation_state,"
        f"authoritative_account_write&run_id=eq.{encoded}",
    )
    if len(run_rows) != 1:
        raise AssertionError("online continuity ledger does not contain exactly one holding-day run")
    stored = run_rows[0]
    if (
        stored["predecessor_run_id"] != package.predecessor_run_id
        or stored["manifest_sha256"] != payload["p_manifest"]["manifest_sha256"]
        or stored["simulation_only"] is not True
        or stored["activation_state"] != "disabled_acceptance"
        or stored["authoritative_account_write"] is not False
    ):
        raise AssertionError("online continuity lineage, hash, or simulation boundary is inconsistent")

    components: dict[str, Any] = {}
    for component, table, payload_key, fields, sort_field in LEDGER_TABLES:
        rows = service.rows(
            table,
            f"select={','.join(fields)}&run_id=eq.{encoded}&order={sort_field}.asc",
        )
        actual = _normalize_rows(rows, fields, sort_field)
        expected = _normalize_rows(payload[payload_key], fields, sort_field)
        if actual != expected:
            raise AssertionError(f"online holding-day {component} differs from the publication")
        digest = hashlib.sha256(canonical_json(payload[payload_key])).hexdigest()
        if digest != payload["p_manifest"]["hashes"][component]:
            raise AssertionError(f"holding-day {component} hash differs from the manifest")
        components[component] = {"count": len(actual), "sha256": digest}

    tampered = copy.deepcopy(payload)
    tampered["p_positions"][0]["mark_price"] = "1.0000"
    changed_content = _expect_denied(
        lambda: service.rpc("publish_v2_simulation_run", tampered),
        "holding-day same-key changed-content publication",
    )
    final_rows = service.rows(
        "v2_simulation_runs",
        f"select=run_id,manifest_sha256&run_id=eq.{encoded}",
    )
    if len(final_rows) != 1 or final_rows[0]["manifest_sha256"] != payload["p_manifest"]["manifest_sha256"]:
        raise AssertionError("holding-day rejection probe changed the immutable run")
    return {
        "run_id": package.run_id,
        "predecessor_run_id": package.predecessor_run_id,
        "business_date": package.business_date.isoformat(),
        "publication_attempts": 10,
        "database_run_rows": 1,
        "component_readback": components,
        "reconciliation": reconcile(package),
        "changed_content": changed_content,
        "manifest_sha256": payload["p_manifest"]["manifest_sha256"],
    }


def run_continuity_acceptance(
    service: LedgerClient,
    public: LedgerClient,
    first: SimulationPackage,
    second: SimulationPackage,
) -> dict[str, Any]:
    from scripts.simulation.ledger_acceptance import run_online_acceptance

    with localcontext() as context:
        context.prec = 34
        assert_continuity(first, second)
        first_report = run_online_acceptance(service, public, first)
        second_report = _publish_and_read_back(service, second)
        if second_report["predecessor_run_id"] != first_report["run_id"]:
            raise AssertionError("online holding-day run does not reference the published transaction day")
        return {
            "schema_version": "m3.5-ledger-continuity-acceptance-v1",
            "accepted": True,
            "simulation_only": True,
            "activation_state": "disabled_acceptance",
            "authoritative_account_write": False,
            "source_commit": first.source_commit,
            "strategy_version": first.strategy_version,
            "engine": {"name": first.engine_name, "version": first.engine_version},
            "transaction_day": {
                "run_id": first_report["run_id"],
                "business_date": first_report["business_date"],
                "publication_attempts": first_report["publication_attempts"],
                "reconciliation": first_report["reconciliation"],
                "manifest_sha256": first_report["manifest_sha256"],
            },
            "holding_day": second_report,
            "continuity": {
                "opening_cash_matches_predecessor": True,
                "opening_position_matches_predecessor": True,
                "sellable_shares_t0": first.closing_positions[0].sellable_shares,
                "sellable_shares_t1": second.closing_positions[0].sellable_shares,
                "holding_day_trade_activity_count": 0,
                "holding_day_position_evaluations": len(second.evaluations),
                "valuation_does_not_authorize_trading": True,
            },
            "rejection_probes": {
                **first_report["rejection_probes"],
                "holding_day_changed_content": second_report["changed_content"],
            },
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish and verify disabled M3.5 two-day continuity")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--report-out", type=Path, required=True)
    args = parser.parse_args()
    source_commit = os.environ.get("GITHUB_SHA", "").strip() or os.environ.get("SOURCE_COMMIT", "").strip()
    if not source_commit:
        raise RuntimeError("GITHUB_SHA or SOURCE_COMMIT is required for continuity lineage")

    result, input_sha256, daily_trace = execute_continuity_backtest(args.input)
    input_release = load_bounded_input(args.input)
    first, second = build_continuity_packages(
        extract_transaction_day_evidence(result),
        extract_holding_day_evidence(result, daily_trace, input_release),
        input_sha256=input_sha256,
        source_commit=source_commit,
    )
    values = read_env_file()
    url = env_value("VITE_SUPABASE_URL", values)
    service_key = env_value("SUPABASE_SERVICE_ROLE_KEY", values)
    public_key = env_value("VITE_SUPABASE_PUBLISHABLE_KEY", values)
    if not public_key:
        raise RuntimeError("VITE_SUPABASE_PUBLISHABLE_KEY is required for public-access rejection probes")
    report = run_continuity_acceptance(
        SupabaseLedgerClient(url, service_key),
        SupabaseLedgerClient(url, public_key),
        first,
        second,
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
