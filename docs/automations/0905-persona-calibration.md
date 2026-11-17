---
id: b2568c20-fd77-463e-8dbd-83153806f656
name: quant-desk · 盘前 人格袖就绪
rrule: FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=5
cwd: D:/quant-desk
captured: 2026-11-15T23:26:58+08:00
prompt_sha256: 7cfe21e91159043ae0f02e43804fd270c14a16cda0ff692509e013fd20849c73
note: 宿主不保存版本历史；本文件由 scripts/freeze_automations.py 实读生成，不要手改。改 automation 后重跑该脚本。
---

## 唯一问题
今天有没有一个**活着的袖**可以下单；没有就切到有活袖的人格。风格判断是第二职责。
11-01 到 11-04 四天零成交的根因就是这一步把人留在 `retail`（唯一袖 CORE 已退役）上。

## 开工契约 v2（内核字段驱动；全文 `docs/12-automation-chain.md` §9）
1. 加载 `quant-desk` 技能并读其分派表。**公开入口只有四个**，均为 deferred 工具：`quant_query`（只读）/
   `quant_order`（订单票据）/ `quant_workflow`（非票据工作流）/ `quant_alert`（外部提醒）。用
   `ToolSearch(tool_names=["quant_query","quant_order","quant_workflow","quant_alert"])` 一次载入，再
   `DeferExecuteTool(toolName="mcp__quant-desk__<上面四个之一>", params={...})` 调用。**工具名只有这四个，
   通配符不是工具名**。resource / operation 名记不准时先 `quant_query(resource="catalog")` 取 78 个
   operation 全表与严格参数，不猜；旧单操作工具名已并入（`quant_context` → `quant_query(resource="context")`），
   别再用。服务不可达先 `curl -s http://127.0.0.1:8811/health` 修服务，不改用记忆里的数、不手搓 HTTP。重的读串行。
2. `quant_query(resource="context").session.market_open_today=false` → 「今日休市，不执行」并结束：不写报告、
   不写 journal、不下单、不补跑日更。RRULE 只认 MO–FR，节假日靠这一步挡。
3. 实际时刻取 `quant_query(resource="market_status").session.now`，窗口取 `session.label`，成交模式取
   `quant_query(resource="context").risk.fill_mode`。**任务名里的「盘前」只是窗口名，不是时刻断言。**
4. `quant_query(resource="duty").upstream_missing` 按 `docs/12` §5 降级并在「缺口」点名；引用当日收盘数前
   看 `quant_query(resource="data_health").pipeline.state`（`running` 不补跑，会被锁拒、退出码 3）。
5. 新闻：只有「解释为什么」的任务（盘后市场结构 / 盘后账本对账）先新闻；「要不要动手」
   先工具，新闻只补「为什么」、不参与定价、不占时间敏感窗口。分钟级在 Windows 计划任务。
6. **所有阈值与上限从工具读，禁止用提示词或历史报告里的字面量当判据**：仓位/次数/风险读
   `quant_query(resource="context").risk.limits`；价格上限读 `quant_query(resource="portfolio").cash_free` 与对应 `max_*_single`；
   成本读票上的 `round_trip_cost_bps`；阶段读 `quant_query(resource="pools").sentiment.tone` 原词；edge 阈值读
   `quant_query(resource="arena").min_decisions_for_an_interval`。数字只来自工具；内核没有 `action` 字段。

## 内核驱动
- `quant_query(resource="persona")`：读 `current.key` / `current.tradable` / `current.open_sleeves` /
  `available[].ready` / `eligibility.blocked_by_sleeve` / `current.switch_limit` 与
  `switches_today`。`tradable=true` 等价于 `open_sleeves` 非空；两者矛盾时以 `tradable` 为准，
  并把矛盾写进依据（那是内核的 bug，不要替它圆）。
- `tradable=false` → 本次**必须**切到 `available` 中袖非空且 `ready=true` 的人格。切换用
  `quant_workflow(operation="persona_switch")`，四个必填参数一个都不能少：`reason`(≥20 字，写清「当前袖全关」)、
  `evidence`(1–8 个真实 id)、`expected_edge_bps`(-500~500；无先验写 0 并注明「用于积累样本」)、
  `invalidation`(可观察，如「次日 AGGRESSIVE 袖也关闭却仍用此人格」)。
  当日切换额度读 `current.switch_limit` / `switches_today`，不看提示词里的字面量。
  要解释「为什么不切 retail」引 `eligibility.blocked_by_sleeve`，别自己编理由。
- `quant_query(resource="regime")` 作风格判据；`quant_query(resource="pools")` 先看 `pool_state.phase` 与它自带的 `warning`：
  `pre_open` / `opening_window` / `missing` 三档的 `counts` 与 `sentiment.tone` 都不是终值，
  照 `warning` 读，不从 `age_seconds` 自己推；三档都不判 hot_money。
- `quant_query(resource="duty")` 的 `blocks="all_new_entries"` 条目（orphan_table / persona_dead_end / wait_data）
  → 今天开不了仓，写进结论第三行，别让下游白跑。`quant_query(resource="risk")` / `quant_query(resource="exposure")` 的
  kill_switch、safe_mode、日内额度、当前暴露任一触发就写「今天不能开新仓」。
- `session.data_ready=false` **不是**跳过理由（盘前当日 bar 必然还没生成）；真正阻塞是
  `data.core_usable=false` 或 `session.bars_behind>0`，此时输出「数据未就绪」并结束。

## 时序（9 点抖动档）
实际开工可能落在计划后约 20 分钟，下游「盘前 首窗决策」同样抖动、两者可能抢跑。
所以**切换在读完 `quant_query(resource="persona")` 后立即执行**，不要排到 regime / duty / 新闻印证之后。
新闻印证一次 WebSearch、一条查询、不先于 `quant_query(resource="context")`、不参与人格判定；与 regime 冲突以 regime 为准并写明矛盾。

## 输出
第一行：结论（带实际开工时刻），三种写法之一：`保持 <人格>，今天可交易` /
`切到 <人格>，今天可交易` / `今天不可交易：<原因>`。
第二行：一句依据，写清 regime 的哪个值 + 哪个袖活着。不写报告、不写文件、不复述过程。

## 纪律
不因「想交易」换人格；不看账户收益与历史盈亏；目标人格 `missing_requirements` 非空不切过去；
被 409/422/429 拒绝不换参数重试，把 code 与原因写进结论。此任务不碰样本、不下单、不改止损。
