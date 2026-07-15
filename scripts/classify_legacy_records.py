"""Classify every frozen legacy record from the M0.2 forensic archive."""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any

from capture_legacy_evidence import (
    LEGACY_TABLES,
    OnlineClient,
    canonical_json,
    env_value,
    read_env_file,
    sha256,
    source_commit,
)


DEFAULT_EVIDENCE_ID = "legacy-20260715T031710984732Z-2ddcee6336bb"
RULE_SET_VERSION = "legacy-reconciliation-v1"
ORDER_TABLES = {"stock_auto_trade_orders", "stock_model_orders"}
TRADE_TABLES = {
    "stock_trade_history": "stock_auto_trade_orders",
    "stock_model_trade_history": "stock_model_orders",
}
REFERENCE_TABLES = {
    "stock_positions",
    "stock_portfolio_snapshots",
    "stock_model_positions",
    "stock_model_portfolio_snapshots",
}


def number(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    return Decimal(str(value))


def timestamp(value: Any) -> datetime:
    text = str(value or "").replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def order_is_structurally_valid(row: dict[str, Any]) -> bool:
    side = str(row.get("side") or "").lower()
    shares = int(row.get("shares") or 0)
    before = int(row.get("position_shares_before") or 0)
    after = int(row.get("position_shares_after") or 0)
    cash_before = number(row.get("cash_before"))
    cash_after = number(row.get("cash_after"))
    if number(row.get("price")) <= 0 or shares <= 0 or number(row.get("amount")) <= 0:
        return False
    if side == "buy":
        return after - before == shares and cash_after < cash_before
    if side == "sell":
        return before - after == shares and cash_after > cash_before
    return False


def nonfill_is_structurally_valid(row: dict[str, Any]) -> bool:
    return (
        int(row.get("shares") or 0) == 0
        and number(row.get("amount")) == 0
        and number(row.get("realized_pnl")) == 0
        and int(row.get("position_shares_before") or 0)
        == int(row.get("position_shares_after") or 0)
    )


def matching_orders(trade: dict[str, Any], orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for order in orders:
        if str(order.get("status") or "").lower() != "filled" or str(order.get("side") or "").lower() != "sell":
            continue
        if str(order.get("code") or "") != str(trade.get("code") or ""):
            continue
        if int(order.get("shares") or 0) != int(trade.get("shares") or 0):
            continue
        if abs(number(order.get("realized_pnl")) - number(trade.get("pnl_amount"))) >= Decimal("0.01"):
            continue
        if abs((timestamp(order.get("created_at")) - timestamp(trade.get("created_at"))).total_seconds()) > 60:
            continue
        matches.append(order)
    return matches


def trade_fingerprint(row: dict[str, Any]) -> tuple[str, ...]:
    fields = ("code", "buy_date", "sell_date", "cost_price", "sell_price", "shares", "pnl_amount")
    return tuple(str(row.get(field) or "") for field in fields)


def base_result(
    evidence_id: str,
    table: str,
    row: dict[str, Any],
    classification_code: str,
    disposition: str,
    rule_id: str,
    reason: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    result = {
        "evidence_id": evidence_id,
        "rule_set_version": RULE_SET_VERSION,
        "source_table": table,
        "source_record_id": str(row.get("id") or ""),
        "source_record_sha256": sha256(canonical_json(row)),
        "classification_code": classification_code,
        "disposition": disposition,
        "rule_id": rule_id,
        "reason": reason,
        "evidence": evidence,
    }
    result["record_result_sha256"] = sha256(canonical_json(result))
    return result


def classify_order(evidence_id: str, table: str, row: dict[str, Any]) -> dict[str, Any]:
    status = str(row.get("status") or "").lower()
    common_evidence = {
        "status": status,
        "side": row.get("side"),
        "price": row.get("price"),
        "shares": row.get("shares"),
        "amount": row.get("amount"),
        "position_shares_before": row.get("position_shares_before"),
        "position_shares_after": row.get("position_shares_after"),
        "cash_before": row.get("cash_before"),
        "cash_after": row.get("cash_after"),
    }
    if status == "filled" and order_is_structurally_valid(row):
        return base_result(
            evidence_id, table, row, "valid_filled_order", "authoritative_candidate",
            "LCR-ORD-001", "已成交订单的价格、数量、金额、现金方向和持仓变化结构一致。", common_evidence,
        )
    if status == "filled":
        return base_result(
            evidence_id, table, row, "review_invalid_filled_order", "review_required",
            "LCR-ORD-002", "订单标记为已成交，但经济字段或现金/持仓变化不完整，禁止自动排除。", common_evidence,
        )
    if status in {"blocked", "failed"} and nonfill_is_structurally_valid(row):
        common_evidence["failure_reason"] = row.get("failure_reason")
        return base_result(
            evidence_id, table, row, "valid_nonfill_order", "audit_only",
            "LCR-ORD-003", "失败或受阻订单为零成交、零盈亏且未改变持仓，保留为合法审计事件。", common_evidence,
        )
    return base_result(
        evidence_id, table, row, "review_unknown_order_state", "review_required",
        "LCR-ORD-004", "订单状态或经济字段不满足已登记规则，保留并等待人工复核。", common_evidence,
    )


def classify_all(tables: dict[str, list[dict[str, Any]]], evidence_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    trade_context: dict[tuple[str, str], dict[str, Any]] = {}
    orphan_groups: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = {}

    for trade_table, order_table in TRADE_TABLES.items():
        orders = tables.get(order_table, [])
        positions_table = "stock_model_positions" if trade_table.startswith("stock_model_") else "stock_positions"
        positions = tables.get(positions_table, [])
        for trade in tables.get(trade_table, []):
            matches = matching_orders(trade, orders)
            open_positions = [
                position for position in positions
                if str(position.get("status") or "").lower() == "open"
                and str(position.get("code") or "") == str(trade.get("code") or "")
                and str(position.get("buy_date") or "") == str(trade.get("buy_date") or "")
                and int(position.get("shares") or 0) == int(trade.get("shares") or 0)
            ]
            key = (trade_table, str(trade.get("id") or ""))
            trade_context[key] = {"matches": matches, "open_positions": open_positions}
            if not matches and open_positions:
                orphan_groups.setdefault((trade_table, trade_fingerprint(trade)), []).append(trade)

    duplicate_of: dict[tuple[str, str], str] = {}
    for (table, _), rows in orphan_groups.items():
        ordered = sorted(rows, key=lambda row: (timestamp(row.get("created_at")), str(row.get("id") or "")))
        for repeated in ordered[1:]:
            duplicate_of[(table, str(repeated.get("id") or ""))] = str(ordered[0].get("id") or "")

    results: list[dict[str, Any]] = []
    for table in LEGACY_TABLES:
        for row in tables.get(table, []):
            if table in ORDER_TABLES:
                result = classify_order(evidence_id, table, row)
            elif table in TRADE_TABLES:
                context = trade_context[(table, str(row.get("id") or ""))]
                matches = context["matches"]
                open_positions = context["open_positions"]
                if len(matches) == 1:
                    result = base_result(
                        evidence_id, table, row, "valid_matched_trade_projection", "derived_projection",
                        "LCR-TRD-001", "成交历史可与一条已成交卖单唯一对应；它是订单的派生投影，不能再次计入权威账本。",
                        {"matched_order_id": matches[0].get("id"), "match_window_seconds": 60, "pnl_tolerance": "0.01"},
                    )
                elif len(matches) == 0 and len(open_positions) == 1:
                    source_id = str(row.get("id") or "")
                    original = duplicate_of.get((table, source_id))
                    result = base_result(
                        evidence_id,
                        table,
                        row,
                        "polluted_duplicate_orphan_trade" if original else "polluted_orphan_trade",
                        "excluded_polluted",
                        "LCR-TRD-003" if original else "LCR-TRD-002",
                        "没有对应已成交卖单，且相同买入批次和股数的持仓仍为开放状态；这是失败卖出流程产生的孤立成交历史。",
                        {
                            "matching_filled_sell_count": 0,
                            "open_position_id": open_positions[0].get("id"),
                            "open_position_shares": open_positions[0].get("shares"),
                            "duplicate_of": original,
                        },
                    )
                else:
                    result = base_result(
                        evidence_id, table, row, "review_ambiguous_trade", "review_required",
                        "LCR-TRD-004", "成交历史无法形成唯一订单匹配，或缺少仍开放持仓证据；禁止自动排除。",
                        {
                            "matching_filled_sell_count": len(matches),
                            "matching_order_ids": [item.get("id") for item in matches],
                            "open_position_ids": [item.get("id") for item in open_positions],
                        },
                    )
            elif table in REFERENCE_TABLES:
                result = base_result(
                    evidence_id, table, row, "reference_read_model", "reference_only",
                    "LCR-REF-001", "持仓或快照是历史状态读模型，仅用于M0.4对账比较，不作为新增权威成交事件。",
                    {"status": row.get("status"), "snapshot_time": row.get("snapshot_time")},
                )
            else:
                result = base_result(
                    evidence_id, table, row, "review_unhandled_record", "review_required",
                    "LCR-GEN-001", "记录来源尚无确定性规则，保留并等待人工复核。", {},
                )
            results.append(result)

    results.sort(key=lambda item: (item["source_table"], item["source_record_id"]))
    category_counts = dict(sorted(Counter(item["classification_code"] for item in results).items()))
    disposition_counts = dict(sorted(Counter(item["disposition"] for item in results).items()))
    excluded_ids = {
        item["source_record_id"] for item in results if item["disposition"] == "excluded_polluted"
    }
    trade_rows = [row for table in TRADE_TABLES for row in tables.get(table, [])]
    excluded_pnl = sum(
        (number(row.get("pnl_amount")) for row in trade_rows if str(row.get("id")) in excluded_ids),
        Decimal("0"),
    )
    report = {
        "record_count": len(results),
        "category_counts": category_counts,
        "disposition_counts": disposition_counts,
        "excluded_count": len(excluded_ids),
        "excluded_pnl": str(excluded_pnl.quantize(Decimal("0.01"))),
        "legitimate_filled_orders_excluded": sum(
            1 for item in results
            if item["source_table"] in ORDER_TABLES
            and item["classification_code"] == "valid_filled_order"
            and item["disposition"] == "excluded_polluted"
        ),
        "review_required_count": disposition_counts.get("review_required", 0),
    }
    return results, report


def load_evidence_archive(client: OnlineClient, evidence_id: str) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    online_manifest = client.fetch_manifest(evidence_id)
    archive = client.download_archive(str(online_manifest["storage_path"]))
    if sha256(archive) != online_manifest["archive_sha256"]:
        raise RuntimeError("M0.2证据包SHA-256复核失败。")
    if len(archive) != int(online_manifest["archive_size_bytes"]):
        raise RuntimeError("M0.2证据包大小复核失败。")
    tables: dict[str, list[dict[str, Any]]] = {}
    with zipfile.ZipFile(BytesIO(archive), "r") as bundle:
        for table in LEGACY_TABLES:
            data = bundle.read(f"tables/{table}.json")
            if sha256(data) != online_manifest["table_hashes"][table]:
                raise RuntimeError(f"{table}表哈希复核失败。")
            rows = json.loads(data.decode("utf-8"))
            if len(rows) != int(online_manifest["table_counts"][table]):
                raise RuntimeError(f"{table}表行数复核失败。")
            tables[table] = rows
    return online_manifest, tables


def publish_classification(client: OnlineClient, run: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    payload = canonical_json({"run_payload": run, "classification_payload": rows})
    response, _, _ = client._request(
        "POST",
        f"{client.url}/rest/v1/rpc/publish_legacy_record_classification",
        body=payload,
        headers=client._supabase_headers(),
    )
    return json.loads(response.decode("utf-8"))


def build_run(
    online_manifest: dict[str, Any],
    rows: list[dict[str, Any]],
    report: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    stable_rows = [{key: value for key, value in row.items() if key != "classification_run_id"} for row in rows]
    classification_sha256 = sha256(canonical_json(stable_rows))
    run_id = f"legacy-recon-{RULE_SET_VERSION}-{classification_sha256[:12]}"
    for row in rows:
        row["classification_run_id"] = run_id
    run = {
        "classification_run_id": run_id,
        "evidence_id": online_manifest["evidence_id"],
        "rule_set_version": RULE_SET_VERSION,
        "source_archive_sha256": online_manifest["archive_sha256"],
        "evidence_source_commit": online_manifest["source_commit"],
        "classifier_commit": source_commit(),
        "classified_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "classification_sha256": classification_sha256,
        "record_count": report["record_count"],
        "category_counts": report["category_counts"],
        "disposition_counts": report["disposition_counts"],
        "excluded_count": report["excluded_count"],
        "excluded_pnl": report["excluded_pnl"],
        "report": report,
    }
    return run, rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify frozen legacy evidence")
    parser.add_argument("--evidence-id", default=DEFAULT_EVIDENCE_ID)
    parser.add_argument("--report-out", type=Path, default=Path("legacy-reconciliation-report.json"))
    args = parser.parse_args()
    env = read_env_file()
    client = OnlineClient(
        env_value("VITE_SUPABASE_URL", env),
        env_value("SUPABASE_SERVICE_ROLE_KEY", env),
    )
    online_manifest, tables = load_evidence_archive(client, args.evidence_id)
    rows, report = classify_all(tables, args.evidence_id)
    if report["legitimate_filled_orders_excluded"] != 0:
        raise RuntimeError("合法已成交订单被错误排除，停止发布。")
    if report["review_required_count"] != 0:
        raise RuntimeError("存在待人工复核记录，停止自动发布。")
    if report["record_count"] != sum(len(tables[table]) for table in LEGACY_TABLES):
        raise RuntimeError("分类没有覆盖全部证据记录，停止发布。")
    run, rows = build_run(online_manifest, rows, report)
    result = publish_classification(client, run, rows)
    output = {"run": run, "publish_result": result}
    args.report_out.parent.mkdir(parents=True, exist_ok=True)
    args.report_out.write_bytes(canonical_json(output))
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
