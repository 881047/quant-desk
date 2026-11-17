"""Canonical research specs.

These live in the library, not in a script, because two callers need the same
definition: the CLI that runs a hypothesis, and the re-costing pass that re-judges
history. When the preset lived only in the script, `scripts/recost.py` had to
reconstruct specs from saved verdicts — and those verdicts were lossy, so a
"re-run" of 正股跌停→转债 silently became 正股涨停→转债 (``link_pool`` fell back
to its default). A spec that cannot be reproduced is not a record of anything.
"""

from __future__ import annotations

PRESETS: dict[str, dict] = {
    "limit_up_first": {"pool": "limit_up", "boards": [1], "entry": "next_open", "hold": 1},
    "limit_up_second": {"pool": "limit_up", "boards": [2], "entry": "next_open", "hold": 1},
    "limit_up_all": {"pool": "limit_up", "boards": [1, 2, 3], "entry": "next_open", "hold": 1},
    "broken_board": {"pool": "broken_board", "entry": "next_open", "hold": 1},
    "broken_board_deepest": {"pool": "broken_board", "entry": "next_open", "hold": 1,
                             "max_per_day": 1, "rank_by": "close_to_limit", "rank_dir": "asc"},
    # T+0 convertible bonds: the only legal home for the intraday reversal.
    "cb_reversal": {"pool": "cb", "entry": "next_open", "hold": 0,
                    "max_per_day": 1, "rank_by": "pct_chg", "rank_dir": "asc"},
    "cb_reversal_all": {"pool": "cb", "entry": "next_open", "hold": 0},
    # Stock limit event, bond as the instrument (T+0).
    "cb_link_up": {"pool": "cb_link", "link_pool": "limit_up", "entry": "next_open",
                   "hold": 0, "max_per_day": 1, "rank_by": "cb_ret", "rank_dir": "desc"},
    "cb_link_down": {"pool": "cb_link", "link_pool": "limit_down", "entry": "next_open",
                     "hold": 0, "max_per_day": 1, "rank_by": "cb_ret", "rank_dir": "asc"},
    "cb_link_down_rich": {"pool": "cb_link", "link_pool": "limit_down", "entry": "next_open",
                          "hold": 0, "max_per_day": 1, "rank_by": "premium",
                          "rank_dir": "desc"},
}
