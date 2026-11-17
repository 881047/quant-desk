# 文档地图：哪份在生效，哪份只是当时的快照

`docs/` 下混着三种东西，读之前先认一眼，别把快照当规范：

| 类型 | 判断依据 | 怎么读 |
|---|---|---|
| **现行规范** | 改了要同步代码或测试 | 以它为准；与代码冲突时先查测试，测试就是契约 |
| **历史快照** | 文件名带日期、内容只描述某一天 | 只用于追溯；结论、数字、工具表都可能已经过期 |
| **生成物** | 文首写明由哪个脚本生成 | 不要手改，改源后重跑脚本 |

> 日期**不是**分类依据：`14-backend-debt-from-frontend-20261108.md` 与
> `session_seasonality_preregistration_20261107.md` 名字里有日期，但都还在生效。
> 下表是准的。

## 现行规范

| 文件 | 管什么 |
|---|---|
| `03-runbook.md` | 操作手册：环境、接口、日常命令 |
| `04-scheduler.md` | Windows 无人值守调度（分钟级任务） |
| `05-llm-protocol.md` | 模型在系统里负责什么、不负责什么 |
| `06-kernel-as-knowledge.md` | 内核即信息源：三层契约（文首有「术语范围」，别和内层 `service/kernel` 混） |
| `07-two-way-kernel.md` | 双向内核：能说「是」的条件与刻度 |
| `08-personas.md` | 三种人格与显式选择层 |
| `09-external-account.md` | 账外实盘账户：只读同步 |
| `10-alerts.md` | 持仓提醒：规则发事实、模型写解读 |
| `12-automation-chain.md` | 8 个自动化任务一条闭环；提示词引用的 §5 / §9 在这里 |
| `13-ux-spec.md` | 前端体验规范（Phase 2） |
| `14-backend-debt-from-frontend-20261108.md` | 后端契约（源于 2026-11-08 前端缺口，`tests/trading/test_backend_debt_contracts.py` 在跑） |
| `session_seasonality_preregistration_20261107.md` | 时段季节性研究的**冻结预注册**，参数不得事后改 |

## 历史快照

| 文件 | 冻结于 | 为什么还留着 |
|---|---|---|
| `11-tool-audit-20261102.md` | 2026-11-02 | 当时的工具能力审计；工具表此后已改，结论以现行工具描述为准 |

## 生成物

| 文件 | 谁生成 | 怎么更新 |
|---|---|---|
| `API_CONTRACT.md` | `python scripts/api_contract.py --write` | 改完接口重跑 |
| `automations/*.md` | `python scripts/freeze_automations.py` | 改完线上 Automation 重冻；`--check` 校验 |
| `USER_GUIDE.html` | 手工维护 | 直接改 |

## 相邻的索引

- `../playbook/README.md` — 规则书哪一版在生效、草案能不能用。
- `../reports/README.md` — `reports/` 里每份文件都属于某一天，没有「最新一份」。
