---
id: 93da3c21-c8ac-4c0c-9984-dabd71be012d
name: quant-desk · 盘前 短线α入场
rrule: FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=24
cwd: D:\Hy4
captured: 2026-11-15T23:26:58+08:00
prompt_sha256: b2e4dbc9942257ca0088f4d0705e646057199a7fad487ec5ca1de1e777be58a9
note: 宿主不保存版本历史；本文件由 scripts/freeze_automations.py 实读生成，不要手改。改 automation 后重跑该脚本。
---

本次请求是由「quant-desk · 盘前 短线α入场」定时任务触发的。
在 Hy4 工作区执行，项目目录 D:/quant-desk。宿主模型固定为 DeepSeek-V4.1-Flash，不要切换模型。
唯一任务：把最近已冻结且今天正当 T+1 入场窗口的 short_alpha 权重意图，经既有风控票据链执行到 paper 模拟账户。内核不替你改票、改权重或改理由；绝不触碰 data/external.sqlite 或任何实盘委托。

## 开工契约 v2
1. 加载 quant-desk 技能。**公开入口只有四个**，均为 deferred 工具：quant_query（只读）/ quant_order（订单票据）/
   quant_workflow（非票据工作流）/ quant_alert（外部提醒）。先
   ToolSearch(tool_names=["quant_query","quant_order","quant_workflow","quant_alert"]) 一次载入 schema，再
   DeferExecuteTool(toolName="mcp__quant-desk__<上面四个之一>") 调用；**工具名只有这四个，通配符不是工具名**。
   resource / operation 名记不准时先 quant_query(resource="catalog") 取 78 个 operation 全表与严格参数，不猜；
   旧单操作工具名已并入（quant_context → quant_query(resource="context")），别再用。重的读串行，不并发。
   服务不可达只报告真实错误，不用记忆补数。
2. quant_query(resource="context").session.market_open_today=false 时输出「今日休市，不执行」并结束：不写文件、不补跑日更、不提案。
3. 实际开工时刻取 quant_query(resource="market_status").session.now，窗口取 session.label，成交口径取 quant_query(resource="context").risk.fill_mode。开盘集合竞价才可写 open_auction；若宿主抖动到连续竞价，必须写 intraday/T+1首个实际可交易窗口，不冒充开盘价成交；不在可交易窗口则停止。
4. quant_query(resource="data_health").pipeline.state 只有 done 才能继续；其他状态或读取失败如实停止，不补跑日更。所有阈值、成本和账户数字只来自工具。
5. 第 1 条已一次载入四入口，本任务实际只用 quant_query 与 quant_order，不必再单独 ToolSearch。

## T+1 入场状态机
1. 调 quant_query(resource="short_alpha_execution", params={limit=12})。只处理 entry_window_day=quant_query(resource="context").session.calendar_today、execution_intent=true 的记录；旧无权重记录 tracking_only 永不追溯下单。今天不是任何信号的 T+1 就明确结束。
2. 逐只核对 target_weight、冻结 thesis/evidence/invalidation 和固定请求号 short-alpha:{signal_day}:entry:{symbol}。state=awaiting_entry 才可新提案；state=proposed 时必须先 quant_query(resource="ticket") 核对同一张今天票据，再决定是否能 approve，不重提；queued/filled 不重复；rejected/expired/cancelled 不换参数重试。
3. 读 quant_query(resource="portfolio") 与 quant_query(resource="persona")。symbol 已有任何策略持仓时报告来源冲突并跳过，不用新 BUY 覆盖原策略标签。sleeve 必须显式取 quant_query(resource="persona").current.open_sleeves 的活袖；persona 如传必须等于 current.key。检查现金、其他 pending、持仓数和风险边界，guard 拒绝是结论。
4. 读取 quant_query(resource="quote", params={symbols=symbol}) 核对真实报价与涨跌停；queue.required 或一字只说明成交不确定，不写成能成交。不得把昨收或日线价冒充实时成交。
5. 三步执行：quant_order(book="paper", action="propose", params={symbol,side=BUY,strategy=short_alpha,sleeve=活袖,target_weight=原冻结值,thesis=原冻结thesis,evidence=原冻结evidence,invalidation=原冻结invalidation,client_request_id=固定入场请求号}) → quant_query(resource="ticket") 读 fillability 与 round_trip_cost_bps → quant_order(book="paper", action="approve", params={ticket_id,confirm=true})。不得改 target_weight、不得手填价格、不得同时给 qty。
6. 每次 approve 后只照实记录 proposed/queued/filled/rejected/expired/armed。queued 是排队，不是成交；超时可沿同一请求号原样重试，结果不确定时不新建第二笔。所有票完成后再次 quant_query(resource="short_alpha_execution") 核对票据与持仓。

## 输出
第一行写实际开工时刻、session.label、fill_basis 和处理结论。逐只报告信号日、角色、冻结权重、票据号、真实状态和成本；没有到期信号、风控拒绝、来源冲突或未成交都要明说。只管理 short_alpha，不处理其他策略持仓，不碰样本账本或账外实盘。
