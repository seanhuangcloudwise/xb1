"""运行状态：幂等记录、运行锁、单次运行日志（PRD 5.1.1 state/、logs/）。"""
from __future__ import annotations

import datetime as _dt
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from . import atomic
from .errors import XB1Error
from .paths import Paths

LOCK_STALE_SECONDS = 2 * 3600


def now_str() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- state.json
class State:
    """state.json：{"last_success_date": str|None, "days": {date: {status, exit_code, checksum, finished_at, ...}}}。
    success 记录是一次运行的“提交点”——最后一步才写，因此中断的运行永远不会被当成已完成。"""

    def __init__(self, paths: Paths):
        self.paths = paths

    def load(self) -> dict:
        data = atomic.read_json(self.paths.state_file, default=None)
        if not isinstance(data, dict):
            data = {}
        data.setdefault("last_success_date", None)
        data.setdefault("days", {})
        return data

    def get(self, date: str) -> Optional[dict]:
        return self.load()["days"].get(date)

    def is_success(self, date: str) -> bool:
        rec = self.get(date)
        return bool(rec and rec.get("status") == "success")

    def last_success_date(self) -> Optional[str]:
        return self.load().get("last_success_date")

    def mark(self, date: str, status: str, exit_code: int, **extra) -> None:
        data = self.load()
        rec = {"status": status, "exit_code": int(exit_code), "finished_at": now_str(), **extra}
        data["days"][date] = rec
        if status == "success":
            last = data.get("last_success_date")
            if not last or date >= last:
                data["last_success_date"] = date
        atomic.write_json(self.paths.state_file, data)


# --------------------------------------------------------------------------- run.lock
def _pid_alive(pid: int) -> bool:
    if os.name == "nt":      # Windows 上 os.kill(pid, 0) 会真的终止进程，只能靠锁龄判断
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _lock_is_stale(path: Path) -> bool:
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        pid, ts = int(info.get("pid", 0)), float(info.get("ts", 0))
    except Exception:
        return True            # 内容损坏 -> 视为遗留
    if time.time() - ts > LOCK_STALE_SECONDS:
        return True
    return not _pid_alive(pid)


@contextmanager
def run_lock(paths: Paths):
    """独占运行锁，防止 21:00 自动化与手动运行并发。遗留锁（进程已死/超 2 小时）自动清除。"""
    path = paths.lock_file
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    for _ in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if _lock_is_stale(path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                continue
            raise XB1Error(f"已有 xb1 运行进行中（锁文件 {path}），请稍后再试或确认无残留后删除该文件")
    if fd is None:
        raise XB1Error(f"无法获取运行锁：{path}")
    try:
        os.write(fd, json.dumps({"pid": os.getpid(), "ts": time.time(), "at": now_str()}).encode("utf-8"))
        os.close(fd)
        yield
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


# --------------------------------------------------------------------------- run-{date}.log
class RunLog:
    """追加写 data/logs/run-{日期}.log。写日志失败不影响主流程。"""

    def __init__(self, path: Optional[Path]):
        self.path = path

    def write(self, msg: str) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(f"{now_str()}\t{msg}\n")
        except OSError:
            pass
