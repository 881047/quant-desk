# quant-desk · 量化台专区

在这个目录开会话，就是进入了量化台的上下文。这里只有一件事：用一个 ¥5,000 的
A 股模拟账户，把每一次交易判断变成可结算的记录。不接券商，不碰真钱；自动委托只进入模拟账本。

## 实盘（账外）与提醒

- 实盘账本：`data/external.sqlite`，与模拟盘完全分离；`scripts/external.py paste|import|add|report`。
- **桌面面板**：双击桌面 `quant-desk`（或 `panel.bat`）——独立窗口，不是网页。
  「记一笔」录成交：佣金/印花税/过户费自动算，持仓与可用资金同时更新。
  持仓也可以直接在「同步账户」页改。不再读券商窗口——读屏那套换台机器就不可靠，
  而一次读错会把止损锚到错的价格上。
- **补历史流水**：`python scripts/external.py paste|import|add|report`，
  或把券商导出丢进 `data/external/drop`（17:35 自动导入）。
- 监控发信：`scripts/monitor.py --once`（去重）、`--digest`（每天一封）、`--test-mail`（配置自检）。
- 三个计划任务：09:26 盘前、09:30–15:00 每 5 分钟、17:35 收盘。
- **整条链路不产生任何委托**。邮件只描述事实与纪律位，不给买卖建议。
- 详见 [`docs/09-external-account.md`](docs/09-external-account.md)、[`docs/10-alerts.md`](docs/10-alerts.md)。

## 60 秒自检

```bash
curl -s http://127.0.0.1:8811/health          # {"ok":true,...}
.venv/Scripts/python.exe -m pytest tests -o addopts="" -q   # 数字会漂，别信手抄
```

> 用例总数每周都在长（2026-11-02 是 1,938 个、6 个因数据依赖跳过）。
> 想知道确切数字就跑 `pytest tests --co -q | tail -3`——**手抄的数字一定会过期**，
> 而过期数字比没有数字更糟：它让人以为测试覆盖的范围是已知的。
> `tests/test_docs_numbers.py` 盯着文档里的工具数（`README.md`、`plugin/README.md`、
> `plugin/lib/index.js` 与本文件提到的那些）；它不盯用例总数，因为那个数字每次
> 加一条测试就过期，只能跑出来、不能抄下来。

**面板**：桌面快捷方式 `quant-desk` → `scripts/panel.pyw`（pywebview 窗口，不是浏览器）。
窗口里装的是**同一份**交易台前端：`web/` 由服务端挂在 `/desk`，窗口只是把它装进一个独立窗口，
口令由 Python 从 `.env` 注入 fragment、页面消费后立刻从地址栏抹掉。
页签：今日 / 盘面 / 持仓与风险 / 决策 / 复盘 / 内核，外加二级页 `#/s/<代码>`（个股 K 线）
与 `#/external`（实盘账外，走 pywebview 的 Python 桥读写 `data/external.sqlite`）。
**只有一份界面**——改前端改 `web/`，不要再去改 `scripts/panel_ui/`；那份是逃生舱，
`panel.pyw legacy` 才打开，留着是因为实盘录入不能因为一次前端重构而不可用。
机器读的那份在 <http://127.0.0.1:8811/dashboard.json>；17:30 的数据任务把当晚冻结成
`reports/daily/<日期>.dashboard.json`。

服务不在时：`.venv/Scripts/python.exe -m uvicorn service.api.app:app --host 127.0.0.1 --port 8811`
（平时由计划任务 `quant-desk-service` 保活；日更分三段：`quant-desk-daily` 17:30 主链路、
`quant-desk-daily-late` 21:40 补 `moneyflow`／`moneyflow_hsgt`、`quant-desk-daily-morning`
08:50 补 T+1 的两融。三段各按 Tushare 的实际发布时点排，不要合并回一趟）。

## 执行顺序：先新闻，后工具（复盘与市场类任务）

**18:50 市场复盘、19:30 收盘对账这类"解释为什么"的任务**，开工第一个动作是检索
新闻面，再调 `quant_*` 工具。三类取数：

1. **外盘与宏观**：美股、原油、黄金、美债收益率、当日政策与监管
2. **板块与持仓**：当前持仓、当日候选所属行业的实际消息（业绩、订单、并购、停复牌、处罚、地缘）
3. **明日已知事件**：财报、解禁、重要数据、会议窗口（仅 19:30 对账需要）

**这条不适用于"要不要动手"的任务**（盘前选股、尾盘决策、巡检）。那几类的第一个
调用由 quant-desk 技能「被定时任务叫醒时」的分派表决定：巡检先 `quant_query(resource="watch")`，
任务型直接进 `quant_query(resource="context")`。顺序写死成"先新闻"会让一次 09:26 的决策在检索上
花掉几分钟，而新闻**不参与定价**——它回答不了"这票现在能不能买"。

**边界（这条比顺序本身重要）**：

- 新闻只回答「为什么」，**不参与定价**。价格、持仓、资金、池子、历史分布一律来自工具，
  **禁止引用新闻里的数字**当行情用
- 冲突时以工具为准，并在依据里写明冲突点
- 检索不到就写「今日无显著消息面」，不许用旧闻或推测填充
- 新闻不能成为放宽门槛、绕过风控、或「破例买一次」的理由
- 时间敏感窗口（14:30 尾盘）检索限时 60 秒，晚于 14:48 直接跳过，优先保证 14:57 前出结论
- **调度器不保证时点**：WorkBuddy 给 8/9/10/14 点档的定时任务加约 20 分钟确定性抖动，
  `MO–FR` 也不是交易日；分钟级监控在 Windows 计划任务，RRULE 表达不了。11 个 automation 的
  prompt 顶部有统一开工契约（交易日闸门、实际开工时刻、deferred 工具取数路径、
  `pipeline.state`），见 [`docs/12-automation-chain.md`](docs/12-automation-chain.md) §9。
  既有 8 个任务按「语义窗口 + 内核对象」命名；Hy4 的 3 个短线α任务以 DeepSeek-V4.1-Flash
  在 09:24、14:28、19:10 运行，分别负责 T+1 入场、动态/最迟 T+2 退出、T 日判断与盘后复评。
  只允许主模拟账本，实际委托仍走 propose→ticket→approve；见 §13 与 `docs/13-short-alpha-paper-execution.md`。
  所有阈值与上限从 `risk.limits` / `cash_free` / 票据字段读，不抄提示词里的字面量。
  改提示词前先读 §9。

## 每天只做三件事

1. **看一眼**：`quant_query(resource="watch")` → `findings` 为空就结束。空仓是合法答案。
2. **要动手**：`quant_query(resource="context")` → `quant_query(resource="observations")` → `quant_query(resource="duty")` → `quant_query(resource="persona")`
   → `quant_query(resource="knowledge", params={topic=ledger})` → `quant_order(book="paper", action="propose")` → `quant_order(book="paper", action="approve")`。
3. **盘后**：18:50 市场复盘写 `reports/market/<日期>.md`（结构、情绪阶段、明日三档情景，只读市场不读自家报告）；
   19:30 收盘对账只核账、不重做结构，输出「明天三条」+「一条改动」给次日 09:26。链路与降级见
   [`docs/12-automation-chain.md`](docs/12-automation-chain.md)。
   `quant_workflow(operation="journal_append")` 写复盘（里面的数字会被当场核，`unmatched` 是待修的清单）；
   读旧复盘时先看 `verified`——`null` 表示**从没核过**，不是"核过没问题"。`scripts/score_memos.py` 结算到期的判断。

## 分工（这条比任何指标都重要）

| 谁 | 给什么 |
|---|---|
| 内核 | 事实、历史分布、测算（成交多少、付多少、历史类似情况之后发生了什么） |
| 模型 | 判断：买不买、买哪个、什么时候认错 |
| 执行层 | 合法性：整手、T+1、涨跌停、现金、单票上限、风控 |

内核**没有** `action` 字段。它不说该买该卖，也不替你决定核心盘要不要动。

## 五条红线

1. 钱只有两条门：`/decision/propose + approve`（个股）、`/core/execute`（核心盘，权重必须调用方给）。
   **核心盘已于 2026-11-01 关闭**（`configs/core.json` 的 `retired: true`）：防守腿 511010 一手
   14,112 元 = 5,007 元账户的 2.8 倍，每个防守日只能写一条 100% 现金的净值行——那是买不起，
   不是选择空仓。`/core/plan` 与 `/core/execute` 现在返回 **409 CORE_RETIRED**，日更不再写
   core_nav，擂台只比 llm 与 market；历史 6 行保留为审计记录，`/core` 仍可读 `state` /
   `executable` / `required_nav`。恢复办法：`retired` 改回 false。
2. 不用日线价冒充实时成交；盘后想买就等次日开盘。
3. 止损被跌破时新开仓会被 409 拒；内核/接口不替你下**新判断**的单，但破线持续 `autostop.confirm_seconds` 后，抓取循环经 `/decision/propose + approve` 代执行建仓票上写好的止损（`configs/params.yaml` 的 `autostop.enabled` 可关）。
4. 不信没有 `n` 和区间的数字；`analog` 的 `match.quality=loose` 就当没查到。
5. 不为了让策略通过而调成本参数——`cost_mult` 是压力测试，不是旋钮。

## 目录

- `service/kernel/` 纯领域：撮合、账本、费用、日历
- `service/trading/` 交易台：决策、风控、人格、观察层、衰减
- `service/research/` 研究层：裁判、成本、试验登记、归因、功效
- `plugin/` DSH 插件（`quant_*` 工具，个数以 `plugin/lib/index.js` 的 `buildTools()` 为准）与技能；`plugin/mcp/` 同一张工具表的 MCP 适配层（WorkBuddy 等宿主）
- `docs/` 规范与快照（哪份在生效看 [`docs/00-README.md`](docs/00-README.md)）；`reports/` 判决与研究记录
  （读法见 [`reports/README.md`](reports/README.md)，每份都属于某一天）；`playbook/` 交易规则
  （**现行版本以 `playbook/meta.json` 的 `active` 为准**，现在是 v3，见 [`playbook/README.md`](playbook/README.md)）
- `data/` 行情与账本（`paper.sqlite` 是唯一账户）

## 当前状态：不要抄，去读

账户、持仓、平仓笔数、arena、regime、情绪每天都在变，**这份文件不写快照**。2026-11-01 这里
曾写「权益 5,007.51、0 持仓」；2026-11-05 账本实际是 **5,000.00**——四天里它已经错了，而读到
这句话的模型会照着引用（技能第一条铁律就是"数字只能来自工具"）。

一次 `quant_query(resource="dashboard")` 给全部：账户、当日时间线、待办、擂台、人格、数据就绪。只要账户用
`quant_query(resource="portfolio")`，只要待办用 `quant_query(resource="duty")`，只要数据状态用 `quant_query(resource="data_health")`。

**唯一不随时间变的 KPI：凑够 20 笔平仓。** 在那之前任何"这套系统行不行"的结论都是噪声。
