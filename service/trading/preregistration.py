"""D-23: frozen pre-registration contract for intraday seasonality research.

Pure constants + one read-only predicate. **No I/O, no vendor calls, no orders,
no config writes.** This module is the single source of truth for the study's
thresholds; ``scripts/preregistration_status.py`` reads it, and the static test
pins its values so a later reader cannot quietly move the goalposts after
seeing the data.

Why a pre-registration exists at all: on 2026-11-07 a descriptive report was
produced from 2026-11-04/05/06 pool_tape data. Those three days are therefore
**contaminated** — any hypothesis chosen after seeing them carries data-snooping
bias. They are permanently excluded from the confirmatory sample, which starts
fresh on 2026-11-09.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Preregistration:
    version: str
    registered_at: str
    #: Seen during the D-12 descriptive audit. Permanently excluded.
    contaminated_days: tuple[str, ...]
    first_eligible_day: str
    min_confirmatory_sessions: int
    holdout_fraction: float
    min_frames_per_bucket: int
    min_bucket_n: int
    min_regimes: int
    metric: str
    primary_window: str
    reference_window: str
    null_hypothesis: str
    max_out_of_sample_decay: float
    alpha: float
    max_confirmatory_tests: int
    forbidden: tuple[str, ...]
    success_is_not: tuple[str, ...]


V1 = Preregistration(
    version="v1",
    registered_at="2026-11-07T21:50:00+08:00",
    contaminated_days=("20261104", "20261105", "20261106"),
    first_eligible_day="20261109",
    min_confirmatory_sessions=60,
    holdout_fraction=0.20,
    min_frames_per_bucket=20,
    min_bucket_n=30,
    min_regimes=3,
    metric="break_rate_proxy",
    primary_window="09:30-09:59",
    reference_window="10:30-13:59",
    null_hypothesis=(
        "早盘与盘中基准窗口的 break_rate_proxy 分布差异在新样本中不稳定："
        "样本外符号翻转，或幅度相对开发期衰减超过 50%。"
    ),
    max_out_of_sample_decay=0.50,
    alpha=0.05,
    max_confirmatory_tests=1,
    forbidden=(
        "自动修改 configs/params.yaml 或 configs/costs.yaml 的任何阈值",
        "把 U 型日内波动当作入场信号或自动阈值",
        "把 OBI_5=1.0（封板时卖档为 0）当作买盘强度或排序依据",
        "拿 snapshot_ts 做 rt_quote ↔ pool_tape 跨表连接键（D-9）",
        "用 break_rate_proxy 与日级 sentiment.break_rate_gate=0.38 机械比较",
        "把被污染的 20261104/05/06 三天计入确认性样本或样本外",
        "以净值曲线变好、p 值好看或参数搜索出的最优桶作为成功标准",
        "在达到 60 个确认性交易日之前读取 lockbox 的结果",
    ),
    success_is_not=(
        "更高的净值或更漂亮的资金曲线",
        "更小的 p 值（p 值不是效应稳定性）",
        "找到某个表现最好的分桶（那是探索，不是确认）",
    ),
)


def readiness(eligible_days: int, holdout_days: int, regimes: int,
              contract: Preregistration = V1) -> dict:
    """只读判定：今天**允不允许**跑确认性检验。不产生任何研究结论。

    ``eligible_days`` 必须是 ``first_eligible_day`` 之后、且不在污染名单里的
    交易日数；``holdout_days`` 是按时间顺序预留的尾部样本天数；``regimes``
    是已标注的情绪状态数。三者都由调用方（只读进度脚本）统计，本函数不读盘。
    """
    required_holdout = int(contract.min_confirmatory_sessions
                           * contract.holdout_fraction)
    checks = [
        {"name": "confirmatory_sessions", "actual": eligible_days,
         "required": contract.min_confirmatory_sessions,
         "ok": eligible_days >= contract.min_confirmatory_sessions},
        {"name": "locked_out_holdout_days", "actual": holdout_days,
         "required": required_holdout,
         "ok": holdout_days >= required_holdout},
        {"name": "regime_labels", "actual": regimes,
         "required": contract.min_regimes,
         "ok": regimes >= contract.min_regimes},
    ]
    return {
        "version": contract.version,
        "allowed_to_test": all(c["ok"] for c in checks),
        "checks": checks,
        "note": ("allowed_to_test 只表示样本门槛达标，不是研究结论，"
                 "也不构成任何改配置或激活意图层的授权。"),
    }
