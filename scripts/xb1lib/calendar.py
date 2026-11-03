"""交易日历：缓存 data/raw/calendar/trade_calendar.json，每周刷新（PRD 5.1.1）。

缓存格式：{"fetched_at": "YYYY-MM-DD", "days": ["2026-01-05", ...]}（原子写）。
刷新失败但有旧缓存 -> 退回旧缓存并把原因记到 TradeCalendar.warnings；没有任何缓存才向上抛。

另提供两个全模块共用的日期小工具 normalize_date / shanghai_today（PRD 6：显式 Asia/Shanghai）。
"""
from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from . import atomic
from .constants import TZ
from .errors import SourceUnavailable
from .paths import Paths
from .provider_base import DataProvider

CALENDAR_REFRESH_DAYS = 7

_DATE_RES = [
    re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})"),
    re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})"),
    re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})"),
    re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日?"),
    re.compile(r"^(\d{4})(\d{2})(\d{2})(?!\d)"),
]


def normalize_date(value) -> str:
    """str/date/datetime/Timestamp -> 'YYYY-MM-DD'；无法识别抛 ValueError。"""
    if isinstance(value, (datetime, date)) or hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    for rx in _DATE_RES:
        m = rx.match(s)
        if m:
            y, mo, d = (int(x) for x in m.groups())
            try:
                return date(y, mo, d).strftime("%Y-%m-%d")
            except ValueError:
                break
    raise ValueError(f"无法识别的日期：{value!r}")


def shanghai_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(TZ)
    except Exception:  # 无 tzdata（如 Windows）时退回固定 UTC+8
        tz = timezone(timedelta(hours=8))
    return datetime.now(tz)


def shanghai_today() -> str:
    return shanghai_now().strftime("%Y-%m-%d")


def days_between(a: str, b: str) -> int:
    """自然日差 b - a。"""
    return (date.fromisoformat(normalize_date(b)) - date.fromisoformat(normalize_date(a))).days


class TradeCalendar:
    """交易日历。日期参数一律 'YYYY-MM-DD'（也接受 date/datetime，会被规范化）。

    n_trading_days_between(a, b)：区间 (a, b] 内的交易日数（不含 a、含 b），b<a 时为负/0。
    count_inclusive(a, b)：闭区间 [a, b] 内的交易日数（上市天数用它）。
    prev_trading_day(date, n)：严格早于 date 的第 n 个交易日（date 本身不必是交易日）。
    """

    def __init__(self, paths: Paths, provider: DataProvider, today: Optional[str] = None,
                 refresh_days: int = CALENDAR_REFRESH_DAYS):
        self.paths = paths
        self.provider = provider
        self.today = normalize_date(today) if today else shanghai_today()
        self.refresh_days = refresh_days
        self.warnings: list[str] = []
        self._days: Optional[list[str]] = None
        self._fetched_at: Optional[str] = None
        self._refreshed = False  # 本实例内是否已向 provider 刷新过

    # ---- 加载/刷新 ----
    def _read_cache(self) -> None:
        try:
            data = atomic.read_json(self.paths.calendar_file, default=None)
            days = data["days"]
            fetched = normalize_date(data["fetched_at"])
            days = sorted({normalize_date(d) for d in days})
            if not days:
                raise ValueError("empty")
            self._days, self._fetched_at = days, fetched
        except Exception:  # 缺失/损坏/空 -> 视为无缓存
            self._days, self._fetched_at = None, None

    def _refresh(self) -> bool:
        self._refreshed = True
        try:
            days = self.provider.trade_calendar()
            days = sorted({normalize_date(d) for d in days})
            if not days:
                raise SourceUnavailable("trade_calendar 返回空列表")
        except Exception as e:
            if self._days:
                self.warnings.append(f"交易日历刷新失败，沿用旧缓存（{self._fetched_at}）：{e}")
                return False
            raise
        self._days, self._fetched_at = days, self.today
        atomic.write_json(self.paths.calendar_file, {"fetched_at": self.today, "days": days})
        return True

    def _ensure(self, need: Optional[str] = None) -> list[str]:
        if self._days is None:
            self._read_cache()
            if self._days is None:
                self._refresh()
            elif days_between(self._fetched_at, self.today) >= self.refresh_days:
                self._refresh()
        if need and not self._refreshed and need > self._days[-1] and self._fetched_at != self.today:
            self._refresh()  # 查询日超出缓存范围：当天只刷新一次
        assert self._days is not None
        return self._days

    @property
    def days(self) -> list[str]:
        return list(self._ensure())

    # ---- 查询 ----
    def is_trading_day(self, d: str) -> bool:
        d = normalize_date(d)
        days = self._ensure(d)
        i = bisect_left(days, d)
        return i < len(days) and days[i] == d

    def latest_trading_day(self, on_or_before: str) -> str:
        d = normalize_date(on_or_before)
        days = self._ensure(d)
        i = bisect_right(days, d) - 1
        if i < 0:
            raise ValueError(f"{d} 早于日历起点 {days[0]}")
        return days[i]

    def prev_trading_day(self, d: str, n: int = 1) -> str:
        if n < 1:
            raise ValueError("n 必须 ≥1")
        d = normalize_date(d)
        days = self._ensure(d)
        i = bisect_left(days, d) - n
        if i < 0:
            raise ValueError(f"{d} 之前不足 {n} 个交易日（日历起点 {days[0]}）")
        return days[i]

    def n_trading_days_between(self, a: str, b: str) -> int:
        a, b = normalize_date(a), normalize_date(b)
        days = self._ensure(max(a, b))
        return bisect_right(days, b) - bisect_right(days, a)

    def count_inclusive(self, a: str, b: str) -> int:
        a, b = normalize_date(a), normalize_date(b)
        days = self._ensure(max(a, b))
        return max(0, bisect_right(days, b) - bisect_left(days, a))
