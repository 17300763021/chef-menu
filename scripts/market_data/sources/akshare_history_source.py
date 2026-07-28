"""AKShare historical-price adapters for M2.3.

`AkshareHistorySource` remains the bounded Sina verification adapter used for
cross-source checks.  `AkshareEastmoneyHistorySource` is the primary historical
bundle adapter used by full shards so the cloud run no longer depends on a
BaoStock login for every symbol.
"""

from __future__ import annotations

import time
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from scripts.market_data.adjustment_engine import (
    build_adjusted_series_from_factor_events,
)
from scripts.market_data.contracts import (
    AMOUNT_QUANTUM,
    PRICE_QUANTUM,
    TURNOVER_QUANTUM,
    DailyBar,
    decimal_value,
    exchange_for_symbol,
    int_value,
    normalize_akshare_row,
    normalize_symbol,
    parse_date,
)
from scripts.market_data.historical_contracts import AdjustmentEvent, SecurityReference


class AkshareHistorySource:
    name = "akshare_sina"

    def __init__(self, timeout_seconds: float = 30.0, attempts: int = 3) -> None:
        self.timeout_seconds = timeout_seconds
        self.attempts = attempts

    def _frame(self, symbol: str, start: date, end: date):
        try:
            import akshare as ak
        except ImportError as error:
            raise RuntimeError("akshare is not installed") from error
        frame = None
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                prefix = "sh" if exchange_for_symbol(symbol) == "SSE" else "sz"
                frame = ak.stock_zh_a_daily(
                    symbol=f"{prefix}{symbol}", start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"), adjust="",
                )
                if frame is not None and not frame.empty:
                    return frame
            except Exception as error:
                last_error = error
            if attempt < self.attempts:
                time.sleep(2 ** (attempt - 1))
        suffix = f": {last_error}" if last_error else ""
        raise RuntimeError(f"Sina returned no raw rows for {symbol}{suffix}")

    def fetch_raw(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        exclude_sources: set[str] | None = None,
    ) -> list[DailyBar]:
        code = normalize_symbol(symbol)
        excluded = exclude_sources or set()
        failures: list[str] = []
        if self.name not in excluded:
            try:
                frame = self._frame(code, start, end)
                rows: list[DailyBar] = []
                for raw in frame.to_dict(orient="records"):
                    volume = int_value(raw.get("volume"), "volume(shares)")
                    amount = decimal_value(raw.get("amount"), "amount(CNY)", AMOUNT_QUANTUM)
                    turnover_ratio = decimal_value(raw.get("turnover"), "turnover(ratio)", TURNOVER_QUANTUM, allow_blank=True)
                    assert amount is not None
                    rows.append(DailyBar(
                        source=self.name, symbol=code, exchange=exchange_for_symbol(code),
                        business_date=parse_date(raw.get("date")),
                        open=decimal_value(raw.get("open"), "open", PRICE_QUANTUM),  # type: ignore[arg-type]
                        high=decimal_value(raw.get("high"), "high", PRICE_QUANTUM),  # type: ignore[arg-type]
                        low=decimal_value(raw.get("low"), "low", PRICE_QUANTUM),  # type: ignore[arg-type]
                        close=decimal_value(raw.get("close"), "close", PRICE_QUANTUM),  # type: ignore[arg-type]
                        previous_close=None, volume_shares=volume, amount_cny=amount,
                        turnover_percent=None if turnover_ratio is None else turnover_ratio * Decimal("100"),
                        trade_status="trading" if volume > 0 else "unknown_zero_volume", is_st=None,
                    ))
                return rows
            except Exception as error:
                failures.append(f"{self.name}: {type(error).__name__}: {error}")
        if "akshare_eastmoney" not in excluded:
            from scripts.market_data.sources.akshare_source import AkshareSource
            try:
                return AkshareSource(timeout_seconds=self.timeout_seconds, attempts=self.attempts).fetch(code, start, end)
            except Exception as error:
                failures.append(f"akshare_eastmoney: {type(error).__name__}: {error}")
        if "tencent_archive" not in excluded:
            from scripts.market_data.sources.tencent_history_source import TencentHistorySource
            try:
                return TencentHistorySource(
                    timeout_seconds=self.timeout_seconds, attempts=self.attempts,
                ).fetch_raw(code, start, end)
            except Exception as error:
                failures.append(f"tencent_archive: {type(error).__name__}: {error}")
        raise RuntimeError(f"verification sources failed for {code}: {'; '.join(failures) or 'all sources excluded'}")


class AkshareEastmoneyHistorySource:
    """Primary M2.3 historical bundle from AKShare's Eastmoney endpoint.

    Raw prices use one source per symbol: Eastmoney first, then Sina, then the
    Tencent archive for historical/delisted identifiers.  Positive adjusted
    prices always come from the separately attributed Sina factor timeline.
    This separation is explicit in every stored bar and never permits one
    vendor's missing raw rows to be silently filled date-by-date by another.
    """

    name = "akshare_eastmoney"

    def __init__(self, timeout_seconds: float = 30.0, attempts: int = 5) -> None:
        self.timeout_seconds = timeout_seconds
        self.attempts = attempts

    def _frame(self, symbol: str, start: date, end: date, adjust: str):
        try:
            import akshare as ak
        except ImportError as error:
            raise RuntimeError("akshare is not installed") from error
        if adjust not in {"", "qfq", "hfq"}:
            raise ValueError("AKShare Eastmoney adjust must be '', 'qfq', or 'hfq'")
        code = normalize_symbol(symbol)
        frame = None
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                frame = ak.stock_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                    adjust=adjust,
                    timeout=self.timeout_seconds,
                )
                if frame is not None and not frame.empty:
                    return frame
            except Exception as error:
                last_error = error
            if attempt < self.attempts:
                time.sleep(2 ** (attempt - 1))
        suffix = f": {last_error}" if last_error else ""
        raise RuntimeError(f"AKShare Eastmoney returned no {adjust or 'raw'} rows for {code}{suffix}")

    def fetch_raw(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        code = normalize_symbol(symbol)
        frame = self._frame(code, start, end, "")
        return [normalize_akshare_row(row, code) for row in frame.to_dict(orient="records")]

    def fetch_adjusted_prices(self, symbol: str, start: date, end: date, adjust: str) -> dict[date, tuple[Decimal, Decimal, Decimal, Decimal]]:
        if adjust not in {"qfq", "hfq"}:
            raise ValueError("AKShare adjusted prices require qfq or hfq")
        code = normalize_symbol(symbol)
        frame = self._frame(code, start, end, adjust)
        rows: dict[date, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
        for raw in frame.to_dict(orient="records"):
            bar = normalize_akshare_row(raw, code)
            rows[bar.business_date] = (bar.open, bar.high, bar.low, bar.close)
        return rows

    def _sina_frame(self, symbol: str, start: date, end: date, adjust: str):
        try:
            import akshare as ak
        except ImportError as error:
            raise RuntimeError("akshare is not installed") from error
        if adjust not in {"", "qfq", "hfq"}:
            raise ValueError("AKShare Sina adjust must be '', 'qfq', or 'hfq'")
        code = normalize_symbol(symbol)
        prefix = "sh" if exchange_for_symbol(code) == "SSE" else "sz"
        frame = None
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                frame = ak.stock_zh_a_daily(
                    symbol=f"{prefix}{code}",
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                    adjust=adjust,
                )
                if frame is not None and not frame.empty:
                    return frame
            except Exception as error:
                last_error = error
            if attempt < self.attempts:
                time.sleep(2 ** (attempt - 1))
        suffix = f": {last_error}" if last_error else ""
        raise RuntimeError(f"AKShare Sina returned no {adjust or 'raw'} rows for {code}{suffix}")

    @staticmethod
    def _sina_raw_bars(symbol: str, frame) -> list[DailyBar]:
        code = normalize_symbol(symbol)
        rows: list[DailyBar] = []
        for raw in frame.to_dict(orient="records"):
            volume = int_value(raw.get("volume"), "volume(shares)")
            amount = decimal_value(raw.get("amount"), "amount(CNY)", AMOUNT_QUANTUM)
            turnover_ratio = decimal_value(raw.get("turnover"), "turnover(ratio)", TURNOVER_QUANTUM, allow_blank=True)
            assert amount is not None
            rows.append(DailyBar(
                source="akshare_sina",
                symbol=code,
                exchange=exchange_for_symbol(code),
                business_date=parse_date(raw.get("date")),
                open=decimal_value(raw.get("open"), "open", PRICE_QUANTUM),  # type: ignore[arg-type]
                high=decimal_value(raw.get("high"), "high", PRICE_QUANTUM),  # type: ignore[arg-type]
                low=decimal_value(raw.get("low"), "low", PRICE_QUANTUM),  # type: ignore[arg-type]
                close=decimal_value(raw.get("close"), "close", PRICE_QUANTUM),  # type: ignore[arg-type]
                previous_close=None,
                volume_shares=volume,
                amount_cny=amount,
                turnover_percent=None if turnover_ratio is None else turnover_ratio * Decimal("100"),
                trade_status="trading" if volume > 0 else "unknown_zero_volume",
                is_st=None,
            ))
        return rows

    @staticmethod
    def _sina_adjusted_prices(symbol: str, frame) -> dict[date, tuple[Decimal, Decimal, Decimal, Decimal]]:
        code = normalize_symbol(symbol)
        rows: dict[date, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
        for raw in frame.to_dict(orient="records"):
            rows[parse_date(raw.get("date"))] = (
                decimal_value(raw.get("open"), f"{code} adjusted open", PRICE_QUANTUM),  # type: ignore[arg-type]
                decimal_value(raw.get("high"), f"{code} adjusted high", PRICE_QUANTUM),  # type: ignore[arg-type]
                decimal_value(raw.get("low"), f"{code} adjusted low", PRICE_QUANTUM),  # type: ignore[arg-type]
                decimal_value(raw.get("close"), f"{code} adjusted close", PRICE_QUANTUM),  # type: ignore[arg-type]
            )
        return rows

    def fetch_sina_adjustments(self, symbol: str, end: date) -> list[AdjustmentEvent]:
        try:
            import akshare as ak
        except ImportError as error:
            raise RuntimeError("akshare is not installed") from error
        code = normalize_symbol(symbol)
        prefix = "sh" if exchange_for_symbol(code) == "SSE" else "sz"

        def factor_frame(adjust: str):
            frame = None
            last_error: Exception | None = None
            for attempt in range(1, self.attempts + 1):
                try:
                    frame = ak.stock_zh_a_daily(
                        symbol=f"{prefix}{code}",
                        start_date="19000101",
                        end_date=end.strftime("%Y%m%d"),
                        adjust=adjust,
                    )
                    if frame is not None and not frame.empty:
                        return frame
                except Exception as error:
                    last_error = error
                if attempt < self.attempts:
                    time.sleep(2 ** (attempt - 1))
            suffix = f": {last_error}" if last_error else ""
            raise RuntimeError(f"AKShare Sina returned no {adjust} rows for {code}{suffix}")

        qfq_rows = {
            parse_date(row.get("date")): decimal_value(row.get("qfq_factor"), "qfq_factor", Decimal("0.000001"))
            for row in factor_frame("qfq-factor").to_dict(orient="records")
        }
        hfq_rows = {
            parse_date(row.get("date")): decimal_value(row.get("hfq_factor"), "hfq_factor", Decimal("0.000001"))
            for row in factor_frame("hfq-factor").to_dict(orient="records")
        }
        events: list[AdjustmentEvent] = []
        for effective_date in sorted(set(qfq_rows) & set(hfq_rows)):
            if effective_date > end:
                continue
            qfq_factor = qfq_rows[effective_date]
            hfq_factor = hfq_rows[effective_date]
            assert qfq_factor is not None and hfq_factor is not None
            events.append(AdjustmentEvent(code, effective_date, qfq_factor, hfq_factor, source="akshare_sina_factor"))
        return events

    def fetch_bundle(
        self,
        symbol: str,
        start: date,
        end: date,
    ) -> tuple[
        dict[date, DailyBar],
        dict[date, tuple[Decimal, Decimal, Decimal, Decimal]],
        dict[date, tuple[Decimal, Decimal, Decimal, Decimal]],
        list[AdjustmentEvent],
        SecurityReference,
        dict[date, dict[str, str]],
        str,
        str,
    ]:
        """Fetch one internally consistent primary bundle for a symbol.

        Eastmoney is preferred, followed by Sina and Tencent raw history.  The
        selected raw series stays single-source for the whole symbol.  A
        separately identified factor timeline creates positive multiplicative
        QFQ/HFQ prices and auditable corporate-action dates.
        """

        code = normalize_symbol(symbol)
        failures: list[str] = []
        raw: dict[date, DailyBar] | None = None
        source_name: str | None = None
        for source_name in ("akshare_eastmoney", "akshare_sina"):
            try:
                if source_name == "akshare_eastmoney":
                    raw = {row.business_date: row for row in self.fetch_raw(code, start, end)}
                else:
                    raw = {row.business_date: row for row in self._sina_raw_bars(code, self._sina_frame(code, start, end, ""))}
                break
            except Exception as error:
                failures.append(f"{source_name}: {type(error).__name__}: {error}")
                raw = None
        if raw is None:
            from scripts.market_data.sources.tencent_history_source import TencentHistorySource
            try:
                raw = {
                    row.business_date: row
                    for row in TencentHistorySource(
                        timeout_seconds=self.timeout_seconds, attempts=min(self.attempts, 2),
                    ).fetch_raw(code, start, end)
                }
                source_name = "tencent_archive"
            except Exception as error:
                failures.append(f"tencent_archive: {type(error).__name__}: {error}")
                raise RuntimeError(f"AKShare primary bundle failed for {code}: {'; '.join(failures)}") from error

        factor_source = "akshare_sina_factor_multiplicative"
        try:
            vendor_events = self.fetch_sina_adjustments(code, end)
            qfq, hfq, events = build_adjusted_series_from_factor_events(
                code, raw, vendor_events, source=factor_source,
            )
        except Exception as error:
            failures.append(f"{factor_source}: {type(error).__name__}: {error}")
            raise RuntimeError(f"AKShare factor bundle failed for {code}: {'; '.join(failures)}") from error
        assert source_name is not None
        reference = self.build_reference(code, raw, source_name=source_name)
        status = self.build_status_from_raw(raw)
        return raw, qfq, hfq, events, reference, status, source_name, factor_source

    @staticmethod
    def _factor(adjusted_close: Decimal, raw_close: Decimal) -> Decimal:
        if raw_close <= 0 or adjusted_close <= 0:
            raise ValueError("cannot derive adjustment factor from nonpositive close")
        return (adjusted_close / raw_close).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

    def build_status_from_raw(self, rows: dict[date, DailyBar]) -> dict[date, dict[str, str]]:
        """Return a status-like map without pretending missing bars are suspensions.

        Dates present in raw data are marked tradable.  Missing dates are left
        absent so downstream tradeability records remain fail-closed with a
        `missing_secondary_status` reason instead of being mislabeled as
        confirmed suspension.
        """

        status: dict[date, dict[str, str]] = {}
        previous_close: Decimal | None = None
        for business_date, row in sorted(rows.items()):
            status[business_date] = {
                "tradestatus": "1",
                "isST": "",
                "preclose": "" if previous_close is None else format(previous_close, "f"),
            }
            previous_close = row.close
        return status

    def build_reference(self, symbol: str, rows: dict[date, DailyBar], *, source_name: str | None = None) -> SecurityReference:
        """Build a conservative reference row from observed primary history.

        AKShare/Eastmoney's fast historical endpoint does not provide a stable,
        point-in-time IPO-date contract.  Using the first observed primary bar is
        conservative for listing-age calculations inside this M2.3 slice: it may
        understate age around the first observed membership date, but it will not
        overstate tradability from an unevidenced listing date.
        """

        code = normalize_symbol(symbol)
        if not rows:
            raise RuntimeError(f"AKShare Eastmoney reference unavailable without raw rows for {code}")
        return SecurityReference(code, exchange_for_symbol(code), code, min(rows), None, source=source_name or self.name)
