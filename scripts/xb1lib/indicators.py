"""F3 指标计算（PRD 4.3 / DEV_CONTRACTS「indicators.py」）。

compute_indicators 是纯函数（不读写文件、不修改入参）；compute_and_cache 负责落盘缓存。

口径说明
--------
* 白线 white = EMA(EMA(C,10),10)：ewm(span=10, adjust=False) 连续两次，首值取首日收盘，
  因此从第 1 行起即有值（EMA 无"窗口不足"概念）。
* 黄线 yellow / BBI bbi / ma60 为简单均线组合，窗口不足（如黄线需 114 行）时为 NaN
  （rolling 的 min_periods = 窗口长度）。
* KDJ(9,3,3)，通达信口径：
    - RSV = (C - LLV(L,9)) / (HHV(H,9) - LLV(L,9)) * 100，LLV/HHV 取 low/high 列的 9 日极值；
    - 前 8 行窗口不足时用已有数据的最高/最低（min_periods=1）；
    - 分母为 0（窗口内最高=最低，如一字板/停牌填充）时 RSV 取 50（按契约；
      不沿用上一日 RSV，保证结果只依赖当日窗口、便于对账）；
    - K = SMA(RSV,3,1) = ewm(alpha=1/3, adjust=False)，K/D 初值 50（即递推 K0 = (RSV0 + 2*50)/3，
      而不是直接令 K0=RSV0）；D 同理对 K 递推；J = 3K - 2D。
* MACD(12,26,9)：dif = EMA12 - EMA26，dea = EMA(dif, 9)，macd = 2*(dif - dea)，均 adjust=False。
* prev_close = close.shift(1)（首行 NaN）；amplitude = (high - low)/prev_close*100（百分数，首行 NaN）。
* 若输入 pct_chg 为 NaN，用 (close/prev_close - 1)*100 补算（首行无昨收，仍为 NaN）；
  已有的 pct_chg 原样保留。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import atomic
from . import constants as C
from .paths import Paths

INDICATOR_COLUMNS = [
    "prev_close", "white", "yellow", "bbi", "ma60",
    "k", "d", "j", "dif", "dea", "macd", "amplitude",
]
OUTPUT_COLUMNS = list(C.KLINE_COLUMNS) + INDICATOR_COLUMNS


def _ma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """输入标准 K 线 DataFrame，返回副本：标准列 + INDICATOR_COLUMNS。不修改入参。"""
    missing = [c for c in C.KLINE_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"K 线缺少标准列: {missing}")

    out = df[C.KLINE_COLUMNS].copy().reset_index(drop=True)
    close = out["close"].astype(float)
    high = out["high"].astype(float)
    low = out["low"].astype(float)

    prev_close = close.shift(1)
    out["prev_close"] = prev_close

    # pct_chg 缺失补算
    pct_calc = (close / prev_close - 1.0) * 100.0
    out["pct_chg"] = out["pct_chg"].astype(float).where(out["pct_chg"].notna(), pct_calc)

    # 白线 / 黄线 / BBI / MA60
    out["white"] = close.ewm(span=10, adjust=False).mean().ewm(span=10, adjust=False).mean()
    out["yellow"] = (_ma(close, 14) + _ma(close, 28) + _ma(close, 57) + _ma(close, 114)) / 4.0
    out["bbi"] = (_ma(close, 3) + _ma(close, 6) + _ma(close, 12) + _ma(close, 24)) / 4.0
    out["ma60"] = _ma(close, 60)

    # KDJ(9,3,3) 通达信口径
    llv = low.rolling(9, min_periods=1).min()
    hhv = high.rolling(9, min_periods=1).max()
    denom = hhv - llv
    rsv = ((close - llv) / denom.where(denom != 0) * 100.0).where(denom != 0, 50.0)
    # 初值 50：在序列前拼一个 50，ewm(adjust=False) 的首值即为 50，再丢掉
    if len(rsv):
        k = pd.concat([pd.Series([50.0]), rsv.reset_index(drop=True)], ignore_index=True) \
            .ewm(alpha=1.0 / 3.0, adjust=False).mean().iloc[1:].reset_index(drop=True)
        d = pd.concat([pd.Series([50.0]), k], ignore_index=True) \
            .ewm(alpha=1.0 / 3.0, adjust=False).mean().iloc[1:].reset_index(drop=True)
    else:
        k = pd.Series([], dtype=float)
        d = pd.Series([], dtype=float)
    out["k"] = k.to_numpy()
    out["d"] = d.to_numpy()
    out["j"] = 3.0 * out["k"] - 2.0 * out["d"]

    # MACD(12,26,9)
    dif = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    dea = dif.ewm(span=9, adjust=False).mean()
    out["dif"] = dif
    out["dea"] = dea
    out["macd"] = 2.0 * (dif - dea)

    out["amplitude"] = (high - low) / prev_close * 100.0
    return out[OUTPUT_COLUMNS]


def _cache_valid(cached: pd.DataFrame, df: pd.DataFrame) -> bool:
    if list(cached.columns) != OUTPUT_COLUMNS:
        return False
    if len(cached) != len(df) or len(df) == 0:
        return False
    if str(cached["date"].iloc[-1]) != str(df["date"].iloc[-1]):
        return False
    # 末日期相同不代表数据相同：盘中那根会被收盘数据覆盖（同日期、同行数、不同数值）。比对价量数值，不一致则重算。
    cols = ["open", "close", "high", "low", "volume"]
    return bool(np.allclose(cached[cols].to_numpy(float), df[cols].to_numpy(float), rtol=1e-9, atol=0.0, equal_nan=True))


def compute_and_cache(paths: Paths, code: str, df: pd.DataFrame) -> pd.DataFrame:
    """计算指标并缓存到 paths.indicators(code)。

    复用判定：缓存存在、列与契约一致、行数相同且末日期相同 -> 直接返回缓存（不重算）；
    否则（有新数据 / 缓存损坏 / 列变化）全量重算并原子覆盖。EMA 类指标依赖整段历史，
    因此不做增量续算，全量重算对单只 250 行开销可忽略。
    """
    path = paths.indicators(code)
    if path.exists():
        try:
            cached = pd.read_parquet(path)
        except Exception:
            cached = None
        if cached is not None and _cache_valid(cached, df):
            return cached.reset_index(drop=True)
    result = compute_indicators(df)
    atomic.write_parquet(path, result)
    return result
