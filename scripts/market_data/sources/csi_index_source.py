"""Official CSI constituent snapshots and adjustment-notice parser."""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from scripts.market_data.calendar_contracts import TradingCalendar
from scripts.market_data.contracts import normalize_symbol, parse_date
from scripts.market_data.universe_contracts import CurrentUniverse, INDEX_SIZES, IndexChange, UniverseEvent


DETAIL_URL = "https://www.csindex.com.cn/csindex-home/announcement/queryAnnouncementById"
LIST_URL = "https://www.csindex.com.cn/csindex-home/announcement/queryAnnouncementByVo"
CURRENT_URL = "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/{index_code}cons.xls"


@dataclass(frozen=True, slots=True)
class NoticeSpec:
    notice_id: int
    announcement_date: date
    event_type: str
    stated_date: date
    after_close: bool
    basis: str


NOTICE_SPECS = (
    NoticeSpec(11518, date(2018, 5, 28), "regular", date(2018, 6, 11), False, "CSI notice: effective on stated date"),
    NoticeSpec(11859, date(2018, 12, 3), "regular", date(2018, 12, 17), False, "CSI notice: effective on stated date"),
    NoticeSpec(12524, date(2018, 12, 17), "temporary", date(2019, 1, 18), False, "CSI notice plus SSE listing date of China Foreign Trade"),
    NoticeSpec(11379, date(2019, 6, 3), "regular", date(2019, 6, 17), False, "CSI notice: effective on stated date"),
    NoticeSpec(11529, date(2019, 12, 2), "regular", date(2019, 12, 16), False, "CSI notice: effective on stated date"),
    NoticeSpec(11429, date(2020, 6, 1), "regular", date(2020, 6, 15), False, "CSI notice: effective on stated date"),
    NoticeSpec(13179, date(2020, 8, 11), "temporary", date(2020, 8, 17), False, "CSI notice: effective on stated date"),
    NoticeSpec(11514, date(2020, 11, 27), "regular", date(2020, 12, 14), False, "CSI notice: effective on stated date"),
    NoticeSpec(13307, date(2021, 3, 3), "temporary", date(2021, 3, 15), False, "CSI notice: effective on stated date"),
    NoticeSpec(12470, date(2021, 5, 28), "regular", date(2021, 6, 11), True, "CSI notice: after stated close"),
    NoticeSpec(13420, date(2021, 8, 30), "temporary", date(2021, 9, 28), False, "CSI notice plus SSE listing date of China Energy Engineering"),
    NoticeSpec(13716, date(2021, 9, 8), "temporary", date(2021, 9, 10), True, "CSI notice: after stated close"),
    NoticeSpec(13888, date(2021, 11, 26), "regular", date(2021, 12, 10), True, "CSI notice: after stated close"),
    NoticeSpec(14223, date(2022, 5, 27), "regular", date(2022, 6, 10), True, "CSI notice: after stated close"),
    NoticeSpec(14497, date(2022, 11, 25), "regular", date(2022, 12, 9), True, "CSI notice: after stated close"),
    NoticeSpec(14796, date(2023, 5, 26), "regular", date(2023, 6, 9), True, "CSI notice: after stated close"),
    NoticeSpec(15044, date(2023, 11, 24), "regular", date(2023, 12, 8), True, "CSI notice: after stated close"),
    NoticeSpec(15267, date(2024, 5, 31), "regular", date(2024, 6, 14), True, "CSI notice: after stated close"),
    NoticeSpec(15471, date(2024, 11, 29), "regular", date(2024, 12, 13), True, "CSI notice: after stated close"),
    NoticeSpec(15546, date(2025, 2, 6), "temporary", date(2025, 3, 4), False, "CSI notice plus SSE delisting date of Haitong Securities"),
    NoticeSpec(15690, date(2025, 5, 30), "regular", date(2025, 6, 13), True, "CSI notice: after stated close"),
    NoticeSpec(1006022, date(2025, 7, 25), "temporary", date(2025, 9, 5), False, "CSI notice plus SSE delisting date of China Shipbuilding Industry"),
    NoticeSpec(3006000, date(2025, 11, 28), "regular", date(2025, 12, 12), True, "CSI notice: after stated close"),
    NoticeSpec(3006027, date(2026, 1, 6), "temporary", date(2026, 1, 9), True, "CSI temporary-adjustment notice: after stated close"),
    NoticeSpec(3006137, date(2026, 5, 29), "regular", date(2026, 6, 12), True, "CSI notice: after stated close"),
)

# Broad-title notices reviewed from their official detail and found not to change CSI 300/500.
REVIEWED_IRRELEVANT_NOTICE_IDS = {3006120}

# Content-addressed evidence is calculated at runtime. Keeping the official attachment
# locations explicit avoids dozens of rate-limited detail calls and makes source drift visible.
ATTACHMENT_URLS = {
    11518: "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/info/files/20180829/1535531071241051.xlsx",
    11859: "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/info/files/20181217/1545023652142456.xlsx",
    12524: "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/info/files/20181217/1545024403493260.xlsx",
    11379: "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/info/files/20190612/1560322933287092.xls",
    11529: "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/info/files/20191213/1576217266220014.xlsx",
    11429: "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/info/files/20200603/1591174826676118.xlsx",
    13179: "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/info/files/20200811/1597109675659536.xlsx",
    11514: "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/info/files/20210331/1617182729617116.xlsx",
    13307: "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/info/files/20210312/1615540258900287.xlsx",
    12470: "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/info/files/20210610/1623319091660435.xlsx",
    13420: "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/info/files/20210830/1630328886593411.xlsx",
    13716: "https://oss-ch.csindex.com.cn/notice/20210923083520-调整名单.xlsx",
    13888: "https://oss-ch.csindex.com.cn/notice/20211130195824-中证指数调入调出名单.xlsx",
    14223: "https://oss-ch.csindex.com.cn/notice%2F20220527185038-%E6%8C%87%E6%95%B0%E6%A0%B7%E6%9C%AC%E8%B0%83%E6%95%B4%E5%90%8D%E5%8D%95.xlsx",
    14497: "https://oss-ch.csindex.com.cn/notice%2F20221207152016-%E6%8C%87%E6%95%B0%E6%A0%B7%E6%9C%AC%E8%B0%83%E6%95%B4%E5%90%8D%E5%8D%95.xlsx",
    14796: "https://oss-ch.csindex.com.cn/notice/20230526172747-附件：部分指数样本调整名单.pdf",
    15044: "https://oss-ch.csindex.com.cn/notice/20231124170025-附件：部分指数样本调整名单.pdf",
    15267: "https://oss-ch.csindex.com.cn/notice/20240531170829-附件：部分指数样本调整名单.pdf",
    15471: "https://oss-ch.csindex.com.cn/notice/20241129172348-附件：部分指数样本调整名单.pdf",
    15546: "https://oss-ch.csindex.com.cn/notice/20250206175421-指数样本调整名单.xlsx",
    15690: "https://oss-ch.csindex.com.cn/notice/20250530154409-附件：部分指数样本调整名单.pdf",
    1006022: "https://oss-ch.csindex.com.cn/notice/20250722164043-指数样本调整名单.xlsx",
    3006000: "https://oss-ch.csindex.com.cn/notice/20251128165753-附件：部分指数样本调整名单.pdf",
    3006027: "https://oss-ch.csindex.com.cn/notice/20260106171931-指数样本调整名单.xlsx",
    3006137: "https://oss-ch.csindex.com.cn/notice/20260529155822-附件：部分指数样本调整名单.pdf",
}

EXPECTED_ATTACHMENT_HASHES = {
    11518: "4afaa21688b3f00b154640378e2aed63bafcec2b49ed85428c9689fcdd16ca37",
    11859: "ff9c823bc031d7ac86c5106739ee96d1f94537d57683ebce74fd08b1a15f03ad",
    12524: "27c50f47d5f7ca741899a1e3abf9ce80c0dff945cc122e4dba1632e94e0cb2e1",
    11379: "4e1908605be2b722fcfba27f5a164066eb017abc7d9aa24c51be103781e6d6f6",
    11529: "41605c35abf583c23702c73d1e41f25a052da1c6fa1560ed727f42e6709c5fa6",
    11429: "5def8f890c60d9970fa073d2cf5873ca5c6e52188f38f880813e75e382c49657",
    13179: "0751b53933bd2787c176f54faff9ad05b27dbad8f65905cd40672bc6e805f7f1",
    11514: "1094e0a6f284a6a6b4b0e5fc3320c6d43cd4467ad844960c7e73fde07665f608",
    13307: "72b98f7c258494d57a2af39c9daa18f52a7fce07b7519226e94103b2b85fbe49",
    12470: "07e8c58d5f02cbaaf5e5b4a98f7c541208218d7a3de9b9627fbff37f31770a07",
    13716: "9cce0059324cb7c876fb067c2655649e216a52302cf9bc22470135567dfcab35",
    13420: "1a8f0c4d1e172eb3ec503b636d64ef06f237ce085604fb02ad97a78217b4b306",
    13888: "f6b9d596f3fa6072258aa1eb2c036517bf20cc74c7879eaad1069cb8e1747909",
    14223: "a3cceb28f6df96a77d4747a804b10a346a17f22d9b521261c222bdb03d4ca7a5",
    14497: "acbf06876606a980fd65c3e3185b7725634ad7530f4fa83ee1d3a3963ee23490",
    14796: "5b6b0487112108e6bf6c962add3092e149683c2f7e7b3bbf72c404ec7f017354",
    15044: "1ccc9012bcb5e539c662232dd26cd7cc17c98923621e2a1970ef4d5bb1fa6ded",
    15267: "bfcdac63888f3d85e5b9a802041a21a596a12f5479d6767c8229228f2373423f",
    15471: "e40f15343f472f08a6cb296d7c7bae02fdca90761ec5d19aaf768fe9b09d1e81",
    15546: "e7b797f2b203dc8d9bdc93605b4b5def237785980230b0dc3895cb1abf3bb3c1",
    15690: "6a114671b42b99264afb5e6d476bbc1bc58249da5424f7cd3882689011b642b8",
    1006022: "03219d87e44f9c9e7038e4741d4c22341c7037db70a9d51913e5f9f74dc9a9a5",
    3006000: "e8d0279d40e5e351d16a5e95a7cd177651d7704fe588c318e1af4bd5ac87e268",
    3006027: "49371333c7c39c2c9be21707f98b39d5664870ad3a2d32c8779d015ee3019f61",
    3006137: "63301b60a2a2040c4bdd97c7db5021afc8425e1f0473b96dc0b472a39ba25d32",
    1222544408: "dd68049c48df826848f361fd9e7b23dd20b6805144a2e5bc36e54db638611488",
}

IMPLIED_SUCCESSOR_CODES = {
    (12524, "000905"): "601598",  # 中国外运 replaces 外运发展 on its SSE listing date
    (13420, "000905"): "601868",  # 中国能建 replaces 葛洲坝 on its SSE listing date
}

IDENTIFIER_EVENTS = (
    {
        "notice_id": 1222544408,
        "announcement_date": date(2025, 2, 15),
        "effective_session": date(2025, 2, 17),
        "basis": "CNINFO implementation notice: 300114 changed to 302132; legal issuer and holdings continued",
        "url": "https://static.cninfo.com.cn/finalpage/2025-02-15/1222544408.PDF",
        "changes": (IndexChange.build("000905", ["300114"], ["302132"]),),
    },
)


class CsiIndexSource:
    name = "csi_official"

    def __init__(self, timeout_seconds: float = 30.0) -> None:
        try:
            import requests
        except ImportError as error:
            raise RuntimeError("requests is not installed") from error
        self.session = requests.Session()
        self.timeout_seconds = timeout_seconds
        self.session.headers.update({"User-Agent": "m2-point-in-time-research/1.0"})

    def _get_bytes(self, url: str) -> bytes:
        response = self.session.get(url, timeout=self.timeout_seconds)
        response.raise_for_status()
        return response.content

    def fetch_current(self) -> CurrentUniverse:
        import pandas as pd

        members: dict[str, tuple[str, ...]] = {}
        urls: dict[str, str] = {}
        hashes: dict[str, str] = {}
        as_of_dates: set[date] = set()
        for index_code in INDEX_SIZES:
            url = CURRENT_URL.format(index_code=index_code)
            payload = self._get_bytes(url)
            frame = pd.read_excel(io.BytesIO(payload), dtype=str)
            code_column = next(column for column in frame.columns if "券代码" in str(column))
            date_column = next(column for column in frame.columns if str(column).strip().startswith("日期"))
            values = tuple(sorted({normalize_symbol(value) for value in frame[code_column].dropna()}))
            members[index_code] = values
            as_of_dates.update(parse_date(value) for value in frame[date_column].dropna().unique())
            urls[index_code] = url
            hashes[index_code] = hashlib.sha256(payload).hexdigest()
        if len(as_of_dates) != 1:
            raise ValueError(f"CSI current snapshots are not date-aligned: {sorted(as_of_dates)}")
        return CurrentUniverse(as_of_date=as_of_dates.pop(), members=members, source_urls=urls, source_hashes=hashes)

    def _detail(self, notice_id: int) -> dict[str, Any]:
        response = self.session.get(DETAIL_URL, params={"id": notice_id}, timeout=self.timeout_seconds)
        response.raise_for_status()
        body = response.json()
        if not body.get("success") or not body.get("data"):
            raise RuntimeError(f"CSI notice detail unavailable: {notice_id}")
        return body["data"]

    def discovered_notice_ids(self) -> set[int]:
        payload = {"lang": "cn", "classlist": [], "indexlist": [], "page": {"desc": "", "key": "", "page": 1, "rows": 5000}, "related_topics": [], "typelist": []}
        response = self.session.post(LIST_URL, json=payload, timeout=self.timeout_seconds)
        response.raise_for_status()
        rows = response.json().get("data") or []
        return {
            int(row["id"])
            for row in rows
            if parse_date(row["publishDate"]) >= date(2018, 1, 1)
            and row.get("noticeType") == "announcement"
            and "精明" not in str(row.get("title", ""))
            and (
                str(row.get("title", "")).startswith(("关于调整沪深300", "关于调整中证500"))
                or ("沪深300" in str(row.get("title", "")) and "定期调整结果" in str(row.get("title", "")))
                or str(row.get("title", "")).startswith("关于中证1000等指数样本临时调整")
            )
        }

    @staticmethod
    def _attachment_url(detail: dict[str, Any]) -> str:
        urls = [str(value.get("fileUrl")) for value in detail.get("enclosureList") or [] if value.get("fileUrl")]
        urls.extend(re.findall(r"https?[^\"' <>]+\.(?:xlsx?|pdf)", str(detail.get("content", "")), flags=re.I))
        urls = list(dict.fromkeys(urls))
        if not urls:
            raise ValueError(f"no attachment for CSI notice {detail.get('id')}")
        return urls[-1]

    @staticmethod
    def _parse_excel(payload: bytes) -> dict[str, tuple[list[str], list[str]]]:
        import pandas as pd

        output = {code: ([], []) for code in INDEX_SIZES}
        book = pd.ExcelFile(io.BytesIO(payload))
        if "调入" in book.sheet_names and "调出" in book.sheet_names:
            for sheet, side in (("调入", 1), ("调出", 0)):
                frame = pd.read_excel(io.BytesIO(payload), sheet_name=sheet, dtype=str)
                for row in frame.itertuples(index=False, name=None):
                    index_code = str(row[0]).split(".")[0].zfill(6)
                    if index_code in output and len(row) >= 3 and re.fullmatch(r"\d{6}(?:\.0)?", str(row[2]).strip()):
                        output[index_code][side].append(normalize_symbol(str(row[2]).split(".")[0]))
        else:
            frame = pd.read_excel(io.BytesIO(payload), sheet_name=book.sheet_names[0], header=None, dtype=str)
            for row in frame.itertuples(index=False, name=None):
                index_code = str(row[0]).split(".")[0].zfill(6)
                if index_code not in output:
                    continue
                for position, side in ((2, 0), (4, 1)):
                    if len(row) > position and re.fullmatch(r"\d{6}(?:\.0)?", str(row[position]).strip()):
                        output[index_code][side].append(normalize_symbol(str(row[position]).split(".")[0]))
        return output

    @staticmethod
    def _parse_pdf(payload: bytes) -> dict[str, tuple[list[str], list[str]]]:
        import pdfplumber

        output = {code: ([], []) for code in INDEX_SIZES}
        current_index: str | None = None
        with pdfplumber.open(io.BytesIO(payload)) as document:
            for page in document.pages:
                for table in page.find_tables():
                    upper = page.crop((0, 0, page.width, max(1, table.bbox[1]))).extract_text() or ""
                    compact = re.sub(r"\s+", "", upper)
                    headings = list(re.finditer(r"(沪深300|中证500|中证1000|中证A50|中证A100|中证A500)指数(样本调整名单|备选名单)", compact))
                    if headings:
                        latest = headings[-1]
                        label, section = latest.group(1), latest.group(2)
                        current_index = {"沪深300": "000300", "中证500": "000905"}.get(label) if section == "样本调整名单" else None
                    for row in table.extract():
                        if not row or len(row) < 4:
                            continue
                        left = re.sub(r"\D", "", str(row[0] or ""))
                        right = re.sub(r"\D", "", str(row[2] or ""))
                        if current_index and len(left) == 6 and len(right) == 6:
                            output[current_index][0].append(normalize_symbol(left))
                            output[current_index][1].append(normalize_symbol(right))
        return output

    def fetch_events(self, calendar: TradingCalendar, through: date) -> tuple[list[UniverseEvent], set[int]]:
        events: list[UniverseEvent] = []
        for spec in NOTICE_SPECS:
            if spec.stated_date > through:
                continue
            url = ATTACHMENT_URLS[spec.notice_id]
            payload = self._get_bytes(url)
            parsed = self._parse_pdf(payload) if url.lower().endswith(".pdf") else self._parse_excel(payload)
            for index_code in INDEX_SIZES:
                successor = IMPLIED_SUCCESSOR_CODES.get((spec.notice_id, index_code))
                if successor and parsed[index_code][0] and not parsed[index_code][1]:
                    parsed[index_code][1].append(successor)
            effective = calendar.next_session(spec.stated_date) if spec.after_close else spec.stated_date
            changes = tuple(IndexChange.build(code, removed, added) for code, (removed, added) in parsed.items() if removed or added)
            events.append(UniverseEvent(spec.notice_id, spec.event_type, spec.announcement_date, effective, spec.basis, url, hashlib.sha256(payload).hexdigest(), changes))
        for item in IDENTIFIER_EVENTS:
            if item["effective_session"] <= through:
                payload = self._get_bytes(str(item["url"]))
                events.append(UniverseEvent(
                    int(item["notice_id"]), "identifier_change", item["announcement_date"], item["effective_session"],
                    str(item["basis"]), str(item["url"]), hashlib.sha256(payload).hexdigest(), item["changes"],
                ))
        return sorted(events, key=lambda value: (value.effective_session, value.notice_id)), self.discovered_notice_ids()
