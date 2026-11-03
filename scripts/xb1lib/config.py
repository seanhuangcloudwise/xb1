"""配置加载：config.example.yaml 作为默认值，config.yaml 深度覆盖。"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from . import atomic
from .errors import ConfigError
from .paths import Paths


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _load_yaml(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"配置文件解析失败 {path}: {e}")
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是映射：{path}")
    return data


def defaults(paths: Paths) -> dict:
    if not paths.config_example.exists():
        raise ConfigError(f"缺少配置模板：{paths.config_example}")
    return _load_yaml(paths.config_example)


def load_config(paths: Paths) -> dict:
    cfg = defaults(paths)
    if paths.config.exists():
        cfg = _deep_merge(cfg, _load_yaml(paths.config))
    validate(cfg)
    return cfg


def validate(cfg: dict) -> None:
    f = cfg.get("fetch", {})
    if int(f.get("batch_size", 20)) > 20 or int(f.get("batch_size", 20)) < 1:
        raise ConfigError("fetch.batch_size 必须在 1..20（westock 上限 20 只/次）")
    if float(f.get("batch_interval", 1.5)) < 1.0:
        raise ConfigError("fetch.batch_interval 不得小于 1.0 秒（限流约束）")
    t = cfg.get("thresholds", {})
    if t.get("perfect_min_score", 75) < t.get("standard_min_score", 50):
        raise ConfigError("perfect_min_score 不得小于 standard_min_score")


def config_hash(cfg: dict) -> str:
    blob = json.dumps(cfg, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:8]


def get_path(cfg: dict, dotted: str) -> Any:
    cur: Any = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise ConfigError(f"配置项不存在：{dotted}")
        cur = cur[part]
    return cur


def set_value(paths: Paths, dotted: str, raw_value: str) -> Any:
    """修改 config.yaml（仅覆盖层）。值按 YAML 解析（数字/布尔/列表）。"""
    base = defaults(paths)
    get_path(base, dotted)  # 必须是已知配置项
    try:
        value = yaml.safe_load(raw_value)
    except yaml.YAMLError as e:
        raise ConfigError(f"值解析失败：{e}")
    over = _load_yaml(paths.config) if paths.config.exists() else {}
    cur = over
    parts = dotted.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value
    validate(_deep_merge(base, over))
    atomic.write_text(paths.config, yaml.safe_dump(over, allow_unicode=True, sort_keys=False))
    return value
