"""Independent Decimal simulator of the AlphaKeel execution profile ``ak-top-of-book-ioc-taker-v2``.

This is a SEPARATE implementation (not the Rust engine, not Nautilus): orders are immediate-or-cancel at the scan's
best bid/ask, taker fees per fill, per-venue/currency accounts, funding settled at each venue's boundary on the last
mark price, USDC converted to USDT once with the scan's USDC/USDT rate, equity valued each scan. It exists so a Python
backtest has its own exact accounting that can be compared event by event with AlphaKeel's engine; it never reads the
service's results.

What it does not model is refused loudly (``Unsupported``) instead of being guessed: reduce-only orders bigger than the
position, reduce-only with no open position, partial fills, quotes that never existed, unsupported order types.

Facts it reproduces from the engine (found by probing the engine, documented in the profile): fees and funding are
rounded to 1e-8 half-even; an opening order is denied when its notional (leverage does not reduce it) exceeds the
account's free balance, i.e. its total balance minus the maintenance margin of the positions open in that account, and
position-reducing orders skip that check; orders at the same decision time are all checked against the account as it was
before any of them filled.

Same-instant margin (profile v2, ``account.same_instant_margin == "cumulative_initial_margin"``): before an opening order is
submitted, the initial margin of the opening orders of one decision instant on one account (venue x settlement currency;
perpetual: notional / leverage, spot: full notional; market orders at the touch, limit orders at the limit; fees not
included) is accumulated in submission order and compared with the account's free balance before the first of them. An
order that would push the total over it is not submitted (status ``not_submitted``, no order id) and does not count towards
the total; reduce-only orders are neither checked nor counted. Orders that pass go to the engine, which checks each one
against the same before-batch account, so no sequential-fill outcome has to be guessed for them. A profile document without
the field is the retired v1 rule (``per_order_engine``): no cumulative check, and same-time orders whose sequential outcome
would differ from the before-batch check are refused with ``Unsupported``.

Where the outcome depends on what is not modelled exactly (the price basis of the maintenance margin, which makes the free
balance a band) the simulator refuses with ``Unsupported`` instead of picking a side; IOC limit orders fill at the touch when
marketable and are cancelled otherwise; the funding rate of a boundary is the last predicted rate reported for it (or the
official event when the profile says so); the settlement price is the last mark seen at or before the boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from typing import Callable

from . import contract
from .errors import ApiError, Unsupported
from .packfile import Inst, Pack

UNIT = Decimal("0.00000001")
HOUR_MS = 3_600_000
QUOTE_FUTURE_TOLERANCE_MS = 5_000
MAX_QTY = Decimal(30_000_000_000_000)
SIZE_DP = 8
BOUNDARY_TOLERANCE_MS = 60_000


def q8(x: Decimal) -> Decimal:
    return x.quantize(UNIT, rounding=ROUND_HALF_EVEN)


def dstr(x: Decimal) -> str:
    """Exact decimal string, no exponent, normalised (matches the service's rendering)."""
    n = x.normalize()
    s = format(n, "f")
    return "0" if s in ("-0", "") else s


def dp_of(x: Decimal) -> int:
    return max(0, -x.normalize().as_tuple().exponent)


def trunc(x: Decimal, places: int) -> Decimal:
    from decimal import ROUND_DOWN

    return x.quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN)


def order_quantity(x: Decimal, dp: int = SIZE_DP) -> Decimal:
    """Quantity after truncation toward zero to the engine's precision and an f64 round trip that must be stable."""
    for places in range(dp, -1, -1):
        q = trunc(x, places)
        for _ in range(4):
            back = Decimal(repr(float(q))).quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_EVEN)
            if back == q:
                return q
            q = back
    raise Unsupported(f"quantity {x} cannot be represented exactly", "intent.overflow")


def aligned_after(after: int, hours: int) -> int:
    iv = max(hours, 1) * HOUR_MS
    return (after // iv + 1) * iv


def next_boundary(now: int, hours: int, nxt: int | None) -> int:
    return nxt if (nxt is not None and nxt > now) else aligned_after(now, hours)


def engine_venue(inst: Inst) -> str:
    v = inst.venue.upper()
    return v + "SPOT" if inst.market == "spot" else v


@dataclass
class Snap:
    bid: Decimal
    ask: Decimal
    mark: Decimal
    funding: tuple[Decimal, int, int | None] | None


@dataclass
class IntentRec:
    intent_id: str
    inst: Inst
    buy: bool
    qty: Decimal
    market: bool
    limit: Decimal | None
    reduce_only: bool
    position_ref: str | None
    pair_id: str | None
    decision_ms: int
    step_seq: int | None
    raw: dict


@dataclass
class Pos:
    ref: str
    inst: Inst
    long: bool
    ccy: str
    net: Decimal = Decimal(0)
    entry_qty: Decimal = Decimal(0)
    entry_notional: Decimal = Decimal(0)
    cash: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)
    funding: Decimal = Decimal(0)
    fees_usdt: Decimal = Decimal(0)
    closed_frame: int | None = None


@dataclass
class Ev:
    ns: int
    rank: int
    ord: int
    kind: str
    instrument: dict | None
    pair_id: str | None
    intent_id: str | None
    order_id: str | None
    position_ref: str | None
    data: dict


@dataclass
class SimResult:
    events: list[dict]
    result: dict
    events_bytes: bytes
    events_sha256: str
    profile: dict
    accesses: list[dict]
    steps: list[dict] = field(default_factory=list)


class Simulator:
    """Frame-by-frame execution. ``decide(frame_index, time_ms, view) -> list[dict]`` supplies intent documents."""

    def __init__(self, pack: Pack, profile: dict, instruments: list[Inst], *, run_id: str, intent_price_dp: dict[str, int] | None = None):
        if not instruments:
            raise ApiError("request.invalid", "declare at least one tradable contract")
        self.pack = pack
        self.profile = profile
        self.run_id = run_id
        self.insts = list(instruments)
        self.start_balance = Decimal(profile["account"]["starting_balance_per_venue"])
        self.leverage = Decimal(profile["account"]["leverage_perp"])
        self.official = profile["funding"]["mode"] == "official_settlement"
        self.strict = bool(profile["funding"]["strict"])
        fees = pack.fees()
        self.max_age = int(fees["engine"]["max_quote_age_ms"])
        mm = fees["engine"].get("maint_margin")
        self.maint_margin: Decimal | None = Decimal(mm) if mm not in (None, "") else None
        # per account: (decision ns, the account as it was before the first order of that decision time)
        self._batch: dict[tuple[str, str], tuple[int, tuple]] = {}
        # same-instant margin rule: "cumulative_initial_margin" (profile v2) or the retired "per_order_engine" (documents without the field)
        self.margin_rule: str = profile["account"].get("same_instant_margin") or "per_order_engine"
        if self.margin_rule not in ("cumulative_initial_margin", "per_order_engine"):
            raise ApiError("capability.profile", f"same_instant_margin {self.margin_rule!r} is not modelled")
        # per account: (decision ns, initial margin of the opening orders already admitted at that instant)
        self._instant: dict[tuple[str, str], tuple[int, Decimal]] = {}
        self.fee_rate = {i: Decimal(fees["fees"][i.venue]["perp" if i.market == "perp" else "spot"]) for i in self.insts}
        self.instr_meta = pack.instruments()
        self.frames = pack.frames(include_warmup=False)
        if not self.frames:
            raise ApiError("data.window_empty", "the pack has no frames inside its window")
        self.intent_price_dp = intent_price_dp or {}
        self._load_market()
        self.accounts: dict[tuple[str, str], Decimal] = {}
        self.peak_exposure: dict[tuple[str, str], Decimal] = {}
        for i in self.insts:
            ccy = self.instr_meta[i].quote
            self.accounts.setdefault((engine_venue(i), ccy), self.start_balance)
        self.start_by_ccy: dict[str, Decimal] = {}
        for (_, ccy), v in self.accounts.items():
            self.start_by_ccy[ccy] = self.start_by_ccy.get(ccy, Decimal(0)) + v
        self.pos: dict[str, Pos] = {}
        self.refs: dict[str, tuple[Inst, bool]] = {}
        self.ref_decl: dict[str, tuple[Inst, bool]] = {}  # bound by the first opening intent naming the ref
        self.last_quote: dict[Inst, tuple[Decimal, Decimal, int]] = {}  # bid, ask, frame index (data time)
        self.fills: list[dict] = []
        self.evs: list[Ev] = []
        self._ord = 0
        self.metas: list[IntentRec] = []
        self.order_ends: list[dict] = []
        self.fund_events: list[dict] = []
        self.valuations: list[dict] = []
        self.seen_ids: set[str] = set()
        self.estimate_fallbacks = 0
        self.official_n = 0
        self.fed_official: dict[tuple[str, int], bool] = {}
        self._last_bal: dict = {}

    # ------------------------------------------------------------------ market

    def _load_market(self) -> None:
        t0, t1 = self.frames[0].t, self.frames[-1].t + 1
        self.pack.note_frames(t0, t1 - 1)
        self.snaps: list[dict[Inst, Snap]] = [dict() for _ in self.frames]
        self.price_dp: dict[Inst, int] = {}
        frame_idx = {f.i: k for k, f in enumerate(self.frames)}
        for inst in self.insts:
            if inst not in self.instr_meta:
                raise ApiError("data.unknown_instrument", f"{inst.leg()} is not in the pack universe")
            if not self.instr_meta[inst].selected:
                raise ApiError("data.unknown_instrument", f"{inst.symbol} was excluded by the pack's instrument filter")
            quotes = {q.frame: q for q in self.pack.quotes(inst, t0, t1)}
            obs = {o.frame: o for o in self.pack.observations(inst, t0, t1)} if inst.market == "perp" else {}
            dp = self.intent_price_dp.get(inst.leg(), 0)
            for fi, k in frame_idx.items():
                q = quotes.get(fi)
                if q is None:
                    continue
                f = self.frames[k]
                venue = f.venues.get(inst.venue, {})
                if not venue.get("ok"):
                    continue
                if inst.market == "perp":
                    o = obs.get(fi)
                    if o is None:
                        continue
                    mid = (q.bid + q.ask) / 2
                    mark = q.mark if q.mark is not None else mid
                    fpx = o.funding_px if o.funding_px is not None else mark
                    quote_ms = q.quote_ts if q.quote_ts is not None else venue.get("fetched_ms")
                    funding = (o.funding_rate, o.interval_hours, o.next_funding_ms)
                else:
                    mid = (q.bid + q.ask) / 2
                    fpx = mid
                    quote_ms = q.quote_ts if q.quote_ts is not None else venue.get("fetched_ms")
                    funding = None
                if not (q.bbo and q.bid > 0 and q.ask >= q.bid and fpx > 0):
                    continue
                if quote_ms is not None and not (quote_ms <= f.t + QUOTE_FUTURE_TOLERANCE_MS and f.t - quote_ms <= self.max_age):
                    continue
                self.snaps[k][inst] = Snap(q.bid, q.ask, fpx, funding)
                dp = max(dp, dp_of(q.bid), dp_of(q.ask), dp_of(fpx))
            self.price_dp[inst] = min(dp, 16)
        # settlement boundaries the venue changed before they happened are never settled (same rule as the engine feed)
        self._cancelled = self._cancelled_boundaries()
        self._official_tables: dict[Inst, list[tuple[int, Decimal]]] = {}
        if self.official:
            for inst in self.insts:
                if inst.market != "perp":
                    continue
                rows = [s for s in self.pack.settlements(inst, t0 - BOUNDARY_TOLERANCE_MS, t1 + BOUNDARY_TOLERANCE_MS + 1) if s.event_kind == "regular"]
                self._official_tables[inst] = sorted((s.t, s.rate) for s in rows)

    def _cancelled_boundaries(self) -> set[tuple[Inst, int]]:
        seen: dict[Inst, list[tuple[int, int]]] = {}
        for k, f in enumerate(self.frames):
            for inst, s in self.snaps[k].items():
                if s.funding is not None:
                    rate, hours, nxt = s.funding
                    seen.setdefault(inst, []).append((f.t, next_boundary(f.t, hours, nxt)))
        out = set()
        for inst, obs in seen.items():
            for b in {b for _, b in obs}:
                last_before = next((nb for t, nb in reversed(obs) if t < b), None)
                if last_before != b:
                    out.add((inst, b))
        return out

    # ------------------------------------------------------------------ helpers

    def _next_ord(self) -> int:
        self._ord += 1
        return self._ord

    def _emit(self, ns: int, rank: int, kind: str, *, inst: Inst | None = None, pair_id=None, intent_id=None, order_id=None, position_ref=None, data: dict) -> int:
        o = self._next_ord()
        self.evs.append(Ev(ns, rank, o, kind, inst.ref() if inst else None, pair_id, intent_id, order_id, position_ref, data))
        return o

    def _official_rate(self, inst: Inst, boundary: int) -> Decimal | None:
        t = self._official_tables.get(inst)
        if not t:
            return None
        near = [r for ts, r in t if abs(ts - boundary) <= BOUNDARY_TOLERANCE_MS]
        return near[0] if len(near) == 1 else None

    # ------------------------------------------------------------------ run

    def run(self, decide: Callable[[int, int, dict], list[dict]], *, steps: list[dict] | None = None, policy_steps: bool = False,
            state_hashes: list[str | None] | None = None) -> SimResult:
        frames = self.frames
        pending: dict[tuple[Inst, int], Decimal] = {}  # (inst, boundary) -> predicted rate (latest wins)
        last_mark: dict[Inst, Decimal] = {}
        fx_last: Decimal | None = None
        last_results: list[dict] = []
        self.step_log: list[dict] = []
        for k, f in enumerate(frames):
            # 1. settlements strictly before or at this frame's data time (b in (t_{k-1}, t_k]); price = last mark seen at or before b
            prev_t = frames[k - 1].t if k > 0 else None
            for (inst, b) in sorted([x for x in pending if (prev_t is None or x[1] > prev_t) and x[1] <= f.t], key=lambda x: (x[1], x[0].leg())):
                rate = pending.pop((inst, b))
                self._settle(inst, b, rate, last_mark_at=(f if b == f.t else frames[k - 1]), k=k)
            # 2. data at t_k
            for inst, s in self.snaps[k].items():
                self.last_quote[inst] = (s.bid, s.ask, k)
                last_mark[inst] = s.mark
                if s.funding is not None:
                    rate, hours, nxt = s.funding
                    b = next_boundary(f.t, hours, nxt)
                    if (inst, b) not in self._cancelled:
                        r = rate
                        official = self._official_rate(inst, b) if self.official else None
                        if official is not None:
                            r = official
                        self.fed_official[(inst.leg(), b)] = official is not None
                        pending[(inst, b)] = r
            fx_last = f.fx if f.fx is not None else fx_last
            # 3. decision and orders (+1 ns)
            view = self._view(k, f, last_results)
            intents = decide(k, f.t, view)
            ns0 = f.t * 1_000_000
            if policy_steps:
                self._emit(ns0, 1, "decision", data={"step_seq": k, "intents": len(intents), "state_sha256": (state_hashes[k] if state_hashes and k < len(state_hashes) else None)})
            elif intents:
                self._emit(ns0, 1, "decision", data={"step_seq": None, "intents": len(intents), "state_sha256": None})
            last_results = []
            for raw in intents:
                rec = self._parse(raw, k, f.t)
                self.metas.append(rec)
                self._emit(ns0, 2, "intent", inst=rec.inst, pair_id=rec.pair_id, intent_id=rec.intent_id, position_ref=rec.position_ref,
                           data={"side": "buy" if rec.buy else "sell", "qty": dstr(rec.qty), "order_type": "market" if rec.market else "limit_ioc",
                                 "limit_price": None if rec.limit is None else dstr(rec.limit), "reduce_only": rec.reduce_only, "decision_time_ms": rec.decision_ms})
                last_results.append(self._execute(rec, k, f.t, ns0 + 1, fx_last))
            # 4. valuation (+2 ns)
            self._value(k, f, ns0 + 2, fx_last)
        return self._finish(frames, fx_by_frame=None, state_hashes=state_hashes)

    # ------------------------------------------------------------------ intents

    def _view(self, k: int, f, last_results: list[dict]) -> dict:
        accounts = [{"account": a, "ccy": c, "total": dstr(v), "free": dstr(v)} for (a, c), v in sorted(self.accounts.items())]
        positions = []
        for ref, p in sorted(self.pos.items()):
            if not p.net.is_zero() and p.entry_qty > 0:
                positions.append({"position_ref": ref, "instrument": p.inst.ref(), "side": "long" if p.net > 0 else "short", "qty": dstr(abs(p.net)),
                                  "avg_entry": dstr(p.entry_notional / p.entry_qty)})
        return {"schema": "alphakeel.policy-context/1", "session_id": self.run_id, "step_seq": k, "time_ms": f.t, "time_ns": str(f.t * 1_000_000),
                "history_len": int(self.profile["decision_clock"]["history_len"]), "data_refs": {"frame_index": k, "frame_seq": f.seq, "decided_ms": f.t},
                "accounts": accounts, "positions": positions, "last_results": last_results}

    def _parse(self, raw: dict, k: int, t: int) -> IntentRec:
        iid = raw.get("intent_id")
        if not isinstance(iid, str) or iid in self.seen_ids:
            raise ApiError("intent.duplicate" if iid in self.seen_ids else "request.invalid", f"bad or repeated intent_id {iid!r}")
        self.seen_ids.add(iid)
        inst = Inst.parse(raw["instrument"])
        if inst not in self.insts:
            raise ApiError("intent.unknown_instrument", f"{inst.symbol} is not a tradable contract of this run")
        qty_s = raw["qty"]
        if not isinstance(qty_s, str):
            raise ApiError("intent.non_finite", "qty must be a decimal string")
        try:
            qty = Decimal(qty_s)
        except Exception as e:  # noqa: BLE001
            raise ApiError("intent.non_finite", f"qty {qty_s!r} is not a decimal") from e
        if not qty.is_finite() or "e" in qty_s.lower() or "n" in qty_s.lower():
            raise ApiError("intent.non_finite", f"qty {qty_s!r} is not an exact finite decimal")
        if qty <= 0:
            raise ApiError("intent.non_positive_qty", "qty must be greater than zero")
        if qty > MAX_QTY:
            raise ApiError("intent.overflow", "qty exceeds the per-order limit")
        ot = raw["order_type"]
        if ot not in ("limit_ioc", "market"):
            raise ApiError("capability.order_type", f"order_type {ot!r} is not supported")
        limit = None
        if ot == "limit_ioc":
            if raw.get("limit_price") is None:
                raise ApiError("intent.limit_price_required", "limit_ioc needs limit_price")
            limit = Decimal(raw["limit_price"])
            if not limit.is_finite() or limit <= 0:
                raise ApiError("intent.non_finite", "limit_price must be a positive finite decimal")
        elif raw.get("limit_price") is not None:
            raise ApiError("request.invalid", "a market order has no limit_price")
        buy = raw["side"] == "buy"
        if inst.market == "spot" and not buy and not raw.get("reduce_only"):
            raise ApiError("capability.product", "spot short selling is not modelled")
        if raw["decision_time_ms"] != t:
            raise ApiError("intent.stale_decision" if raw["decision_time_ms"] < t else "intent.out_of_window", f"this step decides at {t}")
        return IntentRec(iid, inst, buy, qty, ot == "market", limit, bool(raw.get("reduce_only")), raw.get("position_ref"), raw.get("pair_id"), t, k, raw)

    # ------------------------------------------------------------------ execution

    def _execute(self, rec: IntentRec, k: int, t: int, ns: int, fx: Decimal | None) -> dict:
        inst = rec.inst
        pdp = self.price_dp[inst]
        qty_eff = order_quantity(rec.qty)
        adj = []
        if qty_eff != rec.qty:
            adj.append({"field": "qty", "from": dstr(rec.qty), "to": dstr(qty_eff), "reason": "f64_roundtrip" if dp_of(rec.qty) <= SIZE_DP else "precision_toward_zero"})
        limit_eff = None
        if rec.limit is not None:
            q = Decimal(1).scaleb(-pdp)
            limit_eff = rec.limit.quantize(q, rounding=ROUND_CEILING if rec.buy else ROUND_FLOOR)
            if limit_eff != rec.limit:
                adj.append({"field": "limit_price", "from": dstr(rec.limit), "to": dstr(limit_eff), "reason": "price_precision"})
        ref = rec.position_ref or f"auto-{rec.intent_id}"
        oid = f"py-{rec.intent_id}"
        base_data = {"requested_qty": dstr(rec.qty), "effective_qty": dstr(qty_eff), "requested_price": None if rec.limit is None else dstr(rec.limit),
                     "effective_price": None if limit_eff is None else dstr(limit_eff), "adjustments": adj}

        def result(status: str, filled: Decimal, code: str | None, reason: str | None, avg: Decimal | None = None, with_order_id: bool = True) -> dict:
            self._emit(ns, 6, "order_result", inst=inst, pair_id=rec.pair_id, intent_id=rec.intent_id, order_id=oid if with_order_id else None, position_ref=ref,
                       data={**base_data, "status": status, "filled_qty": dstr(filled), "reason_code": code, "reason": reason})
            return {"intent_id": rec.intent_id, "status": status, "filled_qty": dstr(filled), "avg_price": None if avg is None else dstr(avg),
                    "reason_code": code}

        # position bookkeeping rules (no guessing outside the profile). Like the service, a position_ref is bound to its
        # contract and side by the first opening INTENT that names it, whether or not that order then fills.
        existing = self.ref_decl.get(ref)
        if rec.reduce_only:
            if existing is None:
                if rec.position_ref is None:
                    raise ApiError("intent.reduce_only_needs_position", "a reduce-only intent must name its position")
                raise ApiError("intent.reduce_only_needs_position", f"position_ref {ref!r} was never opened")
            if existing[0] != inst:
                raise ApiError("request.invalid", f"position_ref {ref!r} belongs to another contract")
            _, long = existing
            if rec.buy == long:
                raise ApiError("request.invalid", "a reduce-only intent must trade against the position side")
            p = self.pos.get(ref)
            if p is None or p.net.is_zero():
                raise Unsupported("a reduce-only order on a position with nothing open is not modelled")
            if qty_eff > abs(p.net):
                raise Unsupported("a reduce-only order larger than the open position is not modelled")
        else:
            if existing is not None:
                if existing[0] != inst:
                    raise ApiError("request.invalid", f"position_ref {ref!r} belongs to another contract")
                if existing[1] != rec.buy:
                    raise ApiError("request.invalid", "an opening intent cannot flip the side of an existing position_ref")
            else:
                self.ref_decl[ref] = (inst, rec.buy)
        if qty_eff.is_zero():
            raise Unsupported("the order quantity is zero after the engine's precision rules")
        quote = self.last_quote.get(inst)
        if quote is None:
            raise Unsupported(f"no quote for {inst.symbol} has been seen yet: the engine's behaviour for that case is not modelled")
        bid, ask, _ = quote
        touch = ask if rec.buy else bid
        # margin: the notional against the account's free balance (total minus open-position maintenance margin)
        ccy = self.instr_meta[inst].quote
        acct = (engine_venue(inst), ccy)
        price_for_margin = limit_eff if limit_eff is not None else touch
        need = qty_eff * price_for_margin
        batch = self._batch.get(acct)
        if batch is None or batch[0] != ns:
            batch = (ns, self._margin_state(acct))
            self._batch[acct] = batch
        engine = self._margin_verdict(rec, qty_eff, need, batch[1])  # the engine checks against the before-batch account
        if self.margin_rule == "cumulative_initial_margin":
            if not rec.reduce_only:
                refusal = self._cumulative_refusal(acct, inst, need, ns, batch[1])
                if refusal is not None:
                    # not submitted to the engine: no order id, no fill; the order does not count towards the instant's total
                    return result("not_submitted", Decimal(0), "not_submitted", refusal, with_order_id=False)
            if engine is None:
                raise Unsupported("the order's margin falls inside the open-position maintenance-margin band, whose price basis is not modelled")
        else:
            sequential = self._margin_verdict(rec, qty_eff, need, self._margin_state(acct))
            if engine is None or sequential is None:
                raise Unsupported("the order's margin falls inside the open-position maintenance-margin band, whose price basis is not modelled")
            if engine != sequential:
                raise Unsupported("several orders on one account at the same decision time: the engine checks each against the balance "
                                  "before any of them fill, which would accept or deny differently from filling them in order; not modelled")
        if not engine:
            return result("denied", Decimal(0), "denied", "insufficient free balance for the initial margin")
        # marketability
        if rec.limit is not None:
            ok = (limit_eff >= ask) if rec.buy else (limit_eff <= bid)
            if not ok:
                return result("cancelled", Decimal(0), "ioc_not_filled", "ioc_not_filled")
        # fill
        fee = q8(qty_eff * touch * self.fee_rate[inst])
        fill_o = self._emit(ns, 3, "fill", inst=inst, pair_id=rec.pair_id, intent_id=rec.intent_id, order_id=oid, position_ref=ref,
                            data={"side": "buy" if rec.buy else "sell", "qty": dstr(qty_eff), "price": dstr(touch), "fee": dstr(fee), "fee_ccy": ccy, "liquidity": "taker"})
        self._emit(ns, 4, "fee", inst=inst, pair_id=rec.pair_id, intent_id=rec.intent_id, order_id=oid, position_ref=ref,
                   data={"amount": dstr(fee), "ccy": ccy, "fill_event_id": f"@{fill_o}"})
        p = self.pos.get(ref)
        if p is None:
            p = Pos(ref, inst, rec.buy, ccy, closed_frame=None)
            self.pos[ref] = p
            self.refs[ref] = (inst, rec.buy)
        was = p.net
        cash = qty_eff * touch
        p.cash += -cash if rec.buy else cash
        p.fees += fee
        one_usdt = self._usdt({ccy: fee}, fx)
        if one_usdt is None:
            raise ApiError("data.fx_missing", "a USDC fill has no USDC/USDT rate")
        p.fees_usdt += one_usdt
        # realised P&L enters the account balance when a position is reduced (average cost); fees always
        realized = Decimal(0)
        if was != 0 and ((was > 0) != rec.buy):
            avg = p.entry_notional / p.entry_qty
            red = min(qty_eff, abs(was))
            realized = (touch - avg) * red if was > 0 else (avg - touch) * red
        p.net = was + qty_eff if rec.buy else was - qty_eff
        if (rec.buy == p.long) or was == 0:
            p.entry_qty += qty_eff
            p.entry_notional += qty_eff * touch
        self.accounts[acct] = self.accounts[acct] + q8(realized) - fee
        p.closed_frame = k if p.net == 0 else None
        change = "opened" if was == 0 else "closed" if p.net == 0 else "increased" if abs(p.net) > abs(was) else "reduced"
        avg_entry = None if p.net == 0 else dstr(p.entry_notional / p.entry_qty)
        self._emit(ns, 5, "position", inst=inst, pair_id=rec.pair_id, intent_id=rec.intent_id, order_id=oid, position_ref=ref,
                   data={"side": "flat" if p.net == 0 else "long" if p.net > 0 else "short", "qty": dstr(abs(p.net)), "avg_entry": avg_entry, "change": change})
        self.fills.append({"pos": ref, "frame": k, "ccy": ccy, "buy": rec.buy, "qty": qty_eff, "px": touch, "fee": fee})
        status = "filled"
        return result(status, qty_eff, None, None, touch)

    def _margin_state(self, acct: tuple[str, str]) -> tuple:
        """(total balance, maintenance-margin lower and upper bound, long and short open qty by contract) of one account."""
        total = self.accounts[acct]
        lo = hi = Decimal(0)
        longs: dict[Inst, Decimal] = {}
        shorts: dict[Inst, Decimal] = {}
        for p in self.pos.values():
            if p.net == 0 or (engine_venue(p.inst), p.ccy) != acct:
                continue
            q = abs(p.net)
            (longs if p.net > 0 else shorts)[p.inst] = (longs if p.net > 0 else shorts).get(p.inst, Decimal(0)) + q
            rate = Decimal(0) if p.inst.market == "spot" else self.maint_margin
            if rate is None:
                hi = None
                continue
            bid, ask, _ = self.last_quote[p.inst]
            pxs = [bid, ask] + ([p.entry_notional / p.entry_qty] if p.entry_qty else [])
            lo += q * min(pxs) * rate
            if hi is not None:
                hi += q * max(pxs) * rate
        return (total, lo, hi, longs, shorts)

    def _cumulative_refusal(self, acct: tuple[str, str], inst: Inst, need: Decimal, ns: int, state: tuple) -> str | None:
        """Profile v2 same-instant check. None = admitted (and counted); a message = the order is not submitted.

        The free balance of the account before the first order of this instant lies between ``total - hi`` and ``total - lo``
        (the maintenance-margin price basis is not modelled); an outcome that depends on where in that band it lies is refused.
        """
        initial = need / self.leverage if (inst.market == "perp" and self.leverage > 1) else need
        cur = self._instant.get(acct)
        used = cur[1] if cur is not None and cur[0] == ns else Decimal(0)
        total_used = used + initial
        total, lo, hi, _, _ = state
        if hi is not None and total_used <= total - hi:
            self._instant[acct] = (ns, total_used)
            return None
        if total_used > total - lo:
            return (f"same-instant margin: cumulative opening notional {dstr(total_used)} on {acct[0]}/{acct[1]} "
                    "exceeds the free balance at the decision instant")
        raise Unsupported("the cumulative same-instant margin falls inside the open-position maintenance-margin band, whose price basis is not modelled")

    @staticmethod
    def _margin_verdict(rec: "IntentRec", qty: Decimal, need: Decimal, state: tuple) -> bool | None:
        """True = passes the margin check, False = denied, None = depends on the unmodelled maintenance price basis."""
        total, lo, hi, longs, shorts = state
        opposite = (shorts if rec.buy else longs).get(rec.inst, Decimal(0))
        if rec.reduce_only or qty <= opposite:
            return True  # position-reducing orders skip the engine's margin check
        if hi is not None and need <= total - hi:
            return True
        if need > total - lo:
            return False
        return None

    def _usdt(self, by: dict[str, Decimal], fx: Decimal | None) -> Decimal | None:
        tot = Decimal(0)
        for c, v in by.items():
            if c == "USDT":
                tot += v
            elif c == "USDC":
                if fx is None:
                    if v == 0:
                        continue
                    return None
                tot += v * fx
            else:
                raise Unsupported(f"unsupported settlement currency {c}")
        return tot

    # ------------------------------------------------------------------ funding

    def _settle(self, inst: Inst, b: int, rate: Decimal, *, last_mark_at, k: int) -> None:
        """Funding at boundary ``b`` for every open position of ``inst`` (price: last mark seen at or before b)."""
        mk = None
        # last mark at or before b: frame data up to and including b
        kk = max(0, k if last_mark_at is self.frames[k] else k - 1)
        for j in range(kk, -1, -1):
            if self.frames[j].t <= b and inst in self.snaps[j]:
                mk = self.snaps[j][inst].mark
                break
        if mk is None:
            return
        mk = mk.quantize(Decimal(1).scaleb(-self.price_dp[inst]), rounding=ROUND_HALF_EVEN)
        for ref, p in sorted(self.pos.items()):
            if p.inst != inst or p.net == 0:
                continue
            notional = abs(p.net) * mk
            amount = q8(notional * rate * (Decimal(-1) if p.net > 0 else Decimal(1)))
            p.funding += amount
            acct = (engine_venue(inst), p.ccy)
            self.accounts[acct] += amount
            official = self.fed_official.get((inst.leg(), b), False)
            if official:
                self.official_n += 1
            else:
                self.estimate_fallbacks += 1
            self._emit(b * 1_000_000, 0, "funding", inst=inst, position_ref=ref, data={
                "boundary_ms": b, "rate": dstr(rate), "rate_source": "official_settlement" if official else "predicted_estimate",
                "settlement_price": dstr(mk), "position_qty": dstr(p.net), "amount": dstr(amount), "ccy": p.ccy})
            self.fund_events.append({"pos": ref, "ccy": p.ccy, "amount": amount, "official": official, "boundary": b, "inst": inst})

    # ------------------------------------------------------------------ valuation

    def _value(self, k: int, f, ns: int, fx: Decimal | None) -> None:
        by: dict[str, Decimal] = {c: Decimal(0) for c in self.start_by_ccy}
        stale = False
        exposure: dict[tuple[str, str], Decimal] = {}
        for ref, p in self.pos.items():
            by[p.ccy] = by.get(p.ccy, Decimal(0)) + p.cash - p.fees + p.funding
            if p.net == 0:
                continue
            quote = self.last_quote.get(p.inst)
            if quote is None:
                stale = True
                continue
            bid, ask, qk = quote
            if qk != k:
                stale = True
            px = bid if p.net > 0 else ask
            value = p.net * px
            exit_fee = abs(p.net) * px * self.fee_rate[p.inst]
            by[p.ccy] += value - exit_fee
            key = (engine_venue(p.inst), p.ccy)
            exposure[key] = exposure.get(key, Decimal(0)) + abs(p.net) * px
        for key, v in exposure.items():
            self.peak_exposure[key] = max(self.peak_exposure.get(key, Decimal(0)), v)
        eq = {c: self.start_by_ccy.get(c, Decimal(0)) + v for c, v in by.items()}
        close_now = None if stale else self._usdt(by, fx)
        for (a, c), tot in sorted(self.accounts.items()):
            key = f"{a}:{c}"
            if self._last_bal.get(key) != tot:
                self._last_bal[key] = tot
                self._emit(ns, 7, "balance", data={"account": a, "ccy": c, "total": dstr(tot)})
        self._emit(ns, 8, "equity", data={"by_ccy": {c: dstr(q8(v)) for c, v in sorted(eq.items())}, "fx": None if fx is None else dstr(fx),
                                          "close_now_usdt": None if close_now is None else dstr(q8(close_now)), "stale": stale})
        self.valuations.append({"k": k, "t": f.t, "by": by, "stale": stale, "fx": fx, "close_now": close_now})

    # ------------------------------------------------------------------ outputs

    def _finish(self, frames, fx_by_frame, state_hashes) -> SimResult:
        # strict official funding: every applied settlement must come from an official event
        if self.official and self.strict and self.estimate_fallbacks:
            raise ApiError("data.funding_missing", f"strict official settlement: {self.estimate_fallbacks} settlement(s) have no unique official event; nothing was estimated")
        evs = sorted(self.evs, key=lambda e: (e.ns, e.rank, e.ord))
        id_of = {e.ord: f"e{i + 1:08d}" for i, e in enumerate(evs)}
        events = []
        for i, e in enumerate(evs):
            data = dict(e.data)
            if e.kind == "fee" and isinstance(data.get("fill_event_id"), str) and data["fill_event_id"].startswith("@"):
                data["fill_event_id"] = id_of[int(data["fill_event_id"][1:])]
            events.append({"event_id": f"e{i + 1:08d}", "run_id": self.run_id, "kind": e.kind, "time_ms": e.ns // 1_000_000, "time_ns": str(e.ns), "seq": i,
                           "instrument": e.instrument, "pair_id": e.pair_id, "intent_id": e.intent_id, "order_id": e.order_id,
                           "position_ref": e.position_ref, "refs": [], "data": data})
        header = {"schema": "alphakeel.events/1", "run_id": self.run_id, "count": len(events)}
        body = contract.canonical_bytes(header) + b"\n" + b"".join(contract.canonical_bytes(e) + b"\n" for e in events)
        import hashlib

        sha = hashlib.sha256(body).hexdigest()
        result = self._result(frames, sha, len(events))
        return SimResult(events, result, body, sha, self.profile, self.pack.accesses(), self.step_log)

    def _result(self, frames, events_sha: str, n_events: int) -> dict:
        fx_by_frame = []
        last = None
        for f in frames:
            last = f.fx if f.fx is not None else last
            fx_by_frame.append(last)

        def usdt_at(by: dict[str, Decimal], k: int) -> Decimal:
            v = self._usdt(by, fx_by_frame[min(k, len(frames) - 1)])
            if v is None:
                raise ApiError("data.fx_missing", "a USDC leg has no USDC/USDT rate")
            return v

        realized = price_pnl = funding_sum = fees_sum = Decimal(0)
        realized_native: dict[str, Decimal] = {}
        by_ccy: dict[str, list[Decimal]] = {}
        closed_n = open_n = 0
        for ref, p in sorted(self.pos.items()):
            c = by_ccy.setdefault(p.ccy, [Decimal(0), Decimal(0), Decimal(0)])
            c[0] += p.cash
            c[1] += p.fees
            c[2] += p.funding
            if p.closed_frame is None:
                open_n += 1
                continue
            closed_n += 1
            cf = p.closed_frame
            pp = usdt_at({p.ccy: p.cash}, cf)
            fund = usdt_at({p.ccy: p.funding}, cf)
            realized += pp + fund - p.fees_usdt
            price_pnl += pp
            funding_sum += fund
            fees_sum += p.fees_usdt
            realized_native[p.ccy] = realized_native.get(p.ccy, Decimal(0)) + p.cash + p.funding - p.fees
        last_val = self.valuations[-1] if self.valuations else None
        last_fx = fx_by_frame[-1]
        close_now = None
        points = []
        peak = Decimal(0)
        max_dd = Decimal(0)
        for v in self.valuations:
            net = None if v["stale"] else self._usdt(v["by"], fx_by_frame[v["k"]])
            points.append(net)
            if net is not None:
                peak = max(peak, net)
                max_dd = max(max_dd, peak - net)
        close_now = points[-1] if points else None
        open_value = fx_reval = None
        if last_val is not None:
            open_native = dict(last_val["by"])
            for c, v in realized_native.items():
                open_native[c] = open_native.get(c, Decimal(0)) - v
            rv = self._usdt(realized_native, last_fx)
            fx_reval = None if rv is None else rv - realized
            open_value = None if last_val["stale"] else self._usdt(open_native, last_fx)
        amounts_ok = True
        problems = []
        if close_now is not None and open_value is not None and fx_reval is not None:
            gap = close_now - (realized + fx_reval + open_value)
            if abs(gap) > Decimal("1e-12"):
                amounts_ok = False
                problems.append(f"close-now does not equal realized + revaluation + open value (off by {gap})")
        end_by = {c: self.start_by_ccy[c] + (last_val["by"].get(c, Decimal(0)) if last_val else Decimal(0)) for c in self.start_by_ccy}
        peak_capital = Decimal(0)
        for (acct, c), exp in self.peak_exposure.items():
            lev = Decimal(1) if acct.endswith("SPOT") else self.leverage
            peak_capital += self._usdt({c: q8(exp / lev)}, last_fx) or Decimal(0)
        d8 = lambda x: dstr(q8(x))  # noqa: E731
        opt = lambda x: None if x is None else d8(x)  # noqa: E731
        counts = {"intents": len(self.metas), "orders": len(self.metas), "fills": len(self.fills), "funding_events": len(self.fund_events),
                  "valuation_points": len(self.valuations), "positions_closed": closed_n, "positions_open_at_end": open_n}
        res = {
            "schema": "alphakeel.result/1", "run_id": self.run_id, "mode": "fixed_intent_replay", "side": "python_local",
            "execution_ok": True, "amounts_ok": amounts_ok,
            "amounts": {"close_now": opt(close_now), "realized": d8(realized), "open_value": opt(open_value), "price_pnl": d8(price_pnl),
                        "funding": d8(funding_sum), "fees": d8(fees_sum), "fx_revaluation": opt(fx_reval)},
            "by_currency": {c: {"cash_flow": d8(v[0]), "fees": d8(v[1]), "funding": d8(v[2])} for c, v in sorted(by_ccy.items())},
            "equity": {"start_by_ccy": {c: d8(v) for c, v in sorted(self.start_by_ccy.items())}, "end_by_ccy": {c: d8(v) for c, v in sorted(end_by.items())},
                       "end_close_now_usdt": opt(close_now)},
            "drawdown": {"max": d8(max_dd) if any(p is not None for p in points) else None, "points": sum(1 for p in points if p is not None)},
            "capital": {"peak_margin_usdt": d8(peak_capital)},
            "counts": counts, "data_quality": {"stale_points": sum(1 for v in self.valuations if v["stale"] and any(p.net != 0 for p in self.pos.values())),
                                               "official_settlements": self.official_n, "estimate_fallbacks": self.estimate_fallbacks},
            "verification": {"engine_recount": "not_applicable", "native_rule_ledger": "not_applicable", "external_python": "not_applicable"},
            "not_applicable_metrics": ["win_rate", "native_rule_ledger"],
            "limitations": ["Independent Decimal simulation of the AlphaKeel execution profile; scan snapshots only, no depth, queue or impact.",
                            "Margin: an opening order is denied when its notional exceeds the account's free balance (total minus open-position "
                            "maintenance margin), checked against the account before any same-time order fills (as observed in the engine); "
                            + ("opening orders of one decision instant are first checked cumulatively per account (perpetual: notional / leverage, "
                               "spot: full notional; fees not included): an order over the free balance is not submitted. "
                               if self.margin_rule == "cumulative_initial_margin" else
                               "orders whose same-time outcome would differ from that check are refused, not guessed. ")
                            + "Orders inside the maintenance-margin band are refused, not guessed."],
            "event_log": {"name": "events.jsonl", "sha256": events_sha, "count": n_events}, "problems": problems,
        }
        return res


def dumps_exact(doc: dict) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
