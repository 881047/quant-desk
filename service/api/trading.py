"""Trading API — the one surface every host talks to.

The rule is visible in the schema: ``ProposeBody`` has no price, accepts either
``qty`` or ``target_weight``, and forbids extra fields. Approval requires a JSON
body whose only field is the literal ``confirm: true``. Invalid requests get a
422 before any business logic runs. Hosts (WorkBuddy over MCP, DSH over its
plugin loader) are thin clients; none of them carries a second copy of this contract.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Literal

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from service import settings
from service import vocabulary as vocab
from service.kernel.clock import SHANGHAI
from service.kernel.units import SHARES_PER_LOT
from service.trading import get_desk, get_samples_desk
from service.trading import playbook as pb
from service.trading.context_pack import build_context_pack
from service.trading.decisions import DecisionError
from service.trading.pool_cross import compare as cross_pool_sources
from service.trading.screen import ScreenConfig, affordable_price, screen

#: 盘中快照多旧就算"旧"：10 分钟。日内家数每分钟都在动，而抓到一次就复用一天，
#: 是 2026-11-02 那次 2.8 小时滞后读数的成因。
POOL_STALE_AFTER = 600
#: 超过 30 分钟，封单/成交、封板率这类**衍生比率**已经不是在描述"这一刻"。
POOL_DERIVED_AFTER = 1800

from .params import day_param
from .security import ROOT, require_token

REPORTS_DIR = ROOT / "reports"


def _with_units(body: dict, endpoint: str) -> dict:
    """数值端点的响应一律带顶层 ``units``：量纲只有 vocabulary 一处手写。

    前端 ``fmtByUnits()`` 只读这里，不再为每个字段记住"要不要 ×100"；
    插件的量纲说明也由同一张表生成。加端点时不要另写一份说明。
    """
    return {**body, "units": vocab.units_for(endpoint)}
#: 一份 review 实测 8,258 字 / 14.4 KB。宿主对超过 16 KB 的响应体是**整包丢弃**，
#: 不是截断——所以默认一页 6,000 字（约 10 KB），装不下就翻页，不硬塞。
REPORT_PAGE_CHARS = 6000
REPORT_PAGE_MAX = 12000

router = APIRouter(tags=["trading"], dependencies=[Depends(require_token)])


class ProposeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=6, max_length=12, description="600000.SH")
    side: Literal["BUY", "SELL"]
    sleeve: Literal["CORE", "AGGRESSIVE"] = "CORE"
    intent_type: str = Field(default="unspecified", max_length=60)
    thesis: str = Field(min_length=20, max_length=1200)
    evidence: list[str] = Field(min_length=1, max_length=8)
    invalidation: str = Field(min_length=5, max_length=500)
    client_request_id: str | None = Field(
        default=None,
        min_length=8,
        max_length=64,
        pattern=r"^[A-Za-z0-9._:-]+$",
        description=(
            "调用方生成的幂等句柄。**超时重试必须原样带上同一个值**："
            "同一个 id 永远返回同一张票（包括已被拒绝或已成交的那张），"
            "所以重试不会扣两次钱、也不会变成两张票。省略则退回按意图指纹去重，"
            "而指纹含 thesis/evidence 的自由文本——换个说法就是第二张票。"
        ),
    )
    target_weight: float | None = Field(default=None, gt=0, le=1)
    qty: int | None = Field(
        default=None,
        ge=1,
        le=1_000_000,
        description=(
            "股数。与 target_weight 二选一：一个说买多少股，一个说占多少仓位。"
            "买入时仍受合法手数、单票上限、单笔风险预算约束；"
            "卖出时表示减仓数量，省略即清仓。"
        ),
    )
    strategy: str = Field(default="llm", max_length=30)
    directed_by: Literal["agent", "user"] = Field(
        default="agent",
        description=(
            "谁下的这个决定：agent = 模型自己的判断；user = 用户明确指令。"
            "用户指令平掉的仓位照常记账，但**不计入 arena 的 edge 统计**——"
            "它测的是那条指令，不是判断力。"
        ),
    )
    persona: str | None = Field(
        default=None,
        max_length=30,
        description="必须等于当前活跃人格；换风格先 POST /persona/switch",
    )
    contingency: dict | None = Field(
        default=None,
        description=(
            "入场意图的条件单（意图层）。trigger.observe 或 trigger.enabled "
            "任一为 true 时允许提交；两者都关时提交会被 TRIGGER_DISABLED "
            "拒绝并写 proposal_blocked 留痕，绝不静默丢弃。只开 observe 时票"
            "冻结成 armed、逐帧求值写 reports/shadow 轨迹，二次 approve 也"
            "不会出钱；只有 enabled=true 才由盯盘循环兑现同一张票。手工二次 "
            "approve 一律被守卫拒绝（只返回 armed 并附 note），条件终态为 "
            "expired_untriggered / denied 或已越过 valid_until 的票不得复活。"
            "字段：kind(price|frame_time)、op(lte|gte|eq)、value、"
            "valid_from、valid_until、max_triggers=1、on_expiry=mark_only。"
            "带上条件后，首次 approve 只把票冻结成 armed（不出钱、不进撮合队列）；"
            "条件被捕获帧命中时，盯盘循环兑现的是**同一张票**。"
            "armed 票可用 reject 或 cancel 主动撤销，两入口都置 "
            "status=cancelled、不出钱、不动 ledger；条件越过 valid_until 后"
            "记 expired_untriggered 并释放指纹去重槽位，但票状态仍由跨日"
            " expired 裁决。"
        ),
    )


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: Literal[True]


class RejectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=3, max_length=500)


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = ""


class HandoffAnswerBody(BaseModel):
    """The reading, in the model's own words. Nothing else is accepted."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=4, max_length=40)
    analysis: str = Field(min_length=10, max_length=2000)
    # Only for the case where a previous attempt's delivery is unknown and the
    # operator has checked the mailbox. Sending twice by accident is worse than
    # a stalled handoff, so the default is false.
    force: bool = False


class JournalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4000)
    kind: str = Field(default="note", max_length=30)
    symbol: str | None = Field(default=None, max_length=12)
    tags: str = Field(default="", max_length=200)


class DeclinedBody(BaseModel):
    """「看过，没买」。没有价格字段——价格由结算自己读。"""

    model_config = ConfigDict(extra="forbid")
    symbols: list[str] = Field(min_length=1, max_length=30)
    reason: str = Field(min_length=1, max_length=1000)
    invalidation: str = Field(default="", max_length=1000)
    horizon_days: int = Field(default=3, ge=1, le=20)
    day: str | None = Field(default=None, pattern=r"^\d{8}$")


class ShortAlphaJudgmentItem(BaseModel):
    """One WorkBuddy judgment and optional target weight; never an order by itself."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=6, max_length=12)
    role: Literal["candidate", "watch"]
    thesis: str = Field(min_length=20, max_length=1200)
    evidence: list[str] = Field(min_length=1, max_length=8)
    invalidation: str = Field(min_length=5, max_length=500)
    target_weight: float | None = Field(
        default=None, gt=0, le=1,
        description="WorkBuddy 分配的模拟账户目标权重；空值表示只跟踪，旧记录不追溯补权重")


class ShortAlphaJudgmentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: str | None = Field(default=None, pattern=r"^\d{8}$")
    reviewed_evidence: list[str] = Field(min_length=1, max_length=12)
    items: list[ShortAlphaJudgmentItem] = Field(default_factory=list, max_length=5)
    no_candidate_reason: str = Field(default="", max_length=1200)
    replay: bool = False
    client_request_id: str | None = Field(
        default=None, min_length=8, max_length=64,
        pattern=r"^[A-Za-z0-9._:-]+$",
        description="Retry the same judgment with the same id; a different judgment is refused")


class ShortAlphaReviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=6, max_length=12)
    decision: Literal["hold", "exit_next_open", "exit_now"]
    reason: str = Field(min_length=20, max_length=1200)
    evidence: list[str] = Field(min_length=1, max_length=8)


class ShortAlphaReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: str = Field(pattern=r"^\d{8}$")
    items: list[ShortAlphaReviewItem] = Field(min_length=1, max_length=5)
    client_request_id: str = Field(
        min_length=8, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$",
        description="同一复评窗口固定请求号；同内容幂等，不同内容409")


class ControlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: bool


class PlaybookProposeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=50, max_length=20000)
    note: str = Field(default="", max_length=200)


class PlaybookActivateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft_id: str = Field(min_length=6, max_length=60)
    challenge: str = Field(
        min_length=20,
        max_length=200,
        description="one-time human-issued challenge bound to this draft's content hash",
    )


class HypothesisBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement: str = Field(min_length=10, max_length=500)
    rationale: str = Field(default="", max_length=2000)
    test_spec: str = Field(default="", max_length=2000)


class HypothesisResultBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["testing", "promoted", "rejected"]
    metrics: dict = Field(default_factory=dict)
    notes: str = Field(default="", max_length=2000)


class CoreTargetsBody(BaseModel):
    """A dry run of the caller's own allocation. Nothing is booked."""

    model_config = ConfigDict(extra="forbid")
    targets: dict[str, float] = Field(
        min_length=1,
        max_length=30,
        description="core ETF code -> target weight; the remainder stays in cash",
    )
    as_of: str | None = None


class CoreExecuteBody(BaseModel):
    """Executing still needs the caller's own weights and a stated reason.

    ``targets`` is required and has no default on purpose. The server used to
    compute the order list itself and accept a signature on it; now the
    allocation has to be the caller's, and the kernel only does the lot
    arithmetic and the legality checks.
    """

    model_config = ConfigDict(extra="forbid")
    targets: dict[str, float] = Field(
        min_length=1,
        max_length=30,
        description="core ETF code -> target weight; the remainder stays in cash",
    )
    thesis: str = Field(min_length=20, max_length=1200)
    evidence: list[str] = Field(min_length=1, max_length=8)
    invalidation: str = Field(min_length=5, max_length=500)


def _fail(exc: DecisionError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail={"code": exc.code, "message": str(exc)})


@router.get("/context_pack")
def context_pack() -> dict:
    return _with_units(build_context_pack(get_desk()), "/context_pack")


@router.get("/readiness")
def data_readiness(as_of: str | None = None) -> dict:
    """这段行情能不能下结论——而不是"服务活着吗"。

    `code` 取值 READY / SESSION_OPEN / AWAITING_PUBLISH / THIN_COVERAGE /
    NON_TRADING_DAY / NO_CALENDAR。只有 READY 允许写收盘判定；其他状态
    `blocks` 会列出被禁止的写入类别。想知道某一天为什么写不了报告，看这里。
    """
    as_of = _day(as_of, "as_of")
    from service.trading.readiness import check

    return check(get_desk().market, day=as_of).as_dict()


# One validator for every router; see `service/api/params.py`.
_day = day_param


@router.get("/facts")
def close_facts(as_of: str | None = None) -> dict:
    """当日可引用的全部数字，带稳定 key。

    报告里每个关于"今天"的数字都应当在这里能查到。`in_cap` / `one_word`
    / `buyable_at_close` 是三个不同的问题，不能互相替代——2026-10-29 的收盘报告
    把"我取过价的 6 只"当成了"可买 6 只"，而当日上限内实有 23 只。"开过板"另读
    `affordable.board_opens.opened`（东财池，分母不是 `in_cap`）。
    """
    from service.trading.facts import build_facts

    return _with_units(build_facts(get_desk(), _day(as_of, "as_of")), "/facts")


@router.post("/facts")
def persist_facts(as_of: str | None = None) -> dict:
    """把事实包落到 reports/facts/<day>.json。

    写收盘报告之前先调这个：没有事实包的报告会被 `scripts/verify_report.py`
    判为失败——不是因为它写错了，而是因为它的数字无法复现。
    """
    as_of = _day(as_of, "as_of")
    from service.trading.facts import write_facts

    desk = get_desk()
    day = _day(as_of, "as_of")
    latest = desk.market.latest_bar_date()
    if latest and day != latest:
        # 账户半边是"现在"的账本，倒填过去的日子会把今天的余额标成那天的
        # 权威事实。市场半边可以用 GET /facts 读，落盘只允许最新一天。
        raise HTTPException(status_code=409, detail={
            "code": "FACTS_PAST_DAY",
            "message": (f"{day} 的账户半边（equity / cash / positions）无法重建："
                        f"账本读到的是现在，最新日线是 {latest}。"
                        "过去的日期请只读市场半边（GET /facts）或已有的收盘快照。"),
        })
    path = write_facts(desk, day)
    return {"ok": True, "path": str(path), "day": path.stem}


@router.get("/market/frame")
def market_frame(
    day: str | None = None,
    movers: int = Query(default=5, ge=1, le=20),
    lookback: int = Query(default=15, ge=1, le=240),
    as_of: str | None = Query(
        default=None,
        description=(
            "历史回放：只载入不晚于该时刻的帧。ISO 8601 / HHMMSS / HH:MM:SS；"
            "秒级按该秒末比较（文件名微秒只用于同秒防撞名，不是可分辨时刻）。"
            "不传则读最新一帧，行为与改造前一致。"
        ),
    ),
    frame_id: str | None = Query(
        default=None,
        description=(
            "历史回放：按 HHMMSS_%f 精确取一帧（热目录 + rt_archive 冷存）。"
            "与 as_of 二选一；同时给出时以 frame_id 为准。"
            "冷存上线（2026-11-07）之前的帧不可回补。"
        ),
    ),
) -> dict:
    """此刻的全市场：最新一帧盘口里是什么，以及它是怎么走到这里的。

    和 `/facts` 是两个时刻。`/facts` 按 `latest_bar_date()` 算，盘中那是**昨天**；
    这里读 `data/rt_snapshots/<day>/` 的最后一帧，也就是抓取进程最近落下的那一批。

    三组必须一起读的字段：

    * `frame_age_seconds` / `session.in_session` / `limits.available` —— 这帧多旧、
      该不该有新帧、涨跌停算没算出来。缺任何一个，数字都可能被当成"实时"。
    * `delta` —— 拿 `lookback` 分钟前的帧相减。**单帧没有方向**：看到涨停 61 家，
      读不出是从 82 掉下来的还是从 40 涨上来的。
    * `alerts` —— 5 分钟一轮的监控进程今天报过什么，以及它最后跑于何时。
      这个空有两种意思（"看过，没事" / "没人在看"），`alerts.watcher` 分辨它们。
      它看的是**实盘**账户，不是这里。
    * `book_watch` —— 本模拟台的 paper 账本：抓取循环每落一帧就把持仓过一遍
      tripwire。`items` 是发生过的转变，`open` 是**现在仍然成立**的；只读 items
      会把上午已经解除的破线当成现在。

    这里没有建议、没有排序、没有 score。要不要动手是调用方的事。

    ``as_of`` / ``frame_id`` 开启**历史回放**：帧序列改读热目录 + 冷存，只载入
    不晚于目标时刻的帧，``frame_age_seconds`` / ``frame_is_stale`` 置空并标
    ``historical=true``（历史帧拿今天的时钟算 age 会报几十万秒并误判 stale）。
    ``account`` 仍是**当前**账本，不是历史账本，note 里会写明。
    """
    from service.notify import alert_log
    from service.trading import frame as live_frame

    stamp = _day(day)
    payload = live_frame.build(get_desk(), day=stamp, movers=movers,
                               lookback=lookback, as_of=as_of, frame_id=frame_id)
    # 省掉 `?day=` 时 build 会把它解析成今天，而 `_day(None)` 还是 `None`。日志文件名
    # 直接拼这个值，于是就变成 `None_alerts.jsonl`——读者永远看到"今天没跑过"。
    # 两个子系统各算一遍"今天是哪天"是这一段的原罪，以 build 的解为准。
    stamp = str(payload.get("day") or stamp or "")
    # 告警归在端点而不是 frame.build：frame 只回答"市场是什么"，监控日志属于另一个
    # 进程的产物，让它去猜那个目录在哪，等于给两个子系统造一个隐含耦合。
    #
    # 两份日志，两个账户。实盘那份来自 `scripts/monitor.py`（每 5 分钟一轮，会发邮件，
    # 持仓在 data/external.sqlite）；paper 那份来自抓取循环（每帧一轮，只落盘）。
    # 2026-11-05 实盘持 002185、模拟台空仓——不分开说，模型会把别人的止损读成自己的。
    historical = bool(payload.get("historical"))
    log_as_of = ((payload.get("as_of_cutoff") or payload.get("frame_at"))
                 if historical else None)
    payload["alerts"] = alert_log.read(stamp, limit=8, scope="external",
                                       as_of=log_as_of)
    payload["book_watch"] = alert_log.read(stamp, limit=8, scope="paper",
                                           as_of=log_as_of)
    if historical:
        payload["alerts_are_historical"] = True
        payload["book_watch_are_historical"] = True
        payload["pit_scope"] = {
            "market_frames": "frame_time <= as_of_cutoff",
            "alert_transitions": "event_ts <= as_of_cutoff",
            "account": "current_book_at_read_time",
            "heartbeat": "only when the surviving heartbeat predates the cutoff; otherwise unknown",
        }
    return payload


@router.get("/external")
def external_account(day: str | None = None) -> dict:
    """实盘账户：账外、只读、与模拟盘完全分离。

    这里没有任何下单能力，那是设计不是缺失。自 2026-08《证券公司交易信息系统
    接入管理规范》起，个人通过程序向券商柜台发单属于程序化交易，仅限机构；读取
    对账单是记账，不是交易。两条路的边界是第一行 `POST /order`，这里没有。

    数据来自 `scripts/external.py import`（券商导出的成交/持仓文件）或 `add`
    （手工记一笔）。成交流水不含分红送股/转股，所以 `reconcile` 用持仓快照对账，
    有差异时以快照为准。
    """
    import json

    from service.trading.external import ExternalStore, reconcile

    # ``day`` is concatenated into a path. Unvalidated, "../../configs/data"
    # resolved to a real file one directory up and its contents were returned to
    # the caller — the same hole `/facts` had, missed on this endpoint.
    day = _day(day)
    store = ExternalStore(ROOT / "data" / "external.sqlite")
    held = store.holdings()
    alert_path = ROOT / "data" / "external" / "ALERT.txt"
    report_path = ROOT / "reports" / "external" / f"{day or ''}.json"
    report = None
    if day and report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
    else:
        folder = ROOT / "reports" / "external"
        latest = max(folder.glob("*.json"), default=None) if folder.exists() else None
        if latest is not None:
            report = json.loads(latest.read_text(encoding="utf-8"))

    return {
        "ok": bool(held),
        "note": "只读同步：这里不产生任何委托",
        "positions": [
            {
                "symbol": symbol,
                "qty": position.qty,
                "cost": round(position.cost, 4),
                "avg_price": round(position.avg_price, 4),
                "realized": round(position.realized, 2),
                "fees": round(position.fees, 2),
            }
            for symbol, position in sorted(held.items())
        ],
        "snapshot": store.snapshot(day),
        "reconcile": reconcile(store, day),
        "report": report,
        "alert": (alert_path.read_text(encoding="utf-8").strip() if alert_path.exists() else None),
        "imports": store.imports(5),
    }


def refreshed_book(desk):
    """Settle the previous session, then hand back the book.

    Two routes read one account. `/portfolio` rolled the book forward and marked
    prices; `/account` did not, so the same balances could disagree — at first
    only in formatting (`'0'` vs `'0.00'`), later in value after an overnight
    gap. The preparation lives here so there is one of it: a route that forgets
    to call this is a route that reports yesterday.
    """
    date = desk.market.last_trading_day()
    desk.book.rollover(date)
    desk.book.mark_prices(date, live=True)
    return desk


@router.get("/portfolio")
def portfolio() -> dict:
    from service.trading.attribution import tag_position_summary

    desk = refreshed_book(get_desk())
    return _with_units(tag_position_summary(desk, desk.book.summary()), "/portfolio")


@router.get("/risk")
def risk() -> dict:
    desk = get_desk()
    # Same valuation view the pack and the dashboard use. Reading the live book
    # directly reported the last *written* marks (often yesterday's close) while
    # `/portfolio` had already rolled the day forward — one account, two
    # drawdowns. ``live=False`` keeps this endpoint off the network: risk
    # numbers are asked for on every wake-up and must not wait on a vendor.
    day = desk.market.last_trading_day()
    summary = (desk.book.read_view(day, live=False).summary()
               if day else desk.book.summary())
    return _with_units({
        "day": summary["day"],
        "equity": summary["equity"],
        "drawdown": summary["drawdown"],
        "daily_pnl_pct": summary["daily_pnl_pct"],
        "kill_switch": summary["kill_switch"],
        "safe_mode": summary["safe_mode"],
        "limits": desk.limits(),
        "positions": len(summary["positions"]),
    }, "/risk")


@router.get("/market/quote/health")
def quote_health() -> dict:
    """Which quote channels answered, and which are being skipped on purpose.

    A dead vendor does not raise here — it is skipped after three consecutive
    failures and retried after a cooldown. Without this endpoint the only symptom
    is "quotes are slow", which is indistinguishable from "the network is slow".
    """
    return get_desk().quote_health()


@router.get("/market/quote")
def quotes(
    symbols: str = Query(..., description="comma separated ts_codes"),
    live: bool = True,
    depth: bool = True,
) -> dict:
    """Latest prices. ``source`` tells you whether they are live or a stored bar.

    A quote carries ``age_seconds``; if the vendors are down the value falls back
    to the last daily close and ``is_realtime`` becomes false. A stale price
    labelled live would be worse than no price at all.
    """
    desk = get_desk()
    wanted = [s.strip().upper() for s in symbols.split(",") if s.strip()][:20]
    date = desk.market.last_trading_day()
    # `depth=True` 多一次腾讯请求，换五档盘口。涨停时 `depth.bid1_volume` 就是封单量——
    # 2026-11-02 判断打板可买性时，这一手数据当时是拿得到的，只是没有工具去取。
    live_map = desk.quotes(wanted, depth=depth) if live else {}
    out = []
    for symbol in wanted:
        quote = live_map.get(symbol)
        if quote is not None:
            out.append(quote.as_dict())
            continue
        snap = desk.market.snapshot(symbol, date)
        if snap is None:
            continue
        inst = desk.market.instrument(symbol, date)
        out.append(
            {
                "symbol": symbol,
                "name": inst.name,
                "last": str(snap.last),
                "pre_close": str(snap.pre_close),
                "open": str(snap.open),
                "high": str(snap.high),
                "low": str(snap.low),
                "up_limit": str(snap.up_limit),
                "down_limit": str(snap.down_limit),
                "pct_chg": round(float(snap.last / snap.pre_close - 1) * 100, 2)
                if snap.pre_close > 0
                else None,
                "volume": snap.volume,
                "amount": snap.amount,
                "quote_ts": snap.trade_date,
                "age_seconds": None,
                "timestamp_basis": "daily_bar",
                "source": "daily",
                "is_realtime": False,
                "suspended": snap.suspended,
            }
        )
    # P2-2：哪个源被熔断、还剩几个能用。2026-11-02 东财熔断 5 次、同花顺 code=3001，
    # 而响应里只有 price 没有"此刻只有 1 个源在答"这句话——降级要看得见。
    health = desk.quote_health()
    down = sorted(
        name for name, state in (health.get("breakers") or {}).items() if (state or {}).get("open")
    )
    available = [name for name in ("ths", "eastmoney", "tencent") if name not in down]
    degraded_note = (
        f"当前有源被熔断：{', '.join(down)}；只剩 {', '.join(available) or '无'}"
        " 在答，价格可能来自降级路径"
        if down
        else None
    )

    depth_note = None
    if any(quote.get("depth") for quote in out):
        depth_note = (
            "depth 是**这一刻**的五档盘口快照（腾讯源）：bid1_volume 单位是手，"
            "涨停且 limit_state=sealed 时它就是封单量；wb_ratio_pct = "
            "（五档买量 − 五档卖量）/（买量 + 卖量）× 100，只反映挂在盘上的委托，"
            "不含未挂出的意向，也不能当趋势——要趋势得看两次采样的差。"
            "limit_state: one_word 一字板 / sealed 现价封在涨停 / opened 碰过又开板 / none。"
        )
    # quote_ts 的时钟来源逐条标注：东财 f124 / 腾讯行情串是厂商行情时间，
    # THS 是响应时间，日线是交易日。盘后 age_seconds 统一为 null；即使取数
    # 发生在刚刚，也不能把当日收盘价说成"4 秒前成交"。
    return {
        "as_of": date,
        "session": health["session"],
        "quotes": out,
        "timestamp_note": (
            "quote_ts 的含义看每条上的 timestamp_basis："
            "vendor_quote_time = 东财 f124 / 腾讯行情串里的逐票行情时间；"
            "vendor_response_time = 同花顺本次响应时间；fetched = 只有取数时刻；"
            "daily_bar = 已存日线的交易日。盘后 age_seconds 一律为 null——"
            "那时的 last 是当日收盘价，说它多少秒前是错的。"
        ),
        "degraded": bool(down),
        "available_sources": available,
        "breakers": {name: (health.get("breakers") or {}).get(name) for name in down},
        **({"degraded_note": degraded_note} if degraded_note else {}),
        **({"depth_note": depth_note} if depth_note else {}),
    }


@router.get("/market/status")
def market_status() -> dict:
    """Session clock, which quote source is answering, and how stale it is."""
    desk = get_desk()
    from service.trading.live_fill import MAX_QUOTE_AGE

    return {
        "as_of": desk.market.latest_bar_date(),
        **desk.quote_health(),
        # 成交闸门实际执行的报价年龄上限。09:26 的前置检查要读它，不许再抄
        # 一个更大的数（曾写死 300 秒，而 approve 在 90 秒就 STALE_QUOTE）。
        "fill": {"max_quote_age_seconds": float(MAX_QUOTE_AGE)},
    }


@router.get("/market/bars")
def bars(symbol: str, days: int = Query(default=60, ge=5, le=250)) -> dict:
    desk = get_desk()
    # `last_trading_day()` answers "what does the calendar say"; between 15:00
    # and the 17:30 ingest it is *today* while the last stored bar is yesterday,
    # so the label ran a day ahead of the data. For a suspended or delisted name
    # the gap is not one day but months (002717.SZ: as_of 20261030, last bar
    # 20261012). `latest_bar_date()` is the date a decision can be priced at,
    # and `last_bar` now states the symbol's own edge so the gap is visible
    # instead of being averaged into `as_of`.
    date = desk.market.latest_bar_date() or desk.market.last_trading_day()
    hist = desk.market.history(symbol.upper(), date, days)
    if hist.empty:
        raise HTTPException(404, detail={"code": "NO_BARS", "message": symbol})
    cols = ["trade_date", "open", "high", "low", "close", "vol", "amount"]
    if "adj_close" in hist.columns:
        cols.append("adj_close")
    rows = hist[cols].to_dict("records")
    # `adj_close` is a cumulative product (close × each factor), so float64 leaves
    # sixteen digits of tail: 1243.8799999999997 where the price is 1243.88. A
    # reader that copies that number into a sentence is quoting a rounding error,
    # and any equality test on it is noise. Four decimals is finer than any quote
    # this market has (stocks 2, convertible bonds 3, ETFs 3–4).
    for row in rows:
        for key, value in row.items():
            if isinstance(value, float):
                row[key] = round(value, 4) if math.isfinite(value) else None
    return {
        "symbol": symbol.upper(),
        "as_of": date,
        "days": len(hist),
        "last_bar": str(hist["trade_date"].iloc[-1]),
        "bars": rows,
        # The two series answer different questions and both are needed: `close`
        # is what an order pays, `adj_close` is what a return is measured on.
        "note": (
            "close 是未复权成交价；adj_close 是前复权（末端等于 close），"
            "跨日比较收益用 adj_close。as_of 是数据日期；"
            "若 last_bar 早于 as_of，这只票在区间内没有成交（停牌/退市）。"
        ),
    }


@router.get("/market/sector")
def sector(day: str | None = None, limit: int = Query(default=31, ge=5, le=31)) -> dict:
    """申万一级行业榜：涨跌幅（市值加权 + 等权）、成交额、主力净流入。

    2026-11-02 的实盘审计里，"传媒 +6.69% / +29.99 亿"这类判断全部来自外部检索——
    内核没有行业榜。现在它从 `index_member`（申万分类）+ `daily`/`daily_basic`
    + `moneyflow` 自算，口径写在响应里，可复核。
    """
    from service.trading.sector import sectors

    out = sectors(get_desk(), _day(day), limit=limit)
    # 行业榜的 pct_chg 是百分数，而 /facts、/regime 的收益字段是 0..1 的小数，
    # 一个 ×100 的取舍就是 100 倍的错。与 /market/pools 对齐，把单位写进响应。
    out["units"] = vocab.units_for("/market/sector")
    return out


@router.get("/market/mins")
def mins(
    symbol: str,
    date: str | None = None,
    day: str | None = None,
    limit: int = Query(default=48, ge=8, le=240),
    live: bool = False,
) -> dict:
    """5-minute bars for one name on one session — the series behind `intraday`.

    全市场每天盘后入库（`scripts/intraday_sweep.py`），所以有覆盖的日子这是一次纯读；
    早于入库日、或北交所（baostock 无代码），会当场补一次并写明 `from`。

    ``date`` 与 ``day`` 是同一个参数的两个拼法，两个都收。**不能只收其中一个**：
    FastAPI 对未知查询参数是静默忽略的，`?day=20261103` 曾经因此不报错、直接返回
    上一交易日的数据——读的人拿到一条格式完整、日期不对的分时线，比拿不到更危险。
    两个都给且不一致时明确报错，不猜。

    ``limit`` 保留**末尾**几根：盘中读的人要的是最近的那一段，不是开盘那一段。
    ``live=true`` 只刷新当前交易日；历史日期仍按上面的纯读/补缺口语义处理。
    当日快照即便已入库也必须重新向单票分钟源取数，否则页面会把 11:30 的
    快照一直展示到收盘。响应的 ``is_realtime`` 只在本次确实抓到当日数据时为真。
    """
    from service.trading import intraday
    from service.trading.realtime import session_state
    from service.kernel.clock import SHANGHAI

    desk = get_desk()
    now = datetime.now(SHANGHAI)
    session = session_state(now, calendar=getattr(desk.market, "calendar", None))
    today = now.strftime("%Y%m%d")
    picked_date, picked_day = _day(date, "date"), _day(day, "day")
    if picked_date and picked_day and picked_date != picked_day:
        raise HTTPException(
            422,
            detail={
                "code": "AMBIGUOUS_DAY",
                "message": f"date={picked_date} 与 day={picked_day} 不一致，说清要哪一天",
            },
        )
    target = picked_date or picked_day or (
        today if live and session["trading_day"] else desk.market.latest_bar_date()
    )
    if not target:
        return {"ok": False, "symbol": symbol, "error": "没有可用的交易日"}
    refresh = bool(live and session["trading_day"] and target == today)
    frame, how = intraday.collect(desk.market, symbol.upper(), target, refresh=refresh)
    if refresh and (frame is None or frame.empty):
        # 盘前或源暂时没有当日数据时，仍展示最近一个真实交易日，不能留一张
        # 写着"实时"的空图。下一拍 live 请求会再次尝试当日。
        fallback = desk.market.latest_bar_date()
        if fallback and fallback != target:
            target = fallback
            refresh = False
            frame, how = intraday.collect(desk.market, symbol.upper(), target)
    is_realtime = bool(refresh and how.startswith("fetched:")
                       and frame is not None and not frame.empty)
    session_meta = {key: session[key] for key in ("label", "trading_day", "in_session")}
    if frame is None or frame.empty:
        return {
            "ok": False,
            "symbol": symbol.upper(),
            "date": target,
            "from": how,
            "is_realtime": False,
            "last_bar": None,
            "session": session_meta,
            "note": (
                "没抓到"
                if how.startswith(("failed", "unavailable"))
                else "这一天没有 5 分钟线；北交所在 baostock 里没有代码"
            ),
        }
    part = frame.tail(limit)
    # VWAP 只在成交额齐全时才有值（腾讯那批没有成交额）。手 → 股要乘 100。
    vwap = None
    if "amount" in frame.columns:
        amount = pd.to_numeric(frame["amount"], errors="coerce")
        vol = pd.to_numeric(frame["vol"], errors="coerce")
        if amount.notna().all() and float(vol.sum()) > 0 and float(amount.sum()) > 0:
            vwap = round(float(amount.sum()) / (float(vol.sum()) * 100), 2)
    return {
        "ok": True,
        "symbol": symbol.upper(),
        "name": desk.market.instrument(symbol.upper(), target).name,
        "date": target,
        "from": how,
        "is_realtime": is_realtime,
        "last_bar": str(frame["trade_time"].iloc[-1]),
        "session": session_meta,
        "count": len(frame),
        "returned": len(part),
        "source": (str(frame["source"].iloc[0]) if "source" in frame.columns else None),
        "vwap": vwap,
        "bars": [
            {
                "t": str(row["trade_time"])[11:16],
                "o": round(float(row["open"]), 2),
                "h": round(float(row["high"]), 2),
                "l": round(float(row["low"]), 2),
                "c": round(float(row["close"]), 2),
                "vol": None if pd.isna(row["vol"]) else round(float(row["vol"]), 0),
                "amt": None if pd.isna(row.get("amount")) else round(float(row["amount"]), 0),
            }
            for _, row in part.iterrows()
        ],
        "note": "5 分钟粒度；t 是 bar 的**结束**时间，vol 单位是手。",
    }


@router.get("/compare")
def compare_symbols(
    symbols: str,
    day: str | None = None,
    valuation_window: int = Query(default=750, ge=250, le=2500),
) -> dict:
    """Two or three names, side by side, on one basis.

    The point is not the table — it is that both columns come from the *same*
    `profile.build` call with the same window, so "his PE percentile is 0.46 and
    mine is 0.18" cannot be an artefact of two different windows.
    """
    from service.trading import compare

    picked = [part for part in (symbols or "").split(",") if part.strip()]
    out = compare.build(get_desk(), picked, _day(day, "day"), valuation_window=valuation_window)
    if not out.get("ok"):
        raise HTTPException(
            422, detail={"code": "BAD_COMPARE", **{k: v for k, v in out.items() if k != "ok"}}
        )
    return out


@router.get("/symbol/{symbol}/profile")
def symbol_profile(
    symbol: str,
    day: str | None = None,
    valuation_window: int = Query(default=750, ge=250, le=2500),
    sections: str | None = None,
) -> dict:
    """Everything the desk knows about one name, in one answer.

    Nine blocks — identity, tradability, valuation, fundamentals, flow,
    technicals, sentiment, events, position — each carrying the date it is true
    as of and an explicit ``null`` where an input is missing. No score, no
    verdict: see the module docstring.
    """
    from service.trading import profile

    # 逗号分隔的区块名；不传就是全部。分段是为了在 16 KB 上限内把每一块给全，
    # 而不是把所有块都切薄。
    picked = [name for name in (sections or "").split(",") if name.strip()] or None
    out = profile.build(
        get_desk(), symbol, _day(day, "day"), valuation_window=valuation_window, sections=picked
    )
    if not out.get("ok"):
        # 参数错不是"没有这只票"。404 会让读者去查代码，而问题在 sections 拼错了。
        if out.get("sections"):
            raise HTTPException(
                422,
                detail={
                    "code": "BAD_SECTION",
                    "message": out.get("error"),
                    "sections": out["sections"],
                },
            )
        raise HTTPException(404, detail={"code": "NO_SYMBOL", "message": out.get("error")})
    return out


def _loaded_before_close(loaded: str | None, now) -> bool:
    """Whether the partition was last written during the session, not after it.

    ``loaded`` is the max ``ingested_at`` — an ISO string with an offset, e.g.
    ``2026-10-30T14:45:02+08:00``. A missing or unparseable stamp returns True:
    the caller cannot prove the value is a close, and an unprovable close is
    exactly the claim this whole check exists to refuse.
    """
    if not loaded:
        return True
    try:
        stamp = datetime.fromisoformat(str(loaded))
    except (TypeError, ValueError):
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=now.tzinfo)
    return stamp < now.replace(hour=15, minute=0, second=0, microsecond=0)


def _age_seconds(loaded, now) -> int | None:
    """快照到现在的秒数。算不出来就是 None——不是 0。"""
    if not loaded:
        return None
    try:
        stamp = datetime.fromisoformat(str(loaded))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=now.tzinfo)
    return max(int((now - stamp).total_seconds()), 0)


def _pool_phase(date: str, *frames, now=None) -> tuple[str, dict]:
    """Classify today's pools as a partial snapshot, a live read, or the close.

    The three pools are filled progressively. On 2026-10-30 the 09:41 read held
    0 limit-ups and 1 limit-down; the close was 49 and 86. That is a loading
    artefact, and downstream it was written up as "tone=冰点, gates 三项全 false"
    — a bearish conclusion built on an empty table. Marking the window is the
    cheapest way to stop it: the numbers stay as measured, the label says what
    they are.
    """
    from service.kernel.clock import SHANGHAI

    now = now or datetime.now(SHANGHAI)
    today = now.strftime("%Y%m%d")
    minutes = now.hour * 60 + now.minute

    loaded: str | None = None
    for frame in frames:
        if frame is None or getattr(frame, "empty", True):
            continue
        if "ingested_at" not in frame.columns:
            continue
        values = frame["ingested_at"].dropna()
        if values.empty:
            continue
        latest = str(values.max())
        loaded = latest if loaded is None or latest > loaded else loaded

    if date != today:
        phase = "settled"
    elif minutes < 9 * 60 + 30:
        # Before the bell there is no session to describe. A same-day partition at
        # this hour is either absent or a leftover; calling it "intraday" would
        # invite a reader to treat yesterday's boards as this morning's.
        phase = "pre_open"
    elif minutes < 10 * 60:
        phase = "opening_window"
    elif minutes < 15 * 60:
        phase = "intraday"
    elif _loaded_before_close(loaded, now):
        # The clock says the session is over, but the partition is still the
        # 14:45 snapshot: the snapshot task stops before the close and the daily
        # job does not write until 17:30. Labelling this "settled" would put a
        # finality stamp on a value that is 20-165 minutes stale — worse than the
        # unlabelled payload this whole change set out to fix.
        phase = "stale_close"
    else:
        phase = "settled"

    state = {
        "phase": phase,
        "settled": phase == "settled",
        "loaded_at": loaded,
        "quoted_date": date,
        "now": now.isoformat(timespec="seconds"),
    }
    # 快照多旧，要说成一个数。2026-11-02 的那次：10:30 抓的一次快照，13:18 还在被
    # 引用算"封单/成交比"，中间 2.8 小时，而响应里只有两个时间戳，谁也没去减。
    age = _age_seconds(loaded, now)
    state["age_seconds"] = age
    # `stale` 永远是布尔值：缺键的语义是"没这个字段"，而这里要说的是"新/旧"。
    # `stale_close` 也算旧——收盘后读到 14:45 的快照，`age_seconds` 五千多秒而
    # `stale` 说 false，两个字段互相打脸（模拟时抓到）。它是"旧得有原因"，不是"不旧"。
    aging_phases = ("intraday", "opening_window", "stale_close")
    state["stale"] = bool(phase in aging_phases and age is not None and age > POOL_STALE_AFTER)
    if state["stale"]:
        # 超过 30 分钟，衍生比率（封单/成交、封板率）已经不是"这一刻"的读数。
        state["derived_from_stale"] = bool(age > POOL_DERIVED_AFTER)
    if phase == "pre_open":
        state["warning"] = (
            "开盘前。当日三池尚未产生，此刻读到的若是数值，那是上一交易日的残留；"
            "结构判断必须显式改成上一交易日（date=昨日）再读，别把昨日的板当今日的。"
        )
    elif phase == "opening_window":
        state["warning"] = (
            "开盘三十分钟内三池是逐步加载的实时快照，家数会显著低于收盘值"
            "（10-30 实测 09:41 涨停 0 → 收盘 49，跌停 1 → 86）。"
            "此窗口内 tone 返回「未成形」、gates 三项为 null，"
            "不得用 counts 判定冰点或 gate 失败；结构判断请用上一交易日的收盘终值。"
        )
    elif phase == "intraday":
        state["warning"] = (
            "盘中快照，家数仍在增长，会向收盘值收敛（10-30 12:30 涨停 36 → 收盘 49）。"
            "gates 可用，但引用封板率时要说明这是下限/上限而非终值。"
        )
    elif phase == "stale_close":
        state["warning"] = (
            f"已收盘，但这份分区仍是 {loaded} 的盘中快照：快照任务停在 14:45，"
            "日更 17:30 才写收盘终值（10-30 的 14:45 读数是涨停 47 / 跌停 62，"
            "日更后是 47 / 82）。此刻不要把 counts 当收盘数引用，"
            "要终值请等 reports/daily/<date>.dashboard.json。"
        )
    return phase, state


def _pool_age(desk, date: str) -> int | None:
    """今天的池子快照有多旧，秒。算不出来就是 None（调用方据此决定抓不抓）。"""
    try:
        frame = desk.data.read("limit_up_pool", date, date)
    except (KeyError, FileNotFoundError, ValueError):
        return None
    if frame is None or frame.empty or "ingested_at" not in frame.columns:
        return None
    stamps = frame["ingested_at"].dropna()
    if stamps.empty:
        return None
    return _age_seconds(str(stamps.max()), datetime.now(SHANGHAI))


#: 并发保护：TTL 判定与真正抓取之间有一段窗口，两个请求同时进来会各抓一次全市场，
#: 而一次就是 5,500 只。锁只负责"认领"，抓取在锁外跑——锁在抓取期间一直被占着，
#: 第二个请求就会陪着第一个一起等几十秒，把线程池也占住。
_REFRESH_LOCK = threading.Lock()

#: 两次刷新之间的最小间隔。它决定"上游刚失败过一次，多久之后才再试"，属于运维
#: 参数；而这个数同时决定故障期间调用方看到的那句话是什么（见 ``_RefreshState``）。
_REFRESH_MIN_GAP = settings.num("pools.refresh_min_gap")


@dataclass
class _RefreshState:
    """最近一次刷新：在跑的、成功的、失败的，分开记。

    合成一个 ``_LAST_REFRESH_AT`` 时，**失败也会写那个时刻**，于是上游挂掉之后
    ``refresh_min_gap`` 秒内每个请求都读到"另一请求 N 秒前刚抓过，直接复用"——
    故障被报成别人的成功，而调用方没有任何办法知道身后的池子还是旧的。失败照样
    要节流（上游没恢复时立刻重试只是再等一遍超时），但必须说它是失败。
    """

    in_flight_since: float | None = None
    last_ok_at: float | None = None
    last_failure_at: float | None = None
    last_error: str | None = None


_REFRESH_STATE = _RefreshState()


def _refresh_pools(desk) -> dict:
    """One market-wide snapshot, on demand. Slow, so only when asked and stale.

    The scheduled path is `scripts/snapshot_intraday.py --loop 60`; this exists
    for the moment a person needs it right now and cannot restart a task.

    Three answers, and they must stay distinguishable: ``attempted=True, ok=True``
    (抓到了), ``attempted=True, ok=False`` (抓了但上游失败), ``attempted=False``
    (没抓，`skipped` 说明为什么——别人刚抓过 / 上一次失败了 / 另一个正在抓).
    """
    from service.adapters.eastmoney_adapter import EastMoneyAdapter
    from service.trading.snapshot import snapshot as take_snapshot

    state = _REFRESH_STATE
    with _REFRESH_LOCK:
        now = time.monotonic()
        gap = _REFRESH_MIN_GAP
        if state.in_flight_since is not None:
            running = now - state.in_flight_since
            return {
                "attempted": False,
                "skipped": (f"另一个刷新已经跑了 {running:.0f} 秒还没结束，这次不重复抓全市场"),
                "in_flight_seconds": round(running, 1),
            }
        if state.last_ok_at is not None and now - state.last_ok_at < gap:
            return {
                "attempted": False,
                "skipped": f"另一请求 {now - state.last_ok_at:.0f} 秒前刚抓过，直接复用",
            }
        if state.last_failure_at is not None and now - state.last_failure_at < gap:
            waited = now - state.last_failure_at
            return {
                "attempted": False,
                "ok": False,
                "skipped": (
                    f"上一次刷新在 {waited:.0f} 秒前失败，{gap:.0f} 秒内不重试"
                    f"（上游没恢复时重试只是再等一遍超时）"
                ),
                "retry_after": round(gap - waited, 1),
                "last_error": state.last_error,
            }
        state.in_flight_since = now
        started = now

    ok = False
    try:
        basic = desk.data.read("stock_basic")
        codes = [str(code) for code in basic["ts_code"] if not str(code).endswith(".BJ")]
        # `with_tape=False`：这条路径只负责"把池子刷新一下"。封单轨迹由
        # 定时抓取周期独占，一次人工刷新插进来的帧会把两帧之间的间隔变得
        # 不可信，而"封单在两次观察之间变了多少"正是那张表唯一的信息。
        take_snapshot(desk.data, desk.registry, EastMoneyAdapter(), codes, with_tape=False)
        ok = True
        return {"attempted": True, "ok": True, "seconds": round(time.monotonic() - started, 1)}
    except Exception as exc:  # noqa: BLE001 - 上游不顺不该让这个 GET 失败
        message = f"{type(exc).__name__}: {exc}"
        with _REFRESH_LOCK:
            state.last_error = message
        logging.getLogger(__name__).warning("强制刷新池子失败：%s", message)
        return {
            "attempted": True,
            "ok": False,
            "error": message,
            "hint": "定时路径是 snapshot_intraday.py --loop 60；别用这个接口轮询",
        }
    finally:
        # `finally` 而不是两个分支各写一遍：BaseException（取消、超时打断）也会
        # 走到这里。漏掉它，`in_flight_since` 就永远留在那里，之后每一次刷新都会
        # 被自己上一次的"正在跑"挡住。
        with _REFRESH_LOCK:
            state.in_flight_since = None
            if ok:
                state.last_ok_at = time.monotonic()
            else:
                state.last_failure_at = time.monotonic()


def _pool_counts(breadth: dict | None, up_all, broken_all, down_all) -> dict:
    """家数，以及"为什么某个是空"。

    `limit_up_ex_bj` 来自派生池，需要当天的收盘日线——盘中拿不到，于是它是 None。
    一个 `null` 独立存在时读作"没有北交所"，而真实含义是"还没算"。空值要带上原因。
    """
    breadth = breadth or {}
    counts = {
        "limit_up": breadth.get("limit_up", len(up_all)),
        "limit_up_ex_bj": breadth.get("limit_up_ex_bj"),
        "broken_board": breadth.get("broken", len(broken_all)),
        "limit_down": breadth.get("limit_down", len(down_all)),
        "source": breadth.get("source", "eastmoney"),
        "eastmoney": {
            "limit_up": len(up_all),
            "broken_board": len(broken_all),
            "limit_down": len(down_all),
        },
    }
    if counts["limit_up_ex_bj"] is None:
        counts["limit_up_ex_bj_note"] = "派生池要收盘日线，盘中没有；收盘后可见，不是没有北交所"
    return counts


def _group_pool_by_industry(up_all, broken_all) -> list[dict]:
    """涨停池按行业分组：回答"医药有几只连板"，而不是让人逐只去数。

    2026-11-02 审计里 P2-4 的原话。分组只用东财的 `industry` 列——它是 104 个细桶，
    不是申万 31 个，两者不要混着报（要申万看 `quant_sector`）。
    """
    import pandas as pd

    def tally(frame, board_col: str | None) -> dict[str, dict]:
        out: dict[str, dict] = {}
        if frame is None or frame.empty or "industry" not in frame.columns:
            return out
        for industry, part in frame.groupby("industry"):
            name = str(industry or "未分类")
            boards = (
                pd.to_numeric(part.get(board_col), errors="coerce").max()
                if board_col and board_col in part.columns
                else None
            )
            out[name] = {
                "count": len(part),
                "max_boards": None if boards is None or pd.isna(boards) else int(boards),
                "symbols": [str(c) for c in part.get("code", [])][:8],
            }
        return out

    ups = tally(up_all, "boards")
    brokens = tally(broken_all, None)
    rows = []
    for name in sorted(set(ups) | set(brokens)):
        up = ups.get(name) or {}
        broken = brokens.get(name) or {}
        rows.append(
            {
                "industry": name,
                "limit_up": up.get("count", 0),
                "max_boards": up.get("max_boards"),
                "broken": broken.get("count", 0),
                "symbols": up.get("symbols") or broken.get("symbols") or [],
            }
        )
    rows.sort(key=lambda row: (-row["limit_up"], -(row["max_boards"] or 0)))
    return rows


def _pool_theme_sizes(desk, day: str) -> dict[str, dict]:
    """当日每只涨停股所属的最大题材簇：``{code: {"size": n, "tag": 名字}}``。

    东财池没有涨停原因，所以这一列只能挂同花顺的涨停表。清洗规则与
    ``board.NOISE_TAGS`` 共用一份：财报季标签（三季报增长/减亏/扭亏）与身份
    标签（ST/新股/次新/小盘）**不算题材共振**——2026-11-02 有 22 只挂着
    "三季报增长"、7 只挂着"三季报减亏"，那是所有人都在同一周发财报，不是
    22 笔资金抱一个逻辑。留着它，当天每一只带这个标签的票都会被读成"共振极强"。

    **标签名必须跟着数一起给。** 2026-11-02 的最大簇是"AI漫剧"（8 只，真实
    共振）与"「羊」字辈"（8 只，炒名字）。两个都是 8，含义完全不同——只回
    一个 ``theme_size=8`` 会让读者以为它们是一回事。
    """
    from service.trading.board import NOISE_TAGS

    try:
        frame = desk.data.read("ths_limit_up", day, day, columns=["ts_code", "reason"])
    except (KeyError, ValueError, OSError):
        return {}
    if frame is None or frame.empty or "reason" not in frame.columns:
        return {}
    exploded = frame.assign(tag=frame["reason"].astype(str).str.split("+")).explode("tag")
    exploded["tag"] = exploded["tag"].astype(str).str.strip()
    exploded = exploded[(exploded["tag"].str.len() > 0) & (~exploded["tag"].isin(NOISE_TAGS))]
    if exploded.empty:
        return {}
    size = exploded.groupby("tag").size()
    exploded["size"] = exploded["tag"].map(size)
    pick = exploded.loc[exploded.groupby("ts_code")["size"].idxmax()]
    return {
        str(row["ts_code"]).split(".")[0]: {"size": int(row["size"]), "tag": str(row["tag"])}
        for _, row in pick.iterrows()
    }


@router.get("/market/pools")
def pools(
    date: str | None = None,
    as_of: str | None = None,
    force_refresh: bool = False,
    group_by: Literal["none", "industry"] = "none",
    rank_by: Literal[
        "boards", "seal_ratio", "first_seal", "lot_cost", "theme_size", "none"
    ] = "boards",
    # 30 行 × 3 个池放不进 16KB 的宿主上限：加上 seal_ratio / seal_window
    # 之后，12 行正好 11.7KB。超过上限会被截断，而截断后的 JSON 读起来
    # 像数据缺失。要更多就显式传 limit。
    limit: int = Query(default=12, ge=1, le=100),
) -> dict:
    """The three pools from their source tables, plus the sentiment panel.

    It used to read ``limit_pool_t0`` — a derived table — and fall back to the
    limit-up pool alone. On 2026-10-26 the derived table had zero rows for the
    session, so the fallback returned 55 limit-ups, 0 broken boards and 0
    limit-downs; the sentiment panel then computed a break rate of 0.0 and called
    a mixed session "主升". Two faults in one path: a derived table that can go
    stale, and a fallback that silently drops two thirds of the answer.
    """
    desk = get_desk()
    # `as_of` is an alias kept for the callers that use that spelling everywhere
    # else (14 tools use as_of, 4 use date). Accepting both here means the MCP
    # layer's alias is defence in depth rather than the only copy.
    date = _day(date) or _day(as_of, "as_of") or desk.market.last_trading_day()
    # 刷新只在"今天、且快照已经旧了"时才做：一个 GET 顺手抓全市场报价，几分钟一次的
    # 调用就会把东财打到熔断（2026-11-02 当天就是这样断的）。所以显式传 true 才动，
    # 且失败不装作成功——`refresh.ok=false` 会跟着旧数据一起返回。
    #
    # 曾经试过"盘中陈旧即抓"，让 GET 自愈。被全量测试否掉了，而且否得对：
    # 那会让一个 GET 产生写副作用（抓 5,500 只并落库），任何人轮询这个接口
    # 都在替你打上游。新鲜度是**数据管道**的职责（scripts/snapshot_intraday.py
    # --loop 60），不是读接口的。读接口只负责把"多旧"如实报出来。
    today = datetime.now(SHANGHAI).strftime("%Y%m%d")
    refresh = {"attempted": False}
    if date != today:
        # 历史某天的池子已经是终值，重抓是白花一次请求。
        refresh = {"attempted": False, "skipped": "不是今天，历史池子已是终值"}
    elif force_refresh:
        stale = _pool_age(desk, date)
        if stale is not None and stale <= POOL_STALE_AFTER:
            refresh = {
                "attempted": False,
                "skipped": f"快照只有 {stale} 秒，还新鲜（阈值 {POOL_STALE_AFTER} 秒）",
            }
        else:
            refresh = _refresh_pools(desk)
    up_all = desk.data.read("limit_up_pool", date, date)
    broken_all = desk.data.read("broken_board_pool", date, date)
    down_all = desk.data.read("limit_down_pool", date, date)
    if up_all.empty and broken_all.empty and down_all.empty:
        # Empty is not "no limit-ups today", it is "the pools were never fetched".
        # On 2026-10-26 this branch returned a payload with no `ok` and no error,
        # and the review went on to quote pool counts from another vendor without
        # saying so. An empty object that looks like data is worse than an error.
        return {
            "ok": False,
            "as_of": date,
            "source": None,
            "error": (
                f"{date} 没有东财三池数据。先跑 "
                f"`python scripts/backfill.py --datasets panel --start {date} "
                f"--end {date}`；在补齐之前，不要拿别处的涨停家数当作这一天的池子。"
            ),
            "alternative": (
                "quant_knowledge(topic=limit_structure) 走同花顺，"
                "同一天家数与东财不同（10/26 是 45 vs 55），引用时必须标明来源。"
            ),
            "pools": {},
            "sentiment": None,
            # 三态，不是两态：`ok=False` 是这份说明书里"今天没有短线"的说法，
            # 不能让"管道没跑"也用同一句话。全空时 ok=None 且 no_panel 落在
            # indeterminate 而不是 failed——10-30 那天的"冰点"就是这么误判的。
            "gates": {
                "ok": None,
                "checks": {},
                "failed": [],
                "indeterminate": ["no_panel"],
                "phase": None,
                "note": "三池没入库，闸门无法判断；这不是'闸门不过'",
            },
            # 家数写 null 而不是 0：0 是"今天没有涨停"，null 是"没取到"。
            "counts": {"limit_up": None, "broken_board": None, "limit_down": None},
            # 缺数据和数据旧是两个不同的问题，但读的人要先能按同一组键读。
            "stale": True,
            "age_seconds": None,
            "derived_from_stale": False,
            "refresh": refresh,
            "pool_state": {
                "phase": "missing",
                "settled": False,
                "loaded_at": None,
                "quoted_date": date,
                "missing": True,
            },
        }

    from service.trading.sentiment import gates as sentiment_gates
    from service.trading.sentiment import panel as build_panel

    phase, pool_state = _pool_phase(date, up_all, broken_all, down_all)
    # 家数与连板高度改用派生池：东财和同花顺都会漏名字（38 天里东财平均每天少
    # 2.1 只、同花顺少 1.6 只），而派生池的定义是 close == up_limit，能逐只核对。
    # 池子内容（封单、首封时间、行业）仍然只能用东财的。
    from service.trading.tape import breadth as pool_breadth

    day_breadth = pool_breadth(desk.data, date)
    sentiment = build_panel(
        {"limit_up_pool": up_all, "broken_board_pool": broken_all, "limit_down_pool": down_all},
        phase=phase,
        breadth=day_breadth,
    )

    wanted = (
        "code",
        "name",
        "close",
        "pct_chg",
        "boards",
        "limit_price",
        "first_seal_time",
        "seal_amount",
        "industry",
        "consecutive_down",
        "open_times",
        "amount",
    )

    equity = float(desk.book.summary().get("equity") or 0)
    # 一次算完，三个池子共用：``enrich`` 会对每个池各调一次，逐次重算会读三遍
    # 同一张日分区。
    theme_sizes = _pool_theme_sizes(desk, date)
    # 题材簇来自同花顺的涨停原因，而它在 15:20 的 ths-close 任务里才落库。
    # 14:35 做尾盘决策时当日题材一定还没有——此时 ``theme_size`` 是 null，
    # 而 null 读起来像"没有题材"，实际是"今天的题材还没到"。把这件事写在
    # payload 里，否则模型会拿缺失当结论。
    theme_basis = {
        "source": "ths_limit_up:reason（15:20 后落库）",
        "covered": len(theme_sizes),
        "note": (
            "盘中 15:20 之前 covered 为 0：theme_size=null 是「今日题材未入库」，"
            "不是「没有题材共振」。这个限制对 quant_knowledge(topic=board) 的 "
            "theme 块同样成立——两者读的是同一张表。"
        ),
    }

    def enrich(frame: pd.DataFrame) -> pd.DataFrame:
        """给池子补三个基础事实：封单强度、首封时段、题材簇大小。

        封单额排序是**市值**排序，不是强度排序。2026 年的 198 个交易日里，
        绝对额的前五与 ``封单/流通市值`` 的前五只有 34% 重合、从来没有完全一致：
        1.15 亿压在 246 亿流通盘上是当天最弱的一档，6.10 亿压在 54.6 亿上是最强
        的一档。两个数都给，排序交给调用方选。

        ``float_mv`` 的单位是元（东财口径），``seal_amount`` 也是元，所以比值
        不需要换算。``ths_limit_up`` 的 ``seal_money / circ_mv`` 里 ``circ_mv``
        是万元、要乘 10,000——两处的换算不一样，因为来源不一样。
        """
        from service.trading.board import seal_window, split_seal_window

        out = frame.copy()
        if "seal_amount" in out.columns and "float_mv" in out.columns:
            seal = pd.to_numeric(out["seal_amount"], errors="coerce")
            cap = pd.to_numeric(out["float_mv"], errors="coerce")
            # 精度与 `records()` 的输出取整保持一致（4 位），否则 `_pct` 字段
            # 用的是 6 位原值、比例字段是 4 位，两者乘 100 对不上——那等于
            # 又造了一个"看着该相等其实不等"的坑。
            out["seal_ratio"] = (seal / cap.where(cap > 0)).round(4)
            # 同一行里 `seal_ratio` 是比例、`pct_of_equity` 是百分数，早先没有任何
            # 元信息区分，前端只能"每个字段记住要不要 ×100"，实测显示过 1000% /
            # 6780%（docs/14 A1）。规范做法是百分数字段一律 `_pct` 后缀：这里补一个
            # 对齐命名的字段，旧字段原值保留，单位统一写进顶层 `units`。
            out["seal_ratio_pct"] = (out["seal_ratio"] * 100).round(3)
        if "first_seal_time" in out.columns:
            out["seal_window"] = out["first_seal_time"].map(seal_window)
            parts = out["seal_window"].map(split_seal_window)
            out["seal_window_no"] = parts.map(lambda pair: pair[0])
            out["seal_window_text"] = parts.map(lambda pair: pair[1])
        if theme_sizes and "code" in out.columns:
            # 东财池没有涨停原因，这一列只能挂同花顺。匹配不上的行留 None，
            # 不用 1 顶替——"没有题材标签"和"独苗"是两件事。
            code = out["code"].astype(str)
            out["theme_size"] = code.map(lambda c: (theme_sizes.get(c) or {}).get("size"))
            out["theme_tag"] = code.map(lambda c: (theme_sizes.get(c) or {}).get("tag"))
        return out

    def records(
        frame, rows: int, sort_by: str | None = None, *, affordability: bool = False
    ) -> list[dict]:
        if frame.empty:
            return []
        frame = enrich(frame)
        if sort_by == "first_seal" and "first_seal_time" in frame.columns:
            # 早封比晚封强，所以这一列是升序——按字符串排会得到"09:31 < 14:00"
            # 的偶然正确，但 "9:31" 与 "09:31" 混在一起时就错了。用分钟数排。
            minute = frame["first_seal_time"].map(
                lambda s: (
                    (int(str(s).split(":")[0]) * 60 + int(str(s).split(":")[1]))
                    if ":" in str(s)
                    else None
                )
            )
            frame = frame.assign(_seal_minute=minute).sort_values("_seal_minute")
        elif sort_by == "lot_cost" and "close" in frame.columns:
            price = pd.to_numeric(frame["close"], errors="coerce")
            frame = frame.assign(_lot=price * SHARES_PER_LOT).sort_values("_lot")
        elif sort_by and sort_by in frame.columns:
            frame = frame.sort_values(sort_by, ascending=False)
        cols = [c for c in wanted if c in frame.columns]
        cols = cols + [
            c
            for c in ("seal_ratio", "seal_ratio_pct", "seal_window",
                      "seal_window_no", "seal_window_text", "theme_size", "theme_tag")
            if c in frame.columns
        ]
        out = frame.head(rows)[cols].to_dict("records")
        # 东财这一列是 float32：+10% 到手是 9.983452796936035，七个数字的存储
        # 误差挂在一个用来判断「是不是封在涨停价上」的数上。两位小数才是交易所
        # 报价的精度，再细的都是 float32 的尾数。其余浮点给四位：钱够用，也挡住
        # 同一种误差从别的列漏出来。
        for row in out:
            for key, value in list(row.items()):
                if not isinstance(value, float):
                    continue
                if math.isnan(value) or math.isinf(value):
                    # NaN/inf 在 JSON 里不是合法字面量；有的解析器当 null，有的
                    # 直接报错。缺值就写 null，让「没有」看起来就是「没有」。
                    row[key] = None
                else:
                    row[key] = round(value, 2 if key == "pct_chg" else 4)
        if affordability and equity > 0:
            # A 股整手，一手 100 股。短线最先要回答的不是"这票强不强"，是"我这点
            # 钱参不参与得了"：21.93 元的青岛食品一手 2,193 元，占 5,007 元账户的
            # 43.8%——一手就已经超过单票上限，再看好也只能是一笔。这两个数不替谁
            # 做决定，只是把"整手"这道硬门槛摆在票旁边，省掉读者自己乘 100。
            for row in out:
                price = row.get("close")
                if not isinstance(price, (int, float)) or price <= 0:
                    row["lot_cost_cny"] = None
                    row["pct_of_equity"] = None
                    row["exceeds_core_cap"] = None
                    continue
                lot = round(float(price) * SHARES_PER_LOT, 2)
                row["lot_cost_cny"] = lot
                row["pct_of_equity"] = round(lot / equity * 100, 1)
                # 一手就顶破单票上限的票，在这个账户里根本不是"强不强"的问题，
                # 是"买不进"。2026-10-31 与 11-02 各有一次提案卡在这里（001219
                # 一手 2,193 元 = 43.8% > 30%），模型拿到的是"最多 0 股"，从
                # 那句话里看不出根因是票价。把结论放进行内，省掉一次注定被拒
                # 的往返——漏斗里 7 次拦截有 2 次是它。
                row["exceeds_core_cap"] = (lot / equity) > settings.num("risk.max_core_single")
        return out

    grouped = None
    if group_by == "industry":
        grouped = _group_pool_by_industry(up_all, broken_all)
    return {
        "as_of": date,
        "source": "eastmoney",
        # 一份响应里混着比例与百分数，光靠字段名记不住（`pct_of_equity` 是
        # 历史命名，不是 `_pct` 后缀）。机器读的单位声明放在顶层，别让下一个
        # 消费者再去猜要不要 ×100。
        "units": vocab.units_for("/market/pools"),
        "units_note": vocab.note("pools"),
        **({"by_industry": grouped} if grouped is not None else {}),
        "pool_state": pool_state,
        # 顶层再写一遍，是因为读的人先看这里：`stale` 要让"这份快照不是现在的"这句
        # 话出现在第一屏，而不是埋在 pool_state 里让人自己去减时间戳。
        "stale": bool(pool_state.get("stale")),
        "age_seconds": pool_state.get("age_seconds"),
        "derived_from_stale": bool(pool_state.get("derived_from_stale")),
        "refresh": refresh,
        "sentiment": sentiment,
        "gates": sentiment_gates(sentiment),
        "cross_check": cross_pool_sources(
            desk,
            date,
            {"limit_up": len(up_all), "broken": len(broken_all), "limit_down": len(down_all)},
            source="eastmoney",
            phase=phase,
        ),
        "pools": {
            "limit_up": records(
                up_all, limit, None if rank_by == "none" else rank_by, affordability=True
            ),
            "broken_board": records(broken_all, limit),
            "limit_down": records(down_all, limit, "consecutive_down"),
        },
        "rank_by": rank_by,
        "theme_basis": theme_basis,
        "counts": _pool_counts(day_breadth, up_all, broken_all, down_all),
        "note": (
            "counts 的家数来自派生池（close == up_limit，逐只可核对），不是东财的"
            "行数：三家都会漏名字，东财 19 天里平均每天少 2.1 只。counts.eastmoney "
            "是这家自己的数，差多少可以自己比。只在 pool_state.settled=true 时"
            "等于收盘终值；盘中（尤其开盘 30 分钟内）它反映的是抓取那一刻已经封板"
            "的家数。limit_up_ex_bj 剔掉 30% 涨跌幅的北交所，算不算它是口径选择。"
            "pools 列表按 limit 截断，且仍来自东财。涨停池每行另有 lot_cost_cny"
            "（一手 100 股按收盘价的金额）与 pct_of_equity（占当前净值比例）——"
            "整手是 A 股最硬的一道门槛：一手就超过单票上限的票，再看好也只是一笔。"
            "涨停池每行还有 seal_ratio（封单额 ÷ 流通市值）与 seal_window"
            "（首封时段）。rank_by 可选 boards / seal_ratio / first_seal / "
            "lot_cost / none；默认 boards 与改动前一致。封单额排序其实是市值排序："
            "绝对额前五与强度前五在 198 个交易日里只有 34% 重合。逐票的同结构历史"
            "分布不在这里，走 quant_knowledge(topic=board, symbol=...)。"
            "每个数值字段的量纲见顶层 units；首封时段的编号与文字已拆成 "
            "seal_window_no / seal_window_text，不必再从字符串里剥前缀。"
        ),
    }


@router.get("/research/screen")
def research_screen(
    limit: int | None = Query(default=None, ge=1, le=20),
    max_price: float | None = Query(default=None, gt=1, lt=1000),
) -> dict:
    """Screening rules, with the configured thresholds unless the caller narrows them.

    The defaults used to be literals in this signature (``8`` and ``45.0``) while
    ``screen.max_candidates`` and ``screen.max_price`` held the same numbers: the
    yaml could be edited, the process restarted, and this endpoint still screened
    on the old thresholds — a second home for a number that already had one.

    The affordability cap was spelled out here as ``equity * 0.30 / 100`` — the
    0.30 is ``screen.affordable_weight`` and the 100 is one lot. That rule already
    exists as :func:`affordable_price`, so the endpoint calls it instead of
    carrying a copy that would not follow a config change.
    """
    desk = get_desk()
    # The date with bars, not the date on the calendar: during a session today's
    # bar does not exist yet, and screening it returns an empty list that reads
    # as "no candidates today" rather than "today's data is not published".
    date = desk.market.latest_bar_date() or desk.market.last_trading_day()
    equity = float(desk.book.equity())
    cfg = ScreenConfig()
    if limit is not None:
        cfg = replace(cfg, max_candidates=limit)
    ceiling = cfg.max_price if max_price is None else max_price
    # A caller may tighten the ceiling; it may not raise it past what one lot of
    # this account can afford, or the screen would return names it cannot buy.
    cfg = replace(cfg, max_price=min(ceiling, affordable_price(equity)))
    w = cfg.rank_weight_momentum
    return _with_units({
        "as_of": date,
        "max_price": round(cfg.max_price, 2),
        "rank_key_weights": {"momentum": round(w, 3), "adv": round(1.0 - w, 3)},
        "candidates": screen(desk.market, date, cfg),
        # 权重从配置读，不在这句话里再写一遍——两处各写一个数，改一处就打架。
        "note": (
            f"规则候选，按 {w:.2f}×20日动量分位 + {1.0 - w:.2f}×流动性分位排序，"
            f"取前 {cfg.max_candidates} 只。rank_key 的作用是「哪 N 只进候选」，"
            "不是质量评分——mom20_pct / adv20_pct 是原始分位，可以按任一维度"
            "自己重排，也可以不同意这组权重（它来自 "
            "configs/params.yaml 的 screen.rank_weight_momentum）。不是推荐。"
        ),
    }, "/research/screen")


@router.post("/decision/propose")
def propose(body: ProposeBody) -> dict:
    desk = get_desk()
    try:
        return desk.decisions.propose(body.model_dump(), actor="agent")
    except DecisionError as exc:
        # A blocked proposal is a decision that happened (the model tried, the
        # desk said no) and it must leave a row. Without this the audit trail
        # only contains successful tickets, so "the model tried 200 times and was
        # refused" is indistinguishable from "the model did nothing".
        desk.paper.log_decision(
            "proposal_blocked",
            {"code": exc.code, "message": str(exc), "request": body.model_dump()},
            actor="guard",
        )
        raise _fail(exc) from exc


@router.post("/decision/{ticket_id}/approve")
def approve(ticket_id: str, body: ApproveBody) -> dict:
    desk = get_desk()
    try:
        return desk.decisions.approve(ticket_id, actor="agent")
    except DecisionError as exc:
        raise _fail(exc) from exc


@router.post("/decision/{ticket_id}/reject")
def reject(ticket_id: str, body: RejectBody) -> dict:
    """Close a proposal; on an ``armed`` intent this withdraws it instead.

    An armed intent has already been approved but has no order yet, so ``reject``
    reports ``status="cancelled"`` with ``rejected=False`` rather than
    ``"rejected"``. Ordinary ``proposed`` tickets still become ``rejected``.
    """
    desk = get_desk()
    try:
        return desk.decisions.reject(ticket_id, body.reason, actor="agent")
    except DecisionError as exc:
        raise _fail(exc) from exc


@router.post("/decision/{ticket_id}/cancel")
def cancel(ticket_id: str, body: CancelBody) -> dict:
    """Withdraw a queued order, or an armed intent that has not been triggered yet.

    ``reject`` closes a proposal; on an ``armed`` intent **both** routes withdraw
    it to ``status="cancelled"`` (no order exists, nothing is released). A
    ``ceiling`` order cannot be withdrawn in the 14:57–15:00 closing auction;
    an already-filled ticket reports ``status="filled"``, not 409.
    """
    desk = get_desk()
    try:
        return desk.decisions.cancel(ticket_id, actor="agent", reason=body.reason)
    except DecisionError as exc:
        raise _fail(exc) from exc


# ---- 研究样本账本 -----------------------------------------------------------
# 独立本金、独立 sqlite、独立风控：1 只持仓、满仓、每天买一卖一。
# 与主账户完全隔离，主账户的净值、arena 判决与 cases 历史都不受影响——
# 把一天的采样混进自由裁量账本，两个都读不懂了。
# 这里的产物是「带 thesis 与 invalidation 的结算记录」，目的是把证伪周期
# 从年压到月。它花的是研究成本，不是业绩。
SAMPLE_TARGET = settings.integer("api.sample_target")


@router.get("/samples")
def samples_status(limit: int = Query(default=10, ge=1, le=50)) -> dict:
    desk = get_samples_desk()
    # Same two steps `/portfolio` does, for the same two reasons. Without the
    # rollover the T+1 unlock only happens the next time a ticket is proposed,
    # so a caller checking whether yesterday's sample can be sold reads
    # available_qty=0 all morning. Without the mark, market_value and
    # unrealized_pnl sit at the entry price — on 2026-10-30 this endpoint
    # reported 13.88 for a stock that closed at 13.84.
    date = desk.market.last_trading_day()
    desk.book.rollover(date)
    desk.book.mark_prices(date, live=True)
    cases = desk.paper.list_cases(limit=1000)
    account = desk.book.summary()
    # 权重是一个比率：0.695131567712545 读起来像"有精度可用"，实际每个消费者
    # 都会先乘 100 再取两位。
    for position in account.get("positions") or []:
        if isinstance(position.get("weight"), float):
            position["weight"] = round(position["weight"], 4)
    return {
        "as_of": date,
        "account": account,
        "closed_count": len(cases),
        "target": SAMPLE_TARGET,
        "remaining": max(0, SAMPLE_TARGET - len(cases)),
        "recent_cases": cases[:limit],
        "pending": [
            t for t in desk.paper.list_tickets(limit=20) if str(t.get("status")) == "queued"
        ],
        "limits": desk.limits(),
        "note": "独立账本，不并入主账户净值；KPI 是结算笔数，不是收益率。",
    }


@router.post("/samples/propose")
def samples_propose(body: ProposeBody) -> dict:
    desk = get_samples_desk()
    payload = body.model_dump()
    # 策略是固定的：这里不是模型挑的一条 alpha，是这个采样装置本身。
    # 传 llm 会被 60% 的策略上限挡住——2,000 元买一手就占七成。
    payload["strategy"] = "sample_1d"
    try:
        return desk.decisions.propose(payload, actor="agent")
    except DecisionError as exc:
        desk.paper.log_decision(
            "proposal_blocked",
            {"code": exc.code, "message": str(exc), "request": payload},
            actor="guard",
        )
        raise _fail(exc) from exc


@router.post("/samples/{ticket_id}/approve")
def samples_approve(ticket_id: str, body: ApproveBody) -> dict:
    desk = get_samples_desk()
    try:
        return desk.decisions.approve(ticket_id, actor="agent")
    except DecisionError as exc:
        raise _fail(exc) from exc


@router.post("/samples/{ticket_id}/reject")
def samples_reject(ticket_id: str, body: RejectBody) -> dict:
    desk = get_samples_desk()
    try:
        return desk.decisions.reject(ticket_id, body.reason, actor="agent")
    except DecisionError as exc:
        raise _fail(exc) from exc


@router.get("/tickets")
def tickets(
    limit: int = Query(default=20, ge=1, le=100),
    status: Literal["open", "proposed", "armed", "queued", "filled", "rejected",
                    "cancelled", "expired"]
    | None = None,
    view: Literal["brief", "full"] = "brief",
) -> dict:
    """List tickets, newest first. ``status=open`` means "not terminal yet".

    ``view=brief`` (default) drops the guard evidence and the market snapshot.
    That is not cosmetic: a full ticket is 2.8–3.8 KB, so twenty of them is
    56 KB — past the tool-output ceiling, and a list that gets shrunk to five
    rows cannot answer "what did I propose that I never executed". The full
    record is one id away in ``GET /tickets/{ticket_id}``.

    ``status`` is an enum rather than a free string on purpose: a typo used to
    return an empty list, which reads as "nothing outstanding" — the exact
    failure this enumeration exists to prevent.
    """
    rows = get_desk().decisions.list_tickets(limit=limit, status=status, view=view)
    body: dict = {
        "as_of": datetime.now(SHANGHAI).strftime("%Y%m%d"),
        "count": len(rows),
        "tickets": rows,
    }
    if not rows:
        # 空数组不是一个答案。`as_of` 说"这份清单是什么时候的"，这里说"为什么是空的"——
        # 少了任一句，读的人都分不清"真的没有"和"你没查对"。交付审计里这条
        # （F 全空且无说明）就是工具层自己抓出来的。
        if status == "open":
            body["note"] = (
                "当前没有未结票据：proposed / queued 都是空的。"
                "终态要按 status 明确查（filled / rejected / cancelled / expired）。"
            )
        elif status:
            body["note"] = f"没有 status={status} 的票据。"
        else:
            body["note"] = "账本里还没有任何票据。"
    return body


@router.get("/tickets/{ticket_id}")
def ticket(ticket_id: str) -> dict:
    desk = get_desk()
    try:
        return desk.decisions.get(ticket_id)
    except DecisionError as exc:
        raise _fail(exc) from exc


@router.get("/orders")
def orders(limit: int = Query(default=50, ge=1, le=200)) -> dict:
    rows = get_desk().paper.list_orders(limit)
    out = {
        "as_of": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "status": "ok" if rows else "empty",
        "count": len(rows),
        "orders": rows,
    }
    if not rows:
        out["reason"] = "当前没有委托或成交审计记录；这是已读取后的空结果，不是读取失败"
    return out


@router.get("/meta/vocabulary")
def api_vocabulary() -> dict:
    """接口词表：前端映射表该对着哪张表校对，数字该按什么量纲读。

    ``decisions.kind`` / ``actor`` 以前没有值域约束，前端只能看着某一次响应
    现编中文，拼错一个字母整张映射就失效（docs/14 C1）。这里把值域、guard
    检查项、decay reason 与单位约定一次给出；``undeclared_*`` 是把运行时写进
    账本、却没在词表里登记的值显式暴露出来。
    """
    return {"as_of": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
            **vocab.as_dict()}


@router.get("/decisions")
def decisions(limit: int = Query(default=30, ge=1, le=200)) -> dict:
    return {"decisions": get_desk().paper.list_decisions(limit)}


def _facts_for(desk, day: str) -> dict | None:
    """The day's facts, read from the ``ROOT`` this module resolved.

    The logic lives in `service.trading.facts.facts_for` because the backfill
    script needs the same answer: a snapshot on disk wins, a missing one is
    computed once and written. Only the root differs — this route reads the
    directory `.security` fixed, the script takes the repo it lives in — so the
    wrapper passes it through rather than keeping a second copy of the rules.
    """
    from service.trading.facts import facts_for

    return facts_for(desk, day, root=ROOT)


@router.post("/journal")
def add_journal(body: JournalBody) -> dict:
    """Append a note, and say which of its numbers the session cannot reproduce.

    The journal is read back in later sessions — a wrong number here is not a
    one-off, it becomes memory and gets cited again. So every entry is checked
    against that day's facts before it lands. The check never rejects: refusing
    the write would teach the writer to drop numbers, and half the value of a
    review is in its numbers. It reports, and the verdict is stored alongside
    the entry so a later reader knows it was checked.
    """
    from service.trading import journal_check

    desk = get_desk()
    day = desk.book.state.get("day") or desk.market.latest_bar_date()
    # 当日价格也要算进"这次能复现的数"：facts 里没有收盘价，而复盘一定会引用它。
    facts = _facts_for(desk, day)
    verdict = journal_check.verify(desk, body.text, day, facts=facts)
    row_id = desk.paper.add_journal(
        body.text,
        kind=body.kind,
        symbol=body.symbol,
        tags=body.tags,
        snapshot=journal_check.stored(verdict, day),
    )
    return {"id": row_id, "ok": True, "verification": verdict}


@router.post("/declined")
def add_declined(body: DeclinedBody) -> dict:
    """记一条「看过，没买」——第四种会被结算的判断。

    提案由 arena 算超额、人格切换由 scorecard 算、假设由裁判判，唯独"不行动"
    此前只存在于复盘散文里。2026-11-01 到 11-03 三天零成交、零记录，就是三天
    判断力没有任何证据。

    请求里**没有价格字段**：结算自己从 `daily` 读收盘价。否则一条"我不买"可以
    事后挑一个价格来自证。
    """
    from service.trading import declined as declined_mod

    desk = get_desk()
    day = body.day or desk.book.state.get("day") or desk.market.latest_bar_date()
    try:
        return declined_mod.add(
            desk.paper,
            day,
            body.symbols,
            body.reason,
            invalidation=body.invalidation,
            horizon_days=body.horizon_days,
        )
    except ValueError as exc:
        raise HTTPException(422, detail={"code": "BAD_DECLINED", "message": str(exc)}) from exc


@router.get("/declined")
def list_declined(
    limit: int = Query(default=50, ge=1, le=500), settled: bool | None = None
) -> dict:
    from service.trading import declined as declined_mod

    desk = get_desk()
    return {
        "rows": desk.paper.list_declined(limit=limit, settled=settled),
        "summary": declined_mod.summary(desk.paper),
    }


@router.post("/declined/score")
def score_declined(dry_run: bool = False) -> dict:
    """把到期的「不做」结算掉。没到期的报 pending——那不是失败。"""
    from service.trading import declined as declined_mod

    desk = get_desk()
    return declined_mod.score(desk.data, desk.paper, dry_run=dry_run)


VERIFY_NOTE = (
    "verified 是这条记忆**写入时**被核的结果：checked 个数字里 matched 对上了、"
    "unmatched 对不上（列表只列前 3 个，unmatched_count 是全部个数）。"
    "sign_from_words 是「数对上了、方向只在文字里」的那几个。"
    "unmatched_detail / sign_hints 是同一批数的带坐标版本：value、excerpt（前后各 20 字）、"
    "start/end 字符偏移、field（启发式推断的数字角色）；前端据此定位到具体那句话，"
    "裸列表只作兼容保留。claims 是带标签的那部分：labelled 个里 ok 对、"
    "misplaced 贴错标签、variant 换了口径、"
    "unbacked 这个标签下没这个数、provisional 不判但照报。"
    "claims.scope=market_only 表示那天只补得回市场那半边——标签核过，数字没核。"
    "verified 为 null 表示从未被核过——不是「核过没问题」。"
)


def _number_checked(entry: dict) -> bool:
    """Did the *number* check actually run on this entry?

    Not the same as "does it have a verdict": an entry whose day had no facts
    carries `checked: 0, skipped: true`, and one backfilled from market data
    only carries `claims` without `checked`. Both are "we do not know", and
    folding them into "verified" is the failure this project keeps fixing.
    """
    verified = entry.get("verified") or {}
    return "checked" in verified and not verified.get("skipped")


def _disclose_unchecked(payload: dict, entries: list[dict]) -> None:
    """Say how many rows on this page were never number-checked, when some were not.

    A page of ten rows where three carry ``verified: null`` reads as a page of
    seven verdicts unless the count is stated — silence is indistinguishable
    from "all fine". Once the backfill has run this key disappears by itself,
    which is the point: the disclosure tracks the data, not the code.
    """
    unchecked = sum(1 for entry in entries if not _number_checked(entry))
    if unchecked:
        payload["unchecked"] = unchecked
        payload["unchecked_note"] = (
            f"{unchecked}/{len(entries)} 条的数字没核过——写在核对机制之前，或者那天"
            "没有可比的收盘事实；其中一些的标签那半核过（claims.scope=market_only）。"
            "引用它们的数字时按未核处理。"
        )


def _journal_entry(row: dict, *, keep_text: bool) -> dict:
    """One journal row, with the prose only where it is worth the tokens.

    A journal entry is reasoning written for a human: the eleven entries on this
    desk were 22 KB, 2,008 bytes each, and read as the single most expensive
    thing a session could call. Most of that text is a narrative of a day that
    is already settled — the decision it explains is not on the table anymore.
    The first few entries still get their full text, because recency is what
    makes a past decision relevant; the rest keep a summary and their length, so
    the model can see that something long exists and ask for it by id.

    ``verified`` is lifted out of the snapshot so a reader sees it without
    knowing the key existed: the check happens when the note is written, but
    the reader is a different session, and "was this ever checked" is exactly
    what it cannot know.

    The summary is head **and** tail, not just head. These entries are written
    verdict-first and lesson-last: a review opens with the score ("12 条可结算
    → 对 9 / 错 2") and closes with what to change ("下次改一条：…", "缺口：…").
    A prefix-only summary kept the score and dropped every action item, so the
    reason to open the entry was never in the teaser. 300 characters or less is
    shown whole — at that length the "summary" and the entry are the same thing,
    and an ellipsis would only hide which is which.

    The token list is cut to three. At write time the full list is the fix list;
    at read time nobody is going to edit a settled note, and thirty quoted
    numbers are 200 B that answer a question the reader did not ask. The count
    stays, so "8 of 15 don't reproduce" survives the cut.
    """
    text = row.get("text") or ""
    snapshot = dict(row.get("snapshot") or {})
    numbers = snapshot.pop("numbers", None)
    label_claims = snapshot.pop("claims", None)
    out = {key: row.get(key) for key in ("id", "ts", "kind", "symbol", "tags")}
    if numbers or label_claims:
        numbers = dict(numbers or {})
        for key in ("unmatched", "sign_from_words"):
            if len(numbers.get(key) or []) > 3:
                numbers[key] = numbers[key][:3]
        # 裸列表在这里补成带坐标的清单（docs/14 B1/B2）。只对截断后真正显示出来
        # 的那几个求 excerpt；历史条目也走同一条路，不必回填数据库。
        from service.trading import journal_check

        numbers = journal_check.detail(text, numbers)
        if label_claims:
            numbers["claims"] = label_claims
    out["verified"] = numbers or None
    if snapshot:
        out["snapshot"] = snapshot
    out["chars"] = len(text)
    if keep_text:
        out["text"] = text
    elif len(text) <= 300:
        # 300 字以内不截：head+tail 拼出来就是全文，加省略号只会让人分不清
        # 拿到的是摘要还是原文。键名仍是 summary——"compact 模式没有 text 键"
        # 是调用方用来判断拿没拿到全文的依据，换个键名会把这个判断弄坏。
        out["summary"] = text
    else:
        out["summary"] = f"{text[:100]} … {text[-160:]}"
        out["summary_basis"] = "首 100 字 + 末 160 字；要全文用 id 单取"
    return out


@router.get("/journal")
def journal(
    q: str = "",
    limit: int = Query(default=20, ge=1, le=100),
    full: bool = False,
    recent: int = Query(default=3, ge=0, le=20),
    kind: str | None = None,
    since: str | None = None,
    until: str | None = None,
    id: int | None = Query(default=None, ge=1),
) -> dict:
    """Journal entries. Compact by default; full text for the newest ``recent``.

    ``full=true`` restores the old behaviour. A query is still a query: the top
    ``recent`` hits carry their text so a search that finds the right entry hands
    back something usable rather than a teaser.

    ``kind`` / ``since`` / ``until`` answer "the last few reviews" without
    scanning a fixed window, and ``id`` opens one entry — before it, reading a
    single 700-character review meant pulling the whole page at full length.
    """
    try:
        rows = get_desk().paper.search_journal(
            q, limit, kind=kind, since=since, until=until, row_id=id
        )
    except ValueError as exc:
        raise HTTPException(400, detail={"code": "BAD_DATE", "message": str(exc)}) from exc
    # 按 id 单取时那条必然是全文：请求的就是"打开这一条"，再返回一个摘要
    # 等于让调用方多绕一次 full=1。
    entries = [
        _journal_entry(row, keep_text=full or i < recent or id is not None)
        for i, row in enumerate(rows)
    ]
    payload = {
        "query": q,
        "filters": {"kind": kind, "since": since, "until": until, "id": id},
        "count": len(entries),
        "entries": entries,
        "verify_note": VERIFY_NOTE,
        "note": (
            f"默认只给 120 字摘要与字数，前 {recent if not full else len(entries)} "
            "条带全文（recent 可调）；要全部全文传 full=1。"
        ),
    }
    _disclose_unchecked(payload, entries)
    if full:
        payload["note"] = "full=1：全部条目带全文，体积约为默认的 3–5 倍"
    if id is not None and not entries:
        payload["note"] = f"没有 id={id} 这条记录"
        payload["missing"] = int(id)
    return payload


def _report_head(path: Path, limit: int = 160) -> str:
    """First non-empty line, for a listing. Reads a chunk, not the file."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            chunk = handle.read(limit)
    except OSError:
        return ""
    for line in chunk.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:120]
    return ""


def _report_path(name: str) -> Path:
    """Resolve a report name under ``reports/`` or refuse.

    The name comes from the model, and this file's history has the same hole
    twice already: ``/facts`` and the external-report endpoint both concatenated
    a caller-supplied string into a path, and ``../../configs/data`` resolved to
    a real file one directory up. The check is on the **resolved** path, not on
    the string, because that is the only form symlinks and ``..`` cannot walk
    around.
    """
    raw = (name or "").strip().lstrip("/")
    if not raw:
        raise HTTPException(400, detail={"code": "BAD_PATH", "message": "path 不能为空"})
    root = REPORTS_DIR.resolve()
    target = (root / raw).resolve()
    if root not in target.parents:
        raise HTTPException(
            400, detail={"code": "BAD_PATH", "message": f"{name!r} 不在 reports/ 下"}
        )
    if target.suffix.lower() != ".md":
        raise HTTPException(400, detail={"code": "BAD_PATH", "message": "只读 reports/ 下的 .md"})
    if not target.is_file():
        raise HTTPException(404, detail={"code": "NO_REPORT", "message": f"没有 {raw}"})
    return target


@router.get("/reports")
def reports(kind: str | None = None, limit: int = Query(default=30, ge=1, le=200)) -> dict:
    """The markdown under ``reports/``, newest first, without their bodies.

    These files were **write-only** until now: the desk wrote 119 of them (and
    the journal entries point at them with "全文见 reports/review/20261103.md"),
    but no route could open one, so next session the pointer dangled. A report
    reader is what makes a cross-day handoff an actual handoff.

    Listing reads only the first line of each file — the title — because 119
    bodies is 1.6 MB of IO to answer "what is there".
    """
    root = REPORTS_DIR.resolve()
    rows: list[dict] = []
    if root.exists():
        for path in root.rglob("*.md"):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            folder = rel.split("/")[0] if "/" in rel else ""
            if kind and folder != kind:
                continue
            try:
                stat = path.stat()
                title = _report_head(path)
            except OSError:
                continue
            rows.append(
                {
                    "path": rel,
                    "kind": folder,
                    "title": title,
                    "bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime, SHANGHAI).isoformat(
                        timespec="seconds"
                    ),
                }
            )
    rows.sort(key=lambda r: r["modified"], reverse=True)
    folders = sorted({r["kind"] for r in rows if r["kind"]})
    return {
        "count": len(rows),
        "kind": kind,
        "folders": folders,
        "reports": rows[:limit],
        "truncated": len(rows) > limit,
        "note": (
            "要读正文用 path 调 /reports/file。顶层 reports/*.md 是早期审计长文，"
            "review/ close/ open/ noon/ 才是按日交接。"
        ),
    }


@router.get("/reports/file")
def report_file(
    path: str,
    offset: int = Query(default=0, ge=0),
    max_chars: int = Query(default=REPORT_PAGE_CHARS, ge=200, le=REPORT_PAGE_MAX),
) -> dict:
    """One report, paged.

    A review is ~8,300 characters (14.4 KB). The host drops a body over 16 KB
    *whole* rather than shortening it, so returning a long report in one piece
    is the same as returning nothing. Six thousand characters is ~10 KB with
    room to spare; past that the answer says ``truncated`` and hands back
    ``next_offset`` instead of pretending the report ended there.
    """
    target = _report_path(path)
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(500, detail={"code": "READ_FAILED", "message": str(exc)}) from exc
    total = len(text)
    start = min(offset, total)
    chunk = text[start : start + max_chars]
    end = start + len(chunk)
    more = end < total
    payload = {
        "path": path,
        "chars": total,
        "bytes": len(text.encode("utf-8")),
        "offset": start,
        "returned_chars": len(chunk),
        "text": chunk,
        "truncated": more,
        "next_offset": end if more else None,
        "note": (
            f"全文 {total} 字，本页 {len(chunk)} 字。"
            + (f"接下一页传 offset={end}。" if more else "已到结尾。")
        ),
    }
    size = len(chunk.encode("utf-8"))
    if size > 15000:
        payload["warn"] = f"本页 {size} B，接近宿主 16 KB 上限；调小 max_chars 重取"
    return payload


@router.get("/playbook")
def playbook(section: str | None = Query(
        default=None,
        description='章号，逗号分隔，如 "0,2,9"；省略则按字节预算从头取')) -> dict:
    """当前生效的规则 + 待激活草案的元数据。

    草案原先只在 `/playbook/history` 里，而没有任何提示词读那个端点——于是
    「每天生成一版草案、从不激活」在工具输出里完全看不见：`drafts` 从 11-04
    堆到 11-06，每个窗口的 `quant_playbook` 都只显示 v1，看不出有东西等着人批。
    `is_fragment` 是给激活的人看的：草案若只写了新增条款，激活会把现行规则书
    覆盖成两章的碎片，`missing_sections` 列出会丢掉的章节号。

    正文按章切片，`truncated`/`next_section` 说明这次读到了哪：这个响应曾经
    整体 16,301 B，被宿主的 16 KB 上限压成 3,658 B 且 `text` 只剩 241 字，
    读的一端因此从未看到 §2 与 §9。
    """
    current = pb.current(ROOT)
    book = pb.history(ROOT)
    drafts = book.get("drafts", [])
    superseded = book.get("superseded", [])
    active_text = current.get("text", "")
    sections = pb.index_sections(active_text)
    # 直接调用（scripts、报告、测试）时默认值还是 Query 对象，不是 None。
    section = section if isinstance(section, str) else None
    names = {part.strip() for part in section.split(",") if part.strip()} if section else None
    sliced = pb.select_sections(sections, names)

    rows = []
    for draft in drafts:
        # 「待激活」不等于「激活了就好」：这两份草案都是补丁，激活会把规则书
        # 换成两章的碎片。把这件事写进工具输出，否则每个窗口只会重复
        # 「草案连续第 N 日未激活」，把一条不可执行的动作排成待办。
        review = pb.draft_review(ROOT, draft, active_text)
        rows.append({
            "draft_id": draft.get("draft_id"),
            "created_at": draft.get("created_at"),
            "note": (draft.get("note") or "")[:64],
            "file": draft.get("file"),
            "in_effect": False,
            "is_fragment": review["is_fragment"],
            "draft_chars": review["draft_chars"],
            "missing_sections": review["missing_sections"][:12],
        })
    payload = {
        "version": current.get("version"),
        "updated_at": current.get("updated_at"),
        "note": (current.get("note") or "")[:64],
        "chars": len(active_text),
        "text": sliced["text"],
        "sections": [
            {"n": row["n"], "title": row["title"], "chars": row["chars"]}
            for row in sections
        ],
        "included": sliced["included"],
        "truncated": sliced["next_section"] is not None,
        "next_section": sliced["next_section"],
        # 版本表只留「谁、什么时候」，理由在 /playbook/history：这里每一行
        # 都要和正文抢同一个 16 KB 预算，而正文才是读它的人要的东西。
        "history": [
            {"version": v.get("version"), "created_at": v.get("created_at")}
            for v in book["versions"]
        ],
        "draft_count": len(drafts),
        "drafts": rows,

        # 被某个正式版本整份吸收的草案。不算待批，但也不是「从没提过」——
        # 留着才能回答「§7 是什么时候、以什么理由生效的」。
        "superseded_count": len(superseded),
        "superseded": [
            {"draft_id": s.get("draft_id"), "by_version": s.get("by_version")}
            for s in superseded
        ],
    }
    if sliced["next_section"]:
        payload["next"] = (
            f"正文按章切片，本次到 §{sliced['next_section']}；"
            f'用 section="{sliced["next_section"]}" 续读，或 section="0,2,9" 选章'
        )
    return payload


@router.get("/playbook/history")
def playbook_history() -> dict:
    return pb.history(ROOT)


@router.post("/playbook/propose")
def playbook_propose(body: PlaybookProposeBody) -> dict:
    try:
        draft = pb.propose(ROOT, body.text, body.note, author="agent")
    except pb.PlaybookError as exc:
        raise HTTPException(400, detail={"code": "PLAYBOOK", "message": str(exc)}) from exc
    return {"status": "draft", "draft": draft}


@router.post("/playbook/activate")
def playbook_activate(body: PlaybookActivateBody) -> dict:
    try:
        entry = pb.activate(ROOT, body.draft_id, body.challenge)
    except pb.PlaybookError as exc:
        raise HTTPException(
            403, detail={"code": "PLAYBOOK_ACTIVATION", "message": str(exc)}
        ) from exc
    return {"status": "active", "version": entry}


@router.get("/cases")
def cases(
    limit: int = Query(default=20, ge=1, le=200),
    symbol: str | None = None,
    attribution: bool = False,
) -> dict:
    desk = get_desk()
    rows = desk.paper.list_cases(limit=limit, symbol=symbol)
    # 6 位小数：一个比率，不是浮点打印。0.008083874231709023 读起来像"有精度可用"，
    # 而每个消费者都会先乘 100 再取两位。as_of 是这批记录的定价日——没有它，
    # "这是哪天的账"要靠每行的时间戳自己推。
    for row in rows:
        if isinstance(row.get("pnl_pct"), float):
            row["pnl_pct"] = round(row["pnl_pct"], 6)
    out = {"as_of": desk.market.latest_bar_date(), "count": len(rows), "cases": rows}
    if attribution and rows:
        # 只给最近 5 笔：一笔约 300 字节，50 笔就把整个响应顶出 16 KB 上限，
        # 而那正好是"信息补全了但模型拿不到"的老问题。
        from service.trading.attribution_parts import annotate

        recent = rows[:5]
        detail = annotate(desk.market, recent)
        by_id = {row["case_id"]: row.get("attribution") for row in detail.get("rows") or []}
        for row in recent:
            row["attribution"] = by_id.get(row.get("case_id"))
        out["attribution_note"] = detail.get("note")
        out["attribution_summary"] = detail.get("summary")
    return out


@router.get("/memory/search")
def memory_search(
    q: str = "",
    limit: int = Query(default=10, ge=1, le=50),
    full: bool = False,
    recent: int = Query(default=2, ge=0, le=10),
) -> dict:
    """Journal and cases in one shot, with the same token discipline as /journal.

    Search is the reason the caller is here, so the newest hits keep their text —
    but ten full journal entries is 20 KB of prose, and a model that asked "did I
    write about banks before" does not need all ten verbatim.

    Each row carries ``verified``: the verdict its numbers got when the entry was
    written. Reading a past conclusion is the moment that matters — the reader
    is a later session that cannot tell a checked note from an unchecked one,
    and the numbers in a note get quoted again.
    """
    desk = get_desk()
    journal_rows = (
        desk.paper.search_journal(q, limit) if q else desk.paper.search_journal("", limit)
    )
    entries = [
        _journal_entry(row, keep_text=full or i < recent) for i, row in enumerate(journal_rows)
    ]
    payload = {
        "query": q,
        "journal": entries,
        "cases": desk.paper.search_cases(q, limit) if q else desk.paper.list_cases(limit),
        "fts": desk.paper.fts,
        "verify_note": VERIFY_NOTE,
    }
    _disclose_unchecked(payload, entries)
    return payload


@router.get("/research/hypotheses")
def list_hypotheses(limit: int = Query(default=20, ge=1, le=200), full: bool = False) -> dict:
    """Hypotheses, summarised by default.

    Each row carries a 2 KB ``test_spec`` and a ``result`` blob; five rows came to
    11 KB and the default limit was 50, so a single "what have I hypothesised"
    question could return 100 KB into a context window that spends 4 KB on the
    whole session brief. The default now answers the only question a list has to
    answer — which ones exist and how they ended — and ``full=1`` carries the
    spec and the full result for the one you intend to re-run.
    """
    rows = get_desk().paper.list_hypotheses(limit)
    if full:
        return {"hypotheses": rows}
    out = []
    for row in rows:
        result = row.get("result") or {}
        metrics = result.get("metrics") if isinstance(result, dict) else None
        out.append(
            {
                "id": row.get("id"),
                "status": row.get("status"),
                "statement": row.get("statement"),
                "updated_at": row.get("updated_at"),
                # One word for how it ended, lifted out of the result blob: a reader
                # scanning the list does not need the whole metrics dict to know that
                # something was rejected.
                "verdict": (metrics or {}).get("verdict") if isinstance(metrics, dict) else None,
            }
        )
    return {
        "hypotheses": out,
        "note": "摘要：test_spec 与完整 result 要 full=1；statement 已足够判断是哪一条",
    }


@router.post("/research/hypotheses")
def add_hypothesis(body: HypothesisBody) -> dict:
    desk = get_desk()
    hyp_id = desk.paper.add_hypothesis(body.statement, body.rationale, body.test_spec)
    desk.paper.log_decision("hypothesis", body.model_dump(), actor="agent")
    return {"id": hyp_id, "hypothesis": desk.paper.get_hypothesis(hyp_id)}


def _promotion_guard(body: HypothesisResultBody) -> list[str]:
    """Refuse a promotion that its own evidence already contradicts.

    2026-11-12: a factor was settled ``promoted`` on a full-sample t of −3.43,
    then the time split showed −6.09 in the first half and **+0.03** in the
    second. ``factors._time_holdout`` already existed and would have said
    ``sign_agrees: false``; nobody looked. ``promoted`` feeds
    ``promotion_gate``'s hypotheses item, so a stale promotion is not a harmless
    label — it moves a gate.

    Two-tier rule, because a cross-sectional study with a single snapshot has no
    time split to give:

    - ``metrics.time_holdout.sign_agrees == false`` → **refuse** (409). The
      caller's own numbers say the relation flipped between halves.
    - ``time_holdout`` missing entirely → **allow, with a warning** returned
      alongside. It is not always computable; it is always worth saying so.

    Returns the warnings to attach to the response.
    """
    if body.status != "promoted":
        return []
    metrics = body.metrics or {}
    holdout = metrics.get("time_holdout")
    if isinstance(holdout, dict):
        if holdout.get("sign_agrees") is False:
            raise HTTPException(409, detail={
                "code": "PROMOTION_CONTRADICTED",
                "message": ("time_holdout.sign_agrees=false：这条的前后半段方向相反，"
                            "不能记 promoted。降为 testing 并写明时段依赖，"
                            "或补充样本外证据后重新结算。"),
                "time_holdout": holdout,
            })
        return []
    return [("metrics.time_holdout 缺失：promoted 未附时间稳定性证据，"
             "建议补 factors._time_holdout 的读数十秒")]


@router.post("/research/hypotheses/{hyp_id}/result")
def hypothesis_result(hyp_id: int, body: HypothesisResultBody) -> dict:
    desk = get_desk()
    warnings = _promotion_guard(body)
    result = {"metrics": body.metrics, "notes": body.notes}
    updated = desk.paper.update_hypothesis(hyp_id, body.status, result)
    if updated is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": f"hypothesis {hyp_id}"})
    desk.paper.log_decision("hypothesis_result", {"id": hyp_id, **result}, actor="agent")
    out = {"hypothesis": updated}
    if warnings:
        out["warnings"] = warnings
    return out


@router.post("/control/kill-switch")
def kill_switch(body: ControlBody) -> dict:
    desk = get_desk()
    desk.book.set_kill_switch(body.value)
    desk.paper.log_decision("kill_switch", {"value": body.value}, actor="human")
    return {"kill_switch": body.value}


@router.post("/control/safe-mode")
def safe_mode(body: ControlBody) -> dict:
    desk = get_desk()
    desk.book.set_safe_mode(body.value)
    desk.paper.log_decision("safe_mode", {"value": body.value}, actor="human")
    return {"safe_mode": body.value}


# ---------------------------------------------------------------- reasoning
class MemoBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=4, max_length=200)
    thesis: str = Field(min_length=20, max_length=4000)
    mechanism: str = Field(
        default="", max_length=2000, description="why this should work, causally"
    )
    falsifier: str = Field(
        default="", max_length=1000, description="what observation would prove you wrong"
    )
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    predictions: list[dict] = Field(default_factory=list, max_length=6)


@router.get("/discover")
def discover(as_of: str | None = None) -> dict:
    as_of = _day(as_of, "as_of")
    from service.research.discover import discover as build

    desk = get_desk()

    def build_panel() -> dict:
        return build(desk.data, desk.registry, as_of, core_book=desk.core_book())

    # Cached, and single-flighted. The panel costs ~1.4 s to rebuild and it is
    # a pure function of (as_of, stored data). Without the cache a ten-way
    # fan-out measured 21.7 s per caller on 2026-11-02 — past the client's
    # timeout, which then reported the service as unreachable while it was
    # answering. One build, one answer, ten callers.
    return _with_units(desk.market.cached_answer(f"discover:{as_of}", build_panel),
                       "/discover")


def _core_retired() -> dict:
    """Why the core sleeve refuses work, in the caller's terms.

    A 409 with a code beats a 200 carrying an empty order list: the second one
    reads as "the rule had nothing to do today" and gets retried tomorrow, which
    is exactly how six sessions of an unexecutable sleeve went unnoticed.
    """
    return {
        "code": "CORE_RETIRED",
        "message": (
            "核心盘已于 2026-11-01 关闭：防守腿一手金额超过账户规模"
            "（511010 一手 14,112 元 vs 权益 5,007 元），每个防守日只会写"
            "一条 100% 现金的净值行，不构成任何判断。要恢复，把 "
            "configs/core.json 的 retired 改为 false。"
        ),
    }


@router.get("/core")
def core_status() -> dict:
    desk = get_desk()
    from service.trading.core import CoreBook, describe

    book = CoreBook(desk.data, desk.paper)
    day = book.last_day()
    payload = book.status(day)
    payload["rationale"] = describe(book.target_weights(day))
    payload["nav_history"] = desk.paper.core_nav_history(limit=60)
    payload["recent_trades"] = desk.paper.core_trades(limit=20)
    return payload


@router.post("/core/plan")
def core_plan(force: bool = False) -> dict:
    """What the *registered rule* implies right now. Information, never an order.

    The response shows the weights a documented inverse-volatility rule would
    pick, priced into lots. Nothing here is executed by any code path: this
    endpoint has no side effects, and `/core/execute` will not accept this
    output - it needs the caller's own targets. Read it as one opinion among
    several, produced by a rule whose behaviour you can inspect.
    """
    from service.trading.core import CoreBook, describe

    desk = get_desk()
    book = CoreBook(desk.data, desk.paper)
    if book.retired:
        raise HTTPException(409, detail=_core_retired())
    day = desk.market.latest_bar_date()
    if not day:
        raise HTTPException(503, detail={"code": "NO_BARS", "message": "no daily bars"})
    need, why = book.should_rebalance(day)
    if not (need or force):
        return {
            "rebalance_needed": False,
            "orders": [],
            "reason": why,
            "as_of": day,
            "note": "规则与账本一致，规则本身没有订单可下。",
        }
    plan = book.plan(day)
    fees = sum(float(o["fee"]) for o in plan["orders"])
    return {
        "as_of": day,
        "trigger": why if need else "forced",
        "source": "registered rule",
        "plan": plan,
        "cost": {
            "fees_cny": round(fees, 2),
            "turnover_cny": round(
                sum(float(o["qty"]) * float(o["price"]) for o in plan["orders"]), 2
            ),
        },
        "rationale": describe(book.target_weights(day)),
        "note": (
            "这是规则会在当前账本上做出的分配，不是建议，也不会被执行。"
            "要用它，请自己给出 targets 调 /core/preview 和 /core/execute。"
        ),
    }


@router.post("/core/preview")
def core_preview(body: CoreTargetsBody) -> dict:
    """Price the caller's own allocation into whole lots. Pure.

    The useful part is what the arithmetic refuses: a weight whose cash value is
    smaller than one lot buys zero shares, and the bond ETF at ~CNY 147 a share
    is beyond a small account entirely. Seeing that before committing is the
    difference between a plan and a guess.
    """
    from service.trading.core import CoreBook

    desk = get_desk()
    book = CoreBook(desk.data, desk.paper)
    day = body.as_of or desk.market.latest_bar_date()
    if not day:
        raise HTTPException(503, detail={"code": "NO_BARS", "message": "no daily bars"})
    try:
        plan = book.plan(day, targets=body.targets)
    except ValueError as exc:
        raise HTTPException(422, detail={"code": "BAD_TARGETS", "message": str(exc)}) from exc
    return {"as_of": day, "booked": False, **plan}


@router.post("/core/execute")
def core_execute(body: CoreExecuteBody) -> dict:
    """Book the caller's own allocation under the caller's own reasoning.

    The weights come from the request, not from a rule the server picked. The
    kernel does three things: turn weights into lots, refuse the illegal ones,
    and write the decision to the log. Idempotent per trading day.
    """
    from service.trading.core import CoreBook

    desk = get_desk()
    book = CoreBook(desk.data, desk.paper)
    if book.retired:
        raise HTTPException(409, detail=_core_retired())
    day = desk.market.latest_bar_date()
    if not day:
        raise HTTPException(503, detail={"code": "NO_BARS", "message": "no daily bars"})
    try:
        plan = book.plan(day, targets=body.targets)
    except ValueError as exc:
        raise HTTPException(422, detail={"code": "BAD_TARGETS", "message": str(exc)}) from exc
    result = book.execute(plan)
    desk.paper.log_decision(
        "core_execute",
        {
            "targets": body.targets,
            "thesis": body.thesis,
            "evidence": body.evidence,
            "invalidation": body.invalidation,
            "orders": plan["orders"],
            "applied": result.get("applied"),
        },
        actor="agent",
    )
    return {
        "applied": result.get("applied"),
        "orders": len(plan["orders"]),
        "nav": result.get("nav"),
        "cash_weight": result.get("cash_weight"),
        "plan": plan,
        "note": result.get("note"),
    }


@router.get("/watch")
def watch(as_of: str | None = None, include_tape: bool = True, since: str | None = None) -> dict:
    """Should this wake-up do anything at all?

    A scheduled session costs a full agent turn. This answers, in about 200 ms
    and without a model, whether any fact crossed a threshold: a stop within 2%,
    a holding that moved 5%, a queued order about to fill, an extreme tape. Empty
    findings mean "nothing happened, stop here" and the caller can end the turn
    after one tool call.

    It is also honest about what it could not measure: a dead quote feed produces
    an explicit ``quotes_unavailable`` finding rather than a clean bill of health.
    """
    as_of = _day(as_of, "as_of")
    from service.trading import tripwire

    desk = get_desk()
    day = as_of or desk.market.last_trading_day()
    out = tripwire.evaluate(desk, day=day, include_tape=include_tape)
    if since:
        # "有没有越过阈值"和"和上次比变了什么"是两个问题。前者说明有事发生，
        # 后者说明发生了什么——叫醒时先问的其实是第二个。
        from service.trading import changes

        out["changes"] = changes.diff(desk, _day(since, "since"), day)
    return out


@router.get("/monitor/handoff")
def monitor_handoff() -> dict:
    """The question the alert monitor is waiting for an answer to.

    The scheduled monitor cannot reach a model, so it writes its facts and the
    exact question it wants answered, and stops. This is that inbox: a compact
    prompt per pending alert, small enough for a fast model to answer in one
    pass, plus the reminder that answering it is what sends the second mail.

    Nothing here is a recommendation. The rules produced the facts; the reading
    is what the caller supplies to ``POST /monitor/handoff/answer``.
    """
    from service.trading import handoff

    items = handoff.pending()
    window = handoff.READ_WINDOW_HOURS
    fresh = [i for i in items
             if i.get("age_hours") is not None and i["age_hours"] <= window]
    return {
        "ok": True,
        "pending": len(items),
        "readable": len(fresh),
        "stale": len(items) - len(fresh),
        # 窗口由服务端给出，读者不必自己算「多少小时算今天」——原先那句
        # 「只处理今天的」在 15:25 之后产生的条目上永远为假。
        "read_window_hours": window,
        "items": items,
        "answer_with": "POST /monitor/handoff/answer {id, analysis}",
        "note": (
            "每条 item 的 prompt 就是完整的问题；回答后服务端发第二封邮件。"
            f"age_hours ≤ {window} 的条目在解读窗口内，超出窗口的只报数量不解读。"
            "没有待解读项时 pending=0，那不是故障。"
        ),
    }


@router.post("/monitor/handoff/answer")
def monitor_handoff_answer(body: HandoffAnswerBody) -> dict:
    """Attach the reading and send the mail. The only write in this pair."""
    from service.trading import handoff

    try:
        result = handoff.answer(body.id, body.analysis, force=body.force)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail={"code": "NO_HANDOFF", "message": f"没有 {body.id}"}
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail={"code": "EMPTY_ANALYSIS", "message": str(exc)}
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503, detail={"code": "MAIL_FAILED", "message": str(exc)}
        ) from exc
    status = result.get("status")
    if status in ("sending", "send_unknown"):
        # 200 here would read as "accepted" to any caller that only checks the
        # status code, while nothing was sent.
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SENDING" if status == "sending" else "MAIL_UNKNOWN",
                "message": (
                    "另一个进程正在发送这封邮件，稍后用 GET /monitor/handoff 确认。"
                    if status == "sending"
                    else "上一次投递结果未知，邮件可能已经发出；核对邮箱后确认没收到，"
                    "再带 force=true 重发。"
                ),
                "may_have_sent": bool(result.get("may_have_sent")),
                "lock": result.get("lock"),
            },
        )
    return {"ok": True, **result}


@router.get("/observations")
def observations(as_of: str | None = None) -> dict:
    """Constraint state, rule drift and remaining risk budget.

    Facts and explicit hypotheticals only: no action field, no recommendation.
    What to do about any of it is the caller's judgement.
    """
    as_of = _day(as_of, "as_of")
    from service.trading.observation import snapshot

    return snapshot(get_desk(), as_of)


@router.get("/decay")
def decay_status(
    strategy: str | None = None, window: int = Query(default=20, ge=10, le=60)
) -> dict:
    """Has a live strategy's realised record drifted from what it promised?

    Facts only. A strategy in `decaying` is not liquidated by this endpoint and
    not re-weighted: code does not trade. The state is for the operator.
    """
    from service.trading import decay as decay_mod
    from service.trading import strategy as strategy_mod

    desk = get_desk()
    cases = desk.paper.list_cases(limit=2000)
    rows = []
    keys = [strategy] if strategy else list(strategy_mod.REGISTRY)
    for key in keys:
        meta = strategy_mod.get(key)
        if meta is None:
            raise HTTPException(404, detail={"code": "NOT_FOUND", "message": f"strategy {key}"})
        baseline = decay_mod.baseline_from_evidence(meta.evidence)
        if baseline is None:
            rows.append(
                {
                    "key": key,
                    "status": meta.status,
                    "state": "no_baseline",
                    "n": 0,
                    "note": "evidence has no mean/sigma or cagr/sharpe",
                    "note_meta": {"reason": "no_baseline"},
                }
            )
            continue
        if meta.book == "core":
            nav = desk.paper.core_nav_history(limit=2000)
            result = decay_mod.monitor_nav(nav, baseline, strategy=key, window=window)
        else:
            result = decay_mod.monitor(cases, baseline, strategy=key, window=window)
        rows.append({"key": key, "status": meta.status, **result})
    return {
        "as_of": desk.market.latest_bar_date(),
        "strategies": rows,
        "window": window,
        "rule": "decaying when SPRT concludes edge_gone, or CUSUM and rolling agree",
    }


@router.get("/short-alpha/evidence")
def short_alpha_evidence(
    as_of: str | None = None,
    symbols: str | None = None,
    limit: int = Query(default=6, ge=1, le=12),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Kernel evidence only. WorkBuddy owns every candidate/watch judgment."""
    from service.trading.short_alpha import evidence_pack

    desk = get_desk()
    picked = [s.strip() for s in str(symbols or "").split(",") if s.strip()]
    if len(picked) > 12:
        raise HTTPException(422, detail={"code": "TOO_MANY_SYMBOLS", "message": "最多12只"})
    return evidence_pack(desk, _day(as_of, "as_of") or desk.market.latest_bar_date(),
                         symbols=picked or None, limit=limit, offset=offset)


@router.get("/short-alpha/candidates")
def short_alpha_candidates(
    as_of: str | None = None,
    limit: int = Query(default=2, ge=0, le=2),
) -> dict:
    """Information-only 1–2 session net-excess candidate cards; never orders."""
    from service.trading.short_alpha import analyze

    desk = get_desk()
    day = _day(as_of, "as_of") or desk.market.latest_bar_date()
    return analyze(desk, day, limit=limit)


@router.post("/short-alpha/judgments")
def record_short_alpha_judgment(body: ShortAlphaJudgmentBody) -> dict:
    """Freeze WorkBuddy's information-only judgment; never create a trade proposal."""
    from service.trading.short_alpha_tracking import (
        TrackingConflict, record_judgment,
    )

    try:
        result = record_judgment(get_desk(), body.model_dump())
    except TrackingConflict as exc:
        raise HTTPException(409, detail={
            "code": "SHORT_ALPHA_JUDGMENT_FROZEN", "message": str(exc),
            "next": "读取既有冻结记录；不要事后换票或改写理由",
        }) from exc
    except ValueError as exc:
        raise HTTPException(422, detail={"code": "BAD_SHORT_ALPHA_JUDGMENT",
                                         "message": str(exc)}) from exc
    record = result["record"]
    frozen = record["frozen"]
    return {
        "ok": True, "created": result["created"], "day": record["signal_day"],
        "capture_kind": frozen["capture_kind"],
        "judgment_status": record["judgment_status"],
        "candidate_count": sum(i["role"] == "candidate" for i in frozen["items"]),
        "watch_count": sum(i["role"] == "watch" for i in frozen["items"]),
        "frozen_sha256": record["frozen_sha256"],
        "judgment_owner": "WorkBuddy", "kernel_substituted_selection": False,
        "order_created": False, "paper_ledger_modified": False,
    }


@router.post("/short-alpha/reviews")
def record_short_alpha_review(body: ShortAlphaReviewBody) -> dict:
    """Freeze WorkBuddy's dynamic review; this route never creates an order."""
    from service.trading.short_alpha_execution import ExecutionConflict, record_review

    try:
        result = record_review(get_desk(), body.model_dump())
    except ExecutionConflict as exc:
        raise HTTPException(409, detail={
            "code": "SHORT_ALPHA_REVIEW_FROZEN", "message": str(exc),
            "next": "读取既有复评；新判断必须使用新的复评窗口请求号",
        }) from exc
    except ValueError as exc:
        raise HTTPException(422, detail={"code": "BAD_SHORT_ALPHA_REVIEW",
                                         "message": str(exc)}) from exc
    record = result["record"]
    return {
        "ok": True, "created": result["created"], "day": record["signal_day"],
        "review_id": record["review_id"], "captured_at": record["captured_at"],
        "frozen_sha256": record["frozen_sha256"],
        "judgment_owner": "WorkBuddy", "order_created": False,
        "paper_ledger_modified": False,
    }


@router.get("/short-alpha/execution")
def short_alpha_execution(
    day: str | None = Query(default=None, pattern=r"^\d{8}$"),
    limit: int = Query(default=12, ge=0, le=30),
) -> dict:
    """Read-only execution facts; never proposes, approves or modifies either ledger."""
    from service.trading.short_alpha_execution import execution_status

    try:
        return execution_status(get_desk(), day=day, limit=limit)
    except ValueError as exc:
        raise HTTPException(422, detail={"code": "BAD_SHORT_ALPHA_EXECUTION_QUERY",
                                         "message": str(exc)}) from exc


@router.get("/short-alpha/tracking")
def short_alpha_tracking(
    as_of: str | None = None,
    scope: str = Query(default="prospective", pattern="^(prospective|replay|all)$"),
    limit: int = Query(default=12, ge=0, le=30),
) -> dict:
    """Frozen signals and prospective OOS outcomes; read-only, never an order."""
    from service.trading.short_alpha_tracking import TrackingConflict, report

    try:
        return report(get_desk(), scope=scope, limit=limit, as_of=as_of)
    except TrackingConflict as exc:
        raise HTTPException(409, detail={
            "code": "SHORT_ALPHA_TRACKING_CONFLICT", "message": str(exc),
            "next": "保留原快照并检查冻结哈希；不要重写历史判断",
        }) from exc
    except ValueError as exc:
        raise HTTPException(422, detail={"code": "BAD_SHORT_ALPHA_TRACKING_QUERY",
                                         "message": str(exc)}) from exc


@router.get("/knowledge/cards")
def knowledge_cards() -> dict:
    from service.trading import knowledge

    return knowledge.cards(get_desk())


@router.get("/filing/{announcement_id}")
def filing_text(
    announcement_id: str,
    offset: int = Query(default=0, ge=0),
    chars: int = Query(default=4000, ge=400, le=6000),
    fetch: bool = True,
) -> dict:
    """One window of one filing's body text.

    Returns 200 with ``ok: false`` when the text is not available, like the
    knowledge topics do: "正文还没下载" and "这个 id 不存在" are answers about the
    document, and turning them into 4xx makes a tool failure look like a caller
    mistake. The body carries ``need`` so the next step is in the same paste.
    """
    from service.trading import filing

    return filing.read(get_desk(), announcement_id, offset=offset, chars=chars, fetch=fetch)


@router.get("/knowledge/{topic}")
def knowledge_answer(
    topic: str,
    symbol: str | None = None,
    k: int = Query(default=300, ge=50, le=1000),
    same_industry: bool = False,
    as_of: str | None = None,
    date: str | None = None,
    days: int = Query(default=7, ge=1, le=120),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    seat: str | None = None,
    window: int = Query(default=120, ge=60, le=500),
    factor: str | None = None,
    detail: str = Query(default="summary", pattern="^(summary|full)$"),
    horizons: str | None = None,
) -> dict:
    """Ask the kernel one question. Facts and error bars, never a recommendation.

    日期参数收两种拼法：``as_of`` 与 ``date``。**不能只收 as_of**——FastAPI 对
    未知查询参数是静默忽略的，``/knowledge/tape_flow?date=20261103`` 曾经因此
    不报错、直接返回上一交易日的数据，盘中做打板的人会拿昨日的溢价当今天的。
    两个都给且不一致时明确报错，不猜。
    """
    from service.trading import knowledge

    picked_as_of, picked_date = _day(as_of, "as_of"), _day(date, "date")
    if picked_as_of and picked_date and picked_as_of != picked_date:
        raise HTTPException(
            422,
            detail={
                "code": "AMBIGUOUS_DAY",
                "message": f"as_of={picked_as_of} 与 date={picked_date} 不一致，说清要哪一天",
            },
        )
    return knowledge.answer(
        get_desk(),
        topic,
        symbol=symbol,
        k=k,
        same_industry=same_industry,
        as_of=picked_as_of or picked_date,
        days=days,
        limit=limit,
        offset=offset,
        seat=seat,
        window=window,
        factor=factor,
        detail=detail,
        horizons=horizons,
    )


@router.get("/memo")
def list_memos(limit: int = Query(default=20, ge=1, le=200), status: str | None = None) -> dict:
    return {"memos": get_desk().paper.list_memos(limit=limit, status=status)}


@router.post("/memo")
def add_memo(body: MemoBody) -> dict:
    desk = get_desk()
    for pred in body.predictions:
        kind = pred.get("kind", "market_up")
        if kind not in {"market_up", "symbol_up", "core_up"}:
            raise HTTPException(422, detail={"code": "BAD_KIND", "message": kind})
        if not 0.0 <= float(pred.get("probability", 0.5)) <= 1.0:
            raise HTTPException(422, detail={"code": "BAD_PROBABILITY", "message": pred})
    memo_id = desk.paper.add_memo(
        body.topic, body.thesis, body.mechanism, body.falsifier, body.confidence, body.predictions
    )
    desk.paper.log_decision("memo", {"id": memo_id, "topic": body.topic}, actor="agent")
    # Look the row up by id, not by "the newest open memo": a concurrent request
    # can write a second memo in the same second, and `next(...)` over a
    # one-element guess then raises StopIteration — a 500 after the row was
    # already committed, which invites the caller to retry and write it twice.
    written = next((m for m in desk.paper.list_memos(limit=200) if m["id"] == memo_id), None)
    return {"id": memo_id, "memo": written}


@router.post("/memo/score")
def score_memos(dry_run: bool = False) -> dict:
    from service.research.memo import score_memos as run

    desk = get_desk()
    return run(desk.data, desk.paper, dry_run=dry_run)


@router.get("/memo/calibration")
def calibration() -> dict:
    return get_desk().paper.calibration()


# -------------------------------------------------------- free browsing/arena
@router.get("/universe")
def universe(
    as_of: str | None = None,
    industry: str = Query(default="", max_length=64),
    query: str = Query(default="", max_length=64),
    min_amount: float = Query(default=0.0, ge=0.0),
    max_price: float | None = Query(default=None, gt=0),
    limit: int = Query(default=50, ge=1, le=200),
    order: str = Query(default="code", pattern="^(code|amount|move)$"),
) -> dict:
    from service.trading.universe import browse

    desk = get_desk()
    day = _day(as_of, "as_of") or desk.market.last_trading_day()
    return browse(
        desk.data,
        day,
        industry=industry,
        query=query,
        min_amount_cny=min_amount,
        max_price=max_price,
        limit=limit,
        order=order,
    )


@router.get("/arena")
def arena() -> dict:
    from service.trading.arena import scoreboard

    return scoreboard(get_desk())


# --------------------------------------------------------------------- stops
@router.get("/risk/stops")
def risk_stops(
    account: Literal["paper", "external"] = "paper", rule: Literal["pct5", "atr"] = "pct5"
) -> dict:
    """Every open position that carries a stop, and how far it is from it.

    ``account=external`` reads the live book (`data/external.sqlite`) instead of
    the paper one. 2026-11-02: 300 shares at 79% of the account had no stop
    monitored from 13:00 to the close, because this route only ever looked at
    `paper.sqlite` — and answered "没有持仓" for a book it never opened.
    """
    from service.trading.stops import open_stops

    desk = get_desk()
    day = desk.market.latest_bar_date()
    if account == "external":
        from service.trading.external_stops import external_stops

        return external_stops(desk, rule=rule)
    views = [v.as_dict() for v in open_stops(desk, day)]
    return {
        "as_of": day,
        "stops": views,
        "breached": [v["symbol"] for v in views if v["breached"]],
        "blocked_t1": [v["symbol"] for v in views if v["breached"] and v["blocked"]],
        # 空列表是答案，但读者分不清"没有持仓"和"止损没算"。说一句。
        "note": (
            "没有持仓，所以没有止损" if not views else "每条含止损价、距止损的百分比与已跌破标记"
        ),
    }


@router.get("/regime")
def regime() -> dict:
    """One coarse label plus the four numbers behind it."""
    from service.trading.regime import compute

    return _with_units(compute(get_desk().market).as_dict(), "/regime")


@router.get("/risk/exposure")
def risk_exposure() -> dict:
    """Industry/board weights, concentration and beta against the equal-weight market."""
    from service.trading.exposure import describe

    desk = get_desk()
    return _with_units(describe(desk.book, desk.market).as_dict(), "/risk/exposure")


# ------------------------------------------------------------ usage and duty
@router.get("/usage")
def usage(days: int = Query(default=7, ge=1, le=90)) -> dict:
    """Which parts of the desk actually get called, over the last ``days`` days.

    The window applies to every number in the response, ``since`` included so
    the caller does not have to derive the boundary. It used to apply to
    ``by_day`` alone: ``days=1`` returned one day of detail next to a ``total``
    of all recorded history.

    ``db`` names the file these numbers were read from. Counters are written by
    the middleware into the ledger's database; if the two ever disagree,
    ``/health.usage.db`` says where they are actually going — otherwise "no
    calls" reads exactly like "the write path is broken".
    """
    desk = get_desk()
    return {**desk.paper.usage_summary(days=days), "db": str(desk.paper.db_path)}


@router.get("/duty")
def duty() -> dict:
    """The agent work queue: what blocks orders, then an editorial ordering.

    ``blocks`` is the fact (``all_new_entries`` / ``entries_in_subject`` /
    ``None``); ``priority`` is where I put it. Blocking items sort first because
    they do not self-heal, not because they are more important.

    The logic lives in ``service/trading/duty.py`` so the dashboard renders the
    same list: two answers to "what is urgent" would drift apart, and the model
    would read whichever was edited last.
    """
    from service.trading.duty import build

    return build(get_desk())


# ------------------------------------------------------- strategies and money
@router.get("/strategies")
def strategies() -> dict:
    """Which strategies exist, which may run in this regime, and with what weight."""
    from service.trading import strategy as strategy_mod
    from service.trading.allocator import allocate, stats_from_cases
    from service.trading.arena import scoreboard
    from service.trading.regime import compute

    desk = get_desk()
    day = desk.market.latest_bar_date()
    regime = compute(desk.market, day).label
    cases = desk.paper.list_cases(limit=500)
    decisions = scoreboard(desk)["decisions"]
    stats = stats_from_cases(cases, decisions)
    plan = allocate(regime, stats)
    return {
        "as_of": day,
        "regime": regime,
        "strategies": strategy_mod.describe(regime),
        "allocation": plan,
        "stats": {
            k: {
                "n": v.n,
                "mean_excess": (None if v.mean_excess is None else round(v.mean_excess, 5)),
                "max_drawdown": round(v.max_drawdown, 4),
            }
            for k, v in stats.items()
        },
    }


@router.get("/attribution")
def attribution() -> dict:
    """PnL, win rate and market-excess per strategy, and per persona."""
    from service.trading.attribution import by_persona, by_strategy

    desk = get_desk()
    out = by_strategy(desk)
    out["personas"] = by_persona(desk)["personas"]
    return out


class PersonaSwitchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: str = Field(min_length=2, max_length=30)
    reason: str = Field(min_length=20, max_length=800)
    evidence: list[str] = Field(min_length=1, max_length=8)
    expected_edge_bps: float = Field(ge=-500, le=500)
    invalidation: str = Field(min_length=5, max_length=500)
    horizon_days: int | None = Field(default=None, ge=1, le=60)
    regime: str = Field(default="", max_length=20)


@router.get("/persona")
def persona() -> dict:
    """The active decision style, the three available ones, and how switching paid.

    This endpoint reports; it never picks. ``consistent_with_tape`` filters on two
    hard facts only (data readiness, regime fit), and ``scorecard`` settles the
    question "did switching actually make money" against the same equal-weight
    window the arena uses.
    """
    from datetime import datetime as _dt

    from service.kernel.clock import SHANGHAI as _TZ
    from service.trading import persona as persona_mod
    from service.trading.regime import compute as _regime

    desk = get_desk()
    day = desk.market.latest_bar_date()
    regime = _regime(desk.market, day).label
    probe_map = persona_mod.probe(desk.market, desk.registry)
    closed = persona_mod.retired_sleeves()
    eligibility = persona_mod.eligibility(regime, probe_map, retired=closed)
    history = desk.paper.persona_switches(limit=10)
    # 冷调用实测 1963 ms 几乎全在 scorecard：它经 arena.market_for_cases 读
    # 二十年日线。人格/资格这些段每次现算（它们跟盘中的时钟与数据可用性走），
    # 只把纯历史结算结果放进 market 现有的 60 s 缓存——这个缓存带日更失效
    # （cache.market_seconds），不必再引入一套新的 TTL 机制。
    # key 带上切换条数：切一次人格就换一个 key，结算不会停在切换前那一版。
    scorecard = persona_mod.cached_scorecard(desk, day, switches=history)
    return {
        "as_of": day,
        "at": _dt.now(_TZ).isoformat(timespec="seconds"),
        "regime": regime,
        "current": persona_mod.current(desk.paper, regime, probe_map, retired=closed),
        "available": persona_mod.describe(regime, probe_map),
        "eligibility": eligibility,
        # 「按行情切换」要能一句话回答：今天该用哪个。这里给的是三道硬筛后的
        # 候选与理由，不是推荐——选哪个由模型定，选得对不对由 scorecard 结算。
        "today": {
            "regime": regime,
            "can_place_orders": bool(eligibility["consistent_with_tape"]),
            "candidates": eligibility["consistent_with_tape"],
            "why_not_others": {
                "by_data": {row["key"]: row["missing"] for row in eligibility["blocked_by_data"]},
                "by_sleeve": {
                    row["key"]: row["retired"] for row in eligibility["blocked_by_sleeve"]
                },
                "by_regime": {
                    row["key"]: row["regimes"] for row in eligibility["not_suited_to_regime"]
                },
            },
        },
        "probe": probe_map,
        "scorecard": scorecard,
        "history": history,
        "note": (
            "人格决定用哪个 sleeve、持多久、需要什么数据。切换必须带 reason / "
            "evidence / expected_edge_bps / invalidation：没有预期就无法事后结算，"
            "而没有结算的'按收益最大化切换'只是一句口号。"
            "today.candidates 为空表示今天任何人格都下不了单（数据缺、袖全关、"
            "或没有一种风格适合这个 regime）——那不是'今天不适合交易'，"
            "是'这个账户今天不能交易'，两句话的补救办法不同"
        ),
    }


@router.post("/persona/switch")
def persona_switch(body: PersonaSwitchBody) -> dict:
    """Record an explicit change of decision style. Never automatic."""
    from datetime import datetime as _dt

    from service.kernel.clock import SHANGHAI as _TZ
    from service.trading import persona as persona_mod

    desk = get_desk()
    # The switch belongs to the trading session it is meant for, not to the last
    # published bar. A 09:26 decision made before the open must not consume the
    # previous session's two-switch budget.
    today = _dt.now(_TZ).strftime("%Y%m%d")
    cal = desk.market.calendar
    day = today if cal.is_trading_day(today) else (cal.prev(today) or today)
    probe_map = persona_mod.probe(desk.market, desk.registry)
    try:
        return persona_mod.switch(
            desk.paper,
            to=body.to,
            reason=body.reason,
            evidence=body.evidence,
            expected_edge_bps=body.expected_edge_bps,
            invalidation=body.invalidation,
            horizon_days=body.horizon_days,
            regime=body.regime,
            probe_map=probe_map,
            actor="agent",
            nav=float(desk.book.summary()["equity"]),
            day=day,
        )
    except persona_mod.PersonaError as exc:
        desk.paper.log_decision(
            "persona_refused",
            {"code": exc.code, "message": str(exc), "request": body.model_dump()},
            actor="guard",
        )
        raise HTTPException(exc.status, detail={"code": exc.code, "message": str(exc)}) from exc


@router.get("/execution/tca")
def execution_tca(limit: int = Query(default=200, ge=1, le=2000)) -> dict:
    """Post-trade TCA: implementation shortfall per fill, against decision price.

    The other half of ``/execution/estimate``: that one predicts what an order
    will cost, this one measures what it did cost. Both are needed — a model
    calibrated only on predictions never learns whether the prediction was right.
    """
    from service.trading.tca import summary as _summary

    desk = get_desk()
    out = _summary(desk, limit=limit)
    # 滚动汇总必须说自己算到哪一天：同一份 payload 隔一周再读，数字含义完全不同。
    out["as_of"] = desk.market.latest_bar_date()
    return out


@router.get("/execution/estimate")
def execution_estimate(
    symbol: str,
    qty: int = Query(ge=1, le=100_000_000),
    price: float | None = Query(default=None, gt=0),
    side: str = Query(default="BUY", pattern="^(BUY|SELL)$"),
) -> dict:
    """Pre-trade TCA: square-root impact, participation and a TWAP schedule."""
    from datetime import datetime as _dt

    import pandas as _pd

    from service.kernel.clock import SHANGHAI as _TZ
    from service.trading.impact import estimate as _estimate

    desk = get_desk()
    day = desk.market.latest_bar_date()
    snap = desk.market.snapshot(symbol, day)
    if snap is None:
        raise HTTPException(404, detail={"code": "NO_BAR", "message": symbol})
    px = float(price if price is not None else snap.last)
    adv = float(desk.market.adv_map(day).get(symbol, 0.0))
    hist = desk.market.history(symbol, day, 21)
    sigma = 0.0
    if not hist.empty and len(hist) > 2:
        rets = _pd.to_numeric(hist["close"], errors="coerce").pct_change().dropna()
        sigma = float(rets.std())
    est = _estimate(qty, px, adv, sigma)
    from service.trading.impact import round_trip_cost_bps as _round_trip

    instrument = desk.market.instrument(symbol, day)
    notional = float(qty) * px
    fee = _round_trip(notional, instrument)
    combined_bps = float(fee["total_bps"]) + 2.0 * float(est.impact_bps)
    try:
        cash_free = float(desk.book.summary().get("cash_free"))
    except (AttributeError, TypeError, ValueError):
        cash_free = None
    lot_size = int(getattr(instrument, "lot_size", 100) or 100)
    lot_aligned = qty % lot_size == 0
    up_limit = float(snap.up_limit or 0)
    down_limit = float(snap.down_limit or 0)
    queue_required = bool(
        (side == "BUY" and up_limit > 0 and px >= up_limit)
        or (side == "SELL" and down_limit > 0 and px <= down_limit)
    )
    affordable = None if side == "SELL" or cash_free is None else notional <= cash_free
    reasons = []
    if side == "BUY" and affordable is False:
        reasons.append("notional_exceeds_cash_free")
    if not lot_aligned:
        reasons.append("quantity_not_whole_lot")
    if queue_required:
        reasons.append("price_at_market_limit_queue_position_unknown")
    status = ("not_executable_as_requested" if affordable is False or not lot_aligned
              else "queue_required_fill_unknown" if queue_required
              else "liquidity_schedule_only")
    # 价格来源必须写进响应，因为它决定这份结果是"行情"还是"假设"。
    # `schedule` 只按 ADV 拆单；现金、整手、排队与完整费用必须分别可见。
    return {
        "as_of": day,
        "symbol": symbol,
        "side": side,
        "price_source": "caller" if price is not None else "server_last",
        "price_note": (
            "price 由调用方给定，是情景假设，不是行情；引用现价请用 quant_quote"
            if price is not None
            else "price 取自最近一根日线的收盘价，引用现价请用 quant_quote"
        ),
        "server_time": _dt.now(_TZ).isoformat(timespec="seconds"),
        **est.as_dict(),
        "fillability": {
            "status": status,
            "complete": False,
            "schedule_is_fill_proof": False,
            "cash_free": cash_free,
            "notional_cny": round(notional, 2),
            "cash_notional_affordable": affordable,
            "cash_shortfall_cny": (None if side == "SELL" or cash_free is None
                                   else round(max(notional - cash_free, 0.0), 2)),
            "cash_scope": ("notional_only_final_cash_and_fees_checked_by_guard" if side == "BUY"
                           else "not_applicable_for_sell_position_and_t1_checked_by_guard"),
            "position_and_t1_guard_required": side == "SELL",
            "lot_size": lot_size,
            "whole_lot": lot_aligned,
            "queue_required_at_reference_price": queue_required,
            "queue_position_known": False,
            "fill_probability": None,
            "queue_basis": "daily_bar_limit_reference_not_live_order_book",
            "reasons": reasons,
            "final_authority": "proposal_ticket_guard",
        },
        "round_trip_cost": {
            "kernel_fees_taxes_slippage_bps": round(float(fee["total_bps"]), 2),
            "modelled_two_sided_impact_bps": round(2.0 * float(est.impact_bps), 2),
            "combined_estimate_bps": round(combined_bps, 2),
            "combined_estimate_cny": round(notional * combined_bps / 10_000.0, 2),
            "note": "kernel 费用/税费/固定滑点 + 双边模型冲击；不是成交后 TCA。",
        },
        "execution_scope": {
            "pit_safe": False,
            "time_basis": "latest_daily_bar_and_current_book_at_read_time",
            "order_book_depth_available": False,
            "queue_replay_available": False,
            "authorization": "read_only_estimate_no_order_created",
        },
    }
