"""Funding settlements attached to open-time-labelled bars (one rule for every loader that serves perpetuals).

The engines charge funding at a bar's open, before that bar's fills, for the settlements since the previous bar open.
This module turns settlement events into that per-bar shape so the attribution is defined once.
"""

from __future__ import annotations

import bisect

import pandas as pd


def attach_settlements(bars: pd.DataFrame, settlements: list[tuple[int, float]]) -> pd.DataFrame:
    """Add the funding columns to open-time-labelled bars from settlement events ``(t_ms, rate)``.

    A settlement at ``t`` belongs to the first bar whose open is at or after ``t`` — the bar at whose open a position held
    over ``t`` is first observed; the engine charges it there, before that bar's fills (a position opened at that open
    was not held at ``t``; one closed at that open was). Settlements after the last bar open are outside the bars and
    are not attributed. Rates of several settlements inside one bar are summed (``funding_settlements`` says how many).
    """
    out = bars.copy()
    opens = (out.index.astype("int64") // 1_000_000).to_numpy()
    rate = [0.0] * len(out)
    count = [0] * len(out)
    last: list = [pd.NaT] * len(out)
    for t, r in sorted(settlements):
        i = bisect.bisect_left(opens, t)
        if i >= len(opens):
            continue
        rate[i] += r
        count[i] += 1
        last[i] = pd.Timestamp(t, unit="ms")
    out["funding_rate"] = rate
    out["funding_settlement_time"] = pd.to_datetime(last)
    out["funding_settlements"] = count
    return out
