#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
晚上用：A股候选池筛选器 v7

用途：
  收盘后运行一次，全市场筛选候选股，保存到 watchlists/latest_watchlist.csv。
  第二天盘中用 a_stock_realtime_guard_v7.py 快速读取这个观察池，不用重新全市场扫描。

安装：
  python -m pip install -U akshare pandas numpy openpyxl -i https://pypi.tuna.tsinghua.edu.cn/simple

常用：
  python a_stock_night_scanner_v7.py --limit 150 --top 20 --min-score 70
  python a_stock_night_scanner_v7.py --limit 200 --top 30 --min-score 70 --max-mv 2000

免责声明：只做技术筛选和风控提醒，不保证盈利，不构成投资建议。
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from pathlib import Path
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import pandas as pd

from a_stock_trade_common_v7 import (
    get_spot_all, get_hist, score_stock, multi_factor_score,
    sector_momentum_ranking, safe_float, normalize_code,
    is_bad_name, balanced_pool, get_eastmoney_spot_meta
)


def _optional_supabase_client():
    scripts_dir = Path(__file__).resolve().parents[1]
    scripts_path = str(scripts_dir)
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)
    try:
        from sync_stock_data import SupabaseRest, env_value, read_env_file

        env = read_env_file()
        url = env_value("VITE_SUPABASE_URL", env) or os.environ.get("VITE_SUPABASE_URL", "")
        key = env_value("SUPABASE_SERVICE_ROLE_KEY", env) or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        if not url or not key:
            return None
        return SupabaseRest(url, key)
    except Exception:
        return None


def load_capital_flow_cache(client=None, limit: int = 5000) -> dict:
    if client is None:
        client = _optional_supabase_client()
    if client is None:
        return {}
    select = ",".join([
        "code",
        "name",
        "flow_date",
        "north_bound_net_inflow",
        "north_bound_holding_pct",
        "north_bound_holding_change",
        "big_order_net_inflow",
        "big_order_buy_ratio",
        "main_net_inflow",
        "main_net_inflow_ratio",
        "margin_balance_change",
    ])
    try:
        rows = client.request(
            "GET",
            f"stock_capital_flow?select={select}&order=flow_date.desc&limit={int(limit)}",
        ) or []
    except Exception:
        return {}

    cache = {}
    for row in rows:
        code = normalize_code(row.get("code", ""))
        if code and code not in cache:
            cache[code] = row
    return cache


def load_sector_data_cache(client=None, sector_rankings: dict | None = None, limit: int = 6000) -> dict:
    if client is None:
        client = _optional_supabase_client()
    if client is None:
        return {}
    sector_rankings = sector_rankings or {}
    select = "code,name,shenwan_industry_l1,shenwan_industry_l2,concept_tags"
    try:
        rows = client.request("GET", f"stock_sector_mapping?select={select}&limit={int(limit)}") or []
    except Exception:
        return {}

    cache = {}
    for row in rows:
        code = normalize_code(row.get("code", ""))
        if not code:
            continue
        industry = str(row.get("shenwan_industry_l1") or "").strip()
        ranking = sector_rankings.get(industry, {}) if industry else {}
        sector_data = dict(row)
        sector_data["sector_rank"] = ranking.get("rank", "")
        sector_data["sector_return_20d"] = safe_float(ranking.get("momentum_20d"), 0)
        sector_data["sector_return_60d"] = safe_float(ranking.get("momentum_20d"), 0)
        sector_data["avg_volume_ratio"] = safe_float(ranking.get("avg_volume_ratio"), 1)
        cache[code] = sector_data
    return cache


def build_scan_context(args) -> dict:
    client = _optional_supabase_client()
    sector_rankings = {}
    skip_sector_ranking = os.environ.get("A_STOCK_SKIP_SECTOR_RANKING", "").strip() in ["1", "true", "yes"]
    if client is not None and not skip_sector_ranking:
        try:
            sector_rankings = sector_momentum_ranking(client)
        except Exception as e:
            if args.verbose:
                print(f"[multi-factor] 行业动量读取失败，继续使用个股评分：{e}")
    elif skip_sector_ranking and args.verbose:
        print("[multi-factor] 已按环境变量跳过行业动量排名。")
    capital_flow_cache = load_capital_flow_cache(client)
    sector_data_cache = load_sector_data_cache(client, sector_rankings)
    return {
        "capital_flow_cache": capital_flow_cache,
        "sector_data_cache": sector_data_cache,
        "market_state": getattr(args, "market_state", "震荡市"),
    }


def score_with_multifactor_fallback(
    hist: pd.DataFrame,
    code: str,
    capital_flow: dict | None = None,
    sector_data: dict | None = None,
    market_state: str = "震荡市",
) -> tuple[dict, bool]:
    try:
        return multi_factor_score(
            hist,
            code=code,
            capital_flow=capital_flow,
            sector_data=sector_data,
            market_state=market_state,
        ), True
    except Exception:
        return score_stock(hist), False


def classify_pool(row: dict, args) -> str:
    """把夜间结果拆成重点池/观察池，避免把弱确认票和重点票混在一起。"""
    signal = str(row.get("信号", ""))
    reasons = str(row.get("入选理由", ""))
    risks = str(row.get("主要风险", ""))
    score = safe_float(row.get("排名分"), 0)
    day_pct = safe_float(row.get("昨日日涨跌幅"), 0)
    rsi = safe_float(row.get("RSI14"), float("nan"))
    pressure = safe_float(row.get("压力1"))
    close = safe_float(row.get("昨收"))
    pressure_room = (pressure - close) / close * 100 if close and close > 0 and not math.isnan(pressure) else float("nan")
    amount_yi = safe_float(row.get("成交额_亿"), 0)
    turnover = safe_float(row.get("换手率"), float("nan"))
    pct_5d = safe_float(row.get("5日涨跌幅"), float("nan"))
    sector_rank = safe_float(row.get("行业排名"), float("nan"))
    momentum_score = safe_float(row.get("因子动量"), float("nan"))

    standard_buy = (
        "回踩20日线低吸买点" in signal
        or "放量突破买点" in signal
        or "站回20日线修复买点" in signal
        or "低吸买点" in reasons
        or "突破买点" in reasons
        or "修复买点" in reasons
    )
    standard_block = (
        "短线涨幅过大" in risks
        or "RSI过热" in risks
        or "上影线较长" in risks
        or day_pct > 6.0
        or (not math.isnan(rsi) and rsi >= 76)
    )

    quality_trend = (
        score >= args.trend_min_score
        and "股价在向上的20日线上方" in reasons
        and "股价在60日线上方" in reasons
        and "MA5>MA10>MA20" in reasons
        and "放量大跌" not in risks
        and "收盘跌破20日线" not in risks
        and "短线涨幅过大" not in risks
        and "上影线较长" not in risks
        and args.trend_min_day_pct <= day_pct <= args.trend_max_day_pct
        and (math.isnan(rsi) or rsi <= args.trend_max_rsi)
        and (math.isnan(pressure_room) or pressure_room >= args.trend_min_pressure_room)
        and (math.isnan(pct_5d) or pct_5d <= args.trend_max_5d_pct)
        and amount_yi >= args.trend_min_amount / 1e8
        and (
            math.isnan(turnover)
            or args.trend_min_turnover <= turnover <= args.trend_max_turnover
        )
    )

    top_sector_bonus = (
        not math.isnan(sector_rank)
        and sector_rank <= 5
        and standard_buy
        and score >= args.strong_min_score - 3
        and not standard_block
    )
    bottom_sector_cap = (
        not math.isnan(sector_rank)
        and sector_rank >= 19
        and (math.isnan(momentum_score) or momentum_score <= 70)
    )

    if bottom_sector_cap:
        return "观察池"
    if (standard_buy and score >= args.strong_min_score and not standard_block) or quality_trend or top_sector_bonus:
        return "重点池"
    return "观察池"


def strategy_review(item: dict, args) -> tuple[str, str]:
    """补一层交易员复核：技术分不变，但把主线/风险/空间问题显性化。"""
    notes = []
    severe = []
    industry = str(item.get("行业") or "").strip()
    pe = safe_float(item.get("市盈率TTM"), float("nan"))
    amount_yi = safe_float(item.get("成交额_亿"), 0)
    turnover = safe_float(item.get("换手率"), float("nan"))
    amplitude = safe_float(item.get("振幅"), float("nan"))
    main_net = safe_float(item.get("主力净流入_万"), float("nan"))
    pct_5d = safe_float(item.get("5日涨跌幅"), float("nan"))
    pct_60d = safe_float(item.get("60日涨跌幅"), float("nan"))
    close = safe_float(item.get("昨收"))
    pressure = safe_float(item.get("压力1"))
    pressure_room = (pressure - close) / close * 100 if close > 0 and not math.isnan(pressure) else float("nan")

    if not industry:
        notes.append("板块归属缺失，第二天不能按主线票处理")
        severe.append("板块缺失")
    if not math.isnan(pe) and pe >= args.review_high_pe:
        notes.append(f"估值偏高 PE {pe:.0f}，只按短线情绪处理")
    if not math.isnan(turnover) and turnover >= args.review_high_turnover:
        notes.append(f"换手率 {turnover:.1f}% 偏高，波动和分歧较大")
    if not math.isnan(amplitude) and amplitude >= args.review_high_amplitude:
        notes.append(f"振幅 {amplitude:.1f}% 偏大，次日容易反复")
    if not math.isnan(main_net) and amount_yi > 0:
        outflow_ratio = abs(main_net) / max(amount_yi * 10000, 1) * 100
        if main_net < 0 and outflow_ratio >= args.review_main_outflow_ratio:
            notes.append(f"主力净流出约占成交额 {outflow_ratio:.1f}%，需防冲高回落")
    if not math.isnan(pct_5d) and pct_5d >= args.review_max_5d_pct:
        notes.append(f"5日涨幅 {pct_5d:.1f}% 偏大，不适合追")
        severe.append("短线涨幅偏大")
    if not math.isnan(pct_60d):
        if pct_60d <= args.review_min_60d_pct:
            notes.append(f"60日涨幅 {pct_60d:.1f}% 偏弱，中期趋势需复核")
            severe.append("中期弱势")
        elif pct_60d >= args.review_max_60d_pct:
            notes.append(f"60日涨幅 {pct_60d:.1f}% 过大，防高位退潮")
    if not math.isnan(pressure_room) and pressure_room < args.review_min_pressure_room:
        notes.append(f"距离压力1仅 {pressure_room:.1f}%，上方空间不足")
        severe.append("压力空间不足")

    if severe:
        return "降级观察", "；".join(notes)
    if notes:
        return "谨慎候选", "；".join(notes)
    return "核心候选", ""


def analyze_stock(
    row: pd.Series,
    args,
    capital_flow_cache: dict | None = None,
    sector_data_cache: dict | None = None,
    market_state: str = "震荡市",
) -> dict | None:
    code = normalize_code(row["code"])
    name = row.get("name", "")
    hist, source = get_hist(code, days=360)
    capital_flow = (capital_flow_cache or {}).get(code)
    sector_data = (sector_data_cache or {}).get(code)
    r, used_multifactor = score_with_multifactor_fallback(
        hist,
        code,
        capital_flow=capital_flow,
        sector_data=sector_data,
        market_state=market_state,
    )
    industry = str(row.get("industry", "") or "").strip()
    if not industry and isinstance(sector_data, dict):
        industry = str(sector_data.get("shenwan_industry_l1") or "").strip()
    has_buy = "买点" in r["signal"] and "暂无" not in r["signal"]

    if r["score"] < args.min_score and not has_buy:
        return None

    item = {
        "生成日期": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "代码": code,
        "名称": name,
        "行业": industry,
        "排名分": r["score"],
        "昨收": round(r["last_close"], 3),
        "昨日日涨跌幅": round(safe_float(row.get("pct"), 0), 2),
        "成交额_亿": round(safe_float(row.get("amount"), 0) / 1e8, 2),
        "换手率": round(safe_float(row.get("turnover"), float("nan")), 2) if not math.isnan(safe_float(row.get("turnover"), float("nan"))) else np.nan,
        "量比_实时": round(safe_float(row.get("volume_ratio"), float("nan")), 2) if not math.isnan(safe_float(row.get("volume_ratio"), float("nan"))) else np.nan,
        "振幅": round(safe_float(row.get("amplitude"), float("nan")), 2) if not math.isnan(safe_float(row.get("amplitude"), float("nan"))) else np.nan,
        "市盈率TTM": round(safe_float(row.get("pe"), float("nan")), 2) if not math.isnan(safe_float(row.get("pe"), float("nan"))) else np.nan,
        "总市值_亿": round(safe_float(row.get("total_mv"), float("nan")) / 1e8, 2) if not math.isnan(safe_float(row.get("total_mv"), float("nan"))) else np.nan,
        "流通市值_亿": round(safe_float(row.get("float_mv"), float("nan")) / 1e8, 2) if not math.isnan(safe_float(row.get("float_mv"), float("nan"))) else np.nan,
        "主力净流入_万": round(safe_float(row.get("main_net_inflow"), float("nan")), 2) if not math.isnan(safe_float(row.get("main_net_inflow"), float("nan"))) else np.nan,
        "5日涨跌幅": round(safe_float(row.get("pct_5d"), float("nan")), 2) if not math.isnan(safe_float(row.get("pct_5d"), float("nan"))) else np.nan,
        "10日涨跌幅": round(safe_float(row.get("pct_10d"), float("nan")), 2) if not math.isnan(safe_float(row.get("pct_10d"), float("nan"))) else np.nan,
        "20日涨跌幅": round(safe_float(row.get("pct_20d"), float("nan")), 2) if not math.isnan(safe_float(row.get("pct_20d"), float("nan"))) else np.nan,
        "60日涨跌幅": round(safe_float(row.get("pct_60d"), float("nan")), 2) if not math.isnan(safe_float(row.get("pct_60d"), float("nan"))) else np.nan,
        "因子趋势": round(safe_float(r.get("factor_scores", {}).get("trend"), float("nan")), 2) if used_multifactor else "",
        "因子动量": round(safe_float(r.get("factor_scores", {}).get("momentum"), float("nan")), 2) if used_multifactor else "",
        "因子量价": round(safe_float(r.get("factor_scores", {}).get("volume"), float("nan")), 2) if used_multifactor else "",
        "因子资金": round(safe_float(r.get("factor_scores", {}).get("flow"), float("nan")), 2) if used_multifactor else "",
        "因子质量": round(safe_float(r.get("factor_scores", {}).get("quality"), float("nan")), 2) if used_multifactor else "",
        "行业排名": sector_data.get("sector_rank", "") if isinstance(sector_data, dict) else "",
        "动作": r["action"],
        "信号": r["signal"],
        "MA20": round(r["ma20"], 3),
        "量能比": round(r["vol_ratio"], 2) if not math.isnan(r["vol_ratio"]) else np.nan,
        "RSI14": round(r["rsi14"], 2) if not math.isnan(r["rsi14"]) else np.nan,
        "ATR14": round(r["atr14"], 3) if not math.isnan(safe_float(r.get("atr14"))) else np.nan,
        "支撑1": round(r["support1"], 3),
        "支撑2": round(r["support2"], 3),
        "压力1": round(r["pressure1"], 3),
        "压力2": round(r["pressure2"], 3),
        "建议止损": round(r["stop"], 3),
        "支撑区间低": round(r["support_zone_low"], 3) if not math.isnan(safe_float(r.get("support_zone_low"))) else np.nan,
        "支撑区间高": round(r["support_zone_high"], 3) if not math.isnan(safe_float(r.get("support_zone_high"))) else np.nan,
        "压力区间低": round(r["pressure_zone_low"], 3) if not math.isnan(safe_float(r.get("pressure_zone_low"))) else np.nan,
        "压力区间高": round(r["pressure_zone_high"], 3) if not math.isnan(safe_float(r.get("pressure_zone_high"))) else np.nan,
        "箱体下沿": round(r["box_low"], 3) if not math.isnan(safe_float(r.get("box_low"))) else np.nan,
        "箱体上沿": round(r["box_high"], 3) if not math.isnan(safe_float(r.get("box_high"))) else np.nan,
        "颈线位": round(r["neckline"], 3) if not math.isnan(safe_float(r.get("neckline"))) else np.nan,
        "假突破风险": "是" if r.get("false_break_risk") else "否",
        "关键位说明": r.get("zone_note", ""),
        "入选理由": "；".join(r["reasons"][:5]),
        "主要风险": "；".join(r["risks"][:5]),
        "数据源": source,
    }
    item["池分类"] = classify_pool(item, args)
    item["策略等级"], item["策略复核"] = strategy_review(item, args)
    if item["池分类"] == "重点池" and item["策略等级"] == "降级观察":
        item["池分类"] = "观察池"
    return item


def analyze_sequential(selected: pd.DataFrame, args, scan_context: dict | None = None) -> list[dict]:
    scan_context = scan_context or {}
    rows = []
    for i, (_, row) in enumerate(selected.iterrows(), start=1):
        code = normalize_code(row["code"])
        name = row.get("name", "")
        try:
            item = analyze_stock(
                row,
                args,
                capital_flow_cache=scan_context.get("capital_flow_cache"),
                sector_data_cache=scan_context.get("sector_data_cache"),
                market_state=scan_context.get("market_state", "震荡市"),
            )
            if item:
                rows.append(item)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            if args.verbose:
                print(f"[跳过] {code} {name}: {e}")

        if i == 1 or i % 5 == 0 or i == len(selected):
            print(f"总进度 {i}/{len(selected)}，当前入选 {len(rows)} 只。", flush=True)
        time.sleep(args.sleep)
    return rows


def scan(args) -> pd.DataFrame:
    spot = get_spot_all()
    pool = spot.copy()

    for c in ["price", "pct", "amount", "turnover", "total_mv", "float_mv"]:
        if c in pool.columns:
            pool[c] = pd.to_numeric(pool[c], errors="coerce")

    cond = (
        (~pool["name"].astype(str).apply(is_bad_name)) &
        (pool["price"].fillna(0) > args.min_price) &
        (pool["amount"].fillna(0) >= args.min_amount) &
        (pool["pct"].fillna(0).between(-args.max_day_pct, args.max_day_pct))
    )

    turnover_used = False
    mv_used = False
    if "turnover" in pool.columns and pool["turnover"].notna().sum() > 30:
        cond &= pool["turnover"].fillna(0).between(args.min_turnover, args.max_turnover)
        turnover_used = True

    mv_col = "float_mv" if "float_mv" in pool.columns and pool["float_mv"].notna().sum() > 30 else "total_mv"
    if mv_col in pool.columns and pool[mv_col].notna().sum() > 30:
        cond &= pool[mv_col] >= args.min_mv * 1e8
        mv_used = True
        if args.max_mv:
            cond &= pool[mv_col] <= args.max_mv * 1e8

    pool = pool[cond].copy()
    if "industry" not in pool.columns or pool["industry"].astype(str).isin(["", "nan", "None"]).mean() > 0.5:
        meta = get_eastmoney_spot_meta()
        if not meta.empty and {"code", "industry"}.issubset(meta.columns):
            pool = pool.merge(meta[["code", "industry"]].rename(columns={"industry": "industry_meta"}), on="code", how="left")
            if "industry" not in pool.columns:
                pool["industry"] = pool["industry_meta"]
            else:
                pool["industry"] = pool["industry"].where(
                    pool["industry"].astype(str).str.strip().ne("") & pool["industry"].notna(),
                    pool["industry_meta"],
                )
            pool = pool.drop(columns=["industry_meta"], errors="ignore")
    selected = balanced_pool(pool, args.limit, args.pool_mode)

    print(f"开始晚上筛选：候选扫描 {len(selected)} 只。")
    filter_desc = [
        f"成交额>={args.min_amount/1e8:.1f}亿",
        f"日涨跌幅±{args.max_day_pct}%",
        f"股价>{args.min_price}",
    ]
    if turnover_used:
        filter_desc.append(f"换手率 {args.min_turnover}-{args.max_turnover}%")
    if mv_used:
        filter_desc.append(f"市值>={args.min_mv}亿")
        if args.max_mv:
            filter_desc.append(f"市值<={args.max_mv}亿")
    else:
        filter_desc.append("当前行情源无市值字段，已跳过市值过滤")
    print("过滤条件：" + "，".join(filter_desc) + "。")
    print("说明：成交额只做流动性门槛，最后按技术评分和买点排序。\n")

    scan_context = build_scan_context(args)
    if args.verbose:
        print(
            "[multi-factor] "
            f"资金流缓存 {len(scan_context.get('capital_flow_cache', {}))} 条，"
            f"行业数据缓存 {len(scan_context.get('sector_data_cache', {}))} 条。"
        )

    rows = []
    if args.workers <= 1:
        rows = analyze_sequential(selected, args, scan_context)
    else:
        print(f"并发加速：{args.workers} 个进程同时拉日K。")
        try:
            future_map = {}
            with ProcessPoolExecutor(max_workers=args.workers) as executor:
                for _, row in selected.iterrows():
                    future = executor.submit(
                        analyze_stock,
                        row,
                        args,
                        scan_context.get("capital_flow_cache"),
                        scan_context.get("sector_data_cache"),
                        scan_context.get("market_state", "震荡市"),
                    )
                    future_map[future] = (normalize_code(row["code"]), row.get("name", ""))
                    if args.sleep > 0:
                        time.sleep(args.sleep)

                total = len(future_map)
                for i, future in enumerate(as_completed(future_map), start=1):
                    code, name = future_map[future]
                    try:
                        item = future.result()
                        if item:
                            rows.append(item)
                    except KeyboardInterrupt:
                        raise
                    except Exception as e:
                        if args.verbose:
                            print(f"[跳过] {code} {name}: {e}")

                    if i == 1 or i % 5 == 0 or i == total:
                        print(f"总进度 {i}/{total}，当前入选 {len(rows)} 只。", flush=True)
        except Exception as e:
            print(f"并发启动失败，改为稳定单进程：{e}")
            rows = analyze_sequential(selected, args, scan_context)

    df = pd.DataFrame(rows)
    if not df.empty:
        # 重点池优先，其次分数高，最后涨幅低一点更安全
        df["重点优先"] = df["池分类"].eq("重点池")
        df = df.sort_values(["重点优先", "排名分", "昨日日涨跌幅"], ascending=[False, False, True])
        strong = df[df["池分类"].eq("重点池")].head(args.strong_top)
        observe = df[df["池分类"].eq("观察池")].head(args.top)
        df = pd.concat([strong, observe], ignore_index=True)
        df = df.drop(columns=["重点优先"]).reset_index(drop=True)
    return df


def split_pools(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if df.empty or "池分类" not in df.columns:
        return df.iloc[0:0].copy(), df.iloc[0:0].copy()
    strong = df[df["池分类"].eq("重点池")].reset_index(drop=True)
    observe = df[df["池分类"].ne("重点池")].reset_index(drop=True)
    return strong, observe


def save_outputs(df: pd.DataFrame, outdir: str) -> None:
    p = Path(outdir)
    p.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    csv_path = p / f"watchlist_{today}.csv"
    xlsx_path = p / f"watchlist_{today}.xlsx"
    latest_csv = p / "latest_watchlist.csv"
    latest_xlsx = p / "latest_watchlist.xlsx"
    strong_csv = p / f"strong_watchlist_{today}.csv"
    strong_xlsx = p / f"strong_watchlist_{today}.xlsx"
    latest_strong_csv = p / "latest_strong_watchlist.csv"
    latest_strong_xlsx = p / "latest_strong_watchlist.xlsx"
    strong, observe = split_pools(df)

    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    df.to_csv(latest_csv, index=False, encoding="utf-8-sig")
    strong.to_csv(strong_csv, index=False, encoding="utf-8-sig")
    strong.to_csv(latest_strong_csv, index=False, encoding="utf-8-sig")
    try:
        with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
            strong.to_excel(writer, index=False, sheet_name="重点池")
            observe.to_excel(writer, index=False, sheet_name="观察池")
            df.to_excel(writer, index=False, sheet_name="全部")
        with pd.ExcelWriter(latest_xlsx, engine="openpyxl") as writer:
            strong.to_excel(writer, index=False, sheet_name="重点池")
            observe.to_excel(writer, index=False, sheet_name="观察池")
            df.to_excel(writer, index=False, sheet_name="全部")
        strong.to_excel(strong_xlsx, index=False)
        strong.to_excel(latest_strong_xlsx, index=False)
    except Exception:
        pass

    print("\n已保存观察池：")
    print(csv_path)
    print(latest_csv)
    print(f"重点池 {len(strong)} 只；观察池 {len(observe)} 只。")
    print(strong_csv)
    print(latest_strong_csv)
    if xlsx_path.exists():
        print(xlsx_path)
        print(latest_xlsx)
    if strong_xlsx.exists():
        print(strong_xlsx)
        print(latest_strong_xlsx)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=150, help="扫描候选数量，越大越慢")
    ap.add_argument("--top", type=int, default=20, help="最终输出前N只")
    ap.add_argument("--min-score", type=int, default=70, help="最低入选评分")
    ap.add_argument("--strong-top", type=int, default=10, help="重点池最多输出N只")
    ap.add_argument("--strong-min-score", type=int, default=76, help="标准买点进入重点池的最低评分")
    ap.add_argument("--trend-min-score", type=int, default=85, help="高质量趋势票进入重点池的最低评分")
    ap.add_argument("--trend-min-amount", type=float, default=1.5e8, help="高质量趋势票最低成交额")
    ap.add_argument("--trend-min-day-pct", type=float, default=-5.5, help="高质量趋势票当天最低涨跌幅")
    ap.add_argument("--trend-max-day-pct", type=float, default=4.5, help="高质量趋势票当天最高涨幅")
    ap.add_argument("--trend-max-rsi", type=float, default=74.0, help="高质量趋势票最高 RSI")
    ap.add_argument("--trend-min-pressure-room", type=float, default=0.5, help="高质量趋势票距离压力位的最小空间百分比")
    ap.add_argument("--trend-max-5d-pct", type=float, default=12.0, help="高质量趋势票最近5日最高涨幅")
    ap.add_argument("--trend-min-turnover", type=float, default=0.3, help="高质量趋势票最低换手率")
    ap.add_argument("--trend-max-turnover", type=float, default=15.0, help="高质量趋势票最高换手率")
    ap.add_argument("--review-high-pe", type=float, default=300.0, help="策略复核：高估值提示线")
    ap.add_argument("--review-high-turnover", type=float, default=12.0, help="策略复核：高换手提示线")
    ap.add_argument("--review-high-amplitude", type=float, default=9.0, help="策略复核：高振幅提示线")
    ap.add_argument("--review-main-outflow-ratio", type=float, default=8.0, help="策略复核：主力净流出/成交额提示线")
    ap.add_argument("--review-max-5d-pct", type=float, default=12.0, help="策略复核：5日涨幅过大提示线")
    ap.add_argument("--review-min-60d-pct", type=float, default=-25.0, help="策略复核：60日弱势提示线")
    ap.add_argument("--review-max-60d-pct", type=float, default=80.0, help="策略复核：60日过热提示线")
    ap.add_argument("--review-min-pressure-room", type=float, default=1.5, help="策略复核：距离压力位最小空间")
    ap.add_argument("--min-amount", type=float, default=1e8, help="最低成交额，默认1亿")
    ap.add_argument("--min-price", type=float, default=2.0, help="最低股价")
    ap.add_argument("--max-day-pct", type=float, default=7.0, help="过滤当天涨跌幅绝对值过大的股票")
    ap.add_argument("--min-turnover", type=float, default=0.3, help="最低换手率")
    ap.add_argument("--max-turnover", type=float, default=15.0, help="最高换手率")
    ap.add_argument("--min-mv", type=float, default=30.0, help="最低市值，单位亿元")
    ap.add_argument("--max-mv", type=float, default=None, help="最高市值，单位亿元")
    ap.add_argument("--pool-mode", choices=["balanced", "liquidity", "random"], default="balanced")
    ap.add_argument("--market-state", default="震荡市", choices=["强牛市", "弱牛市", "震荡市", "震荡", "熊市", "防御"], help="多因子权重使用的市场状态")
    ap.add_argument("--sleep", type=float, default=0.15, help="每只股票之间停顿，网络差可调大")
    ap.add_argument("--workers", type=int, default=1, help="并发拉取日K的线程数，建议 1-4")
    ap.add_argument("--outdir", default="watchlists")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    df = scan(args)
    if df.empty:
        print("\n没有筛出符合条件的股票。可以降低 --min-score 或增大 --limit。")
        return

    strong, observe = split_pools(df)
    show_cols = ["池分类", "策略等级", "代码", "名称", "排名分", "因子趋势", "因子动量", "因子量价", "因子资金", "因子质量", "行业排名", "昨收", "信号", "动作", "支撑1", "压力1", "建议止损", "入选理由", "主要风险", "策略复核"]
    if not strong.empty:
        print("\n===== 重点池：明天优先盯 =====")
        print(strong[show_cols].to_string(index=False))
    else:
        print("\n===== 重点池：暂无 =====")
    if not observe.empty:
        print("\n===== 观察池：趋势还行，但不急买 =====")
        print(observe[show_cols].to_string(index=False))
    save_outputs(df, args.outdir)

    print("\n第二天盘中执行：")
    print("python a_stock_live_decision_v8.py --watchlist watchlists/latest_strong_watchlist.csv")
    print("\n已有持仓也想一起看卖点，可以建 holdings.csv，格式：代码,名称,成本,股数")


if __name__ == "__main__":
    main()
