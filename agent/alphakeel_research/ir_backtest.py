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
received by shorts for a positive rate (``sim.py``'s sign), ``mark`` = the settlement's own published mark price (AlphaKeel
F04; rounded half-even to the price decimals of the contract seen so far while it was held or traded,
``DatasetCursor.price_dp``, as AlphaKeel's ``MarkRounder`` does), else the close of the last 1m bar closed at or before ``t`` (``ir_views.settlement_price``); ``q8`` = half-even to 1e-8. No margin, leverage, liquidation,
partial fills, latency or funding of spot legs. Equity = realized + every open pair valued close-now (§5a ``net_now``
formula at the touch; a leg without a quote this instant at its last close). Amounts are in the legs' quote currency:
only ``USDT`` is accepted (no FX model).

Memory is bounded: kline rows are spilled per instrument to the work directory in one pass over the pack's objects and
streamed back in time order; frames, decisions and equity points are written as they are produced.
"""

from __future__ import annotations

import bisect
import copy
import datetime as dt
import hashlib
import inspect
import json
import os
import re
import shutil
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

from . import canon, ir_views
from . import ir as IR
from .errors import ApiError, EvidenceError, Unsupported
from . import packfile
from .packfile import Chain, Inst, Pack, Pit
from .sim import ENGINE_MAX_DP, dp_of

RUN_CARD_SCHEMA = "vibe-trading.ir-backtest-run-card/1"
DAY_MS = 86_400_000
MINUTE_MS = 60_000
#: data before the first decision instant: the 1440 bars of the 24 h volume and the last settlement (intervals <= 8 h)
WARMUP_MS = DAY_MS
UNIT = Decimal("0.00000001")

MAKER_NOT_MODELLED = ("maker_not_modelled: the local simulator fills the maker entry leg as taker (taker fee at the touch); "
                      "maker fills are modelled only by AlphaKeel's conservative profile")

APPROXIMATIONS = [
    "touch: bid = ask = close of the last 1m trade bar closed at the decision time (close_time_ms <= now); spread 0",
    "bbo = true is synthetic (no order book in the dataset); price_estimated is false by construction",
    "mark / funding_px in the decision frame = that close (no mark or index series)",
    "funding_rate = the last official settlement visible at the decision time (available_at, or settlement time + the pack's "
    "assumed delay): a last_settled proxy for the predicted rate live scans use; the IR's assumptions.funding_estimate is unchanged",
    "interval_hours from that settlement's interval_seconds; when the dataset leaves it null (Binance/OKX/Bybit/Bitget/Gate/"
    "Aster history) it is INFERRED from the gap between the two latest visible settlements, accepted only at 1, 2, 4 "
    "or 8 h within 60 s (timestamps jitter by milliseconds) (perps with neither are left out; after an interval change the inference lags one settlement); "
    "next_funding_ms = settlement time + interval",
    "volume_quote = sum of volume_quote over the 1440 bars of the 24 h ending with the last closed bar (null unless all 1440 exist)",
    "a funding settlement on an open leg is booked at the settlement row's published mark price (AlphaKeel F04), rounded "
    "half-even to the contract's price decimals seen so far (the most decimals of its quote in a frame where it was held or "
    "closed, or of a fill price, before it; at most 16; AlphaKeel MarkRounder; unrounded before any such frame); without a mark at the close of the last 1m bar "
    "in the 5 minutes before it; with neither it is NOT booked (counts.funding_unpriced); "
    "a leg that stops trading is valued at its last close (stale) and cannot exit",
    f"a leg whose last closed bar is older than {ir_views.DATASET_MAX_BAR_AGE_MS // MINUTE_MS} minutes is not in the frame",
    "base / quote_ccy from the pack's instruments table, or from the declared instrument when the dataset pack leaves them null",
]


def approximations_of(ir_doc: dict) -> list[str]:
    """The run card's approximations: :data:`APPROXIMATIONS`, plus :data:`MAKER_NOT_MODELLED` when the IR has a maker leg."""
    return [*APPROXIMATIONS, MAKER_NOT_MODELLED] if IR.maker_leg(ir_doc) is not None else list(APPROXIMATIONS)


SIM_CONVENTIONS = {
    "fills": "complete and immediate at the frame's touch (long open buys the ask, short open sells the bid, closes the other side)",
    "fee": "q8(qty * price * taker_fee) per fill, taker_fee from the view's fee table",
    "funding": "at each official settlement time t on open positions of that contract: q8(qty * mark * rate), paid by longs and "
               "received by shorts for a positive rate; mark = the settlement's published mark price (AlphaKeel F04) rounded "
               "half-even to the contract's price decimals seen so far while held or traded (AlphaKeel MarkRounder), else the close "
               "of the last 1m bar closed at or before t (within 5 minutes)",
    "rounding": "q8 = half-even to 1e-8; quantity half-even to 8 places (README §5a); the trade's executed quantity is that "
                "quantity truncated toward zero to the decimals AlphaKeel's engine can hold exactly (README §5a engine precision: a "
                "contract's largest position x 10^dp <= 2^43); decisions (net_now) use the IR quantity, fills, fees, funding and "
                "every equity point the executed quantity",
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


def dataset_request(instruments: list[dict], start_ms: int, end_ms: int, *, accept_partial: bool = False,
                    selection: dict | str | None = None) -> dict:
    """Dataset-pack request: 1m trade klines + the latest funding dataset for the instruments, ``WARMUP_MS`` before start.
    ``selection``: how the instrument list was chosen (``Client.dataset_pack_request``; omitted = ``"declared"``)."""
    from .client import Client

    refs = [{"venue": d["venue"], "market": d["market"], "symbol": d["symbol"]} for d in instruments]
    has_perp = any(d["market"] == "perp" for d in instruments)
    return Client.dataset_pack_request(start_ms=start_ms - WARMUP_MS, end_ms=end_ms, instruments=refs,
                                       funding_dataset=None if has_perp else "none", market_tables=["kline_1m"],
                                       price_kinds=["trade"], accept_partial=accept_partial, selection=selection)


def _selection_matches(locked, wanted) -> bool:
    """The pack's ``request.selection`` is the one asked for: ``"declared"`` (or omitted) on both sides, or the same
    ``pit_membership.top_n`` (and ``table_sha256`` when the caller pinned one)."""
    def norm(x):
        return "declared" if x in (None, "declared") else x
    locked, wanted = norm(locked), norm(wanted)
    if wanted == "declared" or locked == "declared":
        return locked == wanted
    lp, wp = (locked or {}).get("pit_membership") or {}, (wanted or {}).get("pit_membership") or {}
    return lp.get("top_n") == wp.get("top_n") and (wp.get("table_sha256") is None or lp.get("table_sha256") == wp.get("table_sha256"))


def check_pack(pack: Pack, request: dict) -> None:
    """The pack must be the dataset pack asked for (window, instruments, tables, versions); a swapped pack is refused."""
    lock = pack.lock
    req = lock.get("request") or {}
    if not pack.is_dataset:
        raise EvidenceError("evidence.reference", "the pack is a scan pack; the long-history backtest needs a dataset pack")
    want = request["window"]
    if req.get("window") != want or req.get("instruments") != request["instruments"]:
        raise EvidenceError("evidence.reference", "the pack does not match the request (window or instruments)")
    if not _selection_matches(req.get("selection"), request.get("selection")):
        raise EvidenceError("evidence.reference", f"the pack's instrument selection {req.get('selection', 'declared')!r} is not the "
                            f"one asked for ({request.get('selection', 'declared')!r})")
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

class _FrozenChain:
    """A cached chain: the ``Chain`` surface the run reads (``rows``, ``first_ms``, ``last_ms``, ``hexdigest()``)."""

    def __init__(self, rows: int, first_ms: int | None, last_ms: int | None, sha256: str):
        self.rows, self.first_ms, self.last_ms, self._sha = rows, first_ms, last_ms, sha256

    def hexdigest(self) -> str:
        return self._sha


_SPILL_KEEP = 8
_STALE_TMP_S = 3600


def _file_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def _spill_key(pack: Pack, insts: list[Inst], start_ms: int, end_ms: int) -> str:
    return canon.canonical_sha256({"format": SPILL_FORMAT, "pack_sha256": pack.lock["pack_sha256"], "insts": sorted(i.leg() for i in insts),
                                   "start_ms": start_ms, "end_ms": end_ms})


def _spill_cache_load(sdir: Path, insts: list[Inst], paths: dict[Inst, Path]):
    """Copy a verified cache entry into ``paths``; None on any mismatch (the entry is then removed)."""
    try:
        man = json.loads((sdir / "manifest.json").read_text(encoding="utf-8"))
        if man.get("format") != SPILL_FORMAT:
            raise ValueError("format")
        chains: dict[Inst, _FrozenChain] = {}
        for i in insts:
            f, c = man["files"][i.leg()], man["chains"][i.leg()]
            src = sdir / f["name"]
            if src.stat().st_size != f["bytes"] or _file_sha256(src) != f["sha256"]:
                raise ValueError("digest")
            chains[i] = _FrozenChain(c["rows"], c["first_ms"], c["last_ms"], c["sha256"])
        for i in insts:
            shutil.copyfile(sdir / man["files"][i.leg()]["name"], paths[i])
        os.utime(sdir)
        return chains
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        shutil.rmtree(sdir, ignore_errors=True)
        return None


def _spill_cache_store(root: Path, key: str, insts: list[Inst], paths: dict[Inst, Path], chains: dict[Inst, Chain]) -> None:
    tmp = root / f".tmp-{key}-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        files, ch = {}, {}
        for i in insts:
            name = paths[i].name
            shutil.copyfile(paths[i], tmp / name)
            files[i.leg()] = {"name": name, "sha256": _file_sha256(tmp / name), "bytes": (tmp / name).stat().st_size}
            c = chains[i]
            ch[i.leg()] = {"rows": c.rows, "first_ms": c.first_ms, "last_ms": c.last_ms, "sha256": c.hexdigest()}
        (tmp / "manifest.json").write_text(json.dumps({"format": SPILL_FORMAT, "files": files, "chains": ch}, sort_keys=True), encoding="utf-8")
        target = root / key
        if target.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            os.replace(tmp, target)
        entries = sorted((d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".tmp-")), key=lambda d: d.stat().st_mtime)
        for d in entries[:-_SPILL_KEEP] if len(entries) > _SPILL_KEEP else []:
            shutil.rmtree(d, ignore_errors=True)
        now = time.time()
        for d in root.iterdir():
            if d.is_dir() and d.name.startswith(".tmp-") and d != tmp and now - d.stat().st_mtime > _STALE_TMP_S:
                shutil.rmtree(d, ignore_errors=True)
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)


def _spill_format() -> str | None:
    """Hash of the code that defines the spilled files and chains; None (no cache) when the source is unavailable."""
    try:
        src = "\n".join(inspect.getsource(o) for o in (_spill_klines_uncached, packfile.Chain, packfile.row_digest_floatfree,
                                                       packfile.Pack._read_rows_floatfree))
    except (OSError, TypeError):
        return None
    return hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]


SPILL_FORMAT: str | None = None  # set below, once ``_spill_klines_uncached`` exists


def spill_klines(pack: Pack, insts: list[Inst], start_ms: int, end_ms: int, work: Path) -> tuple[dict[Inst, Path], dict[Inst, Chain]]:
    """``_spill_klines_uncached`` with a per-pack cache under ``<pack cache root>/spill/<key>/`` (derived, reproducible files;
    at most ``_SPILL_KEEP`` entries). The same pack, instruments and window re-use the verified files instead of re-reading
    the pack. A cache problem never fails the run."""
    root = None
    if SPILL_FORMAT is not None and pack.cache is not None and pack.client is not None:  # an exported directory (no client) is read-only: nothing is written
        try:
            root = pack.cache.root / "spill"
            key = _spill_key(pack, insts, start_ms, end_ms)
        except (OSError, KeyError, TypeError):
            root = None
    if root is None:
        return _spill_klines_uncached(pack, insts, start_ms, end_ms, work)
    work.mkdir(parents=True, exist_ok=True)
    paths = {i: work / f"{i.venue}-{i.market}-{i.symbol}.klines".replace("/", "_") for i in insts}
    sdir = root / key
    if sdir.is_dir():
        frozen = _spill_cache_load(sdir, insts, paths)
        if frozen is not None:
            return paths, frozen  # type: ignore[return-value]
    paths, chains = _spill_klines_uncached(pack, insts, start_ms, end_ms, work)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return paths, chains
    _spill_cache_store(root, key, insts, paths, chains)
    return paths, chains


def _spill_klines_uncached(pack: Pack, insts: list[Inst], start_ms: int, end_ms: int, work: Path) -> tuple[dict[Inst, Path], dict[Inst, Chain]]:
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
        for venue, market in sorted({(i.venue, i.market) for i in insts}):
            # one object family per (venue, market), the same selection as AlphaKeel's ``Pack::market_rows``, so a
            # symbol listed on both markets cannot mix spot and perp bars (and the chain digests stay comparable)
            if any(o["role"] == "market" and o["name"].startswith(f"market/kline_1m/{venue}/part-") for o in pack.lock["objects"]):
                raise EvidenceError("evidence.reference", f"this pack stores kline_1m rows of {venue} spot and perp contracts in one "
                                    "object (layout before market-separated objects); rebuild the pack")
            by_symbol: dict[str, list[Inst]] = {}
            for i in insts:
                if (i.venue, i.market) == (venue, market):
                    by_symbol.setdefault(i.symbol, []).append(i)
            tag = f"market/kline_1m/{venue}/{market}/"
            for o in sorted((o for o in pack.lock["objects"] if o["role"] == "market" and o["name"].startswith(tag)), key=lambda o: o["name"]):
                if (o["last_ms"] or 0) < start_ms or (o["first_ms"] or 0) >= end_ms:
                    continue
                for r in pack._read_rows_floatfree(o["name"]):
                    if r[1] not in by_symbol or not (start_ms <= r[0] < end_ms):
                        continue
                    for i in by_symbol[r[1]]:
                        chains[i].push_floatfree(r[0], r)
                    if r[10] != "trade":
                        continue
                    if r[2] != r[0] + ir_views.KLINE_SPAN_MS:
                        raise EvidenceError("evidence.reference", f"kline at {r[0]} of {venue}:{r[1]} has close_time_ms {r[2]}, not t + 59999")
                    for i in by_symbol[r[1]]:
                        if i in last and r[0] <= last[i]:
                            raise EvidenceError("evidence.reference", f"klines of {i.leg()} are not in strict time order at {r[0]}")
                        last[i] = r[0]
                        files[i].write(f"{r[0]} {r[2]} {r[6]} {r[8]}\n")
    finally:
        for f in files.values():
            f.close()
    return paths, chains


SPILL_FORMAT = _spill_format()


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
        for rows in self.settles.values():
            rows.sort(key=lambda s: s.t)  # stable: bisect windows in settlements_until
        self._settle_ts = {i: [s.t for s in rows] for i, rows in self.settles.items()}
        self._vis = {i: sorted(v, key=lambda s: (s.visible_ms, s.t)) for i, v in self.settles.items()}
        self._vis_ptr = {i: 0 for i in self._vis}
        self._vis_last: dict[Inst, _Settle | None] = {i: None for i in self._vis}
        self._vis_prev: dict[Inst, _Settle | None] = {i: None for i in self._vis}
        #: per contract, the most price decimals observed so far (capped at ``sim.ENGINE_MAX_DP``; :meth:`observe`): the
        #: quotes of held legs and legs closed in a frame, and fill prices -- AlphaKeel's ``MarkRounder.observe`` over the
        #: engine frame's snaps and intents (bid = ask = mark = close here); an official mark is rounded to it, and left as
        #: published while the contract has not been observed yet
        self.price_dp: dict[Inst, int] = {}
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

    def observe(self, inst: Inst, prices) -> None:
        """Raise ``price_dp[inst]`` to the most decimals of ``prices`` (capped at ``ENGINE_MAX_DP``)."""
        self.price_dp[inst] = max(self.price_dp.get(inst, 0), min(max(dp_of(Decimal(p)) for p in prices), ENGINE_MAX_DP))

    def settlements_until(self, end_incl: int, after: int | None) -> list[tuple[int, Inst, _Settle]]:
        """Settlements with ``after < t <= end_incl`` (time order, then instrument order)."""
        out = []
        for i, rows in self.settles.items():
            ts = self._settle_ts[i]
            lo = 0 if after is None else bisect.bisect_right(ts, after)
            out.extend((s.t, i, s) for s in rows[lo:bisect.bisect_right(ts, end_incl)])
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
    #: every funding booking ``(mark, signed rate)`` so the amounts can be redone at the engine's quantity
    settles: list = field(default_factory=list)
    #: the IR's quantity (README §5a, 8 places) when the engine executes a different one (precision cap), else None
    ir_qty: Decimal | None = None

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
                    **({"ir_qty": _s(x.ir_qty)} if x.ir_qty is not None else {}),
                    "entry_price": _s(x.entry_px), "exit_price": None if x.exit_px is None else _s(x.exit_px), "entry_fee": _s(x.entry_fee),
                    "exit_fee": _s(x.exit_fee), "funding": _s(x.funding),
                    "price_pnl": None if x.exit_px is None else _s(x.price_pnl(x.exit_px))}
        closed = self.closed_ms is not None
        return {"trade": self.trade, "pair_key": self.key, "pair_id": self.pair_id, "base": self.base, "opened_ms": self.opened_ms,
                "closed_ms": self.closed_ms, "status": "closed" if closed else "open", "exit_reason": self.reason,
                "long": leg(self.long), "short": leg(self.short), "realized": _s(self.realized()) if closed else None,
                # the entry decision's features (``Features.to_json``: decimals as strings): why this pair was entered
                "entry_features": dict(self.entry_features)}


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
        self.closed_trades: list[Trade] = []
        self.qty_adjusted = 0

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
        self.closed_trades.append(tr)
        return tr

    def settle(self, inst: Inst, rate: Decimal, mark: Decimal | None) -> None:
        for tr in self.open.values():
            for leg in tr.legs():
                if leg.inst != inst:
                    continue
                if mark is None:
                    self.funding_unpriced += 1
                    continue
                signed = rate * (Decimal(-1) if leg.long else Decimal(1))
                amount = q8(leg.qty * mark * signed)
                leg.settles.append((mark, signed))
                leg.funding += amount
                self.funding += amount
                self.funding_events += 1

    def apply_engine_qty(self) -> None:
        """README §5a (engine quantity precision): AlphaKeel's engine executes each trade with its quantity truncated toward
        zero to the decimals its contracts can hold exactly (``sim.engine_trade_dps`` over the WHOLE run: a contract's cap
        depends on its largest position). Decisions used the IR quantity (as AlphaKeel's decision ledger does); the trades'
        fills, fees, funding and the totals are redone at the executed quantity. Usually a no-op (the cap binds only for
        hundreds of thousands of a coin at 8 places)."""
        from .sim import engine_trade_dps, order_quantity

        trades = self.closed_trades + sorted(self.open.values(), key=lambda x: x.trade)
        dps = engine_trade_dps([(tr.trade, [(leg.inst, leg.qty) for leg in tr.legs()]) for tr in trades])
        for tr in trades:
            for leg in tr.legs():
                q = order_quantity(leg.qty, dps[tr.trade])
                if q == leg.qty:
                    continue
                self.qty_adjusted += 1
                leg.ir_qty, leg.qty = leg.qty, q
                leg.entry_fee = q8(q * leg.entry_px * leg.fee_rate)
                if leg.exit_px is not None:
                    leg.exit_fee = q8(q * leg.exit_px * leg.fee_rate)
                leg.funding = sum((q8(q * mk * r) for mk, r in leg.settles), Decimal(0))
        closed = [tr for tr in trades if tr.closed_ms is not None]
        self.fees = sum((leg.entry_fee + leg.exit_fee for tr in trades for leg in tr.legs()), Decimal(0))
        self.funding = sum((leg.funding for tr in trades for leg in tr.legs()), Decimal(0))
        self.realized = sum((tr.realized() for tr in closed), Decimal(0))
        self.price_pnl = sum((leg.price_pnl(leg.exit_px) for tr in closed for leg in tr.legs()), Decimal(0))

    def pair_net_now(self, tr: Trade, touch: dict, quoted: set) -> Decimal | None:
        total = Decimal(0)
        for leg in tr.legs():
            k = (leg.inst.venue, leg.inst.market, leg.inst.symbol)
            if k not in quoted:
                return None
            bid, ask = touch[k]
            total += leg.net_now(Decimal(bid if leg.long else ask))
        return total

    def valuation_marks(self, touch: dict, last_close) -> list:
        """The open legs' close-now prices at this instant: ``[trade, "L"|"S", price, settlements booked so far]`` (the
        touch; a leg without a quote at its last close, else its entry price). The equity point is computed from these
        once the run's executed quantities are known (:meth:`equity_at`)."""
        out = []
        for tr in sorted(self.open.values(), key=lambda x: x.trade):
            for side, leg in (("L", tr.long), ("S", tr.short)):
                k = (leg.inst.venue, leg.inst.market, leg.inst.symbol)
                if k in touch:
                    px = Decimal(touch[k][0] if leg.long else touch[k][1])
                else:
                    px = last_close(leg.inst)
                    px = leg.entry_px if px is None else px
                out.append([tr.trade, side, _s(px), len(leg.settles)])
        return out

    @staticmethod
    def leg_value_at(leg: Leg, px: Decimal, settled: int) -> Decimal:
        """README §5a ``net_now`` of one leg at the leg's (executed) quantity with the first ``settled`` funding bookings."""
        funding = sum((q8(leg.qty * mk * r) for mk, r in leg.settles[:settled]), Decimal(0))
        return leg.price_pnl(px) + funding - leg.entry_fee - leg.qty * px * leg.fee_rate


# ---------------------------------------------------------------------------------------------------------------------
# the backtest
# ---------------------------------------------------------------------------------------------------------------------

class _Diagnostics:
    """Why a run opened (few or) no trades: the first failing check per pair and instant (``decide``'s ``rejected``), and the
    entry features' value distribution per base, sampled every ``stride`` instants. Diagnostic only; never feeds the run."""

    def __init__(self, doc: dict, total_instants: int):
        self.doc = doc
        self.assumptions = doc["assumptions"]
        e = doc["entry"]
        self.conds = {}
        for c in e["conditions"]:
            self.conds.setdefault(c["feature"], c)
        self.features = list(dict.fromkeys([c["feature"] for c in e["conditions"]] + [e["rank_by"]]))
        self.stride = max(1, -(-max(0, total_instants) // 2000))
        self.rejections: dict[str, int] = {}
        self.by_base: dict[str, dict[str, int]] = {}
        # feature -> base -> [values, unavailable, invalid]
        self.samples: dict[str, dict[str, list]] = {f: {} for f in self.features}

    def observe(self, pairs: list[dict], rejected: list[dict], t: int, *, sample: bool) -> None:
        base_of = {p["id"]: IR.ascii_upper(p["base"]) for p in pairs}
        for r in rejected:
            why = r["reason"]
            self.rejections[why] = self.rejections.get(why, 0) + 1
            b = self.by_base.setdefault(base_of.get(r["pair_id"], "?"), {})
            b[why] = b.get(why, 0) + 1
        if not sample:
            return
        for p in pairs:
            base = base_of[p["id"]]
            try:
                f = IR.compute_features(p, self.assumptions, t)
            except IR.PairInvalid:
                f = None
            for name in self.features:
                slot = self.samples[name].setdefault(base, [[], 0, 0])
                if f is None:
                    slot[2] += 1
                    continue
                v = f.value(name)
                if v is None:
                    slot[1] += 1
                else:
                    slot[0].append(v)

    def card(self) -> dict:
        def q(vals: list[Decimal], frac: float) -> Decimal:
            return vals[min(len(vals) - 1, max(0, -(-int(frac * 100) * len(vals) // 100) - 1))]

        feats = {}
        for name in self.features:
            cond = self.conds.get(name)
            by_base = {}
            for base in sorted(self.samples[name]):
                vals, unavailable, invalid = self.samples[name][base]
                vals = sorted(vals)
                row = {"samples": len(vals), "unavailable": unavailable, "invalid": invalid,
                       "p05": None, "p50": None, "p95": None, "max": None, "share_holding": None}
                if vals:
                    row.update(p05=_s(q(vals, 0.05)), p50=_s(q(vals, 0.5)), p95=_s(q(vals, 0.95)), max=_s(vals[-1]))
                    if cond is not None:
                        share = Decimal(sum(1 for v in vals if IR._holds(cond, v))) / len(vals)
                        row["share_holding"] = str(share.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN))
                by_base[base] = row
            feats[name] = {"threshold": None if cond is None else {"op": cond["op"], "value": cond["value"]}, "by_base": by_base}
        return {"note": f"diagnostic only: the first failing check per pair and instant; feature values sampled every {self.stride} instants",
                "sample_stride": self.stride,
                "rejections": dict(sorted(self.rejections.items(), key=lambda kv: (-kv[1], kv[0]))),
                "rejections_by_base": {b: dict(sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))) for b, d in sorted(self.by_base.items())},
                "entry_features": feats}


def run_id_of(sid: str, pack_id: str, start_ms: int, end_ms: int, step: int, fees: dict | None, capital: Decimal) -> str:
    h = canon.canonical_sha256({"strategy_id": sid, "pack": pack_id, "start": start_ms, "end": end_ms, "step": step,
                                "fees": fees, "capital": str(capital)})
    return f"irbt-{sid[3:15]}-{h[:12]}"


def run(ir_doc: dict, pack: Pack, instruments: list[dict], *, start_ms: int, end_ms: int, out_dir: str | Path, step_minutes: int = 2,
        fees: dict | None = None, fees_source: str = "request", capital_base: Decimal | None = None, trials: int | None = None,
        trials_evidence: str | Path | None = None, verbose: bool = False, research_service: dict | None = None,
        diagnostics: bool = False) -> dict:
    """Run the IR over the dataset pack and write ``run_card.json``, ``trades.jsonl``, ``equity.jsonl``,
    ``decisions.jsonl`` into ``out_dir``. Returns the run card."""
    loaded = IR.load(ir_doc)
    sid = loaded.strategy_id
    fees = ir_views.require_fees(fees)
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
    t0 = time.monotonic()  # the spill (DatasetCursor) is part of the run's elapsed time
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
    diag = _Diagnostics(ir_obj.doc, (end_ms - start_ms) // step) if diagnostics else None
    ftr = open(out / "trades.jsonl", "w", encoding="utf-8")
    feq = open(out / "equity.jsonl", "w", encoding="utf-8")
    fde = open(out / "decisions.jsonl", "w", encoding="utf-8")
    # equity points are valued at the engine's executed quantities (README §5a), known only once every trade of the run is:
    # each instant's marks are spilled here and the series is written after the run (bounded memory, like the klines)
    fpts = open(work / "equity-marks.jsonl", "w", encoding="utf-8")
    try:
        def settle_until(t_incl: int) -> None:
            nonlocal last_settled_t
            for st, inst, s in cursor.settlements_until(t_incl, last_settled_t):
                if st < start_ms:
                    continue
                cursor.advance(st)
                b = cursor.bars[inst].last
                # F04: the settlement's published mark, else the last 1m close within DATASET_MAX_BAR_AGE_MS before it
                # an official mark is rounded to the contract's price decimals seen so far (AlphaKeel MarkRounder, R2-D-3)
                sim.settle(inst, s.rate, ir_views.settlement_price(
                    s.mark, lambda: b[2] if b is not None and st - b[1] <= ir_views.DATASET_MAX_BAR_AGE_MS else None,
                    cursor.price_dp.get(inst) if s.mark is not None else None))
            last_settled_t = t_incl if last_settled_t is None else max(last_settled_t, t_incl)

        def equity_point(t: int, touch: dict) -> None:
            fpts.write(json.dumps([t, len(sim.closed_trades), len(sim.open), sim.valuation_marks(touch, cursor.last_close)],
                                  separators=(",", ":")) + "\n")

        def write_equity() -> Decimal:
            """The equity series at the executed quantities: realized of the trades closed by then + the open legs' net_now
            at the spilled marks. Identical to valuing during the run when no quantity was cut (README §5a)."""
            nonlocal peak, peak_t
            fpts.close()
            by_trade = {tr.trade: tr for tr in sim.closed_trades + list(sim.open.values())}
            realized_upto = [Decimal(0)]
            for tr in sim.closed_trades:
                realized_upto.append(realized_upto[-1] + tr.realized())
            eq = Decimal(0)
            with open(work / "equity-marks.jsonl", encoding="utf-8") as f:
                for line in f:
                    t, n_closed, n_open, marks = json.loads(line)
                    unreal = sum((Sim.leg_value_at(by_trade[n].long if side == "L" else by_trade[n].short, Decimal(px), settled)
                                  for n, side, px, settled in marks), Decimal(0))
                    realized = realized_upto[n_closed]
                    eq = realized + unreal
                    _jsonl(feq, {"t": t, "equity": _s(eq), "realized": _s(realized), "unrealized": _s(unreal), "open_pairs": n_open})
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
            for tr in closed_pending:  # README §5: removed from positions one step later, cooling down from its close time
                cooldowns[IR.ascii_upper(tr.base)] = tr.closed_ms
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
            if diag is not None:
                diag.observe(pairs, decision["rejected"], t, sample=(counts["instants"] - 1) % diag.stride == 0)
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
                rec["exits"].append({"position_id": x["position_id"], "reason": x["reason"], "intents": [f"n{done.trade}-L-close", f"n{done.trade}-S-close"]})
            by_id = {p["id"]: p for p in pairs}
            valid = {ir_views.key_of(x) for x in legs if ir_views.valid(x)}
            counts["entries_decided"] += len(decision["entries"])
            for e in decision["entries"]:
                pair = by_id[e["pair_id"]]
                if not all((pair[leg]["venue"], pair[leg]["market"], pair[leg]["symbol"]) in valid for leg in ("long", "short")):
                    counts["entries_skipped_no_bbo"] += 1  # README §5a: no real top of book or an invalid quote; no trade number
                    continue
                qty = ir_views.entry_qty(e["notional_per_leg"], pair["long"]["ask"])
                if qty <= 0:
                    counts["entries_skipped_qty0"] += 1
                    continue
                n, next_trade = next_trade, next_trade + 1
                sim.open_pair(n, pair, qty, t, e["features"])
                rec["entries"].append({"pair_id": e["pair_id"], "position_id": f"pair-{n}", "qty": _s(qty),
                                       "intents": [f"n{n}-L-open", f"n{n}-S-open"]})
            # R2-D-3, AlphaKeel MarkRounder.observe (ab_backtest.rs): the engine frame snaps only the held legs (opened this
            # frame included) and the legs closed this frame whose quote snap_of takes, plus the intents' prices; a leg
            # merely quoted is not counted
            snaps = {ir_views.key_of(x): (x["view"]["bid"], x["view"]["ask"]) for x in legs if ir_views.snap_ok(x)}
            for tr in list(sim.open.values()) + closed_pending:
                for leg in tr.legs():
                    q = snaps.get((leg.inst.venue, leg.inst.market, leg.inst.symbol))
                    fills = [leg.entry_px] if tr.opened_ms == t else []
                    fills += [leg.exit_px] if tr.closed_ms == t else []
                    if q is not None or fills:
                        cursor.observe(leg.inst, [*(q or ()), *fills])
            if verbose:
                rec["rejected_detail"] = decision["rejected"]
            _jsonl(fde, rec)
            equity_point(t, touch)
            t += step
        # funding of settlements after the last instant (positions do not change after it), then the final valuation
        settle_until(end_ms - 1)
        cursor.advance(end_ms - 1)
        final_touch = ir_views.touch(cursor.legs())
        equity_point(end_ms - 1, final_touch)
        # the engine's executed quantities (known only once every trade of the run is): fills, fees, funding, totals and
        # the whole equity series
        sim.apply_engine_qty()
        final_eq = write_equity()
        for tr in sim.closed_trades + sorted(sim.open.values(), key=lambda x: x.trade):
            _jsonl(ftr, tr.record())
    finally:
        ftr.close()
        feq.close()
        fde.close()
        fpts.close()
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
        "step_minutes": step_minutes, "view": ir_views.DATASET_VIEW, "approximations": approximations_of(loaded.doc), "simulator": SIM_CONVENTIONS,
        "assumptions": {"funding_estimate_of_ir": loaded.doc["assumptions"]["funding_estimate"],
                        "fees": fees, "fees_source": fees_source,
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
                   "funding_unpriced": sim.funding_unpriced, "legs_engine_qty_adjusted": sim.qty_adjusted},
        "amounts": {"realized": _s(sim.realized), "price_pnl_closed": _s(sim.price_pnl), "funding": _s(sim.funding), "fees": _s(sim.fees),
                    "equity_end": _s(final_eq), "unrealized_end": _s(final_eq - sim.realized), "ccy": "USDT"},
        "max_drawdown": {"amount": _s(dd["amount"]), "peak_ms": dd["peak_ms"], "trough_ms": dd["trough_ms"],
                         "fraction_of_capital": _s(q8(dd["amount"] / capital_base))},
        "files": {"equity": "equity.jsonl", "trades": "trades.jsonl", "decisions": "decisions.jsonl"},
        "trials": trials_doc,
        "warnings": inert_warnings(loaded.doc),
        "alphakeel_metrics": ak,
        "metrics": {"total_return_on_capital": _s(q8(final_eq / capital_base)), "days": len(days), "trades_closed": sim.closed,
                    "sharpe_daily_annualised": ak.get("sharpe_annualised") if isinstance(ak, dict) else None},
        "research_service": research_service or {"used": False},
        **({"diagnostics": diag.card()} if diag is not None else {}),
        # the exported pack directory the run used (`ir robustness --run` reopens it); None when the pack came from the service
        "pack_dir": str(Path((research_service or {})["pack_dir"]).resolve()) if (research_service or {}).get("pack_dir") else None,
    }
    (out / "run_card.json").write_text(json.dumps(card, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return card


# ---------------------------------------------------------------------------------------------------------------------
# IR runs as Vibe-Trading runs: the run directory under RUNS_DIR carries the files the run list and detail pages read
# ---------------------------------------------------------------------------------------------------------------------

def runs_dir() -> Path:
    """Vibe-Trading's runs directory (``src.config.paths.get_runs_dir()``); without ``src`` (the leaf package used
    standalone) the same rule: ``$VIBE_TRADING_HOME`` or ``~/.vibe-trading``, plus ``/runs``."""
    try:
        from src.config.paths import get_runs_dir

        return Path(get_runs_dir())
    except Exception:  # noqa: BLE001 - src missing or not importable: standalone use
        import os

        home = os.environ.get("VIBE_TRADING_HOME")
        return (Path(home).expanduser() if home else Path.home() / ".vibe-trading") / "runs"


def new_run_dir(now: dt.datetime | None = None) -> Path:
    """``RUNS_DIR/<YYYYMMDD_HHMMSS>_ir`` (UTC; ``_2``, ``_3``... when it exists), created. The name sorts and parses like
    VT's own run ids (``list_runs`` takes the time from the first two ``_`` parts)."""
    t = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    base = runs_dir()
    base.mkdir(parents=True, exist_ok=True)
    stem = f"{t:%Y%m%d_%H%M%S}_ir"
    n = 1
    while True:
        d = base / (stem if n == 1 else f"{stem}_{n}")
        try:
            d.mkdir()
            return d
        except FileExistsError:
            n += 1


def _write_json(path: Path, doc: Any) -> None:
    path.write_text(json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


def _win_rate(out: Path, closed: int) -> Decimal:
    """Share of closed trades in ``trades.jsonl`` with ``realized > 0``; 0 without closed trades."""
    if not closed:
        return Decimal(0)
    wins = 0
    path = out / "trades.jsonl"
    if path.is_file():
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                tr = json.loads(line)
                if tr.get("status") == "closed" and tr.get("realized") is not None and Decimal(tr["realized"]) > 0:
                    wins += 1
    return q8(Decimal(wins) / Decimal(closed))


#: ``artifacts/metrics.csv`` columns: upstream's seven (the run page needs them all to show the metrics), then the run's
#: USDT amounts. Every cell is decimal text, which grounding reads as a number (research-truth B1).
METRICS_COLUMNS = ("final_value", "total_return", "annual_return", "max_drawdown", "sharpe", "win_rate", "trade_count",
                   "net_usdt", "fees_usdt", "funding_usdt", "price_pnl_usdt", "max_drawdown_usdt", "days")


def metrics_row(out: Path, card: dict) -> dict[str, str]:
    """The ``metrics.csv`` row of a run card, upstream column names (``runs_routes`` / grounding), decimal text."""
    capital = Decimal(card["assumptions"]["capital_base"])
    amounts = card["amounts"]
    eq = Decimal(amounts["equity_end"])
    days = int(card["metrics"]["days"])
    total = q8(eq / capital)
    sharpe = (card.get("alphakeel_metrics") or {}).get("sharpe_annualised")
    closed = int(card["counts"]["closed"])
    return {
        "final_value": _s(capital + eq),
        "total_return": _s(total),
        "annual_return": _s(q8(total * 365 / days)) if days else "0",
        "max_drawdown": _s(-Decimal(card["max_drawdown"]["fraction_of_capital"])),
        "sharpe": "" if sharpe is None else repr(sharpe),
        "win_rate": _s(_win_rate(out, closed)),
        "trade_count": str(closed),
        "net_usdt": _s(eq),
        "fees_usdt": _s(Decimal(amounts["fees"])),
        "funding_usdt": _s(Decimal(amounts["funding"])),
        "price_pnl_usdt": _s(Decimal(amounts["price_pnl_closed"])),
        "max_drawdown_usdt": _s(Decimal(card["max_drawdown"]["amount"])),
        "days": str(days),
    }


def write_run_files(out: Path, ir_doc: dict, instruments: list[dict], card: dict) -> None:
    """The files that make an ``ir backtest`` directory a Vibe-Trading run: ``ir.json`` (the IR as run, with
    ``strategy_id``), ``instruments.json``, ``req.json``, ``review_report.json`` (``total_return``, ``sharpe``,
    ``max_drawdown``, ``trade_count`` from the run card), ``artifacts/metrics.csv`` (:data:`METRICS_COLUMNS`, the
    upstream column names the run pages and grounding read), and LAST ``state.json`` ``{"status": "success"}``."""
    loaded = IR.load(ir_doc)
    doc = copy.deepcopy(loaded.doc)
    doc["strategy_id"] = loaded.strategy_id
    _write_json(out / "ir.json", doc)
    _write_json(out / "instruments.json", instruments)
    w = card["window"]
    _write_json(out / "req.json", {"prompt": f"IR {card['name']} {w['start_day']}..{w['end_day']}", "kind": "alphakeel_ir"})
    row = metrics_row(out, card)
    sharpe = (card.get("alphakeel_metrics") or {}).get("sharpe_annualised")
    _write_json(out / "review_report.json", {"total_return": float(row["total_return"]), "sharpe": sharpe,
                                             "max_drawdown": float(row["max_drawdown"]), "trade_count": int(row["trade_count"])})
    (out / "artifacts").mkdir(exist_ok=True)
    (out / "artifacts" / "metrics.csv").write_text(",".join(METRICS_COLUMNS) + "\n" + ",".join(row[c] for c in METRICS_COLUMNS) + "\n",
                                                    encoding="utf-8")
    _write_json(out / "state.json", {"status": "success"})


def backtest(client, ir_doc: dict, instruments: list[dict], *, start_ms: int, end_ms: int, out_dir: str | Path,
             cache_dir: str | Path | None = None, pack_dir: str | Path | None = None, accept_partial: bool = False, register: bool = False,
             selection: dict | str | None = None, **kw: Any) -> dict:
    """Freeze (or reuse, the request key is content-derived) the dataset pack through the research service -- or open an
    exported one (``pack_dir``) -- check it is the pack asked for, and :func:`run`; then write the Vibe-Trading run files
    (:func:`write_run_files`; ``state.json`` says ``failed`` when anything raised)."""
    out = Path(out_dir)
    try:
        fee_table = ir_views.require_fees(kw.get("fees"))  # before a pack is frozen
        # the maker-leg request checks (step, spot leg, maker.perp), the same function as the ir_backtest tool's plan()
        if IR.maker_leg(ir_doc) is not None:
            venues = list(dict.fromkeys(i["venue"] for i in universe_instruments(ir_doc, instruments)))
            bad = IR.maker_request_check(ir_doc, {v: fee_table.get(v) for v in venues}, kw.get("step_minutes", 2))
            if bad is not None:
                raise ApiError("request.invalid", bad[1], field=bad[0])
        if register:
            if client is None:
                raise ApiError("request.invalid", "registering the run needs the research-service client (or --no-register)", field="register")
            client.check()  # contract pin / credential scope fail fast, before the backtest, with the client's own error
        card = _backtest(client, ir_doc, instruments, start_ms=start_ms, end_ms=end_ms, out_dir=out, cache_dir=cache_dir,
                         pack_dir=pack_dir, accept_partial=accept_partial, selection=selection, **kw)
        # Trial ledger (audit 2 P1-1 c): every run is a trial of the research service's global ledger, registered ledger-only
        # (POST /trials: no service run); an unregistered run is refused here rather than left to look like the only one.
        if register:
            from .ir_crosscheck import record_trial_only

            r = record_trial_only(client, IR.load(ir_doc).doc, card)
            card["service_registration"] = {"registered": True, "trial_id": r["trial_id"], "trial_key": r["trial_key"],
                                            "ledger": "research-service trial ledger (POST /trials, ledger only, not executed)"}
        else:
            card["service_registration"] = {"registered": False, "reason": "not requested (--no-register / no client)"}
        (out / "run_card.json").write_text(json.dumps(card, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        write_run_files(out, ir_doc, instruments, card)
        return card
    except Exception as e:
        out.mkdir(parents=True, exist_ok=True)
        msg = getattr(e, "message", None) or str(e)
        (out / "state.json").unlink(missing_ok=True)
        _write_json(out / "state.json", {"status": "failed", "error": msg, "reason": msg})
        raise


def _backtest(client, ir_doc: dict, instruments: list[dict], *, start_ms: int, end_ms: int, out_dir: str | Path,
              cache_dir: str | Path | None = None, pack_dir: str | Path | None = None, accept_partial: bool = False,
              selection: dict | str | None = None, **kw: Any) -> dict:
    insts = universe_instruments(ir_doc, instruments)
    req = dataset_request(insts, start_ms, end_ms, accept_partial=accept_partial, selection=selection)
    service = None
    if pack_dir is not None:
        pack = Pack.from_directory(pack_dir)
        service = {"used": False, "pack_dir": str(pack_dir)}
    else:
        from . import workflow

        who = client.check()
        # A request that names no dataset version means "latest at acceptance"; keyed by its content alone, a re-run after
        # new data was published got the OLD pack back (same key, same job) -- silently stale. The UTC day in the key
        # re-freezes at most daily; unchanged data gives the same content-derived pack_id anyway.
        from .client import key_for

        day_key = key_for("pack", {"request": req, "utc_day": utc_day(int(time.time() * 1000))})
        pack = workflow.freeze_pack(client, req, cache_dir=cache_dir, key=day_key)
        service = {"used": True, "credential": who.get("credential"), "data_horizon": who.get("data_horizon"),
                   "holdout_days": who.get("holdout_days")}
    check_pack(pack, req)
    card = run(ir_doc, pack, insts, start_ms=start_ms, end_ms=end_ms, out_dir=out_dir, research_service=service, diagnostics=True, **kw)
    # G1 1.1 / 1.2 / 1.3 / 1.16 judged by code (research-truth B3): the label the agent reports verbatim. Only the
    # `ir backtest` run gets it (not the robustness battery's sub-runs, which call run() directly).
    from .screen import screen

    card["screen"] = screen(card)
    (Path(out_dir) / "run_card.json").write_text(json.dumps(card, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return card


# ---------------------------------------------------------------------------------------------------------------------
# export: the IR document AlphaKeel's registry imports
# ---------------------------------------------------------------------------------------------------------------------

# AlphaKeel's runtime entry gates the IR does not inherit (``IrDecider::runtime_warnings``, 2026-10-09 review U8): the
# built-in rules require both quotes at most ``max_quote_age_ms`` old and at most ``max_leg_skew_ms`` apart; IR frames
# are not scored, so only the IR's own upper-bound conditions gate its entries. The defaults of AlphaKeel's ScreenConfig.
ENTRY_FRESHNESS = (("quote_age_ms", "max_quote_age_ms", 5000), ("leg_skew_ms", "max_leg_skew_ms", 3000))


def _upper_bound(c: dict) -> Decimal | None:
    if c["op"] in ("<=", "<"):
        return Decimal(c["value"])
    if c["op"] == "between":
        return Decimal(c["value"][1])
    return None


def freshness_warnings(ir_doc: dict) -> list[str]:
    """Entry-freshness bounds missing from or looser than AlphaKeel's runtime defaults (same rule as AlphaKeel's
    ``ir.quote_age_unbounded`` / ``ir.leg_skew_unbounded`` / ``*_looser`` warnings; never changes the IR)."""
    out = []
    for feature, setting, limit in ENTRY_FRESHNESS:
        bounds = [b for c in ir_doc["entry"]["conditions"] if c["feature"] == feature and (b := _upper_bound(c)) is not None]
        if not bounds:
            out.append(f"entry.conditions set no upper bound on {feature}: AlphaKeel's {setting} = {limit} ms does not apply "
                       f"to IR entries; add {{\"feature\": \"{feature}\", \"op\": \"<=\", \"value\": \"{limit}\"}}")
        elif min(bounds) > limit:
            out.append(f"entry bound {feature} <= {min(bounds)} is looser than AlphaKeel's {setting} = {limit} ms; the IR's bound applies")
    return out


#: features the dataset view makes constant by construction (``ir_views`` dataset touch: bid = ask = close, synthetic bbo,
#: quote_ms = the last closed bar's close time on the step grid): a threshold on them separates nothing in this data
DATASET_INERT_FEATURES = {
    "spread": "spread is 0 on the dataset view (bid = ask = the 1m close)",
    "price_estimated": "price_estimated is false on the dataset view (synthetic bbo on every leg)",
    "quote_age_ms": "quote_age_ms is ~1 ms on the dataset view (quote_ms = the last closed bar's close time)",
    "leg_skew_ms": "leg_skew_ms is 0 on the dataset view (both legs' quote_ms = the same bar close time)",
}
#: the features that read ``assumptions.basis_stress`` (``ir.compute_features``): stress and net_conservative
BASIS_STRESS_FEATURES = ("stress", "net_conservative")
_CONDITION_VALUE = re.compile(r"(entry|exit)\.conditions\[([0-9]+)\]\.value(\[[01]\])?")


def basis_stress_inert(ir_doc: dict) -> str | None:
    """Why ``assumptions.basis_stress`` cannot change a dataset backtest of this IR, else None: it only enters the
    ``stress`` and ``net_conservative`` features, and no entry/exit condition or ``entry.rank_by`` reads either."""
    used = {c["feature"] for sec in ("entry", "exit") for c in ir_doc[sec]["conditions"]} | {ir_doc["entry"]["rank_by"]}
    if used & set(BASIS_STRESS_FEATURES):
        return None
    return (f"no entry/exit condition or rank_by reads {' or '.join(BASIS_STRESS_FEATURES)}: basis_stress does not "
            "change any decision")


def inert_parameters(ir_doc: dict) -> list[dict]:
    """``parameters`` whose ``path`` cannot change a dataset-view backtest: a condition value on a feature of
    ``DATASET_INERT_FEATURES``, or ``assumptions.basis_stress`` when :func:`basis_stress_inert`. ``[{"parameter", "path",
    "feature", "reason"}]`` sorted by name. Their robust neighbours rerun the base strategy and prove nothing."""
    out = []
    for name in sorted(ir_doc.get("parameters") or {}):
        path = ir_doc["parameters"][name]["path"]
        m = _CONDITION_VALUE.fullmatch(path)
        feature = why = None
        if m:
            conds = ir_doc[m.group(1)]["conditions"]
            i = int(m.group(2))
            feature = conds[i]["feature"] if i < len(conds) else None
            why = DATASET_INERT_FEATURES.get(feature)
        elif path == "assumptions.basis_stress":
            feature, why = "basis_stress", basis_stress_inert(ir_doc)
        if why:
            out.append({"parameter": name, "path": path, "feature": feature, "reason": f"inert on the dataset view: {why}"})
    return out


def inert_warnings(ir_doc: dict) -> list[str]:
    return [f"parameter {x['parameter']} ({x['path']}) is {x['reason']}; its robustness cannot be tested on this data "
            "(the robustness battery skips its neighbours)" for x in inert_parameters(ir_doc)]


ROBUSTNESS_SCHEMA = "vibe-trading.ir-robustness/2"
#: robustness.json's trials.jsonl lands under this name: AlphaKeel's importer whitelists exactly these evidence names
EVIDENCE_ROBUSTNESS = ("robustness.json", "robustness.md", "robustness-trials.jsonl")


class WindowMismatch(ApiError):
    """The robustness battery was not run over the run card's window (or its OOS window is not after it)."""

    def __init__(self, message: str):
        super().__init__("request.invalid", message, field="robustness")


def _robustness_windows(doc: dict, card: dict) -> dict:
    """``{"is": {start_ms,end_ms}, "oos": {start_ms,end_ms}|None}`` of the battery, after checking it against the run card:
    the IS window must be exactly the card's window; an OOS window must start at or after the card's window end."""
    inputs = doc.get("inputs") if isinstance(doc.get("inputs"), dict) else {}
    cw = card["window"]
    isw = inputs.get("is_window")
    if not (isinstance(isw, dict) and type(isw.get("start_ms")) is int and type(isw.get("end_ms")) is int):
        raise WindowMismatch("robustness.json has no inputs.is_window {start_ms,end_ms}: re-run `ir robustness`")
    if (isw["start_ms"], isw["end_ms"]) != (cw["start_ms"], cw["end_ms"]):
        raise WindowMismatch(f"window mismatch: the robustness battery ran over IS window {isw['start_ms']}..{isw['end_ms']} ms but the "
                             f"run card's window is {cw['start_ms']}..{cw['end_ms']} ms; re-run `ir robustness --run` on this run")
    oos = inputs.get("oos_window")
    oos_out = None
    if oos is not None:
        if not (isinstance(oos, dict) and type(oos.get("start_ms")) is int and type(oos.get("end_ms")) is int):
            raise WindowMismatch("robustness.json inputs.oos_window is not {start_ms,end_ms}")
        if oos["start_ms"] < cw["end_ms"]:
            raise WindowMismatch(f"window mismatch: the OOS window starts {oos['start_ms']} ms, before the run window ends "
                                 f"({cw['end_ms']} ms): it is not out of sample")
        oos_out = {"start_ms": oos["start_ms"], "end_ms": oos["end_ms"]}
    return {"is": {"start_ms": isw["start_ms"], "end_ms": isw["end_ms"]}, "oos": oos_out}


def _robustness_evidence(rd: Path, robustness_dir: str | Path | None, sid: str) -> tuple[Path, dict] | None:
    """(directory, parsed robustness.json) of the battery for ``sid``, or None when there is none. An explicitly given
    directory must hold a robustness.json; the default ``<run>/robustness`` is optional."""
    explicit = robustness_dir is not None
    d = Path(robustness_dir) if explicit else rd / "robustness"
    f = d / "robustness.json"
    if not f.is_file():
        if explicit:
            raise ApiError("request.invalid", f"{f} does not exist: run `ir robustness` first", field="robustness")
        return None
    try:
        doc = json.loads(f.read_text(encoding="utf-8"))
    except ValueError:
        raise ApiError("request.invalid", f"{f} is not JSON", field="robustness") from None
    if not isinstance(doc, dict) or doc.get("schema") != ROBUSTNESS_SCHEMA:
        raise ApiError("request.invalid", f"{f} is not a {ROBUSTNESS_SCHEMA} document: re-run `ir robustness`", field="robustness")
    if doc.get("strategy_id") != sid:
        raise ApiError("request.invalid", f"robustness.json is for {doc.get('strategy_id')}, the IR is {sid}", field="robustness")
    for n in ("robustness.md", "trials.jsonl"):
        if not (d / n).is_file():
            raise ApiError("request.invalid", f"{d / n} is missing: the robustness directory is incomplete", field="robustness")
    return d, doc


def _counted_trial_rows(path: Path) -> int:
    n = 0
    for ln in path.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            n += json.loads(ln).get("counted_trial") is True
        except (ValueError, AttributeError):
            raise ApiError("request.invalid", f"{path} has a line that is not a JSON object", field="robustness") from None
    return n


def check_handoff_consistency(rdir: Path, rdoc: dict, card: dict, reconciliation: str | Path | None, run_dir: Path) -> None:
    """Refuse an export whose evidence disagrees with itself (audit 2 P1-1 b, 2 P2-4): the robustness trial ledger has exactly
    ``trials.distinct_candidate_variants`` counted rows, ``inputs.pack_id`` is present (and the run card's pack), and a
    reconciliation file (``--reconciliation``, else ``<run>/reconciliation.json`` when present) carries a ``pack_id`` equal to
    the robustness IS pack. AlphaKeel's 1.6 / 1.15 re-check all three; failing here saves a rejected import."""
    tri = rdoc.get("trials")
    want = tri.get("distinct_candidate_variants") if isinstance(tri, dict) else None
    have = _counted_trial_rows(rdir / "trials.jsonl")
    if type(want) is not int or want != have:
        raise ApiError("request.invalid", f"trials ledger mismatch: robustness.json trials.distinct_candidate_variants is {want!r} but "
                       f"trials.jsonl has {have} counted_trial rows: re-run `ir robustness`", field="robustness")
    inputs = rdoc.get("inputs")
    pid = inputs.get("pack_id") if isinstance(inputs, dict) else None
    if not (isinstance(pid, str) and pid):
        raise ApiError("request.invalid", "robustness.json has no inputs.pack_id: re-run `ir robustness`", field="robustness")
    if pid != card["pack"]["pack_id"]:
        raise ApiError("request.invalid", f"robustness.json ran on pack {pid}, the run card on {card['pack']['pack_id']}: re-run "
                       "`ir robustness --run` on this run", field="robustness")
    rec = Path(reconciliation) if reconciliation is not None else run_dir / "reconciliation.json"
    if reconciliation is None and not rec.is_file():
        return
    try:
        rd = json.loads(rec.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ApiError("request.invalid", f"{rec} is not a readable JSON reconciliation file", field="reconciliation") from None
    rp = rd.get("pack_id") if isinstance(rd, dict) else None
    if not (isinstance(rp, str) and rp):
        raise ApiError("request.invalid", f"{rec} has no pack_id: re-run `ir review` / `ir crosscheck`", field="reconciliation")
    if rp != pid:
        raise ApiError("request.invalid", f"{rec} reconciles pack {rp}, the robustness in-sample pack is {pid}: AlphaKeel's 1.15 needs "
                       "the reconciliation on the same pack", field="reconciliation")


def export(ir_doc: dict, run_dir: str | Path, out_dir: str | Path, *, research_credential: dict | None = None,
           robustness_dir: str | Path | None = None, reconciliation: str | Path | None = None,
           data_exposure_end_ms: int | None = None) -> dict:
    """Write ``strategy.json`` (the IR with ``strategy_id`` and ``provenance``), the trials evidence,
    ``evidence/ir-backtest-audit.json`` and -- when the battery was run (``robustness_dir``, default ``<run_dir>/robustness``
    if present) -- ``evidence/robustness.json``, ``robustness.md``, ``robustness-trials.jsonl`` into ``out_dir``. Without
    robustness the export still succeeds: ``provenance.robustness`` is null and a warning says so. The definition is
    never changed (same ``strategy_id``)."""
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
    rob = _robustness_evidence(rd, robustness_dir, sid)
    rob_windows = _robustness_windows(rob[1], card) if rob is not None else None
    if rob is not None:
        check_handoff_consistency(rob[0], rob[1], card, reconciliation, rd)
    out = Path(out_dir)
    (out / "evidence").mkdir(parents=True, exist_ok=True)
    for n in EVIDENCE_ROBUSTNESS:  # an earlier export into this directory must not leave stale evidence behind
        (out / "evidence" / n).unlink(missing_ok=True)
    ev_rel = f"evidence/{src.name}"
    shutil.copyfile(src, out / ev_rel)
    window = {"start": card["window"]["start_day"], "end": card["window"]["end_day"]}
    handoff.check_window(window)
    handoff.check_trials(out, tri["count"], ev_rel)
    # the research used the IS window and the OOS window: the credential's data horizon is checked against the later end
    research_end_ms = max(card["window"]["end_ms"], (rob_windows["oos"] or {}).get("end_ms", 0)) if rob_windows else card["window"]["end_ms"]
    horizon = handoff.check_research_credential(research_credential, utc_day(research_end_ms - 1))
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
    # The input's fixed_literals pass through unchanged: AlphaKeel's G1 1.19 and precheck.py read them from this file.
    fixed = (loaded.doc.get("provenance") or {}).get("fixed_literals")
    if isinstance(fixed, dict):
        prov["fixed_literals"] = copy.deepcopy(fixed)
    # AlphaKeel protocol 2.5 (1.14): the latest end of every data read behind this research -- the run's data window, the
    # robustness windows, any read in this process (data_audit.record_exposure) and ``data_exposure_end_ms``: the reads
    # of the conversation's other processes (direct exchange pulls through the backtest tool; its exposure file).
    from backtest.data_audit import data_exposure_end_ms as process_exposure_end_ms

    ends = [card["data_window"]["end_ms"], research_end_ms, process_exposure_end_ms(),
            (card.get("data_audit") or {}).get("data_exposure_end_ms"), data_exposure_end_ms]
    prov["data_exposure_end_ms"] = max(x for x in ends if isinstance(x, int) and not isinstance(x, bool))
    warnings = []
    prov["robustness"] = None
    if rob is not None:
        rdir, rdoc = rob
        shutil.copyfile(rdir / "robustness.json", out / "evidence" / "robustness.json")
        shutil.copyfile(rdir / "robustness.md", out / "evidence" / "robustness.md")
        shutil.copyfile(rdir / "trials.jsonl", out / "evidence" / "robustness-trials.jsonl")
        prov["robustness"] = {"file": "evidence/robustness.json", "schema": rdoc["schema"], "grade": rdoc["rating"]["grade"],
                              "sha256": hashlib.sha256((out / "evidence" / "robustness.json").read_bytes()).hexdigest()}
        prov["robustness_windows"] = rob_windows
        if not (rdoc.get("service_runs") or {}).get("registered"):
            warnings.append("the battery's candidate runs were not registered in the research service's trial ledger "
                            "(service_runs.registered is not true): AlphaKeel's DSR trial floor will not include them")
    else:
        warnings.append("no robustness evidence: run `ir robustness --run DIR` first; without it AlphaKeel's G1 precheck fails every "
                        "research-side item (provenance.robustness is null)")
    # The backtest only ever saw the declared instruments; paper and live scan every listed contract. An IR without a
    # base allow-list would trade bases nobody researched (2026-10-09 review: oil and stock perps on the first paper round).
    researched = sorted({cursor_base for cursor_base in (i.get("base") for i in card["instruments"]) if cursor_base})
    allowed = loaded.doc["universe"].get("bases")
    if allowed is None or set(allowed) - set(researched):
        warnings.append(f"universe.bases {'is absent' if allowed is None else 'allows bases outside the backtest'}: "
                        f"the backtest covered only {researched}; paper/live consider every contract a scan lists")
    warnings += freshness_warnings(loaded.doc)
    warnings += inert_warnings(loaded.doc)
    if warnings:
        prov["warnings"] = warnings
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
            "provenance": prov, "audit": str(out / "evidence" / "ir-backtest-audit.json"), "warnings": warnings}
