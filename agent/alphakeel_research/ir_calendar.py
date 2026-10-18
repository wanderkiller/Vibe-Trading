"""NYSE regular-session calendar for the dataset view's equity leg (design 2026-10-18 section 4.3).

The equity leg of an ``equity_perp`` frame exists only in the signal window of a NYSE trading day: 10:00 ET up to 30 minutes
before the close, both ends inclusive (13:00 early close -> 12:30). Weekends, the holidays below and the early-close days are the
tables of the paper monitor ``stockperp-paper/common.py`` (HOLIDAYS, EARLY_CLOSE), copied as data; the tables end at 2027-03-26
and a day outside them is treated as an ordinary weekday (the paper monitor does the same).

Standard library only (``zoneinfo``; the zone database must be present on the machine).
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
OPEN_MINUTES = 10 * 60          # first minute of the signal window (10:00 ET)
CLOSE_GUARD_MINUTES = 30        # the window ends this long before the close

HOLIDAYS = {dt.date(*x) for x in [
    (2024, 11, 28), (2024, 12, 25),
    (2025, 1, 1), (2025, 1, 9), (2025, 1, 20), (2025, 2, 17), (2025, 4, 18), (2025, 5, 26), (2025, 6, 19),
    (2025, 7, 4), (2025, 9, 1), (2025, 11, 27), (2025, 12, 25),
    (2026, 1, 1), (2026, 1, 19), (2026, 2, 16), (2026, 4, 3), (2026, 5, 25), (2026, 6, 19),
    (2026, 7, 3), (2026, 9, 7), (2026, 11, 26), (2026, 12, 25),
    (2027, 1, 1), (2027, 1, 18), (2027, 2, 15), (2027, 3, 26)]}
EARLY_CLOSE = {dt.date(*x): 13 for x in [(2024, 11, 29), (2024, 12, 24), (2025, 7, 3), (2025, 11, 28), (2025, 12, 24),
                                         (2026, 11, 27), (2026, 12, 24)]}


def is_trading_day(d: dt.date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def close_minutes(d: dt.date) -> int | None:
    """The close, minutes after midnight ET (16:00, or 13:00 on an early-close day); None on a non-trading day."""
    if not is_trading_day(d):
        return None
    return EARLY_CLOSE.get(d, 16) * 60


def _et_ms(d: dt.date, minutes: int) -> int:
    return int(dt.datetime(d.year, d.month, d.day, minutes // 60, minutes % 60, tzinfo=ET).astimezone(UTC).timestamp() * 1000)


def et_date(ms: int) -> dt.date:
    return dt.datetime.fromtimestamp(ms / 1000, UTC).astimezone(ET).date()


def window_bounds(d: dt.date) -> tuple[int, int] | None:
    """``(first_ms, last_ms)`` of the signal window of ET day ``d`` (both inclusive), or None on a non-trading day."""
    c = close_minutes(d)
    if c is None:
        return None
    return _et_ms(d, OPEN_MINUTES), _et_ms(d, c - CLOSE_GUARD_MINUTES)


def in_window(ms: int) -> bool:
    """``ms`` lies in the signal window of its ET day."""
    w = window_bounds(et_date(ms))
    return w is not None and w[0] <= ms <= w[1]
