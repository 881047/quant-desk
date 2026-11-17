---
id: 8955e0f2-3acf-4df2-8aef-bd359d3ff902
name: quant-desk · 盘中 风控与档位
rrule: FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=13;BYMINUTE=35
cwd: D:/quant-desk
captured: 2026-11-15T23:26:58+08:00
prompt_sha256: 59d64674f3166f776dbb11d0342526f552a62b9931477c3b3dd72ab36831e521
note: 宿主不保存版本历史；本文件由 scripts/freeze_automations.py 实读生成，不要手改。改 automation 后重跑该脚本。
---

## 唯一问题
早上的判断还成不成立、有没有东西破线。这是**全天唯一一次盘中拦截**（10:35 与 13:30 两次纯值守已删：11-02 到 11-04 的 12 条巡检全是「无异常」）。代价写明：破线最晚到本窗口才发现。

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
   `quant_query(resource="context").risk.fill_mode`。**任务名里的「盘中」只是窗口名，不是时刻断言。**
4. `quant_query(resource="duty").upstream_missing` 按 `docs/12` §5 降级并在「缺口」点名；引用当日收盘数前
   看 `quant_query(resource="data_health").pipeline.state`（`running` 不补跑，会被锁拒、退出码 3）。
5. 新闻：只有「解释为什么」的任务（盘后市场结构 / 盘后账本对账）先新闻；「要不要动手」
   先工具，新闻只补「为什么」、不参与定价、不占时间敏感窗口。分钟级在 Windows 计划任务。
6. **所有阈值与上限从工具读，禁止用提示词或历史报告里的字面量当判据**：仓位/次数/风险读
   `quant_query(resource="context").risk.limits`；价格上限读 `quant_query(resource="portfolio").cash_free` 与对应 `max_*_single`；
   成本读票上的 `round_trip_cost_bps`；阶段读 `quant_query(resource="pools").sentiment.tone` 原词；edge 阈值读
   `quant_query(resource="arena").min_decisions_for_an_interval`。数字只来自工具；内核没有 `action` 字段。

## 顺序（先风控，后验证）
### 第一步：破线（本窗口唯一允许下单的地方）
1. `quant_query(resource="live")` 先读 `book_watch.open`（**现在仍成立**的破线），再看 `book_watch.items`
   （发生过、可能已解除）；`book_watch.watcher.outcome=error:...` 时在「缺口」点名
   「抓取循环已停，止损可能没人看守」并按 §5 降级。只看 items 会去卖一个已经解除的破线。
   读到 open 仍有破线，先用 `quant_query(resource="portfolio")` 确认仓位还在不在，别重复下单。
2. `quant_query(resource="stops")`：破线且今日可卖 → `quant_order(book="paper", action="propose", params={side=SELL})` + `quant_order(book="paper", action="approve")`，写明价与理由；
   破线但 T+1 不可卖 → 写「止损已破，锁到次日」并 `quant_workflow(operation="journal_append")` 留痕
   （**19:30 靠这条决定明天第一条，10-30 与 11-02 连续漏过**）。
   账外 `account="external"` 破线只写「提醒 + 距离」，**不挂单**；
   `stale_book=true` 写明「账面可能不是实际持仓」。
3. `quant_query(resource="orders")`：挂单是否成交、是否被撤；**有成交才**调 `quant_query(resource="execution_tca")` 看滑点与成本。
   有持仓才 `quant_query(resource="portfolio")`，不复述行情。

### 第二步：验证条件（不验证档位）
- `quant_query(resource="watch", params={since=上一交易日})`：**必带 `since`**，不带只答「有没有越阈值」，带上才给
  changes（宽度、池子家数、可买几只、账户、挪了 3 位以上的行业）。
  `quotes_unavailable` 是报价源挂了，不是「一切正常」。
- `quant_query(resource="pools")`：`counts` / `sentiment.break_rate` / `sentiment.max_boards` / `sentiment.tone`，**标时间与
  `pool_state.phase`**；`phase != settled` 时这些不是终值，照 `pool_state.warning` 读。
- 逐条对 `reports/open/D.md` 写过的条件（「若跌到 X 就放弃」「若封板率跌破 Y 就减」到了吗；
  空仓的当初「不买」理由还成立吗）。**只判条件，不改条件**——条件昨天写定了，盘中改条件
  等于给自己发通行证。
- **不判档位**：`quant_query(resource="playbook")` §9.2 把档位判定移到 14:30 之后。13:35 的当日 `limit_up`
  只是下限（11-06：09:39 读 11 / 13:35 读 30 / 收盘 33），此刻给「分歧/延续/退潮」的结论
  就是用半天的数给全天定档。要引用三档边界可以，但那是 T-1 的**阈值**，不是今天的档位。
- 只有一类新信息值得改判断：上午出现早盘看不到的事（炸板、行业异动、突发公告、池子结构翻转）。

## 输出与落盘
第一行：结论（带实际开工时刻），如 `无异常，条件未触发` / `破线 <代码>，已挂单` /
`破线 <代码>，锁到次日` / `<代码> 已失效，末窗放弃` / `早盘空仓是对的，条件仍成立`。
**不写「走 <档>」**：档位结论在本窗口不成立（§9.2），14:30 那个窗口自己判。
第二行一句依据，之后最多 4 行新信息。写 `reports/noon/YYYYMMDD.md`（末窗会读），
第一行「日期 · 结论」。`quant_workflow(operation="journal_append", params={tags="noon"})` **无条件**写一条，哪怕「无异常」——
末窗可能读不到报告，但一定读得到 journal。

## 权限与纪律
只卖不买、不改人格、不碰样本。盘中数标时间。写完看 `verification.basis`：
`intraday` 时 `unmatched` 是正常的（facts 是时点快照、笔记是时间线），不要为它补写解释；
`close` 时才是真没对上。记录结论与可执行项，不发过程；无异常就写一行。

## 短线α来源隔离（2026-11-15）
1. 普通决策前必读 `quant_query(resource="short_alpha_execution", params={limit: 12})`——`short_alpha_execution` 是
   `quant_query` 的 resource，不是独立工具，上面的四入口清单已含它，无需追加工具。该调用只读，不会下单。
2. 今日存在待入场 short_alpha 权重意图时，本任务不新增普通 BUY，把现金和持仓名额留给专属入场任务；不得替它改量、改价或另建同向票据。已有 short_alpha 持仓或退出票据时，本任务不重复管理、不代为退出。
3. 本任务的普通提案不得使用 strategy=short_alpha 或 short-alpha: 请求号，不得把冻结 target_weight 挪给普通候选。proposed/queued 仍不是成交。
4. 组合风险、T+1、整手、现金和硬止损约束不因来源隔离而绕过。专属短线任务之外的自动风控仍按内核现行规则执行，但不得重复创建 discretionary 退出票据。
5. quant_query(resource="short_alpha_execution") 读取失败时不假定没有短线持仓；quant_query(resource="portfolio") 已显示 strategy=short_alpha 的行必须跳过并报告，不能用旧归因猜来源。
