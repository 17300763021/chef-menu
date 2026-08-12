"""Tencent archive fallback for point-in-time A-share daily history.

The public Tencent response includes raw OHLC, turnover, amount in ten-thousand
CNY, and a market-segment-dependent volume field: ordinary A-share rows use
lots while 688/689 STAR rows use shares.  AKShare's convenience frame
intentionally exposes only the first six fields, so this adapter parses the
public response directly to preserve units and audit provenance.  It is a
bounded fallback only; it does not replace the admitted Eastmoney/Sina primary
path.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Mapping

from scripts.market_data.contracts import (
    AMOUNT_QUANTUM,
    PRICE_QUANTUM,
    TURNOVER_QUANTUM,
    DailyBar,
    decimal_value,
    exchange_for_symbol,
    int_value,
    normalize_symbol,
    parse_date,
)
from scripts.market_data.calendar_contracts import TradingCalendar
from scripts.market_data.manifest import sha256


HFQ_CONTINUITY_TOLERANCE = Decimal("0.002")
HFQ_WITHIN_ROW_TOLERANCE = Decimal("0.003")
CASH_DIVIDEND_QUANTUM = Decimal("0.000001")
EXCHANGE_REFERENCE_QUANTUM = Decimal("0.01")


class TencentIndexCalendarSource:
    """Independent SSE calendar derived from Tencent index archives."""

    name = "tencent_sse_index_calendar"

    def __init__(self, timeout_seconds: float = 20.0, attempts: int = 2) -> None:
        self.timeout_seconds = timeout_seconds
        self.attempts = attempts

    def fetch(self, start: date, end: date) -> TradingCalendar:
        if start > end:
            raise ValueError("calendar start_date is after end_date")
        source = TencentHistorySource(
            timeout_seconds=self.timeout_seconds,
            attempts=self.attempts,
        )
        dates: set[date] = set()
        for year in range(start.year, end.year + 1):
            chunk_start = max(start, date(year, 1, 1))
            chunk_end = min(end, date(year, 12, 31))
            chunk_dates: set[date] = set()
            for row in source._request_block("sh000001", chunk_start, chunk_end, ""):
                if not isinstance(row, list) or not row:
                    raise RuntimeError("Tencent SSE index calendar returned a malformed row")
                business_date = parse_date(row[0])
                if chunk_start <= business_date <= chunk_end:
                    chunk_dates.add(business_date)
            if not chunk_dates:
                raise RuntimeError(
                    "Tencent SSE index calendar returned no dated rows for "
                    f"{chunk_start.isoformat()}..{chunk_end.isoformat()}"
                )
            dates.update(chunk_dates)
        return TradingCalendar.build(self.name, start, end, dates)


class TencentHistorySource:
    name = "tencent_archive"
    endpoint = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"

    def __init__(self, timeout_seconds: float = 20.0, attempts: int = 2) -> None:
        self.timeout_seconds = timeout_seconds
        self.attempts = attempts

    @staticmethod
    def _vendor_symbol(symbol: str) -> str:
        code = normalize_symbol(symbol)
        return ("sh" if exchange_for_symbol(code) == "SSE" else "sz") + code

    @staticmethod
    def _volume_multiplier(symbol: str) -> Decimal:
        """Normalize Tencent's segment-specific volume field to shares."""
        code = normalize_symbol(symbol)
        return Decimal("1") if code.startswith(("688", "689")) else Decimal("100")

    @staticmethod
    def _implied_hfq_factor(raw_row: list[Any], hfq_row: list[Any], symbol: str) -> Decimal:
        ratios: list[Decimal] = []
        for index, field in ((1, "open"), (2, "close"), (3, "high"), (4, "low")):
            raw_price = decimal_value(raw_row[index], f"Tencent raw {field}", PRICE_QUANTUM)
            hfq_price = decimal_value(hfq_row[index], f"Tencent hfq {field}", PRICE_QUANTUM)
            assert raw_price is not None and hfq_price is not None
            if raw_price <= 0 or hfq_price <= 0:
                raise RuntimeError(f"Tencent returned nonpositive {field} for {symbol}:{raw_row[0]}")
            ratios.append(hfq_price / raw_price)
        center = sum(ratios, Decimal("0")) / Decimal(len(ratios))
        if any(abs(value - center) / center > HFQ_WITHIN_ROW_TOLERANCE for value in ratios):
            raise RuntimeError(f"Tencent raw/HFQ ratios are internally inconsistent for {symbol}:{raw_row[0]}")
        return center

    def _request_block(self, vendor_symbol: str, start: date, end: date, adjust: str) -> list[list[Any]]:
        try:
            import requests
        except ImportError as error:
            raise RuntimeError("requests is not installed") from error
        if adjust not in {"", "hfq"}:
            raise ValueError("Tencent archive supports raw or hfq history")
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                params = {
                    "_var": f"kline_day{adjust}{start.year}",
                    "param": (
                        f"{vendor_symbol},day,{start.isoformat()},{end.isoformat()},640,{adjust}"
                    ),
                    "r": "0.8205512681390605",
                }
                response = requests.get(self.endpoint, params=params, timeout=self.timeout_seconds)
                response.raise_for_status()
                payload_text = response.text
                separator = payload_text.find("=")
                if separator < 0:
                    raise RuntimeError("Tencent response is missing its JSON assignment")
                payload = json.loads(payload_text[separator + 1 :])
                security = payload.get("data", {}).get(vendor_symbol)
                if not isinstance(security, dict):
                    raise RuntimeError("Tencent response is missing the requested security")
                rows = security.get("hfqday" if adjust == "hfq" else "day")
                if isinstance(rows, list):
                    return rows
                raise RuntimeError("Tencent response contains no daily rows")
            except Exception as error:
                last_error = error
                if attempt < self.attempts:
                    time.sleep(2 ** (attempt - 1))
        assert last_error is not None
        raise RuntimeError(f"Tencent history request failed for {vendor_symbol}: {last_error}") from last_error

    def _rows(self, symbol: str, start: date, end: date, adjust: str) -> list[list[Any]]:
        vendor_symbol = self._vendor_symbol(symbol)
        output: dict[date, list[Any]] = {}
        block_start = start
        while block_start <= end:
            block_end = min(end, date(min(block_start.year + 1, end.year), 12, 31))
            for row in self._request_block(vendor_symbol, block_start, block_end, adjust):
                if not isinstance(row, list) or len(row) < 6:
                    raise RuntimeError(f"Tencent returned a malformed daily row for {vendor_symbol}")
                business_date = parse_date(row[0])
                if start <= business_date <= end:
                    output[business_date] = row
            block_start = date(block_end.year + 1, 1, 1)
        if not output:
            raise RuntimeError(f"Tencent returned no {adjust or 'raw'} rows for {normalize_symbol(symbol)}")
        return [output[key] for key in sorted(output)]

    def fetch_raw(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        code = normalize_symbol(symbol)
        volume_multiplier = self._volume_multiplier(code)
        result: list[DailyBar] = []
        for row in self._rows(code, start, end, ""):
            if len(row) < 9:
                raise RuntimeError(f"Tencent raw row lacks amount/turnover fields for {code}:{row[0]}")
            volume_vendor_units = decimal_value(row[5], "Tencent volume(vendor units)", Decimal("0.01"))
            amount_ten_thousand = decimal_value(row[8], "Tencent amount(10k CNY)", Decimal("0.0001"))
            turnover = decimal_value(row[7], "Tencent turnover(%)", TURNOVER_QUANTUM, allow_blank=True)
            assert volume_vendor_units is not None and amount_ten_thousand is not None
            result.append(DailyBar(
                source=self.name,
                symbol=code,
                exchange=exchange_for_symbol(code),
                business_date=parse_date(row[0]),
                open=decimal_value(row[1], "Tencent open", PRICE_QUANTUM),  # type: ignore[arg-type]
                close=decimal_value(row[2], "Tencent close", PRICE_QUANTUM),  # type: ignore[arg-type]
                high=decimal_value(row[3], "Tencent high", PRICE_QUANTUM),  # type: ignore[arg-type]
                low=decimal_value(row[4], "Tencent low", PRICE_QUANTUM),  # type: ignore[arg-type]
                previous_close=None,
                volume_shares=int_value(volume_vendor_units * volume_multiplier, "Tencent volume(shares)"),
                amount_cny=(amount_ten_thousand * Decimal("10000")).quantize(AMOUNT_QUANTUM),
                turnover_percent=turnover,
                trade_status="trading" if volume_vendor_units > 0 else "unknown_zero_volume",
                is_st=None,
            ))
        return result

    def fetch_cash_dividend_reference(
        self,
        symbol: str,
        previous_session: date,
        target_session: date,
        accepted_previous_close: Decimal,
    ) -> tuple[Decimal, dict[str, Any]]:
        """Return an ex-date reference price backed by Tencent action metadata.

        This fallback is intentionally limited to a pure cash dividend whose
        registration date and ex-rights date exactly match the requested daily
        transition.  Bonus shares, transfers, rights issues, ambiguous text, or
        missing structured fields fail closed instead of being approximated.
        """
        if previous_session >= target_session:
            raise ValueError("Tencent cash-dividend reference requires an earlier session")
        if accepted_previous_close <= 0:
            raise ValueError("Tencent cash-dividend reference requires a positive accepted close")
        code = normalize_symbol(symbol)
        rows = self._rows(code, target_session, target_session, "")
        if len(rows) != 1 or parse_date(rows[0][0]) != target_session or len(rows[0]) < 7:
            raise RuntimeError(f"Tencent cash-dividend evidence lacks the exact target row for {code}")
        action = rows[0][6]
        if not isinstance(action, Mapping) or not action:
            raise RuntimeError(f"Tencent cash-dividend evidence is missing for {code}:{target_session}")
        content = str(action.get("FHcontent", "")).strip()
        content_match = re.fullmatch(
            r"10派([0-9]+(?:\.[0-9]+)?)元(?:（含税）|\(含税\))?",
            content,
        )
        if content_match is None or any(marker in content for marker in ("送", "转", "配")):
            raise RuntimeError(f"Tencent returned an unsupported corporate action for {code}: {content or action}")
        registration_date = parse_date(action.get("djr"))
        ex_rights_date = parse_date(action.get("cqr"))
        if registration_date != previous_session or ex_rights_date != target_session:
            raise RuntimeError(
                f"Tencent corporate-action dates do not match {code}: "
                f"registration={registration_date} ex_rights={ex_rights_date}"
            )
        cash_per_ten = decimal_value(
            action.get("fh_sh"), "Tencent cash dividend per ten shares", CASH_DIVIDEND_QUANTUM,
        )
        assert cash_per_ten is not None
        if cash_per_ten <= 0:
            raise RuntimeError(f"Tencent returned a nonpositive cash dividend for {code}")
        content_cash = decimal_value(
            content_match.group(1), "Tencent cash dividend content", CASH_DIVIDEND_QUANTUM,
        )
        assert content_cash is not None
        if content_cash != cash_per_ten:
            raise RuntimeError(
                f"Tencent cash-dividend fields disagree for {code}: "
                f"structured={cash_per_ten} content={content_cash}"
            )
        exchange_reference = (accepted_previous_close - cash_per_ten / Decimal("10")).quantize(
            EXCHANGE_REFERENCE_QUANTUM, rounding=ROUND_HALF_UP,
        )
        reference = exchange_reference.quantize(PRICE_QUANTUM)
        if reference <= 0:
            raise RuntimeError(f"Tencent cash dividend produced a nonpositive reference price for {code}")
        details = {
            "previous_session": previous_session.isoformat(),
            "registration_date": registration_date.isoformat(),
            "ex_rights_date": ex_rights_date.isoformat(),
            "accepted_previous_close": format(accepted_previous_close, "f"),
            "cash_per_ten_shares": format(cash_per_ten, "f"),
            "derived_previous_close": format(reference, "f"),
            "action_content": content,
            "vendor_action_sha256": sha256(dict(action)),
        }
        return reference, details

    def recover_no_adjustment_predecessor(
        self,
        symbol: str,
        prior_session: date,
        previous_session: date,
        accepted_prior_close: Decimal,
        required_sessions: Iterable[date],
    ) -> tuple[Decimal, dict[str, Any]]:
        """Recover one missing predecessor only after proving factor continuity.

        The accepted QFQ/HFQ factors are carried forward; Tencent is used only
        to prove that no corporate-action factor change occurred across every
        required trading session and to supply the missing raw close.
        """
        if prior_session >= previous_session:
            raise ValueError("Tencent predecessor recovery requires an earlier accepted state")
        if accepted_prior_close <= 0:
            raise ValueError("Tencent predecessor recovery requires a positive accepted close")
        code = normalize_symbol(symbol)
        required = tuple(sorted(set(required_sessions)))
        if not required or required[0] != prior_session or required[-1] != previous_session:
            raise RuntimeError(f"Tencent predecessor recovery has an incomplete session boundary for {code}")
        raw_rows = self._rows(code, prior_session, previous_session, "")
        hfq_rows = self._rows(code, prior_session, previous_session, "hfq")
        raw = {parse_date(row[0]): row for row in raw_rows}
        hfq = {parse_date(row[0]): row for row in hfq_rows}
        if set(raw) != set(required) or set(hfq) != set(required):
            raise RuntimeError(
                f"Tencent predecessor recovery does not cover every required session for {code}: "
                f"required={len(required)} raw={len(raw)} hfq={len(hfq)}"
            )
        prior_close = decimal_value(raw[prior_session][2], "Tencent accepted-prior close", PRICE_QUANTUM)
        recovered_close = decimal_value(raw[previous_session][2], "Tencent recovered close", PRICE_QUANTUM)
        assert prior_close is not None and recovered_close is not None
        if prior_close != accepted_prior_close:
            raise RuntimeError(
                f"Tencent predecessor recovery disagrees with accepted prior close for {code}: "
                f"accepted={accepted_prior_close} observed={prior_close}"
            )
        factors = [self._implied_hfq_factor(raw[session], hfq[session], code) for session in required]
        baseline = factors[0]
        changes = [abs(value - baseline) / baseline for value in factors]
        maximum_change = max(changes, default=Decimal("0"))
        if maximum_change > HFQ_CONTINUITY_TOLERANCE:
            raise RuntimeError(
                f"Tencent HFQ factor changed during predecessor recovery for {code}: "
                f"maximum_change={maximum_change}"
            )
        details = {
            "prior_session": prior_session.isoformat(),
            "recovered_session": previous_session.isoformat(),
            "accepted_prior_close": format(accepted_prior_close, "f"),
            "recovered_raw_close": format(recovered_close, "f"),
            "observed_sessions": [session.isoformat() for session in required],
            "maximum_implied_hfq_change_rate": format(maximum_change, "f"),
            "raw_rows_sha256": sha256(raw_rows),
            "hfq_rows_sha256": sha256(hfq_rows),
        }
        return recovered_close, details

    def verify_no_adjustment_continuity(
        self,
        symbol: str,
        previous_session: date,
        target_session: date,
    ) -> str:
        """Independently prove that Tencent's implied HFQ factor did not change.

        This is intentionally a narrow fallback for a Sina raw primary when
        Sina's factor endpoint has no rows.  It never manufactures a factor or
        accepts a discontinuity: missing dates, inconsistent OHLC ratios, or a
        factor change all fail closed.
        """
        if previous_session >= target_session:
            raise ValueError("Tencent continuity requires an earlier predecessor session")
        code = normalize_symbol(symbol)
        raw = {parse_date(row[0]): row for row in self._rows(code, previous_session, target_session, "")}
        hfq = {parse_date(row[0]): row for row in self._rows(code, previous_session, target_session, "hfq")}
        required = {previous_session, target_session}
        if not required <= set(raw) or not required <= set(hfq):
            raise RuntimeError(f"Tencent raw/HFQ continuity lacks the exact session pair for {code}")
        previous_factor = self._implied_hfq_factor(raw[previous_session], hfq[previous_session], code)
        target_factor = self._implied_hfq_factor(raw[target_session], hfq[target_session], code)
        if abs(previous_factor - target_factor) / previous_factor > HFQ_CONTINUITY_TOLERANCE:
            raise RuntimeError(
                f"Tencent HFQ factor changed for {code}: {previous_factor} -> {target_factor}"
            )
        return "tencent_hfq_no_adjustment_continuity"
