# 后端债务清单 · 从前端显示反查（2026-11-08）

方法：前端逐页巡检时暴露的每一个可疑读数，都回到 8811 实测取证（响应体、耗时、
字段结构），确认根因在后端才收录。每条都给出：**界面表现 → 实测证据 → 根因 → 修法**。
范围不含纯后端内部问题——只收录"前端已经被迫打补丁、或界面上能看出破绽"的部分。

结论：13 项，按危害分四组。A 组不修会骗到下一个消费者；B 组是核对链路差的最后一步；
C 组让前端永远在猜；D 组是性能与语义细节。文末附前端临时补丁清单——后端修复后应撤。

---

## A. 单位与编码（不修会骗到下一个消费者）

### A1. 同一接口两种量纲

- **界面表现**：涨停池「占净值」曾显示 1000%、6780%。
- **证据**：`GET /market/pools` 同一响应里 `seal_ratio=0.0511`（比例）与
  `pct_of_equity=10.0`（百分数）并存，无任何命名或元信息区分。
- **根因**：序列化层没有量纲约定；两个团队年代不同的字段混在同一结构里。
- **修法**：统一为比例；或百分数字段一律加 `_pct` 后缀（`pct_of_equity` 已符合，
  违规者是 `seal_ratio` 一侧需改名或在 meta 里声明单位）。
- **优先级**：高。前端现在靠"每个字段记住要不要 ×100"活着，这是债务利息。

### A2. 内部编码直接出接口

- **界面表现**：涨停池「首封」列显示 `1_开盘09:30-10:00`。
- **证据**：`seal_window="1_开盘09:30-10:00"`，前导 `1_` 是内部窗口编号。
- **修法**：拆成 `window_no`（int）与 `window_text`（"开盘09:30-10:00"）两个字段；
  或者至少在序列化前剥掉前缀。
- **优先级**：中。前端已用正则剥，但"接口给机器读的编码让人来剥"方向反了。

### A3. 日历数据集造成"未来数据"

- **界面表现**：内核页数据血缘按覆盖日排序时，2027-12-31 的交易日历排第一，
  看起来像未来数据。
- **证据**：`/data/status` 中 `trade_cal` 与 `share_float` 的 `cover_end=20271231`。
- **根因**：日历类数据集的"覆盖到未来"是正常业务语义，但接口没有类型标志，
  任何按 `cover_end` 的通用排序/新鲜度逻辑都会误判。
- **修法**：数据集记录加 `kind: 'calendar' | 'data'`；新鲜度与排序逻辑按 kind 分流。
- **优先级**：中。

---

## B. 核对链路差的最后一步（"当场核对"只报警、不给坐标）

### B1. journal 核对的 unmatched 是裸数字

- **界面表现**：复盘日志显示「核 39 · 错 8 · 待修：5、84、4」——用户**无法知道
  5 指哪一句话**，核对等于只报警不给坐标。
- **证据**：`GET /journal` 的 `verified.unmatched=["5","84","4"]`，
  另有 `matched=31, checked=39, sign_from_words=["-1"]`。
- **修法**：`unmatched` 改为对象数组 `{value, excerpt, field}`——值、所在句子片段
  （前后各 ~20 字）、数字角色（价格/百分比/股数）。前端据此渲染成可点击定位的清单。
- **优先级**：高。这是"写日志会被当场核对"这个核心卖点的可用性瓶颈。

### B2. sign_from_words 结构含义不明

- **证据**：`sign_from_words=["-1"]`——是"从『减 1』这个词抽出的符号"还是抽取结果
  是 -1？无文档、无字段名解释，前端不敢消费。
- **修法**：改名并结构化，如 `sign_hints: [{word: '减', value: -1}]`，或直接并入 B1
  的核对条目。
- **优先级**：低（随 B1 一起做）。

### B3. not_scored 只有计数没有 id

- **界面表现**：复盘页显示「收盘票据未识别 ×1」，但没法知道是哪张票。
- **证据**：`GET /arena` 的 `not_scored={"unknown_closing_ticket": 1}`。
- **修法**：值从计数改为 `{count, ticket_ids: [...]}`；前端把"告警"落地成"待修入口"。
- **优先级**：中。

---

## C. 词汇未收敛（前端被迫每次看着数据现编中文）

### C1. decisions 的 kind/actor 无枚举约束

- **界面表现**：决策历史里出现英文原样的动作名。
- **证据**：实测数据含 `kind="proposal_blocked"`、`actor="guard"`；前端映射表在
  2026-11-07 还把 kind 拼错过一次（`propose_blocked`），直到反向审计对出原文才修正。
  **拼写差一个字母，映射就整个失效**——这正是无枚举的代价。
- **修法**：`service/trading/decisions.py` 里定义 `Kind`/`Actor` 的 Literal/Enum，
  序列化处单点维护；值域变更时前端只改一张表。
- **优先级**：中。

### C2. guard 的 detail 全英文

- **界面表现**：被拒票据的检查表里，说明列是 `0/4 fills today`、`0.02% of ADV20`、
  `daily/adj_factor/stk_limit cover the session`——这是**用户最常看到的风控文案**。
- **修法**：二选一——
  ① guard 文案中文化（改 `service/trading/guard.py` 的字符串，测试样例同步）；
  ② 检查项返回结构化 `{metric, actual, cap, unit}`，文案交给前端拼。
  ②更稳：数字不嵌在句子里，前端可以统一单位与对齐。
- **优先级**：高。风控文案是决策时的关键读数。

### C3. decay 的 note 是英文句子

- **界面表现**：衰减表说明列出现 `need at least 20 closed trades, have 5`。
- **证据**：`GET /decay` 的 `note` 为自由文本；前端用正则
  `/^need at least (\d+) closed trades?, have (\d+)$/` 抓可变参数翻译。
- **修法**：结构化 `{reason: 'sample_below_min', need: 20, have: 5}`，
  或已知句型给出枚举 reason + 参数字段。
- **优先级**：中。

---

## D. 性能与语义细节

### D1. /persona 冷调用 1963 ms

- **证据**：非交易日实测 1963 ms——它在同步探行情。
- **连带伤害**：内核页决策管线曾为它整块空白（前端已绕行 dashboard 的人格字段）。
- **修法**：结果缓存 60 s，或把行情探测挪到后台定时刷新。
- **优先级**：中。

### D2. /symbol/profile 冷调用 1463 ms

- **证据**：11 KB 响应耗时 1463 ms（bars 250 根只要 322 ms/38 KB）。
- **修法**：`identity`/`tradability` 等日内不变段做进程内缓存（按交易日+代码键）。
- **优先级**：中（个股页首屏瓶颈）。

### D3. delist_date=null 语义歧义

- **证据**：`GET /symbol/{s}/profile` 的 `identity.delist_date=null`——
  "未退市（正常股）"与"未知"不可区分。对退市 guard（`sessions to delisting`），
  这个字段是命门。
- **修法**：null 仅表示未知；正常股给显式值或增加 `delist_status` 字段。
- **优先级**：低（当前持仓不受影响，但 guard 依赖它）。

### 正例（值得推广的空态设计）

- `GET /market/frame` 非交易日返回
  `{ok:false, error:'no_frames', note:'抓取进程是 scripts/snapshot_intraday.py --loop 30…'}`——
  错误码、原因、补救路径齐全。前端据此区分"没帧"与"坏了"。**其它端点的空态应向它看齐。**

---

## 前端临时补丁清单（后端修复后应撤）

> 状态更新见文末「修复记录」：**8 处全部已撤**（D1 的 `/persona` 绕行在第四轮
> 随启动预热一并撤除）。下表保留原样作为审计记录，别照着它判断当前代码。

| 前端位置 | 补丁内容 | 对应后端项 |
|---|---|---|
| `web/assets/js/pages/market.js` | `pct_of_equity` 不乘 100、`seal_ratio` 乘 100 的字段级换算 + 注释 | A1 |
| `web/assets/js/pages/market.js` | `seal_window` 剥 `N_` 前缀的正则 | A2 |
| `web/assets/js/pages/kernel.js` | 数据血缘按名字排序（放弃按覆盖日） | A3 |
| `web/assets/js/pages/review.js` | `decayNote()` 的英文句正则翻译 | C3 |
| `web/assets/js/pages/review.js` | 「待修」裸数字直出（无法定位，只能等 B1） | B1 |
| `web/assets/js/zh.js` | DECISION 映射表追着数据加条目（含刚修正的拼写） | C1 |
| `web/assets/js/pages/kernel.js` | 决策管线读 dashboard 的人格字段绕行 /persona | D1 |

## 建议工单拆分（按收益/成本排序）

1. **A1 量纲统一**——纯序列化改动，防未来事故。
2. **C2 guard 文案**——改字符串即可（方案①），或结构化（方案②）。
3. **B1 unmatched 带上下文**——核对链路闭环的最后一步。
4. **C1 kind/actor 枚举**——半小时的活，杜绝映射失效。
5. **D1 /persona 缓存**——一行 lru_cache 级别的改动。
6. **A2/A3/C3/B3**——编码拆分与结构化，小改动。
7. **D2 profile 分段缓存 / D3 delist 语义**——有余力再做。

---

## 修复记录（2026-11-08，同一份清单逐项核实后）

### 核实结论

12 项里 **9 项完全属实**，3 项描述有偏差，均已在下面写明。取证方式：回到源码逐行
定位 + 带口令实测 `8811` 响应体；改动后用 `TestClient` 在**真实账本**上复测
（没有重启运行中的服务，所以 8811 仍是旧代码，重启后才生效）。

| 项 | 判定 | 偏差说明 |
|---|---|---|
| A1 A2 A3 | 属实 | A3 的 `cover_end=20271231` 是 manifest 真实覆盖；项目里**已有** `tags=("reference",)` / `on_demand` 与 `brief._coverage_boundary` 这套等价分类，只是没进 `/data/status` 响应、也没叫 `kind`。 |
| B1 B2 B3 | 属实 | B1 原文一直能经 `GET /journal?id=N&full=1` 取到，缺的是把 token 定位回句子的那一步。 |
| C1 C2 C3 | 属实 | C1 的 `propose_blocked` 拼写前端**已修正**为 `proposal_blocked`；后端仍无值域。实测账本里还有 `author_backfill` / `order_reconstructed` / `fill` 三个源码字面值之外的历史 kind。 |
| D1 | 属实（措辞需改） | 慢在 `scorecard → market_for_cases → 读二十年日线`，**不是**同步探行情报价；`regime.compute` 本来就有缓存。 |
| D2 | 属实 | 13 段各自重读；但 `basic / names / namechange / sw_table` 底层**都已**走 `market._cached`。 |
| D3 | 有偏差 | `profile.py` 早就有 `delist_date_note` 消歧；且 `sessions to delisting` 是可转债 `cb_basic` 的独立链路，**根本不消费** `identity.delist_date`。 |
| 正例 | 属实 | `/market/frame` 的 `no_frames` 三要素齐全，保持不动。 |

### 采用的修法（全部是**增量字段**，旧字段一个没改）

不直接把 `unmatched`、`not_scored` 改成对象数组，也不改 guard 的 `detail` 字符串：
前者会让现有断言与消费者整片失效，后者会动到写进 `cancel reason` / 票据 `reasons`
的结算文本。新增字段、保留旧字段，前端可以分批迁。

| 项 | 落点 | 新增 |
|---|---|---|
| A1 | `service/api/trading.py` + `service/vocabulary.py` | 顶层 `units` / `units_note`；每行 `seal_ratio_pct`（`_pct` 后缀，已 ×100）。精度改成与输出一致，保证 `seal_ratio_pct == seal_ratio × 100`。 |
| A2 | `service/trading/board.py` | `split_seal_window()`；每行 `seal_window_no` (int) / `seal_window_text`。 |
| A3 | `service/adapters/catalog.py` | `dataset_kind()` / `coverage_boundary()` 单点定义；`brief._coverage_boundary` 改为复用；`/data/status` 每条加 `kind` + `coverage_boundary`，顶层给 `dataset_kinds`。 |
| B1 B2 | `service/trading/journal_check.py` | `locate()` / `detail()`：写入时与读回时共用一条路，`unmatched_detail` / `sign_hints` 带 `value / excerpt / start / end / occurrences / field`。历史条目读时按原文重算，**不用回填数据库**。 |
| B3 | `service/trading/arena.py` | 每行补 `closing_ticket_id`；新增 `not_scored_detail`，三类告警都给 `case_ids`，`unknown_closing_ticket` 另给 `ticket_ids`。 |
| C1 | `service/vocabulary.py`（新）+ `state.py` | 27 个 kind / 5 个 actor / 27 个 guard 检查项 / decay reason / 单位约定集中定义；`_log_decision_conn` 软校验登记未声明值；新增 `GET /meta/vocabulary`。 |
| C2 | `service/trading/guard.py` | `Check` 加 `structured` / `detail_zh`；每个检查项都返回 `{metric, actual, cap, unit}`，数值项给精确读数，开关项 `unit=bool, basis=status_only`；三句点名文案附中文。旧 `detail` 保持英文原样。 |
| C3 | `service/trading/decay.py` | `note_meta: {reason, need, have}`；`no_baseline` 与正常态也有 `note_meta`。旧英文句子保留。 |
| D1 | `service/api/trading.py` | `scorecard` 结果按 `(交易日, 人格切换条数)` 放进现有 `market._cached`（TTL 就是 `cache.market_seconds=60`），人格/资格段仍每次现算。 |
| D2 | `service/trading/profile.py` + `service/trading/market.py` | 第一轮：`identity` 段按 `(代码, 交易日, sw_table 形状)` 缓存。第三轮：见下方「D2 收尾」——把重复读表收进共享缓存后热调用 890 → 339 ms。 |
| D3 | `service/trading/profile.py` | `delist_status: listed / delisted / scheduled / unknown` + 语义说明；`null` 不再独自承担语义。 |

> 第六轮补记：`vocabulary.py` 起初放在 `service/trading/` 下。`research/discover.py`
> 引用它之后，`tests/research/test_layering.py` 立刻红了——导入这个模块会先执行
> `service/trading/__init__.py`，把整层 trading 拖进 research。词表本身**零依赖**，
> 于是移到 `service/vocabulary.py`：research 与 trading 都向下引用它，分层方向不变。
> 这类越界只有动态导入测试能抓到，静态 AST 检查看不出"包 `__init__` 的副作用"。

### 实测（真实账本，`TestClient` 同代码；重启后 8811 线上复测一致）

```
/market/pools      seal_ratio=0.0511  seal_ratio_pct=5.11  pct_of_equity=10.0
                   seal_window="1_开盘09:30-10:00" -> no=1, text="开盘09:30-10:00"
/data/status       trade_cal/share_float -> kind=calendar, coverage_boundary=false
                   daily -> kind=data, coverage_boundary=true
/journal           unmatched=["5","84","4"] 保留；detail[0] 带 excerpt/start=89/end=90
/arena             not_scored={"unknown_closing_ticket":1} 保留；
                   detail 给出 case-td-20261028-b81655ca / td-20261028-b81655ca
/decay             core_etf: {reason: sample_below_min, need: 20, have: 5}
/meta/vocabulary   27 kinds / 5 actors / 27 guard checks，undeclared 均为空
/persona           1889 ms（冷） -> 26 ms（缓存命中）
/symbol/profile    3899 ms（冷） -> 890 ms（identity 命中）
```

服务重启后（2026-11-08 01:39，watchdog 3 秒拉起，指纹 `6c4a8528`），在 8811 上
逐端点复测：`/market/pools` 的 `seal_ratio_pct=5.11` / `seal_window_text="开盘09:30-10:00"`、
`/data/status` 的 `trade_cal → calendar,false`、`/journal` 的 `unmatched_detail[0].start=89`、
`/arena` 的 `td-20261028-b81655ca`、`/decay` 的 `note_meta`、`/meta/vocabulary` 的
`undeclared=[]`、`/persona` 第二次 27 ms、`profile.identity.delist_status=listed`——全部到位。

### D2 收尾（2026-11-08 02:04，服务再次重启后线上实测）

第一轮只缓存 `identity`，热调用仍有 890 ms。cProfile 把剩余时间摊开了：
`market.daily()` 一次 build 读三遍同一天横截面（169 ms，object dtype 字符串比较），
`market.panel()` 每次重建 60 日全市场面板（156 ms），`_sentiment` 把 20 个交易日的
龙虎榜分区逐个读、三个池子各读两遍（32 次 `read_partition`）。

采用检索到的 **request-scoped / load-once** 做法，但复用了项目已有的 `market._cached`
（60 s TTL + 日更 manifest 失效），**没有引入 cacheframe / dfcached 这类磁盘缓存库**：
同一份 parquet 不该在内存里再存一份序列化副本。

| 改动 | 效果 |
|---|---|
| `market.daily()` 按 `(日期, 列集合)` 缓存日切片 | 3 次读 → 1 次；列集合进 key 避免 6 列/9 列串味 |
| `market.panel()` 按 `(日期, 窗口)` 缓存带 `adj_close` 的面板 | 156 ms → 命中；所有调用方只读（已逐个核对 copy/pivot 用法） |
| `profile._partition()` 缓存分区读 | 32 次 `read_partition` → 1 次（`stk_mins`）；缺分区缓存为 `None`，不每天重开 20 次 |
| `profile._read_cached()` 支持 `start/end` 参数 | `news` 的年份范围进 key， January 不会拿到 June 的帧 |
| 两个 helper 的缓存键带**读取实现指纹** | monkeypatch `desk.data.read` 自动换键，不会拿上一帧骗过测试；fake desk 无 `_cached` 时直读 |

一次试过把 `ts_code` 转 `category` 消除 object 比较：只省 ~20 ms，却引入 pandas
`observed=False` 弃用警告与潜在语义变化，**已撤销**。

```
线上 8811（指纹 b361d079）：
/symbol/profile   冷 4344 ms -> 热 364 / 339 / 341 ms   （改前热 890 ms，降 62%）
/persona          冷 1681 ms -> 热 28 ms
cache             62 entries / 1.47 GB（上限 1.61 GB，evictions=0）
```

**仍未做到百毫秒级**，且**冷调用无法消除**（进程起来后第一次必须读年分区）。
剩余 ~340 ms 是 `snapshot` 52 ms、`margin_row` 63 ms、各段 pandas 计算与
object dtype 比较；`snapshot` 没缓存是因为它可能在盘中读实时报价，冻 60 s
会让定价停在旧价——这个风险不值得为 52 ms 冒。
另外发现 `peers` / `sentiment` 两段**算完就被 `_fit_blocks` 因 16 KB 响应预算丢掉**，
约 270 ms 白付；要省下它得改分段请求策略，属独立工单。
D1 同理：缓存消除的是**重复**调用，不是首次冷调用。

### 前端补丁：8 处全部已撤（D1 于第四轮撤除）

服务重启后（watchdog 3 秒内拉起，`code_fingerprint` `0c1f5142` → `6c4a8528`），
先在 8811 上逐端点确认新字段全部到位，再做**一次性切换**：直接改读新字段，
不写 `newField ?? oldField` 的静默双路径——双路径会让"哪一端还没迁移"一直不可见。

| 原补丁 | 撤法 | 状态 |
|---|---|---|
| `market.js` 字段级 ×100 换算 + 注释 | `fmt.js` 新增 `pctPlain()`（`*_pct` 字段专用、不换算），读 `seal_ratio_pct`；`pct_of_equity` 仍用 `pctPts`，注释改成指向顶层 `units` | 已撤 |
| `market.js` `seal_window` 剥前缀正则 | 读 `seal_window_text` | 已撤 |
| `kernel.js` 放弃按覆盖日排序 | 按 `coverage_boundary` 分组：逐日面板按 `cover_end` 倒序，其余按名字排并置后；表格加「类型」列 | 已撤 |
| `review.js` `decayNote()` 英文句正则 | 读 `note_meta.reason / need / have`；`zh.js` 的 `DECAY_NOTE` 表与 `decayNote` 出口删除，换成按 reason 键的 `decayReason` | 已撤 |
| `review.js` 「待修」裸数字（两处） | `unmatchedList()` 渲染 `value / excerpt / start-end`，鼠标悬停给字符偏移与数字角色；**缺 detail 时显式报"后端未给坐标"，不静默退回裸数字** | 已撤 |
| `zh.js` DECISION 映射表追着数据加 | 补齐到 27 项、与 `/meta/vocabulary` 逐项对齐；`CHECK` 27 项、`NOT_SCORED` 5 项一并补齐 | 已撤 |
| `zh.js` / `ui.js` / `decide.js` 检查表只翻前缀 | 检查表优先显示 `detail_zh`，英文原句留在 `title`；`zh.reason()` 仍只负责 `failures` 字符串 | 已撤 |
| `kernel.js` 绕行 `/persona` | 第四轮撤除：服务启动时后台预热了 scorecard 与共享帧，`/persona` 不再有冷调用，改为直连读完整的 `current/today`。不写 `p \|\| d.persona` 静默双路径 | 已撤（第四轮） |

顺带修了原清单**没列到**的两处 A3 受害者：`app.js:datasetFreshness()` 与
`kernel.js:freshnessBar()` 都曾拿 `cover_end` 直接算滞后天数，日历类的 2027-12-31
会被画成"极新"。现在非 `coverage_boundary` 的段不着色，悬停写明"覆盖日不作新鲜度判据"。

### 回归

- 新增 `tests/trading/test_backend_debt_contracts.py`：17 条契约测试，含**源码字面值扫描**
  （新增 kind / guard 检查项却忘了登记词表，测试会先红）。
- 更新 `tests/data/test_data_api.py` 的整字典断言（`/data/status` 多了两个顶层键）。
- `docs/API_CONTRACT.md` 用 `scripts/api_contract.py --write` 重生成：93 → 94 条路由。
- 全量 `pytest tests -o addopts="" -q`：**2825 passed, 3 skipped**（首次跑出 3 个
  一致性失败，已逐一收口后复测全绿）。第二轮只改前端 JS 与文档。
- 第三轮 D2 缓存改完 `service/trading/market.py` / `profile.py` 后**再跑一次全量**：
  仍 **2825 passed, 3 skipped**（399 s）。中途 4 个 fake-desk / monkeypatch 用例
  因缓存拿旧帧失败，已用「读取实现指纹进 key + 无缓存直读」收口。
- 前端：`node --input-type=module --check` 扫 17 个 JS 文件，**0 语法失败**；
  `node --test tests/panel_app_safety.test.mjs` **2/2 通过**。
- 前端词表巡检（可复用）：拉 `/meta/vocabulary` 后逐项比对 `zh.js` 的
  `DECISION` / `CHECK` / `DECAY_REASON` —— 27 / 27 / 3 项**全部有中文映射，0 遗漏**；
  `pctPlain(5.11, 3)` 与旧 `ratio(0.0511, 3)` 同为 `5.110%`，撤补丁后显示不变。
- 第四轮（预热 / 计划缓存 / 撤 D1 绕行）后再跑全量：仍 **2825 passed, 3 skipped**（361 s）。

---

## 第四轮：把前面自己列的所有未闭环项一次做完（02:25 起）

范围由执行方界定为前三轮明确写下的待办：**首次冷调用、被丢块的白算、
D1 绕行、浏览器 DOM 实测、缓存内存、全量回归**。以下逐项完成；当时没验到的面
在第五、六轮补上。

### 检索选型

FastAPI 官方 *Lifespan Events* 文档说明启动逻辑应放在 lifespan，并明确警告
"不要放在模块顶层，否则 import 这个 app 的自动化测试也要付一遍加载成本"。
据此：`lifespan` 里起 **daemon 线程**，不阻塞 startup；`sys.modules` 含 `pytest`
时直接跳过，`QUANT_DESK_WARMUP=0` 可关。**不用**同步 lifespan 预热（会拖慢启动），
也**不用**模块顶层加载。

### 1. 消除首次冷调用

`service/api/app.py` 新增 `_warmup()` / `_keep_warm()`，`persona.py` 抽出
`cached_scorecard()` 让 `/persona` 与预热共用同一个缓存键（避免两处字符串字面量
分叉）。预热在进程内跑一遍 `trading_api.persona()` 与 `profile.build(sample, day)`，
把 `regime` 的 300 日面板、`fina_indicator` / `news` / `margin` / 5 分钟线等
十来张表一次填满——比维护一张会漂移的"大表清单"稳。

第一次只预热已知大表时，首请求仍要 1.8 s / 3.2 s；改成跑完整读取路径后：

```
预热（后台）                  5.7 s，不阻塞启动
/persona 首请求     1771 ms -> 95 ms
/symbol/profile 首请求 3243 ms -> 269 ms（预热到的 symbol）
```

**关键修正**：`market._cached` 的 TTL 是 60 s，实测第 109 秒 profile 又回到
3819 ms——预热只管一分钟。命中时 `_cached` 会刷新 `entry.stamp`，所以加了
`_keep_warm()` 每 **40 s** 轻触一次（全部命中，约 0.3 s / 次），并让 lifespan
退出时 `Event.set()` 停线程。日更改 manifest 仍会 invalidate，不会把昨天的帧
保活到今天。

线上复测（指纹 `45178d36`，**uptime 102 s**，已跨过 TTL）：

```
/persona                 29 ms
/symbol/000001.SZ/profile 282 / 293 ms
/market/pools            152 ms
cache 63 entries / 1.47 GB / evictions=0
```

### 2. 不再为注定被丢的块付计算成本

`profile.build()` 此前算完 13 块才由 `_fit_blocks()` 按 16 KB 预算丢掉
`peers` / `sentiment`。改成**计划缓存**：首次照旧全算保证判定准确，把
`(kept, dropped)` 按 `(desk identity, 代码, 交易日, 请求集合, 窗口)` 记 60 s；
后续同键请求只算 `kept`，输出（含 `omitted_sections` / `size_note`）**逐字一致**。
显式 `sections=` 请求不走计划（调用方点名要的绝不能跳过）。key 带 desk identity
是为了测试里换 fake desk 注入新数据不被上一个用例钉住。

验证：`identical payload: True`；显式 `sections=['peers','sentiment']` 仍返回两块；
热调用 339 → 303 ms（白算本身已被第三轮的分区缓存压低，所以这里只省 36 ms，
但消除了无谓计算这一结构性问题）。

### 3. 撤掉最后一处前端补丁（D1）

`kernel.js` 决策管线改回直连 `GET /persona`，读完整 `current/today`；不写
`p || d.persona` 的静默双路径。至此**前端 8 处补丁全部撤完**。

### 4. 浏览器 DOM 实测（此前唯一没做过的验证）

用内置浏览器打开 `/desk`，四个页面逐一截图核对，全部正常渲染、无空白块：

| 页面 | 实测到的改动 |
|---|---|
| 内核 | 决策管线「人格」行显示 `trader · 可下单`（D1 直连成功）；数据血缘「类型」列显示 `逐日面板 / 参考表 / 按需 / 快照 / 日历/日程`，`share_float` 的 2027-12 不再顶到第一（A3） |
| 复盘 | 「待修 8 · 显示前 3 个」+ 每条 `5 → …判不买（15 只 x≤−2%…）` 的原句片段（B1）；衰减表说明列 `至少要 20 笔已平仓，现在 5`（C3）；未计分 KPI 带 `td-20261028-b81655ca`（B3） |
| 盘面 | 「封单/流通」`5.110%`、「首封」`开盘09:30-10:00`（A1 / A2） |

### 5. 缓存内存：实测与判断（**不治理**，附理由）

`health.cache` 报 1.47 GB / 上限 1.61 GB、`evictions=0`；进程实测
**WorkingSet 2.2 GB / Private 3.3 GB**。这是"用户访问过几页之后"的稳态：
年分区帧（`daily` / `daily_basic` / `margin_detail` / `stk_limit`）与 60 日面板
本来就会被缓存，预热只是提前触发。降 `max_bytes` 会淘汰正在服务的帧、
禁用缓存会退回秒级响应，两者都是用正确性换一个数字，因此**不改配置**，
改为记录实测值并建议监控 `evictions` 与进程 RSS（一旦 `evictions > 0`，
说明缓存开始抖动，冷读会重现）。

### 仍存在的边界（诚实列出，不再挂作待办）

1. **百毫秒级没达到**：热调用 ~280 ms，剩余是 `snapshot` 52 ms、`margin_row` 63 ms
   与各段 pandas 计算。`snapshot` 可能读盘中实时报价，冻 60 s 会让定价停旧价，
   不为 52 ms 冒这个险。
2. **任意新 symbol 首次约 1.27 s**：`_valuation` 的 750 日估值序列与 `_technicals`
   的 260 日日线按 symbol 存储，无法跨 symbol 共享；重复访问已有
   `hist:` / `basichist:` 缓存命中。这是"第一次点开某只票"的固有成本，
   比改前的 4.3 s 低 70%。
3. `object dtype` 字符串比较仍是 cProfile 里的最大单项；试过转 `category`，
   只省 ~20 ms 却引入 pandas 弃用警告，已撤销。

### 回归

- 全量 `pytest tests -o addopts="" -q`：**2825 passed, 3 skipped**（361 s）。
- `node --input-type=module --check`：17 个 JS 文件 **0 失败**；
  `node --test tests/panel_app_safety.test.mjs` **2/2**。
- 服务重启后 102 秒复测（见上），全端点 200。

**清单内 12 项后端债务与 8 处前端补丁在第四轮全部处理完**；当时记录的三条是
技术边界。但"全部结清"这个说法随后被推翻两次：第五轮独立复查发现 A1 的
`units` 根本没推广，第六轮复查又发现那份"权威声明"没有任何消费方在读。
**准确读法应是「清单内项已完成 + 本轮验证覆盖到哪」，不是「系统不会再有问题」**；
两轮新发现与修法见第五、六节。

---

## 第五轮：独立复查（05:06，后端自查 + 前端反推后端）

### 方法（检索结论）

采用 **consumer-driven contract** 视角：破坏性变更由消费者定义、逐个响应验证
而非抽样、特别覆盖空集合与可选字段缺失。**不引入** Pact / apiveritas /
schemathesis——它们需要 broker、mock server 与生成式测试栈，对"本地 FastAPI +
无构建步骤的原生 JS 前端"成本远大于收益；本项目已有
`tests/test_api_contract_doc.py` 做路由级一致性。复查用两支本地脚本代替：
全量 GET 路由取样 + 前端字段消费与响应键集合的静态比对。

### 后端自查

| 检查 | 结果 |
|---|---|
| `app.openapi()` 的 70 条 GET 路由逐个实请求 | **63 × 200、0 × 5xx**；6 个 422 全是缺必填 query（`symbols` / `symbol` / `path`），1 个 404 是示例 ticket_id 不存在——都是预期 |
| `scripts/api_contract.py --check` | 94 路由，文档与实现同步 |
| 服务日志（当前进程 02:43 起、跑了 2.4 h、保活 200+ 次） | `warmup refresh failed` **0 次**，当前进程之后 **0 个 ERROR / Traceback / unhandled**；此前的 Traceback 均为历史，含一次客户端断连 `ConnectionResetError` |
| 缓存 | 73 entries / 1.47 GB / **`evictions=0`**，保活有效 |

新观察到三个**首次冷计算**慢端点：`/discover` 17.99 s、`/data/information`
15.06 s、`/watch` 3.35 s；第二次分别 12 / 43 / 92 ms。两者已有单飞缓存
（`/discover` 用 `market.cached_answer`，`/data/information` 用
`information.cached_scorecard`），`plugin/lib/index.js` 也早已把超时提到 60 s
并写明"17.6 s 实测"。属已知设计、不是回归；前端只消费 `/watch`（92 ms），
不在这两个 15 s 端点的路径上。**当时判断不改动** —— 这个理由第六轮被纠正：
`plugin/lib/index.js` 明确有 `quant_discover` 与 `quant_information` 两个工具，
它们是真实消费者，60 s 超时只是**容忍**不是解决。两个端点现已纳入启动预热。

### 前端反推后端

- 扫描 `web/assets/js` 得到 **46 个消费端点**，逐个实请求全部 200
  （`/decision/propose` 是 POST-only，GET 405 属预期）。
- 收集全部响应的键（深度 5，共 **1587 个键**），与每个页面实际读取的属性比对：
  **0 个真实缺失字段**。两个"未命中"已查清——`alternative` 是 `/market/pools`
  无数据日的空态字段（本清单赞誉的正例，实请求
  `?date=20200101` 能取到），`first` 是 `ui.js` 的局部变量。
- 浏览器补看此前没验过的四页：今日、持仓与风险、决策、个股。读数全部正常
  （`炸板率 30.4%`、一手占净值 24%、涨红跌绿、空仓空态），
  无 1000% / NaN / undefined。

### 本轮发现并修复：A1 的 `units` 没有推广到其它端点

`plugin/lib/index.js:389` 用一段中文提示词告诉模型"facts / discover /
industries 是小数，profile 的 `_pct` 是百分数"——**量纲仍靠消费者记忆**，
正是 A1 的原始病因换了个端点。实测确认：

| 端点 | 字段 | 值 | 量纲 |
|---|---|---|---|
| `/facts` | `breadth.equal_weight_return` | -0.018629 | 小数 |
| `/regime` | `breadth_above_ma20` / `vol_percentile_1y` | 0.331 / 0.92 | 小数 |
| `/market/sector` | `pct_chg` / `pct_chg_eq` | 1.4 / 0.58 | **百分数** |
| `/risk` | `daily_pnl_pct` | 0.0（内核是 `(equity-day_start)/day_start`） | 小数，**名字却带 `_pct`** |
| `/market/pools.sentiment` | `break_rate` | 0.304 | 小数 |

`daily_pnl_pct` / `day_pnl_pct` 与本项目"百分数一律 `_pct` 后缀"的约定**直接矛盾**；
改名会动到内核、账本与既有消费者，故登记为**命名例外**而不是重命名。

修法（增量、不改任何既有字段）：

1. `service/vocabulary.py` 新增 `UNIT_CONVENTIONS`：
   `rule` + `by_endpoint`（9 个端点的字段量纲）+ `naming_exceptions`（2 条），
   由 `GET /meta/vocabulary` 的 `unit_conventions` 一次给出，作为权威来源。
2. `service/api/trading.py` 的 `/market/sector` 参照 `/market/pools` 加顶层
   `units`，把最容易混的表格端点口径写进响应本身。
3. `tests/trading/test_backend_debt_contracts.py` 加 3 条测试，其中一条把
   **前端格式化函数**钉在约定上：sector 的 `pct_chg` 必须用 `pctPts()`（不乘），
   `/facts` 的 `equal_weight_return` 与 `/regime` 的 `vol_percentile_1y` 必须用
   `pct()` / `ratio()`（乘 100）。只验后端字段不够——前端换错一个函数就会
   显示 100 倍的数，而后端断言仍然全绿，那正是最初 1000% 的成因。

   > 第六轮核查注：这三条只把口径放到了后端，web 与 plugin 一个都没读；
   > 第 3 条是 6 行源码子串包含断言，而且漏了最危险的 `daily_pnl_pct`。
   > 因此本节的"修法"只完成了一半，完整的消费方接入见第六节。

线上复测（指纹 `95fa36c5`）：`/market/sector.units` 6 项、
`/meta/vocabulary.unit_conventions` 覆盖 9 个端点、2 条命名例外；
首请求 `/persona` 29 ms、`/symbol/profile` 288 ms，预热保活未受影响。

### 当时的观察项（第六轮已处理，保留原判断作为审计记录）

- 494 px 窄视口下底部状态条文字重叠，原判断"桌面窗口远大于此、不修"。
  第六轮改为横向可滚动 + 子项不收缩：信息一项不丢，也不靠藏掉几项假装没事。
- `/discover` / `/data/information` 首次 15–18 s，原判断"塞进启动预热会把预热
  从 5.7 s 推到 20 s 以上、不修"。预热本就是 daemon 线程、不阻塞启动；
  第六轮已纳入，并单独兜住异常，不让慢端点失败连累主预热与 40 s 保活。

### 回归

- 修改前基线全量 **2825 passed / 3 skipped**；相关子集 100 passed。
- 修改后新增 3 条契约测试，最终全量见交付说明。
- `docs/API_CONTRACT.md` 仍为 94 路由（`unit_conventions` 是既有端点的响应字段，
  不新增路由）。

---

## 第六轮：把「话里的问题」修到明面上没有（12:05 起）

用户追问"话有什么问题吗"，按 consumer-driven 的判据把第五轮结论逐条对回代码，
确认有 6 类问题。这一轮不是补文档，是让**消费方真的按契约走**。

### 核查确认的问题

| # | 问题 | 证据 |
|---|---|---|
| 1 | 说"修掉了债务"，其实只搬了信息 | `plugin/lib/index.js:389` 手写规则一个字没改；`web/` 里 `/meta/vocabulary` 只出现在 `zh.js` 两处**注释**，没有任何代码请求。病因（靠消费者记忆）没消除 |
| 2 | "权威来源"不止一处且互相冲突 | `facts.py` / `frame.py:225,385` / `intraday_buckets.py` / `trading.py:1382` / `discover.py:409` 各自手写中文；`discover.py` 写着"profile 的 *_pct 是百分数"，与登记的 `daily_pnl_pct` 小数例外直接矛盾 |
| 3 | "把前端格式化钉在约定上"说满了 | 实为 6 行源码子串包含断言，且最危险的两个命名例外 `daily_pnl_pct` / `day_pnl_pct` 在该测试函数里出现 0 次 |
| 4 | "0 个真实缺失字段"结论过强 | 把全部端点并成 1587 个键的"宇宙"再比，会掩盖跨端点漂移；盲区实测：解构 10 处、深层可选链 20 处、`.map` 回调 55 处、POST 6 个端点 |
| 5 | "不在 15 s 端点路径上"避重就轻 | 插件有 `quant_discover` / `quant_information` 两个真实消费者，60 s 超时是**容忍**不是解决 |
| 6 | 反复"全部结清"又反复冒出新项 | 第三、四轮各宣称一次，第五、六轮各发现新问题 |

### 检索定案

跨 Python/Node 的可靠做法**不是运行时 fetch**，而是把契约导出成**确定性生成物 + `--check`**
（`export_openapi.py` / `api:check` 模式）：离线可用、测试可验、不需要 broker 或构建工具。
不引入 Pact / Zod / openapi-typescript —— 本地原生 JS 无构建链，成本高于收益。

### 修法

1. **单点**：`vocabulary.py` 成为唯一手写源。键本身就是**从响应根出发的完整路径**
   （`industries[].pct_chg`、`account.day_pnl_pct`），删掉第二张 `row_paths` 容器表；
   `units_for()` / `units_text()` / `note()` 都由这张表生成。
2. **生成物**：新增 `scripts/export_units.py`，确定性导出
   `web/assets/js/contract.units.js` 与 `plugin/lib/contract.units.js`；
   `--check` 挂进 pytest，两份字节必须相同且必须与 Python 同源，改词表忘了重跑就红。
3. **消费方接入**：插件 `PROMPT` 改用 `${UNITS_TEXT}` 插值，手写规则删除；
   `fmt.js` 新增 `fmtByUnits(units, field, value)`，按声明选 `pct()` / `pctPts()` /
   `pctPlain()` / `cny()`，未声明字段返回破折号且只警告一次（**不猜换算**）；
   `market.js` / `today.js` / `risk.js` 全部歧义字段改走这个入口，含 `daily_pnl_pct`。
4. **后端**：`_with_units()` 给 10 个数值端点统一注入顶层 `units`；
   `facts` / `frame` / `intraday_buckets` / `discover` 的手写文案全改 `vocab.note()`。
5. **测试**：新增 `tests/trading/test_units_contract.py`（生成物新鲜度、逐端点按完整路径
   精确定位、插件文本同步、前端调用字段 ⊆ 词表、Node 纯函数逐字比对）；
   重写第五轮 3 条脆弱断言，覆盖命名例外。

### 精确定位抓出的两个真问题

- **`float_mv` 是幽灵字段**：它根本不在 `/market/pools` 响应里，只出现在早先手写的
  `units` 中。递归比对会放过它，改成按路径定位立刻暴露，已从词表删除。
- **预热键与请求键不一致**：手算 `f"discover:{latest_bar_date()}"` 与 handler 的
  `_day(None)` 可能差一个日期，预热白做（实测首请求仍 2011 ms、第二次才 9 ms）。
  改为直接调 `trading_api.discover()`，键逐字相同。

### 一次失败尝试（留痕）

第一版把慢端点预热排在 40 s 保活**之前**：`information` 因源码指纹变化重算磁盘缓存，
首屏 `/watch` 撞上它等了 **32 s**。改成保活与慢端点各起一个 daemon 线程、
`watch` 放在慢端点第一位、慢端点失败单独兜住，复测 `/watch` **133 ms**。

### 线上实测（指纹 `910eaaf4`，uptime 22 s，重启后预热完成）

```
/discover           17.99 s ->     9 ms      units=7
/data/information   15.06 s ->   263 ms
/watch               3.35 s ->   133 ms
/persona                          33 ms
/symbol/profile                  335 ms
```

15 个端点全部 200，词表覆盖 11 个端点、2 条命名例外，各响应顶层 `units` 与词表同源；
`cache` 73 entries / 1.49 GB / **evictions=0**。窄视口状态条改为横向可滚动 +
子项不收缩，8 项信息一项不丢，不再叠字。

### 验证

- `scripts/export_units.py --check`、`scripts/api_contract.py --check`（94 路由）均通过。
- 18 个 JS 文件语法 0 失败；`node --test` 2/2；插件 `index.js` 语法通过。
- Node 纯函数逐字比对：`5.110%` / `+10.0%` / `30.40%` / `+1.40%` / `−1.86%` /
  `92%` / `0.00%` 与改前完全一致；未声明字段返回 `—`。
- 插件 PROMPT 含生成的 `unit_conventions` 与 `daily_pnl_pct` 例外，旧手写句已消失。
- 全量 pytest 见交付说明。

### 边界（明确写出，不再说"全部结清"）

1. `information` 的磁盘缓存按源码指纹失效：**改 Python 后第一次重启**，后台仍会重算
   十几秒；首屏不受影响（watch 先热、保活并行），但插件若在启动瞬间调它可能撞上。
2. `cache` 1.49 GB / 1.61 GB 上限，仍需盯 `evictions`。
3. `fmtByUnits` 覆盖的是词表登记的歧义字段；其它数值字段仍由各自格式化函数渲染，
   新增字段必须先登记才会进入本套检查。

---

## 七、第六轮收尾后的三处补修（2026-11-08 12:40–13:00）

第六轮全量回归跑出 **2 个红**，都是 `tests/research/test_layering.py`。

### 7.1 词表住错了包（分层回归）

`service/research/discover.py` 引用 `vocabulary` 后，动态导入测试立刻红：
`import service.trading.vocabulary` 会先执行 `service/trading/__init__.py`，
把 `book / decisions / exposure / guard / impact / market / state / strategy`
整层拖进 research。静态 AST 检查看不出这一点——它只看 import 语句，不看包
`__init__` 的副作用。词表本身零依赖，移到 `service/vocabulary.py`：research 与
trading 都向下引用它，分层方向不变。生成物横幅与 `--check` 一并更新。

### 7.2 静态前端没有缓存策略

`/desk` 的响应只有 `ETag` / `Last-Modified`，没有 `Cache-Control`，浏览器按
启发式缓存（约 `(now - Last-Modified) × 10%`）留副本。改了 JS 后普通刷新仍跑
旧代码，实测撞到 `zh.basis is not a function`——不是代码错，是浏览器拿旧模块
配新页面。给 `/desk/*` 加 `Cache-Control: no-cache`：每次带 ETag 回来校验，
没变 304、变了立刻生效。

### 7.3 同一天两个涨停数，都没有名字（界面实测）

| 页面 | 数字 | 实际来源 | 原来写的标签 |
|---|---|---|---|
| 总览 · 情绪三柱 | 32 / 14 / 15，炸板率 30.4% | 东财收盘数（`break_rate_basis: vendor`） | 脚注裸写英文 `vendor` |
| 盘面 · 三池与闸门 | 33 / 17 / 15，炸板率 34.00% | 派生池（`counts.source: derived:limit_pool_t0`） | 「东方财富」——那是三池明细的来源，不是这三个数的来源 |

两个数都对，但读者没有线索。修法是不动数字、只补名字：`zh.basis()` 把
`break_rate_basis` 译成中文；总览芯片显示「东财收盘口径」；盘面芯片改成
「口径 派生池（收盘）」，并把东财对照数 `东财 32 / 14 / 15` 摆在旁边。
`tests/test_web_sentiment_basis.py` 在 node 里直接调用翻译函数断言不原样透传，
另加一条源码绊线。

### 复测

- 全量 pytest：**2837 passed / 3 skipped**（新增 2 条用例，exit 0）。
- 冷缓存浏览器逐页：今日 / 盘面 / 复盘 / 内核 数字齐全，无「—」满屏；盘面
  「占比」列全值 `+10.0% / +41.5% / +126.2%`，此前看到的 `+10.` 是窄视口截断，
  不是格式化缺位。
- `/desk/assets/js/zh.js` 响应头：`etag` + `cache-control: no-cache`。
