"""M3.6 backtest-versus-disabled-shadow execution parity acceptance.

Both paths independently execute the same immutable input, structured orders,
and RQAlpha backtest engine.  The second result is published only as a disabled
``shadow`` ledger rehearsal; this module does not enable paper trading, a
schedule, or an authoritative account.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import tempfile
from dataclasses import replace
from decimal import localcontext
from pathlib import Path
from typing import Any, Callable

from scripts.cloud_runtime import env_value, read_env_file
from scripts.simulation.contracts import (
    SimulationPackage,
    canonical_json,
    canonicalize,
    simulation_identity,
)
from scripts.simulation.ledger_acceptance import (
    SupabaseLedgerClient,
    extract_transaction_day_evidence,
)
from scripts.simulation.ledger_continuity_acceptance import (
    assert_continuity,
    build_continuity_packages,
    extract_holding_day_evidence,
    run_continuity_acceptance,
)
from scripts.simulation.m2_history_source import load_bounded_input
from scripts.simulation.reconciliation import reconcile
from scripts.simulation.rqalpha_backtest_runner import (
    _normalized_results,
    execute_continuity_backtest,
)


SCHEMA_VERSION = "m3.6-shadow-parity-acceptance-v1"


def economic_payload(package: SimulationPackage) -> dict[str, Any]:
    """Return business semantics while normalizing per-run engine identities.

    RQAlpha assigns a new technical order/exec sequence to each independent
    run.  Parity requires identical lineage shape and economic fields, not the
    same process-local identifiers.
    """

    orders = sorted(
        package.orders,
        key=lambda row: (row.instruction_id, row.symbol, row.side.value, row.order_id),
    )
    order_aliases = {row.order_id: f"order-{index + 1}" for index, row in enumerate(orders)}
    fills = sorted(
        package.fills,
        key=lambda row: (order_aliases[row.order_id], row.symbol, row.side.value, row.fill_id),
    )
    fill_aliases = {row.fill_id: f"fill-{index + 1}" for index, row in enumerate(fills)}
    semantic_orders = [{
        "order_alias": order_aliases[row.order_id],
        "instruction_id": row.instruction_id,
        "symbol": row.symbol,
        "side": row.side,
        "requested_quantity": row.requested_quantity,
        "filled_quantity": row.filled_quantity,
        "price_type": row.price_type,
        "limit_price": row.limit_price,
        "status": row.status,
        "reject_reason": row.reject_reason,
    } for row in orders]
    semantic_fills = [{
        "fill_alias": fill_aliases[row.fill_id],
        "order_alias": order_aliases[row.order_id],
        "symbol": row.symbol,
        "side": row.side,
        "quantity": row.quantity,
        "price": row.price,
        "commission": row.commission,
        "tax": row.tax,
        "slippage": row.slippage,
        "realized_pnl": row.realized_pnl,
    } for row in fills]
    semantic_cash_entries = [{
        "sequence_no": row.sequence_no,
        "fill_alias": fill_aliases[row.fill_id],
        "amount": row.amount,
        "balance_after": row.balance_after,
    } for row in sorted(package.cash_entries, key=lambda row: row.sequence_no)]

    return canonicalize({
        "business_date": package.business_date,
        "data_release_id": package.data_release_id,
        "strategy_version": package.strategy_version,
        "source_commit": package.source_commit,
        "engine_name": package.engine_name,
        "engine_version": package.engine_version,
        "opening_positions": package.opening_positions,
        "instructions": package.instructions,
        "orders": semantic_orders,
        "fills": semantic_fills,
        "cash_entries": semantic_cash_entries,
        "closing_positions": package.closing_positions,
        "evaluations": package.evaluations,
        "snapshot": package.snapshot,
    })


def economic_sha256(package: SimulationPackage) -> str:
    return hashlib.sha256(canonical_json(economic_payload(package))).hexdigest()


def assert_execution_parity(
    reference_normalized: dict[str, Any],
    reference_trace: tuple[dict[str, Any], ...],
    reference_packages: tuple[SimulationPackage, SimulationPackage],
    rehearsal_normalized: dict[str, Any],
    rehearsal_trace: tuple[dict[str, Any], ...],
    rehearsal_packages: tuple[SimulationPackage, SimulationPackage],
) -> dict[str, Any]:
    if reference_normalized != rehearsal_normalized:
        raise ValueError("independent RQAlpha normalized results differ")
    if reference_trace != rehearsal_trace:
        raise ValueError("independent RQAlpha T+1 daily traces differ")
    if len(reference_packages) != 2 or len(rehearsal_packages) != 2:
        raise ValueError("parity acceptance requires exactly two consecutive packages per path")

    daily_reports: list[dict[str, Any]] = []
    for reference, rehearsal in zip(reference_packages, rehearsal_packages):
        with localcontext() as context:
            context.prec = 34
            reference_reconciliation = reconcile(reference)
            rehearsal_reconciliation = reconcile(rehearsal)
        if reference_reconciliation != rehearsal_reconciliation:
            raise ValueError("backtest and rehearsal reconciliation results differ")
        reference_hash = economic_sha256(reference)
        rehearsal_hash = economic_sha256(rehearsal)
        if reference_hash != rehearsal_hash:
            raise ValueError("backtest and rehearsal economic ledger components differ")
        daily_reports.append({
            "business_date": reference.business_date.isoformat(),
            "economic_sha256": reference_hash,
            "reconciliation": reference_reconciliation,
        })
    return {
        "normalized_result_sha256": reference_normalized["result_sha256"],
        "daily_trace_sha256": hashlib.sha256(canonical_json(reference_trace)).hexdigest(),
        "daily_economic_parity": daily_reports,
    }


def to_disabled_shadow_packages(
    packages: tuple[SimulationPackage, SimulationPackage],
) -> tuple[SimulationPackage, SimulationPackage]:
    """Change only ledger environment identity; preserve all RQAlpha economics."""

    first, second = packages
    first_run_id, first_key = simulation_identity(
        "shadow",
        first.business_date,
        first.data_release_id,
        first.strategy_version,
        first.source_commit,
    )
    shadow_first = replace(
        first,
        environment="shadow",
        run_id=first_run_id,
        idempotency_key=first_key,
    )
    second_run_id, second_key = simulation_identity(
        "shadow",
        second.business_date,
        second.data_release_id,
        second.strategy_version,
        second.source_commit,
        first_run_id,
    )
    shadow_second = replace(
        second,
        environment="shadow",
        predecessor_run_id=first_run_id,
        run_id=second_run_id,
        idempotency_key=second_key,
    )
    assert_continuity(shadow_first, shadow_second)
    for source, shadow in zip(packages, (shadow_first, shadow_second)):
        if economic_sha256(source) != economic_sha256(shadow):
            raise ValueError("shadow re-identification changed an economic ledger field")
    return shadow_first, shadow_second


def capture_failure(action: Callable[[], Any]) -> dict[str, str]:
    try:
        action()
    except Exception as error:  # acceptance records the exact fail-closed boundary
        return {"type": type(error).__name__, "message": str(error)}
    raise AssertionError("invalid acceptance input unexpectedly executed")


def assert_failure_parity(
    reference_action: Callable[[], Any],
    rehearsal_action: Callable[[], Any],
) -> dict[str, str]:
    reference = capture_failure(reference_action)
    rehearsal = capture_failure(rehearsal_action)
    if reference != rehearsal:
        raise ValueError("backtest and rehearsal fail-closed behavior differs")
    return reference


def missing_bar_failure_parity(input_path: Path) -> dict[str, str]:
    value = json.loads(input_path.read_text(encoding="utf-8"))
    broken = copy.deepcopy(value)
    broken["bars"] = [
        row for row in broken["bars"]
        if not (str(row.get("symbol")) == "600519" and str(row.get("business_date")) == "2026-07-28")
    ]
    with tempfile.TemporaryDirectory() as directory:
        broken_path = Path(directory) / "missing-required-holding-bar.json"
        broken_path.write_text(
            json.dumps(broken, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        return assert_failure_parity(
            lambda: execute_continuity_backtest(broken_path),
            lambda: execute_continuity_backtest(broken_path),
        )


def _build_packages(
    result: dict[str, Any],
    trace: tuple[dict[str, Any], ...],
    input_path: Path,
    input_sha256: str,
    source_commit: str,
) -> tuple[SimulationPackage, SimulationPackage]:
    release = load_bounded_input(input_path)
    return build_continuity_packages(
        extract_transaction_day_evidence(result),
        extract_holding_day_evidence(result, trace, release),
        input_sha256=input_sha256,
        source_commit=source_commit,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify M3.6 disabled shadow execution parity")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--report-out", type=Path, required=True)
    args = parser.parse_args()
    source_commit = os.environ.get("GITHUB_SHA", "").strip() or os.environ.get("SOURCE_COMMIT", "").strip()
    if not source_commit:
        raise RuntimeError("GITHUB_SHA or SOURCE_COMMIT is required for shadow parity lineage")

    reference_result, reference_input_sha256, reference_trace = execute_continuity_backtest(args.input)
    rehearsal_result, rehearsal_input_sha256, rehearsal_trace = execute_continuity_backtest(args.input)
    if reference_input_sha256 != rehearsal_input_sha256:
        raise ValueError("backtest and rehearsal input SHA-256 values differ")
    reference_normalized = _normalized_results(reference_result, reference_input_sha256)
    rehearsal_normalized = _normalized_results(rehearsal_result, rehearsal_input_sha256)
    reference_packages = _build_packages(
        reference_result, reference_trace, args.input, reference_input_sha256, source_commit,
    )
    rehearsal_packages = _build_packages(
        rehearsal_result, rehearsal_trace, args.input, rehearsal_input_sha256, source_commit,
    )
    parity = assert_execution_parity(
        reference_normalized,
        reference_trace,
        reference_packages,
        rehearsal_normalized,
        rehearsal_trace,
        rehearsal_packages,
    )
    shadow_packages = to_disabled_shadow_packages(rehearsal_packages)

    values = read_env_file()
    url = env_value("VITE_SUPABASE_URL", values)
    service_key = env_value("SUPABASE_SERVICE_ROLE_KEY", values)
    public_key = env_value("VITE_SUPABASE_PUBLISHABLE_KEY", values)
    if not public_key:
        raise RuntimeError("VITE_SUPABASE_PUBLISHABLE_KEY is required for public rejection probes")
    shadow_ledger = run_continuity_acceptance(
        SupabaseLedgerClient(url, service_key),
        SupabaseLedgerClient(url, public_key),
        *shadow_packages,
    )
    report = {
        "schema_version": SCHEMA_VERSION,
        "accepted": True,
        "simulation_only": True,
        "activation_state": "disabled_acceptance",
        "authoritative_account_write": False,
        "rehearsal_environment": "shadow",
        "engine": {"name": "rqalpha", "version": "6.2.1", "run_type": "deterministic_backtest"},
        "input_sha256": reference_input_sha256,
        "source_commit": source_commit,
        "strategy_purpose": "execution_parity_acceptance_not_performance_evidence",
        "parity": parity,
        "technical_identity_policy": "engine order and exec ids may differ; lineage aliases and economics must match",
        "missing_required_bar_failure": missing_bar_failure_parity(args.input),
        "shadow_ledger": shadow_ledger,
        "production_schedule_enabled": False,
        "paper_trading_enabled": False,
    }
    args.report_out.parent.mkdir(parents=True, exist_ok=True)
    args.report_out.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
