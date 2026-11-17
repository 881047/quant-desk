---
id: 29cf30b3-0ae5-479e-a745-8a232b607b77
name: quant-desk · 盘前 首窗决策
rrule: FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=26
cwd: D:/quant-desk
captured: 2026-11-15T23:26:58+08:00
prompt_sha256: 2424a4c3743f0ab188ce6f855dc3fefebeaad6ef28d67396c0107d5452fb5361
note: 宿主不保存版本历史；本文件由 scripts/freeze_automations.py 实读生成，不要手改。改 automation 后重跑该脚本。
---

## 唯一问题
今天第一个下单窗口，买不买、卖不卖。**成交口径由 `quant_query(resource="market_status").session.label`
决定，不由任务名或计划分钟决定**——这个窗口实测从未在 09:26 触发过（10-30 09:42、
11-02 09:40、11-03 09:44、11-04 09:42:02、11-05 09:33:40、11-06 09:39）。

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
3. **必读规则书**：`quant_query(resource="playbook")`（正文按章返回，`truncated=true` 时用 `section=` 续读），
   在报告首行写「按 v<N> 执行」。**本文件不复述 §8/§9 的任何阈值、成本或逻辑关系**——
   复述就是第二份真相，改一处必漂一处（2026-11 的「且/或」事故就是这么来的）。
4. 实际时刻取 `quant_query(resource="market_status").session.now`，窗口取 `quant_query(resource="market_status").session.label`，
   成交模式取 `quant_query(resource="context").risk.fill_mode`。**任务名里的「盘前」只是窗口名，不是时刻断言。**
5. `quant_query(resource="duty").upstream_missing` 按 `docs/12` §5 降级并在「缺口」点名；引用当日收盘数前
   看 `quant_query(resource="data_health").pipeline.state`（`running` 不补跑，会被锁拒、退出码 3）。
6. **所有阈值与上限从工具读，禁止用提示词或历史报告里的字面量当判据**：仓位/次数/风险读
   `quant_query(resource="context").risk.limits`；价格上限读 `quant_query(resource="portfolio").cash_free` 与对应 `max_*_single`；
   成本读票上的 `round_trip_cost_bps`；阶段读 `quant_query(resource="pools").sentiment.tone` 原词；edge 阈值读
   `quant_query(resource="arena").min_decisions_for_an_interval`。数字只来自工具；内核没有 `action` 字段。
7. **优先级**：D-1「一条改动」> `quant_query(resource="playbook")` > 本文件。`drafts` 非空只表示有人提了改动；
   未激活的草案不生效，`is_fragment=true` 的草案是补丁、激活会把规则书覆盖成残页，不要建议激活。
   改动再大也不越过风控、T+1、涨跌停与现金约束。

## 状态机
1. `quant_query(resource="persona").current.tradable=false` → 先读 `quant_query(resource="duty")` 判上游「盘前 人格袖就绪」是否已执行；
   未执行则按 §5 降级（沿用上一交易日人格、不擅自切、写一行 journal、在缺口点名），
   **不要直接判「今天不可交易」收工**（9 点档两个任务会抢跑）。
2. 读交接物：`reports/review/<D-1>.md` 的「明天三条」与「一条改动」（唯一权威隔夜指令）、
   `reports/market/<D-1>.md` 的三档情景，并在报告里写「按 D-1 改动执行：<原文>」。
   `review` 缺失 → 第 1 条改用 `quant_query(resource="observations")` 里 `kind="stop_breach"` +
   `quant_query(resource="stops", params={account="external"})`，其余两条不补、不猜。
3. **先卖后买**：`quant_query(resource="observations").constraints` 找 `kind="stop_breach"`，且「今日可卖 X / 持仓 Y」
   里 X ≥ Y 才处理（除非 `quant_query(resource="portfolio").pending` 里已有同只卖单）。账外破线只写「提醒 + 距离」；
   `stale_book=true` 写明「账面可能不是实际持仓」；**不对账外 propose**。
4. 选股两段：`quant_query(resource="auction", params={as_of=D-1})` + `quant_query(resource="anomaly")` + 今日跳空 → 有事件且能说清传导路径
   就按事件买，写清事件、影响多久、什么算错；否则 `quant_query(resource="screen")` 给候选、
   `quant_query(resource="profile")` 给理由（**排名不等于理由**）、`quant_query(resource="knowledge", params={topic=tape_flow})` 看打板有没有被奖励。
5. 跳空 `x=(开盘价/昨收−1)×100`：**用真实开盘价**；只有 `session.label` 还在开盘集合竞价时
   开盘价未落库，才用 `quant_query(resource="quote").last` 近似并写明「近似」。分档门槛一律读 `quant_query(resource="playbook")` §8。
6. **赔率门**：判据只在 `quant_query(resource="playbook")` §9.3，本文件不复述。逐只给出三个读数
   （`net.by_horizon.5d` / `horizons.5d.t_stat` / `match.distance_band`），按 §9.3 判过没过；
   **通过即进「下单参数」三步**，门后不得再用任何理由追加否决（§9.6）。三个「买不到」口径分开引，
   不许互相替代：`quant_query(resource="facts").affordable` 的 `in_cap` / `one_word` / `buyable_at_close`；
   「开过板几只」另读 `board_opens.opened`（东财池，分母不是 `in_cap`，不能相减或相除）；
   不许拿 `quant_query(resource="screen")` 的几只当全集。说不出「为什么是现在」时，写清缺的是哪一类证据
   （事件 / 传导 / 失效条件），不要用它代替 §9.3 的门。

## 成交口径（按 `market_status.session.label` 分支，写进报告首行）
- `开盘集合竞价`（09:15–09:30）：内核排 `open` 队列、按当日开盘价结算，`fill_basis=open_auction`；
  跳空越过限价则 `OPEN_ABOVE_LIMIT` 不成交。结论写「已挂单（按开盘价）」，不写「已成交」。
- `连续竞价`（09:30 后，也是本任务历次的实际落点）：`risk.fill_mode=intraday`，按实时报价
  立即撮合，`fill_basis=intraday`。结论写「已成交 / 挂单未成交 / 不买」，
  **禁止写「按开盘价挂单」**——19:30 按报告写的口径核账，口径错了会把实时成交当成开盘排队单。
- `quant_query(resource="ticket").fillability.queue.required=true` 表示只能排队等封单，那是市场事实不是否决，
  但要写进依据；单票 `round_trip_cost_bps` 才是够本线。

## 下单参数（唯一合法来源，本次校准的关键）
- **`sleeve` 必须显式传 `quant_query(resource="persona").current.open_sleeves` 里的活袖**。接口 `ProposeBody.sleeve`
  默认 `CORE`（见 `service/api/trading.py`），而 CORE 已 retired → 409 `CORE_RETIRED`；
  换成不持有的袖 → 400 `PERSONA_SLEEVE`。每次重新读，不写死某个值。
- `persona` 若传必须等于 `current.key`；`evidence` **必填**（1–8 个实际读过的 id）；
  `thesis` 20–1200 字、`invalidation` 5–500 字；`qty` 与 `target_weight` 二选一；
  不传 price / limit_price / stop_price；`client_request_id` 由插件自动填，**不要手传**。
- 单票上限按 sleeve 从 `risk.limits` 取（`max_aggressive_single` / `max_core_single` /
  `max_core_etf_single`）；价格上限 = `quant_query(resource="portfolio").cash_free × 上限 ÷ 一手股数`。
  **不写 30%、不写 15.02 元**——11-05/11-06 用 CORE 口径把可买池缩小了约 40%。
- 三步：`quant_order(book="paper", action="propose")` → `quant_query(resource="ticket")`（读 `fillability` 与 `round_trip_cost_bps`）→
  `quant_order(book="paper", action="approve", params={ticket_id, confirm=true})`。超时可原样重试（插件 5 分钟幂等句柄），
  **不改参数重试**；被 409/422 拒绝记 code 与原因。
- 持仓数 / 当日动作数读 `risk.limits.max_positions` / `max_actions_per_day`；
  能花的钱是 `cash_free` 不是 `cash_available`。

## 报价与前置检查
`quant_query(resource="quote")` 四项：source 非 daily、`is_realtime` 为真、`quote_ts` 为今天、`age` 不超过 `quant_query(resource="market_status").fill.max_quote_age_seconds`（实测 90 秒，与成交闸门同源）；
针对候选人的实时报价，不是竞价数据集。`quant_query(resource="auction", params={as_of=今天})` 返回 `ok=false` 正常
（当日名单收盘后才入库），不构成不开仓理由。开盘价等于涨停价 → 剔除，不管几连板；
全买不到就写「不买」，不凑一只。

## 输出
第一行：结论 + 实际开工时刻 + `fill_basis` + 按哪一版 playbook。第二行一句理由，之后最多 6 行依据。
**通过 §9.3 的票必须逐只写明去向**：`propose` / 买不到（封单·一字·一手超上限）/ 否决项名称——
一只都不写「已权衡」等于没权衡。
落盘 `reports/open/YYYYMMDD.md`，第一行「日期 · 结论」。
**过一个门就必须留痕**：通过 §9.3 却没走到 `propose` 的票，本窗口就要写
`quant_workflow(operation="declined_add", params={})`，`reason` 里写清是哪一条否决项；「根本没候选」的不写
（那类仍由末窗统一记，避免同日两笔）。11-12 的教训：过了门的名字只活在报告里，19:30 无从核起。

## 权限与纪律
买入 + 卖出（卖出限昨日及更早持仓）；不碰样本账本、不改人格、不对账外 propose。
新闻只解释「为什么」，结论落定后补、限时 60 秒、最多两条查询，不成为放宽门槛的理由。
空仓是合法答案；数字只来自工具；昨收不当今日开盘价；一字板不推荐。

## 短线α来源隔离（2026-11-15）
1. 普通决策前必读 `quant_query(resource="short_alpha_execution", params={limit: 12})`——`short_alpha_execution` 是
   `quant_query` 的 resource，不是独立工具，上面的四入口清单已含它，无需追加工具。该调用只读，不会下单。
2. 今日存在待入场 short_alpha 权重意图时，本任务不新增普通 BUY，把现金和持仓名额留给专属入场任务；不得替它改量、改价或另建同向票据。已有 short_alpha 持仓或退出票据时，本任务不重复管理、不代为退出。
3. 本任务的普通提案不得使用 strategy=short_alpha 或 short-alpha: 请求号，不得把冻结 target_weight 挪给普通候选。proposed/queued 仍不是成交。
4. 组合风险、T+1、整手、现金和硬止损约束不因来源隔离而绕过。专属短线任务之外的自动风控仍按内核现行规则执行，但不得重复创建 discretionary 退出票据。
5. quant_query(resource="short_alpha_execution") 读取失败时不假定没有短线持仓；quant_query(resource="portfolio") 已显示 strategy=short_alpha 的行必须跳过并报告，不能用旧归因猜来源。
