"""Frozen data packs on the Python side: bounded verified download, standard Decimal loader, point-in-time reader.

Nothing here ever reaches for "latest" data or a network fallback: a pack is a content-addressed set of objects named
by its data lock. Quotes, predicted funding observations, official settlements, FX and market metadata are separate
tables; scan best-bid/ask rows are never turned into OHLCV bars. Every row read is logged (table, contract, range) so
the run can state which inputs it actually loaded; the service recomputes those digests from the pack.

Exact numbers: all money, price, rate and quantity columns are decimal strings and are parsed to ``Decimal``;
nothing is converted to ``float``. DuckDB views keep decimals as VARCHAR on purpose (DuckDB DECIMAL division returns
floating point): use SQL to filter, join and locate differences, never to do accounting.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from . import contract
from .errors import ApiError, EvidenceError, FutureRead

if TYPE_CHECKING:  # the HTTP client is only needed to download; strategies read an exported pack directory
    from .client import Client

CHUNK = 8 << 20  # bounded download size per request
CACHE_MAX_BYTES_ENV = "ALPHAKEEL_PACK_CACHE_MAX_BYTES"
CACHE_MAX_BYTES_DEFAULT = 8 * 1024**3
CACHE_EVICT_MIN_AGE_S = 2 * 3600  # a unit used within this time is never evicted (a run may be reading it)

try:  # optional: without it the service decompresses (encoding=identity) and the client verifies the content digest
    import zstandard as _zstd
except ImportError:  # pragma: no cover - exercised in the clean-environment test
    _zstd = None


def D(s: Any) -> Decimal | None:
    """Parse a decimal string exactly (``None`` stays ``None``). Floats are refused."""
    if s is None:
        return None
    if not isinstance(s, str):
        raise TypeError(f"decimal columns are strings, got {type(s).__name__}")
    return Decimal(s)


def pack_digest(lock: dict) -> str:
    """Semantic digest of a data lock (frozen content only; creation time, committed seq and watermarks excluded)."""
    v = json.loads(json.dumps(lock))
    for k in ("pack_id", "pack_sha256", "created_ms"):
        v.pop(k, None)
    # The service digests ``scans``/``sources``/``funding``/``datasets`` after indexing them mutably, which materialises an
    # absent key as null (a scan pack has no ``datasets``). Existing pack ids depend on it, so the digest must include it.
    for k in ("scans", "sources", "funding", "datasets"):
        v.setdefault(k, None)
    if not isinstance(v["sources"], list):  # every lock names its sources; a lock without them is not evidence
        raise EvidenceError("evidence.reference", "the data lock has no sources list")
    if isinstance(v.get("scans"), dict):  # null for dataset packs (no scan frames)
        v["scans"].pop("committed_last_seq", None)
    for s in v["sources"]:
        s.pop("watermark_ms", None)
    if isinstance(v.get("funding"), dict):
        v["funding"].pop("watermark_ms", None)
    for d in v.get("datasets") or []:
        d.pop("watermark_ms", None)
    return contract.canonical_sha256(v)


def verify_lock(lock: dict) -> None:
    contract.validate_doc(lock)
    sha = pack_digest(lock)
    if lock["pack_sha256"] != sha or lock["pack_id"] != f"pk-{sha[:24]}":
        raise EvidenceError("evidence.hash_mismatch", "the data lock does not match its own digest")


def row_digest(row: list) -> bytes:
    return hashlib.sha256(contract.canonical_bytes(row)).digest()


def row_digest_floatfree(row: list) -> bytes:
    """``row_digest`` for a row decoded by ``Pack._read_rows_floatfree`` (float-free by construction): skips the recursive check."""
    return hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")).digest()


class Chain:
    """Ordered row-digest chain: sha256(d1 || d2 || ...), the same definition the service uses."""

    def __init__(self) -> None:
        self._h = hashlib.sha256()
        self.rows = 0
        self.first_ms: int | None = None
        self.last_ms: int | None = None

    def push(self, t_ms: int, row: list) -> None:
        self._h.update(row_digest(row))
        self.rows += 1
        self.first_ms = t_ms if self.first_ms is None else min(self.first_ms, t_ms)
        self.last_ms = t_ms if self.last_ms is None else max(self.last_ms, t_ms)

    def push_floatfree(self, t_ms: int, row: list) -> None:
        self._h.update(row_digest_floatfree(row))
        self.rows += 1
        self.first_ms = t_ms if self.first_ms is None else min(self.first_ms, t_ms)
        self.last_ms = t_ms if self.last_ms is None else max(self.last_ms, t_ms)

    def hexdigest(self) -> str:
        return self._h.hexdigest()


@dataclass(frozen=True)
class Inst:
    venue: str
    market: str
    symbol: str

    def ref(self) -> dict:
        return {"venue": self.venue, "market": self.market, "symbol": self.symbol}

    def leg(self) -> str:
        return f"{self.venue}|{self.market}|{self.symbol}"

    @staticmethod
    def parse(d: dict) -> "Inst":
        return Inst(d["venue"], d["market"], d["symbol"])


@dataclass(frozen=True)
class Quote:
    t: int
    bid: Decimal
    ask: Decimal
    bbo: bool
    mark: Decimal | None
    volume_quote: Decimal | None
    quote_ts: int | None
    frame: int


@dataclass(frozen=True)
class Observation:
    t: int
    funding_rate: Decimal
    interval_hours: int
    next_funding_ms: int | None
    funding_px: Decimal | None
    funding_px_kind: str
    frame: int


@dataclass(frozen=True)
class Settlement:
    t: int
    instrument_id: str
    rate: Decimal
    event_kind: str
    interval_seconds: int | None
    interval_quality: str
    settle_ccy: str
    contract_kind: str
    availability_basis: str
    available_at_ms: int | None
    fetched_at_ms: int
    mark_price: Decimal | None
    raw_body_sha256: str
    in_window: bool


@dataclass(frozen=True)
class Kline:
    """One 1-minute bar of a dataset pack. ``t`` is the open time; the bar is visible only from ``close_time_ms``."""

    t: int
    close_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume_base: Decimal
    volume_quote: Decimal
    trades: int | None
    price_kind: str


@dataclass(frozen=True)
class Borrow:
    """One reading of a venue's quote-currency borrow/lend pool (``borrow_lend``, design 2026-10-18 section 4.1). ``t`` is the
    time AlphaKeel read it (forward collection: the state is known from then on); the fields are None where the reading
    lacks them. ``cap_used`` = global borrowed / global cap."""

    t: int
    apr: Decimal | None
    util: Decimal | None
    cap_used: Decimal | None


@dataclass(frozen=True)
class Bbo:
    """Best bid/offer snapshot of a dataset pack. ``t`` is the exchange time; visible from ``ts_recv_ms``."""

    t: int
    ts_recv_ms: int | None
    bid: Decimal
    bid_qty: Decimal | None
    ask: Decimal
    ask_qty: Decimal | None
    update_id: int | None


@dataclass(frozen=True)
class Frame:
    i: int
    seq: int
    t: int
    fetch_ms: int
    config_hash: str
    warmup: bool
    venues: dict
    fx: Decimal | None
    all_failed: bool


@dataclass(frozen=True)
class Instrument:
    venue: str
    market: str
    symbol: str
    base: str
    quote: str
    settle: str
    contract_kind: str
    funding_instrument_id: str | None
    funding_status: str
    canonical_symbol: str | None
    multiplier: Decimal | None
    multiplier_status: str
    first_seen_ms: int
    last_seen_ms: int
    selected: bool


class PackCache:
    """Local cache of verified pack content. A partially downloaded or unverified file is never a cache hit."""

    def __init__(self, root: str | os.PathLike, max_bytes: int | None = None, *, evict: bool = False):
        self.root = Path(root)
        if max_bytes is None:
            try:
                max_bytes = int(os.environ.get(CACHE_MAX_BYTES_ENV, ""))
                if max_bytes < 0:
                    raise ValueError
            except ValueError:
                max_bytes = CACHE_MAX_BYTES_DEFAULT
        self.max_bytes = max_bytes
        (self.root / "content").mkdir(parents=True, exist_ok=True)
        (self.root / "locks").mkdir(parents=True, exist_ok=True)
        # a writer killed mid-write (tool timeout: SIGKILL) leaves its unique temporary file behind; an hour-old one is
        # nobody's any more (a write takes seconds). Temporary files only: verified content is never touched.
        stale = time.time() - 3600
        for part in (self.root / "content").glob("*.part"):
            with contextlib.suppress(OSError):
                if part.stat().st_mtime < stale:
                    part.unlink()
        if evict:  # network cache roots only: an exported pack directory is read-only evidence and is never evicted
            with contextlib.suppress(Exception):
                self.evict()

    def evict(self, now: float | None = None) -> int:
        """Delete the least recently used cache units until the cache is within ``max_bytes``; returns the bytes deleted.

        Units are verified ``content/`` files (never ``*.part``) and the first-level directories of ``spill/`` (derived
        caches, one unit each). Everything can be re-downloaded or recomputed and is verified again on use. A unit whose
        mtime is younger than :data:`CACHE_EVICT_MIN_AGE_S` is kept. ``locks/`` is never touched."""
        now = time.time() if now is None else now
        units: list[tuple[float, int, Path]] = []
        with contextlib.suppress(OSError):
            for f in (self.root / "content").iterdir():
                with contextlib.suppress(OSError):
                    if f.is_file() and not f.name.endswith(".part"):
                        st = f.stat()
                        units.append((st.st_mtime, st.st_size, f))
        with contextlib.suppress(OSError):
            for d in (self.root / "spill").iterdir():
                with contextlib.suppress(OSError):
                    if d.is_dir():
                        size = sum(x.stat().st_size for x in d.rglob("*") if x.is_file())
                        units.append((d.stat().st_mtime, size, d))
        total = sum(u[1] for u in units)
        freed = 0
        for mtime, size, path in sorted(units, key=lambda u: u[0]):
            if total <= self.max_bytes:
                break
            if now - mtime < CACHE_EVICT_MIN_AGE_S:
                continue
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except OSError:
                continue
            total -= size
            freed += size
        return freed

    def content_path(self, content_sha: str) -> Path:
        return self.root / "content" / content_sha

    def has(self, content_sha: str) -> bool:
        p = self.content_path(content_sha)
        if not p.exists():
            return False
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for blk in iter(lambda: f.read(1 << 20), b""):
                h.update(blk)
        return h.hexdigest() == content_sha

    def ensure(self, client: "Client", pack_id: str, obj: dict) -> Path:
        """Return the path of the verified (decompressed) content of one pack object, downloading it if needed."""
        csha = obj["content_sha256"]
        dst = self.content_path(csha)
        if self.has(csha):
            with contextlib.suppress(OSError):
                os.utime(dst)  # recently used: evict() removes the least recently used first
            return dst
        use_zstd = obj["codec"] == "jsonl+zstd" and _zstd is not None
        identity = obj["codec"] == "jsonl+zstd" and not use_zstd
        total = obj["content_size"] if identity else obj["size"]
        want_stored = obj["sha256"]
        last_err: ApiError | None = None
        for _attempt in range(2):  # same artifact, retried once
            # one temporary file per writer (pid + uuid, as upstream's loader cache): two calls filling the same object
            # never replace each other's file; os.replace makes the verified content visible atomically
            part = dst.with_name(f"{dst.name}.{os.getpid()}.{uuid.uuid4().hex}.part")
            h_stored = hashlib.sha256()
            buf = io.BytesIO()
            try:
                pos = 0
                while pos < total:
                    end = min(pos + CHUNK, total) - 1
                    r = client.object_range(pack_id, obj["name"], pos, end, identity=identity)
                    data = r.content
                    if len(data) != end - pos + 1:
                        raise EvidenceError("evidence.size", f"short range read for {obj['name']}")
                    h_stored.update(data)
                    buf.write(data)
                    pos = end + 1
                raw = buf.getvalue()
                if use_zstd:
                    if h_stored.hexdigest() != want_stored:
                        raise EvidenceError("evidence.hash_mismatch", f"object {obj['name']} fails its stored digest")
                    raw = _zstd.ZstdDecompressor().stream_reader(io.BytesIO(raw)).read()
                if hashlib.sha256(raw).hexdigest() != csha:
                    raise EvidenceError("evidence.hash_mismatch", f"object {obj['name']} content does not match its recorded digest")
                part.write_bytes(raw)
                os.replace(part, dst)  # becomes visible only after verification
                return dst
            except ApiError as e:
                last_err = e
                if e.code != "evidence.hash_mismatch" and not e.retryable:
                    raise
            finally:
                part.unlink(missing_ok=True)  # any failure (ApiError, OSError, ...) leaves no stray temporary file
        assert last_err is not None
        raise last_err


class AccessLog:
    """Which rows a run actually read: (table, contract) -> loaded time range."""

    def __init__(self) -> None:
        self.ranges: dict[tuple, list[int]] = {}

    def note(self, table: str, inst: Inst | None, first: int, last: int) -> None:
        k = (table, inst)
        r = self.ranges.get(k)
        if r is None:
            self.ranges[k] = [first, last]
        else:
            r[0], r[1] = min(r[0], first), max(r[1], last)


class Pack:
    """A verified, local, read-only view of a frozen data pack."""

    def __init__(self, lock: dict, cache: PackCache, client: "Client | None"):
        verify_lock(lock)
        self.lock = lock
        self.cache = cache
        self.client = client
        self.log = AccessLog()
        self._mem: dict[str, list[list]] = {}
        self._objs = {o["name"]: o for o in lock["objects"]}
        self._fetch_by_t: dict[int, dict[str, int | None]] | None = None

    @classmethod
    def open(cls, client: "Client", pack_id: str, cache_dir: str | os.PathLike) -> "Pack":
        lock = client.pack_lock(pack_id)
        return cls(lock, PackCache(cache_dir, evict=True), client)

    @classmethod
    def from_directory(cls, directory: str | os.PathLike) -> "Pack":
        """Open a pack that was exported to a directory (``lock.json`` + ``content/``): no network involved."""
        d = Path(directory)
        lock = json.loads((d / "lock.json").read_text(encoding="utf-8"))
        return cls(lock, PackCache(d), None)

    @property
    def id(self) -> str:
        return self.lock["pack_id"]

    def export(self, directory: str | os.PathLike) -> Path:
        """Materialise every object (verified) and the lock into a directory a script can read with no credentials."""
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        for o in self.lock["objects"]:
            p = self._path(o)
            tgt = d / "content" / o["content_sha256"]
            tgt.parent.mkdir(parents=True, exist_ok=True)
            if not tgt.exists():
                tgt.write_bytes(p.read_bytes())
        (d / "lock.json").write_text(json.dumps(self.lock, sort_keys=True), encoding="utf-8")
        return d

    def _path(self, obj: dict) -> Path:
        if self.client is None:
            p = self.cache.content_path(obj["content_sha256"])
            if not self.cache.has(obj["content_sha256"]):
                raise EvidenceError("data.pack_expired", f"object {obj['name']} is not in the exported pack")
            with contextlib.suppress(OSError):
                os.utime(p)  # recently used, as ensure() does
            return p
        return self.cache.ensure(self.client, self.id, obj)

    # -- raw tables -----------------------------------------------------------------------------------------------

    def _read(self, name: str) -> list[list]:
        if name in self._mem:
            return self._mem[name]
        obj = self._objs[name]
        raw = self._path(obj).read_bytes()
        lines = raw.split(b"\n")
        rows = [json.loads(ln) for ln in lines[1:] if ln]  # line 0 is the header
        if len(self._mem) > 6:
            self._mem.pop(next(iter(self._mem)))
        self._mem[name] = rows
        return rows

    def _read_rows_floatfree(self, name: str) -> list[list]:
        """Like ``_read`` (uncached) but every line is decoded refusing floats, so the rows are float-free by construction."""
        line = 0

        def refuse(_s: str):
            raise contract.ContractError([contract.Violation("schema.float", f"{name}#{line}", "floating-point numbers are not allowed")])

        raw = self._path(self._objs[name]).read_bytes()
        rows = []
        for line, ln in enumerate(raw.split(b"\n")):
            if line and ln:  # line 0 is the header
                rows.append(json.loads(ln, parse_float=refuse, parse_constant=refuse))
        return rows

    def _json_object(self, name: str) -> dict:
        return json.loads(self._path(self._objs[name]).read_bytes())

    def _chunks(self, role: str, venue: str, market: str) -> list[dict]:
        tag = f"{venue}-{market}/"
        v = [o for o in self.lock["objects"] if o["role"] == role and tag in o["name"]]
        return sorted(v, key=lambda o: o["first_ms"] or 0)

    def _inst_rows(self, role: str, inst: Inst, start: int, end: int, chain: Chain | None = None) -> list[list]:
        out = []
        for o in self._chunks(role, inst.venue, inst.market):
            if (o["last_ms"] or 0) < start or (o["first_ms"] or 0) >= end:
                continue
            for r in self._read(o["name"]):
                if r[1] == inst.symbol and start <= r[0] < end:
                    out.append(r)
                    if chain is not None:
                        chain.push(r[0], r)
        return out

    # -- typed loaders (not point-in-time safe on their own: strategies get a Pit view) ------------------------------

    def quotes(self, inst: Inst, start: int, end: int) -> list[Quote]:
        rows = self._inst_rows("quotes", inst, start, end)
        if rows:
            self.log.note("quotes", inst, rows[0][0], rows[-1][0])
        return [Quote(r[0], D(r[2]), D(r[3]), r[4], D(r[5]), D(r[6]), r[7], r[8]) for r in rows]

    def observations(self, inst: Inst, start: int, end: int) -> list[Observation]:
        rows = self._inst_rows("observations", inst, start, end)
        if rows:
            self.log.note("observations", inst, rows[0][0], rows[-1][0])
        return [Observation(r[0], D(r[2]), r[3], r[4], D(r[5]), r[6], r[7]) for r in rows]

    def settlements(self, inst: Inst, start: int, end: int) -> list[Settlement]:
        ins = self.instruments().get(inst)
        if ins is None or not ins.funding_instrument_id:
            return []
        out = []
        tag = f"settlements/{inst.venue}/"
        for o in self.lock["objects"]:
            if o["role"] != "settlements" or not o["name"].startswith(tag):
                continue
            for r in self._read(o["name"]):
                if r[1] == ins.funding_instrument_id and start <= r[0] < end:
                    out.append(Settlement(r[0], r[1], D(r[2]), r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10], D(r[11]), r[12], r[13]))
        out.sort(key=lambda s: s.t)
        if out:
            self.log.note("settlements", inst, out[0].t, out[-1].t)
        return out

    # -- dataset packs (request.dataset): market tables, no scan frames -----------------------------------------------

    @property
    def is_dataset(self) -> bool:
        return self.lock.get("scans") is None

    def datasets(self) -> list[dict]:
        """The versioned datasets this pack froze (``name``, ``dataset_version``, ``snapshot_sha256``...)."""
        return list(self.lock.get("datasets") or [])

    def dataset_version(self, name: str) -> str | None:
        for d in self.datasets():
            if d["name"] == name:
                return d["dataset_version"]
        f = self.lock.get("funding")
        return f["dataset_version"] if name == "funding" and isinstance(f, dict) else None

    def _market_rows(self, table: str, inst: Inst, start: int, end: int, chain: Chain | None = None) -> list[list]:
        """Rows of one contract (venue + market + symbol). Objects are ``market/<table>/<venue>/<market>/part-N``: spot and
        perp contracts of a venue can share a symbol (Binance BTCUSDT) and a row carries only the symbol. A pack in the
        older layout (``market/<table>/<venue>/part-N``, both markets in one object) cannot tell them apart and is refused.
        ``instrument_meta`` is not windowed (``t`` is the snapshot day; point-in-time filtering is the caller's)."""
        legacy = f"market/{table}/{inst.venue}/part-"
        if any(o["role"] == "market" and o["name"].startswith(legacy) for o in self.lock["objects"]):
            raise EvidenceError("evidence.reference", f"this pack stores {table} rows of {inst.venue} spot and perp contracts in one object "
                                "(layout before market-separated objects): a symbol listed on both markets cannot be told apart; rebuild the pack")
        tag = f"market/{table}/{inst.venue}/{inst.market}/"
        symbol = inst.symbol
        out: list[list] = []
        for o in sorted((o for o in self.lock["objects"] if o["role"] == "market" and o["name"].startswith(tag)), key=lambda o: o["name"]):
            if table != "instrument_meta" and ((o["last_ms"] or 0) < start or (o["first_ms"] or 0) >= end):
                continue
            for r in self._read(o["name"]):
                if r[1] == symbol and (table == "instrument_meta" or start <= r[0] < end):
                    out.append(r)
                    if chain is not None:
                        chain.push(r[0], r)
        return out

    def klines(self, inst: Inst, start: int, end: int, *, price_kind: str = "trade", interval: str = "1m") -> list[Kline]:
        """Bars of table ``kline_<interval>`` (``1m``; ``1h`` and ``1d`` when the pack holds those tables, else none)."""
        table = f"kline_{interval}"
        rows = self._market_rows(table, inst, start, end)
        if rows:
            self.log.note(table, inst, rows[0][0], rows[-1][0])
        out = [Kline(r[0], r[2], D(r[3]), D(r[4]), D(r[5]), D(r[6]), D(r[7]), D(r[8]), r[9], r[10]) for r in rows if r[10] == price_kind]
        out.sort(key=lambda k: k.t)
        return out

    #: ``borrow_lend`` columns: t (read time), symbol (``lend:<CCY>``), apr, utilization, total supply, total borrowed,
    #: global cap, read_ms (when it became known; null = ``t``). Lives under the pseudo market ``lend`` of the venue.
    @staticmethod
    def borrow_inst(venue: str, quote_ccy: str) -> Inst:
        return Inst(venue, "lend", f"lend:{quote_ccy}")

    def has_table(self, table: str, inst: Inst) -> bool:
        tag = f"market/{table}/{inst.venue}/{inst.market}/"
        return any(o["role"] == "market" and o["name"].startswith(tag) for o in self.lock["objects"])

    def borrow_lend(self, venue: str, quote_ccy: str, start: int, end: int, declared_cap: Decimal | None = None) -> list[tuple[int, Borrow]]:
        """``[(visible_ms, Borrow)]`` of the venue's ``quote_ccy`` pool in ``[start, end)`` by read time, sorted by
        ``(visible_ms, t)``; ``visible_ms`` = ``read_ms`` (else ``t``), never before ``t``. Empty when the pack has no such table.
        ``cap_used`` = total borrowed / the reading's global cap when that is positive, else / ``declared_cap`` (the request's
        ``borrow_cap``; None = no declared cap)."""
        inst = self.borrow_inst(venue, quote_ccy)
        if not self.has_table("borrow_lend", inst):
            return []
        rows = self._market_rows("borrow_lend", inst, start, end)
        if rows:
            self.log.note("borrow_lend", inst, rows[0][0], rows[-1][0])
        out = []
        for r in rows:
            borrowed, cap = D(r[5]), D(r[6])
            if cap is None or cap <= 0:
                cap = declared_cap
            cap_used = borrowed / cap if borrowed is not None and cap is not None and cap > 0 else None
            vis = max(r[0], r[7]) if len(r) > 7 and r[7] is not None else r[0]
            out.append((vis, Borrow(r[0], D(r[2]), D(r[3]), cap_used)))
        out.sort(key=lambda x: (x[0], x[1].t))
        return out

    def bbo(self, inst: Inst, start: int, end: int) -> list[Bbo]:
        rows = self._market_rows("bbo", inst, start, end)
        if rows:
            self.log.note("bbo", inst, rows[0][0], rows[-1][0])
        return [Bbo(r[0], r[2], D(r[3]), D(r[4]), D(r[5]), D(r[6]), r[7]) for r in rows]

    _META_COLS = ("t", "symbol", "tick_size", "lot_size", "min_qty", "min_notional", "listed_ms", "delisted_ms", "extra")

    def instrument_meta_history(self, inst: Inst) -> list[dict]:
        """Every metadata snapshot of the pack, in time order, as ``{column: value}`` (decimals stay strings; unknown
        columns, e.g. ``contract_value``, under ``extra``). ``t`` is the snapshot day: the snapshot is known from then on.
        The pack holds the snapshot in effect at the window start plus each one inside the window."""
        rows = self._market_rows("instrument_meta", inst, 0, 0)
        for r in rows:
            if not isinstance(r[0], int) or isinstance(r[0], bool):
                raise EvidenceError("evidence.reference", "instrument_meta t is not an integer")
        return [dict(zip(self._META_COLS, r)) for r in sorted(rows, key=lambda r: r[0])]

    def instrument_meta(self, inst: Inst) -> dict | None:
        """The LATEST metadata snapshot of the pack (not point-in-time: specs changed later in the window show here;
        strategies get ``Pit.instrument_meta``, which returns the snapshot known at ``as_of``)."""
        rows = self.instrument_meta_history(inst)
        return rows[-1] if rows else None

    def frames(self, *, include_warmup: bool = True) -> list[Frame]:
        rows = self._read("frames/frames.jsonl.zst")
        fr = [Frame(r[0], r[1], r[2], r[3], r[5], r[8], {v[0]: {"ok": v[1], "fetched_ms": v[2], "error": v[3]} for v in r[9]}, D(r[10]), r[11]) for r in rows]
        return fr if include_warmup else [f for f in fr if not f.warmup]

    def venue_fetch_ms(self, t: int, venue: str) -> int | None:
        """The time AlphaKeel fetched ``venue`` in the scan frame decided at ``t`` (frames table), or None (no such frame,
        venue not in it, or no fetch time recorded). A quote without an exchange time (``quote_ts`` null) is as old as this."""
        if self._fetch_by_t is None:
            self._fetch_by_t = {r[2]: {v[0]: v[2] for v in r[9]} for r in self._read("frames/frames.jsonl.zst")}
        return self._fetch_by_t.get(t, {}).get(venue)

    def fx(self, start: int, end: int) -> list[tuple[int, Decimal | None]]:
        out = [(r[0], D(r[1])) for r in self._read("fx/fx.jsonl.zst") if start <= r[0] < end]
        if out:
            self.log.note("fx", None, out[0][0], out[-1][0])
        return out

    def instruments(self) -> dict[Inst, Instrument]:
        out = {}
        for r in self._read("instruments/instruments.jsonl.zst"):
            out[Inst(r[0], r[1], r[2])] = Instrument(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], D(r[10]), r[11], r[12], r[13], r[14])
        return out

    def fees(self) -> dict:
        return self._json_object("fees/fees.json")

    def coverage(self) -> dict:
        name = "coverage/datasets.json" if "coverage/datasets.json" in self._objs else "coverage/funding.json"
        return self._json_object(name)

    # -- the inputs this run actually read -----------------------------------------------------------------------

    def accesses(self) -> list[dict]:
        """``run-manifest.inputs``: one entry per (table, contract) with the range read and its row-digest chain,
        recomputed here from the verified rows (the service recomputes it again from its own copy)."""
        out = []
        for (table, inst), (first, last) in sorted(self.log.ranges.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
            ch = Chain()
            if table in ("quotes", "observations") and inst is not None:
                self._inst_rows(table, inst, first, last + 1, ch)
            elif table == "settlements" and inst is not None:
                ins = self.instruments().get(inst)
                tag = f"settlements/{inst.venue}/"
                for o in self.lock["objects"]:
                    if o["role"] == "settlements" and o["name"].startswith(tag):
                        for r in self._read(o["name"]):
                            if ins and r[1] == ins.funding_instrument_id and first <= r[0] < last + 1:
                                ch.push(r[0], r)
            elif table in ("kline_1m", "kline_1h", "kline_1d", "bbo", "borrow_lend") and inst is not None:
                self._market_rows(table, inst, first, last + 1, ch)
            elif table == "fx":
                for r in self._read("fx/fx.jsonl.zst"):
                    if first <= r[0] <= last:
                        ch.push(r[0], r)
            elif table == "frames":
                for r in self._read("frames/frames.jsonl.zst"):
                    if r[8] is not True and first <= r[2] <= last:
                        ch.push(r[2], r)
            else:
                continue
            out.append({"table": table, "instrument": inst.ref() if inst else None, "first_ms": first, "last_ms": last,
                        "rows": ch.rows, "chain_sha256": ch.hexdigest()})
        return out

    def note_frames(self, first: int, last: int) -> None:
        self.log.note("frames", None, first, last)

    # -- DuckDB (filter / join / diff location on frozen slices; decimals stay VARCHAR) -------------------------------

    def duckdb(self, insts: Iterable[Inst], start: int, end: int):
        """An in-memory DuckDB connection with explicit-schema tables ``quotes``, ``observations``, ``fx``.

        Decimal columns are VARCHAR; cast with care and never do accounting in SQL.
        """
        import duckdb  # already a Vibe-Trading dependency

        con = duckdb.connect(":memory:")
        con.execute("CREATE TABLE quotes(t BIGINT, venue VARCHAR, market VARCHAR, symbol VARCHAR, bid VARCHAR, ask VARCHAR, bbo BOOLEAN, mark VARCHAR, volume_quote VARCHAR, quote_ts BIGINT, frame BIGINT)")
        con.execute("CREATE TABLE observations(t BIGINT, venue VARCHAR, market VARCHAR, symbol VARCHAR, funding_rate VARCHAR, interval_hours INTEGER, next_funding_ms BIGINT, funding_px VARCHAR, funding_px_kind VARCHAR, frame BIGINT)")
        con.execute("CREATE TABLE fx(t BIGINT, usdc_usdt VARCHAR)")
        for inst in insts:
            for r in self._inst_rows("quotes", inst, start, end):
                con.execute("INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?,?,?,?)", [r[0], inst.venue, inst.market, r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8]])
            for r in self._inst_rows("observations", inst, start, end):
                con.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?)", [r[0], inst.venue, inst.market, r[1], r[2], r[3], r[4], r[5], r[6], r[7]])
            self.log.note("quotes", inst, start, end - 1)
        for t, v in self.fx(start, end):
            con.execute("INSERT INTO fx VALUES (?,?)", [t, None if v is None else format(v, "f")])
        return con


#: Minimum assumed publication delay of an official settlement without ``available_at`` (the service's floor). A lock that
#: declares less (old builds wrote 0) would make a settlement visible at its settlement instant: it is refused.
MIN_ASSUMED_DELAY_MS = 60_000
_KLINE_SPAN_MS = 59_999
#: ``close_time_ms - t`` of a bar of each interval
KLINE_SPANS_MS = {"1m": 59_999, "1h": 3_599_999, "1d": 86_399_999}


class Pit:
    """The standard point-in-time reader a strategy is given: it exposes only inputs visible at ``as_of_ms``.

    It is the *only* data object a strategy receives (the unfiltered ``Pack`` is not handed out). Rules, identical to the
    service's ``/slice?as_of_ms=``:

    * a window ending after ``as_of_ms + 1`` raises ``FutureRead``;
    * quotes: the frame time and, when present, the exchange ``quote_ts`` must not be after ``as_of`` (no future
      tolerance for decisions; the engine's fill-side tolerance is in the execution profile's ``quotes`` section);
    * official settlements (reconstructed history): visible when ``available_at`` — or, without it, the settlement time
      plus the lock's ``funding.assumed_delay_ms`` (at least 60 s) — is not after ``as_of``; a pack without a funding
      block has no settlements and asking for them is an error, never "delay 0";
    * 1-minute klines: visible when ``close_time_ms <= as_of`` (the service's ``/slice`` uses the same comparison);
      ``close_time_ms`` must be exactly ``t + 59 999`` (checked, not trusted);
    * bbo: visible from ``ts_recv_ms``; without it never visible;
    * instrument metadata: the last snapshot whose ``t`` (snapshot day) is not after ``as_of`` — later spec changes are
      unknown; a contract listed after ``as_of`` does not exist yet; a delisting after ``as_of`` is unknown.

    A hash cannot prove an arbitrary script has no look-ahead; this reader is the enforcement point for the data it hands
    out. It is not a security boundary (Python has no private state), but no API of it returns unfiltered rows.
    """

    __slots__ = ("__pack", "as_of_ms", "_delay")

    def __init__(self, pack: Pack, as_of_ms: int):
        self.__pack = pack
        self.as_of_ms = int(as_of_ms)
        f = pack.lock.get("funding")
        if f is None:
            self._delay: int | None = None
        else:
            d = f.get("assumed_delay_ms") if isinstance(f, dict) else None
            if not isinstance(d, int) or isinstance(d, bool):
                raise EvidenceError("evidence.reference", "the data lock's funding block has no integer assumed_delay_ms")
            if d < MIN_ASSUMED_DELAY_MS:
                raise EvidenceError("evidence.reference", f"the data lock declares funding.assumed_delay_ms = {d}, below the "
                                    f"minimum {MIN_ASSUMED_DELAY_MS}: settlements would be visible at the settlement instant; rebuild the pack")
            self._delay = d

    @property
    def pack_id(self) -> str:
        return self.__pack.id

    def _check(self, end: int) -> None:
        if end > self.as_of_ms + 1:
            raise FutureRead(f"end_ms {end} is after as_of_ms {self.as_of_ms}: only inputs visible at as_of are available")

    def quotes(self, inst: Inst, start: int, end: int | None = None) -> list[Quote]:
        """Scan quotes of frames at or before ``as_of`` whose exchange time (when recorded) is not after ``as_of``."""
        end = self.as_of_ms + 1 if end is None else end
        self._check(end)
        return [q for q in self.__pack.quotes(inst, start, end) if q.quote_ts is None or q.quote_ts <= self.as_of_ms]

    def observations(self, inst: Inst, start: int, end: int | None = None) -> list[Observation]:
        end = self.as_of_ms + 1 if end is None else end
        self._check(end)
        return self.__pack.observations(inst, start, end)

    def settlements(self, inst: Inst, start: int, end: int | None = None) -> list[Settlement]:
        end = self.as_of_ms + 1 if end is None else end
        self._check(end)
        if self._delay is None:
            raise EvidenceError("data.funding_missing", "the pack has no funding dataset block: it holds no settlements")
        rows = self.__pack.settlements(inst, start, end)
        return [s for s in rows if (s.available_at_ms if s.available_at_ms is not None else s.t + self._delay) <= self.as_of_ms]

    def fx(self, start: int, end: int | None = None) -> list[tuple[int, Decimal | None]]:
        end = self.as_of_ms + 1 if end is None else end
        self._check(end)
        return self.__pack.fx(start, end)

    def klines(self, inst: Inst, start: int, end: int | None = None, *, price_kind: str = "trade", interval: str = "1m") -> list[Kline]:
        """Bars (1-minute by default; ``interval`` ``1h`` / ``1d`` for those tables) that are already closed at ``as_of``
        (``close_time_ms <= as_of``): an open bar is not visible.

        ``close_time_ms`` must be exactly ``t`` + the bar length - 1 ms (59 999 for 1m): a row claiming an earlier close would
        otherwise leak its close price before the bar ended, so a wrong value is an evidence error.
        """
        end = self.as_of_ms + 1 if end is None else end
        self._check(end)
        span = KLINE_SPANS_MS[interval]
        out = []
        for k in self.__pack.klines(inst, start, end, price_kind=price_kind, interval=interval):
            if k.close_time_ms != k.t + span:
                raise EvidenceError("evidence.reference", f"kline at {k.t} has close_time_ms {k.close_time_ms}, not t + {span}")
            if k.close_time_ms <= self.as_of_ms:
                out.append(k)
        return out

    def bbo(self, inst: Inst, start: int, end: int | None = None) -> list[Bbo]:
        """Snapshots received by ``as_of`` (``ts_recv_ms``); a snapshot without a receive time is never visible."""
        end = self.as_of_ms + 1 if end is None else end
        self._check(end)
        return [b for b in self.__pack.bbo(inst, start, end) if b.ts_recv_ms is not None and b.ts_recv_ms <= self.as_of_ms]

    def window_start_ms(self) -> int:
        """Start of the pack's frozen request window (a dataset pack's funding, klines and tables all start there)."""
        return int(self.__pack.lock["request"]["window"]["start_ms"])

    def borrow_lend(self, venue: str, quote_ccy: str, declared_cap: Decimal | None = None) -> Borrow | None:
        """The latest reading of the venue's ``quote_ccy`` pool known at ``as_of`` (visible from its read time), or None
        (no such table, or no reading yet). ``declared_cap``: see ``Pack.borrow_lend``."""
        rows = self.__pack.borrow_lend(venue, quote_ccy, 0, self.as_of_ms + 1, declared_cap)
        known = [b for vis, b in rows if vis <= self.as_of_ms]
        return known[-1] if known else None

    def instrument_meta(self, inst: Inst) -> dict | None:
        """Metadata as known at ``as_of``: the last snapshot with ``t <= as_of`` (none yet → ``None``); not listed yet →
        ``None``; a later delisting is not known (``delisted_ms`` = None)."""
        known = [m for m in self.__pack.instrument_meta_history(inst) if m["t"] <= self.as_of_ms]
        if not known:
            return None
        m = known[-1]
        for k in ("listed_ms", "delisted_ms"):
            if m[k] is not None and (not isinstance(m[k], int) or isinstance(m[k], bool)):
                raise EvidenceError("evidence.reference", f"instrument_meta {k} is not an integer")
        if m["listed_ms"] is not None and m["listed_ms"] > self.as_of_ms:
            return None
        if m["delisted_ms"] is not None and m["delisted_ms"] > self.as_of_ms:
            m = {**m, "delisted_ms": None}
        return m

    def instruments(self) -> dict[Inst, Instrument]:
        """Contract identities of the pack visible at ``as_of``: a contract first seen after ``as_of`` is not returned, and
        ``last_seen_ms`` is cut at ``as_of`` (the pack's value is a scan fact of the whole window, i.e. a look-ahead)."""
        return {k: replace(v, last_seen_ms=None if v.last_seen_ms is None else min(v.last_seen_ms, self.as_of_ms))
                for k, v in self.__pack.instruments().items() if v.first_seen_ms is None or v.first_seen_ms <= self.as_of_ms}

    def fees(self) -> dict:
        """The pack's static fee table (it does not change inside a pack)."""
        return self.__pack.fees()

    def venue_fetch_ms(self, venue: str) -> int | None:
        """When ``venue`` was fetched for the scan frame decided at ``as_of`` (never after it); None if not recorded."""
        return self.__pack.venue_fetch_ms(self.as_of_ms, venue)

    def latest_quote(self, inst: Inst) -> Quote | None:
        rows = self.quotes(inst, self.as_of_ms - 24 * 3_600_000, self.as_of_ms + 1)
        return rows[-1] if rows else None
