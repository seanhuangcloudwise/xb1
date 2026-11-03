"""数据源抽象（PRD 3.1-5）。v1 只实现 westock；备用源（baostock/AKShare）按同一接口接入。

约定（DEV_CONTRACTS「provider / calendar / universe / fetcher」）：
- 股票代码在 provider 对外接口里一律是 6 位纯数字（'300124'）；市场前缀（sh/sz）只在适配器内部拼命令时出现。
- K 线 DataFrame 列固定为 constants.KLINE_COLUMNS，date 为 YYYY-MM-DD 字符串，升序无重复，
  amount 已换算为元，pct_chg/turnover 为百分数。
- 解析失败必须抛 ParseError，绝不返回空 DataFrame / 空结果冒充成功。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

MAX_BATCH = 20          # westock kline 单次最多 20 只（PRD 3.1-1）
MAX_PROFILE_BATCH = 10  # westock profile 单次最多 10 只（2026-11-03 实测：传 20 个报"一次最多查询 10 个代码"）


class DataProvider(ABC):
    name: str = "base"

    @abstractmethod
    def version(self) -> str:
        """数据源/CLI 版本字符串（写入 meta.json）。"""

    @abstractmethod
    def get_klines(self, codes: list[str], days: int, fq: str) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
        """取一批（≤20 只）最近 days 根日 K（含今日，若已更新）。

        返回 (成功 {code: 标准 K 线 df}, 单只失败 {code: reason})。
        整批限流抛 RateLimited；整体不可用抛 SourceUnavailable；输出完全无法解析抛 ParseError。
        """

    @abstractmethod
    def get_profiles(self, codes: list[str]) -> dict[str, dict]:
        """{code: {"name": str, "listed_date": 'YYYY-MM-DD' | None}}；取不到的代码不出现在结果里。"""

    @abstractmethod
    def search_sector(self, name: str) -> list[dict]:
        """按名称搜板块 -> [{"code": 板块代码, "name": 板块名}]；确实无结果返回 []。"""

    @abstractmethod
    def sector_constituents(self, sector_code: str) -> list[dict]:
        """板块成分股 -> [{"code": 6 位代码, "name": 名称}]。"""

    @abstractmethod
    def trade_calendar(self) -> list[str]:
        """全部交易日（YYYY-MM-DD，升序）。"""
