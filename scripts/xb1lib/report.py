"""完整报告：Markdown 日报 + Excel（PRD 4.7 / 5.1.1 / DEV_CONTRACTS「report.py」）。"""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

from . import atomic
from . import constants as C
from . import render as R

SHEET_OVERVIEW = "总览"
SHEET_SIGNALS = "B1信号"
SHEET_ALL = "全清单"
SHEET_WARN = "预警"
SHEET_ANOMALY = "数据异常"

SIGNAL_COLS = ["代码", "名称", "等级", "得分", "收盘", "涨幅%", "振幅%", "J", "K", "D",
               "白线", "黄线", "BBI", "止损价", "止损幅度%", "止损过宽", "B1日期", "命中", "扣分"]
ALL_COLS = ["代码", "名称", "板块", "等级", "得分", "收盘", "涨幅%", "J", "白线", "黄线", "BBI",
            "趋势", "预警", "闸门通过", "数据正常", "备注"]
WARN_COLS = ["代码", "名称", "预警", "收盘", "白线", "黄线", "BBI", "趋势"]
ANOMALY_COLS = ["代码", "类型", "原因"]
_BOARD = {"main": "主板", "gem": "创业板", "star": "科创板", "bj": "北交所"}
_FETCH_KIND = {"failed": "失败", "suspended": "停牌", "stale": "未更新"}


def _v(x: Any):
    """写入 Excel 的值：None/NaN -> 空。"""
    if x is None:
        return None
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    return x


def _trend(r: dict) -> str:
    return "多头" if r.get("trend") == "bull" else "破位"


def _warn_text(r: dict) -> str:
    return "、".join(C.WARN_LABELS.get(w, w) for w in (r.get("warnings") or []))


def anomaly_rows(run: dict) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    f = run.get("fetch") or {}
    for item in f.get("failed") or []:
        if isinstance(item, dict):
            rows.append((str(item.get("code")), "失败", str(item.get("reason", ""))))
        else:
            rows.append((str(item), "失败", ""))
    for code in f.get("suspended") or []:
        rows.append((str(code), "停牌", "最后一根 K 线日期不等于当日交易日"))
    for code in f.get("stale") or []:
        rows.append((str(code), "未更新", "数据未更新"))
    for r in run.get("results") or []:
        if r.get("data_ok") is False:
            rows.append((str(r.get("code")), "数据不足", str(r.get("note") or "")))
    return rows


def _signal_rows(results: list[dict]) -> list[dict]:
    out: list[dict] = []
    for lv in (C.LEVEL_CONFIRM, C.LEVEL_PERFECT, C.LEVEL_STANDARD, C.LEVEL_WATCH):
        out += R.by_level(results, lv)
    return out


# --------------------------------------------------------------------------- Markdown
def build_markdown(run: dict, cfg: dict) -> str:
    results = run.get("results") or []
    counts = R.level_counts(results)
    n_warn = len(R.warn_rows(results))
    L: list[str] = [
        f"# B1 复盘日报 {run.get('date')}（{R._weekday(run)}）",
        "",
        f"- 股票池：{run.get('universe_size', len(results))}",
        f"- 引擎：{R._ver(run)}｜配置哈希：{run.get('config_hash', '-')}｜校验：{R._checksum(run)}",
        "- 结论：" + " ｜ ".join([f"{R.LEVEL_LABELS[lv]} {counts[lv]}" for lv in counts] + [f"预警 {n_warn}"]),
        "",
    ]
    if sum(counts.values()) == 0:
        L += ["今日无 B1 信号。", ""]
    for lv, title in R._SECTIONS:
        rows = R.by_level(results, lv)
        if rows:
            L += [f"## {title}（{len(rows)}）", ""] + R.signal_table_lines(rows) + [""]

    L += ["## 预警", ""]
    warn = R.warn_rows(results)
    if warn:
        L += ["| 代码 | 名称 | 预警 | 收盘 | 白线 | 黄线 | BBI |", "|---|---|---|---|---|---|---|"]
        for r in warn:
            L.append("| " + " | ".join(R._cell(x) for x in (
                r.get("code"), r.get("name"), _warn_text(r), R._num(r.get("close"), 2),
                R._num(r.get("white"), 2), R._num(r.get("yellow"), 2), R._num(r.get("bbi"), 2))) + " |")
    else:
        L.append("无")
    L.append("")

    L += ["## 全清单趋势", ""]
    ok = [r for r in results if r.get("data_ok") is not False]
    bull = sum(1 for r in ok if r.get("trend") == "bull")
    L.append(f"多头 {bull}｜破位 {len(ok) - bull}")
    L.append("")
    if results:
        L += ["| 代码 | 名称 | 等级 | 收盘 | J | 趋势 | 预警 |", "|---|---|---|---|---|---|---|"]
        for r in sorted(results, key=lambda r: str(r.get("code"))):
            L.append("| " + " | ".join(R._cell(x) for x in (
                r.get("code"), r.get("name"), R.LEVEL_LABELS.get(r.get("level"), "-"),
                R._num(r.get("close"), 2), R._num(r.get("j"), 1), _trend(r), _warn_text(r) or "-")) + " |")
        L.append("")

    L += ["## 数据异常", ""]
    an = anomaly_rows(run)
    if an:
        L += ["| 代码 | 类型 | 原因 |", "|---|---|---|"]
        L += [f"| {R._cell(c)} | {k} | {R._cell(why)} |" for c, k, why in an]
    else:
        L.append("无")
    L += ["", R.DISCLAIMER]
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- Excel
def build_workbook(run: dict, cfg: dict):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    results = run.get("results") or []
    wb = Workbook()
    bold = Font(bold=True)

    def sheet(ws, header: list[str], rows: list[list], fmts: dict[str, str] | None = None):
        ws.append(header)
        for row in rows:
            ws.append([_v(x) for x in row])
        for c in ws[1]:
            c.font = bold
        ws.freeze_panes = "A2"
        for i, h in enumerate(header, start=1):
            width = max([_disp_width(h)] + [_disp_width(ws.cell(row=r, column=i).value)
                                            for r in range(2, ws.max_row + 1)])
            ws.column_dimensions[get_column_letter(i)].width = min(max(width + 2, 8), 60)
            fmt = (fmts or {}).get(h)
            if fmt:
                for r in range(2, ws.max_row + 1):
                    ws.cell(row=r, column=i).number_format = fmt

    # 总览
    ws = wb.active
    ws.title = SHEET_OVERVIEW
    counts = R.level_counts(results)
    failed, suspended, stale = R.fetch_counts(run)
    ov = [
        ["数据日期", run.get("date")], ["星期", R._weekday(run)],
        ["股票池", run.get("universe_size", len(results))],
        ["引擎版本", R._ver(run)], ["配置哈希", run.get("config_hash")], ["校验码", R._checksum(run)],
    ] + [[R.LEVEL_LABELS[lv], counts[lv]] for lv in counts] + [
        ["预警", len(R.warn_rows(results))],
        ["数据异常-失败", failed], ["数据异常-停牌", suspended], ["数据异常-未更新", stale],
        ["免责声明", "仅供复盘参考，不构成投资建议。"],
    ]
    sheet(ws, ["项目", "值"], ov)
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 36

    # B1 信号
    num2 = "0.00"
    sig = [[r.get("code"), r.get("name"), R.LEVEL_LABELS.get(r.get("level"), "-"), r.get("score"),
            r.get("close"), r.get("pct_chg"), r.get("amplitude"), r.get("j"), r.get("k"), r.get("d"),
            r.get("white"), r.get("yellow"), r.get("bbi"), r.get("stop_price"), r.get("stop_pct"),
            ("是" if r.get("stop_wide") else "否") if r.get("stop_price") is not None else None,
            r.get("b1_date"), "、".join(r.get("hits") or []), "、".join(r.get("deductions") or [])]
           for r in _signal_rows(results)]
    sheet(wb.create_sheet(SHEET_SIGNALS), SIGNAL_COLS, sig,
          {h: num2 for h in ("收盘", "涨幅%", "振幅%", "J", "K", "D", "白线", "黄线", "BBI", "止损价", "止损幅度%")}
          | {"得分": "0"})

    # 全清单
    allrows = [[r.get("code"), r.get("name"), _BOARD.get(r.get("board"), r.get("board")),
                R.LEVEL_LABELS.get(r.get("level"), "-"), r.get("score"), r.get("close"), r.get("pct_chg"),
                r.get("j"), r.get("white"), r.get("yellow"), r.get("bbi"), _trend(r), _warn_text(r) or None,
                "是" if r.get("gates_pass") else "否", "是" if r.get("data_ok") is not False else "否",
                r.get("note") or None]
               for r in sorted(results, key=lambda r: str(r.get("code")))]
    sheet(wb.create_sheet(SHEET_ALL), ALL_COLS, allrows,
          {h: num2 for h in ("收盘", "涨幅%", "J", "白线", "黄线", "BBI")} | {"得分": "0"})

    # 预警
    wrows = [[r.get("code"), r.get("name"), _warn_text(r), r.get("close"), r.get("white"),
              r.get("yellow"), r.get("bbi"), _trend(r)] for r in R.warn_rows(results)]
    sheet(wb.create_sheet(SHEET_WARN), WARN_COLS, wrows,
          {h: num2 for h in ("收盘", "白线", "黄线", "BBI")})

    # 数据异常
    sheet(wb.create_sheet(SHEET_ANOMALY), ANOMALY_COLS, [list(x) for x in anomaly_rows(run)])
    return wb


def _disp_width(v: Any) -> int:
    if v is None:
        return 0
    if isinstance(v, float):
        v = f"{v:.2f}"
    return sum(2 if ord(ch) > 255 else 1 for ch in str(v))


# --------------------------------------------------------------------------- 落盘
def update_latest_link(paths, date: str) -> bool:
    """reports/latest -> {date}（相对软链，原子替换）。不支持软链/权限不足时返回 False，不抛错。"""
    try:
        link = Path(paths.reports_latest)
        tmp = link.parent / f".latest.{os.getpid()}.tmp"
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(date, tmp)
        try:
            os.replace(tmp, link)
        finally:
            if tmp.is_symlink() or tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        return True
    except Exception:
        return False


def write_reports(paths, run: dict, cfg: dict) -> dict:
    """原子写 b1_report.md / b1_report.xlsx，并更新 latest 软链。返回 {md, xlsx, latest_linked}。"""
    date = str(run["date"])
    md_path, xlsx_path = paths.report_md(date), paths.report_xlsx(date)
    wb = build_workbook(run, cfg)
    atomic.write_with(xlsx_path, lambda tmp: wb.save(str(tmp)))
    atomic.write_text(md_path, build_markdown(run, cfg))
    linked = update_latest_link(paths, date)
    return {"md": md_path, "xlsx": xlsx_path, "latest_linked": linked}
