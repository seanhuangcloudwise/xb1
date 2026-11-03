"""signals 单测专用辅助：本地版指标计算 + 合成 K 线构造，与 indicators.py 完全解耦。"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_cfg() -> dict:
    with open(ROOT / "config.example.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def local_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """按 PRD 4.3 口径自算指标列（仅测试用，独立于 indicators.py）。"""
    out = df.copy()
    c, h, l = out["close"], out["high"], out["low"]
    out["prev_close"] = c.shift(1)
    out["white"] = c.ewm(span=10, adjust=False).mean().ewm(span=10, adjust=False).mean()
    ma = lambda n: c.rolling(n).mean()
    out["yellow"] = (ma(14) + ma(28) + ma(57) + ma(114)) / 4
    out["bbi"] = (ma(3) + ma(6) + ma(12) + ma(24)) / 4
    out["ma60"] = ma(60)
    llv = l.rolling(9, min_periods=1).min()
    hhv = h.rolling(9, min_periods=1).max()
    rng = (hhv - llv).replace(0, np.nan)
    rsv = ((c - llv) / rng * 100).fillna(50)
    out["k"] = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    out["d"] = out["k"].ewm(alpha=1 / 3, adjust=False).mean()
    out["j"] = 3 * out["k"] - 2 * out["d"]
    e12 = c.ewm(span=12, adjust=False).mean()
    e26 = c.ewm(span=26, adjust=False).mean()
    out["dif"] = e12 - e26
    out["dea"] = out["dif"].ewm(span=9, adjust=False).mean()
    out["macd"] = 2 * (out["dif"] - out["dea"])
    out["amplitude"] = (h - l) / out["prev_close"] * 100
    return out


def make_bars(pcts, vols, start_price=10.0, amp=0.012, start_date="2025-01-02", open_gap=None) -> pd.DataFrame:
    """由逐日涨跌幅(%)与成交量(股)生成标准 K 线。open=昨收×(1+gap)，high/low 在实体外各扩 amp/2。

    amount = close × volume（元）。首日 pct 视为相对 start_price 的涨幅。
    """
    pcts = list(pcts)
    vols = list(vols)
    assert len(pcts) == len(vols)
    n = len(pcts)
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start_date, periods=n)]
    rows = []
    prev = start_price
    amps = amp if isinstance(amp, (list, tuple, np.ndarray)) else [amp] * n
    for i in range(n):
        cl = prev * (1 + pcts[i] / 100.0)
        op = prev * (1 + (open_gap[i] if open_gap is not None else 0.0))
        hi = max(op, cl) * (1 + amps[i] / 2)
        lo = min(op, cl) * (1 - amps[i] / 2)
        rows.append([dates[i], op, cl, hi, lo, float(vols[i]), cl * vols[i], 1.0, pcts[i]])
        prev = cl
    return pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume", "amount", "turnover", "pct_chg"])


def uptrend(n=150, g=0.6, vol=1e7, surge_at=None, surge_vol=3e7, surge_pct=2.0):
    """稳步上升段：每日 +g%，量 vol；可在 surge_at(距末尾的天数，1=最后一天)放一根放量阳线。"""
    pcts = [g] * n
    vols = [vol] * n
    if surge_at is not None:
        pcts[n - surge_at] = surge_pct
        vols[n - surge_at] = surge_vol
    return pcts, vols


def build(pcts, vols, **kw) -> pd.DataFrame:
    return local_indicators(make_bars(pcts, vols, **kw))


def standard_b1_series():
    """基础“完美 B1”序列：150 日上升趋势（其间有一根放量大阳线），之后 5 日缩量阴跌，最后一日缩量小跌。

    意图：上升趋势后缩量回调、J 超跌、收盘贴近白线。返回 (pcts, vols)。
    """
    pcts, vols = uptrend(150, g=0.6, vol=1e7, surge_at=15, surge_vol=3e7, surge_pct=4.0)
    pcts += [-1.2, -1.0, -1.0, -0.8, -0.8, -0.5]
    vols += [0.8e7, 0.7e7, 0.6e7, 0.5e7, 0.5e7, 0.4e7]
    return pcts, vols
