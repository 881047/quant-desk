---
id: 21a33e02-c481-42c4-b71e-e0f6058a631d
name: quant-desk · 盘后 短线α判断
rrule: FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=19;BYMINUTE=10
cwd: D:\Hy4
captured: 2026-11-15T23:26:58+08:00
prompt_sha256: 77d6e2db6e9470579c66a28a9db2acea2721eb0022f32a0afdbb77c09648cebb
note: 宿主不保存版本历史；本文件由 scripts/freeze_automations.py 实读生成，不要手改。改 automation 后重跑该脚本。
---

本次请求是由「quant-desk · 盘后 短线α判断」定时任务触发的。
在 Hy4 工作区执行，项目目录 D:/quant-desk。宿主模型固定为 DeepSeek-V4.1-Flash，不要切换模型。
唯一任务：先完成最新完整日线的 WorkBuddy 短线α判断与权重冻结，再对既有 short_alpha 模拟持仓做动态复评；需要下一开盘退出时，冻结理由并走模拟 SELL 票据链。只允许 paper 模拟账户，绝不触碰 data/external.sqlite 或任何实盘委托。

## 开工契约 v2
1. 加载 quant-desk 技能。**公开入口只有四个**，均为 deferred 工具：quant_query（只读）/ quant_order（订单票据）/
   quant_workflow（非票据工作流）/ quant_alert（外部提醒）。先
   ToolSearch(tool_names=["quant_query","quant_order","quant_workflow","quant_alert"]) 一次载入 schema，再
   DeferExecuteTool(toolName="mcp__quant-desk__<上面四个之一>") 调用；**工具名只有这四个，通配符不是工具名**。
   resource / operation 名记不准时先 quant_query(resource="catalog") 取 78 个 operation 全表与严格参数，不猜；
   旧单操作工具名已并入（quant_context → quant_query(resource="context")），别再用。重的读串行，不并发。
   服务不可达只报告真实错误，不用记忆补数。
2. quant_query(resource="context").session.market_open_today=false 时输出「今日休市，不执行」并结束：不写文件、不补跑日更、不登记判断、不提案。
3. 实际开工时刻取 quant_query(resource="market_status").session.now，不得假定自己准点运行；成交窗口和 fill_basis 以实际 session.label 为准。
4. 读取 quant_query(resource="data_health").pipeline.state：只有 done 才能继续。done_not_ready、running、missing、pending、not_required 或读取失败都输出真实状态并结束；不生成证据、不提交空判断、不补跑日更、不下单。
5. 所有行情、成本、历史分布和证据只来自上面四个工具；不读新闻；**不调用已弃用的 quant_query(resource="short_alpha_legacy")**（旧无权重接口，仅为兼容保留，不会给你 target_weight）；不把历史规则 candidates 当作你的判断。内核提供信息，你独立决定 candidate/watch、target_weight 与退出。

## A. T 日判断与 α 权重
1. 第 1 条已一次载入四入口；本任务只用其中的 quant_query、quant_order、quant_workflow，不必再单独 ToolSearch。
2. 调 quant_query(resource="short_alpha_tracking", params={scope=prospective,limit=1})。latest_bar_judgment_registered=true 时跳过新登记，不改写旧判断，继续 B 部分。false 才继续 A3。
3. 以最新完整交易日为 as_of 调 quant_query(resource="short_alpha_evidence", params={limit=100,offset=0})，按 page.next_offset 串行续读至 null；分页失败就停止 A，不用残缺证据登记。
4. 阅读全部证据页，独立形成最多 2 个 candidate、3 个 watch。candidate 与 watch 都可参与模拟交易；每只给出正数 target_weight，按你对 α 的判断分配，而非机械等权。读取 quant_query(resource="strategies") 中 short_alpha.max_weight，所有权重之和不得超过它；不得为凑满仓而放宽成本、整手或失效条件。
5. 调 quant_workflow(operation="short_alpha_judge") 且只提交一次：day=信号日，replay=false，client_request_id=sa-{信号日}-hy4-deepseek-v2；reviewed_evidence 填实际用于判断的 1–12 个 evidence id；items 每只引用自身 evidence，写具体 thesis、invalidation 和 target_weight。没有 candidate 必须写 no_candidate_reason；只有完整阅读并判断无候选时才可 items=[]。判断接口本身不下单。
6. created=false 只接受同日同内容幂等。409、校验失败、超时或结果不确定都停止 A 并如实报告；不得换票、补权重、改理由或另换请求号。成功后再次调用 tracking，核对冻结哈希和 candidate/watch。

## B. 持仓动态复评与下一开盘退出
1. 调 quant_query(resource="short_alpha_execution", params={limit=12})。只处理有真实 short_alpha 持仓的 items；tracking_only、awaiting_entry、只有 proposed/queued 入场票据或没有持仓的都不是持仓复评。没有持仓就明确写「无 short_alpha 持仓需复评」并结束 B。
2. 按 signal_day 分组，逐只读取 quant_query(resource="profile", params={symbol}) 和最新执行状态，重新检查原 thesis、invalidation、最新事实与成本。盘后只允许 hold 或 exit_next_open，不允许把盘后提案写成已经成交。
3. 每个 signal_day 调一次 quant_workflow(operation="short_alpha_review")，冻结该组所有持仓的 decision、至少20字独立 reason 和实际读取的1–8个 evidence id；client_request_id=short-alpha-review:{signal_day}:1910。created=false 仅接受同内容幂等；同请求号不同内容409时停止该组，不改写历史。
4. 对 decision=exit_next_open 且尚无退出票据的持仓，先读 quant_query(resource="portfolio") 确认 symbol、sleeve、qty/available_qty 和其他 pending。available_qty=0 时报告 T+1 锁定，不提案；已有 short-alpha:{signal_day}:exit:{symbol} 票据时不重复。
5. 退出仍走三步：quant_order(book="paper", action="propose", params={symbol,side=SELL,strategy=short_alpha,sleeve=持仓实际sleeve,thesis=刚冻结的退出理由,evidence=复评证据,invalidation=原失效条件,client_request_id=short-alpha:{signal_day}:exit:{symbol}})（qty 省略表示清仓） → quant_query(resource="ticket") 核对 → quant_order(book="paper", action="approve", params={confirm=true})。盘后应返回 queued/next_open；queued 不是成交，不得重提。guard 拒绝是结论，不换参数绕过。
6. 最迟 T+2 退出不得被 hold 复评延长；若已到最后边界，decision 必须退出。任何任务失败都不能伪造成交或收益。

## 输出
只报告实际结果：开工时刻、A 是否登记及权重/冻结哈希、B 复评数量与决定、SELL 票据号及 queued/rejected/filled 真实状态、跳过原因。既有日更负责研究样本外结算；本任务不手工改写结算，不触碰账外实盘。
