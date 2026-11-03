# westock 输出格式假设清单（实机校对用）

沙箱无 westock、不能联网，以下全部是依据 PRD 第 3 章推断的假设。实机校对后，**只需改 `scripts/xb1lib/provider_westock.py` 顶部常量**，并把 `tests/test_provider_westock.py` 里的伪造输出换成真实样本。

校对方法：对每条命令实跑一次，把原始输出存成样本，用 `parse_md_table` / 对应 `parse_*` 跑一遍，看是否抛 `ParseError` 或结果不对。

## A. 命令模板（常量 `CMD_*`，可执行文件名取 `cfg.fetch.command`）
| 常量 | 假设 | 校对点 |
|---|---|---|
| `CMD_KLINE` | `westock kline sh600519,sz300124 --period day --count 250 --fq qfq`，代码带 sh/sz 前缀、逗号分隔 | 参数名/顺序；是否真支持逗号批量；`--count` 是否包含当日 |
| `CMD_SEARCH_SECTOR` | `westock search <名> --type sector` | 参数名 |
| `CMD_SECTOR_CONSTITUENT` | `westock sector constituent <板块代码>` | 板块代码是否带 `pt` 前缀；成分股是否分页（目前假设一次返回全部 468 只） |
| `CMD_TRADE_CALENDAR` | `westock trade-calendar`，无参数 | 是否需要年份/范围参数；是否含未来日期 |
| `CMD_PROFILE` | `westock profile sz300124,sh601100`（**假设支持逗号批量**） | 若只支持单只：需改成逐只循环（并遵守批间隔） |
| `CMD_VERSION` | `westock --version`，取输出第一个非空行原样作为版本串 | 输出样式 |
| `FQ_ARG` | `qfq/hfq/nofq` 直接透传 | 不复权是否叫 `nofq` |
| `CMD_TIMEOUT_SEC` | 60 秒（可用 `cfg.fetch.command_timeout` 覆盖） | 20 只×250 日约 3 秒，60 秒留有余量 |

## B. 通用输出结构
1. 输出为 Markdown 表格（`| a | b |` + `|---|---|` 对齐行），前后可能有说明文字、空行、ANSI 颜色码；可能有多张表。
2. 表头与数据行的 `|` 数一致；允许行首尾省略 `|`；单元格可有 `**粗体**`/反引号（解析时去除）。
3. 缺失值用 `-`、`--`、`—`、`N/A`、空；千分位逗号（含全角）；百分数带 `%`；金额可能带 `万/亿/元`。`万/亿` 会换算为绝对数；`%` 只去掉，不除以 100（`pct_chg`/`turnover` 本就是百分数）。
4. 前导 0 的整数列（如 `000001`）保持字符串，不转数字。
5. 正常结果在 stdout；出错信息在 stdout 或 stderr；**非 0 退出码且输出里没有表格** 视为 `SourceUnavailable`（含授权过期等）。有表格时即使退出码非 0 也尝试解析。

## C. 限流
- 输出（stdout 或 stderr，任意位置）含字符串 `LOCAL_RATE_LIMITED`（常量 `RATE_LIMIT_MARK`）即视为**整批**限流，抛 `RateLimited`，不管退出码、不管有没有表格。
- 假设限流时 `westock` 不会同时返回部分有效数据。（PRD 记载 7 批“整批” `LOCAL_RATE_LIMITED`，故按整批处理。）

## D. K 线
- 列名（`KLINE_COLUMN_MAP`）：`date→date, open→open, last→close, high→high, low→low, volume→volume, amount→amount, exchange→turnover, change_pct→pct_chg`；另有少量中文/英文别名（`KLINE_COLUMN_EXTRA_ALIASES`，纯猜测）。表头比较前会做归一化：小写、去空格/下划线/连字符/括号内容/`%`。
- 必需列：date/open/close/high/low/volume/amount；`turnover`、`pct_chg` 缺失时填 NaN（上层若需要 pct_chg，由 indicators 按 close/prev_close 补算）。必需列缺失 -> 该只记单只失败。
- 日期格式：`2026-01-05`、`2026/1/5`、`20260105`、`2026年1月5日` 均可，统一成 `YYYY-MM-DD`；允许带时间后缀。
- 行序不限（解析后按日期升序、同日期保留后出现者）。
- **成交额单位**：无显式单位的 `amount` 单元格乘 `cfg.fetch.amount_unit`（默认 1，即假设就是“元”）；单元格自带 `万/亿/元` 时用显式单位，不再乘。**这是最需要实机核对的一项**——用茅台等已知成交额样本对一下，若 westock 是“万元”就把 `amount_unit` 改成 10000。
- **成交量单位**：假设“股”或“手”均可（指标只用量的比值）；若需统一换算，改 `VOLUME_FACTOR`。
- 价格已经是前复权值；`change_pct` 已是百分数（2.5 表示 2.5%）；`exchange` 已是百分数。
- 是否包含当日未收盘/未更新的 K 线：假设 21:00 后含当日；未更新时末日期落后（由 fetcher 判为 `suspended`）。

## E. 批量 K 线的多股拆分（最不确定）
实现的宽容策略，按优先级：
1. 表中有**代码列**（`code/symbol/代码/股票代码…`），单元格含 6 位代码（可带 sh/sz/bj 前缀或后缀）-> 按代码列分组；
2. 无代码列时，按**表前最近的标签行**归属：`## sz300124`、`### sh600519 贵州茅台`、`**sz300124**`、或含代码的短行（≤80 字符）；标签行里的代码不在请求列表内则不归属；
3. 只请求 1 只时，未标注的 K 线表归属该只。
- 拆不出来 / 该股票没有 K 线表 / 该股票表里有行无法解析 -> 仅该只记失败（`failed: …`），其他照常。
- 整段输出里一张含 date+收盘列 的表都没有 -> 整批 `ParseError`（fetcher 记该批全部失败）。
- 若实机是“每只股票一张表且标题形态不同”（如 `代码：sz300124 名称：汇川技术`），只要标签行含请求内的 6 位代码即可识别；若标题不含代码（只含名称），需要改 `_segments`。

## F. 板块搜索 / 成分股
- 搜索：含 `代码` 列（`code/板块代码/…`）和 `名称` 列的表；有表头无数据行 = 合法的“无结果”（返回 `[]`）；完全没表 -> `ParseError`。取同名精确匹配的第一条，否则取第一条（`universe._load_sector`）。
- 板块代码形如 `pt02GN2238`；若 `universe.sectors` 里直接给了这种形态就跳过搜索。
- 成分股：含代码列的表（可多张表合并、按代码去重）；代码可带前缀/后缀，统一为 6 位；解析不到任何代码 -> `ParseError`（不返回空列表，避免把“解析失败”当成“空板块”）。假设**一次返回全部成分股**（无分页）。

## G. 交易日历
- 含日期列（`date/日期/交易日/trade_date…`）的表；若另有“是否交易”列（`is_trading/是否交易日/…`），仅取值为 `1/true/是/交易/开市/open` 的行；没有该列时认为表里每个日期都是交易日。
- 兜底：输出不是表格但含裸日期（`2026-01-02` / `20260102`）时，取全部日期。
- 假设返回范围覆盖“今天”及此前多年（上市天数计算需要上市日期起的全部交易日）。若实机只返回当年，`universe` 的“上市不足 N 日”会对老股算少（偏向误剔）——需要改命令参数拉多年。`TradeCalendar` 对“日历起点晚于上市日”不做处理，只会少算天数。

## H. profile
- 支持三种形态：(a) 含代码列的多行表；(b) 单只时的单行宽表；(c) 两列键值表 `字段 | 值`；(d) `listedDate: 2012-03-02` 键值行（可在 `## sz300124` 小标题下分段）。
- 名称列别名：`name/名称/股票名称/证券简称…`；上市日期列：`listedDate/上市日期/上市时间…`（`LISTED_DATE_ALIASES`）。
- 上市日期格式同 K 线日期；解析失败 -> `None`（不抛，universe 里记 warning 且不剔除）。
- 取不到的代码不出现在结果里；一个有效字段都解析不到 -> `ParseError`。
- profile 是否支持批量（见 A）。

## I. 其它实现层假设
- 名称带空格（如 `*ST 某某`）：`models.is_st_name` 会去空格后判断。
- 限流重试语义：`max_retries` = 该批最多尝试次数（含首次）；连续 3 次限流放弃，该批全部记 `rate_limited`。（PRD 字面“最多重试 3 次”可解释为 4 次尝试，这里取更保守的 3 次，改 `fetcher.call_with_rate_limit_retry` 一处即可。）
- 日志：每次 subprocess 调用追加一行到 `westock-{上海当日}.log`（时间、命令、耗时、是否限流、退出码、状态）；命令行参数超过 120 字符会截断。不记录环境变量，不含凭证。
