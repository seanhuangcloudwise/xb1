"""cards.py 测试：自包含卡片页、K 线 SVG、筛选/转义、缓存回读与文件落盘。"""
from __future__ import annotations

import re

import pytest

from fakes import make_kline_df
from helpers_render import make_cfg, make_run
from xb1lib import cards, constants as C
from xb1lib.indicators import compute_indicators
from xb1lib.paths import Paths

D = "2026-11-03"


def frames_for(run, days: int = 200) -> dict:
    """按 run 里的代码造指标 DataFrame，并把末行数值回填到结果里（贴近真实 run）。"""
    out = {}
    for r in run["results"]:
        df = compute_indicators(make_kline_df(r["code"], run["date"], days))
        out[r["code"]] = df
        last = df.iloc[-1]
        r["close"] = round(float(last["close"]), 2)
        r["j"] = round(float(last["j"]), 1)
        r["white"] = round(float(last["white"]), 2)
        r["yellow"] = round(float(last["yellow"]), 2)
        r["bbi"] = round(float(last["bbi"]), 2)
        if r["level"] in (C.LEVEL_CONFIRM, C.LEVEL_PERFECT, C.LEVEL_STANDARD):
            stop = round(float(df["low"].tail(12).min()) * 0.99, 2)
            r["stop_price"] = stop
            r["stop_pct"] = round((r["close"] - stop) / r["close"] * 100, 2)
        if r["level"] == C.LEVEL_CONFIRM:
            r["b1_date"] = str(df.iloc[-3]["date"])
    return out


def make_page(**kw):
    run = make_run(date=D, **kw)
    frames = frames_for(run)
    cfg = make_cfg()
    cfg["report"]["cards"] = {"days": 60, "include_watch": True}
    return run, frames, cards.build_html(run, cfg, frames=frames)


# --------------------------------------------------------------------------- 页面结构
def test_page_has_one_card_per_signal_with_svg():
    run, _, html = make_page(confirm=1, perfect=1, standard=2, watch=2, none=3, rope_break=1)
    assert html.startswith("<!DOCTYPE html>")
    assert f"K线卡片 {D}" in html
    assert "仅 B1 信号" in html
    assert run["checksum"] in html
    assert "免责声明：仅供复盘参考，不构成投资建议。" in html
    codes = [r["code"] for r in run["results"] if r["level"] != C.LEVEL_NONE]
    for code in codes:
        assert f'data-code="{code}"' in html
    assert html.count('class="card ') == 6
    assert html.count("<svg") == 6
    assert "data-level=\"confirm\"" in html and "data-level=\"watch\"" in html
    # 每根 K 线都有原生悬浮提示
    assert len(re.findall(r"<title>\d{4}-\d{2}-\d{2} 开", html)) == 6 * 60
    # 完全自包含：没有外部资源
    assert "<link" not in html and " src=" not in html and "http://" not in html and "https://" not in html


def test_confirm_card_marks_b1_date_and_stop():
    run, _, html = make_page(confirm=1)
    r = next(x for x in run["results"] if x["level"] == C.LEVEL_CONFIRM)
    assert f"B1 {r['b1_date'][5:]}" in html
    assert f"止损 {r['stop_price']:.2f}" in html
    assert f'data-b1-date="{r["b1_date"]}"' in html
    assert '<span class="badge">B1确认</span>' in html
    assert '<span class="badge">完美B1</span>' not in html


def test_watch_can_be_excluded():
    run = make_run(standard=1, watch=2, date=D)
    cfg = make_cfg()
    cfg["report"]["cards"] = {"include_watch": False}
    html = cards.build_html(run, cfg, frames=frames_for(run))
    assert 'data-level="watch"' not in html
    assert 'data-level="standard"' in html


def test_empty_page_and_missing_frames():
    run = make_run(none=3, date=D)
    html = cards.build_html(run, make_cfg())
    assert "今日无 B1 信号" in html
    assert "data-code=" not in html

    run2 = make_run(standard=1, date=D)
    html2 = cards.build_html(run2, make_cfg())      # 无 frames、无 paths
    assert "暂无本地 K 线数据" in html2


def test_names_and_hits_are_escaped():
    run = make_run(standard=1, date=D)
    r = run["results"][0]
    r["name"] = '<script>alert("x")</script>'
    r["hits"] = ['<img src=x onerror="alert(1)">', "J<-5"]
    html = cards.build_html(run, make_cfg(), frames=frames_for(run))
    assert '<script>alert("x")</script>' not in html
    assert "&lt;script&gt;alert" in html
    assert '<img src=x' not in html
    assert "&lt;img" in html


def test_days_window_limits_candles():
    run = make_run(standard=1, date=D)
    frames = frames_for(run, days=200)
    cfg = make_cfg()
    cfg["report"]["cards"] = {"days": 30}
    html = cards.build_html(run, cfg, frames=frames)
    assert len(re.findall(r"<title>\d{4}-\d{2}-\d{2} 开", html)) == 30
    assert "近 30 个交易日" in html


# --------------------------------------------------------------------------- 落盘
@pytest.fixture
def paths(tmp_path):
    return Paths(home=tmp_path, check=False)


def test_write_cards_falls_back_to_cache(paths):
    run = make_run(standard=1, date=D)
    frames = frames_for(run)
    code = run["results"][0]["code"]
    paths.ensure_dirs()
    frames[code].to_parquet(paths.indicators(code), index=False)

    out = cards.write_cards(paths, run, make_cfg())
    assert out == paths.report_html(D) and out.exists()
    html = out.read_text(encoding="utf-8")
    assert "<svg" in html and f'data-code="{code}"' in html


def test_write_html_custom_target(paths):
    run = make_run(standard=1, date=D)
    out = paths.home / "sub" / "mine.html"
    cards.write_html(out, run, make_cfg(), frames=frames_for(run))
    assert out.exists() and "<!DOCTYPE html>" in out.read_text(encoding="utf-8")


def test_write_cards_uses_raw_kline_when_indicator_cache_is_stale(paths):
    run = make_run(standard=1, date=D)
    frames = frames_for(run)
    code = run["results"][0]["code"]
    paths.ensure_dirs()
    frames[code].iloc[:-10].to_parquet(paths.indicators(code), index=False)   # 缓存末行早于复盘日
    make_kline_df(code, D, 300).to_parquet(paths.kline(code), index=False)

    out = cards.write_cards(paths, run, make_cfg())
    html = out.read_text(encoding="utf-8")
    assert f"<title>{D} 开" in html
    assert "<svg" in html


def test_build_does_not_mutate_checksum():
    run, frames, _ = make_page(standard=2)
    before = run["checksum"]
    cards.build_html(run, make_cfg(), frames=frames)
    assert run["checksum"] == before


# --------------------------------------------------------------------------- 全部股票视图（include_all）
def test_all_mode_includes_none_level_stocks():
    run = make_run(confirm=1, standard=1, none=3, date=D)
    html = cards.build_html(run, make_cfg(), frames=frames_for(run), include_all=True)
    assert f"K线卡片 {D}" in html
    assert "全部股票" in html
    assert html.count('class="card ') == 5
    assert html.count("<svg") == 5
    assert 'data-level="none"' in html
    assert '<span class="badge">无信号</span>' in html
    assert "今日无 B1 信号" not in html


def test_all_mode_orders_by_level_then_pool_order():
    run = make_run(none=2, standard=1, confirm=1, date=D)
    run["members"] = {r["code"]: {"groups": ["g"], "tags": []} for r in reversed(run["results"])}
    html = cards.build_html(run, make_cfg(), frames=frames_for(run), include_all=True)
    levels = re.findall(r'data-level="([a-z]+)"', html)
    assert levels == ["confirm", "standard", "none", "none"]


def test_all_mode_empty_when_no_results():
    run = make_run(date=D)
    html = cards.build_html(run, make_cfg(), include_all=True)
    assert "没有可展示的股票" in html
