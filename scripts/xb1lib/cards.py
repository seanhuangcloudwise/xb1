"""K线卡片页：自包含 HTML + 内联 SVG 日 K 线（无外部 JS/CSS/字体依赖）。

两种视图
--------
* `include_all=False`（`xb1.py kline --signals`）：只画有 B1 信号（含观察）的股票。
* `include_all=True`（`xb1.py kline`，默认）：把结果里全部股票都画成卡片。

设计
----
* 只做展示：不参与信号计算，不影响 checksum / 历史流水。
* 数据：`run["results"]`（等级、得分、J、止损、命中）+ 指标 DataFrame（pipeline 传入，
  或按 `data/cache/indicators/{code}.parquet` 回读；缓存缺失/过期时回读原始 K 线重算）。
* 图表：最近 N 个交易日的蜡烛图 + 白线/黄线/BBI + 成交量；B1 日与止损价用虚线标注。
* 交互：纯内联 JS，按等级筛选、按代码/名称搜索；无网络请求。
"""
from __future__ import annotations

import html as _html
import math
from pathlib import Path
from typing import Any, Mapping, Optional

import pandas as pd

from . import atomic
from . import constants as C
from . import render as R
from .indicators import compute_indicators
from .paths import Paths

CARD_LEVELS = (C.LEVEL_CONFIRM, C.LEVEL_PERFECT, C.LEVEL_STANDARD, C.LEVEL_WATCH)
DEFAULT_DAYS = 90

# 等级 -> (徽标文案, CSS 类)
_LEVEL_META = {
    C.LEVEL_CONFIRM: ("B1确认", "confirm"),
    C.LEVEL_PERFECT: ("完美B1", "perfect"),
    C.LEVEL_STANDARD: ("标准B1", "standard"),
    C.LEVEL_WATCH: ("观察", "watch"),
    C.LEVEL_NONE: ("无信号", "none"),
}

_CHART_W = 460.0
_PAD_L = 4.0
_PAD_R = 46.0
_PAD_T = 10.0
_PRICE_H = 196.0
_VOL_GAP = 12.0
_VOL_H = 44.0
_AXIS_H = 18.0
_CHART_H = _PAD_T + _PRICE_H + _VOL_GAP + _VOL_H + _AXIS_H

# 颜色（与 CSS 中的图例保持一致）
_C_UP = "#f6465d"
_C_DOWN = "#2ebd85"
_C_WHITE = "#e2e8f0"
_C_YELLOW = "#f5c542"
_C_BBI = "#60a5fa"
_C_STOP = "#fbbf24"
_C_GRID = "#1e293b"
_C_AXIS = "#94a3b8"


# --------------------------------------------------------------------------- 配置
def _card_cfg(cfg: dict) -> tuple[int, bool]:
    c = (cfg.get("report") or {}).get("cards") or {}
    try:
        days = max(20, int(c.get("days", DEFAULT_DAYS)))
    except (TypeError, ValueError):
        days = DEFAULT_DAYS
    return days, bool(c.get("include_watch", True))


# --------------------------------------------------------------------------- 数据
def _slice(df: Any, date: Any, days: int) -> Optional[pd.DataFrame]:
    if df is None or not hasattr(df, "columns") or "date" not in df.columns or len(df) == 0:
        return None
    try:
        sub = df[df["date"].astype(str) <= str(date)].tail(days)
    except Exception:
        return None
    return sub.reset_index(drop=True) if len(sub) else None


def _frame_for(code: str, frames: Optional[Mapping[str, pd.DataFrame]], date: Any, days: int,
               paths: Optional[Paths]) -> Optional[pd.DataFrame]:
    df = (frames or {}).get(code)
    if df is None and paths is not None:
        p = paths.indicators(code)
        if p.exists():
            try:
                df = pd.read_parquet(p)
            except Exception:
                df = None
    covered = (df is not None and len(df) and "date" in df.columns
               and str(df["date"].iloc[-1]) >= str(date))
    # 历史回放：指标缓存可能只覆盖最近 250 日，或末行早于复盘日 -> 回读原始 K 线重算
    if not covered and paths is not None:
        p = paths.kline(code)
        if p.exists():
            try:
                raw = pd.read_parquet(p)
                raw = raw[raw["date"].astype(str) <= str(date)].tail(250)
                if len(raw):
                    df = compute_indicators(raw)
            except Exception:
                pass
    return _slice(df, date, days)


def _clean(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _rows(df: pd.DataFrame) -> list[dict]:
    out: list[dict] = []
    for t in df.itertuples(index=False):
        o, c, h, l = (_clean(getattr(t, k, None)) for k in ("open", "close", "high", "low"))
        if None in (o, c, h, l):
            continue
        out.append({
            "date": str(getattr(t, "date", "")),
            "open": o, "close": c, "high": h, "low": l,
            "volume": _clean(getattr(t, "volume", None)) or 0.0,
            "white": _clean(getattr(t, "white", None)),
            "yellow": _clean(getattr(t, "yellow", None)),
            "bbi": _clean(getattr(t, "bbi", None)),
        })
    return out


# --------------------------------------------------------------------------- 格式化
def _e(v: Any) -> str:
    return _html.escape(str(v), quote=True)


def _price(v: Any) -> str:
    f = _clean(v)
    return "-" if f is None else f"{f:.2f}"


def _vol_text(v: float) -> str:
    if v >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if v >= 1e4:
        return f"{v / 1e4:.0f}万"
    return f"{v:.0f}"


def _pct_chip(r: dict) -> str:
    v = _clean(r.get("pct_chg"))
    if v is None:
        return ""
    cls = "up" if v > 0 else ("down" if v < 0 else "flat")
    sign = "+" if v > 0 else ""
    return f'<span class="pct {cls}">{sign}{v:.2f}%</span>'


# --------------------------------------------------------------------------- SVG 图表
def _chart_svg(r: dict, df: Optional[pd.DataFrame]) -> str:
    if df is None:
        return '<div class="nochart">暂无本地 K 线数据（先运行 run 生成指标缓存）</div>'
    rows = _rows(df)
    if not rows:
        return '<div class="nochart">本地 K 线数据不足，无法绘图</div>'

    n = len(rows)
    step = (_CHART_W - _PAD_L - _PAD_R) / n
    plot_r = _CHART_W - _PAD_R

    stop = _clean(r.get("stop_price"))
    b1_date = str(r["b1_date"]) if r.get("b1_date") else None

    vals: list[float] = []
    for row in rows:
        vals += [row["high"], row["low"]]
    for key in ("white", "yellow", "bbi"):
        vals += [row[key] for row in rows if row[key] is not None]
    if stop is not None:
        vals.append(stop)
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        hi, lo = hi + 1.0, lo - 1.0
    pad = (hi - lo) * 0.04
    lo, hi = lo - pad, hi + pad
    span = hi - lo

    def x(i: int) -> float:
        return _PAD_L + (i + 0.5) * step

    def y(p: float) -> float:
        return _PAD_T + _PRICE_H * (hi - p) / span

    vmax = max((row["volume"] for row in rows), default=0.0)
    if vmax <= 0:
        vmax = 1.0

    body_w = max(1.2, min(9.0, step * 0.62))
    parts: list[str] = [
        f'<svg class="kline" viewBox="0 0 {_CHART_W:.0f} {_CHART_H:.0f}" role="img" '
        f'aria-label="{_e(r.get("code", ""))} {_e(r.get("name", ""))} 日K线图" '
        f'preserveAspectRatio="none">'
    ]

    # 价格网格 + 右侧刻度
    for k in range(4):
        p = hi - span * k / 3.0
        yy = y(p)
        parts.append(f'<line x1="{_PAD_L:.1f}" y1="{yy:.1f}" x2="{plot_r:.1f}" y2="{yy:.1f}" '
                     f'stroke="{_C_GRID}" stroke-width="1"/>')
        parts.append(f'<text x="{plot_r + 4:.1f}" y="{yy + 3.2:.1f}" fill="{_C_AXIS}" '
                     f'font-size="9.5" text-anchor="start">{p:.2f}</text>')

    # 成交量区基线
    vol_top = _PAD_T + _PRICE_H + _VOL_GAP
    parts.append(f'<line x1="{_PAD_L:.1f}" y1="{vol_top + _VOL_H:.1f}" x2="{plot_r:.1f}" '
                 f'y2="{vol_top + _VOL_H:.1f}" stroke="{_C_GRID}" stroke-width="1"/>')

    # 蜡烛 + 成交量（<title> 提供原生悬浮提示）
    for i, row in enumerate(rows):
        color = _C_UP if row["close"] >= row["open"] else _C_DOWN
        cx = x(i)
        tip = (f'{row["date"]} 开{row["open"]:.2f} 高{row["high"]:.2f} '
               f'低{row["low"]:.2f} 收{row["close"]:.2f} 量{_vol_text(row["volume"])}')
        parts.append("<g>")
        parts.append(f"<title>{_e(tip)}</title>")
        parts.append(f'<line x1="{cx:.1f}" y1="{y(row["high"]):.1f}" x2="{cx:.1f}" '
                     f'y2="{y(row["low"]):.1f}" stroke="{color}" stroke-width="1"/>')
        yo, yc = y(row["open"]), y(row["close"])
        top, h = min(yo, yc), max(1.0, abs(yo - yc))
        parts.append(f'<rect x="{cx - body_w / 2:.1f}" y="{top:.1f}" width="{body_w:.1f}" '
                     f'height="{h:.1f}" fill="{color}"/>')
        vh = _VOL_H * row["volume"] / vmax
        parts.append(f'<rect x="{cx - body_w / 2:.1f}" y="{vol_top + _VOL_H - vh:.1f}" '
                     f'width="{body_w:.1f}" height="{max(0.5, vh):.1f}" fill="{color}" opacity="0.55"/>')
        parts.append("</g>")

    # 白线 / 黄线 / BBI
    for key, color, dash in (("white", _C_WHITE, ""), ("yellow", _C_YELLOW, ""), ("bbi", _C_BBI, "3 2")):
        d = ""
        pen = False
        for i, row in enumerate(rows):
            v = row[key]
            if v is None:
                pen = False
                continue
            d += f'{"M" if not pen else "L"}{x(i):.1f} {y(v):.1f}'
            pen = True
        if d:
            dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
            parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.3" '
                         f'stroke-linejoin="round" opacity="0.9"{dash_attr}/>')

    # 止损线
    if stop is not None and lo <= stop <= hi:
        ys = y(stop)
        parts.append(f'<line x1="{_PAD_L:.1f}" y1="{ys:.1f}" x2="{plot_r:.1f}" y2="{ys:.1f}" '
                     f'stroke="{_C_STOP}" stroke-width="1" stroke-dasharray="5 3" opacity="0.9"/>')
        parts.append(f'<text x="{_PAD_L + 3:.1f}" y="{ys - 3.5:.1f}" fill="{_C_STOP}" '
                     f'font-size="9.5" text-anchor="start">止损 {stop:.2f}</text>')

    # B1 日竖线 + 标记
    if b1_date:
        bidx = next((i for i, row in enumerate(rows) if row["date"] == b1_date), None)
        if bidx is not None:
            bx = x(bidx)
            parts.append(f'<line x1="{bx:.1f}" y1="{_PAD_T:.1f}" x2="{bx:.1f}" '
                         f'y2="{vol_top + _VOL_H:.1f}" stroke="{_C_STOP}" stroke-width="1" '
                         f'stroke-dasharray="2 3" opacity="0.8"/>')
            anchor = "end" if bidx >= n - 3 else ("start" if bidx <= 2 else "middle")
            tx = bx - 3 if anchor == "end" else (bx + 3 if anchor == "start" else bx)
            parts.append(f'<text x="{tx:.1f}" y="{_PAD_T + 9:.1f}" fill="{_C_STOP}" '
                         f'font-size="9.5" text-anchor="{anchor}">B1 {b1_date[5:]}</text>')

    # 日期轴
    axis_y = _CHART_H - 4.0
    idxs = [0, n // 2, n - 1] if n >= 3 else list(range(n))
    for j, i in enumerate(idxs):
        anchor = "start" if j == 0 else ("end" if i == n - 1 and j else "middle")
        parts.append(f'<text x="{x(i):.1f}" y="{axis_y:.1f}" fill="{_C_AXIS}" font-size="9.5" '
                     f'text-anchor="{anchor}">{_e(rows[i]["date"][5:])}</text>')

    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------- 活跃市值
_MV_DAYS = 120


def _fmt_mv(v: float) -> str:
    return f"{v / 1e12:.2f} 万亿"


def _market_kline_svg(series: "pd.DataFrame") -> str:
    """0AMV 走势：指南针 STICKLINE 样式——从昨日数值连到今日数值的实心柱（红涨绿跌），
    叠加 5 日 / 13 日成本均线 C5、C13。"""
    if series is None or not len(series):
        return '<p class="muted">暂无活跃市值数据</p>'
    s = series.tail(_MV_DAYS).reset_index(drop=True)
    vals = [float(x) for x in s["amv"]]
    if len(vals) < 2:
        return '<p class="muted">数据不足</p>'
    prev = [vals[0]] + vals[:-1]
    c5 = [float(x) for x in s["c5"]]
    c13 = [float(x) for x in s["c13"]]
    amounts = [float(x) if pd.notna(x) else 0.0 for x in s["amount"]]
    dates = [str(x) for x in s["date"]]

    w = 640.0
    pad_l, pad_r = 6.0, 58.0
    p_t, p_h = 12.0, 176.0
    gap, v_h, ax_h = 10.0, 42.0, 18.0
    h = p_t + p_h + gap + v_h + ax_h
    plot_r = w - pad_r
    n = len(vals)
    step = (plot_r - pad_l) / max(1, n)

    lo = min(min(vals), min(c5), min(c13))
    hi = max(max(vals), max(c5), max(c13))
    if hi <= lo:
        hi, lo = hi * 1.001 + 1e-6, lo * 0.999 - 1e-6
    pad = (hi - lo) * 0.05
    lo, hi = lo - pad, hi + pad
    span = hi - lo

    def x(i):
        return pad_l + (i + 0.5) * step

    def y(v):
        return p_t + p_h * (hi - v) / span

    vmax = max(amounts) if amounts else 0.0
    v_top = p_t + p_h + gap

    def vy(v):
        return v_top + v_h - (v_h * v / vmax if vmax else 0.0)

    bw = max(1.2, min(10.0, step * 0.66))
    parts = [f'<svg class="chart" viewBox="0 0 {w:.0f} {h:.0f}" role="img" aria-label="活跃市值0AMV">']
    for k in range(4):
        v = hi - span * k / 3.0
        yy = y(v)
        parts.append(f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{plot_r}" y2="{yy:.1f}" stroke="#1e293b"/>')
        parts.append(f'<text x="{plot_r + 4}" y="{yy + 3.4:.1f}" class="axis">{v:.2f}</text>')
    parts.append(f'<line x1="{pad_l}" y1="{v_top + v_h}" x2="{plot_r}" y2="{v_top + v_h}" stroke="#1e293b"/>')

    for i in range(n):
        color = _C_UP if vals[i] >= prev[i] else _C_DOWN
        top, bot = y(max(vals[i], prev[i])), y(min(vals[i], prev[i]))
        chg = (vals[i] / prev[i] - 1) * 100 if prev[i] else 0.0
        tip = (f'{dates[i]} 0AMV {vals[i]:.3f} 万亿｜较前日 {chg:+.2f}%｜'
               f'成交额 {amounts[i] / 1e8:.0f} 亿')
        parts.append("<g>")
        parts.append(f"<title>{_e(tip)}</title>")
        parts.append(f'<rect x="{x(i) - bw / 2:.1f}" y="{top:.1f}" width="{bw:.1f}" '
                     f'height="{max(1.0, bot - top):.1f}" fill="{color}"/>')
        parts.append(f'<rect x="{x(i) - bw / 2:.1f}" y="{vy(amounts[i]):.1f}" width="{bw:.1f}" '
                     f'height="{max(0.5, v_top + v_h - vy(amounts[i])):.1f}" fill="{color}" opacity="0.5"/>')
        parts.append("</g>")

    for arr, color, dash in ((c13, "#60a5fa", "4 3"), (c5, _C_YELLOW, "")):
        d = "".join(f'{"M" if i == 0 else "L"}{x(i):.1f} {y(v):.1f}' for i, v in enumerate(arr))
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.4" '
                     f'stroke-linejoin="round" opacity="0.95"{dash_attr}/>')

    for i in (0, n // 2, n - 1):
        anchor = "start" if i == 0 else ("end" if i == n - 1 else "middle")
        parts.append(f'<text x="{x(i):.1f}" y="{h - 5:.1f}" class="axis" text-anchor="{anchor}">'
                     f'{_e(dates[i][5:])}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _market_panel(series: "pd.DataFrame", cfg: dict) -> str:
    from . import market as M
    if series is None or not len(series):
        return ('<div class="card"><h3>活跃市值 0AMV</h3>'
                '<p class="muted">暂无数据：先运行 run 拉取日 K 线。</p></div>')
    s = M.summarize(series)
    key = s.get("region", "unknown")
    chg = "-" if s.get("chg") is None else f"{s['chg']:+.2f}%"
    kpis = [
        (f"{s['amv']:.3f} 万亿", "0AMV"),
        (chg, "较前日"),
        (f"{s['c5']:.3f}", "C5 成本均线"),
        (f"{s['c13']:.3f}", "C13 成本均线"),
        (f"{s['amount'] / 1e8:,.0f} 亿", "成交额"),
        (f"{s['turnover'] * 100:.2f}%" if s.get("turnover") else "-", "全市场换手率"),
    ]
    kpi_html = "".join(f'<div class="kpi"><b>{_e(v)}</b><span>{_e(k)}</span></div>' for v, k in kpis)
    legend = "".join(
        f'<li><b>{_e(M.REGIONS[k][0])}</b>　{_e(M.REGIONS[k][1])}　→ {_e(M.REGIONS[k][2])}'
        f'{"　← 当前" if k == key else ""}</li>' for k in M.REGION_ORDER)
    return f'''<div class="card">
  <h3>活跃市值 0AMV<span class="zone z-{_e(key)}">{_e(s.get("region_name", "-"))}</span>
      <span class="act">建议：{_e(s.get("region_action", "-"))}</span></h3>
  <p class="sub2">口径：<code>0AMV = SMA(全市场成交额,10,1)</code>，柱体从昨日连到今日（红涨绿跌），
    叠加 5 日 / 13 日成本均线（DMA 加权）。<b>用本地股票池成交额复刻，不是指南针原值</b>，
    形态与拐点可比，绝对数值不等价。</p>
  <div class="kpis small">{kpi_html}</div>
  <div class="chartbox">{_market_kline_svg(series)}</div>
  <h4>四区域判别（0AMV 与两条成本均线的相对位置）</h4>
  <ul class="regions">{legend}</ul>
</div>'''


# --------------------------------------------------------------------------- 卡片
def _hits_text(r: dict, limit: int = 4) -> str:
    hits = [str(h) for h in (r.get("hits") or [])]
    if not hits:
        return "-"
    shown = " · ".join(_e(h) for h in hits[:limit])
    if len(hits) > limit:
        shown += " …"
    return f'<span title="{_e("、".join(hits))}">{shown}</span>'


def _card(r: dict, df: Optional[pd.DataFrame]) -> str:
    level = str(r.get("level"))
    label, cls = _LEVEL_META.get(level, (R.LEVEL_LABELS.get(level, level), "watch"))
    code = _e(r.get("code", ""))
    name = _e(r.get("name", ""))
    score = _clean(r.get("score"))
    j = _clean(r.get("j"))
    close = _price(r.get("close"))
    stop = _clean(r.get("stop_price"))
    stop_pct = _clean(r.get("stop_pct"))
    b1_date = str(r["b1_date"]) if r.get("b1_date") else ""

    if stop is not None:
        stop_v = f"{stop:.2f}" + (f' <small>{stop_pct:.1f}%</small>'
                                  if stop_pct is not None else "")
        stop_k = "止损"
    else:                       # 无信号股票没有止损计划，改看趋势
        stop_v = "多头" if r.get("trend") == "bull" else "破位"
        stop_k = "趋势"

    stats = [
        (close, "收盘" + _pct_chip(r)),
        ("-" if score is None else f"{score:.0f}", "得分"),
        ("-" if j is None else f"{j:.1f}", "J"),
        (stop_v, stop_k),
    ]
    stats_html = "".join(
        f'<div class="stat"><b>{v}</b><span>{k}</span></div>' for v, k in stats)

    legend = (
        f'<span class="lg"><i style="background:{_C_WHITE}"></i>白线 {_price(r.get("white"))}</span>'
        f'<span class="lg"><i style="background:{_C_YELLOW}"></i>黄线 {_price(r.get("yellow"))}</span>'
        f'<span class="lg"><i style="background:{_C_BBI}"></i>BBI {_price(r.get("bbi"))}</span>'
    )

    tags = [str(t) for t in (r.get("tags") or [])]
    groups = [str(g) for g in (r.get("groups") or [])]
    meta_bits = []
    if b1_date:
        meta_bits.append(f"B1日 {b1_date[5:]}")
    if groups:
        meta_bits.append("分组 " + "/".join(groups))
    if tags:
        meta_bits.append("标签 " + "/".join(tags))
    warnings = "".join(
        f'<span class="warn">{_e(C.WARN_LABELS.get(w, w))}</span>' for w in (r.get("warnings") or []))
    footer = ""
    if meta_bits or warnings:
        footer = (f'<footer class="card-foot"><span class="meta">{_e(" · ".join(meta_bits))}</span>'
                  f'{warnings}</footer>')

    return f'''
<article class="card lv-{cls}" data-level="{_e(level)}" data-code="{code}" data-name="{name}"
         data-b1-date="{_e(b1_date)}">
  <header class="card-head">
    <div class="ident"><span class="code">{code}</span><span class="name">{name}</span></div>
    <span class="badge">{_e(label)}</span>
  </header>
  <div class="stats">{stats_html}</div>
  <div class="legend">{legend}</div>
  <div class="chart">{_chart_svg(r, df)}</div>
  <div class="hits"><span class="k">命中</span>{_hits_text(r)}</div>
  {footer}
</article>'''.strip()


# --------------------------------------------------------------------------- 页面
_CSS = """
:root{--bg:#f3f4f6;--panel:#fff;--ink:#0f172a;--muted:#64748b;--line:#e2e8f0;
      --up:#e5484d;--down:#0f9d58;--accent:#2563eb;--chart:#0b1220}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC",
     "Hiragino Sans GB","Microsoft YaHei",system-ui,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1240px;margin:0 auto;padding:26px 20px 40px}
.page-head{display:flex;flex-wrap:wrap;gap:14px 24px;align-items:flex-end;justify-content:space-between;margin-bottom:14px}
h1{margin:0;font-size:22px;letter-spacing:.3px}
h1 .thin{font-weight:400;color:var(--muted);font-size:14px;margin-left:8px}
.sub{margin:6px 0 0;color:var(--muted);font-size:12.5px}
.sub code{font:inherit;background:#e9edf3;border-radius:4px;padding:1px 5px}
.counts{display:flex;gap:8px;flex-wrap:wrap}
.cnt{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 10px;font-size:12px;color:var(--muted)}
.cnt b{color:var(--ink);font-size:14px;margin-left:3px}
.toolbar{position:sticky;top:0;z-index:5;background:rgba(243,244,246,.94);backdrop-filter:blur(8px);
         border-bottom:1px solid var(--line);padding:10px 0;margin:6px 0 18px}
.toolbar .inner{max-width:1240px;margin:0 auto;padding:0 20px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.filters{display:flex;gap:6px;flex-wrap:wrap}
.filters button{font:inherit;font-size:12.5px;border:1px solid var(--line);background:var(--panel);color:#334155;
                border-radius:999px;padding:6px 12px;cursor:pointer;transition:.15s}
.filters button:hover{border-color:#cbd5e1}
.filters button.on{background:var(--ink);border-color:var(--ink);color:#fff}
.filters button b{font-weight:600;margin-left:4px;opacity:.75}
.search{margin-left:auto;position:relative}
.search input{font:inherit;font-size:12.5px;border:1px solid var(--line);background:var(--panel);border-radius:8px;
              padding:7px 11px;width:200px;outline:none}
.search input:focus{border-color:#93c5fd;box-shadow:0 0 0 3px #dbeafe}
.shown{font-size:12px;color:var(--muted);min-width:58px;text-align:right}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(430px,1fr));gap:18px}
.card{background:var(--panel);border:1px solid var(--line);border-left:4px solid #cbd5e1;border-radius:12px;
      padding:14px 16px 12px;box-shadow:0 1px 2px rgba(15,23,42,.05),0 8px 24px -18px rgba(15,23,42,.25)}
.card.lv-confirm{border-left-color:#dc2626}
.card.lv-perfect{border-left-color:#d97706}
.card.lv-standard{border-left-color:#2563eb}
.card.lv-watch{border-left-color:#94a3b8}
.card.lv-none{border-left-color:#e2e8f0}
.card-head{display:flex;align-items:center;justify-content:space-between;gap:10px}
.ident{display:flex;align-items:baseline;gap:8px;min-width:0}
.ident .code{font:600 15px/1 ui-monospace,SFMono-Regular,Menlo,monospace;color:#334155}
.ident .name{font-size:16px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.badge{flex:none;font-size:12px;font-weight:600;border-radius:6px;padding:3px 8px}
.lv-confirm .badge{background:#fee2e2;color:#b91c1c}
.lv-perfect .badge{background:#fef3c7;color:#b45309}
.lv-standard .badge{background:#dbeafe;color:#1d4ed8}
.lv-watch .badge{background:#e2e8f0;color:#475569}
.lv-none .badge{background:#f1f5f9;color:#94a3b8}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:12px 0 10px}
.stat b{display:block;font-size:16px;font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap}
.stat b small{font-size:11.5px;color:var(--muted);font-weight:500;margin-left:2px}
.stat>span{display:block;font-size:11px;color:var(--muted);margin-top:2px;white-space:nowrap}
.pct{font-size:11px;font-weight:600;border-radius:4px;padding:1px 4px;margin-left:4px;vertical-align:1px}
.pct.up{color:var(--up);background:#fee2e2}
.pct.down{color:var(--down);background:#dcfce7}
.pct.flat{color:var(--muted);background:#f1f5f9}
.legend{display:flex;gap:12px;flex-wrap:wrap;font-size:11.5px;color:var(--muted);margin:0 0 6px;font-variant-numeric:tabular-nums}
.lg{display:inline-flex;align-items:center;gap:5px}
.lg i{width:10px;height:2px;border-radius:1px;display:inline-block}
.chart{background:var(--chart);border-radius:10px;padding:6px 4px 2px;overflow:hidden}
.chart,.nochart{-webkit-print-color-adjust:exact;print-color-adjust:exact}
.kline{display:block;width:100%;height:auto}
.nochart{padding:44px 10px;text-align:center;color:#64748b;font-size:12px;background:var(--chart);border-radius:10px}
.hits{font-size:11.5px;color:#475569;margin:9px 0 0;line-height:1.55}
.hits .k{display:inline-block;font-size:10.5px;color:var(--muted);border:1px solid var(--line);border-radius:4px;
         padding:0 5px;margin-right:6px;vertical-align:1px}
.card-foot{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;
           margin-top:9px;padding-top:8px;border-top:1px dashed var(--line);font-size:11px;color:var(--muted);min-height:10px}
.warn{color:#b45309;background:#fef3c7;border-radius:4px;padding:1px 6px;font-size:10.5px;margin-left:6px}
.empty{background:var(--panel);border:1px dashed var(--line);border-radius:12px;padding:56px 20px;text-align:center;color:var(--muted)}
.empty b{display:block;color:var(--ink);font-size:17px;margin-bottom:6px}
.page-foot{margin-top:24px;padding-top:14px;border-top:1px solid var(--line);color:var(--muted);font-size:11.5px;
           display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap}
[hidden]{display:none!important}
@media (max-width:720px){.grid{grid-template-columns:1fr}.search{margin-left:0;width:100%}.search input{width:100%}.wrap{padding:18px 12px 32px}}
@media print{body{background:#fff}.toolbar{display:none}.card{box-shadow:none;break-inside:avoid}.wrap{max-width:none}}
/* ---- 标签页 ---- */
.tabs{display:flex;gap:6px;margin:0 0 14px;border-bottom:1px solid var(--line)}
.tabs button{font:inherit;font-size:14px;font-weight:600;background:none;border:0;border-bottom:2px solid transparent;
             color:var(--muted);padding:9px 14px;cursor:pointer;margin-bottom:-1px}
.tabs button:hover{color:#334155}
.tabs button.on{color:var(--ink);border-bottom-color:var(--ink)}
.tabs button b{font-weight:600;font-size:12px;opacity:.7;margin-left:4px}
.panel[hidden]{display:none}
/* ---- 活跃市值 ---- */
.card h3{margin:0 0 8px;font-size:15px;display:flex;align-items:center;gap:10px}
.card h4{margin:16px 0 6px;font-size:12.5px;color:var(--muted);font-weight:600}
.sub2{margin:0 0 12px;font-size:12px;color:var(--muted);line-height:1.6}
.kpis.small{grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:6px}
.kpis.small .kpi{border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.kpis.small .kpi b{font-size:16px}
.chartbox{background:#0b1220;border-radius:10px;padding:8px 6px 4px}
.chartbox .chart .axis{fill:#94a3b8;font-size:10.5px}
.zone{font-size:12px;font-weight:600;border-radius:6px;padding:2px 8px}
.z-up{background:#fee2e2;color:#b91c1c}
.z-down_hesitate{background:#ffedd5;color:#c2410c}
.z-down{background:#dcfce7;color:#15803d}
.act{font-size:12px;font-weight:400;color:var(--muted)}
ul.regions{margin:0;padding-left:18px;font-size:12.5px;color:#334155;line-height:1.9}
ul.regions b{font-weight:600}
.z-up_hesitate{background:#fef3c7;color:#b45309}
.z-unknown{background:#e2e8f0;color:#475569}
"""

_JS = """
(function(){
  var cards=[].slice.call(document.querySelectorAll('.card'));
  var bars=[].slice.call(document.querySelectorAll('.filters button'));
  var q=document.getElementById('q');
  var empty=document.getElementById('empty');
  var shown=document.getElementById('shown');
  var lv='all';
  function apply(){
    var s=(q&&q.value||'').trim().toLowerCase(), n=0;
    cards.forEach(function(c){
      var hit=(lv==='all'||c.getAttribute('data-level')===lv) &&
              (!s||((c.getAttribute('data-code')||'')+' '+(c.getAttribute('data-name')||'')).toLowerCase()
                     .indexOf(s)>=0);
      c.hidden=!hit; if(hit)n++;
    });
    if(empty) empty.hidden=n>0;
    if(shown) shown.textContent=n+' / '+cards.length;
  }
  bars.forEach(function(b){
    b.addEventListener('click',function(){
      lv=b.getAttribute('data-lv');
      bars.forEach(function(x){x.classList.toggle('on',x===b)});
      apply();
    });
  });
  if(q) q.addEventListener('input',apply);
  apply();
  var tabs=[].slice.call(document.querySelectorAll('.tabs button'));
  tabs.forEach(function(b){
    b.addEventListener('click',function(){
      var name=b.getAttribute('data-tab');
      tabs.forEach(function(x){x.classList.toggle('on',x===b)});
      [].slice.call(document.querySelectorAll('.panel')).forEach(function(p){
        p.hidden = p.id !== 'panel-'+name;
      });
    });
  });
})();
"""


def _ordered_all(run: dict, levels: tuple) -> list[dict]:
    """全部股票视图：按等级优先（确认→完美→标准→观察→无信号），同级保持股票池配置顺序。"""
    results = run.get("results") or []
    by_code = {str(r.get("code", "")): r for r in results}
    order = [c for c in (run.get("members") or {}) if c in by_code] or sorted(by_code)
    rank = {lv: i for i, lv in enumerate(levels)}
    return sorted((by_code[c] for c in order), key=lambda r: rank.get(str(r.get("level")), len(levels)))


def build_html(run: dict, cfg: dict, frames: Optional[Mapping[str, pd.DataFrame]] = None,
               paths: Optional[Paths] = None, include_all: bool = False,
               market_series: Optional[pd.DataFrame] = None) -> str:
    """生成完整页面。frames 缺省时按需从 paths.indicators(code) 回读。

    include_all=False：只画有 B1 信号（含观察）的股票；True：画出结果里全部股票（含无信号）。
    """
    days, include_watch = _card_cfg(cfg)
    results = run.get("results") or []
    if include_all:
        levels = C.LEVELS
    else:
        levels = CARD_LEVELS if include_watch else CARD_LEVELS[:3]
    if include_all:
        ordered = _ordered_all(run, levels)
    else:
        results = [r for r in results if r.get("level") in levels]
        ordered = []
        for lv in levels:
            ordered += R.by_level(results, lv)

    cards = []
    for r in ordered:
        df = _frame_for(str(r.get("code", "")), frames, run.get("date"), days, paths)
        cards.append(_card(r, df))

    counts = {lv: sum(1 for r in ordered if r.get("level") == lv) for lv in levels}
    total = len(ordered)
    date = _e(run.get("date", "-"))
    weekday = _e(R._weekday(run)) if run.get("date") else ""
    view = f"｜{_e(run['view'])}" if run.get("view") else ""
    title = "K线卡片"
    scope = "全部股票" if include_all else "仅 B1 信号"

    count_html = f'<span class="cnt">全部<b>{total}</b></span>' + "".join(
        f'<span class="cnt">{_e(_LEVEL_META[lv][0])}<b>{counts[lv]}</b></span>' for lv in levels)
    filters = f'<button class="on" data-lv="all">全部 <b>{total}</b></button>' + "".join(
        f'<button data-lv="{_e(lv)}">{_e(_LEVEL_META[lv][0])} <b>{counts[lv]}</b></button>' for lv in levels)

    if ordered:
        body = f'<div class="grid">{"".join(cards)}</div>\n<p id="empty" class="empty" hidden><b>没有匹配的股票</b>试试其他等级或关键词。</p>'
    elif include_all:
        body = ('<div class="empty"><b>没有可展示的股票</b>'
                '所选分组/标签在当日结果中没有股票，或本地还缺少 K 线缓存。</div>')
    else:
        body = ('<div class="empty"><b>今日无 B1 信号</b>'
                '股票池中没有符合 B1 条件（或观察）的股票，收盘后再运行 run 看看。</div>')

    market_html = _market_panel(market_series, cfg)

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<title>XB1 · {title} {date}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="page-head">
    <div>
      <h1>XB1 · {title}<span class="thin">{date}（{weekday}）｜{scope}</span></h1>
      <p class="sub">股票池 {_e(run.get("universe_size", len(results)))}｜引擎 {_e(R._ver(run))}
        ｜校验 <code>{_e(R._checksum(run))}</code>{view}｜近 {days} 个交易日</p>
    </div>
    <div class="counts">{count_html}</div>
  </header>
  <nav class="tabs">
    <button class="on" data-tab="cards">{title} <b>{total}</b></button>
    <button data-tab="market">活跃市值</button>
  </nav>
  <section class="panel" id="panel-cards">
    <div class="toolbar">
      <div class="inner">
        <div class="filters">{filters}</div>
        <div class="search"><input id="q" type="search" placeholder="代码 / 名称" aria-label="搜索股票"></div>
        <span class="shown" id="shown">{total} / {total}</span>
      </div>
    </div>
    {body}
  </section>
  <section class="panel" id="panel-market" hidden>
    {market_html}
  </section>
  <footer class="page-foot">
    <span>红涨绿跌｜白线 EMA(EMA(C,10),10)｜黄线 (MA14+MA28+MA57+MA114)/4｜BBI (MA3+MA6+MA12+MA24)/4</span>
    <span>{_e(R.DISCLAIMER)}</span>
  </footer>
</div>
<script>{_JS}</script>
</body>
</html>
'''


def write_html(out: Path, run: dict, cfg: dict,
               frames: Optional[Mapping[str, pd.DataFrame]] = None,
               paths: Optional[Paths] = None, include_all: bool = False,
               market_series: Optional[pd.DataFrame] = None) -> Path:
    """原子写任意目标的 HTML 页面。market_series 缺省时按本地 K 线现算（带缓存）。"""
    out = Path(out)
    if market_series is None and paths is not None:
        try:
            from . import market as M
            market_series = M.load_or_build(paths, cfg, kline_latest=str(run.get("date") or ""))
            d = str(run.get("date") or "")
            if d and market_series is not None and len(market_series):
                # 只显示到复盘日，避免历史回放时把"今天"的行情画进旧页面
                market_series = market_series[market_series["date"].astype(str) <= d].reset_index(drop=True)
        except Exception:
            market_series = None
    atomic.write_text(out, build_html(run, cfg, frames=frames, paths=paths,
                                      include_all=include_all, market_series=market_series))
    return out


def write_cards(paths: Paths, run: dict, cfg: dict,
                frames: Optional[Mapping[str, pd.DataFrame]] = None) -> Path:
    """原子写 HTML；默认落到 data/reports/<日期>/b1_cards.html。"""
    return write_html(paths.report_html(str(run.get("date"))), run, cfg, frames=frames, paths=paths)
