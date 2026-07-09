from pathlib import Path
import sys
import unittest

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from sync_sector_mapping import (
    build_sector_rows,
    concept_tags_for,
    should_skip_existing,
    sync_sector_mapping,
)


class FakeClient:
    def __init__(self, existing_count=0) -> None:
        self.existing_count = existing_count
        self.calls = []
        self.requests = []

    def request(self, method, path, body=None, prefer=None):
        self.requests.append((method, path, body, prefer))
        if "stock_sector_mapping" in path:
            return [{"code": str(index).zfill(6)} for index in range(self.existing_count)]
        return []

    def upsert(self, table, rows, conflict):
        self.calls.append((table, rows, conflict))
        return len(rows)


def sample_stock_codes() -> pd.DataFrame:
    return pd.DataFrame([
        {"code": "000001", "name": "平安银行"},
        {"code": "300750", "name": "宁德时代"},
        {"code": "600519", "name": "贵州茅台"},
    ])


def sample_sw_history() -> pd.DataFrame:
    return pd.DataFrame([
        {"symbol": "000001", "start_date": "2021-07-30", "industry_code": "480301"},
        {"symbol": "300750", "start_date": "2021-07-30", "industry_code": "630701"},
        {"symbol": "600519", "start_date": "2021-07-30", "industry_code": "340501"},
    ])


def sample_industry_names() -> dict[str, dict[str, str]]:
    return {
        "480301": {"l1": "银行", "l2": "股份制银行Ⅱ", "l3": "股份制银行Ⅲ"},
        "630701": {"l1": "电力设备", "l2": "电池", "l3": "锂电池"},
        "340501": {"l1": "食品饮料", "l2": "白酒Ⅱ", "l3": "白酒Ⅲ"},
    }


class SectorMappingTest(unittest.TestCase):
    def test_build_rows_maps_latest_sw_industry_and_concepts(self) -> None:
        rows = build_sector_rows(sample_stock_codes(), sample_sw_history(), sample_industry_names())

        self.assertEqual(len(rows), 3)
        by_code = {row["code"]: row for row in rows}
        self.assertEqual(by_code["000001"]["shenwan_industry_l1"], "银行")
        self.assertEqual(by_code["000001"]["shenwan_industry_l2"], "股份制银行Ⅱ")
        self.assertIn("新能源", by_code["300750"]["concept_tags"])
        self.assertIn("白酒", by_code["600519"]["concept_tags"])

    def test_skip_existing_when_not_forced_and_count_exceeds_threshold(self) -> None:
        client = FakeClient(existing_count=501)

        self.assertTrue(should_skip_existing(client, force=False, threshold=500))
        self.assertFalse(should_skip_existing(client, force=True, threshold=500))

    def test_sync_upserts_with_code_conflict(self) -> None:
        client = FakeClient(existing_count=0)

        summary = sync_sector_mapping(
            client,
            force=True,
            stock_codes_loader=sample_stock_codes,
            sw_history_loader=sample_sw_history,
            industry_name_loader=lambda history: sample_industry_names(),
        )

        self.assertEqual(summary["upserted"], 3)
        self.assertEqual(summary["non_empty_l1"], 3)
        self.assertEqual(client.calls[0][0], "stock_sector_mapping")
        self.assertEqual(client.calls[0][2], "code")

    def test_concept_tags_for_common_industries(self) -> None:
        self.assertIn("半导体", concept_tags_for("电子", "半导体"))
        self.assertIn("AI算力", concept_tags_for("通信", "通信设备"))
        self.assertIn("创新药", concept_tags_for("医药生物", "化学制药"))


if __name__ == "__main__":
    unittest.main()
