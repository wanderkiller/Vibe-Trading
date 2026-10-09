"""Strategy IR decision views from a point-in-time pack reader (README §4.1a of contract/strategy-ir).

``scan_frame_pairs`` enumerates the candidate pairs of ONE scan frame from the raw quote, observation, instrument and fee
tables of the pack -- never from screener opportunities (those were cut by scan thresholds and ``top_n``). The result is
the ``frame.pairs`` input of :func:`alphakeel_research.ir.decide`: a list of PairView documents (JSON values, decimal
strings) sorted by ``id``.

Pure apart from reading the ``Pit``; standard library only (imported inside the policy sandbox).
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

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
        legs.append({
            "base": meta.base,
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
    """README §4.1a rules 4–5: ordered cross-venue perp pairs and same-venue (spot long, perp short) pairs, by id."""
    out = []
    for a in legs:
        for b in legs:
            la, sb = a["view"], b["view"]
            if a["base"] != b["base"] or la["quote_ccy"] != sb["quote_ccy"] or sb["market"] != "perp":
                continue
            if la["market"] == "perp" and la["venue"] != sb["venue"]:
                kind, tag = "cross_perp", "cross"
            elif la["market"] == "spot" and la["venue"] == sb["venue"]:
                kind, tag = "spot_perp", "spot"
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


def tradable(legs):
    """``(venue, market, symbol)`` of this frame's legs with a real top of book (``bbo = true``): README §5a."""
    return {(x["view"]["venue"], x["view"]["market"], x["view"]["symbol"]) for x in legs if x["view"]["bbo"]}


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
#   settlements, accepted only when it is exactly 1, 2, 4 or 8 h (``INFERABLE_INTERVALS``). A perp with neither is left
#   out. After a venue changes a contract's interval the inference lags one settlement. ``next_funding_ms`` = settlement
#   time + interval;
# * ``volume_quote`` = sum of ``volume_quote`` over the 1440 one-minute bars of the 24 h ending with the last closed bar
#   (``None`` unless all 1440 are present);
# * ``quote_ms`` = the bar's ``close_time_ms``; a leg whose last closed bar is older than ``DATASET_MAX_BAR_AGE_MS`` is not in
#   the frame (old quotes are not carried over, as §4.1a rule 1 says for scan frames);
# * ``taker_fee`` from a ``{venue: {"perp": "0.0005", "spot": ...}}`` mapping (default: AlphaKeel's public lowest-tier perp
#   taker 0.0005 on every venue; a spot leg without a fee is left out, §4.1a rule 3);
# * ``base`` / ``quote_ccy`` from the pack's instruments table, or -- dataset packs leave them null -- from the declared
#   instrument (``{"venue", "market", "symbol", "base", "quote"}``).
#
# Pairs are enumerated exactly as §4.1a rules 4–5 (``pairs_of_legs``): ordered cross_perp pairs, spot_perp only when spot
# klines are in the frame.

DATASET_VIEW = "kline_close_synthetic_bbo"
DATASET_VOLUME_BARS = 1440
DATASET_MAX_BAR_AGE_MS = 5 * 60_000
DEFAULT_PERP_TAKER_FEE = "0.0005"
SETTLEMENT_KINDS = ("regular", "special")
SETTLEMENT_LOOKBACK_MS = 86_400_000  # longest funding interval of the nine venues is 8 h
KLINE_SPAN_MS = 59_999
#: settlement gaps accepted as a contract's interval when the dataset does not state it (seconds)
INFERABLE_INTERVALS = (3600, 7200, 14400, 28800)
_MIN_MS = 60_000


def default_fees() -> dict:
    """``{venue: {"perp": "0.0005"}}`` for every IR venue (AlphaKeel's public lowest-tier perp taker fee)."""
    return {v: {"perp": DEFAULT_PERP_TAKER_FEE} for v in VENUES}


def dataset_fee(fees: dict | None, venue: str, market: str) -> str | None:
    row = (default_fees() if fees is None else fees).get(venue)
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
                volume_quote: Decimal | None, settlement: tuple[Decimal, int, int] | None, taker_fee: str) -> dict | None:
    """One leg of a dataset frame (``{"base", "view": LegView}``). ``settlement`` = ``(rate, t_ms, interval_seconds)`` of
    the last official settlement visible now (perps); a perp without one, or with an unknown interval, is not a leg."""
    funding = {"funding_rate": None, "interval_hours": None, "next_funding_ms": None}
    if market == "perp":
        if settlement is None:
            return None
        rate, st, iv = settlement
        h = interval_hours_of(iv)
        if h is None:
            return None
        funding = {"funding_rate": fmt(rate), "interval_hours": h, "next_funding_ms": st + iv * 1000}
    px = fmt(close)
    return {"base": base, "view": {
        "venue": venue, "symbol": symbol, "market": market, "quote_ccy": quote_ccy, "bid": px, "ask": px, "bbo": True,
        **funding, "funding_px": px if market == "perp" else None, "volume_quote": _s(volume_quote), "taker_fee": taker_fee,
        "quote_ms": close_time_ms}}


def inferred_interval_seconds(prev_t: int | None, last_t: int) -> int | None:
    """The interval implied by two consecutive visible settlements, or None unless it is one of ``INFERABLE_INTERVALS``."""
    if prev_t is None:
        return None
    gap, rem = divmod(last_t - prev_t, 1000)
    return gap if rem == 0 and gap in INFERABLE_INTERVALS else None


def settlement_tuple(last, prev=None) -> tuple[Decimal, int, int | None]:
    """``(rate, t, interval_seconds)`` of the last visible settlement; a null interval is inferred from ``prev``."""
    iv = last.interval_seconds
    if iv is None:
        iv = inferred_interval_seconds(None if prev is None else prev.t, last.t)
    return (last.rate, last.t, iv)


def dataset_frame_legs(pit_or_pack, insts: list, now_ms: int, *, fees: dict | None = None, view: str = DATASET_VIEW) -> list[dict]:
    """The legs of the dataset frame at ``now_ms`` (see the block comment above), in (venue, market, symbol) order.

    ``pit_or_pack``: a ``Pit`` whose ``as_of_ms == now_ms`` or a dataset ``Pack`` (wrapped in ``Pit(pack, now_ms)``).
    ``insts``: ``[{venue, market, symbol, base?, quote?}]`` (or ``Inst``). Reads go through the point-in-time reader.
    """
    from .packfile import Inst, Pit

    if view != DATASET_VIEW:
        raise ValueError(f"unknown dataset view {view!r} (only {DATASET_VIEW!r})")
    if hasattr(pit_or_pack, "as_of_ms"):
        if pit_or_pack.as_of_ms != now_ms:
            raise ValueError("the Pit's as_of_ms must be the decision time now_ms")
        pit = pit_or_pack
    else:
        pit = Pit(pit_or_pack, now_ms)
    metas = pit.instruments()
    legs = []
    decl = [d if isinstance(d, dict) else d.ref() for d in insts]
    for d in sorted(decl, key=lambda d: (d["venue"], d["market"], d["symbol"])):
        inst = Inst(d["venue"], d["market"], d["symbol"])
        if inst.venue not in VENUES or inst.market not in ("perp", "spot"):
            continue
        base, quote = instrument_identity(metas.get(inst), d)
        if base is None or quote is None:
            raise ValueError(f"{inst.leg()}: base/quote are not in the pack's instruments table; declare them in the instrument")
        fee = dataset_fee(fees, inst.venue, inst.market)
        if fee is None:
            continue
        lookback = DATASET_VOLUME_BARS * _MIN_MS + DATASET_MAX_BAR_AGE_MS + _MIN_MS
        bars = pit.klines(inst, max(0, now_ms - lookback), now_ms + 1, price_kind="trade")
        if not bars or now_ms - bars[-1].close_time_ms > DATASET_MAX_BAR_AGE_MS:
            continue
        last = bars[-1]
        day = [b for b in bars if b.t > last.t - DATASET_VOLUME_BARS * _MIN_MS]
        vol = sum((b.volume_quote for b in day), Decimal(0)) if len(day) == DATASET_VOLUME_BARS else None
        st = None
        if inst.market == "perp":
            rows = [s for s in pit.settlements(inst, max(0, now_ms - SETTLEMENT_LOOKBACK_MS), now_ms + 1) if s.event_kind in SETTLEMENT_KINDS]
            st = settlement_tuple(rows[-1], rows[-2] if len(rows) > 1 else None) if rows else None
        leg = dataset_leg(venue=inst.venue, market=inst.market, symbol=inst.symbol, base=base, quote_ccy=quote, close=last.close,
                          close_time_ms=last.close_time_ms, volume_quote=vol, settlement=st, taker_fee=fee)
        if leg is not None:
            legs.append(leg)
    return legs


def dataset_frame_pairs(pit_or_pack, insts: list, now_ms: int, *, fees: dict | None = None, view: str = DATASET_VIEW) -> list[PairView]:
    """All candidate pairs of the dataset frame at ``now_ms`` (§4.1a rules 4–5 over :func:`dataset_frame_legs`), by ``id``."""
    return pairs_of_legs(dataset_frame_legs(pit_or_pack, insts, now_ms, fees=fees, view=view))
