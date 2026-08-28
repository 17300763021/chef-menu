"""CNINFO announcement metadata used to verify financial disclosure dates."""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Callable

import pandas as pd

from scripts.market_data.contracts import normalize_symbol, parse_date
from scripts.market_data.fundamental_contracts import FundamentalVerification


class CninfoAnnouncementSource:
    name = "cninfo_official_announcement"

    def __init__(self, *, attempts: int = 2, timeout_seconds: int = 15, loader: Callable[..., pd.DataFrame] | None = None) -> None:
        if attempts < 1 or attempts > 3:
            raise ValueError("CNINFO attempts must be between 1 and 3")
        self.attempts = attempts
        self.timeout_seconds = timeout_seconds
        self._module = None
        if loader is None:
            import akshare as ak
            import akshare.stock_feature.stock_disclosure_cninfo as module
            loader = ak.stock_zh_a_disclosure_report_cninfo
            self._module = module
        self.loader = loader

    def fetch_near(self, symbol: str, notice_date: date) -> tuple[FundamentalVerification, ...]:
        code = normalize_symbol(symbol)
        start = notice_date - timedelta(days=2)
        end = notice_date + timedelta(days=2)
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                if self._module is None:
                    frame = self.loader(
                        symbol=code, market="\u6caa\u6df1\u4eac", keyword="\u62a5\u544a", category="",
                        start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
                    )
                else:
                    requests_module = self._module.requests
                    original_get = requests_module.get
                    original_post = requests_module.post

                    def bounded_get(*args, **kwargs):
                        kwargs.setdefault("timeout", self.timeout_seconds)
                        response = original_get(*args, **kwargs)
                        response.raise_for_status()
                        return response

                    def bounded_post(*args, **kwargs):
                        kwargs.setdefault("timeout", self.timeout_seconds)
                        response = original_post(*args, **kwargs)
                        response.raise_for_status()
                        return response

                    requests_module.get = bounded_get
                    requests_module.post = bounded_post
                    try:
                        frame = self.loader(
                            symbol=code, market="\u6caa\u6df1\u4eac", keyword="\u62a5\u544a", category="",
                            start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
                        )
                    finally:
                        requests_module.get = original_get
                        requests_module.post = original_post
                if frame is None or frame.empty:
                    return ()
                required = {"\u4ee3\u7801", "\u516c\u544a\u6807\u9898", "\u516c\u544a\u65f6\u95f4", "\u516c\u544a\u94fe\u63a5"}
                if not required.issubset(frame.columns):
                    raise RuntimeError(f"unexpected CNINFO announcement columns: {list(frame.columns)}")
                rows: list[FundamentalVerification] = []
                for raw in frame.to_dict("records"):
                    returned = normalize_symbol(str(raw["\u4ee3\u7801"]))
                    if returned != code:
                        raise RuntimeError(f"CNINFO returned {returned} for {code}")
                    title = str(raw["\u516c\u544a\u6807\u9898"] or "").strip()
                    if not any(token in title for token in ("\u5e74\u5ea6\u62a5\u544a", "\u534a\u5e74\u5ea6\u62a5\u544a", "\u5b63\u5ea6\u62a5\u544a", "\u4e00\u5b63\u5ea6\u62a5\u544a", "\u4e09\u5b63\u5ea6\u62a5\u544a")):
                        continue
                    rows.append(FundamentalVerification(
                        symbol=code,
                        announcement_date=parse_date(str(raw["\u516c\u544a\u65f6\u95f4"])),
                        title=title,
                        announcement_url=str(raw["\u516c\u544a\u94fe\u63a5"] or "").strip(),
                    ))
                unique = {(row.announcement_date, row.title, row.announcement_url): row for row in rows}
                return tuple(sorted(unique.values(), key=lambda row: (row.announcement_date, row.title)))
            except Exception as error:  # noqa: BLE001 - external source failure becomes diagnostic evidence.
                last_error = error
                if attempt < self.attempts:
                    time.sleep(attempt)
        raise RuntimeError(f"CNINFO announcement verification failed: {last_error}") from last_error
