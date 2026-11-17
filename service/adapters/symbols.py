"""Which exchange a six-digit A-share code belongs to.

Three copies of this rule used to live in three adapters, each slightly wrong in
a different way, and the errors were invisible until a quote came back empty:

    eastmoney  _market_of:  1 if code[0]=="6" or code.startswith(("5","9")) else 0
    tencent    _prefix:     "sh" if code[0] in "659" else "sz"
    realtime   _symbol_of:  "11" -> .SH            (this one was right)

Neither of the first two has a branch for convertibles. An SSE convertible
(110xxx / 111xxx / 113xxx / 118xxx) therefore went out as ``sz111012`` and
``0.111012`` — both nonexistent — and the desk concluded "no quote path for
Shanghai convertibles" when the vendor had been answering all along. The
prefix is a property of the security, not of the adapter, so it lives here once.

Kept deliberately explicit rather than clever: the cost of a wrong digit is a
silent empty result, and an explicit list of what is Shanghai is auditable.
"""

from __future__ import annotations

from decimal import Decimal

from service.kernel.units import (
    LIMIT_PCT_BSE,
    LIMIT_PCT_CB,
    LIMIT_PCT_GROWTH,
    LIMIT_PCT_MAIN,
    LIMIT_PCT_ST,
    LIMIT_PCT_ST_LEGACY,
    LIMIT_PCT_UNREFORMED,
    ST_BAND_CHANGE_DATE,
)

# Shanghai: main board 60x, STAR 68x, funds/ETFs 5xx, B shares 90x,
# convertibles 110/111/113/118, exchangeable bonds 132/145.
SH_PREFIXES = ("6", "5", "9", "110", "111", "113", "118", "132", "145")

# Beijing: 4xx/8xx, plus the 920 range that replaced some NEEQ codes.
BJ_PREFIXES = ("4", "8", "920")

# Three-digit special cases decide the exchange before the broader prefix rules.
# In particular 920xxx is Beijing while 900xxx is a Shanghai B share.
_EXCHANGE_BY_PREFIX = {
    "110": "SH", "111": "SH", "113": "SH", "118": "SH", "132": "SH",
    "920": "BJ",
}


def clean_code(code: str) -> str:
    """Six digits, no suffix. Accepts both ``600000`` and ``600000.SH``."""
    return str(code).split(".")[0].strip().zfill(6)


def exchange_of(code: str) -> str:
    """``"SH"`` / ``"SZ"`` / ``"BJ"`` for one code. Unknown tails default to SZ.

    Beijing is tested first because 920xxx shares the leading ``9`` with the
    Shanghai B-share range (900xxx); testing SH first sends a Beijing code to
    Shanghai, which is the same class of silent-empty-result bug that hid the
    convertible channel.
    """
    text = str(code).strip()
    c = text if len(text) == 6 and text.isdigit() else clean_code(text)
    special = _EXCHANGE_BY_PREFIX.get(c[:3])
    if special is not None:
        return special
    if c.startswith(BJ_PREFIXES):
        return "BJ"
    if c.startswith(SH_PREFIXES):
        return "SH"
    return "SZ"


def ts_code(code: str) -> str:
    """``600000`` -> ``600000.SH``."""
    c = clean_code(code)
    return f"{c}.{exchange_of(c)}"


def tencent_symbol(code: str) -> str:
    """The prefix ``qt.gtimg.cn`` expects: ``sh600000`` / ``sz000001`` / ``bj430047``."""
    c = clean_code(code)
    return f"{exchange_of(c).lower()}{c}"


def eastmoney_market(code: str) -> int:
    """EastMoney ``secid`` market number: 1 = Shanghai, 0 = Shenzhen/Beijing."""
    return 1 if exchange_of(code) == "SH" else 0


# Shanghai/Shenzhen A-share prefixes. The THS snapshot endpoint is an *A-share*
# endpoint (`/api/a-share/prices/snapshot`); asking it for an ETF or a convertible
# does not return an error, it returns nothing — after paying the round trip.
A_SHARE_PREFIXES = ("60", "68", "00", "30", "43", "83", "87", "88", "92")

# Convertible and exchangeable bonds, both exchanges. The list is not a guess:
# it is every prefix that appears in the security master (`cb_basic`, 1,178 rows
# from 1998 to 2026), checked prefix-by-prefix against that table's `exchange`
# column, and `test_every_bond_in_the_master_is_recognised` fails if the vendor
# ever issues a code outside it.
#
# 100/110/111/113/118/132/145 are Shanghai; 117/120/121/123/124/125/126/127/128/129
# are Shenzhen. Kept as one tuple because the *trading rules* are identical on
# both exchanges — 10 bonds a lot, T+0, no stamp duty — and splitting it would
# invite a caller to re-derive the rule per exchange, which is the mistake
# `exchange_of` exists to prevent.
CB_PREFIXES = ("100", "110", "111", "113", "117", "118", "120", "121", "123",
               "124", "125", "126", "127", "128", "129", "132", "145")


def is_a_share(code: str) -> bool:
    """True for ordinary A-shares; False for ETFs, convertibles and B shares."""
    return clean_code(code).startswith(A_SHARE_PREFIXES)


def is_convertible(code: str) -> bool:
    """True for convertible / exchangeable bonds.

    This is the security's identity, not the adapter's: a convertible is quoted
    in 10-bond lots, settles T+0 and pays no stamp duty, on both exchanges. The
    trading desk used to skip this branch entirely, so a bond inherited the
    main-board stock rules — 100-bond lots, T+1, 5bps stamp duty — and the one
    structure that survived the referee (`cb_link_down`) could not be expressed
    as a ticket at all.
    """
    return clean_code(code).startswith(CB_PREFIXES)


# ---------------------------------------------------------------- board
#
# Same argument as ``exchange_of`` and ``limit_pct``: which board a code sits on
# decides four trading rules (band, lot, tick, settlement), so it is a property
# of the security. It had two homes — ``market.instrument`` derived it from three
# prefix tuples for pricing, and ``universe.browse`` decided it from the ``.BJ``
# suffix for display — and the display copy was wrong in the direction that
# matters least visibly: every 沪深 name came back ``board: null``.
#
# Ranges as of 2026-11 (verified 2026-11-01 against 上交所/深交所/北交所 code
# rules): 沪主板 600/601/603/605, 科创板 688/689, 深主板 000/001/002/003,
# 创业板 300/301, 北交所 43/83/87/88 plus 920 which replaces them gradually.
STAR_PREFIXES = ("688", "689")
CHINEXT_PREFIXES = ("300", "301")
BSE_PREFIXES = ("4", "8", "92")
ETF_PREFIXES = ("51", "56", "58", "15", "16", "18")


def board_of(code: str) -> str:
    """``MAIN`` / ``CHINEXT`` / ``STAR`` / ``BSE`` / ``ETF`` / ``CB``.

    The strings are exactly ``kernel.models.Board``'s values, and a test pins
    that — two vocabularies for one fact is how a reader ends up comparing
    ``"BSE"`` against ``Board.BSE`` and getting False.
    """
    c = clean_code(code)
    if c.startswith(CB_PREFIXES):
        return "CB"
    if c[:2] in ETF_PREFIXES:
        return "ETF"
    if c.startswith(STAR_PREFIXES):
        return "STAR"
    if c.startswith(CHINEXT_PREFIXES):
        return "CHINEXT"
    if c.startswith(BSE_PREFIXES):
        return "BSE"
    return "MAIN"
# The daily price band, as a fraction of the previous close. This is a property
# of the security (and of its ST status), not of whichever module happens to need
# it, so it lives beside `exchange_of` for the same reason.
#
# It had drifted into four copies. `market.instrument` had the right rule,
# `external.limit_pct` was missing the 920xxx Beijing range and applied the 5%
# ST band to ChiNext/STAR names (which keep 20%), and `realtime._limits` derived
# it correctly but rounded it half-to-even. A wrong band is not cosmetic: it
# decides whether `blocked_at_open` refuses a limit-down fill and whether the
# monitor shouts "跌停" at a stock that is merely down 5%.
#
# The band is also a *rule that expires*: the main-board ST band was 5% until
# 2026-07-06 and has been 10% since (see ``kernel.units``). This function is
# checked against the exchange's own numbers in
# ``tests/kernel/test_limit_pct_rules.py``, so the next rule change surfaces
# there rather than in a wrong alert.
_BAND_STAR_CHINEXT = ("688", "689", "300", "301")
_BAND_BSE = ("4", "8", "920")


def is_unreformed(name: str | None) -> bool:
    """Is this name a 未完成股权分置改革 (S-prefixed) listing?

    The exchange marks those with a leading "S" in the short name (S佳通,
    S湘火炬, SST佳通, S*ST佳通) and caps them at ±5% regardless of anything else
    — even after the ST label is removed (佳通轮胎 2023-05-16 公告明写"日涨跌幅
    限制不变，仍为 5%").

    That rule outranks the risk-warning band, so this check has to come first:
    a name can be S *and* ST, and then it is 5%, not 10%.

    The one trap is that an ordinary risk-warning name also starts with "S" —
    "ST椰岛" is an S, not an unreformed marker. So the test is "starts with S
    **and** not with ST". The unreformed forms are S+中文 ("S佳通"), S+S+中文
    ("SST佳通") and S+*ST ("S*ST佳通"); the ST forms are "ST*" and "*ST*".
    """
    text = (name or "").strip()
    if not text.startswith("S"):
        return False
    return not text.startswith("ST")


def limit_pct(
    code: str,
    *,
    is_st: bool = False,
    name: str | None = None,
    as_of: str | None = None,
) -> Decimal:
    """Daily price band for ``code``.

    Main board and risk-warning shares are both 10% since 2026-07-06 (the
    exchange aligned main-board ST with ordinary main-board stocks; before that
    date ST was 5%, and ``as_of`` decides which rule applies). ChiNext and STAR
    keep 20% whether or not the name is flagged, which is why ``is_st`` cannot be
    tested first — the board decides. Unreformed (S-prefixed) listings are 5%
    whatever else they are, which is why ``name`` is checked before ``is_st``.

    A convertible's 20% is the band for an *existing* bond; listing day prints a
    wider one and this desk does not trade listing day.

    ``as_of`` is a YYYYMMDD string. ``None`` means "now" — the current rule —
    which is what every live path wants. A historical query must pass the day it
    is asking about, or it explains yesterday's tape with today's rules.

    The magnitudes live in ``kernel.units`` (LIMIT_PCT_*) because they are
    exchange rules; this function only decides which one applies, which needs the
    code prefixes and the short name.
    """
    c = clean_code(code)
    if c.startswith(CB_PREFIXES):
        return LIMIT_PCT_CB
    if c.startswith(_BAND_BSE):
        return LIMIT_PCT_BSE
    # ChiNext and STAR keep their 20% band after a name is flagged, which is
    # why `is_st` cannot be tested first.
    if c.startswith(_BAND_STAR_CHINEXT):
        return LIMIT_PCT_GROWTH
    # Unreformed listings keep 5% even while they also carry an ST label.
    if is_unreformed(name):
        return LIMIT_PCT_UNREFORMED
    if is_st:
        day = (as_of or "").replace("-", "")
        if day and day < ST_BAND_CHANGE_DATE:
            return LIMIT_PCT_ST_LEGACY
        return LIMIT_PCT_ST
    return LIMIT_PCT_MAIN
