"""股票池名单构建：从数据源生成主板/创业板/科创板/北交所的代码表。

用途：`pools/main_board.txt` 这类名单会随新股/退市/转板变化，需要定期重建。
只生成代码清单文件（每行一个 6 位代码 + 头注释），不改 config.yaml。
"""
from __future__ import annotations

from pathlib import Path

from . import atomic
from .models import board_of, normalize_code
from .provider_base import DataProvider

BOARDS = {
    "main": ("主板", lambda c: board_of(c) == "main"),
    "gem": ("创业板", lambda c: board_of(c) == "gem"),
    "star": ("科创板", lambda c: board_of(c) == "star"),
    "bj": ("北交所", lambda c: board_of(c) == "bj"),
    "all": ("全部 A 股", lambda c: True),
}


def build_codes(provider: DataProvider, board: str = "main") -> tuple[list[str], int]:
    """返回 (符合该板块的代码升序列表, 数据源返回的总数)。"""
    if board not in BOARDS:
        raise ValueError(f"未知板块 {board!r}，可选：{'/'.join(BOARDS)}")
    listing = provider.list_universe()          # [{"code": 6 位, "name": ...}]
    total = len(listing)
    pred = BOARDS[board][1]
    codes, seen = [], set()
    for item in listing:
        try:
            c = normalize_code(item.get("code") or item.get("symbol") or "")
        except ValueError:
            continue
        if c in seen or not pred(c):
            continue
        seen.add(c)
        codes.append(c)
    return sorted(codes), total


def read_pool_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        try:
            out.append(normalize_code(s.split()[0]))
        except ValueError:
            continue
    return out


def write_pool_file(path: Path, codes: list[str], board: str, source: str) -> None:
    label = BOARDS[board][0]
    header = (f"# {label} A 股代码表（{len(codes)} 只），由 xb1.py pool build 生成，勿手工编辑\n"
              f"# 板块：{board}｜数据源：{source}｜重新生成：python3 scripts/xb1.py pool build --board {board}\n")
    atomic.write_text(path, header + "\n".join(codes) + "\n")


def diff_codes(old: list[str], new: list[str]) -> tuple[list[str], list[str]]:
    o, n = set(old), set(new)
    return sorted(n - o), sorted(o - n)
