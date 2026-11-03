"""build_universe：并集去重、过滤规则、板块/profile 缓存、警告。"""
from __future__ import annotations

import pytest

from fakes import FakeProvider, FakeSleep, make_env, make_trading_days
from xb1lib.universe import refresh_profiles
from xb1lib import atomic
from xb1lib.errors import ConfigError, SourceUnavailable
from xb1lib.universe import build_universe

TD = "2026-11-03"
CAL = make_trading_days("2015-01-05", "2026-12-31")
OLD, NEW = "2018-03-01", "2026-09-01"      # NEW 到 TD 仅 ~45 个交易日


def prof(name, listed=OLD):
    return {"name": name, "listed_date": listed}


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path)


def mk(profiles, sectors=None):
    return FakeProvider(profiles=profiles, calendar=CAL, sectors=sectors)


def build(env, provider, today=TD, stocks=None, sectors=None, files=None, prime=True, **cfgover):
    """prime=True：模拟用户已执行过 `profile update`，把 provider 里有资料的股票写入缓存（build 本身从不联网取资料）。"""
    paths, cfg = env
    cfg["universe"].update(stocks=stocks or [], sectors=sectors or [], files=files or [])
    for k, v in cfgover.items():
        cfg[k].update(v)
    if prime and provider.profiles:
        try:
            refresh_profiles(paths, cfg, provider, list(provider.profiles), TD, sleep=lambda s: None)
        except SourceUnavailable:
            pass                       # 数据源故障类测试：缓存留空即可
        provider.reset_calls()
    sl = FakeSleep(provider)
    metas, excl = build_universe(paths, cfg, provider, TD, sleep=sl, today=today)
    return metas, excl, sl


def codes(metas):
    return [m.code for m in metas]


def test_union_dedup_sorted_and_meta_fields(env, tmp_path):
    paths, _ = env
    f = tmp_path / "my.txt"
    f.write_text("# 注释\n300124\n\n002979  # 行内\n601100\n", encoding="utf-8")
    csvf = tmp_path / "my.csv"
    csvf.write_text("code,name\n688017,绿的谐波\n300124,汇川\n", encoding="utf-8")
    p = mk({c: prof(f"股{c}") for c in ["300124", "601100", "688017", "002979", "600519"]},
           sectors={"人形机器人": {"code": "pt02GN2238", "members": [{"code": "sh601100", "name": "恒立液压"},
                                                                      {"code": "600519", "name": "贵州茅台"}]}})
    metas, excl, _ = build(env, p, stocks=["600519", 300124], sectors=["人形机器人"], files=[str(f), str(csvf)])
    assert codes(metas) == ["002979", "300124", "600519", "601100", "688017"]
    by = {m.code: m for m in metas}
    assert by["688017"].board == "star" and by["300124"].board == "gem" and by["600519"].board == "main"
    assert by["600519"].listed_date == OLD and by["601100"].name == "股601100"
    assert excl == {"_warnings": []}


def test_int_stock_code_zero_padded_and_invalid_raises(env):
    p = mk({"000001": prof("平安银行")})
    metas, _, _ = build(env, p, stocks=[1])
    assert codes(metas) == ["000001"]
    with pytest.raises(ConfigError):
        build(env, p, stocks=["abc"])


def test_missing_universe_file_is_config_error(env):
    with pytest.raises(ConfigError):
        build(env, mk({}), files=["/no/such/file.txt"])


def test_exclude_st_and_delisting_by_name(env):
    p = mk({"600001": prof("*ST 某某"), "600002": prof("ST华微"), "600003": prof("退市长油"),
            "600004": prof("某某退"), "600005": prof("正常股份")})
    metas, excl, _ = build(env, p, stocks=["600001", "600002", "600003", "600004", "600005"])
    assert set(excl) - {"_warnings"} == {"600001", "600002", "600003", "600004"}
    assert all(excl[c].startswith("st:") for c in ["600001", "600002", "600003", "600004"])
    assert codes(metas) == ["600005"]


def test_exclude_st_switch_off(env):
    p = mk({"600001": prof("*ST 某某")})
    metas, excl, _ = build(env, p, stocks=["600001"], filters={"exclude_st": False})
    assert codes(metas) == ["600001"] and set(excl) == {"_warnings"}


def test_exclude_bj_and_switch(env):
    p = mk({"600519": prof("茅台"), "830799": prof("艾融软件"), "430047": prof("诺思兰德")})
    metas, excl, _ = build(env, p, stocks=["600519", "830799", "430047"])
    assert codes(metas) == ["600519"] and excl["830799"] == "bj" and excl["430047"] == "bj"
    assert p.profile_calls == []                                 # run 不取资料；北交所先剔除，也不读其资料
    metas, excl, _ = build(env, mk({"830799": prof("艾融软件")}), stocks=["830799"], filters={"exclude_bj": False})
    assert codes(metas) == ["830799"]


def test_min_listed_days_uses_trading_calendar(env):
    p = mk({"300001": prof("新股", NEW), "300002": prof("老股", OLD), "300003": prof("临界", "2026-11-03")})
    metas, excl, _ = build(env, p, stocks=["300001", "300002", "300003"])
    assert codes(metas) == ["300002"]
    assert excl["300001"].startswith("new_listing:") and "< 120" in excl["300001"]
    assert excl["300003"].startswith("new_listing:") and "1 个交易日" in excl["300003"]


def test_min_listed_days_boundary(env):
    # 找一个上市日使 [listed, TD] 恰好 120 / 119 个交易日
    from xb1lib.calendar import TradeCalendar
    paths, _ = env
    days = [d for d in CAL if d <= TD]
    exactly120, only119 = days[-120], days[-119]
    p = mk({"300010": prof("刚好", exactly120), "300011": prof("差一天", only119)})
    metas, excl, _ = build(env, p, stocks=["300010", "300011"])
    assert codes(metas) == ["300010"] and "300011" in excl


def test_missing_listed_date_kept_not_listed_in_excluded_but_warned(env):
    p = mk({"300001": {"name": "缺上市日", "listed_date": None}, "300002": prof("正常")})
    metas, excl, _ = build(env, p, stocks=["300001", "300002", "300003"])      # 300003 完全没有 profile
    assert codes(metas) == ["300001", "300002", "300003"]
    assert "300001" not in excl and "300003" not in excl
    ws = excl["_warnings"]
    assert any("缺少上市日期" in w and "300001" in w for w in ws)     # 汇总一条，示例为首只
    assert any("缺少资料缓存" in w and "1 只" in w for w in ws)       # 300003 无缓存，run 不联网补
    assert p.profile_calls == []                                    # run 从不取资料
    assert [k for k in excl if k != "_warnings"] == []


def test_zero_min_listed_days_skips_profile_when_names_known(env):
    p = mk({}, sectors={"板块": {"code": "pt01", "members": [{"code": "600519", "name": "贵州茅台"},
                                                          {"code": "600001", "name": "ST某某"}]}})
    metas, excl, _ = build(env, p, sectors=["板块"], filters={"min_listed_days": 0})
    assert codes(metas) == ["600519"] and excl["600001"].startswith("st:")
    assert p.profile_calls == []


# ---------------------------------------------------------------- 板块缓存
def sector_prov():
    return mk({"600519": prof("茅台"), "601100": prof("恒立")},
              sectors={"人形机器人": {"code": "pt02GN2238", "members": [{"code": "600519", "name": "茅台"},
                                                                         {"code": "601100", "name": "恒立"}]}})


def test_sector_snapshot_written_and_cached_7_days(env):
    paths, _ = env
    p = sector_prov()
    build(env, p, sectors=["人形机器人"])
    snap = paths.universe_snapshot("pt02GN2238", TD)
    assert snap.exists()
    data = atomic.read_json(snap)
    assert data["sector_code"] == "pt02GN2238" and data["sector_name"] == "人形机器人" and len(data["constituents"]) == 2
    assert p.sector_search_calls == ["人形机器人"] and p.sector_constituent_calls == ["pt02GN2238"]
    p.reset_calls()
    metas, _, _ = build(env, p, sectors=["人形机器人"], today="2026-11-10")      # 7 天龄仍有效（≤7）
    assert codes(metas) == ["600519", "601100"]
    assert p.sector_search_calls == [] and p.sector_constituent_calls == []
    metas, _, _ = build(env, p, sectors=["pt02GN2238"], today="2026-11-10")      # 直接给板块代码也命中缓存
    assert p.sector_constituent_calls == []


def test_sector_cache_expires_after_7_days(env):
    paths, _ = env
    p = sector_prov()
    build(env, p, sectors=["人形机器人"])
    p.reset_calls()
    build(env, p, sectors=["人形机器人"], today="2026-11-11")                      # 8 天
    assert p.sector_constituent_calls == ["pt02GN2238"]
    assert paths.universe_snapshot("pt02GN2238", "2026-11-11").exists()


def test_sector_refresh_failure_uses_old_snapshot_with_warning(env):
    p = sector_prov()
    build(env, p, sectors=["人形机器人"])
    p.unavailable = True
    metas, excl, _ = build(env, p, sectors=["人形机器人"], today="2026-11-20", filters={"min_listed_days": 0})
    assert codes(metas) == ["600519", "601100"]
    assert any("旧快照" in w for w in excl["_warnings"])


def test_sector_failure_without_snapshot_propagates(env):
    p = sector_prov()
    p.unavailable = True
    with pytest.raises(SourceUnavailable):
        build(env, p, sectors=["人形机器人"])


def test_sector_not_found_warns_and_continues(env):
    p = sector_prov()
    metas, excl, _ = build(env, p, stocks=["600519"], sectors=["不存在的板块"])
    assert codes(metas) == ["600519"]
    assert any("不存在的板块" in w for w in excl["_warnings"])


# ---------------------------------------------------------------- profile：只手动更新，run 不联网
CFG_FETCH = {"fetch": {"batch_size": 20, "batch_interval": 1.5, "rate_limit_wait": 0, "max_retries": 3}}


def _sleep_recorder():
    calls = []
    return (lambda s: calls.append(s)), calls


def test_run_never_fetches_profile_even_when_cache_is_old(env):
    """缓存再旧也不在 run 里刷新：资料只由 profile update 手动更新。"""
    paths, cfg = env
    p = mk({"600519": prof("茅台")})
    sl, _ = _sleep_recorder()
    refresh_profiles(paths, cfg, p, ["600519"], today="2026-01-01", sleep=sl)     # 手动更新一次
    p.reset_calls()
    metas, excl, _ = build(env, p, stocks=["600519"], today="2026-11-10")          # 远超 7 天
    assert codes(metas) == ["600519"] and metas[0].name == "茅台"                   # 用缓存里的名称
    assert p.profile_calls == []
    assert not any("缺少" in w for w in excl["_warnings"])


def test_profile_update_batches_le_10_with_interval(env):
    """westock profile 一次最多 10 只（实测）；批间隔遵守 batch_interval。"""
    paths, cfg = env
    cs = [f"6000{i:02d}" for i in range(45)]
    p = mk({c: prof("某股") for c in cs})
    sl, calls = _sleep_recorder()
    res = refresh_profiles(paths, cfg, p, cs, today=TD, sleep=sl)
    assert res == {"updated": 45, "failed": []}
    assert [len(b) for b in p.profile_calls] == [10, 10, 10, 10, 5]
    assert calls == [1.5] * 4
    assert all(paths.profile(c).exists() for c in cs)


def test_missing_cache_is_one_aggregated_warning_without_network(env):
    p = mk({})
    metas, excl, _ = build(env, p, stocks=["600519", "600520", "600521"], filters={"min_listed_days": 120})
    assert codes(metas) == ["600519", "600520", "600521"]                # 缺资料不剔除
    assert p.profile_calls == []
    ws = [w for w in excl["_warnings"] if "缺少资料缓存" in w]
    assert len(ws) == 1 and "3 只" in ws[0] and "profile update" in ws[0]


def test_cache_written_by_update_is_used_by_run(env):
    paths, cfg = env
    p = mk({"300001": {"name": "ST某某", "listed_date": OLD}})
    sl, _ = _sleep_recorder()
    refresh_profiles(paths, cfg, p, ["300001"], today=TD, sleep=sl)
    metas, excl, _ = build(env, mk({}), stocks=["300001"])
    assert codes(metas) == [] and excl["300001"].startswith("st:")       # 名称来自手动更新的缓存
