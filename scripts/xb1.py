#!/usr/bin/env python3
"""XB1 统一入口。所有数字/信号只从这里产出；Agent 只负责触发与原样转述。

退出码：0 成功｜10 非交易日｜20 数据源不可用｜30 数据未更新｜40 配置错误｜50 工作卷未挂载/不可写｜1 其他
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xb1lib import cards  # noqa: E402
from xb1lib import constants as C  # noqa: E402
from xb1lib import config as cfgmod  # noqa: E402
from xb1lib import history, maintenance, pipeline, render  # noqa: E402
from xb1lib.calendar import TradeCalendar, shanghai_now, shanghai_today  # noqa: E402
from xb1lib.errors import ConfigError, NotTradingDay, XB1Error  # noqa: E402
from xb1lib.paths import Paths  # noqa: E402
from xb1lib.state import RunLog, State, run_lock  # noqa: E402


# --------------------------------------------------------------------------- 参数
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--home", default=argparse.SUPPRESS, help="项目根目录（环境变量 XB1_HOME 优先）")

    p = argparse.ArgumentParser(prog="xb1.py", description="XB1 —— B1 选股 Skill 引擎", parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", parents=[common], help="检查 westock/依赖/配置")

    r = sub.add_parser("run", parents=[common], help="执行完整复盘（幂等）")
    r.add_argument("--date", help="数据日期 YYYY-MM-DD，默认今天；用于历史回放")
    r.add_argument("--force", action="store_true", help="忽略幂等，强制重算")
    r.add_argument("--format", choices=["brief", "json"], default="brief")
    r.add_argument("--group", help="只看指定分组的结果（信号对全部股票池只算一次，分组是视图）")
    r.add_argument("--tag", help="只看带该标签的股票")

    la = sub.add_parser("latest", parents=[common], help="读取最近一次结果，不重算")
    la.add_argument("--format", choices=["brief", "json"], default="brief")
    la.add_argument("--group")
    la.add_argument("--tag")

    e = sub.add_parser("explain", parents=[common], help="单只股票的指标/命中条件/扣分项")
    e.add_argument("code")
    e.add_argument("--date")

    li = sub.add_parser("list", parents=[common], help="按类别列出完整名单")
    li.add_argument("kind", choices=list(render.LIST_KINDS))
    li.add_argument("--date")
    li.add_argument("--group")
    li.add_argument("--tag")

    h = sub.add_parser("kline", parents=[common], aliases=["html"],
                       help="K线卡片：把股票画成 HTML 日K卡片页（默认全部股票，--signals 只看 B1 信号）")
    h.add_argument("--date")
    h.add_argument("--group")
    h.add_argument("--tag")
    scope = h.add_mutually_exclusive_group()
    scope.add_argument("--signals", action="store_true", help="只画有 B1 信号（含观察）的股票")
    scope.add_argument("--all", action="store_true", help="画出全部股票（默认行为，保留以兼容旧命令）")
    h.add_argument("--days", type=int, help="每张卡片的 K 线根数，默认取 config 的 report.cards.days")
    h.add_argument("--out", help="输出路径（默认 data/reports/<日期>/kline_cards.html，--signals 为 b1_cards.html；视图自动加后缀）")
    h.add_argument("--open", action="store_true", help="生成后用默认浏览器打开")

    pr = sub.add_parser("profile", parents=[common], help="个股资料（名称/上市日期）：手动更新，run 从不联网取资料")
    prsub = pr.add_subparsers(dest="pr_cmd", required=True)
    pu = prsub.add_parser("update", parents=[common], help="手动更新资料缓存（不指定则取最近一次 run 的股票池）")
    pu.add_argument("--codes", help="股票代码，逗号分隔")
    pu.add_argument("--file", help="代码文件（CSV/TXT，每行一个代码）")

    ov = sub.add_parser("overview", parents=[common], help="数据概览：对本地已拉取的数据做统计，生成 HTML 页面")
    ov.add_argument("--out", help="输出路径（默认 data/reports/overview.html）")
    ov.add_argument("--open", action="store_true", help="生成后用默认浏览器打开")
    pl = sub.add_parser("pool", parents=[common], help="股票池名单：从数据源重建代码表（如主板全池）")
    pls = pl.add_subparsers(dest="pl_cmd", required=True)
    pb = pls.add_parser("build", parents=[common], help="重新生成代码表文件（不改 config.yaml）")
    pb.add_argument("--board", default="main", choices=["main", "gem", "star", "bj", "all"],
                    help="板块：main 主板（默认）/ gem 创业板 / star 科创板 / bj 北交所 / all 全部 A 股")
    pb.add_argument("--out", help="输出文件（默认 pools/<board>.txt）")
    pb.add_argument("--dry-run", action="store_true", help="只统计不写文件")

    ex = sub.add_parser("export", parents=[common], help="导出数据包：只含股票池 + 这些股票的原始数据（换机器用）")
    ex.add_argument("--out", required=True, help="输出文件，如 ~/xb1-data.tar.gz")
    ex.add_argument("--codes", help="只导出这些代码（逗号分隔）；默认整个股票池")

    im = sub.add_parser("import", parents=[common], help="导入 export 生成的数据包")
    im.add_argument("archive", help="数据包路径")
    im.add_argument("--force", action="store_true", help="覆盖本机已存在的同名数据和 config.yaml")

    sub.add_parser("backfill", parents=[common], help="回填历史信号的 T+3/5/10 收益")

    c = sub.add_parser("config", parents=[common], help="查看/修改阈值")
    c.add_argument("action", nargs="?", choices=["get", "set"], default="get")
    c.add_argument("key", nargs="?")
    c.add_argument("value", nargs="?")

    sub.add_parser("gc", parents=[common], help="清理过期的 runs/logs/raw-universe（history 永不清理）")

    # ---- portfolio：股票档案与标签 ----
    pf = sub.add_parser("portfolio", parents=[common], help="股票档案、标签、定期复核")
    psub = pf.add_subparsers(dest="pf_cmd", required=True)

    p_init = psub.add_parser("init", parents=[common], help="按分组批量建档（板块命中自动打标，见 tags.yaml）")
    p_init.add_argument("--group", required=True, help="分组名（config.yaml universe.groups 的键）")
    p_init.add_argument("--tag", action="append", default=[], help="额外给整批打的标签（可多次）")
    p_init.add_argument("--no-auto-tags", action="store_true", help="不按 tags.yaml 规则自动打标")

    p_list = psub.add_parser("list", parents=[common], help="列出档案")
    p_list.add_argument("--group")
    p_list.add_argument("--tag")
    p_list.add_argument("--due", action="store_true", help="只看已到复核日的")
    p_list.add_argument("--limit", type=int, default=50, help="最多显示条数，0=全部")

    p_show = psub.add_parser("show", parents=[common], help="查看单只档案")
    p_show.add_argument("code")

    p_tag = psub.add_parser("tag", parents=[common], help="打/去标签（标签必须先在 tags.yaml 定义）")
    p_tag.add_argument("tags", nargs="+")
    p_tag.add_argument("--codes", help="股票代码，逗号分隔")
    p_tag.add_argument("--group", help="作用于整个分组的已建档股票")
    p_tag.add_argument("--remove", action="store_true", help="移除而非添加")

    p_upd = psub.add_parser("update", parents=[common], help="更新档案文字字段")
    p_upd.add_argument("code")
    p_upd.add_argument("--name")
    p_upd.add_argument("--description", help="一句话业务描述")
    p_upd.add_argument("--thesis", help="投资逻辑/关注理由")
    p_upd.add_argument("--review-days", type=int, help="该股复核周期（天）")

    p_as = psub.add_parser("assess", parents=[common], help="追加一条评估（同时顺延下次复核日）")
    p_as.add_argument("code")
    p_as.add_argument("note")
    p_as.add_argument("--level", help="对应 B1 等级")
    p_as.add_argument("--score", type=int)
    p_as.add_argument("--date")

    p_note = psub.add_parser("note", parents=[common], help="追加备注")
    p_note.add_argument("code")
    p_note.add_argument("text")
    p_note.add_argument("--date")

    p_ref = psub.add_parser("refresh", parents=[common], help="定期更新：拉最新资料 + 记录最新 B1 状态快照")
    p_ref.add_argument("--group")
    p_ref.add_argument("--codes")
    p_ref.add_argument("--all", action="store_true", help="不论是否到期，全部刷新")

    psub.add_parser("tags", parents=[common], help="列出 tags.yaml 的标签定义")
    p_td = psub.add_parser("tag-define", parents=[common], help="新增标签定义（追加到 tags.yaml，保留注释）")
    p_td.add_argument("name")
    p_td.add_argument("--description", default="")
    p_td.add_argument("--color", default="")
    p_td.add_argument("--category", default="")
    p_td.add_argument("--sector", action="append", default=[], help="自动规则：命中该板块即打此标签（可多次）")
    p_td.add_argument("--min-sector-hits", type=int, help="自动规则：命中分组内≥N个板块即打此标签")

    return p


# --------------------------------------------------------------------------- 子命令
def cmd_run(args, paths, cfg, provider, sleep, today, now=None) -> int:
    date = pipeline.resolve_date(args.date, today)
    st = State(paths)
    log = RunLog(paths.run_log(date))

    _check_view(cfg, args)
    # 幂等：同一交易日已成功 -> 直接返回上次结果，不联网。
    # 盘中（<15:30）产生的结果不算“已完成”：收盘后必须重算，否则 21:00 的任务会拿到盘中数据。
    rec = st.get(date) or {}
    if (not args.force and st.is_success(date) and not rec.get("intraday")
            and paths.brief_md(date).exists() and paths.signals_json(date).exists()):
        run = pipeline.load_run(paths, date)
        if run and run.get("config_hash") != cfgmod.config_hash(cfg):
            print(f"[XB1] 提示：配置自 {date} 运行后已变更，以下为当时结果；如需按新配置重算请加 --force", file=sys.stderr)
        if run and not paths.report_html(date).exists():   # 旧版本/被删：用指标缓存补生成 K线卡片，不联网
            try:
                cards.write_cards(paths, run, cfg)
            except Exception as e:
                log.write(f"cards failed: {type(e).__name__}: {e}")
        log.write("idempotent hit -> 返回已有结果")
        _emit(run, cfg, args.format, None, args.group, args.tag)   # 按当前 XB1_HOME 重新渲染，路径不会是旧的
        return C.EXIT_OK

    log.write(f"RUN date={date} today={today} force={args.force} format={args.format}")
    paths.ensure_dirs()
    try:
        with run_lock(paths):
            prov = provider or pipeline.build_provider(cfg, paths)
            run = pipeline.run_pipeline(paths, cfg, prov, date, today, sleep, log, now=now)
    except NotTradingDay as e:
        log.write(f"NOT_TRADING_DAY {e}")
        print(f"{date} 非交易日，无复盘。")
        return C.EXIT_NOT_TRADING_DAY
    except XB1Error as e:
        log.write(f"FAILED exit={e.exit_code} {e}")
        if e.exit_code not in (C.EXIT_VOLUME_NOT_MOUNTED,):
            try:
                st.mark(date, "failed", e.exit_code, error=str(e)[:300])
            except Exception:
                pass
        raise
    _emit(run, cfg, args.format, None, args.group, args.tag)
    return C.EXIT_OK


def _check_view(cfg: dict, args) -> None:
    g = getattr(args, "group", None)
    if g and g not in (cfg.get("universe", {}).get("groups") or {}):
        raise ConfigError(f"分组 {g!r} 不存在（config.yaml universe.groups 现有：{sorted(cfg.get('universe', {}).get('groups') or {})}）")


def _emit(run: dict, cfg: dict, fmt: str, brief_text: str | None, group=None, tag=None) -> None:
    if group or tag:       # 视图：重新渲染（校验码按视图内结果重算）
        run, brief_text = pipeline.filter_run(run, group, tag), None
    if fmt == "json":
        print(json.dumps(pipeline.compact_json(run, cfg), ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(brief_text if brief_text is not None else render.render_brief(run, cfg))


def _load_for(paths, date_arg):
    d = date_arg or pipeline.latest_date(paths)
    if not d:
        raise XB1Error("尚无任何复盘结果，请先执行 run")
    run = pipeline.load_run(paths, d)
    if not run:
        raise XB1Error(f"没有 {d} 的复盘结果（先执行 run --date {d}）")
    return d, run


def cmd_latest(args, paths, cfg) -> int:
    _check_view(cfg, args)
    d, run = _load_for(paths, None)
    _emit(run, cfg, args.format, None, args.group, args.tag)
    return C.EXIT_OK


def cmd_explain(args, paths, cfg) -> int:
    from xb1lib.models import normalize_code
    try:
        code = normalize_code(args.code)
    except ValueError as e:
        raise ConfigError(str(e))
    d, run = _load_for(paths, args.date)
    for r in run.get("results", []):
        if r.get("code") == code:
            sys.stdout.write(render.render_explain(r))
            return C.EXIT_OK
    fetch = run.get("fetch") or {}
    if code in (fetch.get("suspended") or []):
        raise XB1Error(f"{code} 在 {d} 停牌/未更新，无当日信号")
    for item in fetch.get("failed") or []:
        if item.get("code") == code:
            raise XB1Error(f"{code} 在 {d} 取数失败：{item.get('reason')}")
    raise XB1Error(f"{code} 不在 {d} 的股票池（可能未配置或已被过滤规则剔除）")


def cmd_list(args, paths, cfg) -> int:
    _check_view(cfg, args)
    _, run = _load_for(paths, args.date)
    sys.stdout.write(render.render_list(pipeline.filter_run(run, args.group, args.tag), args.kind, cfg))
    return C.EXIT_OK


def _slug(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff-]+", "-", str(text)).strip("-") or "view"


def cmd_html(args, paths, cfg) -> int:
    """K线卡片页：从已有结果生成，不联网、不重算。官方命令是 kline，html 为兼容别名。"""
    import copy as _copy
    import webbrowser
    _check_view(cfg, args)
    signals = bool(getattr(args, "signals", False))
    d, run = _load_for(paths, args.date)
    if args.group or args.tag:
        run = pipeline.filter_run(run, args.group, args.tag)
    days = getattr(args, "days", None)
    if days:
        cfg = _copy.deepcopy(cfg)
        cfg.setdefault("report", {}).setdefault("cards", {})["days"] = int(days)
    if args.out:
        out = Path(args.out).expanduser()
    else:
        stem = "b1_cards" if signals else "kline_cards"
        out = paths.report_dir(d) / f"{stem}.html"
        if run.get("view"):
            out = out.with_name(f"{stem}-{_slug(run['view'])}.html")
    cards.write_html(out, run, cfg, paths=paths, include_all=not signals)
    out = out.resolve()
    print(f"K线卡片：{out}")
    if args.open:
        webbrowser.open(out.as_uri())
    return C.EXIT_OK


def cmd_profile(args, paths, cfg, provider, sleep) -> int:
    """手动刷新资料缓存。run 不调用本命令，资料只在用户明确要求时更新。"""
    from xb1lib import universe as U
    from xb1lib.models import normalize_code
    codes: list[str] = []
    if args.codes:
        codes += [c.strip() for c in args.codes.split(",") if c.strip()]
    if args.file:
        codes += U.read_code_file(paths, args.file)
    if not codes:
        d = pipeline.latest_date(paths)
        if not d:
            raise XB1Error("没有可更新的股票：请用 --codes / --file 指定，或先执行 run")
        codes = list((pipeline.load_run(paths, d) or {}).get("members", {}).keys())
    if not codes:
        raise ConfigError("没有可更新的股票代码")
    try:
        uniq = sorted({normalize_code(c) for c in codes})
    except ValueError as e:
        raise ConfigError(str(e))
    prov = provider or pipeline.build_provider(cfg, paths)
    paths.ensure_dirs()
    res = U.refresh_profiles(paths, cfg, prov, uniq, shanghai_today(), sleep)
    print(f"资料更新：{res['updated']} / {len(uniq)} 只")
    for batch, reason in res["failed"]:
        print(f"[提示] 批次获取失败 {batch}：{reason}", file=sys.stderr)
    return C.EXIT_OTHER if res["failed"] else C.EXIT_OK


def cmd_overview(args, paths, cfg) -> int:
    """数据概览页：只读本地数据，不联网、不改数据。"""
    import webbrowser
    from xb1lib import overview
    paths.ensure_dirs()
    out = overview.write_overview(paths, cfg, Path(args.out).expanduser() if args.out else None).resolve()
    print(f"数据概览：{out}")
    if args.open:
        webbrowser.open(out.as_uri())
    return C.EXIT_OK


def cmd_pool(args, paths, cfg, provider) -> int:
    """重建股票池代码表。只写名单文件，不动 config.yaml。"""
    from xb1lib import pool as mod
    prov = provider or pipeline.build_provider(cfg, paths)
    try:
        codes, total = mod.build_codes(prov, args.board)
    except NotImplementedError as e:
        raise XB1Error(str(e))
    label = mod.BOARDS[args.board][0]
    out = Path(args.out).expanduser() if args.out else paths.home / "pools" / f"{args.board}.txt"
    old = mod.read_pool_file(out)
    added, removed = mod.diff_codes(old, codes)
    print(f"{label}：数据源返回 {total} 只，命中 {len(codes)} 只")
    if old:
        print(f"与现有名单相比：新增 {len(added)} 只，移除 {len(removed)} 只")
        if added:
            print("  新增：" + " ".join(added[:20]) + (" …" if len(added) > 20 else ""))
        if removed:
            print("  移除：" + " ".join(removed[:20]) + (" …" if len(removed) > 20 else ""))
    if args.dry_run:
        print("（--dry-run：未写文件）")
        return C.EXIT_OK
    if not codes:
        raise XB1Error(f"{label}名单为空，未写入（数据源可能异常）")
    out.parent.mkdir(parents=True, exist_ok=True)
    mod.write_pool_file(out, codes, args.board, getattr(prov, "name", "?"))
    print(f"已写入：{out.resolve()}")
    return C.EXIT_OK


def cmd_export(args, paths, cfg) -> int:
    from xb1lib import transfer
    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else None
    m = transfer.export_bundle(paths, cfg, Path(args.out).expanduser(), codes=codes)
    mb = m["bytes"] / 1024 / 1024
    print(f"已导出：{Path(args.out).expanduser().resolve()}（{mb:.1f} MB）")
    print(f"股票 {m['codes']} 只｜K 线 {m['kline_files']} 个｜资料 {m['profile_files']} 个"
          + (f"｜缺 K 线 {m['missing_kline']} 只" if m["missing_kline"] else ""))
    if m.get("config_uses_files"):
        print("[提示] config.yaml 引用了外部代码文件，换机器后该绝对路径可能失效；"
              "包内已附 data/raw/pool/pool.txt（扁平代码表），可直接改为引用它")
    if not m["config_included"]:
        print("[提示] 本机没有 config.yaml，包里不含股票池配置")
    return C.EXIT_OK


def cmd_import(args, paths, cfg) -> int:
    from xb1lib import transfer
    r = transfer.import_bundle(paths, Path(args.archive).expanduser(), force=args.force)
    print(f"已导入 {r['copied_files']} 个文件到 {paths.home}")
    print(f"配置文件：{r['config']}")
    print("下一步：确认 config.yaml 的股票池后执行 run（结果不随包迁移，需要重算）")
    return C.EXIT_OK


def cmd_backfill(args, paths, cfg) -> int:
    paths.ensure_dirs()
    sys.stdout.write(history.format_summary(history.backfill(paths, cfg)))
    return C.EXIT_OK


def cmd_config(args, paths, cfg) -> int:
    import yaml
    if args.action == "set":
        if not args.key or args.value is None:
            raise ConfigError("用法：config set <key> <value>，如 config set thresholds.j_trigger 15")
        v = cfgmod.set_value(paths, args.key, args.value)
        print(f"{args.key} = {v!r}")
        return C.EXIT_OK
    if args.key:
        v = cfgmod.get_path(cfg, args.key)
        print(yaml.safe_dump(v, allow_unicode=True, sort_keys=False).rstrip() if isinstance(v, (dict, list)) else f"{args.key} = {v!r}")
    else:
        print(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False).rstrip())
    return C.EXIT_OK


def cmd_gc(args, paths, cfg, today) -> int:
    sys.stdout.write(maintenance.gc(paths, cfg, today))
    return C.EXIT_OK


def _codes_arg(port, raw: str) -> list[str]:
    from xb1lib.models import normalize_code
    if raw.strip().lower() == "all":
        return sorted(port.all().keys())
    try:
        return [normalize_code(c) for c in raw.split(",") if c.strip()]
    except ValueError as e:
        raise ConfigError(str(e))


def _one_code(raw: str) -> str:
    from xb1lib.models import normalize_code
    try:
        return normalize_code(raw)
    except ValueError as e:
        raise ConfigError(str(e))


def _port(paths, cfg, today):
    from xb1lib.portfolio import Portfolio
    days = int((cfg.get("portfolio") or {}).get("review_every_days", 30))
    return Portfolio(paths, review_days=days, today=today)


def _fmt_tags(rec) -> str:
    return "/".join(rec.get("tags") or []) or "-"


def cmd_portfolio(args, paths, cfg, provider, sleep, today) -> int:
    import yaml
    from xb1lib import portfolio as pf
    port, reg, sub = _port(paths, cfg, today), pf.TagRegistry(paths), args.pf_cmd

    # ---- 标签定义 ----
    if sub == "tags":
        tags = reg.tags()
        if not tags:
            print(f"{paths.tags_file} 不存在或为空。用 portfolio tag-define <名> 新增，或直接编辑该文件。")
            return C.EXIT_OK
        cats: dict[str, list] = {}
        for t in tags:
            cats.setdefault(t.get("category") or "其他", []).append(t)
        for cat, ts in cats.items():
            print(f"── {cat} ──")
            for t in ts:
                rule = ""
                if t.get("sectors"):
                    rule += f" [自动:命中板块 {'/'.join(t['sectors'])}]"
                if t.get("min_sector_hits"):
                    rule += f" [自动:命中≥{t['min_sector_hits']}个板块]"
                print(f"  {t['name']}：{t.get('description', '')}{rule}")
        print(f"\n定义文件：{paths.tags_file}")
        return C.EXIT_OK

    if sub == "tag-define":
        added = reg.define(args.name, args.description, args.color, args.category,
                           sectors=args.sector, min_sector_hits=args.min_sector_hits)
        print(f"已添加标签定义：{args.name}" if added else f"标签 {args.name} 已存在，未改动")
        return C.EXIT_OK

    # ---- 查询 ----
    if sub == "list":
        recs = port.find(args.group, args.tag)
        if args.due:
            due = {r["code"] for r in port.due()}
            recs = {c: r for c, r in recs.items() if c in due}
        if not recs:
            print("（无匹配档案）")
            return C.EXIT_OK
        rows = sorted(recs.values(), key=lambda r: r["code"])
        shown = rows if not args.limit else rows[:args.limit]
        print(f"共 {len(rows)} 档" + (f"（显示前 {len(shown)}）" if len(shown) < len(rows) else ""))
        print("| 代码 | 名称 | 分组 | 标签 | 下次复核 |\n|---|---|---|---|---|")
        for r in shown:
            print(f"| {r['code']} | {r.get('name') or '-'} | {'/'.join(r.get('groups') or []) or '-'} | "
                  f"{_fmt_tags(r)} | {r.get('next_review') or '未设置'} |")
        if len(shown) < len(rows):
            print(f"另有 {len(rows) - len(shown)} 只，加 --limit 0 查看全部")
        return C.EXIT_OK

    if sub == "show":
        code = _one_code(args.code)
        if not port.exists(code):
            raise XB1Error(f"{code} 还没有档案（portfolio init --group <组> 或 portfolio update {code} 创建）")
        rec = port.get(code)
        for k in ("assessments", "notes"):
            if len(rec[k]) > 5:
                rec[k] = rec[k][-5:]
                rec[f"({k} 仅显示最近5条)"] = True
        sys.stdout.write(yaml.safe_dump(rec, allow_unicode=True, sort_keys=False, width=120))
        return C.EXIT_OK

    # ---- 维护 ----
    if sub == "tag":
        if bool(args.codes) == bool(args.group):
            raise ConfigError("tag 需要且只需要 --codes 或 --group 之一")
        if not args.remove:
            bad = reg.unknown(args.tags)
            if bad:
                raise ConfigError(f"标签未在 tags.yaml 定义：{bad}。先 portfolio tag-define，或编辑 {paths.tags_file}")
        codes = (_codes_arg(port, args.codes) if args.codes else sorted(port.find(args.group)))
        if not codes:
            raise XB1Error("没有匹配的股票")
        for code in codes:
            rec = port.remove_tags(code, args.tags) if args.remove else port.add_tags(code, args.tags)
        print(f"已{'移除' if args.remove else '添加'}标签 {args.tags}：{len(codes)} 只"
              + (f"（{codes[0]} 现为 {_fmt_tags(rec)}）" if len(codes) == 1 else ""))
        return C.EXIT_OK

    if sub == "update":
        code = _one_code(args.code)
        rec = port.update_fields(code, name=args.name, description=args.description,
                                 thesis=args.thesis, review_every_days=args.review_days)
        print(f"已更新 {code} {rec.get('name', '')}；下次复核 {rec.get('next_review')}")
        return C.EXIT_OK

    if sub == "assess":
        rec = port.add_assessment(_one_code(args.code), args.note, b1_level=args.level, score=args.score, date=args.date)
        print(f"已记录评估 {rec['code']}（共 {len(rec['assessments'])} 条）；下次复核 {rec['next_review']}")
        return C.EXIT_OK

    if sub == "note":
        rec = port.add_note(_one_code(args.code), args.text, date=args.date)
        print(f"已追加备注 {rec['code']}（共 {len(rec['notes'])} 条）")
        return C.EXIT_OK

    # ---- init：分组 -> 档案 ----
    if sub == "init":
        from xb1lib.universe import build_universe
        bad = reg.unknown(args.tag)
        if bad:
            raise ConfigError(f"标签未在 tags.yaml 定义：{bad}")
        prov = provider or pipeline.build_provider(cfg, paths)
        paths.ensure_dirs()
        metas, excluded = build_universe(paths, cfg, prov, today, sleep=sleep, today=today, group=args.group)
        warnings = list(excluded.pop("_warnings", []))
        members, w2 = pf.group_members(paths, cfg, prov, args.group, today, sleep)
        warnings += w2
        defs = [] if args.no_auto_tags else reg.tags()
        created = merged = 0
        tag_count: dict[str, int] = {}
        for m in metas:
            info = members.get(m.code, {"sectors": [], "manual": False})
            auto = pf.auto_tags(defs, info["sectors"])
            src = {"板块命中": info["sectors"], "命中数": len(info["sectors"])}
            if info.get("manual"):
                src["手工指定"] = True
            rec, is_new = port.upsert(m.code, name=m.name, board=m.board, groups=[args.group],
                                      tags=auto + list(args.tag), sources=src)
            created += is_new
            merged += (not is_new)
            for t in rec["tags"]:
                tag_count[t] = tag_count.get(t, 0) + 1
        print(f"分组「{args.group}」：入池 {len(metas)} 只（过滤 {len(excluded)} 只）→ 新建档案 {created}，合并更新 {merged}")
        if tag_count:
            print("标签分布：" + "、".join(f"{t} {n}" for t, n in sorted(tag_count.items(), key=lambda x: -x[1])))
        by_reason: dict[str, int] = {}
        for v in excluded.values():
            k = str(v).split(":")[0]
            by_reason[k] = by_reason.get(k, 0) + 1
        if by_reason:
            print("过滤原因：" + "、".join(f"{k} {n}" for k, n in by_reason.items()))
        pool = {m.code for m in metas}
        stale = sorted(c for c in port.find(group=args.group) if c not in pool)
        if stale:
            print(f"[提示] {len(stale)} 只档案属于该分组但本次不在池中（被过滤或已移出板块），档案已保留未删除：{'、'.join(stale[:8])}{'…' if len(stale) > 8 else ''}")
        for w in warnings[:5]:
            print(f"[提示] {w}")
        if len(warnings) > 5:
            print(f"[提示] 另有 {len(warnings) - 5} 条提示，见 {paths.run_log(today)}")
        print(f"档案目录：{paths.portfolio_dir}")
        return C.EXIT_OK

    # ---- refresh：定期更新 ----
    if sub == "refresh":
        from xb1lib.fetcher import call_with_rate_limit_retry
        from xb1lib.provider_base import MAX_PROFILE_BATCH
        from xb1lib.render import LEVEL_LABELS
        if args.codes:
            targets = _codes_arg(port, args.codes)
        else:
            pool = port.find(args.group)
            due = {r["code"] for r in port.due()}
            targets = sorted(c for c in pool if args.all or c in due)
        if not targets:
            print("没有需要刷新的档案（都未到复核日；加 --all 强制全部刷新）")
            return C.EXIT_OK
        prov = provider or pipeline.build_provider(cfg, paths)
        interval = float((cfg.get("fetch") or {}).get("batch_interval", 1.5))
        profiles: dict[str, dict] = {}
        failed_batches = 0
        for i in range(0, len(targets), MAX_PROFILE_BATCH):
            if i:
                sleep(interval)
            batch = targets[i:i + MAX_PROFILE_BATCH]
            try:
                profiles.update(call_with_rate_limit_retry(lambda: prov.get_profiles(batch), cfg, sleep))
            except XB1Error as e:
                failed_batches += 1
                print(f"[提示] 资料批次获取失败（{len(batch)} 只）：{e}", file=sys.stderr)
        run_date = pipeline.latest_date(paths)
        results = {}
        if run_date:
            run = pipeline.load_run(paths, run_date) or {}
            results = {r["code"]: r for r in run.get("results", [])}
        n_prof = n_snap = 0
        for code in targets:
            p = profiles.get(code)
            snap = None
            r = results.get(code)
            if r and r.get("data_ok", True):
                trend = "多头" if r.get("trend") == "bull" else "破位"
                snap = {"date": run_date, "b1_level": r.get("level"), "score": r.get("score"),
                        "note": f"自动快照：{LEVEL_LABELS.get(r.get('level'), r.get('level'))}，得分 {r.get('score')}，"
                                f"J={r.get('j'):.1f}，趋势{trend}，收盘 {r.get('close'):.2f}" if r.get("j") is not None else
                                f"自动快照：{LEVEL_LABELS.get(r.get('level'), r.get('level'))}，得分 {r.get('score')}"}
            _, added = port.record_refresh(code, profile=p, snapshot=snap)
            n_prof += bool(p)
            n_snap += added
        print(f"已刷新 {len(targets)} 档：资料更新 {n_prof}，新增状态快照 {n_snap}"
              + (f"（快照取自 {run_date} 复盘）" if run_date else "（尚无复盘结果，仅更新资料）")
              + (f"；{failed_batches} 批资料获取失败" if failed_batches else ""))
        return C.EXIT_OK

    raise ConfigError(f"未知 portfolio 子命令：{sub}")


def cmd_doctor(args, paths, cfg, provider) -> int:
    lines, worst = [], C.EXIT_OK

    def row(ok, text, code=C.EXIT_OTHER):
        nonlocal worst
        lines.append(("✓ " if ok else "✗ ") + text)
        if not ok and worst == C.EXIT_OK:
            worst = code

    row(True, f"根目录 {paths.home}（可写）")
    row(sys.version_info >= (3, 10), f"Python {sys.version.split()[0]}（需 ≥3.10）", C.EXIT_OTHER)
    missing = []
    for mod in ("pandas", "yaml", "openpyxl", "pyarrow"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    row(not missing, "依赖完整" if not missing else f"缺少依赖：{', '.join(missing)}（pip install -r requirements.txt）", C.EXIT_OTHER)
    u = cfg.get("universe") or {}
    n_src = len(u.get("stocks") or []) + len(u.get("sectors") or []) + len(u.get("files") or [])
    groups = u.get("groups") or {}
    n_group_src = sum(len(g.get("stocks") or []) + len(g.get("sectors") or []) + len(g.get("files") or [])
                      for g in groups.values() if isinstance(g, dict))
    row(n_src > 0 or n_group_src > 0,
        f"配置有效，股票池来源 {n_src} 项" + (f" + {len(groups)} 个分组（{n_group_src} 项）" if groups else "")
        + f"（config.yaml {'存在' if paths.config.exists() else '不存在，使用模板默认值'}）",
        C.EXIT_CONFIG_ERROR)
    try:
        prov = provider or pipeline.build_provider(cfg, paths)
        row(True, f"数据源 {getattr(prov, 'name', '?')} 版本 {prov.version()}")
        try:
            cal = TradeCalendar(paths, prov, today=shanghai_today())
            t = shanghai_today()
            row(True, f"交易日历可用；今天 {t} {'是' if cal.is_trading_day(t) else '不是'}交易日")
        except XB1Error as e:
            row(False, f"交易日历获取失败：{e}", C.EXIT_SOURCE_UNAVAILABLE)
    except XB1Error as e:
        row(False, f"数据源不可用：{e}（运行 scripts/setup.sh 检查安装与授权）", C.EXIT_SOURCE_UNAVAILABLE)
    last = State(paths).last_success_date()
    row(True, f"最近成功复盘：{last or '无'}")
    sys.stdout.write("\n".join(lines) + "\n")
    return worst


# --------------------------------------------------------------------------- 入口
def main(argv=None, *, provider=None, sleep=time.sleep, today=None, now=None) -> int:
    """today/now 仅供测试注入。调用方显式给了 today 而没给 now -> 视为已收盘（不做盘中判断），保证测试不依赖真实时钟。"""
    args = build_parser().parse_args(argv)
    try:
        paths = Paths(getattr(args, "home", None))
        cfg = cfgmod.load_config(paths)
        if now is None and today is None:
            now = shanghai_now()
        today = today or shanghai_today()
        if args.cmd == "run":
            return cmd_run(args, paths, cfg, provider, sleep, today, now)
        if args.cmd == "latest":
            return cmd_latest(args, paths, cfg)
        if args.cmd == "explain":
            return cmd_explain(args, paths, cfg)
        if args.cmd == "list":
            return cmd_list(args, paths, cfg)
        if args.cmd in ("kline", "html"):
            return cmd_html(args, paths, cfg)
        if args.cmd == "profile":
            return cmd_profile(args, paths, cfg, provider, sleep)
        if args.cmd == "overview":
            return cmd_overview(args, paths, cfg)
        if args.cmd == "pool":
            return cmd_pool(args, paths, cfg, provider)
        if args.cmd == "export":
            return cmd_export(args, paths, cfg)
        if args.cmd == "import":
            return cmd_import(args, paths, cfg)
        if args.cmd == "backfill":
            return cmd_backfill(args, paths, cfg)
        if args.cmd == "config":
            return cmd_config(args, paths, cfg)
        if args.cmd == "gc":
            return cmd_gc(args, paths, cfg, today)
        if args.cmd == "doctor":
            return cmd_doctor(args, paths, cfg, provider)
        if args.cmd == "portfolio":
            return cmd_portfolio(args, paths, cfg, provider, sleep, today)
        raise ConfigError(f"未知子命令：{args.cmd}")
    except XB1Error as e:
        print(f"[XB1 退出码 {e.exit_code}] {e}", file=sys.stderr)
        return e.exit_code
    except Exception as e:  # 兜底：绝不吐 traceback 给 Agent，也不编造结果
        print(f"[XB1 退出码 {C.EXIT_OTHER}] 未预期错误：{type(e).__name__}: {e}", file=sys.stderr)
        return C.EXIT_OTHER


if __name__ == "__main__":
    sys.exit(main())
