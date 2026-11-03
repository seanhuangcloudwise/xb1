"""render/report/history 测试共用：按 DEV_CONTRACTS 契约构造 run 字典。"""
from __future__ import annotations

import copy

from xb1lib import constants as C
from xb1lib.render import compute_checksum

HOME = "/tmp/xb1_test_home"


def make_result(code: str, name: str, level: str, *, score: int = 60, j: float = 10.0, close: float = 10.0,
                date: str = "2026-11-03", warnings=None, data_ok: bool = True, board: str = "main",
                hits=None, b1_date=None) -> dict:
    has_plan = level in (C.LEVEL_CONFIRM, C.LEVEL_PERFECT, C.LEVEL_STANDARD)
    stop_price = round(close * 0.95, 2) if has_plan else None
    stop_pct = round((close - stop_price) / close * 100, 4) if has_plan else None
    base = {C.LEVEL_CONFIRM: C.LEVEL_STANDARD}.get(level, level)
    return {
        "code": code, "name": name, "board": board, "date": date, "close": close, "pct_chg": 1.234,
        "amplitude": 3.21, "j": j, "k": 25.5, "d": 30.2, "white": close * 1.01, "yellow": close * 0.95,
        "bbi": close * 0.99,
        "gates": {"G1": True, "G2": True, "G3": True, "G4": level != C.LEVEL_NONE},
        "gates_pass": level != C.LEVEL_NONE, "trigger": level in (C.LEVEL_PERFECT, C.LEVEL_STANDARD),
        "q": {"Q1": 20, "Q2": 10, "Q3": 15, "Q4": 15, "Q5": 0, "Q6": 10, "Q7": 0},
        "score": score, "base_level": base, "level": level,
        "b1_date": b1_date if b1_date else ("2026-11-02" if level == C.LEVEL_CONFIRM else None),
        "hits": list(hits) if hits is not None else ["J<-5", "缩量", "振幅小"],
        "deductions": ["未贴近白线", "近30日无放量上涨"],
        "stop_price": stop_price, "stop_pct": stop_pct, "stop_wide": bool(stop_pct and stop_pct > 8.0),
        "trend": "bull" if not warnings else "broken",
        "warnings": list(warnings or []),
        "data_ok": data_ok, "note": "" if data_ok else "数据不足114行",
    }


def make_run(confirm=0, perfect=0, standard=0, watch=0, none=0, rope_break=0, below_bbi=0,
             failed=0, suspended=0, stale=0, date: str = "2026-11-03", home: str = HOME) -> dict:
    """生成确定性的 run：各等级 N 只；rope_break / below_bbi 为额外带预警的 none 级股票。"""
    results = []
    counter = [0]

    def code():
        counter[0] += 1
        return f"{600000 + counter[0]:06d}"

    def add(level, n, score0, j0, jstep, prefix):
        for i in range(n):
            c = code()
            results.append(make_result(c, f"{prefix}{i + 1}", level, score=score0 - i, j=round(j0 + i * jstep, 3),
                                       close=round(10 + i * 0.37, 2), date=date))

    add(C.LEVEL_CONFIRM, confirm, 88, 8.0, 0.5, "确认")
    add(C.LEVEL_PERFECT, perfect, 90, -6.0, 0.4, "完美")
    add(C.LEVEL_STANDARD, standard, 70, 5.0, 0.6, "标准")
    add(C.LEVEL_WATCH, watch, 35, 19.9, -0.7, "观察")  # J 逐只递减，验证观察按 J 升序
    for i in range(none):
        results.append(make_result(code(), f"无信号{i + 1}", C.LEVEL_NONE, score=0, j=40 + i, date=date))
    for i in range(rope_break):
        results.append(make_result(code(), f"牛绳{i + 1}", C.LEVEL_NONE, score=0, j=45, date=date,
                                   warnings=[C.WARN_BULL_ROPE_BREAK]))
    for i in range(below_bbi):
        results.append(make_result(code(), f"破线{i + 1}", C.LEVEL_NONE, score=0, j=50, date=date,
                                   warnings=[C.WARN_BELOW_BBI_2D]))
    run = {
        "date": date, "weekday": "周二", "universe_size": len(results) + failed + suspended + stale,
        "engine_version": "1.2.0", "config_hash": "abcd1234", "checksum": "",
        "results": results,
        "fetch": {"failed": [{"code": f"3000{i:02d}", "reason": "failed: 超时"} for i in range(failed)],
                  "suspended": [f"0020{i:02d}" for i in range(suspended)],
                  "stale": [f"6881{i:02d}" for i in range(stale)]},
        "report_xlsx_path": f"{home}/data/reports/{date}/b1_report.xlsx",
        "report_md_path": f"{home}/data/reports/{date}/b1_report.md",
        "report_html_path": f"{home}/data/reports/{date}/b1_cards.html",
    }
    run["checksum"] = compute_checksum(results)
    return copy.deepcopy(run)


def make_cfg(top_n: int = 10) -> dict:
    return {"report": {"top_n": top_n}, "thresholds": {}}
