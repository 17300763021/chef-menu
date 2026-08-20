from __future__ import annotations

import unittest
from datetime import date

from scripts.market_data.manifest import sha256
from scripts.market_data.sources.eastmoney_corporate_action_source import (
    EastmoneyCorporateActionSource,
)


class _Response:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


def _row(symbol: str, ex_date: str = "2026-07-31") -> dict[str, object]:
    return {
        "SECURITY_CODE": symbol,
        "EX_DIVIDEND_DATE": ex_date,
        "EQUITY_RECORD_DATE": "2026-07-30 00:00:00",
        "REPORT_DATE": "2025-12-31 00:00:00",
        "NOTICE_DATE": "2026-07-24 00:00:00",
        "ASSIGN_PROGRESS": "实施分配",
        "PRETAX_BONUS_RMB": "0.15",
        "BONUS_RATIO": "0",
        "IT_RATIO": "0",
        "IMPL_PLAN_PROFILE": "10派0.15元",
    }


class EastmoneyCorporateActionSourceTests(unittest.TestCase):
    def test_fetches_complete_deterministic_inventory(self) -> None:
        calls: list[int] = []

        def get(_url: str, *, params: dict[str, str], **_kwargs: object) -> _Response:
            page = int(params["pageNumber"])
            calls.append(page)
            data = [_row("601866")] if page == 1 else [_row("000001")]
            return _Response({
                "success": True,
                "result": {"pages": 2, "count": 2, "data": data},
            })

        inventory = EastmoneyCorporateActionSource(
            attempts=1, request_get=get,
        ).fetch(date(2026, 7, 31))

        self.assertEqual(calls, [1, 2])
        self.assertEqual(inventory.symbols, ("000001", "601866"))
        self.assertEqual(inventory.records[1]["cash_per_ten_shares"], "0.15")
        self.assertEqual(inventory.evidence_sha256, sha256(list(inventory.records)))

    def test_rejects_incomplete_pagination(self) -> None:
        def get(*_args: object, **_kwargs: object) -> _Response:
            return _Response({
                "success": True,
                "result": {"pages": 1, "count": 2, "data": [_row("601866")]},
            })

        with self.assertRaisesRegex(RuntimeError, "row count mismatch"):
            EastmoneyCorporateActionSource(
                attempts=1, request_get=get,
            ).fetch(date(2026, 7, 31))

    def test_rejects_out_of_scope_date(self) -> None:
        def get(*_args: object, **_kwargs: object) -> _Response:
            return _Response({
                "success": True,
                "result": {"pages": 1, "count": 1, "data": [_row("601866", "2026-08-03")]},
            })

        with self.assertRaisesRegex(RuntimeError, "ex-date mismatch"):
            EastmoneyCorporateActionSource(
                attempts=1, request_get=get,
            ).fetch(date(2026, 7, 31))

    def test_retries_then_fails_closed(self) -> None:
        calls = 0

        def get(*_args: object, **_kwargs: object) -> _Response:
            nonlocal calls
            calls += 1
            raise TimeoutError("offline")

        with self.assertRaisesRegex(RuntimeError, "unavailable after 2 attempts"):
            EastmoneyCorporateActionSource(
                attempts=2, backoff_seconds=0, request_get=get,
            ).fetch(date(2026, 7, 31))
        self.assertEqual(calls, 2)

    def test_rejects_duplicate_symbol_rows(self) -> None:
        def get(*_args: object, **_kwargs: object) -> _Response:
            return _Response({
                "success": True,
                "result": {"pages": 1, "count": 2, "data": [_row("601866"), _row("601866")]},
            })

        with self.assertRaisesRegex(RuntimeError, "duplicate Eastmoney"):
            EastmoneyCorporateActionSource(
                attempts=1, request_get=get,
            ).fetch(date(2026, 7, 31))


if __name__ == "__main__":
    unittest.main()
