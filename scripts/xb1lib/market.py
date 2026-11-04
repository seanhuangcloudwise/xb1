"""市场择时：指南针 0AMV（活跃市值 / 活筹指数）复刻。

**口径来源**（指南针官方未公开算法，以下为公开流传的民间公式）::

    VAR1 := SMA(全市场成交额, 10, 1) / 1e7;
    AA: STICKLINE(VAR1 > REF(VAR1,1), VAR1, REF(VAR1,1), 8, 1), COLORRED;   # 涨：红柱
    AD: STICKLINE(VAR1 < REF(VAR1,1), VAR1, REF(VAR1,1), 8, 0), COLORGREEN; # 跌：绿柱
    C5:  DMA(SMA(VAR1,3,1), VOL/0.02/CAPITAL);    # 5 日成本均线
    C13: DMA(SMA(VAR1,3,1), VOL/0.1/CAPITAL);     # 13 日成本均线
    C34: DMA(SMA(VAR1,3,1), VOL/0.18/CAPITAL);    # 34 日成本均线
    INF: DMA(VAR1, VOL/1.1/CAPITAL);              # 无穷成本均线

其中 VOL/CAPITAL 为全市场换手率（Σ成交量 ÷ Σ流通股本）。

**四区域判别**（指南针官方用法：0AMV 与 5 日 / 13 日成本均线的相对位置）：
    持续上涨区   0AMV > C5 > C13     重仓/满仓
    下跌犹豫区   0AMV < C5 且 C5 > C13  逢高减仓，仓位 ≤50%
    上涨犹豫区   0AMV > C5 且 C5 < C13  逢低布局，仓位 ≤50%
    持续下跌区   0AMV < C5 < C13      空仓

**与官方的差异**：官方用的是 0AMV 专属的"活跃筹码"成交额；本实现用本地股票池的
Σ成交额代替。因此是**同口径的代理序列**，形态与拐点可比，绝对数值不等于指南针。
展示单位：万亿元（官方原始单位为 0AMV 点，1 点 = 1e7 元）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

from . import atomic
from .paths import Paths

_COLS = ["date", "close", "volume", "turnover", "amount"]
REGIONS = {
    "up": ("持续上涨区", "0AMV > C5 > C13", "可重仓"),
    "down_hesitate": ("下跌犹豫区", "0AMV < C5，C5 > C13", "逢高减仓"),
    "up_hesitate": ("上涨犹豫区", "0AMV > C5，C5 < C13", "逢低布局"),
    "down": ("持续下跌区", "0AMV < C5 < C13", "空仓"),
}
REGION_ORDER = ["up", "up_hesitate", "down_hesitate", "down"]


def _sma(values: list[float], n: int, m: int = 1) -> list[float]:
    """通达信 SMA(X,N,M)：y = (M*x + (N-M)*y_prev) / N。"""
    out, y = [], None
    for x in values:
        y = x if y is None else (m * x + (n - m) * y) / n
        out.append(y)
    return out


def _dma(values: list[float], weights: list[float]) -> list[float]:
    """通达信 DMA(X,A)：y = A*x + (1-A)*y_prev。"""
    out, y = [], None
    for x, a in zip(values, weights):
        a = 0.0 if a is None or a != a else min(1.0, max(0.0, a))
        y = x if y is None else a * x + (1 - a) * y
        out.append(y)
    return out


def compute_series(paths: Paths, cfg: dict, days: int = 250) -> pd.DataFrame:
    """扫描 raw/kline，返回 [date, amv, c5, c13, c34, inf, amount, turnover, n]（amv 单位：万亿元）。"""
    files = sorted(paths.kline_dir.glob("*.parquet")) if paths.kline_dir.exists() else []
    loaded, latest = [], None
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

    frames = []
    for df in loaded:
        if str(df["date"].max()) != latest:      # 只用数据已更到最新交易日的股票，避免口径跳变
            continue
        to = pd.to_numeric(df["turnover"], errors="coerce")
        vol = pd.to_numeric(df["volume"], errors="coerce")
        amt = pd.to_numeric(df["amount"], errors="coerce")
        cap = (vol * 100.0 / (to / 100.0)).where((to > 0) & (vol > 0))   # 流通股本（股），vol 单位手
        frames.append(pd.DataFrame({"date": df["date"].values, "vol": (vol * 100.0).values,   # 手 -> 股
                                    "amt": amt.values, "cap": cap.values}))
    if not frames:
        return pd.DataFrame(columns=["date", "amv", "c5", "c13", "c34", "inf", "amount", "turnover", "n"])

    allrows = pd.concat(frames, ignore_index=True)
    g = allrows.groupby("date", sort=True)
    agg = pd.DataFrame({
        "amount": g["amt"].sum(min_count=1),
        "vol": g["vol"].sum(min_count=1),
        "cap": g["cap"].sum(min_count=1),
        "n": g["amt"].count(),
    }).reset_index().sort_values("date").reset_index(drop=True)
    agg = agg.tail(days).reset_index(drop=True)

    amount = [float(x) if pd.notna(x) else 0.0 for x in agg["amount"]]
    vol = [float(x) if pd.notna(x) else 0.0 for x in agg["vol"]]
    cap = [float(x) if pd.notna(x) else 0.0 for x in agg["cap"]]
    turnover = [(v / c) if c > 0 else 0.0 for v, c in zip(vol, cap)]
    amv = [v / 1e12 for v in _sma(amount, 10, 1)]           # 万亿
    sma3 = _sma(amv, 3, 1)
    c5 = _dma(sma3, [t / 0.02 for t in turnover])
    c13 = _dma(sma3, [t / 0.10 for t in turnover])
    c34 = _dma(sma3, [t / 0.18 for t in turnover])
    inf = _dma(amv, [t / 1.10 for t in turnover])
    return pd.DataFrame({"date": agg["date"].values, "amv": amv, "c5": c5, "c13": c13,
                         "c34": c34, "inf": inf, "amount": amount, "turnover": turnover,
                         "n": agg["n"].values})


def cache_path(paths: Paths) -> Path:
    return paths.cache / "market" / "amv.parquet"


def load_or_build(paths: Paths, cfg: dict, days: int = 250,
                  kline_latest: Optional[str] = None) -> pd.DataFrame:
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


def region_at(row) -> tuple[str, str, str, str]:
    """单日四区域判别 -> (key, 名称, 判据, 仓位建议)。"""
    amv, c5, c13 = float(row["amv"]), float(row["c5"]), float(row["c13"])
    if amv > c5 and c5 > c13:
        key = "up"
    elif amv > c5 and c5 <= c13:
        key = "up_hesitate"
    elif amv <= c5 and c5 > c13:
        key = "down_hesitate"
    else:
        key = "down"
    name, rule, action = REGIONS[key]
    return key, name, rule, action


def summarize(series: pd.DataFrame) -> dict:
    if series is None or not len(series):
        return {}
    s = series.reset_index(drop=True)
    last = s.iloc[-1]
    prev = s.iloc[-2] if len(s) > 1 else None
    chg = None
    if prev is not None and float(prev["amv"]) > 0:
        chg = (float(last["amv"]) / float(prev["amv"]) - 1) * 100
    key, name, rule, action = region_at(last)
    return {
        "date": str(last["date"]),
        "amv": float(last["amv"]),
        "c5": float(last["c5"]),
        "c13": float(last["c13"]),
        "c34": float(last.get("c34")) if "c34" in last else None,
        "inf": float(last.get("inf")) if "inf" in last else None,
        "amount": float(last["amount"]),
        "turnover": float(last["turnover"]) if "turnover" in last else None,
        "n": int(last["n"]),
        "chg": chg,
        "region": key,
        "region_name": name,
        "region_rule": rule,
        "region_action": action,
    }


def zone(chg: Optional[float] = None, cum3: Optional[float] = None,
         region: Optional[str] = None) -> tuple[str, str]:
    """兼容旧接口：优先用四区域，其次退回日变化阈值。"""
    if region:
        return region, REGIONS[region][0] if region in REGIONS else "未知"
    if chg is None:
        return "unknown", "数据不足"
    if chg >= 4.0:
        return "up", "偏多"
    if chg <= -2.3:
        return "down", "偏空"
    return "up_hesitate", "震荡"
