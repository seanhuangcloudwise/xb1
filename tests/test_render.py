"""render.py 测试：校验码、brief 固定格式（golden）、top_n 截断、空信号、list/explain。"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from helpers_render import make_cfg, make_result, make_run
from xb1lib import constants as C
from xb1lib import render

GOLDEN = Path(__file__).parent / "fixtures" / "brief_golden.md"


def golden_run() -> dict:
    return make_run(confirm=1, perfect=1, standard=2, watch=2, rope_break=1, below_bbi=1, suspended=1,
                    home="/XB1")


# ---------------------------------------------------------------- checksum
def test_checksum_stable_and_format():
    run = golden_run()
    a = render.compute_checksum(run["results"])
    assert a == render.compute_checksum(copy.deepcopy(run["results"]))
    assert len(a) == 6 and int(a, 16) >= 0
    # 与入参顺序无关
    assert a == render.compute_checksum(list(reversed(run["results"])))


@pytest.mark.parametrize("key,delta", [("close", 0.01), ("j", 0.001), ("score", 1), ("stop_price", 0.01)])
def test_checksum_sensitive_to_any_number(key, delta):
    run = golden_run()
    base = render.compute_checksum(run["results"])
    r = copy.deepcopy(run["results"])
    tgt = next(x for x in r if x["level"] == C.LEVEL_STANDARD)
    tgt[key] = tgt[key] + delta
    assert render.compute_checksum(r) != base


def test_checksum_sensitive_to_nested_and_level():
    run = golden_run()
    base = render.compute_checksum(run["results"])
    r = copy.deepcopy(run["results"])
    r[0]["q"]["Q3"] = 0
    assert render.compute_checksum(r) != base
    r = copy.deepcopy(run["results"])
    r[0]["level"] = C.LEVEL_WATCH
    assert render.compute_checksum(r) != base


def test_checksum_handles_nan_and_none():
    r = [make_result("600001", "甲", C.LEVEL_NONE)]
    r[0]["j"] = float("nan")
    assert len(render.compute_checksum(r)) == 6
    assert len(render.compute_checksum([])) == 6


# ---------------------------------------------------------------- brief
def test_brief_golden():
    text = render.render_brief(golden_run(), make_cfg())
    assert GOLDEN.exists(), "缺少 golden 文件"
    assert text == GOLDEN.read_text(encoding="utf-8")


def test_brief_header_and_sections_order():
    run = make_run(confirm=2, perfect=1, standard=3, watch=3, rope_break=2, below_bbi=1)
    text = render.render_brief(run, make_cfg())
    lines = text.splitlines()
    assert lines[0].startswith("【B1 复盘】数据日期 2026-11-03（周二）｜股票池 ")
    assert f"校验 {run['checksum']}" in lines[0]
    assert lines[1] == "结论：B1确认 2 ｜ 完美B1 1 ｜ 标准B1 3 ｜ 观察 3 ｜ 预警 3"
    pos = [text.index(s) for s in ("🔥 B1 确认", "⭐ 完美 B1", "标准 B1（", "观察（", "⚠️ 预警", "数据异常：", "完整报告：", "K线卡片：", "免责声明：")]
    assert pos == sorted(pos)
    assert render.TABLE_HEADER in text
    assert text.rstrip().endswith("免责声明：仅供复盘参考，不构成投资建议。")


def test_brief_sorting_score_desc_and_watch_j_asc():
    run = make_run(standard=4, watch=4)
    text = render.render_brief(run, make_cfg())
    std = [l for l in text.splitlines() if l.startswith("| 6000") and "标准" in l]
    scores = [int(l.split("|")[3]) for l in std]
    assert scores == sorted(scores, reverse=True) and len(scores) == 4
    watch = [l for l in text.splitlines() if l.startswith("| 6000") and "观察" in l]
    js = [float(l.split("|")[5]) for l in watch]
    assert js == sorted(js) and len(js) == 4


def test_brief_number_formats():
    r = make_result("300001", "测试", C.LEVEL_STANDARD, score=71, j=-5.5555, close=12.3456, board="gem")
    r["stop_price"], r["stop_pct"] = 11.1111, 9.0909
    r["stop_wide"] = True
    run = make_run()
    run["results"] = [r]
    text = render.render_brief(run, make_cfg())
    assert "| 300001 | 测试 | 71 | 12.35 | -5.6 | 11.11 | 9.1% 止损过宽 |" in text


def test_brief_top_n_truncation():
    run = make_run(standard=13, watch=3)
    text = render.render_brief(run, make_cfg(top_n=10))
    assert text.count("| 标准") == 10
    assert "另有 3 只，见 list 命令" in text
    assert "标准 B1（13）" in text
    assert "观察" in text and text.count("另有") == 1  # 观察未超限不提示
    text5 = render.render_brief(run, make_cfg(top_n=2))
    assert text5.count("| 标准") == 2 and "另有 11 只，见 list 命令" in text5


def test_brief_empty_signals():
    run = make_run(none=3)
    text = render.render_brief(run, make_cfg())
    assert "结论：B1确认 0 ｜ 完美B1 0 ｜ 标准B1 0 ｜ 观察 0 ｜ 预警 0" in text
    assert "今日无 B1 信号" in text
    assert "⚠️ 预警：牛绳断 0｜连续破BBI 0" in text
    assert "数据异常：失败 0｜停牌 0｜未更新 0" in text
    assert "完整报告：" in text and "免责声明" in text
    # 完全空
    run0 = make_run()
    assert "股票池 0" in render.render_brief(run0, make_cfg())
    run0["results"] = []
    run0["fetch"] = {}
    render.render_brief(run0, make_cfg())


def test_brief_warn_names_and_anomalies():
    run = make_run(rope_break=2, below_bbi=1, failed=2, suspended=3, stale=1)
    text = render.render_brief(run, make_cfg())
    assert "牛绳断 2（牛绳1、牛绳2）" in text
    assert "连续破BBI 1（破线1）" in text
    assert "数据异常：失败 2｜停牌 3｜未更新 1" in text
    assert f"完整报告：{run['report_xlsx_path']}" in text


def test_brief_token_budget_typical():
    # 典型：确认2 完美2 标准6 观察10，预警若干 -> 目标 <=1000 token，粗估<=2000 字符量级
    run = make_run(confirm=2, perfect=2, standard=6, watch=7, rope_break=3, below_bbi=2, suspended=2)
    text = render.render_brief(run, make_cfg())
    assert len(text) <= 2000 * 2, len(text)  # 命中列较长的最坏样本放宽到 2 倍
    small = render.render_brief(make_run(confirm=1, perfect=1, standard=2, watch=2, rope_break=1), make_cfg())
    assert len(small) <= 2000, len(small)


def test_brief_pipes_in_name_escaped():
    run = make_run(standard=1)
    run["results"][0]["name"] = "甲|乙"
    text = render.render_brief(run, make_cfg())
    assert "甲/乙" in text


# ---------------------------------------------------------------- list
@pytest.mark.parametrize("kind,count", [("confirm", 2), ("perfect", 1), ("standard", 12), ("watch", 3)])
def test_list_not_truncated(kind, count):
    run = make_run(confirm=2, perfect=1, standard=12, watch=3, rope_break=1)
    text = render.render_list(run, kind, make_cfg(top_n=2))
    assert f"共 {count} 只" in text
    rows = [l for l in text.splitlines() if l.startswith("| 6")]
    assert len(rows) == count
    assert "另有" not in text and "免责声明" in text


def test_list_warn():
    run = make_run(rope_break=2, below_bbi=1)
    text = render.render_list(run, "warn", make_cfg())
    assert "共 3 只" in text and "牛绳断" in text and "连续破BBI" in text
    assert "| 代码 | 名称 | 预警 |" in text


def test_list_empty_and_invalid():
    text = render.render_list(make_run(none=1), "watch", make_cfg())
    assert "共 0 只" in text and "（无）" in text
    with pytest.raises(ValueError):
        render.render_list(make_run(), "bogus", make_cfg())


# ---------------------------------------------------------------- explain
def test_explain_content_and_size():
    r = make_result("600001", "甲", C.LEVEL_CONFIRM, score=88, j=8.04, close=10.0)
    text = render.render_explain(r)
    for s in ("600001 甲", "B1确认", "J 8.0", "G1✓ G2✓ G3✓ G4✓", "Q1=20", "Q7=0", "得分 88",
              "命中：J<-5", "扣分：未贴近白线", "止损 9.50", "幅度 5.0%", "白线 10.10", "黄线 9.50", "BBI 9.90"):
        assert s in text, s
    assert len(text) <= 450  # ≈300 token


def test_explain_none_level_and_warnings():
    r = make_result("600009", "丁", C.LEVEL_NONE, score=0, warnings=[C.WARN_BULL_ROPE_BREAK])
    text = render.render_explain(r)
    assert "不符合" in text and "预警：牛绳断" in text and "止损" not in text and "趋势 破位" in text


def test_explain_data_insufficient():
    r = make_result("600010", "戊", C.LEVEL_NONE, data_ok=False)
    text = render.render_explain(r)
    assert "数据不足" in text
