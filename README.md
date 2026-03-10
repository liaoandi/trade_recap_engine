# 交易复盘引擎 Trade Recap Engine

[中文](#中文) | [English](#english)

---

## 中文

基于 AI 的美股交易复盘工具，帮助交易员对抗 FOMO、执行交易纪律、系统性地回顾每日决策。

### 为什么做这个

交易中最大的敌人不是市场，是自己。FOMO 追高、临盘改单、无聊补仓 -- 这些情绪驱动的错误反复出现，但事后复盘时往往一笔带过，下次照犯。

传统的交易日记要么太随意，变成流水账；要么太费时间，坚持不下来。这个工具用 Gemini 把盘中的碎片化记录自动整理成结构化的复盘报告：时间线还原、计划 vs 实际偏差量化、错误根因分析、次日具体挂单策略。重点不是"今天赚了还是亏了"，而是"哪些错误在重复，怎么用规则堵住"。

### 产出示例

以下为一份真实的盘后复盘报告，2026-02-23 交易 AppLovin $APP。

> **交易员身份确认**：危机狩猎者 Crisis Hunter
> **核心教训**：利润来自"恐慌插针"，亏损来自"临盘改单"。
>
> ## 1. 当日关键时间线
>
> *   **盘前回顾**：上周五原计划在 $394.5 挂单买入，但因盯盘情绪干扰，手动改价至 $416.5 追高成交。
> *   **盘中现状**：APP 股价回落至 $380，$416.5 仓位浮亏约 9%。
> *   **策略确立**：确认自己擅长捕捉"极度恐慌的插针"，而非趋势跟随。制定了基于结构位+期权墙的量化挂单策略。
>
> ## 2. 原计划 vs 实际执行偏差
>
> | 项目 | 原计划 | 实际执行 | 偏差后果 |
> | :--- | :--- | :--- | :--- |
> | **上周五买入** | 挂单 **$394.5** | 盯盘后改单至 **$416.5** | 本周一开盘即被套 |
> | **今日补仓** | 需间隔 15% 以上 | 盘中多次想在 $375/$380 动手 | 若执行，成本无法有效摊薄 |
> | **交易模式** | 提前计算，GTC 挂单 | 临盘改单 | 历史数据显示：临盘改单胜率极低 |
>
> ## 3. 关键错误与纠偏
>
> **错误 A：临盘改单** -- 将 $394.5 改为 $416.5，FOMO 战胜理性。纠偏规则：挂单一旦确认，严禁向不利方向修改价格。
>
> **错误 B：无效补仓** -- 试图在 $380 补仓 $416 的亏损单，跌幅仅 8.6%。纠偏规则：15% 硬性间距。
>
> **错误 C：无聊成本** -- 盘尾觉得"今天跌不到"，想提高买价。买不到 = 赚到了。
>
> ## 4. 次日挂单策略
>
> | 订单类型 | 价格 | 触发逻辑 |
> | :--- | :--- | :--- |
> | Buy Limit 1 | $361.50 | 前低 $359.38 + 期权墙 $360 的溢价保护 |
> | Buy Limit 2 | $348.50 | 防备击穿 $360 后的恐慌止损盘 |
> | Sell Limit | $430.00 | 针对 $416.5 错误仓位，反弹出局 |
>
> **禁区**：$365 - $400，严禁任何买入操作。
>
> ## 5. 可执行清单
>
> 1. [ ] 拒绝改单：挂好 $361.50 后绝对禁止上移
> 2. [ ] 补仓间距：下一笔必须比上一笔低 13%-15% 以上
> 3. [ ] 定义"针"：只接 5-15 分钟内跌幅 >3% 且触及关键支撑的急跌
> 4. [ ] 无聊管理：股价不动就关闭行情软件，禁止为了"参与感"强行开仓
> 5. [ ] 心态转换：挂得低没买到是好事，追高买到了是坏事

### 设计迭代

**V1: 半自动模式 -- Gemini Web UI 导出**

最初的工作流是在 Gemini Web UI 里和 AI 对话做复盘，手动导出聊天记录为 Markdown，再用脚本解析生成报告。问题：
- 导出格式不稳定，解析经常出错
- Web UI 不支持图片导出，多模态分析丢失
- 手动导出这个步骤本身就是摩擦力，经常忘记或偷懒跳过

**V2: 全自动模式 -- 终端内交互**

改为直接在终端中和 Gemini API 对话，所有交互实时记录为 JSONL。盘中随时聊、贴图表截图，盘后一键生成报告。摩擦力降到最低。

`semi_auto_scripts/` 保留了 V1 的代码作为备用导入通道。

### 报告结构

每份复盘报告遵循以下标准结构：

| 章节 | 说明 |
| :--- | :--- |
| 关键时间线 | 按时间顺序还原当日决策节点，标注情绪状态与关键价格 |
| 计划 vs 实际执行 | 表格对比原定计划与实际操作的偏差，量化偏差后果 |
| 错误分析 | 逐条拆解关键错误，追溯根因，给出纠偏规则 |
| 次日行动计划 | 具体的挂单策略、禁区定义、量化触发条件 |
| 纪律清单 | 可勾选的执行清单，覆盖改单禁令、仓位管理、情绪管理 |

### 功能

- **交互式终端交易日志**：在终端中直接进行盘前规划和盘中记录
- **多模态支持**：可直接输入图表和截图
- **自动汇总**：将数小时的非结构化盘中记录自动合成为结构化复盘报告
- **半自动导入**：支持导入从 Gemini Web UI 导出的聊天记录
- **隐私优先**：代码和交易数据严格分离

### 快速开始

```bash
pip install -r requirements.txt

# 盘前规划
./trade.sh pre

# 盘中记录
./trade.sh in

# 生成盘后复盘报告
./trade.sh recap
```

API 密钥统一存放在 `~/.config/api-keys.env`，由 shell 自动加载。

### 仓库结构

本仓库仅包含代码/引擎部分，个人交易数据存放在独立的私有仓库 trading_logbook 中。

```
auto_scripts/
├── gemini_chat_session.py    # 终端交互主程序，实时记录为 JSONL
└── session_to_recap.py       # JSONL 日志转结构化复盘报告

semi_auto_scripts/
├── gemini_vertex_recap.py    # V1 遗留：处理 Web UI 导出的 Markdown
└── zip_diff_gemini_pipeline.py

trade.sh                      # 日常使用的主入口
RUNBOOK.md                    # 脚本运行机制的详细说明
```

### 依赖

- Python 3.10+
- Google Vertex AI, Gemini 3 Pro
- google-genai SDK

---

## English

AI-powered post-market recap tool for US stock traders. Helps combat FOMO, enforce trading discipline, and systematically review daily decisions.

### Why This Project

The biggest enemy in trading isn't the market -- it's yourself. FOMO-driven chasing, modifying limit orders mid-session, boredom-induced averaging down -- these emotionally driven mistakes repeat endlessly, but post-hoc journaling often glosses over them.

Traditional trading journals are either too casual and turn into stream-of-consciousness logs, or too time-consuming to maintain consistently. This tool uses Gemini to automatically transform fragmented intraday notes into structured recap reports: timeline reconstruction, plan-vs-actual deviation quantification, error root cause analysis, and concrete next-day order strategies. The focus isn't "did I make money today" but "which mistakes are repeating, and what rules can prevent them."

### Sample Output

Below is a real post-market recap report, from a 2026-02-23 trade on AppLovin $APP.

> **Trader Identity**: Crisis Hunter
> **Core Lesson**: Profits come from "panic spikes," losses come from "modifying orders mid-session."
>
> ## 1. Key Timeline
>
> *   **Pre-market review**: Last Friday's plan was a $394.5 limit buy, but FOMO led to manually changing the price to $416.5.
> *   **Intraday**: APP dropped to $380, leaving the $416.5 position at ~9% unrealized loss.
> *   **Strategy crystallized**: Confirmed strength in catching "extreme panic spikes," not trend following. Defined a quantitative order strategy based on structural levels + option walls.
>
> ## 2. Plan vs Actual Execution
>
> | Item | Plan | Actual | Consequence |
> | :--- | :--- | :--- | :--- |
> | Last Friday buy | Limit $394.5 | Changed to $416.5 | Trapped at Monday open |
> | Today's averaging | 15%+ spacing required | Wanted to act at $375/$380 | Would fail to effectively reduce cost basis |
> | Trading mode | Pre-calculated GTC orders | Modified orders mid-session | Historical data: mid-session modifications have very low win rate |
>
> ## 3. Key Errors & Corrections
>
> **Error A: Mid-session order modification** -- Changed $394.5 to $416.5, FOMO overrode rationality. Rule: once a limit order is set, never modify price in the unfavorable direction.
>
> **Error B: Inefficient averaging** -- Attempted to average down at $380 on a $416 loss, only 8.6% drop. Rule: 15% hard spacing minimum.
>
> **Error C: Boredom cost** -- End of session, felt "it won't drop today," wanted to raise buy price. Not buying = making money.
>
> ## 4. Next-day Order Strategy
>
> | Order Type | Price | Trigger Logic |
> | :--- | :--- | :--- |
> | Buy Limit 1 | $361.50 | Structural defense: prior low $359.38 + $360 option wall |
> | Buy Limit 2 | $348.50 | Crash protection below $360 panic stops |
> | Sell Limit | $430.00 | Exit the $416.5 mistake position on bounce |
>
> **No Trade Zone**: $365 - $400, absolutely no buying.

### Design Iterations

**V1: Semi-auto -- Gemini Web UI export**

The original workflow involved chatting with AI in Gemini's Web UI for post-trade review, manually exporting chat logs as Markdown, then parsing them with scripts to generate reports. Problems:
- Export format was unstable, parsing frequently broke
- Web UI didn't support image export, losing multimodal analysis
- The manual export step itself was friction; it was often forgotten or skipped

**V2: Fully automated -- in-terminal interaction**

Switched to direct Gemini API conversations in the terminal, with all interactions recorded as JSONL in real-time. Chat anytime during sessions, paste chart screenshots, generate reports with one command after close. Friction reduced to near zero.

`semi_auto_scripts/` retains V1 code as a fallback import channel.

### Report Structure

Each recap report follows this standard structure:

| Section | Description |
| :--- | :--- |
| Key Timeline | Chronologically reconstructs decision points, annotating emotional state and key prices |
| Plan vs Actual Execution | Table comparing plan against actual actions, quantifying deviation consequences |
| Error Analysis | Breaks down each critical error, traces root causes, proposes corrective rules |
| Next-day Action Plan | Specific limit order strategies, no-trade zones, and quantitative trigger conditions |
| Discipline Checklist | Actionable checkbox list covering order modification bans, position sizing, and emotional management |

### Features

- **Interactive terminal trading log**: Chat directly in terminal during pre-market and intraday sessions
- **Multimodal support**: Feed charts and screenshots directly into the workflow
- **Auto-summarization**: Synthesizes hours of unstructured notes into structured recap reports
- **Semi-auto import**: Supports importing chat logs exported from Gemini Web UI
- **Privacy first**: Code and trading data strictly separated

### Quick Start

```bash
pip install -r requirements.txt

# Pre-market planning
./trade.sh pre

# Intraday logging
./trade.sh in

# Generate post-market recap
./trade.sh recap
```

API keys are centrally stored in `~/.config/api-keys.env` and loaded automatically by the shell.

### Repository Architecture

This repository contains only the code/engine. Personal trading data is stored in a separate private repository, trading_logbook.

```
auto_scripts/
├── gemini_chat_session.py    # Terminal chat runner, logs to JSONL in real-time
└── session_to_recap.py       # Converts JSONL logs to structured recap reports

semi_auto_scripts/
├── gemini_vertex_recap.py    # V1 legacy: processes Markdown exported from Web UI
└── zip_diff_gemini_pipeline.py

trade.sh                      # Main entrypoint for daily usage
RUNBOOK.md                    # Detailed internal documentation
```

### Dependencies

- Python 3.10+
- Google Vertex AI, Gemini 3 Pro
- google-genai SDK
