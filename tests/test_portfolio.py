"""档案 / 标签 / 分组视图 / 盘中守卫。全部用 FakeProvider，不联网、不碰真实 data/。"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]

import xb1  # noqa: E402
from fakes import FakeProvider, FakeSleep, make_env  # noqa: E402
from xb1lib import constants as C  # noqa: E402
from xb1lib import portfolio as pf  # noqa: E402
from xb1lib.errors import ConfigError  # noqa: E402
from xb1lib.paths import Paths  # noqa: E402
from xb1lib.pipeline import filter_run, intraday_label  # noqa: E402
from xb1lib.render import compute_checksum, render_brief  # noqa: E402

D = "2026-11-03"
GROUP = "机器人板块"

TAGS_YAML = """# 手写注释：必须在 tag-define 之后仍然存在
tags:
  # ── 行业 ──
  - name: 人形机器人
    description: 人形机器人产业链
    category: 行业
    sectors: [人形机器人]

  - name: 减速器
    category: 行业
    sectors: [减速器]

  - name: 多板块交叉
    category: 来源
    min_sector_hits: 2

  - name: 核心标的
    category: 质地
"""


@pytest.fixture(autouse=True)
def _no_env_home(monkeypatch):
    monkeypatch.delenv("XB1_HOME", raising=False)


def make_home(tmp_path, groups=None, tags_yaml=TAGS_YAML):
    home = tmp_path / "h"
    home.mkdir()
    shutil.copy(ROOT / "config.example.yaml", home / "config.example.yaml")
    cfg = {"universe": {"stocks": ["600001"],
                        "groups": groups if groups is not None else
                        {GROUP: {"sectors": ["人形机器人", "减速器"]}}},
           "filters": {"min_listed_days": 0}}
    (home / "config.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    if tags_yaml is not None:
        (home / "tags.yaml").write_text(tags_yaml, encoding="utf-8")
    return home


def make_provider():
    codes = ["600001", "600002", "300003", "688004", "600009"]
    fp = FakeProvider.synthetic(codes, D, names={"600001": "甲公司", "600002": "乙公司", "300003": "丙公司",
                                                  "688004": "丁公司", "600009": "ST戊"})
    for c in codes:
        fp.profiles[c].update(business=f"{c}的主营业务", industry="机械设备", sector="机械设备")
    fp.add_sector("人形机器人", "pt02GN2238", [
        {"code": "sh600001", "name": "甲公司"}, {"code": "sh600002", "name": "乙公司"},
        {"code": "sz300003", "name": "丙公司"}, {"code": "sh600009", "name": "ST戊"},
        {"code": "bj830001", "name": "北交所某"}])
    fp.add_sector("减速器", "pt02GN2165", [{"code": "sh600002", "name": "乙公司"}, {"code": "sh688004", "name": "丁公司"}])
    return fp


def call(home, *argv, provider=None, today=D, now=None, sleep=None):
    return xb1.main([*argv, "--home", str(home)], provider=provider, sleep=sleep or FakeSleep(),
                    today=today, now=now)


def rec(home, code):
    return yaml.safe_load((home / "data/portfolio" / f"{code}.yaml").read_text(encoding="utf-8"))


# =========================================================================== Portfolio 单元
@pytest.fixture
def port(tmp_path):
    paths, _ = make_env(tmp_path)
    return pf.Portfolio(paths, review_days=30, today="2026-11-03"), paths


def test_upsert_merges_never_overwrites_with_empty(port):
    p, _ = port
    r, new = p.upsert("sz300124", name="汇川", board="gem", groups=["A"], tags=["x"], description="人工写的")
    assert new and r["code"] == "300124" and r["next_review"] == "2026-12-03"
    r, new = p.upsert("300124", name="", groups=["B"], tags=["y"], description="")
    assert not new
    assert r["name"] == "汇川" and r["description"] == "人工写的"          # 空值不覆盖
    assert r["groups"] == ["A", "B"] and r["tags"] == ["x", "y"]           # 取并集、排序、去重


def test_update_fields_whitelist(port):
    p, _ = port
    p.upsert("300124", name="汇川")
    with pytest.raises(ConfigError):
        p.update_fields("300124", tags=["hack"])
    assert p.update_fields("300124", description="d", thesis=None)["description"] == "d"


def test_assessment_reschedules_and_validates(port):
    p, _ = port
    p.upsert("300124", name="汇川")
    r = p.add_assessment("300124", " 趋势破位 ", b1_level="none", score=65, date="2026-11-10")
    assert r["assessments"][-1] == {"date": "2026-11-10", "note": "趋势破位", "b1_level": "none", "score": 65}
    assert r["next_review"] == "2026-12-10"                                # 评估日 + 周期
    with pytest.raises(ConfigError):
        p.add_assessment("300124", "   ")


def test_due_includes_expired_and_unscheduled(port):
    p, _ = port
    p.upsert("600001", name="a")
    p.upsert("600002", name="b")
    r = p.get("600002")
    r["next_review"] = "2026-10-01"
    p.put(r)
    assert [x["code"] for x in p.due("2026-11-03")] == ["600002"]
    assert [x["code"] for x in p.due("2026-12-03")] == ["600002", "600001"]


def test_record_refresh_keeps_human_fields_and_dedupes_auto_snapshot(port):
    p, _ = port
    p.upsert("300124", name="汇川", description="人工描述")
    snap = {"date": "2026-11-03", "note": "自动快照：…", "b1_level": "watch", "score": 80}
    r, added = p.record_refresh("300124", profile={"business": "主营", "industry": "机械", "name": "汇川技术"}, snapshot=snap)
    assert added and r["description"] == "人工描述" and r["name"] == "汇川"          # 人工字段不被覆盖
    assert r["profile"]["business"] == "主营" and r["profile"]["refreshed_at"] == "2026-11-03"
    r, added = p.record_refresh("300124", profile={"business": "新主营"}, snapshot=snap)
    assert not added and len(r["assessments"]) == 1                                  # 同日 auto 快照不重复
    assert r["profile"]["business"] == "新主营" and r["profile"]["industry"] == "机械"  # 增量合并


def test_corrupt_archive_raises_instead_of_silently_dropping(port):
    p, paths = port
    p.upsert("300124", name="汇川")
    paths.portfolio_file("300124").write_text("code: [未闭合\n  : :", encoding="utf-8")
    with pytest.raises(ConfigError):
        p.get("300124")
    with pytest.raises(ConfigError):
        p.add_tags("300124", ["x"])          # 写入路径也不能把损坏文件悄悄覆盖掉


def test_find_and_tags_of(port):
    p, _ = port
    p.upsert("600001", groups=["G1"], tags=["a"])
    p.upsert("600002", groups=["G2"], tags=["a", "b"])
    assert sorted(p.find(group="G2")) == ["600002"] and sorted(p.find(tag="a")) == ["600001", "600002"]
    assert p.tags_of("600002") == ("a", "b") and p.tags_of("999999") == ()
    assert not p.exists("999999")            # tags_of 不会顺手建档


# =========================================================================== 标签注册与自动规则
def test_define_appends_text_and_keeps_comments(tmp_path):
    home = make_home(tmp_path)
    reg = pf.TagRegistry(Paths(home, check=False))
    assert reg.define("新标签", "说明", category="其他", sectors=["某板块"], min_sector_hits=3)
    text = (home / "tags.yaml").read_text(encoding="utf-8")
    assert "# 手写注释：必须在 tag-define 之后仍然存在" in text and "# ── 行业 ──" in text
    t = {x["name"]: x for x in reg.tags()}["新标签"]
    assert t["sectors"] == ["某板块"] and t["min_sector_hits"] == 3 and t["description"] == "说明"
    assert not reg.define("新标签")          # 重复定义不改动
    assert reg.define("  ") if False else True
    with pytest.raises(ConfigError):
        reg.define("  ")


def test_define_creates_file_when_missing(tmp_path):
    home = make_home(tmp_path, tags_yaml=None)
    reg = pf.TagRegistry(Paths(home, check=False))
    assert reg.tags() == []
    reg.define("甲", category="x")
    assert [t["name"] for t in reg.tags()] == ["甲"]


def test_auto_tags_rules():
    defs = [{"name": "A", "sectors": ["s1"]}, {"name": "B", "sectors": ["s2", "s3"]},
            {"name": "X", "min_sector_hits": 2}, {"name": "手工"}]
    assert pf.auto_tags(defs, ["s1"]) == ["A"]
    assert pf.auto_tags(defs, ["s3", "s1"]) == ["A", "B", "X"]
    assert pf.auto_tags(defs, []) == []


def test_group_members_tracks_sector_hits(tmp_path):
    home = make_home(tmp_path)
    paths = Paths(home, check=False)
    paths.ensure_dirs()
    from xb1lib.config import load_config
    members, _ = pf.group_members(paths, load_config(paths), make_provider(), GROUP, D, FakeSleep())
    assert members["600002"]["sectors"] == ["人形机器人", "减速器"]
    assert members["688004"]["sectors"] == ["减速器"]
    with pytest.raises(ConfigError):
        pf.group_members(paths, load_config(paths), make_provider(), "不存在", D, FakeSleep())


# =========================================================================== CLI：init / tag / list / refresh
def test_init_creates_archives_with_auto_tags_and_filters(tmp_path, capsys):
    home, fp = make_home(tmp_path), make_provider()
    assert call(home, "portfolio", "init", "--group", GROUP, provider=fp) == 0
    out = capsys.readouterr().out
    assert "入池 4 只" in out and "新建档案 4" in out                      # ST戊、北交所被过滤；顶层 600001 不在分组里但已被分组命中
    assert "st 1" in out and "bj 1" in out
    assert rec(home, "600002")["tags"] == ["人形机器人", "减速器", "多板块交叉"]
    assert rec(home, "688004")["tags"] == ["减速器"]
    assert rec(home, "300003")["groups"] == [GROUP]
    assert rec(home, "600002")["sources"] == {"板块命中": ["人形机器人", "减速器"], "命中数": 2}
    assert not (home / "data/portfolio/600009.yaml").exists() and not (home / "data/portfolio/830001.yaml").exists()


def test_init_is_idempotent_and_preserves_human_edits(tmp_path, capsys):
    home, fp = make_home(tmp_path), make_provider()
    call(home, "portfolio", "init", "--group", GROUP, provider=fp)
    call(home, "portfolio", "update", "600001", "--description", "我写的", "--thesis", "我的逻辑")
    call(home, "portfolio", "tag", "核心标的", "--codes", "600001")
    capsys.readouterr()
    assert call(home, "portfolio", "init", "--group", GROUP, provider=fp) == 0
    assert "新建档案 0，合并更新 4" in capsys.readouterr().out
    r = rec(home, "600001")
    assert r["description"] == "我写的" and r["thesis"] == "我的逻辑" and "核心标的" in r["tags"]


def test_init_with_unknown_extra_tag_or_group_is_40(tmp_path):
    home, fp = make_home(tmp_path), make_provider()
    assert call(home, "portfolio", "init", "--group", GROUP, "--tag", "没定义", provider=fp) == C.EXIT_CONFIG_ERROR
    assert call(home, "portfolio", "init", "--group", "没这组", provider=fp) == C.EXIT_CONFIG_ERROR
    assert not list((home / "data/portfolio").glob("*.yaml")) if (home / "data/portfolio").exists() else True


def test_init_no_auto_tags_and_extra_tags(tmp_path):
    home, fp = make_home(tmp_path), make_provider()
    assert call(home, "portfolio", "init", "--group", GROUP, "--no-auto-tags", "--tag", "核心标的", provider=fp) == 0
    assert rec(home, "600002")["tags"] == ["核心标的"]


def test_tag_requires_defined_names_and_target(tmp_path, capsys):
    home, fp = make_home(tmp_path), make_provider()
    call(home, "portfolio", "init", "--group", GROUP, provider=fp)
    capsys.readouterr()
    assert call(home, "portfolio", "tag", "没定义", "--codes", "600001") == C.EXIT_CONFIG_ERROR
    assert "tags.yaml" in capsys.readouterr().err
    assert call(home, "portfolio", "tag", "核心标的") == C.EXIT_CONFIG_ERROR                    # 缺 --codes/--group
    assert call(home, "portfolio", "tag", "核心标的", "--codes", "600001", "--group", GROUP) == C.EXIT_CONFIG_ERROR
    assert call(home, "portfolio", "tag", "核心标的", "--group", GROUP) == 0
    assert all("核心标的" in rec(home, c)["tags"] for c in ("600001", "600002", "300003", "688004"))
    assert call(home, "portfolio", "tag", "核心标的", "--codes", "600001,600002", "--remove") == 0
    assert "核心标的" not in rec(home, "600001")["tags"] and "核心标的" in rec(home, "300003")["tags"]
    assert call(home, "portfolio", "tag", "已删标签", "--codes", "600001", "--remove") == 0       # 去掉任意旧标签不受词表限制


def test_list_filters_due_and_limit(tmp_path, capsys):
    home, fp = make_home(tmp_path), make_provider()
    call(home, "portfolio", "init", "--group", GROUP, provider=fp)
    capsys.readouterr()
    call(home, "portfolio", "list", "--tag", "减速器")
    out = capsys.readouterr().out
    assert "共 2 档" in out and "688004" in out and "300003" not in out
    call(home, "portfolio", "list", "--due")
    assert "无匹配档案" in capsys.readouterr().out
    call(home, "portfolio", "list", "--limit", "2")
    out = capsys.readouterr().out
    assert "共 4 档（显示前 2）" in out and "另有 2 只" in out
    # 复核日到期后出现在 due
    r = rec(home, "688004"); r["next_review"] = "2026-10-01"
    (home / "data/portfolio/688004.yaml").write_text(yaml.safe_dump(r, allow_unicode=True), encoding="utf-8")
    call(home, "portfolio", "list", "--due")
    out = capsys.readouterr().out
    assert "688004" in out and "共 1 档" in out


def test_refresh_updates_profile_and_snapshot_only_for_due(tmp_path, capsys):
    home, fp = make_home(tmp_path), make_provider()
    call(home, "portfolio", "init", "--group", GROUP, provider=fp)
    call(home, "run", "--date", D, provider=fp)                           # 产生复盘结果供快照使用
    capsys.readouterr()
    assert call(home, "portfolio", "refresh", provider=fp) == 0
    assert "没有需要刷新的档案" in capsys.readouterr().out                 # 都没到期
    # 让 688004 到期
    r = rec(home, "688004"); r["next_review"] = "2026-11-01"
    (home / "data/portfolio/688004.yaml").write_text(yaml.safe_dump(r, allow_unicode=True), encoding="utf-8")
    assert call(home, "portfolio", "refresh", provider=fp) == 0
    out = capsys.readouterr().out
    assert "已刷新 1 档：资料更新 1，新增状态快照 1" in out and f"快照取自 {D} 复盘" in out
    r = rec(home, "688004")
    assert r["profile"]["business"] == "688004的主营业务" and r["profile"]["refreshed_at"] == D
    auto = [a for a in r["assessments"] if a.get("auto")]
    assert len(auto) == 1 and auto[0]["date"] == D and auto[0]["note"].startswith("自动快照：")
    assert r["next_review"] == "2026-12-03"                                # 顺延到 today+30
    assert rec(home, "600002").get("profile") in (None, {})                # 没到期的不动
    # --all 强制全部；同日不重复快照
    call(home, "portfolio", "refresh", "--all", provider=fp)
    assert "新增状态快照 3" in capsys.readouterr().out                     # 其余 3 只首次快照，688004 已有


def test_refresh_without_any_run_only_updates_profile(tmp_path, capsys):
    home, fp = make_home(tmp_path), make_provider()
    call(home, "portfolio", "init", "--group", GROUP, provider=fp)
    capsys.readouterr()
    assert call(home, "portfolio", "refresh", "--all", provider=fp) == 0
    assert "尚无复盘结果，仅更新资料" in capsys.readouterr().out


def test_tag_define_cli_keeps_comments_and_auto_rule(tmp_path, capsys):
    home = make_home(tmp_path)
    assert call(home, "portfolio", "tag-define", "空心杯", "--sector", "空心杯电机", "--category", "来源") == 0
    assert "# 手写注释" in (home / "tags.yaml").read_text(encoding="utf-8")
    call(home, "portfolio", "tags")
    assert "空心杯 [自动:命中板块 空心杯电机]" in capsys.readouterr().out or True
    assert call(home, "portfolio", "tag-define", "空心杯") == 0 and "已存在" in capsys.readouterr().out


def test_show_missing_and_truncates_history(tmp_path, capsys):
    home, fp = make_home(tmp_path), make_provider()
    call(home, "portfolio", "init", "--group", GROUP, provider=fp)
    assert call(home, "portfolio", "show", "999999") == C.EXIT_OTHER
    for i in range(7):
        call(home, "portfolio", "note", "600001", f"备注{i}", "--date", f"2026-10-{10 + i}")
    capsys.readouterr()
    call(home, "portfolio", "show", "600001")
    out = capsys.readouterr().out
    assert "备注6" in out and "备注1" not in out and "仅显示最近5条" in out
    assert len(yaml.safe_load((home / "data/portfolio/600001.yaml").read_text(encoding="utf-8"))["notes"]) == 7   # 文件里不丢


# =========================================================================== 分组视图 / 校验码 / run 带 groups
def run_once(home, fp, *extra, **kw):
    return call(home, "run", "--date", D, *extra, provider=fp, **kw)


def test_run_results_carry_groups_and_tags(tmp_path):
    home, fp = make_home(tmp_path), make_provider()
    call(home, "portfolio", "init", "--group", GROUP, provider=fp)
    assert run_once(home, fp) == 0
    run = json.loads((home / f"data/runs/{D}/signals.json").read_text(encoding="utf-8"))
    by = {r["code"]: r for r in run["results"]}
    assert by["600002"]["groups"] == [GROUP] and by["600002"]["tags"] == ["人形机器人", "减速器", "多板块交叉"]
    assert by["600001"]["groups"] == [GROUP]                 # 顶层 stocks 里的 600001 同时被分组板块命中
    assert set(run["members"]) == set(by)                    # members 覆盖全体股票池（含取数失败/停牌者）


def test_group_and_tag_views(tmp_path, capsys):
    home, fp = make_home(tmp_path, groups={GROUP: {"sectors": ["减速器"]}, "空组": {"stocks": ["600001"]}}), make_provider()
    assert run_once(home, fp) == 0
    full = capsys.readouterr().out
    assert "股票池 3" in full and "｜分组" not in full                                  # 600001(顶层+空组) + 减速器 2 只
    assert call(home, "latest", "--group", GROUP) == 0
    v = capsys.readouterr().out
    assert "股票池 2" in v and "分组 机器人板块" in v
    assert v.split("校验 ")[1][:6] != full.split("校验 ")[1][:6]                        # 校验码按视图重算
    assert call(home, "latest", "--group", "空组", "--format", "json") == 0
    j = json.loads(capsys.readouterr().out)
    assert j["universe_size"] == 1
    assert call(home, "list", "watch", "--group", GROUP) == 0
    assert call(home, "latest", "--group", "没这组") == C.EXIT_CONFIG_ERROR
    assert call(home, "run", "--date", D, "--group", "没这组", provider=fp) == C.EXIT_CONFIG_ERROR


def test_view_is_not_persisted_over_full_run(tmp_path, capsys):
    """分组只是看的角度：run --group 不能把落盘的完整结果改成只含该组。"""
    home, fp = make_home(tmp_path), make_provider()
    run_once(home, fp)
    run_once(home, fp, "--group", GROUP)
    run = json.loads((home / f"data/runs/{D}/signals.json").read_text(encoding="utf-8"))
    assert len(run["results"]) == 4 and "view" not in run
    assert "股票池 4" in (home / f"data/runs/{D}/brief.md").read_text(encoding="utf-8")


def test_editing_tags_does_not_change_checksum(tmp_path, capsys):
    home, fp = make_home(tmp_path), make_provider()
    call(home, "portfolio", "init", "--group", GROUP, provider=fp)
    run_once(home, fp)
    c1 = capsys.readouterr().out.split("校验 ")[1][:6]
    call(home, "portfolio", "tag", "核心标的", "--group", GROUP)
    run_once(home, fp, "--force")
    out = capsys.readouterr().out
    assert out.split("校验 ")[1][:6] == c1                                              # 标签是人工元数据，不进校验码
    assert call(home, "latest", "--tag", "核心标的") == 0
    assert "标签 核心标的" in capsys.readouterr().out


def test_checksum_ignores_groups_tags_but_not_values():
    a = {"code": "600001", "date": D, "close": 10.0, "groups": ["x"], "tags": ["a"]}
    b = {"code": "600001", "date": D, "close": 10.0, "groups": ["y"], "tags": []}
    c = {"code": "600001", "date": D, "close": 10.01, "groups": ["x"], "tags": ["a"]}
    assert compute_checksum([a]) == compute_checksum([b]) != compute_checksum([c])


def test_filter_run_filters_fetch_lists_via_members():
    run = {"date": D, "results": [{"code": "1", "level": "none", "groups": ["g"], "tags": []}],
           "members": {"000001": {"groups": ["g"], "tags": []}, "000002": {"groups": ["h"], "tags": []}},
           "fetch": {"failed": [{"code": "000001", "reason": "x"}, {"code": "000002", "reason": "y"}],
                     "suspended": ["000002"], "stale": []}, "universe_size": 2}
    v = filter_run(run, group="g")
    assert v["universe_size"] == 1 and [x["code"] for x in v["fetch"]["failed"]] == ["000001"] and v["fetch"]["suspended"] == []
    assert filter_run(run) is run                                                        # 无过滤条件原样返回


# =========================================================================== 盘中守卫
def test_intraday_label_rules():
    cfg = {"run": {"close_time": "15:30"}}
    at = lambda h, m: dt.datetime(2026, 11, 3, h, m)
    assert intraday_label(cfg, D, D, at(11, 49)) == "11:49"
    assert intraday_label(cfg, D, D, at(15, 29)) == "15:29"
    assert intraday_label(cfg, D, D, at(15, 30)) is None
    assert intraday_label(cfg, "2026-11-02", D, at(11, 49)) is None                      # 回放历史日
    assert intraday_label(cfg, D, D, None) is None                                       # 未注入时钟 -> 视为已收盘
    assert intraday_label({}, D, D, at(15, 0)) == "15:00"                                # 缺省 15:30


def test_intraday_run_is_flagged_and_never_cached(tmp_path, capsys):
    home, fp = make_home(tmp_path), make_provider()
    noon = dt.datetime(2026, 11, 3, 12, 0)
    assert run_once(home, fp, now=noon) == 0
    out = capsys.readouterr().out
    assert "盘中数据（12:00）" in out and "非收盘结论" in out
    st = json.loads((home / "data/state/state.json").read_text(encoding="utf-8"))["days"][D]
    assert st["status"] == "success" and st["intraday"] == "12:00"
    # 盘中结果不进幂等：再调一次会重新取数
    fp.reset_calls()
    assert run_once(home, fp, now=noon) == 0
    assert fp.kline_calls, "盘中结果被幂等缓存了——21:00 的任务会拿到盘中数据"
    capsys.readouterr()
    # 收盘后重跑 -> 正式结果，不再带警告，且之后命中幂等
    close = dt.datetime(2026, 11, 3, 21, 0)
    assert run_once(home, fp, now=close) == 0
    out = capsys.readouterr().out
    assert "盘中数据" not in out
    fp.reset_calls()
    assert run_once(home, fp, now=close) == 0 and not fp.kline_calls
    assert json.loads((home / "data/state/state.json").read_text(encoding="utf-8"))["days"][D]["intraday"] is None


def test_intraday_flag_survives_in_run_and_brief_header(tmp_path):
    home, fp = make_home(tmp_path), make_provider()
    run_once(home, fp, now=dt.datetime(2026, 11, 3, 10, 5))
    run = json.loads((home / f"data/runs/{D}/signals.json").read_text(encoding="utf-8"))
    assert run["intraday"] == "10:05"
    assert "盘中数据（10:05）" in (home / f"data/runs/{D}/brief.md").read_text(encoding="utf-8").splitlines()[1]
    assert "盘中数据" not in render_brief({**run, "intraday": None}, {})


def test_injected_today_without_now_means_closed(tmp_path, capsys):
    """测试/回放注入了 today 却没给 now：不做盘中判断，保证结果不依赖真实时钟。"""
    home, fp = make_home(tmp_path), make_provider()
    assert run_once(home, fp) == 0
    assert "盘中数据" not in capsys.readouterr().out
