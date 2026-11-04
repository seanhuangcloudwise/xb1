# XB1 开发接口约定（并行模块共用）

所有模块位于 `scripts/xb1lib/`，测试位于 `tests/`。Python ≥3.10，依赖仅 pandas/numpy/pyyaml/openpyxl/pyarrow(parquet)/pytest。
业务代码不得出现硬编码路径或 `~`，路径只走 `paths.Paths`；阈值只读 `cfg`（见 `config.example.yaml`）；退出码/等级名只用 `constants`；异常只用 `errors`；写文件只用 `atomic`。

## K 线标准 DataFrame
列固定为 `constants.KLINE_COLUMNS = [date, open, close, high, low, volume, amount, turnover, pct_chg]`；
`date` 为 `YYYY-MM-DD` 字符串，升序、无重复；`amount` 已换算为**元**；`pct_chg` 为百分数（2.5 表示 +2.5%）；`turnover` 为百分数。

## indicators.py（模块 B）
`compute_indicators(df) -> DataFrame`：返回副本，在标准列后追加
`prev_close, white, yellow, bbi, ma60, k, d, j, dif, dea, macd, amplitude`。
- `white=EMA(EMA(C,10),10)`（ewm span=10, adjust=False 两次）；`yellow=(MA14+MA28+MA57+MA114)/4`；`bbi=(MA3+MA6+MA12+MA24)/4`；窗口不足为 NaN。
- KDJ(9,3,3)：RSV=(C-LLV9)/(HHV9-LLV9)*100（分母 0 时 RSV=50 前值处理按通达信：取 50）；`K=ewm(alpha=1/3,adjust=False)`，K/D 初值 50；`J=3K-2D`。前 8 行 RSV 窗口不足时按通达信口径用已有数据的最高/最低（min_periods=1）。
- MACD(12,26,9)：`dif=EMA12-EMA26`，`dea=EMA(dif,9)`，`macd=2*(dif-dea)`，均 adjust=False。
- `amplitude=(high-low)/prev_close*100`；`prev_close=close.shift(1)`；首行 NaN。
- 若输入 `pct_chg` 为 NaN，用 close/prev_close 补算。
- 纯函数，不读写文件。另提供 `compute_and_cache(paths, code, df) -> DataFrame`（写 `data/cache/indicators/{code}.parquet`，按末日期判断是否复用）。

## signals.py（模块 C）
`evaluate(df_ind, meta: StockMeta, cfg, at: int | None = None) -> dict`，`at` 为行号（默认最后一行；用于 `--date` 回放和 F5 回看）。返回可 JSON 序列化的 dict：
```
code, name, board, date, close, pct_chg, amplitude, j, k, d, white, yellow, bbi,
gates: {G1,G2,G3,G4: bool}, gates_pass: bool, trigger: bool,
q: {Q1..Q7: int}, score: int,
base_level: perfect|standard|watch|none      # 当日 B1 等级（F4）
level: confirm|perfect|standard|watch|none   # 最终等级（confirm 优先，F5）
b1_date: str|None                            # confirm 时对应的 B1 日期
hits: [str], deductions: [str],              # 中文短语，供 explain
stop_price, stop_pct, stop_wide: bool,       # F6；仅 level in confirm/perfect/standard 时计算，否则 None
trend: "bull"|"broken",                      # F7：白>黄 且 收盘>=黄 为 bull
warnings: [WARN_* ...],                      # F7：rope_break（今日白线下穿黄线）、below_bbi_2d（连续 2 日收盘<BBI）
data_ok: bool, note: str                     # 数据不足 114 行等情况 data_ok=False，level=none
```
G2：收盘≥黄线，允许连续跌破不超过 2 天（即当日及此前连续收盘<黄线的天数 ≤ g2_max_below_yellow_days，且当日收盘≥黄线或连续跌破天数在允许内——实现按 PRD 4.4 解读并在 docstring 写明）。
F5 confirm：`at-3..at-1` 内任一日 base_level ∈ {perfect, standard}，且当日收阳、(涨幅≥confirm_min_pct 或 量≥前 5 日均量×confirm_vol_mult)、收盘站上白线或 BBI。
另提供 `scan_history(df_ind, meta, cfg, start=None) -> list[dict]`（逐日 evaluate，供 backfill / 单测）。

## provider / calendar / universe / fetcher（模块 D）
`provider_base.DataProvider`(ABC)：
`name`；`version() -> str`；
`get_klines(codes: list[str], days: int, fq: str) -> tuple[dict[str, DataFrame], dict[str, str]]`（一批，≤20 只；整批限流抛 `RateLimited`；单只失败写入第二个 dict {code: reason}）；
`get_profiles(codes) -> dict[str, {"name","listed_date"}]`；
`search_sector(name) -> list[{"code","name"}]`；`sector_constituents(sector_code) -> list[{"code","name"}]`；`trade_calendar() -> list[str]`（全部交易日 YYYY-MM-DD）。
`provider_westock.WestockProvider(cfg, paths)`：用 subprocess 调 westock CLI；所有命令模板集中在文件顶部常量；Markdown 表格统一经 `parse_md_table(text) -> DataFrame`，**解析失败抛 `ParseError`，不返回空**；每次调用写 `westock-{date}.log`（命令、耗时、是否限流，不含凭证）。
`calendar.py`：`TradeCalendar(paths, provider)`，`is_trading_day(date)`、`latest_trading_day(on_or_before)`、`prev_trading_day(date, n)`、`n_trading_days_between(a,b)`；缓存 `trade_calendar.json`，每周刷新。
`universe.py`：`build_universe(paths, cfg, provider, trade_date) -> tuple[list[StockMeta], dict]`（并集去重；板块成分股缓存 7 天；按 cfg.filters 剔除 ST/退市/北交所/上市不足 N 交易日；第二返回值为剔除明细 {code: reason}）。停牌在取数后用 K 线末日期判定（fetcher 负责）。
`fetcher.py`：`fetch_klines(provider, paths, cfg, codes, trade_date, sleep=time.sleep) -> tuple[dict[str,DataFrame], dict[str,str]]`：增量缓存 parquet（只补缺失日期，回放历史 `--date` 时截断到该日）、批 20、批间隔、`RateLimited` 等待 `rate_limit_wait` 后仅重试该批、最多 `max_retries`；末根 K 线日期≠trade_date 记 `suspended`；单只失败逐只记录。`sleep` 可注入，便于测试秒过。
测试用 `tests/fakes.py::FakeProvider`（可编程返回、可注入限流/失败/停牌）——**其他模块的集成测试也会用它**，接口需稳定。

## render.py / report.py / history.py（模块 E）
`run` 汇总字典（由 xb1.py 组装，传入 render/report）：
```
{date, weekday(中文'周二'), universe_size, engine_version, checksum, config_hash,
 results: [signals.evaluate 的 dict ...], fetch: {failed:[{code,reason}], suspended:[code], stale:[code]},
 report_xlsx_path, report_md_path}
```
- `render.compute_checksum(results) -> str`（6 位 hex，对规范化 JSON 取 sha1，稳定可复现）。
- `render.render_brief(run, cfg) -> str`：严格按 PRD 5.3 格式；每类最多 `report.top_n`，超出“另有 X 只，见 list 命令”；表头 `| 代码 | 名称 | 得分 | 收盘 | J | 止损 | 止损幅度 | 命中 |`；数字格式固定（收盘 2 位小数，J 1 位，止损幅度 1 位 %）；包含数据日期、股票池、引擎版本、校验码、结论计数、预警、数据异常、完整报告绝对路径、免责声明。目标 ≤1000 token。
- `render.render_list(run, kind, cfg) -> str`（kind: confirm/perfect/standard/watch/warn，完整表格不截断）；`render.render_explain(result) -> str`（≤300 token：指标值、闸门、触发、Q1–Q7 得分、扣分项、止损）。
- `report.build_markdown(run, cfg) -> str`；`report.write_reports(paths, run, cfg)`：原子写 `b1_report.md` 与 `b1_report.xlsx`（openpyxl：总览 / B1 信号 / 全清单趋势 / 预警 / 数据异常 多 sheet），并更新 `reports/latest` 软链（原子替换，失败不报错）。
- `history.append_history(paths, run)`：向 `data/history/signals.parquet` 追加当日信号流水（日期、代码、名称、等级、得分、J、收盘、止损价…），同日重复运行去重覆盖，原子写；`history.backfill(paths, cfg, calendar_days=(3,5,10)) -> dict`：用 history + raw/kline 计算 T+3/5/10 收益写 `outcomes.parquet`，返回汇总统计（按等级的样本数、胜率、平均收益）。

## cards.py（K线卡片，只读展示）
`build_html(run, cfg, frames=None, paths=None, include_all=False) -> str`；`write_cards(paths, run, cfg, frames=None) -> Path`；`write_html(out, run, cfg, frames=None, paths=None, include_all=False) -> Path`。
- 输入同一个 `run` 字典；`frames` 为 `{code: indicators 的 DataFrame}`，缺省时从 `paths.indicators(code)` 回读并按 `run["date"]` 截断。
- 输出自包含 HTML（内联 CSS/JS/SVG，无外部依赖）：每只信号股一张卡片，主体为近 `report.cards.days` 个交易日蜡烛图（白线/黄线/BBI/成交量/止损/B1 日标注）。
- `report.cards.include_watch: false` 可排除观察级别。K线卡片不参与 checksum 与 history。
- `include_all=True`（CLI `kline` 默认；`b1_cards` 走 `kline --signals` / `run`）：不筛信号，把 `run["results"]` 里全部股票都画成卡片；排序为 确认→完美→标准→观察→无信号，同级保持股票池配置顺序；无信号卡片用灰色「无信号」徽标，第 4 个指标显示趋势而非止损。
- CLI：`xb1.py kline [--date] [--group] [--tag] [--signals] [--days N] [--out] [--open]`；`html` 为兼容别名，`--all` 为默认行为。

## universe.py 资料（profile）规则
- `build_universe` 只读 `data/raw/profile/*.json`，**不联网、不看年龄**；缺失只汇总为一条警告。
- 资料只由 `refresh_profiles`（CLI：`xb1.py profile update`）显式更新，增量拉取 K 线时不会触发。

## 我（集成方）负责
`state.py`（state.json、run.lock、幂等）、`logs`、`xb1.py` 全部子命令、`gc`、SKILL.md、setup.sh、集成测试。

## market.py（0AMV 活跃市值复刻）
`compute_series(paths, cfg, days=250) -> DataFrame[date, amv, c5, c13, c34, inf, amount, turnover, n]`；`load_or_build(...)`（缓存 `cache/market/amv.parquet`）；`region_at(row)`；`summarize(series)`。
- 民间公开公式：`0AMV = SMA(Σ成交额, 10, 1)`；成本均线 `C5/C13/C34/INF = DMA(SMA(0AMV,3,1) 或 0AMV, 全市场换手率/0.02|0.1|0.18|1.1)`。
- 四区域：`0AMV>C5>C13` 持续上涨区（可重仓）／`0AMV>C5 且 C5<C13` 上涨犹豫区（逢低布局）／`0AMV<C5 且 C5>C13` 下跌犹豫区（逢高减仓）／`0AMV<C5<C13` 持续下跌区（空仓）。
- **指南针未公开算法**，本实现用本地股票池 Σ成交额代替其专属活跃筹码口径，形态可比、绝对数值不等价；单位万亿（官方为 0AMV 点，1 点 = 1e7 元）。
- 只统计"数据已更到最新交易日"的股票，避免旧池子遗留文件造成口径跳变。

## pool.py（股票池名单）
`build_codes(provider, board) -> (codes, total)`；`read_pool_file(path)` / `write_pool_file(path, codes, board, source)`；`diff_codes(old, new) -> (added, removed)`。
- `board` 取 `main/gem/star/bj/all`，按 `models.board_of` 判定；数据源用 `DataProvider.list_universe()`（westock 分页拉 `universe --market hs --type stock`）。
- 只写代码表文件（头注释 + 每行一个 6 位代码），不改 `config.yaml`；`--dry-run` 只报告新增/移除。
- 名单会随新股/退市/转板变化，需定期重建；`config.yaml` 用 `universe.groups.<组>.files` 引用它。

## transfer.py（数据迁移）
`export_bundle(paths, cfg, out, codes=None) -> dict`；`import_bundle(paths, archive, force=False) -> dict`；`pool_codes(paths, cfg) -> list[str]`。
- 只打包 `config.yaml` + `data/raw/{kline,profile,calendar,universe}` + `data/raw/pool/pool.txt` + `manifest.json`；**不含** runs/reports/history/state/cache（可重算）。
- 配置含 `universe.sectors` 时拒绝导出（离线无法展开成分股）；含 `files` 时提示绝对路径可能失效。
- 导入会校验包内路径（拒绝 `..` 等越界条目）；本机已有 `config.yaml` 时保留，包的副本另存为 `config.yaml.from-bundle`。

## Skill 注册（OpenCode）
- 正本：`$XB1/.opencode/skills/xb1/SKILL.md`，ID 为 `xb1`（由目录名决定，与 frontmatter 的 `name: XB1` 无关）。
- 根目录 `SKILL.md` 是指向正本的软链，供人读与其他 Agent（WorkBuddy/Claude）读取，避免双份内容漂移。
- 全局可用：`~/.config/opencode/opencode.json` 的 `skills` 数组含 `$XB1/.opencode/skills`；项目内则自动发现。
- 校验：`opencode api get /api/skill` 返回列表里应有 `xb1`。
