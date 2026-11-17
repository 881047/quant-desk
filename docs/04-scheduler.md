# Windows 无人值守调度

目标：交易日自动跑三件事，不需要人打开终端。

1. **盘前**（09:15）：拉当日日历/基础信息，确认数据可用。
2. **盘中**（09:25 / 10:30 / 13:30 / 14:45 / 14:55）：实时快照 + 三池 + 情绪面板。
   `14:55` 那个是 2026-10-31 补的：封板与炸板集中在最后 15 分钟，而原来的四次快照
   在 14:45 就断了，收盘前那一段完全看不见。它和 14:45 共用同一条命令
   （`--close-snapshot` 按报价自身时点打标签），只是时间不同。
3. **盘后**（17:30）：增量回填、质量校验、生成日报、写核心盘净值。
4. **晚间补拉**（21:40）：只抓 19:00 / 18–20 点才发布的 `moneyflow`、`moneyflow_hsgt`。
5. **晨间补拉**（08:50）：只抓 T+1 8:30 才发布的 `margin_detail`，外加对上面两条的重试。

> 第 4、5 条是 2026-11-01 加的。在此之前这三张表写在 17:30 的 `--datasets` 里，
> 而那是一个**永远不会成功**的请求：数据还没发布，接口返回空帧，账本照样记 `done`，
> 于是 capital flow 与两融永远慢一个交易日，且没有任何一处报错。缺口不是"没排任务"，
> 是"排在了错误的时辰"。

> 没有做常驻守护进程：P1 用系统计划任务就够了，少一个会挂的组件。
> 如果机器会休眠，必须先把电源计划改成“从不睡眠”，否则 09:25 的任务会静默跳过。

## 1. 一次性创建任务

在 **管理员 PowerShell** 中执行（路径按实际安装位置调整）：

```powershell
$PY  = "D:\quant-desk\.venv\Scripts\python.exe"
$DIR = "D:\quant-desk"

# 盘前检查（工作日 09:15）
schtasks /Create /F /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 09:15 `
  /TN "quant-desk\premarket" /TR "`"$PY`" `"$DIR\scripts\daily_job.py`" --days 5 --skip-verify --snapshot"

# 盘中单帧快照（工作日 10:30 / 13:30）
# 09:25 / 14:45 / 14:55 三个单帧任务已被下面两个分帧任务取代并停用：
#   同一时刻两个进程写同一个 pool_tape 分区会撞清单指针（11-02 那次事故的形态）。
foreach ($t in @("10:30","13:30")) {
  $name = "quant-desk\snap-" + $t.Replace(":","")
  schtasks /Create /F /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST $t `
    /TN $name /TR "`"$PY`" `"$DIR\scripts\snapshot_intraday.py`""
}

# 分帧采集（工作日 09:25，一个进程覆盖全天，密度分段）
# 用 PowerShell 而不是 schtasks：这里需要 ExecutionTimeLimit=6 小时，
# schtasks 没有这个开关，而默认的 PT8M 会在第 8 分钟把循环杀掉。
#
# 密度安排（窗口写在脚本的 DEFAULT_WINDOWS 里，任务只需 --loop 30）：
#   09:25-10:00 / 14:30-15:00  每 30 秒    首封与尾盘，封单变化最快
#   10:00-11:30 / 13:00-14:30  每 3 分钟  封单金额变化慢，但"封这件事"在发生
#   11:30-13:00 不抓           午休的池子就是 11:30 那一刻
#
# 盘中那两段不是可选的：22 个交易日 544 笔炸板样本里，首次封板时间落在
# 10:00-14:30 的占 50.0%。只抓首尾两段，回封模型的训练集直接少一半，
# 而且少的那一半没有系统性。
$tpl = "-NoProfile -NonInteractive -WindowStyle Hidden -Command `"& '$PY' '$DIR\scripts\snapshot_intraday.py' --loop 30 *>> '$DIR\data\snapshot.log'`""
$set = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit (New-TimeSpan -Hours 6) -StartWhenAvailable `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "quant-desk-snapshot-loop" -Force `
  -Action (New-ScheduledTaskAction -Execute "powershell.exe" -Argument $tpl) `
  -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek MON,TUE,WED,THU,FRI -At 09:25) `
  -Settings $set | Out-Null

# 盘后数据 + 质量 + 日报（工作日 17:30）
schtasks /Create /F /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 17:30 `
  /TN "quant-desk\close" /TR "`"$PY`" `"$DIR\scripts\daily_job.py`" --days 3"

# 晚间补拉（工作日 21:40）：moneyflow 19:00 / moneyflow_hsgt 18–20 点
schtasks /Create /F /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 21:40 `
  /TN "quant-desk\late" /TR "`"$PY`" `"$DIR\scripts\daily_job.py`" --days 3 --pass late"

# 晨间补拉（工作日 08:50）：margin_detail 为 T+1 8:30
schtasks /Create /F /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 08:50 `
  /TN "quant-desk\morning" /TR "`"$PY`" `"$DIR\scripts\daily_job.py`" --days 3 --pass morning"
```

> 更省事的做法是直接跑 `scripts/install_daily_task.ps1`，它会把上面三条
> （`quant-desk-daily` / `-late` / `-morning`）一次注册好，并带"拒绝覆盖非本项目任务"的断言。

说明：

- **为什么是 17:30，不是 16:10**：Tushare 官方口径是 `daily` / `daily_basic`
  在交易日 **15:00–17:00** 之间入库（接口页写 15–16 点）。16:10 落在这个窗口内，
  遇到慢发布的那天会抓到不完整的当日数据，`core_nav` 于是记成 skipped，账户继续按
  昨天的收盘价估值。17:30 在官方上限之后 30 分钟，又在 18:10 的 agent 唤醒之前 40 分钟，
  唤醒时读到的是完整交易日。
- 日更跑两组：`p1`（行情/复权/涨跌停价）和 `panel`（东财三池）。最晚的成员是 17:00 的
  `daily` 和东财盘后池子。
- **同花顺盘面**（`ths_*` 九个数据集）不在回填链路里，由 `scripts/fetch_ths_special.py`
  单独抓——每个端点有各自的历史下限，天梯接口还会静默忽略日期参数。日更现在会调它，
  失败只报告不致命。
- `moneyflow`（19:00）、`moneyflow_hsgt`（18–20 点）、`margin_detail`（T+1 8:30）
  不在 17:30 那一趟里，各有自己的窗口（见上面的第 4、5 条）。`morning` 会重试前两者，
  因为只靠一个窗口补的洞，那个窗口坏一次就变成永久洞。
- **可转债日线与文本**（`cb_daily`、`news`）同样不在回填链路里，各有一个独立脚本，
  日更现在都调：`scripts/backfill_cb.py`（Tushare `cb_daily`，每日约 315 只、2.5 秒）
  与 `scripts/fetch_text.py --top 10 --per 6`（东财头条，约 3.5 秒，66 条）。
  在此之前两者都停在 2026-10-23 而无人知晓：可转债自 P21 退役 `cb_link_down` 后没有
  实时消费者，文本则是 `quant_knowledge(news)` 唯一的输入。前者留着是为了研究回放不会
  静默地在一个提前三天结束的窗口上计算，后者是决策链上的一环——两者失败都只报告，不阻断。
- 这些盘面数据以前完全不在日更里：2026-10-26 晚看板显示的"涨停 55 家"读自三天前的分区，
  而判定只检查东财三池，所以游资袖看起来只缺实时报价。现在 12 个盘面数据集逐个显示
  覆盖日与滞后天数。
- `daily_job.py` 自己判断是不是交易日；节假日跑一次只会刷新参考表，不会产生假数据。
- **三个 pass 共用一把锁**（`data/daily_job.lock`，pid + 6 小时超时）：同一个脚本 17:30 / 21:40 /
  08:50 各跑一次，而补跑是人工在前台再跑一次——两条路径会重叠，重叠就丢行（合并是读-改-写，
  清单守卫只排序不串行化）。拿不到锁的进程退出码 3，一个分区都不碰。锁的 6 小时上限对应下面
  `ExecutionTimeLimit`，超过它的锁是崩溃留下的文件，下一次运行自行接管。
- 每次跑到最后写 `data/daily_done/<day>.json`（`gate` + `failed_steps`）。`ALERT.txt` 只在失败时
  存在，所以「没有告警」不能证明跑过；补跑前先看 `quant_data_health` 的 `pipeline.state`。
- 任务失败会以非 0 退出码结束，可在“任务计划程序 → 上次运行结果”看到。
- 默认任务只在用户登录时运行。若希望锁屏也跑：
  任务属性 → 常规 → 勾选“不管用户是否登录都要运行”，并保存凭据；
  同时勾选“使用最高权限运行”。

## 1.5 实际安装的任务（2026-10-28）

本文档写过盘中快照，但**从未安装**——`rt_quote` 因此只有手工跑过的那一天，
而 `/data/information` 把这件事读成"盘中序列 0.1 分"。现已安装：

| 任务名 | 时间 | 作用 |
|---|---|---|
| `quant-desk-daily` | 工作日 17:30 | 盘后数据 + 质量 + 日报 |
| `quant-desk-external-preopen` / `-monitor` / `-close` | 09:30 / 15:00 / 17:35 | 外部账户同步与监控 |
| `quant-desk-snapshot-0925` / `-1030` / `-1330` / `-1445` / `-1455` | 09:25 / 10:30 / 13:30 / 14:45 / 14:55 | **全部停用**——落在 `-loop` 的窗口内，两个进程同时写 `pool_tape` 会撞清单指针（11-02 那次事故的形态） |
| `quant-desk-snapshot-loop` | 工作日 09:25 | 单个进程覆盖全天：09:25–10:00 与 14:30–15:00 每 30 秒、10:00–11:30 与 13:00–14:30 每 3 分钟、午休不抓 → `pool_tape` + `rt_quote` |
| `quant-desk-snapshot-tail` | — | **已停用**（合并进 `-loop`，单写入者） |
| `quant-desk-auction-0926` | 工作日 09:26 | 完整采集竞价告警，并写入不可覆盖的 `ths_auction_freeze` |
| `quant-desk-ths-close` | 工作日 15:20 | 同花顺 T1 数据补抓 |
| `quant-desk-service` | 登录自启 | uvicorn + 看门狗 |

`ths_anomaly` 仍是 **TODAY_ONLY**：上游只返回当天，历史漏日不能补写，所以 15:20 与
17:30 的冗余继续保留。`ths_auction` 在 2026-11-15 经真实只读请求验证支持滚动一年
历史（20251114 越界、20251117 可返回），可由 `fetch_ths_special.py --datasets
ths_auction --days N --force` 回补；每次主分区完整替换，不能把不同采集快照 merge 在一起。
09:26 的 `ths_auction_freeze` 只写真实当天且已存在就跳过，盘后刷新主分区不会改写冻结值。

`quant-desk-daily` 还会结算 `data/short_alpha_oos/` 中**已经由 WorkBuddy 冻结**的短线判断，
写 `reports/daily/<date>_short_alpha_oos.json`，并在主日报的 `short_alpha_oos` 留状态。
它只调用 settle/report，不调用候选分析或判断登记：日更没有替模型选股票的权限。
结算失败是测量缺口，不阻断行情与质量主链路；最新完整日线若没有模型记录，保持“未登记”，
不能自动补成“明确无候选”。

**分帧任务是一个进程，四段密度。** 首封与尾盘按 30 秒（封单在这两段变化最快，
分辨率就是信息），10:00–11:30 与 13:00–14:30 按 3 分钟，午休不抓（那 90 分钟的
池子就是 11:30 那一刻）。

盘中那两段不是凑数：22 个交易日 544 笔炸板样本里，**首次封板时间落在 10:00–14:30
的占 50.0%**（295 笔"封一次就炸"里 50.2%）。第一版只抓 09:25–10:00 与 14:30–15:00，
理由是"中间三小时封单基本不动"——那句话对封单额的斜率成立，对"封这件事有没有
发生"不成立，于是回封模型的训练集少了一半，而且少的那一半没有系统性。

这几段的数据是回封模型的唯一训练集：`seal_amount` 只在收盘仍封的票上有，炸板一侧
的封单过程**只有分帧能看见**。东财炸板池的原始响应里没有 `fund` 字段（实测 15 个
字段，涨停池才有 `fund`），同花顺的涨停列表也不含炸板票——所以没有免费源能补历史，
只能自己从前向数据里攒。一次全市场快照耗时 1–2 秒，30 秒一帧的负载可以忽略；
全天约 200 帧、2 万行。

窗口由 `--windows` 判定（`HH:MM-HH:MM[/秒]`），**进程在最后一个窗口结束时自己退出**，
不靠外部 kill：kill 掉的话下一次调度可能撞上一个还在写的 parquet。
`ExecutionTimeLimit` 必须是 6 小时——调度器默认 PT8M，会在第 8 分钟把循环杀掉，
而表现只是"数据少了一半"，不会报错。

只留**一个**分帧进程。原来的 `-0925` / `-1030` / `-1330` / `-1445` / `-1455` 五个单帧
任务全部停用：它们现在都落在 `-loop` 的窗口内，两个进程同一刻写同一个 `pool_tape`
分区会撞清单指针——11-02 那次三个池分区读 500 就是这么来的。

## 2. 验证任务

```powershell
schtasks /Query /TN "quant-desk\close" /V /FO LIST
```

手动触发一次：

```powershell
schtasks /Run /TN "quant-desk\close"
```

然后检查：

```powershell
Get-Content D:\quant-desk\reports\daily\<yyyymmdd>.json
curl -H "X-Quant-Token: <token>" http://127.0.0.1:8811/data/status
```

## 3. 每日自检清单（30 秒）

| 检查 | 期望 |
|---|---|
| `reports/daily/<date>.json` 是否存在 | 存在，`backfill_rc=0`、`verify_rc=0` |
| `/data/status` 的 `quarantined` | 空；非空则看 `/data/quality` |
| `/data/status` 的 `rt_quote.age_seconds` | 盘中 < 120 秒 |
| `reports/daily/<date>_quality.json` 的 `counts` | 无新增 `error`；`quarantine=0` |
| `short_alpha_oos` | 已冻结判断被结算；未登记/非交易日/失败原因分开，不自动选票 |
| `data/backfill.log` 末尾 | 无 `[error]` / `[permission]` |

## 3.5 每个数据集必须有归属

2026-10-26 一天里发现四个"有目录条目、有写入者、没有调度者"的数据集：`cb_daily` 和
`news` 落后三个交易日（两者都在决策时被读），`limit_pool_t0` 落后三个交易日（研究层读），
`stk_mins` 则根本没有读者。四个的形状完全一样，且都是靠人手动翻注册表才发现的。

所以把规则写成了契约 + 测试（`tests/data/test_schedule_contract.py`）：catalog 里每个数据集
必须属于以下四类之一——

| 归属 | 含义 | 声明位置 |
|---|---|---|
| 日更 | 由 17:30 任务刷新 | `catalog.DAILY_JOB_STEPS` |
| 参考表 | `cover_end` 是最新主体自己的日期，不是覆盖边界 | `tags=("reference",)` |
| 快照 | 只在盘中存在，盘后不可能更新 | `tags=("snapshot",)` |
| 按需 | 明确不抓，需要时才取 | `on_demand=True` |

不属于任何一类就是"没人负责"，测试失败并点名。契约里还钉了第二条：**声明了步骤不等于
真的调用了**——`CALLS` 与 `DAILY_JOB_STEPS` 必须一致，且 `scripts/daily_job.py` 里必须出现
对应调用，否则同样失败。

两个判断值得记下来，因为容易被误当成遗漏：
- `stk_mins` 标成按需，不是漏调度。它的唯一读者 `intraday.describe` 没有任何调用方——
  P13 描述的 `STOP_TRIGGER=intraday` 开关从未接线。等接线时要同时做两件事：加进
  `DAILY_JOB_STEPS`，**并且**在盘中抓（17:30 只能抓到已经收盘的那个 session）。
- `fina_indicator` 标成按需，因为它按季度更新。若算进覆盖度，`/dashboard.json` 会每天报
  "滞后 90 日"，而那不是缺陷。

## 4. 停止与回滚

```powershell
schtasks /End    /TN "quant-desk\close"
schtasks /Delete /TN "quant-desk\close" /F
```

停止调度不影响已有数据；重新启用只需再建任务。数据回滚见
`docs/03-runbook.md` 的 quarantine 说明：隔离分区对策略不可见，但文件仍在，
人工复核后可把 manifest 状态改回 `ok`。
