"""测试替身（模块 D 提供；其它模块的集成测试也会用，接口保持稳定）。

主要入口
--------
make_kline_df(code, end_date, days, seed=0, ...) -> 标准列合成 K 线（确定性、升序、前缀稳定）
make_trading_days(start, end, holidays=()) -> 周一至周五（去掉 holidays）的交易日列表
FakeProvider(klines=None, profiles=None, calendar=None, sectors=None, ...)
    .synthetic(codes, end_date, days=300, ...)            用合成 K 线 + 自动 profile + 日历一键构造
    .inject_rate_limit(times, when_contains=None)         整批限流 times 次后恢复（可限定“含某代码的批”）
    .fail_codes[code] = reason                            指定代码单只失败（进第二个 dict）
    .suspend(code, last_date)                             指定代码停牌：返回的 K 线末日期 = last_date
    .available_until = 'YYYY-MM-DD'                       模拟“数据只发布到该日”（所有代码，不含已停牌更早者）
    .unavailable = True                                   整体不可用（抛 SourceUnavailable）
    .kline_calls / .batches / .batch_sizes / .requested_days   get_klines 调用记录（含被限流的尝试）
    .events                                               ("kline", codes, days) 与 FakeSleep 写入的 ("sleep", sec) 的时间序
FakeSleep(provider=None)                                  可注入的 sleep：不真睡，只记录，.calls 为秒数列表
make_env(tmp_path, **overrides) -> (Paths, cfg)           tmp 目录 + 拷贝 config.example.yaml + 载入配置（不碰真实 data/）
"""
from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from xb1lib.constants import KLINE_COLUMNS
from xb1lib.errors import RateLimited, SourceUnavailable
from xb1lib.models import normalize_code
from xb1lib.provider_base import DataProvider

ROOT = Path(__file__).resolve().parents[1]
_ANCHOR = date(2015, 1, 5)          # 合成序列的固定起点（周一）；保证不同 end_date 下重叠日期的数据逐位一致
_NO_HOLIDAYS: frozenset = frozenset()


# --------------------------------------------------------------------------- 合成数据
def _is_weekday(d: date) -> bool:
    return d.weekday() < 5


def make_trading_days(start: str, end: str, holidays: Iterable[str] = ()) -> list[str]:
    """[start, end] 内周一至周五且不在 holidays 的日期（YYYY-MM-DD，升序）。"""
    hol = set(holidays)
    d, e, out = date.fromisoformat(start), date.fromisoformat(end), []
    while d <= e:
        s = d.isoformat()
        if _is_weekday(d) and s not in hol:
            out.append(s)
        d += timedelta(days=1)
    return out


def _code_seed(code: str, seed: int) -> int:
    return int(hashlib.md5(f"{normalize_code(code)}:{seed}".encode()).hexdigest()[:8], 16)


def make_kline_df(code: str, end_date: str, days: int = 250, seed: int = 0,
                  holidays: Iterable[str] = (), base_price: Optional[float] = None) -> pd.DataFrame:
    """确定性合成 K 线：标准列 KLINE_COLUMNS、date 升序无重复、amount 为元、pct_chg/turnover 为百分数。

    交易日 = 周一至周五去掉 holidays；end_date 不是交易日时向前取最近交易日。
    前缀稳定：同一 (code, seed, holidays) 下，不同 end_date 生成的重叠日期数据逐位一致
    （因此“增量缓存”测试里缓存与新数据的收盘价能对上）。
    """
    if days < 1:
        raise ValueError("days 必须 ≥1")
    hol = set(holidays)
    all_days = make_trading_days(_ANCHOR.isoformat(), end_date, hol)
    if len(all_days) < days:
        raise ValueError(f"end_date={end_date} 之前的合成交易日不足 {days}")
    n = len(all_days)
    rng = np.random.default_rng(_code_seed(code, seed))
    z = rng.standard_normal((n, 5))                       # 行主序 -> 前缀稳定
    base = base_price if base_price is not None else 10.0 + (_code_seed(code, 0) % 900) / 10.0
    close = np.round(base * np.exp(np.cumsum(0.015 * z[:, 0])), 2).clip(min=0.5)
    prev = np.concatenate([[base], close[:-1]])
    open_ = np.round(prev * (1 + 0.005 * z[:, 1]), 2).clip(min=0.5)
    high = np.round(np.maximum(open_, close) * (1 + 0.01 * np.abs(z[:, 2])), 2)
    low = np.round(np.minimum(open_, close) * (1 - 0.01 * np.abs(z[:, 3])), 2).clip(min=0.3)
    volume = np.round(5_000_000 * (1 + 0.3 * np.abs(z[:, 4]))).astype(float)
    df = pd.DataFrame({
        "date": all_days, "open": open_, "close": close, "high": high, "low": low, "volume": volume,
        "amount": np.round(close * volume, 2),
        "turnover": np.round(volume / 5e7 * 100, 4),
        "pct_chg": np.round((close / prev - 1) * 100, 4),
    })[KLINE_COLUMNS]
    return df.tail(days).reset_index(drop=True)


# --------------------------------------------------------------------------- 可注入 sleep
class FakeSleep:
    """sleep 替身。provider 非空时把 ("sleep", sec) 写入 provider.events，便于断言“批间隔出现在两次 kline 调用之间”。"""

    def __init__(self, provider: Optional["FakeProvider"] = None):
        self.provider = provider
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(float(seconds))
        if self.provider is not None:
            self.provider.events.append(("sleep", float(seconds)))

    @property
    def total(self) -> float:
        return sum(self.calls)


@dataclass
class KlineCall:
    codes: list[str]
    days: int
    fq: str
    rate_limited: bool = False


# --------------------------------------------------------------------------- FakeProvider
class FakeProvider(DataProvider):
    name = "fake"

    def __init__(self, klines: Optional[dict[str, pd.DataFrame]] = None,
                 profiles: Optional[dict[str, dict]] = None,
                 calendar: Optional[list[str]] = None,
                 sectors: Optional[dict[str, dict]] = None,
                 version: str = "fake-1.0.0"):
        self.klines: dict[str, pd.DataFrame] = {normalize_code(c): d for c, d in (klines or {}).items()}
        self.profiles: dict[str, dict] = {normalize_code(c): dict(p) for c, p in (profiles or {}).items()}
        self.calendar: list[str] = list(calendar or [])
        # sectors: {板块名: {"code": "pt02GN2238", "members": [{"code","name"}, ...]}}
        self.sectors: dict[str, dict] = dict(sectors or {})
        self._version = version
        # 注入
        self.fail_codes: dict[str, str] = {}
        self.suspended: dict[str, str] = {}
        self.available_until: Optional[str] = None
        self.unavailable: bool = False
        self._rate_rules: list[dict] = []
        # 记录
        self.kline_calls: list[KlineCall] = []
        self.profile_calls: list[list[str]] = []
        self.sector_search_calls: list[str] = []
        self.sector_constituent_calls: list[str] = []
        self.calendar_calls: int = 0
        self.events: list[tuple] = []

    # ---- 构造便利 ----
    @classmethod
    def synthetic(cls, codes: Iterable[str], end_date: str, days: int = 300, seed: int = 0,
                  holidays: Iterable[str] = (), names: Optional[dict[str, str]] = None,
                  listed_date: str = "2015-06-01") -> "FakeProvider":
        codes6 = [normalize_code(c) for c in codes]
        names = names or {}
        hol = list(holidays)
        fp = cls(
            klines={c: make_kline_df(c, end_date, days, seed, hol) for c in codes6},
            profiles={c: {"name": names.get(c, f"测试{c}"), "listed_date": listed_date} for c in codes6},
            calendar=make_trading_days("2015-01-05", end_date, hol),
        )
        return fp

    def add_sector(self, name: str, code: str, members: list[dict]) -> None:
        self.sectors[name] = {"code": code, "members": [dict(m) for m in members]}

    def inject_rate_limit(self, times: int = 1, when_contains: Optional[str] = None) -> None:
        """接下来 times 次（命中条件的）get_klines 调用抛 RateLimited，之后恢复。
        when_contains 给定时只对“含该代码的批”生效（用于验证“仅重试该批”）。可叠加多条规则。"""
        self._rate_rules.append({"remaining": int(times),
                                 "contains": normalize_code(when_contains) if when_contains else None})

    def suspend(self, code: str, last_date: str) -> None:
        self.suspended[normalize_code(code)] = last_date

    # ---- 记录视图 ----
    @property
    def batches(self) -> list[list[str]]:
        return [list(c.codes) for c in self.kline_calls]

    @property
    def batch_sizes(self) -> list[int]:
        return [len(c.codes) for c in self.kline_calls]

    @property
    def requested_days(self) -> list[int]:
        return [c.days for c in self.kline_calls]

    def reset_calls(self) -> None:
        self.kline_calls.clear(); self.profile_calls.clear(); self.sector_search_calls.clear()
        self.sector_constituent_calls.clear(); self.events.clear(); self.calendar_calls = 0

    # ---- DataProvider ----
    def version(self) -> str:
        return self._version

    def get_klines(self, codes: list[str], days: int, fq: str):
        codes6 = [normalize_code(c) for c in codes]
        call = KlineCall(codes=codes6, days=int(days), fq=fq)
        self.kline_calls.append(call)
        self.events.append(("kline", tuple(codes6), int(days)))
        if self.unavailable:
            raise SourceUnavailable("FakeProvider: unavailable")
        for rule in self._rate_rules:
            if rule["remaining"] > 0 and (rule["contains"] is None or rule["contains"] in codes6):
                rule["remaining"] -= 1
                call.rate_limited = True
                raise RateLimited("FakeProvider: LOCAL_RATE_LIMITED")
        ok: dict[str, pd.DataFrame] = {}
        bad: dict[str, str] = {}
        for c in codes6:
            if c in self.fail_codes:
                bad[c] = self.fail_codes[c]
                continue
            df = self.klines.get(c)
            if df is None:
                bad[c] = "failed: unknown code"
                continue
            if self.available_until:
                df = df[df["date"] <= self.available_until]
            if c in self.suspended:
                df = df[df["date"] <= self.suspended[c]]
            if df.empty:
                bad[c] = "failed: no data"
                continue
            ok[c] = df.tail(int(days)).reset_index(drop=True).copy()
        return ok, bad

    def get_profiles(self, codes: list[str]) -> dict[str, dict]:
        codes6 = [normalize_code(c) for c in codes]
        self.profile_calls.append(codes6)
        if self.unavailable:
            raise SourceUnavailable("FakeProvider: unavailable")
        return {c: dict(self.profiles[c]) for c in codes6 if c in self.profiles}

    def search_sector(self, name: str) -> list[dict]:
        self.sector_search_calls.append(name)
        if self.unavailable:
            raise SourceUnavailable("FakeProvider: unavailable")
        return [{"code": v["code"], "name": k} for k, v in self.sectors.items() if name in k]

    def sector_constituents(self, sector_code: str) -> list[dict]:
        self.sector_constituent_calls.append(sector_code)
        if self.unavailable:
            raise SourceUnavailable("FakeProvider: unavailable")
        for v in self.sectors.values():
            if v["code"] == sector_code:
                return [dict(m) for m in v["members"]]
        return []

    def trade_calendar(self) -> list[str]:
        self.calendar_calls += 1
        if self.unavailable:
            raise SourceUnavailable("FakeProvider: unavailable")
        return list(self.calendar)


# --------------------------------------------------------------------------- 环境
def _deep_update(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
    return base


def make_env(tmp_path, **overrides):
    """在 tmp_path 下建 Paths(home=tmp_path, check=False)，拷贝 config.example.yaml，载入配置。
    overrides 为按顶层键的嵌套字典，如 make_env(tmp, fetch={"batch_interval": 1.5}, filters={"min_listed_days": 0})，
    在载入（含校验）之后深度合并，因此可以设出非法值用于测试。XB1_HOME 环境变量若存在会被临时屏蔽。"""
    from xb1lib.config import load_config
    from xb1lib.paths import Paths
    home = Path(tmp_path)
    shutil.copy(ROOT / "config.example.yaml", home / "config.example.yaml")
    saved = os.environ.pop("XB1_HOME", None)
    try:
        paths = Paths(home=home, check=False)
    finally:
        if saved is not None:
            os.environ["XB1_HOME"] = saved
    paths.ensure_dirs()
    cfg = load_config(paths)
    _deep_update(cfg, overrides)
    return paths, cfg
