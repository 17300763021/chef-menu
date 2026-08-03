"""CNINFO industry hierarchy and per-security verification adapters."""

from __future__ import annotations

from datetime import date
import time
from typing import Callable

import pandas as pd

from scripts.market_data.contracts import normalize_symbol
from scripts.market_data.industry_contracts import (
    SW_2021_EFFECTIVE_DATE,
    IndustryNode,
    IndustryVerification,
    SwsAssignmentRecord,
    normalize_assignment_industry_code,
    parse_industry_date,
)


def assignments_from_cninfo_changes(
    rows: tuple[IndustryVerification, ...] | list[IndustryVerification],
) -> tuple[SwsAssignmentRecord, ...]:
    """Convert first-party CNINFO Shenwan change events into primary assignments."""
    grouped: dict[tuple[str, date], list[IndustryVerification]] = {}
    current_before_cutover: dict[str, list[IndustryVerification]] = {}
    for row in rows:
        if row.standard_code == "008003" and row.change_date <= SW_2021_EFFECTIVE_DATE:
            current_before_cutover.setdefault(row.symbol, []).append(row)
            continue
        if row.standard_code != "008003" and row.change_date >= SW_2021_EFFECTIVE_DATE:
            continue
        grouped.setdefault((row.symbol, row.change_date), []).append(row)
    for symbol, candidates in current_before_cutover.items():
        latest_date = max(row.change_date for row in candidates)
        latest = [row for row in candidates if row.change_date == latest_date]
        grouped.setdefault((symbol, SW_2021_EFFECTIVE_DATE), []).extend(latest)
    assignments: list[SwsAssignmentRecord] = []
    for key, candidates in sorted(grouped.items()):
        codes = {row.industry_code for row in candidates}
        if len(codes) != 1:
            raise RuntimeError(f"CNINFO returned conflicting primary assignment codes for {key}: {sorted(codes)}")
        standard_names = {row.standard_name for row in candidates if row.standard_name}
        standard_codes = {row.standard_code for row in candidates if row.standard_code}
        assignment = SwsAssignmentRecord.build(
            symbol=key[0],
            source_effective_from=key[1],
            industry_code=next(iter(codes)),
            source_updated_at=None,
            source=(
                "cninfo_official_api:sw2021_cutover_normalized"
                if key[1] == SW_2021_EFFECTIVE_DATE and standard_codes == {"008003"}
                else "cninfo_official_api"
            ),
            standard_name=next(iter(standard_names)) if len(standard_names) == 1 else None,
            standard_code=next(iter(standard_codes)) if len(standard_codes) == 1 else None,
        )
        assignments.append(assignment)
    return tuple(assignments)


def _text(value: object) -> str | None:
    text = str(value or "").strip()
    return None if text.lower() in {"", "nan", "none", "nat"} else text


def normalize_cninfo_catalog(frame: pd.DataFrame) -> tuple[IndustryNode, ...]:
    required = ("类目编码", "类目名称", "终止日期", "行业类型", "行业类型编码", "父类编码", "分级")
    if not all(column in frame.columns for column in required):
        raise RuntimeError(f"unexpected CNINFO industry catalog columns: {list(frame.columns)}")
    rows: list[IndustryNode] = []
    for item in frame.loc[:, list(required)].to_dict("records"):
        code = str(item["类目编码"] or "").strip().upper()
        name = str(item["类目名称"] or "").strip()
        if not code or not name:
            raise RuntimeError("CNINFO industry catalog contains a blank node code or name")
        terminated = _text(item["终止日期"])
        rows.append(IndustryNode(
            node_code=code,
            node_name=name,
            parent_code=_text(item["父类编码"]),
            level=int(item["分级"]),
            standard_name=str(item["行业类型"] or "").strip(),
            standard_code=str(item["行业类型编码"] or "").strip(),
            termination_date=None if terminated is None else parse_industry_date(terminated),
        ))
    keys = [row.node_code for row in rows]
    if len(keys) != len(set(keys)):
        raise RuntimeError("CNINFO industry catalog contains duplicate node codes")
    return tuple(sorted(rows, key=lambda value: (value.level, value.node_code)))


def normalize_cninfo_changes(frame: pd.DataFrame, requested_symbol: str) -> tuple[IndustryVerification, ...]:
    required = (
        "行业中类", "行业大类", "行业次类", "行业门类", "行业编码",
        "分类标准", "分类标准编码", "证券代码", "变更日期",
    )
    if frame is None or frame.empty:
        return ()
    if not all(column in frame.columns for column in required):
        raise RuntimeError(f"unexpected CNINFO industry-change columns: {list(frame.columns)}")
    requested = normalize_symbol(requested_symbol)
    rows: list[IndustryVerification] = []
    for item in frame.loc[:, list(required)].to_dict("records"):
        standard_name = str(item["分类标准"] or "").strip()
        if "申银万国" not in standard_name:
            continue
        symbol = normalize_symbol(str(item["证券代码"] or requested))
        if symbol != requested:
            raise RuntimeError(f"CNINFO returned {symbol} while {requested} was requested")
        code = normalize_assignment_industry_code(item["行业编码"])
        rows.append(IndustryVerification(
            symbol=symbol,
            change_date=parse_industry_date(item["变更日期"]),
            industry_code=code,
            level1_name=_text(item["行业门类"]) or _text(item["行业大类"]),
            level2_name=_text(item["行业次类"]) or _text(item["行业大类"]),
            level3_name=_text(item["行业中类"]) or _text(item["行业大类"]),
            standard_name=standard_name,
            standard_code=str(item["分类标准编码"] or "").strip(),
        ))
    unique: dict[tuple[str, date, str, str], IndustryVerification] = {}
    for row in rows:
        existing = unique.get(row.key)
        if existing is not None and existing.canonical() != row.canonical():
            raise RuntimeError(f"CNINFO returned conflicting industry rows for {row.key}")
        unique[row.key] = row
    return tuple(sorted(unique.values(), key=lambda value: value.key))


class CninfoIndustrySource:
    def __init__(
        self,
        *,
        catalog_loader: Callable[[], pd.DataFrame] | None = None,
        changes_loader: Callable[[str, str, str], pd.DataFrame] | None = None,
        timeout_seconds: int = 30,
        attempts: int = 1,
    ) -> None:
        if timeout_seconds < 1 or attempts < 1 or attempts > 3:
            raise ValueError("CNINFO timeout must be positive and attempts must be between 1 and 3")
        self.timeout_seconds = timeout_seconds
        self.attempts = attempts
        self._uses_default_catalog = catalog_loader is None
        self._uses_default_changes = changes_loader is None
        self._akshare_module = None
        if catalog_loader is None or changes_loader is None:
            import akshare as ak
            import akshare.stock.stock_industry_cninfo as akshare_module
            self._akshare_module = akshare_module
            catalog_loader = catalog_loader or (
                lambda: ak.stock_industry_category_cninfo(symbol="申银万国行业分类标准")
            )
            changes_loader = changes_loader or (
                lambda symbol, start, end: ak.stock_industry_change_cninfo(
                    symbol=symbol, start_date=start, end_date=end,
                )
            )
        self.catalog_loader = catalog_loader
        self.changes_loader = changes_loader

    def _call(self, loader: Callable[..., pd.DataFrame], *args: str, bounded: bool) -> pd.DataFrame:
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            empty_records_response = False
            try:
                if not bounded or self._akshare_module is None:
                    return loader(*args)
                requests_module = self._akshare_module.requests
                original_get = requests_module.get
                original_post = requests_module.post

                def bounded_get(*request_args, **request_kwargs):
                    nonlocal empty_records_response
                    request_kwargs.setdefault("timeout", self.timeout_seconds)
                    response = original_get(*request_args, **request_kwargs)
                    response.raise_for_status()
                    try:
                        payload = response.json()
                    except Exception:
                        payload = None
                    empty_records_response = isinstance(payload, dict) and payload.get("records") == []
                    return response

                def bounded_post(*request_args, **request_kwargs):
                    nonlocal empty_records_response
                    request_kwargs.setdefault("timeout", self.timeout_seconds)
                    response = original_post(*request_args, **request_kwargs)
                    response.raise_for_status()
                    try:
                        payload = response.json()
                    except Exception:
                        payload = None
                    empty_records_response = isinstance(payload, dict) and payload.get("records") == []
                    return response

                requests_module.get = bounded_get
                requests_module.post = bounded_post
                try:
                    return loader(*args)
                finally:
                    requests_module.get = original_get
                    requests_module.post = original_post
            except Exception as error:
                if empty_records_response:
                    return pd.DataFrame()
                last_error = error
                if attempt < self.attempts:
                    time.sleep(min(2 ** (attempt - 1), 2))
        raise RuntimeError(f"CNINFO request failed after {self.attempts} attempt(s): {last_error}")

    def fetch_catalog(self) -> tuple[IndustryNode, ...]:
        assert self.catalog_loader is not None
        frame = self._call(self.catalog_loader, bounded=self._uses_default_catalog)
        return normalize_cninfo_catalog(frame)

    def fetch_changes(self, symbol: str, start: date, end: date) -> tuple[IndustryVerification, ...]:
        assert self.changes_loader is not None
        frame = self._call(
            self.changes_loader, symbol, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"),
            bounded=self._uses_default_changes,
        )
        return normalize_cninfo_changes(frame, symbol)
