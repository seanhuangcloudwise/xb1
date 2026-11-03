"""overview.py：本地数据统计页（只读、自包含）。"""
from __future__ import annotations

from fakes import make_kline_df
from xb1lib import overview
from xb1lib.paths import Paths

D = "2026-11-03"


def _cfg():
    return {"fetch": {"amount_unit": 1, "kline_days": 250}}


def _seed(tmp_path):
    paths = Paths(home=tmp_path, check=False)
    paths.ensure_dirs()
    for code in ("600519", "000001", "300750", "688981"):
        make_kline_df(code, D, 260).to_parquet(paths.kline(code), index=False)
    make_kline_df("601398", "2026-10-30", 100).to_parquet(paths.kline("601398"), index=False)   # 落后且历史短
    (paths.kline_dir / "bad.parquet").write_bytes(b"not parquet")
    return paths


def test_collect_counts_boards_lag_and_broken(tmp_path):
    paths = _seed(tmp_path)
    s = overview.collect(paths, _cfg())
    assert s["stocks"] == 5 and s["broken"] == ["bad"]
    assert s["latest"] == D and s["earliest"] is not None
    assert s["boards"]["main"] == 3 and s["boards"]["gem"] == 1 and s["boards"]["star"] == 1
    assert s["lagging"] == ["601398"]
    assert s["short"] == 1
    assert s["today"]["n"] == 4 and s["today"]["up"] + s["today"]["down"] + s["today"]["flat"] == 4


def test_html_is_self_contained_and_escapes(tmp_path):
    paths = _seed(tmp_path)
    out = overview.write_overview(paths, _cfg())
    assert out == paths.reports / "overview.html" and out.exists()
    html = out.read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>") and "数据概览" in html
    assert "<link" not in html and " src=" not in html and "http://" not in html and "https://" not in html
    assert "601398" in html and "末根日期落后" in html and "无法读取的 K 线文件" in html


def test_empty_home_renders_without_error(tmp_path):
    paths = Paths(home=tmp_path, check=False)
    paths.ensure_dirs()
    s = overview.collect(paths, _cfg())
    assert s["stocks"] == 0 and s["latest"] is None
    html = overview.build_html(paths, _cfg(), stats=s)
    assert "暂无数据" in html


def test_load_run_rebases_paths_after_moving_data(tmp_path):
    """把 data/ 拷到别处后，摘要里的路径应指向新位置而不是原机器路径。"""
    from xb1lib import pipeline
    from xb1lib import atomic

    home = tmp_path / "newhome"
    paths = Paths(home=home, check=False)
    paths.ensure_dirs()
    run = {"date": D, "results": [], "checksum": "x",
           "report_xlsx_path": "/old/place/data/reports/2026-11-03/b1_report.xlsx",
           "report_md_path": "/old/place/data/reports/2026-11-03/b1_report.md",
           "report_html_path": "/old/place/data/reports/2026-11-03/b1_cards.html"}
    atomic.write_json(paths.signals_json(D), run)
    got = pipeline.load_run(paths, D)
    assert got["report_xlsx_path"] == str(paths.report_xlsx(D))
    assert got["report_md_path"] == str(paths.report_md(D))
    assert got["report_html_path"] == str(paths.report_html(D))
    assert "/old/place" not in got["report_html_path"]
