# quant-desk runbook

## 0. Environment

- Python 3.12 venv at `D:/quant-desk/.venv`
- System Python is 3.13; do not install project deps into it.
- Tushare token: put `TUSHARE_TOKEN=...` in `D:/quant-desk/.env` (never commit).
- Local API token: `QUANT_TOKEN=...` in `.env`; default `change-me-local-only` is for local dev only.
- Data lives in `data/`:
  - `data/parquet/<dataset>/<partition>.parquet` — the data itself
  - `data/registry.sqlite` — manifest, ingest ledger, quality verdicts
  - `data/rejects/` — rows dropped by the repair step
  - `data/rt_snapshots/<yyyymmdd>/` — intraday quote archives
  - `data/sentiment/<yyyymmdd>.json` — ZT/ZB/DT panel

## 1. Tests

```bash
cd D:/quant-desk
.venv/Scripts/python.exe -m pytest tests -q
```

Expected: P0 golden 12 + P1 data tests (store, ledger, quality, PIT, backfill, adapters).
All data tests are offline; no Tushare/EastMoney calls.

### 1.0 生成的文档要跟着代码走

`docs/API_CONTRACT.md` 是**生成物**（`app.openapi()` 的表格），它存在的全部意义是
"模型不许凭记忆调接口"。2026-11-02 的审查发现它写着 69 条路由、实际服务 85 条，
缺了 13 条——包括模型用得最多的 `/symbol/{symbol}/profile` 和整个样本账本。
原因很朴素：改了代码没重跑生成器。

所以它有一道闸：

```bash
.venv/Scripts/python.exe scripts/api_contract.py --check    # 过期就退出码 1
```

CI 里应当这样用（先生成，再看有没有差异）：

```bash
.venv/Scripts/python.exe scripts/api_contract.py --write
git diff --exit-code docs/API_CONTRACT.md   # 有差异 = 有人忘了重新生成
```

改动了任何路由（加、改、删）之后都要跑一次 `--write` 并把结果一起提交。
`tests/test_api_contract_doc.py` 做的是同一件事的另一种形式：它比对文档与
`app.openapi()`，并检查插件调用的每条路径都真实存在——路由改名而插件没改，
工具就会指向一个不存在的路径。

### 1.1 四个审计：问的是不同的问题

服务起着的时候，`plugin/` 下有四个独立脚本，走**真实调用链路**
（`buildTools` → `execute` → `output.render`），看到的正是模型看到的东西。
它们不是 pytest 的一部分：pytest 验代码，这四个验"模型拿到手的是什么"。

```bash
cd D:/quant-desk/plugin
node audit_all.mjs           # 四个一起跑（并行约 1 分钟），并和上一次比
```

`audit_all.mjs` 就是下面四条的一次跑完。它存在的理由不是省事：这四条只有**真跑**才算数，
而人会忘。它把结果写进 `reports/audits/<时间>.json`（本机状态，不进仓库），下次和上一次比，
**只报回归**——某个响应涨过 5%、某个工具从干净变脏、某一项检查变分歧、某个工具不再自述年龄。

退出码 1 有三种来源：**某条审计没跑成**、**比上次变坏**、**第一次跑就有绝对值问题**。
第三种是 2026-11-04 补的：只看"和上次比"的话第一次永远绿，而一个从没修好的问题两次
一模一样、回归也看不见它。`degraded`（16 KB 上限下砍行）不算失败——那是设计内的取舍，
`quant_pools(limit=20)` 与 `quant_journal_search(limit=20)` 每天都在，算进去就是那个
"永远在响的告警"；它变没变大由回归差分盯。

单跑某一条：

```bash
node delivery_audit.mjs      # 送达：字节够不够，会不会被宿主整包丢掉
node quality_audit.mjs       # 表述：时间锚、占位符、精度、失败信封
node freshness_audit.mjs     # 年龄：这份数据有多旧，它自己说不说
node consistency_audit.mjs   # 一致：同一个事实的两个出口是不是同一个数
```

| 脚本 | 问的问题 | 失败意味着 |
|---|---|---|
| `delivery` | 响应会不会超过宿主的 16 KB 被丢掉 | 模型拿到空/error，而不是数据 |
| `quality` | 每个数字说不说得清（锚点、单位、空≠0） | 模型会读错，但看不出错 |
| `freshness` | 读者知不知道这份东西多旧 | 老数据被当成今天 |
| `consistency` | 两个工具说同一件事时，数一样吗 | 读者只能二选一，且没有依据 |

`consistency_audit.mjs` 单独带自检：

```bash
node consistency_audit.mjs --selftest   # 注入计数漂移 + 100 倍量纲错位 + 双出口分歧，必须都被抓到
```

一个从没报过警的审计不算证据。**改动任何出口之后跑这四个**；日更之后至少跑
`delivery` 与 `consistency`——前者抓"加字段把别的东西挤掉"，后者抓"改一处忘另一处"。

为什么不放进 daily_job：它们要求服务正在运行，而 17:30 的盘后任务可能跑在服务启动之前。
放到"服务起来之后"那一步跑，比塞进定时任务里更可靠。

### 1.2 工具层的覆盖率闸

`scripts/coverage_gate.py` 只看 Python。73 个 `quant_*` 工具住在 `plugin/lib/index.js`，
既不在 `service/` 下，也不在 pytest 的统计范围里——工具层从来没进过闸。

```bash
cd D:/quant-desk/plugin
node coverage_gate.mjs        # 跑 plugin 与 mcp 两套测试 + 判阈值
```

门槛按行 / 分支 / 函数三条给（95 / 80 / 72），因为它们的失败方式不同：改描述会先掉
branch，漏测一个新分支会先掉 line，抽函数不测会先掉 functions。

覆盖率答不了"每个工具都测过吗"——73 个工具的定义（描述、参数、输出 schema）都是"行"，
而 `run` 只有一两行，91% 的行覆盖可以和"30 个 `run` 一次都没执行过"同时成立（事实上有过）。
所以这条由 `test/tools.test.mjs` 里的结构用例保证：按 schema 的必填项自动填参数，要求
每条工具**真的发出一次请求**。加工具时它先红，覆盖率闸管的是另一半——工具内部的分支。

`mcp/server.mjs` 的 `main()` 用 `/* node:coverage disable */` 显式豁免：它由
`mcp/test` 的 e2e 子进程覆盖，v8 的覆盖率不跨进程收集。写成"未覆盖"比假装覆盖过诚实，
前提是那段别长出判断来。

## 2. Data pipeline

### 2.1 Permission probe

```bash
.venv/Scripts/python.exe scripts/probe_permissions.py
```

At 2000 Tushare points `limit_list_d` and `anns_d` are **not** available; the
pipeline uses EastMoney pools + akshare instead and marks those datasets as
external. Re-run this after changing the token.

### 2.2 Backfill (resumable)

```bash
# default P1 set, from 2023 to today
.venv/Scripts/python.exe scripts/backfill.py --datasets p1 --start 20230101 --end 20261024

# one dataset, refetch everything
.venv/Scripts/python.exe scripts/backfill.py --datasets daily --start 20240101 --force

# pools only (EastMoney; ~2020 onwards, some early days incomplete)
.venv/Scripts/python.exe scripts/backfill.py --datasets pools --start 20240101
```

How resume works:

- Work is split into `(dataset, partition)` units; progress is written to
  `ingest_ledger` every `--flush-every` keys (default 20).
- If the process crashes, the unit is `running` with a `cursor`; the next run
  continues after that cursor.
- If a finished unit is asked for a **wider** range (including backwards), it is
  refetched and merged (dedupe on the natural key), because the cursor alone
  cannot prove coverage of a different range.
- Identical content produces the same `snapshot_id`; re-ingestion is a no-op in
  terms of lineage.

Expected cost at 200 calls/min: 4 daily panels × ~900 trading days ≈ 3,600 calls
≈ 20 minutes; storage ≈ 20M rows.

### 2.3 Derived T0 pools (required for the aggressive sleeve)
```bash
.venv/Scripts/python.exe scripts/derive_pools.py --start 20230101 --stats
```

EastMoney's ZT/ZB/DT endpoints only serve about the **last 10–15 trading days**;
asking for 2024 returns `data: null` (verified: 2,034 requests produced only 14
days). So the historical backbone is derived from T0 bars:

- `limit_up` = close at `up_limit`
- `broken_board` = high touched `up_limit`, close did not
- `limit_down` = close at `down_limit`
- `boards` = consecutive limit-up streak

Writes `limit_pool_t0` (partition = year). EastMoney stays the **live**
enrichment source for seal amount / first-seal time, not the history.

### 2.4 Quality verification

```bash
.venv/Scripts/python.exe scripts/verify_data.py --start 20230101 --reconcile
```

- Writes `reports/data_quality.json` and `reports/P1_data_quality.md` **only for a
  manual full run**（不带 `--out`）。日更链不写这两个路径：它带
  `--out reports/daily/<日期>_quality.json`，按天留档（见 `reports/README.md`）。
  所以 `reports/data_quality.json` 上的日期是"最后一次手动全量"的日期，
  不是"昨天"——2026-11-02 之后它一直没动，而每个交易日都在写 `daily/`。
  要知道**现在**的状态，看 `GET /data/quality`（读 registry）或内核页，不读文件。
- Updates `quality_report` and flips manifest status to `quarantine` when a
  partition is structurally broken (>2% bad rows, schema mismatch, missing file).
- `--reconcile` samples Tushare closes against baostock (network, optional).

Quarantined partitions are invisible to `DatasetStore.read()` and therefore to
the agent. Repair, not quarantine, is the default for isolated bad rows.

### 2.4b 清单指针指向的文件不见了（2026-11-02 的一次真故障）

症状：读某个分区抛 `current manifest has no readable parquet/metadata`，
端点回 500。检查清单就会看到它指着一个磁盘上已经不在的版本：

```bash
.venv/Scripts/python.exe scripts/repair_manifest.py            # 只报告
.venv/Scripts/python.exe scripts/repair_manifest.py --apply    # 把指针接回去
```

**为什么会发生**：同一个分区有多个写入者——10:30 盘中快照、15:20 收盘任务、
17:30 日更都写 `limit_up_pool`。每个写入者都提交自己的清单，而**最后提交的那个
说了算**，不是最新写的那次。一个 10:30 开工、17:35 才提交的慢写入者会把指针
拖回它自己的 10:30 版本，而那个版本已经被后来的写入者清理掉了。

**已经修掉根因**：`Registry.upsert_manifest` 只允许**不比现有记录旧**的记录
接管指针（比较 `ingested_at`，也就是写入开始的时刻）。`_prune_versions` 在
删文件之前会再读一次指针，确认删的不是它现在指着的那个。
`tests/data/test_manifest_pointer.py` 把这两条钉住。

**已经在库里坏掉的指针**要靠 `repair_manifest.py` 修：它拿清单里的 checksum 去
版本目录里比对，找到还活着且内容对得上的那一版，把指针指过去；找不到就
用磁盘上最新的一版，并**用那一版自己的元数据重述承诺**（checksum / schema_hash
/ rows）。内容一个字节都不改，改的只是指针。

### 2.5 Intraday snapshot

```bash
.venv/Scripts/python.exe scripts/snapshot_intraday.py            # one shot
.venv/Scripts/python.exe scripts/snapshot_intraday.py --loop 60  # every minute
```

Writes `rt_quote` (partition = today, de-duped by `snapshot_ts,code`), the three
pools, an archive parquet per snapshot, and `data/sentiment/<date>.json`
(`limit_up / broken_board / limit_down / max_boards / break_rate / tone`).

Sources: EastMoney first, Tencent (`qt.gtimg.cn`) as fallback. EastMoney starts
dropping connections after a few thousand rapid requests; the fallback is
automatic and tagged `source=tencent` in the data.

Non-trading days are skipped on purpose: EastMoney returns the previous
session's pool for a weekend date, which would poison date-based queries.

**The loop also watches the paper book.** With `--loop`, every archived frame is
handed to `service/trading/watch.py`: the tripwire runs over the paper positions
priced at *that* frame, transitions go to `reports/paper/<date>_alerts.jsonl`,
the open set to `_watch.json`, and a heartbeat to `_heartbeat.json`
(`ok` / `idle` between windows / `closed` at 15:00 / `error: ...`). `--no-watch`
turns it off. The capture is the irreplaceable half, so a failing watch is
written into the heartbeat instead of killing the loop.

**A confirmed stop is sold, not just written down.** The stop price is the number
the model wrote on the ticket at entry, so executing it is not a second decision:
once the breach has been open for `autostop.confirm_seconds` (120 s, counted from
`first_at`, not from frames), `service/trading/autostop.py` puts the sell through
`/decision/propose` + approve exactly like a model order — the guard, T+1, the
price bands, the fees and the live-fill staleness check all still apply. The fill
carries `actor=stop:<entry ticket>`, and the thesis quotes the entry ticket and
the stop it wrote, so the record shows two authors rather than one.

`autostop.enabled: false` in `configs/params.yaml` (or `configs/local.yaml`) goes
back to "record only". A sale the guard vetoes, or a position frozen by T+1, is
reported to the loop's stdout as `[auto] <status>` and stays in `open` until it
can actually be sold.

`closed` is written only if the loop actually judged at least one frame today and
the last one finished. A holiday run starts at 09:25, finds no session and exits
immediately; a `closed` there would tell the reader the book was watched all day.
Same reason a crash on the final frame is left as `error: ...` — the heartbeat
keeps only the last line, so overwriting it with `closed` would erase the only
record that the watch broke.

**Which pool the counts come from.** `counts.limit_up`, `max_streak` and the
per-board promotion rate are read from `limit_pool_t0`, not from a vendor feed.
The derived pool is defined as `close == stk_limit.up_limit`, so its membership
can be checked stock by stock; the vendors' cannot, and both of them miss names.
Over the 38 sessions from 2026-09-01 to 10-30, 同花顺 matched the exchange
ceilings per-name on 16 days (1.5 missing a day on average, 16 on 2026-10-26) and
东财 on 4 of 19 (2.1 missing a day). The vendor number is still returned, as
`counts.vendor` (`counts.eastmoney` on `/market/pools`), and a gap wider than 5%
is explained in `notes` rather than averaged away.

`limit_up_ex_bj` excludes the 30%-band 北交所 names, and both numbers are given
because whether they belong in "涨停家数" is a convention while an omission is
not.

**北交所 is information, not inventory.** The counts, the ladder and the tone all
include BSE names — they are part of the tape. But this desk does not select or
buy them: 30% band, different liquidity, a different participant pool.

| 层 | 行为 |
|---|---|
| `breadth` / `promotion` / `tone` | 含北交所（盘面事实） |
| `screen` | 排除（`ScreenConfig.exclude_boards` 默认含 `.BJ`） |
| `browse` | 列出，但 `board: "BSE"`、`band: 0.30`、`tradeable: false`（沪深行同样给 `board`/`band`） |
| `guard.preflight` | **买入直接失败**，`failed: ["board"]`；卖出放行 |

The block lives in the guard rather than only in the screen because `screen` is
just "look for candidates" — a proposal can arrive from any other path, and a
check is the one thing that cannot be routed around. It is re-evaluated at fill
time too (`book._preflight`), so a ticket queued before the rule existed is still
stopped. Sells are exempt: an existing position must always be able to leave.

During a session the derived pool for *today* does not exist yet — it is built
from the close — so the counts fall back to the vendor and `counts.source` says
`eastmoney` / `vendor:ths`. Nothing silently claims the derived basis when it is
not there.

**Watching the derived pool itself.** `/data/status` and `/data/brief` carry a
`derived_pools` block from `tape.pool_health()`. It has three failure modes and
none of them raises:

| `status` | meaning | what the numbers become |
|---|---|---|
| `fresh` | built for the session, ladder looks warmed up | audited, `close == up_limit` |
| `missing` | no partition for that session | counts fall back to a vendor (1.6 short/day) |
| `unheated` | `max_boards <= 1` with ≥5 limit-ups | ladder unusable; tone flips 主升→修复 |

`sessions_behind` counts **open sessions**, not calendar days. `unheated` says
"suspect", not "wrong": a genuinely all-first-board day looks the same, so the
block reports the shape and lets the reader decide.

The same check caught a data-build artefact worth knowing about: the 2010–2022
year partitions were rebuilt **without** warm-up, so every one of those years
starts with a session where all `boards` are 1. `promotion_series` now detects
that boundary and returns `promotion_rate: null` with `reset: true` instead of a
fabricated 0% — "nothing promoted" and "no previous session to compare" are
different answers. All 17 partitions have since been rebuilt with
`--replace --warmup-days 30`, so the flag no longer fires on stored data
(0 of 4,084 sessions); it stays as a guard against the next cold rebuild.

The detector looks at **names sealed on both sessions**: their `boards` must be
exactly one higher. The obvious alternative — "today's max board is 1 while
yesterday's was 2+" — fires on 73 of those 4,084 sessions, and every one of them
is a real all-first-board day.

**A suspension is not a continuation.** `derive.py` used to count rows, so a name
that halted mid-streak resumed at the next board in the ladder: 002309.SZ was
sealed five times, halted two months, and reopened at the cap — which the pool
called its sixth board and carried to eight; 001331.SZ's 6-board run plus a
3-day halt plus 7 more boards read as **13**. Streaks are now gated on
`previous session == expected previous session`, and `derive_pools` takes a
`calendar_days` argument so a missing `stk_limit` day cannot quietly become
"contiguous".

**Break-rate thresholds.** 炸板率 has two definitions: the vendor pool counts
"sealed then opened", the derived pool counts any intraday touch of the ceiling.
Over the 246 sessions where both exist the derived number runs **4.84pp higher**
(0.3026 vs 0.2542). The playbook's 35% was measured on the vendor basis, so the
gate is 0.38 on the derived basis — that keeps 95.1% of verdicts, versus 87.8% if
the same 35% is applied to both. The divergence threshold (tone → 分歧) maps
45% → 47.5% the same way. `tone()` takes `basis=` and `gates()` compares each
rate against its own threshold; `borderline` is raised only when both are right
and still disagree.

**Trying to unify them with daily bars does not work — this was tested, not
assumed.** The vendor count is a steady 75% of the derived one (6,245 / 8,333)
and correlates 0.953 day to day, so it is a subset: the missing quarter is
"touched once and fell back without ever sealing", which is intraday. Three
daily proxies were checked and all fail:

| 代理 | 结果 |
|---|---|
| `open == up_limit`（开盘即封） | 只占炸板的 8.2%，与厂商差 22pp |
| 剔掉北交所 | 只去掉 0.6%，什么也解释不了 |
| 收盘距涨停价 ≤5% | 聚合上 72.8%、看着刚好；逐日平均差 **4.7 只**（基数 ~30）——拟合，不是定义 |

So: two bases, two thresholds, no merge. Settling it needs minute data that can
answer "how long was it sealed", and the free minute feed is still rate-limiting.

**"Next session" is a calendar question, not a row question.** Three places used
to take the next *row* as the next session, which is only correct when nothing
was suspended:

| 位置 | 旧行为 | 现在 |
|---|---|---|
| `derive.py` boxes | 停牌不打断连板（002309 复牌算第 6 板，001331 接成 13 板） | 相邻交易日才 +1，断层必须重新从 1 开始 |
| `promotion_series` | 按"池子里出现过的日期"配对，零涨停日会被跳过 | 按 `trade_cal` 配对 |
| `seat_record` | 自然日跨度 ≤7 就算次日，停两天+周末会混进 T+2 | `next_date == 下一个交易日`，`gap_basis` 写明用的哪一版 |

The `seat_record` one was wrong in **both** directions: `span <= 7` dropped every
sample taken on the last session before a long holiday (2026-09-30 → 10-08 is 8
calendar days — 27 rows for one seat in a 60-day window) while keeping a
one-day-halt T+2. Aligning to the calendar took that seat from 1,932 samples to
1,958.

`scripts/pool_invariants.py` 把上面这些变成可执行的检查：价格关系、池子互斥、
主键唯一、连板增量与断层重置。全库 472,763 行 / 68,350 对相邻交易日，当前 0 违反。
重建分区之后跑一遍：

```bash
.venv/Scripts/python.exe scripts/pool_invariants.py
.venv/Scripts/python.exe scripts/derive_pools.py --start 20260101 --end 20261030 --warmup-days 30 --verify
```

`derive_pools.py` 现在会打印输入缺了哪些交易日（`[gap] ...`），并且当**目标日**
缺输入时以退出码 3 结束——日更把它记成 `derived_pools` 失败，gate 变红。

数据边界：`stk_limit` 从 2010-01-04 起才有，所以 `limit_pool_t0` 也只能从那天起；
从那天到今天它**没有缺过任何一个交易日**（`daily` 自 2005 年起同样无缺口）。

### 2.5b 全市场 5 分钟线（baostock）

```bash
.venv/Scripts/python.exe scripts/intraday_sweep.py --dry          # 先看要抓多少、几次请求
.venv/Scripts/python.exe scripts/intraday_sweep.py --days 1       # 当天，全市场（日更已含）
.venv/Scripts/python.exe scripts/intraday_sweep.py --days 5       # 补最近 5 个交易日
.venv/Scripts/python.exe scripts/intraday_sweep.py --day 20261030 --refresh   # 换源覆盖
```

`daily_job` 每天盘后跑一次 `--days 1`，结果记在 `reports/daily/<day>.json` 的
`minutes_sweep` 里，**不进 gate**：分钟线是研究燃料，源不顺不该挡住明天的日线。

**baostock 的两条硬规则，违反不是慢，是被拉黑：**

| 规则 | 这条落成的形状 |
|---|---|
| 每日 ≤ 50,000 次请求 | 一次查询拉**一个代码的一整段区间**。全市场 1 天 = 5,571 次，20 天也是 5,571 次 |
| 不许并发连接 | 单进程、单个 login、串行；`data/baostock.lock` 挡住第二次运行 |

预算落在 `data/baostock_budget.json`（按日期计数，重启不清零，只用八成上限）。
开始前先算"需要多少 vs 还剩多少"，不够就**不开始**，而不是跑到一半被拉黑。

时间（实测 ~5 请求/秒、单连接）：**1 天 ≈ 18 分钟，N 天 ≈ N × 18 分钟**。
体积 48 根/票/日 × 12 字节 ≈ 3.3 MB/日、0.8 GB/年。

**两个源不能混着看，落库前已归一：**

- 成交量：baostock 给**股**，表里统一存**手**（`profile` 的 VWAP 按"手 × 100 = 股"算）。
- bar 边界：同一天 09:35 那根，腾讯收 11.45、baostock 收 11.46——两家切分不同。
  所以一个 symbol-day 只该有一个 `source`；`--refresh` 是换源的手段。
- 北交所没有 baostock 代码（报"代码未标识sh或sz"），在清单里记 `unsupported`。

### 2.6 Unattended daily job

```bash
.venv/Scripts/python.exe scripts/daily_job.py --days 3
```

Runs incremental backfill → verification → optional in-session snapshot and
writes `reports/daily/<yyyymmdd>.json`. Exit code ≠ 0 means the scheduler should
alert. See `docs/04-scheduler.md` for the Windows Task Scheduler setup.

The default dataset set is `p1,panel,dividend`. `dividend` is not optional: the
ledger applies it on the ex-date, so a stale table means positions that go ex
are marked as losses that never happened (see §5.3).

### 2.6b Dividends and bonus shares

```bash
# one-off: 2023 to today, ~920 ex-date calls (~5 min at 200/min)
.venv/Scripts/python.exe scripts/backfill.py --datasets dividend --start 20230101
```

`dividend` is pulled **by `ex_date`** — the same column it is partitioned on, so
a unit and a partition are the same thing. Pulling by `ann_date` (the old
behaviour) scattered rows into partitions of a different date and made the
partition snapshot depend on which announcement days happened to be fetched.

Rows are filtered to `div_proc == "实施"`; `stk_div` is read as the *total*
bonus+transfer per share (`stk_bo_rate` repeats it when both are present), and
rights issues (`stk_co_rate`) are ignored because this account never subscribes.

### 2.7 What is actually covered

Run `python scripts/coverage.py` for the live answer; this is the shape of it.
Coverage is **per dataset**, and the differences are not small:

| dataset | from | sessions | note |
|---|---|---|---|
| `daily`, `adj_factor`, `daily_basic` | 2005-01-04 | 5,294 | 5,894 codes incl. delisted |
| `stk_limit` | 2010-01 | ~4,000 | the vendor has nothing before 2009 |
| `fund_daily` (core ETFs) | 2007-01 | 4,573 | 18 exposures, each from its own listing |
| `limit_pool_t0` (derived) | 2023-01 | 920 | re-derive to extend |
| `ths_limit_up` | 2020-12-01 | 1,429 | the endpoint's own floor |
| `ths_limit_down` / `_break` / `lhb` / `_seat` | rolling 1 year | 242 | upstream limit, not ours |
| `ths_ladder` | last 30 sessions | 30 | date parameter ignored upstream |
| `ths_auction` / `ths_anomaly` | today | 1 | cannot be backfilled at all |
| `stk_mins` | last 7 sessions | 7 | held positions only |

Three things follow, and they are not fixable by trying harder:

- **Nothing before 2009 has limit prices.** `stk_limit` starts there, so any
  event study reaching back further cannot model 涨跌停 fills: it can say a stock
  limited up, it cannot say at what price it could not have been bought.
- **The tape is shallower than the prices.** Prices go back 21 years; the
  limit-up pool 5.9; the broken-board pool and the dragon-tiger board 1. Those
  four are the inputs to every short-horizon question.
- **Auction alerts and anomaly attribution can never have history.** Both
  endpoints return today regardless of the date requested (verified against four
  dates). The store accumulates them one session at a time from the day it
  started, and the responses say so rather than pretending otherwise.

An empty day and a missing day are different. `fetch_ths_special.py` records
zero-row answers in the ingest ledger, and `coverage.py` counts a day as a gap
only when there is neither a partition nor such a record - otherwise a quiet
session would look like a failed fetch, or a failed fetch like a quiet session.

### 2.1 Is the desk allowed to conclude about this session?

```bash
.venv/Scripts/python.exe scripts/check_readiness.py            # today
.venv/Scripts/python.exe scripts/check_readiness.py --as-of 20261030
.venv/Scripts/python.exe scripts/check_readiness.py --quiet    # exit code + one log line
```

Exit 0 when `READY`, 1 otherwise. Nothing is written except, on failure,
`data/READINESS_ALERT.txt`.

Installed as a weekday task at 09:00 and 17:45:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_readiness.ps1
Get-ScheduledTask -TaskName quant-desk-readiness
Get-Content data\readiness.log -Tail 5
```

**Why a second job when `daily_job` already has a gate.** `daily_job` writes
`data/ALERT.txt` when it runs and fails. It cannot write anything on a day it
never starts — machine off, task disabled, user logged out — and that is the
2026-10-29 case: the bar never reached the store, `data_ready` was false, and a
close report was written off an intraday snapshot anyway. Each writer clears only
its own file; `context_pack` joins both into one `pipeline_alert`, because a
session should not have to know which one spoke.

The check is deliberately read-only. It does not backfill, retry, or repair: a
checker that also fixes things cannot be trusted as a checker.

## 3. Baseline backtest

```bash
.venv/Scripts/python.exe scripts/backtest_baseline.py
```

Writes `reports/baseline_etf_rotation.json`, `reports/baseline_equity.csv`, and
caches ETF data in `data/parquet/etf_daily.parquet`. P0 findings:
`reports/P0_findings.md`.

## 4. Local API

```bash
.venv/Scripts/python.exe -m uvicorn service.api.app:app --host 127.0.0.1 --port 8811
```

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /health` | no | liveness |
| `GET /account` | token | paper account — same view as `/portfolio`, plus the order/fill snapshot |
| `GET /context` | token | regime + permission, sentiment (with its date), data health, memory |
| `GET /data/status` | token | datasets, rows, coverage, quarantine, quote age |
| `GET /data/quality` | token | latest verdicts and issue details |
| `GET /data/fingerprint` | token | `snapshot_id` per partition, for decision records |
| `GET /data/manifest/{dataset}` | token | partition-level lineage |
| `GET /data/sentiment` | token | latest ZT/ZB/DT panel |
| `GET /data/calendar/{yyyymmdd}` | token | trading-day check and neighbours |
| `GET /context_pack` | token | <4KB pack for the agent |
| `GET /portfolio` `GET /risk` | token | paper account and risk state |
| `GET /market/quote` `GET /market/bars` `GET /market/pools` | token | market reads |
| `GET /research/screen` | token | affordable candidates |
| `POST /decision/propose` | token | create a ticket (qty or target_weight; no price fields) |
| `POST /decision/{id}/approve` `.../reject` | token | approve with `{"confirm":true}` or reject |
| `GET /tickets` `GET /orders` `GET /decisions` | token | audit trail (`/tickets` defaults to `view=brief`; add `view=full` for the guard evidence, `status=open` for what is still live) |
| `POST /journal` `GET /journal` `GET /playbook` | token | memory and rules |
| `GET /memory/search` `GET /cases` | token | journal + closed-case retrieval |
| `GET/POST /research/hypotheses` `POST .../{id}/result` | token | hypothesis registry |
| `GET /playbook/history` `POST /playbook/propose` `POST /playbook/activate` | token | versioned rules |
| `POST /control/kill-switch` `.../safe-mode` | token | human stop |

Never bind to `0.0.0.0`; this service must stay local.

**核对结论跟着条目走。** `POST /journal` 把复盘里的每个数字和当天 facts 对一遍，
结论存在条目旁边；`GET /journal` 与 `GET /memory/search` 把它作为 `verified` 返回
（`null` = 从没核过，不是"核过没问题"）。写在核对机制之前的条目用这条补：

```bash
.venv/Scripts/python.exe scripts/backfill_journal_verify.py --dry   # 先看要改哪些
.venv/Scripts/python.exe scripts/backfill_journal_verify.py
```

只改 `snapshot`，不动正文；当天没有 facts 快照的条目**不补算**（账户余额读的是
"现在"，补算会把今天的余额写进那天的文件），它们保持 `null`，页级 `unchecked` 会报数。
新写一条复盘后不再需要它——那是写入时的事。

**One account, one view.** `/account`, `/portfolio` and `/risk` all read
`book.summary()`. They used to compute equity, drawdown and the position list
separately, and `drawdown` came back as `"-0.00228731278603862530110"` from
`/account` and `-0.0023` from the other two — same number, two types, two
precisions, and no way for a reader to tell which was the value and which was a
formatting artefact. `/account` keeps the extra `snapshot` block because it
answers a different question: which orders and fills produced those balances.
`positions` carries `qty` and `total_qty` (the same number under both names, until
callers have moved), plus `name`, `weight`, `sleeve` and `frozen_qty`.

**Conflicts answer 409, not 500.** The ledger is SQLite with a `revision`
optimistic lock, and the service, the scheduled jobs and manual entries all write
it. "Somebody changed the row between my read and my write" is a designed
situation, so `StateConflict` → `409 STATE_CONFLICT` and `ExecutionExists` →
`409 EXECUTION_EXISTS`, each with a `next` line saying to re-read the book. A 500
would send the caller to check its own parameters, which are fine.

**Sentiment has a date.** `/context`, `/data/brief`, `/context_pack` and
`/dashboard.json` all read it through `trading.sentiment.read_snapshot`, which
takes the newest file **at or before** the requested day and returns the date it
belongs to. Before this, three modules each read `{day}.json` and nothing else, so
on a weekend or before the 17:30 job the page showed nothing while the brief
showed the previous session — the exact disagreement `/dashboard.json`'s own
comment claimed to prevent. The block now carries `trade_date`, and `data.py`
adds `sessions_behind` (trading sessions, from `trade_cal`; `-1` when the
calendar cannot answer, because "unknown" and "zero" are different).

**The 4 KB pack sheds commentary first.** When `/context_pack` runs long it drops
`candidates`/`recent_tickets`, then `rules`/`candidates_basis`/`exposure`, then
`observations`, and only then touches `market` or `sentiment`. It used to start
by dropping `sentiment`, `observations` and `market` — the two answers the pack
exists to give. If even the skeleton will not fit it returns `degraded: true`
with an `error` rather than a plausible-looking pack with the tape missing.

### 4.1 Run it as a silent background service

The DSH plugin must not depend on a terminal or on WorkBuddy staying open:

```powershell
# install (per-user scheduled task, starts at logon, no console window)
powershell -ExecutionPolicy Bypass -File scripts\install_service.ps1

# check
Get-ScheduledTask -TaskName quant-desk-service | Select-Object State
Invoke-WebRequest http://127.0.0.1:8811/health

# remove
powershell -ExecutionPolicy Bypass -File scripts\uninstall_service.ps1
```

`scripts/supervise.py` runs under `pythonw.exe` as a watchdog: if uvicorn exits
(kill, crash, bad import), it restarts within ~3 seconds. Logs append to
`data/service.log` in UTF-8. Killing the uvicorn process is a safe restart.

**What the process table actually shows.** `.venv\Scripts\python.exe` is a uv
shim, so every role appears twice — once as the shim, once as the interpreter it
resolves to — and the parent chain is four deep:

```
.venv\pythonw.exe  supervise.py     <- the scheduled task's action
 └ uv\pythonw.exe  supervise.py     <- shim -> real interpreter
    └ .venv\python.exe  -m uvicorn  <- spawned by the supervisor
       └ uv\python.exe  -m uvicorn  <- listens on 8811
```

This is **one** service, not two. Killing a middle process takes the whole tree
down — including the supervisor, so nothing restarts it and the port stays closed
(observed: 65 seconds of downtime while waiting for a watchdog that had been
killed). Restart through the checked script; it verifies the listener's command
line and parent before stopping only that process, then waits for a different
listener whose health endpoint says `code_changed_since_start=false`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\restart_service.ps1
```

`Stop-ScheduledTask` / `Start-ScheduledTask` is **not** an API restart: Task
Scheduler can replace the watchdog while leaving its uvicorn child alive and
listening. The restart script exists because a running scheduled task and an
open port do not prove that the current code was loaded.

## 5. Trading protocol and the DSH plugin

### 5.1 The only allowed flow

```
quant_context  ->  quant_observations (constraint state + arithmetic)
               ->  quant_knowledge(ledger / analog / liquidity / risk)
               ->  quant_screen / quant_pools / quant_bars
               ->  quant_propose (intent + evidence + invalidation)
               ->  quant_approve(confirm=true)   [guard re-runs]
               ->  quant_journal_append
```

There is exactly **one** door to the aggressive book, and one to the core book:

| door | who opens it | what it does |
|---|---|---|
| `POST /decision/propose` + `/approve` | the model | its own idea, sized and vetted by the service |
| `POST /core/execute` | the model, with its own thesis | books the registered ETF plan |

**ETFs go through the first door now.** Until 2026-10-29 they could not be priced
at all (`daily` holds zero ETFs; the 18 core funds live in `fund_daily`), so the
second door was the only way to hold one — and it booked into `core_trade`, a
separate account with its own CNY 5,000. That account never traded: its rule asks
for 100% 511010 in a defensive tape, one lot of which is ~CNY 14,700 against a
CNY 5,000 book. `quote_dataset()` closes the gap, so an ETF now proposes, fills
and marks in the same account as any stock.

`/core/execute` still works and still writes `core_trade`; removing it means
migrating a batch-plan ledger into the ticket path while keeping the eight safety
properties `tests/trading/test_core.py` pins. Until then it is a second write
path and should be treated as one.

`POST /decision/propose` **forbids extra fields**: `qty`, `price`, `limit_price`,
`stop_price` all return 422. The service computes size from equity x target,
clamps to the single-name and sleeve caps, then caps by the risk budget
(`equity x 4% / stop distance`). Stop distance = `max(3%, 1.5xATR20 / price)`,
capped at 12%. Limit price = last +/-0.5% inside the exchange band.

A `BUY` is refused with 409 `OPEN_VIOLATION` while any position closes below the
stop written on its ticket. That is a risk-budget rule evaluated from prices, not
a message waiting for a reply. A `SELL` is never refused by it.

### 5.2 Install the plugin

```bash
node plugin/install.mjs        # links + profile bundle entry, backs up package.json
# restart DSH, then run:  quant_query(resource="context")
```

Uninstall with `node plugin/install.mjs --uninstall`. The plugin has no UI and
serves only the 4 public tools listed in `plugin/README.md` (backed by 73 internal handlers).

### 5.3 Paper execution model

Every fill basis, and the label matters as much as the price:

| `fill_basis` | when | price |
|---|---|---|
| `next_open` (default) | approved after the close | next session's open ± 10bps; cancelled when the open gaps through the limit |
| `intraday` | during a session, quote < 90s old | the quote, with `price_source` and `quote_age_seconds` |
| `close` | `FILL_MODE=close`; queued until the 17:30 bar when that session's bar is not published yet | that session's close; cancelled when the limit sits on the wrong side of it |
| `open_auction` | ticket written in 09:25–09:30, settled after the 17:30 bar | that session's opening print; cancelled when it gaps through the limit |
| `ceiling_queue` | `FILL_MODE=limit_queue` (board buying) | that session's up-limit × queue ratio from seal amount / traded amount |

The queue is not a second chance at the same price. A ticket written on session
D can only fill on D+1 or later, it dies after three sessions without a bar
(`PaperBook.QUEUE_TTL_SESSIONS`) — and that clock now covers the same-session
queues as well: `close`, `open_auction` and `ceiling_queue` each name one
session, and one still unsettled three sessions past it is cancelled as
`QUEUE_EXPIRED` instead of being priced off a bar the desk was not running for.
**The guard runs again at the fill**: the
position count, industry, sleeve and cash floor are properties of the account,
and the account at the open is not the account that proposed the ticket. A
ticket that fills is marked `filled` and its stop is re-anchored to the price
actually paid (`stop_price_final`).

### 5.4 Can it fill at all — `ticket.fillability`

`fill_basis` says *when and at what price*; it never said *whether*. `propose`
now attaches a `fillability` block computed by `service/trading/fill.py` (named
apart from `ticket.fill`, which is the executed fill record):

| field | meaning |
|---|---|
| `board.state` | `trading` / `sealed_up` / `sealed_down` / `suspended` / `unknown` |
| `board.opened_at_ceiling` | the session opened already at the ceiling — no queue to compete for |
| `queue.required` | a market order cannot cross the ceiling; the fill is a queueing question |
| `seal.ratio` | 封单 / 流通市值, plus `rank_in_pool` and the pool median |
| `fill_ratio.value` | **always `null`** — no subscribed feed carries queue depth |

Two things this deliberately does not do. It does not veto: `queue.required` is
a fact about the market, and turning it into a refusal would be the kernel
deciding not to trade. And it does not invent a probability: a sealed board
either fills by queueing or does not fill, and the depth in front of the order
is not in any feed here, so the ratio is `null` with the reason attached rather
than a number that would then be traded on.

The same distinction reaches the matcher. A historical bar's `open` is the 09:30
auction print, so a refusal there is `OPEN_AT_LIMIT` — the day opened sealed. A
live quote's `open` is the current price, so a refusal there is
`QUEUE_NO_PRIORITY` — the board is sealed now and can still open. `Bar.priced_at`
carries which one it was.

T+1 is enforced by the kernel: a buy raises `total_qty` but not `available_qty`;
the next trading day's `rollover()` calls `settle()`.

Corporate actions are applied on `rollover`, before the queue settles: cash
dividends to `dividend_income`, bonus/transfer shares to `total_qty` (tradable
the same day), cost basis adjusted. The stored bars are unadjusted, so without
this an ex-date is a fake loss.

### 5.4b What the guard blocks

Data (`core_usable` / `aggressive_usable` — availability, not permission), kill switch, safe mode, ST, suspension,
price band, cash, max 3 positions, single-name cap (30% stock / 40% ETF / 50%
aggressive), sleeve cap (70% core / 60% aggressive), min cash 20%, daily loss
-6%/-10%, drawdown -8% (half size) / -12% (stop), T+1, order ≤ 1% of ADV20.

A rejection is a result, not an error: the ticket is stored with `reasons` and
the agent must explain it rather than retry with different numbers.

### 5.4c Writing a close report

A close report is a claim about a session, so it needs the session. Two calls,
in this order — the first decides whether the second is allowed:

```bash
GET  /readiness?as_of=20261029     # must return {"ok": true, "code": "READY"}
POST /facts?as_of=20261029         # writes reports/facts/20261029.json
```

- `code != READY` → **do not write a verdict**. `blocks` names what is
  forbidden (`verdict`, `settlement`, `close_report`). `AWAITING_PUBLISH` is the
  15:00–17:30 window: the bar is not stored yet, so a "close" written then is a
  snapshot, and a snapshot number cannot be reproduced the next morning.
  `THIN_COVERAGE` is a truncated feed — worse than a missing one, because it
  looks like data.
- Every number the report states about the session must exist under a key in
  `reports/facts/<day>.json`. `in_cap`, `one_word` and `buyable_at_close`
  are three different questions and cannot substitute for one another;
  `board_opens.opened` answers a fourth one from a different vendor, and its
  denominator is that vendor's pool, not `in_cap`. On
  2026-10-29 a close report answered the third with a count belonging to none of
  them (6, where the session had 23 inside the cap).
- The account half is read **now**, not from `day`: `account_as_of` says which
  session it belongs to, and a facts file for another day carries `account_note`.
  For a day that is not `account_as_of`, quote only the market half (breadth /
  pools / affordable) — 2026-11-05's facts were written on 11-06 and only looked
  right because the account happened to be flat.
- Verify after writing:

```bash
.venv/Scripts/python.exe scripts/verify_report.py 20261029 --strict
```

The checker fails when a close report exists for a day that was not `READY`, or
when it quotes a session count the facts file does not contain. Noon/open notes
are written mid-session and are deliberately **not** checked against closing
facts — 13 boards had opened at 12:30, 14 by the close.

Since P85 it also re-reads each sentence as (label, number) pairs, because
"the number exists somewhere in facts" is a weak test: "可买的 6 只" passes it
whenever 6 appears anywhere. `service/trading/claims.py` reports `misplaced`
(the number belongs to another fact — 893 家跌停, 跌停 was 93), `variant`
(another vendor's figure for the same fact) and `unbacked` (that label never had
that number). Anything whose frame of reference is not the close — 盘中读数、
「我那几只」、别的交易日、引用另一份报告、变化量、N 日累计 — prints as `[INFO]`
and does not fail the run: a check that fires on correct prose is worse than none.

When `daily_job` ends with `gate=not_ready` it writes `data/ALERT.txt`, and the
context pack carries it as `pipeline_alert`. The task's exit code alone is not
a signal the desk ever sees.

### 5.5 Memory, cases and the playbook

- Every sell creates a **case** (entry/exit/PnL/thesis/invalidation, linked to
  both tickets). `GET /cases` and `quant_cases` read them.
- Journal and cases are searchable through `GET /memory/search?q=`; FTS5 uses
  the `trigram` tokenizer, so Chinese substrings of 3+ characters work.
- A hypothesis is registered as `draft`; only `testing` with metrics can move
  to `promoted` or `rejected`. Statistics decides, not the model.
- Playbook changes are drafts. A human first runs
  `.venv/Scripts/python.exe -m service.trading.playbook challenge <draft_id>` locally,
  then submits the returned one-time value as
  `POST /playbook/activate {"draft_id":"...","challenge":"..."}`. The challenge
  is bound to the draft content hash and cannot be replayed or used for another
  draft. The API has no challenge-issuing endpoint. `GET /playbook/history`
  keeps every version. The agent has `quant_playbook_propose`, never activate.

### 5.6 Research referee (P4)

A hypothesis carries a whitelisted JSON spec; the referee turns it into a
verdict. Nothing is `eval`'d and the model cannot widen the spec.

```bash
.venv/Scripts/python.exe scripts/run_research.py --hypothesis 2
.venv/Scripts/python.exe scripts/run_research.py --preset limit_up_first
.venv/Scripts/python.exe scripts/run_research.py --spec '{"pool":"broken_board","entry":"next_open","hold":0,"max_per_day":1,"rank_by":"close_to_limit","rank_dir":"asc"}'
```

- The window is a spec field, **not** tied to the data: `start` defaults to
  `20230101` so that a verdict recorded last month still means what it meant.
  Since the store now holds prices from 2005, `--start 20050101` runs a 21-year
  study - with one honest gap: `stk_limit` only begins in 2010, so before that a
  next-open fill at the upper limit cannot be detected. Studies reaching back
  should either start at 2010 or report the unmodelled window.
- One pass takes ~25s (loads four years of daily bars) and writes
  `reports/research_<id>.json`; `--hypothesis` also writes the verdict back to
  `paper.sqlite`.
- Gates: positive excess vs an equal-weight benchmark aligned to the holding
  window, bootstrap p, Deflated Sharpe over the trial grid, PBO/CSCV, CPCV with
  purging, 2x/3x cost stress, and a **capacity check** (can this account hold
  the book with one lot per name?).
- `verdict` answers "is there an edge"; `shadow_candidate` answers "is it worth
  a paper shadow book". A low Deflated Sharpe blocks capital, not research.
- Fill rules: **A-share T+1 is enforced** (a same-day round trip is rejected at
  spec parse time, so the earliest exit is the next open/close), a next-open
  entry is skipped when the open is at the upper limit, a suspension on the
  entry day drops the trade, and selection uses T0 information only (rank first,
  then check whether it could be bought).
- **`entry` has three values, and the third one is the 打板 leg:**
  `next_open` (default) buys the next session's open, `next_close` buys the next
  close, `t_close` buys **the signal session's own close** — the only entry that
  can express "buy the limit-up / buy the tail". `hold` counts bars *after* the
  entry bar, so `t_close` + `hold=1` exits on T+1 and `t_close` + `hold=1` +
  `exit_at=open` is the overnight-only leg. A `t_close` fill at a sealed limit is
  an assumption (queue), not a fact: `trades.attrs["execution_basis"]` reports
  `close_assumed` so a report can say which optimism it used.
  `t_close` is **not** yet in the referee's default grid (that would change the
  family size and invalidate recorded p-values); a verdict obtained with it
  should be labelled as such.

Run research out of process. The trading service must never block behind a
pandas job.

### 5.6.1 Convertible bonds (P7, T+0)

```bash
python scripts/backfill_cb.py --start 20230101 --end 20261023
python scripts/run_research.py --preset cb_reversal
```

- `cb_daily` is the only T+0 universe in the repo. `parse_spec` allows `hold=0`
  **only** for `pool="cb"`; stocks keep the T+1 rejection.
- Lot size is a property of the instrument (`lot=10` for CBs, 100 otherwise) and
  the capacity check uses it.
- Vendor data ships `open=0` rows; `load_panel` treats any non-positive price as
  missing so a benchmark cannot be inflated to infinity.

### 5.6.2 Stock limit -> bond (P8)

```bash
python scripts/run_research.py --preset cb_link_up
python scripts/run_research.py --preset cb_link_down_rich
```

- `pool="cb_link"` maps a *stock* event to its bond via `cb_basic.stk_code`; the
  instrument is still T+0 (`lot=10`), so `hold=0` is legal.
- `link_pool` selects the stock event (`limit_up | limit_down | broken_board`).
- `rank_by="premium"` ranks by conversion premium: `cb_close / (100/conv_price * stock_close) - 1`.
  High premium won in the sample, which is a tail-lottery story, not a convergence story.
- Never read a `testing` verdict as approval: the four gates are p, 2x cost,
  CPCV 5th percentile and Deflated Sharpe.

### 5.7 Core sleeve (P6 / P15)

The only validated book is slow: inverse-vol risk parity over ten ETFs, a
200-day trend gate, a 12% volatility target, and the remainder in a treasury
ETF. It runs once a day after the close.

```bash
python scripts/core_job.py              # mark + print the reference allocation
curl -X POST -H "$T" "/core/plan?force=true"          # what the rule would do
curl -X POST -H "$T" /core/preview -d '{"targets":{"510300.SH":0.4}}'
curl -X POST -H "$T" /core/execute -d '{"targets":{"510300.SH":0.4},
     "thesis":"...","evidence":["daily:20261023"],"invalidation":"..."}'
```

- **`/core/plan` is not executable.** It shows what the registered inverse-vol
  rule would allocate, priced into lots. `/core/execute` will refuse a plan whose
  weights came from that rule - and so will `CoreBook.execute()`, so a scheduler
  cannot do `book.execute(book.plan(day))` either. Copying the rule's weights
  into `targets` is allowed; then the decision is yours and the log says so.
- `--apply` is gone from `core_job.py` for the same reason: a job that can move
  money on a schedule is a trading bot whatever the flag is called.
- `/core/preview` prices *your* weights without booking. The useful output is
  what the arithmetic refuses - `skipped_unaffordable` (one lot costs more than
  the whole target) and `rounding_gaps` (the target is smaller than one lot).

- `CoreBook.plan()` is pure: it returns the order list and moves nothing.
  `POST /core/execute` books it and is idempotent per plan, so calling twice
  cannot double-buy. It requires a thesis, evidence and invalidation, which are
  written to the decision log.
- NAV goes to `core_nav`, fills to `core_trade`, both in `paper.sqlite`.
- **Universe** lives in [`configs/core.json`](../configs/core.json), not in code:
  one line per *exposure* (eighteen names, seventeen risk + one bond). Tracking
  the same index with six ETFs is one exposure, and risk parity over duplicates
  only splits a position into six tickets. `core.py` reads the file, and a test
  asserts that every code allocated there is also in `configs/data.json`'s
  `etf_codes`, so a held name is always a fetched name. It used to be two
  hardcoded tuples that had already drifted: 512690 was downloaded for two years
  and never allocated a cent.
- **The bond sleeve is the residual, not a parity member.** `vol` used to be
  computed over every column in the price frame, so `511010` entered the
  inverse-volatility pool with a ~2% vol, swallowed the weight, was clipped, and
  then *also* received the volatility-target remainder - while occupying one of
  the position slots. The docstring always said "the remainder in a treasury
  ETF"; the code disagreed. On 2025-10-23 the bond came out at 64% instead of
  45%, and the book held five risky names instead of six.
- **`max_positions` is a property of the account, not of the strategy.** Over 22
  trend-on quarterly samples (2018-2026) on a CNY 5,000 book:

  | positions | invested | names held | per-rebalance names that cannot buy one lot |
  |---:|---:|---:|---:|
  | 3 | 72% | 3.0 | 0.0 |
  | 4 | 65% | 3.8 | 0.2 |
  | 5 | 60% | 4.5 | 0.5 |
  | 6 | 52% | 5.0 | 0.8 |
  | 8 | 41% | 5.3 | 1.5 |

  Each extra name buys about half a holding and costs 5-7 points of exposure,
  because one sixth of 5,000 is 830 and a lot of 510300 already costs 582. The
  config sets 4. At a larger account this number should rise.
- **Lot reality**: a 100-share lot of `511010.SH` costs ~CNY 14,700, so a
  CNY 5,000 book cannot hold the bond sleeve. Whatever it cannot buy stays in
  cash and is reported in `skipped_unaffordable` / `cash_weight`; it is also
  excluded from the drift calculation, because an unexecutable rule is not a
  violated rule.
- A selected name whose target is *smaller than one lot* buys zero shares. That
  used to be silent, so a 55% target became a 22% book with nothing in the
  output to explain it. It now appears in `rounding_gaps` with the target, the
  lot price and the reason, and `effective_positions` reports what was actually
  held.
- `max_weight` is a risk limit and is enforced **after** the breadth cut. If
  `max_positions × max_weight < 1` the two cannot both hold, and the call raises
  instead of clipping and renormalising the weights back over the cap.

### 5.8 Reasoning loop (P6)

The model is not given a ranking; it is given facts and a scoring rule.

```bash
curl -H "$T" /discover            # breadth, limit structure, industries, gaps, regime
curl -H "$T" /memo                # memos with predictions
python scripts/score_memos.py     # resolve due predictions, compute Brier
```

- `POST /memo` requires `thesis` + `falsifier` + predictions of kind
  `market_up | symbol_up | core_up`, each with `horizon` and `probability`.
- `scripts/score_memos.py` resolves them against later data and writes the
  Brier score; `GET /memo/calibration` returns per-bucket calibration.
- Nothing here can promote a strategy: the referee still decides, and the
  calibration only grades the model's own claims.

See `docs/05-llm-protocol.md` for the full loop and its anti-patterns.

### 5.9 Hard stops and data readiness (P10 / P15)

A stop written on a ticket is a **constraint the model set for itself**, and the
kernel reports whether it currently holds. It does not act.

```bash
python scripts/stop_check.py            # state + exit arithmetic
curl -H "$T" /risk/stops                # distance per position
curl -H "$T" /observations              # full constraint + measures
```

**Nothing here sells.** `stops.enforce()`, `POST /risk/stops/enforce` and
`stop_check.py --enforce` were removed: a kernel that measures the breach,
prices the exit and then also pulls the trigger has stopped being an instrument.
A test asserts the module exposes no function whose name contains sell/execute/
close, so re-adding the trigger fails the build.

- Trigger is close-based (`last <= stop`). The system does not pretend to fill
  an intraday touch: with daily bars that cannot be verified. A day that pierces
  the stop intraday but closes above it is reported as `pierce_only`.
- The observation carries: close, stop, how many sessions it has been below, the
  fee + impact + net proceeds of exiting at the next open, and how this symbol
  behaved after similar down days. It carries no recommendation.
- If you *do* sell, it goes through the normal `propose -> approve` path, so T+1,
  ST, suspension, price band and the append-only audit still apply.
- `scripts/stop_check.py` exits 1 while a breach is open, so a scheduler or a
  human sees it instead of a green line.

Decisions are refused unless the bar for *today* exists:

| condition | error |
|---|---|
| not a trading day | `MARKET_CLOSED` (409) |
| trading day, no bar yet | `DATA_NOT_READY` (409) |

`GET /context_pack` exposes `session.data_ready`, `session.bars_behind` and
`session.note`, and its `as_of` is the last date with a bar — not merely today's
calendar date. Treating "data not published" as "no candidates" is the mistake
this field exists to prevent.

`scripts/daily_job.py` backs up `paper.sqlite` and `registry.sqlite` with the
SQLite backup API into `data/backups/<YYYYMMDD>/` (14 kept) before it touches
anything.

### 5.10 Execution, regime and exposure (P11)

```bash
curl -H "$T" /regime            # risk_on | neutral | risk_off | stressed + 3 numbers
curl -H "$T" /risk/exposure     # industry/board weights, HHI, beta
```

Execution mode is `next_open` out of the box; this desk runs `FILL_MODE=intraday`
(`.env`), so an order approved during a session fills at the live quote
(`fill_basis=intraday`) and falls back to the next open when there is no live
price. `FILL_MODE=close` restores the legacy
same-bar fill for contract tests):

- `propose` computes the ticket from today's close; `approve` **queues** it.
- The queue executes at the next session's open through `BarExecutionModel`
  (slippage 10 bps stocks / 5 bps ETFs, 10% volume cap, one-word boards refused).
- A gap through the limit cancels the ticket and writes a `cancel` audit row; it
  is never chased. A session without a bar leaves the ticket queued.
- Limit buffer is 2% in this mode; 0.5% made almost every entry die on an
  overnight move.

### 4.4 Intraday fills（`FILL_MODE=intraday`）




Needed before any "wake me during the session" schedule can act. Three changes,
all in service of one rule: never stamp a price you could not have got.

- **Pricing.** `_date(symbol)` now accepts today when today's daily bar does not
  exist yet but a real-time quote does. `_reference()` builds a `Snapshot` whose
  `source` reads `live:ths`, and the guard, the limit rule and the sizing code
  take it unchanged. With neither a bar nor a quote the answer is still
  `DATA_NOT_READY` - no decision gets priced off a stale close.
- **Filling.** `live_fill.build()` turns the quote into a `Bar` and the *same*
  matcher runs: price band, suspension, 10% volume cap, slippage. `bar.open` is
  set to the current price, because for a live bar the session open is hours in
  the past and filling there would be a look-back. Every record carries
  `fill_basis="intraday"`, `price_source` and `quote_age_seconds`.
- **Falling through.** Outside a session, or with a stale/failed feed, the order
  is queued for the next open instead of being filled. The refusal is written to
  the decision log as `live_refused` with its code (`MARKET_CLOSED`, `NOT_LIVE`,
  `STALE_QUOTE`), so a fallback is never silent.

What this costs: intraday fills are **not reproducible**. A replay of the day
cannot recover the quote that existed at 10:31, so the track record now has a
part that a backtest can never confirm. That is why the label is on every fill
rather than in a note somewhere.

Waking the model is a separate problem, and it lives in the host, not here: DSH
schedules the sessions (`schedule_create` with a cron expression), and each one
calls `GET /watch` first. That endpoint returns findings only where a threshold
was crossed - stop within 2%, holding moved 5%, queued order within 1% of its
limit, extreme limit-up tape - so a session with nothing to do ends after one
tool call instead of reading the whole book. A dead quote feed produces
`quotes_unavailable`, which is a finding, not a clean bill of health.

`regime` is descriptive, not predictive: equal-weight index vs MA200, 20d
realised vol and its 1-year percentile, share of names above MA20. It gates
position-taking in the daily job (`stressed` = de-risk only).

`exposure` adds the check the position count cannot: one industry is capped at
35% of equity. That cap must sit above the 30% single-name cap, otherwise it
silently becomes the single-name cap (the first attempt used 25% and rejected
every maximum-size ticket).

### 5.11 Strategy layer, allocation and impact (P13)

```bash
curl -H "$T" /strategies              # registry + this regime's capital plan
curl -H "$T" /attribution             # PnL / win rate / excess per strategy
curl -H "$T" "/execution/estimate?symbol=002966.SZ&qty=2000"
python scripts/backfill_mins.py --days 10   # 5-minute bars for holdings
```

- A ticket may carry `strategy` (default `llm`). Unknown or retired strategies are
  rejected at proposal time; `guard.strategy_cap` enforces the strategy's
  `max_weight` on current value + the new order.
- Allocation is `eligible × probation × track-record × drawdown`, normalised then
  capped, with the remainder in cash. No covariance optimisation: with a handful
  of strategies and <20 closed trades per sleeve it would be fiction.
- `core_etf` spends from its own ledger, so `book="core"` is skipped in the paper
  allocation and reported under `skipped`.

### 5.12 Information source (P15 / P16)

```bash
python scripts/fetch_ths_special.py --days 1   # limit pools, ladder, LHB
```

- `ths_limit_up` / `ths_limit_down` / `ths_limit_break`: pools with seal money,
  first seal time, opener count and the vendor's own limit reason. Supports
  history through `date_ms`.
- `ths_ladder`: the streak ladder. The endpoint always returns 30 sessions, so
  the fetch script writes only the requested day; each board holds at most 4
  names, which is why the aggregation counts streaks from the pool instead.
- `ths_limit_stat`: seal rate and per-board promotion rate. **This endpoint
  ignores `date_ms`** and always returns the current session (verified against
  three dates), so the script partitions by the upstream `trade_date` and warns
  once. Historical seal rates are derived as sealed/(sealed+broken) and flagged
  `seal_rate_derived`.
- `ths_lhb` / `ths_lhb_seat`: dragon-tiger rows at stock level and at hot-money
  seat level. Published after the close, so `data_date` in the response is the
  session the board actually belongs to — never silently today's.

### 5.13 Knowledge API and the analog window (P15)

```bash
python scripts/prepare_session.py     # warm the analog index + observation snapshot (2 s)
curl -H "$T" /knowledge/cards         # the map: market / symbol / portfolio / history
curl -H "$T" /knowledge/limit_structure
curl -H "$T" "/knowledge/lhb?symbol=002966.SZ"
curl -H "$T" "/knowledge/analog?symbol=002966.SZ&k=300"
curl -H "$T" "/knowledge/distribution?symbol=002966.SZ"
curl -H "$T" "/knowledge/crowding?symbol=002966.SZ"
curl -H "$T" "/knowledge/liquidity?symbol=002966.SZ&sizes=1000,5000,20000"
curl -H "$T" /knowledge/risk
curl -H "$T" /knowledge/ledger        # falsified setups, with the numbers
curl -H "$T" /knowledge/regime        # what each market state paid historically
curl -H "$T" /observations            # constraint state + arithmetic, no advice
```

- `analog` builds a 3.4M-row index over 3,000 sessions from 2014-06 (liquid top
  1200, amount >= CNY 20M), cached under `data/cache/analog/` at ~130 MB. Cold
  38 s, warm 0.2 s, one query 0.9 s. It is rebuilt when `daily.cover_end` moves.
- The window is **derived, not hardcoded**: `default_start()` reads `daily`'s
  `cover_start` from the manifest and keeps the last `MAX_SESSIONS` (3,000, about
  twelve years). It used to be the literal `20230101`, which meant a backfilled
  decade was stored, indexed around, and never queried. The cache key includes
  the start date, so widening the window invalidates the old index.
- `python scripts/coverage.py` answers "how much history do we actually hold".
  It lists every dataset with its cover, and for the per-session datasets it
  names the **missing trading days by date**. A hole reads as a quiet day to
  every downstream query, so this is the first thing to run after a backfill.
- Every knowledge answer carries `caveats` and `provenance`. `match.quality:
  "loose"` means today has no close historical analogue - treat it as no answer.
- `observation` has no `action` field by construction; a test walks every dict key
  and fails on `action` / `recommendation` / `suggest` / `should`.
- `limit_structure` carries a `history` block: the percentile of today's limit-up
  count and seal rate against every session the store holds, with the sample size
  and the p10/p50/p90 it was computed from. Without it "82 limit-ups, seal rate
  0.87" is a number with no scale.
- Fields the vendor cannot serve for a date come back **null, never zero**:
  querying 2023 has no limit-down or broken-board pool, so `counts.limit_down`,
  `counts.broken` and `seal_rate` are `None` with a note. An empty pool used to
  derive a seal rate of 1.0 - the strongest possible tape, invented.
- `ledger` merges the `hypotheses` table with the pre-registry rejections from
  `reports/P5-P8`, so the model cannot re-propose a setup that was already
  falsified.

Architecture contract: [`06-kernel-as-knowledge.md`](06-kernel-as-knowledge.md).

### 4.5 天花板单（`FILL_MODE=limit_queue`）

打板唯一说得通的执行方式。默认不开。

`next_open` 会在次日开盘买——那正是被证伪的动作（全样本 -0.05%）；`intraday`
在挂单那一刻就判"封死、排队未知"而拒绝。所以在这条模式之前，打板在这套系统里
**没有任何一条能成交的路径**：分析可以做，单下不出去。

```bash
FILL_MODE=limit_queue
```

- `propose` 照常出票；`approve` **不立即成交**，而是当日有效排队
  （`queue_kind=ceiling`，`expires_day = created_day`）。
- **「只在它已经封住时挂单」是使用条件，不是内核的拦截。** 限价单的价格高于
  市价时，撮合器会**以市价成交**——所以对一只开盘 16.38、收盘涨停 19.51 的票
  （300556.SZ 2026-11-02），挂 19.51 的买单会成交在 16.38。那不是打板，是开盘
  买入，而开盘买入的无偏期望是 **-0.15%**（n=298,528）。内核不拦这一单：挂什么
  价、什么时候挂是调用方的决定，内核只按限价撮合，并把成交价如实写进记录。
- 结算发生在**下一个交易日开盘**：那时当日的 `limit_up_pool` 已落库，用
  `open_times` 与 `seal_amount` 判定。开盘就在涨停、且全天没开板 → `OPEN_AT_LIMIT`，
  一分不成交。
- 「全天没开板」不等于「买不到」：000995.SZ 2026-11-02 的 `open_times=0`，但它是
  open 15.53 → 收盘 15.60 的盘中封板，封板之前随便买。把这两件事混起来，会让
  93% 的样本被误判成买不到。
- 开过板的，成交比例 = `成交额 ÷（成交额 + 封单额）`，**不打折**。这个式子用的是
  收盘封单，对开过板的票系统性偏乐观（2026-11-02 的 83 只开板票中位 94.9%），
  因为被砸开过的票收盘封单本来就薄。要不要打折、打多少，是调用方的判断，
  内核只给原始计算与口径。
- 票据上多一个 `expected_fill`：`{ratio, qty, frozen_qty}`。按 `qty` 冻结现金、
  按 `qty × ratio` 成交，**风险预算要按成交后的数量读**，否则整体偏大一倍以上。
- 冻结的是全量现金（真实券商也这样），余量在部分成交后释放。
- 小账户会遇到一道硬门槛：A 股买入必须整手。委托 100 股、成交率 24% 时，
  `int(100 × 0.24) = 24` 股不足一手，整张单会被拒（`BAD_LOT`）。**对只买得起
  一手的账户，成交率低于 100% 等于买不到**，不是"买一点"。

`FILL_MODE=close` 恢复旧的同 bar 成交，只给契约测试用。
