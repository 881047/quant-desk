# 持仓提醒：什么时候该告诉你

**一句话**：确定性规则判断，命中才发一封邮件；模型只负责写最后三句解读，没有模型也照发。

## 为什么分成两层

邮件里最容易坏的一环不是措辞，是**数字**。所以：

| 层 | 谁做 | 失败的后果 |
|---|---|---|
| 规则 | 代码，纯函数，有测试 | 会漏报或误报，但能被复算出来 |
| 解读 | 可选 LLM，只准引用已给的事实 | 跳过这一段，邮件照发 |

规则命中时附带的每一条事实都必须能用手算复核：现价、成本、止损线、距离、成交额与 20 日均量。**一条无法复算的提醒不是提醒，是观点。**

## 九条规则

| 规则 | 触发条件 | 级别 |
|---|---|---|
| `limit_down` | 现价 ≤ 跌停价 | 🔴 |
| `stop_breach` | 现价 ≤ 买入价 × 0.95 | 🔴 |
| `auction_breach` | 竞价价 ≤ 昨收 × 0.98（**只在盘前、且有真实竞价价时**） | 🟠 |
| `near_stop` | 距止损 ≤ 1.5% | 🔵 |
| `big_drop` | 单日 ≤ −5% | 🟠 |
| `volume_breakdown` | 单日 ≤ −3% 且成交额 ≥ 1.5×20 日均量 | 🟠 |
| `ma5_break` | 昨收在 MA5 上方、今收跌破 | 🔵 |
| `reconcile_gap` | 成交流水与券商持仓快照不一致 | 🟠 |
| `holdings_drift` | 账户面板市值 ≠ 账本「股数 × 现价」（差 > 2% 或 30 元） | 🟠 |
| `data_stale` | 盘中行情停在前一个交易日，或报价不是实时 | 🟠 |

同一只股票同一天只会收到**最强的那一条**：跌停了就不再单独发"破止损"。
`holdings_drift` 的去重键里带了当时的账户市值，所以**第二次成交会再提醒一次**，
不会被"今天已经发过"吞掉。

### `holdings_drift`：为什么值得单独一条

持仓明细是手工更新的（导出或粘贴），因为同花顺的明细表读不到。这没问题——股数只在交易时变。
但它有一个静默失效点：你换了仓却忘了更新，监控会拿旧股数一直算下去，止损挂在一个不存在的
仓位价上，而所有数字看起来都正常。

校验用的是账户面板（`service/trading/ths_account.py`，只读 `Static`，不抢焦点、不按键）：
账户市值 vs 账本 `Σ 股数 × 现价`，差超过 2%（且 > 30 元）就发一条。它只说"对不上"，
不裁定谁对；也不写账本、不产生任何委托。

第一次跑就抓到真实情况：账本停在 `000700 × 100`（13.86 → 1,386），账户里是 `000590 × 200`
（1,638），差 15.4%——中间那笔换仓从来没记进来。

## 两个设计上的坑

**① 竞价线不能拿 `last` 和自己比。** 竞价线 = 最近收盘 × 0.98，如果比较对象也是 `last`，这条规则永远不会触发。第一版就是这么写的。现在 `auction_price` 是独立字段，只有拿到**今天的**价格（实时行情源，非日线快照）才参与比较；拿不到就不判断——宁可不说，也不能用昨天的价格触发今天的纪律。

**② 去重是按天+按规则。** 每 5 分钟一轮，一天 66 轮；没有去重的话一次破位会发 66 封邮件。键是 `规则|代码|日期`，写进 `alerts` 表主键，`INSERT OR IGNORE` 决定谁发。发送失败时把这条**释放**，下一轮重试——一条没人收到的提醒不算发过。

## 配置（三行）

```ini
QQMAIL_USER=123456@qq.com
QQMAIL_AUTH_CODE=xxxxxxxxxxxxxxxx      # 授权码，不是 QQ 登录密码
QQMAIL_TO=123456@qq.com
```

授权码在 QQ 邮箱 → 设置 → 账户 → POP3/SMTP 服务 → 生成授权码。填登录密码会得到 `535 Error: authentication failed`，所以代码里专门把这条错误翻译成"要用授权码"。

自检：

```bash
.venv/Scripts/python.exe scripts/monitor.py --test-mail
```

可选的模型解读（不填就不写这一段）：

```ini
LLM_API_KEY=sk-...
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
```

## 什么时候发

| 任务 | 时间 | 行为 |
|---|---|---|
| `quant-desk-external-preopen` | 工作日 09:26 | 出盘前线 + 破竞价线才发 |
| `quant-desk-external-monitor` | 工作日 09:30–15:00，每 5 分钟 | 只在命中且当天没发过时发 |
| `quant-desk-external-close` | 工作日 17:35 | 导入 + 报告 + **每天一封**收盘摘要 |

摘要每天都发（哪怕什么都没发生），因为它的作用是证明这条管道还活着。事件提醒只在命中时发，所以"没收到邮件"表示"没事"，而不是"系统挂了"——这正是摘要存在的理由。

## 手动跑

```bash
.venv/Scripts/python.exe scripts/monitor.py --once --dry-run   # 看会发什么，不发
.venv/Scripts/python.exe scripts/monitor.py --once             # 真发（去重）
.venv/Scripts/python.exe scripts/monitor.py --digest --dry-run # 收盘摘要预览
.venv/Scripts/python.exe scripts/monitor.py --once --force      # 忽略去重
```


## 解读层：先交给 DSH，再退到直连

规则命中后，解读按这个顺序走：

| 顺序 | 后端 | 条件 |
|---|---|---|
| 1 | `dsh headless`（你自己的 agent） | 需要一个能连到模型的 profile，见下 |
| 2 | OpenAI 兼容接口 | `LLM_API_KEY` 或 `DEEPSEEK_API_KEY` |
| 3 | 交接文件 | 永远写，哪怕前两条都失败 |

三条路都不通时，邮件**照发**，正文只写事实，并附上这段：

> 事实与提示词已写入 `data/external/handoff/<id>.json`；
> 在 quant-desk 会话里跑 `python scripts/handoff.py show <id>` 取出问题，
> 回答后用 `python scripts/handoff.py answer <id> "解读"` 补发。

### 为什么不是"直接把消息塞进正在运行的会话"

试过三条路，都不成立，记在这里免得再试一次：

- `dsh --profile desktop headless` → `profile "desktop" is managed exclusively by the Electron application`，CLI 不允许启动桌面 profile；
- 自带的 `headless` profile 能启动，但默认路由是 `deepseek-official`，要 `DEEPSEEK_API_KEY`；凭据库里只有账号 token 与 ZAI/Qwen 的 key，没有这个；
- `dsh web` 在 64950 上确实在跑，但要求 `dsh web` 启动时打印的访问令牌，磁盘上找不到。

所以"发给它"落在了交接文件上：**DSH 在 quant-desk 会话里读这段提示词、回答，再由 `answer` 发信**。这是当前唯一能自动衔接、且不需要额外凭据的形态。

补上 `DEEPSEEK_API_KEY` 之后，第 1 条会自动生效，整条链路就变成全自动——不需要改代码。

### 在 DSH 里怎么用

```
python scripts/handoff.py pending                       # 有没有待解读的
python scripts/handoff.py show 20261027-093012          # 提示词（含全部事实）
python scripts/handoff.py answer 20261027-093012 "三句话解读"
python scripts/handoff.py drop 20261027-093012          # 不想发就丢弃
```

`answer` 会把解读写回交接文件、标成 `answered`，然后发一封 `[持仓解读]` 邮件。

## 待解读的不会自己消失（2026-11-09 补）

解读层由 `quant-desk · 收盘 提醒解读`（工作日 15:25）接管，但它的规则是**只答当日**：
历史条目一律不回答，因为十天前的事实拿来解释今天的持仓会误导。于是出现一个结构性后果——

- **15:25 之后产生的条目没有消费者。** 17:35 的收盘摘要若命中 `near_stop`、`ma5_break`，
  交接文件照样写，但当天已经没有窗口会读它，第二天起它变成「历史」，永久 pending。
- 判断口径不同时，交接会越堆越多，而「pending=N」本身不区分「等着人答」与「永远不会被答」。

清理口径（`drop` 是状态迁移，不改文档正文，`prompt` 与 `alerts` 全部保留）：

```bash
python scripts/handoff.py pending                        # 先看，按 created_at 分当日/历史
python scripts/handoff.py drop <id> --reason "为什么丢弃"
```

`--reason` 写进 `drop_reason`。没有它，三个月后只看得到一批 `dropped_at`，无法区分
「误报」与「人决定不发」——2026-11-09 清理 18 条时，其中 8 条的成因是当天修掉的两个
误报（跨日账户比对、午休误判），正是靠这句话才留得下来。

2026-11-09 的清理结果：18 条 pending → 6 条，剩下的全部是真实触发，最早 11-02。

## 它不做什么

- 不下单。整条链路里没有 `POST /order`。
- 不给买卖建议。正文最后一段写明：纪律位来自开仓时写下的失效条件，执行与否由你决定。
- 不猜。行情停更时只发一条"本轮跳过判断"，不拿昨天的价格算今天的纪律。
