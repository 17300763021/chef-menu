from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import Mock, patch

import pandas as pd

from scripts.market_data.fundamental_contracts import FundamentalFact, FundamentalReport
from scripts.market_data.fundamental_quality_gates import evaluate_fundamentals
from scripts.market_data.fundamental_runner import _connection_configs, _load_market_scope, reusable_checkpoint
from scripts.market_data.quality_gates import accepted
from scripts.market_data.sources.eastmoney_fundamental_source import EastmoneyFundamentalSource


def frame(statement: str) -> pd.DataFrame:
    row = {
        "SECURITY_CODE": "000001", "REPORT_DATE": "2025-12-31", "NOTICE_DATE": "2026-03-21",
        "UPDATE_DATE": "2026-04-25", "REPORT_TYPE": "annual", "CURRENCY": "CNY", "ORG_TYPE": "bank",
    }
    if statement == "balance":
        row.update(TOTAL_ASSETS="1000", TOTAL_LIABILITIES="700", TOTAL_EQUITY="300", TOTAL_PARENT_EQUITY="280")
    elif statement == "income":
        row.update(OPERATE_INCOME="100", OPERATE_PROFIT="30", TOTAL_PROFIT="29", NETPROFIT="20", PARENT_NETPROFIT="19")
    else:
        row.update(NETCASH_OPERATE="25", NETCASH_INVEST="-10", NETCASH_FINANCE="-5", CCE_ADD="10")
    return pd.DataFrame([row])


class FundamentalTests(unittest.TestCase):
    def test_dual_connection_configs_keep_market_read_and_research_write_distinct(self) -> None:
        env = {
            "TIDB_HOST": "research.example", "TIDB_PORT": "4000", "TIDB_USER": "research",
            "TIDB_PASSWORD": "research-secret", "TIDB_DATABASE": "chef_menu_research",
            "TIDB_MARKET_HOST": "market.example", "TIDB_MARKET_PORT": "4000", "TIDB_MARKET_USER": "market",
            "TIDB_MARKET_PASSWORD": "market-secret", "TIDB_MARKET_DATABASE": "chef_menu_market",
        }
        market, research = _connection_configs(env)
        self.assertEqual(market.database, "chef_menu_market")
        self.assertEqual(research.database, "chef_menu_research")

    def test_dual_connection_configs_reject_same_database_identity(self) -> None:
        env = {
            "TIDB_HOST": "same.example", "TIDB_PORT": "4000", "TIDB_USER": "same",
            "TIDB_PASSWORD": "one", "TIDB_DATABASE": "same_db",
            "TIDB_MARKET_HOST": "same.example", "TIDB_MARKET_PORT": "4000", "TIDB_MARKET_USER": "same",
            "TIDB_MARKET_PASSWORD": "two", "TIDB_MARKET_DATABASE": "same_db",
        }
        with self.assertRaisesRegex(RuntimeError, "must be distinct"):
            _connection_configs(env)

    @patch("scripts.market_data.fundamental_runner._scope")
    @patch("scripts.market_data.fundamental_runner.connect")
    def test_market_connection_is_closed_after_scope_only_read(self, mock_connect: Mock, mock_scope: Mock) -> None:
        connection = Mock()
        mock_connect.return_value = connection
        mock_scope.return_value = ["scope-row"]
        config = Mock()
        result = _load_market_scope(config, "base", "sample")
        self.assertEqual(result, ["scope-row"])
        mock_scope.assert_called_once_with(connection, "base", "sample")
        connection.close.assert_called_once_with()

    def test_effective_date_uses_later_update_and_prevents_lookahead(self) -> None:
        report = FundamentalReport.build(
            symbol="000001", statement_type="balance", report_date="2025-12-31",
            notice_date="2026-03-21", update_date="2026-04-25", report_type="annual",
            currency="CNY", organization_type="bank", source="fixture", source_row={"a": 1},
        )
        self.assertEqual(report.effective_on, date(2026, 4, 25))
        with self.assertRaisesRegex(ValueError, "precede"):
            FundamentalReport.build(
                symbol="000001", statement_type="balance", report_date="2025-12-31",
                notice_date="2025-01-01", update_date="2026-01-01", report_type="annual",
                currency="CNY", organization_type="bank", source="fixture", source_row={},
            )

    def test_source_keeps_only_versions_known_by_as_of_date(self) -> None:
        loaders = {kind: (lambda _symbol, kind=kind: frame(kind)) for kind in ("balance", "income", "cashflow")}
        source = EastmoneyFundamentalSource(loaders=loaders)
        before_reports, _ = source.fetch("000001", history_start=date(2017, 1, 1), as_of_date=date(2026, 4, 24))
        self.assertEqual(before_reports, [])
        reports, facts = source.fetch("000001", history_start=date(2017, 1, 1), as_of_date=date(2026, 4, 25))
        self.assertEqual(len(reports), 3)
        self.assertGreaterEqual(len(facts), 12)
        self.assertTrue(all(row.effective_on == date(2026, 4, 25) for row in facts))

    def test_quality_gates_accept_complete_balanced_fixture(self) -> None:
        loaders = {kind: (lambda _symbol, kind=kind: frame(kind)) for kind in ("balance", "income", "cashflow")}
        reports, facts = EastmoneyFundamentalSource(loaders=loaders).fetch(
            "000001", history_start=date(2017, 1, 1), as_of_date=date(2026, 4, 25),
        )
        gates = evaluate_fundamentals(
            expected_symbols=["000001"], reports=reports, facts=facts,
            successful_symbols={"000001"}, excluded_symbols=set(),
        )
        self.assertTrue(accepted(gates), [gate.canonical() for gate in gates])

    def test_broken_accounting_equation_fails(self) -> None:
        report = FundamentalReport.build(
            symbol="000001", statement_type="balance", report_date="2025-12-31", notice_date="2026-03-21",
            update_date="2026-03-21", report_type="annual", currency="CNY", organization_type="bank",
            source="fixture", source_row={},
        )
        facts = [FundamentalFact(report.version_id, "000001", "balance", report.report_date, report.effective_on, metric, value)
                 for metric, value in (("TOTAL_ASSETS", 1000), ("TOTAL_LIABILITIES", 700), ("TOTAL_EQUITY", 100))]
        gates = evaluate_fundamentals(expected_symbols=["000001"], reports=[report], facts=facts, successful_symbols={"000001"})
        self.assertFalse(next(g for g in gates if g.name == "balance_sheet_accounting_equation").passed)

    def test_declared_usd_is_preserved_but_fact_unit_must_match(self) -> None:
        report = FundamentalReport.build(
            symbol="688981", statement_type="balance", report_date="2019-06-30",
            notice_date="2019-08-30", update_date="2019-08-30", report_type="中报",
            currency="USD", organization_type="通用", source="fixture", source_row={"currency": "USD"},
        )
        facts = [
            FundamentalFact(report.version_id, report.symbol, report.statement_type, report.report_date,
                            report.effective_on, "TOTAL_ASSETS", Decimal("100"), "USD"),
            FundamentalFact(report.version_id, report.symbol, report.statement_type, report.report_date,
                            report.effective_on, "TOTAL_LIABILITIES", Decimal("70"), "USD"),
            FundamentalFact(report.version_id, report.symbol, report.statement_type, report.report_date,
                            report.effective_on, "TOTAL_EQUITY", Decimal("30"), "CNY"),
        ]
        gates = evaluate_fundamentals(
            expected_symbols=[report.symbol], reports=[report], facts=facts,
            successful_symbols={report.symbol},
        )
        by_name = {gate.name: gate for gate in gates}
        self.assertTrue(by_name["fundamental_currency_contract"].passed)
        self.assertFalse(by_name["fundamental_fact_currency_lineage"].passed)

    def test_currency_aliases_are_normalized_without_converting_values(self) -> None:
        report = FundamentalReport.build(
            symbol="000001", statement_type="balance", report_date="2025-12-31",
            notice_date="2026-03-21", update_date="2026-03-21", report_type="annual",
            currency="人民币", organization_type="bank", source="fixture", source_row={},
        )
        self.assertEqual(report.currency, "CNY")

    def test_only_confirmed_delisted_exclusion_is_resumable(self) -> None:
        self.assertTrue(reusable_checkpoint("succeeded", None))
        self.assertFalse(reusable_checkpoint("excluded", None))
        self.assertFalse(reusable_checkpoint("excluded", "delisted_source_empty"))
        self.assertTrue(reusable_checkpoint(
            "excluded", "confirmed_delisted_source_empty_after_two_responses;out_date=2020-01-01",
        ))

    def test_active_symbol_cannot_be_excluded_from_coverage(self) -> None:
        gates = evaluate_fundamentals(
            expected_symbols=["000001"], reports=[], facts=[], successful_symbols=set(),
            excluded_symbols={"000001"}, allowed_excluded_symbols=set(),
        )
        self.assertFalse(next(g for g in gates if g.name == "fundamental_exclusion_eligibility").passed)


if __name__ == "__main__":
    unittest.main()
