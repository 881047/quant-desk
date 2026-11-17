# quant-desk

A-share paper-trading kernel for the DeepSeek Harness `quant-desk` plugin.
No real broker, no real money: a deterministic exchange/accounting body with an
LLM-native decision layer on top.

## What is (and is not) in this repository

This is the **paper-trading kernel and its decision layer**. It never connects to a
broker and never moves real money.

Deliberately excluded, because they belong to one operator's machine rather than to
the project:

| Path | Why it is not here |
|---|---|
| `data/` | market cache and the paper ledger — state produced by running the thing, tens of GB under `parquet/` |
| `reports/` | one file per trading day: reviews, reconciliations, and a real-money account's positions |
| `journal/` | daily notes |
| `configs/stops.json` | hand-set stop levels for one account's holdings |

`reports/...` links inside this README therefore point at files that are not shipped;
they are kept as a record of what was measured and on which day.

You bring your own data. `TUSHARE_TOKEN` and `THS_API_KEY` are read from `.env`
(see `.env.example` — both empty); without them the kernel only sees whatever you
have already stored locally.

## Quick start

```bash
cp .env.example .env            # fill in TUSHARE_TOKEN
uv sync                         # or: pip install -e .
python -m uvicorn service.api.app:app --host 127.0.0.1 --port 8811
python -m pytest tests -o addopts="" -q
```

## License

MIT — see [LICENSE](LICENSE).

## Status

> 以下按 P 序号记录**历史条目**：每条里的「N 个工具 / N 个用例 / N 个场景」都是**当时**的数，
> 不是现在的。当前值只有一个来源：公开工具数看 `plugin/lib/index.js` 的 `buildTools()`
> （现在 4 个，内部 handler 73 个），用例数看 `pytest -q`，窗口时间看 `docs/12-automation-chain.md`。
> 文件地图与「哪份文档在生效」见 [`docs/00-README.md`](docs/00-README.md)。

- **P0–P13 完成**：确定性内核（T+1、涨跌停、整手、费用、除权、账本、风控）、Parquet 数据层与 PIT、
  决策协议与 21 项风控、`next_open` 执行、统计裁判（DSR/PBO/CPCV/bootstrap）、策略注册表与归因、
  每日无人值守流水线。
- **P14 完成**：历史类比引擎（105 万行索引）、证伪账本、状态史、拥挤度、持仓体检。
- **P15 完成**：内核重新定位为**信息源**。删掉"建议层"（不再有 `action: SELL_ALL` 这种指令），
  改成只报事实、历史分布与条件测算的观察层。
- **P19 完成**：删掉"代码能自己下单"的最后三条路径——`stops.enforce`（止损强制执行）、
  `CoreBook.rebalance`（自动调仓）、`core_job --apply`。核心盘必须由调用方给权重，
  `/core/plan` 展示的规则分配不可执行（服务端与 `CoreBook.execute()` 双重拒绝）。
  43 个 DSH 工具，并有测试保证这些路径不会被加回来。
- **P16 完成**：实时行情（同花顺 → 东财 → 腾讯 → 日线，每条带 source/age），
  以及盘面结构（涨停/跌停/炸板池、连板天梯、封板率、晋级率、龙虎榜与游资席位）；
  43 个 DSH 工具。详见 [`docs/06-kernel-as-knowledge.md`](docs/06-kernel-as-knowledge.md)。
- **P17 完成**：把"覆盖多少历史"从口头变成可测量的一件事。
  日线/复权/估值回溯到 **2005-01**（1,617 万行、5,294 个交易日、5,894 只含退市），
  涨停池回溯到 **2020-12**（1,429 个交易日），跌停/炸板/龙虎榜到上游允许的一年。
  `scripts/coverage.py` 逐个数据集报告覆盖与**具体缺失的交易日**；
  类比索引从 105 万行扩到 **340 万行 / 3,000 日**，窗口改为从清单推导而不是写死。
  核心盘的 ETF 机会集从 9 只（写死在代码里）扩到 **18 个暴露**（配置驱动），
  并修正了"国债 ETF 被当成风险仓参与平价"的既有缺陷。
  详见 runbook [2.7 覆盖范围](docs/03-runbook.md) 与 [5.7 核心盘](docs/03-runbook.md)。
- **P20 完成**：给统计裁判装正控。此前 9 个 `rejected` 与"裁判坏了"在观测上不可区分；
  现在用注入真值 + placebo + 随机录取对照跑**真实的** `runner.run()`：
  α≥+0.5%/笔 时录取 10/10，α=0 与 placebo 各 0/5，同录取率下 lift **+60%**。
  判决阈值正好落在成本线上。合成世界高斯同方差，**TPR 是上界**。
  详见 [`reports/P20_referee_calibration.md`](reports/P20_referee_calibration.md)
  与 [`docs/07-two-way-kernel.md`](docs/07-two-way-kernel.md)。
- **P21 完成**：研究层改调内核 `CostModel` 逐笔计费，`cost_bps` 退化为滑点，
  `cost_mult` 乘"手续费+滑点"全部成本，票面按 `min(权益, max(权益/信号数, 一手))` 推导。
  10 个 preset 重判：**2 个 `testing → rejected`，仓位表 0 个 testing**；
  唯一候选（正股跌停→转债）+0.298% → +0.098%，p 0.078 → 0.339。
  在 ¥5,000 / 3 只持仓下，盈亏平衡 **0.85%/笔**、裁判放行 **1.7%/笔**。
  详见 [`reports/P21_cost_unification.md`](reports/P21_cost_unification.md)。
- **P22 完成**：全局试验登记。DSR 的分母从"单次 run 的网格"改为**族内全部搜索**：
  10 个 preset 累计 **124 次搜索**，min p 0.339、BH 调整后 FDR 1.0、q=0.10 下 0 survivors。
  同一 spec 重跑记为 `look` 而非新 trial。顺带修掉一个有损记录缺陷：
  旧 verdict 文件缺 `link_pool`/`rank_dir`，重放会变成另一个假设。
  详见 [`reports/P22_trial_registry.md`](reports/P22_trial_registry.md)。
- **P23 完成**：因子归因用**同日特征近邻**（市值/beta/60 日/反转/流动性）做基准，
  不估系数——在 300 笔样本上跑回归，残差天生 in-sample。特征只用 T 日收盘及之前，
  近邻与被判交易同 entry/exit 口径。结果：匹配让残差比等权超额高 1–25bps，
  **符号一个没变**；唯一候选残差 +0.13%/笔、t 0.6–0.7，`t<2` 一律不放行。
  详见 [`reports/P23_attribution.md`](reports/P23_attribution.md)。
- **P24 完成**：p 值改为**按日 block bootstrap**（同一天几十笔交易不是独立观测），
  并给每个判决附 MDE 与所需样本。10 个 preset 的 p 变动 ≤0.014、判决不变，
  但区间宽度差 √(笔/天)。**十个结构的 MDE 18–70bps、门槛 80–228bps，
  无一 `underpowered`**——不是看不见，是确实没有；唯一候选的 +8bps 要测准需约 40 年。
  新增 `underpowered` 判决，只用于"正效应、不显著、且设计看不见门槛"。
  详见 [`reports/P24_power.md`](reports/P24_power.md)。
- **P25 完成**：上线后的衰减监测。CUSUM + 滚动 z + SPRT 三路，状态机
  `insufficient / ok / watch / decaying`（单路报警只到 watch，两路一致或 SPRT 判定才降级）。
  基准只接受逐笔 `mean/std` 或 `cagr/sharpe`（近似，标注来源），没有 sigma 就判 `no_baseline`。
  今日真实输出：`core_etf` 有基准但 0 天净值 → `insufficient`，其余 `no_baseline`。
  输出不含任何 action 字段——**它不下单**。
  详见 [`reports/P25_decay.md`](reports/P25_decay.md)。
- **P26 完成**：人格层。三种决策风格（游资 / 散户 / 交易员）各自绑定 sleeve、周期、
  regime 与**数据前提**；切换必须带 `reason` / `evidence` / `expected_edge_bps` /
  `invalidation`，每天上限 2 次，提案的 `persona` 必须等于当前人格。代码**不自动选人格**
  （`suggest` 只做数据与 regime 两道硬筛），切换本身在 `/persona` 的 scorecard 里与
  同期等权市场结算。同批修掉核心盘净值缺陷：`core_nav` 只在 `execute()` 时写，
  空仓日无记录 → `/decay` 对 core_etf 永远 `insufficient`；现在每个交易日写一行。
  日更任务从 16:10 改到 **17:30**：Tushare 的 `daily` 官方入库窗口是 15:00–17:00，
  16:10 落在窗口内，慢发布那天会抓到半个交易日。46 个 DSH 工具，390 passed。
  详见 [`docs/08-personas.md`](docs/08-personas.md) 与
  [`docs/04-scheduler.md`](docs/04-scheduler.md)。
- **2026-10-26 运营修复**：中午一次咨询产生 **241 次提案、0 张票据**，事后无法复盘——
  日志丢 53%、计数把失败重试当工作量、账本里还有一张没有代码支持的影子表。
  修完之后：access log 独立成 `data/access.log`（带时间戳、午夜轮转）、
  `usage` 按状态码分 ok/fail、4xx 熔断 30 次/60 秒、`docs/API_CONTRACT.md` 列出
  64 条真实路由与 5 条不存在的路径、`fill_basis` 跟随 `book.fill_mode` 并新增
  `backfill` 标签、CORE 袖不再占用 llm 额度、影子表每天上 `/duty`。
  另外补上从未存在过的 `quant-desk-daily`（工作日 16:10），当天数据已补到 20261026。
  详见 [`reports/FIX_20261026.md`](reports/FIX_20261026.md)。
- **P27 完成**：内核算子与裁判口径的两处 P0 修正（2026-10-27 审查发现）。
  1) **撮合不再用收盘价定价**：原来 `bar.is_limit_up`（判的是 close）决定买入价，
     于是"当天收盘涨停"的市价买入按涨停价成交（实测开 10.00 → 成交 11.00），
     是 look-ahead，且恰好命中事件策略的全部样本。现在只按 `open` 判可否成交
     （开盘即涨停/跌停 → 拒单），成交价永远只来自 open ± 滑点。
  2) **裁判按全交易日年化**：`portfolio_series` 只返回有持仓日，`metrics` 却按
     `n/252` 年化。同一批 233 笔、同一变体：CAGR **10.46% → 3.96%**、
     Sharpe **0.47 → 0.29**（= ×√暴露率）。
  3) **新增账户层** `service/research/capital.py`：同一批交易在 ¥5,000 / 3 个仓位 /
     整手 / 现金约束下逐日撮合，产出"已实现现金净值"。唯一候选
     `cb_link_down_rich` 由此从 `testing` 变 **`rejected`**：信号层 +0.105%/笔，
     账户层 **−23.0%**——一手 ¥1,241 的票，¥5 最低佣金来回就是 86bps，
     而买得起的子集毛收益只有 48bps。边缘在买不起的票上（跳过组毛收益 0.68%）。
  4) 订单语义补全：市价单只撮合一次（当日 IOC，`IOC_NO_FILL`），限价单收盘作废
     （`EXPIRED_AT_CLOSE`），`Bar.seal_amount` + `derive_fill_ratio()` 让封单数据
     可以落成 `fill_ratio`；CPCV embargo 由 `hold` 改为 `hold+1`（标签跨 hold+1 天）。
  5) 费率只留一份：`Instrument` 是唯一来源，`CostModel` 只做"max(最低佣金, 费率×金额)"，
     `trading/fees.py` 退化为读配置 + 拆明细。ETF / 可转债用**零费率**表达"不交印花税"，
     `Instrument` 会拒绝"is_etf 却带印花税"的组合。可转债复核 `round_trip(5000)=¥10.00`，
     研究结论逐位不变。
  6) 两条定价路径合成一条规则：`kernel/microstructure.py` 的 `blocked_at_open` /
     `fill_price_at_open` 同时服务内核与研究层（后者用向量化版本，共用 `PRICE_EPS`），
     并有逐分对拍测试。`/liquidity` 现在分开报 `fill_cost_bps`（账本真收的）与
     `impact_bps`（只估计、不进账本），不再出现"报 38bps、记 25bps"。
  7) 卖出侧补上跌停约束：`build_trades` 原来把"收盘跌停"也当成能卖掉。实测 **3.2%–3.8%
     的退出落在跌停收盘**，那些交易的毛收益是 **−8.7% / −7.5%**。现在卖不掉就顺延一个交易日
     （`rolled`），顺延那天作为额外持仓腿进入净值序列；连续两天无买盘则标记 `stuck`。
     panel 前向列扩到 `f4` 并补 `down_f{k}`。同一批交易：`limit_up_first` 毛收益
     −0.132% → **−0.209%**、Sharpe −5.76 → **−6.12**；`broken_board` +0.001% → **−0.087%**。
     不改变任何已有判决（本来就都是 rejected），改的是"这些策略到底亏多少"。
  18 个 golden 场景、549 个用例（2 skip）全绿。详见 [`reports/P27_kernel_fixes.md`](reports/P27_kernel_fixes.md)。
- **P28 完成**：把 `discover` 的**读数**变成**先验**。新增 `service/research/state.py`：
  市场状态 = 宽度三分位 × 20 日收益三分位（9 格），每格给出之后 5/20 日的分布、
  **段数**（连续出现次数，不是天数）与样本跨度。前向收益从**次日开盘**起算，
  信号日那根跳空不计入；分位只用截至当日的 252 个交易日（有测试：截断未来不改变今天）。
  2005+ 全样本 9/9 桶可用。今天的 `low|low`：全样本 20 日 p50 **+3.17%**（n=1329、205 段、胜率 65%），
  近三年 **+2.22%**（n=223、胜率 59%、p25 −4.23%）——**方向一致、幅度衰减、左尾变厚**。
  已接进 `discover` 的 `state` 块（+839 字节），按最后一个交易日失效的缓存（建 12s / 缓存命中后
  `discover` 7.3s）。踩过一个坑：切片按桶内行号取而不是按日历取，导致横跨 19 年的桶自称
  "100% 在近一年"；已修并加回归测试。559 个用例（2 skip）全绿。
  详见 [`reports/P28_state_history.md`](reports/P28_state_history.md)。
- **P29 完成**：补上「模型的主张 → 可检验对象」这条线。`service/research/claim.py` 把一句中文
  主张编译成三种形式之一（`bucket` / `overlay` / `event_study`），或给出**带缺项的拒绝**；
  `bucket` 当场判，`event_study` 输出可直接跑裁判的 spec。三条硬规则：无 `eval`、
  翻译的假设必须外显、拒绝是结果。
  首个真实结论：**「跌多了会反弹」在数据上没有支持**——`low|low` 之后 20 日 +2.68%，
  而平均日 +4.19%，差 **−1.51%**（p=0.136，202 段）。
  这条反过来修了 P28：`state` 块原来只给桶的分布、不给基线，会被读成 edge；现在补了
  `baseline` 与 `edge_5d/edge_20d`（段加权，与检验同口径），当前 `edge_20d = −1.51%`。
  575 个用例（2 skip）全绿。详见 [`reports/P29_claim_compiler.md`](reports/P29_claim_compiler.md)。
- **P29 修订**：`claim.evaluate()` **不再返回判决**。原第一版返回 `verdict`，那是代码在替模型下判断。
  现在只给 `evidence`（分布、p、样本、口径）；要记账时用 `label_under(policy)` 套一个**具名阈值**，
  和结果一起打印、可替换。分工是：**代码判合法性与算术，DSH 判解释。**
- **P30 完成**：把上一条落到两处遗漏。
  1) `service/research/overlay.py`：overlay 主张的**双臂对比原语**——同一批交易跑两遍，
     输出两臂分布、配对差与区间、以及「规则真正改变了哪些笔」的子集，**没有 verdict**。
     触发依据收盘价（日线给不出盘中路径），成本不随交易剪短而变小，配对差报两次（全样本/仅受影响）。
  2) 裁判 `_verdict()` 返回**审计链**：每条闸门的值、算子、阈值、失败等级，
     外加晋升闸门（即使 rejected 也输出）。`cb_link_down_rich` 判决与 P27 逐项相同，
     只是现在能看到是 `p=0.268 / 2x-Sharpe −0.68 / 账户 −23.01%` 这三条在拒它。
  3) 主张编译器：阈值必填（「加个止损」被拒绝）、`持有 N 天` 识别为 overlay、事件优先于规则。
  599 个用例全绿。详见 [`reports/P30_overlay_and_policy.md`](reports/P30_overlay_and_policy.md)。
- **P31 完成**：把「每一次 look」记进 `TrialRegistry`，而不只是 `run()`。
  `bucket` 检验、`overlay` 对比、`event study` 进同一张表，身份哈希分别是
  `bucket_hash` / `overlay_hash` / `spec_hash`；`n_variants` 表示这一次 look 含多少次搜索
  （event study 记整个网格，另两者各记 1），`variants_total` 才是 DSR 的分母。
  **账本开门第一天抓到两个错**：① bootstrap p 值被写成 0（2000 次重采样的分辨率下限是 1/2001），
  在 BH 里排到第一，成了一个假发现；② `--no-record` 连**读取**分母也跳过了，
  于是不记账就能让 Sharpe 更好看。两者都已修，各有回归测试。
  族现状：12 条 look / 126 次搜索，`min_p 0.0005`，BH 后 0.006，q=0.10 存活 1 条
  （5% 止损，减完仍 −1.09%/笔——BH 存活 ≠ 可交易）。账本是 gitignore 的 sqlite，
  因此另有 `reports/trial_ledger.json` 快照（`scripts/export_trials.py --check` 可自查），
  快照里分母**变小**就是被删过的证据。608 个用例全绿。
  详见 [`reports/P31_look_ledger.md`](reports/P31_look_ledger.md)。
- **P32 完成**：回收。一条 trial 被后续证据推翻时，追加一条 `supersessions` 记录
  （`trial_id / superseded_by / reason / created_at`），**不删行、不改分母**——
  `program_status` 里写着 `denominator_includes_superseded: true`，
  因为「把这些行清掉」这个念头一定会在某个时刻出现。
  新增 `superseded / live / superseded_share / superseded_effect_mean / survivors_live`
  五个数；`superseded_effect_mean` 若显著为正，说明被推翻的曾是看起来有希望的那些，
  那才叫自我修正。**判断留给 DSH**：`scripts/supersede_trial.py` 只负责在写之前把两条
  trial 并排打出来、要求写理由。当前账本 0 条回收、12 条在场（诚实：确实还没有哪条被真正推翻）。
  617 个用例全绿。详见 [`reports/P32_supersession.md`](reports/P32_supersession.md)。
- **P33 完成**：持仓提醒的规则层退到事实层。`Alert.why`（九条自由发挥，其中一条直接说
  「先减风险」）换成 `Alert.ref`——**只能引用，且必须标出处**（计划/规程/算式），
  六条有出处、三条留白（留白的正是原来塞判断的地方）。`analyst.note()` 的默认后端由
  `harness`（一整个 agent turn）→ `llm`（另一个模型）改为 **`handoff`**：
  规则写完问题就停，解读在 quant-desk 工作区用便宜模型回答。
  问题卡 1,409 字节：编号事实 + 引用 + 持仓 + 可选背景 + 输出契约（每句必须引用一个数字，
  事实不足就说缺什么）。新增 `GET /monitor/handoff`、`POST /monitor/handoff/answer`、
  插件工具 `quant_alert(action="list")`；`service/trading/handoff.py` 承接读/答/渲染，
  `scripts/handoff.py` 退为薄壳。628 个用例全绿。
  详见 [`reports/P33_monitor_interpretation.md`](reports/P33_monitor_interpretation.md)。
- **P34 完成**：补上 P33 留的两处半。
  ① 问题卡接入**背景块**（`low|low` 的分位、其后 5/20 日分布、n 与段数、口径日期），
  1303 → **1667 字节**；只在**真有触发时**才算（5.7s），取不到就留空，不阻塞提醒。
  ② 所有 `ref` 改由 `CITATIONS` 注册表经 `cite()` 生成，标签与措辞结构上绑定；
  标了 `playbook` 的条目由测试核对**当前生效版本**是否仍含该短语——**引用漂移在这里是构建失败**。
  ③ `_via_llm` 没删（`ANALYST` 是环境变量，删了可能静默改变定时任务），
  但让它自称「外部模型，非工作区」，出现在日志里。
  633 个用例全绿。详见 [`reports/P34_citations_and_background.md`](reports/P34_citations_and_background.md)。
- **P35 完成**：背景按人格取窗口。`state.HORIZONS` 从 `(5,20)` 扩到 `(1,2,5,20)`，
  缓存加 `SCHEMA` 版本号（原来只按最后交易日失效，加窗口会静默拿到缺列的旧 frame）；
  `block(horizons=)` 按问题裁剪，读取只遍历实际存在的列。
  人格映射 `hot_money→(1,2) / trader→(2,5) / retail→(5,20)`，**只能挑不能造**
  （不在构建表里的窗口抛 ValueError）。游资额外两行涨停结构位置 + **一句「未建」**：
  封板率历史仅 243 个交易日，撑不起分布，所以只报位置。
  顺带发现同一个桶 `low|low` 的 edge 在 1-2 日是 **+0.3~0.4%**、在 20 日是 **−1.5%**——
  同一个状态，两个窗口方向相反，而这正是游资与散户该看的东西。
  638 个用例全绿。详见 [`reports/P35_persona_windows.md`](reports/P35_persona_windows.md)。
- **P36 完成**：**炸板不用等上游**。P35 判断「卡在数据上，要补炸板池历史」是错的——
  炸板 = 日线高点触及涨停价、收盘未封住，而 `stk_limit` 按年分区覆盖 2010–2026。
  于是封板率的样本从 **243 天变成 4,081 天**（`service/research/tape_state.py`）。
  前向指标 **必须是 T+1 可执行**：第一版「次日开盘买、当日收盘卖」算出 +0.29%，
  那是同日回转、拿不到；改成本条「次日开盘买、第三日收盘卖」。
  顺带把两个缓存的过期检查改成先查缓存（`latest_bar_date()` 3ms）：
  背景耗时 **15.0s → 0.03s**。
  详见 [`reports/P36_tape_derived.md`](reports/P36_tape_derived.md)。
- **P37 完成**：**推导的分位可以当上游用，但那个 +0.9% 是 2015-19 挣的**。
  同窗口（120 日）下两边分桶：封板率同桶 **85.5%**、涨停家数 **87.3%**，
  **远桶 0.0% / 0.1%**（从不在「冷还是热」上反向）；但尾档 95% 一致、中档只有 63%，
  所以卡片只在落中档时附一句。第一次比出 60.5% 是窗口长度造成的假象，不是序列差异。
  更要紧的是前向：`low|low` 全样本 +0.954%，分段后
  **2024-今 −0.28% / 2020-23 +0.26% / 2015-19 +2.47%**——2015-19 一段贡献 76%。
  （其中「2024-今 −0.28%」被 P38 推翻：加上区间后它横跨零。）
  4,081 天给的是精度不是时效，卡片改成近端在前、并指出全样本是哪一段挣的。
  校准落成可重跑的命令（`scripts/tape_calibration.py --check`，低于 80% 退出码 1），
  `provenance` 不再带写死的常数（旧测试在断言那个错数字，已改）。
  详见 [`reports/P37_tape_calibration.md`](reports/P37_tape_calibration.md)。
- **P38 完成**：**5% 板查清了，但它把 P37 的一半结论推翻了。**
  年比 1.255→1.041 的下降确实来自 5% 板：上游对它的收录率从 2021 的 **39%**
  升到 2026 的 **82%**（不是「不收」，是覆盖率在变），而 30% 板（北交所）
  每一年都是 **0%**（稳定排除）。
  更要紧的是：换成分口径重算 `low|low` 的近年均值会**符号翻转**（−0.28% → +0.17%），
  但在同一批 684 天上剔 ST 只动 **0.08pp**——所以翻转来自**桶成员换了十天**，
  不是成分。结论是那个数从来**没精确到能承载一个符号**。
  段级 bootstrap 后：2015-19 **+2.62% [+1.99, +3.28]**（远离零），
  2024-今 **−0.09% [−0.67, +0.47]**（横跨零）。P37 说「近三年为负」是算术对、推断空。
  不改口径（成分只值 0.08pp，改了像是挑好看的定义），改精度：卡片近端改为
  **段均 + 区间**，`[−0.6%~+0.4%]` 让「近三年为负」这句话写不出来。
  673 个用例全绿。详见 [`reports/P38_band_effect.md`](reports/P38_band_effect.md)。
- **P39 完成**：**20% 档确实跨了制度，但被切开的其实是 10% 档。**
  创业板 2020-08 才从 10% 改成 20%，所以 2015-19 的「10% 板」里有近三成是创业板
  （21,682 笔），2024-今 是 0——**按幅度分档本身随制度漂移**。
  顺带查清「档位之外」的 1,612 笔：**97% 是上市首日、幅度 44%**（发行价 × 1.44），
  占 2015-19 封板的 1.31%、2024-今 的 0%。
  只有主板全程 10%，所以加了四个口径的稳健性检查：2015-19 **+2.62/+2.84/+3.02/+2.87**
  全部远离零，2024-今 **−0.09/+0.11/+0.17/+0.17** 全部横跨零，**而桶标签只一致 74.8%**
  ——结论不依赖口径。2020-23 只在最宽口径下越过零，同样不能说成一个方向。
  新增 `board_of` / `band_of`、`tape_calibration.py --robustness`。
  676 个用例全绿。详见 [`reports/P39_rule_change.md`](reports/P39_rule_change.md)。
- **P40 完成**：**把三次修正合成一次运行**（`tape_state.audit`）。
  P37 修区间、P38 修符号、P39 修口径，三次形状一样：一个算术正确的数字被当成它撑不住
  的断言。现在一次查完三件事，返回 `sides` 计数而不是判决（有测试钉住不许出现
  `verdict` 字段）：
  `--audit 'low|low' --era 2024-今` → **crosses 4/4**；
  `--era 2015-19` → **positive 4/4**。而两段的桶标签一致率都只有 62.5% / 66.1%
  ——**标签不稳本身不否决结论，四票是否同侧才否决**。
  审计只打印不写快照（它是一次查询，不是常设测量）。
  查这一轮时发现：`state.py` 里 `ci_low`/`bootstrap` 出现 **0** 次——
  **区间的修法只到了 tape 侧，`retail`/`trader` 卡片上那个「较平均日 +0.4%」至今没有区间。**
  684 个用例全绿。详见 [`reports/P40_audit.md`](reports/P40_audit.md)。
- **P48 完成**：**把"信息有多全"从感觉变成一个能下降的数字**。12 个维度、0–100 分，
  现为 **88.2**（`/data/information`、`quant_query(resource="information")`）。三件事必须先说清楚：
  ①`limit_pool_t0` 的 2026 分区只有 1 天、且 `boards` 全为 1——`scripts/coverage.py`
     看不见它，因为校验只查"覆盖区间内的缺失日"，而这里两端都有值、中间空了两百天。
     带预热重建后 196 天 / 28,155 行，2026-10-28 连板高度 **1 → 5**，与同花顺、东财三源一致。
  ②`derive_pools.py` 原来按年分段计算连板，每年 1 月 1 日都会把跨年连板截断；只跑一天更是全员 1 板。
     现在整窗计算再按年落盘，`--warmup-days` 提供只读预热。
  ③`discover.core` 的 `benchmark` 是后复权价（close×累计因子），读作 **5.261**，而 510300 当天
     成交价是 **4.152**；`bond_lot_cny` 同样被放大，**14,716 → 14,122**。前者让模型对不上报价，
     后者是"能不能买国债腿"的判据。
  新增按品种探测的实时报价维度：股票走同花顺（2–4 秒），ETF 与转债走腾讯/东财。
  探测最初报"沪市转债没有通道"，查下去是**同一个 bug 的三份拷贝**：`_market_of`、
  `_prefix`、`_symbol_of` 各自决定沪深，前两份没有转债分支，沪市转债被发成
  `sz111012` / `0.111012`，供应商当然什么都没回。现在统一到 `service/adapters/symbols.py`。
  同一轮还修了回退链：**部分命中会遮蔽其余标的**——混合请求里同花顺只回了那只股票，
  循环就 break，ETF 和转债悄悄退回昨日收盘。现在按标的逐个回退。
  盘中数据接进调度：`quant-desk-snapshot-0925/1030/1330/1445`（工作日四次）+
  `quant-desk-ths-close`（15:20，TODAY_ONLY 数据漏一天不可回补，故与 17:30 双跑）。
  `scripts/tool_bandwidth.py` 量化每次工具调用的字节与 token：34 次全量 **155KB≈41k token**，
  最贵的 `journal` 11 条 6,038 token，最小开工 4 次 2,250 token。
  据此做了两处压缩：`/journal` 默认只给 120 字摘要 + 前 3 条全文（`full=1` 取回全量），
  **22,087B/6,038token → 8,041B/2,209token**；`/memory/search` 同样处理。
  给行情源加了熔断：连续 3 次异常后跳过 300 秒，`/market/quote/health` 暴露状态。
  实测东财 `push2` 挂掉时，报价从 **7.9 秒 → 0.11 秒**。
  再往下查，慢的根因都是同一种写法：**用全量读取换一个末尾窗口**。
  `DatasetStore.read` 现在按清单里的 `cover_start/cover_end` 裁剪分区（覆盖缺失就照读，
  宁可多读也不误删），`universe` 从读 22 年改成读 75 天，`discover` 从读全量改成读 420 天，
  判 ST 从"每只票过滤一遍名称变更表"改成一次 groupby。实测：
  `universe` **9.7s → 0.5s**、`discover` **9.9s → 2.6s**、`context_pack` **5.7s → 2.5s**、
  `limit_structure` 5.0s → 2.1s、`/data/information` 冷启 22.8s → 7.6s（热 0.012s）。
  同一类问题在归因侧也有一份：`arena._market_returns` 重建 20 年等权收益只为了查几个持仓窗口。
  现在窗口由被结算的记录本身推导（早于最早一笔 10 个日历日，因为 `_window_return`
  锚在入场前那根收盘），取不到就退回全量。`attribution` **9.5s → 0.37s**、
  `arena` **4.7s → 0.17s**、`persona` **6.6s → 1.9s**（0 次切换时直接短路，不再白读）。
  顺手把三个模块里三份符号约定不同的 `_shift` 收成一个 `_days_before`——
  其中一份用负数调用时会把日期往后推，有界读取于是静默返回 0 行。
  工具带宽总量：**155KB/41k token → 128KB/34k token**。
  补上一直没算的**固定开销**：50 个工具定义 23,062B ≈ 5,747 token + 提示词 8,955B ≈ 2,575 token
  = **每次请求 8,322 token**（这是当时的 50 个工具；现在 67 个，当前值见下），
  与返回内容无关。`plugin/tools_size.mjs` 量它，
  `scripts/tool_bandwidth.py` 把固定开销与四条决策链路并排打印，于是"一次决策的窗口压力"
  第一次是个完整数字：最小开工 10,676、研究一票 17,919、完整决策 16,430 token。
  熔断状态落盘（`data/cache/breakers.json`，墙钟存取、只在状态翻转时写）：
  重启后第一次报价 **8.4s → 0.83s**，不必重新失败三次。
  833 个 Python 用例 + 7 个 Node 用例全绿（全量单测从 467s 降到 388s）。

## 分工（这条比任何指标都重要）

内核给**事实、历史分布、测算**；模型给**判断**；执行层只负责**合法性**。

- 内核不替你买卖，**一笔都不**。没有代码写的止损、没有自动再平衡、没有定时调仓：
  `stops.enforce` / `POST /risk/stops/enforce` / `core_job.py --apply` 都已删除，
  并由测试保证不会被加回来。止损只是状态（距离、连续几日、次日退出成本），报告完就停下。
- 想动钱只有一条门：`/decision/propose + approve`（个股）。核心盘走 `/core/execute`，
  但**权重必须由调用方给出**——`/core/plan` 展示的规则分配不可执行，服务端和 `CoreBook.execute()`
  都会拒绝由规则生成的计划。照抄规则权重是允许的，只是那时决定权在你，日志里记的也是你。
- 唯一的硬约束是风控：有持仓收盘价低于它自己的止损时，新开仓返回 409 `OPEN_VIOLATION`；
  卖出永远不受限。这是按价格算的规则，不是"等你回消息"。
- 模型不能编数字：提案 schema 里没有 `qty` / `price`；每个历史统计都带 `n`、区间、出处和 caveats。

## Quick start

```bash
cd D:/quant-desk

# 1) 测试（离线，不碰网络）
.venv/Scripts/python.exe -m pytest tests -q

# 2) 数据权限探测
.venv/Scripts/python.exe scripts/probe_permissions.py

# 3) 回填（可断点续跑；中断后重跑即可）
.venv/Scripts/python.exe scripts/backfill.py --datasets p1 --start 20230101 --end 20261024

# 4) 数据质量报告
.venv/Scripts/python.exe scripts/verify_data.py --start 20230101 --reconcile

# 5) 盘中快照（交易时段运行）
.venv/Scripts/python.exe scripts/snapshot_intraday.py --loop 60

# 6) 本地 API
.venv/Scripts/python.exe -m uvicorn service.api.app:app --host 127.0.0.1 --port 8811

# 7) 接进宿主（DSH / WorkBuddy）
node plugin/install.mjs        # DSH：插件 + /quant-desk 技能
node plugin/mcp/install.mjs    # WorkBuddy：MCP 连接器 + 同名技能
```

内核有三个入口，同一套账本与风控：HTTP API（`127.0.0.1:8811`）、DSH 插件
（`plugin/lib/index.js`）、MCP stdio 服务（`plugin/mcp/server.mjs`）。后两者都是
薄客户端，4 个公开 `quant_*` 工具只有一份定义，73 个内部 handler 继续复用原路由，
改一处两个宿主同时生效。

双击桌面上的 `quant-desk`（或 `panel.bat`）打开面板——一个独立窗口，不是网页：
实盘（总额、对账、持仓与纪律位）、同步账户（改持仓、记一笔成交，佣金税费自动算）、
今日动作（提案/成交/被拒/备忘/换人格/复盘的时间线）、系统（模拟账户、待办、擂台、
数据是否就绪）。机器读的那份是 `/dashboard.json`；17:30 的数据任务把当晚冻结到
`reports/daily/<日期>.dashboard.json`。

## Layout

- `service/kernel/` — pure domain, no I/O and no wall clock: `units` (market constants), `models`
  (Decimal money, `Instrument`/`Bar`/`Account`/`Order`/`Fill`), `clock`, `calendar`, `costs`,
  `microstructure`, `execution` (bar → fill), `oms` + `ledger` (the only writers of cash and
  share counts on the trade path), `corp_action`. `risk` + `engine` are a reference
  implementation for `tests/golden/` and are not imported by production
  (`tests/kernel/test_boundaries.py` enforces both this and the no-I/O rule).
- `service/adapters/` — catalog, Parquet store, SQLite registry, Tushare/EastMoney adapters, quality, PIT.
- `service/policy/` — core ETF/value, aggressive, LLM policy.
- `service/research/` — factors, CPCV/DSR/PBO, registry.
- `service/api/` — account/context + `/data/status`, `/data/quality`, `/data/fingerprint`.
- `plugin/` — DSH 插件（4 个公开 `quant_*` 工具 + 73 个内部 handler + 技能）；`plugin/mcp/` 是同一张工具表的 MCP 适配层。
- `tests/golden/` — 12 deterministic execution scenarios.
- `docs/` — 规范与历史快照，**[地图见 `docs/00-README.md`](docs/00-README.md)**（日期不是分类依据）。
- `playbook/` — 交易规则，**[版本状态见 `playbook/README.md`](playbook/README.md)**（现行以 `meta.json` 的 `active` 为准）。
- `reports/` — 每天的报告与专项留档，**[读法见 `reports/README.md`](reports/README.md)**（没有“最新一份”）。
- `tests/data/` — store/ledger/quality/PIT/backfill/adapter tests (no network).
- `scripts/` — permission probe, backfill, verify, intraday snapshot.

## Data guarantees

- 每个分区都有 `source / trust_tier / as_of / ingested_at / snapshot_id / checksum`。
- `snapshot_id` 由内容哈希生成：同样数据重复入库得到同一个 id，决策可复现。
- 质量结论三态：`ok / warn / quarantine`；`quarantine` 分区对 `read()` 不可见。
- 行级缺陷（零价、OHLC 矛盾、重复键）被修复并写入 `data/rejects/`，不会因一行坏数据冻结整年。
- 财务数据只用 `ann_date` 做 PIT，`assert_no_lookahead` 会在越界时直接报错。

## Hard rules

- Money is `Decimal`; shares are `int`; time is tz-aware `Asia/Shanghai`.
- No business code calls `datetime.now()`; only `LiveClock` does.
- Backtest and paper share the same `TradingEngine.step()`.
- No real broker adapter in this repository.
