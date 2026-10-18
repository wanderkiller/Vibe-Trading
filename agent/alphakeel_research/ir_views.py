"""Strategy IR decision views from a point-in-time pack reader (README §4.1a of contract/strategy-ir).

``scan_frame_pairs`` enumerates the candidate pairs of ONE scan frame from the raw quote, observation, instrument and fee
tables of the pack -- never from screener opportunities (those were cut by scan thresholds and ``top_n``). The result is
the ``frame.pairs`` input of :func:`alphakeel_research.ir.decide`: a list of PairView documents (JSON values, decimal
strings) sorted by ``id``.

Pure apart from reading the ``Pit``; standard library only (imported inside the policy sandbox).
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import ROUND_HALF_EVEN, Context, Decimal
from typing import Any

from .errors import ApiError
from .ir import VENUES, fmt

PairView = dict  # README §4.1 PairView as a JSON document


def _s(d: Any) -> str | None:
    return None if d is None else fmt(d)


def _fee(fees: dict, venue: str, market: str) -> str | None:
    table = fees.get("fees") if isinstance(fees, dict) else None
    row = table.get(venue) if isinstance(table, dict) else None
    v = row.get(market) if isinstance(row, dict) else None
    if v is None or v == "":
        return None
    return fmt(Decimal(str(v)))


def frame_legs(pit, frame_index: int | None, now_ms: int) -> list[dict]:
    """The legs of one scan frame (README §4.1a rules 1–3), each as a LegView plus ``base``.

    A leg is a ``selected`` contract with a quote row in this frame (``quotes.frame == frame_index``; with
    ``frame_index=None`` the frame is identified by its decision time, ``quote.t == now_ms``). Funding fields come from
    the observation row of the SAME frame; a perp without one has them missing. A leg without a taker fee in the pack's
    fee table, or on a venue outside the IR venue list, is left out.

    Note: the ``Pit`` hides a quote whose exchange time ``quote_ts`` is after ``as_of`` (= ``now_ms``), so such a
    contract is not in the frame here (README §4.2 would clamp its age at 0 instead).

    Besides ``base`` and ``view`` each leg carries what README §5a's tradability rules read and the view does not hold:
    ``mark`` (the quote row's mark, else the mid) and ``quote_time`` (``quote_ts``, else the venue's fetch time in this
    frame -- the age AlphaKeel's engine and ledger judge staleness by; the view's ``quote_ms`` stays ``quote_ts``).
    """
    fees = pit.fees()
    legs = []
    for inst, meta in sorted(pit.instruments().items(), key=lambda kv: (kv[0].venue, kv[0].market, kv[0].symbol)):
        if not meta.selected or inst.venue not in VENUES or inst.market not in ("perp", "spot"):
            continue
        fee = _fee(fees, inst.venue, inst.market)
        if fee is None:
            continue
        rows = pit.quotes(inst, now_ms, now_ms + 1)
        q = next((r for r in reversed(rows) if (r.frame == frame_index if frame_index is not None else r.t == now_ms)), None)
        if q is None:
            continue
        o = None
        if inst.market == "perp":
            obs = pit.observations(inst, now_ms, now_ms + 1)
            o = next((r for r in reversed(obs) if r.frame == q.frame), None)
        fpx = o.funding_px if o is not None and o.funding_px is not None else q.mark
        mark = q.mark if q.mark is not None else (q.bid + q.ask) / 2
        legs.append({
            "base": meta.base,
            "mark": fmt(mark),
            "quote_time": q.quote_ts if q.quote_ts is not None else pit.venue_fetch_ms(inst.venue),
            "view": {
                "venue": inst.venue, "symbol": inst.symbol, "market": inst.market, "quote_ccy": meta.quote,
                "bid": fmt(q.bid), "ask": fmt(q.ask), "bbo": bool(q.bbo),
                "funding_rate": _s(o.funding_rate) if o is not None else None,
                "interval_hours": o.interval_hours if o is not None else None,
                "next_funding_ms": o.next_funding_ms if o is not None else None,
                "funding_px": _s(fpx),
                "volume_quote": _s(q.volume_quote),
                "taker_fee": fee,
                "quote_ms": q.quote_ts,
            },
        })
    return legs


def pairs_of_legs(legs: list[dict]) -> list[PairView]:
    """README §4.1a rules 4–5: ordered cross-venue perp pairs, same-venue (spot long, perp short) pairs and (2026-10-18)
    ordered cross-venue (equity, perp) and (perp, equity) pairs, by id."""
    out = []
    for a in legs:
        for b in legs:
            la, sb = a["view"], b["view"]
            if a["base"] != b["base"] or la["quote_ccy"] != sb["quote_ccy"]:
                continue
            ma, mb = la["market"], sb["market"]
            if ma == "perp" and mb == "perp" and la["venue"] != sb["venue"]:
                kind, tag = "cross_perp", "cross"
            elif ma == "spot" and mb == "perp" and la["venue"] == sb["venue"]:
                kind, tag = "spot_perp", "spot"
            elif (ma, mb) in (("equity", "perp"), ("perp", "equity")) and la["venue"] != sb["venue"]:
                kind, tag = "equity_perp", "equity"
            else:
                continue
            pid = f"{tag}:{a['base']}:{la['venue']}:{la['symbol']}>{sb['venue']}:{sb['symbol']}"
            out.append({"id": pid, "base": a["base"], "kind": kind, "long": dict(la), "short": dict(sb)})
    out.sort(key=lambda p: p["id"])
    return out


def scan_frame_pairs(pit, frame_index: int | None, now_ms: int) -> list[PairView]:
    """All candidate pairs of the frame decided at ``now_ms`` (README §4.1a), sorted by ``id``."""
    return pairs_of_legs(frame_legs(pit, frame_index, now_ms))


def touch(legs: list[dict]) -> dict[tuple[str, str, str], tuple[str, str]]:
    """``(venue, market, symbol) -> (bid, ask)`` of this frame's legs (for pricing orders at the touch)."""
    return {(x["view"]["venue"], x["view"]["market"], x["view"]["symbol"]): (x["view"]["bid"], x["view"]["ask"]) for x in legs}


# ---------------------------------------------------------------------------------------------------------------------
# README §5a execution rules shared by ir_policy (scan frames) and ir_backtest (dataset frames)
# ---------------------------------------------------------------------------------------------------------------------

QTY_DP = 8
#: the engine's fill-side future tolerance (execution profile ``quotes.fill_future_tolerance_ms``; AlphaKeel
#: ``screener::score::QUOTE_FUTURE_TOLERANCE_MS``): a quote time further ahead than this is not usable
FUTURE_TOLERANCE_MS = 5_000


def key_of(x) -> tuple[str, str, str]:
    v = x["view"]
    return (v["venue"], v["market"], v["symbol"])


def valid(x) -> bool:
    """README §5a: a leg whose quote can be traded on at all -- a real top of book (``bbo``) and sane numbers: bid > 0,
    ask >= bid, mark > 0, funding price > 0 (the view's ``funding_px``, else the mark) and (perp) |funding_rate| <= 1
    (AlphaKeel ``paper::open_planned`` / ``obs_valid``; the funding price is the mark AlphaKeel's engine takes a quote by,
    so a leg with ``funding_px <= 0`` is not a quote either side trades or values on). An entry with a leg that is not
    valid submits nothing and takes no trade number."""
    v = x["view"]
    if not v["bbo"]:
        return False
    bid, ask = Decimal(v["bid"]), Decimal(v["ask"])
    mark = Decimal(x["mark"]) if x.get("mark") is not None else (bid + ask) / 2
    fpx = Decimal(v["funding_px"]) if v.get("funding_px") is not None else mark
    if not (bid > 0 and ask >= bid and mark > 0 and fpx > 0):
        return False
    return v.get("funding_rate") is None or abs(Decimal(v["funding_rate"])) <= 1


def snap_ok(x) -> bool:
    """AlphaKeel ``ab_backtest::snap_of``: a quote the engine takes into its frame (and ``MarkRounder.observe`` counts) --
    ``bbo``, bid > 0, ask >= bid and funding price > 0 (the view's ``funding_px``, else the mark). Unlike :func:`valid` it
    does not judge the funding rate. Freshness is the frame's own (dataset view: the 5-minute bar age, README §5a)."""
    v = x["view"]
    if not v["bbo"]:
        return False
    bid, ask = Decimal(v["bid"]), Decimal(v["ask"])
    mark = Decimal(x["mark"]) if x.get("mark") is not None else (bid + ask) / 2
    fpx = Decimal(v["funding_px"]) if v.get("funding_px") is not None else mark
    return bid > 0 and ask >= bid and fpx > 0


def fresh(x, now_ms: int, max_age_ms: int) -> bool:
    """README §5a: the leg's quote time (``quote_ts``, else the venue's fetch time) is at most ``max_age_ms`` old and at
    most ``FUTURE_TOLERANCE_MS`` ahead of ``now_ms``; an unknown quote time is not judged (AlphaKeel ``obs_fresh``)."""
    t = x.get("quote_time")
    return t is None or (t <= now_ms + FUTURE_TOLERANCE_MS and now_ms - t <= max_age_ms)


def tradable(legs, now_ms: int | None = None, max_age_ms: int | None = None):
    """``(venue, market, symbol)`` of this frame's legs with a tradable quote (README §5a): :func:`valid` and, when
    ``max_age_ms`` is given, :func:`fresh`. A held pair exits only when both its legs are in this set."""
    return {key_of(x) for x in legs if valid(x) and (max_age_ms is None or fresh(x, now_ms, max_age_ms))}


def entry_qty(notional_per_leg, long_ask, qty_dp=QTY_DP):
    """README §5a: ``notional_per_leg / long.ask`` rounded half-even to ``qty_dp`` places (``Decimal``; may be 0)."""
    return (Decimal(notional_per_leg) / Decimal(long_ask)).quantize(Decimal(1).scaleb(-qty_dp), rounding=ROUND_HALF_EVEN)


def exit_quoted(p, quoted):
    """Both legs of held pair ``p`` have a tradable quote in this frame (``quoted`` from :func:`tradable`)."""
    return all((p[leg]["venue"], p[leg]["market"], p[leg]["symbol"]) in quoted for leg in ("long", "short"))


# ---------------------------------------------------------------------------------------------------------------------
# Dataset view: long-history research frames from a DATASET pack (1m klines + official settlements; no scan frames)
# ---------------------------------------------------------------------------------------------------------------------
#
# The scan archive (quotes with a real top of book, predicted funding) starts 2026-10; AlphaKeel's datasets reach back to
# 2019 but hold 1-minute trade klines and official funding settlements only. ``dataset_frame_pairs`` builds the §4.1
# PairView frame from those with these APPROXIMATIONS (recorded in every run card as ``view`` + ``approximations``):
#
# * touch: ``bid = ask = close`` of the last 1m bar closed at ``now`` (``close_time_ms <= now``), and ``bbo = True`` is
#   SYNTHETIC -- there is no order book in the data; spread is 0 and ``price_estimated`` is false by construction;
# * ``mark`` / ``funding_px`` = that close (no mark/index series);
# * ``funding_rate`` = the last OFFICIAL settlement visible at ``now`` (``available_at`` -- or settlement time + the lock's
#   assumed delay -- ``<= now``): a ``last_settled`` proxy for the venues' predicted rate a live scan would show. The IR's
#   ``assumptions.funding_estimate`` is NOT changed (it stays ``predicted``); this is the view's approximation, stated as such;
# * ``interval_hours`` from that settlement's ``interval_seconds``; when the dataset leaves it null (Binance, OKX, Bybit,
#   Bitget, Gate and Aster history never state it), INFERRED point-in-time from the gap between the two latest visible
#   settlements, accepted only when it is 1, 2, 4 or 8 h within 60 s (``INFERABLE_INTERVALS``; timestamps jitter by ms). A perp with neither is left
#   out. After a venue changes a contract's interval the inference lags one settlement. ``next_funding_ms`` = settlement
#   time + interval;
# * ``volume_quote`` = sum of ``volume_quote`` over the 1440 one-minute bars of the 24 h ending with the last closed bar
#   (``None`` unless all 1440 are present);
# * ``quote_ms`` = the bar's ``close_time_ms``; a leg whose last closed bar is older than ``DATASET_MAX_BAR_AGE_MS`` is not in
#   the frame (old quotes are not carried over, as §4.1a rule 1 says for scan frames);
# * ``taker_fee`` from the ``{venue: {"perp": rate, "spot": rate}}`` table the caller must give (``require_fees``: there is
#   no default rate, D2-13); a leg whose market has no fee in it is left out (§4.1a rule 3);
# * ``base`` / ``quote_ccy`` from the pack's instruments table, or -- dataset packs leave them null -- from the declared
#   instrument (``{"venue", "market", "symbol", "base", "quote"}``).
#
# Pairs are enumerated exactly as §4.1a rules 4–5 (``pairs_of_legs``): ordered cross_perp pairs, spot_perp only when spot
# klines are in the frame.
#
# 2026-10-18 additions (design docs/plans/2026-10-18-hl-onchain-arb-integration.md §4.3; each only when the pack holds the
# data, otherwise the view is exactly the one above):
#
# * coarse bars: a leg uses the FINEST fresh series of the pack -- 1m (last closed bar at most 5 minutes old), else 1h (at most
#   65 minutes), else 1d (at most 1 day + 5 minutes); ``volume_quote`` = sum over the bars covering the 24 h ending with that
#   bar (1440 one-minute / 24 one-hour / 1 one-day bar, all present, else null); ``quote_ms`` = that bar's close time;
# * ``funding_mean_7d`` / ``funding_mean_30d`` / ``funding_pos_share_30d`` of a perp leg from the official settlements visible
#   now (``funding_history_fields``); missing when the settlements cover less than 0.9 x the window in hours;
# * ``listed_ms`` from the pack's ``instrument_meta`` (the snapshot known now); missing without it;
# * ``borrow_apr`` / ``borrow_util`` / ``borrow_cap_used`` of a perp leg: the latest ``borrow_lend`` reading of the leg's venue and
#   quote currency visible now, else the request's constants ``borrow.<venue> = {apr, util?, cap_used?}``, else missing;
# * equity leg (``market = equity``, venue ``ibkr``): ``bid = ask`` = close of the INDEX-price bar (``price_kind = index``) of
#   the declared Binance perp of the same base (or the instrument's ``reference``), same finest-fresh rule, only inside the NYSE
#   signal window (``ir_calendar``); its fee is ``fees.ibkr.equity`` (none -> the leg is out); base is the instrument's ``base``
#   (default: its symbol), quote its ``quote`` (default: the reference's).

DATASET_VIEW = "kline_close_synthetic_bbo"
DATASET_VOLUME_BARS = 1440
DATASET_MAX_BAR_AGE_MS = 5 * 60_000
#: the bar series a leg may use, finest first: ``(interval, bar length ms, bars in 24 h, max age of the last closed bar ms)``
BAR_SPECS = (("1m", 60_000, 1440, DATASET_MAX_BAR_AGE_MS),
             ("1h", 3_600_000, 24, 65 * 60_000),
             ("1d", 86_400_000, 1, 86_400_000 + 5 * 60_000))
BAR_SPECS_INDEX = {spec[0]: k for k, spec in enumerate(BAR_SPECS)}
EQUITY_VENUE = "ibkr"
REFERENCE_VENUE = "binance"  # the venue whose index klines price an equity leg
HISTORY_WINDOWS = ((7, "funding_mean_7d"), (30, "funding_mean_30d"))  # days, view field
HISTORY_LOOKBACK_MS = 31 * 86_400_000  # the 30-day window plus one more day for the interval inference of its first row
HISTORY_MIN_COVERAGE_PCT = 90
#: a borrow/lend reading older than this (by its read time) at the decision time is not a current pool state: borrow_* are missing
BORROW_MAX_AGE_MS = 6 * 3_600_000
SETTLEMENT_KINDS = ("regular", "special")
SETTLEMENT_LOOKBACK_MS = 86_400_000  # longest funding interval of the nine venues is 8 h
KLINE_SPAN_MS = 59_999
#: settlement gaps accepted as a contract's interval when the dataset does not state it (seconds)
INFERABLE_INTERVALS = (3600, 7200, 14400, 28800)
_MIN_MS = 60_000


def settlement_price(official_mark: Decimal | None, last_seen: Callable[[], Decimal | None],
                     price_dp: int | None = None) -> Decimal | None:
    """The price a funding settlement is booked at (AlphaKeel F04: its engine and event ledger use the same rule).

    The settlement's own published mark when it has one; otherwise ``last_seen()``, the last price observed at or before
    the settlement (``None``: unpriced, the caller counts it). Rounded half-even to ``price_dp`` decimals when given
    (AlphaKeel ``round_official_mark``: the engine holds prices at the contract's price precision). The one rule for
    ``sim.Simulator._settle`` (scan packs) and ``ir_backtest`` (dataset packs)."""
    px = official_mark if official_mark is not None else last_seen()
    if px is None or price_dp is None:
        return px
    return px.quantize(Decimal(1).scaleb(-price_dp), rounding=ROUND_HALF_EVEN)


def require_fees(fees) -> dict:
    """The dataset view's fee table ``{venue: {perp|spot: rate}}``, which must be given: there is no default rate (the same
    rule as AlphaKeel's ``dataset_view``, whose ``fees`` is required; audit 2026-10-12 D2-13)."""
    if not isinstance(fees, dict) or not fees:
        raise ApiError("request.invalid", "the dataset view needs an explicit fee table {venue: {perp: rate, spot: rate}} "
                       "(--fees); there is no default taker fee", field="fees")
    return fees


def dataset_fee(fees: dict, venue: str, market: str) -> str | None:
    """The taker fee of ``(venue, market)`` in the fee table (None: that market has no fee and is left out of the frame)."""
    row = require_fees(fees).get(venue)
    v = row.get(market) if isinstance(row, dict) else None
    return None if v in (None, "") else fmt(Decimal(str(v)))


def instrument_identity(meta, declared: dict) -> tuple[str | None, str | None]:
    """``(base, quote)``: the pack's instruments table when it has them, else the declared instrument's ``base``/``quote``."""
    base = getattr(meta, "base", None) or declared.get("base")
    quote = getattr(meta, "quote", None) or declared.get("quote")
    return (None if base is None else str(base)), (None if quote is None else str(quote))


def interval_hours_of(interval_seconds) -> int | None:
    if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int) or interval_seconds < 3600 or interval_seconds % 3600:
        return None
    return interval_seconds // 3600


def dataset_leg(*, venue: str, market: str, symbol: str, base: str, quote_ccy: str, close: Decimal, close_time_ms: int,
                volume_quote: Decimal | None, settlement: tuple[Decimal, int, int] | None, taker_fee: str,
                extra: dict | None = None) -> dict | None:
    """One leg of a dataset frame (``{"base", "view": LegView}``). ``settlement`` = ``(rate, t_ms, interval_seconds)`` of
    the last official settlement visible now (perps); a perp without one, or with an unknown interval, is not a leg.
    ``extra``: the 2026-10-18 view fields (history, borrow, ``listed_ms``) that are present; a missing one is not written."""
    funding = {"funding_rate": None, "interval_hours": None, "next_funding_ms": None}
    if market == "perp":
        if settlement is None:
            return None
        rate, st, iv = settlement
        h = interval_hours_of(iv)
        if h is None:
            return None
        # the settlement timestamp's millisecond jitter is not carried into the next boundary (same rule as AlphaKeel)
        funding = {"funding_rate": fmt(rate), "interval_hours": h, "next_funding_ms": (st + 30_000) // 60_000 * 60_000 + iv * 1000}
    px = fmt(close)
    return {"base": base, "mark": px, "quote_time": close_time_ms, "view": {
        "venue": venue, "symbol": symbol, "market": market, "quote_ccy": quote_ccy, "bid": px, "ask": px, "bbo": True,
        **funding, "funding_px": px if market == "perp" else None, "volume_quote": _s(volume_quote), "taker_fee": taker_fee,
        "quote_ms": close_time_ms, **(extra or {})}}


def require_borrow(borrow) -> dict:
    """The request's borrow constants ``{venue: {apr, util?, cap_used?}}`` (decimal text or numbers) as ``{venue: {view
    field: canonical decimal}}`` -- ``borrow_apr`` always, ``borrow_util`` / ``borrow_cap_used`` when given. None -> ``{}``."""
    if borrow in (None, {}):
        return {}
    if not isinstance(borrow, dict):
        raise ApiError("request.invalid", "borrow must be {venue: {apr, util?, cap_used?}}", field="borrow")
    out = {}
    for v, row in borrow.items():
        if v not in VENUES or not isinstance(row, dict) or set(row) - {"apr", "util", "cap_used"} or row.get("apr") in (None, ""):
            raise ApiError("request.invalid", f"borrow.{v}: a known venue with {{apr, util?, cap_used?}} (apr required)", field="borrow")
        try:
            vals = {"borrow_" + k: fmt(Decimal(str(row[k]))) for k in ("apr", "util", "cap_used") if row.get(k) not in (None, "")}
        except ArithmeticError:
            raise ApiError("request.invalid", f"borrow.{v}: not a decimal", field="borrow") from None
        out[v] = vals
    return out


def borrow_fields(state, venue: str, constants: dict, now_ms: int | None = None) -> dict:
    """``borrow_*`` view fields of a perp leg: the pool reading ``state`` (``packfile.Borrow``) when there is one -- its fields
    that are None are missing, and all are missing when it was read more than ``BORROW_MAX_AGE_MS`` before ``now_ms`` (a stale
    pool is not replaced by the constants) -- else the venue's constants, else nothing."""
    if state is not None:
        if now_ms is not None and state.t < now_ms - BORROW_MAX_AGE_MS:
            return {}
        got = {"borrow_apr": state.apr, "borrow_util": state.util, "borrow_cap_used": state.cap_used}
        return {k: fmt(x) for k, x in got.items() if x is not None}
    return dict(constants.get(venue) or {})


def listed_ms_at(history: list[dict], now_ms: int) -> int | None:
    """``listed_ms`` of the ``instrument_meta`` snapshot known at ``now_ms`` (the last with ``t <= now``); None when there is none,
    it has no listing time, or the contract is listed after ``now`` (same rule as ``Pit.instrument_meta``)."""
    known = [m for m in history if m["t"] <= now_ms]
    if not known:
        return None
    v = known[-1]["listed_ms"]
    if v is None or v > now_ms:
        return None
    if not isinstance(v, int) or isinstance(v, bool):
        raise ApiError("evidence.reference", "instrument_meta listed_ms is not an integer")
    return v


_HIST_CTX = Context(prec=60, rounding=ROUND_HALF_EVEN)


def funding_history_fields(rows: list[tuple[int, Decimal, int | None]], now_ms: int) -> tuple[dict, int | None]:
    """``(fields, valid_until_ms)``: the 2026-10-18 history view fields of one perp from its official settlements.

    ``rows`` = ``(t, rate, interval_seconds | None)`` of the settlements visible at ``now_ms``, ascending and unique by ``t``, reaching
    at least one day before the 30-day window (the interval of a row with a null ``interval_seconds`` is inferred from the gap
    to the row before it, ``inferred_interval_seconds``; a row with no usable interval is left out). Per window ``W`` days
    (7, 30): the rows with ``now - W d <= t < now``; ``funding_mean_Wd`` = the arithmetic mean of their ``rate / interval_hours``;
    the field is missing when their intervals add up to less than 0.9 x ``24 W`` hours. ``funding_pos_share_30d`` = the share of
    the 30-day rows with ``rate > 0`` (same coverage rule). Values are exact decimals at 60 significant digits.

    ``valid_until_ms``: the result is unchanged for every ``now`` up to and including it, as long as no new settlement becomes
    visible (the streaming cursor recomputes only then); None = no bound."""
    items = []  # (t, hourly rate, rate > 0, hours)
    prev_t = None
    for t, rate, iv in rows:
        h = interval_hours_of(iv if iv is not None else inferred_interval_seconds(prev_t, t))
        if h is not None:
            items.append((t, _HIST_CTX.divide(rate, Decimal(h)), rate > 0, h))
        prev_t = t
    out: dict = {}
    until: int | None = None
    future = [x[0] for x in items if x[0] >= now_ms]
    if future:
        until = min(future)
    for days, field in HISTORY_WINDOWS:
        lo = now_ms - days * 86_400_000
        win = [x for x in items if lo <= x[0] < now_ms]
        if win:
            until = win[0][0] + days * 86_400_000 if until is None else min(until, win[0][0] + days * 86_400_000)
        if sum(x[3] for x in win) * 100 < HISTORY_MIN_COVERAGE_PCT * 24 * days:
            continue
        total = Decimal(0)
        for x in win:
            total = _HIST_CTX.add(total, x[1])
        out[field] = fmt(_HIST_CTX.divide(total, Decimal(len(win))))
        if days == 30:
            out["funding_pos_share_30d"] = fmt(_HIST_CTX.divide(Decimal(sum(1 for x in win if x[2])), Decimal(len(win))))
    return out, until


def visible_settlements(pit, inst, now_ms: int) -> list:
    """The official settlements of ``inst`` visible at ``now_ms``, unique by settlement time ``t`` and ascending, from
    ``max(pack window start, now - 31 d)``: when two visible rows share a ``t`` the one that became visible LATER replaces the
    earlier (rows ordered by ``(visible time, t)``; the streaming cursor does the same)."""
    lo = max(pit.window_start_ms(), now_ms - HISTORY_LOOKBACK_MS)
    delay = pit._delay
    raw = [s for s in pit.settlements(inst, lo, now_ms + 1) if s.event_kind in SETTLEMENT_KINDS]
    raw.sort(key=lambda s: ((s.available_at_ms if s.available_at_ms is not None else s.t + delay), s.t))
    by_t = {s.t: s for s in raw}
    return [by_t[t] for t in sorted(by_t)]


def require_sources(pack, decl: list[dict], identity: dict, needs, constants: dict) -> None:
    """The data each view need of the IR (``ir.view_needs``) is sourced from, else ``request.invalid``: ``borrow`` -- the pack
    has a ``borrow_lend`` table for the perp's venue and quote currency, or the request gave a constant for the venue;
    ``listing`` -- the pack's ``instrument_meta`` of every non-equity leg has a listing time. (``history`` needs the pack to reach
    ``HISTORY_LOOKBACK_MS`` before the first decision: checked by the caller, which knows the run window.) ``identity``:
    ``Inst -> (base, quote)``."""
    from .packfile import Inst

    for d in decl:
        inst = Inst(d["venue"], d["market"], d["symbol"])
        if "borrow" in needs and inst.market == "perp":
            if not pack.has_table("borrow_lend", pack.borrow_inst(inst.venue, identity[inst][1])) and inst.venue not in constants:
                raise ApiError("request.invalid", f"the IR reads borrow_* but {inst.leg()} has neither a borrow_lend table in the pack "
                               f"(pool {inst.venue}:lend:{identity[inst][1]}) nor a --borrow constant for {inst.venue}", field="borrow")
        if "listing" in needs and inst.market != "equity":
            if not pack.has_table("instrument_meta", inst) or not any(m["listed_ms"] is not None for m in pack.instrument_meta_history(inst)):
                raise ApiError("request.invalid", f"the IR reads listing_age_hours but the pack has no instrument_meta listing time for {inst.leg()}",
                               field="instruments")


def require_history_reach(pack_start_ms: int, first_decision_ms: int) -> None:
    """``history`` need: the pack's funding must reach ``HISTORY_LOOKBACK_MS`` before the first decision."""
    if pack_start_ms > first_decision_ms - HISTORY_LOOKBACK_MS:
        raise ApiError("request.invalid", "the IR reads funding_apr_7d/30d or funding_pos_share_30d: the pack's funding must start "
                       f"{HISTORY_LOOKBACK_MS // 86_400_000} days before the window (ir_backtest.dataset_request asks for it)", field="window")


def equity_identity(d: dict, ref_quote: str) -> tuple[str, str]:
    """``(base, quote)`` of a declared equity instrument: its ``base`` (default its symbol) and ``quote`` (default the quote of
    the reference perp)."""
    return str(d.get("base") or d["symbol"]), str(d.get("quote") or ref_quote)


def equity_reference(d: dict, decl: list[dict]) -> dict | None:
    """The declared perp whose index klines price the equity instrument ``d``: its ``reference`` ``{venue, market, symbol}``
    when given, else the first declared ``binance`` perp (by symbol) with the same base. None when there is none."""
    if isinstance(d.get("reference"), dict):
        r = d["reference"]
        return next((x for x in decl if (x["venue"], x["market"], x["symbol"]) == (r.get("venue"), r.get("market"), r.get("symbol"))), None)
    base = str(d.get("base") or d["symbol"])
    return next((x for x in sorted(decl, key=lambda x: x["symbol"])
                 if x["venue"] == REFERENCE_VENUE and x["market"] == "perp" and x.get("base") == base), None)


#: settlement timestamps jitter by a few milliseconds (Binance ``fundingTime`` ...000, ...004, ...005): a gap within this
#: tolerance of a standard interval is that interval (the same 60 s the official-settlement boundary match uses)
INTERVAL_TOLERANCE_MS = 60_000


def inferred_interval_seconds(prev_t: int | None, last_t: int) -> int | None:
    """The interval implied by two consecutive visible settlements: the standard interval (``INFERABLE_INTERVALS``)
    within ``INTERVAL_TOLERANCE_MS`` of their gap, else None."""
    if prev_t is None:
        return None
    gap = last_t - prev_t
    for iv in INFERABLE_INTERVALS:
        if abs(gap - iv * 1000) <= INTERVAL_TOLERANCE_MS:
            return iv
    return None


def settlement_tuple(last, prev=None) -> tuple[Decimal, int, int | None]:
    """``(rate, t, interval_seconds)`` of the last visible settlement; a null interval is inferred from ``prev``."""
    iv = last.interval_seconds
    if iv is None:
        iv = inferred_interval_seconds(None if prev is None else prev.t, last.t)
    return (last.rate, last.t, iv)


def _bar_window(bars: list, last, n: int, span_ms: int) -> Decimal | None:
    """Sum of ``volume_quote`` over the ``n`` bars of ``span_ms`` ending with ``last`` (None unless all ``n`` are there)."""
    day = [b for b in bars if b.t > last.t - n * span_ms]
    return sum((b.volume_quote for b in day), Decimal(0)) if len(day) == n else None


def _finest_fresh_bar(pit, inst, now_ms: int, price_kind: str):
    """``(last bar, 24 h quote volume)`` of the finest bar series of ``inst`` whose last closed bar is fresh at ``now_ms``
    (``BAR_SPECS``); None when no series is."""
    for interval, span, n, max_age in BAR_SPECS:
        bars = pit.klines(inst, max(0, now_ms - (n * span + max_age + span)), now_ms + 1, price_kind=price_kind, interval=interval)
        if bars and now_ms - bars[-1].close_time_ms <= max_age:
            return bars[-1], _bar_window(bars, bars[-1], n, span)
    return None


def dataset_frame_legs(pit_or_pack, insts: list, now_ms: int, *, fees: dict, view: str = DATASET_VIEW, borrow: dict | None = None,
                       needs=frozenset()) -> list[dict]:
    """The legs of the dataset frame at ``now_ms`` (see the block comment above), in (venue, market, symbol) order.

    ``pit_or_pack``: a ``Pit`` whose ``as_of_ms == now_ms`` or a dataset ``Pack`` (wrapped in ``Pit(pack, now_ms)``).
    ``insts``: ``[{venue, market, symbol, base?, quote?}]`` (or ``Inst``). Reads go through the point-in-time reader.
    ``borrow``: the request's borrow constants (:func:`require_borrow`), used for a perp whose pack has no ``borrow_lend`` table.
    """
    from . import ir_calendar
    from .packfile import Inst, Pit

    if view != DATASET_VIEW:
        raise ValueError(f"unknown dataset view {view!r} (only {DATASET_VIEW!r})")
    if hasattr(pit_or_pack, "as_of_ms"):
        if pit_or_pack.as_of_ms != now_ms:
            raise ValueError("the Pit's as_of_ms must be the decision time now_ms")
        pit = pit_or_pack
    else:
        pit = Pit(pit_or_pack, now_ms)
    constants = require_borrow(borrow)
    metas = pit.instruments()
    legs = []
    decl = [d if isinstance(d, dict) else d.ref() for d in insts]
    if needs and not hasattr(pit_or_pack, "as_of_ms"):  # the source checks need the unfiltered pack
        ident = {Inst(d["venue"], d["market"], d["symbol"]): instrument_identity(metas.get(Inst(d["venue"], d["market"], d["symbol"])), d)
                 for d in decl if d["market"] != "equity"}
        require_sources(pit_or_pack, decl, ident, needs, constants)
    for d in sorted(decl, key=lambda d: (d["venue"], d["market"], d["symbol"])):
        inst = Inst(d["venue"], d["market"], d["symbol"])
        if inst.venue not in VENUES or inst.market not in ("perp", "spot", "equity"):
            continue
        if inst.market == "equity":
            ref = equity_reference(d, decl)
            if ref is None:
                raise ValueError(f"{inst.leg()}: no declared {REFERENCE_VENUE} perp of base {d.get('base') or d['symbol']} (or a "
                                 "'reference' instrument) to take the index price from")
            base, quote = equity_identity(d, instrument_identity(metas.get(Inst(ref["venue"], ref["market"], ref["symbol"])), ref)[1] or "")
            if not quote:
                raise ValueError(f"{inst.leg()}: quote is not known (declare it or the reference's)")
            src, kind = Inst(ref["venue"], ref["market"], ref["symbol"]), "index"
        else:
            base, quote = instrument_identity(metas.get(inst), d)
            if base is None or quote is None:
                raise ValueError(f"{inst.leg()}: base/quote are not in the pack's instruments table; declare them in the instrument")
            src, kind = inst, "trade"
        fee = dataset_fee(fees, inst.venue, inst.market)
        if fee is None:
            continue
        if inst.market == "equity" and not ir_calendar.in_window(now_ms):
            continue
        got = _finest_fresh_bar(pit, src, now_ms, kind)
        if got is None:
            continue
        last, vol = got
        if inst.market == "equity":
            vol = None  # the index price of the reference perp carries no volume of the equity: missing, never the perp's
        st = None
        extra: dict = {}
        if inst.market == "perp":
            rows = visible_settlements(pit, inst, now_ms)  # unique by t (the later-visible row wins), from max(pack start, now - 31 d)
            near = [s for s in rows if s.t >= now_ms - SETTLEMENT_LOOKBACK_MS]
            st = settlement_tuple(near[-1], near[-2] if len(near) > 1 else None) if near else None
            extra.update(funding_history_fields([(s.t, s.rate, s.interval_seconds) for s in rows], now_ms)[0])
            extra.update(borrow_fields(pit.borrow_lend(inst.venue, quote), inst.venue, constants, now_ms))
        if inst.market != "equity":
            listed = (pit.instrument_meta(inst) or {}).get("listed_ms")
            if listed is not None:
                extra["listed_ms"] = listed
        leg = dataset_leg(venue=inst.venue, market=inst.market, symbol=inst.symbol, base=base, quote_ccy=quote, close=last.close,
                          close_time_ms=last.close_time_ms, volume_quote=vol, settlement=st, taker_fee=fee, extra=extra)
        if leg is not None:
            legs.append(leg)
    return legs


def dataset_frame_pairs(pit_or_pack, insts: list, now_ms: int, *, fees: dict, view: str = DATASET_VIEW,
                        borrow: dict | None = None) -> list[PairView]:
    """All candidate pairs of the dataset frame at ``now_ms`` (§4.1a rules 4–5 over :func:`dataset_frame_legs`), by ``id``."""
    return pairs_of_legs(dataset_frame_legs(pit_or_pack, insts, now_ms, fees=fees, view=view, borrow=borrow))
