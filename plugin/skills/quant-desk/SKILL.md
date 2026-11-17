---
name: quant-desk
description: |
  A 股模拟盘交易台：读盘、选股、出票、复核、成交、复盘。所有数字来自本地
  quant_* 工具，模型只给意图与理由；数量可给，价格不可给。
  触发词（用户不需要知道任何工具名，说人话就行）：模拟盘、交易台、看盘、复盘、
  今天买什么、有什么机会、能买吗、这票怎么样、该不该卖、持仓、赚了多少、
  短线、超短、打板、追涨、低吸、连板、涨停、炸板、情绪周期、仓位、止损、风控、
  量化、quant、paper trading。
whenToUse: |
  用户用任何一种说法问"今天能不能做、做什么、买哪只、卖不卖、账户怎么样"，
  或者提到短线/打板/连板/情绪、要看持仓与盈亏、要复盘、要改交易规则、
  提到 quant 工具或模拟盘、或者任何需要用本地行情与账户数据才能回答的交易问题。
  用户不必知道工具名，也不必会写参数：技能里有一张"用户原话 → 调用串"的表，
  照它走就能给出完整的分析，而不是反问用户"你想调哪个接口"。
  不适用于：真实券商下单（本系统不接券商）、纯宏观闲聊、与 A 股无关的行情。
license: MIT
---

# quant-desk · 模拟盘交易台

一个 ¥5,000 的 A 股模拟账户。目标是**活下来并验证 edge**，不是每天交易。

> **宿主**：这个工作区跑在 WorkBuddy 上，工具经 MCP 接入，名字带 `mcp__quant-desk__`
> 前缀（如 `mcp__quant-desk__quant_query`），参数与纪律和旧 DSH 插件完全一致；下文一律写短名。
> 内核是**纯信息源**：它给事实、给测算、给"如果这么做会付多少"，不给结论。

## 用户说一句外行话，你要调哪几个

公开入口只有四个：`quant_query` 只读、`quant_order` 订单/票据、`quant_workflow` 非票据工作流、
`quant_alert` 外部提醒。具体操作与严格参数由 `quant_query(resource="catalog")` 返回；
需要完整 schema 时传 `params={operation="context", full=true}`。这张表是**入口**：左边是用户可能说的原话，
右边是照做就够的一组调用。**不要凭工具名猜**——`screen` 听起来像选股，但它是
低吸/趋势的池子，不是短线的入口（下面第 2 行）。

| 用户原话（大意） | 按这个顺序调 | 只读这一个结论 |
|---|---|---|
| 今天能买什么 / 有什么机会 | `quant_query(resource="context")` → `quant_query(resource="screen")` → `quant_query(resource="quote", params={symbols=候选})` | `session.tradable_now`：false 时一切都是"明天的候选" |
| **短线怎么样 / 还能打板吗 / 追不追** | `quant_query(resource="pools")` → `quant_query(resource="limit_structure")` → `quant_query(resource="quote", params={symbols=选中的1-2只})` | `gates.ok` 与 `break_rate`：闸门不过就没有短线这一说 |
| **直接找未来1–2日短线α个股** | `quant_query(resource="short_alpha_evidence")` → **WorkBuddy独立分析** → `quant_workflow(operation="short_alpha_judge")`（含权重）→ T+1 `quant_order(book="paper", action="propose")/quant_order(book="paper", action="approve")` → `quant_workflow(operation="short_alpha_review")` / `quant_query(resource="short_alpha_execution")` | 冻结模型 candidate/watch、理由、证据与目标权重；内核不替模型选票， queued 不是成交 |
| 这只票怎么样 / 能不能买 | `quant_query(resource="profile")` → `quant_query(resource="quote")`（盘中）→ `quant_query(resource="analog")` | `tradability` 与 `analog.net.survives_cost` |
| 我持仓怎么办 / 该不该卖 | `quant_query(resource="portfolio")` → `quant_query(resource="stops")` → `quant_query(resource="watch")` | `stops` 里每行到止损的百分比 |
| 我实盘的持仓 / 真实账户 | `quant_query(resource="stops", params={account="external"})` → `quant_query(resource="dashboard")`（`external` 块） | `stale_book`：true 时这是"账本知道的"，不一定是"你持有的" |
| 我赚了多少 / 账户什么样 | `quant_query(resource="portfolio")`（要"今天干了什么"再加 `quant_query(resource="dashboard")`） | `equity` 与 `realized_pnl` |
| 复盘 / 今天发生了什么 | `quant_query(resource="dashboard")` → `quant_query(resource="facts")` → `quant_workflow(operation="journal_append")` | `facts` 里的数（复盘每个数字都要能在这查到） |
| 帮我买 / 帮我卖 | `quant_order(book="paper", action="propose")` → `quant_query(resource="ticket", params={ticket_id})` → `quant_order(book="paper", action="approve", params={ticket_id, confirm=true})`；不做了就 `quant_order(book="paper", action="reject", params={ticket_id, reason})` | `fillability.queue.required`：true 就是排队，不是成交 |
| 数据到哪天了 / 为什么不能交易 | `quant_query(resource="data_health")` → `quant_query(resource="market_status")` | `core.usable` 与 `quotes.fresh` |

三条规矩，避免最常见的三种走法：

1. **先问"现在能不能交易"，再谈买什么。** `quant_query(resource="context")` 的 `session` 块就写着。
   盘后给候选时必须带一句"这是下一交易日的候选，今晚下不了单"。
2. **短线不碰 `quant_query(resource="screen")`。** 它剔除涨停、接近涨停与 ST——恰好是短线要看的那批。
   短线的池子是 `quant_query(resource="pools")` 的 `limit_up` 行（带连板数、首封时间、封单、开板次数）。
3. **不知道问什么就先 `quant_query(resource="context")`。** 4 KB，账户、风险额度、情绪、待办、候选都在，
   而且是唯一一个"调用它永远不算错"的工具。

**"我持仓"在这台子上有两个答案，先问清是哪一个。** 模拟账本（`quant_query(resource="portfolio")`，
¥5,000，可能空仓）与实盘镜像（`quant_query(resource="stops", params={account="external"})`，来自
`data/external.sqlite` 的成交重放，只读）是两个账户。2026-11-02 那天模拟账本空仓，
实盘镜像里却有两只票——直接调 `quant_query(resource="portfolio")` 会答成"你没有持仓"，而用户明明有。
回答里必须写明是哪一个账本。

### 工具箱的其余部分：不在入口表里，但不是没有

入口表覆盖日常八成问题。下面这些是**按需**的，别因为没列在上面就跳过，也别自己拿
日线手算——它们回答的都是"模型推不出来、只有内核有"的事实。

| 你想干什么 | 调它 | 先读哪个字段 |
|---|---|---|
| 想看原始事实、自己找反常（不被池子框住） | `quant_query(resource="discover")` | `breadth` / `limit_structure` / 行业离散度，先问"哪条最反常"再谈买 |
| 看一只票的日线形态 / 当日分时承接 | `quant_query(resource="bars")` / `quant_query(resource="mins")` | `window_capped`（被夹过会说）；分时的 `t` 是 bar 结束时间、`vol` 是手 |
| 想知道谁在买、席位战绩如何 | `quant_query(resource="lhb")` | `data_date`（收盘后才发布）；传 `seat` 才给该席位的次日开盘/收盘历史 |
| 想知道钱在往哪个行业走 | `quant_query(resource="sector")` | `pct_chg`（市值加权）与 `pct_chg_eq`（等权）是两条口径；`flow_as_of` 说明资金流是哪天 |
| 下单前算这笔的冲击、可成交边界与参与率 | `quant_query(resource="execution_estimate")` | 先读 `fillability.status`：schedule 只是流动性排程，不是成交承诺；再看现金、整手、排队与 `round_trip_cost`。`price_source=caller` 时那个价是假设，不是行情 |
| 想知道账户真实敞口 | `quant_query(resource="exposure")` | `top1` / `top3` / `hhi` 与 `beta`：同行业两只就是一次主题下注，不是分散 |
| 下注前看策略额度、策略有没有衰减 | `quant_query(resource="strategies")` / `quant_query(resource="decay")` | 每个权重都带理由；`state=insufficient` 是"还不能判"，不是"没问题" |
| 想知道自己有没有 edge、收益从哪来 | `quant_query(resource="arena")` / `quant_query(resource="attribution")` / `quant_query(resource="cases")` | arena 少于 20 笔不声称 edge；cases 的七项归因相加**等于** `pnl_pct`，且满 5 笔才给聚合 |
| 想读规则 / 改规则 | `quant_query(resource="playbook")` / `quant_workflow(operation="playbook_propose")` | 草稿不会生效，激活由人决定；别把草稿当现行规则引用 |
| 想把一个直觉变成可检验的假设 | `quant_query(resource="hypothesis")` | `action=add` 必须带 `test_spec`；只有统计能改状态，`action=result` 是唯一出口 |
| 提案的 evidence 要写数据快照 id | `quant_query(resource="lineage")` | 当天那批 `snapshot_id`；写进 evidence 才算可复现 |
| 提案不想做了 / 票据要作废 | `quant_order(book="paper", action="reject")` | `ticket_id` + `reason`。**只 propose 不 approve 会一直挂在 `quant_query(resource="tickets", params={})` 里**，别用新提案盖旧的 |
| 已排队（queued）的单想撤 | `quant_order(book="paper", action="cancel")` | `ticket_id` + `reason`（可选）。撤的是**已提交订单**不是提案；不是卖出（减仓用 `quant_order(book="paper", action="propose", params={side=SELL})`）；`ceiling` 单 14:57–15:00 不可撤；已成交返回 `status="filled"`、不撤销 |
| 想试算核心盘的整手与费用 | `quant_query(resource="core_preview")` | 纯试算不记账；`core_plan` / `core_execute` 已 409 CORE_RETIRED，别调 |
| 告警邮件等着人解读 | `quant_alert(action="list")` | 先无参列出待办，读它给的 prompt，三句话作答即发信；一条只答一次 |

这张表里有两个是流程必须的：**`quant_order(book="paper", action="reject")`** 与 **`quant_order(book="paper", action="cancel")`**。提案的出口是
approve 或 reject；**已提交（queued）的订单想撤，用 `quant_order(book="paper", action="cancel", params={ticket_id, reason})`
撤剩余**（撤单不是卖出——减仓用 `quant_order(book="paper", action="propose", params={side=SELL})`）。停在 `proposed` 的票
没人替你收尾，19:30 对账会把它当成"提了没下文"。

**登记了假设就要跑裁判**，否则它永远停在 `draft`：`cd D:/quant-desk &&
.venv/Scripts/python.exe scripts/run_research.py --hypothesis <id>`，结论自动回填。
只 add 不 result，等于提了一个没有人会回答的问题。

## 短线（1–2 日）：十五分钟怎么走完

短线在这台子上有明确的门槛和算术，不是"看哪个涨得猛"。按这四步走，每步只读一个数：

```
1. quant_query(resource="pools")                     # 情绪 + 三池 + 每只票的一手金额
   → 读 gates.ok / break_rate / max_boards。**这三项是阈值读数，不是命令。**
     闸门不过意味着"按预注册的那几条线，今天的情绪结构不支持开新仓"；
     要不要据此停手是你的判断，停了就把理由写进 journal。
     要说清的是：它们是**先验门槛**，不是对你手上这只票的评估。
2. quant_query(resource="limit_structure")           # 结构 + setup_thresholds（先读这张表）
   → `setup_thresholds` 按身位给"历史回封率 / 你要的回封率 / 差多少 pp /
     n_buyable / 一字占比"。**它没有判词了**——`gap_pp` 为正表示实际回封率
     低于这道门槛，负表示跨过去了；要不要打这个身位由你决定，
     但要把 gap 和你的置信度一起写出来，别只写"看起来强"。
   → 表里的 `reseal_rate` 已经是**买得到的那一组**（`buyable`）：限价单只在价格
     跌破涨停价时成交，全天没跌破的一字只能靠排队优先权。一字占比随身位从
     2.2% 涨到 28.6%，而它们的回封率按构造是 100%——把这一组算进去会让
     "打高位板"看起来更好。`reseal_rate_all` 是全样本值，只用来对账。
     实测：按全样本算，gap 随身位收窄（19.2→12.7pp）；按可买组算，
     gap 随身位**扩大**（21.7→31.0pp）。方向是反的，别读错。
   → 再读 promotion[].promotion_rate 与 seal_rate：强势有没有在被奖励。
     晋级率低而封板率高 = 有人封、没人接，第二天开盘就是兑现盘。
   → 这张表回答不了的写在 `still_missing` 里（排板成交概率），别假装它答了。
3. quant_query(resource="quote", params={symbols=选中的1-2只})             # 单票：现价、涨跌停、五档
   → 读 limit_state：sealed 是现在封着，opened 是封过又开。
4. quant_order(book="paper", action="propose")                   # 出票，只给 qty 或 target_weight，不给价格
```

第 1 步里每只票已经带 `lot_cost_cny`（一手 100 股的金额）和 `pct_of_equity`（占净值）。
**先看这两个数再看涨幅**：5,007 元的账户，一手 2,193 元的票就是 43.8%，一手已经超过
单票上限——这种票再强也只是一笔，仓位管理在它身上没有意义。

**算一遍再开口。** 票上的 `est_fees` 只是**买入这一边**：311 元的票显示 5.00 元，看起来才 1.6%，而它的 `round_trip_cost_bps` 是 **346.7bps**——3.5%，股票还没动。决策看后者，不看前者。一笔 1,500 元的往返成本约 **92bps**（最低佣金 5 元×2 + 印花税 5bps
+ 双边滑点 20bps）。所以"1–2 日"的短线，毛边必须大于 0.92% 才不亏——`quant_query(resource="analog")`
的 `net` 块直接给扣完成本的净超额，`survives_cost=false` 就不要拿毛涨幅当理由。
这也是 ledger 里那些短线结构过不了门槛的原因：它们最好的毛边是 0.30%。

**但那句话有适用范围，别读宽了。** ledger 里的判决测的都是**次日开盘接力**
（`entry=next_open`）——不是打板本身。打板的钱在隔夜段，而"T 日收盘价买入"这个
入场点此前根本表达不出来，所以**没有任何一条判决说过"打板不赚钱"**。
`quant_query(resource="knowledge", params={topic=ledger})` 的 `scope` 块写的就是这条边界；引用之前先读它。
要对打板下结论，得先跑一条 `entry=t_close` 的假设，走 `run_research` 拿到判决。

### 直接要“未来 1–2 日、扣成本后可能跑赢市场”的个股

主链路是 **内核证据 → WorkBuddy 独立判断 → 冻结模型判断 → 样本外结算**。不要把旧
`quant_query(resource="short_alpha_legacy")` 的规则 candidates 改名成 WorkBuddy 的判断，也不要让日更自动补选票。

1. `quant_query(resource="short_alpha_evidence", params={as_of, limit=2})`：读取派生收盘涨停池与同花顺封板面板
   交集里的证据卡。行按代码稳定排序，**不按收益排名，不含 candidate/watch 判词**。
   每行有 `evidence_id`；用 `page.next_offset` 续读，或按 `symbols` 收窄。当前范围
   不覆盖低吸、炸板反转或全市场非涨停排名。
2. **判断由你完成**：综合 `evidence.selected_rule / n / n_days / net_excess_mean_ci95`、
   `recent_250`、`base_rate.gap_pp`、`counter_evidence`、分帧封单和 `execution`。
   历史证据是探索性分布，未做跨股票 BH 校正，不是“已证实 α”。`account_ok=true`
   只表示当前账户整手情景可负担；`queue_required_fill_unknown` 不是能成交，
   `order_created=false` 表示没有订单。
3. `quant_workflow(operation="short_alpha_judge")` 冻结你的判断：`reviewed_evidence` 填实际读过的 1–12 个
   evidence id；`items` 最多 2 个 candidate、3 个 watch，每只引用自身证据并写
   `thesis`（≥20 字）与 `invalidation`（≥5 字）。没有 candidate 必须写
   `no_candidate_reason`；`items=[]` 是**明确无候选**，与“没有登记”不同。
   自动模拟交易时，candidate 与 watch 都逐只给 `target_weight`；总和不得超过
   `quant_query(resource="strategies")` 里 short_alpha 的上限。权重是目标仓位意图，不是股数或成交承诺。
   同日同请求幂等，不同判断 409，不能事后换票、补权重或改写理由。
4. **T+1 开盘入场由你另走票据链**：先用 `quant_query(resource="short_alpha_execution")` 核对信号日、
   固定请求号与现有票据，再读 `quant_query(resource="context")/quant_query(resource="portfolio")/quant_query(resource="quote")`。逐只调用
   `quant_order(book="paper", action="propose", params={strategy="short_alpha", target_weight=冻结值,
   client_request_id="short-alpha:{T}:entry:{symbol}"})`，读票后 `quant_order(book="paper", action="approve")`。
   `queued` 只是排队；guard 拒绝就照实记录，不换参数绕过。同票已有其他策略持仓时不入场。
5. **持仓中动态复评**：重读最新证据，用 `quant_workflow(operation="short_alpha_review")` 冻结每只的
   `hold / exit_next_open / exit_now`、理由和新证据。复评本身不下单；退出另走 SELL
   `quant_order(book="paper", action="propose", params={strategy="short_alpha", client_request_id="short-alpha:{T}:exit:{symbol}"})`
   与 `quant_order(book="paper", action="approve")`。最迟 T+2 退出，复评不能延长这个边界。
6. `quant_query(resource="short_alpha_execution")` 只读关联 target_weight、entry/exit 票据、当前持仓、
   复评和 T+2 截止日。旧无权重记录显示 `tracking_only`，不能追溯生成订单。
7. `quant_query(resource="short_alpha_tracking")` 只看真实后续：T 收盘是研究锚点，T+1 开盘是可执行
   机会代理，不是成交回报；一字、排队未知、缺 bar、停牌和预算不足不记 0 收益。
   `prospective` 与 `replay` 永远分组，按 candidate/watch、T+1/T+2 给净超额、独立日
   和交易日块区间。n<30 或独立日<8 时不能声称统计充分。
   `latest_bar_judgment_registered=false` 只表示模型尚未登记。

判断和复评接口都不创建订单、不改模拟账本；真实模拟买卖只走
propose→ticket→approve，最终现金、T+1、整手、费用和风控以 guard 重算为准。
`quant_query(resource="portfolio")` / `quant_query(resource="context")` 持仓的 `strategy` 用来区分短线 α 与其他任务；
其他自动化不重复管理 short_alpha 持仓，但任何硬止损与风控都不能绕过。
历史 `as_of` 的账户现金与上限仍是当前快照，不得写成账户级 PIT 回测。

旧 `quant_query(resource="short_alpha_legacy")` 保留为**历史规则研究接口**，只用于对照，不作为
`quant_workflow(operation="short_alpha_judge")` 的输入，也不进入 WorkBuddy 的样本外记录。


## 第 0 步：确认服务活着

所有 `quant_*` 工具都是本地服务 `127.0.0.1:8811` 的薄客户端。工具报
`quant service unreachable` 时先看服务，**不要改用记忆里的数字**：

```bash
cd D:/quant-desk
.venv/Scripts/python.exe -m uvicorn service.api.app:app --host 127.0.0.1 --port 8811
```

平时它由计划任务 `quant-desk-service` 保活；健康检查是 `GET /health` 返回 `{"ok":true}`。
日更任务 `quant-desk-daily` 在工作日 17:30 跑（回填 + 质量校验 + 写核心盘净值）。
两个任务都不跑时，先修管道再谈交易——数据不新会变成 `409 DATA_NOT_READY`，
那不是行情判断，是管道故障。

### 取数路径：先 ToolSearch，再谈降级

`quant_*` 是 deferred 工具，**不在初始 tool list 里**。会话开头先
`ToolSearch(tool_names=[...])` 载入 schema，再用 `DeferExecuteTool` 调用。
「tool list 里没看到」不等于不存在。

一次可以给一组公开名（数组），按"这个问题要用的那几个"一起载入；公开名只有
`quant_query`、`quant_order`、`quant_workflow`、`quant_alert`，写短名就行，宿主会解析成
`mcp__quant-desk__quant_*`（2026-11-05 实测：`ToolSearch(["quant_query"])` 返回
`mcp__quant-desk__quant_query`）。**别一个一个地来回问**，每次 ToolSearch 都是一个往返。

但**调用时只能用返回的那个全名**，resource 放在 params 里，不能拼进工具名：
`DeferExecuteTool(toolName="mcp__quant-desk__quant_query", params={"resource":"context","params":{}})`。
短名在 ToolSearch 里能解析，在 DeferExecuteTool 里会报 `not available in the current
environment`（同一天实测：短名 `quant_query` 失败、带前缀的全名成功）。照技能正文抄短名
去调用，就会看起来像"工具不存在"。

**也别并发发多个重读。** 服务是单进程：10 个并发 `/discover` 实测每个 21.7 s，而串行一次
约 1.4 s——并发只是把请求排进同一个队列，先到 60 s 上限的那个先超时，然后被误报成
"不可达"。要并行的只有"互不依赖且都轻"的读，重的读一个一个来。

只有 ToolSearch 也失败（连接器未启用、服务不可达）才降级到 `curl` + `.env` 的
`QUANT_TOKEN`。那是兜底，不是常规路径：2026-10-30 的 15:09 尾盘任务跳过了 ToolSearch
直接 curl，从 `.env` 读令牌手搓 HTTP，花了 16 次 Bash；同一任务的 14:58 那次走 MCP，只用 3 次。

问「数据在哪、覆盖到哪天」之前，先调 `quant_query(resource="data_health", params={date})`，不要 `ls data/parquet`。
19:30 那次对账先花了 13 次 Bash 摸目录，中途一度误判「2026 日线不在库里」，第 14 次调
`data_health` 才发现数据一直是好的。

**问“今天怎么样”、复盘、或者准备动手之前，先调 `quant_query(resource="dashboard")`。** 它一次返回
当日时间线（提案/成交/被拒/备忘/换人格/复盘）、账户、待办、擂台进度、人格、各接口
调用次数、数据就绪状态。带 `.json` 后缀是同一份数据的结构化版本。不要用十几个工具
手工拼这张图：2026-10-26 有一次复盘为此花了 52 次调用、6.5 分钟，而答案是同一份。

## 分工：内核是信息源，不是顾问

- 内核只给三样东西：**事实**（现在是什么样）、**历史分布**（类似情况之后发生过什么）、**测算**（如果你这么做，要付多少、能成交多少）。
- 它没有 `action` 字段，不说该买该卖，也不替你决定核心盘要不要动。`quant_query(resource="observations")` 里的每一条都只有 `fact` / `measures` / `attention`。
- 唯一的硬约束是风控：有持仓收盘价低于它自己的止损时，新开仓被 409 `OPEN_VIOLATION` 拒绝。这不是建议，是"风险预算已违约就不加仓"，卖出不受限。

## 三条铁律

1. **数字只能来自工具。** 价格、涨跌幅、持仓、历史统计，必须调用 `quant_*` 获得。凭记忆报一个价格，等于伪造证据。
2. **数量可以说，价格不能说。** `quant_order(book="paper", action="propose")` 写标的、方向、理由、证据、失效条件，
   以及 `qty`（股数）或 `target_weight`（**目标**仓位占权益比例）**二选一**。`target_weight` 是按现持仓算差额：已有 30% 再提 30% 不会变成 60%，而是没有要买的部分。给了 `qty` 就按股数，
   但仍受合法手数、单票上限、单笔风险预算三重约束——超了会告诉你"最多多少股"。
   限价与止损永远由服务算，不接受手写。卖出时 `qty` 表示减仓数量，省略即清仓。
3. **被拒绝不要换参数重试。** guard 的拒绝是结论，不是障碍。读 `reasons`，解释给用户，或放弃这笔。

## 被定时任务叫醒时

调度由宿主负责（WorkBuddy 用自动化任务；DSH 用 `schedule_create`），内核不自己计时。

**11 个 automation 的 prompt 顶部是「开工契约 v2」（全文见
`docs/12-automation-chain.md` §9；3 个 Hy4 短线α任务见 §13）**：deferred 工具取数路径、`market_open_today` 交易日闸门、
`session.now` / `session.label` / `risk.fill_mode` 决定窗口与成交口径、
`quant_query(resource="duty").upstream_missing` 降级、`pipeline.state` 日更状态、新闻顺序分工、
分钟级任务归 Windows 计划任务，以及最重要的一条——**所有阈值与上限从工具读，
不用提示词或历史报告里的字面量**。改任何 prompt 之前先读 §9。

任务名已从 `quant-desk 0926 开盘决策` 这类带分钟的形式改成**语义窗口 + 内核对象**
（`quant-desk · 盘前 首窗决策` 等；原 8 个对照表见 §9.1，另外 3 个为
`quant-desk · 盘前 短线α入场`、`quant-desk · 尾盘 短线α退出` 与
`quant-desk · 盘后 短线α判断`）。名字里没有分钟，因为平台
8/9/10/14 点档有约 20 分钟确定性抖动，分钟不是事实；`session.label` 才是。

**下单参数只有内核一个来源**（2026-11-06 实测，写死在提示词里会直接把单子送死）：
- `quant_order(book="paper", action="propose")` 的 `sleeve` **默认 `CORE`**（`plugin/lib/index.js:1004`、`decisions.py:264`），
  而 CORE 已 `retired` → 409 `CORE_RETIRED`；换成不持有的袖 → 400 `PERSONA_SLEEVE`。
  **必须显式传 `quant_query(resource="persona").current.open_sleeves` 里的活袖**。
- 单票上限按 sleeve 从 `quant_query(resource="context").risk.limits` 取：`max_aggressive_single=0.5`、
  `max_core_single=0.3`、`max_core_etf_single=0.4`（`decisions.py:652-653`）。活袖是
  AGGRESSIVE 时按 0.5 算；旧提示词写死 30%，11-05/11-06 报告据此算出 15.02 元价格上限，
  **把可买池缩小了约 40%**。价格上限 = `portfolio.cash_free × 上限 ÷ 一手股数`。
- `evidence` 必填（1–8 个实际读过的 id）、`thesis` 20–1200 字、`invalidation` 5–500 字；
  `persona` 若传必须等于 `current.key`；`qty` 与 `target_weight` 二选一；不传价格；
  `client_request_id` 由插件自动填，不要手传。

**先看这次醒来要回答什么问题**，再决定第一个调用：

| 醒来要回答的 | 第一个调用 |
|---|---|
| 「此刻盘口怎么样」——盘中巡检、看全市场 | `quant_query(resource="live")` |
| 「有没有事」——告警、巡检、持仓盯盘 | `quant_query(resource="watch")`（此刻、按实时报价）／`quant_query(resource="live").book_watch`（你睡着时发生过什么） |
| 「买不买 / 对不对」——盘前选股、尾盘决策、午间验证 | `quant_query(resource="information")` 或 `quant_query(resource="context")` |
| 「今天是什么日子 / 明天怎么走」——18:50 市场复盘 | `quant_query(resource="facts")` + `quant_query(resource="regime")` |

**盘中的「现在」只有 `quant_query(resource="live")` 一条路。** `quant_query(resource="context")` 的候选票、`quant_query(resource="facts")` 的
breadth 都按 `latest_bar_date()` 算，而 10:30 时那一天是**昨天**——读它们得到的是昨天收盘
价算出来的名单，不是此刻。两个工具不冲突：`quant_query(resource="live")` 答「现在」，`quant_query(resource="facts")` 答「这个
交易日收盘后有什么」，收盘报告引用的每个数字仍然必须来自后者。

任务型 automation 不需要前置 `quant_query(resource="watch")`：它本来就要读全市场，「有没有异常」不构成前置条件，先调只是多一次往返。

`quant_query(resource="live")` 的读法：

- **三个字段必须一起读**：`frame_age_seconds`（这一帧多旧）、`session.in_session`（现在是不是该有新帧）、`limits.available`（涨跌停数算出来没有）。缺任何一个，后面的数字都会被当成立刻的。
- 收盘后 `frame_age_seconds` 会一直涨而 `frame_is_stale` 是 `false`——快照本来就停了，那不是 stale。盘中连续两个批次（约 6 分钟）没有新帧才是，那时先看 `quant_query(resource="data_health")`。
- **`delta` 是唯一能给出「变化」的字段。** 单帧没有方向：读到涨停 61 家，读不出它是从 82 掉下来的还是从 40 涨上来的——同一个数字，两件相反的事。`delta.change` 是**差值不是百分比**（`equal_weight_return` 0.021→0.008 记作 `-0.013`，即 -1.3 个百分点）；`turnover_cny` 的差值是这段窗口**新增**的成交额，不是总量。`delta.short_of_lookback=true` 表示今天还不够 `lookback` 分钟，拿到的是更短的窗口，真实间隔看 `elapsed_seconds`——别把它当成 15 分钟的变动。
- **`alerts` 的空有两种意思，而且它不属于这个账户。** 归属看 `alerts.scope`：那是**实盘**账户（`scripts/monitor.py` 每 5 分钟一轮，读手工录入/券商导入的持仓），不是本模拟台的 paper 持仓——两个账户的票可以完全不同（2026-11-05：实盘 002185，模拟台空仓）。至于那个空：`alerts.watcher` 为 `null` = 监控进程今天一次都没跑过（没人在看），不是「没事」；`watcher.is_stale=true` = 跑到某一刻就停了，下面列的几条是停机之前留下的。只有 `watcher` 存在、不 stale、且 `count=0` 时，才可以说「巡检过，无异常」。
- **`book_watch` 才是你自己账本的看守。** 抓取循环每落一帧（30 秒–3 分钟）就把 paper 持仓过一遍 tripwire，写在 `reports/paper/`。读法只有一条：**先看 `open`，再看 `items`**。`open` 是此刻仍然成立的（带 `first_at`/`last_seen`）；`items` 是发生过的转变，上午破线、两分钟后又回到止损上方的那些，`items` 里仍留着一条 `fired`——只读它就会去卖一个已经好了的仓位。`watcher.outcome` 说明循环的状态：`ok` 在跑、`idle` 午休或窗口之间（没有新帧可判，不是没人看）、`closed` 随收盘正常退出、`error: ...` 这一轮崩了。同一条 finding 持续成立只写一行，不会把 8 条窗口刷满；解除要连续两分钟不成立才写，因为停损价常压在整数关口上。**破线持续两分钟仍成立时，循环会替你卖掉**（判断用那一帧的价，成交取当时的实时报价）（`configs/params.yaml` 的 `autostop.enabled`，关掉就退回只记不卖）。它是独立一笔 SELL，`actor=stop:<建仓票号>`，理由引用你建仓时写下的那个止损数——不是你又要卖一次。所以读到 `open` 里还有破线，先用 `quant_query(resource="portfolio")` 看仓位还在不在，别重复下单；T+1 卖不掉的那条会一直留在 `open` 里，次日第一帧自动成交。
- `limits.up` 用 `stk_limit` 逐票判，`quant_query(resource="pools").counts` 走派生池，两边可能差 1-3 家。要权威池子读 `quant_query(resource="pools")`；要「这一帧可逐票复核的数」读这里。`limits.available=false` 是「没算出来」，不是「零家涨停」。
- `account.equity` 是按**这一帧的价格**重算的，不是收盘价。`unpriced` 里的票退回了账本价——有它们时这个数只部分有效。
- `frame_id` 是这一批全市场报价的**批次外键**，写进 `quant_workflow(operation="journal_append")` 等于记下「我当时看的是哪一帧」。热目录（`rt_snapshots/<day>/`）只留最近 120 帧，但**裁掉之前会 `copy2` 到 `rt_archive/<day>/`**，所以 `/market/frame` 带 `as_of` 或 `frame_id` 可以跨日回放（`quant_query(resource="live")` 已暴露这两个参数，回放响应里 `account_is_historical=false` 提醒你账本仍是当前的）。**冷存自 2026-11-07 起生效：此前的分区没有 `frame_id`，永久不可回补**——引用那些老日期确实会是空的，别拿别的数据源假装补上。

`quant_query(resource="watch")` 的读法：

- `findings` 为空 → 回一行「无异常」就结束。不要因为被叫醒就一定要读数据或交易。
- 有 `critical`（止损已破）→ 先处理这笔敞口，或写清为什么继续持有。
- 有 `high`（距止损 2% 内、持仓单日 ±5%）→ 读 `quant_query(resource="observations")` 再决定。
- `quotes_unavailable` 不是「一切正常」，是「没测到」；按收盘价判断并说明。
- `checked.positions=0` 且 `pending` 为空 → 账户确实空仓，直接结束；不要再调 `quant_query(resource="portfolio")` 复核，那只是把「没有仓」再确认一遍。
- **每轮巡检必须写 `quant_workflow(operation="journal_append", params={kind="note", tags="watch,<时点>"})`**，哪怕结论是「无异常」。19:30 对账靠 journal 追跨日止损，不写等于这个时点没发生过——2026-10-30 的 10:30 与 13:30 两次巡检都没有留痕，对账时被记为缺口。

`quant_query(resource="pools")` 的读法（这一段是硬门槛，别跳过）：

- 先读 `pool_state.phase`。`opening_window`（09:30–10:00）里三池还在逐步加载，`tone` 会返回「未成形」、`gates.checks` 三项为 `null`、`gates.indeterminate` 列出不可判断项。
- 「未成形」不是冰点，`indeterminate` 不是失败。此时不要用 `counts` 判断市场冷热，结构判断改用上一交易日的收盘终值。
- 2026-10-30 的教训：09:41 读到涨停 0 / 跌停 1 / tone 冰点 / gates 三项全 false，收盘是 49 / 86。那份否决理由有两个支柱建立在空表上。
- `gates.failed` 只列**算出来不通过**的项；算不出来的在 `indeterminate` 里，附 `why_indeterminate`。引用 `gates.ok=false` 时要说清是「不通过」还是「不可判断」。

节奏（Asia/Shanghai，工作日）：09:05 人格校准、09:26 开盘决策、09:35 样本轮换、13:35 午间验证、14:35 尾盘建仓、18:50 市场复盘、19:30 收盘对账。**13:35 是唯一走 `quant_query(resource="watch")` 的入口**——10:30 与 13:30 两次巡检自 2026-11-03 起并进这一趟，12:30 的独立验证也并入，代价是破线最晚 13:35 才被**处理**；它被**记下**的时间早得多：抓取循环每帧都看一眼账本，见 `quant_query(resource="live").book_watch`。其余任务直接进 `quant_query(resource="information")` / `quant_query(resource="context")` / `quant_query(resource="facts")`。**要加密盘中巡检，加的是 `quant_query(resource="live")` 时点，不是 `quant_query(resource="watch")`**：前者读一帧、毫秒级，后者要一只只取实时报价；把 `quant_query(resource="watch")` 提到每 15 分钟一次，等于全天对行情源做压力测试。两个都调的话，先 `quant_query(resource="live")` 判断这一趟值不值得，再决定要不要 `quant_query(resource="watch")`。**18:50 只读市场、不读自家报告，19:30 只核账不重做结构复盘**——两者分工与降级阶梯见 `docs/12-automation-chain.md`。成交口径分两种，看 `fill_basis`：09:26 的 PRE_OPEN 单按**当日开盘价**结算（`open_auction`，日线落库后可复现），14:35 走 `FILL_MODE=intraday`——用实时报价造 Bar、走同一个撮合器（`intraday`，**不可复现**，所以它在业绩里被单独标注）。引用成交价前先读 `fill_basis`，否则会把一个不可复现的价格当成可复现的。

**上游缺席要当场看见**：定时任务动手前读一次 `quant_query(resource="duty")`，`kind="upstream_missing"` 就是「按时间该在、但不在」的交接物——五份报告，带 `artifact`（相对路径）与 `due_by`（读它的那个任务的时点）。有就按 `docs/12-automation-chain.md` §5 降级，并在输出的「缺口」里点名；**不要用别的数字补它**，也不要因为上游没跑就跳过自己这一步。非交易日不列，没到 `due_by` 也不列——所以它不会在 08:00 抱怨 19:30 的报告。

**日更跑完没有，看 `quant_query(resource="data_health")` 的 `pipeline.state`**（六种，对应六种做法）：

| state | 含义 | 该做什么 |
|---|---|---|
| `done` | 17:30 那次跑完，`gate=ready` | 正常引用当天数据 |
| `done_not_ready` | 跑完了，但有步骤失败 | 读 `failed_steps`；它点名的数据集今天**不可信**，别从里面取数写结论 |
| `running` | 正在跑 | **不要再起一个**，也别引用它还没写完的数字；等它结束再读一次 |
| `missing` | 交易日过了 17:30，没有完成标记 | 没跑，或崩在中途。先看 `data/daily_job.log` 末 20 行，再决定补不补 |
| `pending` | 还没到点 | 什么都不用做（每个上午都是这个） |
| `not_required` | 非交易日 | 没有日更要等 |

`bars_behind=0` 只说明**日线**到了，不说明池子、情绪、派生池也到了——日线先落库，后面几步
后落库，崩在中间时两者会分叉，而这正是「数据看着齐了其实一半」的形状。`ALERT.txt` 只在
失败时存在，所以**没有告警不等于跑过了**；`pipeline` 是那个正面答案。

> **为什么尾盘是 14:35 而不是 14:50**：平台调度器给 8/9/10/14 点档的定时任务加一段确定性抖动，
> 窗口 20 分钟（其余钟点只有 30 秒）。14:50 计划的最坏落点是 15:10，已经收盘——2026-10-30 实测
> 15:09:43 触发，任务如实报「无成交路径」，那是第 4 次。
>
> 14:35 的落点区间是 **14:35–14:55**：最好情况离截止 22 分钟，最坏只剩 2 分钟，而任务自身要跑
> 96–111 秒，属于贴边。所以 prompt 里写死了降级——**开工晚于 14:52 直接判「不买」**，不为凑一笔
> 而跳过检查。要更宽的余量就把计划时刻前移到 14:20（落点 14:20–14:40，最坏仍有 17 分钟）；
> 但**不要往 15:00 方向挪**，那个方向的尽头是收盘之后。

> **9 点档抖动同样存在，而且已经把 09:26 窗口整个吃掉。** `reports/open/` 里 10-30 09:42、
> 11-02 09:40、11-03 09:44、11-04 **09:42:02**、11-05 **09:33:40** 是五次实测；11-05 报告原话
> 「这个窗口从建立以来从未在 09:26 真正跑过」。09:05 的人格切换也落在 09:13:38。
> 现在 09:26 的 prompt 按**实际开工时刻**分叉：`<09:30` 才用 `open` 队列与「已挂单（按开盘价）」；
> `≥09:30` 用真实开盘价算跳空 `x`、按 `quant_query(resource="context").risk.fill_mode=intraday` 实时成交，
> 结论标 `fill_basis=intraday`，**禁止再写「按开盘价挂单」**——19:30 按报告写的口径核账，
> 口径错了会把一张实时成交的单当成开盘排队单。09:05 与 09:26 会抢跑，09:26 遇到
> `tradable=false` 先读 `quant_query(resource="duty")` 判 09:05 是否已跑，别直接判「今天不可交易」收工。

### 一天里谁有权下单

权限按时点切，不按模型意愿切。**同一个动作在同一天只有一个执行者**——写「等会儿再买」等于没买，因为没有第二个执行者会接手。

| 时点（计划） | 任务（automation 名，不含分钟） | 能做 | 不能做 |
|---|---|---|---|
| 09:05 | `盘前 人格袖就绪` | `persona_switch` | 任何下单 |
| 09:26 | `盘前 首窗决策` | BUY + SELL（卖出限昨日及更早持仓；成交口径看 `session.label`，不假定 PRE_OPEN） | 样本账本、换人格 |
| 09:35 | `早盘 样本账本轮换` | `quant_order(book="samples", action="propose")` / `_approve` / `_reject` | 主账户任何操作 |
| 13:35 | `盘中 风控与档位` | SELL（T+1 已解锁、破线才挂单）+ 写报告 | 开新仓、换人格 |
| 14:35 | `尾盘 末窗决策` | BUY + SELL（14:57 前；卖出同样受 T+1 限制） | 样本账本、换人格 |
| 15:25 | `收盘 提醒解读`（新增） | `quant_alert(action="list")` 当日 pending → 三句解读 → 第二封邮件 | 任何下单、改止损、写 journal |
| 18:50 | `盘后 市场结构` | 只读 + 写 `reports/market/` | 任何下单、换人格、写账户盈亏 |
| 19:30 | `盘后 账本对账` | `quant_query(resource="tickets", params={status="open"})` 枚举未结票据 → journal / memo / 次日开盘委托；`pipeline.state="missing"` 时可补跑一次 daily_job（`running` 时会被拒，退出码 3） | 当日成交（已收盘）、结构复盘 |

两条读法，两种错都要付一整趟窗口：

- **时段标签一档一个名字**：`quant_query(resource="market_status").session.label` 依次是 `开盘集合竞价`（09:15–09:30）/ `连续竞价` / `午休` / `收盘集合竞价`（14:57–15:00）/ `已收盘`（15:00–15:05）/ `盘前/盘后`。`in_session` 到 15:05 仍为真——那是为了让 15:01 抓的快照算这一场，**不代表那一刻能按连续竞价成交**。14:57 之后没有撮合，服务端也不拦单，这条边界只能自己守。
- **参数名有一处不统一**：`quant_query(resource="facts")` / `quant_query(resource="sector")` / `quant_query(resource="data_health")` / `quant_query(resource="information")` 要日期时用 `date`，`quant_query(resource="auction")` / `quant_query(resource="lhb")` / `quant_query(resource="limit_structure")` / `quant_query(resource="watch")` 用 `as_of`。传错不是被忽略，而是**整单不发**（工具层报 `unknown argument(s)`，服务端什么也没收到）——照提示词里的写法抄，别自己换算。

八条推论：

1. **09:26 下单就是真的下单**，不是"给个计划"。PRE_OPEN 能成交，所以结论只有两种写法：「已下单」或「不买」。
2. **09:26 先卖后买**：昨天及更早的持仓今天开盘就能卖，所以它要先处理遗留的止损，再谈建仓。顺序反了等于把风险留在账上过一天。
3. **09:26 分两段**：先看隔夜事件（昨日 review 的「明天三条」+ 竞价异动 + 公告）；没有事件再按 5 日窗口选股，门槛比 14:35 更严——今天买的票当天卖不了，错了没有纠错机会。
4. **13:35 只卖不买**。它判断「09:26 的判断还成立吗」：破线才挂单，其余只读并写报告，输出是 14:35 的输入。14:35 之后没有第二个叫醒。
5. **14:35 先看止损再看机会**：09:26 买过的票只判加/持/减/清，不重复买；满 3 只后只能卖。
6. **T+1 决定谁能卖**：当天买的当天卖不了。`quant_query(resource="observations")` 的 `constraints` 里写着「T+1: 今日可卖 X / 持仓 Y」，`X < Y` 就是锁住的——这时不要提议卖出，写「次日 09:26 处理」并记一条 journal，否则第二天没人记得。
7. **18:50 与 19:30 看的是两件事**：18:50 只看市场（结构、情绪阶段、明日三档情景），不读自家报告；19:30 只看自己（说对了什么、明天第一条做什么），不重做结构。结构复盘塞进对账会退化成「只核报告提到过的数」——11-02 之前就是这样，没人回答「今天是什么日子」。
8. **19:30 先枚举票据再核账**：`quant_query(resource="tickets", params={})` 默认给未结的那几张（`proposed` / `queued`）。以前它们谁都不在——`quant_query(resource="orders")` 只有进了内核的委托，`quant_query(resource="portfolio").pending` 只有排队的开盘撮合——所以「提了但没 approve」的票一整天不可见，19:30 只能按 id 抽查，抽不到就当没有。枚举出来的每一张当天必须有个去处：执行、带理由 reject、或在 journal 里写明为什么留到明天。

### 输出纪律：结论优先

所有定时任务的第一行必须是结论。不是"我读了什么"，不是"分析显示"，是结论本身。

| 任务 | 第一行长什么样 |
|---|---|
| 09:05 人格 | 「保持 retail」/「切到 trader」 |
| 09:26 开盘 | 「买 XXX 100 股，已下单」/「止损：卖 XXX，已下单」/「不买」 |
| 09:35 样本 | 「买 XXX 100 股」/「今天不买，因为…」 |
| 13:35 验证 | 「无异常」/「卖 XXX，已下单」/「XXX 止损已破但今日不可卖」/「早盘判断成立」 |
| 14:35 尾盘 | 「买 XXX，价 YYY，已下单」/「不买」 |
| 18:50 复盘 | 「主升：涨停 132 / 跌停 11 / 封板率 0.795」+「明日基准：分歧，须 …」 |
| 19:30 对账 | 「对 X 条、错 Y 条、空仓代价 Z%」 |

第二行一句理由，之后最多 5–6 行依据。**第一行不是结论，这次输出就算没做到。**

"我先看…""分析显示…""综合来看…"都是失败开头——它们把结论推到了第三段，而读到第一段的人已经不在听了。

## 三种人格：谁在花这笔钱

风格选择是显式的，不是散文里的暗示。当前人格决定你能用哪个 sleeve、持多久：

| 人格 | sleeve | 周期 | 什么时候不该用 |
|---|---|---|---|
| `hot_money` 游资 | 只能 AGGRESSIVE | 1–2 日 | 炸板率高（两套门槛：tone 判分歧 **0.475** / `gates` 判能不能开新仓 **0.38**，见 `configs/params.yaml`）、最高板 <3、拿不到实时报价时，历史超额最差——**这是读数，不是禁令** |
| `retail` 散户 | 只能 CORE | 10–60 日 | risk_on 里必然跑输；不许用来"等回本"。**CORE 已于 2026-11-01 关闭，所以这个人格现在下不了任何单**（`tradable=false`） |
| `trader` 交易员 | CORE + AGGRESSIVE | 1–5 日 | 说不清"为什么是现在"、写不出失效条件 |

`hot_money` 的 `evidence` 里没有 `verdict` 了——那个词删了，但它的 `why` 必须读：
里面那些判决测的是 `next_open`（次日开盘接力），而打板是「T 日收盘价买入」——
另一个动作，从未被覆盖。要拿那些判决关闭打板，先用 `entry=t_close` 重跑。
`evidence` 是**带 `as_of` 的静态记录**，现算的战绩在 `scorecard` 里。

`quant_query(resource="persona")` 读当前人格、三者状态、数据是否齐，以及**过去每次切换是否真的赚到了钱**。
换风格用 `quant_workflow(operation="persona_switch")`，四样必填：`reason`、`evidence[]`、`expected_edge_bps`、`invalidation`。
每天上限 2 次。提案里的 `persona` 必须等于当前人格，否则 409 `PERSONA_MISMATCH`。

**盘前先读 `quant_query(resource="persona")` 的 `today` 块**——它把「按行情该用哪个」变成一句话：

```
today.candidates        # 今天能下单的人格，按三道硬筛后的幸存者
today.why_not_others    # 其余三个为什么不：by_data / by_sleeve / by_regime
today.can_place_orders  # false = 今天这个账户下的任何一单都会被拒
```

三道筛是硬事实，不是偏好：**数据齐不齐**（`probe`）、**袖还开着吗**
（`configs/core.json` 的 `retired`）、**regime 对不对口**。
`current.tradable=false` 时 `current.dead_end` 会说清两条失败路径各是什么。

`by_sleeve` 与 `by_regime` 必须分开读：前者是「哪天都不能用」，后者是「今天不合适」。
2026-11-01 之后 CORE 关闭而 `retail` 只持有 CORE，于是默认人格一个袖都用不了——
11-02、11-03 两天的复盘都把「0 提案 0 成交」写成了判断力的结果，没写它其实也买不了。

**这不是"换个人格重开一局"。** 每次切换都带着你当时写下的预期，事后与同期等权市场结算；
没有预期就无法结算，所以接口直接拒绝没有 `expected_edge_bps` 的切换。
`eligibility` 只做三件硬筛，从不替你选——
自动按收益轮换人格是被明确排除的设计，理由见 `docs/08-personas.md`。

## 一次完整决策

```
quant_query(resource="dashboard")               # 先看一眼：今天发生了什么、账户、待办、擂台、调用统计
quant_query(resource="context")                 # 数据是否可用、账户、风险额度
quant_query(resource="observations")            # 约束状态：止损、上限、核心盘偏离、剩余预算（含测算）
quant_query(resource="duty")                    # 待办清单，按 priority
quant_query(resource="knowledge", params={topic=ledger})       # 已测过的结构：spec + 样本 + 数字；判断是你的
quant_query(resource="analog", params={symbol})          # 最像的 300 次之后 1/2/3/4/5/10 日；net 块含扣完成本的净超额
quant_query(resource="knowledge", params={topic=distribution, symbol})  # 尾部、偏度、最大回撤、最长连跌
quant_query(resource="knowledge", params={topic=crowding, symbol})      # 换手分位、离 250 日新高、连涨天数
quant_query(resource="knowledge", params={topic=liquidity, symbol})     # 你的量级要付多少冲击，1% ADV 容量
quant_query(resource="knowledge", params={topic=cycle})  # 情绪状态转移：今天这档炸板率/晋级率，历史上之后 1/3 天怎么走。
                              # 与 regime 分工：regime 看指数，cycle 看情绪结构。
                              # 读 cohort.n_days 之前先读 cohort.n_episodes——
                              # 546 天可能只来自 8 段事件，天数是假样本量。
quant_query(resource="knowledge", params={topic=tape_flow, as_of=今天}) # 昨日涨停今日溢价/晋级率/红盘率。
                              # 参数名是 as_of 不是 date（date 也收，但别赌）
quant_query(resource="knowledge", params={topic=seal_tape, as_of=今天}) # 封单的**过程**：每帧涨停家数、个股封单起止、
                              # 收工前被撤单的名单、中途炸板出池的名单。
                              # 收盘的"涨停 48 家"是结局；从 53 掉到 48 和从 44 回到 48
                              # 是两回事，这条给的是过程。数据从 2026-11-03 起落盘，
                              # 更早的日期会 ok=false 并说明原因（不是当天没涨停）。
                              # frames<2 时 decay_top 为空、erosion 为 None——那是
                              # "没观察过第二次"，不是"纹丝没动"。curve 默认只给 20 帧，
                              # 用 market.pagination.next_offset 作为下一次 offset 续读；
                              # peak/erosion 按全量帧算；告警只给最严重前 10 条，
                              # alerts_total/alerts_truncated 标明全量，不凭一页说全部。
quant_query(resource="knowledge", params={topic=industries})            # 申万一级横截面：涨跌、成交、主力净额
quant_query(resource="knowledge", params={topic=industry_rank, symbol}) # 这只票**当日**在同行业里的排名与龙头。
                              # 与 profile.relative_strength 分工：那个是 20/60 日累计
                              # 超额，这个是当日位置。2026-11-02 分众 20 日超额 +17pp，
                              # 当日却排 25/28——两个结论都对，问的不是同一件事。
quant_query(resource="risk")                    # 组合历史模拟 VaR、集中度、相关性、距阈值
quant_query(resource="filing", params={announcement_id}) # 读公告正文，分段；coverage 说清抽了多少页
quant_query(resource="compare", params={symbols=两个标的})          # 同一窗口并排；报告期不同时财务三项不可比
quant_query(resource="screen") / quant_query(resource="universe") # 选 1-2 个标的（北交所只可看，不可选、不可买）
quant_order(book="paper", action="propose")                 # 出票：thesis / evidence / invalidation
quant_order(book="paper", action="approve", params={confirm=true})   # 只在你确认逻辑仍成立时调用
quant_query(resource="facts")                   # 盘后：今天可引用的数字（涨跌家数、等权/中位收益、可买几只）
quant_workflow(operation="journal_append")          # 盘后复盘——每个数字都要能在 quant_query(resource="facts") 里查到；
                              # 返回 verification 会列出 unmatched，写完先看它
```

**核心盘已于 2026-11-01 关闭**（`configs/core.json` 的 `retired: true`）。不要调
`quant_query(resource="core")` / `quant_query(resource="core_preview")`——它们返回 **409 CORE_RETIRED**。`quant_query(resource="core")`
仍可读：`state=retired`、`executable`（一手金额 / 占净值比 / 需要多少净值）都是有效信息，
用来向用户解释"为什么关"，不是可执行建议。原因：防守腿 511010 一手 14,112 元，是
5,007 元账户的 2.8 倍，每个防守日只能写一条 100% 现金的净值行。要恢复，把 `retired`
改回 false。

**北交所只做信息，不做仓。** 涨停家数、连板高度、情绪标签都把它算进去——它是盘面的一部分；
但 `quant_query(resource="screen")` 的候选里没有它，`quant_query(resource="universe")` 会给它 `tradeable: false`，而
`quant_order(book="paper", action="propose")` 对它的任何买入都会返回 `failed: ["board"]`。看到 `tradeable: false`
就不要出票，那不是"可以试试"的信号，是一条已经写下来的规矩。

**每行的 `board` 和 `band` 都是填好的，直接用，不要从前缀推。** 主板 0.10、创业板/
科创板 0.20、北交所 0.30；**主板 ST 也是 0.10**（交易所 2026-07-06 起把沪深主板 ST/*ST 的
涨跌幅由 5% 调为 10%，与主板普通股统一；创业板/科创板 ST 仍 20%、北交所 *ST 仍 30%）。
判断"+8% 离涨停还有多远"看 `band`；从代码推会在 ST 上错一半——那样算出来的止损距离，
比没有还危险。

**成本是这台子上最容易被忽略的对手。** 5,007 元的账户，一笔 1,500 元的往返：佣金按
最低 5 元收两次（66bps），加印花税 5bps 与双边滑点 20bps，合计约 **92bps**。而 ledger 里
每条被判死的结构，判据是扣掉 **20bps** 之后还剩不剩——同样的历史超额，在你这个规模上
比在研究里少活 72bps。`quant_query(resource="analog")` 的 `net` 块直接给扣完成本的净超额与 `survives_cost`；
`survives_cost=false` 就不要拿毛超额当理由。
最低佣金在**单笔 2 万元**才失去支配地位（5 ÷ 0.00025）：到那时佣金回到 2.5bps/边，
往返约 30bps。这个账户单笔上限 2,500 元，差着 8 倍——所以在这个规模上，任何靠
频繁小额交易兑现的 edge 都不成立，「少做、做大一点」比「多做几笔」便宜，不是纪律
而是算术。

**内核不会替你交易。** 没有代码写的止损、没有自动再平衡、没有定时调仓。止损只是状态：跌破时系统告诉你距离、连续几日、次日开盘退出的成本，然后停下来等你。`POST /core/execute` 会拒绝由规则生成的计划——照抄权重可以，但必须是你提交的。

读核心盘计划时看三个字段：`skipped_unaffordable`（目标权重有、但一手买不起，钱留在现金）、`rounding_gaps`（选中了、目标金额却小于一手，于是 0 股）和 `effective_positions`（实际持有几只）。在 ¥5,000 的账户上，一个 55% 的风险目标经常只能落地成 20% 的仓位——这不是失败，是整手约束，但你必须知道它发生了，而不是把 cash_weight 当成策略观点。

## 怎么读这些信息

### 先看一只票本身：`quant_query(resource="profile")`

谈单票之前先调它。九个块，每块都带一个日期：

| 块 | 里面有什么 | 什么时候最要紧 |
|---|---|---|
| `identity` | 行业（厂商标签 + **申万 L1/L2/L3**）、`comparison_group`（下面比较用的是哪一级）、板块、ST、上市天数、曾用名 | 判断"次新/老票"与涨跌停幅度（创业板 20%、北交所 30%、主板 ST 10%）；`comparison_group.skipped` 说明更细的那一级为什么没用 |
| `tradability` | 涨跌停价与距离、一手多少股、**最小金额** | 5,000 元账户买不了 100 股 × 135 元的票，看一眼就知道 |
| `valuation` | PE/PB/PS/股息率 + **窗口内分位**、市值、换手 | 分位是这只票跟自己的历史比，不是跟行业比 |
| `fundamentals` | 最新已公告财报（按 ann_date 过滤）+ 近几季趋势 | 季报刚出时会有跳变；没公告的季度不会出现 |
| `flow` | 主力净额 1/5/20 日、两融余额与净买入 | 价格涨但主力净流出，是另一回事 |
| `technicals` | 5/20/60/120/250 日收益、MA20/60/200 偏离、波动率、250 日区间位置、ATR | **全部用前复权价**，末端等于当日收盘 |
| `relative_strength` | 20/60 日：本票 / 同行业等权 / 全市场等权，以及两个超额 | **`vs_industry_pp` 才是选股那部分**；区间涨幅里有多少是行业给的，看这里 |
| `sentiment` | 今日是否在涨停/炸板/跌停池、20 日龙虎榜、最近异动，附 `checked` 读了多少个交易日 | `lhb_20d: null` 分两种：真没上榜，还是分区没抓到——看 `checked.lhb_sessions_read` |
| `events` | 分红除权日/派现、**巨潮公告**（标题+四类标签+PDF+正文摘要）、最近新闻 | `announcements` 才是公司公告；`recent_news` 不是——看 `scope`。四个标签都是标题关键词推的，判重大要看 `text.excerpt` |
| `intraday` | 当日 5 分钟线的形状：前 30 分钟成交量占比、收盘相对 VWAP、自日内高点的回撤、止损是否在盘中被打穿 | 全市场每天盘后入库（baostock），所以通常直接 `stored`。早于开始入库的日子、或者北交所（没有 baostock 代码），会**当场补一次**（约 1 秒）；补不到时说清是哪一种：`provider_has_no_<day>` 是能力边界，不是当天没交易。`vwap` 为空要看 `vwap_note`（腾讯源没有成交额） |
| `position` | 是否持有、成本、权重、止损价与距离 | 有仓位时先看这块再看别的 |

**读法**：`missing` 列出当前拿不到的块，每块自带 `reason`。`null` 表示"没有这个数"，不是 0 —— 估值分位是 `null` 时不要当"最便宜"。

### 一页比你要的短：`capped` 与 `_shrink`

两种"短"不一样，都写在答案里，别把一页当全部：

- **`capped`**：工具层把行数夹到能塞进 16 KB 的上界。`asked_limit` 是你要的、`served_limit` 是给的，`why` 说明为什么。看到它就换手段（用 `q` 关键词、`as_of` 换一天、`symbol` 收窄），**不要原地加大 `limit`**——那只会继续被夹住。
- **`_shrink`**：宿主渲染时超预算，先压字、必要时丢行。`level` 与 `dropped` 会写清楚动了什么；时间序列留末尾、排行留头部。

宿主对超过 16 KB 的响应是**整包丢弃**，不是截断——所以"没数据"和"要得太多"在模型这边长得一样。工具层的上界就是为这件事设的：`quant_query(resource="pools")` 20、`quant_query(resource="universe")` 40、`quant_query(resource="tickets")` 15、`quant_query(resource="auction")` 40、`quant_query(resource="anomaly")` 8、`quant_query(resource="memory_search")` 12（`full=true` 时 6）、`quant_query(resource="execution_tca")` 60。

**两种"没有"要分开**：`missing` 是"算了但没算出来"（数据缺），`omitted_sections` 是
"装不下所以没给"（有，只是没在这一份里）。后者配一条 `size_note`。十三块加起来 16.7 KB，
超过单次响应上限，所以默认只给装得下的那些——**看到 `omitted_sections` 就说明还有东西，
用 `quant_query(resource="profile", params={symbol, sections=[...]})` 取回来，不要把"没给"读成"没有"。**

三个容易读反的地方：

- **分位有方向**：`valuation.direction` 写明每个指标"越高越贵"还是"越高越便宜"。PE 在 92 分位是贵，股息率在 3 分位**也是贵**——方向相反，不要只看数字大小。
- **财报是累计**：`fundamentals.basis` 写明"年初至今累计"，`trend[].roe_ytd` 每年前三个月会重置。把 9.39 → 2.72 读成业绩崩塌是错的。
- **`peers.comparables` 是可比公司，不是推荐**：同一比较组内总市值最接近的 4 只，附各自的 PE/PB/涨跌。
- **中位数有归属**：`peers.taxonomy` / `peers.level` / `peers.peer_count` 说明这组数是在哪个集合上算的。`stock_basic.industry` 把 42 家银行算一类，申万 L3 拆成国有大型（6）/ 股份制（9）/ 城商行（17）/ 农商行——中位数因此会不一样，别跨组比较。`identity.comparison_group.skipped` 会写"国有大型银行Ⅲ 只有 6 只，样本太少未采用"。

### 复盘写进去之前，数字先过一遍

`quant_workflow(operation="journal_append")` 返回里有一个 `verification`：

| 字段 | 说的是什么 |
|---|---|
| `checked` / `matched` | 文本里抽到几个数、其中几个在当天 facts 里查得到 |
| `unmatched` | 查不到的。**不等于编的**——账户、时间、「我买了 3 只」这类数本来就不在 facts 里 |
| `sign_from_words` | 数对上了，但方向只在文字里（跌 3.04% 对 facts 里的 -0.030363）。符号没核 |
| `skipped` | 当天没有 facts 快照，**没法核对**——不是通过 |

它不拦写入。理由很实际：拦会教模型把数字删掉，而复盘的价値一半在数字上。
但日志会被**以后的会话读回来**（`quant_query(resource="memory_search")`），错的数字在这里不是一次性的，
它会变成记忆然后被再次引用——所以写的时候至少要看见它。

### 跨日交接：上一轮写了什么

复盘是写给**下一个会话**的，不是写给当天的人看的。所以读它要按「哪天、哪一类」取，
而不是翻最新 20 条——note 的产出速度是 review 的 4 倍，翻着翻着窗口里就全是巡检记录。

| 我要回答 | 怎么问 |
|---|---|
| 昨晚那条复盘的结论与待改项 | `quant_query(resource="journal_search", params={kind="review", limit=2})` |
| 今天写过复盘了吗 | `quant_query(resource="journal_search", params={kind="review", since=today, until=today})` |
| 某条复盘的原文 | `quant_query(resource="journal_search", params={id=28})` — 单条全文，约 2.7 KB |
| 复盘引用的长文（表格、明细） | `quant_query(resource="reports", params={path="review/20261103.md"})` |

摘要默认是**首 100 字 + 末 160 字**：开头是战果，结尾是「下次改一条」。两条都要，
因为盘前真正决定行为的只有后面那句。

`reports/` 的正文是分页的（一页 6,000 字，因为一份复盘 14.4 KB，超过宿主 16 KB 上限会被
**整包丢弃**而不是截断）。返回 `truncated=true` 时按 `next_offset` 取下一页，别以为
报告就写到那儿。

### 读旧结论时，先看 `verified`

上面那个结论不会丢：它跟着条目一起回来，`quant_query(resource="journal_search")` 与
`quant_query(resource="memory_search")` 的每一行都有。

| `verified` 的值 | 说的是什么 |
|---|---|
| `{checked, matched, unmatched_count}` | 写入时核过：`checked` 个数字里 `matched` 对上了、`unmatched_count` 对不上 |
| `{checked: 0, …}` | 核过了，但文本里没有数字 |
| `{skipped: true}` | 有数字，那天没有 facts，**没核成** |
| `null` | **从来没核过**（写在机制之前，或那天没有快照）——不是"核过没问题" |
| `basis: "intraday"` | 写这条时**当天日线还没落库**：核对用的是写入当刻的快照，而笔记记的是一条时间线，两者对不上是结构决定的。**不要为它补写解释**，19:30 会重核 |
| `basis: "close"` | 收盘后写的，`unmatched` 就是真没对上 |

里面的 `claims` 是**带标签**的那部分，它比"查不到"更有用：

| `claims` 的键 | 说的是什么 |
|---|---|
| `ok` / `labelled` | 带标签的断言里，几个对得上 |
| `misplaced` | **数是真的，标签错了**：写"893 家跌停"，跌停其实是 93，893 是上涨家数 |
| `variant` | 同一个数的另一个口径（东财 47 家 vs 同花顺 49 家），写明来源就行 |
| `unbacked` | 这个标签下没有这个数："可买的 6 只"而当天上限内 26、收盘可买 0 |
| `provisional` | 不判但照报：盘中读数 / 我那几只 / 别的交易日 / 引用别的报告 / 变化量 / N 日累计 |
| `scope: market_only` | 那天只剩行情补得回来：**标签核过，数字没核**（`verified` 里没有 `checked`） |

**盘中写的笔记（`basis: intraday`）不要拿 `unmatched` 当错误处理。** 2026-11-04
实测：13:30 那条巡检 14 个数里 12 个"未匹配"，全是盘中值（11:29 的涨停 31 家、
13:25 的 36 家），而 facts 只有一个时点。那次模型又写了一条 journal 去逐条解释——
一天四条盘中笔记就要写四条解释，而它们没有一条推进判断。判不了就说判不了，
`basis_note` 已经写清了什么时候能判。

页级 `unchecked: n` 数的是"**数字**没核过"的条数——`scope: market_only` 的行也算在里面。
这些条目在引用数字之前先 `quant_query(resource="facts")` 重核一遍：一条三天前的复盘引用"当日涨停 49 家"，
那是三天前的 49，不是今天的。

### 公告与新闻是两件事

`events.announcements` 来自巨潮资讯网，是**公司公告**（标题 + 四类标签 + PDF 链接 + 正文摘要）。
`events.recent_news` 是上游打的概念标签，`scope=company` 才勉强算本票相关。

**摘要只截了 320 字，判重大要读正文：`quant_query(resource="filing", params={announcement_id=…})`。**
它按段返回，一次约 3,000–4,000 字（按字节收口，中文密的一页装得少），
`next_offset` 是唯一的续读依据——不要用 `offset + chars` 自己算，窗口在句读处收口、长度不固定。
两个必须先看的字段：

| 字段 | 为什么 |
|---|---|
| `coverage` | 写 `12 / 187 页` 就说明**后面 175 页的内容这里没有**。年报里"营业收入 1,2xx 亿"可能在附注，别下"没有披露"的结论 |
| `done` / `remaining` | `done:false` 时你手上只是第一页；判重大前先看 `pages_at_this_size` 值不值得读完 |

正文是 PDF 抽取的纯文本，**表格会塌成一行行数字**：金额、股数、比例要回 `pdf_url` 核，
或者以 `fundamentals` 里的结构化字段为准。

四条标签是两个正交的问题，不要只看一条：

| 字段 | 回答什么 | 取值 |
|---|---|---|
| `category` | 这份公告说的是**什么事** | 回购 / 减持 / 募集资金 / 诉讼 …（26 个桶，`其他` 约 10%） |
| `kind` | 是**什么体裁** | 公告 / 决议 / 意见 / 通知 / 报告 / 说明 / 函件 |
| `stage` | 这件事走到**哪一步** | 计划 / 进展 / 完成 / 终止 / 调整 / 更正，**多数为 null**（标题没写） |
| `paperwork` | 是否**不带来新事实** | true = 议程、章程、意见书、摘要这类文件 |

`basis` 给出每个标签实际命中的那个词（`{"category":"回购","stage":"进展",…}`），
标签看着不对时先看它——规则是关键词，不是官方分类。

`stage` 是这几条里最省钱的一条：`减持计划` 与 `减持计划实施完毕` 同一个 `category`、
相反的结论，`stage` 一个 `计划` 一个 `完成`。`paperwork=true` 约占 17%，
多是股东会通知与章程修订——事件本身在别处，这类文件可以先跳过。
巨潮只给日期不给时刻，所以当天发布的公告可能收盘后才到——拿它做盘中回放会多看几小时。

### 两套价格口径，不能混

- `close`：真实成交价。挂单、限价、止损、成本、涨跌停距离——全用它。
- `adj_close`：前复权价，末端等于当日 `close`。算收益、均线、区间位置、波动率、相关性——全用它。

用未复权价算跨除权日的收益，会把分红记成亏损。实测：600519 的 250 日收益，未复权 −8.69%，复权 −4.94%；全市场 200 日窗口有 20% 的票偏差超过 2%，2.1% 的票涨跌方向相反。

- `analog`：先看 `match.quality`（loose 就当没查到），再看 `excess_ci95`（跨零就是不知道），再看 `n_days`（300 个匹配可能只来自 48 个交易日）。**`excess_mean` / `win_rate` 是按笔算的，会被邻居密集出现的那几天主导，可能和 `excess_ci95` 符号相反——超额只引 `excess_day_mean` 与 `excess_ci95`。**
- `risk`：VaR 是账户口径、含现金，低仓位时很小；看持仓风险要除以 `invested_weight`。
- `liquidity`：参与率低于 1% 时冲击可以忽略，超过 10% 单日成交就不现实。
- `ledger`：**读数表，不是判决表。** 每行给 spec（买了什么、卖了什么、什么池子、多少成本）与数字，`gate_reading` 是预注册阈值的机械读数——换一套阈值会得到另一个词，同一个数不变。用法：把你打算做的和它的 spec 逐项比，四项里任何一项不同就是另一个实验、这些数字不适用；四项都一样就该预期同样的结果。判断完写进 journal，带上你凭什么这么判。

## 样本账本：第二个账本，与主账户无关

主账户按 regime 走，`retail` 的 10–60 日周期意味着 20 笔平仓要 1–5 年。这个账本把证伪周期压到一个月：
独立 2,000 本金、独立 `data/samples.sqlite`、独立风控（**1 只持仓、满仓、每天买一卖一**）。

| 工具 | 用途 |
|---|---|
| `quant_query(resource="samples")` | 账户、已结算笔数、距 20 笔还差多少、最近记录 |
| `quant_order(book="samples", action="propose")` | 提案（字段同 `quant_order(book="paper", action="propose")`） |
| `quant_order(book="samples", action="approve")` | 确认，重跑该账本自己的 guard |
| `quant_order(book="samples", action="reject")` | 拒绝并写理由 |

四条纪律：

1. **一笔完整的买入→卖出才算 1 笔结算**，只有 `directed_by=agent` 的计入。让它挂着不算。
2. **这里允许亏损。** KPI 是结算笔数，不是收益率；亏掉的钱是研究成本，不是业绩。
3. **不要为了凑数买一只说不清的票。** 空仓一天，好过一笔垃圾样本——样本一旦没有 thesis，就没有证伪价值。
4. **只在样本账本里下单**，不要用主账户的 `quant_order(book="paper", action="propose")` 去凑这个数。

主账户的 `arena`、`portfolio`、净值**都不读这个账本**：一个 2,000 元的一日轮换混进自由裁量账本，两边都读不懂了。

## 什么时候不交易

- 数据 `core_usable=false`、`aggressive_usable=false`（做游资时）、`kill_switch=true`。前两项是**数据可用性**，不是准入。
- 当天已亏到 -6%（禁止开新仓）或 -10%（只减仓）。
- 账户回撤 -8%（仓位减半）、-12%（停止开新仓）。
- 有持仓跌破止损（此时新开仓会被 409 拒绝）——注意是**新开仓**被拒，不是系统替你卖；卖不卖仍然由你决定。
- 找不到能说清“为什么是现在”的标的。**空仓是合法决策。**

## 提案写法

好的提案（可证伪、有证据、有时效）：

```
symbol: 600000.SH, side: BUY, sleeve: CORE
thesis: 银行板块 20 日动量转正、估值处于近三年低位，核心仓从现金回补到 20%。
evidence: ["daily:20261023", "lineage:20261023", "screen:20261023"]
invalidation: 收盘跌破 9.24（-3%）或数据被隔离，则逻辑失效。
```

坏的提案（会被拒绝）：

- “感觉要涨”“主力进场了” —— 没有证据 id。
- “买 500 股，限价 9.5” —— 股数可以（`qty=500`），限价不属于你。
- “跌了就拿着” —— 没有失效条件。

## 游资仓：哪些是硬的，哪些是读数

这一段原来写的是「`sleeve=AGGRESSIVE` 只在以下三条同时成立时才提」，读起来像闸门。
实际上只有一部分是闸门，其余是判断依据——**代码不校验 `tone`，也不会因为它拒单**。
把判断写成规则，结果是读者要么高估它（以为会被拒），要么低估它（以为纯装饰）。

**硬的（超了 409，说话的不是你）：** `sleeve_cap`（`configs/risk.yaml` 的
`max_aggressive_weight`）、单票上限、行业集中度、T+1 同进同出、印花税与佣金。
提案还必须带 `invalidation`——没有失效条件会被拒。但「次日竞价不及预期就走」是策略，
不是校验：系统不会替你判断「不及预期」。

**读数（判断依据，给数不给命令）：**

1. `tone` 六档：未成形 / 冰点 / 退潮 / 分歧 / 修复 / 主升。它和 `gates.ok` 用**两套阈值**：
   tone 判「分歧」看炸板率 > 47.5%，`gates` 判「能不能开新仓」看 > 38%。
   两者可能同时给出相反答案——2026-11-03 实测 0.4146 就是 `tone=主升` 而
   `gates.ok=false`。它们回答的是两个问题：「市场强不强」与「现在开新仓有没有结构优势」。
   引用其中一个时说明你在回答哪个。
   注意 `phase=opening_window` 时 `tone` 恒为「未成形」——「tone 不在冰点/退潮」与
   「phase 不是 opening_window」不是两条判据，是同一条。
2. `pool_state.phase`：`opening_window`（09:30–10:00）里三池还在陆续加载，那时的
   封板率与连板高度是快照噪声。这个数不是「低」也不是「高」，是「还没到能判的时候」。
3. 身位与封板质量：连板高度、首次封板时间、封单/流通市值。
   `setup_thresholds` 给每个身位的历史回封率与 `gap_pp`——读之前先分清
   `buyable` 组与一字组（一字从未跌破涨停价，限价单排不到队，它们的回封率
   按构造是 100%）。

三条读数都指向「可以更激进」时，那是一个理由，不是许可；都不指向时也不是禁令。
**提不提、提多少，你决定，并在 journal 里写下你依据了哪几条、忽略了哪几条。**

## 复盘模板（`quant_workflow(operation="journal_append")`）

```
kind=review, symbol=..., tags=discipline
- 前提：账户今天能不能下单（quant_query(resource="duty") 里有没有 persona_dead_end / stale_book / blocked）
- 计划：
- 实际：
- 偏差原因（数据/执行/情绪）：
- 下次改一条：
```

**今天决定「不做」，有两处要留痕，它们回答不同的问题：**

1. `quant_workflow(operation="declined_add", params={})` —— **你差一点就买的那几只**，加上理由和期限。
   到期后 `action="score"` 给出：如果你买了，它们在持有窗口里的收益、同期等权
   市场的收益、以及两者的差。`mean_excess` 为负 = 那些票跑输了市场 = 那次不买
   是对的；为正 = 克制在那一次是成本。**请求里没有价格字段**——成交价与结算价由
   结算自己从日线读，一条"我不买"不能事后挑价格来自证。
   没到期的行一直是 `pending`，那不是失败，是还没到能判的时候。
2. `quant_query(resource="memo")` —— 一条带概率的**信念**（"这波情绪三天内退"），到期用 Brier 结算。

两处都写，才算把一个「不做」记录完整：`declined` 说结果，`memo` 说信念。
只写散文的后果已经发生过一次——11-01 到 11-03 三天零成交、零记录，等于三天
判断力没有任何证据。

**读 calibration 只看 `bss`。** 它是技巧分 `1 − brier/brier_climatology`：
0 = 和「只会报基础率」一样好，负数 = 更差，1 = 完美。

本账实测（n=7）：`brier` 0.3008，`base_rate` 0.5714（4/7 次发生），
`brier_climatology` 0.2449，**`bss` −0.2282**——比什么都不知道差 23%。
分档看两档都反向（押 0.30–0.50 兑现 0.80，押 0.50–0.70 兑现 0.00）。

**别拿 0.5 当基准。** 那是 `s=0.5` 的特例，也是文献里最弱的一条参照；
样本不均衡时它会让你显得比实际好。也别把"基准减 Brier"当成技巧分——那是差值，
不是 `BSS`，同一组数据差值是 −0.05，听起来像"略有不如"。

**「前提」这一行不是形式。** 2026-11-01 起 CORE 袖关闭，默认人格 `retail` 只剩
`("CORE",)`，于是一个袖都用不了——`propose(sleeve=AGGRESSIVE)` 拿到 400
`PERSONA_SLEEVE`，`sleeve=CORE` 拿到 409 `CORE_RETIRED`。而 11-02、11-03 两天的复盘
都把「0 提案 0 成交」写成了判断力的结果（"09:26 判不买"），没写它其实也买不了。
两件事同时为真，但只记一件会让复盘给自己发一枚它没挣到的勋章。

`quant_query(resource="duty")` 会把这件事放在 priority 2，盘前读一次，写进「前提」。

## 故障处理

| 现象 | 处理 |
|---|---|
| 工具报 `quant service unreachable` | 服务没启动；告诉用户运行 runbook 的 uvicorn 命令，不要自己造数据 |
| `unknown argument(s) for quant_query(resource="auction"): limt. legal: as_of, limit` | 参数名拼错，或这个工具没有这个字段。**什么都没发出去**：照 `legal:` 里的名字改名重提。以前这类字段会被静默丢掉、按默认值执行，答案看起来正常但答的是另一个问题 |
| `422 Extra inputs are not permitted` | 服务端自己的校验；`qty` 需为正整数且不能与 `target_weight` 同给。价格字段（`price` / `limit_price` / `stop_price`）在提案工具上永远由服务端算，传了会被 guard 直接拒 |
| `409 ALREADY_FILLED` | 票据已成交；不要再点 |
| `409 EXECUTION_EXISTS` | 这一笔已经记账；先去 `quant_query(resource="orders")` / `quant_query(resource="ticket")` 确认，**不要重复提交** |
| `409 STATE_CONFLICT` | 账本版本在你读完之后变了，**这次没有写入**；直接重试同一请求 |
| `409 STORE_BUSY` | 另一个写入者占着账本，**这次没有写入**；等一两秒重试同一请求，别改参数也别换标的 |
| 数据 `quarantine` | 该数据集当天不可用；停止相关交易并报告 |
| `daily_job` 退出码 3 / `another close run is in progress` | 另一次日更正持着锁（`data/daily_job.lock`）。**没有写入任何分区**；等它结束，不要连着重试——两次运行合并同一分区会丢掉先跑那次的行 |

工具报错时先看 `code`，再看 `next`：`next` 是服务端写给你的下一步动作（它知道这笔写没写进去），
`retry_after_seconds` 是等待时间。**照 `next` 做，不要照直觉做**——`EXECUTION_EXISTS` 与
`STATE_CONFLICT` 都是 409，但一个叫你别重提、一个叫你直接重提，读错就是把同一笔钱承诺两次。
提案超时重试是安全的：插件为同一笔交易复用同一个幂等句柄，五分钟内重试拿到的是**同一张票**；
journal / memo 的写入没有幂等键，重试前先读一遍。
