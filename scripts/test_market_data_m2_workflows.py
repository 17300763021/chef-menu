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

    def test_all_publications_remain_research_only(self) -> None:
        for name in ("tidb_fundamental_store.py", "tidb_index_store.py", "tidb_flow_store.py", "m2_release_gate.py"):
            text = (ROOT / "scripts" / "market_data" / name).read_text(encoding="utf-8")
            self.assertIn("simulation_orders_allowed", text)
            self.assertNotIn("stock_trade_history", text)
            self.assertNotIn("paper_trade", text)

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

    def test_full_fundamentals_use_short_resumable_shards_without_more_parallel_pressure(self) -> None:
        text = (ROOT / ".github" / "workflows" / "market-data-fundamental-acceptance.yml").read_text(encoding="utf-8")
        self.assertIn('else 16', text)
        self.assertIn("max-parallel: 4", text)
        self.assertIn("timeout-minutes: 180", text)
        self.assertEqual(text.count("TIDB_MARKET_HOST: ${{ secrets.TIDB_HOST }}"), 2)
        self.assertEqual(text.count("TIDB_HOST: ${{ secrets.TIDB_RESEARCH_HOST }}"), 2)

    def test_fundamental_replay_requires_pinned_delisting_evidence_identity(self) -> None:
        text = (ROOT / ".github" / "workflows" / "market-data-fundamental-acceptance.yml").read_text(encoding="utf-8")
        self.assertIn("replay_delisting_evidence_run_id", text)
        self.assertIn("replay_delisting_evidence_sha256", text)
        self.assertIn("run id and SHA-256 must be supplied together", text)
        self.assertIn("actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093", text)
        self.assertIn("run-id: ${{ inputs.replay_delisting_evidence_run_id }}", text)
        self.assertIn("frozen official delisting evidence hash mismatch", text)
        self.assertIn("actions: read", text)


if __name__ == "__main__":
    unittest.main()
