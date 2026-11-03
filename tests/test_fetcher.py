"""fetch_klines：批大小/间隔、限流重试、增量缓存、历史回放、停牌、单只失败、缓存损坏。"""
from __future__ import annotations

import pandas as pd
import pytest

from fakes import FakeProvider, FakeSleep, make_env, make_kline_df
from xb1lib import fetcher
from xb1lib.constants import KLINE_COLUMNS
from xb1lib.errors import ParseError, RateLimited, SourceUnavailable
from xb1lib.fetcher import fetch_klines, load_cache

TD = "2026-11-03"          # 周二
PREV = "2026-11-02"


def code_list(n, start=600000):
    return [f"{start + i}" for i in range(n)]


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path)


def run(env, prov, codes, td=TD, today=None, sl=None):
    paths, cfg = env
    sl = sl or FakeSleep(prov)
    out, fails = fetch_klines(prov, paths, cfg, codes, td, sleep=sl, today=today or td)
    return out, fails, sl


def synth(codes, end=TD, days=300, **kw):
    return FakeProvider.synthetic(codes, end, days, **kw)


# ---------------------------------------------------------------- 批 / 间隔
def test_45_codes_make_3_batches_le_20_with_intervals(env):
    cs = code_list(45)
    p = synth(cs)
    out, fails, sl = run(env, p, cs)
    assert p.batch_sizes == [20, 20, 5] and max(p.batch_sizes) <= 20
    assert len(out) == 45 and fails == {}
    assert sl.calls == [1.5, 1.5]                                  # 批间隔 = cfg.fetch.batch_interval，首批前不等待
    kinds = [e[0] for e in p.events]
    assert kinds == ["kline", "sleep", "kline", "sleep", "kline"]  # 间隔恰在两次调用之间
    assert all(len(df) == 250 and list(df.columns) == KLINE_COLUMNS for df in out.values())
    assert all(d == 250 for d in p.requested_days)                 # 无缓存 -> 全量 kline_days


def test_batch_size_capped_at_20_even_if_misconfigured(env):
    paths, cfg = env
    cfg["fetch"]["batch_size"] = 50
    cs = code_list(25)
    p = synth(cs)
    fetch_klines(p, paths, cfg, cs, TD, sleep=FakeSleep(p), today=TD)
    assert p.batch_sizes == [20, 5]


def test_small_batch_size_config_respected_and_dupes_removed(env):
    paths, cfg = env
    cfg["fetch"]["batch_size"] = 7
    cs = code_list(15)
    p = synth(cs)
    out, _ = fetch_klines(p, paths, cfg, cs + cs[:3] + ["sh600000"], TD, sleep=FakeSleep(p), today=TD)
    assert p.batch_sizes == [7, 7, 1] and len(out) == 15


def test_no_sleep_for_single_batch(env):
    p = synth(code_list(3))
    _, _, sl = run(env, p, code_list(3))
    assert sl.calls == []


# ---------------------------------------------------------------- 限流
def test_rate_limited_batch_waits_then_retries_only_that_batch(env):
    cs = code_list(45)
    p = synth(cs)
    p.inject_rate_limit(times=2, when_contains=cs[20])             # 第 2 批（20..39）连续限流 2 次后恢复
    out, fails, sl = run(env, p, cs)
    assert len(out) == 45 and fails == {}
    assert sl.calls.count(30.0) == 2                                # rate_limit_wait=30 等了两次
    b = p.batches
    assert b.count(cs[20:40]) == 3                                  # 该批共尝试 3 次
    assert b.count(cs[:20]) == 1 and b.count(cs[40:]) == 1          # 其它批没被重复请求
    ev = [e[0] if e[0] == "kline" else e[1] for e in p.events]
    i = [k for k, e in enumerate(p.events) if e[0] == "kline" and e[1][0] == cs[20]][0]
    assert ev[i + 1] == 30.0                                        # 限流后立刻等 30 秒再重试同一批


def test_rate_limit_exceeding_max_retries_marks_batch_failed_continues(env):
    cs = code_list(45)
    p = synth(cs)
    p.inject_rate_limit(times=99, when_contains=cs[40])             # 最后一批（5 只）永远限流
    out, fails, sl = run(env, p, cs)
    assert set(fails) == set(cs[40:]) and len(out) == 40            # 5/45 < 50%：不抛
    assert all(v.startswith("rate_limited") for v in fails.values())
    assert p.batches.count(cs[40:]) == 3                            # max_retries=3 = 最多尝试 3 次
    assert sl.calls.count(30.0) == 2                                # 3 次尝试之间等 2 次（放弃前不再白等）


def test_rate_limited_ge_50_percent_raises_source_unavailable(env):
    cs = code_list(40)
    p = synth(cs)
    p.inject_rate_limit(times=99)                                   # 所有批都限流
    with pytest.raises(SourceUnavailable):
        run(env, p, cs)
    p2 = synth(cs)
    p2.inject_rate_limit(times=99, when_contains=cs[0])             # 恰好 1/2 批失败 = 50%
    with pytest.raises(SourceUnavailable):
        run(env, p2, cs)


def test_just_under_50_percent_does_not_raise(env):
    cs = code_list(60)                                              # 3 批，1 批失败 = 33%
    p = synth(cs)
    p.inject_rate_limit(times=99, when_contains=cs[0])
    out, fails, _ = run(env, p, cs)
    assert len(fails) == 20 and len(out) == 40


def test_source_unavailable_propagates_immediately(env):
    p = synth(code_list(5))
    p.unavailable = True
    with pytest.raises(SourceUnavailable):
        run(env, p, code_list(5))
    assert len(p.kline_calls) == 1


def test_parse_error_on_batch_marks_failed(env):
    cs = code_list(45)
    p = synth(cs)
    orig = p.get_klines

    def flaky(codes, days, fq):
        if cs[20] in codes:
            raise ParseError("garbage")
        return orig(codes, days, fq)
    p.get_klines = flaky
    out, fails, _ = run(env, p, cs)
    assert set(fails) == set(cs[20:40]) and all("parse_error" in v for v in fails.values())


# ---------------------------------------------------------------- 单只失败 / 停牌
def test_single_failure_does_not_affect_others(env):
    cs = code_list(10)
    p = synth(cs)
    p.fail_codes[cs[3]] = "failed: no such security"
    p.fail_codes[cs[4]] = "weird reason"
    out, fails, _ = run(env, p, cs)
    assert set(out) == set(cs) - {cs[3], cs[4]}
    assert fails[cs[3]] == "failed: no such security" and fails[cs[4]] == "failed: weird reason"


def test_unknown_code_is_single_failure(env):
    p = synth(code_list(3))
    out, fails, _ = run(env, p, code_list(3) + ["688999"])
    assert "688999" in fails and len(out) == 3


def test_suspended_detected_by_last_bar_and_excluded(env):
    cs = code_list(5)
    p = synth(cs)
    p.suspend(cs[2], "2026-10-28")
    out, fails, _ = run(env, p, cs)
    assert cs[2] not in out and fails[cs[2]].startswith("suspended") and "2026-10-28" in fails[cs[2]]
    assert len(out) == 4


def test_suspended_kept_when_exclude_suspended_false(env):
    paths, cfg = env
    cfg["filters"]["exclude_suspended"] = False
    cs = code_list(3)
    p = synth(cs)
    p.suspend(cs[0], "2026-10-28")
    out, fails = fetch_klines(p, paths, cfg, cs, TD, sleep=FakeSleep(p), today=TD)
    assert cs[0] in out and out[cs[0]]["date"].iloc[-1] == "2026-10-28" and fails[cs[0]].startswith("suspended")


def test_data_not_yet_published_marks_all_suspended(env):
    cs = code_list(4)
    p = synth(cs)
    p.available_until = PREV                                        # 数据源尚未出当日 K 线
    out, fails, _ = run(env, p, cs)
    assert out == {} and set(fails) == set(cs) and all(v.startswith("suspended") for v in fails.values())


def test_suspended_does_not_count_toward_source_unavailable(env):
    cs = code_list(10)
    p = synth(cs)
    p.available_until = PREV
    run(env, p, cs)                                                 # 不抛：停牌/未更新由上层按 stale_ratio 判断


# ---------------------------------------------------------------- 增量缓存
def test_cache_written_atomically_and_reloadable(env):
    paths, _ = env
    cs = code_list(3)
    p = synth(cs)
    run(env, p, cs)
    df = load_cache(paths, cs[0])
    assert df is not None and list(df.columns) == KLINE_COLUMNS and len(df) == 250
    assert not list(paths.kline_dir.glob(".*tmp"))


def test_second_run_same_day_only_refetches_the_live_day(env):
    """实时运行时缓存里“当日”那根不可信（可能是盘中半成品）：同日二次运行不再 0 联网，
    但也只补最后几天（请求天数很小），不会全量重拉。幂等由 pipeline 的 state 负责，不是 K 线缓存。"""
    cs = code_list(30)
    p = synth(cs)
    run(env, p, cs)
    p.reset_calls()
    out, fails, _ = run(env, p, cs)
    assert fails == {} and len(out) == 30
    assert p.batches and all(d <= 10 for d in p.requested_days), p.requested_days
    assert all(df["date"].iloc[-1] == TD and len(df) == 250 for df in out.values())


def test_incremental_only_fetches_missing_days(env):
    cs = code_list(25)
    p1 = synth(cs, end=PREV)
    out1, _, _ = run(env, p1, cs, td=PREV)
    assert all(d == 250 for d in p1.requested_days) and len(out1[cs[0]]) == 250
    p2 = synth(cs, end=TD)                                          # 次日：多出 1 根
    out2, fails, _ = run(env, p2, cs)
    assert fails == {} and len(out2) == 25
    assert p2.requested_days == [3, 3] and max(p2.requested_days) < 250   # 1 天间隔 + 2 余量，远小于 250
    assert p2.batch_sizes == [20, 5]
    df = out2[cs[0]]
    assert len(df) == 250 and df["date"].iloc[-1] == TD and df["date"].is_monotonic_increasing and df["date"].is_unique
    pd.testing.assert_frame_equal(df, make_kline_df(cs[0], TD, 250), check_exact=False)
    assert load_cache(env[0], cs[0])["date"].iloc[-1] == TD
    assert len(load_cache(env[0], cs[0])) == 251                    # 缓存是追加（不截断），返回值才限 kline_days


def test_incremental_days_grow_with_gap_and_fall_back_to_full_when_large(env):
    cs = code_list(2)
    run(env, synth(cs, end="2026-10-23"), cs, td="2026-10-23")
    p = synth(cs, end=TD)
    run(env, p, cs)
    assert p.requested_days == [13]                                 # 10-23 -> 11-03 间隔 11 天 + 2
    cs2 = code_list(2, 601000)
    run(env, synth(cs2, end="2026-01-05", days=300), cs2, td="2026-01-05")
    p2 = synth(cs2, end=TD)
    run(env, p2, cs2)
    assert p2.requested_days == [250]                               # 缺口 ≥ kline_days：直接全量


def test_mixed_cached_and_uncached_codes_use_separate_batches(env):
    cached, fresh = code_list(3), code_list(3, 601000)
    run(env, synth(cached, end=PREV), cached, td=PREV)
    p = synth(cached + fresh, end=TD)
    out, _, _ = run(env, p, cached + fresh)
    assert len(out) == 6
    by_days = {tuple(c.codes): c.days for c in p.kline_calls}
    assert by_days[tuple(fresh)] == 250 and by_days[tuple(cached)] == 3


def test_incremental_price_mismatch_falls_back_to_full_refetch(env):
    """除权导致前复权序列整体改写：增量重叠行与缓存不一致 -> 该只全量重拉并覆盖缓存。"""
    paths, _ = env
    cs = code_list(2)
    run(env, synth(cs, end=PREV), cs, td=PREV)
    p = synth(cs, end=TD)
    p.klines[cs[0]] = p.klines[cs[0]].assign(close=lambda d: d["close"] * 0.9)    # 整条序列被改写
    out, fails, _ = run(env, p, cs)
    assert fails == {} and p.requested_days == [3, 250]
    assert out[cs[0]]["close"].iloc[-1] == pytest.approx(p.klines[cs[0]]["close"].iloc[-1])
    assert len(load_cache(paths, cs[0])) == 250
    cached_last = load_cache(paths, cs[0])["close"].iloc[-1]
    assert cached_last == pytest.approx(p.klines[cs[0]]["close"].iloc[-1])


def test_incremental_no_overlap_falls_back_to_full(env):
    cs = code_list(1)
    run(env, synth(cs, end="2026-10-27"), cs, td="2026-10-27")
    p = synth(cs, end=TD)
    orig = p.get_klines
    p.get_klines = lambda codes, days, fq: (lambda ok, bad: ({c: d.tail(1) for c, d in ok.items()} if days < 250 else ok, bad))(*orig(codes, days, fq))
    out, fails, _ = run(env, p, cs)
    assert fails == {} and len(out[cs[0]]) == 250 and p.requested_days[-1] == 250


# ---------------------------------------------------------------- 缓存损坏
@pytest.mark.parametrize("kind", ["garbage", "empty", "wrong_columns", "truncated"])
def test_corrupt_cache_falls_back_to_full_refetch(env, kind):
    paths, _ = env
    cs = code_list(2)
    run(env, synth(cs, end=PREV), cs, td=PREV)
    f = paths.kline(cs[0])
    if kind == "garbage":
        f.write_bytes(b"this is not parquet")
    elif kind == "empty":
        f.write_bytes(b"")
    elif kind == "truncated":
        f.write_bytes(f.read_bytes()[:50])
    else:
        pd.DataFrame({"a": [1]}).to_parquet(f)
    assert load_cache(paths, cs[0]) is None
    p = synth(cs, end=TD)
    out, fails, _ = run(env, p, cs)
    assert fails == {} and len(out[cs[0]]) == 250
    assert p.requested_days.count(250) == 1 and p.requested_days.count(3) == 1      # 坏的那只全量、好的那只增量
    assert len(load_cache(paths, cs[0])) == 250                                      # 缓存已被修复


# ---------------------------------------------------------------- 历史回放
def test_replay_historical_date_truncates_and_uses_cache(env):
    cs = code_list(3)
    p = synth(cs, end=TD, days=400)
    run(env, p, cs)                                                  # 缓存到 11-03（满窗口 250 根）
    p.reset_calls()
    # 截断到 10-20 后只剩 240 根 < 250：缺更早历史 -> 全量重拉一次补足
    out, fails, _ = run(env, p, cs, td="2026-10-20", today=TD)
    assert fails == {} and p.batches == [cs] and p.requested_days[0] > 250
    for df in out.values():
        assert df["date"].iloc[-1] == "2026-10-20" and (df["date"] <= "2026-10-20").all() and len(df) == 250
    p.reset_calls()
    out2, fails2, _ = run(env, p, cs, td="2026-10-20", today=TD)     # 缓存已补足：不再联网
    assert p.kline_calls == [] and fails2 == {}
    pd.testing.assert_frame_equal(out2[cs[0]], out[cs[0]])
    p.reset_calls()
    out3, _, _ = run(env, p, cs, td="2026-11-03", today=TD)          # 回到最新日（实时日）：只补最后几天
    assert p.requested_days and all(d <= 10 for d in p.requested_days) and out3[cs[0]]["date"].iloc[-1] == TD


def test_replay_without_cache_requests_extra_days_then_truncates(env):
    cs = code_list(2)
    p = synth(cs, end=TD, days=400)
    out, fails, _ = run(env, p, cs, td="2026-10-20", today=TD)
    assert fails == {}
    assert p.requested_days[0] > 250                                 # 需多拉 20-> 今天的间隔
    for df in out.values():
        assert df["date"].iloc[-1] == "2026-10-20" and len(df) == 250
    pd.testing.assert_frame_equal(out[cs[0]], make_kline_df(cs[0], "2026-10-20", 250), check_exact=False)


def test_replay_date_stale_cache_is_extended_incrementally(env):
    cs = code_list(1)
    run(env, synth(cs, end="2026-10-14"), cs, td="2026-10-14")
    p = synth(cs, end=TD)
    out, fails, _ = run(env, p, cs, td="2026-10-20", today=TD)
    assert fails == {} and out[cs[0]]["date"].iloc[-1] == "2026-10-20"
    assert p.requested_days == [22]                                  # 缓存末日 10-14 距今天 20 天 + 2


def test_replay_date_non_published_is_suspended(env):
    cs = code_list(2)
    p = synth(cs, end="2026-10-16")
    out, fails, _ = run(env, p, cs, td="2026-10-20", today=TD)
    assert out == {} and all(v.startswith("suspended") for v in fails.values())


# ---------------------------------------------------------------- 杂项
def test_invalid_provider_frame_is_single_failure(env):
    cs = code_list(8)
    p = synth(cs)
    orig = p.get_klines

    def bad(codes, days, fq):
        ok, b = orig(codes, days, fq)
        ok[cs[1]] = ok[cs[1]].rename(columns={"close": "收盘"})
        ok[cs[2]] = ok[cs[2]].iloc[0:0]
        return ok, b
    p.get_klines = bad
    out, fails, _ = run(env, p, cs)
    assert set(out) == set(cs) - {cs[1], cs[2]} and set(fails) == {cs[1], cs[2]}


def test_results_do_not_alias_provider_frames_and_codes_normalised(env):
    p = synth(["600519"])
    out, _, _ = run(env, p, ["sh600519"])
    out["600519"].loc[0, "close"] = -1
    assert p.klines["600519"]["close"].iloc[0] != -1


def test_call_with_rate_limit_retry_helper(env):
    _, cfg = env
    sl = FakeSleep()
    n = {"i": 0}

    def fn():
        n["i"] += 1
        if n["i"] < 3:
            raise RateLimited("x")
        return "ok"
    assert fetcher.call_with_rate_limit_retry(fn, cfg, sl) == "ok" and sl.calls == [30.0, 30.0]
    n["i"] = -10
    with pytest.raises(RateLimited):
        fetcher.call_with_rate_limit_retry(fn, cfg, sl)


# ---------------------------------------------------------------- 当日 K 线可信度（盘中半成品）
def _cached(env, code):
    paths, _ = env
    return pd.read_parquet(paths.kline(code))


def test_polluted_cache_for_live_day_is_discarded_and_refetched(env):
    """旧缓存里有一根“盘中半成品”的当日 K 线：收盘后运行必须丢弃并用新数据覆盖，而不是因末日期==今天就跳过。"""
    cs = code_list(3)
    p = synth(cs, end=TD)
    run(env, p, cs)
    paths, _ = env
    for c in cs:                                           # 人为污染：当日收盘价/成交量改成“半成品”
        df = _cached(env, c)
        df.loc[df.index[-1], ["close", "volume"]] = [df["close"].iloc[-1] * 0.9, df["volume"].iloc[-1] * 0.3]
        df.to_parquet(paths.kline(c), index=False)
    p.reset_calls()
    out, fails, _ = run(env, p, cs)
    assert fails == {} and p.batches
    for c in cs:
        truth = make_kline_df(c, TD, 250).iloc[-1]
        assert out[c]["close"].iloc[-1] == pytest.approx(truth["close"]) and out[c]["volume"].iloc[-1] == pytest.approx(truth["volume"])
        assert _cached(env, c)["close"].iloc[-1] == pytest.approx(truth["close"])          # 缓存也被修正


def test_persist_today_false_keeps_provisional_bar_out_of_cache(env):
    """盘中（persist_today=False）：当日这根只用于本次计算，不写入 raw/kline。"""
    cs = code_list(3)
    p1 = synth(cs, end=PREV)
    run(env, p1, cs, td=PREV)                              # 缓存到 PREV（收盘数据）
    p2 = synth(cs, end=TD)
    paths, cfg = env
    out, fails = fetch_klines(p2, paths, cfg, cs, TD, FakeSleep(p2), today=TD, persist_today=False)
    assert fails == {} and all(df["date"].iloc[-1] == TD for df in out.values())          # 本次结果含当日
    for c in cs:
        assert _cached(env, c)["date"].iloc[-1] == PREV                                  # 缓存仍停在上一个收盘日


def test_persist_today_false_cold_start_writes_only_closed_days(env):
    cs = code_list(2)
    p = synth(cs, end=TD)
    paths, cfg = env
    out, _ = fetch_klines(p, paths, cfg, cs, TD, FakeSleep(p), today=TD, persist_today=False)
    assert all(len(df) == 250 and df["date"].iloc[-1] == TD for df in out.values())
    for c in cs:
        cached = _cached(env, c)
        assert (cached["date"] < TD).all() and len(cached) == 249


def test_replay_cache_is_trusted_when_not_live_day(env):
    """回放历史日（trade_date < today）：缓存是收盘数据，照常信任（不受“当日不可信”规则影响）。"""
    cs = code_list(2)
    p = synth(cs, end=TD, days=400)
    run(env, p, cs)
    p.reset_calls()
    run(env, p, cs, td="2026-10-20", today=TD)
    p.reset_calls()
    run(env, p, cs, td="2026-10-20", today=TD)
    assert p.kline_calls == []
