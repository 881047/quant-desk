---
id: 9c079219-82b9-4287-b44d-af588635f68f
name: quant-desk · 尾盘 末窗决策
rrule: FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=14;BYMINUTE=30
cwd: D:/quant-desk
captured: 2026-11-15T23:26:58+08:00
prompt_sha256: 743b7fdc7457bee1fd0e8feafd2f65c32dca5ad56a6510d75b9e68b601254c08
note: 宿主不保存版本历史；本文件由 scripts/freeze_automations.py 实读生成，不要手改。改 automation 后重跑该脚本。
---

## 唯一问题
收盘前这一笔做不做。跑赢指**超额**，不是上涨：大盘涨 3% 而它涨 2% 算失败。
这是当日最后一个下单窗口，写「等会儿再看」等于没做。

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
   成交模式取 `quant_query(resource="context").risk.fill_mode`。**任务名里的「尾盘」只是窗口名，不是时刻断言。**
5. `quant_query(resource="duty").upstream_missing` 按 `docs/12` §5 降级并在「缺口」点名；引用当日收盘数前
   看 `quant_query(resource="data_health").pipeline.state`（`running` 不补跑，会被锁拒、退出码 3）。
6. **所有阈值与上限从工具读，禁止用提示词或历史报告里的字面量当判据**：仓位/次数/风险读
   `quant_query(resource="context").risk.limits`；价格上限读 `quant_query(resource="portfolio").cash_free` 与对应 `max_*_single`；
   成本读票上的 `round_trip_cost_bps`；阶段读 `quant_query(resource="pools").sentiment.tone` 原词；edge 阈值读
   `quant_query(resource="arena").min_decisions_for_an_interval`。数字只来自工具；内核没有 `action` 字段。
7. **优先级**：D-1「一条改动」> `quant_query(resource="playbook")` > 本文件。`drafts` 非空只表示有人提了改动；
   未激活的草案不生效，`is_fragment=true` 的草案是补丁、激活会把规则书覆盖成残页，不要建议激活。
   改动再大也不越过风控、T+1、涨跌停与现金约束。
8. **档位在本窗口判**（§9.2 要求 14:30 之后才够条件）：当日三值读 `quant_query(resource="limit_structure")` 的
   `counts.limit_up` / `seal_rate` / `max_streak`；三项任一为 null，或
   `quant_query(resource="pools").gates.checks` 的 `tone_ok` / `break_rate_ok` / `height_ok` 任一为 null，就写
   `tier=undetermined`。不许拿 T-1 的读数定今天的档，也不继承 13:35 那次（它按 §9.2 不给档位结论）。

## 时点（由内核会话状态决定）
- `quant_query(resource="market_status").session.now` 取实际开工时刻；`session.in_session` 决定还能不能提交。
- **14:57 是纪律位**：此后 `session.label` 变「收盘集合竞价」，`fill_basis=intraday` 会拿
  集合竞价参考价成交，而不是连续竞价的成交价。服务端并不拦（`in_session` 到 15:05），
  **这条边界自己守**。开工晚于 14:52 直接判「不买」并写明「开工时间不足」，该原因写进 `declined`。
- 开工后 4 分钟内出结论；14:57 前写不完就直接给结论。本任务计划 14:30，实测抖动 0–20 分钟
  （11-12 落到 14:52:17，直接撞上硬门、当天这一窗等于没做），上面的降级就是为它设计的，不得放宽。

## 内核驱动（先看持仓，再看机会）
- `quant_query(resource="persona").current.tradable=false` → 仍做完选股与论证（那是明天与结算的输入），
  结论写「不买：<人格> 无活袖」，`declined` 里制度原因与市场理由都要写；**不在这里切人格**。
- `quant_query(resource="portfolio").cash_free` 才是能花的钱（`cash_available` 含未结算占用）；
  `pending` 里的买单和卖单都代表今天的意图，不重复提。首窗的买单按开盘价结算、收盘后才成交，
  挂在 `pending` 里是正常的，不是「没买」。
- `quant_query(resource="observations").constraints`：「今日可卖 X / 持仓 Y」里 X ≥ Y 必须处理（减或清）；
  X < Y 就写「止损已破，锁到次日」并 journal，**不提议卖出**。
  账外 `quant_query(resource="stops", params={account="external"})` 破线只写「提醒 + 距离」，不对账外 propose。
- 读 `reports/open/D.md` 与 `reports/noon/D.md`：已买或 pending 的判「加仓/持有/减仓/清仓」，
  不重复买同一只；空仓的本窗口按 §9 自己判出的档位走，或写清为什么改。
  持仓数达 `risk.limits.max_positions` 只能卖。
  首窗若写了「按 D-1 改动执行」，本窗口沿用同一条——同一天两笔下单不能按两套规则。
- 独占工具（**这里不调就一天没人调**）：`quant_query(resource="screen")` 候选、`quant_query(resource="profile")` 给理由
  （排名不等于理由）、`quant_query(resource="knowledge", params={topic=industries})`（申万 31 行四者一起读，主力净流入
  标 `flow_as_of` 日期，通常是 T-1）、`quant_query(resource="knowledge", params={topic=seal_tape})`（封单过程）、
  `quant_query(resource="execution_estimate", params={symbol, qty})`（一侧冲击成本与分批表，参与率过高就不能「今天买满」）。
- **赔率门**：判据只在 `quant_query(resource="playbook")` §9.3，本文件不复述。逐只给出三个读数
  （`net.by_horizon.5d` / `horizons.5d.t_stat` / `match.distance_band`），按 §9.3 判过没过；
  **通过即进「下单参数」三步**，门后不得再用任何理由追加否决（§9.6）。说清收益来自行业、风格、
  事件还是估值修复，`quant_query(resource="information")` 的 `blind_spots` / `conflicts` 原文照读。三个「买不到」
  口径分开引，不许互相替代：`in_cap` / `one_word` / `buyable_at_close`；「开过板」另读
  `board_opens.opened`（分母是东财池，不是 `in_cap`，不能相减或相除）；不许拿
  `quant_query(resource="screen")` 的几只当全集。没有 `quant_query(resource="profile").intraday` 就不谈盘中走势，只能说单点距离。

## 下单参数（唯一合法来源，本次校准的关键）
- **`sleeve` 必须显式传 `quant_query(resource="persona").current.open_sleeves` 里的活袖**。接口 `ProposeBody.sleeve`
  默认 `CORE`（见 `service/api/trading.py`），而 CORE 已 retired → 409 `CORE_RETIRED`；
  换成不持有的袖 → 400 `PERSONA_SLEEVE`。每次重新读，不写死某个值。
- `persona` 若传必须等于 `current.key`；`evidence` **必填**（1–8 个实际读过的 id）；
  `thesis` 20–1200 字、`invalidation` 5–500 字；`qty` 与 `target_weight` 二选一；
  不传 price / limit_price / stop_price；`client_request_id` 由插件自动填，**不要手传**。
- 单票上限按 sleeve 从 `risk.limits` 取（`max_aggressive_single` / `max_core_single` /
  `max_core_etf_single`）；价格上限 = `cash_free × 上限 ÷ 一手股数`。
  **不写 30%、不写 15.02 元**——11-05/11-06 用 CORE 口径把可买池缩小了约 40%。
- 三步：`quant_order(book="paper", action="propose")` → `quant_query(resource="ticket")`（读 `fillability.queue.required` 与
  `round_trip_cost_bps`）→ `quant_order(book="paper", action="approve", params={ticket_id, confirm=true})`。超时原样重试（5 分钟幂等），
  不改参数重试；`approve` 返回 `queued` 也是成功，不要重提这张票。
  成交按实时报价，`fill_basis=intraday`，不是收盘价。

## 判「不买」必须留痕
`quant_workflow(operation="declined_add", params={})`：`symbols` 是真权衡过、犹豫过的 1–30 只（不是候选全集），
`reason` 必须**同时写制度原因与市场理由**（否则结算会把「这个人格让他不买」读成对的），
`invalidation` 可留空，`horizon_days` 默认 3；`day` 如需显式给取 `session.calendar_today`；
**不收价格**（传了 422）。11-01 到 11-04 四天零成交、`declined` 0 行，克制没有任何记分。

## 输出
第一行：结论 + 实际开工时刻 + `fill_basis=intraday` + 按哪一版 playbook，带标的、数量、是否已下单；
未成交照实写「挂单未成交：<原因>」，不写「已成交」。第二行一句理由，之后最多 5 行依据。
**通过 §9.3 的票必须逐只写明去向**：`propose` / 买不到（封单·一字·一手超上限）/ 否决项名称——
一只都不写「已权衡」等于没权衡。落盘 `reports/close/YYYYMMDD.md`，第一行「日期 · 结论」；
19:30 逐条核账，`scripts/verify_report.py` 会检查家数能否复现。

## 纪律
新闻限时 60 秒、最多两条查询、晚于 14:48 跳过；新闻只解释「为什么」，禁止引用其数字当行情。
不对账外 propose、不改人格、不碰样本；空仓合法但必须留痕；14:57 后不交新单；
不许用「会涨」代替「能跑赢」。

## 短线α来源隔离（2026-11-15）
1. 普通决策前必读 `quant_query(resource="short_alpha_execution", params={limit: 12})`——`short_alpha_execution` 是
   `quant_query` 的 resource，不是独立工具，上面的四入口清单已含它，无需追加工具。该调用只读，不会下单。
2. 今日存在待入场 short_alpha 权重意图时，本任务不新增普通 BUY，把现金和持仓名额留给专属入场任务；不得替它改量、改价或另建同向票据。已有 short_alpha 持仓或退出票据时，本任务不重复管理、不代为退出。
3. 本任务的普通提案不得使用 strategy=short_alpha 或 short-alpha: 请求号，不得把冻结 target_weight 挪给普通候选。proposed/queued 仍不是成交。
4. 组合风险、T+1、整手、现金和硬止损约束不因来源隔离而绕过。专属短线任务之外的自动风控仍按内核现行规则执行，但不得重复创建 discretionary 退出票据。
5. quant_query(resource="short_alpha_execution") 读取失败时不假定没有短线持仓；quant_query(resource="portfolio") 已显示 strategy=short_alpha 的行必须跳过并报告，不能用旧归因猜来源。
