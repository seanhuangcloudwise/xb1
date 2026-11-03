"""signals.py 单测。

全部用合成 K 线 + 本地指标 helper（tests/helpers_signals.py），与 indicators.py 解耦。
合成价格路径：稳步上升（每日 +0.6%，量 1e7 股，约 2.5 亿成交额/日）→ 缩量阴跌 → J 超跌，
是“上升趋势后缩量回调、贴近白线”的典型 B1。
"""
from __future__ import annotations

import copy
import json
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers_signals import (  # noqa: E402
    build, load_cfg, standard_b1_series, uptrend,
)
from xb1lib import signals  # noqa: E402
from xb1lib.constants import (  # noqa: E402
    LEVEL_CONFIRM, LEVEL_NONE, LEVEL_PERFECT, LEVEL_STANDARD, LEVEL_WATCH,
    WARN_BELOW_BBI_2D, WARN_BULL_ROPE_BREAK,
)
from xb1lib.models import StockMeta  # noqa: E402

CONTRACT_KEYS = {
    "code", "name", "board", "date", "close", "pct_chg", "amplitude", "j", "k", "d", "white",
    "yellow", "bbi", "gates", "gates_pass", "trigger", "q", "score", "base_level", "level",
    "b1_date", "hits", "deductions", "stop_price", "stop_pct", "stop_wide", "trend", "warnings",
    "data_ok", "note",
}
B1_LEVELS = (LEVEL_PERFECT, LEVEL_STANDARD)

MAIN = StockMeta.make("600000", "主板测试")
GEM = StockMeta.make("300001", "创业板测试")
STAR = StockMeta.make("688001", "科创板测试")


# --------------------------------------------------------------------------- 夹具/小工具
@pytest.fixture()
def cfg():
    return copy.deepcopy(load_cfg())


@lru_cache(maxsize=None)
def _cached(key: str) -> pd.DataFrame:
    p, v = standard_b1_series()
    if key == "base":
        return build(p, v)
    raise KeyError(key)


def base_df() -> pd.DataFrame:
    """156 行；最后一行(索引 155)为完美 B1，得分 100。"""
    return _cached("base").copy()


def setrow(df, i, **kw):
    for k, v in kw.items():
        df.iloc[i, df.columns.get_loc(k)] = v
    return df


def ev(df, cfg, meta=MAIN, at=None):
    return signals.evaluate(df, meta, cfg, at=at)


def confirm_df(n: int, bounce=2.5, confirm_pct=3.5, confirm_vol=1.2e7):
    """完美 B1（索引 155）之后 n 天出现确认阳线。n-1 个“小阳反弹日”(+2.5%，低量)把 J 抬离超卖区，
    使它们本身不构成 B1，从而 B1 日与确认日恰好相隔 n 个交易日。"""
    p, v = standard_b1_series()
    p = p + [bounce] * (n - 1) + [confirm_pct]
    v = v + [0.5e7] * (n - 1) + [confirm_vol]
    return build(p, v)


def assert_native(o, path="$"):
    """结果必须只含 Python 原生类型。"""
    if isinstance(o, dict):
        for k, v in o.items():
            assert isinstance(k, str), path
            assert_native(v, f"{path}.{k}")
    elif isinstance(o, list):
        for i, v in enumerate(o):
            assert_native(v, f"{path}[{i}]")
    else:
        assert o is None or type(o) in (str, int, float, bool), f"{path}: {type(o)}"
        if isinstance(o, float):
            assert np.isfinite(o), path


# --------------------------------------------------------------------------- 基础与契约
class TestContract:
    def test_keys_and_base_case(self, cfg):
        r = ev(base_df(), cfg)
        assert set(r) == CONTRACT_KEYS
        assert set(r["gates"]) == {"G1", "G2", "G3", "G4"}
        assert set(r["q"]) == {f"Q{i}" for i in range(1, 8)}
        assert r["data_ok"] is True and r["note"] == ""
        assert r["gates_pass"] is True and r["trigger"] is True
        assert r["score"] == 100 and r["base_level"] == LEVEL_PERFECT and r["level"] == LEVEL_PERFECT
        assert r["b1_date"] is None
        assert r["trend"] == "bull"
        assert r["code"] == "600000" and r["board"] == "main"
        assert all(isinstance(s, str) for s in r["hits"] + r["deductions"])

    def test_json_dumps(self, cfg):
        for df in (base_df(), confirm_df(1), base_df().iloc[:100]):
            r = ev(df, cfg)
            assert_native(r)
            json.dumps(r, ensure_ascii=False, allow_nan=False)
        rs = signals.scan_history(confirm_df(3), MAIN, cfg)
        assert_native(rs)
        json.dumps(rs, ensure_ascii=False, allow_nan=False)

    def test_nan_becomes_none(self, cfg):
        df = base_df()
        setrow(df, -1, j=np.nan)
        r = ev(df, cfg)
        assert r["data_ok"] is False and r["level"] == LEVEL_NONE
        assert r["j"] is None
        json.dumps(r, allow_nan=False)


# --------------------------------------------------------------------------- 数据不足
class TestInsufficientData:
    def test_113_rows(self, cfg):
        df = base_df().iloc[:113]
        r = ev(df, cfg)
        assert r["data_ok"] is False and r["level"] == LEVEL_NONE and r["base_level"] == LEVEL_NONE
        assert "114" in r["note"]
        assert r["stop_price"] is None and r["warnings"] == []
        assert set(r) == CONTRACT_KEYS
        json.dumps(r, allow_nan=False)

    def test_114_rows_ok(self, cfg):
        df = base_df().iloc[:114]
        r = ev(df, cfg)
        assert r["data_ok"] is True

    def test_at_before_114(self, cfg):
        df = base_df()
        assert ev(df, cfg, at=112)["data_ok"] is False
        assert ev(df, cfg, at=113)["data_ok"] is True

    def test_empty_and_missing_columns(self, cfg):
        assert ev(base_df().iloc[:0], cfg)["data_ok"] is False
        r = ev(base_df().drop(columns=["white"]), cfg)
        assert r["data_ok"] is False and "white" in r["note"]


# --------------------------------------------------------------------------- 闸门
class TestGates:
    def test_g1(self, cfg):
        df = base_df()
        y = df["yellow"].iloc[-1]
        setrow(df, -1, white=y)  # 相等：严格大于才算
        r = ev(df, cfg)
        assert r["gates"]["G1"] is False and r["level"] == LEVEL_NONE and r["base_level"] == LEVEL_NONE
        setrow(df, -1, white=y + 0.001)
        assert ev(df, cfg)["gates"]["G1"] is True

    @pytest.mark.parametrize("k,ok", [(0, True), (1, True), (2, True), (3, False)])
    def test_g2_consecutive_below_yellow(self, cfg, k, ok):
        # 当日及此前连续收盘<黄线的天数 ≤2 允许
        df = base_df()
        for j in range(1, k + 1):
            setrow(df, -j, close=df["yellow"].iloc[-j] - 0.01)
        r = ev(df, cfg)
        assert r["gates"]["G2"] is ok
        if not ok:
            assert r["level"] == LEVEL_NONE and r["gates_pass"] is False
        else:
            assert r["level"] != LEVEL_NONE

    def test_g2_only_current_streak_counts(self, cfg):
        # 今日跌破 1 天；昨日在黄线上；更早又有 3 天跌破：连续天数=1，通过
        df = base_df()
        for j in (1, 3, 4, 5):
            setrow(df, -j, close=df["yellow"].iloc[-j] - 0.01)
        assert ev(df, cfg)["gates"]["G2"] is True

    def test_g2_threshold_from_cfg(self, cfg):
        df = base_df()
        setrow(df, -1, close=df["yellow"].iloc[-1] - 0.01)
        assert ev(df, cfg)["gates"]["G2"] is True
        cfg["thresholds"]["g2_max_below_yellow_days"] = 0
        assert ev(df, cfg)["gates"]["G2"] is False

    def test_g3_strict_increase_over_lookback(self, cfg):
        df = base_df()
        setrow(df, -1, ma60=df["ma60"].iloc[-6])  # 与 5 日前相等：不算上升
        assert ev(df, cfg)["gates"]["G3"] is False
        setrow(df, -1, ma60=df["ma60"].iloc[-6] + 1e-4)
        assert ev(df, cfg)["gates"]["G3"] is True

    def test_g3_lookback_from_cfg(self, cfg):
        df = base_df()
        # 仅 1 日前的 MA60 更高 -> lookback=1 不通过，lookback=5 通过
        setrow(df, -2, ma60=df["ma60"].iloc[-1] + 1.0)
        assert ev(df, cfg)["gates"]["G3"] is True
        cfg["thresholds"]["g3_ma60_lookback"] = 1
        assert ev(df, cfg)["gates"]["G3"] is False

    def test_g4_threshold_and_window(self, cfg):
        thr = cfg["thresholds"]["g4_min_amount"]
        df = base_df()
        df.iloc[-14:, df.columns.get_loc("amount")] = 1e8
        setrow(df, -14, amount=thr)  # 窗口最老一天，恰好等于阈值
        assert ev(df, cfg)["gates"]["G4"] is True
        setrow(df, -14, amount=thr - 1)
        assert ev(df, cfg)["gates"]["G4"] is False
        # 达标那天在窗口外(第 15 天)不算
        setrow(df, -14, amount=1e8)
        setrow(df, -15, amount=1e9)
        assert ev(df, cfg)["gates"]["G4"] is False

    def test_g4_fail_means_no_signal_even_if_oversold(self, cfg):
        df = base_df()
        df["amount"] = 1e7
        r = ev(df, cfg)
        assert r["trigger"] is True and r["gates"]["G4"] is False
        assert r["level"] == LEVEL_NONE and r["stop_price"] is None


# --------------------------------------------------------------------------- T1 触发 / 观察
class TestTrigger:
    def test_j_13_hits(self, cfg):
        df = setrow(base_df(), -1, j=13.0)
        r = ev(df, cfg)
        assert r["trigger"] is True and r["base_level"] in B1_LEVELS

    def test_j_13_01_misses(self, cfg):
        df = setrow(base_df(), -1, j=13.01)
        r = ev(df, cfg)
        assert r["trigger"] is False
        assert r["level"] == LEVEL_WATCH  # 过闸门且 13<J≤20 -> 观察
        assert r["stop_price"] is None and r["stop_pct"] is None and r["stop_wide"] is False

    def test_watch_upper_bound(self, cfg):
        assert ev(setrow(base_df(), -1, j=20.0), cfg)["level"] == LEVEL_WATCH
        assert ev(setrow(base_df(), -1, j=20.01), cfg)["level"] == LEVEL_NONE

    def test_watch_requires_gates(self, cfg):
        df = setrow(base_df(), -1, j=16.0)
        setrow(df, -1, white=df["yellow"].iloc[-1] - 1)
        assert ev(df, cfg)["level"] == LEVEL_NONE

    def test_threshold_from_cfg(self, cfg):
        df = setrow(base_df(), -1, j=15.0)
        assert ev(df, cfg)["trigger"] is False
        cfg["thresholds"]["j_trigger"] = 15
        assert ev(df, cfg)["trigger"] is True


# --------------------------------------------------------------------------- Q1–Q7
class TestQuality:
    def test_q1_boundary(self, cfg):
        assert ev(setrow(base_df(), -1, j=-5.01), cfg)["q"]["Q1"] == 20
        assert ev(setrow(base_df(), -1, j=-5.0), cfg)["q"]["Q1"] == 0  # 严格小于

    def test_q2_tiers(self, cfg):
        # 前 20 日（不含当日）最大量 = 3e7（放量大阳线，位于窗口最老一天）
        mx = 3e7

        def q2(ratio):
            return ev(setrow(base_df(), -1, volume=ratio * mx), cfg)["q"]["Q2"]
        assert q2(0.30) == 20
        assert q2(0.31) == 10
        assert q2(0.50) == 10
        assert q2(0.51) == 0
        assert q2(1.60) == 0  # 放量

    def test_q2_window_is_20_days_excluding_today(self, cfg):
        # 把 3e7 的放量日从窗口内(索引 135)移到窗口外(134)：最大量变 1e7，当日 3.5e6 -> 0.35 -> 10 分
        df = base_df()
        setrow(df, 135, volume=1e7)
        setrow(df, 134, volume=3e7)
        setrow(df, -1, volume=3.5e6)
        assert ev(df, cfg)["q"]["Q2"] == 10
        setrow(df, 135, volume=3e7)  # 回到窗口内 -> 0.117 -> 20
        assert ev(df, cfg)["q"]["Q2"] == 20

    def test_q3_boundary(self, cfg):
        for v, exp in ((1.99, 15), (-1.99, 15), (2.0, 0), (-2.0, 0), (0.0, 15)):
            assert ev(setrow(base_df(), -1, pct_chg=v), cfg)["q"]["Q3"] == exp, v

    def test_q3_uses_prev_close_when_pct_missing(self, cfg):
        df = base_df()
        setrow(df, -1, pct_chg=np.nan, close=df["prev_close"].iloc[-1] * 1.015)
        r = ev(df, cfg)
        assert r["q"]["Q3"] == 15 and abs(r["pct_chg"] - 1.5) < 1e-6

    @pytest.mark.parametrize("meta,amp,exp", [
        (MAIN, 3.99, 15), (MAIN, 4.0, 0),
        (GEM, 6.99, 15), (GEM, 7.0, 0), (GEM, 5.0, 15),
        (STAR, 6.99, 15), (STAR, 7.0, 0),
    ])
    def test_q4_by_board(self, cfg, meta, amp, exp):
        assert ev(setrow(base_df(), -1, amplitude=amp), cfg, meta=meta)["q"]["Q4"] == exp

    def test_q5_near_white(self, cfg):
        df = base_df()
        w = df["white"].iloc[-1]
        edge = w * (1 - 3.0 / 100.0)
        assert ev(setrow(df, -1, close=edge), cfg)["q"]["Q5"] == 10        # 恰好 -3%
        assert ev(setrow(df, -1, close=edge - 0.01), cfg)["q"]["Q5"] == 0   # 低于白线超过 3%
        assert ev(setrow(df, -1, close=w * 1.05), cfg)["q"]["Q5"] == 10     # 在白线之上

    def _bear(self, df, r, *, pct=-4.0, vol=2e7, hi_mult=None):
        """在行 r 写入一根阴线：开盘=昨收，收盘按 pct 计，量 vol(前 5 日均量=1e7)。"""
        op = df["close"].iloc[r - 1]
        cl = op * (1 + pct / 100)
        setrow(df, r, open=op, close=cl, pct_chg=pct, volume=vol)
        if hi_mult is not None:
            setrow(df, r, high=cl * hi_mult)
        return df

    def test_q6_heavy_bear_in_window(self, cfg):
        assert ev(base_df(), cfg)["q"]["Q6"] == 10
        df = self._bear(base_df(), 146)  # i-9：窗口内最老一天
        assert ev(df, cfg)["q"]["Q6"] == 0
        df = self._bear(base_df(), 145)  # i-10：窗口外
        assert ev(df, cfg)["q"]["Q6"] == 10

    def test_q6_each_condition_is_needed(self, cfg):
        assert ev(self._bear(base_df(), 150, pct=-3.99), cfg)["q"]["Q6"] == 10   # 跌幅不够
        assert ev(self._bear(base_df(), 150, vol=1.99e7), cfg)["q"]["Q6"] == 10  # 量不够(<2×前5日均量)
        assert ev(self._bear(base_df(), 150, hi_mult=1.25), cfg)["q"]["Q6"] == 10  # 收盘仅为 60 日高点的 80%
        df = self._bear(base_df(), 150)
        setrow(df, 150, open=df["close"].iloc[150] * 0.99)  # 收盘>开盘：不是阴线
        assert ev(df, cfg)["q"]["Q6"] == 10
        assert ev(self._bear(base_df(), 150), cfg)["q"]["Q6"] == 0

    def test_q6_avg_excludes_bear_day_itself(self, cfg):
        # 前 5 日均量不含当日：若含当日，2e7 的均量会被抬高而漏判。此处 2e7 / 前 5 日均 1e7 = 2.0 倍 -> 命中
        assert ev(self._bear(base_df(), 150, vol=2e7), cfg)["q"]["Q6"] == 0

    def test_q7_rise_segment_with_volume(self, cfg):
        assert ev(base_df(), cfg)["q"]["Q7"] == 10
        # 同样的涨幅但没有放量 -> 0
        p, v = uptrend(150, g=0.6, vol=1e7, surge_at=15, surge_vol=1e7, surge_pct=4.0)
        p += [-1.0] * 6
        v += [0.5e7] * 6
        assert ev(build(p, v), cfg)["q"]["Q7"] == 0

    @pytest.mark.parametrize("surge_pct,surge_at,exp", [
        (16.0, 10, 10),   # 一日 +16% 放量 -> ≥15%
        (13.0, 10, 0),    # 一日 +13%（其余日横盘）不到 15%
        (16.0, 29, 10),   # 在 30 日窗口最老可用位置
        (16.0, 31, 0),    # 放量上涨日在窗口之外
    ])
    def test_q7_boundaries(self, cfg, surge_pct, surge_at, exp):
        p, v = uptrend(150, g=0.0, vol=1e7, surge_at=surge_at, surge_vol=3e7, surge_pct=surge_pct)
        assert ev(build(p, v), cfg)["q"]["Q7"] == exp


# --------------------------------------------------------------------------- 分级
class TestGrading:
    def test_grade_function_boundaries(self, cfg):
        t = cfg["thresholds"]
        g = signals._grade
        assert g(75, 20, 20, t) == LEVEL_PERFECT
        assert g(74, 20, 20, t) == LEVEL_STANDARD
        assert g(100, 0, 20, t) == LEVEL_STANDARD   # Q1 缺
        assert g(100, 20, 0, t) == LEVEL_STANDARD   # Q2 缺
        assert g(100, 0, 0, t) == LEVEL_STANDARD
        assert g(50, 0, 10, t) == LEVEL_STANDARD
        assert g(49, 0, 10, t) == LEVEL_NONE

    def test_score_75_perfect_70_standard(self, cfg):
        # Q3、Q5 失分 -> 100-15-10=75 -> 完美（Q1/Q2 仍满分）
        df = base_df()
        setrow(df, -1, pct_chg=-2.5, close=df["white"].iloc[-1] * 0.9)
        r = ev(df, cfg)
        assert r["score"] == 75 and r["level"] == LEVEL_PERFECT
        # Q3、Q4 失分 -> 70 -> 标准
        df = setrow(base_df(), -1, pct_chg=-2.5, amplitude=5.0)
        r = ev(df, cfg)
        assert r["score"] == 70 and r["level"] == LEVEL_STANDARD

    def test_boundary_via_cfg(self, cfg):
        df = base_df()  # 得分 100
        cfg["thresholds"]["perfect_min_score"] = 100
        assert ev(df, cfg)["level"] == LEVEL_PERFECT
        cfg["thresholds"]["perfect_min_score"] = 101
        assert ev(df, cfg)["level"] == LEVEL_STANDARD
        cfg["thresholds"]["standard_min_score"] = 100
        assert ev(df, cfg)["level"] == LEVEL_STANDARD
        cfg["thresholds"]["standard_min_score"] = 101
        assert ev(df, cfg)["level"] == LEVEL_NONE

    def test_perfect_needs_q1_and_q2(self, cfg):
        # 只缺 Q1：J=-4 -> 得分 80 ≥75，但不能完美
        r = ev(setrow(base_df(), -1, j=-4.0), cfg)
        assert (r["score"], r["q"]["Q1"], r["q"]["Q2"]) == (80, 0, 20)
        assert r["level"] == LEVEL_STANDARD
        assert any("降为标准" in s for s in r["deductions"])
        # 只缺 Q2：放量 -> 得分 80，不能完美
        r = ev(setrow(base_df(), -1, volume=2.0e7), cfg)
        assert (r["score"], r["q"]["Q1"], r["q"]["Q2"]) == (80, 20, 0)
        assert r["level"] == LEVEL_STANDARD
        # 都缺：60 -> 标准
        df = setrow(base_df(), -1, j=-4.0, volume=2.0e7)
        r = ev(df, cfg)
        assert r["score"] == 60 and r["level"] == LEVEL_STANDARD

    def test_below_standard_is_none(self, cfg):
        df = base_df()
        setrow(df, -1, j=-4.0, volume=2.0e7, pct_chg=3.0, amplitude=5.0)  # 仅 Q5/Q6/Q7 = 30
        r = ev(df, cfg)
        assert r["trigger"] and r["gates_pass"] and r["score"] == 30
        assert r["level"] == LEVEL_NONE and r["stop_price"] is None


# --------------------------------------------------------------------------- AC2：已知 B1 / 非 B1 样本
def _series(pcts, vols, **kw):
    return build(pcts, vols, **kw)


def _known_b1_samples():
    bp, bv = standard_b1_series()
    out = []
    # B1-1 完美：150 日稳步上升(含一根放量大阳线)后连续 6 日缩量阴跌，当日 -0.5%、量仅为前高的 13%，J=-15 超跌，
    #   收盘贴近白线(-1.5%)。Q1..Q7 全中，100 分。
    out.append(("perfect_pullback", _series(bp, bv), MAIN, LEVEL_PERFECT, 100))
    # B1-2 标准：同一走势提前一天（J=-4.7，仅差一点未达 J<-5，Q1 不得分），缩量仍然充分 -> 80 分但非完美。
    out.append(("standard_j_not_deep_enough", _series(bp[:154], bv[:154]), MAIN, LEVEL_STANDARD, 80))
    # B1-3 标准：回调没有缩量（量维持 1.8e7，Q2=0），J=-15 超跌 Q1 得分 -> 80 分，Q2 缺失不能完美。
    p, v = standard_b1_series()
    out.append(("standard_no_volume_shrink", _series(p[:150] + p[150:], v[:150] + [1.8e7] * 6), MAIN, LEVEL_STANDARD, 80))
    # B1-4 创业板标准：最后一日振幅 ~5.5%，主板线(4%)会失分，创业板线(7%)仍得 Q4。
    amps = [0.012] * (len(bp) - 1) + [0.05]
    out.append(("gem_wide_range_ok", _series(bp, bv, amp=amps), GEM, LEVEL_STANDARD, 80))
    # B1-5 确认：完美 B1 次日放量(+3.5%)阳线站上白线 -> confirm。
    out.append(("confirm_next_day", confirm_df(1), MAIN, LEVEL_CONFIRM, None))
    return out


def _known_non_b1_samples():
    bp, bv = standard_b1_series()
    out = []
    # N-1 白线<黄线：100 日上涨后 60 日持续阴跌，趋势已破，尽管 J 很低(6.9)也不是 B1。
    p, v = uptrend(100, g=0.8, vol=1e7)
    p += [-0.8] * 60
    v += [0.8e7] * 60
    out.append(("white_below_yellow", _series(p, v), MAIN))
    # N-2 成交额不足：同一个完美 B1 走势但成交量只有 5%（近 14 日最大成交额 ≈ 1.3e7 元 << 1.5 亿）。
    out.append(("amount_too_small", _series(bp, [x * 0.05 for x in bv]), MAIN))
    # N-3 MA60 下行：同一个完美 B1 走势，但 MA60 已走平转下（其余指标不变，隔离出 G3）。
    df = _series(bp, bv)
    n = len(df)
    df.iloc[-6:, df.columns.get_loc("ma60")] = [30.0, 29.9, 29.8, 29.7, 29.6, 29.5]
    out.append(("ma60_declining", df, MAIN))
    # N-4 J 太高：一路上涨没有回调，J≈90，只是强势股。
    p, v = uptrend(160, g=0.6, vol=1e7, surge_at=15, surge_vol=3e7, surge_pct=4.0)
    out.append(("j_too_high", _series(p, v), MAIN))
    # N-5 放量阴线：回调中出现 -5% 巨量(2.5e7，前 5 日均量 3 倍以上)阴线，当日 J=-6.5 但振幅大、
    #   量大、远离白线、Q6 失分，只有 30 分 < 50。
    p, v = uptrend(150, g=0.6, vol=1e7, surge_at=15, surge_vol=3e7, surge_pct=4.0)
    p += [-1.0, -1.0, -1.0, -5.0]
    v += [0.8e7, 0.7e7, 0.6e7, 2.5e7]
    out.append(("heavy_volume_bear", _series(p, v), MAIN))
    return out


class TestKnownSamples:
    @pytest.mark.parametrize("name,df,meta,level,score", _known_b1_samples(), ids=lambda x: x if isinstance(x, str) else "")
    def test_known_b1(self, cfg, name, df, meta, level, score):
        r = ev(df, cfg, meta=meta)
        assert r["data_ok"] is True
        assert r["level"] == level, (name, r["level"], r["score"], r["hits"], r["deductions"])
        if score is not None:
            assert r["score"] == score
        if level == LEVEL_CONFIRM:
            assert r["b1_date"] is not None and r["base_level"] != LEVEL_CONFIRM
        assert r["stop_price"] is not None and r["stop_pct"] is not None

    @pytest.mark.parametrize("name,df,meta", _known_non_b1_samples(), ids=lambda x: x if isinstance(x, str) else "")
    def test_known_non_b1(self, cfg, name, df, meta):
        r = ev(df, cfg, meta=meta)
        assert r["data_ok"] is True
        assert r["level"] == LEVEL_NONE, (name, r["level"], r["score"], r["gates"], r["j"])
        assert r["base_level"] == LEVEL_NONE
        assert r["stop_price"] is None

    def test_non_b1_reasons(self, cfg):
        by = {n: ev(df, cfg, meta=m) for n, df, m in _known_non_b1_samples()}
        assert by["white_below_yellow"]["gates"]["G1"] is False and by["white_below_yellow"]["trend"] == "broken"
        assert by["amount_too_small"]["gates"]["G4"] is False and by["amount_too_small"]["trigger"] is True
        assert by["ma60_declining"]["gates"]["G3"] is False and by["ma60_declining"]["gates_pass"] is False
        assert by["j_too_high"]["trigger"] is False and by["j_too_high"]["j"] > 20
        r = by["heavy_volume_bear"]
        assert r["gates_pass"] and r["trigger"] and r["q"]["Q6"] == 0 and r["score"] == 30

    def test_watch_sample(self, cfg):
        # 同一走势再提前 3 天：J=16.0 (13<J≤20)，过闸门 -> 观察
        bp, bv = standard_b1_series()
        r = ev(build(bp[:153], bv[:153]), cfg)
        assert r["level"] == LEVEL_WATCH and r["trigger"] is False and 13 < r["j"] <= 20


# --------------------------------------------------------------------------- F5 确认
class TestConfirm:
    @pytest.mark.parametrize("n", [1, 2, 3])
    def test_confirm_on_day_1_2_3(self, cfg, n):
        df = confirm_df(n)
        base_date = df["date"].iloc[155]
        # 构造自检：B1 日与确认日之间的反弹日本身不是 B1
        for i in range(156, 155 + n):
            assert ev(df, cfg, at=i)["base_level"] not in B1_LEVELS
        assert ev(df, cfg, at=155)["base_level"] == LEVEL_PERFECT
        r = ev(df, cfg)
        assert r["level"] == LEVEL_CONFIRM
        assert r["b1_date"] == base_date
        assert r["date"] == df["date"].iloc[155 + n]
        assert r["stop_price"] is not None

    def test_expired_on_day_4(self, cfg):
        df = confirm_df(4)
        r = ev(df, cfg)
        assert r["level"] != LEVEL_CONFIRM and r["b1_date"] is None
        assert r["stop_price"] is None  # 非 B1/确认级别
        # 回看窗口来自 cfg
        cfg["thresholds"]["confirm_lookback"] = 4
        assert ev(df, cfg)["level"] == LEVEL_CONFIRM

    def test_nearest_b1_is_b1_date(self, cfg):
        # 索引 154、155 都是完美 B1：确认日(156)取最近的 155
        df = confirm_df(1)
        assert ev(df, cfg, at=154)["base_level"] == LEVEL_PERFECT
        assert ev(df, cfg)["b1_date"] == df["date"].iloc[155]

    def test_bearish_day_not_confirmed(self, cfg):
        df = confirm_df(1)
        setrow(df, -1, open=df["close"].iloc[-1] * 1.01)  # 收阴
        r = ev(df, cfg)
        assert r["level"] != LEVEL_CONFIRM
        assert any("F5" in s and "收阴" in s for s in r["deductions"])

    def test_below_white_and_bbi_not_confirmed(self, cfg):
        # 小阳线放量(量能条件满足、涨幅 0.5%)，但收盘仍在白线、BBI 之下
        bp, bv = standard_b1_series()
        df = build(bp + [0.5], bv + [1.2e7])
        r = ev(df, cfg)
        assert df["close"].iloc[-1] < df["white"].iloc[-1] and df["close"].iloc[-1] < df["bbi"].iloc[-1]
        assert r["level"] != LEVEL_CONFIRM
        assert any("未站上白线/BBI" in s for s in r["deductions"])

    def test_above_white_or_bbi(self, cfg):
        df = confirm_df(1)
        c = df["close"].iloc[-1]
        setrow(df, -1, white=c * 1.05)  # 在 BBI 之上、白线之下 -> 仍确认
        assert ev(df, cfg)["level"] == LEVEL_CONFIRM
        setrow(df, -1, bbi=c * 1.05)    # 两者都在上方 -> 不确认
        assert ev(df, cfg)["level"] != LEVEL_CONFIRM

    def test_pct_threshold(self, cfg):
        assert ev(confirm_df(1, confirm_pct=3.0, confirm_vol=0.5e7), cfg)["level"] == LEVEL_CONFIRM
        assert ev(confirm_df(1, confirm_pct=2.99, confirm_vol=0.5e7), cfg)["level"] != LEVEL_CONFIRM

    def test_volume_threshold_alternative(self, cfg):
        df = confirm_df(1, confirm_pct=3.5)
        setrow(df, -1, pct_chg=1.0)  # 涨幅不够，靠量
        avg5 = float(df["volume"].iloc[-6:-1].mean())  # 前 5 日均量，不含当日
        setrow(df, -1, volume=avg5 * 1.5 * 1.0001)
        assert ev(df, cfg)["level"] == LEVEL_CONFIRM
        setrow(df, -1, volume=avg5 * 1.5 * 0.9999)
        assert ev(df, cfg)["level"] != LEVEL_CONFIRM

    def test_watch_in_lookback_is_not_enough(self, cfg):
        bp, bv = standard_b1_series()
        df = build(bp[:153] + [3.5], bv[:153] + [1.2e7])  # 前一日仅为“观察”
        assert ev(df, cfg, at=152)["level"] == LEVEL_WATCH
        assert ev(df, cfg)["level"] != LEVEL_CONFIRM

    def test_confirm_stop_uses_b1_day_low(self, cfg):
        df = confirm_df(2)
        r = ev(df, cfg)
        assert r["level"] == LEVEL_CONFIRM
        b = 155
        expect = max(df["low"].iloc[b] * 0.99, df["low"].iloc[b - 20:b].min())
        assert r["stop_price"] == round(expect, 2)
        today_based = max(df["low"].iloc[-1] * 0.99, df["low"].iloc[-21:-1].min())
        assert round(today_based, 2) != r["stop_price"]
        close = df["close"].iloc[-1]
        assert r["stop_pct"] == round((close - expect) / close * 100, 2)


# --------------------------------------------------------------------------- F6 止损
class TestStop:
    def test_perfect_stop_formula(self, cfg):
        df = base_df()
        r = ev(df, cfg)
        expect = max(df["low"].iloc[-1] * 0.99, df["low"].iloc[-21:-1].min())
        assert r["stop_price"] == round(expect, 2)
        c = df["close"].iloc[-1]
        assert r["stop_pct"] == round((c - expect) / c * 100, 2)
        assert r["stop_wide"] is False

    def test_stop_buffer_from_cfg(self, cfg):
        df = base_df()
        cfg["thresholds"]["stop_buffer"] = 0.5  # 此时 max() 由结构前低决定
        r = ev(df, cfg)
        assert r["stop_price"] == round(df["low"].iloc[-21:-1].min(), 2)

    def test_structure_low_wins_when_higher(self, cfg):
        df = base_df()
        lowcol = df.columns.get_loc("low")
        df.iloc[-21:-1, lowcol] = 23.0
        setrow(df, -1, low=22.4)  # 0.99×22.4=22.176 < 23.0
        assert ev(df, cfg)["stop_price"] == 23.0

    def test_structure_window_is_20_days_before_b1(self, cfg):
        df = base_df()
        lowcol = df.columns.get_loc("low")
        df.iloc[-21:-1, lowcol] = 23.0
        setrow(df, -1, low=22.4)
        setrow(df, -21, low=22.6)       # 第 20 个前日：在窗口内 -> 结构前低=22.6
        assert ev(df, cfg)["stop_price"] == 22.6
        df = base_df()
        df.iloc[-21:-1, lowcol] = 23.0
        setrow(df, -1, low=22.4)
        setrow(df, -22, low=1.0)        # 第 21 个前日 -> 不在窗口
        setrow(df, -21, low=23.0)
        assert ev(df, cfg)["stop_price"] == 23.0

    def test_stop_wide(self, cfg):
        df = base_df()
        base = ev(df, cfg)
        assert base["stop_wide"] is False
        cfg["thresholds"]["stop_wide_pct"] = base["stop_pct"] - 0.01
        r = ev(df, cfg)
        assert r["stop_wide"] is True and any("止损过宽" in s for s in r["deductions"])

    def test_none_for_watch_and_none(self, cfg):
        r = ev(setrow(base_df(), -1, j=16.0), cfg)
        assert r["level"] == LEVEL_WATCH
        assert (r["stop_price"], r["stop_pct"], r["stop_wide"]) == (None, None, False)


# --------------------------------------------------------------------------- F7 趋势/预警
class TestTrendAndWarnings:
    def test_trend_bull_and_broken(self, cfg):
        df = base_df()
        assert ev(df, cfg)["trend"] == "bull"
        setrow(df, -1, close=df["yellow"].iloc[-1] - 0.01)  # 收盘破黄线
        assert ev(df, cfg)["trend"] == "broken"
        df = base_df()
        setrow(df, -1, white=df["yellow"].iloc[-1] - 0.01)  # 白线在黄线下
        assert ev(df, cfg)["trend"] == "broken"

    def test_close_equal_yellow_is_bull(self, cfg):
        df = base_df()
        setrow(df, -1, close=df["yellow"].iloc[-1])
        assert ev(df, cfg)["trend"] == "bull"

    def test_rope_break_synthetic(self, cfg):
        df = base_df()
        y1, y2 = df["yellow"].iloc[-2], df["yellow"].iloc[-1]
        setrow(df, -2, white=y1 + 0.01)
        setrow(df, -1, white=y2 - 0.01)
        assert WARN_BULL_ROPE_BREAK in ev(df, cfg)["warnings"]
        # 昨日已在黄线之下：不是“刚”下穿
        setrow(df, -2, white=y1 - 0.01)
        assert WARN_BULL_ROPE_BREAK not in ev(df, cfg)["warnings"]
        # 今日仍在黄线之上
        setrow(df, -2, white=y1 + 0.01)
        setrow(df, -1, white=y2 + 0.01)
        assert WARN_BULL_ROPE_BREAK not in ev(df, cfg)["warnings"]

    def test_rope_break_on_natural_downtrend(self, cfg):
        p, v = uptrend(100, g=0.8, vol=1e7)
        p += [-0.8] * 60
        v += [0.8e7] * 60
        df = build(p, v)
        w, y = df["white"].to_numpy(), df["yellow"].to_numpy()
        cross = [i for i in range(115, len(df)) if w[i - 1] >= y[i - 1] and w[i] < y[i]]
        assert len(cross) == 1
        i = cross[0]
        assert WARN_BULL_ROPE_BREAK in ev(df, cfg, at=i)["warnings"]
        assert WARN_BULL_ROPE_BREAK not in ev(df, cfg, at=i - 1)["warnings"]
        assert WARN_BULL_ROPE_BREAK not in ev(df, cfg, at=i + 1)["warnings"]
        assert ev(df, cfg, at=i)["trend"] == "broken"

    def test_below_bbi_2d_synthetic(self, cfg):
        df = base_df()
        for k in (1, 2):
            setrow(df, -k, close=df["bbi"].iloc[-k] - 0.01)
        assert WARN_BELOW_BBI_2D in ev(df, cfg)["warnings"]
        # 昨日在 BBI 之上：只破 1 天，不预警
        setrow(df, -2, close=df["bbi"].iloc[-2] + 0.01)
        assert WARN_BELOW_BBI_2D not in ev(df, cfg)["warnings"]
        # 今日在 BBI 之上
        setrow(df, -2, close=df["bbi"].iloc[-2] - 0.01)
        setrow(df, -1, close=df["bbi"].iloc[-1] + 0.01)
        assert WARN_BELOW_BBI_2D not in ev(df, cfg)["warnings"]

    def test_below_bbi_2d_on_natural_decline(self, cfg):
        df = base_df()  # 末三日收盘均低于 BBI
        c, b = df["close"].to_numpy(), df["bbi"].to_numpy()
        first = next(i for i in range(120, len(df)) if c[i] < b[i] and c[i - 1] >= b[i - 1])
        assert WARN_BELOW_BBI_2D not in ev(df, cfg, at=first)["warnings"]
        assert WARN_BELOW_BBI_2D in ev(df, cfg, at=first + 1)["warnings"]

    def test_no_warnings_in_healthy_uptrend(self, cfg):
        p, v = uptrend(160, g=0.6)
        assert ev(build(p, v), cfg)["warnings"] == []


# --------------------------------------------------------------------------- 回放与 scan_history
class TestReplay:
    def test_truncation_equivalence(self, cfg):
        df = confirm_df(3)  # 159 行，含 perfect / watch / confirm 多种状态
        for k in range(113, len(df)):
            full = ev(df, cfg, at=k)
            trunc = ev(df.iloc[: k + 1].copy(), cfg)
            assert full == trunc, (k, full["date"])

    def test_future_rows_never_used(self, cfg):
        df = confirm_df(3)
        k = 156
        ref = ev(df, cfg, at=k)
        bad = df.copy()
        num = [c for c in bad.columns if c != "date"]
        rng = np.random.default_rng(0)
        bad.iloc[k + 1:, [bad.columns.get_loc(c) for c in num]] = rng.uniform(1, 1e9, (len(bad) - k - 1, len(num)))
        assert ev(bad, cfg, at=k) == ref

    def test_at_defaults_to_last_and_negative_index(self, cfg):
        df = base_df()
        assert ev(df, cfg) == ev(df, cfg, at=len(df) - 1) == ev(df, cfg, at=-1)
        with pytest.raises(IndexError):
            ev(df, cfg, at=len(df))

    def test_scan_history(self, cfg):
        df = confirm_df(2)
        rs = signals.scan_history(df, MAIN, cfg)
        assert len(rs) == len(df) - 113
        assert rs[0]["date"] == df["date"].iloc[113]
        for off, r in enumerate(rs):
            assert r == ev(df, cfg, at=113 + off)
        levels = [r["level"] for r in rs]
        assert LEVEL_PERFECT in levels and LEVEL_CONFIRM in levels and LEVEL_WATCH in levels
        # start：行号 / 日期
        assert len(signals.scan_history(df, MAIN, cfg, start=150)) == len(df) - 150
        d = df["date"].iloc[150]
        assert signals.scan_history(df, MAIN, cfg, start=d)[0]["date"] == d
        # 早于第 114 行的 start：返回 data_ok=False 的行
        early = signals.scan_history(df, MAIN, cfg, start=100)
        assert early[0]["data_ok"] is False and early[-1]["data_ok"] is True
        assert signals.scan_history(df.iloc[:50], MAIN, cfg) == []
