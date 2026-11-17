# 自动化任务链路（11 个任务，一条闭环）

写给下一次改提示词的人。这里只回答一件事：**每个任务凭什么存在、它读谁的、谁读它的**。
规则细节在各任务自己的提示词里，本文不重复。

## 1. 一条链，不是七个孤岛

每个任务只回答一个问题。问题重复 = 两个执行者在同一件事上给出不同答案；问题缺失 = 那件事没人做。

| 时点 | 任务 | 只回答一个问题 | 读 | 写 | 权限 |
|---|---|---|---|---|---|
| 09:05 | 人格校准 | 今天该由谁花这笔钱 | `quant_query(resource="context")` `quant_query(resource="persona")` `quant_query(resource="duty")` `quant_query(resource="risk")` `quant_query(resource="exposure")`；`quant_query(resource="regime")` 作风格判据 | 无文件，结论即输出 | `persona_switch` |
| 09:24 | **短线α入场（Hy4）** | 今天正当 T+1 的冻结权重如何进入模拟账户 | `quant_query(resource="short_alpha_execution")`、冻结 thesis/evidence/invalidation、会话/账户/报价 | entry 票据与账本 | 仅 `short_alpha` BUY |
| 09:26 | 开盘决策 | 开盘这一下买不买、卖不卖 | `review(D-1)` 明日三条、`market(D-1)` 情景、`persona`、`portfolio.pending`、`observations`、`information`、`auction(D-1)`、`stops(external)` | `reports/open/D.md` | BUY + SELL（PRE_OPEN，按当日开盘价结算，见 2.5） |
| 09:35 | 样本轮换 | 样本账本今天那一笔是什么 | `quant_query(resource="samples")` | 无（账本自己记） | `samples_*` |
| 13:35 | 午间验证 | 早上的判断还成不成立、有没有破线 | `quant_query(resource="stops")` `quant_query(resource="orders")` `quant_query(resource="portfolio")` `quant_query(resource="watch", params={since=D-1})` `quant_query(resource="pools")`、`open(D)`、`market(D-1)` 三档 | `reports/noon/D.md` + journal | 只卖（破线才挂单） |
| 14:28 | **短线α退出（Hy4）** | `exit_now` 或最迟 T+2 的持仓如何退出 | `quant_query(resource="short_alpha_execution")`、最新证据、`quant_workflow(operation="short_alpha_review")`、报价 | review、exit 票据与账本 | 仅 `short_alpha` SELL |
| 14:30 | 尾盘建仓 | 收盘前这一笔做不做 | `open(D)`、`noon(D)`、`market(D)`、当日 journal、`observations`、`stops(external)`、`information` | `reports/close/D.md` | BUY + SELL（14:57 硬边界，`intraday` 成交） |
| 18:50 | **市场复盘** | 今天是什么日子、明天怎么走 | `facts` `regime` `pools` `limit_structure` `sector` `lhb` `auction` `anomaly` `knowledge(news)` + 新闻 | `reports/market/D.md` | **只读** |
| 19:10 | **短线α判断（Hy4）** | 最新完整日线如何冻结 candidate/watch 与权重，已有持仓如何复评 | `quant_query(resource="short_alpha_tracking")` `quant_query(resource="short_alpha_evidence")` `quant_query(resource="short_alpha_execution")` | `data/short_alpha_oos/D.json`、reviews、必要的 queued exit 票据 | judge/review + `short_alpha` SELL |
| 19:30 | 收盘对账 | 今天说对了几句、明天第一条是什么 | `open/noon/close(D)`、`market(D)`、`facts`、`portfolio`、`journal(D)`、`stops(external)` | `reports/review/D.md` + journal `kind=review` | journal / memo / 次日委托 |

> 2026-11-03 把 10:30 与 13:30 两次巡检合并进 13:35，12:30 的独立验证也并进同一趟：
> 巡检连续 12 条「无异常」，而午间验证产出过真实结论。代价是**破线最晚 13:35 才发现**。

两个新任务的分工是本文的重点：**18:50 看市场，19:30 看自己**。

- 18:50 不读 `open/noon/close/review`。读了就会把「报告里写的」当成「市场发生的」，
  而这份报告存在的意义正是独立于自家叙事。
- 19:30 不重做结构复盘。它只需要两样：报告里写过的那些数对不对、明天开盘第一条做什么。
- 19:30 要抽 3 个数（涨停 / 跌停 / 封板率 / 最高板任选三个）与 `facts` 对一遍，
  这是复盘报告唯一的外部校验——没有它，一份编造家数的复盘不会被任何环节发现。

## 2. 交接物只有五份 + 一本 journal

五份报告 + 一本 journal，传递关系是线性的，不是网状：

| 序 | 任务 | 读 | 写 |
|---|---|---|---|
| 1 | 09:05 人格校准 | `market(D-1)` 只作印证 | 无文件（`persona.current`） |
| 2 | 09:26 开盘决策 | `review(D-1)` 的三条 + 一条改动、`market(D-1)` 的档位 | `open(D)` |
| 3 | 13:35 午间验证 | `open(D)`、`market(D-1)` 的档位、`journal` | `noon(D)` |
| 4 | 14:30 尾盘建仓 | `open(D)`、`noon(D)`、journal(13:35) | `close(D)` |
| 5 | 18:50 市场复盘 | **只有工具与新闻**，不读当天任何报告 | `market(D)` |
| 6 | 19:30 收盘对账 | `open/noon/close(D)`、`market(D)`、journal | `review(D)` + 次日委托 |

一条闭环：`market(D) → review(D) → open(D+1) → noon(D+1) → close(D+1) → market(D+1)`。
每一环只读上一环的**结论**，不重读它的推理过程——重读会让下游把上游的措辞当成新证据。

三条跨日通道，缺一条就有信息永久丢失：

1. **`review(D)` 的「明日三条」**——09:26 唯一权威的隔夜指令。第 1 条永远是遗留止损。
2. **`review(D)` 的「一条改动」**——写成可执行指令（改哪条规则、参数多少），
   次日 09:26 与 14:30 读它并执行，在报告里写明「按 D-1 改动执行」。只写感想等于没改。
3. **journal 的巡检留痕**——`quant_query(resource="watch")` 的结论不落在任何文件里，
   而 19:30 只认 journal。10-30 与 11-02 连续两次巡检没留痕，持仓为 0 时无损失，
   有持仓时会把止损漏到第三天。

## 2.5 单子什么时候成交（五种 basis，别混）

| 下单时点 | 队列 | 成交价 | 落库时机 | `fill_basis` |
|---|---|---|---|---|
| 09:26（PRE_OPEN） | `open` | **当日开盘价**；跳空越过限价则不成交 | 当日 17:30 日线落库后 | `open_auction` |
| 09:30–14:57（连续竞价） | 无 | 实时报价（含滑点，不穿透限价） | 立即 | `intraday` |
| 14:57–15:00（收盘竞价） | 无 | 当前报价 | 立即 | `intraday` |
| 19:30（盘后） | `next_open` | **次日开盘价** | 次日首次调用时 | `next_open` |
| 打板（`limit_queue`） | `ceiling` | 当日涨停价 × 排队比例 | 当日 17:30 后 | `ceiling_queue` |
| `close` 模式任一时点 | `close` | **当日收盘价**；限价站在收盘价错误一侧则取消，不按限价改价 | 当日 17:30 日线落库后 | `close` |

两条推论，改提示词时必须遵守：

1. **09:26 的结论不能写「已成交」**，只能写「已挂单（按开盘价）」——开盘价 09:30 才有，
   账本收盘后才更新。14:30 读 `open(D)` 看到的是「挂单」而不是「持仓」，
   `quant_query(resource="portfolio").pending` 才是真相。
2. **`open`、`ceiling`、`close` 都只赌那一天**：`expires_day = created_day`，所以 `_rollover` 必须
   先跑 `settle_same_session_due`、再跑 `settle_pending`；反了就是在结算之前把单子当
   过期取消（2026-11-04 修的就是这个顺序，打板与盘前单都受影响）。

## 3. 为什么复盘是 18:50

数据时序决定，不是审美：

| 时刻 | 事件 |
|---|---|
| 09:26 | `auction-0926` 完整采集竞价并冻结 `ths_auction_freeze`；冻结分区不可覆盖 |
| 15:20 | `ths-close` 抓同花顺 T1（`ths_anomaly` 仍为 TODAY_ONLY；`ths_auction` 可回补滚动一年历史） |
| 17:30 | `daily_job` 主链路：日线、复权、三池、日报 |
| **18:50** | **市场复盘**（17:30 后 80 分钟，覆盖 10-29 那种 18:39 才补上的迟到） |
| 19:30 | 收盘对账（读复盘，留 40 分钟） |
| 21:40 | `late` 补 `moneyflow` / `moneyflow_hsgt` |
| 次日 08:50 | `morning` 补 T+1 两融 |

代价必须写进报告：18:50 时**当日 `moneyflow` 还没入库**，`quant_query(resource="sector")` 的
`flow_as_of` 通常是 T-1。所以资金流一律标注日期，不得当作今日资金；
游资行为不看资金流，看封单强度、首封时间、晋级率（`quant_query(resource="limit_structure")`）。

没人愿意为了资金流把复盘挪到 22:00：那会让 19:30 的对账拿不到复盘，
「明日三条」就退回今天这种「结构复盘顺带核账」的混合体——那正是这次要拆掉的东西。

### 3.1 「17:30 跑完了」是一个可以读的事实

`bars_behind=0` 回答的是「日线到了没有」。它不等于「日更跑完了」：日线是 17:30 那次
的第一批，池子、情绪、派生池在后面几步，崩在中间时每一个数据集的 `cover_end` 都可能
是对的，而当天的结构面板还停在昨天。**「数据看着齐了其实一半」就是这个形状。**

两种标记，回答两个问题：

| 文件 | 什么时候有 | 能回答什么 |
|---|---|---|
| `data/ALERT.txt` | 只有 `gate=not_ready` 时 | 失败了，原因在这 |
| `data/daily_done/<day>.json` | **每次跑到最后都有**（写完报告、写完或清掉 ALERT 之后） | 跑完了，以及 `gate` 与 `failed_steps` |

只读第一个的话，文件不存在既是「跑干净了」也是「根本没跑」——2026-10-29 是后者，
而当天照样出了一份收盘复盘。所以 `quant_query(resource="data_health")` 的 `pipeline.state` 是那个正面答案：
`done` / `done_not_ready` / `running` / `missing` / `pending` / `not_required`。

补跑之所以要小心，是因为两个 `daily_job` 同时写同一批分区会**丢行**：合并是读-改-写，
清单守卫只按 `ingested_at` 排序，不做串行化。所以 17:30 那次从开始到结束持
`data/daily_job.lock`（`O_CREAT|O_EXCL`，pid + 6 小时超时），第二个进程直接退出 3，
一个分区都不碰。

## 4. 单一执行者矩阵（按时点切权限，不按模型意愿切）

| 动作 | 唯一执行者 | 其他任务 |
|---|---|---|
| 换人格 | 09:05 | 一律不许 |
| 开盘买卖 | 09:26 | 13:35 只卖不买 |
| 盘中卖出 | 13:35 | 09:26 与 14:30 也可以卖，但同一只票已有挂单不重复提 |
| 尾盘买卖 | 14:30（14:57 截止） | **定时任务**开工晚于 14:52 判「不买」——那是给 96–111 秒检查链留的缓冲，不是市场规则；**人工对话**以 **14:57** 为界，标的已定且 `propose`+`approve` 能在 14:57 前走完就该执行，不得拿 14:52 当借口 |
| 样本账本 | 09:35 | 主账户工具一律不碰样本，反之亦然 |
| 次日委托 | 19:30 | 状态是 `queued`，不是 `filled` |
| 市场结论 | 18:50 | 19:30 不自创市场判断，只引用与核对 |

**账外实盘（`quant_query(resource="stops", params={account="external"})`）只提醒、不挂单**：任何任务都不对它
`propose`。2026-11-02 的记录是 79% 的账外仓位整个下午没有止损被监控，
所以巡检、09:26、14:30、19:30 各读一次，`stale_book=true` 时写明「账面可能不是实际持仓」。

## 5. 降级阶梯：上游缺了，下游怎么办

不许猜，不许用旧数补。每一格都是已经定好的退化动作：

| 缺失 | 下游动作 |
|---|---|
| 09:05 人格校准没跑 | `persona` 沿用上一交易日。`tradable=true` 就照常做；`tradable=false` 则 09:26 按第 -2 步收工并在 journal 写明「09:05 未跑、人格死路未修」。**不要**在 09:26 擅自切人格——切换要 reason ≥20 字、evidence、`expected_edge_bps`，那是要留痕的决定，不是补一个漏掉的步骤 |
| 09:26 开盘决策没写 `open(D)` | 13:35 没有「早上的判断」可验：只判破线（`quant_query(resource="stops")` + `quant_query(resource="watch")`），不重述行情；14:30 因此没有「已买过的不再买」的清单，只能靠 `quant_query(resource="orders")` 自己重建，并在报告里写明这是重建的 |
| 18:50 复盘没写 | 19:30 自己取数核账，在「缺口」里点名，明天三条照出（第 2 条改成自己取的结构数） |
| 19:30 对账没写 | 09:26 第 1 条改用 `observations.stop_breach` + `stops(external)`；其余两条不补 |
| 13:35 午间没写 | 14:30 按 `open(D)` + 当日实盘判断，复述一遍行情代替不了这一步 |
| 13:35 没写 journal | 19:30 记缺口。这是已知缺陷，不是可接受的常态 |
| 当日日线没落库 | 先看 `quant_query(resource="data_health")` 的 `pipeline.state`：`running` 就等（这时补跑会被锁拒，退出码 3）；`missing` 才补跑一次 `daily_job.py --days 3`；仍失败则整份作废并贴日志末 20 行 |
| 当日 `quant_query(resource="auction")` 为空 | 正常。当日竞价要收盘后 `fetch_ths_special.py --only auction` 才入库，用 D-1 名单 + 今日跳空还原 |

**这张表现在是机器可读的一半**：`quant_query(resource="duty")` 返回的 `items` 里，`kind="upstream_missing"`
逐条列出「按时间该在、但不在」的交接物，带 `artifact`（相对路径）与 `due_by`（读它的那个
任务的时点）。判据只有两条：今天是交易日，且当前时钟已过 `due_by`——所以 08:00 的队列不会
抱怨 19:30 那份复盘，永远响的告警等于没有告警。

它只能看见**文件**。09:05 没有交接物（不写文件，只改 `persona.current`），所以它缺席时
不会出现在 `upstream_missing` 里，只会以 `persona_dead_end` 的形式露出来——那一格得靠
09:26 的 `tradable` 判断兜住，没有第二条路。


## 6. 这一版修掉的五个缺陷

> 尾盘 14:35 → 14:30（2026-11-13）：`playbook/v3` 的四处窗口名与本文 §1 / §2 / §4 / §5
> 的表格已同步；§6 / §8 / §11 里带日期的记录保留当时的 14:35——那是历史，不是现行规则。

1. **「14:20 尾盘」的旧引用**：尾盘任务 2026-10-31 已前移到 14:35（抖动窗口 14:35–14:55），
   但 09:26 / 12:30 的提示词还在写「14:20 会读这份报告」。改齐，否则下游按错误时点安排节奏。
2. **巡检不留痕**：提示词只在「T+1 锁住」时才要求写 journal，与技能里的
   「每轮巡检必须留痕」相反。改成无条件写一条，哪怕结论是「无异常」。
3. **抽样当全集**：11-02 的 14:35 把 `quant_query(resource="screen")` 返回的 3 只当成 cap 内可买全集，
   收盘 `in_cap=63`、`buyable_at_close=0`。旧口径里的 `opened_intraday` 已拆成
   `one_word`（开盘即封、全天没离开涨停价）与 `board_opens.opened`（封后开过板，
   东财池、分母不同）。几个问题不能互相替代，也不能相减或相除。
4. **没有市场复盘**：结构判断原来寄生在 19:30 的「收盘核对」里，只覆盖「报告提到过的数」，
   没人回答「今天是什么日子、明天怎么走」。18:50 独立成任务，19:30 只核账。
5. **09:26 的单根本没有成交路径**（2026-11-04）：`fill_mode=intraday` 下内核拒绝在
   PRE_OPEN 撮合，09:26 的票被 `MARKET_CLOSED: PRE_OPEN` 当场退掉；`next_open` 模式下
   又要等到 T+1 开盘。现在统一排进 `open` 队列，按**当日开盘价**结算，跳空越限价即取消。
   同时两个下单窗口的提示词都补上了 `quant_order(book="paper", action="approve", params={confirm=true})`——只提案不批准等于没下单。

## 7. 改提示词时的自检

- 第一行是不是结论？不是就重写。
- 有没有写清「我读谁、谁读我、我没写会怎样」？
- 引用数字有没有 n 与区间？分位是不是历史序列口径？
- 有没有把「新闻说的」混进数字里？
- 新加的规则有没有失效条件与样本量？（没有就先当试点，写进「一条改动」）

## 8. 2026-11-06 校准：提示词对齐平台与内核的真实能力

7 个 WorkBuddy automation 的 `rrule`（`FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=…;BYMINUTE=…`）、
`cwds=D:/quant-desk`、本地时区三项本来就对，**本次只改 prompt**：不动调度、不新增任务、
不改 §4 的单一执行者矩阵。错的是提示词里对「什么时候跑、能调什么、按什么价成交」的假设。

### 8.1 实测证据（不是推测）

| 事实 | 来源 |
|---|---|
| 09:26 任务实际开工 09:42:02（11-04）、09:33:40（11-05，报告记为「第 7 次晚于窗口」）；10-30 09:42、11-02 09:40、11-03 09:44 | `reports/open/20261104.md:12`、`reports/open/20261105.md:12` 与两份报告的「缺口」节 |
| 09:05 的人格切换实际发生在 09:13:38（11-05） | `reports/open/20261105.md:6` |
| 14:35 实际 14:42 开工、14:47 出结论 | `reports/close/20261105.md:1` |
| `quant_query(resource="context").session.market_open_today`、`risk.fill_mode=intraday`、`quant_query(resource="data_health").pipeline.state=done` 均真实存在 | 2026-11-06 20:50 工具实读 |
| `quant_order(book="samples", action="propose").evidence` 必填、`quant_query(resource="anomaly").limit` 上界 8、`quant_query(resource="tickets")` 默认 `status=open` | `plugin/lib/index.js:1156-1168`、`1852`、`1123` |
| 样本账本不受 CORE retired 影响：`/samples/propose` 用 `get_samples_desk()` 且强制 `strategy=sample_1d`，`_core_retired()` 只在 `/core/*` 路由 | `service/api/trading.py:1507-1515`、`2236`、`2306` |
| 分钟级链路在 Windows 侧：`external-monitor` 每 5 分钟、`snapshot-loop` 约 40 秒一帧；后者 `LastResult=1` 对应日志末行 `loop stopped: outside a trading session`，是收盘正常收工，不是崩溃 | `Get-ScheduledTask`、`data/snapshot.log`（439 行） |

### 8.2 平台边界（WorkBuddy 侧）

1. RRULE 支持 `HOURLY / DAILY / WEEKLY / MONTHLY / YEARLY`，**不认 `MINUTELY`**。分钟级巡检留在 Windows 计划任务，不搬进 automation，也不要为它新建任务。
2. 8/9/10/14 点档有约 20 分钟确定性抖动，其余钟点约 30 秒。受影响的是 09:05 / 09:26 / 09:35 / 14:35；14:35 的「晚于 14:52 判不买」已适配，本次补齐其余三个的开工时刻自适应。
3. `MO–FR` ≠ 交易日。RRULE 表达不了交易所日历，`quant_query(resource="context").session.market_open_today` 是唯一闸门；节假日照常触发但必须当场退出、不写文件、不补跑日更。
4. `quant_*` 经 `~/.workbuddy/mcp.json` 的 `quant-desk` MCP 接入，属 deferred 工具：先 `ToolSearch` 载入全名 schema，再 `DeferExecuteTool` 用全名调用。校准前 7 个 prompt 里只有 18:50 写了这条。
5. 不合并任务。「同一逻辑任务的多时点用 `BYHOUR=9,15` 合并」这条通用建议在这里不适用：8 个任务回答 8 个不同问题，权限按 §4 的时点切分，合并会让一个执行者同时拿到买卖与对账权。

### 8.3 统一开工契约（8 个任务共用，优先级高于各 prompt 中与之冲突的条款）

1. **取数路径**：`ToolSearch` → `DeferExecuteTool(toolName="mcp__quant-desk__quant_*")`；服务不可达先 `curl -s http://127.0.0.1:8811/health` 修服务，不许改用记忆里的数、不许跳过工具层手搓 HTTP。重的读串行，不并发。
2. **交易日闸门**：`quant_query(resource="context").session.market_open_today=false` → 输出「今日休市，不执行」并结束，不写报告、不写 journal、不下单、不补跑日更。
3. **开工时刻**：用 `quant_query(resource="market_status").session.now` 取实际时刻写进结论，不假定自己跑在计划时刻上。
4. **上游缺席**：动手前读一次 `quant_query(resource="duty")`，`kind="upstream_missing"` 按 §5 降级并在「缺口」点名，不用别的数字补它。
5. **日更状态**：引用当日收盘数前看 `quant_query(resource="data_health").pipeline.state`——`done` 正常；`done_not_ready` 不引用 `failed_steps` 点名的数据集；`running` 不补跑也不引用未写完的数；`missing` 才补跑一次 `daily_job.py --days 3`。`bars_behind=0` 只说明日线到了。
6. **新闻顺序**：只有「解释为什么」的任务（18:50 / 19:30）先新闻；「要不要动手」的任务（盘前选股、尾盘决策、巡检）先工具，新闻只补「为什么」、不参与定价、不占时间敏感窗口（与 `AGENTS.md` 的执行顺序一致）。
7. **分钟级不在本平台**：见 8.2 第 1 条。

### 8.4 逐任务改动

| 任务 | 改动 |
|---|---|
| 09:05 | 加契约；人格切换提到 `quant_query(resource="duty")` 与新闻印证之前（9 点档抖动会让它和 09:26 抢跑）；新闻印证一次、不先于 `quant_query(resource="context")` |
| 09:26 | **开工时刻自适应**：<09:30 才用 `open` 队列与「已挂单（按开盘价）」；≥09:30 改用真实开盘价算跳空、按 `fill_basis=intraday` 实时成交、禁止写「按开盘价挂单」；新闻从「先于任何工具」改为结论后补、限时 60 秒；`tradable=false` 先查 09:05 是否跑过再降级 |
| 09:35 | 加契约；补 `evidence` 必填、`thesis`/`invalidation` 长度；新闻后置为可选；T+1 按账本日期而非计划时刻判断 |
| 13:35 | 加契约；先读 `quant_query(resource="live").book_watch.open` 与 `watcher.outcome`；`quant_query(resource="watch", params={since=…})` 仍必带 |
| 14:35 | 加契约；保留已适配的 14:52 / 14:57 降级与 60 秒新闻限时；`declined(day=…)` 取 `calendar_today` |
| 18:50 | 数据门槛改以 `pipeline.state` 为准；`quant_query(resource="anomaly", params={limit=10})` 改成 `limit=8`；新闻先行的分工保留 |
| 19:30 | 数据前提改以 `pipeline.state` 为准；开工先枚举 `quant_query(resource="tickets", params={status="open"})` 再读 `quant_query(resource="duty")`；新闻先行保留 |

### 8.5 明确不做的事

- **不引入 APScheduler / Prefect / Airflow**：调度已经分两层（Windows 计划任务管数据与监控，WorkBuddy automation 管判断），交易日历在 `service/kernel/calendar.py`，再引一层只增加重叠运行风险。开源实践里 `max_instances=1` + `coalesce` + 跨进程锁正是为「上一趟没跑完下一趟又起」准备的，这里已由 `data/daily_job.lock` 与计划任务的 `IgnoreNew` 覆盖。
- **不把 09:26 的时刻往 09:31 挪**：那会换一套策略；也不往 09:15 挪，集合竞价前拿不到可用的开盘参考价。抖动由 prompt 的时点分支吸收，不由调度时刻硬扛。
- **不改内核代码、不碰 `configs/`**：本次校准的输入是既有字段的真实读法，不是新功能。

## 9. 2026-11-06 第二轮：名字、任务集合、内容一起对齐内核

§8 修的是「提示词里的假设」，但那轮仍留了双层结构（旧段落 + 前置覆盖条款），
名字还带着分钟，任务集合里也还有内核能力没人接。这一轮把三层一起改掉。

### 9.1 名字：分钟不是事实，内核对象才是

平台给 8/9/10/14 点档约 20 分钟抖动，名字里的 `0926` / `1435` 读作时刻断言会骗人；
真正稳定的是**该任务绑定的内核对象与它造成的那次状态转移**。新名字只保留语义窗口 +
内核对象，不保留分钟。链路序号不写进名字——交接物是线性的，但触发不是。

| 旧名 | 新名 | 绑定的内核对象 / 状态转移 |
|---|---|---|
| `quant-desk 0905 开工校准` | `quant-desk · 盘前 人格袖就绪` | `quant_query(resource="persona").current.open_sleeves` → `quant_workflow(operation="persona_switch")` |
| `quant-desk 0926 开盘决策` | `quant-desk · 盘前 首窗决策` | `market_status.session.label` → `propose/ticket/approve` |
| `quant-desk 0935 样本轮换` | `quant-desk · 早盘 样本账本轮换` | `quant_query(resource="samples").remaining` → `samples_propose/approve` |
| `quant-desk 1335 午间验证` | `quant-desk · 盘中 风控与档位` | `quant_query(resource="live").book_watch.open` → `stops/orders` + 三档 |
| `quant-desk 1435 尾盘建仓` | `quant-desk · 尾盘 末窗决策` | `market_status.in_session` + `portfolio.cash_free` → `declined` |
| `quant-desk 1850 市场复盘` | `quant-desk · 盘后 市场结构` | `pipeline.state` + `quant_query(resource="facts")` + `quant_query(resource="pools").sentiment.tone` |
| `quant-desk 1930 收盘对账` | `quant-desk · 盘后 账本对账` | `quant_query(resource="tickets", params={status=open})` + `declined/memo/arena` |
| —（新增） | `quant-desk · 收盘 提醒解读` | `quant_alert(action="list")` pending → 三句解读 + `answer` |

### 9.2 增减：净增 1 个，一个都不删

**新增 `quant-desk · 收盘 提醒解读`（工作日 15:25）**，唯一理由是它接的是一条断了很久的闭环：
2026-11-06 21:05 实读 `quant_alert(action="list", params={})` 返回 **`pending=15`**，最早一条是
`20261027-092605`，最近是 `20261106-093020-f2a61f`；其中今天的
`20261106-092620-174707` 是 002185 跌破止损 14.35 的提醒。`docs/10-alerts.md` 把解读层
设计成「规则发事实邮件 → 交接文件 → agent 三句解读 → 第二封邮件」，但原来的 7 个任务里
**没有任何一个调用 `quant_alert(action="list")`**，所以 15 条一直堆着。它只回答一个问题，
不碰模拟盘、不下单、不改人格，与 13:35 的模拟盘止损互不替代。

处理窗口是 `age_hours <= 96`（服务端在 `/monitor/handoff` 返回 `age_hours`、`read_window_hours`
与 `stale`，读者不必自己减时刻）。**原先写的是「只处理 created_at 属于当天的条目」，那是一个
结构性空转**：15:25 之后到收盘之间入库的提醒，到次日已经是「历史」，而次日仍只看「今天」——
这批条目没有任何一次运行会读到，只能等人工 drop。96 小时装得下「上一交易日 15:25 之后」再加
周末与平台抖动；更早的仍不补发解读（拿十天前的事实解释今天会误导），只在结论里报数量与最早
日期，由人用 `python scripts/handoff.py drop <id>` 清理。窗口内跨日的条目，解读的时间锚是该条
自己的 `created_at`，不是运行时刻。
`pending=0` 就一行收工，不制造工作。

**一个都不删**，理由是 §4 的单一执行者矩阵仍然成立：8 个任务各自绑定一个别人不做的状态转移，
合并会让一个执行者同时拿到「换人格」和「下单」、或「看市场」和「核自己的账」两种权限。
特别是 09:05 与 09:26 虽然同在 9 点抖动档会抢跑，但 §5 明确「切换要留痕，不是补一个漏掉的步骤」，
合并会抹掉这条边界；抢跑由 09:26 的 `tradable=false` 降级分支吸收，不由合并吸收。

**明确不新增**三类：① 核心盘任务（`quant_core_*` 已 409 `CORE_RETIRED`）；
② 分钟级巡检（RRULE 不认 `MINUTELY`，见 §8.2）；③ 周度规则验收
（`quant_workflow(operation="playbook_propose")` 的草案「never take effect until a human activates them」，
自动任务无法闭环，只会每周产一份没人激活的草案）。

### 9.3 内容：从「说明书」改成「状态机描述符」

上一轮把补丁放在 prompt 顶部、旧段落留在下面，读的人要自己判断哪句作废。这一轮把补丁
**合并进正文**，并把写死的数字全部改成从内核字段读。最要紧的两处实证：

| 写死的东西 | 内核的真实值 / 后果 | 出处 |
|---|---|---|
| 不传 `sleeve` | 默认 **`CORE`**，而 `configs/core.json retired=true` → **409 `CORE_RETIRED`**；换成不持有的袖 → 400 `PERSONA_SLEEVE` | `plugin/lib/index.js:1004`、`service/trading/decisions.py:264`、`duty.py:144` |
| 「股票 100 股 / 30%」 | `single_cap` 按 sleeve 取：`AGGRESSIVE → max_aggressive_single=0.5`，CORE → 0.3（ETF 0.4）。活袖是 AGGRESSIVE，按 50% 算价格上限应是 `cash_free × 0.5 / 100`；11-05/11-06 报告用 30% 算出 15.02 元，**可买池被人为缩小约 40%** | `decisions.py:652-653`、`quant_query(resource="context").risk.limits` 实读 |
| `evidence` 没写 | `quant_order(book="paper", action="propose")` 与 `quant_order(book="samples", action="propose")` 都把它列为 **required**（1–8 个实际读过的 id） | `index.js:1007-1011`、`1160-1165` |
| `persona` 没写 | 提案写了就必须等于当前活跃人格，否则 BUY 被 409 | `index.js:1023-1026`、`decisions.py:310-314` |
| `client_request_id` | 插件自动填并复用 5 分钟幂等句柄，**不要手传** | `index.js:1036-1043` |
| 45bps / 346.7bps | 用票上的 `round_trip_cost_bps`；`est_fees` 只是买侧 | `index.js:1000` |
| `min_decisions` 写死 | 读 `quant_query(resource="arena").min_decisions_for_an_interval`（当前 20） | `quant_query(resource="dashboard")` 实读 |
| mcp.json 描述「48 个工具」 | `buildTools()` 实际 **67** 个 `quant_*` | `plugin/lib/index.js` 计数 |

### 9.4 开工契约 v2（8 个任务共用，已内联到各 prompt）

1. 加载 `quant-desk` 技能并读其分派表；`quant_*` 是 deferred 工具，`ToolSearch` 载入全名后用
   `DeferExecuteTool(toolName="mcp__quant-desk__quant_*")` 调；服务不可达先修服务。
2. `quant_query(resource="context").session.market_open_today=false` → 「今日休市」结束，不写文件、不下单、不补跑日更。
3. 实际时刻取 `quant_query(resource="market_status").session.now`，窗口取 `session.label`，成交模式取
   `quant_query(resource="context").risk.fill_mode`；**任务名里的窗口不是时刻断言**。
4. `quant_query(resource="duty").upstream_missing` 按 §5 降级并点名；引用当日收盘数前看
   `quant_query(resource="data_health").pipeline.state`（`running` 不补跑，会被锁拒、退出码 3）。
5. 新闻：只有「解释为什么」的任务先新闻；「要不要动手」先工具。分钟级在 Windows 计划任务。
6. **所有阈值与上限一律从工具读**：仓位/次数/风险读 `risk.limits`；价格上限读
   `cash_free` 与对应 `max_*_single`；成本读 `round_trip_cost_bps`；阶段读 `sentiment.tone` 原词；
   edge 阈值读 `min_decisions_for_an_interval`。提示词与历史报告里的字面量只作例子，不作判据。
7. 下单三步 `propose → ticket → approve(confirm=true)`；超时原样重试（5 分钟幂等），不改参数重试。
8. 数字只来自工具；新闻只解释「为什么」；内核没有 `action` 字段，买不买是模型的判断。

**裸名的归属**（`scripts/audit_prompts.py` 按此核对）：`session.label` 与
`session.in_session` 指的是 `quant_query(resource="market_status").session`；`session.market_open_today`
与 `session.tradable_now` 指的是 `quant_query(resource="context").session`。两处都叫 `session`，但不是同一个块——
写成裸名时，读者要按上下文判断。


### 9.5 副本不再手写：`scripts/freeze_automations.py`

2026-11-08 的审查发现：仓库里 7 份副本全部停在 `captured: 2026-11-05` 的 v1，
而线上 8 个任务已经是 v2。副本里写「股票 30%」「age ≤300 秒」「无闸门」，
线上写「按 sleeve 从 `risk.limits` 取」「不写 30%」「`market_open_today=false` 即退出」。
第 8 个任务（15:25 提醒解读）在仓库里根本没有副本。**按副本改线上会把已修好的东西改回去。**

现在副本由脚本从宿主数据库实读生成：

```bash
python scripts/freeze_automations.py            # 重写 11 份（frontmatter 带 prompt_sha256）
python scripts/freeze_automations.py --check    # 文件数 / 哈希 / 与线上逐字比对
```

两条校验分工：

| 校验 | 位置 | 需要宿主 DB | 抓什么 |
|---|---|---|---|
| 副本自身 | `tests/test_automation_copies.py` | 否 | 11 份、id 唯一、文件名=HHMM、哈希、契约标记、废弃字面量 |
| 与线上一致 | `scripts/freeze_automations.py --check` | 是 | 线上改了但没重冻、线上多/少任务、rrule 改了 |

**注意**：`--check` 只在本机能跑（DB 是 `~/.workbuddy/workbuddy.db`）；
换台机器或 CI 只跑 pytest 那一半。改完 automation 必须重跑脚本，否则 `--check` 会红——
这正是它存在的意义。

最后一个字面量也清掉了：09:26 的报价前置检查原来写死 `age ≤300 秒`，而成交闸门是
`live_fill.max_quote_age=90`——90–300 秒的报价会过一次前置、在 approve 被 `STALE_QUOTE`
拒，白跑一次提案。现在改为读 `quant_query(resource="market_status").fill.max_quote_age_seconds`
（`/market/status` 的 `fill` 块，值与 `live_fill` 同源，实测 90）。

## 10. 2026-11-08 第三轮：把副本变成可信记录

这一轮不改交易规则，只做两件事：让仓库副本等于线上，并把「提示词读的路径」逐条对着
真实响应核一遍。

### 10.1 校准结论（逐项实读）

| 项 | 结论 |
|---|---|
| 8 份契约块 | 逐条比对：差异只在窗口名（盘前/早盘/盘中/尾盘/盘后）、休市措辞、第 5 条的任务名单。**有意，不统一**——统一会把「不复盘」「不对账」改成不准确的「不执行」 |
| 15:25 的契约 | 只取前 4 条（不交易、不引用收盘数），第 4/5/6 条对它不适用；9.4 的「8 个任务共用」指同一份契约的来源，不是每条都逐字复制 |
| 20 日均额 ≥ 5000 万 | = `screen.min_adv_cny`，一致 |
| `quant_query(resource="anomaly", params={limit=8})` | = 工具上界 1–8，一致 |
| `quant_query(resource="auction", params={limit=10})` | 工具允许 1–40，默认 10，合法 |
| 14:48 / 14:52 / 14:57 | 纪律位，不是配置项；14:57 与 `calendar.py` 的收盘竞价边界一致 |
| 0–3% / >3% / <0 | **2026-11-09 已删除**：这行是 v1 的回退规则，而 v2 §8 把 `−2% < x ≤ 0` 放行了。提示词不再复述分档，改读 playbook §8（见 §11.2） |
| 09:26 `age ≤300` | **唯一的真不一致，已改**（见 9.5 末段） |

### 10.2 新增的两个校验

- `scripts/audit_prompts.py --live`：v2 的 **54 条**反引号路径逐条对真实响应解析
  （2026-11-09 新增 `limit_structure.*` 三条），0 悬空（7 条源码取证、2 条显式豁免）。
  新增路径必须先在 `BY_PREFIX` / `BY_TOOL` / `EXACT_ROUTE` / `SOURCE_EVIDENCE` 里登记归属，
  否则静态检查就红。裸 `counts` 这类在多处出现的名字不许当作前缀——要带端点名写全。
- `tests/test_automation_copies.py`：副本自身完整性（8 份、哈希、契约标记、废弃字面量）。

**一个必须记住的教训**：重冻后 `tests/test_automation_prompts.py` 立刻 6 红——它钉的是
v1 结构。那些用例一直是绿的，因为校验对象是过期副本。**测试通过不等于线上正确。**

## 11. 2026-11-09 第四轮：规则生效了，但没人读

### 11.1 激活本身不是终点

`playbook/meta.json` 的 `active` 从 v1 走到 v2（12:28），三份累积草案（§7 窗口权限、
§8 跳空门分档、§9 档位判据）一次生效。激活之后才发现更根本的一层：

**8 个任务里只有 19:30 引用 `quant_query(resource="playbook")`，而它是对账任务，不决定买卖。真正下单的
09:26 与 14:35 从来没读过它。** 规则写在 playbook 里，执行者读的是提示词里的复述——
所以「草案连续 4 天没激活」和「激活了也等于没激活」是同一个病的两个阶段：规则的唯一
真源没有被任何决策者打开过。

这一轮的修法不是把 playbook 抄进提示词（那会立刻产生第二个真源），而是反过来：
**提示词删掉规则复述，只留流程；规则一律读 `quant_query(resource="playbook")`**。

### 11.2 三处真实的规则冲突

| 提示词 | 它写的 | v2 写的 | 后果 |
|---|---|---|---|
| 09:26 第 5 步 | 「0–3% 可买、>3% 不买、`<0` 不买」（v1 原规则） | `−2% < x ≤ 0` 放行到赔率门 | 一票否决了 v2 明确允许的低开票，`−2%~0` 那一档永远走不到 |
| 13:35 第二步 | 「判三档里命中哪一档」 | §9.2 档位判定移到 14:30 之后 | 用半天读数给全天定档；11-06 的 09:39 读 11 / 收盘 33 就是这么错的 |
| 14:35 | 「空仓的按午间档位走」 | 档位由 14:35 自己判 | 继承一个按新规则不该存在的结论 |

改动落在三份提示词：0926 与 1435 各加一条同构的开工契约（第 7 条，规则真源 + 优先级），
1335 改第二步（判条件不判档位）与输出格式（不写「走 <档>」）：

- 规则真源是 `quant_query(resource="playbook")`，本文件与它冲突时按它执行，并在结论写明版本；
- 优先级 `D-1「一条改动」> quant_query(resource="playbook") > 本文件`；
- 档位在 14:35 判：三值读 `limit_structure.counts.limit_up` / `limit_structure.seal_rate` /
  `limit_structure.max_streak`，`quant_query(resource="pools").gates.checks` 三项任一为 null 就写
  `tier=undetermined`。

### 11.3 草案的「反向」空转

激活最新一份之后，前两份草案的内容已经生效，却仍留在 `drafts` 里，`/playbook` 的
`draft_count=2`。读者看到的是「还有 2 份等着批」，而批了不会有任何变化——这跟当初
「草案根本看不见」是同一个毛病，只是方向相反。

新增 `playbook.supersede(draft_id, by_version, note)`（CLI `supersede --by v2 --note ...`）：
把被吸收的草案移出待批队列，写进 `meta.json` 的 `superseded`，文件保留作证据。
`by_version` 必须真实存在，`note` 要写清凭什么判定已被吸收——文本是否真被吸收无法自动
判定，所以这是显式动作，不是推断。`/playbook` 现在同时返回 `superseded_count` 与
`superseded[]`。

同一次还修了 v2 正文里的两处自指错误：首行标题仍是 `playbook v1`，§6 版本表把三行
草案全标成「v2（草案）」。规则条款一字未改，订正记录写在 `meta.versions[v2].amend_note`。

### 11.4 15:25 的解读窗口：一个永远走不到的判据

原提示词写「只挑 `created_at` 是**今天**的条目」。15:25 之后到收盘之间入库的提醒，到次日
已经是「历史」，而次日仍只看「今天」——这批条目没有任何一次运行会读到，只能等人工 drop。

改成 `age_hours <= 96`（`handoff.READ_WINDOW_HOURS`），并且**这个数由服务端算**
（`pending()` 新增 `age_hours`，`/monitor/handoff` 新增 `readable` / `stale` /
`read_window_hours`）。理由是模型减时刻不可靠，而「这条该不该现在解读」完全取决于它。
96 小时装得下「上一交易日 15:25 之后」再加周末与平台抖动；更早的仍只报数量不解读。
窗口内跨日的条目，解读的时间锚是该条自己的 `created_at`，不是运行时刻。

### 11.5 这一轮的验收

- `scripts/freeze_automations.py --check`：8 份与线上逐字一致；
- `scripts/audit_prompts.py`：54 条路径 0 悬空；
- `pytest`：全绿（含新增的 2 条 handoff 窗口用例、1 条 supersede 用例）；
- `npm test` 69 绿、`npm run test:mcp` 30 绿；
- `playbook/meta.json`：`active=v2`、`drafts=0`、`superseded=2`。

## 12. 季度 / 事件数据增量同步（2026-11-10）

这两条链路**不进 17:30 日更**，是设计不是遗漏：`catalog.py` 的 `fina_indicator`
规格写明「Quarterly by nature, so it stays out of the daily job」——该 token 只接受
按 `ts_code` 逐票查询，全市场一次约 5900 次调用，塞进日更会把 30 分钟窗口吃满。
业绩预告 / 快报虽可按 `ann_date` 全市场拉（一天一次调用），但「这一天没有预告」
是常态，不是日更失败，所以也不放进 `gate` 的 `failed_steps`。

### 12.1 两个脚本

| 脚本 | 数据源 | 写入 | 游标 |
|---|---|---|---|
| `scripts/quarterly_fundamentals.py` | Tushare `fina_indicator` | `fina_indicator/all` | `data/state/quarterly_fundamentals.json`（`last_code`，按 `stock_basic` 顺序分批） |
| `scripts/sync_forecast.py` | Tushare `forecast` / `express` | 同名 `all` 分区 | `data/state/sync_forecast.json` |

```bash
# 财报季（1/4/7/10 月末起）跑，--limit 0 = 全市场一次扫完
python scripts/quarterly_fundamentals.py --since 20261029 --limit 400
python scripts/quarterly_fundamentals.py --since 20261029 --symbols 002185.SZ  # 定向补一只
# 预告/快报按公告日增量，一次 30~45 天只有几十次调用
python scripts/sync_forecast.py --since 20261001
```

候选源刻意用 `stock_basic` 全市场而不是公告元数据：2026-11-10 的 `anns` 里
`002185.SZ` 在 10-20 之后没有任何公告，而它的三季报 10-31 已披露。按标题
筛「季度报告」会把要补的那只票直接漏掉。游标分批保证最终覆盖每一只。

### 12.2 怎么知道补齐了

- `/data/information` 的 `fundamental_sla`：`coverage` = 已入库最新报告期的股票数 /
  在市股票数，`sla_breached` 低于 `research.fundamental_min_coverage` 时为真。
  它量的是「已披露未入库」，不是距今天数（季度数据 90 天没新报告是正常的）。
  2026-11-10 实测：同步前 0.459 / breached，同步 1500 只后 0.5661 / 不 breached。
- 同一端点的 `announcement_text_coverage`：近 30 天公告里已有正文缓存的比例。
  正文由 `scripts/fetch_anns_text.py` 按持仓 / 池子抓取，`quant_query(resource="filing")` 按需分页读，
  所以全市场口径天然低；这个字段只是让缺口可数，不是要求全量下载。

### 12.3 运行窗口

两个脚本最后都会**整表覆写** `all` 分区。不要在 17:30–18:00 日更窗口或全量
`pytest` 期间运行（存储层虽有 manifest 退避重试，高频重写仍会耗尽）。建议放在
日更结束、21:40 任务之前，或财报季手动执行。`--dry-run` 只看不写。

### 12.4 每周调度入口（2026-11-10）

`scripts/sync_research_data.py` 把两条链路收成一个「该不该跑」的入口：读
`fundamental_sla`，若 `sla_breached` 或最新报告期公告日距今 ≤ 45 天（新披露季）
才执行；`_fundamental_sla` 不可读时返回 due=True（未知要检查，不是跳过）。
新窗口会重置 `quarterly_fundamentals` 的 `last_code` / `cycle_done`，否则上一季
走完一轮的游标会让新一季静默不跑。默认 `--batch 400`，`--force` 忽略 due，
`--dry-run` 不写游标也不调接口。

`scripts/install_sync_tasks.ps1` 注册 `quant-desk-research-sync`：**每周日 20:30**
执行 `--batch 2000`，避开 17:30 日更、19:30 对账、21:40 late pass，也避开全量
pytest。任务注册本身不触发同步，脚本幂等（`-Force` 重注册不产生重复任务）。
停用方式：在任务计划程序里禁用 `quant-desk-research-sync`。

### 12.5 个股研报的可证伪落点

`scripts/log_thesis_memo.py` 把一次个股研判写成 `symbol_up` 断言，`POST /memo`
落账本；`service/research/memo.py` 以 memo 锚定日收盘与 horizon 后收盘比较写
`outcome`，`state.calibration()` 汇总 Brier / BSS。2026-11-10 为 002185 研判登记
memo id=8，`probability=0.5` 是**气候基线、不押方向**（无论结果如何 Brier 贡献
恒为 0.25），目的是建立「每份研报必留可结算断言」的流程起点，不是宣称判断力。
`arena.edge.n` 的 20 笔平仓 KPI 仍需交易时段逐日推进，与 Brier 校准是两条
不同的证据链，不能互相替代。

### 12.6 收盘后固定清单（2026-11-10）

`scripts/postclose_check.py` 把收盘核对固化成一张顺序固定的只读清单：
**完整性**（服务 commit / 当日日更 gate / 两份报告是否带修复后状态注记 /
同步游标 `cycle_period`）→ **一致性**（`point_in_time`、默认 `as_of` 是否落后、
`fundamental_sla`）→ **新鲜度**（公告正文覆盖率、日内分区天数、handoff、
`arena.edge` 与样本账本平仓数）。

```bash
python scripts/postclose_check.py --human
python scripts/postclose_check.py --strict
```

17:30 之前跑，`daily_job` / `as_of_lag` 是 `info` 不是失败；当日已有新公告而
`as_of` 仍是上一交易日时，`point_in_time` 的 fail-closed 报警同样符合预期
（用 `?day=<今天>` 可复核当天口径）。`--strict` 只在出现 `warn` 时返回 1，
适合挂在 18:00 之后的任务里。

报告正文是**历史快照**：能力或数据改动落地后必须加「盘后状态补记」说明哪些
数字已失效、哪些结论不变，而不是回写正文数字——清单里 `report:*` 两项专门
盯这件事。2026-11-10 已为 `reports/002185-20261110.html` 与
`reports/kernel-capability-audit-20261109.html` 补记。

### 12.7 三个资金/账户层面的决定（2026-11-10 预注册）

这三件事此前一直挂着「需账户主人拍板」，本日做出决定并固化成门禁，
**不是留白**。理由按检索到的 promotion-gate 实践：门槛必须在看到结果之前
写成数字，升级与降级同样机械；「在 2× 成本下死掉的策略是成本模型，不是策略」。

| 决定 | 内容 | 理由 |
|---|---|---|
| ① `trigger.enabled` | **保持 false，不开** | 5 条资格只满足 1 条（见下表）。`observe=true` 已让模型写条件买卖点并留影子轨迹，零资金；出钱是另一个决定 |
| ② 账户粒度 | **不注资、不动风控阈值**；20 笔平仓 KPI 交给 `data/samples.sqlite`（¥2,000 样本账本，已 2 笔） | 注资会污染历史 KPI 可比性；调风控阈值等于为了能下单而放松风控。主账本 ¥5,000 保持空仓 |
| ③ Level-2 逐笔 | **不接** | 需外部行情授权与费用；且 Grinold `IR ≈ IC × √BR` 下补微观结构只抬 IC，抬不了单票天然不足的 breadth。优先级远低于攒样本。冷存前分时同样不可回补 |

**门槛写在 `configs/params.yaml` 的 `promotion:` 段**（代码里不写值）：
`min_memo_n=20`、`min_bss>0`、`min_accepted_hypotheses=1`、
`atr_multiple=2.5`、`benchmark_symbol=002185.SZ`。
`min_decisions` 刻意不重复，唯一真相源是 `arena.min_decisions_for_an_interval`（20）。

`scripts/promotion_gate.py` 只读检查这五条，输出 `gate` / `blockers` / `compliance`：

```bash
python scripts/promotion_gate.py --human     # 资格 1/5，决定：保持 enabled=false
python scripts/promotion_gate.py --strict   # 仅在 enabled=true 且未过门槛时返回 1
```

2026-11-10 实测四条 blocker：`decisions 0/20`（CI 未出）、
`calibration 7/20 & bss=-0.2282`、`hypotheses 0/1`（7 条全 rejected）、
`sizing 5000 < 6026.62 元`（002185 last 14.61 / ATR20 6.6% 下 2.5×ATR 止损
一手需权益 ≥6026.62，差 20.5%）；唯一满足的是 `observe=true`。

**降级规则**：`postclose_check.py` 每天监督同一份门禁。
`enabled=false` 且 blocked 是**合规**（`info`）；只有 `enabled=true` 且任一
前置不满足才报 `warn`，提示按机械降级规则关回。门禁脚本本身不改任何开关、
不发信号、不下单——它只回答「现在够不够格」。交易时段是 runtime 条件，
不计入资格。

## 13. 2026-11-15：Hy4 短线α判断与模拟交易自动化

短线α在 WorkBuddy 的 Hy4 分组有 3 个专属任务，均为 `ACTIVE`、
`model_id=deepseek-v4.1-flash`、`model_is_thinking=1`、cwd `D:\Hy4`，项目目录写在
prompt 中为 `D:/quant-desk`：

| 语义窗口 | 任务 | 职责 |
|---|---|---|
| 09:24 | `quant-desk · 盘前 短线α入场` | 只处理今天正当 T+1 的冻结权重，复用原 thesis/evidence/invalidation，走 propose→ticket→approve；实际已进连续竞价就如实写 intraday |
| 14:28 | `quant-desk · 尾盘 短线α退出` | 重读证据，冻结 exit_now 理由；执行已决定退出或最迟 T+2 的 SELL，不用收盘价冒充成交 |
| 19:10 | `quant-desk · 盘后 短线α判断` | 最新完整日线的 candidate/watch 与 α 权重冻结；既有持仓盘后复评，必要时冻结 exit_next_open 并提交 queued SELL |

线上正本分别冻结为 `0924-short-alpha-entry.md`、`1428-short-alpha-exit.md`、
`1910-short-alpha-judgment.md`。入场请求号为
`short-alpha:{T}:entry:{symbol}`，退出为 `short-alpha:{T}:exit:{symbol}`；review 只冻结
复评，execution 只读，二者本身都不下单。实际买卖仍由 DeepSeek 显式调用既有票据链，
guard 拒绝是结论，不换参数绕过。queued/proposed 不是成交。

19:10 的正常路径是：分页读取 evidence → 独立形成 candidate/watch → 按 α 分配
target_weight（candidate/watch 都允许）→ judge 冻结一次 → 对已有 short_alpha 持仓动态
复评。旧无权重记录保持 tracking-only，不追溯补权重或补订单。既有日更仍只负责研究
样本外结算，不把 T+1-open 机会代理写成真实成交。

09:26、13:35、14:30 三个既有任务追加了来源隔离条款：今日有待入场 short_alpha 时，
09:26 不用普通 BUY 争用现金；其他任务不重复管理 short_alpha 持仓或退出票据。组合风险、
T+1、现金和硬止损仍按内核现行规则执行，不因来源隔离而绕过。

本轮安装时 WorkBuddy 长文本编辑器出现截断占位且保存按钮无动作，因此先实读确认原任务
未被改动，再用单个 SQLite 事务备份并更新 4 个既有任务、克隆新增 2 个任务；提交后逐字
核验 prompt、模型、工作目录、排程和状态。安装回执为
`reports/20261115-short-alpha-automation-install.json`，订单创建数为 0，模拟与账外账本均未改。
首次安装/新增任务后必须重启 WorkBuddy：数据库已有 11 条而界面仍显示 9 条，说明运行器
尚未加载；重启后实测为「全部 11 / 进行中 11」。量化台 pywebview 若仍显示旧前端，也只
重启 `scripts/panel.pyw`；HTTP 已下发新 JS 不代表长期运行的面板进程已刷新模块。
自然调度与真实成交尚不能由安装回执替代，必须在后续交易日继续核验。
