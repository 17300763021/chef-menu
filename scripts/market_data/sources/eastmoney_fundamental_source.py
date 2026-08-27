"""Eastmoney statement adapter with conservative point-in-time availability."""

from __future__ import annotations

import time
from datetime import date
from typing import Any, Callable

import pandas as pd

from scripts.market_data.contracts import exchange_for_symbol, normalize_symbol
from scripts.market_data.fundamental_contracts import (
    METRIC_COLUMNS,
    FundamentalFact,
    FundamentalReport,
    decimal_or_none,
)


class FundamentalSourceEmptyResponse(RuntimeError):
    """A statement endpoint returned no usable payload for a symbol."""


class FundamentalSourceRowAnomaly(ValueError):
    """A single source row violates the point-in-time contract."""


class EastmoneyFundamentalSource:
    name = "akshare_eastmoney_financial_statements"

    def __init__(self, *, attempts: int = 2, loaders: dict[str, Callable[[str], pd.DataFrame]] | None = None) -> None:
        if attempts < 1 or attempts > 3:
            raise ValueError("fundamental source attempts must be between 1 and 3")
        self.attempts = attempts
        if loaders is None:
            import akshare as ak
            import akshare.stock_feature.stock_three_report_em as module

            loaders = {
                "balance": ak.stock_balance_sheet_by_report_em,
                "income": ak.stock_profit_sheet_by_report_em,
                "cashflow": ak.stock_cash_flow_sheet_by_report_em,
            }
            self._delisted_loaders = {
                "balance": ak.stock_balance_sheet_by_report_delisted_em,
                "income": ak.stock_profit_sheet_by_report_delisted_em,
                "cashflow": ak.stock_cash_flow_sheet_by_report_delisted_em,
            }
            self._module = module
        else:
            self._delisted_loaders = {}
            self._module = None
        self.loaders = loaders

    @staticmethod
    def vendor_symbol(symbol: str) -> str:
        code = normalize_symbol(symbol)
        return ("SH" if exchange_for_symbol(code) == "SSE" else "SZ") + code

    def _call(self, statement_type: str, vendor_symbol: str, *, delisted: bool) -> pd.DataFrame:
        loader = (self._delisted_loaders if delisted else self.loaders).get(statement_type)
        if loader is None:
            return pd.DataFrame()
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                if self._module is None:
                    frame = loader(vendor_symbol)
                else:
                    requests_module = self._module.requests
                    original_get = requests_module.get

                    def bounded_retry_get(*request_args, **request_kwargs):
                        request_kwargs.setdefault("timeout", 20)
                        request_error: Exception | None = None
                        for request_attempt in range(1, 4):
                            try:
                                response = original_get(*request_args, **request_kwargs)
                                response.raise_for_status()
                                return response
                            except Exception as error:  # noqa: BLE001 - bounded page retry.
                                request_error = error
                                if request_attempt < 3:
                                    time.sleep(request_attempt)
                        assert request_error is not None
                        raise request_error

                    requests_module.get = bounded_retry_get
                    try:
                        frame = loader(vendor_symbol)
                    finally:
                        requests_module.get = original_get
                if not isinstance(frame, pd.DataFrame):
                    raise RuntimeError("financial source did not return a DataFrame")
                return frame
            except Exception as error:  # noqa: BLE001 - source errors are checkpointed by symbol.
                last_error = error
                if attempt < self.attempts:
                    time.sleep(attempt)
        error_text = str(last_error).lower()
        if (
            (isinstance(last_error, KeyError) and "data" in error_text)
            or (isinstance(last_error, TypeError) and "nonetype" in error_text and "subscriptable" in error_text)
        ):
            raise FundamentalSourceEmptyResponse(
                f"{self.name} {statement_type} empty response: missing data payload"
            ) from last_error
        raise RuntimeError(f"{self.name} {statement_type} failed: {last_error}") from last_error

    def fetch(
        self,
        symbol: str,
        *,
        history_start: date,
        as_of_date: date,
        delisted: bool = False,
    ) -> tuple[list[FundamentalReport], list[FundamentalFact]]:
        code = normalize_symbol(symbol)
        vendor_symbol = self.vendor_symbol(code)
        reports: list[FundamentalReport] = []
        facts: list[FundamentalFact] = []
        statement_reports: dict[str, int] = {statement: 0 for statement in METRIC_COLUMNS}
        statement_candidates: dict[str, int] = {statement: 0 for statement in METRIC_COLUMNS}
        row_anomalies: list[str] = []
        for statement_type in METRIC_COLUMNS:
            statement_delisted = delisted
            try:
                frame = self._call(statement_type, vendor_symbol, delisted=delisted)
            except FundamentalSourceEmptyResponse as primary_empty:
                # Some historical symbols are absent from the normal endpoint
                # even though Eastmoney exposes them through its delisted API.
                # Probe that already-supported route once; never synthesize data.
                if delisted or not self._delisted_loaders:
                    raise
                try:
                    frame = self._call(statement_type, vendor_symbol, delisted=True)
                    statement_delisted = True
                except Exception as fallback_error:  # noqa: BLE001 - preserve both source diagnostics.
                    raise FundamentalSourceEmptyResponse(
                        f"{primary_empty}; delisted_route={type(fallback_error).__name__}: {fallback_error}"
                    ) from fallback_error
            required = {
                "SECURITY_CODE", "REPORT_DATE", "NOTICE_DATE", "UPDATE_DATE",
                "REPORT_TYPE", "CURRENCY", "ORG_TYPE",
            }
            missing = sorted(required - set(frame.columns))
            if missing and not frame.empty:
                raise RuntimeError(f"unexpected {statement_type} columns; missing {missing}")
            for raw in frame.to_dict("records"):
                notice_value = raw.get("NOTICE_DATE")
                update_value = raw.get("UPDATE_DATE")
                notice_missing = str(notice_value).strip().lower() in {"", "none", "nan", "nat"}
                update_missing = str(update_value).strip().lower() in {"", "none", "nan", "nat"}
                if notice_missing and update_missing:
                    continue
                if notice_missing:
                    notice_value = update_value
                if update_missing:
                    update_value = notice_value
                try:
                    report = FundamentalReport.build(
                        symbol=raw.get("SECURITY_CODE") or code,
                        statement_type=statement_type,
                        report_date=raw.get("REPORT_DATE"),
                        notice_date=notice_value,
                        update_date=update_value,
                        report_type=raw.get("REPORT_TYPE"),
                        currency=raw.get("CURRENCY"),
                        organization_type=raw.get("ORG_TYPE"),
                        source=self.name + ("_delisted" if statement_delisted else ""),
                        source_row=raw,
                    )
                except ValueError as error:
                    if "notice date cannot precede report period end" not in str(error):
                        raise
                    row_anomalies.append(f"{statement_type}:{error}")
                    continue
                if report.symbol != code:
                    raise RuntimeError(f"financial source returned {report.symbol} for {code}")
                if report.report_date < history_start or report.effective_on > as_of_date:
                    continue
                statement_candidates[statement_type] += 1
                reports.append(report)
                statement_reports[statement_type] += 1
                for metric in METRIC_COLUMNS[statement_type]:
                    value = decimal_or_none(raw.get(metric))
                    if value is not None:
                        facts.append(FundamentalFact(
                            report_version_id=report.version_id,
                            symbol=code,
                            statement_type=statement_type,
                            report_date=report.report_date,
                            effective_on=report.effective_on,
                            metric_code=metric,
                            value=value,
                            unit=report.currency or "CNY",
                        ))
        missing_statements = [
            statement for statement, count in statement_reports.items()
            if count == 0 and (statement_candidates[statement] > 0 or any(item.startswith(statement + ":") for item in row_anomalies))
        ]
        if missing_statements:
            detail = ";".join(row_anomalies[:5])
            raise FundamentalSourceEmptyResponse(
                f"{self.name} missing usable statements={missing_statements}; anomalies={detail}"
            )
        report_map = {row.key: row for row in reports}
        if len(report_map) != len(reports):
            raise RuntimeError(f"duplicate financial report versions for {code}")
        fact_map = {row.key: row for row in facts}
        if len(fact_map) != len(facts):
            raise RuntimeError(f"duplicate financial facts for {code}")
        return sorted(report_map.values(), key=lambda row: row.key), sorted(fact_map.values(), key=lambda row: row.key)
