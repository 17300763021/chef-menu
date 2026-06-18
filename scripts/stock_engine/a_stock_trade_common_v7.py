#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股交易流程工具公共模块 v7

仅用于技术筛选、盘中确认、风控提醒。
不保证盈利，不构成投资建议。
"""

from __future__ import annotations

import argparse
import contextlib
import math
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import requests


def kill_proxy_env() -> None:
    """清理代理环境变量，避免 requests 被坏代理拦截。"""
    for k in [
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ]:
        os.environ.pop(k, None)
    os.environ.setdefault("NO_PROXY", "*")
    os.environ.setdefault("no_proxy", "*")


@contextlib.contextmanager
def suppress_output():
    """屏蔽第三方库的刷屏进度条。"""
    import os as _os
    old_out, old_err = sys.stdout, sys.stderr
    try:
        with open(_os.devnull, "w", encoding="utf-8") as devnull:
            sys.stdout = devnull
            sys.stderr = devnull
            yield
    finally:
        sys.stdout = old_out
        sys.stderr = old_err


def import_akshare():
    try:
        import akshare as ak
        return ak
    except ModuleNotFoundError:
        print("缺少 akshare，请先安装：")
        print("python -m pip install -U akshare pandas numpy openpyxl -i https://pypi.tuna.tsinghua.edu.cn/simple")
        raise


def normalize_code(code: Any) -> str:
    s = str(code).strip()
    s = s.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    s = "".join(ch for ch in s if ch.isdigit())
    return s.zfill(6)


def market_symbol(code: str) -> str:
    code = normalize_code(code)
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("0", "3")):
        return "sz" + code
    if code.startswith(("8", "4", "9")):
        return "bj" + code
    return code


def eastmoney_secid(code: str) -> str:
    """东方财富 secid：沪市 1.xxxxxx，深/北按 0.xxxxxx 处理。"""
    code = normalize_code(code)
    market_id = "1" if code.startswith("6") else "0"
    return f"{market_id}.{code}"


def safe_float(x: Any, default: float = float("nan")) -> float:
    try:
        if x is None:
            return default
        if isinstance(x, str):
            x = x.replace(",", "").replace("%", "").strip()
            if x in ["", "-", "--", "None", "nan"]:
                return default
        v = float(x)
        if math.isinf(v):
            return default
        return v
    except Exception:
        return default


def fmt(x: Any, n: int = 2) -> str:
    v = safe_float(x)
    if math.isnan(v):
        return "-"
    return f"{v:.{n}f}"


def retry_call(func, *args, retries: int = 2, sleep: float = 0.8, **kwargs):
    last = None
    for i in range(retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last = e
            if i < retries:
                time.sleep(sleep)
    raise last


EASTMONEY_SPOT_URLS = [
    "https://push2.eastmoney.com/api/qt/clist/get",
    "https://82.push2.eastmoney.com/api/qt/clist/get",
    "https://72.push2.eastmoney.com/api/qt/clist/get",
    "https://7.push2.eastmoney.com/api/qt/clist/get",
]

EASTMONEY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/center/gridlist.html",
    "Accept": "application/json,text/plain,*/*",
}

_SPOT_CACHE: Optional[pd.DataFrame] = None
_SPOT_CACHE_AT = 0.0
_SPOT_CACHE_SOURCE = ""
_EM_META_CACHE: Optional[pd.DataFrame] = None
_EM_META_CACHE_AT = 0.0
_INDUSTRY_CACHE: Optional[pd.DataFrame] = None
_INDUSTRY_CACHE_AT = 0.0
_INDUSTRY_TREND_CACHE: Dict[str, Dict[str, Any]] = {}


def _eastmoney_spot_direct() -> pd.DataFrame:
    """直接请求东方财富行情接口，作为 AkShare 接口异常时的备用通道。"""
    http_timeout = safe_float(os.environ.get("A_STOCK_HTTP_TIMEOUT"), 8)
    http_retries = int(safe_float(os.environ.get("A_STOCK_HTTP_RETRIES"), 1))
    base_params = {
        "pn": "1",
        "pz": "500",
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f12",
        "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048",
        "fields": (
            "f2,f3,f5,f6,f7,f8,f9,f10,f12,f14,f15,f16,f17,f18,"
            "f20,f21,f23,f100,f102"
        ),
    }
    field_map = {
        "f12": "code",
        "f14": "name",
        "f2": "price",
        "f3": "pct",
        "f5": "volume",
        "f6": "amount",
        "f7": "amplitude",
        "f15": "high",
        "f16": "low",
        "f17": "open",
        "f18": "pre_close",
        "f10": "volume_ratio",
        "f8": "turnover",
        "f9": "pe",
        "f23": "pb",
        "f20": "total_mv",
        "f21": "float_mv",
        "f100": "industry",
        "f102": "area",
    }

    errors = []
    for url in EASTMONEY_SPOT_URLS:
        try:
            rows = []
            params = base_params.copy()
            first = retry_call(
                requests.get,
                url,
                params=params,
                headers=EASTMONEY_HEADERS,
                timeout=http_timeout,
                retries=http_retries,
                sleep=1.5,
            )
            first.raise_for_status()
            data = first.json().get("data") or {}
            diff = data.get("diff") or []
            total = int(data.get("total") or len(diff))
            if not diff:
                raise RuntimeError("东方财富返回空行情")
            rows.extend(diff)

            page_size = int(params["pz"])
            total_page = max(1, math.ceil(total / page_size))
            for page in range(2, total_page + 1):
                params["pn"] = str(page)
                time.sleep(0.4)
                resp = retry_call(
                    requests.get,
                    url,
                    params=params,
                    headers=EASTMONEY_HEADERS,
                    timeout=http_timeout,
                    retries=http_retries,
                    sleep=1.5,
                )
                resp.raise_for_status()
                page_data = resp.json().get("data") or {}
                rows.extend(page_data.get("diff") or [])

            raw = pd.DataFrame(rows)
            if raw.empty:
                raise RuntimeError("东方财富行情表为空")
            out = pd.DataFrame()
            for src, dst in field_map.items():
                if src in raw.columns:
                    out[dst] = raw[src]
            if "code" not in out.columns:
                raise RuntimeError(f"东方财富字段异常：{list(raw.columns)}")
            out["code"] = out["code"].apply(normalize_code)
            for c in [
                "price", "pct", "volume", "amount", "amplitude", "high", "low",
                "open", "pre_close", "volume_ratio", "turnover", "pe", "pb",
                "total_mv", "float_mv",
            ]:
                if c in out.columns:
                    out[c] = pd.to_numeric(out[c], errors="coerce")
            return out
        except Exception as e:
            errors.append(f"{url}: {e}")

    raise RuntimeError("实时行情获取失败：" + " | ".join(errors[-3:]))


def _eastmoney_industry_board_direct() -> pd.DataFrame:
    """东方财富行业板块直连兜底，避免 AkShare 板块接口异常时完全失明。"""
    http_timeout = safe_float(os.environ.get("A_STOCK_HTTP_TIMEOUT"), 8)
    http_retries = int(safe_float(os.environ.get("A_STOCK_HTTP_RETRIES"), 1))
    params = {
        "pn": "1",
        "pz": "200",
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f3",
        "fs": "m:90+t:2",
        "fields": "f12,f14,f2,f3,f8,f104,f105,f128",
    }
    errors = []
    for url in EASTMONEY_SPOT_URLS:
        try:
            resp = retry_call(
                requests.get,
                url,
                params=params,
                headers=EASTMONEY_HEADERS,
                timeout=http_timeout,
                retries=http_retries,
                sleep=1,
            )
            resp.raise_for_status()
            rows = (resp.json().get("data") or {}).get("diff") or []
            raw = pd.DataFrame(rows)
            if raw.empty:
                raise RuntimeError("东方财富行业板块返回空表")
            out = pd.DataFrame()
            out["industry"] = raw.get("f14", "").astype(str).str.strip()
            out["pct"] = pd.to_numeric(raw.get("f3"), errors="coerce")
            out["price"] = pd.to_numeric(raw.get("f2"), errors="coerce")
            out["turnover"] = pd.to_numeric(raw.get("f8"), errors="coerce")
            out["up_count"] = pd.to_numeric(raw.get("f104"), errors="coerce")
            out["down_count"] = pd.to_numeric(raw.get("f105"), errors="coerce")
            out["leader"] = raw.get("f128", "").astype(str)
            out = out.dropna(subset=["industry", "pct"]).sort_values("pct", ascending=False).reset_index(drop=True)
            out["rank"] = np.arange(1, len(out) + 1)
            out["total"] = len(out)
            return out
        except Exception as e:
            errors.append(f"{url}: {e}")
    raise RuntimeError("行业板块直连失败：" + " | ".join(errors[-3:]))


def _tencent_spot_direct() -> pd.DataFrame:
    """腾讯证券全市场行情，字段比新浪更适合夜间筛选。"""
    url = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList"
    http_timeout = safe_float(os.environ.get("A_STOCK_HTTP_TIMEOUT"), 8)
    http_retries = int(safe_float(os.environ.get("A_STOCK_HTTP_RETRIES"), 1))
    page_size = 200
    rows = []
    total = None
    offset = 0

    while True:
        params = {
            "_appver": "11.17.0",
            "board_code": "aStock",
            "sort_type": "price",
            "direct": "down",
            "offset": str(offset),
            "count": str(page_size),
        }
        resp = retry_call(
            requests.get,
            url,
            params=params,
            headers=EASTMONEY_HEADERS,
            timeout=http_timeout,
            retries=http_retries,
            sleep=1,
        )
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        page_rows = data.get("rank_list") or []
        if total is None:
            total = int(data.get("total") or 0)
        if not page_rows:
            break
        rows.extend(page_rows)
        offset += len(page_rows)
        if total and offset >= total:
            break
        time.sleep(0.15)

    raw = pd.DataFrame(rows)
    if raw.empty:
        raise RuntimeError("腾讯行情返回空表")

    out = pd.DataFrame()
    out["code"] = raw["code"].astype(str).str.replace(r"^(sh|sz|bj)", "", regex=True).apply(normalize_code)
    out["name"] = raw.get("name", "")
    out["price"] = pd.to_numeric(raw.get("zxj"), errors="coerce")
    out["price_change"] = pd.to_numeric(raw.get("zd"), errors="coerce")
    out["pct"] = pd.to_numeric(raw.get("zdf"), errors="coerce")
    out["volume"] = pd.to_numeric(raw.get("volume"), errors="coerce") * 100
    # 腾讯 turnover 单位通常为万元；本项目内部统一按元处理。
    out["amount"] = pd.to_numeric(raw.get("turnover"), errors="coerce") * 1e4
    out["amplitude"] = pd.to_numeric(raw.get("zf"), errors="coerce")
    out["volume_ratio"] = pd.to_numeric(raw.get("lb"), errors="coerce")
    out["turnover"] = pd.to_numeric(raw.get("hsl"), errors="coerce")
    out["pe"] = pd.to_numeric(raw.get("pe_ttm"), errors="coerce")
    # 腾讯 zsz/ltsz 单位通常为亿元；本项目内部统一按元处理。
    out["total_mv"] = pd.to_numeric(raw.get("zsz"), errors="coerce") * 1e8
    out["float_mv"] = pd.to_numeric(raw.get("ltsz"), errors="coerce") * 1e8
    out["speed"] = pd.to_numeric(raw.get("speed"), errors="coerce")
    out["main_net_inflow"] = pd.to_numeric(raw.get("zljlr"), errors="coerce")
    out["main_inflow"] = pd.to_numeric(raw.get("zllr"), errors="coerce")
    out["main_outflow"] = pd.to_numeric(raw.get("zllc"), errors="coerce")
    out["pct_5d"] = pd.to_numeric(raw.get("zdf_d5"), errors="coerce")
    out["pct_10d"] = pd.to_numeric(raw.get("zdf_d10"), errors="coerce")
    out["pct_20d"] = pd.to_numeric(raw.get("zdf_d20"), errors="coerce")
    out["pct_60d"] = pd.to_numeric(raw.get("zdf_d60"), errors="coerce")
    out["pct_year"] = pd.to_numeric(raw.get("zdf_y"), errors="coerce")
    for src in ["hy", "industry", "industry_name", "bk", "bk_name", "board_name", "sshy"]:
        if src in raw.columns:
            out["industry"] = raw[src].astype(str)
            break
    for src in ["area", "dq", "region"]:
        if src in raw.columns:
            out["area"] = raw[src].astype(str)
            break
    return out


def _normalize_spot_df(df: pd.DataFrame) -> pd.DataFrame:
    rename = {
        "代码": "code", "名称": "name", "最新价": "price", "涨跌幅": "pct",
        "成交量": "volume", "成交额": "amount", "振幅": "amplitude",
        "最高": "high", "最低": "low", "今开": "open", "昨收": "pre_close",
        "量比": "volume_ratio", "换手率": "turnover", "市盈率-动态": "pe",
        "市净率": "pb", "总市值": "total_mv", "流通市值": "float_mv",
        "所属行业": "industry", "行业": "industry", "地区": "area",
    }
    out = pd.DataFrame()
    for cn, en in rename.items():
        if cn in df.columns:
            out[en] = df[cn]
    if "code" not in out.columns:
        raise RuntimeError(f"实时行情字段异常：{list(df.columns)}")
    out["code"] = out["code"].apply(normalize_code)
    for c in ["price", "pct", "volume", "amount", "amplitude", "high", "low", "open", "pre_close", "volume_ratio", "turnover", "pe", "pb", "total_mv", "float_mv"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


def get_spot_all(force_refresh: bool = False) -> pd.DataFrame:
    """获取A股实时行情池。"""
    global _SPOT_CACHE, _SPOT_CACHE_AT, _SPOT_CACHE_SOURCE

    ak = import_akshare()
    kill_proxy_env()
    source = os.environ.get("A_STOCK_SPOT_SOURCE", "auto").strip().lower()
    cache_seconds = safe_float(os.environ.get("A_STOCK_SPOT_CACHE_SECONDS"), 60)
    if cache_seconds < 0:
        cache_seconds = 0
    now = time.time()
    if (
        not force_refresh
        and _SPOT_CACHE is not None
        and _SPOT_CACHE_SOURCE == source
        and cache_seconds > 0
        and now - _SPOT_CACHE_AT <= cache_seconds
    ):
        return _SPOT_CACHE.copy()

    errors = []
    result = None

    if source == "sina":
        with suppress_output():
            result = _normalize_spot_df(retry_call(ak.stock_zh_a_spot, retries=2, sleep=2))
        _SPOT_CACHE, _SPOT_CACHE_AT, _SPOT_CACHE_SOURCE = result.copy(), time.time(), source
        return result

    if source in ["tencent", "tx"]:
        result = _tencent_spot_direct()
        _SPOT_CACHE, _SPOT_CACHE_AT, _SPOT_CACHE_SOURCE = result.copy(), time.time(), source
        return result

    if source not in ["auto", "eastmoney", "em"]:
        print(f"未知行情源 {source}，按 auto 处理。")

    if source in ["auto", "eastmoney", "em"]:
        try:
            with suppress_output():
                df = retry_call(ak.stock_zh_a_spot_em, retries=3, sleep=2)
            result = _normalize_spot_df(df)
            _SPOT_CACHE, _SPOT_CACHE_AT, _SPOT_CACHE_SOURCE = result.copy(), time.time(), source
            return result
        except Exception as ak_error:
            errors.append(f"东方财富/AkShare: {ak_error}")
            print(f"AkShare 实时行情接口失败，改用东方财富备用通道：{ak_error}")

        try:
            result = _eastmoney_spot_direct()
            _SPOT_CACHE, _SPOT_CACHE_AT, _SPOT_CACHE_SOURCE = result.copy(), time.time(), source
            return result
        except Exception as em_error:
            errors.append(f"东方财富/直连: {em_error}")
            print(f"东方财富备用通道也失败，改用腾讯行情接口：{em_error}")

    try:
        result = _tencent_spot_direct()
        _SPOT_CACHE, _SPOT_CACHE_AT, _SPOT_CACHE_SOURCE = result.copy(), time.time(), source
        return result
    except Exception as tx_error:
        errors.append(f"腾讯: {tx_error}")
        print(f"腾讯行情接口失败，改用新浪行情接口：{tx_error}")

    try:
        with suppress_output():
            df = retry_call(ak.stock_zh_a_spot, retries=2, sleep=2)
        result = _normalize_spot_df(df)
        _SPOT_CACHE, _SPOT_CACHE_AT, _SPOT_CACHE_SOURCE = result.copy(), time.time(), source
        return result
    except Exception as sina_error:
        errors.append(f"新浪: {sina_error}")

    raise RuntimeError("实时行情获取失败：" + " | ".join(errors))


def get_realtime_one(code: str) -> Optional[Dict[str, Any]]:
    """获取单只股票实时行情，失败返回 None。"""
    code = normalize_code(code)
    try:
        spot = get_spot_all()
        row = spot[spot["code"] == code]
        if row.empty:
            return None
        return row.iloc[0].to_dict()
    except Exception:
        return None


def get_eastmoney_spot_meta(force_refresh: bool = False) -> pd.DataFrame:
    """补充个股行业/地区等元数据。主行情源为腾讯时也可单独取一次东方财富字段。"""
    global _EM_META_CACHE, _EM_META_CACHE_AT
    cache_seconds = safe_float(os.environ.get("A_STOCK_META_CACHE_SECONDS"), 300)
    now = time.time()
    if (
        not force_refresh
        and _EM_META_CACHE is not None
        and cache_seconds > 0
        and now - _EM_META_CACHE_AT <= cache_seconds
    ):
        return _EM_META_CACHE.copy()
    try:
        df = _eastmoney_spot_direct()
        _EM_META_CACHE, _EM_META_CACHE_AT = df.copy(), time.time()
        return df
    except Exception:
        return pd.DataFrame()


def get_industry_board_snapshot(force_refresh: bool = False) -> pd.DataFrame:
    """获取行业板块涨跌幅，用于判断板块轮动强弱。失败时返回空表，不影响原策略。"""
    global _INDUSTRY_CACHE, _INDUSTRY_CACHE_AT
    cache_seconds = safe_float(os.environ.get("A_STOCK_INDUSTRY_CACHE_SECONDS"), 300)
    now = time.time()
    if (
        not force_refresh
        and _INDUSTRY_CACHE is not None
        and cache_seconds > 0
        and now - _INDUSTRY_CACHE_AT <= cache_seconds
    ):
        return _INDUSTRY_CACHE.copy()

    ak = import_akshare()
    kill_proxy_env()
    try:
        with suppress_output():
            raw = retry_call(ak.stock_board_industry_name_em, retries=2, sleep=1)
        if raw is None or raw.empty:
            raise RuntimeError("AkShare 行业板块返回空表")

        rename = {
            "板块名称": "industry",
            "名称": "industry",
            "涨跌幅": "pct",
            "最新价": "price",
            "换手率": "turnover",
            "上涨家数": "up_count",
            "下跌家数": "down_count",
            "领涨股票": "leader",
        }
        out = pd.DataFrame()
        for cn, en in rename.items():
            if cn in raw.columns:
                out[en] = raw[cn]
        if "industry" not in out.columns or "pct" not in out.columns:
            return pd.DataFrame()
        out["industry"] = out["industry"].astype(str).str.strip()
        for c in ["pct", "price", "turnover", "up_count", "down_count"]:
            if c in out.columns:
                out[c] = pd.to_numeric(out[c], errors="coerce")
        out = out.dropna(subset=["industry", "pct"]).sort_values("pct", ascending=False).reset_index(drop=True)
        out["rank"] = np.arange(1, len(out) + 1)
        out["total"] = len(out)
        _INDUSTRY_CACHE, _INDUSTRY_CACHE_AT = out.copy(), time.time()
        return out
    except Exception:
        try:
            out = _eastmoney_industry_board_direct()
            _INDUSTRY_CACHE, _INDUSTRY_CACHE_AT = out.copy(), time.time()
            return out
        except Exception:
            return pd.DataFrame()


def get_industry_trend(industry: str) -> Dict[str, Any]:
    """补充行业板块 3/5 日持续性。接口失败时返回空值，不阻断交易判断。"""
    industry = str(industry or "").strip()
    if not industry:
        return {"板块3日涨跌幅": float("nan"), "板块5日涨跌幅": float("nan"), "板块持续性": "缺失"}
    if industry in _INDUSTRY_TREND_CACHE:
        return _INDUSTRY_TREND_CACHE[industry].copy()

    ak = import_akshare()
    kill_proxy_env()
    try:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
        with suppress_output():
            raw = retry_call(
                ak.stock_board_industry_hist_em,
                symbol=industry,
                start_date=start,
                end_date=end,
                period="日k",
                adjust="",
                retries=1,
                sleep=0.8,
            )
        df = standardize_hist(raw)
        if df.empty or len(df) < 6:
            raise RuntimeError("行业历史不足")
        close = df["close"]
        pct3 = (safe_float(close.iloc[-1]) / safe_float(close.iloc[-4]) - 1) * 100 if safe_float(close.iloc[-4]) else float("nan")
        pct5 = (safe_float(close.iloc[-1]) / safe_float(close.iloc[-6]) - 1) * 100 if safe_float(close.iloc[-6]) else float("nan")
        if not math.isnan(pct3) and not math.isnan(pct5) and pct3 > 1.0 and pct5 > 1.5:
            state = "持续走强"
        elif not math.isnan(pct3) and not math.isnan(pct5) and pct3 < -1.0 and pct5 < -1.5:
            state = "持续走弱"
        elif not math.isnan(pct3) and pct3 > 0.5:
            state = "短线转强"
        elif not math.isnan(pct3) and pct3 < -0.5:
            state = "短线转弱"
        else:
            state = "震荡"
        out = {"板块3日涨跌幅": pct3, "板块5日涨跌幅": pct5, "板块持续性": state}
    except Exception:
        out = {"板块3日涨跌幅": float("nan"), "板块5日涨跌幅": float("nan"), "板块持续性": "缺失"}
    _INDUSTRY_TREND_CACHE[industry] = out.copy()
    return out


def _pick_text_value(*values: Any) -> str:
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if s and s not in ["nan", "None", "-", "--"]:
            return s
    return ""


def get_market_context(spot: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    """用全市场涨跌分布判断今天是否适合开新仓。"""
    try:
        if spot is None:
            spot = get_spot_all()
        pct = pd.to_numeric(spot.get("pct"), errors="coerce").dropna()
        if pct.empty:
            raise RuntimeError("全市场涨跌幅缺失")
        up_ratio = float((pct > 0).mean() * 100)
        down_ratio = float((pct < 0).mean() * 100)
        strong_ratio = float((pct >= 2).mean() * 100)
        weak_ratio = float((pct <= -2).mean() * 100)
        median_pct = float(pct.median())

        if up_ratio >= 62 and median_pct >= 0.35 and strong_ratio >= weak_ratio:
            state = "强势"
            advice = "市场赚钱效应较好，允许按规则小仓试错"
        elif up_ratio <= 38 or median_pct <= -0.45 or weak_ratio >= max(18, strong_ratio * 1.5):
            state = "弱势"
            advice = "市场偏弱，原则上不新开仓，优先处理持仓风险"
        else:
            state = "震荡"
            advice = "市场分化，只做板块强、位置低、风控清楚的票"

        return {
            "市场环境": state,
            "市场建议": advice,
            "上涨家数占比": up_ratio,
            "下跌家数占比": down_ratio,
            "强势股占比": strong_ratio,
            "弱势股占比": weak_ratio,
            "全市场中位涨幅": median_pct,
        }
    except Exception as e:
        return {
            "市场环境": "缺失",
            "市场建议": f"市场数据缺失：{e}",
            "上涨家数占比": float("nan"),
            "下跌家数占比": float("nan"),
            "强势股占比": float("nan"),
            "弱势股占比": float("nan"),
            "全市场中位涨幅": float("nan"),
        }


def get_sector_context(
    code: str,
    name: str = "",
    spot_row: Optional[Dict[str, Any]] = None,
    candidate: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """返回个股所属行业、行业强弱、个股相对板块强弱。"""
    code = normalize_code(code)
    spot_row = spot_row or {}
    candidate = candidate or {}

    industry = _pick_text_value(
        candidate.get("行业"),
        candidate.get("所属行业"),
        candidate.get("板块"),
        candidate.get("industry"),
        spot_row.get("industry"),
        spot_row.get("行业"),
        spot_row.get("板块"),
    )

    if not industry:
        meta = get_eastmoney_spot_meta()
        if not meta.empty and "code" in meta.columns:
            hit = meta[meta["code"] == code]
            if not hit.empty:
                industry = _pick_text_value(hit.iloc[0].get("industry"), hit.iloc[0].get("行业"))

    stock_pct = safe_float(spot_row.get("pct"), safe_float(candidate.get("涨跌幅")))
    boards = get_industry_board_snapshot()
    sector_pct = float("nan")
    sector_rank = float("nan")
    sector_total = float("nan")
    leader = ""

    if industry and not boards.empty:
        hit = boards[boards["industry"].eq(industry)]
        if hit.empty:
            hit = boards[boards["industry"].str.contains(industry, regex=False, na=False)]
        if hit.empty:
            hit = boards[boards["industry"].apply(lambda x: bool(x) and (x in industry or industry in x))]
        if not hit.empty:
            row = hit.iloc[0]
            sector_pct = safe_float(row.get("pct"))
            sector_rank = safe_float(row.get("rank"))
            sector_total = safe_float(row.get("total"))
            leader = _pick_text_value(row.get("leader"))

    rank_pct = sector_rank / sector_total if _is_level(sector_rank) and _is_level(sector_total) else float("nan")
    if not industry:
        state = "缺失"
        note = "板块数据缺失，按原技术规则判断"
    elif not math.isnan(sector_pct) and not math.isnan(rank_pct):
        if sector_pct >= 1.0 and rank_pct <= 0.35:
            state = "强"
            note = "板块处在市场前列，顺势票可信度更高"
        elif sector_pct <= -0.5 or rank_pct >= 0.70:
            state = "弱"
            note = "板块偏弱，个股冲高容易反复"
        else:
            state = "一般"
            note = "板块一般，需要更看重个股位置和量价确认"
    elif not math.isnan(sector_pct):
        if sector_pct >= 1.0:
            state = "强"
            note = "板块涨幅较强"
        elif sector_pct <= -0.5:
            state = "弱"
            note = "板块涨幅偏弱"
        else:
            state = "一般"
            note = "板块涨幅一般"
    else:
        state = "缺失"
        note = "行业已识别，但板块涨跌幅缺失"

    relative_pct = stock_pct - sector_pct if not math.isnan(stock_pct) and not math.isnan(sector_pct) else float("nan")
    if math.isnan(relative_pct):
        relative_state = "缺失"
    elif relative_pct >= 1.0:
        relative_state = "强于板块"
    elif relative_pct <= -1.0:
        relative_state = "弱于板块"
    else:
        relative_state = "跟随板块"

    trend = get_industry_trend(industry) if industry else {
        "板块3日涨跌幅": float("nan"),
        "板块5日涨跌幅": float("nan"),
        "板块持续性": "缺失",
    }
    continuity = str(trend.get("板块持续性") or "缺失")
    if continuity == "持续走强" and state in ["强", "一般"]:
        note += "；3/5日持续走强，板块主线质量更好"
    elif continuity == "持续走弱":
        if state == "强":
            state = "一般"
        elif state == "一般":
            state = "弱"
        note += "；3/5日持续走弱，当日上涨可能只是反抽"

    return {
        "所属板块": industry,
        "板块涨跌幅": sector_pct,
        "板块3日涨跌幅": trend.get("板块3日涨跌幅", float("nan")),
        "板块5日涨跌幅": trend.get("板块5日涨跌幅", float("nan")),
        "板块持续性": continuity,
        "板块排名": int(sector_rank) if not math.isnan(sector_rank) else "",
        "板块总数": int(sector_total) if not math.isnan(sector_total) else "",
        "板块强弱": state,
        "板块提示": note,
        "领涨股": leader,
        "个股相对板块": relative_pct,
        "相对强弱": relative_state,
    }


def get_hist(code: str, days: int = 500) -> Tuple[pd.DataFrame, str]:
    """获取日K，默认前复权。"""
    ak = import_akshare()
    kill_proxy_env()
    hist_source = os.environ.get("A_STOCK_HIST_SOURCE", "auto").strip().lower()
    code = normalize_code(code)
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=max(days * 2, 800))).strftime("%Y%m%d")

    errors = []

    if hist_source in ["sina", "sina_fast"]:
        try:
            with suppress_output():
                df = retry_call(
                    ak.stock_zh_a_daily,
                    symbol=market_symbol(code),
                    start_date=start,
                    end_date=end,
                    adjust="" if hist_source == "sina_fast" else "qfq",
                    retries=2,
                    sleep=1,
                )
            df = standardize_hist(df)
            if not df.empty:
                source_name = "新浪快速日线" if hist_source == "sina_fast" else "新浪前复权日线"
                return df.tail(days).reset_index(drop=True), source_name
        except Exception as e:
            errors.append(f"新浪失败：{e}")
        raise RuntimeError("日线数据获取失败；" + " | ".join(errors[:2]))

    # 东方财富历史行情
    if hist_source in ["auto", "eastmoney", "em"]:
        try:
            with suppress_output():
                df = retry_call(
                    ak.stock_zh_a_hist,
                    symbol=code,
                    period="daily",
                    start_date=start,
                    end_date=end,
                    adjust="qfq",
                    retries=2,
                    sleep=1,
                )
            df = standardize_hist(df)
            if not df.empty:
                return df.tail(days).reset_index(drop=True), "东方财富前复权日线"
        except Exception as e:
            errors.append(f"东方财富失败：{e}")

    # 新浪备用
    try:
        with suppress_output():
            df = retry_call(
                ak.stock_zh_a_daily,
                symbol=market_symbol(code),
                start_date=start,
                end_date=end,
                adjust="qfq",
                retries=2,
                sleep=1,
            )
        df = standardize_hist(df)
        if not df.empty:
            return df.tail(days).reset_index(drop=True), "新浪前复权日线"
    except Exception as e:
        errors.append(f"新浪失败：{e}")

    raise RuntimeError("日线数据获取失败；" + " | ".join(errors[:2]))


def _finalize_minute_df(out: pd.DataFrame) -> pd.DataFrame:
    if out is None or out.empty or "datetime" not in out.columns:
        return pd.DataFrame()
    out = out.copy()
    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    for c in ["open", "close", "high", "low", "volume", "amount", "avg_price"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna(subset=["datetime", "close"]).sort_values("datetime").reset_index(drop=True)
    if out.empty:
        return pd.DataFrame()
    if "avg_price" not in out.columns or out["avg_price"].isna().all():
        if "amount" in out.columns and "volume" in out.columns:
            # 不同源的分钟成交量可能是“股”或“手”，用价格量级自动校正。
            vol_raw = out["volume"].replace(0, np.nan)
            avg_by_share = out["amount"] / vol_raw
            avg_by_lot = out["amount"] / (vol_raw * 100)
            close_ref = safe_float(out["close"].median())
            share_gap = abs(safe_float(avg_by_share.median()) - close_ref)
            lot_gap = abs(safe_float(avg_by_lot.median()) - close_ref)
            vol_shares = vol_raw if share_gap <= lot_gap else vol_raw * 100
            grouped = out.groupby(out["datetime"].dt.date, group_keys=False)
            amount_cum = grouped["amount"].cumsum()
            volume_cum = grouped.apply(lambda x: vol_shares.loc[x.index].cumsum())
            out["avg_price"] = amount_cum / volume_cum.replace(0, np.nan)
        elif "close" in out.columns:
            out["avg_price"] = out.groupby(out["datetime"].dt.date)["close"].expanding().mean().reset_index(level=0, drop=True)
    return out.reset_index(drop=True)


def _eastmoney_minute_direct(code: str, period: str = "1") -> pd.DataFrame:
    """东方财富分钟K直连兜底。只用于公开行情接口失败时的合法备份。"""
    http_timeout = safe_float(os.environ.get("A_STOCK_HTTP_TIMEOUT"), 8)
    http_retries = int(safe_float(os.environ.get("A_STOCK_HTTP_RETRIES"), 1))
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": eastmoney_secid(code),
        "klt": str(period),
        "fqt": "0",
        "end": "20500101",
        "lmt": "320",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    }
    resp = retry_call(
        requests.get,
        url,
        params=params,
        headers=EASTMONEY_HEADERS,
        timeout=http_timeout,
        retries=http_retries,
        sleep=1,
    )
    resp.raise_for_status()
    rows = (resp.json().get("data") or {}).get("klines") or []
    parsed = []
    for line in rows:
        parts = str(line).split(",")
        if len(parts) < 7:
            continue
        parsed.append({
            "datetime": parts[0],
            "open": parts[1],
            "close": parts[2],
            "high": parts[3],
            "low": parts[4],
            "volume": parts[5],
            "amount": parts[6],
        })
    return _finalize_minute_df(pd.DataFrame(parsed))


def get_minute(code: str, period: str = "1") -> pd.DataFrame:
    """获取分钟线，AkShare 失败时走东方财富直连兜底；全部失败返回空表。"""
    ak = import_akshare()
    kill_proxy_env()
    code = normalize_code(code)
    try:
        with suppress_output():
            df = retry_call(ak.stock_zh_a_hist_min_em, symbol=code, period=period, adjust="", retries=1, sleep=0.5)
        if df is not None and not df.empty:
            rename = {
                "时间": "datetime", "开盘": "open", "收盘": "close",
                "最高": "high", "最低": "low", "成交量": "volume",
                "成交额": "amount", "均价": "avg_price",
            }
            out = pd.DataFrame()
            for cn, en in rename.items():
                if cn in df.columns:
                    out[en] = df[cn]
            out = _finalize_minute_df(out)
            if not out.empty:
                return out
    except Exception:
        pass

    try:
        with suppress_output():
            df = retry_call(ak.stock_zh_a_minute, symbol=market_symbol(code), period=period, adjust="", retries=1, sleep=0.5)
        if df is not None and not df.empty:
            rename = {
                "day": "datetime", "时间": "datetime", "日期": "datetime",
                "open": "open", "开盘": "open",
                "close": "close", "收盘": "close",
                "high": "high", "最高": "high",
                "low": "low", "最低": "low",
                "volume": "volume", "成交量": "volume",
                "amount": "amount", "成交额": "amount",
            }
            out = pd.DataFrame()
            for col in df.columns:
                if col in rename:
                    out[rename[col]] = df[col]
            out = _finalize_minute_df(out)
            if not out.empty:
                return out
    except Exception:
        pass

    try:
        return _eastmoney_minute_direct(code, period=period)
    except Exception:
        return pd.DataFrame()


def standardize_hist(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    rename = {
        "日期": "date", "date": "date",
        "开盘": "open", "open": "open",
        "收盘": "close", "close": "close",
        "最高": "high", "high": "high",
        "最低": "low", "low": "low",
        "成交量": "volume", "volume": "volume",
        "成交额": "amount", "amount": "amount",
        "涨跌幅": "pct", "pct": "pct",
    }
    out = pd.DataFrame()
    for col in df.columns:
        if col in rename:
            out[rename[col]] = df[col]

    required = ["date", "open", "close", "high", "low", "volume"]
    if not set(required).issubset(out.columns):
        return pd.DataFrame()

    out["date"] = pd.to_datetime(out["date"])
    for c in ["open", "close", "high", "low", "volume", "amount", "pct"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna(subset=["date", "open", "close", "high", "low"]).sort_values("date").reset_index(drop=True)
    if "pct" not in out.columns:
        out["pct"] = out["close"].pct_change() * 100
    return out


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy().sort_values("date").reset_index(drop=True)
    for n in [5, 10, 20, 60]:
        df[f"ma{n}"] = df["close"].rolling(n).mean()

    df["vol5"] = df["volume"].rolling(5).mean().shift(1)
    df["vol20"] = df["volume"].rolling(20).mean().shift(1)
    df["high20_prev"] = df["high"].rolling(20).max().shift(1)
    df["low20_prev"] = df["low"].rolling(20).min().shift(1)
    df["pct1"] = df["close"].pct_change() * 100
    df["pct3"] = df["close"].pct_change(3) * 100
    df["pct5"] = df["close"].pct_change(5) * 100
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(14).mean()

    # MACD
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["dif"] = ema12 - ema26
    df["dea"] = df["dif"].ewm(span=9, adjust=False).mean()
    df["macd"] = 2 * (df["dif"] - df["dea"])

    # RSI14
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi14"] = 100 - 100 / (1 + rs)

    rng = (df["high"] - df["low"]).replace(0, np.nan)
    df["close_pos"] = (df["close"] - df["low"]) / rng
    df["upper_shadow"] = (df["high"] - df[["open", "close"]].max(axis=1)) / df["close"] * 100
    return df


def support_pressure(df: pd.DataFrame) -> Tuple[float, float, float, float]:
    df = add_indicators(df)
    last = df.iloc[-1]
    close = safe_float(last["close"])
    ma20 = safe_float(last.get("ma20"))
    ma60 = safe_float(last.get("ma60"))
    recent = df.tail(30)

    support_candidates = []
    pressure_candidates = []
    for x in [
        safe_float(recent[recent["low"] < close]["low"].max()),
        safe_float(df.tail(60)["low"].min()),
        ma20,
        ma60,
    ]:
        if math.isnan(x) or x <= 0:
            continue
        if x < close:
            support_candidates.append(x)
        elif x > close:
            pressure_candidates.append(x)

    for x in [
        safe_float(recent[recent["high"] > close]["high"].min()),
        safe_float(recent["high"].max()),
        safe_float(df.tail(60)["high"].max()),
    ]:
        if math.isnan(x) or x <= 0:
            continue
        if x > close:
            pressure_candidates.append(x)
        elif x < close:
            support_candidates.append(x)

    supports = sorted({round(x, 6) for x in support_candidates if x < close}, reverse=True)
    pressures = sorted({round(x, 6) for x in pressure_candidates if x > close})
    support1 = supports[0] if supports else close * 0.95
    support2 = supports[1] if len(supports) > 1 else min(support1 * 0.97, close * 0.90)
    pressure1 = pressures[0] if pressures else close * 1.06
    pressure2 = pressures[1] if len(pressures) > 1 else max(pressure1 * 1.03, close * 1.12)
    return support1, support2, pressure1, pressure2


def _is_level(x: Any) -> bool:
    v = safe_float(x)
    return not math.isnan(v) and not math.isinf(v) and v > 0


def _dedup_levels(levels: List[float]) -> List[float]:
    out: List[float] = []
    for v in sorted([safe_float(x) for x in levels if _is_level(x)]):
        if not out or abs(v - out[-1]) / max(v, 0.01) > 0.003:
            out.append(v)
    return out


def _join_levels(levels: List[float]) -> str:
    vals = _dedup_levels(levels)
    return "、".join(fmt(x) for x in vals) if vals else ""


def _cluster_levels(levels: List[float], tolerance: float = 0.018) -> List[Dict[str, Any]]:
    vals = sorted([safe_float(x) for x in levels if _is_level(x)])
    clusters: List[List[float]] = []
    for v in vals:
        if not clusters:
            clusters.append([v])
            continue
        mid = float(np.mean(clusters[-1]))
        if abs(v - mid) / max(mid, 0.01) <= tolerance:
            clusters[-1].append(v)
        else:
            clusters.append([v])

    out = []
    for group in clusters:
        low = min(group)
        high = max(group)
        mid = float(np.mean(group))
        out.append({
            "low": low,
            "high": high,
            "mid": mid,
            "strength": len(group),
        })
    return out


def _local_extreme_levels(recent: pd.DataFrame, side: str, window: int = 2) -> List[Tuple[int, float]]:
    levels: List[Tuple[int, float]] = []
    if recent is None or len(recent) < window * 2 + 3:
        return levels
    values = recent["high" if side == "high" else "low"].reset_index(drop=True)
    for i in range(window, len(values) - window):
        part = values.iloc[i - window:i + window + 1]
        v = safe_float(values.iloc[i])
        if not _is_level(v):
            continue
        if side == "high" and v >= safe_float(part.max()):
            levels.append((i, v))
        elif side == "low" and v <= safe_float(part.min()):
            levels.append((i, v))
    return levels


def _detect_double_bottom_neckline(recent: pd.DataFrame) -> float:
    if recent is None or len(recent) < 35:
        return float("nan")
    lows = _local_extreme_levels(recent, "low", window=2)
    if len(lows) < 2:
        return float("nan")

    best = float("nan")
    best_score = -1.0
    for i, (p1, l1) in enumerate(lows[:-1]):
        for p2, l2 in lows[i + 1:]:
            gap = p2 - p1
            if gap < 8 or gap > 55:
                continue
            if abs(l1 - l2) / max(min(l1, l2), 0.01) > 0.05:
                continue
            between = recent.reset_index(drop=True).iloc[p1:p2 + 1]
            neckline = safe_float(between["high"].max())
            if not _is_level(neckline):
                continue
            depth = (neckline - max(l1, l2)) / max(neckline, 0.01) * 100
            if depth < 6:
                continue
            score = depth + gap / 10
            if score > best_score:
                best = neckline
                best_score = score
    return best


def key_price_zones(df: pd.DataFrame) -> Dict[str, Any]:
    """用前高/前低密集区、箱体和客观颈线补充支撑压力区域。"""
    df = add_indicators(df)
    last = df.iloc[-1]
    close = safe_float(last["close"])
    ma20 = safe_float(last.get("ma20"))
    ma60 = safe_float(last.get("ma60"))
    recent = df.tail(90).reset_index(drop=True)
    box = df.tail(40).reset_index(drop=True)

    high_levels = [v for _, v in _local_extreme_levels(recent, "high", window=2)]
    low_levels = [v for _, v in _local_extreme_levels(recent, "low", window=2)]
    for x in [
        safe_float(df.tail(20)["high"].max()),
        safe_float(df.tail(60)["high"].max()),
        safe_float(df.tail(20)["low"].min()),
        safe_float(df.tail(60)["low"].min()),
        ma20,
        ma60,
    ]:
        if not _is_level(x):
            continue
        if x >= close:
            high_levels.append(x)
        else:
            low_levels.append(x)

    neckline = _detect_double_bottom_neckline(recent)
    if _is_level(neckline):
        if neckline >= close:
            high_levels.append(neckline)
        else:
            low_levels.append(neckline)

    pressure_clusters = [c for c in _cluster_levels(high_levels) if c["mid"] > close * 1.003]
    support_clusters = [c for c in _cluster_levels(low_levels) if c["mid"] < close * 0.997]

    def pick_pressure(clusters: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not clusters:
            return {"low": float("nan"), "high": float("nan"), "mid": float("nan"), "strength": 0}
        return sorted(
            clusters,
            key=lambda c: (-(c["strength"]), abs(c["mid"] - close) / max(close, 0.01)),
        )[0]

    def pick_support(clusters: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not clusters:
            return {"low": float("nan"), "high": float("nan"), "mid": float("nan"), "strength": 0}
        return sorted(
            clusters,
            key=lambda c: (-(c["strength"]), abs(close - c["mid"]) / max(close, 0.01)),
        )[0]

    pressure = pick_pressure(pressure_clusters)
    support = pick_support(support_clusters)

    box_low = safe_float(box["low"].min())
    box_high = safe_float(box["high"].max())
    box_width = (box_high - box_low) / box_low * 100 if _is_level(box_low) and _is_level(box_high) else float("nan")
    box_valid = _is_level(box_width) and box_width <= 22 and box_low * 0.97 <= close <= box_high * 1.05

    vol_ratio = safe_float(last["volume"]) / safe_float(last.get("vol5")) if safe_float(last.get("vol5")) > 0 else float("nan")
    upper = safe_float(last.get("upper_shadow"), 0)
    close_pos = safe_float(last.get("close_pos"), 0.5)
    high20 = safe_float(last.get("high20_prev"))
    false_break = (
        _is_level(high20)
        and close > high20 * 1.005
        and ((upper >= 4.0 and close_pos < 0.65) or (not math.isnan(vol_ratio) and vol_ratio >= 2.8 and close_pos < 0.60))
    )

    notes = []
    if pressure["strength"] >= 2:
        notes.append(f"前高密集压力 {fmt(pressure['low'])}-{fmt(pressure['high'])}")
    if support["strength"] >= 2:
        notes.append(f"前低密集支撑 {fmt(support['low'])}-{fmt(support['high'])}")
    if box_valid:
        notes.append(f"箱体 {fmt(box_low)}-{fmt(box_high)}")
    if _is_level(neckline):
        notes.append(f"客观颈线 {fmt(neckline)}")
    if false_break:
        notes.append("疑似放量假突破，需等回踩确认")

    return {
        "support_zone_low": support["low"],
        "support_zone_high": support["high"],
        "support_zone_mid": support["mid"],
        "support_zone_strength": int(support["strength"]),
        "pressure_zone_low": pressure["low"],
        "pressure_zone_high": pressure["high"],
        "pressure_zone_mid": pressure["mid"],
        "pressure_zone_strength": int(pressure["strength"]),
        "box_low": box_low if box_valid else float("nan"),
        "box_high": box_high if box_valid else float("nan"),
        "box_width": box_width if box_valid else float("nan"),
        "neckline": neckline,
        "false_break_risk": bool(false_break),
        "zone_note": "；".join(notes),
    }


def _support_level_valid(level: float, y_close: float, pressure1: float) -> bool:
    if not _is_level(level):
        return False
    if _is_level(y_close) and level > y_close * 1.01:
        return False
    if _is_level(pressure1) and level >= pressure1 * 0.995:
        return False
    return True


def build_price_structure(
    price: float,
    y_close: float,
    support1: float,
    support2: float,
    pressure1: float,
    pressure2: float,
    stop: float,
) -> Dict[str, Any]:
    """把昨晚关键位按今天实时价重新分层，避免旧支撑/压力角色混用。"""
    notes: List[str] = []
    if not _is_level(price):
        return {
            "state": "实时价格缺失",
            "current_support": float("nan"),
            "current_pressure": float("nan"),
            "breakout_levels": [],
            "reclaim_levels": [],
            "broken_supports": [],
            "defense_level": stop,
            "invalid_notes": ["实时价格缺失，无法判断位阶"],
            "pressure_state": "实时价格缺失",
            "near_pressure": False,
            "pressure_room": float("nan"),
        }

    raw_supports = [safe_float(support1), safe_float(support2)]
    raw_pressures = [safe_float(pressure1), safe_float(pressure2)]
    valid_supports = [x for x in raw_supports if _support_level_valid(x, y_close, pressure1)]
    if any(_is_level(x) for x in raw_supports) and len(valid_supports) < len([x for x in raw_supports if _is_level(x)]):
        notes.append("昨晚支撑位顺序异常或高于昨收，已按实时价重排")
    if _is_level(support1) and _is_level(pressure1) and support1 >= pressure1:
        notes.append("昨晚支撑/压力顺序异常，旧字段不可直接当买卖线")

    level_items: List[Tuple[str, float]] = []
    for label, v in [("支撑1", support1), ("支撑2", support2), ("压力1", pressure1), ("压力2", pressure2), ("止损", stop)]:
        vv = safe_float(v)
        if _is_level(vv):
            level_items.append((label, vv))

    below = _dedup_levels([v for _, v in level_items if v <= price * 0.998])
    above = _dedup_levels([v for _, v in level_items if v >= price * 1.002])
    current_support = below[-1] if below else float("nan")
    current_pressure = above[0] if above else float("nan")

    breakout_levels = _dedup_levels([x for x in raw_pressures if _is_level(x) and price >= x * 1.005])
    reclaim_levels = _dedup_levels([x for x in raw_supports if _is_level(x) and price <= x * 0.995])
    broken_supports = _dedup_levels([x for x in valid_supports if price <= x * 0.995])

    pressure_room = (
        (current_pressure - price) / price * 100
        if _is_level(current_pressure) and _is_level(price)
        else float("nan")
    )
    near_pressure = _is_level(pressure_room) and pressure_room < 2.5

    if _is_level(stop) and price <= stop * 1.003:
        state = "跌破/贴近止损位"
    elif broken_supports:
        state = "跌破有效支撑"
    elif breakout_levels and reclaim_levels:
        state = "突破旧压力，但上方仍有待收复位"
    elif breakout_levels and near_pressure:
        state = "突破旧压力，临近上方压力"
    elif breakout_levels:
        state = "突破旧压力，等待回踩确认"
    elif reclaim_levels and not valid_supports:
        state = "关键位顺序异常，需复核"
    elif reclaim_levels:
        state = "低于昨晚关键位，需先收复"
    elif near_pressure:
        state = "逼近当前有效压力"
    elif _is_level(current_support) and _is_level(current_pressure):
        state = "支撑压力区间内"
    elif _is_level(current_support):
        state = "压力位缺失，按有效支撑防守"
    else:
        state = "有效关键位不足，需复核"

    if breakout_levels:
        pressure_state = f"已突破旧压力 {_join_levels(breakout_levels)}"
        if reclaim_levels:
            pressure_state += f"；上方待收复 {_join_levels(reclaim_levels)}"
    elif _is_level(current_pressure):
        pressure_state = f"当前有效压力 {fmt(current_pressure)}，空间约 {fmt(pressure_room)}%"
    else:
        pressure_state = "当前有效压力缺失"

    defense_candidates = [stop]
    defense_candidates += [x for x in valid_supports if x <= price * 1.002]
    defense_candidates += breakout_levels
    defense_candidates = [x for x in defense_candidates if _is_level(x) and x <= price * 1.01]
    defense_level = max(defense_candidates) if defense_candidates else stop

    invalid_notes = []
    for n in notes:
        if n not in invalid_notes:
            invalid_notes.append(n)

    return {
        "state": state,
        "current_support": current_support,
        "current_pressure": current_pressure,
        "breakout_levels": breakout_levels,
        "reclaim_levels": reclaim_levels,
        "broken_supports": broken_supports,
        "defense_level": defense_level,
        "invalid_notes": invalid_notes,
        "pressure_state": pressure_state,
        "near_pressure": near_pressure,
        "pressure_room": pressure_room,
    }


def score_stock(df: pd.DataFrame) -> Dict[str, Any]:
    df = add_indicators(df)
    if len(df) < 80:
        raise RuntimeError("日线数量不足，无法计算。")
    last, prev = df.iloc[-1], df.iloc[-2]
    close, open_, high, low = map(safe_float, [last["close"], last["open"], last["high"], last["low"]])
    ma5, ma10, ma20, ma60 = [safe_float(last.get(f"ma{n}")) for n in [5, 10, 20, 60]]
    ma20_5 = safe_float(df.iloc[-6].get("ma20")) if len(df) >= 26 else float("nan")
    vol, vol5 = safe_float(last["volume"]), safe_float(last.get("vol5"))
    vol_ratio = vol / vol5 if vol5 and vol5 > 0 else float("nan")
    pct, pct3, pct5 = safe_float(last.get("pct1")), safe_float(last.get("pct3")), safe_float(last.get("pct5"))
    rsi = safe_float(last.get("rsi14"))
    atr14 = safe_float(last.get("atr14"))
    close_pos = safe_float(last.get("close_pos"), 0.5)
    upper = safe_float(last.get("upper_shadow"), 0)
    high20 = safe_float(last.get("high20_prev"))

    score = 50
    reasons, risks = [], []

    if close > ma20 and ma20 > ma20_5:
        score += 18; reasons.append("股价在向上的20日线上方")
    else:
        score -= 15; risks.append("未站在向上的20日线上方")
    if ma5 > ma10 > ma20:
        score += 10; reasons.append("MA5>MA10>MA20，多头排列")
    else:
        risks.append("均线不是标准多头排列")
    if close > ma60:
        score += 6; reasons.append("股价在60日线上方")
    else:
        score -= 8; risks.append("股价在60日线下方")

    pullback = close > ma20 and low <= ma20 * 1.015 and close >= open_ and 0.7 <= vol_ratio <= 1.6 and 38 <= rsi <= 68 and pct3 < 10
    breakout = high20 > 0 and close > high20 * 1.01 and vol_ratio >= 1.5 and 2 <= pct <= 7 and close_pos >= 0.65 and upper <= 3.5
    reclaim = safe_float(prev["close"]) < safe_float(prev.get("ma20")) and close > ma20 and vol_ratio >= 1.2 and close_pos >= 0.6

    if pullback:
        score += 25; reasons.append("低吸买点：回踩20日线附近不破")
    if breakout:
        score += 28; reasons.append("突破买点：放量突破20日新高")
    if reclaim:
        score += 14; reasons.append("修复买点：重新站回20日线")

    if pct <= -4 and vol_ratio >= 1.5:
        score -= 25; risks.append("放量大跌")
    if pct3 >= 15 or pct5 >= 25:
        score -= 18; risks.append("短线涨幅过大，追高风险")
    if upper >= 5:
        score -= 10; risks.append("上影线较长，上方抛压大")
    if rsi >= 75:
        score -= 10; risks.append("RSI过热")
    if close < ma20 * 0.99:
        score -= 18; risks.append("收盘跌破20日线")
    if safe_float(last.get("dif")) > safe_float(last.get("dea")) and safe_float(last.get("macd")) > 0:
        score += 5; reasons.append("MACD偏多")
    elif safe_float(last.get("dif")) < safe_float(last.get("dea")):
        score -= 4; risks.append("MACD偏弱")

    zones = key_price_zones(df)
    pressure_zone_low = safe_float(zones.get("pressure_zone_low"))
    support_zone_high = safe_float(zones.get("support_zone_high"))
    if zones.get("false_break_risk"):
        score -= 8; risks.append("疑似放量假突破")
    if _is_level(pressure_zone_low):
        pressure_room = (pressure_zone_low - close) / close * 100
        if 0 <= pressure_room <= 2.5 and safe_float(zones.get("pressure_zone_strength")) >= 2:
            score -= 5; risks.append("临近前高密集压力")
    if _is_level(support_zone_high):
        support_room = (close - support_zone_high) / close * 100
        if 0 <= support_room <= 3.5 and safe_float(zones.get("support_zone_strength")) >= 2:
            reasons.append("靠近前低密集支撑区")

    if breakout:
        signal = "放量突破买点"
    elif pullback:
        signal = "回踩20日线低吸买点"
    elif reclaim:
        signal = "站回20日线修复买点"
    elif score >= 68:
        signal = "趋势尚可，但买点不标准"
    else:
        signal = "暂无标准买点"

    score = int(max(0, min(100, score)))
    s1, s2, p1, p2 = support_pressure(df)
    stop = min(s1 * 0.985, close * 0.94)

    if score >= 78 and "买点" in signal and "涨幅过大" not in "；".join(risks):
        action = "可进入第二天确认"
    elif score >= 70:
        action = "观察，等第二天确认"
    elif score >= 60:
        action = "只观察，不建议追买"
    else:
        action = "不建议新买"

    return {
        "score": score,
        "signal": signal,
        "action": action,
        "reasons": reasons[:6],
        "risks": risks[:6],
        "support1": s1,
        "support2": s2,
        "pressure1": p1,
        "pressure2": p2,
        "stop": stop,
        "last_close": close,
        "ma20": ma20,
        "ma5": ma5,
        "ma10": ma10,
        "ma60": ma60,
        "vol_ratio": vol_ratio,
        "rsi14": rsi,
        "atr14": atr14,
        "pct1": pct,
        "pct3": pct3,
        "pct5": pct5,
        "support_zone_low": zones.get("support_zone_low"),
        "support_zone_high": zones.get("support_zone_high"),
        "support_zone_strength": zones.get("support_zone_strength"),
        "pressure_zone_low": zones.get("pressure_zone_low"),
        "pressure_zone_high": zones.get("pressure_zone_high"),
        "pressure_zone_strength": zones.get("pressure_zone_strength"),
        "box_low": zones.get("box_low"),
        "box_high": zones.get("box_high"),
        "neckline": zones.get("neckline"),
        "false_break_risk": zones.get("false_break_risk"),
        "zone_note": zones.get("zone_note"),
        "df": df,
    }


def is_bad_name(name: str) -> bool:
    name = str(name).upper()
    return any(x in name for x in ["ST", "退", "N", "C"])


def balanced_pool(pool: pd.DataFrame, limit: int, mode: str = "balanced") -> pd.DataFrame:
    if pool.empty or len(pool) <= limit:
        return pool.copy()
    mode = mode.lower()
    if mode == "liquidity":
        return pool.sort_values("amount", ascending=False).head(limit).copy()
    if mode == "random":
        return pool.sample(limit, random_state=42).copy()

    mv_col = "float_mv" if "float_mv" in pool.columns and pool["float_mv"].notna().sum() > 30 else "total_mv"
    if mv_col in pool.columns and pool[mv_col].notna().sum() > 30:
        valid = pool.dropna(subset=[mv_col]).copy()
        valid["mv_group"] = pd.qcut(valid[mv_col].rank(method="first"), 3, labels=["小市值", "中市值", "大市值"])
        each = max(1, math.ceil(limit / 3))
        parts = []
        for _, g in valid.groupby("mv_group"):
            parts.append(g.sort_values("amount", ascending=False).head(each))
        return pd.concat(parts, ignore_index=True).head(limit).copy()

    head = pool.sort_values("amount", ascending=False).head(max(1, limit // 2))
    rest = pool.drop(head.index, errors="ignore")
    if len(rest) > limit - len(head):
        rest = rest.sample(limit - len(head), random_state=42)
    return pd.concat([head, rest], ignore_index=True).copy()


def market_risk_policy(market_state: str) -> Dict[str, Any]:
    if market_state == "弱势":
        return {
            "check_ok": False,
            "hard_block": False,
            "position_rate": 0.03,
            "note": "大盘环境弱，仅允许3%试错仓",
        }
    if market_state == "震荡":
        return {
            "check_ok": True,
            "hard_block": False,
            "position_rate": 0.06,
            "note": "大盘震荡，只能降低仓位",
        }
    return {
        "check_ok": market_state == "强势",
        "hard_block": not bool(market_state) or market_state == "缺失",
        "position_rate": 0.08,
        "note": "",
    }


def strategy_risk_count(risk_notes: List[str]) -> int:
    soft_data_markers = ["数据缺失", "不能确认主线强度"]
    return sum(
        1 for note in risk_notes
        if not any(marker in note for marker in soft_data_markers)
    )


def decision_from_realtime(
    candidate: Dict[str, Any],
    rt: Optional[Dict[str, Any]],
    minute: pd.DataFrame,
    mode: str = "buy",
    cost: Optional[float] = None,
    shares: Optional[int] = None,
    sector_context: Optional[Dict[str, Any]] = None,
    market_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """根据候选股昨晚数据 + 今日实时/分钟线给买卖判断。"""
    code = normalize_code(candidate.get("代码") or candidate.get("code"))
    name = str(candidate.get("名称") or candidate.get("name") or "")
    y_close = safe_float(candidate.get("昨收") or candidate.get("现价") or candidate.get("last_close"))
    support1 = safe_float(candidate.get("支撑1") or candidate.get("support1"))
    support2 = safe_float(candidate.get("支撑2") or candidate.get("support2"))
    pressure1 = safe_float(candidate.get("压力1") or candidate.get("pressure1"))
    pressure2 = safe_float(candidate.get("压力2") or candidate.get("pressure2"))
    stop = safe_float(candidate.get("建议止损") or candidate.get("stop"))
    signal = str(candidate.get("信号") or candidate.get("signal") or "")
    support_zone_low = safe_float(candidate.get("支撑区间低") or candidate.get("support_zone_low"))
    support_zone_high = safe_float(candidate.get("支撑区间高") or candidate.get("support_zone_high"))
    pressure_zone_low = safe_float(candidate.get("压力区间低") or candidate.get("pressure_zone_low"))
    pressure_zone_high = safe_float(candidate.get("压力区间高") or candidate.get("pressure_zone_high"))
    box_low = safe_float(candidate.get("箱体下沿") or candidate.get("box_low"))
    box_high = safe_float(candidate.get("箱体上沿") or candidate.get("box_high"))
    neckline = safe_float(candidate.get("颈线位") or candidate.get("neckline"))
    atr14 = safe_float(candidate.get("ATR14") or candidate.get("atr14"))
    zone_note = str(candidate.get("关键位说明") or candidate.get("zone_note") or "")
    hold_days = int(safe_float(candidate.get("持仓天数") or candidate.get("hold_days"), 0))
    false_break_raw = candidate.get("假突破风险")
    if false_break_raw is None:
        false_break_raw = candidate.get("false_break_risk")
    false_break_risk = str(false_break_raw).strip().lower() in ["true", "1", "yes", "是"]
    sector_context = sector_context or {}
    market_context = market_context or {}

    if rt:
        price = safe_float(rt.get("price"), y_close)
        pct = safe_float(rt.get("pct"))
        open_ = safe_float(rt.get("open"))
        high = safe_float(rt.get("high"))
        low = safe_float(rt.get("low"))
        volume_ratio = safe_float(rt.get("volume_ratio"))
    else:
        price = y_close
        pct = float("nan")
        open_ = high = low = volume_ratio = float("nan")

    if not _is_level(stop):
        stop_candidates = []
        if _is_level(support1):
            stop_candidates.append(support1 * 0.985)
        if _is_level(y_close):
            stop_candidates.append(y_close * 0.94)
        if _is_level(price):
            stop_candidates.append(price * 0.94)
        stop = min(stop_candidates) if stop_candidates else float("nan")

    atr_stop = float("nan")
    if _is_level(price) and _is_level(atr14):
        atr_stop = price - 1.8 * atr14
        if _is_level(atr_stop) and atr_stop < price and (not _is_level(stop) or atr_stop > stop):
            stop = atr_stop

    structure = build_price_structure(price, y_close, support1, support2, pressure1, pressure2, stop)
    current_support = safe_float(structure.get("current_support"))
    current_pressure = safe_float(structure.get("current_pressure"))
    breakout_levels = structure.get("breakout_levels", [])
    reclaim_levels = structure.get("reclaim_levels", [])
    broken_supports = structure.get("broken_supports", [])
    defense_level = safe_float(structure.get("defense_level"))
    pressure_room = safe_float(structure.get("pressure_room"))
    breakout_pressure = bool(breakout_levels)
    pressure_state = str(structure.get("pressure_state") or "")

    zone_pressure = float("nan")
    for x in [pressure_zone_low, pressure_zone_high, box_high, neckline]:
        if _is_level(x) and _is_level(price) and x > price * 1.002:
            zone_pressure = x if not _is_level(zone_pressure) else min(zone_pressure, x)
    if _is_level(zone_pressure) and (not _is_level(current_pressure) or zone_pressure < current_pressure):
        current_pressure = zone_pressure
        pressure_room = (current_pressure - price) / price * 100 if _is_level(price) else float("nan")
        pressure_state = f"当前有效压力 {fmt(current_pressure)}，空间约 {fmt(pressure_room)}%"

    zone_support = float("nan")
    for x in [support_zone_high, support_zone_low, box_low, neckline]:
        if _is_level(x) and _is_level(price) and x < price * 0.998:
            zone_support = x if not _is_level(zone_support) else max(zone_support, x)
    if _is_level(zone_support) and (not _is_level(current_support) or zone_support > current_support):
        current_support = zone_support
        if _is_level(defense_level):
            defense_level = max(defense_level, current_support)
        else:
            defense_level = current_support

    checks = []
    risk_notes = list(structure.get("invalid_notes", []))
    hard_blockers: List[str] = []
    data_notes: List[str] = []
    data_quality = 100

    def add_data_issue(text: str, penalty: int = 15, block_new_buy: bool = False) -> None:
        nonlocal data_quality
        if text not in data_notes:
            data_notes.append(text)
        if text not in risk_notes:
            risk_notes.append(text)
        data_quality = max(0, data_quality - penalty)
        if block_new_buy and text not in hard_blockers:
            hard_blockers.append(text)

    if not rt:
        checks.append(("实时行情可用", False))
        add_data_issue("实时行情缺失，禁止新买，只能按旧关键位做持仓风控", 35, True)
    elif not _is_level(price):
        checks.append(("实时价格有效", False))
        add_data_issue("实时价格无效，禁止新买", 35, True)
    else:
        checks.append(("实时行情可用", True))

    # 买入确认：先排除坏位置，再看分时和量能。
    market_state = str(market_context.get("市场环境") or "")
    if not market_state or market_state == "缺失":
        checks.append(("大盘数据可用", False))
        add_data_issue("大盘环境数据缺失，新开仓降级观察", 20, True)
    elif market_state in ["弱势", "强势", "震荡"]:
        market_policy = market_risk_policy(market_state)
        checks.append(("大盘环境支持正常仓位", market_policy["check_ok"]))
        if market_policy["note"]:
            risk_notes.append(market_policy["note"])
        if market_policy["hard_block"]:
            hard_blockers.append("大盘环境不可用")

    sector_state = str(sector_context.get("板块强弱") or "")
    relative_state = str(sector_context.get("相对强弱") or "")
    sector_continuity = str(sector_context.get("板块持续性") or "")
    if not sector_state or sector_state == "缺失":
        checks.append(("板块数据可用", False))
        add_data_issue("板块数据缺失，不能确认主线强度，新买降级", 20, False)
    elif sector_state == "弱":
        sector_ok = relative_state == "强于板块"
        checks.append(("板块不弱或个股明显强于板块", sector_ok))
        risk_notes.append("所属板块偏弱，冲高容易反复")
        if not sector_ok:
            hard_blockers.append("板块弱势")
    elif sector_state in ["强", "一般"]:
        checks.append(("板块不弱或个股明显强于板块", True))

    if sector_continuity == "持续走弱":
        checks.append(("板块3/5日没有持续走弱", False))
        risk_notes.append("板块3/5日持续走弱，当日上涨容易是反抽")
        if relative_state != "强于板块":
            hard_blockers.append("板块持续走弱")
    elif sector_continuity in ["持续走强", "短线转强", "震荡"]:
        checks.append(("板块3/5日没有持续走弱", True))

    relative_pct = safe_float(sector_context.get("个股相对板块"))
    if not math.isnan(relative_pct):
        rel_ok = relative_pct >= -0.8
        checks.append(("个股不明显弱于板块", rel_ok))
        if not rel_ok:
            risk_notes.append("个股明显弱于所属板块")

    if not math.isnan(pct):
        pct_ok = -2.8 <= pct <= 3.8
        checks.append(("涨跌幅处在可确认区间", pct_ok))
        if pct > 3.8:
            risk_notes.append("涨幅超过3.8%，新买容易追高")
        if pct < -2.8:
            risk_notes.append("个股明显走弱")
            hard_blockers.append("个股明显走弱")
    else:
        checks.append(("实时涨跌幅缺失", False))

    if broken_supports:
        checks.append(("没有跌破有效支撑", False))
        risk_notes.append(f"跌破有效支撑 {_join_levels(broken_supports)}")
        hard_blockers.append("跌破有效支撑")
    elif _is_level(stop) and _is_level(price) and price <= stop * 1.003:
        checks.append(("没有触及止损位", False))
        risk_notes.append(f"触及/贴近止损 {fmt(stop)}")
        hard_blockers.append("触及止损")
    else:
        checks.append(("没有跌破有效支撑/止损", True))

    if _is_level(support_zone_low) and _is_level(price) and price <= support_zone_low * 0.995:
        checks.append(("没有跌破前低密集支撑区", False))
        risk_notes.append(f"跌破前低密集支撑区 {fmt(support_zone_low)}-{fmt(support_zone_high)}")
        hard_blockers.append("跌破前低密集支撑区")
    elif _is_level(support_zone_low):
        checks.append(("没有跌破前低密集支撑区", True))

    if breakout_pressure:
        checks.append(("突破旧压力后不按旧压力卖出", True))
    elif _is_level(current_pressure):
        room_ok = pressure_room >= 2.5
        checks.append(("距离当前有效压力仍有空间", room_ok))
        if not room_ok:
            risk_notes.append(f"距离当前有效压力 {fmt(current_pressure)} 太近，上方空间不足")
    else:
        checks.append(("当前有效压力缺失，需降低仓位", False))

    if reclaim_levels:
        checks.append(("没有上方待收复旧支撑", False))
        risk_notes.append(f"上方待收复 {_join_levels(reclaim_levels)}，先看能否站回")
    else:
        checks.append(("没有上方待收复旧支撑", True))

    # 分时判断
    intraday_ok = False
    vwap_ok = False
    not_break_low = True
    if minute is not None and not minute.empty:
        last_m = minute.iloc[-1]
        m_price = safe_float(last_m.get("close"), price)
        avgp = safe_float(last_m.get("avg_price"))
        first_low = safe_float(minute.head(min(len(minute), 15))["low"].min())
        intraday_high = safe_float(minute["high"].max())
        intraday_low = safe_float(minute["low"].min())
        if not math.isnan(avgp):
            vwap_ok = m_price >= avgp
        not_break_low = m_price >= first_low * 0.995 if not math.isnan(first_low) else True
        intraday_ok = vwap_ok and not_break_low
        checks.append(("分时站在均价线上方", vwap_ok))
        checks.append(("没有跌破早盘低点", not_break_low))
    else:
        checks.append(("分时数据缺失，无法确认", False))
        add_data_issue("分时数据缺失，不能确认盘中买点", 25, True)

    # 量比：太大且涨不动危险，适中更好
    if not math.isnan(volume_ratio):
        vol_ok = 0.7 <= volume_ratio <= 3.5
        checks.append(("量比不过热", vol_ok))
        if volume_ratio > 3.5:
            risk_notes.append("量比过大，可能情绪过热或出货")
    else:
        checks.append(("量比缺失", False))
        add_data_issue("量比缺失，追涨/突破买点可信度下降", 10, False)

    anchor = defense_level if _is_level(defense_level) else current_support
    distance_from_anchor = (
        (price - anchor) / anchor * 100
        if _is_level(price) and _is_level(anchor)
        else float("nan")
    )
    chase_block = False
    if _is_level(distance_from_anchor) and distance_from_anchor > 4.8 and (math.isnan(pct) or pct > 2.5):
        chase_block = True
        risk_notes.append(f"当前价距离防守位约 {fmt(distance_from_anchor)}%，不适合追")
    if breakout_pressure and not math.isnan(pct) and pct > 4.5:
        chase_block = True
        risk_notes.append("突破后涨幅偏大，等回踩确认更稳")
    if false_break_risk:
        chase_block = True
        risk_notes.append("日线有疑似假突破风险，必须等回踩确认")
    checks.append(("没有明显追高", not chase_block))

    pass_count = sum(1 for _, ok in checks if ok)
    check_total = len(checks)

    buy_low = float("nan")
    buy_high = float("nan")
    if breakout_pressure:
        breakout_anchor = max(breakout_levels)
        buy_low = breakout_anchor * 0.995
        buy_high = breakout_anchor * 1.018
        if _is_level(current_pressure) and current_pressure > buy_low:
            buy_high = min(buy_high, current_pressure * 0.985)
    elif _is_level(current_support):
        buy_low = current_support * 1.002
        buy_high = min(price * 1.006, current_support * 1.035)
        if _is_level(support_zone_high) and support_zone_high < price * 1.005:
            buy_low = max(buy_low, support_zone_low * 1.002 if _is_level(support_zone_low) else buy_low)
            buy_high = min(buy_high, support_zone_high * 1.018)
        if _is_level(current_pressure) and current_pressure > buy_low:
            buy_high = min(buy_high, current_pressure * 0.975)
    elif _is_level(price):
        risk_notes.append("缺少可靠支撑，无法给出有效买入区间")

    buy_zone_valid = _is_level(buy_low) and _is_level(buy_high) and buy_low <= buy_high
    if not buy_zone_valid:
        buy_low = float("nan")
        buy_high = float("nan")
        risk_notes.append("买入区间无效，先观察不下单")

    price_in_buy_zone = (
        buy_zone_valid
        and _is_level(price)
        and buy_low <= price <= buy_high * 1.003
    )

    risk_pct = (
        (price - stop) / price * 100
        if _is_level(price) and _is_level(stop) and price > stop
        else float("nan")
    )
    basis_price = safe_float(cost, price) if cost and safe_float(cost) > 0 else price
    r_unit = basis_price - stop if _is_level(basis_price) and _is_level(stop) and basis_price > stop else float("nan")
    target_1r = basis_price + r_unit if _is_level(r_unit) else float("nan")
    target_2r = basis_price + 2 * r_unit if _is_level(r_unit) else float("nan")
    risk_notes = list(dict.fromkeys(risk_notes))

    # 卖出判断
    sell_action = "持有观察"
    sell_reason = []
    if cost and shares:
        cost = float(cost)
        pnl_pct = (price - cost) / cost * 100 if cost else 0
        if not math.isnan(stop) and price <= stop:
            sell_action = "触发止损/减仓"
            sell_reason.append(f"跌破建议止损 {fmt(stop)}")
        elif broken_supports:
            sell_action = "减仓防守"
            sell_reason.append(f"跌破有效支撑 {_join_levels(broken_supports)}")
        elif _is_level(defense_level) and price <= defense_level * 0.995:
            sell_action = "减仓防守"
            sell_reason.append(f"跌破防守位 {fmt(defense_level)}")
        elif pnl_pct <= -5:
            sell_action = "亏损超过5%，按纪律止损/减仓"
            sell_reason.append(f"当前亏损约 {fmt(pnl_pct)}%")
        elif hold_days >= 3 and pnl_pct < 2 and not breakout_pressure:
            sell_action = "时间止损/减仓观察"
            sell_reason.append(f"持仓 {hold_days} 天仍未走强，短线交易不宜拖成被动持仓")
        elif _is_level(target_2r) and price >= target_2r:
            sell_action = "达到2R，止盈一部分并跟踪"
            sell_reason.append(f"价格达到2R目标 {fmt(target_2r)}")
        elif breakout_pressure and pnl_pct > 0:
            sell_action = "突破压力，持有并上移防守"
            sell_reason.append(f"已突破旧压力 {_join_levels(breakout_levels)}，旧压力改作回踩防守")
        elif _is_level(current_pressure) and price >= current_pressure * 0.98 and pnl_pct > 0:
            sell_action = "接近压力位，分批止盈"
            sell_reason.append(f"接近当前有效压力 {fmt(current_pressure)}")
        elif _is_level(target_1r) and price >= target_1r and pnl_pct > 0:
            sell_action = "达到1R，先保护利润"
            sell_reason.append(f"价格达到1R目标 {fmt(target_1r)}，止损可上移到成本附近")
        elif pnl_pct >= 10:
            sell_action = "盈利较多，至少止盈一部分"
            sell_reason.append(f"当前盈利约 {fmt(pnl_pct)}%")
        elif risk_notes and pnl_pct > 0:
            sell_action = "有风险信号，考虑减仓"
            sell_reason += risk_notes[:2]
    else:
        pnl_pct = float("nan")

    if (
        pass_count >= max(5, check_total - 2)
        and not hard_blockers
        and not chase_block
        and price_in_buy_zone
        and strategy_risk_count(risk_notes) <= 1
    ):
        buy_action = "可以买小仓"
    elif pass_count >= 4 and "触及止损" not in hard_blockers and "跌破有效支撑" not in hard_blockers:
        buy_action = "谨慎观察，等回踩确认"
    else:
        buy_action = "不建议买"

    if mode == "sell":
        final_action = sell_action
    else:
        final_action = buy_action

    if _is_level(defense_level) and _is_level(stop):
        invalid_line = f"跌破防守位 {fmt(defense_level)} 或止损 {fmt(stop)}"
    elif _is_level(stop):
        invalid_line = f"跌破止损 {fmt(stop)}"
    elif _is_level(defense_level):
        invalid_line = f"跌破防守位 {fmt(defense_level)}"
    else:
        invalid_line = "关键位缺失，需人工复核"

    if mode == "sell":
        position_suggestion = sell_action
    elif buy_action == "可以买小仓":
        if not math.isnan(risk_pct) and risk_pct > 6.5:
            position_suggestion = f"止损距离约 {fmt(risk_pct)}%，只允许试错仓；单笔账户风险≤0.5%"
        else:
            risk_text = f"止损距离约 {fmt(risk_pct)}%" if not math.isnan(risk_pct) else "止损距离缺失"
            position_suggestion = f"首仓不超过计划仓位的1/3；{risk_text}；单笔账户风险≤1%"
    elif buy_action == "谨慎观察，等回踩确认":
        position_suggestion = "先观察，回踩确认后再小仓"
    else:
        position_suggestion = "空仓等待"

    review_notes = []
    for n in risk_notes:
        if "异常" in n or "无效" in n or "缺失" in n:
            review_notes.append(n)
    review_text = "；".join(dict.fromkeys(review_notes))

    return {
        "代码": code,
        "名称": name,
        "昨晚信号": signal,
        "当前价": price,
        "涨跌幅": pct,
        "通过数": pass_count,
        "检查总数": check_total,
        "检查项": checks,
        "买入建议": buy_action,
        "压力状态": pressure_state,
        "突破昨晚压力": breakout_pressure,
        "突破参考位": max(breakout_levels) if breakout_pressure else float("nan"),
        "位阶状态": structure.get("state", ""),
        "当前有效支撑": current_support,
        "当前有效压力": current_pressure,
        "已突破位": _join_levels(breakout_levels),
        "待收复位": _join_levels(reclaim_levels),
        "防守位": defense_level,
        "失效条件": invalid_line,
        "仓位建议": position_suggestion,
        "复核提示": review_text,
        "数据质量分": data_quality,
        "数据缺口": "；".join(data_notes),
        "持仓天数": hold_days,
        "止损距离%": risk_pct,
        "ATR14": atr14,
        "ATR止损位": atr_stop,
        "1R目标价": target_1r,
        "2R目标价": target_2r,
        "买入区间有效": buy_zone_valid,
        "买入区间低": buy_low,
        "买入区间高": buy_high,
        "止损位": stop,
        "支撑1": support1,
        "支撑2": support2,
        "压力1": pressure1,
        "压力2": pressure2,
        "支撑区间低": support_zone_low,
        "支撑区间高": support_zone_high,
        "压力区间低": pressure_zone_low,
        "压力区间高": pressure_zone_high,
        "箱体下沿": box_low,
        "箱体上沿": box_high,
        "颈线位": neckline,
        "关键位说明": zone_note,
        "假突破风险": false_break_risk,
        "市场环境": market_context.get("市场环境", ""),
        "市场建议": market_context.get("市场建议", ""),
        "所属板块": sector_context.get("所属板块", ""),
        "板块涨跌幅": sector_context.get("板块涨跌幅", float("nan")),
        "板块3日涨跌幅": sector_context.get("板块3日涨跌幅", float("nan")),
        "板块5日涨跌幅": sector_context.get("板块5日涨跌幅", float("nan")),
        "板块持续性": sector_context.get("板块持续性", ""),
        "板块排名": sector_context.get("板块排名", ""),
        "板块总数": sector_context.get("板块总数", ""),
        "板块强弱": sector_context.get("板块强弱", ""),
        "板块提示": sector_context.get("板块提示", ""),
        "个股相对板块": sector_context.get("个股相对板块", float("nan")),
        "相对强弱": sector_context.get("相对强弱", ""),
        "风险提示": risk_notes[:4],
        "持仓建议": sell_action,
        "卖出理由": sell_reason[:4],
        "盈亏比例": pnl_pct,
        "最终动作": final_action,
    }
