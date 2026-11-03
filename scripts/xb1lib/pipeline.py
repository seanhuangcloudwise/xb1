"""run 流水线：股票池 → 取数 → 指标 → 信号 → 落盘。

提交顺序（保证原子性，AC13）：报告(md/xlsx) → runs/{日期}/* → history → meta → state=success。
每个文件都是原子写；state 的 success 记录最后写，是唯一的“已完成”判据。
"""
from __future__ import annotations

import datetime as _dt
import time
from collections import Counter
from typing import Callable, Optional

from . import atomic
from . import cards
from . import constants as C
from . import history, render, report
from .calendar import TradeCalendar, shanghai_today
from .config import config_hash
from .errors import ConfigError, DataStale, NotTradingDay, SourceUnavailable
from .fetcher import fetch_klines
from .indicators import compute_and_cache, compute_indicators
from .paths import Paths
from .signals import evaluate
from .state import RunLog, State, run_lock
from .universe import build_universe

_WEEKDAYS = "一二三四五六日"


def weekday_cn(date: str) -> str:
    return "周" + _WEEKDAYS[_dt.date.fromisoformat(date).weekday()]


def build_provider(cfg: dict, paths: Paths):
    name = str((cfg.get("fetch") or {}).get("provider", "westock")).lower()
    if name == "westock":
        from .provider_westock import WestockProvider
        return WestockProvider(cfg, paths)
    raise ConfigError(f"未实现的数据源：{name}（v1 仅支持 westock）")


def resolve_date(date_arg: Optional[str], today: str) -> str:
    if not date_arg:
        return today
    try:
        d = _dt.date.fromisoformat(date_arg).isoformat()
    except ValueError:
        raise ConfigError(f"--date 格式应为 YYYY-MM-DD：{date_arg}")
    if d > today:
        raise ConfigError(f"--date {d} 晚于今天 {today}，无法复盘未来")
    return d


def load_run(paths: Paths, date: str) -> Optional[dict]:
    return atomic.read_json(paths.signals_json(date), default=None)


def latest_date(paths: Paths) -> Optional[str]:
    """最近一次成功的数据日期：优先 state，其次扫描 runs/ 目录。"""
    d = State(paths).last_success_date()
    if d and paths.signals_json(d).exists():
        return d
    if paths.runs.exists():
        for p in sorted((x for x in paths.runs.iterdir() if x.is_dir()), reverse=True):
            if (p / "signals.json").exists() and (p / "brief.md").exists():
                return p.name
    return None


def _fetch_with_stale_retry(provider, paths, cfg, codes, date, today, sleep, log: RunLog, persist_today: bool = True):
    f = cfg.get("fetch", {})
    limit = float(f.get("stale_ratio_limit", 0.2))
    wait = float(f.get("stale_retry_wait", 600))
    max_retries = int(f.get("stale_max_retries", 3))
    attempt = 0
    while True:
        frames, failures = fetch_klines(provider, paths, cfg, codes, date, sleep, today=today,
                                        persist_today=persist_today)
        susp = [c for c, r in failures.items() if r.startswith(C.FETCH_SUSPENDED)]
        ratio = len(susp) / max(1, len(codes))
        log.write(f"fetch attempt={attempt} ok={len(frames)} failures={len(failures)} not_updated={len(susp)} ratio={ratio:.1%}")
        if ratio <= limit:
            return frames, failures
        # 未更新占比超限：只有“当日数据”才值得等（历史回放等了也不会变）
        if date != today or attempt >= max_retries:
            raise DataStale(f"{date} 数据未更新：{len(susp)}/{len(codes)} 只（{ratio:.0%}）最后一根 K 线不是 {date}"
                            f"，已重试 {attempt} 次")
        attempt += 1
        log.write(f"数据未更新占比 {ratio:.0%} > {limit:.0%}，等待 {wait:g}s 后重试（{attempt}/{max_retries}）")
        sleep(wait)


def intraday_label(cfg: dict, date: str, today: str, now) -> Optional[str]:
    """当日且早于收盘截止时间（默认 15:30）返回 'HH:MM'，否则 None。now 为 None（调用方注入了 today，如测试/回放）视为已收盘。
    westock 盘中最后一根 K 线是实时未收盘数据（WESTOCK_NOTES §10.5），不能当收盘结论。"""
    if now is None or date != today:
        return None
    hh, mm = str((cfg.get("run") or {}).get("close_time", "15:30")).split(":")
    if (now.hour, now.minute) < (int(hh), int(mm)):
        return now.strftime("%H:%M")
    return None


def filter_run(run: dict, group: Optional[str] = None, tag: Optional[str] = None) -> dict:
    """按分组/标签取“视图”：结果、数据异常、股票池大小都只含匹配的股票，校验码按视图内结果重算，header 带视图标记。
    不改写落盘的完整 run——分组只是看的角度，信号计算只做一次。"""
    if not group and not tag:
        return run
    results = run.get("results") or []
    members = run.get("members") or {r["code"]: r for r in results}

    def ok(info: dict) -> bool:
        return ((not group or group in (info.get("groups") or []))
                and (not tag or tag in (info.get("tags") or [])))

    keep = {c for c, info in members.items() if ok(info)}
    v = dict(run)
    v["results"] = [r for r in results if r["code"] in keep]
    v["universe_size"] = len(keep)
    f = run.get("fetch") or {}
    v["fetch"] = {
        "failed": [x for x in f.get("failed") or [] if (x["code"] if isinstance(x, dict) else x) in keep],
        "suspended": [c for c in f.get("suspended") or [] if c in keep],
        "stale": [c for c in f.get("stale") or [] if c in keep],
    }
    v["checksum"] = render.compute_checksum(v["results"])
    v["view"] = " ".join(x for x in ((f"分组 {group}" if group else ""), (f"标签 {tag}" if tag else "")) if x)
    return v


def run_pipeline(paths: Paths, cfg: dict, provider, date: str, today: str, sleep: Callable[[float], None],
                 log: RunLog, calendar: Optional[TradeCalendar] = None, now=None) -> dict:
    t0 = time.monotonic()
    replay = date < today
    intraday = intraday_label(cfg, date, today, now)
    cal = calendar or TradeCalendar(paths, provider, today=today)
    if not cal.is_trading_day(date):
        raise NotTradingDay(f"{date} 非交易日")

    metas, excluded = build_universe(paths, cfg, provider, date, sleep=sleep, today=today, calendar=cal)
    warnings = list(excluded.pop("_warnings", []))
    log.write(f"universe size={len(metas)} excluded={len(excluded)} warnings={len(warnings)}")
    if not metas:
        raise ConfigError("股票池为空：请在 config.yaml 的 universe.stocks/sectors/files 中配置股票")

    codes = [m.code for m in metas]
    frames, failures = _fetch_with_stale_retry(provider, paths, cfg, codes, date, today, sleep, log,
                                               persist_today=not intraday)

    results, eval_failed = [], {}
    chart_frames = {}
    for meta in metas:
        df = frames.get(meta.code)
        if df is None:
            continue
        try:
            ind = compute_indicators(df) if replay else compute_and_cache(paths, meta.code, df)
            r = evaluate(ind, meta, cfg)
            r["groups"], r["tags"] = list(meta.groups), list(meta.tags)
            results.append(r)
            if r.get("level") in cards.CARD_LEVELS:   # K线卡片只需要信号股的 K 线
                chart_frames[meta.code] = ind
        except Exception as e:  # 单只异常不影响整体
            eval_failed[meta.code] = f"{C.FETCH_FAILED}: eval: {type(e).__name__}: {e}"
    results.sort(key=lambda r: r["code"])

    failures = {**failures, **eval_failed}
    suspended = sorted(c for c, r in failures.items() if r.startswith(C.FETCH_SUSPENDED))
    failed = [{"code": c, "reason": r} for c, r in sorted(failures.items()) if not r.startswith(C.FETCH_SUSPENDED)]
    log.write(f"results={len(results)} failed={len(failed)} suspended={len(suspended)}")
    for item in failed:
        log.write(f"FAILED {item['code']} {item['reason']}")

    run = {
        "date": date,
        "weekday": weekday_cn(date),
        "universe_size": len(metas),
        "engine_version": C.ENGINE_VERSION,
        "config_hash": config_hash(cfg),
        "results": results,
        "fetch": {"failed": failed, "suspended": suspended, "stale": []},
        "members": {m.code: {"groups": list(m.groups), "tags": list(m.tags)} for m in metas},
        "intraday": intraday,
        "report_xlsx_path": str(paths.report_xlsx(date)),
        "report_md_path": str(paths.report_md(date)),
        "report_html_path": str(paths.report_html(date)),
    }
    run["checksum"] = render.compute_checksum(results)
    brief = render.render_brief(run, cfg)

    # ---- 提交 ----
    report.write_reports(paths, run, cfg)
    try:
        cards.write_cards(paths, run, cfg, frames=chart_frames)
    except Exception as e:  # K线卡片失败不影响信号/报告落盘
        log.write(f"cards failed: {type(e).__name__}: {e}")
    atomic.write_json(paths.signals_json(date), run)
    atomic.write_json(paths.fetch_errors_json(date), {
        "date": date, "failed": failed, "suspended": suspended,
        "excluded": {k: v for k, v in excluded.items()}, "warnings": warnings,
    })
    atomic.write_text(paths.brief_md(date), brief)
    # 盘中结果不进历史流水：history 是回测底座，只收收盘结论（收盘后重跑会正式写入同日记录）
    n_hist = 0 if intraday else history.append_history(paths, run)

    try:
        wv = provider.version()
    except Exception:
        wv = "unknown"
    elapsed = round(time.monotonic() - t0, 2)
    atomic.write_json(paths.meta_json(date), {
        "date": date, "engine_version": C.ENGINE_VERSION, "config_hash": run["config_hash"],
        "universe_size": len(metas), "results": len(results), "checksum": run["checksum"],
        "elapsed_sec": elapsed, "exit_code": C.EXIT_OK, "provider": getattr(provider, "name", "?"),
        "provider_version": wv, "replay": replay, "history_rows": n_hist, "intraday": intraday,
        "excluded_summary": dict(Counter(str(v).split(":")[0] for v in excluded.values())),
    })
    State(paths).mark(date, "success", C.EXIT_OK, checksum=run["checksum"], config_hash=run["config_hash"],
                      elapsed_sec=elapsed, intraday=intraday)
    log.write(f"DONE checksum={run['checksum']} elapsed={elapsed}s exit=0")
    return run


def compact_json(run: dict, cfg: dict) -> dict:
    """--format json：精简结构化摘要（不含 none 级别与完整指标），供程序化消费。"""
    top_n = int((cfg.get("report") or {}).get("top_n", 10))
    results = run.get("results") or []
    sig = {}
    for lv in (C.LEVEL_CONFIRM, C.LEVEL_PERFECT, C.LEVEL_STANDARD, C.LEVEL_WATCH):
        rows = render.by_level(results, lv)
        sig[lv] = {
            "count": len(rows),
            "items": [{k: r.get(k) for k in ("code", "name", "score", "close", "j", "stop_price", "stop_pct", "b1_date")}
                      for r in rows[:top_n]],
        }
    return {
        "date": run.get("date"), "weekday": run.get("weekday"), "universe_size": run.get("universe_size"),
        "engine_version": run.get("engine_version"), "checksum": run.get("checksum"),
        "config_hash": run.get("config_hash"), "signals": sig,
        "warnings": {w: [r["code"] for r in render.warn_rows(results, w)] for w in (C.WARN_BULL_ROPE_BREAK, C.WARN_BELOW_BBI_2D)},
        "fetch": run.get("fetch"), "report_xlsx_path": run.get("report_xlsx_path"),
        "report_md_path": run.get("report_md_path"),
        "report_html_path": run.get("report_html_path"),
    }
