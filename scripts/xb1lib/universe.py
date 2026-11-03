"""股票池（PRD 4.1 / F1）。

build_universe(paths, cfg, provider, trade_date) -> (list[StockMeta], excluded)
- 输入取并集去重：cfg.universe.stocks / sectors / files；结果按代码升序（同配置同缓存下顺序确定）。
- 板块：名称 -> search_sector（精确同名优先，否则取第一个；形如 pt02GN2238 的直接当板块代码）-> sector_constituents。
  快照落 data/raw/universe/{板块代码}_{抓取日}.json，缓存 cfg.universe.sector_cache_days（默认 7）天；
  刷新失败但有旧快照时退回旧快照并写 warning。
- profile：data/raw/profile/{code}.json。**build_universe 只读本地缓存，从不联网；** 资料只在用户执行 `profile update` 时通过 refresh_profiles 更新。
- 剔除（顺序：北交所 -> ST -> 次新），excluded 里的 reason：
    "bj"                              board_of == bj 且 filters.exclude_bj
    "st: <名称>"                       models.is_st_name(名称) 且 filters.exclude_st（名称取 profile，其次成分股名）
    "new_listing: 已上市 K 个交易日 < N"  上市交易日数（含上市日、含 trade_date，用 TradeCalendar 计算）< filters.min_listed_days
- profile 缺失（或缺 listed_date）：不剔除、不写入 excluded 的股票键，只在 excluded["_warnings"] 里加一条汇总警告。**excluded 里以下划线开头的键（目前仅 "_warnings": list[str]）
  不是股票代码，消费方遍历剔除明细时必须跳过。** 板块搜不到、使用旧快照、profile 批失败等也记入 "_warnings"。
- 停牌不在这里判定（取数后由 fetcher 用 K 线末日期判）。
- 关键字参数 sleep/today/calendar 仅为可测试性与限流节流（profile/板块请求同样遵守 batch_interval 与 rate_limit_wait）。
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import atomic
from .calendar import TradeCalendar, days_between, normalize_date, shanghai_today
from .errors import ConfigError, ParseError, RateLimited, SourceUnavailable
from .fetcher import call_with_rate_limit_retry
from .models import StockMeta, board_of, is_st_name, normalize_code
from .paths import Paths
from .provider_base import MAX_BATCH, MAX_PROFILE_BATCH, DataProvider

PROFILE_REFRESH_DAYS = 7


def _group_sources(ucfg: dict) -> dict[str, dict]:
    """返回 {group_name: {"stocks":[..], "sectors":[..], "files":[..]}}，源自 universe.groups。"""
    raw = ucfg.get("groups") or {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for name, block in raw.items():
        if not isinstance(block, dict):
            continue
        out[str(name)] = {
            "stocks":  list(block.get("stocks") or []),
            "sectors": list(block.get("sectors") or []),
            "files":   list(block.get("files") or []),
        }
    return out


_SECTOR_CODE_RE = re.compile(r"^pt\d{2}[A-Za-z0-9]+$")


def _code_from_config(v) -> str:
    if isinstance(v, int):
        return f"{v:06d}"     # YAML 会把 000001 读成整数 1
    try:
        return normalize_code(str(v))
    except ValueError:
        raise ConfigError(f"universe.stocks 含无效股票代码：{v!r}")


def read_code_file(paths: Paths, p: str) -> list[str]:
    """CSV/TXT：每行一个代码（取行内第一个 6 位数字），# 开头为注释，无代码的行（如表头）跳过。"""
    fp = Path(str(p)).expanduser()
    if not fp.is_absolute():
        fp = paths.home / fp
    if not fp.exists():
        raise ConfigError(f"universe.files 指定的文件不存在：{fp}")
    out: list[str] = []
    with open(fp, "r", encoding="utf-8-sig") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            first = re.split(r"[,;\t ]+", s)[0]
            try:
                out.append(normalize_code(first))
            except ValueError:
                continue
    return out


def _snapshots(paths: Paths):
    """[(sector_code, 抓取日, 路径)]，按抓取日降序。"""
    out = []
    if paths.universe_dir.exists():
        for fp in paths.universe_dir.glob("*_????-??-??.json"):
            code, _, d = fp.stem.rpartition("_")
            if code:
                out.append((code, d, fp))
    return sorted(out, key=lambda x: x[1], reverse=True)


def _find_snapshot(paths: Paths, key: str) -> Optional[tuple[str, str, dict]]:
    for code, d, fp in _snapshots(paths):
        try:
            data = atomic.read_json(fp, default=None)
        except ValueError:      # 快照损坏：忽略
            continue
        if not isinstance(data, dict) or not isinstance(data.get("constituents"), list):
            continue
        if key in (code, data.get("sector_name")):
            return code, d, data
    return None


def _load_sector(paths, cfg, provider, key: str, today: str, throttle, warnings: list[str]) -> list[dict]:
    ttl = int(cfg.get("universe", {}).get("sector_cache_days", 7))
    snap = _find_snapshot(paths, key)
    if snap and 0 <= days_between(snap[1], today) <= ttl:
        return snap[2]["constituents"]
    try:
        if _SECTOR_CODE_RE.match(key):
            code, sname = key, (snap[2].get("sector_name") if snap else key)
        else:
            throttle()
            hits = call_with_rate_limit_retry(lambda: provider.search_sector(key), cfg, throttle.sleep)
            if not hits:
                warnings.append(f"板块 {key!r}: 未搜索到，已跳过")
                return []
            pick = next((h for h in hits if h.get("name") == key), hits[0])
            code, sname = pick["code"], pick.get("name", key)
        throttle()
        cons = call_with_rate_limit_retry(lambda: provider.sector_constituents(code), cfg, throttle.sleep)
    except (RateLimited, ParseError, SourceUnavailable) as e:
        if snap:
            warnings.append(f"板块 {key!r}: 刷新失败（{e}），使用 {snap[1]} 的旧快照")
            return snap[2]["constituents"]
        raise
    cons = [{"code": normalize_code(c["code"]), "name": c.get("name", "")} for c in cons]
    atomic.write_json(paths.universe_snapshot(code, today), {
        "sector_code": code, "sector_name": sname, "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "constituents": cons})
    return cons


class _Throttle:
    """批间隔节流：第一次调用不 sleep，之后每次调用前 sleep(batch_interval)。"""

    def __init__(self, interval: float, sleep: Callable[[float], None]):
        self.interval, self.sleep, self.n = interval, sleep, 0

    def __call__(self) -> None:
        if self.n:
            self.sleep(self.interval)
        self.n += 1


def _read_profile(paths: Paths, code: str) -> Optional[dict]:
    """读取本地资料缓存。不看年龄：资料只由 `profile update` 手动更新，run 永远不联网取资料。"""
    try:
        d = atomic.read_json(paths.profile(code), default=None)
        if isinstance(d, dict) and d.get("code"):
            return d
    except (KeyError, ValueError, TypeError):
        pass
    return None


def refresh_profiles(paths: Paths, cfg: dict, provider: DataProvider, codes: list, today: str,
                     sleep: Callable[[float], None] = time.sleep) -> dict:
    """显式刷新个股资料（名称/上市日期）。仅由用户手动命令 `profile update` 调用，build_universe 不调用。
    返回 {"updated": n, "failed": [(批次描述, 原因)]}。"""
    codes6 = list(dict.fromkeys(normalize_code(c) for c in codes))
    fcfg = cfg.get("fetch", {}) or {}
    bs = max(1, min(int(fcfg.get("batch_size", 20)), MAX_PROFILE_BATCH))
    interval = float(fcfg.get("batch_interval", 1.5))
    updated, failed = 0, []
    for i in range(0, len(codes6), bs):
        batch = codes6[i:i + bs]
        if i:
            sleep(interval)
        try:
            got = call_with_rate_limit_retry(lambda: provider.get_profiles(batch), cfg, sleep)
        except (RateLimited, ParseError) as e:
            failed.append((f"{batch[0]}..{batch[-1]}（{len(batch)} 只）", str(e)))
            continue
        for c, p in got.items():
            c = normalize_code(c)
            if c not in batch:
                continue
            rec = {"code": c, "name": p.get("name") or "", "listed_date": p.get("listed_date"), "fetched_at": today}
            atomic.write_json(paths.profile(c), rec)
            updated += 1
    return {"updated": updated, "failed": failed}


def build_universe(paths: Paths, cfg: dict, provider: DataProvider, trade_date: str, *,
                   sleep: Callable[[float], None] = time.sleep, today: Optional[str] = None,
                   calendar: Optional[TradeCalendar] = None,
                   group: Optional[str] = None) -> tuple[list[StockMeta], dict]:
    """构建股票池。
    group=None：顶层 stocks/sectors/files + 所有 groups 的并集。
    group=<名>：只取该分组。
    """
    td = normalize_date(trade_date)
    today = normalize_date(today) if today else shanghai_today()
    ucfg, flt, fcfg = cfg.get("universe", {}) or {}, cfg.get("filters", {}) or {}, cfg.get("fetch", {}) or {}
    warnings: list[str] = []
    throttle = _Throttle(float(fcfg.get("batch_interval", 1.5)), sleep)
    throttle.sleep = sleep

    # 读档案里的 tags/groups
    try:
        from .portfolio import Portfolio
        portfolio = Portfolio(paths)
    except Exception:
        portfolio = None

    groups_cfg = _group_sources(ucfg)
    if group is not None and group not in groups_cfg:
        raise ConfigError(f"分组 {group!r} 不存在（已有：{list(groups_cfg.keys())}）")

    # 来源列表：[(source_dict, group_name_or_None)]
    sources: list[tuple[dict, Optional[str]]] = []
    if group is not None:
        sources.append((groups_cfg[group], group))
    else:
        # 顶层
        sources.append(({
            "stocks":  list(ucfg.get("stocks") or []),
            "sectors": list(ucfg.get("sectors") or []),
            "files":   list(ucfg.get("files") or []),
        }, None))
        # 所有分组
        for gname, gsrc in groups_cfg.items():
            sources.append((gsrc, gname))

    names: dict[str, str] = {}
    code_groups: dict[str, set] = {}   # code -> set of group names

    def _record(c: str, gname: Optional[str]) -> None:
        order.setdefault(c)
        if gname:
            code_groups.setdefault(c, set()).add(gname)

    order: dict[str, None] = {}
    for src, gname in sources:
        for v in src.get("stocks") or []:
            _record(_code_from_config(v), gname)
        for p in src.get("files") or []:
            for c in read_code_file(paths, p):
                _record(c, gname)
        for key in src.get("sectors") or []:
            for item in _load_sector(paths, cfg, provider, str(key), today, throttle, warnings):
                c = normalize_code(item["code"])
                _record(c, gname)
                if item.get("name"):
                    names.setdefault(c, item["name"])

    excluded: dict = {}
    codes = sorted(order)
    if flt.get("exclude_bj", True):
        for c in codes:
            if board_of(c) == "bj":
                excluded[c] = "bj"
        codes = [c for c in codes if c not in excluded]

    # ---- profile（只读本地缓存；缺失时不联网，汇总为一条警告，由用户执行 profile update）----
    min_days = int(flt.get("min_listed_days", 0) or 0)
    exclude_st = bool(flt.get("exclude_st", True))
    need = [c for c in codes if min_days > 0 or (exclude_st and not names.get(c))]
    profiles: dict[str, dict] = {}
    missing: list[str] = []
    for c in need:
        cached = _read_profile(paths, c)
        if cached is not None:
            profiles[c] = cached
        else:
            missing.append(c)
    if missing:
        warnings.append(f"{len(missing)} 只缺少资料缓存（ST/上市天数未判定），"
                        f"如需更新请执行：xb1.py profile update --codes/--file/--run；示例 {missing[0]}")

    # ---- 过滤 ----
    cal = calendar
    no_ld: list[str] = []
    metas: list[StockMeta] = []
    for c in codes:
        prof = profiles.get(c)
        name = (prof or {}).get("name") or names.get(c, "")
        if exclude_st and is_st_name(name):
            excluded[c] = f"st: {name}"
            continue
        ld = (prof or {}).get("listed_date")
        if min_days > 0:
            if not ld:
                no_ld.append(c)
            else:
                if cal is None:
                    cal = TradeCalendar(paths, provider, today=today)
                k = cal.count_inclusive(ld, td)
                if k < min_days:
                    excluded[c] = f"new_listing: 已上市 {k} 个交易日 < {min_days}"
                    continue
        # 从档案读 tags（没有档案则为空，且绝不因档案问题让 run 失败）
        tags: tuple = ()
        if portfolio is not None:
            try:
                tags = portfolio.tags_of(c)
            except Exception as e:
                warnings.append(f"{c}: 档案读取失败，标签忽略（{e}）")
        grps = tuple(sorted(code_groups.get(c) or set()))
        metas.append(StockMeta.make(c, name, ld, groups=grps, tags=tags))
    if no_ld:
        warnings.append(f"{len(no_ld)} 只缺少上市日期（未做上市天数过滤），如需更新请执行 profile update；示例 {no_ld[0]}")
    excluded["_warnings"] = warnings
    return metas, excluded
