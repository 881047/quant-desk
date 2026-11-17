---
id: e42ba095-2373-4f80-997a-e06d8de96f7a
name: quant-desk · 尾盘 短线α退出
rrule: FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=14;BYMINUTE=28
cwd: D:\Hy4
captured: 2026-11-15T23:26:58+08:00
prompt_sha256: b64d674ace3aad8b0ee7762ce750a88f04389c41572410f6ac6f10f54159cb78
note: 宿主不保存版本历史；本文件由 scripts/freeze_automations.py 实读生成，不要手改。改 automation 后重跑该脚本。
---

本次请求是由「quant-desk · 尾盘 短线α退出」定时任务触发的。
在 Hy4 工作区执行，项目目录 D:/quant-desk。宿主模型固定为 DeepSeek-V4.1-Flash，不要切换模型。
唯一任务：对 short_alpha 模拟持仓执行已冻结的 exit_now 复评，或执行最迟 T+2 的退出边界；退出理由必须先由你重读证据并冻结，再走 SELL 票据链。不开新仓，不处理其他策略持仓，绝不触碰 data/external.sqlite 或任何实盘委托。

## 开工契约 v2
1. 加载 quant-desk 技能。**公开入口只有四个**，均为 deferred 工具：quant_query（只读）/ quant_order（订单票据）/
   quant_workflow（非票据工作流）/ quant_alert（外部提醒）。先
   ToolSearch(tool_names=["quant_query","quant_order","quant_workflow","quant_alert"]) 一次载入 schema，再
   DeferExecuteTool(toolName="mcp__quant-desk__<上面四个之一>") 调用；**工具名只有这四个，通配符不是工具名**。
   resource / operation 名记不准时先 quant_query(resource="catalog") 取 78 个 operation 全表与严格参数，不猜；
   旧单操作工具名已并入（quant_context → quant_query(resource="context")），别再用。重的读串行，不并发。
   服务不可达只报告真实错误，不用记忆补数。
2. quant_query(resource="context").session.market_open_today=false 时输出「今日休市，不执行」并结束：不写文件、不补跑日更、不提案。
3. 实际开工时刻取 quant_query(resource="market_status").session.now，窗口取 session.label，成交口径取 quant_query(resource="context").risk.fill_mode。只有连续竞价可执行本窗口 SELL；晚于14:57或已不可交易就报告错过边界，不用收盘价伪造成交，不擅自延长持有期限。
4. 所有行情、成本、仓位和证据只来自工具。第 1 条已一次载入四入口，本任务实际只用 quant_query、quant_order 与 quant_workflow，不必再单独 ToolSearch。

## 动态退出与最迟 T+2
1. 调 quant_query(resource="short_alpha_execution", params={limit=12}) 与 quant_query(resource="portfolio")。只选有真实 short_alpha 持仓、available_qty>0，且满足任一条件的 items：latest_review.decision=exit_now；exit_deadline_day=今天；或已错过 exit_deadline_day 仍有持仓。只有 entry queued/proposed、没有持仓或 available_qty=0 时不提案，照实说明。
2. 已有同一只 short-alpha:{signal_day}:exit:{symbol} 的 queued/filled 票据时不重复；proposed 先 quant_query(resource="ticket") 核对同一票据，rejected/expired/cancelled 不换参数重试。
3. 对准备退出的 signal_day 分组，逐只读取 quant_query(resource="profile", params={symbol})、quant_query(resource="quote", params={symbols=symbol})、原 thesis/invalidation 和最新执行状态。时间到期也必须写清事实：这是预先冻结的 T+2 边界，不是假装新的 α 判断。
4. 每组调 quant_workflow(operation="short_alpha_review")，items 写 decision=exit_now、至少20字独立 reason 和实际读取的1–8个 evidence id；client_request_id=short-alpha-review:{signal_day}:1430。复评先冻结，订单后发生；created=false 只接受同内容幂等，不同内容409则停止该组，不改写旧理由。
5. 三步退出：quant_order(book="paper", action="propose", params={symbol,side=SELL,strategy=short_alpha,sleeve=持仓实际sleeve,thesis=刚冻结reason,evidence=复评证据,invalidation=原失效条件或T+2时间边界,client_request_id=short-alpha:{signal_day}:exit:{symbol}})（qty 省略表示清仓）→ quant_query(resource="ticket") 核对数量和成本 → quant_order(book="paper", action="approve", params={ticket_id,confirm=true})。不得手填价格，不改买方向，不处理非 short_alpha 持仓。
6. filled 才是成交；queued/proposed/rejected 均照实记录，不把计划收益写成实际收益。完成后再次 quant_query(resource="short_alpha_execution") 和 quant_query(resource="portfolio")，核对剩余持仓；若仍有 short_alpha 持仓，逐只说明未退出原因和实际状态。

## 输出
第一行写实际开工时刻、session.label、fill_basis 和退出结论。逐只报告信号日、退出理由、复评哈希、票据号、真实状态和剩余可卖数量。未到期且无 exit_now 的持仓明确写「本窗口无需退出」；错过边界或 guard 拒绝不得隐瞒。
