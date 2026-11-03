"""数据迁移：只打包「股票池 + 这些股票的原始数据」，用于换机器。

打包内容（不含 runs/reports/history/state/cache，这些可重算或属个人产物）：
  config.yaml                     股票池与阈值（原样字节，保留注释）
  data/raw/kline/*.parquet        池内每只股票的日 K 线
  data/raw/profile/*.json         池内每只股票的资料（名称/上市日期）
  data/raw/calendar/*.json        交易日历缓存（省一次联网）
  data/raw/universe/*.json        板块快照（仅当配置用了 sectors）
  data/raw/pool/pool.txt          池内全部代码（扁平清单，便于新机器直接用 files 引用）
  manifest.json                   清单：代码数、各目录文件数、导出时间

还原后必须重新执行一次 run 才有信号/报告（本包不含结果）。
"""
from __future__ import annotations

import json
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Iterable, Optional

from . import atomic
from .calendar import shanghai_now
from .errors import ConfigError, XB1Error
from .models import normalize_code
from .paths import Paths
from .universe import read_code_file

MANIFEST = "manifest.json"
POOL_DIR = Path("data/raw/pool")
POOL_TXT = POOL_DIR / "pool.txt"


def pool_codes(paths: Paths, cfg: dict) -> list[str]:
    """配置里的全部股票代码（顶层 + 所有分组）。files 会读成代码，sectors 无法离线展开。"""
    u = cfg.get("universe") or {}
    sources = [u] + [g for g in (u.get("groups") or {}).values() if isinstance(g, dict)]
    out: list[str] = []
    has_sectors = False
    for src in sources:
        for c in src.get("stocks") or []:
            try:
                out.append(normalize_code(str(c)))
            except ValueError:
                pass
        for f in src.get("files") or []:
            out += read_code_file(paths, str(f))
        if src.get("sectors"):
            has_sectors = True
    if has_sectors:
        raise XB1Error("配置里有 universe.sectors（板块自动展开），离线无法确定成分股；"
                       "请先把板块展开成 stocks/files 再导出")
    return sorted(set(out))


def _existing(p: Path) -> list[Path]:
    return sorted(x for x in p.iterdir() if x.is_file()) if p.exists() else []


def export_bundle(paths: Paths, cfg: dict, out: Path,
                  codes: Optional[Iterable[str]] = None) -> dict:
    """打包股票池与对应原始数据。codes 缺省 = 配置里的整个股票池。"""
    out = Path(out).expanduser()
    if out.exists():
        raise ConfigError(f"目标已存在，先删除或换个名字：{out}")
    pool = sorted(set(pool_codes(paths, cfg) if codes is None else
                      (normalize_code(str(c)) for c in codes)))
    if not pool:
        raise ConfigError("股票池为空，无可导出的数据")

    kline_dir, prof_dir = paths.kline_dir, paths.profile_dir
    kl = [p for p in (kline_dir / f"{c}.parquet" for c in pool) if p.exists()]
    pr = [p for p in (prof_dir / f"{c}.json" for c in pool) if p.exists()]
    cal = _existing(paths.raw / "calendar")
    uni = _existing(paths.raw / "universe")

    uses_files = [f for src in [(cfg.get("universe") or {})] + list((cfg.get("universe") or {}).get("groups", {}).values())
                  for f in (src.get("files") or [])] if isinstance(cfg, dict) else []
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    manifest = {
        "format": "xb1-bundle/1",
        "exported_at": shanghai_now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_home": str(paths.home),
        "codes": len(pool),
        "kline_files": len(kl),
        "profile_files": len(pr),
        "config_included": paths.config.exists(),
        "config_uses_files": bool(uses_files),
    }
    with tarfile.open(tmp, "w:gz") as tf:
        def add(p: Path, arcname: str) -> None:
            tf.add(p, arcname=arcname, recursive=False)

        if paths.config.exists():
            add(paths.config, "config.yaml")
        for p in kl:
            add(p, f"data/raw/kline/{p.name}")
        for p in pr:
            add(p, f"data/raw/profile/{p.name}")
        for p in cal:
            add(p, f"data/raw/calendar/{p.name}")
        for p in uni:
            add(p, f"data/raw/universe/{p.name}")
        pool_txt = Path(tempfile.mkdtemp()) / "pool.txt"
        pool_txt.write_text("\n".join(pool) + "\n", encoding="utf-8")
        add(pool_txt, POOL_TXT.as_posix())
        mf = Path(tempfile.mkdtemp()) / MANIFEST
        mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        add(mf, MANIFEST)
    tmp.replace(out)
    manifest["bytes"] = out.stat().st_size
    manifest["missing_kline"] = len(pool) - len(kl)
    return manifest


def import_bundle(paths: Paths, archive: Path, force: bool = False) -> dict:
    """把包解到当前 XB1_HOME。已存在的 config.yaml 默认不覆盖（保留本机配置）。"""
    archive = Path(archive).expanduser()
    if not archive.exists():
        raise ConfigError(f"文件不存在：{archive}")
    paths.ensure_dirs()
    with tempfile.TemporaryDirectory() as td:
        with tarfile.open(archive, "r:gz") as tf:
            for m in tf.getmembers():          # 防目录穿越
                if not (m.name == MANIFEST or m.name.startswith(("data/", "config.yaml"))):
                    raise ConfigError(f"包内有非法路径：{m.name}")
                if ".." in Path(m.name).parts:
                    raise ConfigError(f"包内有非法路径：{m.name}")
            tf.extractall(td, filter="data")   # 只还原目录与文件内容，不还原属主/权限
        root = Path(td)
        if not (root / MANIFEST).exists():
            raise ConfigError("不是 XB1 数据包（缺 manifest.json）")
        manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))

        copied = 0
        for sub in ("kline", "profile", "calendar", "universe"):
            src = root / "data" / "raw" / sub
            if not src.exists():
                continue
            dst = paths.raw / sub
            dst.mkdir(parents=True, exist_ok=True)
            for f in src.iterdir():
                target = dst / f.name
                if target.exists() and not force:
                    continue
                atomic.write_bytes(target, f.read_bytes())
                copied += 1
        pool_src = root / POOL_TXT
        if pool_src.exists():
            (paths.raw / "pool").mkdir(parents=True, exist_ok=True)
            atomic.write_bytes(paths.raw / "pool" / "pool.txt", pool_src.read_bytes())

        cfg_src = root / "config.yaml"
        config_state = "kept"
        if cfg_src.exists():
            if paths.config.exists() and not force:
                paths.config.with_name("config.yaml.from-bundle").write_bytes(cfg_src.read_bytes())
                config_state = "kept (bundled copy saved as config.yaml.from-bundle)"
            else:
                atomic.write_bytes(paths.config, cfg_src.read_bytes())
                config_state = "written"
    return {"copied_files": copied, "config": config_state, "manifest": manifest}
