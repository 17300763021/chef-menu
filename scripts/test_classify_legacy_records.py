from copy import deepcopy
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from capture_legacy_evidence import LEGACY_TABLES, canonical_json, sha256
from classify_legacy_records import build_run, classify_all


def empty_tables():
    return {table: [] for table in LEGACY_TABLES}


def order(order_id, status="filled", side="sell", pnl="-100"):
    is_filled = status == "filled"
    return {
        "id": order_id,
        "created_at": "2026-07-13T05:00:05+00:00",
        "code": "600001",
        "side": side,
        "status": status,
        "price": "9.9" if is_filled else "10",
        "shares": 100 if is_filled else 0,
        "amount": "990" if is_filled else "0",
        "cash_before": ("1000" if side == "sell" else "2000") if is_filled else "0",
        "cash_after": ("1990" if side == "sell" else "1010") if is_filled else "0",
        "position_shares_before": 100 if side == "sell" else 0,
        "position_shares_after": (0 if side == "sell" else 100) if is_filled else (100 if side == "sell" else 0),
        "realized_pnl": pnl if is_filled else "0",
        "failure_reason": "limit-down sell blocked" if status == "blocked" else "",
    }


def trade(trade_id, code="600001", sell_price="9.9", pnl="-100", created_at="2026-07-13T05:00:00+00:00"):
    return {
        "id": trade_id,
        "created_at": created_at,
        "code": code,
        "buy_date": "2026-07-10",
        "sell_date": "2026-07-13",
        "cost_price": "10.9",
        "sell_price": sell_price,
        "shares": 100,
        "pnl_amount": pnl,
    }


class ClassifyLegacyRecordsTest(unittest.TestCase):
    def fixture(self):
        tables = empty_tables()
        tables["stock_auto_trade_orders"] = [
            order("filled-order"),
            order("blocked-order", status="blocked", pnl="0"),
        ]
        tables["stock_trade_history"] = [
            trade("matched-trade"),
            trade("orphan-first", code="600002", sell_price="8.8", pnl="-210", created_at="2026-07-13T06:00:00+00:00"),
            trade("orphan-repeat", code="600002", sell_price="8.8", pnl="-210", created_at="2026-07-13T06:10:00+00:00"),
        ]
        tables["stock_positions"] = [{
            "id": "open-position",
            "code": "600002",
            "buy_date": "2026-07-10",
            "shares": 100,
            "status": "open",
        }]
        return tables

    def test_classifies_matched_orphan_duplicate_and_blocked_records(self):
        rows, report = classify_all(self.fixture(), "evidence-test")
        by_id = {row["source_record_id"]: row for row in rows}
        self.assertEqual(by_id["filled-order"]["disposition"], "authoritative_candidate")
        self.assertEqual(by_id["blocked-order"]["disposition"], "audit_only")
        self.assertEqual(by_id["matched-trade"]["disposition"], "derived_projection")
        self.assertEqual(by_id["orphan-first"]["classification_code"], "polluted_orphan_trade")
        self.assertEqual(by_id["orphan-repeat"]["classification_code"], "polluted_duplicate_orphan_trade")
        self.assertEqual(by_id["orphan-repeat"]["evidence"]["duplicate_of"], "orphan-first")
        self.assertEqual(report["excluded_count"], 2)
        self.assertEqual(report["excluded_pnl"], "-420.00")
        self.assertEqual(report["legitimate_filled_orders_excluded"], 0)

    def test_repeated_classification_has_identical_hash(self):
        first_rows, first_report = classify_all(self.fixture(), "evidence-test")
        second_rows, second_report = classify_all(deepcopy(self.fixture()), "evidence-test")
        self.assertEqual(sha256(canonical_json(first_rows)), sha256(canonical_json(second_rows)))
        self.assertEqual(first_report, second_report)

    def test_unmatched_trade_without_open_position_requires_review_not_exclusion(self):
        tables = empty_tables()
        tables["stock_trade_history"] = [trade("ambiguous", code="600009")]
        rows, report = classify_all(tables, "evidence-test")
        self.assertEqual(rows[0]["disposition"], "review_required")
        self.assertEqual(report["excluded_count"], 0)

    def test_run_hash_is_independent_of_classification_time(self):
        rows, report = classify_all(self.fixture(), "evidence-test")
        manifest = {
            "evidence_id": "evidence-test",
            "archive_sha256": "a" * 64,
            "source_commit": "source",
        }
        first, _ = build_run(manifest, deepcopy(rows), report)
        second, _ = build_run(manifest, deepcopy(rows), report)
        self.assertEqual(first["classification_run_id"], second["classification_run_id"])
        self.assertEqual(first["classification_sha256"], second["classification_sha256"])

    def test_migration_uses_atomic_rpc_and_append_only_triggers(self):
        migrations = sorted((ROOT / "supabase" / "migrations").glob("*_add_legacy_record_classifications.sql"))
        self.assertEqual(len(migrations), 1)
        sql = migrations[0].read_text(encoding="utf-8").lower()
        self.assertIn("publish_legacy_record_classification", sql)
        self.assertIn("before update or delete or truncate", sql)
        self.assertIn("security invoker", sql)
        self.assertIn("revoke all on table public.legacy_record_classifications from anon, authenticated", sql)


if __name__ == "__main__":
    unittest.main()
