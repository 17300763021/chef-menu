"""Rebuild frozen legacy account baselines from classified M0 forensic evidence."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import quote

from capture_legacy_evidence import OnlineClient, canonical_json, env_value, read_env_file, sha256, source_commit
from classify_legacy_records import DEFAULT_EVIDENCE_ID, RULE_SET_VERSION as CLASSIFICATION_RULES, classify_all, load_evidence_archive


BASELINE_RULE_SET = "legacy-account-baseline-v1"
INITIAL_CAPITAL = Decimal("1000000")
CENT = Decimal("0.01")


def d(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def money(value: Decimal) -> str:
    rounded = value.quantize(CENT, rounding=ROUND_HALF_UP)
    return "0.00" if rounded == 0 else str(rounded)


def result_hash(value: dict[str, Any]) -> str:
    return sha256(canonical_json(value))


def latest(rows: list[dict[str, Any]], time_field: str) -> dict[str, Any] | None:
    return max(rows, key=lambda row: (str(row.get(time_field) or ""), str(row.get("id") or ""))) if rows else None


def original_metrics(
    account_key: str,
    trades: list[dict[str, Any]],
    references: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
) -> dict[str, Any]:
    realized = sum((d(row.get("pnl_amount")) for row in trades), Decimal(0))
    floating = sum((d(row.get("floating_pnl")) for row in references), Decimal(0))
    market_value = sum((d(row.get("market_value")) for row in references), Decimal(0))
    total_pnl = realized + floating
    total_assets = INITIAL_CAPITAL + total_pnl
    snapshot = latest(snapshots, "snapshot_time")
    return {
        "account_key": account_key,
        "cash": money(total_assets - market_value),
        "holding_market_value": money(market_value),
        "realized_pnl": money(realized),
        "floating_pnl": money(floating),
        "total_pnl": money(total_pnl),
        "total_assets": money(total_assets),
        "latest_stored_snapshot": snapshot,
        "trust_status": "polluted_or_non_authoritative",
    }


def replay_account(
    account_key: str,
    account_label: str,
    filled_orders: list[dict[str, Any]],
    reference_positions: list[dict[str, Any]],
    original: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    cash = INITIAL_CAPITAL
    holdings: dict[str, dict[str, Any]] = {}
    entries: list[dict[str, Any]] = []
    realized = Decimal(0)
    fee_total = Decimal(0)
    slippage_total = Decimal(0)
    cash_field_mismatches = 0

    ordered = sorted(filled_orders, key=lambda row: (str(row.get("created_at") or ""), str(row.get("id") or "")))
    for sequence_no, order in enumerate(ordered, 1):
        code = str(order.get("code") or "")
        side = str(order.get("side") or "").lower()
        shares = int(order.get("shares") or 0)
        price = d(order.get("price"))
        gross = d(order.get("amount"))
        fee = d(order.get("fee_amount"))
        slippage = d(order.get("slippage_amount"))
        if shares <= 0 or price <= 0 or gross <= 0 or abs(gross - price * shares) > CENT:
            raise RuntimeError(f"{account_key}订单{order.get('id')}成交字段无效。")
        holding = holdings.setdefault(code, {
            "code": code,
            "name": str(order.get("name") or ""),
            "shares": 0,
            "book_cost": Decimal(0),
        })
        position_before = int(holding["shares"])
        if int(order.get("position_shares_before") or 0) != position_before:
            raise RuntimeError(f"{account_key}订单{order.get('id')}成交前持仓不连续。")
        cash_before = cash
        event_realized = Decimal(0)
        if side == "buy":
            cash -= gross + fee
            holding["shares"] += shares
            holding["book_cost"] += gross + fee
        elif side == "sell":
            if position_before < shares or position_before <= 0:
                raise RuntimeError(f"{account_key}订单{order.get('id')}卖出超过可用持仓。")
            average_cost = holding["book_cost"] / position_before
            allocated_cost = average_cost * shares
            cash += gross - fee
            event_realized = gross - fee - allocated_cost
            realized += event_realized
            holding["shares"] -= shares
            holding["book_cost"] -= allocated_cost
        else:
            raise RuntimeError(f"{account_key}订单{order.get('id')}方向无效。")
        position_after = int(holding["shares"])
        if int(order.get("position_shares_after") or 0) != position_after:
            raise RuntimeError(f"{account_key}订单{order.get('id')}成交后持仓不连续。")
        recorded_before = d(order.get("cash_before"))
        recorded_after = d(order.get("cash_after"))
        before_difference = recorded_before - cash_before
        after_difference = recorded_after - cash
        if abs(before_difference) > CENT or abs(after_difference) > CENT:
            cash_field_mismatches += 1
        fee_total += fee
        slippage_total += slippage
        entry = {
            "account_key": account_key,
            "sequence_no": sequence_no,
            "source_order_id": str(order.get("id") or ""),
            "fill_evidence_kind": "filled_order_surrogate",
            "event_time": order.get("created_at"),
            "code": code,
            "name": holding["name"],
            "side": side,
            "price": money(price),
            "shares": shares,
            "gross_amount": money(gross),
            "fee_amount": money(fee),
            "slippage_amount": money(slippage),
            "reconstructed_cash_before": money(cash_before),
            "reconstructed_cash_after": money(cash),
            "recorded_cash_before": money(recorded_before),
            "recorded_cash_after": money(recorded_after),
            "cash_before_difference": money(before_difference),
            "cash_after_difference": money(after_difference),
            "position_shares_before": position_before,
            "position_shares_after": position_after,
            "reconstructed_realized_pnl": money(event_realized),
            "recorded_realized_pnl": money(d(order.get("realized_pnl"))),
        }
        entry["result_sha256"] = result_hash(entry)
        entries.append(entry)

    references_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in reference_positions:
        if str(row.get("status") or "").lower() == "open":
            references_by_code[str(row.get("code") or "")].append(row)

    all_codes = set(holdings) | set(references_by_code)
    positions: list[dict[str, Any]] = []
    for code in sorted(all_codes):
        holding = holdings.get(code, {"shares": 0, "book_cost": Decimal(0), "name": ""})
        replayed_shares = int(holding["shares"])
        reference_shares = sum(int(row.get("shares") or 0) for row in references_by_code.get(code, []))
        if replayed_shares != reference_shares:
            raise RuntimeError(f"{account_key}股票{code}最终持仓差异：重放{replayed_shares}，参考{reference_shares}。")
        if replayed_shares <= 0:
            continue
        refs = references_by_code[code]
        if not refs:
            raise RuntimeError(f"{account_key}股票{code}缺少冻结估值，禁止猜测。")
        market_value = sum((d(row.get("market_value")) for row in refs), Decimal(0))
        reference_price = market_value / replayed_shares
        if reference_price <= 0:
            raise RuntimeError(f"{account_key}股票{code}冻结估值无效。")
        book_cost = d(holding["book_cost"])
        position = {
            "account_key": account_key,
            "code": code,
            "name": str(holding.get("name") or refs[0].get("name") or ""),
            "shares": replayed_shares,
            "total_book_cost": money(book_cost),
            "average_book_cost": money(book_cost / replayed_shares),
            "reference_price": money(reference_price),
            "market_value": money(market_value),
            "floating_pnl": money(market_value - book_cost),
            "source_position_ids": [str(row.get("id") or "") for row in refs],
        }
        position["result_sha256"] = result_hash(position)
        positions.append(position)

    holding_market_value = sum((d(row["market_value"]) for row in positions), Decimal(0))
    floating = sum((d(row["floating_pnl"]) for row in positions), Decimal(0))
    total_pnl = realized + floating
    total_assets = cash + holding_market_value
    equity_difference = total_assets - (INITIAL_CAPITAL + total_pnl)
    cash_equity_difference = total_assets - (cash + holding_market_value)
    if abs(equity_difference) > CENT or abs(cash_equity_difference) > CENT:
        raise RuntimeError(f"{account_key}权益会计等式不平。")
    account = {
        "account_key": account_key,
        "account_label": account_label,
        "initial_capital": money(INITIAL_CAPITAL),
        "cash": money(cash),
        "holding_market_value": money(holding_market_value),
        "realized_pnl": money(realized),
        "floating_pnl": money(floating),
        "total_pnl": money(total_pnl),
        "total_assets": money(total_assets),
        "total_return_rate": money(total_pnl / INITIAL_CAPITAL * 100),
        "recorded_fee_total": money(fee_total),
        "recorded_slippage_total": money(slippage_total),
        "filled_order_count": len(entries),
        "open_position_count": len(positions),
        "original_metrics": original,
        "reconciliation": {
            "cash_plus_market_value_difference": money(cash_equity_difference),
            "initial_plus_pnl_difference": money(equity_difference),
            "final_position_share_difference": "0.00",
            "legacy_cash_field_mismatch_orders": cash_field_mismatches,
            "fill_evidence_kind": "filled_order_surrogate",
            "slippage_treatment": "embedded_in_execution_price_not_deducted_twice",
            "valuation_source": "frozen_M0.2_position_read_model",
        },
    }
    account["result_sha256"] = result_hash(account)
    return account, entries, positions


def get_online_classification(client: OnlineClient, evidence_id: str) -> dict[str, Any]:
    response, _, _ = client._request(
        "GET",
        f"{client.url}/rest/v1/legacy_reconciliation_runs?evidence_id=eq.{quote(evidence_id)}&rule_set_version=eq.{quote(CLASSIFICATION_RULES)}&select=*",
        headers=client._supabase_headers(),
    )
    rows = json.loads(response.decode("utf-8"))
    if len(rows) != 1:
        raise RuntimeError("M0.3分类运行不存在或不唯一。")
    return rows[0]


def build_baseline(client: OnlineClient, evidence_id: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    manifest, tables = load_evidence_archive(client, evidence_id)
    classifications, classification_report = classify_all(tables, evidence_id)
    classification_hash = sha256(canonical_json(classifications))
    online_classification = get_online_classification(client, evidence_id)
    if online_classification["classification_sha256"] != classification_hash:
        raise RuntimeError("M0.3分类哈希与固定证据重算结果不一致。")
    valid_order_ids = {
        row["source_record_id"] for row in classifications
        if row["classification_code"] == "valid_filled_order"
        and row["disposition"] == "authoritative_candidate"
    }

    main_orders = [row for row in tables["stock_auto_trade_orders"] if str(row.get("id")) in valid_order_ids]
    main_refs = [row for row in tables["stock_positions"] if str(row.get("status") or "").lower() == "open"]
    main_original = original_metrics(
        "legacy_main", tables["stock_trade_history"], main_refs, tables["stock_portfolio_snapshots"]
    )
    accounts: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    account, account_entries, account_positions = replay_account(
        "legacy_main", "旧主模拟账户（冻结审计基线）", main_orders, main_refs, main_original
    )
    accounts.append(account); entries.extend(account_entries); positions.extend(account_positions)

    model_keys = sorted({
        str(row.get("strategy_account") or "") for row in tables["stock_model_orders"] + tables["stock_model_positions"]
        if str(row.get("strategy_account") or "")
    })
    for model_key in model_keys:
        model_orders = [
            row for row in tables["stock_model_orders"]
            if str(row.get("id")) in valid_order_ids and str(row.get("strategy_account")) == model_key
        ]
        model_refs = [
            row for row in tables["stock_model_positions"]
            if str(row.get("strategy_account")) == model_key and str(row.get("status") or "").lower() == "open"
        ]
        model_trades = [row for row in tables["stock_model_trade_history"] if str(row.get("strategy_account")) == model_key]
        model_snapshots = [row for row in tables["stock_model_portfolio_snapshots"] if str(row.get("strategy_account")) == model_key]
        original = original_metrics(model_key, model_trades, model_refs, model_snapshots)
        account, account_entries, account_positions = replay_account(
            model_key, f"旧模型模拟账户（冻结审计基线）：{model_key}", model_orders, model_refs, original
        )
        accounts.append(account); entries.extend(account_entries); positions.extend(account_positions)

    stable_payload = {"accounts": accounts, "entries": entries, "positions": positions}
    baseline_hash = sha256(canonical_json(stable_payload))
    run_id = f"legacy-baseline-{BASELINE_RULE_SET}-{baseline_hash[:12]}"
    for collection in (accounts, entries, positions):
        for row in collection:
            row["baseline_run_id"] = run_id
    report = {
        "evidence_id": evidence_id,
        "classification_run_id": online_classification["classification_run_id"],
        "classification_sha256": classification_hash,
        "classification_report": classification_report,
        "account_comparisons": [
            {
                "account_key": row["account_key"],
                "original": row["original_metrics"],
                "reconciled": {
                    key: row[key] for key in (
                        "cash", "holding_market_value", "realized_pnl", "floating_pnl",
                        "total_pnl", "total_assets", "recorded_fee_total", "recorded_slippage_total"
                    )
                },
            }
            for row in accounts
        ],
        "acceptance": {
            "cash_equity_difference": "0.00",
            "position_share_difference": "0.00",
            "initial_plus_pnl_difference": "0.00",
            "legitimate_orders_excluded": classification_report["legitimate_filled_orders_excluded"],
            "source_data_mutated": False,
        },
    }
    run = {
        "baseline_run_id": run_id,
        "evidence_id": evidence_id,
        "classification_run_id": online_classification["classification_run_id"],
        "rule_set_version": BASELINE_RULE_SET,
        "source_archive_sha256": manifest["archive_sha256"],
        "classification_sha256": classification_hash,
        "baseline_sha256": baseline_hash,
        "classifier_commit": source_commit(),
        "rebuilt_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "account_count": len(accounts),
        "entry_count": len(entries),
        "position_count": len(positions),
        "report": report,
        "status": "frozen",
    }
    return run, accounts, entries, positions


def publish(client: OnlineClient, run: dict[str, Any], accounts: list[dict[str, Any]], entries: list[dict[str, Any]], positions: list[dict[str, Any]]) -> dict[str, Any]:
    payload = canonical_json({
        "run_payload": run,
        "account_payload": accounts,
        "entry_payload": entries,
        "position_payload": positions,
    })
    response, _, _ = client._request(
        "POST", f"{client.url}/rest/v1/rpc/publish_legacy_account_baseline",
        body=payload, headers=client._supabase_headers(),
    )
    return json.loads(response.decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild frozen legacy account baseline")
    parser.add_argument("--evidence-id", default=DEFAULT_EVIDENCE_ID)
    parser.add_argument("--report-out", type=Path, default=Path("legacy-baseline-report.json"))
    args = parser.parse_args()
    env = read_env_file()
    client = OnlineClient(env_value("VITE_SUPABASE_URL", env), env_value("SUPABASE_SERVICE_ROLE_KEY", env))
    run, accounts, entries, positions = build_baseline(client, args.evidence_id)
    publish_result = publish(client, run, accounts, entries, positions)
    output = {"run": run, "accounts": accounts, "publish_result": publish_result}
    args.report_out.parent.mkdir(parents=True, exist_ok=True)
    args.report_out.write_bytes(canonical_json(output))
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
