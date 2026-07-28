"""CNINFO industry hierarchy and per-security verification adapters."""

from __future__ import annotations

from datetime import date
from typing import Callable

import pandas as pd

from scripts.market_data.contracts import normalize_symbol
from scripts.market_data.industry_contracts import (
    IndustryNode,
    IndustryVerification,
    normalize_assignment_industry_code,
    parse_industry_date,
)


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
    ) -> None:
        if catalog_loader is None or changes_loader is None:
            import akshare as ak
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

    def fetch_catalog(self) -> tuple[IndustryNode, ...]:
        assert self.catalog_loader is not None
        return normalize_cninfo_catalog(self.catalog_loader())

    def fetch_changes(self, symbol: str, start: date, end: date) -> tuple[IndustryVerification, ...]:
        assert self.changes_loader is not None
        frame = self.changes_loader(symbol, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
        return normalize_cninfo_changes(frame, symbol)
