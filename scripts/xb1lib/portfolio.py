"""股票档案与标签（data/portfolio/{code}.yaml + tags.yaml）。

档案字段
    code/name/board        基础信息（name/board 由分组初始化或 profile 填入）
    groups                 所属分组（config.yaml universe.groups 的键，可多个）
    tags                   标签（必须是 tags.yaml 里定义过的名字，除非显式 allow_new）
    description            一句话业务描述
    thesis                 投资逻辑 / 为什么关注
    sources                初始化来源，如 {"板块命中": ["人形机器人", "减速器"], "命中数": 2}
    assessments[]          评估历史：{date, note, b1_level?, score?}，只追加
    notes[]                备注：{date, text}，只追加
    review_every_days      复核周期（天），默认 cfg.portfolio.review_every_days
    next_review            下次复核日期；`portfolio due` 列出已到期的
    created_at/updated_at

设计要点：所有写入走 atomic；读取遇到损坏文件抛 ConfigError 而不是静默跳过（档案是人工资产，不能悄悄丢）。
"""
from __future__ import annotations

import datetime as _dt
from typing import Iterable, Optional

import yaml

from . import atomic
from .errors import ConfigError
from .models import normalize_code
from .paths import Paths

DEFAULT_REVIEW_DAYS = 30


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _skeleton(code: str) -> dict:
    return {
        "code": code, "name": "", "board": "", "groups": [], "tags": [],
        "description": "", "thesis": "", "sources": {},
        "assessments": [], "notes": [],
        "review_every_days": None, "next_review": None,
        "created_at": None, "updated_at": None,
    }


def _read_yaml(path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        d = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError) as e:
        raise ConfigError(f"档案文件损坏，已拒绝覆盖：{path}（{e}）")
    if not isinstance(d, dict):
        raise ConfigError(f"档案文件格式错误（顶层应为映射）：{path}")
    return d


def _dedupe_sorted(items: Iterable[str]) -> list[str]:
    return sorted({str(x).strip() for x in items if str(x).strip()})


class TagRegistry:
    """tags.yaml：{"tags": [{name, description?, color?, category?}, ...]}。人工维护，也可 tag-define 追加。"""

    def __init__(self, paths: Paths):
        self.paths = paths

    def tags(self) -> list[dict]:
        d = _read_yaml(self.paths.tags_file) or {}
        out = d.get("tags") or []
        if not isinstance(out, list):
            raise ConfigError(f"tags.yaml 的 tags 必须是列表：{self.paths.tags_file}")
        return [t for t in out if isinstance(t, dict) and t.get("name")]

    def names(self) -> set[str]:
        return {str(t["name"]) for t in self.tags()}

    def unknown(self, tags: Iterable[str]) -> list[str]:
        known = self.names()
        return [t for t in tags if t not in known]

    def define(self, name: str, description: str = "", color: str = "", category: str = "",
               sectors: Iterable[str] = (), min_sector_hits: Optional[int] = None) -> bool:
        """追加标签定义；已存在返回 False。
        以“文本追加”方式写入，保留 tags.yaml 里手写的注释；追加后重新解析校验，失败则回滚（不写盘）。"""
        name = name.strip()
        if not name:
            raise ConfigError("标签名不能为空")
        path = self.paths.tags_file
        existing = path.read_text(encoding="utf-8") if path.exists() else "tags:\n"
        d = _read_yaml(path) or {}
        if any(isinstance(t, dict) and t.get("name") == name for t in (d.get("tags") or [])):
            return False
        entry = {"name": name}
        for k, v in (("description", description), ("color", color), ("category", category)):
            if v:
                entry[k] = v
        if sectors:
            entry["sectors"] = list(sectors)
        if min_sector_hits:
            entry["min_sector_hits"] = int(min_sector_hits)
        block = yaml.safe_dump([entry], allow_unicode=True, sort_keys=False)
        block = "".join("  " + ln if ln.strip() else ln for ln in block.splitlines(True))
        new_text = existing.rstrip("\n") + "\n\n" + block
        try:
            parsed = yaml.safe_load(new_text) or {}
            ok = isinstance(parsed.get("tags"), list) and parsed["tags"][-1].get("name") == name
        except yaml.YAMLError:
            ok = False
        if not ok:   # 文件顶层结构不是“tags 列表在最后”——退回整体重写（会丢注释）
            lst = list(d.get("tags") or []) + [entry]
            atomic.write_text(path, yaml.safe_dump({**d, "tags": lst}, allow_unicode=True, sort_keys=False))
            return True
        atomic.write_text(path, new_text)
        return True


class Portfolio:
    def __init__(self, paths: Paths, review_days: int = DEFAULT_REVIEW_DAYS, today: Optional[str] = None):
        self.paths = paths
        self.review_days = int(review_days)
        self.today = today or _dt.date.today().isoformat()

    # ---- 读 ----
    def exists(self, code: str) -> bool:
        return self.paths.portfolio_file(normalize_code(code)).exists()

    def get(self, code: str) -> dict:
        code = normalize_code(code)
        rec = _skeleton(code)
        rec.update(_read_yaml(self.paths.portfolio_file(code)) or {})
        return rec

    def all(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        d = self.paths.portfolio_dir
        if not d.exists():
            return out
        for p in sorted(d.glob("*.yaml")):
            rec = _read_yaml(p) or {}
            code = str(rec.get("code") or p.stem)
            base = _skeleton(code)
            base.update(rec)
            out[code] = base
        return out

    def find(self, group: Optional[str] = None, tag: Optional[str] = None) -> dict[str, dict]:
        recs = self.all()
        if group:
            recs = {c: r for c, r in recs.items() if group in (r.get("groups") or [])}
        if tag:
            recs = {c: r for c, r in recs.items() if tag in (r.get("tags") or [])}
        return recs

    def tags_of(self, code: str) -> tuple:
        """给 universe 用：没有档案时返回空元组，不创建文件。"""
        if not self.exists(code):
            return ()
        return tuple(self.get(code).get("tags") or [])

    # ---- 写 ----
    def put(self, rec: dict) -> dict:
        rec = dict(rec)
        code = normalize_code(rec["code"])
        rec["code"] = code
        now = _now()
        rec["created_at"] = rec.get("created_at") or now
        rec["updated_at"] = now
        rec["groups"] = _dedupe_sorted(rec.get("groups") or [])
        rec["tags"] = _dedupe_sorted(rec.get("tags") or [])
        atomic.write_text(self.paths.portfolio_file(code),
                          yaml.safe_dump(rec, allow_unicode=True, sort_keys=False, width=120))
        return rec

    def _schedule(self, rec: dict, from_date: Optional[str] = None) -> None:
        days = int(rec.get("review_every_days") or self.review_days)
        base = _dt.date.fromisoformat(from_date or self.today)
        rec["next_review"] = (base + _dt.timedelta(days=days)).isoformat()

    def upsert(self, code: str, *, name: str = "", board: str = "", groups: Iterable[str] = (),
               tags: Iterable[str] = (), description: str = "", sources: Optional[dict] = None) -> tuple[dict, bool]:
        """初始化/合并：已有字段不被空值覆盖；groups/tags 取并集。返回 (档案, 是否新建)。"""
        code = normalize_code(code)
        existed = self.exists(code)
        rec = self.get(code)
        rec["name"] = rec["name"] or name
        rec["board"] = rec["board"] or board
        rec["groups"] = _dedupe_sorted(list(rec["groups"]) + list(groups))
        rec["tags"] = _dedupe_sorted(list(rec["tags"]) + list(tags))
        rec["description"] = rec["description"] or description
        if sources:
            rec["sources"] = {**(rec.get("sources") or {}), **sources}
        if not rec.get("next_review"):
            self._schedule(rec)
        return self.put(rec), not existed

    def update_fields(self, code: str, **fields) -> dict:
        allowed = {"name", "description", "thesis", "review_every_days"}
        bad = set(fields) - allowed
        if bad:
            raise ConfigError(f"不允许直接修改的字段：{sorted(bad)}")
        rec = self.get(code)
        for k, v in fields.items():
            if v is not None:
                rec[k] = v
        if "review_every_days" in fields and fields["review_every_days"]:
            self._schedule(rec)
        return self.put(rec)

    def add_tags(self, code: str, tags: Iterable[str]) -> dict:
        rec = self.get(code)
        rec["tags"] = list(rec["tags"]) + list(tags)
        return self.put(rec)

    def remove_tags(self, code: str, tags: Iterable[str]) -> dict:
        rec = self.get(code)
        drop = set(tags)
        rec["tags"] = [t for t in rec["tags"] if t not in drop]
        return self.put(rec)

    def add_assessment(self, code: str, note: str, b1_level: Optional[str] = None,
                       score: Optional[int] = None, date: Optional[str] = None, auto: bool = False) -> dict:
        """追加评估并把下次复核日顺延（评估即视为一次复核）。"""
        if not note.strip():
            raise ConfigError("评估内容不能为空")
        rec = self.get(code)
        d = date or self.today
        entry = {"date": d, "note": note.strip()}
        if b1_level:
            entry["b1_level"] = b1_level
        if score is not None:
            entry["score"] = int(score)
        if auto:
            entry["auto"] = True
        rec["assessments"] = list(rec["assessments"]) + [entry]
        self._schedule(rec, d)
        return self.put(rec)

    def record_refresh(self, code: str, profile: Optional[dict] = None, snapshot: Optional[dict] = None) -> tuple[dict, bool]:
        """一次定期刷新只写一次盘：
        profile  -> rec["profile"]（机器维护：business/industry/sector/listed_date + refreshed_at），不碰人工写的 description/thesis；
        snapshot -> 追加一条 auto 评估 {date, note, b1_level, score, auto: True}，同一日期已有 auto 快照则不重复；
        并把下次复核日顺延到 today + 复核周期。返回 (档案, 是否追加了快照)。"""
        rec = self.get(code)
        if profile:
            prof = dict(rec.get("profile") or {})
            prof.update({k: v for k, v in profile.items() if v})
            prof["refreshed_at"] = self.today
            rec["profile"] = prof
            if not rec["name"] and profile.get("name"):
                rec["name"] = profile["name"]
        added = False
        if snapshot:
            dup = any(a.get("auto") and a.get("date") == snapshot["date"] for a in rec["assessments"])
            if not dup:
                e = {"date": snapshot["date"], "note": snapshot["note"], "auto": True}
                for k in ("b1_level", "score"):
                    if snapshot.get(k) is not None:
                        e[k] = snapshot[k]
                rec["assessments"] = list(rec["assessments"]) + [e]
                added = True
        self._schedule(rec)
        return self.put(rec), added

    def add_note(self, code: str, text: str, date: Optional[str] = None) -> dict:
        if not text.strip():
            raise ConfigError("备注内容不能为空")
        rec = self.get(code)
        rec["notes"] = list(rec["notes"]) + [{"date": date or self.today, "text": text.strip()}]
        return self.put(rec)

    def due(self, as_of: Optional[str] = None) -> list[dict]:
        """next_review <= as_of 的档案（含从未设置复核日的），按到期日升序。"""
        d = as_of or self.today
        rows = []
        for rec in self.all().values():
            nr = rec.get("next_review")
            if not nr or str(nr) <= d:
                rows.append(rec)
        rows.sort(key=lambda r: (str(r.get("next_review") or ""), r["code"]))
        return rows

    def last_assessment(self, code: str) -> Optional[dict]:
        a = self.get(code).get("assessments") or []
        return a[-1] if a else None


# --------------------------------------------------------------------------- 分组成员 / 自动标签
def group_members(paths: Paths, cfg: dict, provider, group: str, today: str, sleep) -> tuple[dict[str, dict], list[str]]:
    """分组的“原始”成员（未做 ST/北交所/次新过滤）及其来源：{code: {"name", "sectors": [板块名...], "manual": bool}}。
    过滤由 build_universe 负责；init 用两者的交集，既保留板块命中信息，又与 run 实际跑的股票池一致。"""
    from .universe import (_Throttle, _code_from_config, _group_sources, _load_sector, read_code_file)
    groups = _group_sources(cfg.get("universe") or {})
    if group not in groups:
        raise ConfigError(f"分组 {group!r} 不存在（已有：{sorted(groups)}）")
    src, warnings = groups[group], []
    throttle = _Throttle(float((cfg.get("fetch") or {}).get("batch_interval", 1.5)), sleep)
    throttle.sleep = sleep
    out: dict[str, dict] = {}

    def slot(c: str) -> dict:
        return out.setdefault(c, {"name": "", "sectors": [], "manual": False})

    for v in src["stocks"]:
        slot(_code_from_config(v))["manual"] = True
    for p in src["files"]:
        for c in read_code_file(paths, p):
            slot(c)["manual"] = True
    for key in src["sectors"]:
        for it in _load_sector(paths, cfg, provider, str(key), today, throttle, warnings):
            s = slot(normalize_code(it["code"]))
            s["name"] = s["name"] or it.get("name", "")
            if str(key) not in s["sectors"]:
                s["sectors"].append(str(key))
    return out, warnings


def auto_tags(tagdefs: list[dict], hit_sectors: Iterable[str]) -> list[str]:
    """tags.yaml 里的自动打标规则：
        sectors: [板块名...]     命中其中任一板块 -> 打该标签
        min_sector_hits: N       命中本分组内 ≥N 个不同板块 -> 打该标签
    两个条件各自独立生效（满足任一即打）。"""
    hits = set(hit_sectors)
    out = []
    for t in tagdefs:
        rule_sectors = set(t.get("sectors") or [])
        need = t.get("min_sector_hits")
        if (rule_sectors and hits & rule_sectors) or (need and len(hits) >= int(need)):
            out.append(t["name"])
    return out
