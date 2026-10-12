"""Funding settlements attached to open-time-labelled bars (one rule for every loader that serves perpetuals).

The engines charge funding at a bar's open, before that bar's fills, for the settlements since the previous bar open.
This module turns settlement events into that per-bar shape so the attribution is defined once. Accounting and signals
are attributed differently: cash by settlement time (``attach_settlements``), the rate a strategy may read by the time the
rate became visible (``attach_visible_rate``).
"""

from __future__ import annotations

import bisect
from decimal import Decimal
from typing import Iterable

import pandas as pd


def _decimal(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def attach_settlements(bars: pd.DataFrame, settlements: Iterable[tuple]) -> pd.DataFrame:
    """Add the funding columns to open-time-labelled bars from settlement events ``(t_ms, rate[, mark])``.

    A settlement at ``t`` belongs to the first bar whose open is at or after ``t`` — the bar at whose open a position held
    over ``t`` is first observed; the engine charges it there, before that bar's fills (a position opened at that open
    was not held at ``t``; one closed at that open was). Settlements after the last bar open are outside the bars and
    are not attributed. Columns:

    * ``funding_events``: per bar, the tuple of ``(t_ms, rate, mark)`` settled in it (exact ``Decimal`` rate; ``mark`` =
      the settlement's published mark price or ``None``); ``CryptoFunding`` books them one by one;
    * ``funding_rate``: the sum of those rates as ``float`` (display and the strict perpetual path);
      ``funding_settlement_time``: the last of them (NaT when none); ``funding_settlements``: how many.

    Rates stay ``Decimal`` until the float summary column; a float input is read through its ``str``.
    """
    out = bars.copy()
    opens = (out.index.astype("int64") // 1_000_000).to_numpy()
    events: list[list] = [[] for _ in range(len(out))]
    for t, r, *rest in sorted(settlements, key=lambda e: e[0]):
        i = bisect.bisect_left(opens, t)
        if i >= len(opens):
            continue
        mark = rest[0] if rest else None
        events[i].append((int(t), _decimal(r), None if mark is None else _decimal(mark)))
    out["funding_rate"] = [float(sum((e[1] for e in ev), Decimal(0))) for ev in events]
    out["funding_settlement_time"] = pd.to_datetime([pd.Timestamp(ev[-1][0], unit="ms") if ev else pd.NaT for ev in events])
    out["funding_settlements"] = [len(ev) for ev in events]
    out["funding_events"] = [tuple(ev) for ev in events]
    return out


def attach_visible_rate(bars: pd.DataFrame, visible: Iterable[tuple[int, int, Decimal]], bar_ms: int) -> pd.DataFrame:
    """Add ``funding_visible_rate``: the rate of the last settlement visible at each bar's close.

    ``visible``: ``(visible_ms, t_ms, rate)`` per settlement, ``visible_ms`` = its ``available_at_ms`` (or settlement time +
    the pack's assumed publication delay). A bar labelled with its open holds what is visible at its close
    (``open + bar_ms - 1``), the rule of the ``alphakeel:`` universe panel; NaN before the first visible settlement. This is
    the column a signal may read; the cash columns of ``attach_settlements`` are for accounting only (a settlement is
    charged at its time, which can be before it is published).
    """
    out = bars.copy()
    ev = sorted((int(v), int(t), _decimal(r)) for v, t, r in visible)
    vis = [e[0] for e in ev]
    closes = (out.index.astype("int64") // 1_000_000).to_numpy() + int(bar_ms) - 1
    col = []
    for c in closes:
        i = bisect.bisect_right(vis, int(c))
        col.append(float(ev[i - 1][2]) if i else float("nan"))
    out["funding_visible_rate"] = col
    return out
