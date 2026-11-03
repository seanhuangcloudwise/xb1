---
name: XB1
description: A 股 B1 选股复盘（Z 哥 B1 战法：KDJ 的 J 值超卖 + 白线/黄线趋势 + 量能）。用户提到 B1、选股复盘、白线黄线、J 值超卖、"跑一下今天的 B1"、某只股票是否符合 B1 时使用。所有数字与信号只能来自 xb1.py 的输出。
---

# XB1 —— B1 选股 Skill

根目录 `/Volumes/work/Workspace/XB1/`（下称 `$XB1`）。你是**触发器 + 展示层**：计算、判断、评分全部由 `python3 $XB1/scripts/xb1.py` 完成。

## 命令

| 场景 | 命令 |
|---|---|
| 跑今天的复盘（幂等，同日重复调用秒回） | `python3 $XB1/scripts/xb1.py run` |
| 看最近一次结果，不联网 | `python3 $XB1/scripts/xb1.py latest` |
| **K线卡片**（HTML 日K卡片页，默认全部股票） | `python3 $XB1/scripts/xb1.py kline [--date YYYY-MM-DD] [--group G] [--tag T] [--days N] [--open]` |
| K线卡片：只看当天有 B1 信号的股票 | `python3 $XB1/scripts/xb1.py kline --signals` |
| 只看某分组 / 某标签（视图，不重算） | `latest --group 机器人板块` / `latest --tag 核心标的`（`run`、`list` 同样支持） |
| 股票档案：列表/详情 | `portfolio list [--group G] [--tag T] [--due]` / `portfolio show <代码>` |
| 打标签（须先在 `tags.yaml` 定义） | `portfolio tag <标签...> --codes 300124,688017`（或 `--group G`；`--remove` 去标签） |
| 写描述/逻辑/评估/备注 | `portfolio update <代码> --description … --thesis …` / `assess <代码> "结论"` / `note <代码> "备注"` |
| 定期复核到期的档案 | `portfolio list --due` → `portfolio refresh`（更新资料 + 记录最新 B1 状态快照） |
| 手动更新个股资料（名称/上市日期，ST 与次新过滤用） | `profile update [--codes 600519,000001] [--file 代码文件]`（不指定则取最近一次 run 的股票池；**run 从不联网取资料**，仅在用户明确要求时执行） |
| 重建股票池名单（新股/退市后） | `pool build [--board main\|gem\|star\|bj\|all] [--dry-run]`，默认写 `pools/<board>.txt` |
| 迁移到另一台机器（只带股票池 + 原始数据） | `export --out ~/xb1-data.tar.gz` → 新机器 `import ~/xb1-data.tar.gz` |
| 数据概览页（本地已拉取数据的统计，只读） | `python3 $XB1/scripts/xb1.py overview [--open]`，产物 `$XB1/data/reports/overview.html` |
| 新分组首次建档 | 先在 `config.yaml` 的 `universe.groups` 加分组，再 `portfolio init --group <组>` |
| 回放某天 | `python3 $XB1/scripts/xb1.py run --date YYYY-MM-DD` |
| 某只股票为什么入选/落选 | `python3 $XB1/scripts/xb1.py explain <6位代码>` |
| 某类完整名单 | `python3 $XB1/scripts/xb1.py list <confirm\|perfect\|standard\|watch\|warn>` |
| 强制重算 | `run --force` |
| 查看/调阈值 | `config get [key]` / `config set thresholds.j_trigger 15` |
| 环境自检 | `doctor` |
| 回填 T+3/5/10 收益统计 | `backfill` |

> 命名：**K线卡片** = `kline` 子命令（HTML 日K卡片页）。`html` 是旧的兼容别名，行为相同。

## 硬规则（必须遵守）

1. **只通过 `xb1.py` 取得任何数字。** 禁止自己计算指标、禁止联网搜索行情、禁止凭记忆补全、禁止读取 `$XB1/data/raw/` 里的 K 线。
2. **原样转述**摘要中的表格与数字。可以在末尾加一句评论，但不得改动、增补、推测任何数据，不得重排或"润色"表格里的数值。
3. 回答必须带上摘要首行的**数据日期**和**校验码**。
4. 结尾保留免责声明（摘要末行已有，不要删）。
5. 用户追问单只股票 → 调 `explain`；追问某类全部名单 → 调 `list`。不要凭印象回答。
6. 不确定该用哪个命令时，先 `latest`，别编造。

## 分组与标签

- 分组在 `config.yaml` 的 `universe.groups`（每组可含 `sectors/stocks/files`，成分股随板块更新）。信号对**全部股票池只算一次**，`--group/--tag` 只是看的角度，摘要首行会标明视图，校验码按视图重算。
- 标签在 `$XB1/tags.yaml` 维护（人工编辑，或 `portfolio tag-define`）。标签可带自动规则：`sectors`（命中某板块即打）、`min_sector_hits`（命中≥N 个板块即打）。**打未定义的标签会被拒绝（退出码 40）。**
- 档案在 `$XB1/data/portfolio/<代码>.yaml`，含描述、逻辑、评估历史、备注、复核日。**这是人工资产，和 `$XB1/data/history/` 一样需要备份。** 人工写的字段不会被 `init/refresh` 覆盖；`refresh` 只更新 `profile`（机器维护）并追加 `auto: true` 的状态快照。
- 评估/描述/标签是用户的判断，**你只能按用户给的原话写入**，不要自行"补全"或"总结"后写入档案。

## 盘中数据

交易日 15:30 前运行 `run`，摘要第 2 行会出现"⚠️ 盘中数据"——此时最后一根 K 线未收盘，**必须原样保留该警告并告知用户这不是收盘结论**。盘中结果不进历史流水、不进幂等缓存，收盘后再跑 `run` 会重新计算。定时任务设在 21:00 不受影响。

## 退出码（据此决定行为，不要靠解析文字）

| 码 | 含义 | 你该做 |
|---|---|---|
| 0 | 成功 | 原样呈现 stdout |
| 10 | 非交易日 | 回答"今天非交易日，无复盘"，**不是错误**；定时任务应静默 |
| 20 | 数据源不可用（westock 未装/未授权/网络/限流到放弃） | 如实告知；引导运行 `$XB1/scripts/setup.sh`；**禁止编造结果**；可用 `latest` 给出上一次的结果并**明确标注其日期** |
| 30 | 当日数据未更新 | 如实告知"数据尚未更新"，稍后重试；禁止编造 |
| 40 | 配置错误 | 把 stderr 原文告诉用户 |
| 50 | 工作卷未挂载/不可写 | 告知用户检查 `/Volumes/work` 是否已挂载 |
| 1 | 其他错误 | 把 stderr 原文告诉用户（含"已有运行进行中"） |

错误信息在 stderr，格式 `[XB1 退出码 N] 说明`。

## 摘要怎么读（仅供理解，不要自行重算）

- 等级：**B1确认**（近 3 日出现过标准以上 B1 且今日放量/大阳站上白线）> **完美B1**（≥75 分且 J<-5 且缩量）> **标准B1**（50–74）> **观察**（过趋势闸门且 13<J≤20）。
- 预警：**牛绳断**（白线刚下穿黄线）、**连续破BBI**（连续 2 日收盘<BBI）。
- "数据异常"：失败/停牌/未更新的股票数，这些股票不参与当日信号。
- 超出展示条数的类别会写"另有 X 只，见 list 命令"，此时用 `list` 获取全部。
- B1 规则细则见 `$XB1/references/rules.md`，输出字段见 `$XB1/references/output-schema.md`——**仅在用户追问规则本身时才读**。

## 产物位置（绝对路径可直接给用户）

- 报告：`$XB1/data/reports/<日期>/b1_report.xlsx`、`b1_report.md`（`$XB1/data/reports/latest` 指向最新）
- K线卡片（全部股票）：`$XB1/data/reports/<日期>/kline_cards[-分组名].html`（`kline` 生成，含无信号的股票）
- K线卡片（B1 信号）：`$XB1/data/reports/<日期>/b1_cards[-分组名].html`（`run` 自动生成；`kline --signals` 可重生成）
- 完整信号：`$XB1/data/runs/<日期>/signals.json`（**你不需要读它**）
- 配置：`$XB1/config.yaml`（股票池、阈值）

## 定时（由 Agent 侧实现）

周一至周五 21:00（Asia/Shanghai）执行 `run`，原样转发 stdout；退出码 10 静默，20/30/50 发告警。Skill 本身不含调度与推送。
