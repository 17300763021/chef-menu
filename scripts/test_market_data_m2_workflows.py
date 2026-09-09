from __future__ import annotations

import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class M2WorkflowTests(unittest.TestCase):
    def test_new_workflows_parse_and_pin_actions(self) -> None:
        names = (
            "market-data-fundamental-acceptance.yml", "market-data-index-acceptance.yml",
            "market-data-flow-admission.yml", "market-data-m2-release-acceptance.yml",
        )
        for name in names:
            text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
            yaml.safe_load(text)
            self.assertIn("actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803", text)
            self.assertIn("actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1", text)
            self.assertIn('FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: "true"', text)

    def test_all_persistence_workflows_use_only_mysql_adapter_and_secret_contract(self) -> None:
        names = (
            "market-data-daily-incremental.yml",
            "market-data-flow-admission.yml",
            "market-data-fundamental-acceptance.yml",
            "market-data-history-acceptance.yml",
            "market-data-index-acceptance.yml",
            "market-data-industry-acceptance.yml",
            "market-data-m2-release-acceptance.yml",
        )
        for name in names:
            text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
            yaml.safe_load(text)
            self.assertNotIn("TIDB_", text, name)
            self.assertNotIn("tidb_", text, name)
            self.assertNotIn("TiDB", text, name)
            if "MYSQL_HOST:" in text:
                for key in ("HOST", "PORT", "USER", "PASSWORD", "DATABASE", "SSL_MODE"):
                    self.assertIn(f"MYSQL_{key}: ${{{{ secrets.MYSQL_{key} }}}}", text, name)

    def test_all_publications_remain_research_only(self) -> None:
        for name in ("mysql_fundamental_store.py", "mysql_index_store.py", "mysql_flow_store.py", "m2_release_gate.py"):
            text = (ROOT / "scripts" / "market_data" / name).read_text(encoding="utf-8")
            self.assertIn("simulation_orders_allowed", text)
            self.assertNotIn("stock_trade_history", text)
            self.assertNotIn("paper_trade", text)

    def test_all_market_data_writers_fail_closed_on_mysql_capacity(self) -> None:
        names = (
            "market-data-daily-incremental.yml",
            "market-data-flow-admission.yml",
            "market-data-fundamental-acceptance.yml",
            "market-data-history-acceptance.yml",
            "market-data-index-acceptance.yml",
            "market-data-industry-acceptance.yml",
            "market-data-m2-release-acceptance.yml",
        )
        for name in names:
            text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
            self.assertIn("mysql_storage_capacity_percent:", text, name)
            self.assertIn("mysql_capacity_checked_at:", text, name)
            self.assertIn("--storage-percent", text, name)
            self.assertIn("--checked-at", text, name)

    def test_each_specialized_mysql_store_test_runs_in_its_workflow(self) -> None:
        expected = {
            "market-data-flow-admission.yml": "python -m scripts.test_mysql_flow_store",
            "market-data-fundamental-acceptance.yml": "python -m scripts.test_mysql_fundamental_store",
            "market-data-index-acceptance.yml": "python -m scripts.test_mysql_index_store",
        }
        for name, command in expected.items():
            text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
            self.assertIn(command, text, name)

    def test_daily_cloud_catchup_is_bounded_sharded_and_quota_gated(self) -> None:
        text = (ROOT / ".github" / "workflows" / "market-data-daily-incremental.yml").read_text(encoding="utf-8")
        yaml.safe_load(text)
        self.assertIn("parallel_shards=4", text)
        self.assertIn("parallel_shards=1", text)
        self.assertIn("--parallel-shards \"$parallel_shards\"", text)
        self.assertIn("supersedes_dataset_id", text)
        self.assertIn("timeout-minutes: 300", text)
        self.assertIn("--storage-percent", text)
        self.assertIn("max_sessions", text)

    def test_historical_push_runs_deterministic_tests_without_live_acquisition(self) -> None:
        path = ROOT / ".github" / "workflows" / "market-data-history-acceptance.yml"
        text = path.read_text(encoding="utf-8")
        workflow = yaml.safe_load(text)
        triggers = workflow.get("on", workflow.get(True))
        self.assertIn("push", triggers)
        deterministic = workflow["jobs"]["deterministic-tests"]
        self.assertNotIn("if", deterministic)
        sample = workflow["jobs"]["sample"]
        self.assertEqual(
            sample["if"],
            "github.event_name == 'workflow_dispatch' && inputs.mode == 'sample' && inputs.operation == 'capture'",
        )
        self.assertIn("historical_bars", str(sample))
        for name, job in workflow["jobs"].items():
            if name != "sample":
                self.assertNotIn("--mode sample", str(job), name)

    def test_full_fundamentals_use_short_resumable_shards_without_more_parallel_pressure(self) -> None:
        text = (ROOT / ".github" / "workflows" / "market-data-fundamental-acceptance.yml").read_text(encoding="utf-8")
        self.assertIn('else 16', text)
        self.assertIn("max-parallel: 4", text)
        self.assertIn("timeout-minutes: 180", text)


if __name__ == "__main__":
    unittest.main()
