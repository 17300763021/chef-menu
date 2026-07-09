from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import sync_capital_flow as sync_capital_flow_module
from sync_capital_flow import build_capital_flow_rows, sync_capital_flow


class FakeClient:
    def __init__(self) -> None:
        self.calls = []
        self.request_calls = []

    def upsert(self, table, rows, conflict):
        self.calls.append((table, rows, conflict))
        return len(rows)

    def request(self, method, path, body=None, prefer=None):
        self.request_calls.append((method, path, body, prefer))
        return [
            {"code": "600001"},
            {"code": "000002"},
            {"code": "300003"},
            {"code": "600004"},
            {"code": "000005"},
        ]


def fake_eastmoney_payload(url, params):
    fields = params.get("fields", "")
    if "f62" in fields:
        return {
            "data": {
                "diff": [
                    {"f12": "600001", "f14": "A", "f62": 1200, "f64": 700, "f66": 500, "f184": 8.5},
                    {"f12": "000002", "f14": "B", "f62": -50, "f64": 300, "f66": 200, "f184": -1.2},
                    {"f12": "300003", "f14": "C", "f62": 900, "f64": 600, "f66": 400, "f184": 6.8},
                    {"f12": "600004", "f14": "D", "f62": 100, "f64": 50, "f66": 40, "f184": 1.1},
                    {"f12": "000005", "f14": "E", "f62": 300, "f64": 180, "f66": 140, "f184": 3.2},
                ]
            }
        }
    return {
        "data": {
            "diff": [
                {"f12": "600001", "f14": "A", "f62": 2000, "f184": 0.22, "f3": 2.1},
                {"f12": "000002", "f14": "B", "f62": 100, "f184": 0.05, "f3": -0.4},
                {"f12": "300003", "f14": "C", "f62": 1600, "f184": 0.18, "f3": 1.5},
                {"f12": "600004", "f14": "D", "f62": -200, "f184": -0.03, "f3": 0.2},
                {"f12": "000005", "f14": "E", "f62": 500, "f184": 0.11, "f3": 3.8},
            ]
        }
    }


def fake_fallback_payload(url, params):
    if "clist" in url:
        raise RuntimeError("clist unavailable")
    if "ulist" in url:
        diff = []
        for secid in params["secids"].split(","):
            code = secid.split(".")[-1]
            diff.append({
                "f12": code,
                "f14": f"Stock{code[-1]}",
                "f62": 1000 + int(code[-1]),
                "f72": 500 + int(code[-1]),
                "f75": 2.5,
                "f184": 4.5,
            })
        return {"data": {"diff": diff}}
    secid = params["secid"]
    code = secid.split(".")[-1]
    return {
        "data": {
            "code": code,
            "name": f"Stock{code[-1]}",
            "klines": [f"2026-07-04,{1000 + int(code[-1])},10,20,{500 + int(code[-1])}"],
        }
    }


class CapitalFlowSyncTest(unittest.TestCase):
    def test_http_client_falls_back_to_curl_when_requests_fails(self) -> None:
        class FakeCompleted:
            returncode = 0
            stdout = '{"data":{"diff":[{"f12":"600001"}]}}'
            stderr = ""

        with patch("sync_capital_flow.requests.Session.request", side_effect=RuntimeError("blocked")):
            with patch("sync_capital_flow.subprocess.run", return_value=FakeCompleted()):
                payload = sync_capital_flow_module.eastmoney_get_json(
                    "https://push2.eastmoney.com/api/qt/clist/get",
                    {"fields": "f12"},
                    attempts=1,
                )

        self.assertEqual(payload["data"]["diff"][0]["f12"], "600001")

    def test_build_rows_merges_north_bound_and_moneyflow_fields(self) -> None:
        rows = build_capital_flow_rows(
            flow_date="2026-07-04",
            north_bound_rows=[
                {"f12": "600001", "f14": "A", "f62": 2000, "f184": 0.22},
                {"f12": "000002", "f14": "B", "f62": 100, "f184": 0.05},
            ],
            moneyflow_rows=[
                {"f12": "600001", "f14": "A", "f62": 1200, "f64": 700, "f66": 500, "f184": 8.5},
                {"f12": "000002", "f14": "B", "f62": -50, "f64": 300, "f66": 200, "f184": -1.2},
            ],
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["code"], "600001")
        self.assertEqual(rows[0]["flow_date"], "2026-07-04")
        self.assertEqual(rows[0]["north_bound_net_inflow"], 2000)
        self.assertEqual(rows[0]["north_bound_holding_change"], 0.22)
        self.assertEqual(rows[0]["main_net_inflow"], 1200)
        self.assertEqual(rows[0]["big_order_net_inflow"], 1200)
        self.assertEqual(rows[0]["big_order_buy_ratio"], 8.5)

    def test_sync_upserts_at_least_five_rows_with_code_date_conflict(self) -> None:
        client = FakeClient()

        summary = sync_capital_flow(
            client=client,
            flow_date="2026-07-04",
            http_get_json=fake_eastmoney_payload,
        )

        self.assertEqual(summary["upserted"], 5)
        self.assertGreaterEqual(summary["nonzero_rows"], 5)
        self.assertEqual(len(client.calls), 1)
        table, rows, conflict = client.calls[0]
        self.assertEqual(table, "stock_capital_flow")
        self.assertEqual(conflict, "code,flow_date")
        self.assertEqual(len(rows), 5)

    def test_sync_falls_back_to_stock_history_pool_when_clist_is_unavailable(self) -> None:
        client = FakeClient()

        summary = sync_capital_flow(
            client=client,
            flow_date="2026-07-04",
            http_get_json=fake_fallback_payload,
            fallback_limit=5,
        )

        self.assertEqual(summary["source"], "eastmoney_ulist_fallback")
        self.assertEqual(summary["upserted"], 5)
        self.assertEqual(summary["nonzero_rows"], 5)
        self.assertEqual(client.calls[0][2], "code,flow_date")
        self.assertTrue(client.request_calls)


if __name__ == "__main__":
    unittest.main()
