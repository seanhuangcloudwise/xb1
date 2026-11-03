"""信号历史流水与 T+N 收益回填（PRD 5.1.1 / DEV_CONTRACTS「history.py」）。

收益口径：以信号日在 raw/kline 中的收盘为基准，取其后第 N 个交易日（K 线行）收盘，
ret_tN = close[T+N] / close[T] - 1，单位为百分数（2.5 表示 +2.5%）。未到期留空（NaN）。
"""
from __future__ import annotations

import math

import pandas as pd

from . import atomic
from . import constants as C

SIGNAL_LEVELS = (C.LEVEL_CONFIRM, C.LEVEL_PERFECT, C.LEVEL_STANDARD, C.LEVEL_WATCH)

HISTORY_COLUMNS = ["date", "code", "name", "board", "level", "base_level", "score", "j", "close",
                   "pct_chg", "stop_price", "stop_pct", "stop_wide", "b1_date", "hits",
                   "checksum", "engine_version"]
_FLOAT_COLS = ["score", "j", "close", "pct_chg", "stop_price", "stop_pct"]


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return float("nan")
    return v


def _empty_history() -> pd.DataFrame:
    df = pd.DataFrame({c: pd.Series(dtype="object") for c in HISTORY_COLUMNS})
    for c in _FLOAT_COLS:
        df[c] = df[c].astype("float64")
    df["stop_wide"] = df["stop_wide"].astype("bool")
    return df


def _rows_from_run(run: dict) -> pd.DataFrame:
    rows = []
    for r in run.get("results") or []:
        if r.get("level") not in SIGNAL_LEVELS:
            continue
        rows.append({
            "date": str(r.get("date") or run.get("date")), "code": str(r.get("code")),
            "name": r.get("name") or "", "board": r.get("board") or "", "level": r.get("level"),
            "base_level": r.get("base_level") or C.LEVEL_NONE,
            "score": _f(r.get("score")), "j": _f(r.get("j")), "close": _f(r.get("close")),
            "pct_chg": _f(r.get("pct_chg")), "stop_price": _f(r.get("stop_price")),
            "stop_pct": _f(r.get("stop_pct")), "stop_wide": bool(r.get("stop_wide")),
            "b1_date": r.get("b1_date") or "", "hits": "、".join(r.get("hits") or []),
            "checksum": str(run.get("checksum") or ""), "engine_version": str(run.get("engine_version") or ""),
        })
    if not rows:
        return _empty_history()
    return pd.DataFrame(rows, columns=HISTORY_COLUMNS)


def read_history(paths) -> pd.DataFrame:
    p = paths.signals_history
    if not p.exists():
        return _empty_history()
    return pd.read_parquet(p)


def append_history(paths, run: dict) -> int:
    """追加当日信号（仅 confirm/perfect/standard/watch）。同日重复运行：先删该日旧行再写入。返回写入行数。"""
    date = str(run["date"])
    old = read_history(paths)
    old = old[old["date"] != date]
    new = _rows_from_run(run)
    parts = [d for d in (old, new) if len(d)]
    df = pd.concat(parts, ignore_index=True) if parts else _empty_history()
    df = df.sort_values(["date", "code"], kind="stable").reset_index(drop=True)
    atomic.write_parquet(paths.signals_history, df)
    return len(new)


def _load_kline(paths, code: str, cache: dict):
    if code in cache:
        return cache[code]
    p = paths.kline(code)
    out = None
    if p.exists():
        try:
            k = pd.read_parquet(p, columns=["date", "close"])
            k = k.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
            out = (k["date"].astype(str).tolist(), k["close"].astype(float).tolist())
        except Exception:
            out = None
    cache[code] = out
    return out


def backfill(paths, cfg: dict, calendar_days=(3, 5, 10)) -> dict:
    """回填 T+N 收益，写 outcomes.parquet，返回汇总。

    返回结构::
        {"total": 信号总数, "days": [3,5,10],
         "levels": {level: {"signals": n, "T+3": {"n": 已到期样本数, "win_rate": 0~1 或 None,
                                                   "avg_return": 百分数 或 None}, ...}}}
    """
    days = tuple(int(n) for n in calendar_days)
    hist = read_history(paths)
    hist = hist[hist["level"].isin(SIGNAL_LEVELS)].drop_duplicates(["date", "code"], keep="last")
    cache: dict = {}
    rows = []
    for h in hist.itertuples(index=False):
        row = {"date": h.date, "code": h.code, "name": h.name, "level": h.level, "base_close": float("nan")}
        for n in days:
            row[f"date_t{n}"] = None
            row[f"ret_t{n}"] = float("nan")
        kl = _load_kline(paths, h.code, cache)
        if kl is not None and h.date in kl[0]:
            dates, closes = kl
            i = dates.index(h.date)
            base = closes[i]
            row["base_close"] = base
            if base and not math.isnan(base) and base > 0:
                for n in days:
                    if i + n < len(closes):
                        row[f"date_t{n}"] = dates[i + n]
                        row[f"ret_t{n}"] = (closes[i + n] / base - 1.0) * 100.0
        rows.append(row)

    cols = (["date", "code", "name", "level", "base_close"]
            + [c for n in days for c in (f"date_t{n}", f"ret_t{n}")])
    out = pd.DataFrame(rows, columns=cols)
    for n in days:
        out[f"ret_t{n}"] = out[f"ret_t{n}"].astype("float64")
    out["base_close"] = out["base_close"].astype("float64")
    atomic.write_parquet(paths.outcomes_history, out)

    levels: dict = {}
    for lv in SIGNAL_LEVELS:
        sub = out[out["level"] == lv]
        entry: dict = {"signals": int(len(sub))}
        for n in days:
            s = sub[f"ret_t{n}"].dropna()
            entry[f"T+{n}"] = {
                "n": int(len(s)),
                "win_rate": float((s > 0).mean()) if len(s) else None,
                "avg_return": float(s.mean()) if len(s) else None,
            }
        levels[lv] = entry
    return {"total": int(len(out)), "days": list(days), "levels": levels}


def format_summary(summary: dict) -> str:
    """把 backfill 汇总渲染成固定格式文本（供 CLI 直接输出）。"""
    from .render import LEVEL_LABELS

    days = summary.get("days") or []
    lines = [f"【B1 回填】信号总数 {summary.get('total', 0)}（胜率=收益>0；收益为收盘价口径）"]
    lines += ["| 等级 | 信号数 | " + " | ".join(f"T+{n} 样本/胜率/均收益" for n in days) + " |",
              "|---|---|" + "---|" * len(days)]
    for lv in SIGNAL_LEVELS:
        e = (summary.get("levels") or {}).get(lv) or {"signals": 0}
        cells = []
        for n in days:
            s = e.get(f"T+{n}") or {"n": 0, "win_rate": None, "avg_return": None}
            if s["n"]:
                cells.append(f"{s['n']} / {s['win_rate'] * 100:.1f}% / {s['avg_return']:.2f}%")
            else:
                cells.append("0 / - / -")
        lines.append(f"| {LEVEL_LABELS[lv]} | {e['signals']} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"
