"""所有路径的唯一来源（PRD 5.1.2）。业务代码禁止硬编码路径或使用 ~。

根目录解析优先级：环境变量 XB1_HOME > 命令行 --home > 脚本位置向上两级。
"""
from __future__ import annotations

import os
from pathlib import Path

from .errors import VolumeNotMounted

DEFAULT_HOME_HINT = "/Volumes/work/Workspace/XB1"


def resolve_home(cli_home: str | os.PathLike | None = None) -> Path:
    env = os.environ.get("XB1_HOME")
    if env:
        return Path(env).expanduser().resolve()
    if cli_home:
        return Path(cli_home).expanduser().resolve()
    # xb1lib/paths.py -> xb1lib -> scripts -> <home>
    return Path(__file__).resolve().parent.parent.parent


def check_volume(home: Path) -> None:
    """/Volumes/<vol>/... 下的目录，要求挂载点 /Volumes/<vol> 存在，
    否则抛 VolumeNotMounted（退出码 50），绝不在别处新建同名目录。"""
    parts = Path(home).parts
    if len(parts) >= 3 and parts[0] == os.sep and parts[1] == "Volumes":
        mount = Path(os.sep, parts[1], parts[2])
        if not mount.exists():
            raise VolumeNotMounted(f"工作卷未挂载：{mount}（期望根目录 {home}）")
    if not Path(home).exists():
        raise VolumeNotMounted(f"项目根目录不存在：{home}")
    if not os.access(home, os.W_OK):
        raise VolumeNotMounted(f"项目根目录不可写：{home}")


class Paths:
    def __init__(self, home: str | os.PathLike | None = None, check: bool = True):
        self.home: Path = resolve_home(home)
        if check:
            check_volume(self.home)

    # ---- 代码/配置 ----
    @property
    def config(self) -> Path: return self.home / "config.yaml"
    @property
    def config_example(self) -> Path: return self.home / "config.example.yaml"
    @property
    def references(self) -> Path: return self.home / "references"
    @property
    def tags_file(self) -> Path: return self.home / "tags.yaml"      # 标签定义（人工维护，随项目走）

    # ---- data 根 ----
    @property
    def data(self) -> Path: return self.home / "data"

    # portfolio：股票档案（人工+定期评估，价值随时间增长，与 history 同级需要备份）
    @property
    def portfolio_dir(self) -> Path: return self.data / "portfolio"
    def portfolio_file(self, code: str) -> Path: return self.portfolio_dir / f"{code}.yaml"

    # raw
    @property
    def raw(self) -> Path: return self.data / "raw"
    def kline(self, code: str) -> Path: return self.raw / "kline" / f"{code}.parquet"
    @property
    def kline_dir(self) -> Path: return self.raw / "kline"
    @property
    def universe_dir(self) -> Path: return self.raw / "universe"
    def universe_snapshot(self, sector_code: str, date: str) -> Path:
        return self.universe_dir / f"{sector_code}_{date}.json"
    @property
    def calendar_file(self) -> Path: return self.raw / "calendar" / "trade_calendar.json"
    def profile(self, code: str) -> Path: return self.raw / "profile" / f"{code}.json"
    @property
    def profile_dir(self) -> Path: return self.raw / "profile"

    # cache
    @property
    def cache(self) -> Path: return self.data / "cache"
    def indicators(self, code: str) -> Path: return self.cache / "indicators" / f"{code}.parquet"

    # runs
    @property
    def runs(self) -> Path: return self.data / "runs"
    def run_dir(self, date: str) -> Path: return self.runs / date
    def signals_json(self, date: str) -> Path: return self.run_dir(date) / "signals.json"
    def brief_md(self, date: str) -> Path: return self.run_dir(date) / "brief.md"
    def meta_json(self, date: str) -> Path: return self.run_dir(date) / "meta.json"
    def fetch_errors_json(self, date: str) -> Path: return self.run_dir(date) / "fetch_errors.json"

    # reports
    @property
    def reports(self) -> Path: return self.data / "reports"
    def report_dir(self, date: str) -> Path: return self.reports / date
    def report_xlsx(self, date: str) -> Path: return self.report_dir(date) / "b1_report.xlsx"
    def report_md(self, date: str) -> Path: return self.report_dir(date) / "b1_report.md"
    def report_html(self, date: str) -> Path: return self.report_dir(date) / "b1_cards.html"
    @property
    def reports_latest(self) -> Path: return self.reports / "latest"

    # history
    @property
    def history(self) -> Path: return self.data / "history"
    @property
    def signals_history(self) -> Path: return self.history / "signals.parquet"
    @property
    def outcomes_history(self) -> Path: return self.history / "outcomes.parquet"

    # logs
    @property
    def logs(self) -> Path: return self.data / "logs"
    def run_log(self, date: str) -> Path: return self.logs / f"run-{date}.log"
    def westock_log(self, date: str) -> Path: return self.logs / f"westock-{date}.log"

    # state
    @property
    def state_dir(self) -> Path: return self.data / "state"
    @property
    def state_file(self) -> Path: return self.state_dir / "state.json"
    @property
    def lock_file(self) -> Path: return self.state_dir / "run.lock"

    def ensure_dirs(self) -> None:
        for p in (self.kline_dir, self.universe_dir, self.calendar_file.parent, self.profile_dir,
                  self.cache / "indicators", self.runs, self.reports, self.history,
                  self.logs, self.state_dir, self.portfolio_dir):
            p.mkdir(parents=True, exist_ok=True)
