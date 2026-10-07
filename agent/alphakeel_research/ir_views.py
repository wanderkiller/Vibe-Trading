"""Strategy IR decision views from a point-in-time pack reader (README §4.1a of contract/strategy-ir).

``scan_frame_pairs`` enumerates the candidate pairs of ONE scan frame from the raw quote, observation, instrument and fee
tables of the pack -- never from screener opportunities (those were cut by scan thresholds and ``top_n``). The result is
the ``frame.pairs`` input of :func:`alphakeel_research.ir.decide`: a list of PairView documents (JSON values, decimal
strings) sorted by ``id``.

Pure apart from reading the ``Pit``; standard library only (imported inside the policy sandbox).
"""

from __future__ import annotations

from decimal import Decimal
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
