"""history.py 测试：追加去重、backfill T+N 手算对账、未到期留空。"""
from __future__ import annotations

import pandas as pd
import pytest

from helpers_render import make_cfg, make_result, make_run
from xb1lib import constants as C
from xb1lib import history
from xb1lib.paths import Paths


@pytest.fixture
def paths(tmp_path):
    return Paths(home=tmp_path, check=False)


def _run(date, specs):
    """specs: [(code, level, close)]"""
    run = make_run(date=date)
    run["results"] = [make_result(c, f"股{c}", lv, close=cl, date=date, score=60, j=10.0) for c, lv, cl in specs]
    return run


def _write_kline(paths, code, dates, closes):
    paths.kline_dir.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame({"date": dates, "open": closes, "close": closes, "high": closes, "low": closes,
                       "volume": 1.0, "amount": 1.0, "turnover": 1.0, "pct_chg": 0.0})
    df.to_parquet(paths.kline(code), index=False)


def test_append_history_only_signal_levels(paths):
    run = _run("2026-11-03", [("600001", C.LEVEL_STANDARD, 10.0), ("600002", C.LEVEL_NONE, 5.0),
                              ("600003", C.LEVEL_WATCH, 7.0), ("600004", C.LEVEL_CONFIRM, 8.0)])
    assert history.append_history(paths, run) == 3
    df = pd.read_parquet(paths.signals_history)
    assert sorted(df["code"]) == ["600001", "600003", "600004"]
    for col in ("date", "code", "name", "level", "score", "j", "close", "stop_price"):
        assert col in df.columns
    row = df[df["code"] == "600001"].iloc[0]
    assert row["close"] == 10.0 and row["stop_price"] == 9.5 and row["level"] == "standard"


def test_append_same_day_rerun_no_duplicates(paths):
    run = _run("2026-11-03", [("600001", C.LEVEL_STANDARD, 10.0), ("600002", C.LEVEL_WATCH, 7.0)])
    history.append_history(paths, run)
    history.append_history(paths, run)
    assert len(pd.read_parquet(paths.signals_history)) == 2
    # 同日结果变化：覆盖而非追加
    run2 = _run("2026-11-03", [("600001", C.LEVEL_PERFECT, 10.5)])
    history.append_history(paths, run2)
    df = pd.read_parquet(paths.signals_history)
    assert len(df) == 1 and df.iloc[0]["level"] == "perfect" and df.iloc[0]["close"] == 10.5


def test_append_multi_days_and_sorted(paths):
    history.append_history(paths, _run("2026-11-03", [("600001", C.LEVEL_STANDARD, 10.0)]))
    history.append_history(paths, _run("2026-11-02", [("600001", C.LEVEL_WATCH, 9.0)]))
    df = pd.read_parquet(paths.signals_history)
    assert list(df["date"]) == ["2026-11-02", "2026-11-03"]


def test_append_empty_signals_creates_file_and_clears_day(paths):
    history.append_history(paths, _run("2026-11-03", [("600001", C.LEVEL_STANDARD, 10.0)]))
    assert history.append_history(paths, _run("2026-11-03", [("600001", C.LEVEL_NONE, 10.0)])) == 0
    assert len(pd.read_parquet(paths.signals_history)) == 0
    # 空历史上追加空
    p2 = Paths(home=paths.home / "x", check=False)
    history.append_history(p2, _run("2026-11-03", []))
    assert p2.signals_history.exists()


def test_append_leaves_no_tmp(paths):
    history.append_history(paths, _run("2026-11-03", [("600001", C.LEVEL_STANDARD, 10.0)]))
    assert [p for p in paths.history.iterdir() if p.name.endswith(".tmp")] == []


DATES = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02",
         "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10"]


def test_backfill_hand_computed(paths):
    # 600001：信号日 10-27 收盘 10.0；之后收盘序列 10-28..：见下
    closes = [9.0, 10.0, 10.5, 9.0, 11.0, 12.0, 10.0, 8.0, 10.0, 11.0, 9.0, 12.5]
    _write_kline(paths, "600001", DATES, closes)
    # 600002：信号日 11-02 收盘 12.0 -> T+3 = 11-05(10.0)，T+5 = 11-09 (9.0)，T+10 未到期
    # 600003：信号日 11-09（倒数第 2 日）-> 仅 T+1 可得，T+3/5/10 均留空
    _write_kline(paths, "600002", DATES, closes)
    _write_kline(paths, "600003", DATES, closes)
    history.append_history(paths, _run("2026-10-27", [("600001", C.LEVEL_STANDARD, 10.0)]))
    history.append_history(paths, _run("2026-11-02", [("600002", C.LEVEL_PERFECT, 12.0)]))
    history.append_history(paths, _run("2026-11-09", [("600003", C.LEVEL_WATCH, 9.0)]))

    s = history.backfill(paths, make_cfg())
    out = pd.read_parquet(paths.outcomes_history).set_index("code")

    # 600001: base=close[10-27]=10.0；T+3=10-30 -> 11.0 => +10%；T+5=11-03 -> 10.0 => 0%；T+10=11-10 -> 12.5 ... 索引：
    # dates idx 1 -> +3 = idx4 (10-30)=11.0, +5 = idx6 (11-03)=10.0, +10 = idx11 (11-10)=12.5
    r1 = out.loc["600001"]
    assert r1["base_close"] == 10.0
    assert r1["date_t3"] == "2026-10-30" and r1["ret_t3"] == pytest.approx(10.0)
    assert r1["date_t5"] == "2026-11-03" and r1["ret_t5"] == pytest.approx(0.0)
    assert r1["date_t10"] == "2026-11-10" and r1["ret_t10"] == pytest.approx(25.0)
    # 600002: idx5 base=12.0；+3=idx8 (11-05)=10.0 => -16.666..%；+5=idx10 (11-09)=9.0 => -25%；+10 未到期
    r2 = out.loc["600002"]
    assert r2["ret_t3"] == pytest.approx((10.0 / 12.0 - 1) * 100)
    assert r2["ret_t5"] == pytest.approx(-25.0)
    assert pd.isna(r2["ret_t10"]) and r2["date_t10"] is None
    # 600003: idx10，后面仅 1 根
    r3 = out.loc["600003"]
    assert pd.isna(r3["ret_t3"]) and pd.isna(r3["ret_t5"]) and pd.isna(r3["ret_t10"])

    lv = s["levels"]
    assert s["total"] == 3 and s["days"] == [3, 5, 10]
    assert lv["standard"]["T+3"] == {"n": 1, "win_rate": 1.0, "avg_return": pytest.approx(10.0)}
    assert lv["standard"]["T+5"]["win_rate"] == 0.0           # 收益=0 不算胜
    assert lv["standard"]["T+10"]["avg_return"] == pytest.approx(25.0)
    assert lv["perfect"]["T+3"]["win_rate"] == 0.0 and lv["perfect"]["T+10"] == {"n": 0, "win_rate": None, "avg_return": None}
    assert lv["watch"]["signals"] == 1 and lv["watch"]["T+3"]["n"] == 0
    assert lv["confirm"]["signals"] == 0


def test_backfill_uses_kline_close_as_base_not_history_close(paths):
    closes = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
    d = DATES[:6]
    _write_kline(paths, "600001", d, closes)
    # history 里 close 故意写成错值，应以 raw/kline 收盘为准
    history.append_history(paths, _run(d[0], [("600001", C.LEVEL_STANDARD, 99.0)]))
    s = history.backfill(paths, make_cfg(), calendar_days=(3, 5))
    assert s["levels"]["standard"]["T+3"]["avg_return"] == pytest.approx(30.0)
    assert s["levels"]["standard"]["T+5"]["avg_return"] == pytest.approx(50.0)


def test_backfill_groups_and_win_rate(paths):
    d = DATES[:8]
    up = [10, 10, 10, 11, 11, 11, 11, 12]
    down = [10, 10, 10, 9, 9, 9, 9, 8]
    _write_kline(paths, "600001", d, up)
    _write_kline(paths, "600002", d, down)
    _write_kline(paths, "600003", d, up)
    history.append_history(paths, _run(d[0], [("600001", C.LEVEL_STANDARD, 10), ("600002", C.LEVEL_STANDARD, 10),
                                              ("600003", C.LEVEL_STANDARD, 10)]))
    s = history.backfill(paths, make_cfg(), calendar_days=(3,))
    t3 = s["levels"]["standard"]["T+3"]
    assert t3["n"] == 3 and t3["win_rate"] == pytest.approx(2 / 3) and t3["avg_return"] == pytest.approx((10 - 10 + 10) / 3)


def test_backfill_missing_kline_or_date(paths):
    _write_kline(paths, "600002", DATES[:3], [10, 10, 10])   # 不含信号日
    history.append_history(paths, _run(DATES[5], [("600001", C.LEVEL_STANDARD, 10), ("600002", C.LEVEL_WATCH, 10)]))
    s = history.backfill(paths, make_cfg())
    out = pd.read_parquet(paths.outcomes_history)
    assert len(out) == 2 and out["ret_t3"].isna().all()
    assert s["levels"]["standard"]["T+3"]["n"] == 0


def test_backfill_no_history(paths):
    s = history.backfill(paths, make_cfg())
    assert s["total"] == 0 and paths.outcomes_history.exists()
    assert all(s["levels"][lv]["signals"] == 0 for lv in ("confirm", "perfect", "standard", "watch"))


def test_backfill_idempotent_and_format(paths):
    _write_kline(paths, "600001", DATES[:8], [10, 10, 10, 11, 11, 11, 11, 12])
    history.append_history(paths, _run(DATES[0], [("600001", C.LEVEL_PERFECT, 10)]))
    a = history.backfill(paths, make_cfg())
    b = history.backfill(paths, make_cfg())
    assert a == b
    text = history.format_summary(a)
    assert "完美B1 | 1 | 1 / 100.0% / 10.00%" in text
    assert "T+10 样本/胜率/均收益" in text
