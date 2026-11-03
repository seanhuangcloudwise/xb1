"""B1 信号判定（PRD 4.4–4.7，接口见 docs/DEV_CONTRACTS.md「signals.py」）。

本模块只消费 K 线标准列 + 指标列
``prev_close, white, yellow, bbi, ma60, k, d, j, dif, dea, macd, amplitude``，
不依赖 indicators.py 本身。所有阈值取自 ``cfg['thresholds']``。

公共接口
--------
- ``evaluate(df_ind, meta, cfg, at=None) -> dict``
- ``scan_history(df_ind, meta, cfg, start=None) -> list[dict]``

只使用 ``[0..at]`` 行的数据（回放不泄露未来）；结果仅含 Python 原生类型，NaN -> None，可直接 json.dumps。
"""
from __future__ import annotations

import math
from typing import Any, Optional

import numpy as np
import pandas as pd

from .constants import (
    LEVEL_CONFIRM,
    LEVEL_NONE,
    LEVEL_PERFECT,
    LEVEL_STANDARD,
    LEVEL_WATCH,
    WARN_BELOW_BBI_2D,
    WARN_BULL_ROPE_BREAK,
)
from .models import StockMeta

MIN_ROWS = 114  # 黄线需要 MA114，第 114 行起才有值

_NUM_COLS = [
    "open", "close", "high", "low", "volume", "amount", "pct_chg",
    "prev_close", "white", "yellow", "bbi", "ma60", "k", "d", "j", "amplitude",
]
_REQUIRED = ["date"] + _NUM_COLS


# --------------------------------------------------------------------------- 内部工具
class _Arr:
    """把 DataFrame 一次性转成 numpy 数组，避免逐行取值的开销。"""

    def __init__(self, df: pd.DataFrame):
        self.n = len(df)
        self.date = [str(x) for x in df["date"].tolist()]
        for c in _NUM_COLS:
            setattr(self, c, pd.to_numeric(df[c], errors="coerce").to_numpy(dtype=float))
        # 涨跌幅：优先 pct_chg 列，缺失时用 close/prev_close 补
        pct = self.pct_chg.copy()
        with np.errstate(divide="ignore", invalid="ignore"):
            alt = (self.close / self.prev_close - 1.0) * 100.0
        bad = ~np.isfinite(pct)
        pct[bad] = alt[bad]
        pct[~np.isfinite(pct)] = np.nan
        self.pct = pct


def _ok(x: float) -> bool:
    return x is not None and np.isfinite(x)


def _f(x: Any, nd: int = 4) -> Optional[float]:
    """numpy/浮点 -> Python float（四位小数）；NaN/None -> None。"""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return round(v, nd)


def _win(a: np.ndarray, i: int, n: int) -> np.ndarray:
    """以 i 结尾、长度 n 的窗口（含 i；不足则取已有部分）。"""
    return a[max(0, i - n + 1): i + 1]


def _prior(a: np.ndarray, i: int, n: int) -> np.ndarray:
    """i 之前（不含 i）的 n 个值。"""
    return a[max(0, i - n): i]


def _row_valid(a: _Arr, i: int) -> bool:
    if i < 0 or i + 1 < MIN_ROWS or i >= a.n:
        return False
    keys = (a.close[i], a.open[i], a.high[i], a.low[i], a.volume[i], a.white[i],
            a.yellow[i], a.bbi[i], a.ma60[i], a.j[i], a.amplitude[i], a.pct[i])
    return all(_ok(v) for v in keys)


def _grade(score: int, q1: int, q2: int, t: dict) -> str:
    """已过闸门且已触发 T1 时的分级。完美需 ≥perfect_min_score 且 Q1、Q2 均得分>0。"""
    if score >= t["perfect_min_score"] and q1 > 0 and q2 > 0:
        return LEVEL_PERFECT
    if score >= t["standard_min_score"]:
        return LEVEL_STANDARD
    return LEVEL_NONE


# --------------------------------------------------------------------------- 闸门 / 评分
def _gates(a: _Arr, t: dict, i: int) -> tuple[dict, list[str], list[str]]:
    hits: list[str] = []
    ded: list[str] = []
    g: dict[str, bool] = {}

    # G1 白线 > 黄线
    g["G1"] = bool(a.white[i] > a.yellow[i])
    (hits if g["G1"] else ded).append("G1 白线>黄线" if g["G1"] else "G1 白线未在黄线之上")

    # G2 收盘 >= 黄线，允许连续跌破 <= g2_max_below_yellow_days 天
    below = 0
    k = i
    while k >= 0 and _ok(a.yellow[k]) and _ok(a.close[k]) and a.close[k] < a.yellow[k]:
        below += 1
        k -= 1
    g["G2"] = below <= t["g2_max_below_yellow_days"]
    if below == 0:
        hits.append("G2 收盘≥黄线")
    elif g["G2"]:
        hits.append(f"G2 连续跌破黄线{below}天(允许≤{t['g2_max_below_yellow_days']})")
    else:
        ded.append(f"G2 连续跌破黄线{below}天(>{t['g2_max_below_yellow_days']})")

    # G3 MA60 较 N 日前上升
    lb = int(t["g3_ma60_lookback"])
    g["G3"] = bool(i - lb >= 0 and _ok(a.ma60[i - lb]) and a.ma60[i] > a.ma60[i - lb])
    (hits if g["G3"] else ded).append(
        f"G3 MA60较{lb}日前上升" if g["G3"] else f"G3 MA60较{lb}日前未上升")

    # G4 近 N 日最大成交额 >= 阈值
    w = _win(a.amount, i, int(t["g4_amount_window"]))
    mx = float(np.nanmax(w)) if w.size and np.isfinite(w).any() else float("nan")
    g["G4"] = bool(_ok(mx) and mx >= t["g4_min_amount"])
    yi = t["g4_min_amount"] / 1e8
    (hits if g["G4"] else ded).append(
        f"G4 近{t['g4_amount_window']}日最大成交额≥{yi:g}亿" if g["G4"]
        else f"G4 近{t['g4_amount_window']}日最大成交额不足{yi:g}亿")
    return g, hits, ded


def _is_heavy_bear(a: _Arr, t: dict, k: int) -> bool:
    """高位放量阴线：收阴、跌幅>=q6_drop_pct、量>=前 N 日均量(不含当日)×倍数、收盘在近 60 日高点 85% 以上。"""
    n = int(t["q6_vol_avg_days"])
    if k < n or not (_ok(a.close[k]) and _ok(a.open[k]) and _ok(a.pct[k])):
        return False
    if not (a.close[k] < a.open[k] and a.pct[k] <= -t["q6_drop_pct"]):
        return False
    avg = float(np.nanmean(_prior(a.volume, k, n)))
    if not (_ok(avg) and avg > 0 and a.volume[k] >= avg * t["q6_vol_mult"]):
        return False
    hh = float(np.nanmax(_win(a.high, k, int(t["q6_high_window"]))))
    return bool(a.close[k] >= hh * t["q6_high_ratio"])


def _has_vol_rise_segment(a: _Arr, t: dict, i: int) -> bool:
    """近 q7_window 日内存在 [s, e]（s<e）使 close[e]/close[s]-1 >= q7_rise_pct，
    且 (s, e] 内至少有一个放量上涨日（收涨、量>=前 q6_vol_avg_days 日均量×q6_vol_mult）。"""
    w = int(t["q7_window"])
    lo = max(0, i - w + 1)
    n = int(t["q6_vol_avg_days"])
    # 放量上涨日标记
    surge = np.zeros(i + 1, dtype=bool)
    for k in range(lo, i + 1):
        if k >= 1 and k >= n and _ok(a.pct[k]) and a.pct[k] > 0:
            avg = float(np.nanmean(_prior(a.volume, k, n)))
            if _ok(avg) and avg > 0 and a.volume[k] >= avg * t["q6_vol_mult"]:
                surge[k] = True
    need = t["q7_rise_pct"] / 100.0
    for s in range(lo, i):
        cs = a.close[s]
        if not (_ok(cs) and cs > 0):
            continue
        for e in range(s + 1, i + 1):
            if _ok(a.close[e]) and a.close[e] / cs - 1.0 >= need and surge[s + 1: e + 1].any():
                return True
    return False


def _quality(a: _Arr, meta: StockMeta, t: dict, i: int) -> tuple[dict, list[str], list[str]]:
    q: dict[str, int] = {}
    hits: list[str] = []
    ded: list[str] = []

    # Q1 J < -5 -> 20
    j = a.j[i]
    q["Q1"] = 20 if j < t["q1_j_below"] else 0
    (hits if q["Q1"] else ded).append(
        f"Q1 J={j:.1f}<{t['q1_j_below']:g}(+20)" if q["Q1"] else f"Q1 J={j:.1f}未<{t['q1_j_below']:g}")

    # Q2 缩量：当日量 vs 前 N 日(不含当日)最大量
    mxv = float(np.nanmax(_prior(a.volume, i, int(t["q2_vol_window"])))) if i >= 1 else float("nan")
    ratio = a.volume[i] / mxv if _ok(mxv) and mxv > 0 else float("nan")
    if _ok(ratio) and ratio <= t["q2_ratio_third"]:
        q["Q2"] = 20
    elif _ok(ratio) and ratio <= t["q2_ratio_half"]:
        q["Q2"] = 10
    else:
        q["Q2"] = 0
    if q["Q2"]:
        hits.append(f"Q2 缩量至前{t['q2_vol_window']}日最大量的{ratio * 100:.0f}%(+{q['Q2']})")
    else:
        ded.append("Q2 未缩量" + (f"(量比{ratio * 100:.0f}%)" if _ok(ratio) else ""))

    # Q3 |涨跌幅| < 2%
    pct = a.pct[i]
    q["Q3"] = 15 if abs(pct) < t["q3_abs_pct"] else 0
    (hits if q["Q3"] else ded).append(
        f"Q3 涨跌幅{pct:+.2f}%波动小(+15)" if q["Q3"] else f"Q3 涨跌幅{pct:+.2f}%过大")

    # Q4 振幅（主板 / 创业板科创板）
    lim = t["q4_amp_main"] if meta.board == "main" else t["q4_amp_growth"]
    amp = a.amplitude[i]
    q["Q4"] = 15 if amp < lim else 0
    (hits if q["Q4"] else ded).append(
        f"Q4 振幅{amp:.2f}%<{lim:g}%(+15)" if q["Q4"] else f"Q4 振幅{amp:.2f}%≥{lim:g}%")

    # Q5 收盘贴近白线（<=3%）或在其上
    near = a.close[i] >= a.white[i] * (1.0 - t["q5_near_white_pct"] / 100.0)
    q["Q5"] = 10 if near else 0
    dist = (a.close[i] / a.white[i] - 1.0) * 100.0
    (hits if near else ded).append(
        f"Q5 收盘距白线{dist:+.1f}%(+10)" if near else f"Q5 收盘低于白线{abs(dist):.1f}%")

    # Q6 近 N 日无高位放量阴线
    w = int(t["q6_window"])
    bad = [a.date[k] for k in range(max(0, i - w + 1), i + 1) if _is_heavy_bear(a, t, k)]
    q["Q6"] = 0 if bad else 10
    (ded if bad else hits).append(
        f"Q6 近{w}日有高位放量阴线({bad[-1]})" if bad else f"Q6 近{w}日无高位放量阴线(+10)")

    # Q7 近 N 日有放量上涨段
    has = _has_vol_rise_segment(a, t, i)
    q["Q7"] = 10 if has else 0
    (hits if has else ded).append(
        f"Q7 近{t['q7_window']}日有放量上涨段≥{t['q7_rise_pct']:g}%(+10)" if has
        else f"Q7 近{t['q7_window']}日无放量上涨段")
    return q, hits, ded


def _eval_b1(a: _Arr, meta: StockMeta, t: dict, i: int) -> dict:
    """第 i 行当日的 B1 判定（F4），不含 F5 确认。要求 _row_valid(a, i)。"""
    gates, gh, gd = _gates(a, t, i)
    q, qh, qd = _quality(a, meta, t, i)
    score = int(sum(q.values()))
    gates_pass = all(gates.values())
    j = a.j[i]
    trigger = bool(j <= t["j_trigger"])
    hits = list(gh)
    ded = list(gd)
    if trigger:
        hits.append(f"T1 J={j:.1f}≤{t['j_trigger']:g}")
    else:
        ded.append(f"T1 J={j:.1f}>{t['j_trigger']:g}")
    hits += qh
    ded += qd

    base = LEVEL_NONE
    if gates_pass and trigger:
        base = _grade(score, q["Q1"], q["Q2"], t)
        if base == LEVEL_NONE:
            ded.append(f"得分{score}<{t['standard_min_score']}，不构成B1")
        elif base == LEVEL_STANDARD and score >= t["perfect_min_score"]:
            ded.append(f"得分{score}≥{t['perfect_min_score']}但Q1/Q2未同时得分，降为标准B1")
    elif gates_pass and t["j_trigger"] < j <= t["j_watch_max"]:
        base = LEVEL_WATCH
        hits.append(f"观察：{t['j_trigger']:g}<J={j:.1f}≤{t['j_watch_max']:g}")
    return dict(gates=gates, gates_pass=gates_pass, trigger=trigger, q=q, score=score,
                base_level=base, hits=hits, deductions=ded)


def _base_level_at(a: _Arr, meta: StockMeta, t: dict, i: int) -> str:
    if not _row_valid(a, i):
        return LEVEL_NONE
    return _eval_b1(a, meta, t, i)["base_level"]


def _stop(a: _Arr, t: dict, b: int, close_now: float) -> tuple[Optional[float], Optional[float], bool]:
    """F6 止损：max(B1 当日最低×stop_buffer, 近期结构前低)；近期结构前低 = B1 当日之前 20 日内最低 low。"""
    cand = [a.low[b] * t["stop_buffer"]]
    prev = _prior(a.low, b, 20)
    prev = prev[np.isfinite(prev)]
    if prev.size:
        cand.append(float(prev.min()))
    stop = max(cand)
    if not _ok(stop) or not _ok(close_now) or close_now <= 0:
        return None, None, False
    pct = (close_now - stop) / close_now * 100.0
    return _f(stop, 2), _f(pct, 2), bool(pct > t["stop_wide_pct"])


# --------------------------------------------------------------------------- 对外接口
def _base_result(a: _Arr, meta: StockMeta, i: int) -> dict:
    have = a is not None and 0 <= i < a.n

    def at_(name):
        return _f(getattr(a, name)[i]) if have else None

    return {
        "code": meta.code, "name": meta.name, "board": meta.board,
        "date": a.date[i] if have else None,
        "close": at_("close"), "pct_chg": at_("pct"),
        "amplitude": at_("amplitude"),
        "j": at_("j"), "k": at_("k"), "d": at_("d"),
        "white": at_("white"), "yellow": at_("yellow"), "bbi": at_("bbi"),
        "gates": {"G1": False, "G2": False, "G3": False, "G4": False},
        "gates_pass": False, "trigger": False,
        "q": {f"Q{n}": 0 for n in range(1, 8)}, "score": 0,
        "base_level": LEVEL_NONE, "level": LEVEL_NONE, "b1_date": None,
        "hits": [], "deductions": [],
        "stop_price": None, "stop_pct": None, "stop_wide": False,
        "trend": "broken", "warnings": [],
        "data_ok": False, "note": "",
    }


def _evaluate_arr(a: _Arr, meta: StockMeta, t: dict, at: Optional[int]) -> dict:
    n = a.n
    if n == 0:
        res = _base_result(None, meta, -1)
        res["note"] = "无K线数据"
        return res
    i = n - 1 if at is None else int(at)
    if i < 0:
        i += n
    if i < 0 or i >= n:
        raise IndexError(f"at={at} 超出范围 [0,{n - 1}]")

    res = _base_result(a, meta, i)
    if i + 1 < MIN_ROWS:
        res["note"] = f"数据不足：截至当日仅{i + 1}行，至少需要{MIN_ROWS}行（黄线需MA114）"
        return res
    if not _row_valid(a, i):
        res["note"] = "当日关键指标缺失（NaN），无法判定"
        return res

    r = _eval_b1(a, meta, t, i)
    res.update(gates=r["gates"], gates_pass=r["gates_pass"], trigger=r["trigger"], q=r["q"],
               score=r["score"], base_level=r["base_level"], level=r["base_level"],
               hits=r["hits"], deductions=r["deductions"], data_ok=True, note="")

    # ---- F5 B1 确认 ----
    lookback = int(t["confirm_lookback"])
    b_idx = None
    for k in range(1, lookback + 1):  # 优先取最近的一次 B1
        if i - k >= 0 and _base_level_at(a, meta, t, i - k) in (LEVEL_PERFECT, LEVEL_STANDARD):
            b_idx = i - k
            break
    if b_idx is not None:
        up = a.close[i] > a.open[i]
        avg5 = float(np.nanmean(_prior(a.volume, i, int(t["q6_vol_avg_days"]))))
        by_pct = a.pct[i] >= t["confirm_min_pct"]
        by_vol = _ok(avg5) and avg5 > 0 and a.volume[i] >= avg5 * t["confirm_vol_mult"]
        above = a.close[i] > a.white[i] or a.close[i] > a.bbi[i]
        if up and (by_pct or by_vol) and above:
            res["level"] = LEVEL_CONFIRM
            res["b1_date"] = a.date[b_idx]
            res["hits"].insert(0, f"F5 B1确认：{a.date[b_idx]}出现B1，今日收阳放量/大涨且站上白线或BBI")
        else:
            why = []
            if not up:
                why.append("收阴")
            if not (by_pct or by_vol):
                why.append(f"涨幅<{t['confirm_min_pct']:g}%且量能不足")
            if not above:
                why.append("未站上白线/BBI")
            res["deductions"].append(f"F5 {a.date[b_idx]}有B1但今日未确认：" + "、".join(why))

    # ---- F6 止损 ----
    if res["level"] in (LEVEL_CONFIRM, LEVEL_PERFECT, LEVEL_STANDARD):
        b = b_idx if res["level"] == LEVEL_CONFIRM else i
        sp, spct, wide = _stop(a, t, b, a.close[i])
        res["stop_price"], res["stop_pct"], res["stop_wide"] = sp, spct, wide
        if wide:
            res["deductions"].append(f"止损幅度{spct:.1f}%>{t['stop_wide_pct']:g}%，止损过宽")

    # ---- F7 趋势与预警 ----
    res["trend"] = "bull" if (a.white[i] > a.yellow[i] and a.close[i] >= a.yellow[i]) else "broken"
    warns: list[str] = []
    if (i >= 1 and _ok(a.white[i - 1]) and _ok(a.yellow[i - 1])
            and a.white[i - 1] >= a.yellow[i - 1] and a.white[i] < a.yellow[i]):
        warns.append(WARN_BULL_ROPE_BREAK)
    if i >= 1 and _ok(a.bbi[i - 1]) and a.close[i] < a.bbi[i] and a.close[i - 1] < a.bbi[i - 1]:
        warns.append(WARN_BELOW_BBI_2D)
    res["warnings"] = warns
    return res


def _check_df(df_ind: pd.DataFrame) -> Optional[str]:
    missing = [c for c in _REQUIRED if c not in df_ind.columns]
    return f"缺少列：{','.join(missing)}" if missing else None


def evaluate(df_ind: pd.DataFrame, meta: StockMeta, cfg: dict, at: Optional[int] = None) -> dict:
    """对第 ``at`` 行（默认最后一行）做 B1 判定，返回契约规定的 dict。

    口径（PRD 有歧义处的解读）
    --------------------------
    1. 数据量：截至 ``at`` 的行数 < 114（黄线需 MA114）或当日关键指标为 NaN 时
       ``data_ok=False, level='none'``，note 说明原因。判定只使用 ``[0..at]`` 行，不看未来。
    2. G1：当日 白线 > 黄线（严格大于）。
    3. G2：从当日向前数“连续收盘 < 黄线”的天数 n（当日收盘≥黄线则 n=0）；n ≤ g2_max_below_yellow_days 即通过。
       即“收盘≥黄线，允许连续跌破不超过 2 天”，含当日本身正处于跌破第 1/2 天的情形。
    4. G3：MA60[at] > MA60[at - g3_ma60_lookback]（严格大于）。
    5. G4：含当日在内最近 g4_amount_window 日的最大成交额（元） ≥ g4_min_amount。
    6. T1：J ≤ j_trigger（13 命中，13.01 不命中）。
    7. Q1：J < q1_j_below（严格小于）得 20。
    8. Q2：当日量与“前 q2_vol_window 日（不含当日）最大量”之比 ≤0.3 得 20，≤0.5 得 10，否则 0。
    9. Q3：用当日涨跌幅（pct_chg；缺失时由 close/prev_close 补算）|x| < q3_abs_pct 得 15。
    10. Q4：用当日振幅；main 板用 q4_amp_main，gem/star/bj 用 q4_amp_growth；严格小于得 15。
    11. Q5：收盘 ≥ 白线×(1 - q5_near_white_pct%)，即在白线上或低于白线不超过 3% 得 10。
    12. Q6：含当日的近 q6_window 日中，没有“高位放量阴线”得 10。高位放量阴线 = 收盘<开盘 且
        涨跌幅 ≤ -q6_drop_pct 且 量 ≥ 前 q6_vol_avg_days 日均量（不含该日）×q6_vol_mult
        且 收盘 ≥ 含该日的近 q6_high_window 日最高价×q6_high_ratio。前 5 日不足 5 日则不算放量。
    13. Q7：含当日的近 q7_window 日内存在起点 s<终点 e，使 close[e]/close[s]-1 ≥ q7_rise_pct，
        且 (s,e] 内至少有一个放量上涨日（收涨且量 ≥ 前 5 日均量×q6_vol_mult）得 10。
        PRD 未给“放量”阈值，复用 Q6 的放量定义。
    14. 评分 score 无论闸门/触发是否通过都按当日数据计算（便于 explain）；等级仍由闸门+触发决定。
    15. 分级：过闸门且触发后，score ≥ perfect_min_score 且 Q1>0 且 Q2>0 为 perfect；
        否则 score ≥ standard_min_score 为 standard（含“分数够但 Q1/Q2 缺一”）；
        score < standard_min_score 为 none。过闸门且 j_trigger < J ≤ j_watch_max 为 watch。
    16. F5 确认：at-1..at-confirm_lookback 日中最近一个 base_level∈{perfect,standard} 的日期为 b1_date，
        且当日 收盘>开盘、(涨跌幅≥confirm_min_pct 或 量≥前 5 日均量(不含当日)×confirm_vol_mult)、
        收盘>白线 或 收盘>BBI，则 level='confirm'（优先于 base_level）。B1 后第 1/2/3 天可确认，第 4 天过期。
        确认时不再要求当日重新通过闸门。
    17. F6 止损：仅 level∈{confirm,perfect,standard} 计算。止损 = max(B1 当日最低×stop_buffer, 近期结构前低)；
        “近期结构前低” = B1 当日之前 20 个交易日（不含 B1 当日）内的最低 low。
        confirm 级别以 b1_date 当天为 B1 日；perfect/standard 以当日为 B1 日。
        参考买入价=当日收盘；stop_pct=(收盘-止损)/收盘×100；stop_pct > stop_wide_pct 则 stop_wide=True。
    18. F7：trend='bull' 当且仅当 白线>黄线 且 收盘≥黄线，否则 'broken'。
        rope_break：昨日 白线≥黄线 且 今日 白线<黄线。below_bbi_2d：今日与昨日收盘均 < 各自 BBI。
    19. 数值：浮点保留 4 位（止损价/止损幅度 2 位），NaN->None，全部为 Python 原生类型。
    """
    err = _check_df(df_ind)
    if err:
        res = _base_result(None, meta, -1)
        res["note"] = err
        return res
    a = _Arr(df_ind)
    return _evaluate_arr(a, meta, cfg["thresholds"], at)


def scan_history(df_ind: pd.DataFrame, meta: StockMeta, cfg: dict, start=None) -> list[dict]:
    """逐日 evaluate（每个结果都只使用当日及之前的数据）。

    ``start``：None 表示从第一个数据充足的行（第 114 行）开始；int 为起始行号；
    str 为起始日期 'YYYY-MM-DD'（取首个 date ≥ start 的行）。早于 114 行的行会返回 data_ok=False 的结果。
    """
    if _check_df(df_ind):
        return []
    a = _Arr(df_ind)
    t = cfg["thresholds"]
    if start is None:
        s = MIN_ROWS - 1
    elif isinstance(start, str):
        s = next((k for k, d in enumerate(a.date) if d >= start), a.n)
    else:
        s = int(start)
    s = max(0, s)
    return [_evaluate_arr(a, meta, t, i) for i in range(s, a.n)]
