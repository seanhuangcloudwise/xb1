"""市场择时：活跃市值（代理）序列。

**重要：这不是指南针的专有指标原值**，而是用本地日 K 数据构造的同名代理——
"活跃市值"衡量的是**有真实交易意愿的资金所对应的市值规模**：只有成交足够大的股票才算"活跃"，
热钱退潮时活跃股票数减少，总量比指数跌得更快。据此定义：

    个股流通市值 = 收盘价 × 流通股本，其中 流通股本 = 成交量(手)×100 / (换手率/100)
    活跃市值     = Σ 当日成交额 ≥ active_amount_floor 的个股流通市值
    活跃家数     = 满足条件的个股数量

阈值可在 config.yaml 的 `market.active_amount_floor` 调整（默认 1 亿元）。
输出单位：元（展示时换算为万亿元）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

from . import atomic
from .paths import Paths

_COLS = ["date", "close", "volume", "turnover", "amount", "pct_chg"]


def default_floor(cfg: dict) -> float:
    return float((cfg.get("market") or {}).get("active_amount_floor", 1e8))


def compute_series(paths: Paths, cfg: dict, days: int = 250) -> pd.DataFrame:
    """扫描 raw/kline，返回按日期升序的 DataFrame[date, active_mv, total_mv, n_active, n_total, amount]。"""
    unit = float((cfg.get("fetch") or {}).get("amount_unit", 1) or 1)
    floor = default_floor(cfg)
    files = sorted(paths.kline_dir.glob("*.parquet")) if paths.kline_dir.exists() else []
    loaded = []
    latest = None
    for fp in files:
        try:
            df = pd.read_parquet(fp, columns=_COLS)
        except Exception:
            continue
        if df.empty:
            continue
        df = df.copy()
        df["date"] = df["date"].astype(str)
        last = str(df["date"].max())
        latest = last if latest is None else max(latest, last)
        loaded.append(df)

    rows = []
    for df in loaded:
        # 固定成分：只用"数据已更到最新交易日"的股票，避免旧池子遗留文件造成口径跳变
        if str(df["date"].max()) != latest:
            continue
        df = df.sort_values("date").tail(days)
        to = pd.to_numeric(df["turnover"], errors="coerce")
        vol = pd.to_numeric(df["volume"], errors="coerce")
        amt = pd.to_numeric(df["amount"], errors="coerce") * unit
        close = pd.to_numeric(df["close"], errors="coerce")
        shares = vol * 100.0 / (to / 100.0)      # 流通股本（股）：成交量单位为手
        shares = shares.where((to > 0) & (vol > 0))
        mv = (close * shares).where(shares.notna() & (close > 0))   # 流通市值（元）
        rows.append(pd.DataFrame({
            "date": df["date"].values,
            "mv": mv.values,
            "amount": amt.values,
        }))
    if not rows:
        return pd.DataFrame(columns=["date", "active_mv", "total_mv", "n_active", "n_total", "amount"])

    allrows = pd.concat(rows, ignore_index=True)
    allrows["active"] = allrows["amount"] >= floor
    g = allrows.groupby("date", sort=True)
    out = pd.DataFrame({
        "active_mv": g.apply(lambda x: x.loc[x["active"], "mv"].sum(min_count=1), include_groups=False),
        "total_mv": g["mv"].sum(min_count=1),
        "n_active": g["active"].sum(),
        "n_total": g["mv"].count(),
        "amount": g["amount"].sum(min_count=1),
    }).reset_index()
    return out.sort_values("date").reset_index(drop=True)


def cache_path(paths: Paths) -> Path:
    return paths.cache / "market" / "active_mv.parquet"


def load_or_build(paths: Paths, cfg: dict, days: int = 250,
                  kline_latest: Optional[str] = None) -> pd.DataFrame:
    """带缓存：缓存末日期 ≥ kline_latest 时直接复用，否则重算。"""
    p = cache_path(paths)
    if p.exists():
        try:
            cached = pd.read_parquet(p)
            if len(cached) and (kline_latest is None or str(cached["date"].iloc[-1]) >= str(kline_latest)):
                return cached
        except Exception:
            pass
    df = compute_series(paths, cfg, days=days)
    if len(df):
        atomic.write_parquet(p, df)
    return df


def summarize(series: pd.DataFrame) -> dict:
    """当日概览：活跃市值、日变化、3 日累计、连续阳线天数、活跃家数占比。"""
    if series is None or not len(series):
        return {}
    s = series.reset_index(drop=True)
    last = s.iloc[-1]
    prev = s.iloc[-2] if len(s) > 1 else None
    chg = None
    if prev is not None and float(prev["active_mv"]) > 0:
        chg = (float(last["active_mv"]) / float(prev["active_mv"]) - 1) * 100
    cum3 = None
    if len(s) >= 4 and float(s.iloc[-4]["active_mv"]) > 0:
        cum3 = (float(last["active_mv"]) / float(s.iloc[-4]["active_mv"]) - 1) * 100
    up_days = 0
    for i in range(len(s) - 1, 0, -1):
        if float(s.iloc[i]["active_mv"]) > float(s.iloc[i - 1]["active_mv"]):
            up_days += 1
        else:
            break
    return {
        "date": str(last["date"]),
        "active_mv": float(last["active_mv"]),
        "total_mv": float(last["total_mv"]),
        "n_active": int(last["n_active"]),
        "n_total": int(last["n_total"]),
        "amount": float(last["amount"]),
        "chg": chg,
        "cum3": cum3,
        "up_days": up_days,
        "ratio": (float(last["active_mv"]) / float(last["total_mv"])) if float(last["total_mv"]) else None,
    }


def zone(chg: Optional[float], cum3: Optional[float]) -> tuple[str, str]:
    """按 Z 哥的 4% / -2.3% 口径给三档判断（代理指标，仅供参考）。"""
    if chg is None:
        return "unknown", "数据不足"
    if chg >= 4.0 or (cum3 is not None and cum3 > 4.0):
        return "bull", "多头区间"
    if chg <= -2.3:
        return "bear", "空头区间"
    return "neutral", "变革期"
