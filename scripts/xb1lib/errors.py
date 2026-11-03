"""统一异常。CLI 入口捕获 XB1Error，用其 exit_code 退出。"""
from __future__ import annotations

from . import constants as C


class XB1Error(Exception):
    exit_code = C.EXIT_OTHER

    def __init__(self, message: str, exit_code: int | None = None):
        super().__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code


class NotTradingDay(XB1Error):
    exit_code = C.EXIT_NOT_TRADING_DAY


class SourceUnavailable(XB1Error):
    exit_code = C.EXIT_SOURCE_UNAVAILABLE


class DataStale(XB1Error):
    exit_code = C.EXIT_DATA_STALE


class ConfigError(XB1Error):
    exit_code = C.EXIT_CONFIG_ERROR


class VolumeNotMounted(XB1Error):
    exit_code = C.EXIT_VOLUME_NOT_MOUNTED


class ParseError(XB1Error):
    """westock 输出解析失败：必须抛出，不得返回空 DataFrame（PRD 3.1-2）。"""
    exit_code = C.EXIT_SOURCE_UNAVAILABLE


class RateLimited(XB1Error):
    exit_code = C.EXIT_SOURCE_UNAVAILABLE
