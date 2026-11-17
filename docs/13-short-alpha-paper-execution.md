# 短线 α 自动模拟交易契约

> 2026-11-15 用户明确选择：自动模拟交易；candidate 与 watch 都可参与；持仓中动态复评；按 α 分配权重。只使用既有 `paper.sqlite` 模拟账户，绝不读写 `data/external.sqlite`，不接券商。

## 1. 职责不变

- **内核**：提供证据、历史分布、成本、整手/现金/T+1/涨跌停/风险校验、票据与账本。
- **WorkBuddy**：独立选择 candidate/watch，给出每只股票的 `target_weight`；持仓中重新读取证据，独立决定持有或退出，并冻结理由。
- **执行层**：仍走 `quant_order(book="paper", action="propose") → quant_query(resource="ticket") → quant_order(book="paper", action="approve")`。判断登记与复评登记本身不创建订单、不改账本。

## 2. 时间与状态

1. **T 日收盘后**：DeepSeek-V4.1-Flash 读取证据，冻结 `symbol/role/thesis/evidence/invalidation/target_weight`。candidate 与 watch 都允许给出正权重；旧记录没有权重时只跟踪，不追溯下单。
2. **T+1 开盘窗口**：WorkBuddy 读取最近一个已冻结交易日，逐只检查实时会话、现金、持仓、成本和风控；以 `strategy=short_alpha`、固定 `client_request_id=short-alpha:{T}:entry:{symbol}` 提案并批准。`queued` 是排队，不是成交；被拒或买不满不得伪造成交。
3. **持仓中复评**：每个收盘后窗口读取最新证据，逐只冻结 `hold / exit_next_open / exit_now`、理由与证据。需要退出时仍由 WorkBuddy 走 SELL 提案与批准，固定 `client_request_id=short-alpha:{T}:exit:{symbol}`。
4. **最迟 T+2 退出**：T+1 收盘后可排 T+2 开盘卖单；未提前退出者，T+2 尾盘窗口必须退出。T+1 买入当日受 A 股 T+1 约束，不能当天卖出。复评可以提前退出，不能把持有期限延长到 T+2 之后。
5. **结算**：原 research/executable 双轨样本外结算继续保留；实际模拟票据、成交、未成交、复评与退出另列，不把排队或计划写成收益。

## 3. 账户隔离与冲突

- 短线 α 是主模拟账户中的独立策略标签 `short_alpha`，不是另一本账，也不触碰账外实盘。
- `short_alpha` 注册为 `discretionary + probation`，总权重上限 0.60；这不是有效性证明。
  `auto_allocate=false`：通用 allocator 不预先分走现金，资金只来自 WorkBuddy 已冻结的
  逐股 `target_weight`；上限仍由 guard 使用。
- 组合仍受同一现金、单票、策略总权重、持仓数与风险预算约束；target_weight 总和不得超过策略上限。
- `quant_query(resource="portfolio")` 与 `quant_query(resource="context")` 的持仓必须带 `strategy`，票据索引必须带 `client_request_id`，便于其他自动化识别来源。
- 既有 09:26、13:35、14:30 自动化不得对 `strategy=short_alpha` 持仓提出新的自由裁量买卖；专属短线 α 入场/退出任务负责管理。所有任务仍必须执行既有硬性止损与风控，不能把“专属管理”当成绕过止损的理由。
- 同一股票已有非 short_alpha 持仓时，不另起短线 α 入场；先报告来源冲突，避免 latest BUY 标签覆盖原策略归属。

## 4. 接口与工具

- `POST /short-alpha/judgments`：item 新增可选 `target_weight`；新自动交易判断必须逐只提供，旧快照保持原哈希与原含义。
- `POST /short-alpha/reviews`：不可变、同请求号幂等的 WorkBuddy 复评冻结；只记录，不直接下单。
- `GET /short-alpha/execution`：按冻结信号日关联 entry/exit 票据、当前持仓、复评与 T+2 边界；只读。
- 插件/MCP 增加 `quant_workflow(operation="short_alpha_review")` 与 `quant_query(resource="short_alpha_execution")`；实际订单继续使用既有 `quant_order(book="paper", action="propose")/quant_order(book="paper", action="approve")`。

## 5. 自动化

- 既有 19:10 任务保留 T 日判断，并承担持仓收盘后复评。
- 新增 **09:24** 短线 α 入场任务：只处理最近已冻结信号的 T+1，不处理历史 pending 研究记录；先于既有 09:26 普通首窗任务。
- 新增 **14:28** 短线 α 退出任务：处理 `exit_now` 与最迟 T+2 退出，不新开仓；先于既有 14:30 普通末窗任务。
- 自动任务只在 WorkBuddy Hy4 的 DeepSeek-V4.1-Flash 会话执行；宿主排程抖动时以 `session.label/now` 判断实际窗口。
- 休市、数据未就绪、服务不可达、非 T+1/T+2、已处理或 guard 拒绝均是明确终态，不换参数绕过。
- 2026-11-15 已在宿主安装并实读为 11 个 ACTIVE 任务；4 个既有任务已更新，2 个专属任务已新增。安装回执见 `reports/20261115-short-alpha-automation-install.json`；该回执只证明配置，不证明自然执行或成交。

## 6. 验收边界

- 判断与复评不可篡改、请求号幂等；不同内容同请求号返回 409。
- candidate/watch 都能冻结权重；缺失/负数/超总预算被拒绝；旧记录不补权重、不补订单。
- 订单只来自 WorkBuddy 显式 propose/approve；review/judgment/execution 查询本身均不写账本。
- 测试使用独立临时账本和临时数据目录，不碰真实 `paper.sqlite`、`external.sqlite` 或 20261113 正式快照。
- 本轮未经过自然调度与真实 T+1/T+2 前，不得声称已产生样本外模拟收益或已完成全自动实盘验收。
