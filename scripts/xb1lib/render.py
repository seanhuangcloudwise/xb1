"""预渲染文本（PRD 5.3 / DEV_CONTRACTS「render.py」）。

Agent 只做转述：所有数字、等级、校验码均在此一次性渲染完成。
数字格式固定：收盘/止损价 2 位小数，J 1 位，止损幅度 1 位 %，得分整数。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
from typing import Any, Iterable

from . import constants as C

DISCLAIMER = "免责声明：仅供复盘参考，不构成投资建议。"
TABLE_HEADER = "| 代码 | 名称 | 得分 | 收盘 | J | 止损 | 止损幅度 | 命中 |"
TABLE_SEP = "|---|---|---|---|---|---|---|---|"
MAX_HITS_SHOWN = 4

_WEEKDAYS = "一二三四五六日"

# kind -> (标题, level 或 None)
_SECTIONS = (
    (C.LEVEL_CONFIRM, "🔥 B1 确认"),
    (C.LEVEL_PERFECT, "⭐ 完美 B1"),
    (C.LEVEL_STANDARD, "标准 B1"),
    (C.LEVEL_WATCH, "观察"),
)
LIST_KINDS = (C.LEVEL_CONFIRM, C.LEVEL_PERFECT, C.LEVEL_STANDARD, C.LEVEL_WATCH, "warn")
LEVEL_LABELS = {
    C.LEVEL_CONFIRM: "B1确认",
    C.LEVEL_PERFECT: "完美B1",
    C.LEVEL_STANDARD: "标准B1",
    C.LEVEL_WATCH: "观察",
    C.LEVEL_NONE: "不符合",
}


# --------------------------------------------------------------------------- 工具
def _canon(obj: Any) -> Any:
    """规范化：浮点保留 6 位，NaN/inf -> None，dict 键排序由 json.dumps 完成。"""
    if isinstance(obj, bool) or obj is None or isinstance(obj, str):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return round(obj, 6)
    if isinstance(obj, dict):
        return {str(k): _canon(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_canon(v) for v in obj]
    try:  # numpy 标量等
        return _canon(obj.item())
    except Exception:
        return str(obj)


_CHECKSUM_SKIP = ("groups", "tags")   # 分组/标签是人工元数据，改它们不应改变“信号数据”的校验码


def compute_checksum(results: Iterable[dict]) -> str:
    """results 规范化 JSON（按 code 排序、键排序、浮点 6 位）取 sha1，前 6 位 hex。
    不含 groups/tags（人工元数据）。"""
    rows = sorted((_canon({k: v for k, v in r.items() if k not in _CHECKSUM_SKIP}) for r in results),
                  key=lambda r: (str(r.get("code")), str(r.get("date"))))
    blob = json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:6]


def _num(v: Any, nd: int) -> str:
    if v is None:
        return "-"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "-"
    if math.isnan(f) or math.isinf(f):
        return "-"
    return f"{f:.{nd}f}"


def _pct(v: Any, nd: int = 1) -> str:
    s = _num(v, nd)
    return s if s == "-" else f"{s}%"


def _cell(s: Any) -> str:
    return str(s).replace("|", "/").replace("\n", " ")


def _ver(run: dict) -> str:
    v = str(run.get("engine_version") or C.ENGINE_VERSION)
    return v if v.startswith("v") else f"v{v}"


def _weekday(run: dict) -> str:
    wd = run.get("weekday")
    if wd:
        return str(wd)
    try:
        d = _dt.date.fromisoformat(str(run.get("date")))
        return "周" + _WEEKDAYS[d.weekday()]
    except Exception:
        return ""


def _checksum(run: dict) -> str:
    return run.get("checksum") or compute_checksum(run.get("results") or [])


def _results(run: dict) -> list[dict]:
    return list(run.get("results") or [])


def by_level(results: list[dict], level: str) -> list[dict]:
    rows = [r for r in results if r.get("level") == level]
    if level == C.LEVEL_WATCH:  # 观察按 J 升序
        return sorted(rows, key=lambda r: (_sort_num(r.get("j")), str(r.get("code"))))
    return sorted(rows, key=lambda r: (-_sort_num(r.get("score"), 0.0), str(r.get("code"))))


def _sort_num(v: Any, default: float = 1e9) -> float:
    try:
        f = float(v)
        return default if math.isnan(f) else f
    except (TypeError, ValueError):
        return default


def warn_rows(results: list[dict], warn: str | None = None) -> list[dict]:
    rows = [r for r in results if r.get("warnings") and (warn is None or warn in r["warnings"])]
    return sorted(rows, key=lambda r: str(r.get("code")))


def level_counts(results: list[dict]) -> dict:
    return {lv: sum(1 for r in results if r.get("level") == lv) for lv, _ in _SECTIONS}


def fetch_counts(run: dict) -> tuple[int, int, int]:
    f = run.get("fetch") or {}
    return len(f.get("failed") or []), len(f.get("suspended") or []), len(f.get("stale") or [])


_GATE_PREFIX = ("G1", "G2", "G3", "G4")


def _hits_text(r: dict) -> str:
    # 进入信号表的股票必然已过 G1–G4 闸门，这些命中没有区分度，只展示触发(T)与评分(Q)项；
    # 没有非闸门命中时才回退到全部命中。完整闸门情况见 explain。
    allhits = list(r.get("hits") or [])
    hits = [h for h in allhits if not str(h).startswith(_GATE_PREFIX)] or allhits
    s = "、".join(hits[:MAX_HITS_SHOWN])
    if len(hits) > MAX_HITS_SHOWN:
        s += "…"
    if r.get("level") == C.LEVEL_CONFIRM and r.get("b1_date"):
        s = f"B1日{str(r['b1_date'])[5:]}" + (f"；{s}" if s else "")
    return s or "-"


def _stop_pct_text(r: dict) -> str:
    s = _pct(r.get("stop_pct"))
    if s != "-" and r.get("stop_wide"):
        s += " 止损过宽"
    return s


def signal_row(r: dict) -> str:
    return "| " + " | ".join(_cell(x) for x in (
        r.get("code", ""), r.get("name", ""),
        _num(r.get("score"), 0), _num(r.get("close"), 2), _num(r.get("j"), 1),
        _num(r.get("stop_price"), 2), _stop_pct_text(r), _hits_text(r),
    )) + " |"


def signal_table_lines(rows: list[dict]) -> list[str]:
    return [TABLE_HEADER, TABLE_SEP] + [signal_row(r) for r in rows]


def _names(rows: list[dict], limit: int) -> str:
    names = [str(r.get("name") or r.get("code")) for r in rows[:limit]]
    s = "、".join(names)
    if len(rows) > limit:
        s += f"…等{len(rows)}只"
    return s


def _warn_line(results: list[dict], top_n: int) -> str:
    parts = []
    for w in (C.WARN_BULL_ROPE_BREAK, C.WARN_BELOW_BBI_2D):
        rows = warn_rows(results, w)
        label = C.WARN_LABELS[w]
        parts.append(f"{label} {len(rows)}" + (f"（{_names(rows, top_n)}）" if rows else ""))
    return "⚠️ 预警：" + "｜".join(parts)


def _anomaly_line(run: dict, results: list[dict]) -> str:
    failed, suspended, stale = fetch_counts(run)
    s = f"数据异常：失败 {failed}｜停牌 {suspended}｜未更新 {stale}"
    insufficient = sum(1 for r in results if r.get("data_ok") is False)
    if insufficient:
        s += f"｜数据不足 {insufficient}"
    return s


def _top_n(cfg: dict) -> int:
    try:
        return max(1, int((cfg.get("report") or {}).get("top_n", 10)))
    except (TypeError, ValueError):
        return 10


# --------------------------------------------------------------------------- brief
def render_brief(run: dict, cfg: dict) -> str:
    results = _results(run)
    top_n = _top_n(cfg)
    counts = level_counts(results)
    n_warn = len(warn_rows(results))

    lines = [
        f"【B1 复盘】数据日期 {run.get('date')}（{_weekday(run)}）｜股票池 {run.get('universe_size', len(results))}"
        f"｜引擎 {_ver(run)}｜校验 {_checksum(run)}" + (f"｜{run['view']}" if run.get("view") else ""),
        "结论：" + " ｜ ".join(
            [f"{LEVEL_LABELS[lv]} {counts[lv]}" for lv, _ in _SECTIONS] + [f"预警 {n_warn}"]),
        "",
    ]
    if run.get("intraday"):
        lines.insert(1, f"⚠️ 盘中数据（{run['intraday']}），最后一根 K 线未收盘，非收盘结论；收盘后（≥15:30）重跑 run 才是正式复盘")
    if sum(counts.values()) == 0:
        lines += ["今日无 B1 信号。", ""]
    for lv, title in _SECTIONS:
        rows = by_level(results, lv)
        if not rows:
            continue
        lines.append(f"{title}（{len(rows)}）")
        lines += signal_table_lines(rows[:top_n])
        if len(rows) > top_n:
            lines.append(f"另有 {len(rows) - top_n} 只，见 list 命令（xb1.py list {lv}）")
        lines.append("")
    lines += [
        _warn_line(results, top_n),
        _anomaly_line(run, results),
        f"完整报告：{run.get('report_xlsx_path') or '（未生成）'}",
        f"K线卡片：{run.get('report_html_path') or '（未生成）'}",
        DISCLAIMER,
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- list
def render_list(run: dict, kind: str, cfg: dict) -> str:
    if kind not in LIST_KINDS:
        raise ValueError(f"未知类别 {kind!r}，可选：{'/'.join(LIST_KINDS)}")
    results = _results(run)
    head = f"【B1 列表·{'预警' if kind == 'warn' else LEVEL_LABELS[kind]}】数据日期 {run.get('date')}｜校验 {_checksum(run)}"
    if kind == "warn":
        rows = warn_rows(results)
        lines = [head, f"共 {len(rows)} 只", ""]
        if rows:
            lines += ["| 代码 | 名称 | 预警 | 收盘 | 白线 | 黄线 | BBI |", "|---|---|---|---|---|---|---|"]
            for r in rows:
                labels = "、".join(C.WARN_LABELS.get(w, w) for w in r["warnings"])
                lines.append("| " + " | ".join(_cell(x) for x in (
                    r.get("code", ""), r.get("name", ""), labels, _num(r.get("close"), 2),
                    _num(r.get("white"), 2), _num(r.get("yellow"), 2), _num(r.get("bbi"), 2))) + " |")
    else:
        rows = by_level(results, kind)
        lines = [head, f"共 {len(rows)} 只", ""]
        if rows:
            lines += signal_table_lines(rows)
    if not rows:
        lines.append("（无）")
    lines += ["", DISCLAIMER]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- explain
def _gate_text(r: dict) -> str:
    g = r.get("gates") or {}
    return " ".join(f"{k}{'✓' if g.get(k) else '✗'}" for k in ("G1", "G2", "G3", "G4"))


def render_explain(result: dict) -> str:
    r = result
    lv = r.get("level", C.LEVEL_NONE)
    lines = [f"【解释】{r.get('code')} {r.get('name', '')}｜数据日期 {r.get('date')}｜{r.get('board', '')}"]
    if r.get("data_ok") is False:
        lines.append(f"数据不足，未评估：{r.get('note') or '-'}")
        return "\n".join(lines) + "\n"
    head = f"等级：{LEVEL_LABELS.get(lv, lv)}"
    if r.get("base_level") not in (None, lv):
        head += f"（当日B1：{LEVEL_LABELS.get(r['base_level'], r['base_level'])}"
        head += f"，B1日 {r['b1_date']}）" if r.get("b1_date") else "）"
    lines.append(head)
    lines.append(
        f"收盘 {_num(r.get('close'), 2)}｜涨幅 {_pct(r.get('pct_chg'), 2)}｜振幅 {_pct(r.get('amplitude'), 2)}"
        f"｜J {_num(r.get('j'), 1)}（K {_num(r.get('k'), 1)} D {_num(r.get('d'), 1)}）")
    lines.append(
        f"白线 {_num(r.get('white'), 2)}｜黄线 {_num(r.get('yellow'), 2)}｜BBI {_num(r.get('bbi'), 2)}"
        f"｜趋势 {'多头' if r.get('trend') == 'bull' else '破位'}")
    lines.append(f"闸门 {_gate_text(r)}｜闸门通过 {'是' if r.get('gates_pass') else '否'}｜触发 {'是' if r.get('trigger') else '否'}")
    q = r.get("q") or {}
    qtxt = " ".join(f"{k}={q[k]}" for k in sorted(q))
    lines.append(f"得分 {_num(r.get('score'), 0)}：{qtxt or '-'}")
    lines.append("命中：" + ("、".join(r.get("hits") or []) or "-"))
    lines.append("扣分：" + ("、".join(r.get("deductions") or []) or "-"))
    if r.get("stop_price") is not None:
        lines.append(f"止损 {_num(r.get('stop_price'), 2)}（幅度 {_pct(r.get('stop_pct'))}"
                     f"{'，止损过宽' if r.get('stop_wide') else ''}）")
    if r.get("warnings"):
        lines.append("预警：" + "、".join(C.WARN_LABELS.get(w, w) for w in r["warnings"]))
    return "\n".join(lines) + "\n"
