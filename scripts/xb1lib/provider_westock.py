"""westock CLI 适配器。

!!! 实机校对入口 !!!
westock 的真实输出格式在沙箱里拿不到。凡是“假设”的东西都集中在本文件顶部的常量与下面的
parse_* 小函数里；实机只需要调整：
  1. CMD_* 命令模板（参数顺序/开关名）；
  2. *_ALIASES 列名映射（westock 列名 -> 标准列）；
  3. RATE_LIMIT_MARK、FQ_ARG、VOLUME_FACTOR；
  4. cfg.fetch.amount_unit（成交额单位系数，单元格自带 万/亿/元 单位时不再乘）。
全部假设列在 references/westock-assumptions.md。

解析总原则：parse_md_table 宽容（多表、前后说明文字、|---| 对齐行、千分位逗号、带单位），
但拿不到表头或数据行时一律抛 ParseError，绝不返回空 DataFrame（PRD 3.1-2）。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

import pandas as pd

from .calendar import normalize_date, shanghai_today
from .constants import FETCH_FAILED, KLINE_COLUMNS
from .errors import ParseError, RateLimited, SourceUnavailable
from .models import market_prefix, normalize_code
from .paths import Paths
from .provider_base import MAX_BATCH, MAX_PROFILE_BATCH, DataProvider

DEFAULT_CMD = "westock"

# ======================================================================================
# 1. 命令模板（实机校对）。元素里的 {codes}/{count}/{fq}/{name}/{code} 会被 str.format 替换；
#    可执行文件名取自 cfg.fetch.command。每个元素是独立 argv，不经 shell。
# ======================================================================================
CMD_VERSION = ["version", "--json"]   # 实测：`westock version --json` 返回 JSON；`--version` 未测
CMD_KLINE = ["kline", "{codes}", "--period", "day", "--limit", "{count}", "--fq", "{fq}"]  # 实测：--limit，不是 --count
CMD_SEARCH_SECTOR = ["search", "{name}", "--type", "sector"]
CMD_SECTOR_CONSTITUENT = ["sector", "constituent", "{code}"]
CMD_TRADE_CALENDAR = ["trade-calendar", "--trading-only", "--year", "{year}", "--limit", "9999"]  # 实测：必须指定年份，--limit 9999 才能拿全年（默认 limit≈50）
CMD_TRADE_CALENDAR_YEARS = 8   # 拉最近 N 年（当年 + 前 N-1 年），覆盖 250 日 K 线所需历史
CMD_PROFILE = ["profile", "{codes}"]  # 实测：支持逗号批量，格式同 kline（sh/sz 前缀逗号分隔）

CMD_TIMEOUT_SEC = 60          # 单次 subprocess 超时；可被 cfg.fetch.command_timeout 覆盖
RATE_LIMIT_MARK = "LOCAL_RATE_LIMITED"   # 兼容旧引用；判定请用 RATE_LIMIT_RE
# 实测两种限流：本地 [LOCAL_RATE_LIMITED] 请求过于频繁；服务端 [SERVER_RATE_LIMITED] ... msg=服务限频
# （后者输出 "[Batch] 状态: error | 失败: N"，退出码仍为 0，不能只看退出码或表格）。任意位置出现即视为整次调用限流。
RATE_LIMIT_RE = re.compile(r"[A-Z]+_RATE_LIMITED|服务限频|请求过于频繁")
FQ_ARG = {"qfq": "qfq", "hfq": "hfq", "nofq": "nofq", "none": "nofq", "": "nofq"}  # cfg.fetch.fq -> CLI 值
VOLUME_FACTOR = 1.0           # 成交量换算到“股”的系数（westock 是手还是股未知；指标只用比值，默认不换算）

# ======================================================================================
# 2. 列名映射（实机校对）。key 为 westock 表头（比较前会 _norm_header：小写、去空格/下划线/括号单位/%）。
# ======================================================================================
KLINE_COLUMN_MAP = {            # PRD 3 章实测字段 -> 标准列
    "date": "date", "open": "open", "last": "close", "high": "high", "low": "low",
    "volume": "volume", "amount": "amount", "exchange": "turnover", "change_pct": "pct_chg",
}
KLINE_COLUMN_EXTRA_ALIASES = {  # 宽容别名（猜测，非实测）
    "日期": "date", "时间": "date", "tradedate": "date", "day": "date",
    "开盘": "open", "开盘价": "open", "今开": "open",
    "收盘": "close", "收盘价": "close", "close": "close", "最新": "close", "最新价": "close",
    "最高": "high", "最高价": "high", "最低": "low", "最低价": "low",
    "成交量": "volume", "vol": "volume", "成交额": "amount", "amt": "amount",
    "换手": "turnover", "换手率": "turnover", "turnover": "turnover", "turnoverrate": "turnover",
    "涨跌幅": "pct_chg", "涨幅": "pct_chg", "changepct": "pct_chg", "pctchg": "pct_chg", "pctchange": "pct_chg",
}
KLINE_REQUIRED = ["date", "open", "close", "high", "low", "volume", "amount"]  # turnover/pct_chg 缺失记 NaN

CODE_ALIASES = {"code", "symbol", "stockcode", "secucode", "代码", "股票代码", "证券代码", "板块代码", "sectorcode", "id"}
NAME_ALIASES = {"name", "stockname", "secuname", "名称", "股票名称", "证券简称", "简称", "证券名称", "板块名称", "sectorname"}
LISTED_DATE_ALIASES = {"listeddate", "listdate", "listingdate", "ipodate", "上市日期", "上市时间"}
DATE_ALIASES = {"date", "日期", "tradedate", "tradingday", "交易日", "交易日期", "day", "calendardate"}
TRADING_FLAG_ALIASES = {"istrading", "isopen", "trading", "open", "tradeflag", "是否交易", "是否交易日", "是否开市", "交易状态"}
TRUTHY = {"1", "1.0", "true", "yes", "y", "是", "交易", "开市", "open", "trading", "t"}

_MISSING = {"", "-", "--", "---", "—", "——", "n/a", "na", "nan", "null", "none", "/"}

# ======================================================================================
# 3. Markdown 表格解析
# ======================================================================================
_SEP_CELL = re.compile(r"^:?-+:?$")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_CODE_TOKEN = re.compile(r"(?<![0-9A-Za-z])(?:sh|sz|bj)?(\d{6})(?!\d)", re.I)
_PAREN = re.compile(r"[\(（\[【][^\)）\]】]*[\)）\]】]")
_SCALE = {"万亿": 1e12, "亿": 1e8, "万": 1e4, "千": 1e3}
_NUM_RE = re.compile(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(.*)$")
_UNIT_RE = re.compile(r"^(万亿|亿|万|千)?(元|股|手|%|倍|点)?$")
_DATE_IN_TEXT = re.compile(r"(?<!\d)(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{8})(?!\d)")


def _norm_header(h: str) -> str:
    s = _PAREN.sub("", str(h))
    s = s.replace("%", "").replace("*", "").replace("`", "")
    return re.sub(r"[\s_\-]+", "", s).lower()


def _norm_set(items: Iterable[str]) -> set[str]:
    return {_norm_header(x) for x in items}


_CODE_N, _NAME_N = _norm_set(CODE_ALIASES), _norm_set(NAME_ALIASES)
PROFILE_EXTRA_ALIASES = {     # 实测 profile 表头 -> 档案字段（只取这三个，其余如电话/地址不要）
    "business": "business", "industry": "industry", "sector": "sector",
}
_LISTED_N, _DATE_N, _FLAG_N = _norm_set(LISTED_DATE_ALIASES), _norm_set(DATE_ALIASES), _norm_set(TRADING_FLAG_ALIASES)
_KLINE_ALIAS_N = {_norm_header(k): v for k, v in {**KLINE_COLUMN_EXTRA_ALIASES, **KLINE_COLUMN_MAP}.items()}


def parse_number_unit(s) -> tuple[Optional[float], bool]:
    """'1,234.5' / '2.5%' / '3.2亿' / '--' -> (值, 是否带显式金额单位[万/亿/千/元])。缺失值返回 (None, False)；
    不是数字抛 ValueError。千分位逗号、全角逗号、空格均忽略；'万/亿' 会换算成绝对数。"""
    if s is None:
        return None, False
    if isinstance(s, (int, float)):
        return (None, False) if s != s else (float(s), False)
    t = str(s).strip().replace(",", "").replace("，", "").replace(" ", "").replace(" ", "")
    t = t.replace("−", "-").replace("－", "-").replace("＋", "+")
    if t.lower() in _MISSING:
        return None, False
    m = _NUM_RE.match(t)
    if not m:
        raise ValueError(f"非数字：{s!r}")
    val, rest = float(m.group(1)), m.group(2)
    um = _UNIT_RE.match(rest)
    if not um:
        raise ValueError(f"无法识别的单位：{s!r}")
    scale, unit = um.group(1), um.group(2)
    if scale:
        val *= _SCALE[scale]
    return val, bool(scale or unit == "元")


def parse_number(s) -> Optional[float]:
    return parse_number_unit(s)[0]


@dataclass
class RawTable:
    header: list[str]
    rows: list[list[str]]          # 全是字符串，已 strip 并去掉 ** 与反引号


@dataclass
class _Segment:
    code: Optional[str] = None     # 该段标题/标签行里的（请求范围内的）6 位代码
    tables: list[RawTable] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [re.sub(r"\*\*|`", "", c.replace("\\|", "|")).strip() for c in re.split(r"(?<!\\)\|", s)]


def _is_sep(cells: list[str]) -> bool:
    nz = [c.replace(" ", "") for c in cells if c.strip()]
    return bool(nz) and all(_SEP_CELL.match(c) for c in nz)


def _build_table(block: list[str]) -> tuple[list[str], Optional[RawTable]]:
    """一段连续含 '|' 的行 -> (被当作说明文字的行, 表)。以对齐行 |---| 定位表头；无对齐行时要求 ≥2 列。"""
    rows = [_split_row(l) for l in block]
    k = next((i for i in range(1, len(rows)) if _is_sep(rows[i])), None)
    if k is not None:
        pre, hdr_i = block[:k - 1], k - 1
    else:
        if _is_sep(rows[0]) or len(rows[0]) < 2:
            return block, None
        pre, hdr_i = [], 0
    header = rows[hdr_i]
    body = rows[k + 1:] if k is not None else rows[1:]
    hdr_l = [h.lower() for h in header]
    out: list[list[str]] = []
    for r in body:
        if _is_sep(r) or [c.lower() for c in r] == hdr_l:   # 续页重复表头/多余对齐行
            continue
        if len(r) < len(header):
            r = r + [""] * (len(header) - len(r))
        elif len(r) > len(header):
            if any(c for c in r[len(header):]):
                raise ParseError(f"表格行列数({len(r)})多于表头({len(header)})：{'|'.join(r)[:80]}")
            r = r[:len(header)]
        out.append(r)
    seen: dict[str, int] = {}
    fixed = []
    for i, h in enumerate(header):
        h = h or f"col{i}"
        if h in seen:
            seen[h] += 1
            h = f"{h}.{seen[h]}"
        else:
            seen[h] = 0
        fixed.append(h)
    return pre, RawTable(fixed, out)


def _iter_items(text: str):
    text = _ANSI.sub("", text or "")
    lines = text.splitlines()
    i, n = 0, len(lines)
    while i < n:
        if "|" in lines[i] and lines[i].strip():
            j = i
            while j < n and "|" in lines[j] and lines[j].strip():
                j += 1
            pre, tbl = _build_table(lines[i:j])
            for l in pre:
                yield "text", l
            if tbl is not None:
                yield "table", tbl
            i = j
        else:
            yield "text", lines[i]
            i += 1


def parse_md_tables_raw(text: str, keep_empty: bool = False) -> list[RawTable]:
    """文本里所有表（字符串单元格）。默认丢弃无数据行的表。"""
    tabs = [x for kind, x in _iter_items(text) if kind == "table"]
    return tabs if keep_empty else [t for t in tabs if t.rows]


def _convert_columns(t: RawTable, numeric: bool) -> pd.DataFrame:
    df = pd.DataFrame(t.rows, columns=t.header)
    if not numeric:
        return df
    protected = _CODE_N | _NAME_N | _DATE_N | _LISTED_N
    for col in df.columns:
        if _norm_header(col) in protected:
            continue
        try:
            vals = [parse_number(c) for c in df[col]]
        except ValueError:
            continue
        if any(re.fullmatch(r"0\d+", str(c).strip()) for c in df[col]):   # 000001 这类代码保持字符串
            continue
        df[col] = pd.Series(vals, dtype="float64", index=df.index)   # 全缺失的列 -> 全 NaN
    return df


def parse_md_table(text: str, required: Optional[Iterable[str]] = None, numeric: bool = True) -> pd.DataFrame:
    """文本 -> DataFrame（第一张满足条件的表）。

    宽容：多表、前后说明文字、|---| 对齐行、千分位逗号、带单位（%/万/亿/元）、**粗体**、ANSI 颜色码。
    required：若给定，选第一张表头（经 _norm_header）包含全部这些列名的表；否则选第一张有数据行的表。
    numeric=True 时，非代码/日期/名称列若全部单元格可解析为数字则转 float（缺失值 -> NaN；万/亿 换算为绝对数）。
    找不到表头或数据行 -> ParseError（绝不返回空）。
    """
    tabs = parse_md_tables_raw(text)
    if not tabs:
        raise ParseError(f"未找到带数据行的 Markdown 表格；输出前 200 字符：{(text or '')[:200]!r}")
    if required:
        need = {_norm_header(r) for r in required}
        for t in tabs:
            if need <= {_norm_header(h) for h in t.header}:
                return _convert_columns(t, numeric)
        raise ParseError(f"没有表同时含有列 {sorted(need)}；表头们：{[t.header for t in tabs][:3]}")
    return _convert_columns(tabs[0], numeric)


def _find_col(header: list[str], normset: set[str]) -> Optional[int]:
    for i, h in enumerate(header):
        if _norm_header(h) in normset:
            return i
    return None


def _wanted_codes(codes: Iterable[str]) -> list[str]:
    return [normalize_code(c) for c in codes]


def _segments(text: str, wanted: list[str]) -> list[_Segment]:
    """按 `## sz300124` 之类小标题/加粗行/短标签行把输出切成段；段内的表归属该代码。
    标签行里的 6 位代码不在 wanted 内则该段无归属（code=None），避免误挂。"""
    wset = set(wanted)
    segs = [_Segment()]
    for kind, x in _iter_items(text):
        if kind == "table":
            segs[-1].tables.append(x)
            continue
        s = x.strip()
        if not s:
            continue
        is_head = s.startswith("#") or s.startswith("**") or s.startswith("__")
        if is_head or len(s) <= 80:
            found = [m.group(1) for m in _CODE_TOKEN.finditer(s)]
            hit = next((c for c in found if c in wset), None)
            if hit or (is_head and found):
                segs.append(_Segment(code=hit))
                segs[-1].lines.append(s)
                continue
        segs[-1].lines.append(s)
    return segs


# ---- K 线 ----
def _map_kline_header(header: list[str]) -> dict[str, int]:
    cmap: dict[str, int] = {}
    for i, h in enumerate(header):
        std = _KLINE_ALIAS_N.get(_norm_header(h))
        if std and std not in cmap:
            cmap[std] = i
    return cmap


def _kline_frame(header: list[str], rows: list[list[str]], cmap: dict[str, int], amount_unit: float) -> pd.DataFrame:
    missing = [c for c in KLINE_REQUIRED if c not in cmap]
    if missing:
        raise ParseError(f"K 线表缺少列 {missing}；表头={header}")
    recs = []
    for ri, r in enumerate(rows):
        rec: dict = {}
        try:
            rec["date"] = normalize_date(r[cmap["date"]])
        except ValueError as e:
            raise ParseError(f"K 线第 {ri + 1} 行日期无法解析：{e}")
        for col in ("open", "close", "high", "low", "volume", "amount", "turnover", "pct_chg"):
            if col not in cmap:
                rec[col] = float("nan")
                continue
            raw = r[cmap[col]]
            try:
                v, explicit = parse_number_unit(raw)
            except ValueError as e:
                raise ParseError(f"K 线第 {ri + 1} 行 {col} 无法解析：{e}")
            if v is None:
                if col in ("turnover", "pct_chg"):
                    rec[col] = float("nan")
                    continue
                raise ParseError(f"K 线第 {ri + 1} 行 {col} 缺失：{raw!r}")
            if col == "amount" and not explicit:
                v *= amount_unit
            elif col == "volume":
                v *= VOLUME_FACTOR
            rec[col] = v
        recs.append(rec)
    df = pd.DataFrame(recs, columns=KLINE_COLUMNS)
    df = df.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
    if df.empty:
        raise ParseError("K 线无有效数据行")
    return df


def parse_kline_batch(text: str, codes: list[str], amount_unit: float = 1.0
                      ) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """批量 K 线输出 -> ({code: 标准 df}, {code: 失败原因})。

    拆分策略（宽容）：
      1) 表里有代码列（code/symbol/代码…）-> 按代码列分组；
      2) 否则按表前最近的“标签行”（## sz300124 / **sz300124** / 含代码的短行）归属；
      3) 只请求 1 只时，未标注的 K 线表归属该只。
    拆不出来的股票记单只失败。整段输出里一张 K 线表（含 date+收盘列）都没有 -> ParseError。
    """
    wanted = _wanted_codes(codes)
    wset = set(wanted)
    segs = _segments(text, wanted)
    parts: dict[str, list[pd.DataFrame]] = {c: [] for c in wanted}
    errs: dict[str, str] = {}
    found_table = False
    for seg in segs:
        for t in seg.tables:
            cmap = _map_kline_header(t.header)
            if "date" not in cmap or "close" not in cmap or not t.rows:
                continue
            found_table = True
            ci = _find_col(t.header, _CODE_N)
            groups: dict[str, list[list[str]]] = {}
            if ci is not None:
                for r in t.rows:
                    m = _CODE_TOKEN.search(r[ci])
                    if m and m.group(1) in wset:
                        groups.setdefault(m.group(1), []).append(r)
            elif seg.code:
                groups[seg.code] = t.rows
            elif len(wanted) == 1:
                groups[wanted[0]] = t.rows
            for c, rows in groups.items():
                try:
                    parts[c].append(_kline_frame(t.header, rows, cmap, amount_unit))
                except ParseError as e:
                    errs[c] = f"{FETCH_FAILED}: parse_error: {e}"
    if not found_table:
        raise ParseError(f"批量 K 线输出中没有任何含 date/收盘 列的表；输出前 200 字符：{(text or '')[:200]!r}")
    ok: dict[str, pd.DataFrame] = {}
    bad: dict[str, str] = {}
    for c in wanted:
        if c in errs:
            bad[c] = errs[c]
        elif parts[c]:
            df = pd.concat(parts[c], ignore_index=True)
            ok[c] = df.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
        else:
            bad[c] = f"{FETCH_FAILED}: 批量输出中拆分不出该股票的 K 线表"
    return ok, bad


# ---- 其它命令 ----
def parse_sector_search(text: str) -> list[dict]:
    tabs = parse_md_tables_raw(text, keep_empty=True)
    for t in tabs:
        ci, ni = _find_col(t.header, _CODE_N), _find_col(t.header, _NAME_N)
        if ci is None:
            continue
        out = [{"code": r[ci].strip(), "name": (r[ni].strip() if ni is not None else "")}
               for r in t.rows if r[ci].strip()]
        if out or not t.rows:     # 有表头无数据 => 确实无结果（合法的空）
            return out
    raise ParseError(f"板块搜索输出中没有含“代码”列的表；输出前 200 字符：{(text or '')[:200]!r}")


def parse_constituents(text: str) -> list[dict]:
    tabs = parse_md_tables_raw(text)
    out, seen = [], set()
    for t in tabs:
        ci, ni = _find_col(t.header, _CODE_N), _find_col(t.header, _NAME_N)
        if ci is None:
            continue
        for r in t.rows:
            m = _CODE_TOKEN.search(r[ci])
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            out.append({"code": m.group(1), "name": r[ni].strip() if ni is not None else ""})
    if not out:
        raise ParseError(f"成分股输出中解析不到任何代码；输出前 200 字符：{(text or '')[:200]!r}")
    return out


def parse_calendar(text: str) -> list[str]:
    days: set[str] = set()
    for t in parse_md_tables_raw(text):
        di = _find_col(t.header, _DATE_N)
        if di is None and len(t.header) == 1:
            di = 0
        if di is None:
            continue
        fi = _find_col(t.header, _FLAG_N)
        for r in t.rows:
            if fi is not None and r[fi].strip().lower() not in TRUTHY:
                continue
            try:
                days.add(normalize_date(r[di]))
            except ValueError:
                continue
    if not days:    # 兜底：输出不是表格而是裸日期列表
        for m in _DATE_IN_TEXT.finditer(_ANSI.sub("", text or "")):
            try:
                days.add(normalize_date(m.group(1)))
            except ValueError:
                pass
    if not days:
        raise ParseError(f"交易日历输出中解析不到任何日期；输出前 200 字符：{(text or '')[:200]!r}")
    return sorted(days)


_KV_LINE = re.compile(r"^\s*(?:[-*•]\s*)?\**([^:：|\n]{1,30}?)\**\s*[:：]\s*(.+?)\s*$")


def _profile_entry(name: Optional[str], listed: Optional[str]) -> dict:
    ld = None
    if listed:
        try:
            ld = normalize_date(listed)
        except ValueError:
            ld = None
    return {"name": (name or "").strip(), "listed_date": ld}


def parse_profiles(text: str, codes: list[str]) -> dict[str, dict]:
    """个股资料 -> {code: {"name","listed_date"}}。支持：
    (a) 含代码列的多行表；(b) 单行宽表（按标签/单只归属）；(c) 两列键值表（字段|值）；(d) `listedDate: 2012-03-02` 键值行。"""
    wanted = _wanted_codes(codes)
    wset = set(wanted)
    found: dict[str, dict] = {}

    def put(code: Optional[str], name: Optional[str], listed: Optional[str], extras: Optional[dict] = None) -> None:
        if code is None and len(wanted) == 1:
            code = wanted[0]
        if code not in wset or not (name or listed):
            return
        cur = found.setdefault(code, {"name": "", "listed_date": None})
        e = _profile_entry(name, listed)
        cur["name"] = cur["name"] or e["name"]
        cur["listed_date"] = cur["listed_date"] or e["listed_date"]
        for k, v in (extras or {}).items():        # 仅当表里真有这些列且非空时才出现，保持旧输出形状不变
            if v and not cur.get(k):
                cur[k] = v

    for seg in _segments(text, wanted):
        kv: dict[str, str] = {}
        for t in seg.tables:
            if not t.rows:
                continue
            ci, ni, li = _find_col(t.header, _CODE_N), _find_col(t.header, _NAME_N), _find_col(t.header, _LISTED_N)
            if li is not None or ni is not None:
                for r in t.rows:
                    code = None
                    if ci is not None:
                        m = _CODE_TOKEN.search(r[ci])
                        code = m.group(1) if m else None
                    else:
                        code = seg.code
                    extras = {f: r[i].strip() for i, h in enumerate(t.header)
                              for k, f in PROFILE_EXTRA_ALIASES.items() if _norm_header(h) == k and i < len(r)}
                    put(code, r[ni] if ni is not None else None, r[li] if li is not None else None, extras)
            elif ci is None and len(t.header) >= 2:   # 键值表：字段 | 值
                for r in t.rows:
                    kv[_norm_header(r[0])] = r[1]
        for line in seg.lines:
            m = _KV_LINE.match(line)
            if m:
                kv.setdefault(_norm_header(m.group(1)), m.group(2).strip("` "))
        name = next((v for k, v in kv.items() if k in _NAME_N), None)
        listed = next((v for k, v in kv.items() if k in _LISTED_N), None)
        if name or listed:
            put(seg.code, name, listed)
    if not found:
        raise ParseError(f"个股资料输出中解析不到 {wanted[:3]}… 的名称/上市日期；输出前 200 字符：{(text or '')[:200]!r}")
    return found


# ======================================================================================
# 4. Provider
# ======================================================================================
def resolve_bin(fetch_cfg: dict | None = None) -> str:
    """westock 可执行文件路径。优先级：WESTOCK_BIN 环境变量 > cfg.fetch.command > 常见安装位置 > PATH。
    launchd 的 PATH 很短，不含 ~/.local/bin，因此必须解析成绝对路径（WESTOCK_NOTES 第 10 节）。"""
    env = os.environ.get("WESTOCK_BIN")
    if env:
        return env
    name = (fetch_cfg or {}).get("command")
    if name and name != DEFAULT_CMD:
        return name
    candidates = [
        os.path.expanduser("~/.local/bin/westock"),
        os.path.expanduser("~/.westock/channels/wbgeneral/bin/westock"),
        "/opt/homebrew/bin/westock", "/usr/local/bin/westock",
    ]
    for c in candidates:
        if os.path.exists(c) and os.access(c, os.X_OK):
            return c
    return shutil.which(DEFAULT_CMD) or DEFAULT_CMD


class WestockProvider(DataProvider):
    name = "westock"

    def __init__(self, cfg: dict, paths: Paths):
        fetch = (cfg or {}).get("fetch", {}) or {}
        self.fetch_cfg = fetch
        self.command: str = resolve_bin(fetch)
        self.amount_unit: float = float(fetch.get("amount_unit", 1))
        self.timeout: float = float(fetch.get("command_timeout", CMD_TIMEOUT_SEC))
        self.paths = paths

    # ---- 底层调用 ----
    def _log(self, argv: list[str], elapsed: float, rate_limited: bool, rc: Optional[int], status: str) -> None:
        """追加写 westock-{date}.log（命令/耗时/是否限流；不含凭证）。日志是追加流，不走 atomic；失败静默。"""
        try:
            p = self.paths.westock_log(shanghai_today())
            p.parent.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            cmd = " ".join([argv[0]] + [a if len(a) <= 120 else a[:117] + "..." for a in argv[1:]])
            with open(p, "a", encoding="utf-8") as f:
                f.write(f"{ts}\t{cmd}\telapsed={elapsed:.2f}s\trate_limited={int(rate_limited)}\trc={rc}\t{status}\n")
        except OSError:
            pass

    def _run(self, template: list[str], **kw) -> str:
        argv = [self.command] + [a.format(**kw) for a in template]
        t0 = time.monotonic()
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=self.timeout, stdin=subprocess.DEVNULL)
        except (FileNotFoundError, NotADirectoryError, PermissionError) as e:
            self._log(argv, time.monotonic() - t0, False, None, "command_unavailable")
            raise SourceUnavailable(f"westock 命令不可用（{self.command}）：{e}")
        except subprocess.TimeoutExpired:
            self._log(argv, time.monotonic() - t0, False, None, "timeout")
            raise SourceUnavailable(f"westock 调用超时（>{self.timeout:g}s）：{' '.join(argv[:3])}")
        elapsed = time.monotonic() - t0
        out = _ANSI.sub("", proc.stdout or "")
        err = _ANSI.sub("", proc.stderr or "")
        m = RATE_LIMIT_RE.search(out) or RATE_LIMIT_RE.search(err)
        if m:
            self._log(argv, elapsed, True, proc.returncode, "rate_limited")
            raise RateLimited(f"westock 限流（{m.group(0)}）：{' '.join(argv[:3])}")
        if proc.returncode != 0 and "|" not in out:
            self._log(argv, elapsed, False, proc.returncode, "error")
            raise SourceUnavailable(f"westock 退出码 {proc.returncode}：{(err or out).strip()[:200]}")
        self._log(argv, elapsed, False, proc.returncode, "ok")
        return out

    # ---- DataProvider ----
    def version(self) -> str:
        """返回 "v0.0.9" 格式。CMD_VERSION = ["version","--json"]，输出是 JSON。"""
        out = self._run(CMD_VERSION)
        try:
            import json as _json
            v = _json.loads(out.strip()).get("version", "")
            if v:
                return v if v.startswith("v") else f"v{v}"
        except Exception:
            pass
        line = next((l.strip() for l in out.splitlines() if l.strip()), "")
        if not line:
            raise ParseError("westock version --json 输出为空")
        return line

    def get_klines(self, codes: list[str], days: int, fq: str):
        codes6 = list(dict.fromkeys(_wanted_codes(codes)))
        if not codes6:
            raise ValueError("codes 不能为空")
        if len(codes6) > MAX_BATCH:
            raise ValueError(f"单批最多 {MAX_BATCH} 只，收到 {len(codes6)}")
        arg = ",".join(f"{market_prefix(c)}{c}" for c in codes6)
        out = self._run(CMD_KLINE, codes=arg, count=int(days), fq=FQ_ARG.get(str(fq).lower(), str(fq)))
        return parse_kline_batch(out, codes6, self.amount_unit)

    def get_profiles(self, codes: list[str]) -> dict[str, dict]:
        """个股资料。westock profile 一次最多 10 只，超过时在这里自动分批（批间隔取 cfg.fetch.batch_interval），
        调用方不必关心；任一子批限流/解析失败直接抛出，由调用方的重试逻辑处理整次调用。"""
        codes6 = list(dict.fromkeys(_wanted_codes(codes)))
        if not codes6:
            raise ValueError("codes 不能为空")
        out: dict[str, dict] = {}
        for i in range(0, len(codes6), MAX_PROFILE_BATCH):
            if i:
                time.sleep(float(self.fetch_cfg.get("batch_interval", 1.5)))
            chunk = codes6[i:i + MAX_PROFILE_BATCH]
            text = self._run(CMD_PROFILE, codes=",".join(f"{market_prefix(c)}{c}" for c in chunk))
            out.update(parse_profiles(text, chunk))
        return out

    def search_sector(self, name: str) -> list[dict]:
        return parse_sector_search(self._run(CMD_SEARCH_SECTOR, name=name))

    def sector_constituents(self, sector_code: str) -> list[dict]:
        return parse_constituents(self._run(CMD_SECTOR_CONSTITUENT, code=sector_code))

    def trade_calendar(self) -> list[str]:
        """按年逐年拉取，合并返回全部交易日（YYYY-MM-DD 升序）。
        实测：trade-calendar 必须指定 --year（或 --start/--end），且默认 limit≈50，需 --limit 9999 才是全年。
        数据源错误（未安装/超时/限流/未授权）直接抛出；某一年解析不出（如未来年份尚未发布）跳过；
        所有年份都拿不到数据时抛 ParseError。"""
        this_year = int(shanghai_today()[:4])
        years = range(this_year - CMD_TRADE_CALENDAR_YEARS + 1, this_year + 2)
        days: set[str] = set()
        last_err: Optional[Exception] = None
        for yr in years:
            out = self._run(CMD_TRADE_CALENDAR, year=str(yr))
            try:
                days.update(parse_calendar(out))
            except ParseError as e:
                last_err = e
        if not days:
            raise ParseError(f"交易日历解析失败（{years.start}–{years.stop - 1} 均无数据）：{last_err}")
        return sorted(days)
