"""market.py：0AMV（活跃市值）复刻序列 + 四区域判别 + 卡片页标签页。"""
from __future__ import annotations

import pandas as pd
import pytest

from fakes import make_kline_df
from xb1lib import market
from xb1lib.paths import Paths

D = "2026-11-03"
COLS = ["date", "amv", "c5", "c13", "c34", "inf", "amount", "turnover", "n"]


def _cfg():
    return {"fetch": {"amount_unit": 1}}


def _seed(tmp_path, codes=("600519", "000001", "300750")):
    paths = Paths(home=tmp_path, check=False)
    paths.ensure_dirs()
    for c in codes:
        make_kline_df(c, D, 260).to_parquet(paths.kline(c), index=False)
    return paths


def _row(amv, c5, c13):
    return {"amv": amv, "c5": c5, "c13": c13}


def test_series_shape_and_scale(tmp_path):
    paths = _seed(tmp_path)
    s = market.compute_series(paths, _cfg(), days=60)
    assert list(s.columns) == COLS
    assert len(s) == 60 and s["date"].is_monotonic_increasing
    assert (s["amv"] > 0).all() and s["n"].iloc[-1] == 3
    # 全市场换手率是百分数量级（0~20%），不是 0.0001
    assert 0 < float(s["turnover"].iloc[-1]) < 0.2


def test_cost_lines_are_finite_and_fan_out(tmp_path):
    paths = _seed(tmp_path)
    s = market.compute_series(paths, _cfg(), days=120)
    last = s.iloc[-1]
    for col in ("c5", "c13", "c34", "inf"):
        assert pd.notna(last[col]) and last[col] > 0
    # 加权不同 -> 长周期线更平滑，与短周期线不应完全相等
    assert float(last["c5"]) != float(last["inf"])


def test_stale_files_are_excluded(tmp_path):
    paths = _seed(tmp_path)
    make_kline_df("600000", "2026-10-30", 100).to_parquet(paths.kline("600000"), index=False)
    s = market.compute_series(paths, _cfg(), days=10)
    assert s["n"].iloc[-1] == 3


@pytest.mark.parametrize("row,expected", [
    (_row(1.20, 1.10, 1.00), "up"),
    (_row(1.05, 1.00, 1.10), "up_hesitate"),
    (_row(0.95, 1.00, 1.10), "down"),            # 0AMV<C5 且 C5<C13
    (_row(0.90, 1.00, 0.95), "down_hesitate"),   # 0AMV<C5 但 C5>C13
])
def test_region_rules(row, expected):
    key, name, rule, action = market.region_at(row)
    assert key == expected and name == market.REGIONS[expected][0] and action


def test_summarize_from_series(tmp_path):
    paths = _seed(tmp_path)
    s = market.compute_series(paths, _cfg(), days=80)
    m = market.summarize(s)
    assert m["date"] == D and m["region"] in market.REGIONS
    assert m["region_name"] == market.REGIONS[m["region"]][0]
    assert m["amv"] > 0 and m["n"] == 3
    assert market.summarize(pd.DataFrame()) == {}


def test_cache_reused_and_invalidated(tmp_path):
    paths = _seed(tmp_path)
    a = market.load_or_build(paths, _cfg(), kline_latest=D)
    assert market.cache_path(paths).exists()
    b = market.load_or_build(paths, _cfg(), kline_latest=D)
    pd.testing.assert_frame_equal(a, b)
    c = market.load_or_build(paths, _cfg(), kline_latest="2027-01-01")   # 要求更新 -> 重算
    assert c["date"].iloc[-1] == D


def test_cards_page_has_amv_tab(tmp_path):
    from helpers_render import make_cfg, make_run
    from xb1lib import cards
    paths = _seed(tmp_path)
    run = make_run(standard=1, date=D)
    s = market.compute_series(paths, _cfg(), days=40)
    html = cards.build_html(run, make_cfg(), frames=None, paths=paths, market_series=s)
    assert 'data-tab="cards"' in html and 'data-tab="market"' in html
    assert 'id="panel-market"' in html
    assert "活跃市值 0AMV" in html and "成本均线" in html
    assert "复刻" in html and "不是指南针原值" in html
    assert html.count('fill="#f6465d"') + html.count('fill="#2ebd85"') > 0     # STICKLINE 柱


def test_cards_page_without_series_shows_placeholder(tmp_path):
    from helpers_render import make_cfg, make_run
    from xb1lib import cards
    paths = Paths(home=tmp_path, check=False)
    paths.ensure_dirs()
    html = cards.build_html(make_run(date=D), make_cfg(), paths=paths, market_series=None)
    assert "暂无数据" in html
