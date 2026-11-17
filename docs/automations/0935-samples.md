---
id: be61509b-3750-41c4-9772-a875c1483fe2
name: quant-desk · 早盘 样本账本轮换
rrule: FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=35
cwd: D:/quant-desk
captured: 2026-11-15T23:26:58+08:00
prompt_sha256: 68705b3b6d0f32430fe8b6988933abaeafa49036b44def474c435a16bb3a6ecf
note: 宿主不保存版本历史；本文件由 scripts/freeze_automations.py 实读生成，不要手改。改 automation 后重跑该脚本。
---

## 唯一问题
样本账本今天那一笔是什么。目标是每天产出一笔**带理由的可结算记录**，不是盈利；
是否轮换由 `quant_query(resource="samples").remaining` 驱动。今天的任务实际在 09:46 才开工（9 点档抖动），
所以 T+1 一律按账本记录日期判断，不按计划时刻。

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
   `quant_query(resource="context").risk.fill_mode`。**任务名里的「早盘」只是窗口名，不是时刻断言。**
4. `quant_query(resource="duty").upstream_missing` 按 `docs/12` §5 降级并在「缺口」点名；引用当日收盘数前
   看 `quant_query(resource="data_health").pipeline.state`（`running` 不补跑，会被锁拒、退出码 3）。
5. 新闻：只有「解释为什么」的任务（盘后市场结构 / 盘后账本对账）先新闻；「要不要动手」
   先工具，新闻只补「为什么」、不参与定价、不占时间敏感窗口。分钟级在 Windows 计划任务。
6. **所有阈值与上限从工具读，禁止用提示词或历史报告里的字面量当判据**：仓位/次数/风险读
   `quant_query(resource="context").risk.limits`；价格上限读 `quant_query(resource="portfolio").cash_free` 与对应 `max_*_single`；
   成本读票上的 `round_trip_cost_bps`；阶段读 `quant_query(resource="pools").sentiment.tone` 原词；edge 阈值读
   `quant_query(resource="arena").min_decisions_for_an_interval`。数字只来自工具；内核没有 `action` 字段。

## 独立沙盒
独立本金、独立账本、独立风控；不读 `reports/*`、不写报告、不碰主账户的 `quant_order(book="paper", action="propose")`。
唯一交接是 19:30 对账读 `quant_query(resource="samples")` 的结算笔数。主账户买什么、持多久都不构成这里的理由。

## 内核驱动
1. `quant_query(resource="samples")` 读 `account` / `closed_count` / `target` / `remaining`。
   `remaining ≤ 0` → 输出「样本已满（closed_count/target），停止轮换」并结束。
2. 有持仓 → `quant_order(book="samples", action="propose", params={side=SELL})`（qty 省略表示清仓）→ `quant_order(book="samples", action="approve")`。
   卖出前确认它是昨天或更早买的；失败没卖的第二天再卖也可以，但**不因「拿了两天」改成长持**，
   这个账本周期就是 1 天。
3. 空仓后：`quant_query(resource="screen")` / `quant_query(resource="universe")` 给候选，`quant_query(resource="profile")` 至少看 tradability 与
   relative_strength（北交所 `tradeable=false` 只看不选）。硬条件：非 ST、不在涨停、
   20 日均额 ≥ 5000 万、价格 ≤ 样本账本可用现金 ÷ 一手股数；**价格由工具算，不手写限价**。
4. 写 thesis（为什么是今天、为什么是它）与 invalidation（什么情况下判断错了），
   写不出就不做；空仓一天好过一笔垃圾样本。

## 下单参数（唯一合法来源）
- `quant_order(book="samples", action="propose")` 的 **`evidence` 是必填数组**（1–8 个实际读过的 id，如
  `daily:20261106`、`screen:20261106`）；`thesis` 20–1200 字、`invalidation` 5–500 字；
  `side=SELL` 省略 `qty`；不传价格。
- 服务在 `/samples/propose` 强制 `strategy=sample_1d`（`service/api/trading.py:1513`），
  样本账本**不受 CORE retired 影响**，因此**不手填 `sleeve`**。
- 被 guard 拒绝：读 `reasons`，不换参数重试，今天不开新仓。

## 输出
第一行：结论（带实际开工时刻），「买 XXX 100 股」或「今天不买，因为 YYY」。
第二行：「样本 <closed_count> / <target>」，两个数都从 `quant_query(resource="samples")` 读回来，
**不要抄提示词或历史报告里的字面量**。不写报告、不复述行情、不在结论前写过程。

## 纪律
新闻只在 thesis 需要「为什么是今天」的印证时**后置**补一次、一条查询、限时 60 秒；
找不到能说清传导路径的消息就写「无消息面，按结构」，不编故事；
新闻不参与定价、不构成硬条件的例外、不引用其中任何数字。
