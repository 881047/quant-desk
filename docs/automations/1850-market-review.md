---
id: 63b4f905-6953-4bc0-8967-8619395d9932
name: quant-desk · 盘后 市场结构
rrule: FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=18;BYMINUTE=50
cwd: D:/quant-desk
captured: 2026-11-15T23:26:58+08:00
prompt_sha256: 5864c5d0d3c00762da51e2e36c658ba244fd48d5abf0d0b14313129ae727eebd
note: 宿主不保存版本历史；本文件由 scripts/freeze_automations.py 实读生成，不要手改。改 automation 后重跑该脚本。
---

## 唯一问题
今天市场是什么日子、明天大概率怎么走。**只读市场，不读自家叙事、不核自己的账、不预测个股、不下单。**
这份报告存在的意义是独立于 `reports/open|noon|close|review`——读了就会把「报告里写的」
当成「市场发生的」。

## 开工契约 v2（内核字段驱动；全文 `docs/12-automation-chain.md` §9）
1. 加载 `quant-desk` 技能并读其分派表。**公开入口只有四个**，均为 deferred 工具：`quant_query`（只读）/
   `quant_order`（订单票据）/ `quant_workflow`（非票据工作流）/ `quant_alert`（外部提醒）。用
   `ToolSearch(tool_names=["quant_query","quant_order","quant_workflow","quant_alert"])` 一次载入，再
   `DeferExecuteTool(toolName="mcp__quant-desk__<上面四个之一>", params={...})` 调用。**工具名只有这四个，
   通配符不是工具名**。resource / operation 名记不准时先 `quant_query(resource="catalog")` 取 78 个
   operation 全表与严格参数，不猜；旧单操作工具名已并入（`quant_context` → `quant_query(resource="context")`），
   别再用。服务不可达先 `curl -s http://127.0.0.1:8811/health` 修服务，不改用记忆里的数、不手搓 HTTP。重的读串行。
2. `quant_query(resource="context").session.market_open_today=false` → 「今日休市，不复盘」并结束：不写报告、
   不写 journal、不下单、不补跑日更。周一到周五的节假日照常触发，别把休市写成数据故障。
3. 实际时刻取 `quant_query(resource="market_status").session.now`，窗口取 `quant_query(resource="market_status").session.label`，
   成交模式取 `quant_query(resource="context").risk.fill_mode`。**任务名里的「盘后」只是窗口名，不是时刻断言。**
4. `quant_query(resource="duty").upstream_missing` 按 `docs/12` §5 降级并在「缺口」点名；引用当日收盘数前
   看 `quant_query(resource="data_health").pipeline.state`（`running` 不补跑，会被锁拒、退出码 3）。
5. 新闻：只有「解释为什么」的任务（本任务 / 盘后账本对账）先新闻；「要不要动手」先工具，
   新闻只补「为什么」、不参与定价、不占时间敏感窗口。分钟级在 Windows 计划任务。
6. **所有阈值与上限从工具读，禁止用提示词或历史报告里的字面量当判据**：仓位/次数/风险读
   `quant_query(resource="context").risk.limits`；价格上限读 `quant_query(resource="portfolio").cash_free` 与对应 `max_*_single`；
   成本读票上的 `round_trip_cost_bps`；阶段读 `quant_query(resource="pools").sentiment.tone` 原词；edge 阈值读
   `quant_query(resource="arena").min_decisions_for_an_interval`。数字只来自工具；内核没有 `action` 字段。

## 数据门槛（`pipeline.state` 才是「日更跑完」的正面答案）
`bars_behind=0` 只说明日线到了，不等于池子、情绪、派生池都写完了。
- `done` → 正常写。`done_not_ready` → 不引用 `failed_steps` 点名的数据集。
- `running` → **不要补跑**（第二个 `daily_job` 会持 `data/daily_job.lock` 失败、退出码 3），
  也不引用未写完的数。
- `missing` → 前台跑一次 `.venv/Scripts/python.exe scripts/daily_job.py --days 3` 后重查。
- 仍是昨天 → 第一行写「复盘不成立·数据未落库」+ 日志末 20 行，只写不依赖当日数据的部分
  （昨日阶段、明日已知事件）。不许用盘中快照或媒体数字冒充收盘。
18:50 时 `moneyflow` 还没入库（21:40 补）、两融要次日 08:50，**资金流一律标 `flow_as_of` 日期**，
不得当作今日资金；游资行为看封单强度、首封时间、晋级率。

## 新闻（本任务属于「解释为什么」，先行、限时 90 秒）
WebSearch 两条（今日收盘与主线、明日已知事件）+ `quant_query(resource="knowledge", params={topic=news, days=1})`
（PIT 过滤的公告与头条）。媒体给的家数**一个都不许进报告**；与工具冲突以工具为准并写明冲突点；
检索不到写「今日无显著消息面」。

## 七块取数（一块一个工具，不重复）
1. `quant_query(resource="facts", params={date=D})`：涨跌平家数、等权与中位收益、派生池与厂商口径并列；
   `affordable` 的 `in_cap` / `one_word` / `buyable_at_close` **三口径分开写**；
   「开过板」另读 `board_opens.opened`（分母是东财池，不是 `in_cap`，不能相减或相除）。
2. `quant_query(resource="regime")`：label / trend / 波动分位 / 宽度（是「能不能加仓」的开关，不是复盘结论）。
3. `quant_query(resource="limit_structure", params={as_of=D})`：封板率用 `history.seal_rate.percentile` 等**历史序列分位**，
   不是当日派生值；炸板率，以及 `quant_query(resource="limit_structure")` 的 `max_streak` / `tiers` / `promotion` /
   `first_seal` / `seal_strength` / `top_reasons` / `trend`（`promotion` 分母为 0 时写 null、
   不写 0%），
   和 `yesterday` 的 `open_premium_mean` / `today_return_mean` / `red_ratio` / `promoted_ratio`
   （昨天那批票今天兑现成什么样）。
4. `quant_query(resource="sector", params={date=D})`：市值加权与等权并列、成交额、主力净流入（标 `flow_as_of`）。
5. `quant_query(resource="lhb", params={as_of=D, limit=20})`：`data_date` 不是今天就写明「龙虎榜为 T-1」。
6. `quant_query(resource="auction", params={as_of=D, limit=10})`：当日为空正常（收盘后才入库），
   此时用 D-1 名单 + 今日收盘逐只还原。
7. `quant_query(resource="anomaly", params={limit=8})`：**工具 schema 上界 1–8**，
   机器摘要是线索不是公告，判重大用 `quant_query(resource="filing")` 读正文。

## 判断（本任务的价值都在这一段）
A **阶段与转折**：`sentiment.tone` 原词 + 封板率分位 + 最高板 + 晋级率 + 涨停家数分位，
并写与上一交易日**同源**的一组对比（涨停 a→b、封板率 c→d、最高板 e→f、炸板率 g→h）。
只描述今天一天不算复盘。
B **结构来源**：等权 vs 中位、sector 市值加权 vs 等权、涨停原因分布三组交叉；
矛盾时写矛盾，不取平均。
C **一条被证伪的叙事**：媒体讲的因、价格没兑现的写「新闻说 X，价格是 Y」；没有写「无」。
D **明日三档情景**（延续/分歧/退潮）：每档写「看哪个数 / 到多少 / 在哪看」，
条件必须是首窗或末窗读得到的数，给出基准档与理由；不写「看情绪」「注意风险」。
E **明日一条观察**：可证伪的结构判断，带数字与失效条件，且能在次日 09:45 之后读到
（它会成为明天三条的第 2 条）。

## 输出
第一行：`<sentiment.tone 原词>：涨停 X / 跌停 Y / 封板率 Z`（带实际开工时刻）；
`sentiment.provisional=true` 时先读 `pool_state.warning`，不许自造近义词。
第二行：`明日基准：<延续|分歧|退潮>，须 <可读的数> <阈值>`。之后最多 8 行，每行一个结论。
落盘 `reports/market/YYYYMMDD.md`。每个统计量给 n 与区间；当日数字必须能在
`quant_query(resource="facts")` 或所引工具返回里原样找到并写明来源（19:30 会抽三个复核，写不出来的数不要写）。

## 权限与纪律
只读。唯一写操作是本报告 + 可选一条 journal note(`kind=note, tags=market`)。
不写账户盈亏、不核自己的报告；跨日比较必须同源；写不出方向就写「今日结构无明确方向」，不凑结论。
