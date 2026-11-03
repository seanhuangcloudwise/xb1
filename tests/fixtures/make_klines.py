"""确定性合成 K 线生成器（固定随机种子）。其他模块的测试可直接复用 CSV 或 generate()。

用法：python3 tests/fixtures/make_klines.py   # 重新生成 synthetic_a.csv / synthetic_b.csv

synthetic_a（320 行）：温和上行 -> 放量上涨段（~行 90-104）-> 高位横盘 -> 连续下跌超跌段（~行 190-230）
                      -> 缩量企稳 -> 反弹。
synthetic_b（260 行）：另一条路径（不同种子，整体震荡偏弱，含放量段），便于多股票测试。
列为标准 K 线列：date, open, close, high, low, volume, amount(元), turnover(%), pct_chg(%)。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

COLUMNS = ["date", "open", "close", "high", "low", "volume", "amount", "turnover", "pct_chg"]
HERE = Path(__file__).resolve().parent


def generate(seed: int = 20261103, n: int = 320, start: str = "2024-01-02",
             start_price: float = 10.0, segments=None) -> pd.DataFrame:
    """segments: [(起始行, 结束行(不含), 日均漂移%, 波动%, 量倍数)]；默认见 synthetic_a 说明。"""
    rng = np.random.default_rng(seed)
    if segments is None:
        segments = [
            (0, 90, 0.25, 1.2, 1.0),      # 温和上行
            (90, 105, 1.6, 1.5, 2.8),     # 放量上涨段
            (105, 190, 0.0, 1.0, 1.0),    # 高位横盘
            (190, 230, -2.0, 1.6, 1.3),   # 连续下跌（超跌）
            (230, 260, 0.0, 0.5, 0.35),   # 缩量企稳
            (260, n, 0.5, 1.2, 1.0),      # 反弹
        ]
    drift = np.zeros(n)
    vol = np.ones(n)
    volmul = np.ones(n)
    for s, e, dr, sg, vm in segments:
        e = min(e, n)
        drift[s:e] = dr
        vol[s:e] = sg
        volmul[s:e] = vm
    ret = rng.normal(drift, vol) / 100.0
    ret = np.clip(ret, -0.095, 0.095)

    dates = pd.bdate_range(start, periods=n).strftime("%Y-%m-%d")
    close = np.empty(n)
    prev = start_price
    for i in range(n):
        prev = prev * (1.0 + ret[i])
        close[i] = prev
    close = np.round(np.maximum(close, 1.0), 2)
    prev_close = np.concatenate([[start_price], close[:-1]])
    open_ = np.round(prev_close * (1.0 + rng.normal(0, 0.004, n)), 2)
    hi_extra = np.abs(rng.normal(0.004, 0.004, n))
    lo_extra = np.abs(rng.normal(0.004, 0.004, n))
    high = np.round(np.maximum(open_, close) * (1.0 + hi_extra), 2)
    low = np.round(np.minimum(open_, close) * (1.0 - lo_extra), 2)
    high = np.maximum(high, np.maximum(open_, close))
    low = np.minimum(low, np.minimum(open_, close))

    base_vol = 8.0e6 * volmul * rng.uniform(0.8, 1.2, n)
    base_vol = base_vol * (1.0 + 4.0 * np.abs(ret))   # 波动大量也大
    volume = np.round(base_vol / 100.0) * 100.0
    amount = np.round(volume * (high + low + open_ + close) / 4.0, 2)
    turnover = np.round(volume / 6.0e8 * 100.0, 2)
    pct_chg = np.round((close / prev_close - 1.0) * 100.0, 2)

    return pd.DataFrame({
        "date": dates, "open": open_, "close": close, "high": high, "low": low,
        "volume": volume, "amount": amount, "turnover": turnover, "pct_chg": pct_chg,
    })[COLUMNS]


def make_a() -> pd.DataFrame:
    return generate(seed=20261103, n=320)


def make_b() -> pd.DataFrame:
    segs = [
        (0, 60, -0.1, 1.4, 1.0),
        (60, 75, 1.2, 1.8, 2.5),
        (75, 150, -0.15, 1.1, 0.9),
        (150, 180, -1.8, 1.5, 1.2),
        (180, 260, 0.1, 0.9, 0.7),
    ]
    return generate(seed=7, n=260, start="2024-03-01", start_price=25.0, segments=segs)


def main() -> None:
    make_a().to_csv(HERE / "synthetic_a.csv", index=False)
    make_b().to_csv(HERE / "synthetic_b.csv", index=False)
    print("written:", HERE / "synthetic_a.csv", HERE / "synthetic_b.csv")


if __name__ == "__main__":
    main()
