"""跨模块共享的数据模型与小工具。"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Iterable, Optional

_CODE_RE = re.compile(r"(\d{6})")


def normalize_code(raw: str) -> str:
    """'sz300124' / '300124.SZ' / ' 300124 ' -> '300124'。无 6 位数字则抛 ValueError。"""
    m = _CODE_RE.search(str(raw))
    if not m:
        raise ValueError(f"无效股票代码：{raw!r}")
    return m.group(1)


def market_prefix(code: str) -> str:
    """6 位代码 -> westock 市场前缀 sh/sz/bj。"""
    c = normalize_code(code)
    if c.startswith(("60", "68", "900")):
        return "sh"
    if c.startswith(("00", "30", "200")):
        return "sz"
    if c.startswith(("8", "4", "92")):
        return "bj"
    return "sh" if c.startswith("6") else "sz"


def board_of(code: str) -> str:
    """main / gem(创业板 30) / star(科创板 688) / bj。"""
    c = normalize_code(code)
    if c.startswith("688") or c.startswith("689"):
        return "star"
    if c.startswith("30"):
        return "gem"
    if market_prefix(c) == "bj":
        return "bj"
    return "main"


def is_st_name(name: str) -> bool:
    n = (name or "").replace(" ", "").upper()
    return n.startswith(("ST", "*ST", "S*ST", "SST")) or "退" in n[:4] or n.endswith("退")


@dataclass
class StockMeta:
    code: str                       # 6 位
    name: str = ""
    board: str = "main"             # main/gem/star/bj
    listed_date: Optional[str] = None   # YYYY-MM-DD
    groups: tuple = ()              # 所属股票池分组（config.yaml universe.groups 的键），可多个
    tags: tuple = ()                # 档案里的人工标签（data/portfolio/{code}.yaml）

    @classmethod
    def make(cls, code: str, name: str = "", listed_date: Optional[str] = None,
             groups: Iterable[str] = (), tags: Iterable[str] = ()) -> "StockMeta":
        c = normalize_code(code)
        return cls(code=c, name=name, board=board_of(c), listed_date=listed_date,
                   groups=tuple(groups), tags=tuple(tags))

    def to_dict(self) -> dict:
        return asdict(self)
