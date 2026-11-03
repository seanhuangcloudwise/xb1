"""indicators 单测：独立逐行递推对账 + 契约检查 + 缓存复用。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from xb1lib import constants as C
from xb1lib import indicators as ind
from xb1lib.paths import Paths

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TOL = 1e-9


def _load_maker():
    spec = importlib.util.spec_from_file_location("make_klines", FIXTURES / "make_klines.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def df_a() -> pd.DataFrame:
    csv = FIXTURES / "synthetic_a.csv"
    if not csv.exists():
        _load_maker().main()
    return pd.read_csv(csv)


@pytest.fixture()
def paths(tmp_path, monkeypatch) -> Paths:
    monkeypatch.delenv("XB1_HOME", raising=False)
    p = Paths(tmp_path, check=False)
    p.ensure_dirs()
    return p


# ---------------- 独立手写参考实现（不使用 ewm/rolling） ----------------
def ref_ema(values, span):
    a = 2.0 / (span + 1)
    out, prev = [], None
    for v in values:
        prev = v if prev is None else a * v + (1 - a) * prev
        out.append(prev)
    return out


def ref_kdj(high, low, close):
    n = len(close)
    K, D, J = [], [], []
    k_prev = d_prev = 50.0
    for i in range(n):
        lo = min(low[max(0, i - 8): i + 1])
        hi = max(high[max(0, i - 8): i + 1])
        rsv = 50.0 if hi == lo else (close[i] - lo) / (hi - lo) * 100.0
        k = (rsv + 2.0 * k_prev) / 3.0          # SMA(X,3,1) = (1*X + 2*Y')/3
        d = (k + 2.0 * d_prev) / 3.0
        K.append(k); D.append(d); J.append(3 * k - 2 * d)
        k_prev, d_prev = k, d
    return K, D, J


def ref_ma(values, n, i):
    return sum(values[i - n + 1: i + 1]) / n


def maxdiff(a, b):
    return float(np.max(np.abs(np.asarray(a, float) - np.asarray(b, float))))


# ---------------- 对账 ----------------
def test_kdj_matches_manual_recursion(df_a):
    out = ind.compute_indicators(df_a)
    K, D, J = ref_kdj(df_a["high"].tolist(), df_a["low"].tolist(), df_a["close"].tolist())
    assert maxdiff(out["k"], K) <= TOL
    assert maxdiff(out["d"], D) <= TOL
    assert maxdiff(out["j"], J) <= TOL


def test_kdj_first_row_uses_initial_50(df_a):
    out = ind.compute_indicators(df_a)
    r = df_a.iloc[0]
    rsv0 = 50.0 if r.high == r.low else (r.close - r.low) / (r.high - r.low) * 100
    assert out["k"].iloc[0] == pytest.approx((rsv0 + 100.0) / 3.0, abs=TOL)
    assert out["d"].iloc[0] == pytest.approx((out["k"].iloc[0] + 100.0) / 3.0, abs=TOL)


def test_kdj_zero_denominator_rsv_50():
    n = 12
    df = pd.DataFrame({
        "date": pd.bdate_range("2025-01-01", periods=n).strftime("%Y-%m-%d"),
        "open": 10.0, "close": 10.0, "high": 10.0, "low": 10.0,
        "volume": 1e6, "amount": 1e7, "turnover": 1.0, "pct_chg": 0.0,
    })
    out = ind.compute_indicators(df)
    assert np.allclose(out["k"], 50.0, atol=TOL)
    assert np.allclose(out["d"], 50.0, atol=TOL)
    assert np.allclose(out["j"], 50.0, atol=TOL)


def test_white_line_matches_manual(df_a):
    out = ind.compute_indicators(df_a)
    ref = ref_ema(ref_ema(df_a["close"].tolist(), 10), 10)
    assert maxdiff(out["white"], ref) <= TOL


def test_macd_matches_manual(df_a):
    out = ind.compute_indicators(df_a)
    c = df_a["close"].tolist()
    e12, e26 = ref_ema(c, 12), ref_ema(c, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = ref_ema(dif, 9)
    macd = [2 * (a - b) for a, b in zip(dif, dea)]
    assert maxdiff(out["dif"], dif) <= TOL
    assert maxdiff(out["dea"], dea) <= TOL
    assert maxdiff(out["macd"], macd) <= TOL


def test_ma_family_matches_manual(df_a):
    out = ind.compute_indicators(df_a)
    c = df_a["close"].tolist()
    for i in (113, 150, 250, len(c) - 1):
        yellow = sum(ref_ma(c, n, i) for n in (14, 28, 57, 114)) / 4
        assert out["yellow"].iloc[i] == pytest.approx(yellow, abs=TOL)
    for i in (23, 100, len(c) - 1):
        bbi = sum(ref_ma(c, n, i) for n in (3, 6, 12, 24)) / 4
        assert out["bbi"].iloc[i] == pytest.approx(bbi, abs=TOL)
    for i in (59, 200):
        assert out["ma60"].iloc[i] == pytest.approx(ref_ma(c, 60, i), abs=TOL)


# ---------------- 契约 ----------------
def test_columns_and_order(df_a):
    out = ind.compute_indicators(df_a)
    expected = C.KLINE_COLUMNS + ["prev_close", "white", "yellow", "bbi", "ma60",
                                  "k", "d", "j", "dif", "dea", "macd", "amplitude"]
    assert list(out.columns) == expected
    assert len(out) == len(df_a)
    assert (out["date"].values == df_a["date"].values).all()


def test_nan_when_window_insufficient(df_a):
    out = ind.compute_indicators(df_a)
    assert out["yellow"].iloc[:113].isna().all() and out["yellow"].iloc[113:].notna().all()
    assert out["bbi"].iloc[:23].isna().all() and out["bbi"].iloc[23:].notna().all()
    assert out["ma60"].iloc[:59].isna().all() and out["ma60"].iloc[59:].notna().all()
    # EMA / KDJ / MACD 从第 1 行起有值
    for col in ("white", "k", "d", "j", "dif", "dea", "macd"):
        assert out[col].notna().all(), col


def test_does_not_mutate_input(df_a):
    before = df_a.copy(deep=True)
    df_in = df_a.copy(deep=True)
    df_in.loc[5, "pct_chg"] = np.nan
    snapshot = df_in.copy(deep=True)
    out = ind.compute_indicators(df_in)
    pd.testing.assert_frame_equal(df_in, snapshot)
    assert out is not df_in
    pd.testing.assert_frame_equal(df_a, before)


def test_amplitude_and_prev_close(df_a):
    out = ind.compute_indicators(df_a)
    assert np.isnan(out["prev_close"].iloc[0]) and np.isnan(out["amplitude"].iloc[0])
    assert (out["prev_close"].iloc[1:].values == df_a["close"].iloc[:-1].values).all()
    exp = (df_a["high"].iloc[1:].values - df_a["low"].iloc[1:].values) \
        / df_a["close"].iloc[:-1].values * 100
    assert np.allclose(out["amplitude"].iloc[1:].values, exp, atol=1e-12)


def test_pct_chg_filled_when_missing(df_a):
    df_in = df_a.copy()
    df_in.loc[10:12, "pct_chg"] = np.nan
    df_in.loc[0, "pct_chg"] = np.nan
    out = ind.compute_indicators(df_in)
    for i in (10, 11, 12):
        exp = (df_a["close"].iloc[i] / df_a["close"].iloc[i - 1] - 1) * 100
        assert out["pct_chg"].iloc[i] == pytest.approx(exp, abs=1e-9)
    assert np.isnan(out["pct_chg"].iloc[0])          # 首行无昨收，无法补算
    # 未缺失的保持原值
    assert out["pct_chg"].iloc[20] == df_a["pct_chg"].iloc[20]


def test_missing_standard_column_raises(df_a):
    with pytest.raises(ValueError):
        ind.compute_indicators(df_a.drop(columns=["amount"]))


def test_fixture_properties(df_a):
    assert len(df_a) >= 300
    assert list(df_a.columns) == C.KLINE_COLUMNS
    assert df_a["date"].is_monotonic_increasing and df_a["date"].is_unique
    assert (df_a["high"] >= df_a[["open", "close"]].max(axis=1)).all()
    assert (df_a["low"] <= df_a[["open", "close"]].min(axis=1)).all()
    # 含超跌段（J 曾 < 13）与放量段（量 > 20 日均量 2 倍）
    out = ind.compute_indicators(df_a)
    assert (out["j"] < 13).any()
    assert (df_a["volume"] > 2 * df_a["volume"].rolling(20).mean().shift(1)).any()


def test_fixture_deterministic(df_a):
    mk = _load_maker()
    pd.testing.assert_frame_equal(mk.make_a(), mk.make_a())
    reread = mk.make_a().round(6)
    pd.testing.assert_frame_equal(reread, df_a.round(6), check_dtype=False)


# ---------------- 缓存 ----------------
def test_cache_written_and_reused(paths, df_a, monkeypatch):
    out1 = ind.compute_and_cache(paths, "000001", df_a)
    f = paths.indicators("000001")
    assert f.exists()
    pd.testing.assert_frame_equal(out1, ind.compute_indicators(df_a), check_dtype=False)

    calls = []
    real = ind.compute_indicators
    monkeypatch.setattr(ind, "compute_indicators", lambda d: calls.append(1) or real(d))
    mtime = f.stat().st_mtime_ns
    out2 = ind.compute_and_cache(paths, "000001", df_a)
    assert calls == []                      # 末日期相同：不重算
    assert f.stat().st_mtime_ns == mtime    # 也不重写
    pd.testing.assert_frame_equal(out2, out1)


def test_cache_updates_on_new_data(paths, df_a):
    base = df_a.iloc[:300].reset_index(drop=True)
    out1 = ind.compute_and_cache(paths, "600000", base)
    assert out1["date"].iloc[-1] == base["date"].iloc[-1]
    out2 = ind.compute_and_cache(paths, "600000", df_a)
    assert len(out2) == len(df_a)
    assert out2["date"].iloc[-1] == df_a["date"].iloc[-1]
    pd.testing.assert_frame_equal(out2, ind.compute_indicators(df_a), check_dtype=False)
    cached = pd.read_parquet(paths.indicators("600000"))
    assert cached["date"].iloc[-1] == df_a["date"].iloc[-1]


def test_cache_recomputed_when_corrupt_or_stale_columns(paths, df_a):
    f = paths.indicators("300001")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"not a parquet")
    out = ind.compute_and_cache(paths, "300001", df_a)
    pd.testing.assert_frame_equal(out, ind.compute_indicators(df_a), check_dtype=False)
    # 旧版缓存列缺失 -> 重算
    pd.read_parquet(f).drop(columns=["macd"]).to_parquet(f, index=False)
    out = ind.compute_and_cache(paths, "300001", df_a)
    assert list(out.columns) == ind.OUTPUT_COLUMNS
