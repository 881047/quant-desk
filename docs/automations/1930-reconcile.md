---
id: 870aeac8-28c4-4e3d-84b8-8f77e3b36115
name: quant-desk · 盘后 账本对账
rrule: FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=19;BYMINUTE=30
cwd: D:/quant-desk
captured: 2026-11-15T23:26:58+08:00
prompt_sha256: 0132354386133551c0034454f042680a6bb9525b2513a9bb3e57e41fec0e0393
note: 宿主不保存版本历史；本文件由 scripts/freeze_automations.py 实读生成，不要手改。改 automation 后重跑该脚本。
---

## 唯一问题
今天说对了几句、明天开盘第一条做什么。**不播报行情、不重做结构复盘**——结构在盘后市场结构那份。

## 开工契约 v2（内核字段驱动；全文 `docs/12-automation-chain.md` §9）
1. 加载 `quant-desk` 技能并读其分派表。**公开入口只有四个**，均为 deferred 工具：`quant_query`（只读）/
   `quant_order`（订单票据）/ `quant_workflow`（非票据工作流）/ `quant_alert`（外部提醒）。用
   `ToolSearch(tool_names=["quant_query","quant_order","quant_workflow","quant_alert"])` 一次载入，再
   `DeferExecuteTool(toolName="mcp__quant-desk__<上面四个之一>", params={...})` 调用。**工具名只有这四个，
   通配符不是工具名**。resource / operation 名记不准时先 `quant_query(resource="catalog")` 取 78 个
   operation 全表与严格参数，不猜；旧单操作工具名已并入（`quant_context` → `quant_query(resource="context")`），
   别再用。服务不可达先 `curl -s http://127.0.0.1:8811/health` 修服务，不改用记忆里的数、不手搓 HTTP。重的读串行。
2. `quant_query(resource="context").session.market_open_today=false` → 「今日休市，不对账」并结束：不写报告、
   不写 journal、不下单、不补跑日更。RRULE 只认 MO–FR，节假日靠这一步挡。
3. 实际时刻取 `quant_query(resource="market_status").session.now`，窗口取 `session.label`，成交模式取
   `quant_query(resource="context").risk.fill_mode`。**任务名里的「盘后」只是窗口名，不是时刻断言。**
4. `quant_query(resource="duty").upstream_missing` 按 `docs/12` §5 降级并在「缺口」点名；引用当日收盘数前
   看 `quant_query(resource="data_health").pipeline.state`（`running` 不补跑，会被锁拒、退出码 3）。
5. 新闻：只有「解释为什么」的任务（本任务 / 盘后市场结构）先新闻；「要不要动手」先工具，
   新闻只补「为什么」、不参与定价、不占时间敏感窗口。分钟级在 Windows 计划任务。
6. **所有阈值与上限从工具读，禁止用提示词或历史报告里的字面量当判据**：仓位/次数/风险读
   `quant_query(resource="context").risk.limits`；价格上限读 `quant_query(resource="portfolio").cash_free` 与对应 `max_*_single`；
   成本读票上的 `round_trip_cost_bps`；阶段读 `quant_query(resource="pools").sentiment.tone` 原词；edge 阈值读
   `quant_query(resource="arena").min_decisions_for_an_interval`。数字只来自工具；内核没有 `action` 字段。

## 开工顺序（固定）
`quant_query(resource="tickets", params={status="open"})` 枚举未结票据 → `quant_query(resource="duty")` 看 `upstream_missing` →
`quant_query(resource="context")` 与 `quant_query(resource="data_health").pipeline.state` → 逐条对账。
枚举出的每张票当天必须有去处：执行 / 带理由 `quant_order(book="paper", action="reject")` / journal 写明留到明天。
`pipeline.state=running` 不补跑（第二个 `daily_job` 持锁失败、退出码 3）；`missing` 才前台
补跑一次 `.venv/Scripts/python.exe scripts/daily_job.py --days 3`；仍失败写
「数据没落库 · 已补跑仍失败」+ 日志末 20 行，但仍给出「明天三条」里与数据无关的部分。

## 逐条对账
读 `reports/open|noon|close/D.md` 与 `reports/market/D.md`（只引用结构结论，不重做），
每句判断核三件事：① 结论对不对（用收盘数）；② 理由对不对（说「一字板买不到」查收盘还封着没有；
理由是消息面的，用是否兑现判断，不用涨跌判断）；③ 没买也要算账——**先看
`quant_query(resource="declined")` 有没有今天的记录**，那是末窗写下、系统按同一口径算出的数；
没有就手算，并在「缺口」点名「末窗判不买但没有写 declined」。
判断错（逻辑有问题，改规则）与运气差（逻辑对结果反了，写「坚持」）分开，不许写「下次注意」。

## 结算（唯一让判断变成数字的地方）
- `quant_workflow(operation="declined_score", params={})`：`mean_excess` 为负 = 不买对了，为正 = 克制是成本；
  `pending` 是没到期，不是失败。
- `quant_workflow(operation="memo_score", params={})`：看 **`bss`** 不是裸 brier
  （`bss=1−brier/brier_climatology`；样本全同一边时 `bss=null`，不是零）。
- `quant_query(resource="arena")`：`edge.n < min_decisions_for_an_interval` 就不许说有 edge，
  **阈值从响应读，不写死**；样本没到只说方向不说优劣。
- `quant_query(resource="hypothesis")` / `quant_query(resource="playbook")` / `quant_query(resource="decay")` / `quant_query(resource="attribution")` / `quant_query(resource="cases")` /
  `quant_query(resource="reports")` / `quant_query(resource="memory_search")`：不是清单任务，**只报有内容的**；
  没平仓写「无平仓，arena 不成立」，没假设变更写「无」，空着比编一行强。
- 抽 `market(D)` 里三个数（涨停/跌停/封板率/最高板任选三）与 `quant_query(resource="facts")` /
  `quant_query(resource="limit_structure")` 复核，不一致写进「缺口」——这是复盘报告唯一的外部校验。

## 台账
1. journal 写复盘（`kind=review`，格式：计划 / 实际 / 差在哪 / 下次改一条），
   写完看 `verification`：`basis=intraday` 时 `unmatched` 正常、不要补写解释；
   `claims.misplaced` / `claims.unbacked` 当场改。
2. `quant_query(resource="stops", params={account="external"})` 破线只写「提醒」，不挂单；`stale_book=true` 写明账面可能不是实际持仓。
3. 模拟盘有持仓就看止损：卖就挂次日开盘委托（`propose` SELL + `approve`，状态 `queued` 不是 `filled`），
   不卖写清为什么继续持有。`quant_query(resource="duty")` 里的 `memo_due` 到期项按提示结算，别让它挂着。
4. **读今天的 journal**，把盘中风控记下的「止损已破但今日不可卖」逐条捞出写进明天第 1 条
   （10-30 与 11-02 连续漏过）；没留痕就在「缺口」点名。

## 输出
第一行：`对 X 条 / 错 Y 条 / 空仓代价 Z%`（带实际开工时刻）。
第二行：`一条改动：<增/删/改哪条规则 + 具体参数>`，必须可执行，次日首窗会读它执行；
写「下次注意」等于没改。若值得长期生效，同时 `quant_workflow(operation="playbook_propose")` 落草案，
并写明**草案需人工激活才生效**（工具明确 never take effect until a human activates them）。
之后：对账表 → 结算 → 明天三条 → 缺口。落盘 `reports/review/YYYYMMDD.md`。
明天三条：第 1 条固定遗留止损（没有也写「无遗留止损」，永远不省略）；
第 2 条来自结构复盘的三档情景（复盘缺失就自取 `quant_query(resource="limit_structure")` 并注明）；
第 3 条是规则验收（写清 n 与作废条件）。每条三要素齐全：看哪个数 / 到多少成立 / 在哪看。

## 纪律
新闻先行、只补不复述；对账里所有数字一律来自工具，媒体与工具冲突以工具为准并写明差异。
只用收盘数据；空仓要算账；ETF 和转债不能用股票成本；结构复盘不在本任务；`bss` 才是技巧分。
