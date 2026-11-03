"""原子写入：先写同目录临时文件，再 os.replace（PRD 5.1.2-5）。"""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


@contextmanager
def _tmp_target(path: Path) -> Iterator[Path]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        yield tmp_path
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def write_bytes(path: Path, data: bytes) -> None:
    with _tmp_target(path) as t:
        with open(t, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())


def write_text(path: Path, text: str) -> None:
    write_bytes(path, text.encode("utf-8"))


def write_json(path: Path, obj: Any) -> None:
    write_text(path, json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def write_parquet(path: Path, df) -> None:
    with _tmp_target(path) as t:
        df.to_parquet(t, index=False)


def write_with(path: Path, writer) -> None:
    """writer(tmp_path) 自行写入（如 openpyxl 的 wb.save）。"""
    with _tmp_target(path) as t:
        writer(t)


def read_json(path: Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)
