"""数据概览：对本地已拉取的数据做统计，生成自包含 HTML（内联 CSS/SVG，无外部依赖，不联网）。

只读：读取 data/raw/kline、data/raw/profile、data/runs、data/reports 等目录，不改任何数据、不重算信号。
统计口径：
  * 股票数 = raw/kline 下可读的 parquet 文件数；损坏文件单独计数，不计入统计。
  * 最新交易日 = 所有股票末根 K 线日期的最大值；"落后"指末根日期早于最新交易日的股票。
  * 涨跌统计只看最新交易日当天 pct_chg（百分数）。
  * 成交额按 cfg.fetch.amount_unit 换算到元后再汇总，页面显示为亿元。
"""
from __future__ import annotations

import html as _html
import os
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from . import atomic
from .calendar import shanghai_now
from .models import board_of
from .paths import Paths

BOARD_NAMES = {"main": "主板", "gem": "创业板", "star": "科创板", "bj": "北交所"}
BOARD_ORDER = ["main", "gem", "star", "bj"]
HIST_BUCKETS = [("< 60 根", 0, 60), ("60–119 根", 60, 120), ("120–249 根", 120, 250), ("≥ 250 根", 250, 10**9)]
LAG_SAMPLE = 20
_COLS = ["date", "close", "pct_chg", "amount"]


def _e(v: Any) -> str:
    return _html.escape(str(v), quote=True)


def _fmt_bytes(n: float) -> str:
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} GB"


def _dir_size(p: Path) -> tuple[int, int]:
    total, count = 0, 0
    if not p.exists():
        return 0, 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
                count += 1
            except OSError:
                pass
    return total, count


# --------------------------------------------------------------------------- 统计
def collect(paths: Paths, cfg: dict) -> dict:
    fcfg = cfg.get("fetch") or {}
    unit = float(fcfg.get("amount_unit", 1) or 1)
    kline_days = int(fcfg.get("kline_days", 250))
    files = sorted(paths.kline_dir.glob("*.parquet")) if paths.kline_dir.exists() else []

    boards: Counter = Counter()
    lengths: list[int] = []
    last_dates: Counter = Counter()
    first_dates: list[str] = []
    latest_rows: dict[str, dict] = {}      # code -> 末根记录
    broken: list[str] = []
    total_rows = 0

    for fp in files:
        code = fp.stem
        try:
            df = pd.read_parquet(fp, columns=_COLS)
        except Exception:
            broken.append(code)
            continue
        if df.empty:
            broken.append(code)
            continue
        df["date"] = df["date"].astype(str)
        df = df.sort_values("date").reset_index(drop=True)
        last = df.iloc[-1]
        n = len(df)
        total_rows += n
        lengths.append(n)
        boards[board_of(code)] += 1
        last_dates[str(last["date"])] += 1
        first_dates.append(str(df["date"].iloc[0]))
        latest_rows[code] = {
            "date": str(last["date"]),
            "pct_chg": float(last["pct_chg"]) if pd.notna(last["pct_chg"]) else None,
            "amount": float(last["amount"]) * unit if pd.notna(last["amount"]) else None,
        }

    latest = max(last_dates) if last_dates else None
    today_rows = [r for r in latest_rows.values() if r["date"] == latest] if latest else []
    pcts = [r["pct_chg"] for r in today_rows if r["pct_chg"] is not None]
    up = sum(1 for v in pcts if v > 0)
    down = sum(1 for v in pcts if v < 0)
    amounts = [r["amount"] for r in today_rows if r["amount"] is not None]
    lagging = sorted(c for c, r in latest_rows.items() if latest and r["date"] < latest)

    prof_n = len(list(paths.profile_dir.glob("*.json"))) if paths.profile_dir.exists() else 0

    disk = []
    for name, p in (("原始 K 线 raw/kline", paths.kline_dir), ("资料 raw/profile", paths.profile_dir),
                    ("指标缓存 cache", paths.cache), ("每日运行 runs", paths.runs),
                    ("报告 reports", paths.reports), ("历史 history", paths.history),
                    ("档案 portfolio", paths.portfolio_dir)):
        size, cnt = _dir_size(p)
        disk.append({"name": name, "size": size, "files": cnt})

    return {
        "home": str(paths.home),
        "stocks": len(lengths),
        "broken": broken,
        "total_rows": total_rows,
        "latest": latest,
        "earliest": min(first_dates) if first_dates else None,
        "boards": {b: boards.get(b, 0) for b in BOARD_ORDER},
        "lengths": lengths,
        "last_dates": last_dates,
        "today": {"n": len(pcts), "up": up, "down": down, "flat": len(pcts) - up - down,
                  "median": statistics.median(pcts) if pcts else None,
                  "amount_sum": sum(amounts) if amounts else None,
                  "amount_median": statistics.median(amounts) if amounts else None},
        "lagging": lagging,
        "short": sum(1 for n in lengths if n < kline_days),
        "profiles": prof_n,
        "disk": disk,
        "kline_days": kline_days,
    }


# --------------------------------------------------------------------------- SVG 条形图
def _hbars(items: list, *, width: int = 620, label_w: int = 110, fmt=str, color: str = "#2563eb") -> str:
    if not items:
        return '<p class="muted">暂无数据</p>'
    vmax = max(v for _, v in items) or 1
    row_h, bar_h = 26, 16
    h = row_h * len(items) + 6
    bar_area = width - label_w - 90
    parts = [f'<svg class="chart" viewBox="0 0 {width} {h}" role="img">']
    for i, (label, v) in enumerate(items):
        y = i * row_h + 4
        w = max(1.0, bar_area * v / vmax) if v > 0 else 0.0
        parts.append(f'<text x="0" y="{y + bar_h - 3}" class="axis">{_e(label)}</text>')
        parts.append(f'<rect x="{label_w}" y="{y}" width="{w:.1f}" height="{bar_h}" rx="3" fill="{color}"/>')
        parts.append(f'<text x="{label_w + w + 6:.1f}" y="{y + bar_h - 3}" class="val">{_e(fmt(v))}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _stacked(up: int, down: int, flat: int) -> str:
    total = up + down + flat
    if total <= 0:
        return '<p class="muted">暂无数据</p>'
    segs = [("上涨", up, "#e5484d"), ("下跌", down, "#0f9d58"), ("平盘", flat, "#94a3b8")]
    bar = "".join(f'<div style="width:{v / total * 100:.2f}%;background:{c}" title="{n} {v} 只"></div>'
                  for n, v, c in segs if v)
    legend = "".join(f'<span><i style="background:{c}"></i>{n} {v:,} 只（{v / total:.1%}）</span>'
                     for n, v, c in segs)
    return f'<div class="stack">{bar}</div><div class="legend">{legend}</div>'


# --------------------------------------------------------------------------- 页面
_CSS = """
:root{--ink:#0f172a;--muted:#64748b;--line:#e2e8f0;--bg:#f3f4f6;--card:#fff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC",
     "Hiragino Sans GB","Microsoft YaHei",system-ui,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:26px 20px 40px}
h1{font-size:22px;margin:0 0 4px} h2{font-size:15px;margin:0 0 12px}
.sub{color:var(--muted);font-size:12.5px;margin:0 0 18px;word-break:break-all}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:18px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.kpi b{display:block;font-size:20px;font-variant-numeric:tabular-nums}
.kpi span{font-size:12px;color:var(--muted)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
.chart{display:block;width:100%;height:auto}
.chart .axis{font-size:12px;fill:#334155}
.chart .val{font-size:11.5px;fill:#475569}
.stack{display:flex;height:22px;border-radius:6px;overflow:hidden;background:#e2e8f0}
.legend{display:flex;gap:14px;flex-wrap:wrap;margin-top:8px;font-size:12.5px;color:#334155}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}
th{color:var(--muted);font-weight:600;font-size:12px}
td.num,th.num{text-align:right}
.muted{color:var(--muted);font-size:13px}
.warn{color:#b45309}
.codes{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:#334155;word-break:break-all;line-height:1.8}
footer{margin-top:20px;color:var(--muted);font-size:11.5px}
@media (max-width:760px){.grid{grid-template-columns:1fr}}
"""


def build_html(paths: Paths, cfg: dict, stats: Optional[dict] = None) -> str:
    s = stats or collect(paths, cfg)
    t = s["today"]
    n = s["stocks"]
    ratio = min(1.0, s["profiles"] / n) if n else 0.0     # 资料可能多于 K 线（被过滤的股票也有资料）
    lag_n = len(s["lagging"])

    kpis = [
        (f"{n:,}", "股票数（有 K 线）"),
        (_e(s["latest"] or "-"), "最新交易日"),
        (_e(s["earliest"] or "-"), "最早数据日期"),
        (f"{s['total_rows']:,}", "K 线总行数"),
        (_fmt_bytes(sum(d["size"] for d in s["disk"])), "本地占用（统计目录合计）"),
        (f"{ratio:.0%}", f"资料缓存覆盖（共 {s['profiles']:,} 只资料）"),
    ]
    kpi_html = "".join(f'<div class="kpi"><b>{v}</b><span>{k}</span></div>' for v, k in kpis)

    board_items = [(BOARD_NAMES[b], float(s["boards"][b])) for b in BOARD_ORDER if s["boards"][b]]
    board_html = _hbars(board_items, fmt=lambda v: f"{int(v):,} 只")

    hist_items = [(label, float(sum(1 for x in s["lengths"] if lo <= x < hi))) for label, lo, hi in HIST_BUCKETS]
    hist_html = _hbars(hist_items, fmt=lambda v: f"{int(v):,} 只", color="#7c3aed")

    date_items = sorted(s["last_dates"].items(), reverse=True)[:12]
    date_html = _hbars([(d, float(c)) for d, c in date_items], fmt=lambda v: f"{int(v):,} 只", color="#0891b2")

    med = "-" if t["median"] is None else f"{t['median']:+.2f}%"
    amt_sum = "-" if t["amount_sum"] is None else f"{t['amount_sum'] / 1e8:,.1f} 亿元"
    amt_med = "-" if t["amount_median"] is None else f"{t['amount_median'] / 1e8:,.2f} 亿元"
    today_stats = ('<table><tr><th>统计</th><th class="num">数值</th></tr>'
                   f'<tr><td>参与统计股票</td><td class="num">{t["n"]:,}</td></tr>'
                   f'<tr><td>涨跌中位数</td><td class="num">{med}</td></tr>'
                   f'<tr><td>成交额合计</td><td class="num">{amt_sum}</td></tr>'
                   f'<tr><td>单只成交额中位数</td><td class="num">{amt_med}</td></tr></table>')

    disk_rows = "".join(
        f'<tr><td>{_e(d["name"])}</td><td class="num">{d["files"]:,}</td><td class="num">{_fmt_bytes(d["size"])}</td></tr>'
        for d in s["disk"])
    disk_html = ('<table><tr><th>目录</th><th class="num">文件数</th><th class="num">占用</th></tr>'
                 f'{disk_rows}</table>')

    notes = []
    if lag_n:
        sample = s["lagging"][:LAG_SAMPLE]
        more = f" …（另 {lag_n - len(sample)} 只）" if lag_n > len(sample) else ""
        notes.append(f'<p class="warn">末根日期落后于最新交易日 {_e(s["latest"])}：{lag_n:,} 只（可能停牌，取数时被标记为未更新）</p>'
                     f'<div class="codes">{_e(" ".join(sample))}{_e(more)}</div>')
    if s["short"]:
        notes.append(f'<p class="warn">历史不足 {s["kline_days"]} 根：{s["short"]:,} 只（次新股或回补不完整）</p>')
    if s["broken"]:
        notes.append(f'<p class="warn">无法读取的 K 线文件：{len(s["broken"]):,} 个</p>'
                     f'<div class="codes">{_e(" ".join(s["broken"][:LAG_SAMPLE]))}</div>')
    if ratio < 1:
        notes.append(f'<p class="muted">资料缓存覆盖 {ratio:.0%}：未覆盖的股票没有名称/上市日期，ST 与次新过滤不生效。'
                     '可执行 <code>profile update</code> 补全。</p>')
    if not notes:
        notes.append('<p class="muted">未发现异常。</p>')

    gen = shanghai_now().strftime("%Y-%m-%d %H:%M")
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>XB1 · 数据概览 {_e(s['latest'] or '')}</title>
<style>{_CSS}</style></head>
<body><div class="wrap">
<h1>XB1 · 数据概览</h1>
<p class="sub">本地数据目录：{_e(s['home'])}｜最新交易日 {_e(s['latest'] or '-')}｜生成于 {gen}</p>
<div class="kpis">{kpi_html}</div>
<div class="grid">
  <div class="card"><h2>板块分布</h2>{board_html}</div>
  <div class="card"><h2>历史长度分布（K 线根数）</h2>{hist_html}</div>
  <div class="card"><h2>末根日期分布（最近 12 个日期）</h2>{date_html}</div>
  <div class="card"><h2>最新交易日涨跌（{_e(s['latest'] or '-')}）</h2>{_stacked(t['up'], t['down'], t['flat'])}<div style="margin-top:12px">{today_stats}</div></div>
  <div class="card"><h2>本地占用</h2>{disk_html}</div>
  <div class="card"><h2>注意事项</h2>{''.join(notes)}</div>
</div>
<footer>仅统计本地已拉取的数据，不联网、不重算信号。K 线取数与判定请以 xb1.py 输出为准。</footer>
</div></body></html>
"""


def write_overview(paths: Paths, cfg: dict, out: Optional[Path] = None) -> Path:
    out = Path(out) if out else paths.reports / "overview.html"
    atomic.write_text(out, build_html(paths, cfg))
    return out
