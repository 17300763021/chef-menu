from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from sync_stock_data import SupabaseRest, scan_row


class FakeResponse:
    def __init__(self, payload: str) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self) -> bytes:
        return self.payload.encode("utf-8")


class FakeErrorBody:
    def read(self) -> bytes:
        return b'{"message":"bad request"}'

    def close(self) -> None:
        return None


class SupabaseRestTest(unittest.TestCase):
    def test_scan_row_maps_multi_factor_columns(self) -> None:
        mapped = scan_row({
            "生成日期": "2026-07-09 15:40:00",
            "代码": "600001",
            "名称": "Alpha",
            "因子趋势": "71.5",
            "因子动量": "82",
            "因子量价": "63",
            "因子资金": "77",
            "因子质量": "55",
            "行业排名": "4",
        })

        self.assertEqual(mapped["factor_trend"], 71.5)
        self.assertEqual(mapped["factor_momentum"], 82)
        self.assertEqual(mapped["factor_volume"], 63)
        self.assertEqual(mapped["factor_flow"], 77)
        self.assertEqual(mapped["factor_quality"], 55)
        self.assertEqual(mapped["sector_rank"], 4)

    def test_request_retries_transient_network_errors(self) -> None:
        client = SupabaseRest("https://example.supabase.co", "service-key", retry_delay_seconds=0)
        calls = []

        def flaky_urlopen(request, timeout):
            calls.append((request, timeout))
            if len(calls) == 1:
                raise URLError("temporary ssl eof")
            return FakeResponse('[{"id":"ok"}]')

        with patch("sync_stock_data.urlopen", side_effect=flaky_urlopen):
            result = client.request("POST", "stock_backtest_runs", {"note": "probe"})

        self.assertEqual(result, [{"id": "ok"}])
        self.assertEqual(len(calls), 2)

    def test_request_does_not_retry_http_errors(self) -> None:
        client = SupabaseRest("https://example.supabase.co", "service-key")
        error = HTTPError(
            url="https://example.supabase.co/rest/v1/table",
            code=400,
            msg="bad request",
            hdrs={},
            fp=FakeErrorBody(),
        )

        with patch("sync_stock_data.urlopen", side_effect=error) as urlopen_mock:
            with self.assertRaisesRegex(RuntimeError, "bad request"):
                client.request("POST", "stock_backtest_runs", {"note": "probe"})

        self.assertEqual(urlopen_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
