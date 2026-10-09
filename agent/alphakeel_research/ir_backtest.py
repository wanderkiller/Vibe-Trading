"""Long-history Strategy IR backtest on AlphaKeel DATASET packs (1m klines + official settlements, 2019 onwards).

This is RESEARCH EVIDENCE, not reconciliation input: the scan-frame reconciliation with AlphaKeel's Rust evaluator is
``workflow.ir_review`` (real top of book, predicted funding, the engine's execution profile). Here the frames come from
``ir_views`` dataset frames (``view = "kline_close_synthetic_bbo"``, approximations listed in ``APPROXIMATIONS`` and in
every run card) and execution is a small separate Decimal simulator (``Sim`` below), because ``sim.Simulator`` is bound to
a scan pack (scan frames, execution profile, quote-age and margin rules) and cannot be driven by an external view.

Decision loop (the same state handling as ``ir_policy``): at every instant ``start + k * step`` that has at least one leg in
its dataset frame, (1) settle funding of every official settlement whose time is ``<=`` the instant, (2) drop pairs closed at
an earlier instant (their base cools down from THIS instant, as ``ir_policy`` reconciles one step later), (3) call
``ir.decide`` with the open pairs (``net_now`` per README §5a, null when a leg has no quote in this frame), (4) close exits
at this frame's touch only when both legs are quoted in this frame (README §5a hold rule; never an older quote), (5) open
entries with ``qty = notional_per_leg / long.ask`` half-even to 8 places (an entry with ``qty = 0`` takes no trade number),
both legs the same qty, named ``n<trade>-<L|S>-<open|close>`` / ``pair-<trade>``.

``Sim`` conventions (stated in the run card): fills are complete and immediate at the frame's touch (long opens buy at the
ask, short opens sell at the bid, closes the other side); a taker fee ``q8(qty * price * taker_fee)`` per fill; funding at
each official settlement time ``t`` on every open position of that contract: ``q8(qty * mark * rate)`` paid by longs and
received by shorts for a positive rate (``sim.py``'s sign), ``mark`` = close of the last 1m bar closed at or before ``t``
(the settlement's own mark price when no bar is there); ``q8`` = half-even to 1e-8. No margin, leverage, liquidation,
partial fills, latency or funding of spot legs. Equity = realized + every open pair valued close-now (§5a ``net_now``
formula at the touch; a leg without a quote this instant at its last close). Amounts are in the legs' quote currency:
only ``USDT`` is accepted (no FX model).

Memory is bounded: kline rows are spilled per instrument to the work directory in one pass over the pack's objects and
streamed back in time order; frames, decisions and equity points are written as they are produced.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import shutil
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

from . import canon, ir_views
from . import ir as IR
from .errors import ApiError, EvidenceError, Unsupported
from .packfile import Chain, Inst, Pack, Pit

RUN_CARD_SCHEMA = "vibe-trading.ir-backtest-run-card/1"
DAY_MS = 86_400_000
MINUTE_MS = 60_000
#: data before the first decision instant: the 1440 bars of the 24 h volume and the last settlement (intervals <= 8 h)
WARMUP_MS = DAY_MS
UNIT = Decimal("0.00000001")

APPROXIMATIONS = [
    "touch: bid = ask = close of the last 1m trade bar closed at the decision time (close_time_ms <= now); spread 0",
    "bbo = true is synthetic (no order book in the dataset); price_estimated is false by construction",
    "mark / funding_px = that close (no mark or index series)",
    "funding_rate = the last official settlement visible at the decision time (available_at, or settlement time + the pack's "
    "assumed delay): a last_settled proxy for the predicted rate live scans use; the IR's assumptions.funding_estimate is unchanged",
    "interval_hours from that settlement's interval_seconds; when the dataset leaves it null (Binance/OKX/Bybit/Bitget/Gate/"
    "Aster history) it is INFERRED from the gap between the two latest visible settlements, accepted only at exactly 1, 2, 4 "
    "or 8 h (perps with neither are left out; after an interval change the inference lags one settlement); "
    "next_funding_ms = settlement time + interval",
    "volume_quote = sum of volume_quote over the 1440 bars of the 24 h ending with the last closed bar (null unless all 1440 exist)",
    "a funding settlement on an open leg with no 1m bar in the 5 minutes before it and no mark price in the settlement row "
    "is NOT booked (counts.funding_unpriced); a leg that stops trading is valued at its last close (stale) and cannot exit",
    f"a leg whose last closed bar is older than {ir_views.DATASET_MAX_BAR_AGE_MS // MINUTE_MS} minutes is not in the frame",
    "base / quote_ccy from the pack's instruments table, or from the declared instrument when the dataset pack leaves them null",
]
SIM_CONVENTIONS = {
    "fills": "complete and immediate at the frame's touch (long open buys the ask, short open sells the bid, closes the other side)",
    "fee": "q8(qty * price * taker_fee) per fill, taker_fee from the view's fee table",
    "funding": "at each official settlement time t on open positions of that contract: q8(qty * mark * rate), paid by longs and "
               "received by shorts for a positive rate; mark = close of the last 1m bar closed at or before t (else the settlement's mark)",
    "rounding": "q8 = half-even to 1e-8; quantity half-even to 8 places (README §5a)",
    "equity": "realized + open pairs valued close-now (README §5a net_now formula at the touch; a leg without a quote at its last close)",
    "not_modelled": ["margin", "leverage", "liquidation", "partial fills", "latency", "slippage beyond the synthetic touch",
                     "FX (USDT legs only)", "funding on spot legs"],
}


def q8(x: Decimal) -> Decimal:
    return x.quantize(UNIT, rounding=ROUND_HALF_EVEN)


def _s(x: Decimal) -> str:
    return IR.fmt(x)


def utc_day(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%d")


def _jsonl(f, doc: dict) -> None:
    f.write(json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str) + "\n")


# ---------------------------------------------------------------------------------------------------------------------
# instruments and the dataset pack
# ---------------------------------------------------------------------------------------------------------------------

_SCALE_PREFIX = __import__("re").compile(r"^(?:10{3,6}|1M)(?=[A-Z])|^k(?=[A-Z])")


def scaled_symbol(symbol: str) -> bool:
    """A contract whose price unit is a multiple of its coin (Binance ``1000PEPEUSDT``, ``1MBABYDOGEUSDT``, Hyperliquid
    ``kPEPE``): the same prefixes AlphaKeel's ``screener::classify_base`` treats as scaled."""
    return bool(_SCALE_PREFIX.match(symbol))


def universe_instruments(ir_doc: dict, declared: list[dict]) -> list[dict]:
    """The declared instruments the IR can trade: on its venues (``universe.pairs``), of its shape's markets. Keeps any
    declared ``base``/``quote``; ``{venue, market, symbol}`` order is (venue, market, symbol)."""
    loaded = IR.load(ir_doc)
    u = loaded.universe
    if any(q != "USDT" for q in u["quote_ccys"]):
        raise Unsupported("the dataset backtest has no FX model: universe.quote_ccys must be [\"USDT\"]")
    markets = {"cross_perp": {"perp"}, "spot_perp": {"spot", "perp"}}[u["shape"]]
    venues = None if u["pairs"] == "any" else {v for pair in u["pairs"] for v in pair}
    out: dict[tuple, dict] = {}
    for d in declared:
        if not isinstance(d, dict) or not all(isinstance(d.get(k), str) and d.get(k) for k in ("venue", "market", "symbol")):
            raise ApiError("request.invalid", "instruments must be [{venue, market, symbol, base?, quote?}]", field="instruments")
        if d["venue"] not in IR.VENUES or d["market"] not in markets or (venues is not None and d["venue"] not in venues):
            continue
        if scaled_symbol(d["symbol"]):
            raise Unsupported(f"{d['venue']}:{d['symbol']} is a price-scaled contract (1000x / 1M / k prefix): the dataset view pairs "
                              "legs by base and gives both the same quantity, it has no price-scale model -- leave it out")
        out[(d["venue"], d["market"], d["symbol"])] = {k: d[k] for k in ("venue", "market", "symbol", "base", "quote") if d.get(k) is not None}
    if not out:
        raise ApiError("data.insufficient", "none of the declared instruments is on the IR's venues and markets", field="instruments")
    return [out[k] for k in sorted(out)]


def dataset_request(instruments: list[dict], start_ms: int, end_ms: int, *, accept_partial: bool = False) -> dict:
    """Dataset-pack request: 1m trade klines + the latest funding dataset for the instruments, ``WARMUP_MS`` before start."""
    from .client import Client

    refs = [{"venue": d["venue"], "market": d["market"], "symbol": d["symbol"]} for d in instruments]
    has_perp = any(d["market"] == "perp" for d in instruments)
    return Client.dataset_pack_request(start_ms=start_ms - WARMUP_MS, end_ms=end_ms, instruments=refs,
                                       funding_dataset=None if has_perp else "none", market_tables=["kline_1m"],
                                       price_kinds=["trade"], accept_partial=accept_partial)


def check_pack(pack: Pack, request: dict) -> None:
    """The pack must be the dataset pack asked for (window, instruments, tables, versions); a swapped pack is refused."""
    lock = pack.lock
    req = lock.get("request") or {}
    if not pack.is_dataset:
        raise EvidenceError("evidence.reference", "the pack is a scan pack; the long-history backtest needs a dataset pack")
    want = request["window"]
    if req.get("window") != want or req.get("instruments") != request["instruments"]:
        raise EvidenceError("evidence.reference", "the pack does not match the request (window or instruments)")
    if "kline_1m" not in ((req.get("dataset") or {}).get("market_tables") or []) or pack.dataset_version("market") is None:
        raise EvidenceError("evidence.reference", "the pack holds no versioned kline_1m market dataset")
    if any(i["market"] == "perp" for i in request["instruments"]) and pack.dataset_version("funding") is None:
        raise EvidenceError("evidence.reference", "the pack holds no versioned funding dataset")


def pack_summary(pack: Pack) -> dict:
    lock = pack.lock
    return {"pack_id": pack.id, "pack_sha256": lock["pack_sha256"], "window": (lock.get("windows") or {}).get("actual") or lock["request"]["window"],
            "accept_partial": bool(lock["request"].get("accept_partial")),
            "dataset_versions": {d["name"]: d["dataset_version"] for d in pack.datasets()},
            "datasets": [{"name": d["name"], "dataset_version": d["dataset_version"], "snapshot_sha256": d.get("snapshot_sha256"),
                          "visibility": d.get("visibility")} for d in pack.datasets()],
            "funding_dataset_version": pack.dataset_version("funding")}


# ---------------------------------------------------------------------------------------------------------------------
# streaming dataset frames
# ---------------------------------------------------------------------------------------------------------------------

def spill_klines(pack: Pack, insts: list[Inst], start_ms: int, end_ms: int, work: Path) -> tuple[dict[Inst, Path], dict[Inst, Chain]]:
    """One pass over the pack's ``kline_1m`` objects: each wanted instrument's trade bars in ``[start, end)`` go to its own
    file (``t close_time close volume_quote`` per line, time order checked; ``close_time`` must be ``t + 59999``).

    Also returns each instrument's row-digest chain (the ``run-manifest.inputs`` definition, ``Pack.accesses``), computed
    while streaming: ``Pack.accesses`` would re-read every row into memory for a multi-year window."""
    work.mkdir(parents=True, exist_ok=True)
    paths = {i: work / f"{i.venue}-{i.market}-{i.symbol}.klines".replace("/", "_") for i in insts}
    files = {i: open(p, "w", encoding="utf-8") for i, p in paths.items()}
    last: dict[Inst, int] = {}
    chains = {i: Chain() for i in insts}
    try:
        for venue in sorted({i.venue for i in insts}):
            want = {(i.market, i.symbol): i for i in insts if i.venue == venue}
            by_symbol: dict[str, list[Inst]] = {}
            for i in want.values():
                by_symbol.setdefault(i.symbol, []).append(i)
            tag = f"market/kline_1m/{venue}/"
            for o in sorted((o for o in pack.lock["objects"] if o["role"] == "market" and o["name"].startswith(tag)), key=lambda o: o["name"]):
                if (o["last_ms"] or 0) < start_ms or (o["first_ms"] or 0) >= end_ms:
                    continue
                for r in pack._read(o["name"]):
                    if r[1] not in by_symbol or not (start_ms <= r[0] < end_ms):
                        continue
                    for i in by_symbol[r[1]]:
                        chains[i].push(r[0], r)
                    if r[10] != "trade":
                        continue
                    if r[2] != r[0] + ir_views.KLINE_SPAN_MS:
                        raise EvidenceError("evidence.reference", f"kline at {r[0]} of {venue}:{r[1]} has close_time_ms {r[2]}, not t + 59999")
                    for i in by_symbol[r[1]]:  # a symbol is one market per venue in practice; both get the row otherwise
                        if i in last and r[0] <= last[i]:
                            raise EvidenceError("evidence.reference", f"klines of {i.leg()} are not in strict time order at {r[0]}")
                        last[i] = r[0]
                        files[i].write(f"{r[0]} {r[2]} {r[6]} {r[8]}\n")
                pack._mem.pop(o["name"], None)  # bounded memory: one decoded object at a time
    finally:
        for f in files.values():
            f.close()
    return paths, chains


class _Bars:
    """One instrument's spilled bars, advanced in time; keeps the 24 h rolling quote volume exactly."""

    def __init__(self, path: Path):
        self._f = open(path, encoding="utf-8")
        self._next: tuple[int, int, Decimal, Decimal] | None = self._read()
        self.last: tuple[int, int, Decimal, Decimal] | None = None  # t, close_time, close, volume_quote
        self._win: deque[tuple[int, Decimal]] = deque()
        self._sum = Decimal(0)

    def _read(self):
        ln = self._f.readline()
        if not ln:
            return None
        t, ct, c, v = ln.split()
        return int(t), int(ct), Decimal(c), Decimal(v)

    def advance(self, now: int) -> None:
        while self._next is not None and self._next[1] <= now:
            b = self._next
            self.last = b
            self._win.append((b[0], b[3]))
            self._sum += b[3]
            while self._win and self._win[0][0] <= b[0] - ir_views.DATASET_VOLUME_BARS * MINUTE_MS:
                self._sum -= self._win.popleft()[1]
            self._next = self._read()

    def volume(self) -> Decimal | None:
        return self._sum if len(self._win) == ir_views.DATASET_VOLUME_BARS else None

    def close(self) -> None:
        self._f.close()


@dataclass
class _Settle:
    t: int
    visible_ms: int
    rate: Decimal
    interval_seconds: int | None
    mark: Decimal | None


class DatasetCursor:
    """Streams the dataset frames of ``ir_views.dataset_frame_legs`` in time order (same legs, bounded memory)."""

    def __init__(self, pack: Pack, instruments: list[dict], start_ms: int, end_ms: int, *, fees: dict | None, work: Path):
        self.decl = sorted(instruments, key=lambda d: (d["venue"], d["market"], d["symbol"]))
        metas = pack.instruments()
        self.insts = [Inst(d["venue"], d["market"], d["symbol"]) for d in self.decl]
        self.identity: dict[Inst, tuple[str, str]] = {}
        self.fee: dict[Inst, str | None] = {}
        for d, i in zip(self.decl, self.insts):
            base, quote = ir_views.instrument_identity(metas.get(i), d)
            if base is None or quote is None:
                raise ApiError("request.invalid", f"{i.leg()}: base/quote are not in the pack's instruments table; declare them "
                               "in the instruments file ({venue, market, symbol, base, quote})", field="instruments")
            self.identity[i] = (base, quote)
            self.fee[i] = ir_views.dataset_fee(fees, i.venue, i.market)
        lo = pack.lock["request"]["window"]["start_ms"]
        paths, self.chains = spill_klines(pack, self.insts, lo, end_ms, work)
        self.bars = {i: _Bars(paths[i]) for i in self.insts}
        # official settlements (small): visibility for the view, settlement times for the cashflows
        delay = None
        self.settles: dict[Inst, list[_Settle]] = {}
        if any(i.market == "perp" for i in self.insts):
            delay = Pit(pack, end_ms)._delay
            if delay is None:
                raise EvidenceError("data.funding_missing", "the pack has no funding dataset block: it holds no settlements")
        for i in self.insts:
            if i.market != "perp":
                continue
            rows = [s for s in pack.settlements(i, lo, end_ms) if s.event_kind in ir_views.SETTLEMENT_KINDS]
            self.settles[i] = [_Settle(s.t, s.available_at_ms if s.available_at_ms is not None else s.t + delay, s.rate,
                                       s.interval_seconds, s.mark_price) for s in rows]
        self._vis = {i: sorted(v, key=lambda s: (s.visible_ms, s.t)) for i, v in self.settles.items()}
        self._vis_ptr = {i: 0 for i in self._vis}
        self._vis_last: dict[Inst, _Settle | None] = {i: None for i in self._vis}
        self._vis_prev: dict[Inst, _Settle | None] = {i: None for i in self._vis}
        self.now = None

    def advance(self, now: int) -> None:
        if self.now is not None and now < self.now:
            raise ValueError("the cursor only moves forward")
        self.now = now
        for b in self.bars.values():
            b.advance(now)
        for i, rows in self._vis.items():
            k = self._vis_ptr[i]
            while k < len(rows) and rows[k].visible_ms <= now:
                cur = self._vis_last[i]
                if cur is None or rows[k].t > cur.t:
                    self._vis_prev[i] = cur
                    self._vis_last[i] = rows[k]
                elif rows[k].t == cur.t:
                    self._vis_last[i] = rows[k]
                k += 1
            self._vis_ptr[i] = k

    def last_close(self, inst: Inst) -> Decimal | None:
        b = self.bars[inst].last
        return None if b is None else b[2]

    def legs(self) -> list[dict]:
        now = self.now
        out = []
        for i in self.insts:
            fee = self.fee[i]
            b = self.bars[i]
            if fee is None or b.last is None or now - b.last[1] > ir_views.DATASET_MAX_BAR_AGE_MS:
                continue
            st = None
            if i.market == "perp":
                s = self._vis_last[i]
                if s is not None and s.t < now - ir_views.SETTLEMENT_LOOKBACK_MS:
                    s = None  # same lookback as the point-in-time function
                prev = self._vis_prev[i]
                if prev is not None and prev.t < now - ir_views.SETTLEMENT_LOOKBACK_MS:
                    prev = None
                st = None if s is None else ir_views.settlement_tuple(s, prev)
            base, quote = self.identity[i]
            leg = ir_views.dataset_leg(venue=i.venue, market=i.market, symbol=i.symbol, base=base, quote_ccy=quote, close=b.last[2],
                                       close_time_ms=b.last[1], volume_quote=b.volume(), settlement=st, taker_fee=fee)
            if leg is not None:
                out.append(leg)
        return out

    def settlements_until(self, end_incl: int, after: int | None) -> list[tuple[int, Inst, _Settle]]:
        """Settlements with ``after < t <= end_incl`` (time order, then instrument order)."""
        out = []
        for i, rows in self.settles.items():
            out.extend((s.t, i, s) for s in rows if (after is None or s.t > after) and s.t <= end_incl)
        out.sort(key=lambda x: (x[0], x[1].leg()))
        return out

    def inputs(self, pack: Pack) -> list[dict]:
        """``run-manifest.inputs``-style entries: the streamed klines' chains + the pack's own log (settlements)."""
        out = [{"table": "kline_1m", "instrument": i.ref(), "first_ms": c.first_ms, "last_ms": c.last_ms, "rows": c.rows,
                "chain_sha256": c.hexdigest()} for i, c in self.chains.items() if c.rows]
        return out + [a for a in pack.accesses() if a["table"] != "kline_1m"]

    def close(self) -> None:
        for b in self.bars.values():
            b.close()


# ---------------------------------------------------------------------------------------------------------------------
# simulator
# ---------------------------------------------------------------------------------------------------------------------

@dataclass
class Leg:
    ref: str
    inst: Inst
    long: bool
    qty: Decimal
    entry_px: Decimal
    fee_rate: Decimal
    entry_fee: Decimal
    funding: Decimal = Decimal(0)
    exit_px: Decimal | None = None
    exit_fee: Decimal = Decimal(0)

    def price_pnl(self, px: Decimal) -> Decimal:
        return self.qty * ((px - self.entry_px) if self.long else (self.entry_px - px))

    def net_now(self, px: Decimal) -> Decimal:
        """README §5a per leg: touch pnl + settled funding - entry fee - close-now taker fee (exact)."""
        return self.price_pnl(px) + self.funding - self.entry_fee - self.qty * px * self.fee_rate


@dataclass
class Trade:
    trade: int
    key: str
    pair_id: str
    base: str
    opened_ms: int
    long: Leg
    short: Leg
    entry_features: dict = field(default_factory=dict)
    closed_ms: int | None = None
    reason: str | None = None

    def legs(self) -> tuple[Leg, Leg]:
        return self.long, self.short

    def realized(self) -> Decimal:
        return sum((leg.price_pnl(leg.exit_px) + leg.funding - leg.entry_fee - leg.exit_fee for leg in self.legs()), Decimal(0))

    def record(self) -> dict:
        def leg(x: Leg) -> dict:
            return {"position_ref": x.ref, "instrument": x.inst.ref(), "side": "long" if x.long else "short", "qty": _s(x.qty),
                    "entry_price": _s(x.entry_px), "exit_price": None if x.exit_px is None else _s(x.exit_px), "entry_fee": _s(x.entry_fee),
                    "exit_fee": _s(x.exit_fee), "funding": _s(x.funding),
                    "price_pnl": None if x.exit_px is None else _s(x.price_pnl(x.exit_px))}
        closed = self.closed_ms is not None
        return {"trade": self.trade, "pair_key": self.key, "pair_id": self.pair_id, "base": self.base, "opened_ms": self.opened_ms,
                "closed_ms": self.closed_ms, "status": "closed" if closed else "open", "exit_reason": self.reason,
                "long": leg(self.long), "short": leg(self.short), "realized": _s(self.realized()) if closed else None}


class Sim:
    """Decimal execution and accounting for the dataset backtest (conventions in ``SIM_CONVENTIONS``)."""

    def __init__(self):
        self.open: dict[str, Trade] = {}
        self.realized = Decimal(0)
        self.fees = Decimal(0)
        self.funding = Decimal(0)
        self.price_pnl = Decimal(0)
        self.funding_events = 0
        self.funding_unpriced = 0
        self.opened = 0
        self.closed = 0

    def open_pair(self, n: int, pair: dict, qty: Decimal, t: int, features: dict | None = None) -> Trade:
        legs = []
        for side, lg, px_key in (("L", pair["long"], "ask"), ("S", pair["short"], "bid")):
            px = Decimal(lg[px_key])
            rate = Decimal(lg["taker_fee"])
            fee = q8(qty * px * rate)
            self.fees += fee
            legs.append(Leg(f"p{n}{side}", Inst(lg["venue"], lg["market"], lg["symbol"]), side == "L", qty, px, rate, fee))
        tr = Trade(n, f"pair-{n}", pair["id"], pair["base"], t, legs[0], legs[1], features or {})
        self.open[tr.key] = tr
        self.opened += 1
        return tr

    def close_pair(self, key: str, touch: dict, t: int, reason: str) -> Trade:
        tr = self.open.pop(key)
        for leg in tr.legs():
            bid, ask = touch[(leg.inst.venue, leg.inst.market, leg.inst.symbol)]
            px = Decimal(bid if leg.long else ask)
            leg.exit_px = px
            leg.exit_fee = q8(leg.qty * px * leg.fee_rate)
            self.fees += leg.exit_fee
            self.price_pnl += leg.price_pnl(px)
        tr.closed_ms, tr.reason = t, reason
        self.realized += tr.realized()
        self.closed += 1
        return tr

    def settle(self, inst: Inst, rate: Decimal, mark: Decimal | None) -> None:
        for tr in self.open.values():
            for leg in tr.legs():
                if leg.inst != inst:
                    continue
                if mark is None:
                    self.funding_unpriced += 1
                    continue
                amount = q8(leg.qty * mark * rate * (Decimal(-1) if leg.long else Decimal(1)))
                leg.funding += amount
                self.funding += amount
                self.funding_events += 1

    def pair_net_now(self, tr: Trade, touch: dict, quoted: set) -> Decimal | None:
        total = Decimal(0)
        for leg in tr.legs():
            k = (leg.inst.venue, leg.inst.market, leg.inst.symbol)
            if k not in quoted:
                return None
            bid, ask = touch[k]
            total += leg.net_now(Decimal(bid if leg.long else ask))
        return total

    def unrealized(self, touch: dict, last_close) -> Decimal:
        total = Decimal(0)
        for tr in self.open.values():
            for leg in tr.legs():
                k = (leg.inst.venue, leg.inst.market, leg.inst.symbol)
                if k in touch:
                    px = Decimal(touch[k][0] if leg.long else touch[k][1])
                else:
                    px = last_close(leg.inst)
                    px = leg.entry_px if px is None else px
                total += leg.net_now(px)
        return total


# ---------------------------------------------------------------------------------------------------------------------
# the backtest
# ---------------------------------------------------------------------------------------------------------------------

def run_id_of(sid: str, pack_id: str, start_ms: int, end_ms: int, step: int, fees: dict | None, capital: Decimal) -> str:
    h = canon.canonical_sha256({"strategy_id": sid, "pack": pack_id, "start": start_ms, "end": end_ms, "step": step,
                                "fees": fees, "capital": str(capital)})
    return f"irbt-{sid[3:15]}-{h[:12]}"


def run(ir_doc: dict, pack: Pack, instruments: list[dict], *, start_ms: int, end_ms: int, out_dir: str | Path, step_minutes: int = 2,
        fees: dict | None = None, fees_source: str = "default", capital_base: Decimal | None = None, trials: int | None = None,
        trials_evidence: str | Path | None = None, verbose: bool = False, research_service: dict | None = None) -> dict:
    """Run the IR over the dataset pack and write ``run_card.json``, ``trades.jsonl``, ``equity.jsonl``,
    ``decisions.jsonl`` into ``out_dir``. Returns the run card."""
    loaded = IR.load(ir_doc)
    sid = loaded.strategy_id
    if not (isinstance(step_minutes, int) and step_minutes >= 1):
        raise ApiError("request.invalid", "step_minutes must be an integer >= 1", field="step_minutes")
    if not end_ms > start_ms:
        raise ApiError("request.invalid", "end_ms must be after start_ms", field="window")
    if trials is not None and (isinstance(trials, bool) or not isinstance(trials, int) or trials < 1):
        raise ApiError("request.invalid", "trials must be an integer >= 1", field="trials")
    if trials is not None and trials > 1 and trials_evidence is None:
        raise ApiError("request.invalid", "trials > 1 needs --trials-evidence (the file that lists every parameter set tried)", field="trials")
    if trials_evidence is not None and trials is None:
        raise ApiError("request.invalid", "--trials-evidence needs --trials", field="trials")
    insts = universe_instruments(loaded.doc, instruments)
    lo = pack.lock["request"]["window"]["start_ms"]
    if lo > start_ms - WARMUP_MS or pack.lock["request"]["window"]["end_ms"] < end_ms:
        raise ApiError("request.invalid", "the pack's window does not cover the backtest window and its warm-up day", field="window")
    sizing = loaded.doc["sizing"]
    if capital_base is None:
        capital_base, capital_source = Decimal(sizing["notional_per_leg"]) * 2 * sizing["max_open"], "notional_per_leg x 2 legs x max_open"
    else:
        capital_base, capital_source = Decimal(capital_base), "given"
    if capital_base <= 0:
        raise ApiError("request.invalid", "capital_base must be positive", field="capital_base")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    step = step_minutes * MINUTE_MS
    run_id = run_id_of(sid, pack.id, start_ms, end_ms, step_minutes, fees, capital_base)
    work = out / ".work"
    cursor = DatasetCursor(pack, insts, start_ms, end_ms, fees=fees, work=work)
    sim = Sim()
    ir_obj = IR.Ir(loaded.doc)
    cooldowns: dict[str, int] = {}
    closed_pending: list[Trade] = []
    next_trade = 1
    counts = {"instants": 0, "instants_without_data": 0, "entries_decided": 0, "entries_skipped_qty0": 0, "entries_skipped_no_bbo": 0,
              "exits_decided": 0, "exits_held_no_quote": 0}
    daily: dict[str, Decimal] = {}
    # the run starts flat with equity 0: that is the first peak (entry fees of the first trades are already a drawdown)
    peak = Decimal(0)
    dd = {"amount": Decimal(0), "peak_ms": None, "trough_ms": None}
    peak_t = start_ms
    last_settled_t: int | None = None
    t0 = time.monotonic()
    ftr = open(out / "trades.jsonl", "w", encoding="utf-8")
    feq = open(out / "equity.jsonl", "w", encoding="utf-8")
    fde = open(out / "decisions.jsonl", "w", encoding="utf-8")
    try:
        def settle_until(t_incl: int) -> None:
            nonlocal last_settled_t
            for st, inst, s in cursor.settlements_until(t_incl, last_settled_t):
                if st < start_ms:
                    continue
                cursor.advance(st)
                b = cursor.bars[inst].last
                mark = b[2] if b is not None and st - b[1] <= ir_views.DATASET_MAX_BAR_AGE_MS else None
                sim.settle(inst, s.rate, mark if mark is not None else s.mark)
            last_settled_t = t_incl if last_settled_t is None else max(last_settled_t, t_incl)

        def equity_point(t: int, touch: dict) -> Decimal:
            nonlocal peak, peak_t
            unreal = sim.unrealized(touch, cursor.last_close)
            eq = sim.realized + unreal
            _jsonl(feq, {"t": t, "equity": _s(eq), "realized": _s(sim.realized), "unrealized": _s(unreal), "open_pairs": len(sim.open)})
            daily[utc_day(t)] = eq
            if eq > peak:
                peak, peak_t = eq, t
            if peak - eq > dd["amount"]:
                dd.update(amount=peak - eq, peak_ms=peak_t, trough_ms=t)
            return eq

        t = start_ms
        while t < end_ms:
            settle_until(t)
            cursor.advance(t)
            legs = cursor.legs()
            if not legs:
                counts["instants_without_data"] += 1
                t += step
                continue
            counts["instants"] += 1
            for tr in closed_pending:  # ir_policy reconciles a filled close one step later: the base cools down from now
                cooldowns[IR.ascii_upper(tr.base)] = t
            closed_pending = []
            pairs = ir_views.pairs_of_legs(legs)
            touch = ir_views.touch(legs)
            quoted = ir_views.tradable(legs)
            views = []
            for key, tr in sorted(sim.open.items()):
                nn = sim.pair_net_now(tr, touch, quoted)
                views.append({"id": key, "base": tr.base, "long": {"venue": tr.long.inst.venue, "symbol": tr.long.inst.symbol},
                              "short": {"venue": tr.short.inst.venue, "symbol": tr.short.inst.symbol}, "opened_ms": tr.opened_ms,
                              "net_now": None if nn is None else _s(nn)})
            decision = IR.decide(ir_obj, {"pairs": pairs}, {"positions": views, "cooldowns": cooldowns}, t)
            rec = {"t": t, "pairs": len(pairs), "exits": [], "held": [], "entries": [], "rejected": len(decision["rejected"])}
            counts["exits_decided"] += len(decision["exits"])
            for x in decision["exits"]:
                tr = sim.open[x["position_id"]]
                pos = {"long": tr.long.inst.ref(), "short": tr.short.inst.ref()}
                if not ir_views.exit_quoted(pos, quoted):
                    counts["exits_held_no_quote"] += 1
                    rec["held"].append({"position_id": x["position_id"], "reason": x["reason"]})
                    continue
                done = sim.close_pair(x["position_id"], touch, t, x["reason"])
                closed_pending.append(done)
                _jsonl(ftr, done.record())
                rec["exits"].append({"position_id": x["position_id"], "reason": x["reason"], "intents": [f"n{done.trade}-L-close", f"n{done.trade}-S-close"]})
            by_id = {p["id"]: p for p in pairs}
            counts["entries_decided"] += len(decision["entries"])
            for e in decision["entries"]:
                pair = by_id[e["pair_id"]]
                if not (pair["long"]["bbo"] and pair["short"]["bbo"]):
                    counts["entries_skipped_no_bbo"] += 1
                    continue
                qty = ir_views.entry_qty(e["notional_per_leg"], pair["long"]["ask"])
                if qty <= 0:
                    counts["entries_skipped_qty0"] += 1
                    continue
                n, next_trade = next_trade, next_trade + 1
                sim.open_pair(n, pair, qty, t, e["features"])
                rec["entries"].append({"pair_id": e["pair_id"], "position_id": f"pair-{n}", "qty": _s(qty),
                                       "intents": [f"n{n}-L-open", f"n{n}-S-open"]})
            if verbose:
                rec["rejected_detail"] = decision["rejected"]
            _jsonl(fde, rec)
            equity_point(t, touch)
            t += step
        # funding of settlements after the last instant (positions do not change after it), then the final valuation
        settle_until(end_ms - 1)
        cursor.advance(end_ms - 1)
        final_touch = ir_views.touch(cursor.legs())
        final_eq = equity_point(end_ms - 1, final_touch)
        for tr in sorted(sim.open.values(), key=lambda x: x.trade):
            _jsonl(ftr, tr.record())
    finally:
        ftr.close()
        feq.close()
        fde.close()
        cursor.close()
        shutil.rmtree(work, ignore_errors=True)

    # statistics: UTC-daily returns over the capital base (AlphaKeel convention)
    import pandas as pd

    from backtest.alphakeel_metrics import alphakeel_metrics, daily_returns

    # every UTC day of the window is a point: a day without any data instant carries the previous day's equity (no
    # valuation that day), instead of silently dropping out and folding its P&L into the next day's return
    prev_eq = Decimal(0)
    day = start_ms - start_ms % DAY_MS
    while day < end_ms:
        k = utc_day(day)
        prev_eq = daily.setdefault(k, prev_eq)
        day += DAY_MS
    days = sorted(daily)
    series = pd.Series([float(daily[d]) for d in days], index=pd.to_datetime(days))
    rets = daily_returns(series, float(capital_base), 0.0)
    ak = alphakeel_metrics(rets, trials=trials or 1)
    trials_doc = None
    if trials is not None:
        if trials_evidence is None:  # one trial: this run is the only parameter set tried; the evidence is generated
            ev = out / "trials.jsonl"
            ev.write_text(json.dumps({"trial": 1, "strategy_id": sid, "run_id": run_id, "generated": True,
                                      "note": "single trial: this run is the only parameter set tried"}, sort_keys=True) + "\n", encoding="utf-8")
        else:
            src = Path(trials_evidence)
            if not src.is_file():
                raise ApiError("request.invalid", f"trials evidence {str(trials_evidence)!r} is not a file", field="trials_evidence")
            ev = out / f"trials-evidence{src.suffix}"
            if src.resolve() != ev.resolve():
                shutil.copyfile(src, ev)
        trials_doc = {"count": trials, "evidence": ev.name, "sha256": hashlib.sha256(ev.read_bytes()).hexdigest()}
    psum = pack_summary(pack)
    card = {
        "schema": RUN_CARD_SCHEMA, "run_id": run_id, "strategy_id": sid, "strategy_sha256": canon.canonical_sha256(loaded.definition),
        "name": loaded.doc["name"], "created_ms": int(time.time() * 1000), "elapsed_s": round(time.monotonic() - t0, 3),
        "purpose": "research evidence (long-history dataset backtest); not reconciliation input",
        "pack": psum, "window": {"start_ms": start_ms, "end_ms": end_ms, "start_day": utc_day(start_ms), "end_day": utc_day(end_ms - 1)},
        "data_window": {"start_ms": lo, "end_ms": end_ms, "start_day": utc_day(lo), "end_day": utc_day(end_ms - 1), "warmup_ms": start_ms - lo},
        "step_minutes": step_minutes, "view": ir_views.DATASET_VIEW, "approximations": APPROXIMATIONS, "simulator": SIM_CONVENTIONS,
        "assumptions": {"funding_estimate_of_ir": loaded.doc["assumptions"]["funding_estimate"],
                        "fees": fees if fees is not None else ir_views.default_fees(), "fees_source": fees_source,
                        "fees_note": None if fees is not None else "AlphaKeel's public lowest-tier perp taker fee 0.0005 on every venue (assumption)",
                        "capital_base": _s(capital_base), "capital_base_source": capital_source},
        "instruments": [{**d, "base": cursor.identity[Inst(d["venue"], d["market"], d["symbol"])][0],
                         "quote": cursor.identity[Inst(d["venue"], d["market"], d["symbol"])][1]} for d in insts],
        "data_audit": {"auditable": not psum["accept_partial"], "coverage_partial": psum["accept_partial"],
                       "coverage_note": ("the pack was frozen with accept_partial: some (instrument, table) coverage is not complete; "
                                         "the window is the requested one, the data inside it has gaps (pack coverage report)")
                       if psum["accept_partial"] else None,
                       "sources": {"alphakeel_b2": {"pack_id": psum["pack_id"], "pack_sha256": psum["pack_sha256"],
                                                                       "datasets": psum["datasets"], "verified": True}},
                       "non_auditable_sources": []},
        "inputs": cursor.inputs(pack),
        "counts": {**counts, "opened": sim.opened, "closed": sim.closed, "open_at_end": len(sim.open), "funding_events": sim.funding_events,
                   "funding_unpriced": sim.funding_unpriced},
        "amounts": {"realized": _s(sim.realized), "price_pnl_closed": _s(sim.price_pnl), "funding": _s(sim.funding), "fees": _s(sim.fees),
                    "equity_end": _s(final_eq), "unrealized_end": _s(final_eq - sim.realized), "ccy": "USDT"},
        "max_drawdown": {"amount": _s(dd["amount"]), "peak_ms": dd["peak_ms"], "trough_ms": dd["trough_ms"],
                         "fraction_of_capital": _s(q8(dd["amount"] / capital_base))},
        "files": {"equity": "equity.jsonl", "trades": "trades.jsonl", "decisions": "decisions.jsonl"},
        "trials": trials_doc,
        "alphakeel_metrics": ak,
        "metrics": {"total_return_on_capital": _s(q8(final_eq / capital_base)), "days": len(days), "trades_closed": sim.closed,
                    "sharpe_daily_annualised": ak.get("sharpe_annualised") if isinstance(ak, dict) else None},
        "research_service": research_service or {"used": False},
    }
    (out / "run_card.json").write_text(json.dumps(card, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return card


def backtest(client, ir_doc: dict, instruments: list[dict], *, start_ms: int, end_ms: int, out_dir: str | Path,
             cache_dir: str | Path | None = None, pack_dir: str | Path | None = None, accept_partial: bool = False, **kw: Any) -> dict:
    """Freeze (or reuse, the request key is content-derived) the dataset pack through the research service -- or open an
    exported one (``pack_dir``) -- check it is the pack asked for, and :func:`run`."""
    insts = universe_instruments(ir_doc, instruments)
    req = dataset_request(insts, start_ms, end_ms, accept_partial=accept_partial)
    service = None
    if pack_dir is not None:
        pack = Pack.from_directory(pack_dir)
        service = {"used": False, "pack_dir": str(pack_dir)}
    else:
        from . import workflow

        who = client.check()
        pack = workflow.freeze_pack(client, req, cache_dir=cache_dir)
        service = {"used": True, "credential": who.get("credential"), "data_horizon": who.get("data_horizon"),
                   "holdout_days": who.get("holdout_days")}
    check_pack(pack, req)
    return run(ir_doc, pack, insts, start_ms=start_ms, end_ms=end_ms, out_dir=out_dir, research_service=service, **kw)


# ---------------------------------------------------------------------------------------------------------------------
# export: the IR document AlphaKeel's registry imports
# ---------------------------------------------------------------------------------------------------------------------

def export(ir_doc: dict, run_dir: str | Path, out_dir: str | Path, *, research_credential: dict | None = None) -> dict:
    """Write ``strategy.json`` (the IR with ``strategy_id`` and ``provenance``), the trials evidence and
    ``evidence/ir-backtest-audit.json`` into ``out_dir``. The definition is never changed (same ``strategy_id``)."""
    from . import handoff

    loaded = IR.load(ir_doc)
    sid = loaded.strategy_id
    rd = Path(run_dir)
    try:
        card = json.loads((rd / "run_card.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ApiError("request.invalid", f"{rd / 'run_card.json'} does not exist: run `ir backtest` first", field="run") from None
    if card.get("schema") != RUN_CARD_SCHEMA:
        raise ApiError("request.invalid", f"the run card is not a {RUN_CARD_SCHEMA} document", field="run")
    if card.get("strategy_id") != sid:
        raise ApiError("request.invalid", f"the run card is for {card.get('strategy_id')}, the IR is {sid}", field="run")
    tri = card.get("trials")
    if not isinstance(tri, dict) or not tri.get("evidence"):
        raise ApiError("request.invalid", "the run has no trials evidence: re-run `ir backtest` with --trials N (and "
                       "--trials-evidence FILE when N > 1)", field="trials")
    src = rd / tri["evidence"]
    if not src.is_file() or hashlib.sha256(src.read_bytes()).hexdigest() != tri.get("sha256"):
        raise ApiError("request.invalid", f"the trials evidence {src} is missing or differs from the run card", field="trials")
    out = Path(out_dir)
    (out / "evidence").mkdir(parents=True, exist_ok=True)
    ev_rel = f"evidence/{src.name}"
    shutil.copyfile(src, out / ev_rel)
    window = {"start": card["window"]["start_day"], "end": card["window"]["end_day"]}
    handoff.check_window(window)
    handoff.check_trials(out, tri["count"], ev_rel)
    horizon = handoff.check_research_credential(research_credential, window["end"])
    audit = handoff.audit_of(card, research_credential)
    prov: dict[str, Any] = {
        "tool": "vibe-trading", "run_id": card["run_id"], "window": window,
        "data_window": {"start": card["data_window"]["start_day"], "end": card["data_window"]["end_day"]},
        "trials": {"count": tri["count"], "evidence": ev_rel},
        "classification": audit["classification"],
        "data_audit": {k: audit[k] for k in ("auditable", "classification", "sources", "non_auditable_sources")},
        "data_view": card["view"], "approximations": card["approximations"],
        "backtest": {"pack_id": card["pack"]["pack_id"], "pack_sha256": card["pack"]["pack_sha256"],
                     "dataset_versions": card["pack"]["dataset_versions"], "start_ms": card["window"]["start_ms"],
                     "end_ms": card["window"]["end_ms"], "step_minutes": card["step_minutes"], "audit": "evidence/ir-backtest-audit.json",
                     "purpose": "research evidence, not reconciliation input"},
    }
    if research_credential is not None:
        prov["research_credential"] = research_credential.get("credential")
    doc = copy.deepcopy(loaded.doc)
    doc["strategy_id"] = sid
    doc["provenance"] = prov
    again = IR.load(json.loads(json.dumps(doc)))
    if again.strategy_id != sid:  # provenance never enters the id; a change here is a bug, not a new strategy
        raise ApiError("engine.failure", "the exported document's strategy_id differs from the input IR's")
    audit_doc = {"schema": "vibe-trading.ir-backtest-audit/1", "strategy_id": sid, "classification": audit["classification"],
                 "reasons": audit["reasons"], "window": window, "trials": prov["trials"], "data_audit": card["data_audit"],
                 "alphakeel_metrics": card.get("alphakeel_metrics"), "vibe_trading_metrics": card.get("metrics"),
                 "research_credential": research_credential and {"credential": research_credential.get("credential"),
                                                                 "holdout_days": research_credential.get("holdout_days"),
                                                                 "data_horizon": horizon},
                 "run_card": card}
    (out / "evidence" / "ir-backtest-audit.json").write_text(json.dumps(audit_doc, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    (out / "strategy.json").write_text(json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"strategy_id": sid, "path": str(out / "strategy.json"), "classification": audit["classification"], "reasons": audit["reasons"],
            "provenance": prov, "audit": str(out / "evidence" / "ir-backtest-audit.json")}
