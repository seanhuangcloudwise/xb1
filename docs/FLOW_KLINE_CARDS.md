# K线卡片：实现流程与职责边界

> 结论：**运行时，从取数、判定信号、筛选股票到生成 HTML/SVG，全部由 `xb1.py` 的确定性程序完成；
> 大模型只负责“听懂需求 → 触发命令 → 转述结果”。** 大模型不参与任何数字计算、信号判定和画图。

```mermaid
flowchart TD
    U["用户<br/>「跑一下今天的 B1 / 看 K线卡片」"]:::human
    A["大模型 Agent<br/>① 理解意图 ② 选择命令与参数<br/>③ 原样转述结果"]:::llm

    subgraph P["xb1.py · 确定性程序（所有计算与产物）"]
        direction TB
        P1["加载 config.yaml + 交易日历<br/>幂等判断 / 运行锁 / 退出码"]:::prog
        P2["构建股票池<br/>分组·板块 + ST/停牌/北交所过滤"]:::prog
        P3["取数：调用 westock CLI<br/>前复权日 K 线，增量缓存"]:::prog
        P4["计算指标<br/>白线 / 黄线 / BBI / KDJ / MACD"]:::prog
        P5["B1 判定与评分<br/>G1–G4 闸门 + Q1–Q7 评分<br/>F4 分级 / F5 确认 / F6 止损 / F7 预警"]:::prog
        P6{"筛选出信号股<br/>confirm / perfect / standard<br/>（可选 watch）"}:::prog
        P7["生成既有报告<br/>brief.md / b1_report.md / xlsx"]:::prog
        P8["cards.py 生成 K线卡片<br/>每只股票一张卡片<br/>+ 内联 SVG 日 K 线：<br/>蜡烛 / 成交量 / 白线 / 黄线 / BBI / 止损 / B1 标记"]:::prog
        P9["原子写盘<br/>reports/&lt;日期&gt;/kline_cards.html<br/>signals.json / history / state"]:::prog
        P1 --> P2 --> P3 --> P4 --> P5 --> P6
        P6 --> P7 --> P9
        P6 --> P8 --> P9
    end

    W["westock 行情 CLI<br/>（外部数据源）"]:::ext
    B["浏览器<br/>等级筛选 / 代码搜索 / 悬浮看 K 线"]:::human
    N["开发期：卡片模板、CSS、SVG 画法<br/>由大模型编写成 Python 代码<br/>运行期不再调用大模型"]:::note

    U --> A
    A -->|"python3 scripts/xb1.py run"| P1
    P3 <--> W
    P9 -->|"stdout：摘要 + K线卡片绝对路径"| A
    A -->|"转述摘要 / 返回文件路径<br/>不重算、不改数字"| U
    P9 -->|"静态 HTML（CSS/JS/SVG 全内联）"| B
    A -.->|"可选：kline --group/--tag<br/>按分组/标签重新生成视图"| P8
    N -.-> P8

    classDef human fill:#fef3c7,stroke:#d97706,color:#78350f
    classDef llm fill:#ede9fe,stroke:#7c3aed,color:#4c1d95
    classDef prog fill:#e0f2fe,stroke:#0284c7,color:#0c4a6e
    classDef ext fill:#f1f5f9,stroke:#64748b,color:#334155
    classDef note fill:#f8fafc,stroke:#94a3b8,stroke-dasharray:4 3,color:#475569
```

## 职责划分

| 环节 | 实现方 | 说明 |
|---|---|---|
| 听懂“看 B1 卡片”并决定跑 `run` 还是 `html` | **大模型** | 只做意图理解与命令选择 |
| 选择 `--date / --group / --tag` 等参数 | **大模型** | 用户明确指定；不确定时先 `latest` |
| 交易日历、股票池、取数、缓存 | 程序 | `calendar.py` / `universe.py` / `fetcher.py` |
| 指标计算（白线/黄线/BBI/KDJ） | 程序 | `indicators.py`，纯函数 |
| B1 信号判定、评分、等级、止损、预警 | 程序 | `signals.py`，阈值全部来自 `config.yaml` |
| 决定哪些股票进入卡片页 | 程序 | 默认全部股票；`--signals` 时按 `cards.CARD_LEVELS` + `report.cards.include_watch` 过滤 |
| 计算图表坐标、画蜡烛/均线/成交量/止损线 | 程序 | `cards.py` 直接输出内联 SVG |
| 生成 HTML 页面、落盘、更新软链 | 程序 | `cards.write_cards()` + `atomic` |
| 摘要转述、给用户文件路径、补充一句评论 | **大模型** | 项目硬规则：数字只能原样转述，不能重算或补全 |
| 打开页面、点筛选按钮、悬浮看 K 线 | 用户 / 浏览器 | 纯前端交互，不再调用大模型 |

## 关键边界

- **大模型不接触原始 K 线**，不计算任何指标，不判断 B1，不参与评分。
- **K线卡片是静态文件**：生成后即使断网、关闭大模型，也能直接打开。
- **同一份信号只算一次**：`--group / --tag` 只是生成不同视图，不会重新判定信号。
- **可复现**：页面上的数字来自 `signals.json`，图表来自指标缓存/原始 K 线；同一日期、同一配置重跑结果一致。
