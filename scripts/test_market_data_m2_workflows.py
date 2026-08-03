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


if __name__ == "__main__":
    unittest.main()
