"""TradeCalendar：缓存、每周刷新、查询语义、降级。"""
from __future__ import annotations

import pytest

from fakes import FakeProvider, make_env, make_trading_days
from xb1lib import atomic
from xb1lib.calendar import TradeCalendar, days_between, normalize_date
from xb1lib.errors import SourceUnavailable

HOLIDAYS = ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07"]
DAYS = make_trading_days("2026-01-01", "2026-12-31", HOLIDAYS)


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path)


def cal_for(env, today="2026-11-03", provider=None):
    paths, _ = env
    prov = provider or FakeProvider(calendar=DAYS)
    return TradeCalendar(paths, prov, today=today), prov


def test_normalize_date_variants():
    assert normalize_date("2026-1-5") == "2026-01-05"
    assert normalize_date("20260105") == "2026-01-05"
    assert normalize_date("2026/01/05") == "2026-01-05"
    assert normalize_date("2026年1月5日") == "2026-01-05"
    assert normalize_date("2026-01-05 15:00:00") == "2026-01-05"
    import datetime
    assert normalize_date(datetime.date(2026, 1, 5)) == "2026-01-05"
    with pytest.raises(ValueError):
        normalize_date("hello")
    with pytest.raises(ValueError):
        normalize_date("2026-13-40")
    assert days_between("2026-01-01", "2026-01-11") == 10


def test_first_use_fetches_and_writes_cache(env):
    paths, _ = env
    cal, prov = cal_for(env)
    assert cal.is_trading_day("2026-11-03") is True
    assert prov.calendar_calls == 1
    data = atomic.read_json(paths.calendar_file)
    assert data["fetched_at"] == "2026-11-03" and data["days"] == DAYS
    cal2, prov2 = cal_for(env)                      # 新实例读缓存，不联网
    assert cal2.is_trading_day("2026-11-03") and prov2.calendar_calls == 0


def test_is_trading_day_weekend_and_holiday(env):
    cal, _ = cal_for(env)
    assert cal.is_trading_day("2026-10-31") is False      # 周六
    assert cal.is_trading_day("2026-10-01") is False      # 假期
    assert cal.is_trading_day("2026-10-08") is True
    assert cal.is_trading_day("20261102") is True         # 接受多种日期写法


def test_latest_trading_day(env):
    cal, _ = cal_for(env)
    assert cal.latest_trading_day("2026-11-03") == "2026-11-03"
    assert cal.latest_trading_day("2026-11-01") == "2026-10-30"      # 周日 -> 上周五
    assert cal.latest_trading_day("2026-10-07") == "2026-09-30"      # 长假中 -> 节前最后一天
    with pytest.raises(ValueError):
        cal.latest_trading_day("2025-12-31")


def test_prev_trading_day(env):
    cal, _ = cal_for(env)
    assert cal.prev_trading_day("2026-11-03") == "2026-11-02"
    assert cal.prev_trading_day("2026-11-02", 1) == "2026-10-30"
    assert cal.prev_trading_day("2026-10-08", 1) == "2026-09-30"
    assert cal.prev_trading_day("2026-11-03", 5) == "2026-10-27"
    assert cal.prev_trading_day("2026-11-01", 1) == "2026-10-30"      # 非交易日也可作为起点
    with pytest.raises(ValueError):
        cal.prev_trading_day("2026-11-03", 0)
    with pytest.raises(ValueError):
        cal.prev_trading_day("2026-01-02", 5)


def test_n_trading_days_between_is_open_closed(env):
    cal, _ = cal_for(env)
    assert cal.n_trading_days_between("2026-11-02", "2026-11-03") == 1
    assert cal.n_trading_days_between("2026-11-03", "2026-11-03") == 0
    assert cal.n_trading_days_between("2026-10-30", "2026-11-03") == 2      # 跨周末
    assert cal.n_trading_days_between("2026-09-30", "2026-10-08") == 1      # 跨国庆
    assert cal.n_trading_days_between("2026-11-03", "2026-11-02") == -1
    assert cal.count_inclusive("2026-11-02", "2026-11-03") == 2
    assert cal.count_inclusive("2026-11-03", "2026-11-02") == 0


def test_weekly_refresh(env):
    paths, _ = env
    cal, prov = cal_for(env, today="2026-11-03")
    cal.is_trading_day("2026-11-03")
    assert prov.calendar_calls == 1
    c2, p2 = cal_for(env, today="2026-11-09")                         # 6 天：不刷新
    c2.is_trading_day("2026-11-03")
    assert p2.calendar_calls == 0
    c3, p3 = cal_for(env, today="2026-11-10")                         # 7 天：刷新
    c3.is_trading_day("2026-11-03")
    assert p3.calendar_calls == 1
    assert atomic.read_json(paths.calendar_file)["fetched_at"] == "2026-11-10"


def test_refresh_failure_falls_back_to_stale_cache_with_warning(env):
    cal_for(env, today="2026-11-03")[0].is_trading_day("2026-11-03")
    bad = FakeProvider(calendar=DAYS)
    bad.unavailable = True
    cal, _ = cal_for(env, today="2026-12-01", provider=bad)
    assert cal.is_trading_day("2026-11-03") is True
    assert cal.warnings and "沿用旧缓存" in cal.warnings[0]


def test_no_cache_and_provider_down_raises(env):
    bad = FakeProvider(calendar=DAYS)
    bad.unavailable = True
    cal, _ = cal_for(env, provider=bad)
    with pytest.raises(SourceUnavailable):
        cal.is_trading_day("2026-11-03")


def test_empty_provider_calendar_is_rejected(env):
    cal, _ = cal_for(env, provider=FakeProvider(calendar=[]))
    with pytest.raises(SourceUnavailable):
        cal.is_trading_day("2026-11-03")


@pytest.mark.parametrize("garbage", ["{not json", '{"days": []}', '{"fetched_at": "x", "days": ["2026-01-02"]}', "[]"])
def test_corrupt_cache_is_refetched(env, garbage):
    paths, _ = env
    paths.calendar_file.parent.mkdir(parents=True, exist_ok=True)
    paths.calendar_file.write_text(garbage, encoding="utf-8")
    cal, prov = cal_for(env)
    assert cal.is_trading_day("2026-11-03")
    assert prov.calendar_calls == 1
    assert atomic.read_json(paths.calendar_file)["days"] == DAYS


def test_query_beyond_cache_triggers_single_refresh(env):
    paths, _ = env
    old = [d for d in DAYS if d <= "2026-11-03"]
    cal_for(env, today="2026-11-03", provider=FakeProvider(calendar=old))[0].is_trading_day("2026-11-03")
    prov = FakeProvider(calendar=DAYS)                                 # 数据源已更新到年底
    cal, _ = cal_for(env, today="2026-11-05", provider=prov)           # 缓存只有 2 天龄，但要查更晚的日子
    assert cal.is_trading_day("2026-11-20") is True
    assert prov.calendar_calls == 1
    assert cal.is_trading_day("2027-06-01") is False                    # 仍超出：同一实例不再反复刷新
    assert prov.calendar_calls == 1


def test_provider_calendar_is_deduped_sorted(env):
    cal, _ = cal_for(env, provider=FakeProvider(calendar=["2026-01-06", "2026-01-05", "2026-01-05", "20260107"]))
    assert cal.days == ["2026-01-05", "2026-01-06", "2026-01-07"]
