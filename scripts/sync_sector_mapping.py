"""Sync Shenwan industry classification into Supabase.

The output is used for research and paper-trading explainability only.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import requests

from sync_stock_data import SupabaseRest, env_value, read_env_file


ROOT = Path(__file__).resolve().parents[1]
STOCK_ENGINE_DIR = ROOT / "scripts" / "stock_engine"
if str(STOCK_ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(STOCK_ENGINE_DIR))

from a_stock_trade_common_v7 import import_akshare, kill_proxy_env, normalize_code  # noqa: E402


SW_STOCK_CLASSIFY_URL = "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"

L1_BY_CODE_PREFIX = {
    "11": "农林牧渔",
    "21": "煤炭",
    "22": "石油石化",
    "23": "环保",
    "24": "电力设备",
    "27": "电子",
    "28": "汽车",
    "33": "家用电器",
    "34": "食品饮料",
    "35": "纺织服饰",
    "36": "轻工制造",
    "37": "医药生物",
    "41": "公用事业",
    "42": "交通运输",
    "43": "房地产",
    "45": "商贸零售",
    "46": "社会服务",
    "48": "银行",
    "49": "非银金融",
    "51": "综合",
    "61": "建筑材料",
    "62": "建筑装饰",
    "63": "电力设备",
    "64": "机械设备",
    "65": "国防军工",
    "71": "计算机",
    "72": "传媒",
    "73": "通信",
    "74": "基础化工",
    "75": "美容护理",
    "76": "钢铁",
    "77": "有色金属",
}


def clean_industry_code(value: Any) -> str:
    text = str(value or "").strip().upper().replace(".SI", "")
    if text.startswith("S"):
        text = text[1:]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(6) if digits else ""


def concept_tags_for(l1: str, l2: str = "", l3: str = "") -> list[str]:
    text = " ".join([str(l1 or ""), str(l2 or ""), str(l3 or "")])
    rules = [
        ("新能源", ["电力设备", "锂电池", "光伏", "风电", "储能"]),
        ("AI算力", ["通信", "通信设备", "光模块", "计算机", "服务器", "算力"]),
        ("半导体", ["半导体", "电子", "集成电路", "芯片"]),
        ("创新药", ["创新药", "化学制药", "生物制品", "医药生物"]),
        ("消费电子", ["消费电子", "电子", "元件", "光学光电子"]),
        ("白酒", ["白酒", "食品饮料"]),
        ("银行", ["银行"]),
        ("券商保险", ["证券", "保险", "非银金融"]),
        ("新能源汽车", ["汽车", "乘用车", "电动乘用车"]),
    ]
    tags = []
    for tag, keywords in rules:
        if any(keyword in text for keyword in keywords):
            tags.append(tag)
    return tags


def load_stock_codes() -> pd.DataFrame:
    ak = import_akshare()
    kill_proxy_env()
    raw = ak.stock_info_a_code_name()
    if raw is None or raw.empty:
        raise RuntimeError("akshare stock_info_a_code_name returned empty data")
    out = raw.rename(columns={"证券代码": "code", "股票代码": "code", "证券简称": "name", "股票简称": "name"}).copy()
    if "code" not in out.columns or "name" not in out.columns:
        raise RuntimeError(f"Unexpected stock code columns: {list(raw.columns)}")
    out["code"] = out["code"].apply(normalize_code)
    out["name"] = out["name"].astype(str)
    return out[["code", "name"]].drop_duplicates("code")


def load_sw_history_from_excel() -> pd.DataFrame:
    warnings.filterwarnings("ignore", message="Unverified HTTPS request")
    response = requests.get(
        SW_STOCK_CLASSIFY_URL,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=60,
        verify=False,
    )
    response.raise_for_status()
    raw = pd.read_excel(io.BytesIO(response.content), dtype={"股票代码": "str", "行业代码": "str"})
    raw = raw.rename(columns={
        "股票代码": "symbol",
        "计入日期": "start_date",
        "行业代码": "industry_code",
        "更新日期": "update_time",
    })
    raw["start_date"] = pd.to_datetime(raw["start_date"], errors="coerce")
    raw["industry_code"] = raw["industry_code"].apply(clean_industry_code)
    raw["symbol"] = raw["symbol"].apply(normalize_code)
    return raw[["symbol", "start_date", "industry_code"]].dropna(subset=["symbol", "industry_code"])


def load_sw_history() -> pd.DataFrame:
    ak = import_akshare()
    kill_proxy_env()
    try:
        raw = ak.stock_industry_clf_hist_sw()
        raw = raw.rename(columns={"股票代码": "symbol", "行业代码": "industry_code", "计入日期": "start_date"})
        raw["symbol"] = raw["symbol"].apply(normalize_code)
        raw["industry_code"] = raw["industry_code"].apply(clean_industry_code)
        raw["start_date"] = pd.to_datetime(raw["start_date"], errors="coerce")
        return raw[["symbol", "start_date", "industry_code"]].dropna(subset=["symbol", "industry_code"])
    except Exception as error:
        print(f"akshare stock_industry_clf_hist_sw failed, using official Excel fallback: {error}", file=sys.stderr)
        return load_sw_history_from_excel()


def latest_history_by_symbol(sw_history: pd.DataFrame) -> pd.DataFrame:
    history = sw_history.copy()
    history["start_date"] = pd.to_datetime(history["start_date"], errors="coerce")
    history = history.sort_values(["symbol", "start_date"])
    return history.drop_duplicates("symbol", keep="last")


def sw_name_row_from_cninfo(code: str) -> dict[str, str] | None:
    ak = import_akshare()
    raw = ak.stock_industry_change_cninfo(symbol=code, start_date="19900101", end_date=datetime.now().strftime("%Y%m%d"))
    if raw is None or raw.empty or "分类标准" not in raw.columns:
        return None
    sw_rows = raw[raw["分类标准"].astype(str).str.contains("申银万国行业分类标准", na=False)].copy()
    if sw_rows.empty:
        return None
    if "变更日期" in sw_rows.columns:
        sw_rows["变更日期"] = pd.to_datetime(sw_rows["变更日期"], errors="coerce")
        sw_rows = sw_rows.sort_values("变更日期")
    row = sw_rows.iloc[-1]
    industry_code = clean_industry_code(row.get("行业编码"))
    return {
        "industry_code": industry_code,
        "l1": str(row.get("行业门类") or "").strip(),
        "l2": str(row.get("行业次类") or row.get("行业大类") or "").strip(),
        "l3": str(row.get("行业中类") or row.get("行业大类") or "").strip(),
    }


def load_industry_names(sw_history: pd.DataFrame) -> dict[str, dict[str, str]]:
    latest = latest_history_by_symbol(sw_history)
    samples = latest.drop_duplicates("industry_code", keep="first")
    names: dict[str, dict[str, str]] = {}
    for item in samples.to_dict("records"):
        code = str(item.get("symbol") or "")
        try:
            row = sw_name_row_from_cninfo(code)
        except Exception as error:
            print(f"CNINFO industry lookup failed for {code}: {error}", file=sys.stderr)
            row = None
        if row and row["industry_code"]:
            names[row["industry_code"]] = {"l1": row["l1"], "l2": row["l2"], "l3": row["l3"]}
        time.sleep(0.05)
    return names


def fallback_industry_names(industry_code: str) -> dict[str, str]:
    l1 = L1_BY_CODE_PREFIX.get(industry_code[:2], "未知申万行业")
    return {"l1": l1, "l2": f"申万二级{industry_code[:4]}", "l3": f"申万三级{industry_code}"}


def build_sector_rows(
    stock_codes: pd.DataFrame,
    sw_history: pd.DataFrame,
    industry_names: dict[str, dict[str, str]],
    limit: int | None = None,
) -> list[dict[str, Any]]:
    latest = latest_history_by_symbol(sw_history)
    stocks = stock_codes.copy()
    stocks["code"] = stocks["code"].apply(normalize_code)
    merged = stocks.merge(latest, left_on="code", right_on="symbol", how="inner")
    if limit:
        merged = merged.head(limit)
    rows: list[dict[str, Any]] = []
    for item in merged.to_dict("records"):
        industry_code = clean_industry_code(item.get("industry_code"))
        names = industry_names.get(industry_code) or fallback_industry_names(industry_code)
        l1 = names.get("l1") or fallback_industry_names(industry_code)["l1"]
        l2 = names.get("l2") or fallback_industry_names(industry_code)["l2"]
        l3 = names.get("l3") or ""
        rows.append({
            "code": normalize_code(item.get("code")),
            "name": str(item.get("name") or ""),
            "shenwan_industry_l1": l1,
            "shenwan_industry_l2": l2,
            "concept_tags": concept_tags_for(l1, l2, l3),
        })
    return rows


def existing_mapping_count(client: SupabaseRest, threshold: int = 500) -> int:
    rows = client.request("GET", f"stock_sector_mapping?select=code&limit={threshold + 1}") or []
    if rows and isinstance(rows[0], dict) and "count" in rows[0]:
        return int(rows[0].get("count") or 0)
    return len(rows)


def should_skip_existing(client: SupabaseRest, force: bool, threshold: int = 500) -> bool:
    if force:
        return False
    return existing_mapping_count(client, threshold=threshold) > threshold


def upsert_in_batches(client: SupabaseRest, rows: list[dict[str, Any]], batch_size: int = 500) -> int:
    total = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        total += client.upsert("stock_sector_mapping", batch, "code")
    return total


def sync_sector_mapping(
    client: SupabaseRest,
    force: bool = False,
    limit: int | None = None,
    stock_codes_loader: Callable[[], pd.DataFrame] = load_stock_codes,
    sw_history_loader: Callable[[], pd.DataFrame] = load_sw_history,
    industry_name_loader: Callable[[pd.DataFrame], dict[str, dict[str, str]]] = load_industry_names,
) -> dict[str, Any]:
    if should_skip_existing(client, force=force):
        return {"skipped": True, "reason": "stock_sector_mapping already has more than 500 rows"}

    stock_codes = stock_codes_loader()
    sw_history = sw_history_loader()
    name_scope_history = sw_history
    if limit:
        limited_codes = set(stock_codes.head(limit)["code"].apply(normalize_code))
        name_scope_history = sw_history[sw_history["symbol"].apply(normalize_code).isin(limited_codes)]
    industry_names = industry_name_loader(name_scope_history)
    rows = build_sector_rows(stock_codes, sw_history, industry_names, limit=limit)
    upserted = upsert_in_batches(client, rows)
    non_empty_l1 = sum(1 for row in rows if str(row.get("shenwan_industry_l1") or "").strip())
    return {
        "skipped": False,
        "stock_codes": len(stock_codes),
        "sw_history_rows": len(sw_history),
        "industry_name_mappings": len(industry_names),
        "rows": len(rows),
        "upserted": upserted,
        "non_empty_l1": non_empty_l1,
    }


def get_client() -> SupabaseRest:
    env = read_env_file()
    return SupabaseRest(
        env_value("VITE_SUPABASE_URL", env),
        env_value("SUPABASE_SERVICE_ROLE_KEY", env),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Fetch and upsert even when table already has >500 rows")
    parser.add_argument("--limit", type=int, default=None, help="Optional maximum stock rows to upsert")
    parser.add_argument("--min-rows", type=int, default=500, help="Minimum acceptable upserted/mapped rows")
    args = parser.parse_args()

    summary = sync_sector_mapping(get_client(), force=args.force, limit=args.limit)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not summary.get("skipped") and int(summary.get("non_empty_l1") or 0) < args.min_rows:
        print(f"Sector mapping sync did not meet acceptance: need >= {args.min_rows} non-empty L1 rows.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
