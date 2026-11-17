"""所有"会有人想改的数"的唯一入口。

## 为什么要有这一层

此前同一个参数常常有三个去处：`configs/risk.yaml` 里一份、`guard.RiskLimits`
的 dataclass 默认值一份、`kernel/risk.RiskConfig` 的默认值一份。三份里只有
dataclass 那两份生效——**`risk.yaml` 从来没有被读过**，它是文档不是配置。
改风控上限的人按最直觉的做法去编辑 yaml，得到零效果，而且不会有任何提示。

所以这里的规则改成：

1. **代码里不写值。** 本模块的 `SPECS` 只声明"有这个参数、它是什么类型、允许
   什么范围、它是干什么的"，值一律在 `configs/params.yaml`。
2. **缺键就报错。** 不做"读不到就用默认值"的兜底。兜底会让一份打错的 yaml 安静
   地退化成去年的参数，而账户还在按那份参数下单。宁可启动失败。
3. **一个名字只出现一次。** 想改阈值，改 yaml；想加参数，在 `SPECS` 里声明并
   在 yaml 里给值，两步都做才通过。
4. **来源可查。** `describe()` 记录每个值来自哪个文件、有没有被环境变量覆盖，
   `/health` 与排查都读它。

## 覆盖顺序

`configs/params.yaml` < `configs/local.yaml`（本机覆盖，不入库）< 环境变量
`QUANT__RISK__MAX_POSITIONS=3`。环境变量优先是为了让"临时把仓位上限压到 1"
不需要改文件、也不需要重启后忘记改回来——shell 退出即失效，而文件会留下来。

## 与 `kernel/units.py` 的分工

1 手 100 股、一年 252 个交易日这类**改了就是错**的事实不在这里，在 units。
这里的每个键都允许被合理地改掉。
"""

from __future__ import annotations

import math
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "configs"
PARAMS_FILE = CONFIG_DIR / "params.yaml"
COSTS_FILE = CONFIG_DIR / "costs.yaml"
LOCAL_FILE = CONFIG_DIR / "local.yaml"

ENV_PREFIX = "QUANT__"


class MissingParameter(KeyError):
    """A key the code asks for has no value in any config file.

    Callers that catch ``KeyError`` to mean "the data is unavailable" must
    re-raise this class first: a missing rate is a configuration error, and
    downgrading it to a default value (or to "price unknown") hides the one
    fact the reader needs. Catch it explicitly and let it out.
    """


class BadParameter(ValueError):
    """A value exists but is the wrong type, out of range, or breaks an invariant.

    Same rule as :class:`MissingParameter`: do not fold it into a generic
    ``ValueError`` fallback that degrades to a default.
    """


@dataclass(frozen=True)
class Spec:
    """Schema for one parameter. Holds *no* value — that is the whole point."""

    kind: type
    doc: str
    lo: float | None = None
    hi: float | None = None
    unit: str = ""


def _s(kind: type, doc: str, lo: float | None = None, hi: float | None = None,
       unit: str = "") -> Spec:
    return Spec(kind, doc, lo, hi, unit)


# ── 声明区 ──────────────────────────────────────────────────────────────────
# 只增不改语义：删掉一个键之前先确认没有代码引用（tests/test_settings.py 会查）。
SPECS: dict[str, Spec] = {
    # ── 数据源 ──────────────────────────────────────────────────────────────
    # 抓取层的超时与分页。它们不影响判断，但影响"数据到没到"，而数据没到会让
    # 下游把缺失当成零。放在这里是为了限流时能只调一处。
    "source.tencent.timeout": _s(float, "腾讯行情超时秒数", 0.5, 120),
    "source.tencent.retries": _s(int, "腾讯行情重试次数", 0, 20),
    "source.tencent.chunk": _s(int, "腾讯行情单次请求代码数", 1, 500),
    "source.tencent.max_seconds": _s(float, "腾讯行情总耗时上限秒数", 1, 600),
    "source.eastmoney.timeout": _s(float, "东财超时秒数", 0.5, 120),
    "source.eastmoney.retries": _s(int, "东财重试次数", 0, 20),
    "source.eastmoney.min_interval": _s(float, "东财两次调用最小间隔秒数", 0, 10),
    "source.eastmoney.max_seconds": _s(float, "东财总耗时上限秒数", 1, 600),
    "source.cninfo.timeout": _s(float, "巨潮公告超时秒数", 1, 300),
    "source.cninfo.retries": _s(int, "巨潮重试次数", 0, 20),
    "source.cninfo.min_interval": _s(float, "巨潮两次调用最小间隔秒数", 0, 10),
    "source.cninfo.max_seconds": _s(float, "巨潮总耗时上限秒数", 1, 600),
    "source.cninfo.max_pdf_bytes": _s(int, "单个公告 PDF 字节上限", 1024, 1_073_741_824),
    "source.ths.timeout": _s(float, "同花顺超时秒数", 1, 120),
    "source.ths.page_size": _s(int, "同花顺分页大小", 1, 500),
    "source.ths.max_pool_pages": _s(int, "同花顺池子最多翻几页", 1, 500),
    "source.ths.auction_page": _s(int, "同花顺集合竞价单页行数（接口 >20 报 1003）", 1, 20),

    # ── 存储与缓存 ──────────────────────────────────────────────────────────
    "storage.validated_cache_limit": _s(int, "parquet 校验结果缓存条目数", 16, 1_000_000),
    "storage.lock_wait_seconds": _s(float, "等待写锁的秒数", 1, 3600),
    "cache.analog_keep": _s(int, "相似日索引保留几份", 1, 20),
    "cache.pool_limit": _s(int, "池子解析缓存条目数", 1, 50),
    "cache.market_seconds": _s(
        float, "行情事实（帧/清单/ST 标记）的新鲜窗口秒数", 0, 3600),
    "cache.system_ttl": _s(float, "/system 缓存秒数", 0, 600),
    "cache.system_limit": _s(int, "/system 缓存条目数", 1, 100),
    # 强制刷新池子的节流：一次是全市场 5,500 只。这个数原来是
    # `_REFRESH_MIN_GAP = 120` 的字面量，于是"上游刚挂过一次，多久后重试"
    # 只能改代码；而它同时决定了故障期间调用方看到的那句话是什么。
    "pools.refresh_min_gap": _s(float, "强制刷新池子的最小重试间隔（秒）", 0, 3600),
    "api.sample_target": _s(int, "样本账户目标成交笔数", 1, 500),
    "research.default_horizon": _s(int, "研究默认持有期（session）", 1, 500),

    # 账户与风控（configs/risk.yaml 的继任者）
    "risk.initial_cash": _s(float, "起始资金，元", 0, unit="CNY"),
    # 样本账本（data/samples.sqlite）的独立规模。它原来是模块级
    # `SAMPLES_CASH = os.getenv("SAMPLES_CASH", "2000")`：读一次、import 时定死，
    # 测试改不动，也不出现在 provenance 里。账户规模属于配置，两本账都一样。
    "research.samples_cash": _s(float, "样本账本起始资金，元", 0, unit="CNY"),
    "risk.max_positions": _s(int, "同时持有的最大标的数", 1, 20),
    "risk.max_core_weight": _s(float, "核心仓（ETF）总权重上限", 0, 1),
    "risk.max_aggressive_weight": _s(float, "进攻仓（个股）总权重上限", 0, 1),
    "risk.min_cash_weight": _s(float, "现金最低权重", 0, 1),
    "risk.max_core_single": _s(float, "单只核心个股上限", 0, 1),
    "risk.max_core_etf_single": _s(float, "单只核心 ETF 上限", 0, 1),
    "risk.max_aggressive_single": _s(float, "单只进攻个股上限", 0, 1),
    "risk.max_industry": _s(float, "单行业上限，必须大于 risk.max_core_single", 0, 1),
    "risk.max_trade_risk": _s(float, "单笔亏损占权益上限", 0, 1),
    "risk.max_gap_loss": _s(float, "跳空情形下单笔亏损上限", 0, 1),
    # 市价买入按开盘价冻结，成交价可能更高（滑点、竞价偏移）。这个缓冲是
    # "冻结多少"，不是"允许亏多少"；曾经在 oms 和 risk 各写死一次 1.02。
    "risk.market_buy_buffer": _s(float, "市价买入冻结现金的缓冲比例", 0, 0.5),
    # 限价单的缓冲：next_open 的票要跟上开盘跳空，盘中单只给一点余量。
    # 这两个值曾经写死在 decisions.py 里，与上面的冻结缓冲各说各话。
    "execution.next_open_buffer": _s(float, "next_open 限价相对现价的缓冲比例", 0, 0.5),
    "execution.intraday_buffer": _s(float, "盘中限价相对现价的缓冲比例", 0, 0.5),
    "execution.min_ticket_bps": _s(float, "单笔往返固定成本上限 bps；0 表示只报告不拦单", 0, 1000),
    "risk.max_adv_ratio": _s(float, "单笔金额占 20 日均额比例上限", 0, 1),
    "risk.max_actions_per_day": _s(int, "每 session 成交笔数上限（买卖合计）", 1, 100),
    "risk.daily_loss_no_new": _s(float, "日内亏损到此不再开新仓（负值）", -1, 0),
    "risk.daily_loss_reduce_only": _s(float, "日内亏损到此只许减仓（负值）", -1, 0),
    "risk.drawdown_half": _s(float, "回撤到此仓位减半（负值）", -1, 0),
    "risk.drawdown_stop": _s(float, "回撤到此停止开仓（负值）", -1, 0),
    "risk.max_consecutive_losses": _s(int, "连续亏损几笔后降档", 1, 50),

    # 选股域（configs/universe.yaml 的继任者）
    "screen.max_price": _s(float, "候选最高股价：5000 元账户要买得起 1 手", 0, unit="CNY"),
    "screen.min_adv_cny": _s(float, "候选最低 20 日均额", 0, unit="CNY"),
    "screen.min_list_days": _s(int, "上市天数下限", 0, 3650),
    "screen.exclude_st": _s(bool, "剔除 ST"),
    "screen.exclude_boards": _s(tuple, "剔除的板块前缀/后缀，如 .BJ / 688"),
    "screen.max_candidates": _s(int, "每日候选数量上限", 1, 100),
    "screen.momentum_days": _s(int, "排序用的动量窗口", 1, 250),
    "screen.rank_weight_momentum": _s(
        float, "rank_key 里动量分位的权重；流动性拿剩下的", 0, 1),
    "screen.max_abs_pct": _s(float, "当日涨幅超过此值不再追（%）", 0, 30),
    "screen.affordable_weight": _s(float, "按仓位算可买股价时用的权重", 0, 1),
    "screen.panel_days": _s(int, "为凑够 30 个 session 而回看的日历天", 30, 400),
    "screen.browse_limit": _s(int, "自由浏览单次返回行数上限", 1, 1000),

    # 情绪与周期
    "sentiment.break_rate_gate": _s(float, "推导口径炸板率闸门：高于此值视为退潮", 0, 1),
    "sentiment.break_rate_gate_vendor": _s(float, "上游口径炸板率闸门", 0, 1),
    "sentiment.break_rate_divergence": _s(float, "推导口径炸板率：分歧区上沿", 0, 1),
    "sentiment.break_rate_divergence_vendor": _s(float, "上游口径分歧区上沿", 0, 1),
    "sentiment.tone_max_boards": _s(int, "tone 判「主升」要求的最高连板数", 1, 30),
    "sentiment.tone_min_limit_up": _s(int, "tone 判「主升」要求的涨停家数", 1, 500),
    "tape.promotion_spread_warn": _s(float, "两来源晋级率差超过此值就写明", 0, 1),
    "tape.count_gap_warn": _s(float, "两来源涨停家数相对差超过此值就写明", 0, 1),

    # 大盘状态
    "regime.trend_ma": _s(int, "趋势过滤均线窗口", 5, 500),
    "regime.vol_window": _s(int, "波动率窗口", 2, 250),
    "regime.breadth_window": _s(int, "宽度窗口", 2, 250),
    "regime.vol_stress": _s(float, "波动率一年分位高于此值算紧张", 0, 1),
    "regime.vol_calm": _s(float, "波动率分位低于此值算平静", 0, 1),

    # 盯盘与预警
    "monitor.hard_stop_pct": _s(float, "硬止损幅度（正数，-5% 写 0.05）", 0, 0.5),
    "monitor.auction_line_pct": _s(float, "低开预警幅度（正数，2% 写 0.02）", 0, 0.3),
    "monitor.near_stop_pct": _s(float, "距止损多近发提醒（正数，1.5% 写 0.015）", 0, 0.3),
    "tripwire.stop_proximity": _s(float, "距止损多近算「贴脸」", 0, 0.5),
    "tripwire.position_move": _s(float, "单票日内波动多大算异动", 0, 0.5),
    "tripwire.pending_distance": _s(float, "挂单距现价多近可能开盘成交", 0, 0.2),

    # 规则代下的那一种单：止损。开关放在这里而不是代码里，因为"要不要让
    # 代码扣扳机"是账户主人的决定，不是部署细节——关掉它，破线就退回"只记
    # 不卖"，模型下一次醒来才处理。
    "autostop.enabled": _s(bool, "破线后由循环代下卖单（false = 只记不卖）"),
    "autostop.confirm_seconds": _s(
        float, "破线持续多少秒才算成立（按时间不按帧，抓取节奏会变）", 0, 3600),

    # 入场意图拆成两级：observe 只让模型把条件写进票并逐帧求值（shadow 轨迹，
    # 零资金副作用），enabled 才允许盯盘循环对命中条件的 armed 票调 approve
    # 出钱。两者独立是刻意的——「能写买卖点」不该自动等于「能下单」。
    # 与 autostop 也独立：关掉 autostop 不等于允许入场意图自动兑现。
    # 缺省全关，缺键即启动失败（本模块不做代码兜底），这是资金路径的硬约束。
    "trigger.observe": _s(bool, "允许模型写入场条件并做影子求值（false = 一律拒收条件）"),
    "trigger.enabled": _s(bool, "允许帧兑现 armed 意图出钱（false = 只记不兑现）"),

    # 成交与冲击成本
    "impact.c_small_cap": _s(float, "平方根冲击模型的小盘系数", 0, 10),
    "impact.max_schedule_days": _s(int, "拆单最长天数", 1, 1000),
    "impact.max_participation": _s(float, "单日参与率上限", 0, 1),

    # 交易成本（configs/costs.yaml）。换券商只改那一处。
    # 这 8 个值此前有三份：costs.yaml、impact.py 的模块常量、
    # kernel/execution.BarExecutionModel 的 dataclass 默认值。后两份生效，
    # yaml 是文档。现在只有 yaml 生效。
    "costs.commission_rate": _s(float, "佣金费率", 0, 0.01),
    "costs.min_commission": _s(float, "单笔佣金下限", 0, unit="CNY"),
    "costs.stamp_duty_sell": _s(float, "卖出印花税", 0, 0.01),
    "costs.transfer_fee": _s(float, "过户费", 0, 0.01),
    "costs.slippage.stock_bps": _s(float, "个股滑点假设", 0, 500, unit="bps"),
    "costs.slippage.etf_bps": _s(float, "ETF 滑点假设", 0, 500, unit="bps"),
    # 转债 tick 0.001、盘口薄，按个股 10bps 估会低估冲击；单列一项而不是
    # 继承 ETF 的 5bps（生产里 CB 的 is_etf 是 False，曾经落到个股分支）。
    "costs.slippage.cb_bps": _s(float, "可转债滑点假设", 0, 500, unit="bps"),
    "costs.limit_up_penalty_bps": _s(float, "涨停买入的额外惩罚", 0, 500, unit="bps"),
    "costs.volume_cap_ratio": _s(float, "单笔不超过当日成交额比例", 0, 1),

    # 就绪度与数据质量
    "readiness.min_coverage": _s(float, "收盘前判定数据齐全的最低覆盖率", 0, 1),
    "readiness.close_time": _s(str, "收盘时刻 HH:MM"),
    "quality.quarantine_ratio": _s(float, "单日坏行占比超过此值隔离该分区", 0, 1),
    "quality.factor_identity_tolerance": _s(float, "复权因子恒等检验容差", 0, 1),
    "quality.reconcile_budget_seconds": _s(float, "对账预算秒数", 1, 3600),

    # 日更的状态与单写者锁
    "pipeline.daily_due": _s(str, "日更到点时刻 HH:MM（与 install_daily_task.ps1 一致）"),
    "pipeline.stale_after_minutes": _s(float, "锁超过此分钟数按崩溃遗留处理", 1, 1440),

    # 行情缓存与熔断
    "realtime.default_ttl": _s(float, "报价默认缓存秒数", 0, 300),
    "realtime.stale_after": _s(float, "报价超过此秒数标记 stale", 0, 3600),
    "realtime.batch": _s(int, "批量行情单次代码数", 1, 500),
    "realtime.timeout": _s(float, "行情请求超时秒数", 0.5, 60),
    "realtime.breaker_threshold": _s(int, "连续失败几次熔断", 1, 100),
    "realtime.breaker_cooldown": _s(float, "熔断冷却秒数", 0, 3600),
    "api.breaker_window": _s(float, "接口错误率统计窗口秒数", 1, 3600),
    "api.breaker_limit": _s(int, "窗口内错误数上限", 1, 10000),
    # 91 条路由各占两个 bucket（匹配 + 未匹配的 ! 孪生）≈ 182，所以下限给 256：
    # 配得比这更小，正常路由会互相挤掉，熔断静默失效。
    "api.breaker_max_keys": _s(int, "熔断表保留的路径形状上限", 256, 100000),

    # 输出预算
    "context.max_bytes": _s(int, "context_pack 的字节上限", 512, 65536, unit="B"),
    "persona.max_switches_per_day": _s(int, "人格切换次数上限", 0, 50),
    "persona.min_reason_chars": _s(int, "切换理由最少字数", 0, 500),
    "persona.min_invalidation_chars": _s(int, "失效条件最少字数", 0, 500),
    "persona.edge_bps_bound": _s(float, "edge 声明的合理上限", 1, 100000, unit="bps"),
    "playbook.max_chars": _s(int, "规程正文长度上限", 100, 200000),
    "playbook.min_chars": _s(int, "规程正文长度下限", 0, 10000),
    "timeline.max_read_limit": _s(int, "时间线单次读取上限", 10, 1000000),

    # 相似日检索
    "analog.top_n": _s(int, "参与检索的标的数", 10, 6000),
    "analog.min_amount": _s(float, "进入索引的最低日均额", 0, unit="CNY"),
    "analog.min_history": _s(int, "最少历史 session 数", 10, 2000),
    "analog.max_abs_ret": _s(float, "单日涨跌幅超过此值视为数据错误", 0, 5),
    "analog.bootstrap_draws": _s(int, "bootstrap 抽样次数", 10, 100000),
    "analog.seed": _s(int, "检索随机种子：同一天必须给出同一答案", 0, 2**31 - 1),
    "analog.max_sessions": _s(int, "索引保留的最长历史", 100, 20000),
    "analog.weights": _s(tuple, "各特征距离权重，长度必须等于特征数"),
    "analog.horizons": _s(tuple, "前瞻收益的持有期（session）"),
    "analog.index_version": _s(int, "索引格式版本，改动特征时 +1", 1, 1000),

    # 衰减与账目核对
    "account_check.tolerance_pct": _s(float, "账目核对相对容差", 0, 1),
    "account_check.min_abs": _s(float, "账目核对绝对容差", 0, unit="CNY"),
    "account_check.drift_floor": _s(float, "低于此市值不判漂移", 0, unit="CNY"),
    "manual.tolerance_pct": _s(float, "手工填单核对相对容差", 0, 1),
    "manual.min_abs": _s(float, "手工填单核对绝对容差", 0, unit="CNY"),
    # 债券利息的个人所得税，按次代扣。这是税制而不是本台偏好，所以可调；
    # 写死在 corp_action 里会让人以为它是物理常数。
    "tax.cb_coupon_withholding": _s(float, "可转债利息代扣税率", 0, 1),
    "guard.cb_last_buy_sessions": _s(int, "转债到期前多少 session 停止买入", 0, 250),
    "handoff.stale_lock_seconds": _s(float, "交接锁过期秒数", 1, 86400),
    "live_fill.max_quote_age": _s(float, "报价超过此秒数不得用于成交", 0, 3600),
    "ths_account.identity_tolerance": _s(float, "同花顺两面板总资产一致容差", 0, 1),

    # 核心盘（ETF 风险平价 + MA regime + 波动目标）
    # 这些曾经是 CoreConfig 里的字面量：core.json 决定"买哪些"，这些数决定
    # "怎么配比"，而后者改一次要动代码。佣金与每手股数不在这里——它们分别是
    # costs.* 与 units.SHARES_PER_LOT，抄一份就等于给同一笔钱两套费率。
    "core.vol_window": _s(int, "核心盘波动率窗口（session）", 2, 250),
    "core.vol_target": _s(float, "核心盘年化波动目标", 0, 1),
    "core.max_weight": _s(float, "核心盘单腿权重上限", 0, 1),
    "core.ma_window": _s(int, "核心盘趋势过滤均线窗口", 5, 500),
    "core.min_trade_cny": _s(float, "核心盘调仓最小成交金额", 0, unit="CNY"),

    # 研究域
    "research.min_trades": _s(int, "回测最少成交笔数", 1, 100000),
    "research.account_max_positions": _s(int, "研究账户同时持仓上限", 1, 50),
    "research.min_account_trades": _s(int, "研究账户最少成交笔数", 1, 100000),
    "research.max_hold": _s(int, "最长持有 session", 1, 500),
    "research.forward_bars": _s(int, "前瞻收益的 bar 数", 1, 20),
    "research.scan_threshold": _s(float, "横截面扫描的显著性阈值", 0, 1),
    "research.scan_min_amount": _s(float, "扫描的最低成交额", 0, unit="CNY"),
    "research.pct_window": _s(int, "分位窗口", 20, 2000),
    "research.pct_min": _s(int, "算分位所需最少样本", 10, 2000),
    "research.min_bucket_n": _s(int, "单个分桶最少样本", 1, 5000),
    "research.state_min_episodes": _s(int, "状态分桶最少 episode 样本", 1, 5000),
    "research.tape_min_episodes": _s(int, "ticker-tape 最少 episode 样本", 1, 5000),
    "research.recent_sessions": _s(int, "近期窗口 session 数", 20, 5000),
    "research.cancel_ratio": _s(float, "撤单率分档阈值", 0, 1),
    "research.band_tol": _s(float, "涨跌停档位判定容差", 0, 0.1),
    "research.max_days": _s(int, "研究取数的日历天上限", 30, 5000),
    "research.lookback": _s(int, "信息面自评回看 session", 20, 2000),
    "research.master_grace": _s(int, "数据主表缺失容忍天数", 0, 30),
    "research.weak_dimension_ceiling": _s(
        float, "信息面自评中列为弱维度的分数上限（仅影响列举，不改评分）", 0, 1),
    "research.quote_stale_seconds": _s(float, "信息面自评里报价过期秒数", 0, 3600),
    "research.fundamental_min_coverage": _s(
        float, "最新报告期入库覆盖度下限（低于此值=已披露未入库）", 0, 1),
    "research.fundamental_max_stale_days": _s(
        int, "最新报告期公告日距今上限（日历天）", 1, 400),
    "research.min_neighbours": _s(int, "同类样本最少邻居数", 1, 500),
    "research.default_k": _s(int, "同类分析默认近邻数", 1, 500),
    # trigger.enabled 的晋级门禁。数值预注册在 params.yaml，代码里不写死。
    # scripts/promotion_gate.py 只读，postclose_check.py 每天监督；两者都
    # 不会替人打开资金开关。缺键即启动失败，避免静默沿用旧门槛。
    "promotion.min_memo_n": _s(int, "Brier 校准所需已结算断言数", 1, 1000),
    "promotion.min_bss": _s(float, "Brier Skill Score 下限（0=优于基础率）", -1, 1),
    "promotion.min_accepted_hypotheses": _s(
        int, "通过 2x 成本与 DSR 的预注册策略最少条数", 0, 100),
    "promotion.atr_multiple": _s(
        float, "账户粒度体检用的正常波动止损宽度（ATR 倍数）", 0.5, 10),
    "promotion.benchmark_symbol": _s(str, "账户粒度体检的基准票（口径见脚本注释）"),
    # 影子晋级的两个闸门。它们与 referee.* 不是同一件事：referee 判"是不是噪声"，
    # 影子判"值不值得花资源跟踪"。所以 p 值更严（0.025 < p_max 0.05），
    # 而选择风险不严于资本晋级（shadow_gate >= pbo_promote）。
    # 2026-11-12 desk/v2 随 referee 一起收紧，保持"影子 p 更严"的相对关系。
    "research.shadow_gate": _s(float, "影子晋级的 PBO 上限", 0, 1),
    "research.shadow_p_max": _s(float, "影子晋级的 p 值上限", 0, 1),

    # 预注册判决规则。数值可调，但**改之前先想清楚**：这套阈值是事先定好、
    # 不逐条协商的，否则每拒绝一个策略就换一次门槛，回测就变成了叙述。
    "research.referee.p_max": _s(float, "bootstrap p 值上限", 0, 1),
    "research.referee.pbo_max": _s(float, "PBO 上限", 0, 1),
    "research.referee.pbo_promote": _s(float, "PBO 达到此值只给观察不给晋级", 0, 1),
    "research.referee.dsr_min": _s(float, "DSR 下限", 0, 1),
    "research.referee.dsr_promote": _s(float, "DSR 达到此值才可晋级", 0, 1),
    "research.referee.stress_multipliers": _s(tuple, "成本压力测试倍数序列，第一个是主判决用的"),
    "research.referee.cpcv_p05_min": _s(float, "CPCV 5% 分位下限", -1, 1),
    "research.referee.cpcv_positive_min": _s(float, "CPCV 分块为正的最低比例", 0, 1),
    "research.referee.account_total_min": _s(float, "研究账户总收益下限", -1, 10),
}

# 跨字段不变式。单字段范围查不出"单票上限大于行业上限"这类错——
# 那种配置会让每一笔都过不了行业检查，而每条单字段校验都是合规的。
INVARIANTS: list[tuple[tuple[str, ...], Callable[[dict[str, float]], bool], str]] = [
    (("risk.max_industry", "risk.max_core_single"),
     lambda v: v["risk.max_industry"] > v["risk.max_core_single"],
     "risk.max_industry 必须大于 risk.max_core_single，否则单票上限永远先被行业检查挡住"),
    (("risk.drawdown_stop", "risk.drawdown_half"),
     lambda v: v["risk.drawdown_stop"] <= v["risk.drawdown_half"],
     "risk.drawdown_stop 必须不高于 drawdown_half（都是负值，越小越深）"),
    (("risk.daily_loss_reduce_only", "risk.daily_loss_no_new"),
     lambda v: v["risk.daily_loss_reduce_only"] <= v["risk.daily_loss_no_new"],
     "risk.daily_loss_reduce_only 必须比 daily_loss_no_new 更深"),
    (("regime.vol_stress", "regime.vol_calm"),
     lambda v: v["regime.vol_stress"] > v["regime.vol_calm"],
     "regime.vol_stress 必须大于 regime.vol_calm"),
    (("sentiment.tone_max_boards", "sentiment.tone_min_limit_up"),
     lambda v: v["sentiment.tone_max_boards"] >= 1 and v["sentiment.tone_min_limit_up"] >= 1,
     "tone 的门槛必须是正整数，否则「主升」永远不会出现"),
    (("quality.quarantine_ratio",),
     lambda v: 0 < v["quality.quarantine_ratio"] < 1,
     "quality.quarantine_ratio 必须在 (0,1) 之间，否则要么永不隔离要么全隔离"),
    (("research.shadow_gate", "research.referee.pbo_promote"),
     lambda v: v["research.shadow_gate"] >= v["research.referee.pbo_promote"],
     "影子晋级的选择风险不该比资本晋级更严：shadow_gate 必须不小于 pbo_promote"),
]

_LOCK = threading.Lock()
_VALUES: dict[str, Any] = {}
_SOURCE: dict[str, str] = {}
_LOADED = False


def _flatten(node: Any, prefix: str = "") -> dict[str, Any]:
    """Turn nested yaml into dotted keys. A list stays a value, not a subtree."""
    out: dict[str, Any] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                out.update(_flatten(value, name))
            else:
                out[name] = value
    return out


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    import yaml

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise BadParameter(f"{path} 不是合法 YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise BadParameter(f"{path} 顶层必须是映射")
    return raw


def _coerce(key: str, spec: Spec, value: Any, where: str) -> Any:
    if spec.kind is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}:
            return True
        if isinstance(value, str) and value.strip().lower() in {"0", "false", "no", "off"}:
            return False
        raise BadParameter(f"{where}: {key} 需要布尔值，实际是 {value!r}")

    if spec.kind is float:
        try:
            out = float(value)
        except (TypeError, ValueError) as exc:
            raise BadParameter(f"{where}: {key} 需要数字，实际是 {value!r}") from exc
        if not math.isfinite(out):
            raise BadParameter(f"{where}: {key} 必须是有限数，实际是 {value!r}")
        _range(key, spec, out, where)
        return out

    if spec.kind is int:
        if isinstance(value, bool):
            raise BadParameter(f"{where}: {key} 需要整数，得到布尔值")
        try:
            out = int(value)
        except (TypeError, ValueError) as exc:
            raise BadParameter(f"{where}: {key} 需要整数，实际是 {value!r}") from exc
        if isinstance(value, float) and float(value) != out:
            raise BadParameter(f"{where}: {key} 需要整数，实际是 {value!r}")
        _range(key, spec, out, where)
        return out

    if spec.kind is str:
        text = str(value).strip()
        if not text:
            raise BadParameter(f"{where}: {key} 不能为空")
        return text

    if spec.kind is tuple:
        if isinstance(value, str):
            return tuple(p.strip() for p in value.split(",") if p.strip())
        if not isinstance(value, (list, tuple)):
            raise BadParameter(f"{where}: {key} 需要列表，实际是 {value!r}")
        return tuple(value)

    raise BadParameter(f"{where}: {key} 的类型声明不认识: {spec.kind!r}")


def _range(key: str, spec: Spec, value: float, where: str) -> None:
    if spec.lo is not None and value < spec.lo:
        raise BadParameter(f"{where}: {key}={value} 小于下限 {spec.lo}")
    if spec.hi is not None and value > spec.hi:
        raise BadParameter(f"{where}: {key}={value} 大于上限 {spec.hi}")


def _env_overrides() -> dict[str, str]:
    prefix = ENV_PREFIX
    out: dict[str, str] = {}
    for name, raw in os.environ.items():
        if not name.startswith(prefix):
            continue
        out[name[len(prefix):].lower().replace("__", ".")] = raw
    return out


def load(force: bool = False) -> dict[str, Any]:
    """Read every config file once, apply env overrides, validate, cache."""
    global _LOADED
    with _LOCK:
        if _LOADED and not force:
            return _VALUES

        values: dict[str, Any] = {}
        source: dict[str, str] = {}
        for path, prefix in ((PARAMS_FILE, ""), (COSTS_FILE, "costs"), (LOCAL_FILE, "")):
            for key, raw in _flatten(_read(path), prefix).items():
                values[key] = raw
                source[key] = path.name

        for key, raw in _env_overrides().items():
            values[key] = raw
            source[key] = "env"

        unknown = sorted(k for k in values if k not in SPECS)
        if unknown:
            raise BadParameter(
                "这些键在配置里存在但代码没有声明（拼错了？还是改完代码忘了登记？）：\n  "
                + "\n  ".join(f"{k}  ({source[k]})" for k in unknown))

        resolved: dict[str, Any] = {}
        for key, spec in SPECS.items():
            if key not in values:
                continue
            resolved[key] = _coerce(key, spec, values[key], source[key])

        missing = sorted(k for k in SPECS if k not in resolved)
        if missing:
            raise MissingParameter(
                "这些参数没有配置值（本模块不做代码兜底，请在 configs/params.yaml 补上）：\n  "
                + "\n  ".join(f"{k}  — {SPECS[k].doc}" for k in missing))

        for keys, predicate, message in INVARIANTS:
            subset = {k: resolved[k] for k in keys}
            if not predicate(subset):
                raise BadParameter(message)

        _VALUES.clear()
        _VALUES.update(resolved)
        _SOURCE.clear()
        _SOURCE.update(source)
        _LOADED = True
        return _VALUES


def _get(key: str) -> Any:
    values = load()
    try:
        return values[key]
    except KeyError:
        if key not in SPECS:
            raise MissingParameter(f"{key} 没有在 settings.SPECS 里声明") from None
        raise MissingParameter(f"{key} 没有配置值") from None


def pick(value, key: str, reader):
    """An explicit argument if given, otherwise the configured value.

    Adapters take ``timeout=8.0``-style keyword defaults so tests can pin them.
    Hardcoding those literals meant a rate-limit incident required a code edit;
    this keeps the override and moves the default to config.
    """
    return reader(key) if value is None else value


def num(key: str) -> float:
    value = _get(key)
    return float(value)


def integer(key: str) -> int:
    return int(_get(key))


def flag(key: str) -> bool:
    return bool(_get(key))


def text(key: str) -> str:
    return str(_get(key))


def seq(key: str) -> tuple:
    value = _get(key)
    return tuple(value)


def decimal(key: str) -> Decimal:
    try:
        return Decimal(str(_get(key)))
    except InvalidOperation as exc:
        raise BadParameter(f"{key} 无法转成 Decimal") from exc


def initial_cash() -> Decimal:
    """开户资金，元。

    这个数曾经有四个来源：``Account`` 的 dataclass 默认值、``Ledger.fresh`` 的
    参数默认值、``PaperBook`` 的参数默认值，以及三处
    ``os.getenv("INITIAL_CASH", "5000")``——而 ``params.yaml`` 里的
    ``risk.initial_cash`` 谁都没读。账户规模是一个会变的数（换账户、加资金），
    所以它属于配置，不属于代码。

    那三处 ``INITIAL_CASH`` 后来只删掉了两处半：``build_desk`` 里仍留着
    ``initial_cash or os.getenv("INITIAL_CASH") or settings.initial_cash_text()``，
    而 ``.env`` 里就写着 ``INITIAL_CASH=5000``、``security.py`` 又在 import 时
    把整个 ``.env`` 灌进进程环境——所以 env 一直在悄悄压过配置，只是两个值
    恰好相同、没人发现。临时覆盖请用 ``QUANT__RISK__INITIAL_CASH=6000``：
    它走 :func:`load` 的覆盖链，会被 :func:`provenance` 记成 ``source="env"``，
    ``/health.config.overridden`` 里点得到名。``INITIAL_CASH`` 现在被忽略，
    并由 :func:`legacy_env` 报出来，不再静默。
    """
    return decimal("risk.initial_cash")


#: 曾经绕过配置、直接读进程环境的变量，以及替代它们的配置键。
#:
#: 不是"历史遗留清单"，而是**会被 /health 念出来的东西**：一个能压过
#: ``params.yaml`` 却不出现在 ``provenance()`` 里的来源，症状就是"改了配置没反应"。
#: 删除读取之后如果连提都不提，那只是把静默从"改了没用"换成"改了没人理"。
LEGACY_ENV: dict[str, str] = {
    "INITIAL_CASH": "risk.initial_cash",
    "SAMPLES_CASH": "research.samples_cash",
}


def legacy_env() -> dict[str, str]:
    """本进程里仍设着、但已经不再生效的旧变量 → 现在该改的配置键。"""
    return {name: key for name, key in LEGACY_ENV.items() if os.getenv(name)}


def initial_cash_text() -> str:
    """``initial_cash`` as the two-decimal string the ledger stores."""
    return f"{initial_cash():.2f}"


def provenance() -> dict[str, dict[str, Any]]:
    """Every parameter with its value, source file, and documentation."""
    values = load()
    return {key: {"value": values[key], "source": _SOURCE.get(key, "?"), "doc": spec.doc,
                  "unit": spec.unit}
            for key, spec in SPECS.items()}


def missing() -> list[str]:
    """Keys with no value — used by /health to say so before a trade does."""
    values = load()
    return sorted(k for k in SPECS if k not in values)


def reload() -> None:
    """Re-read from disk. Tests and config-edit tooling use this; the desk does not."""
    load(force=True)
    # Instrument memoises the fee schedule for the hot path; a reload that left
    # it behind would make "changed the commission in costs.yaml" a no-op for
    # anything constructed afterwards.
    from service.kernel import execution as kernel_execution
    from service.kernel import models as kernel_models

    kernel_models.reset_defaults()
    kernel_execution.reset_slippage_cache()


# 导入即校验：拼错一个键应当在进程启动时炸，而不是在第一次下单时。
load()
