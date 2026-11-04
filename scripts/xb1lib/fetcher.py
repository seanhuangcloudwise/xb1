"""增量取数（PRD 3.1 / 4.2）：批 20、批间隔、限流重试、增量 parquet 缓存、停牌识别。

fetch_klines 语义（DEV_CONTRACTS + 下列选择）：
- 缓存：data/raw/kline/{code}.parquet（标准列、升序）。缓存末日期 ≥ trade_date -> 不联网；
  缓存存在但落后 -> 只补缺失日期（请求 days = 距今自然日数 + 2，≥ kline_days 则改全量）；无缓存/缓存损坏 -> 全量 kline_days。
  增量合并后，用 TradeCalendar 列出 (缓存末日, trade_date] 的全部交易日，断言合并结果中均有对应 bar；任一缺失则该只回退全量重拉（日历不可用则跳过）。
  增量拿回的重叠行收盘价与缓存不一致（除权导致前复权序列整体改写）或没有重叠 -> 该只回退全量重拉并覆盖缓存。
- `--date` 历史回放：返回前截断到 date <= trade_date；缓存已覆盖则不联网；需联网时 days 额外加上
  trade_date 到今天的间隔（provider 没有“截止日期”参数，只能多拉再截断）。缓存是满窗口、但截断到 trade_date
  后不足 kline_days 根时，视为缺更早历史，对该只全量重拉（结果覆盖缓存，之后回放同区间不再联网）。
- 批：每批 ≤ min(cfg.fetch.batch_size, 20)；相邻两次 get_klines 调用之间 sleep(batch_interval)。
- 限流：某批抛 RateLimited -> sleep(rate_limit_wait) 后仅重试该批；**max_retries = 该批最多尝试次数（含首次）**，
  即连续 max_retries(默认 3) 次限流 -> 该批全部股票记 "rate_limited: ..." 单只失败，继续下一批。
  （PRD 的“重试最多 3 次”此处按“连续 3 次限流即放弃”实现，见 DEV 汇报。）
- 整体不可用：需联网取数的股票中，失败（rate_limited / failed，不含 suspended）占比 ≥ 50% -> 抛 SourceUnavailable
  （已成功的缓存先落盘）。provider 本身抛 SourceUnavailable（命令不存在/超时/退出码）立即向上传播。
  某批 ParseError -> 该批全部记 "failed: parse_error: ..."，计入上述占比。
- 单只失败（含 provider 返回的单只失败）只记入第二个 dict，不影响其他股票；失败股票不出现在第一个 dict。
- 停牌：截断后末根 K 线日期 != trade_date -> 第二个 dict 记 "suspended: last_bar=YYYY-MM-DD"。
  cfg.filters.exclude_suspended=true（默认）时该股不出现在第一个 dict；为 false 时仍返回其 df（同时仍记 reason）。
- 返回的 df 为 date<=trade_date 的最近 kline_days 根。
"""
from __future__ import annotations

import bisect
import math
import time
from collections import deque
from typing import Callable, Optional

import numpy as np
import pandas as pd

from . import atomic
from .calendar import TradeCalendar, days_between, normalize_date, shanghai_today
from .constants import FETCH_FAILED, FETCH_RATE_LIMITED, FETCH_STALE, FETCH_SUSPENDED, KLINE_COLUMNS
from .errors import ParseError, RateLimited, SourceUnavailable
from .models import normalize_code
from .paths import Paths
from .provider_base import MAX_BATCH, DataProvider

FAIL_RATIO_LIMIT = 0.5
_KNOWN_PREFIXES = (FETCH_RATE_LIMITED, FETCH_FAILED, FETCH_SUSPENDED, FETCH_STALE)


def call_with_rate_limit_retry(fn: Callable, cfg: dict, sleep: Callable[[float], None] = time.sleep):
    """调用 fn()；RateLimited 时 sleep(rate_limit_wait) 后重试，最多 max_retries 次尝试（含首次），仍限流则抛最后一次的 RateLimited。"""
    f = cfg.get("fetch", {})
    attempts = max(1, int(f.get("max_retries", 3)))
    wait = float(f.get("rate_limit_wait", 30))
    for i in range(attempts):
        try:
            return fn()
        except RateLimited:
            if i == attempts - 1:
                raise
            sleep(wait)


def load_cache(paths: Paths, code: str) -> Optional[pd.DataFrame]:
    """读 K 线缓存；不存在/损坏/列不对/空 -> None（调用方回退全量重拉）。"""
    p = paths.kline(code)
    if not p.exists():
        return None
    try:
        df = pd.read_parquet(p)
        if list(df.columns) != KLINE_COLUMNS or df.empty:
            return None
        df = df.copy()
        df["date"] = df["date"].astype(str)
        df = df.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
        return df
    except Exception:
        return None


def _valid_new(df) -> Optional[str]:
    if not isinstance(df, pd.DataFrame):
        return "provider 返回的不是 DataFrame"
    if list(df.columns) != KLINE_COLUMNS:
        return f"K 线列不符：{list(df.columns)}"
    if df.empty:
        return "K 线为空"
    return None


def _consistent(cache: pd.DataFrame, new: pd.DataFrame) -> bool:
    """增量重叠行的收盘价是否与缓存一致；没有重叠视为不一致（无法验证复权基准）。"""
    m = cache[["date", "close"]].merge(new[["date", "close"]], on="date", suffixes=("_c", "_n"))
    if m.empty:
        return False
    return bool(np.isclose(m["close_c"].to_numpy(float), m["close_n"].to_numpy(float), rtol=1e-4, atol=1e-6).all())


def _gap_trading_days_missing(merged, last_date: str, td_date: str, cal_days) -> list:
    """返回 (last_date, td_date] 内、merged 缺失的交易日列表。

    cal_days 为升序 ISO 日期字符串列表（来自 TradeCalendar.days）。用于补缺后断言：
    若缓存末日 last_date 到 trade_date 之间的任一交易日在合并结果里没有对应 bar，返回这些缺失日，
    调用方据此对该只回退全量重拉。日历不可用（cal_days 为 None/空）或 last_date >= td_date 时返回空列表。
    """
    if not cal_days or last_date >= td_date:
        return []
    lo = bisect.bisect_right(cal_days, last_date)
    hi = bisect.bisect_right(cal_days, td_date)
    window = cal_days[lo:hi]
    if not window:
        return []
    have = set(merged["date"].tolist())
    return [d for d in window if d not in have]


def _persist(paths: Paths, code: str, df: pd.DataFrame, td: str, persist_today: bool) -> None:
    """写 raw/kline 缓存。persist_today=False 时丢掉 date >= td 的行（盘中半成品不落盘）。"""
    out = df if persist_today else df[df["date"] < td].reset_index(drop=True)
    if len(out):
        atomic.write_parquet(paths.kline(code), out)


def _chunks(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def fetch_klines(provider: DataProvider, paths: Paths, cfg: dict, codes: list[str], trade_date: str,
                 sleep: Callable[[float], None] = time.sleep, *, today: Optional[str] = None,
                 persist_today: bool = True
                 ) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """...（原说明见模块头）

    当日 K 线的可信度（WESTOCK_NOTES §10.5：盘中最后一根 K 线是实时未收盘数据）：
      * 实时运行（trade_date == today）时，缓存里日期 == trade_date 的那根一律不信任：读缓存时丢弃并重新拉取，
        否则盘中跑一次后，收盘后的运行会因“缓存末日期已是今天”而跳过取数，一直用着半成品。
      * persist_today=False（调用方判定为盘中）时，当日这根只用于本次计算，不写入 raw/kline 缓存，
        保证 raw/ 里只有收盘数据（history.backfill 的 T+N 收益也读它）。
      * 回放历史日（trade_date < today）的缓存是收盘数据，照常信任。
    """
    f = cfg.get("fetch", {})
    batch_size = max(1, min(int(f.get("batch_size", 20)), MAX_BATCH))
    interval = float(f.get("batch_interval", 1.5))
    kline_days = int(f.get("kline_days", 250))
    fq = str(f.get("fq", "qfq"))
    exclude_susp = bool(cfg.get("filters", {}).get("exclude_suspended", True))

    td = normalize_date(trade_date)
    now = max(normalize_date(today) if today else shanghai_today(), td)
    lag = days_between(td, now)
    full_days = kline_days + (0 if lag == 0 else math.ceil(lag * 5 / 7) + 2)

    # 交易日历：补缺后断言 (缓存末日, td] 每个交易日都有 bar；不可用时静默跳过（不影响取数）
    _cal_days = None
    try:
        _cal_days = TradeCalendar(paths, provider, today=now).days
    except Exception:
        _cal_days = None

    codes6 = list(dict.fromkeys(normalize_code(c) for c in codes))
    caches: dict[str, Optional[pd.DataFrame]] = {c: load_cache(paths, c) for c in codes6}
    if lag == 0:       # 实时运行：当日那根不可信，丢掉重拉（可能是盘中半成品，也可能是旧版本遗留的污染）
        for c, cache in list(caches.items()):
            if cache is not None and (cache["date"] >= td).any():
                trimmed = cache[cache["date"] < td].reset_index(drop=True)
                caches[c] = trimmed if len(trimmed) else None
    final: dict[str, pd.DataFrame] = {c: d for c, d in caches.items() if d is not None}
    failures: dict[str, str] = {}

    # ---- 规划：哪些要联网、请求多少天 ----
    full_codes: list[str] = []
    inc: list[tuple[int, str]] = []
    for c in codes6:
        cache = caches[c]
        if cache is None:
            full_codes.append(c)
            continue
        last = cache["date"].iloc[-1]
        if last >= td:
            # 回放：缓存是满窗口(≥kline_days 根)但截断到 td 后不足 kline_days 根 -> 更早的历史没缓存，全量重拉补足
            if last > td and len(cache) >= kline_days and int((cache["date"] <= td).sum()) < kline_days:
                full_codes.append(c)
            continue
        gap = days_between(last, now) + 2
        if gap >= kline_days:
            full_codes.append(c)
        else:
            inc.append((gap, c))
    to_fetch = set(full_codes) | {c for _, c in inc}

    queue: deque = deque()
    for chunk in _chunks(full_codes, batch_size):
        queue.append((full_days, chunk, "full"))
    inc.sort(key=lambda x: (-x[0], x[1]))
    for chunk in _chunks(inc, batch_size):
        queue.append((chunk[0][0], [c for _, c in chunk], "inc"))

    refetch: list[str] = []
    calls = 0
    while queue:
        days, batch, kind = queue.popleft()
        if calls > 0:
            sleep(interval)
        calls += 1
        try:
            ok, bad = call_with_rate_limit_retry(lambda: provider.get_klines(batch, days, fq), cfg, sleep)
        except RateLimited as e:
            for c in batch:
                failures[c] = f"{FETCH_RATE_LIMITED}: 连续限流 {max(1, int(f.get('max_retries', 3)))} 次放弃（{e}）"
            ok, bad = {}, {}
        except ParseError as e:
            for c in batch:
                failures[c] = f"{FETCH_FAILED}: parse_error: {e}"
            ok, bad = {}, {}
        for c in batch:
            if c in failures:
                continue
            if c in bad:
                r = str(bad[c])
                failures[c] = r if r.startswith(_KNOWN_PREFIXES) else f"{FETCH_FAILED}: {r}"
                continue
            new = ok.get(c)
            if new is None:
                failures[c] = f"{FETCH_FAILED}: provider 未返回该股票数据"
                continue
            why = _valid_new(new)
            if why:
                failures[c] = f"{FETCH_FAILED}: {why}"
                continue
            new = new.copy()
            new["date"] = new["date"].astype(str)
            new = new.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
            cache = caches[c]
            if kind == "inc" and cache is not None:
                if not _consistent(cache, new):
                    refetch.append(c)     # 除权改写/无重叠：回退全量
                    continue
                merged = pd.concat([cache, new], ignore_index=True).drop_duplicates("date", keep="last")
                merged = merged.sort_values("date").reset_index(drop=True)
                # 补缺保险：缺口 (缓存末日, td] 内任一交易日缺失 -> 该只全量重拉
                miss = _gap_trading_days_missing(merged, cache["date"].iloc[-1], td, _cal_days) if _cal_days else []
                if miss:
                    refetch.append(c)
                    continue
                if not merged.equals(cache):
                    _persist(paths, c, merged, td, persist_today)
            else:
                merged = new
                _persist(paths, c, merged, td, persist_today)
            final[c] = merged
        if not queue and refetch:
            for chunk in _chunks(refetch, batch_size):
                queue.append((full_days, chunk, "full"))
            refetch = []

    # ---- 整体可用性 ----
    if to_fetch:
        bad_n = sum(1 for c in to_fetch if c in failures and not failures[c].startswith(FETCH_SUSPENDED))
        if bad_n / len(to_fetch) >= FAIL_RATIO_LIMIT:
            raise SourceUnavailable(f"取数失败 {bad_n}/{len(to_fetch)} 只（≥{FAIL_RATIO_LIMIT:.0%}），数据源视为不可用")

    # ---- 截断 + 停牌识别 ----
    results: dict[str, pd.DataFrame] = {}
    for c in codes6:
        if c in failures:
            continue
        df = final.get(c)
        if df is None:
            failures[c] = f"{FETCH_FAILED}: 无可用数据"
            continue
        df = df[df["date"] <= td]
        if df.empty:
            failures[c] = f"{FETCH_FAILED}: {td} 及以前无 K 线"
            continue
        df = df.tail(kline_days).reset_index(drop=True)
        last = df["date"].iloc[-1]
        if last != td:
            failures[c] = f"{FETCH_SUSPENDED}: last_bar={last} < {td}"
            if exclude_susp:
                continue
        results[c] = df
    return results, failures
