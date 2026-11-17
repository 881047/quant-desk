---
id: bbb195bd-f4d0-4e54-b87c-2c2fde00911a
name: quant-desk · 收盘 提醒解读
rrule: FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=15;BYMINUTE=25
cwd: D:/quant-desk
captured: 2026-11-15T23:26:58+08:00
prompt_sha256: a2f566f0243c3308e77e2aa1f354b7d4b00eecd8e2543609a240e572d4419f03
note: 宿主不保存版本历史；本文件由 scripts/freeze_automations.py 实读生成，不要手改。改 automation 后重跑该脚本。
---

## 唯一问题
`quant_alert(action="list")` 里有没有**最近一个交易日**的持仓提醒还没写解读？这是账外实盘监控链路的收口，不是模拟盘任务：不读 `reports/*`、不碰主账户、不下单、不改人格、不写 journal。它存在是因为 `docs/10-alerts.md` 的解读层（规则发事实邮件 → 交接文件 → 三句解读 → 第二封邮件）原先没有任何定时任务接手，2026-11-06 实读已堆积 `pending=15`，最早一条是 2026-10-27。

## 开工契约 v2（全文见 `docs/12-automation-chain.md` §9）
1. 加载 `quant-desk` 技能。**公开入口只有四个**，均为 deferred 工具：`quant_query`（只读）/`quant_order`（订单票据）/
   `quant_workflow`（非票据工作流）/`quant_alert`（外部提醒）。用
   `ToolSearch(tool_names=["quant_query","quant_order","quant_workflow","quant_alert"])` 一次载入，再
   `DeferExecuteTool(toolName="mcp__quant-desk__<上面四个之一>", params={...})` 调用。**工具名只有这四个，
   通配符不是工具名**。resource / operation 名记不准时先 `quant_query(resource="catalog")` 取 78 个
   operation 全表与严格参数，不猜；旧单操作工具名已并入（`quant_alert_handoff` 的 list/answer 就是本任务用的
   `quant_alert(action="list"|"answer")`）。服务不可达先 `curl -s http://127.0.0.1:8811/health` 修服务，
   不改用记忆、不手搓 HTTP。
2. `quant_query(resource="context").session.market_open_today=false` → 「今日休市，不解读」并结束。
3. 实际时刻取 `quant_query(resource="market_status").session.now`；任务名里的「收盘」只是窗口名，不是时刻断言。
4. 数字只来自工具与其返回的 prompt，新闻不参与本任务；分钟级巡检在 Windows 计划任务。

## 步骤
1. `quant_alert(action="list", params={})`（不带参数）列出 pending。`pending=0` → 输出「无待解读提醒」并结束，**不要为了交差写东西**。
2. 挑 `age_hours <= read_window_hours` 的条目——两个数都直接读 `quant_alert(action="list")` 的返回值，不要自己拿时刻相减。窗口默认 96 小时，覆盖「上一交易日 15:25 → 本次 15:25」再加周末与平台抖动。**原先只挑「今天」是漏的**：15:25 之后产生的条目到次日已是历史，而次日仍只看今天，这批条目永远不会被任何一次运行读到。窗口外的条目一律不回答（十天前的事实拿来解释今天会误导），只报「窗口外 pending N 条，最早 <日期>，待人工用 `python scripts/handoff.py drop <id>` 清理」。
3. 对每个窗口内条目，先读它自己的 `prompt`（已经是完整问题，含编号事实、引用条款、持仓）。
4. 写三句话，按 prompt 要求：① 触发了什么；② 对这笔持仓意味着什么；③ 接下来最该确认哪一件事。
   - **每句都要引用 prompt 里给出的一个数字**（现价 / 成本 / 止损线 / 距离 / 成交额 / 均量倍数）。
   - 只用 prompt 里出现的事实与数字，**不得引入任何未出现的价格、比例、事件、公司信息**；查不到就写「这里缺什么」，不要填空。
   - 时间锚是该条的 `created_at`，不是现在：窗口内跨日的条目要写明「<created_at> 时」，不得把当时的价格说成现价。
   - 不给买卖建议、不写免责声明、不复述表格、不做行情预测。
   - 这是「解释为什么」，不是「要不要动手」——不因此放宽任何纪律位。
5. 用 `quant_alert(action="answer", params={id=<该条 id>, analysis=<三句话>})` 提交，**每个 id 最多一次**；提交后由服务端发第二封邮件，不要自己发信、不要重复提交。
6. 全部处理完再输出结论；被工具拒绝就记下 id 与原因，不换参数重试。

## 输出
第一行：结论，例「窗口内 2 条已解读 / 已发 2 封；窗口外 13 条待人工清理」或「无待解读提醒」。
第二行：实际开工时刻 + 窗口内处理、窗口外跳过各几条。
之后最多 3 行，每行一条：`<id> · <触发项一句话>`。不写过程，不复述邮件正文，不落盘报告。

## 边界
- 账外实盘（`account="external"`）**只提醒、不挂单**；这里的输出是邮件解读，不是交易动作。
- 不回答「该不该卖」，那是人的决定；不修改止损位、不写 `data/external.sqlite`。
- 与 `quant-desk · 盘中 风控与档位` 不重复：那个处理模拟盘的 `propose`，这个只补邮件解读。
- 工作日 15:25 触发（收盘后、17:30 日更之前）。每次覆盖 `age_hours <= 96` 的全部提醒——它装得下「上一交易日 15:25 之后」再加周末，也装得下 15:25 之后才入库的那批；只认「今天」会让后者永远等不到读者。
