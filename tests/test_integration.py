"""集成测试：xb1.py 全流程（FakeProvider，无网络）。对应 PRD 验收 AC3/4/7/11/12/13 与退出码约定。"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]

import xb1  # noqa: E402
from fakes import FakeProvider, FakeSleep, make_kline_df, make_trading_days  # noqa: E402
from xb1lib import atomic, constants as C  # noqa: E402
from xb1lib.state import State  # noqa: E402

D = "2026-11-03"          # 周二
CODES = ["600001", "600002", "300003", "688004", "000005"]


@pytest.fixture(autouse=True)
def _no_env_home(monkeypatch):
    monkeypatch.delenv("XB1_HOME", raising=False)


def make_home(tmp_path, codes=CODES, **cfg_over):
    home = tmp_path / "xb1home"
    home.mkdir()
    shutil.copy(ROOT / "config.example.yaml", home / "config.example.yaml")
    cfg = {"universe": {"stocks": list(codes)}, "filters": {"min_listed_days": 0}}
    for k, v in cfg_over.items():
        cfg.setdefault(k, {}).update(v)
    (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return home


def call(home, *argv, provider=None, today=D, sleep=None):
    return xb1.main([*argv, "--home", str(home)], provider=provider, sleep=sleep or FakeSleep(), today=today)


def all_files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


# --------------------------------------------------------------------------- 基本流程
def test_run_success_layout_and_brief(tmp_path, capsys):
    home = make_home(tmp_path)
    fp = FakeProvider.synthetic(CODES, D)
    assert call(home, "run", "--date", D, provider=fp) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"【B1 复盘】数据日期 {D}（周二）｜股票池 5")
    assert "校验 " in out and "免责声明" in out
    # AC12：结构与 5.1.1 一致
    expect = [f"data/runs/{D}/signals.json", f"data/runs/{D}/brief.md", f"data/runs/{D}/meta.json",
              f"data/runs/{D}/fetch_errors.json", f"data/reports/{D}/b1_report.xlsx", f"data/reports/{D}/b1_report.md",
              f"data/reports/{D}/b1_cards.html",
              "data/history/signals.parquet", "data/state/state.json", f"data/logs/run-{D}.log",
              "data/raw/calendar/trade_calendar.json"] + [f"data/raw/kline/{c}.parquet" for c in CODES] \
             + [f"data/cache/indicators/{c}.parquet" for c in CODES]
    files = all_files(home)
    for e in expect:
        assert e in files, e
    # 运行锁已释放、无临时文件残留
    assert not (home / "data/state/run.lock").exists()
    assert not [f for f in files if f.endswith(".tmp")]
    meta = json.loads((home / f"data/runs/{D}/meta.json").read_text())
    assert meta["checksum"] in out and meta["exit_code"] == 0 and meta["provider_version"] == "fake-1.0.0"


def test_nothing_written_outside_home(tmp_path):
    """AC12：$XB1_HOME 之外无新增文件。"""
    home = make_home(tmp_path)
    before = {p for p in tmp_path.rglob("*") if home not in p.parents and p != home}
    call(home, "run", "--date", D, provider=FakeProvider.synthetic(CODES, D))
    after = {p for p in tmp_path.rglob("*") if home not in p.parents and p != home}
    assert before == after


# --------------------------------------------------------------------------- AC3
def test_non_trading_day_returns_10(tmp_path, capsys):
    home = make_home(tmp_path)
    fp = FakeProvider.synthetic(CODES, "2026-11-06")
    sat = "2026-11-07"
    assert call(home, "run", "--date", sat, provider=fp, today=sat) == C.EXIT_NOT_TRADING_DAY
    assert "非交易日" in capsys.readouterr().out
    assert not (home / "data/reports" / sat).exists() and not (home / "data/runs" / sat).exists()
    assert fp.kline_calls == []                      # 没有取数
    assert not State(__import__("xb1lib.paths", fromlist=["Paths"]).Paths(home, check=False)).is_success(sat)


# --------------------------------------------------------------------------- AC4
def test_idempotent_and_force(tmp_path, capsys):
    home = make_home(tmp_path)
    fp = FakeProvider.synthetic(CODES, D)
    call(home, "run", "--date", D, provider=fp)
    first = capsys.readouterr().out
    n_calls = len(fp.kline_calls)
    assert call(home, "run", "--date", D, provider=fp) == 0
    second = capsys.readouterr().out
    assert second == first and len(fp.kline_calls) == n_calls       # 秒回：不联网、结果一致
    fp.reset_calls()
    assert call(home, "run", "--date", D, "--force", provider=fp) == 0
    third = capsys.readouterr().out
    assert third == first                                           # 同输入逐位一致（可复现）
    assert fp.calendar_calls >= 0


def test_idempotent_works_when_source_down(tmp_path, capsys):
    home = make_home(tmp_path)
    call(home, "run", "--date", D, provider=FakeProvider.synthetic(CODES, D))
    capsys.readouterr()
    down = FakeProvider.synthetic(CODES, D); down.unavailable = True
    assert call(home, "run", "--date", D, provider=down) == 0       # 已成功的日子不依赖数据源
    assert down.kline_calls == []


# --------------------------------------------------------------------------- AC7 / 退出码 20
def test_source_down_returns_20_no_partial_report(tmp_path, capsys):
    home = make_home(tmp_path)
    fp = FakeProvider.synthetic(CODES, D); fp.unavailable = True
    assert call(home, "run", "--date", D, provider=fp) == C.EXIT_SOURCE_UNAVAILABLE
    assert not (home / "data/reports" / D).exists()
    assert not (home / "data/runs" / D / "brief.md").exists()
    assert not (home / "data/history/signals.parquet").exists()
    assert not (home / "data/state/run.lock").exists()
    st = json.loads((home / "data/state/state.json").read_text())
    assert st["days"][D]["status"] == "failed" and st["days"][D]["exit_code"] == 20
    # 失败日不被幂等短路：恢复后可正常重跑
    assert call(home, "run", "--date", D, provider=FakeProvider.synthetic(CODES, D)) == 0


# --------------------------------------------------------------------------- 退出码 30
def test_data_stale_returns_30_after_retries(tmp_path):
    home = make_home(tmp_path)
    fp = FakeProvider.synthetic(CODES, D)
    fp.available_until = "2026-11-02"                # 当日数据尚未发布
    sl = FakeSleep(fp)
    assert call(home, "run", "--date", D, provider=fp, sleep=sl) == C.EXIT_DATA_STALE
    assert sl.calls.count(600) == 3                   # 等 10 分钟 × 3 次
    assert not (home / "data/reports" / D).exists()


def test_stale_then_recovers(tmp_path):
    home = make_home(tmp_path)
    fp = FakeProvider.synthetic(CODES, D)
    fp.available_until = "2026-11-02"

    class Heal(FakeSleep):
        def __call__(self, s):
            super().__call__(s)
            if s == 600:
                fp.available_until = None            # 第一次等待后数据发布

    assert call(home, "run", "--date", D, provider=fp, sleep=Heal(fp)) == 0


def test_few_suspended_is_not_stale(tmp_path, capsys):
    home = make_home(tmp_path, codes=[f"6000{i:02d}" for i in range(1, 11)])
    codes = [f"6000{i:02d}" for i in range(1, 11)]
    fp = FakeProvider.synthetic(codes, D)
    fp.suspend("600001", "2026-10-30")               # 1/10 = 10% ≤ 20%
    assert call(home, "run", "--date", D, provider=fp) == 0
    out = capsys.readouterr().out
    assert "停牌 1" in out
    run = json.loads((home / f"data/runs/{D}/signals.json").read_text())
    assert run["fetch"]["suspended"] == ["600001"] and len(run["results"]) == 9


# --------------------------------------------------------------------------- 单只失败
def test_single_failure_does_not_break_run(tmp_path, capsys):
    codes = [f"6000{i:02d}" for i in range(1, 11)]
    home = make_home(tmp_path, codes=codes)
    fp = FakeProvider.synthetic(codes, D)
    fp.fail_codes["600003"] = "failed: 接口报错"
    assert call(home, "run", "--date", D, provider=fp) == 0
    assert "失败 1" in capsys.readouterr().out
    err = json.loads((home / f"data/runs/{D}/fetch_errors.json").read_text())
    assert err["failed"][0]["code"] == "600003"


# --------------------------------------------------------------------------- AC11
def test_replay_history_date(tmp_path, capsys):
    home = make_home(tmp_path)
    fp = FakeProvider.synthetic(CODES, "2026-11-03")
    call(home, "run", "--date", "2026-10-29", provider=fp, today="2026-11-03")
    out1 = capsys.readouterr().out
    assert "数据日期 2026-10-29" in out1
    # 同一历史日期，换一个“数据只到当日”的源，结果应逐位一致
    home2 = make_home(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path)
    fp2 = FakeProvider.synthetic(CODES, "2026-10-29")
    call(home2, "run", "--date", "2026-10-29", provider=fp2, today="2026-10-29")
    out2 = capsys.readouterr().out
    cs = lambda s: s.split("校验 ")[1].split("\n")[0]
    assert cs(out1) == cs(out2)


def test_future_date_is_config_error(tmp_path):
    home = make_home(tmp_path)
    assert call(home, "run", "--date", "2026-12-31", provider=FakeProvider.synthetic(CODES, D)) == C.EXIT_CONFIG_ERROR


# --------------------------------------------------------------------------- AC13
def test_interrupted_run_leaves_no_partial_and_rerun_works(tmp_path, monkeypatch):
    home = make_home(tmp_path)
    fp = FakeProvider.synthetic(CODES, D)
    import xb1lib.report as report

    real = report.atomic.write_with
    def boom(path, writer):
        if str(path).endswith(".xlsx"):
            raise KeyboardInterrupt("simulated kill")
        return real(path, writer)
    monkeypatch.setattr(report.atomic, "write_with", boom)
    with pytest.raises(KeyboardInterrupt):
        call(home, "run", "--date", D, provider=fp)
    monkeypatch.undo()
    files = all_files(home / "data")
    assert not [f for f in files if f.endswith(".tmp")]
    assert f"reports/{D}/b1_report.xlsx" not in files
    assert not (home / "data/state/run.lock").exists()                   # 锁在 finally 释放
    assert not State(__import__("xb1lib.paths", fromlist=["Paths"]).Paths(home, check=False)).is_success(D)
    assert call(home, "run", "--date", D, provider=fp) == 0              # 重跑正常完成
    assert (home / f"data/reports/{D}/b1_report.xlsx").exists()


def test_concurrent_lock_rejected(tmp_path, capsys):
    home = make_home(tmp_path)
    (home / "data/state").mkdir(parents=True)
    (home / "data/state/run.lock").write_text(json.dumps({"pid": os.getpid(), "ts": __import__("time").time()}))
    assert call(home, "run", "--date", D, provider=FakeProvider.synthetic(CODES, D)) == C.EXIT_OTHER
    assert "运行进行中" in capsys.readouterr().err


def test_stale_lock_is_cleared(tmp_path):
    home = make_home(tmp_path)
    (home / "data/state").mkdir(parents=True)
    (home / "data/state/run.lock").write_text(json.dumps({"pid": 99999999, "ts": 0}))
    assert call(home, "run", "--date", D, provider=FakeProvider.synthetic(CODES, D)) == 0


# --------------------------------------------------------------------------- 卷未挂载 / 配置
def test_volume_not_mounted_returns_50_and_creates_nothing(tmp_path, capsys):
    ghost = tmp_path / "no_such_volume" / "XB1"
    rc = xb1.main(["run", "--home", str(ghost)], provider=None, sleep=FakeSleep(), today=D)
    assert rc == C.EXIT_VOLUME_NOT_MOUNTED
    assert not ghost.exists() and not (tmp_path / "no_such_volume").exists()
    assert "XB1 退出码 50" in capsys.readouterr().err


def test_env_home_beats_cli_home(tmp_path, monkeypatch):
    from xb1lib.paths import resolve_home
    monkeypatch.setenv("XB1_HOME", str(tmp_path / "env"))
    assert resolve_home(str(tmp_path / "cli")) == (tmp_path / "env").resolve()
    monkeypatch.delenv("XB1_HOME")
    assert resolve_home(str(tmp_path / "cli")) == (tmp_path / "cli").resolve()
    assert resolve_home(None) == ROOT


def test_empty_universe_is_config_error(tmp_path):
    home = make_home(tmp_path, codes=[])
    assert call(home, "run", "--date", D, provider=FakeProvider.synthetic(CODES, D)) == C.EXIT_CONFIG_ERROR


def test_bad_config_is_40(tmp_path):
    home = make_home(tmp_path, fetch={"batch_size": 50})
    assert call(home, "run", "--date", D, provider=FakeProvider.synthetic(CODES, D)) == C.EXIT_CONFIG_ERROR


# --------------------------------------------------------------------------- 查询类子命令
def test_latest_list_explain_after_run(tmp_path, capsys):
    home = make_home(tmp_path)
    call(home, "run", "--date", D, provider=FakeProvider.synthetic(CODES, D))
    brief = capsys.readouterr().out
    assert call(home, "latest") == 0 and capsys.readouterr().out == brief
    assert call(home, "latest", "--format", "json") == 0
    j = json.loads(capsys.readouterr().out)
    assert j["date"] == D and "signals" in j and "results" not in j
    assert call(home, "list", "warn") == 0 and "预警" in capsys.readouterr().out
    assert call(home, "explain", "sz600001") == 0 and "【解释】600001" in capsys.readouterr().out
    assert call(home, "explain", "123456") == C.EXIT_OTHER
    assert call(home, "explain", "abc") == C.EXIT_CONFIG_ERROR


def test_latest_without_any_run(tmp_path):
    assert call(make_home(tmp_path), "latest") == C.EXIT_OTHER


def test_html_page_generated_on_run_and_on_demand(tmp_path, capsys):
    home = make_home(tmp_path, universe={"groups": {"测试组": {"stocks": CODES}}})
    call(home, "run", "--date", D, provider=FakeProvider.synthetic(CODES, D))
    capsys.readouterr()
    page = home / f"data/reports/{D}/b1_cards.html"
    assert page.exists()
    text = page.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in text and "K线卡片" in text and "免责声明" in text

    page.unlink()                                  # 幂等重跑时补生成缺失的 K线卡片
    assert call(home, "run", "--date", D) == 0 and page.exists()
    capsys.readouterr()

    assert call(home, "kline", "--date", D) == 0
    assert "K线卡片：" in capsys.readouterr().out
    assert (home / f"data/reports/{D}/kline_cards.html").exists()
    assert call(home, "kline", "--date", D, "--signals", "--group", "测试组") == 0
    assert (home / f"data/reports/{D}/b1_cards-分组-测试组.html").exists()
    assert call(home, "kline", "--date", D, "--group", "测试组", "--all") == 0
    all_page = home / f"data/reports/{D}/kline_cards-分组-测试组.html"
    assert all_page.exists()
    assert "全部股票" in all_page.read_text(encoding="utf-8")
    assert call(home, "html", "--date", D, "--signals") == 0          # html 为兼容别名
    assert call(home, "kline", "--date", D, "--signals", "--days", "30",
                "--out", str(home / "d30.html")) == 0
    assert "近 30 个交易日" in (home / "d30.html").read_text(encoding="utf-8")
    assert call(home, "kline", "--group", "不存在") == C.EXIT_CONFIG_ERROR
    assert call(home, "html", "--date", "1999-01-01") == C.EXIT_OTHER


def test_profile_is_manual_only(tmp_path, capsys):
    """run 从不联网取资料（增量拉取不碰 profile）；只有 profile update 才会取。"""
    home = make_home(tmp_path, universe={"groups": {"测试组": {"stocks": CODES}}})
    prov = FakeProvider.synthetic(CODES, D)
    assert call(home, "run", "--date", D, provider=prov) == 0
    assert prov.profile_calls == []
    capsys.readouterr()
    assert call(home, "profile", "update", "--codes", ",".join(CODES), provider=prov) == 0
    assert prov.profile_calls and "资料更新" in capsys.readouterr().out
    assert call(home, "profile", "update", "--codes", "12345x") == C.EXIT_CONFIG_ERROR


def test_config_get_set_and_hash_change(tmp_path, capsys):
    home = make_home(tmp_path)
    assert call(home, "config", "get", "thresholds.j_trigger") == 0
    assert "13" in capsys.readouterr().out
    assert call(home, "config", "set", "thresholds.j_trigger", "15") == 0
    assert yaml.safe_load((home / "config.yaml").read_text())["thresholds"]["j_trigger"] == 15
    assert (home / "config.yaml").read_text().count("universe") == 1       # 不丢原有覆盖项
    assert call(home, "config", "set", "nope.key", "1") == C.EXIT_CONFIG_ERROR
    assert call(home, "config", "set", "fetch.batch_size", "99") == C.EXIT_CONFIG_ERROR
    capsys.readouterr()
    # 改阈值后 config_hash 变化，--force 重算后新哈希写入 meta
    call(home, "run", "--date", D, provider=FakeProvider.synthetic(CODES, D))
    h1 = json.loads((home / f"data/runs/{D}/meta.json").read_text())["config_hash"]
    call(home, "config", "set", "thresholds.j_trigger", "10")
    call(home, "run", "--date", D, "--force", provider=FakeProvider.synthetic(CODES, D))
    assert json.loads((home / f"data/runs/{D}/meta.json").read_text())["config_hash"] != h1


def test_history_accumulates_and_backfill(tmp_path, capsys):
    home = make_home(tmp_path)
    fp = FakeProvider.synthetic(CODES, D)
    call(home, "run", "--date", "2026-10-26", provider=fp)
    call(home, "run", "--date", D, provider=fp)
    capsys.readouterr()
    assert call(home, "backfill") == 0
    assert "【B1 回填】" in capsys.readouterr().out


def test_gc_removes_old_runs_but_not_history(tmp_path, capsys):
    home = make_home(tmp_path)
    call(home, "run", "--date", D, provider=FakeProvider.synthetic(CODES, D))
    old = home / "data/runs/2020-01-01"; old.mkdir()
    (old / "brief.md").write_text("x")
    (home / "data/logs/run-2020-01-01.log").write_text("x")
    hist = home / "data/history/signals.parquet"
    existed = hist.exists()
    capsys.readouterr()
    assert call(home, "gc") == 0
    assert not old.exists() and not (home / "data/logs/run-2020-01-01.log").exists()
    assert (home / f"data/runs/{D}").exists() and hist.exists() == existed


def test_doctor(tmp_path, capsys):
    home = make_home(tmp_path)
    assert call(home, "doctor", provider=FakeProvider.synthetic(CODES, D)) == 0
    out = capsys.readouterr().out
    assert "数据源 fake" in out and out.count("\n") <= 10
    # 真实 WestockProvider + 命令不存在 -> 退出码 20，并给出 setup.sh 指引
    home2 = make_home(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path,
                      fetch={"command": "definitely-not-westock-xyz"})
    assert call(home2, "doctor") == C.EXIT_SOURCE_UNAVAILABLE
    assert "setup.sh" in capsys.readouterr().out


def test_west_missing_command_gives_exit_20(tmp_path, capsys):
    """真实 WestockProvider + 不存在的命令 -> 退出码 20，不留残缺产物（AC7 的真实路径）。"""
    home = make_home(tmp_path, fetch={"command": "definitely-not-westock-xyz"})
    rc = call(home, "run", "--date", D, provider=None)
    assert rc == C.EXIT_SOURCE_UNAVAILABLE
    assert not (home / "data/reports" / D).exists()
