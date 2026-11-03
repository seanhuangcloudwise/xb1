# 输出契约（仅在需要程序化消费时读取）

## `run` / `latest` 的 stdout（`--format brief`，默认）

预渲染 Markdown，Agent 原样转述。首行固定：

```
【B1 复盘】数据日期 YYYY-MM-DD（周X）｜股票池 N｜引擎 vX.Y.Z｜校验 xxxxxx
```

校验码 = 全部结果规范化 JSON 的 sha1 前 6 位；同日期、同配置、同缓存数据结果逐位一致。

## `--format json`（精简结构化摘要）

```json
{ "date": "...", "weekday": "...", "universe_size": 0, "engine_version": "...", "checksum": "...",
  "config_hash": "...",
  "signals": { "confirm|perfect|standard|watch": { "count": 0, "items": [
      {"code","name","score","close","j","stop_price","stop_pct","b1_date"} ] } },
  "warnings": { "rope_break": ["代码"], "below_bbi_2d": ["代码"] },
  "fetch": { "failed": [{"code","reason"}], "suspended": ["代码"], "stale": [] },
  "report_xlsx_path": "...", "report_md_path": "..." }
```

`items` 最多 `report.top_n` 条，`count` 为该类全部数量。

## 落盘文件

| 文件 | 内容 |
|---|---|
| `data/runs/<日期>/signals.json` | 全部股票的完整指标、闸门、评分、命中/扣分（`results[]` 每项字段见 `docs/DEV_CONTRACTS.md`「signals.py」） |
| `data/runs/<日期>/brief.md` | brief 原文 |
| `data/runs/<日期>/meta.json` | 数据日期、引擎版本、配置哈希、股票池大小、校验码、耗时、退出码、数据源版本、是否回放 |
| `data/runs/<日期>/fetch_errors.json` | 取数失败/停牌明细、被过滤股票及原因、告警 |
| `data/history/signals.parquet` | 每日信号流水（回测底座，**需备份**） |
| `data/history/outcomes.parquet` | `backfill` 产生的 T+3/5/10 收益 |
| `data/portfolio/<代码>.yaml` | 股票档案：name/board/groups/tags、description/thesis（人工）、profile（机器维护）、assessments[]、notes[]、next_review（**需备份**） |
| `tags.yaml`（项目根） | 标签定义：name/description/color/category，可选自动规则 sectors、min_sector_hits |
| `data/state/state.json` | 每个日期的运行状态与最近成功日期（幂等依据） |

## 退出码

0 成功｜10 非交易日｜20 数据源不可用｜30 数据未更新｜40 配置错误｜50 工作卷未挂载/不可写｜1 其他。
错误信息在 stderr：`[XB1 退出码 N] 说明`。


## run 的附加字段（signals.json）

`results[].groups / tags`：该股所属分组与档案标签（不参与校验码）；`members`：全体股票池的 `{code: {groups, tags}}`（含取数失败/停牌者，用于分组视图过滤）；`intraday`：盘中运行时为 `"HH:MM"`，否则 `null`。
`--group/--tag` 视图：只在输出时过滤，不改写落盘的完整结果；首行追加 `｜分组 X`/`｜标签 Y`，校验码按视图内结果重算。
