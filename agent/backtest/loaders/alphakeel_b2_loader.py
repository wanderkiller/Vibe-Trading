"""``alphakeel_b2``: auditable crypto data from AlphaKeel's versioned datasets, read through the research service.

What it serves
  * OHLCV bars built from the 1-minute klines of AlphaKeel's market dataset (``kline_1m``, trade prices), resampled to
    the requested interval; only complete bars are returned.
  * Funding settlement history (``fetch_funding``) for all nine venues of the funding dataset (binance, okx, bybit,
    bitget, gate, aster, hyperliquid, lighter, backpack), 2019 onwards.

How it reads (why it is auditable)
  The loader never holds object-store credentials and never talks to an exchange. It asks the research service to freeze a
  *dataset pack* (``POST /packs`` with ``dataset``), which pins the dataset versions at acceptance, applies the strict gap
  policy (any missing minute/settlement is an error, never a silent fill) and returns a data lock whose digest names the
  content. The client then downloads the objects and verifies every one (stored digest, content digest, lock digest)
  before a row is used. Every result carries ``df.attrs["alphakeel_b2"]`` (pack id, pack digest, dataset versions and
  snapshots, window, visibility) and the same facts are recorded for the run card (``backtest.data_audit``).

Hard rules
  * No fallback, ever: it is in the no-network-fallback set, it is not part of any ``auto`` chain, and an unavailable
    market dataset, a coverage gap or a hold-out refusal is raised, not papered over with another source (use ``ccxt``
    explicitly for non-auditable exploration).
  * Hold-out: the service keeps the most recent N days (per credential, default 60) invisible; a window reaching into it
    is refused with ``data.holdout`` and surfaced here unchanged.
  * Prices become ``float`` only at this boundary (the engines are float); the pack itself and ``fetch_funding`` keep exact
    ``Decimal``.

Codes: ``venue:market:SYMBOL`` (e.g. ``okx:perp:BTC-USDT-SWAP``) or, for venues whose symbols are BASEQUOTE
(binance, bybit, bitget, aster), the shorthand ``BTC-USDT`` (spot) / ``BTC-USDT-PERP`` on the default venue
(``ALPHAKEEL_B2_VENUE``, default ``binance``).

``is_available`` only checks that a service URL and credential are configured and the client package imports; it never
touches the network.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Any

import pandas as pd

from backtest.data_audit import record_provenance
from backtest.loaders.base import NoAvailableSourceError, validate_date_range
from backtest.loaders.registry import register
from src.config.accessor import get_env_value

VENUES = ("aster", "backpack", "binance", "bitget", "bybit", "gate", "hyperliquid", "lighter", "okx")
_CONCAT_VENUES = ("aster", "binance", "bitget", "bybit")
_MARKETS = ("perp", "spot")
_DAY_MS = 86_400_000
_MINUTE_MS = 60_000
_INTERVAL_MINUTES = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30,
    "1H": 60, "1h": 60, "4H": 240, "4h": 240,
    "1D": 1440, "1d": 1440,
}


def _env(name: str, default: str = "") -> str:
    return get_env_value(name, default).strip()


def parse_code(code: str, default_venue: str = "binance", *, force_market: str | None = None) -> tuple[str, str, str]:
    """``(venue, market, symbol)`` from a loader code; ambiguity is an error, never a guess."""
    c = str(code).strip()
    parts = c.split(":")
    if len(parts) == 3:
        venue, market, symbol = parts[0].lower(), parts[1].lower(), parts[2]
    elif len(parts) == 1:
        venue = default_venue.lower()
        if venue not in _CONCAT_VENUES:
            raise ValueError(
                f"{code!r}: venue {venue!r} needs the explicit form venue:market:SYMBOL (its symbols are not BASEQUOTE)"
            )
        u = c.upper()
        market = "perp" if u.endswith("-PERP") else "spot"
        base = u[: -len("-PERP")] if market == "perp" else u
        symbol = base.replace("-", "").replace("/", "")
    else:
        raise ValueError(f"{code!r}: use venue:market:SYMBOL or BASE-QUOTE[-PERP]")
    if venue not in VENUES:
        raise ValueError(f"{code!r}: unknown venue {venue!r} (known: {', '.join(VENUES)})")
    if market not in _MARKETS:
        raise ValueError(f"{code!r}: market must be perp or spot, got {market!r}")
    if not symbol:
        raise ValueError(f"{code!r}: empty symbol")
    if force_market and market != force_market:
        raise ValueError(f"{code!r}: this call needs a {force_market} instrument")
    return venue, market, symbol


def _window_ms(start_date: str, end_date: str) -> tuple[int, int]:
    s = pd.Timestamp(start_date).tz_localize(None).normalize()
    e = pd.Timestamp(end_date).tz_localize(None).normalize() + pd.Timedelta(days=1)
    return int(s.timestamp() * 1000), int(e.timestamp() * 1000)


def resample_klines(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Aggregate 1-minute bars (index = open time) to ``minutes``; incomplete buckets are dropped, never extrapolated."""
    if minutes == 1 or df.empty:
        return df
    ms = (df.index.astype("int64") // 1_000_000)  # ns -> ms
    bucket = ms // (minutes * _MINUTE_MS)
    g = df.groupby(bucket)
    out = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
        "close": g["close"].last(), "volume": g["volume"].sum(), "_n": g["close"].size(),
    })
    out = out[out["_n"] == minutes].drop(columns="_n")
    out.index = pd.to_datetime(out.index.to_numpy() * minutes * _MINUTE_MS, unit="ms")
    out.index.name = "trade_date"
    return out


@register
class DataLoader:
    name = "alphakeel_b2"
    markets = {"crypto"}  # reachability only (registry: no-network-fallback source, never in an auto chain)
    requires_auth = True

    #: Injected by tests; production builds a ``alphakeel_research.client.Client`` from the environment.
    _client_factory = None

    # -- availability (never touches the network) ----------------------------------------------------------------

    @staticmethod
    def _credentials() -> tuple[str, str] | None:
        url = _env("ALPHAKEEL_RESEARCH_URL")
        tok = _env("ALPHAKEEL_RESEARCH_TOKEN")
        tok_file = _env("ALPHAKEEL_RESEARCH_TOKEN_FILE")
        if not tok and tok_file and os.path.isfile(tok_file):
            tok = Path(tok_file).read_text(encoding="utf-8").strip()
        return (url, tok) if url and tok else None

    def is_available(self) -> bool:
        if self._client_factory is None and self._credentials() is None:
            return False
        try:
            import alphakeel_research  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        return True

    def _client(self):
        if self._client_factory is not None:
            return self._client_factory()
        creds = self._credentials()
        if creds is None:
            raise NoAvailableSourceError(
                "alphakeel_b2: no research-service credential (set ALPHAKEEL_RESEARCH_URL and "
                "ALPHAKEEL_RESEARCH_TOKEN or ALPHAKEEL_RESEARCH_TOKEN_FILE); no fallback source is used"
            )
        from alphakeel_research.client import Client

        return Client(creds[0], creds[1])

    def _cache_dir(self) -> str:
        return _env("ALPHAKEEL_B2_CACHE_DIR") or str(Path.home() / ".vibe-trading" / "alphakeel-b2-cache")

    # -- freezing and verifying --------------------------------------------------------------------------------

    def _freeze(self, client, venue: str, market: str, symbol: str, start_ms: int, end_ms: int, *,
                klines: bool, funding: bool):
        """Ask the service for a dataset pack, wait for it, open it with full verification. Returns ``Pack``."""
        from alphakeel_research.client import key_for
        from alphakeel_research.errors import ApiError
        from alphakeel_research.packfile import Pack

        request: dict[str, Any] = {
            "window": {"start_ms": start_ms, "end_ms": end_ms}, "warmup_frames": 0, "venues": [venue],
            "instruments": [{"venue": venue, "market": market, "symbol": symbol}], "accept_partial": False,
            "include_native": False, "include_tables": True,
            "funding_dataset": None if funding else "none",
            "dataset": {"market_tables": ["kline_1m"], "price_kinds": ["trade"]} if klines else {},
        }
        key = key_for("pack", {"request": request, "salt": _env("ALPHAKEEL_B2_PACK_SALT")})
        try:
            job = client.create_pack(request, key=key)
            if job.get("status") not in ("ready", "failed", "cancelled", "interrupted", "expired"):
                job = client.wait_pack(job["job_id"])
            if job.get("status") != "ready":
                err = job.get("error") or {}
                raise ApiError(err.get("code", "data.insufficient"),
                               err.get("message", f"the pack job ended as {job.get('status')}"))
            pack = Pack.open(client, job["pack_id"], self._cache_dir())
        except ApiError as e:
            raise NoAvailableSourceError(f"alphakeel_b2: {e.code}: {e.message}") from e
        self._check_lock(pack, venue, market, symbol, start_ms, end_ms, klines=klines, funding=funding)
        return pack

    @staticmethod
    def _read(fn):
        """Objects are downloaded and verified lazily on first read: a failed check is a source error, not data."""
        from alphakeel_research.errors import ApiError

        try:
            return fn()
        except ApiError as e:
            raise NoAvailableSourceError(f"alphakeel_b2: {e.code}: {e.message}") from e

    @staticmethod
    def _check_lock(pack, venue: str, market: str, symbol: str, start_ms: int, end_ms: int, *, klines: bool, funding: bool) -> None:
        """The pack we got must be the pack we asked for (a swapped or stale pack is an error, not data)."""
        lock = pack.lock
        req = lock["request"]
        same = (
            lock["scans"] is None
            and req["window"] == {"start_ms": start_ms, "end_ms": end_ms}
            and req.get("instruments") == [{"venue": venue, "market": market, "symbol": symbol}]
        )
        if not same:
            raise NoAvailableSourceError("alphakeel_b2: the service returned a pack that does not match the request; refusing to use it")
        if klines and pack.dataset_version("market") is None:
            raise NoAvailableSourceError("alphakeel_b2: the pack has no market dataset version; refusing to use it")
        if funding and pack.dataset_version("funding") is None:
            raise NoAvailableSourceError("alphakeel_b2: the pack has no funding dataset version; refusing to use it")

    @staticmethod
    def _tag(pack, start_ms: int, end_ms: int, table: str) -> dict:
        lock = pack.lock
        tag = {
            "source": "alphakeel_b2", "pack_id": lock["pack_id"], "pack_sha256": lock["pack_sha256"], "table": table,
            "window": {"start_ms": start_ms, "end_ms": end_ms},
            "datasets": [{"name": d["name"], "dataset_version": d["dataset_version"], "snapshot_sha256": d["snapshot_sha256"],
                          "visibility": d["visibility"]} for d in pack.datasets()],
            "verified": True,
        }
        record_provenance("alphakeel_b2", tag)
        return tag

    # -- OHLCV -------------------------------------------------------------------------------------------------

    def fetch(self, codes: list[str], start_date: str, end_date: str, *, interval: str = "1D",
              fields: list[str] | None = None) -> dict[str, pd.DataFrame]:
        validate_date_range(start_date, end_date)
        minutes = _INTERVAL_MINUTES.get(interval)
        if minutes is None:
            raise NoAvailableSourceError(f"alphakeel_b2: interval {interval!r} is not supported (use one of {sorted(_INTERVAL_MINUTES)})")
        start_ms, end_ms = _window_ms(start_date, end_date)
        default_venue = _env("ALPHAKEEL_B2_VENUE", "binance")
        client = self._client()
        result: dict[str, pd.DataFrame] = {}
        try:
            for code in codes:
                try:
                    venue, market, symbol = parse_code(code, default_venue)
                except ValueError as e:
                    raise NoAvailableSourceError(f"alphakeel_b2: {e}") from e
                pack = self._freeze(client, venue, market, symbol, start_ms, end_ms, klines=True, funding=False)
                from alphakeel_research.packfile import Inst

                bars = self._read(lambda: pack.klines(Inst(venue, market, symbol), start_ms, end_ms, price_kind="trade"))
                if not bars:
                    raise NoAvailableSourceError(f"alphakeel_b2: no 1-minute bars for {code} in the window")
                df = pd.DataFrame({
                    "open": [float(b.open) for b in bars], "high": [float(b.high) for b in bars],
                    "low": [float(b.low) for b in bars], "close": [float(b.close) for b in bars],
                    "volume": [float(b.volume_base) for b in bars],
                }, index=pd.to_datetime([b.t for b in bars], unit="ms"))
                df.index.name = "trade_date"
                df = resample_klines(df, minutes)
                if df.empty:
                    raise NoAvailableSourceError(f"alphakeel_b2: the window holds no complete {interval} bar for {code}")
                df.attrs["alphakeel_b2"] = self._tag(pack, start_ms, end_ms, "kline_1m")
                result[code] = df
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        return result

    # -- funding settlement history (all nine venues) ---------------------------------------------------------

    def fetch_funding(self, codes: list[str], start_date: str, end_date: str) -> dict[str, pd.DataFrame]:
        """Official funding settlements per perpetual contract; exact ``Decimal`` rates, one row per settlement.

        Rows are reconstructed history (``availability_basis`` says how): use ``available_at_ms`` (or the settlement time
        plus the pack's assumed delay) for point-in-time questions.
        """
        validate_date_range(start_date, end_date)
        start_ms, end_ms = _window_ms(start_date, end_date)
        default_venue = _env("ALPHAKEEL_B2_VENUE", "binance")
        client = self._client()
        out: dict[str, pd.DataFrame] = {}
        try:
            for code in codes:
                try:
                    venue, market, symbol = parse_code(code, default_venue, force_market="perp")
                except ValueError as e:
                    raise NoAvailableSourceError(f"alphakeel_b2: {e}") from e
                pack = self._freeze(client, venue, market, symbol, start_ms, end_ms, klines=False, funding=True)
                from alphakeel_research.packfile import Inst

                rows = self._read(lambda: pack.settlements(Inst(venue, market, symbol), start_ms, end_ms))
                tag = self._tag(pack, start_ms, end_ms, "settlements")
                df = pd.DataFrame({
                    "rate": [r.rate for r in rows], "event_kind": [r.event_kind for r in rows],
                    "interval_seconds": [r.interval_seconds for r in rows], "interval_quality": [r.interval_quality for r in rows],
                    "settle_ccy": [r.settle_ccy for r in rows], "mark_price": [r.mark_price for r in rows],
                    "availability_basis": [r.availability_basis for r in rows], "available_at_ms": [r.available_at_ms for r in rows],
                    "dataset_version": [pack.dataset_version("funding")] * len(rows),
                }, index=pd.to_datetime([r.t for r in rows], unit="ms"))
                df.index.name = "settled_at"
                df.attrs["alphakeel_b2"] = tag
                out[code] = df
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        return out
