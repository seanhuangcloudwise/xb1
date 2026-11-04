"""market.py：活跃市值代理序列 + 卡片页标签页。"""
from __future__ import annotations

import pandas as pd
import pytest

from fakes import make_kline_df
from xb1lib import market
from xb1lib.paths import Paths

D = "2026-11-03"


def _cfg(floor=1e8):
    return {"fetch": {"amount_unit": 1}, "market": {"active_amount_floor": floor}}


def _seed(tmp_path, codes=("600519", "000001", "300750")):
    paths = Paths(home=tmp_path, check=False)
    paths.ensure_dirs()
    for c in codes:
        make_kline_df(c, D, 260).to_parquet(paths.kline(c), index=False)
    return paths


def test_series_columns_and_tail(tmp_path):
    paths = _seed(tmp_path)
    s = market.compute_series(paths, _cfg(), days=60)
    assert list(s.columns) == ["date", "active_mv", "total_mv", "n_active", "n_total", "amount"]
    assert len(s) == 60 and s["date"].is_monotonic_increasing
    assert s["active_mv"].iloc[-1] > 0 and s["n_total"].iloc[-1] == 3


def test_floor_filters_active_names(tmp_path):
    paths = _seed(tmp_path)
    all_s = market.compute_series(paths, _cfg(floor=0), days=30)
    big_s = market.compute_series(paths, _cfg(floor=1e12), days=30)
    assert all_s["n_active"].iloc[-1] == 3
    assert big_s["n_active"].iloc[-1] == 0
    active = big_s["active_mv"].iloc[-1]
    assert active == 0 or pd.isna(active)


def test_stale_files_are_excluded(tmp_path):
    """数据没更到最新交易日的旧文件不参与计算，避免口径跳变。"""
    paths = _seed(tmp_path)
    make_kline_df("600000", "2026-10-30", 100).to_parquet(paths.kline("600000"), index=False)
    s = market.compute_series(paths, _cfg(), days=10)
    assert s["n_total"].iloc[-1] == 3          # 600000 被排除


def test_summarize_and_zone():
    s = pd.DataFrame({"date": ["2026-10-30", "2026-11-02", "2026-11-03", "2026-11-04"],
                      "active_mv": [100.0, 100.0, 103.0, 108.0], "total_mv": [120.0] * 4,
                      "n_active": [10] * 4, "n_total": [12] * 4, "amount": [1e12] * 4})
    m = market.summarize(s)
    assert round(m["chg"], 2) == 4.85
    assert m["up_days"] == 2          # 100 -> 100 不算上涨，连阳在此断开
    assert m["ratio"] == pytest.approx(108 / 120)
    assert market.zone(5.0, None)[0] == "bull"
    assert market.zone(-3.0, None)[0] == "bear"
    assert market.zone(0.5, 0.5)[0] == "neutral"
    assert market.zone(None, None)[0] == "unknown"


def test_cache_reused_and_invalidated(tmp_path):
    paths = _seed(tmp_path)
    a = market.load_or_build(paths, _cfg(), kline_latest=D)
    assert market.cache_path(paths).exists()
    b = market.load_or_build(paths, _cfg(), kline_latest=D)
    pd.testing.assert_frame_equal(a, b)
    c = market.load_or_build(paths, _cfg(), kline_latest="2027-01-01")   # 要求更新 -> 重算
    assert c["date"].iloc[-1] == D


def test_cards_page_has_market_tab(tmp_path):
    from helpers_render import make_cfg, make_run
    from xb1lib import cards
    paths = _seed(tmp_path)
    run = make_run(standard=1, date=D)
    s = market.compute_series(paths, _cfg(), days=40)
    html = cards.build_html(run, make_cfg(), frames=None, paths=paths, market_series=s)
    assert 'data-tab="cards"' in html and 'data-tab="market"' in html
    assert 'id="panel-market"' in html
    assert "活跃市值（代理指标）" in html and "代理值" in html
    assert "<svg" in html


def test_cards_page_without_series_shows_placeholder(tmp_path):
    from helpers_render import make_cfg, make_run
    from xb1lib import cards
    paths = Paths(home=tmp_path, check=False)
    paths.ensure_dirs()
    html = cards.build_html(make_run(date=D), make_cfg(), paths=paths, market_series=None)
    assert "暂无数据" in html
