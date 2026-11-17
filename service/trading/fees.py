"""A股的费用：佣金、印花税、过户费。

为什么要算，而不是让人填一个数：一天两三笔的时候"大概五块"看不出来，攒到月底
成本价会悄悄偏掉，而成本价正是止损锚。填错 5 元，止损就在错的位置上站一个月。

费率写在 ``configs/costs.yaml``，因为它是账户属性不是代码属性——换券商只改这一处。
读值走 ``service.settings``，键名 ``costs.*``；本文件不再保留默认值副本。此前
这里有第二份 DEFAULTS、``kernel.costs.CostModel`` 里第三份、``Instrument`` 字段
里第四份，改一次费率要改四个地方——其中三个是静默生效的。

这一段只负责"读配置 + 拆明细"，算法在 ``kernel.costs.CostModel`` 里。
"""

from __future__ import annotations

import math
from typing import Any

from service import settings
from service.kernel.costs import CostModel
from service.kernel.models import D, Instrument, money
from service.kernel.units import BPS

KEYS = ("commission_rate", "min_commission", "stamp_duty_sell", "transfer_fee")


def _checked_config(config: dict) -> dict[str, float]:
    """Overlay a caller's schedule on the defaults, then validate every value.

    ``config[key]`` used to be a hard index, so the most natural call — "my
    broker charges 0.02% commission now" — raised ``KeyError: min_commission``
    on the booking path. A partially supplied schedule is a normal thing to
    want; an incomplete one is not an error as long as the rest is explicit.
    """
    base = _config()
    values = {}
    for key in KEYS:
        value = float(config.get(key, base[key]))
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{key} must be finite and non-negative")
        values[key] = value
    return values


def _config() -> dict[str, float]:
    """The account's schedule, straight from configs/costs.yaml.

    No fallback values here on purpose: a missing or malformed rate is a
    configuration error, and booking a fill at a guessed commission is worse
    than refusing to book it.
    """
    return {key: settings.num(f"costs.{key}") for key in KEYS}


def schedule() -> dict[str, float]:
    """The account's fee schedule, for display and for tests.

    Public because "which rates is this desk actually using" is a question the
    panel and /health both need to answer, and reconstructing it from a
    ``rate_note`` string is how the answer drifts from the truth.
    """
    return _config()


def _model(cfg: dict) -> CostModel:
    return CostModel(commission_rate=D(str(cfg["commission_rate"])),
                     min_commission=D(str(cfg["min_commission"])))


def _instrument(cfg: dict) -> Instrument:
    return Instrument(symbol="", name="股票",
                      stamp_duty=D(str(cfg["stamp_duty_sell"])),
                      transfer_fee=D(str(cfg["transfer_fee"])))


def compute(side: str, price: float, qty: int, *, config: dict | None = None,
            instrument: Instrument | None = None) -> dict[str, Any]:
    """Fees for one fill, itemised so the reader can check each line.

    Commission is the only line with a floor; stamp duty is sell-side only
    (0.05% since 2023-08-28); transfer fee applies to both sides.

    ``config`` and ``instrument`` are mutually exclusive, and passing both is
    an error rather than a silent precedence: the instrument owns the tax /
    transfer rates, so a supplied instrument makes the ``stamp_duty_sell`` /
    ``transfer_fee`` keys of ``config`` dead while ``rate_note`` still quotes
    the commission from ``config`` — two halves of two different schedules in
    one answer. Pass ``instrument`` for anything that is not a stock (an ETF
    pays neither, expressed by zero rates rather than a branch in the model),
    or ``config`` to override the account's schedule; never both.
    """
    if config is not None and instrument is not None:
        raise ValueError("pass either config or instrument, not both")
    cfg = _checked_config(config) if config is not None else _config()
    inst = instrument or _instrument(cfg)
    model = _model(cfg)
    notional = money(D(str(price)) * D(int(qty)))
    selling = str(side).upper() == "SELL"

    total = (model.sell_fees(notional, inst) if selling
             else model.buy_fees(notional, inst))
    commission = model.commission(notional)
    transfer = money(notional * inst.transfer_fee) if inst.transfer_fee else D("0")
    # Read the rate itself, not ``total - commission - transfer``. Subtraction
    # silently attributes any fourth line CostModel ever grows (a per-share levy,
    # an exchange fee) to stamp duty, and it only works while sell_fees happens
    # to be exactly three terms. With the current three-term model the two forms
    # are cent-for-cent identical; this one stays correct when the model grows.
    stamp = money(notional * inst.stamp_duty) if selling else D("0")

    return {"notional": float(notional), "commission": float(commission),
            "stamp_duty": float(stamp), "transfer_fee": float(transfer),
            "total": float(total),
            "rate_note": (f"佣金 {cfg['commission_rate'] * float(BPS):.2f}‱"
                          f"（最低 {cfg['min_commission']:.0f} 元）"
                          + (f"，印花税 {float(inst.stamp_duty) * 100:g}%" if stamp else "")
                          + f"，过户费 {float(inst.transfer_fee) * 100:g}%")}


def describe(fees: dict) -> str:
    bits = [f"佣金 {fees['commission']:.2f}"]
    if fees.get("stamp_duty"):
        bits.append(f"印花税 {fees['stamp_duty']:.2f}")
    if fees.get("transfer_fee"):
        bits.append(f"过户费 {fees['transfer_fee']:.2f}")
    return " + ".join(bits) + f" = {fees['total']:.2f}"
