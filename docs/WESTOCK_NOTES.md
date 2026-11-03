# westock 安装与实测经验

> 实测日期：2026-11-03　　westock 版本：0.0.9（通道 `wbgeneral`）　　平台：macOS arm64
> 用途：给 XB1 的数据层（`provider_westock.py`）和 `setup.sh`、`doctor` 提供依据。
> 本文只记录**实际验证过**的事实；未验证的内容单独标注为“未验证”。

---

## 1. westock 是什么

腾讯自选股提供的命令行工具（Go 单文件二进制），提供 A 股/港美股行情、K 线、技术指标、板块成分股、交易日历等。输出为 **Markdown 表格文本**，不是 JSON。

## 2. 安装

### 2.1 本次的安装方式
使用 WorkBuddy 自带 skill（`~/.workbuddy/skills/westockdata/`）里的脚本：

```bash
bash ~/.workbuddy/skills/westockdata/scripts/setup.sh
```

### 2.2 安装脚本做了什么（已读源码确认）
1. 从官方地址 `https://stockbuddy.gtimg.com/release/wbgeneral/cli` 下载对应系统/架构的二进制。
2. 校验链：脚本内置公钥（ed25519）→ 校验 `SHA256.txt` 签名 → 校验清单哈希 → 校验二进制 SHA256。校验失败即终止。
3. 调用二进制自带的 `install-self` 安装到 `~/.westock/channels/wbgeneral/bin/westock`，并在 `~/.local/bin/westock` 建符号链接。
4. 脚本内固定了版本（包内是 v0.0.7），但实际装上的是 **0.0.9**。原因：脚本会按 `WESTOCK_VERSION` → 包内固定版本 → `latest.txt` 的顺序取版本，而这次落地版本高于包内固定版本；具体触发路径**未深究**。结论：**不要假设装到的版本等于脚本里写的版本，以 `westock version --json` 为准。**

### 2.3 安装脚本可用的环境变量

| 变量 | 作用 |
|---|---|
| `WESTOCK_VERSION` | 指定版本 |
| `WESTOCK_INSTALL_DIR` | 符号链接目录，默认 `~/.local/bin` |
| `WESTOCK_HOME` | 根目录，默认 `~/.westock` |
| `WESTOCK_FORCE=1` | 强制重装 |
| `WESTOCK_BASE` | 自定义下载基址（只放宽来源，SHA256 校验仍强制） |
| `WESTOCK_ALLOW_UNOFFICIAL_BASE=1` | 允许非官方基址 |

`bash setup.sh --dry-run` 可只预演不改动。

### 2.4 装完后的文件布局

```
~/.local/bin/westock  ->  ~/.westock/channels/wbgeneral/bin/westock   (符号链接)
~/.westock/bindir                                                       (记录安装目录)
~/Library/Application Support/westock/                                  (授权凭证，内容未读取)
```

### 2.5 验证安装
```bash
westock version --json
# {"version":"0.0.9","channel":"wbgeneral","tier":20,"auth":true,"rate_limit_source":"remote","goos":"darwin","goarch":"arm64"}
```
关键字段：`auth`（是否已授权）、`rate_limit_source: remote`（**限流参数由腾讯远端下发，可能随时变化**）。

### 2.6 未验证项
- 脱离 WorkBuddy 的 skill 目录，从零安装（例如在另一台没有 WorkBuddy 的机器上）是否可行。安装脚本的下载地址是官方域名、逻辑自包含，**理论上可以**，但没有实际跑过。M0 阶段要验证。
- Linux / Windows 的行为（脚本对 Windows 要求用 `setup.ps1`）。

---

## 3. 授权（需要人工一步）

westock 需要“专属 APIKey”，授权要在浏览器里点一次确认。

### 3.1 流程
1. 首次调用返回未授权 → 运行 `westock auth fetch --wait 300`，它会打印一个授权链接：
   `https://gu.qq.com/resource/westockCliAuth/index.html?code=<一次性码>&channel=wbgeneral`
2. 用**系统浏览器**打开链接并确认。
3. 授权完成后再执行一次 `westock auth fetch --wait 60`，凭证写入本机并缓存。
4. 以后调用无需再授权，**定时任务可直接用**。

### 3.2 踩过的坑
| 现象 | 原因 | 处理 |
|---|---|---|
| `auth fetch --wait 300` 超时 | 授权码有效期约 5 分钟，我等待期间用户没来得及点 | 重新生成授权码，让用户点完**再**执行 `fetch` |
| 链接过期 | 同上 | 重新 `auth fetch` 取新链接 |

### 3.3 状态检查
```bash
westock auth status      # 已授权输出：READY
```
`auth` 子命令：`status / fetch / set / clear`。`set` 是手动设置 APIKey 的兜底，`clear` 清除后需重新授权。

### 3.4 授权与 WorkBuddy 的关系（已实测）
用 `env -i HOME=$HOME PATH=/usr/bin:/bin:$HOME/.local/bin westock kline ...`（清空所有环境变量，没有任何 WorkBuddy 变量）仍然调用成功。**运行时不依赖 WorkBuddy 进程**，脚本、launchd、其他 Agent 都能直接调。只有“安装渠道（wbgeneral）”和授权页面带 WorkBuddy 标识。

---

## 4. 命令速查（已实测的）

| 目的 | 命令 | 备注 |
|---|---|---|
| 单只日 K | `westock kline sz300124 --period day --limit 250 --fq qfq` | 250 日约 0.5 秒 |
| 批量日 K | `westock kline sz300124,sh601100,... --period day --limit 250 --fq qfq` | **一次最多 20 只** |
| 按日期区间 | `--start YYYY-MM-DD --end YYYY-MM-DD` | |
| 交易日历 | `westock trade-calendar --start 2026-10-26 --end 2026-11-06` | 官方接口 |
| 搜板块 | `westock search 人形机器人 --type sector` | 返回板块代码，如 `pt02GN2238` |
| 板块成分股 | `westock sector constituent pt02GN2238` | “人形机器人”468 只 |
| 个股资料 | `westock profile sz300124` | 含上市日期（`listedDate`） |
| 实时行情 | `westock quote sh600519` | 无 ST/停牌字段 |
| 技术指标 | `westock technical sz300124 --start D1 --end D2 --limit 30` | 含 MA/MACD/KDJ/RSI/BOLL，可作第二口径 |

代码格式：`sh`/`sz`/`bj` + 6 位数字（如 `sz300124`）。

---

## 5. 输出格式

### 5.1 单只
```
| date | open | last | high | low | volume | amount | exchange | change_pct |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-11-03 | 55 | 56.32 | 56.88 | 54.8 | 154218 | 863281634 | 0.64 | 2.1029731689630236 |
```

### 5.2 批量（多一列 `code`，且前面有批次汇总行）
```
[Batch] 状态: success | 总数: 20 | 成功: 20 | 失败: 0

| code | date | open | last | high | low | volume | amount | exchange | change_pct |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sz300403 | 2026-11-02 | 8.26 | 8.34 | ...
```
部分失败时，文本里会出现 `[Batch] 状态: error | 总数: 20 | 成功: 0 | 失败: 20` 和：
```
失败列表:
  sz300952,sh605189,...: [LOCAL_RATE_LIMITED] 请求过于频繁，请稍后再试

数据为空
```

### 5.3 字段含义（对照 XB1 标准 K 线列）

| westock | XB1 标准列 | 说明 |
|---|---|---|
| `date` | `date` | 批量时倒序，需自行升序排序 |
| `open` / `high` / `low` | 同名 | |
| `last` | `close` | **收盘价叫 `last`** |
| `volume` | `volume` | |
| `amount` | `amount` | 样例中 863281634 对应约 8.6 亿元，**单位为元**；但部分样本值（如 135600000）末位有补零现象，疑似做过精度截断，**不要用它做精确对账** |
| `exchange` | `turnover` | 换手率，百分数 |
| `change_pct` | `pct_chg` | 涨跌幅，百分数 |

### 5.4 解析注意事项
- 一律按 `| 列 | 列 |` 表格解析，跳过 `| --- |` 分隔行。
- 数值都是字符串，需 `astype(float)`。
- 批量的顺序是**按代码分组、日期倒序**；单只也是倒序（最新在最上或最下需以日期排序为准，不要依赖行顺序）。
- **解析失败必须抛错，不得返回空 DataFrame**，否则会把“限流导致的空数据”误当成“没有数据”。
- 没有 JSON 输出开关（`kline --help` 里未发现），只能解析 Markdown。

---

## 6. 限流（最关键的发现）

### 6.1 现象
24 批（468 只，每批 20 只）**无间隔连续**调用：
- **17 批成功，7 批失败**（6 批 × 20 只 + 1 批 × 8 只 = **128 只失败，340 只成功**）。
- 失败是**整批失败**，不是批内部分失败。
- 错误码：`[LOCAL_RATE_LIMITED] 请求过于频繁，请稍后再试`。
- 总共只拿到 84,194 行（理论 468×250≈11.7 万）。

> 更正：此前在对话和 PRD v1.1/v1.2 中写的“340 只失败”是笔误，把成功数当成了失败数，正确是**失败 128 只**。

### 6.2 后续测试（限流不是固定阈值）
| 测试 | 条件 | 结果 |
|---|---|---|
| 间隔 1 秒 | 6 批 | 限流 0 次 |
| 间隔 2 秒 | 6 批 | 限流 0 次 |
| 同一批 20 只连发 8 次（10:47，静置 20 秒后） | 无间隔 | 8/8 成功 |
| 24 批中取 12 个不同批次连发（10:48，静置 15 秒后） | 无间隔 | 12/12 成功 |

**结论：** 01:3x 那次无间隔 24 批被限流，10:48 这次无间隔 12 批却全部通过。所以限流**不是固定的“每秒 N 次”**，更像是**滑动窗口 / 累计配额**（01:3x 那次之前我已连续做过多轮压测，窗口内累计请求更多），且由远端下发（`rate_limit_source: remote`）。
- 不能写死阈值，也不能认为“间隔 1 秒就永远安全”。
- 触发过一次限流后，静置 20~30 秒恢复。
- 单次调用本身约 3 秒，相当于天然带了 ~3 秒间隔；真正危险的是**短时间内累计大量请求**（如压测、重复重跑）。
- 因此 XB1 的策略是：**保守间隔 + 命中就退避重试 + 缓存减少请求量**，而不是追求精确阈值。

### 6.3 其他限制
- **批量上限 20 只/次**。50 只一批实测无有效返回，30 只未得出明确结论（只有 20 只验证为稳定）。**固定用 20。**
- 20 只 × 250 日 K 线单次约 3 秒。
- `LOCAL_RATE_LIMITED` 的“LOCAL”表示**由客户端本地限流器拦截**，不一定是服务端拒绝；而 `version` 里 `rate_limit_source: remote` 表示限流规则由远端下发。所以限流阈值**可能随时调整**，不能写死。

### 6.4 XB1 的应对规则（已写入 PRD 3.1）
1. 批大小 20，批间隔默认 **1.5 秒**，均可配置。
2. 遇 `LOCAL_RATE_LIMITED`：等待 **30 秒**后**只重试该批**，最多 3 次。
3. 批内成功/失败要逐只记录，写入 `data/runs/{日期}/fetch_errors.json`。
4. 增量缓存：每只股票只补缺失日期，日常运行主要是拉 1 个点。
5. 估算：468 只冷启动 ≈ 24 批 ×（3s + 1.5s）≈ 2 分钟；考虑重试按 8 分钟上限。
6. 未验证：**带间隔的全量 24 批完整跑一遍**还没做（只测了 6 批和 12 批的片段）。M0 后补做，并记录总耗时。
7. 要避免的行为：开发调试时反复 `--force` 全量重跑，会在短时间内耗光配额，导致生产的 21:00 任务前后被限流。调试用小股票池。

---

## 7. 复权验证

用 sh600519（茅台，近年有分红除权）比较 250 日前的价格：

| 参数 | 价格 | 结论 |
|---|---|---|
| `--fq qfq`（前复权） | 1406.72 | |
| `--fq nofq`（不复权） | 1458.70 | |
| `--fq hfq`（后复权） | 9720.00 | 三者互不相同，复权真实生效 |
| `--fq bfq` | **报服务错误** | 不复权要写 `nofq`，不是 `bfq` |
| 不写 `--fq` | 未确认默认值 | **一律显式写 `--fq qfq`** |

---

## 8. 指标口径验证

用 PRD 公式自算 KDJ(9,3,3)（通达信 SMA 口径，K/D 初值 50，`ewm(alpha=1/3, adjust=False)`），与 `westock technical` 的 `KDJ_J` 对比：

- 样本：sz300124、sh601100、sh688017、sz002979、sh600519，各 30 个交易日。
- 结果：**最大 |J 差| = 0.000**。
- 说明：口径一致，且 westock 的 `technical` 可作为 XB1 每日自检的“第二口径”（抽 3 只比对，误差 >0.1 告警）。
- 未验证：白线 `EMA(EMA(C,10),10)`、黄线（MA14/28/57/114 均值）、BBI westock 没有对应字段，**无法用它对账**，需要以通达信人工抽查为准（PRD 的 AC1）。

---

## 9. PRD 需要自算/自判的项目（westock 没有现成字段）

| 需求 | westock | XB1 做法 |
|---|---|---|
| ST 标记 | 无字段；`search "*ST"` 返回的是港美股 | 按名称前缀：`ST` / `*ST` / `退`（名称取自 `profile`/`quote`，**需确认名称字段，M0 验证**） |
| 停牌 | 无字段 | 最后一根 K 线日期 ≠ 当日交易日 → 视为停牌或数据未更新 |
| 次新股 | `profile` 有 `listedDate` | 计算上市交易日数，<120 剔除 |
| 振幅 | 无 | `(high-low)/昨收*100` |
| 白线/黄线/BBI | 无 | 自算 |
| 北交所 | 代码前缀 `bj`（468 只中 18 只） | 按前缀剔除 |
| 板块归属（主板/创业板/科创板） | 无字段 | 按代码段：`300/301`=创业板，`688/689`=科创板，其余沪深主板 |

---

## 10. 实用提醒

1. **调用方式：** Python 里 `subprocess.run([westock_bin, ...], capture_output=True, text=True, timeout=...)`，必须设超时。
2. **westock 路径：** 用绝对路径。优先读 `WESTOCK_BIN` 环境变量，其次 `~/.local/bin/westock`。**launchd 的 PATH 很短，不会包含 `~/.local/bin`。**
3. **不能只看退出码：** 压测脚本用 `|| fail++` 统计命令失败批次，而实际有 7 批 `状态: error`，说明限流整批失败时**退出码很可能仍是 0**（这是由脚本结果反推的，我没有单独对一次失败调用直接读取 `$?`，M0 补验）。无论如何，**必须解析 `[Batch] 状态:` 行和“失败列表”**。
4. **版本固定：** 当前 0.0.9，很新，接口可能变。`doctor` 要打印版本；`runs/{日期}/meta.json` 记录 westock 版本。
5. **盘中最后一根 K 线是未收盘的实时数据（已实测）：** 2026-11-03 10:40 与 10:47 两次取 `sz300124` 的当日 K 线，日期都是 `2026-11-03`，但成交量从 154218 变为 160526、成交额 863281634 变为 898779674，说明**交易时段内当日这一行会实时变化**。因此 PRD F2 里“最后一根 K 线日期等于今日交易日”**只能证明有当日数据，不能证明已收盘**。
   - 21:00 运行时此问题不存在（已收盘）；但**手动在盘中运行**会把未收盘数据当成收盘数据算信号。
   - 对策：`run` 在交易日 15:30 之前运行，要么拒绝并提示，要么在摘要里标注“盘中数据，非收盘结论”。需要写进 PRD。
   - 另：此前我写过“凌晨 01:18 也返回了当日 K 线”，这一点我没有保存当时的输出，**不作为结论**。
6. **WorkBuddy 的 skill 副本：** `~/.workbuddy/skills/westockdata/references/commands.md` 有完整命令文档，需要更多命令时去那里查。

---

## 11. `doctor` 应覆盖的检查（对应上述经验）

| 检查项 | 判定 | 失败提示 |
|---|---|---|
| westock 存在 | `WESTOCK_BIN` 或 `~/.local/bin/westock` 可执行 | 运行 `scripts/setup.sh` |
| 版本 | `westock version --json` 可解析 | 记录版本号 |
| 已授权 | `westock auth status` 输出 `READY` | 运行 `westock auth fetch`，在浏览器打开链接确认后再次 fetch |
| 联通 | 拉 1 只股票 1 条 K 线成功 | 检查网络 |
| 限流探测 | 可选：连发 2 批看是否被限 | 调大批间隔 |
| 自检指标 | 抽 3 只，自算 J 与 `technical` 差 ≤0.1 | 告警 |

区分三种失败：**没装**、**没授权**、**授权过期**，各给不同指引。

---

## 12. 待办（M0 阶段验证）

- [ ] 在无 WorkBuddy 环境（或用 `WESTOCK_HOME` 指向新目录）完整重装一遍，确认 `setup.sh` 可独立使用。
- [ ] 全量 468 只在 1.5 秒间隔下跑完，确认零整批失败。
- [ ] 确认 ST 名称取自哪个字段（`profile` 还是 `quote`）。
- [ ] 21:00 复测：收盘后当日 K 线是否稳定不再变化；盘中防误跑的判定逻辑（见第 10 节第 5 点）。
- [ ] 明确 `amount` 的精度和单位。
- [ ] 验证 30 只/批是否可用（目前只确认 20 稳定）。
