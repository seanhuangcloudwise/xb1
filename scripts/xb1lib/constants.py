"""全局常量：退出码、版本、等级名。业务代码一律从这里取，不得硬编码。"""
from __future__ import annotations

ENGINE_VERSION = "1.2.0"
TZ = "Asia/Shanghai"

# ---- 退出码（PRD 5.2）----
EXIT_OK = 0
EXIT_OTHER = 1
EXIT_NOT_TRADING_DAY = 10
EXIT_SOURCE_UNAVAILABLE = 20
EXIT_DATA_STALE = 30
EXIT_CONFIG_ERROR = 40
EXIT_VOLUME_NOT_MOUNTED = 50

# ---- 信号等级（signals.evaluate 返回的 level 取值）----
LEVEL_CONFIRM = "confirm"    # B1 确认（F5）
LEVEL_PERFECT = "perfect"    # 完美 B1
LEVEL_STANDARD = "standard"  # 标准 B1
LEVEL_WATCH = "watch"        # 观察
LEVEL_NONE = "none"          # 不符合
LEVELS = (LEVEL_CONFIRM, LEVEL_PERFECT, LEVEL_STANDARD, LEVEL_WATCH, LEVEL_NONE)

# ---- 预警类别（list warn 的子类）----
WARN_BULL_ROPE_BREAK = "rope_break"   # 白线刚下穿黄线（牛绳断）
WARN_BELOW_BBI_2D = "below_bbi_2d"    # 连续 2 日跌破 BBI
WARN_LABELS = {
    WARN_BULL_ROPE_BREAK: "牛绳断",
    WARN_BELOW_BBI_2D: "连续破BBI",
}

# ---- 取数状态（fetch_errors.json 的 reason 前缀）----
FETCH_RATE_LIMITED = "rate_limited"
FETCH_FAILED = "failed"
FETCH_SUSPENDED = "suspended"   # 最后一根 K 线日期 != 当日交易日
FETCH_STALE = "stale"

# ---- K 线标准列（所有 provider 必须归一到这些列名，顺序固定）----
KLINE_COLUMNS = ["date", "open", "close", "high", "low", "volume", "amount", "turnover", "pct_chg"]
