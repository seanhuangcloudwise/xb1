"""gc：按 retention 清理过期的 runs/、logs/、raw/universe（PRD 5.1.2-6）。
history/、raw/kline、reports/ 永不自动清理。"""
from __future__ import annotations

import datetime as _dt
import re
import shutil
from pathlib import Path

from .paths import Paths

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _age_days(name: str, today: str) -> int | None:
    m = _DATE_RE.search(name)
    if not m:
        return None
    try:
        return (_dt.date.fromisoformat(today) - _dt.date.fromisoformat(m.group(1))).days
    except ValueError:
        return None


def _sweep(directory: Path, keep_days: int, today: str, dirs: bool, pattern: str = "*") -> tuple[int, int]:
    removed = kept = 0
    if not directory.exists():
        return 0, 0
    for p in sorted(directory.glob(pattern)):
        if p.is_dir() != dirs or p.is_symlink():
            continue
        age = _age_days(p.name, today)
        if age is None or age <= keep_days:
            kept += 1
            continue
        if dirs:
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                p.unlink()
            except OSError:
                continue
        removed += 1
    return removed, kept


def gc(paths: Paths, cfg: dict, today: str) -> str:
    r = cfg.get("retention") or {}
    plan = [
        ("runs", paths.runs, int(r.get("runs_days", 365)), True, "*"),
        ("logs(run)", paths.logs, int(r.get("logs_days", 90)), False, "run-*.log"),
        ("logs(westock)", paths.logs, int(r.get("westock_logs_days", 30)), False, "westock-*.log"),
        ("raw/universe", paths.universe_dir, int(r.get("universe_days", 90)), False, "*.json"),
    ]
    lines = ["【XB1 gc】"]
    for label, d, days, is_dir, pat in plan:
        removed, kept = _sweep(d, days, today, is_dir, pat)
        lines.append(f"{label}：删除 {removed}，保留 {kept}（保留 {days} 天）")
    lines.append("history/、raw/kline、reports/ 不清理。")
    return "\n".join(lines) + "\n"
