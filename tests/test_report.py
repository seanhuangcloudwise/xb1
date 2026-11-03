"""report.py 测试：Markdown、xlsx 结构、原子写、latest 软链。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from openpyxl import load_workbook

from helpers_render import make_cfg, make_run
from xb1lib import report
from xb1lib.paths import Paths


@pytest.fixture
def paths(tmp_path):
    return Paths(home=tmp_path, check=False)


def _run(paths, **kw):
    kw.setdefault("home", str(paths.home))
    return make_run(**kw)


def test_build_markdown_sections():
    run = make_run(confirm=1, perfect=1, standard=2, watch=2, none=3, rope_break=1, below_bbi=1, failed=1, suspended=1)
    md = report.build_markdown(run, make_cfg())
    for s in ("# B1 复盘日报 2026-11-03（周二）", "## 🔥 B1 确认（1）", "## ⭐ 完美 B1（1）", "## 标准 B1（2）",
              "## 观察（2）", "## 预警", "## 全清单趋势", "## 数据异常", "配置哈希：abcd1234", "免责声明"):
        assert s in md, s
    assert "另有" not in md  # 完整报告不截断
    assert "| 3000" in md and "停牌" in md


def test_build_markdown_not_truncated_by_top_n():
    run = make_run(standard=15)
    md = report.build_markdown(run, make_cfg(top_n=3))
    sec = md.split("## 标准 B1（15）")[1].split("## ")[0]
    assert sec.count("| 标准") == 15


def test_build_markdown_empty():
    md = report.build_markdown(make_run(none=2), make_cfg())
    assert "今日无 B1 信号" in md and "## 预警\n\n无" in md and "## 数据异常\n\n无" in md


def test_write_reports_xlsx_structure(paths):
    run = _run(paths, confirm=1, perfect=1, standard=2, watch=2, none=3, rope_break=1, below_bbi=1,
               failed=1, suspended=2, stale=1)
    out = report.write_reports(paths, run, make_cfg())
    assert out["xlsx"] == paths.report_xlsx("2026-11-03") and out["xlsx"].exists()
    assert out["md"] == paths.report_md("2026-11-03") and out["md"].exists()

    wb = load_workbook(out["xlsx"])
    assert wb.sheetnames == ["总览", "B1信号", "全清单", "预警", "数据异常"]
    n = len(run["results"])
    assert wb["B1信号"].max_row == 1 + 6          # 1+1+2+2
    assert wb["全清单"].max_row == 1 + n
    assert wb["预警"].max_row == 1 + 2
    assert wb["数据异常"].max_row == 1 + 1 + 2 + 1
    for ws in wb.worksheets:
        assert ws.freeze_panes == "A2"
        assert all(c.font.bold for c in ws[1] if c.value is not None)
    sig = wb["B1信号"]
    header = [c.value for c in sig[1]]
    assert header[:5] == ["代码", "名称", "等级", "得分", "收盘"]
    assert sig.cell(row=2, column=3).value == "B1确认"       # confirm 排最前
    assert sig.cell(row=2, column=5).number_format == "0.00"
    assert isinstance(sig.cell(row=2, column=5).value, (int, float))
    assert sig.column_dimensions["B"].width >= 8
    ov = {r[0].value: r[1].value for r in wb["总览"].iter_rows(min_row=2)}
    assert ov["数据日期"] == "2026-11-03" and ov["校验码"] == run["checksum"] and ov["B1确认"] == 1


def test_write_reports_empty(paths):
    run = _run(paths, none=2)
    run["fetch"] = {}
    out = report.write_reports(paths, run, make_cfg())
    wb = load_workbook(out["xlsx"])
    assert wb["B1信号"].max_row == 1 and wb["预警"].max_row == 1 and wb["数据异常"].max_row == 1


def test_write_reports_overwrites(paths):
    run = _run(paths, standard=1)
    report.write_reports(paths, run, make_cfg())
    run2 = _run(paths, standard=3)
    out = report.write_reports(paths, run2, make_cfg())
    assert load_workbook(out["xlsx"])["B1信号"].max_row == 4


def _no_tmp(d: Path):
    return [p for p in d.iterdir() if p.name.endswith(".tmp")]


def test_atomic_md_failure_leaves_no_file(paths, monkeypatch):
    run = _run(paths, standard=1)

    def boom(run_, cfg_):
        raise RuntimeError("中途异常")

    # md 在 xlsx 之后写；先让 xlsx 之前就失败的场景：build_markdown 本身在写入前完成，改用 atomic 层注入
    from xb1lib import atomic

    orig = atomic.write_bytes

    def failing_write_bytes(path, data):
        # 模拟写入中途异常：先写一半临时文件，再抛错
        with atomic._tmp_target(path) as t:
            Path(t).write_bytes(data[: len(data) // 2])
            raise OSError("disk full")

    monkeypatch.setattr(atomic, "write_bytes", failing_write_bytes)
    with pytest.raises(OSError):
        report.write_reports(paths, run, make_cfg())
    d = paths.report_dir("2026-11-03")
    assert not paths.report_md("2026-11-03").exists()
    assert _no_tmp(d) == []


def test_atomic_xlsx_failure_leaves_no_file(paths, monkeypatch):
    run = _run(paths, standard=1)
    import openpyxl.workbook.workbook as wbmod

    def bad_save(self, filename):
        Path(filename).write_bytes(b"PK partial")
        raise OSError("写入中断")

    monkeypatch.setattr(wbmod.Workbook, "save", bad_save)
    with pytest.raises(OSError):
        report.write_reports(paths, run, make_cfg())
    d = paths.report_dir("2026-11-03")
    assert not paths.report_xlsx("2026-11-03").exists()
    assert not paths.report_md("2026-11-03").exists()
    assert _no_tmp(d) == []


def test_atomic_failure_keeps_previous_file(paths, monkeypatch):
    run = _run(paths, standard=1)
    report.write_reports(paths, run, make_cfg())
    before = paths.report_xlsx("2026-11-03").read_bytes()
    import openpyxl.workbook.workbook as wbmod

    def bad_save(self, filename):
        Path(filename).write_bytes(b"garbage")
        raise OSError("x")

    monkeypatch.setattr(wbmod.Workbook, "save", bad_save)
    with pytest.raises(OSError):
        report.write_reports(paths, _run(paths, standard=5), make_cfg())
    assert paths.report_xlsx("2026-11-03").read_bytes() == before
    assert _no_tmp(paths.report_dir("2026-11-03")) == []


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlink")
def test_latest_symlink_updated(paths):
    try:
        probe = paths.home / "probe"
        os.symlink("x", probe)
        probe.unlink()
    except (OSError, NotImplementedError):
        pytest.skip("环境不支持软链")
    report.write_reports(paths, _run(paths, standard=1, date="2026-11-02"), make_cfg())
    latest = paths.reports_latest
    assert latest.is_symlink() and os.readlink(latest) == "2026-11-02"
    report.write_reports(paths, _run(paths, standard=1, date="2026-11-03"), make_cfg())
    assert os.readlink(latest) == "2026-11-03"
    assert (latest / "b1_report.md").exists()
    assert [p for p in paths.reports.iterdir() if p.name.startswith(".latest")] == []


def test_symlink_failure_is_silent(paths, monkeypatch):
    def nope(*a, **k):
        raise OSError("symlink not permitted")

    monkeypatch.setattr(os, "symlink", nope)
    out = report.write_reports(paths, _run(paths, standard=1), make_cfg())
    assert out["latest_linked"] is False
    assert paths.report_xlsx("2026-11-03").exists()
    assert [p for p in paths.reports.iterdir() if p.name.startswith(".latest")] == []
