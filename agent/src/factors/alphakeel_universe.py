"""``alphakeel:<spec>``: a crypto cross-section for the alpha bench, built from an AlphaKeel research *dataset pack*.

The stock universes (``csi300``, ``sp500``) have a multi-name panel; ``btc-usdt`` is one instrument, so no alpha-zoo factor
could be benchmarked on a crypto cross-section. This universe builds the same wide panel (``open/high/low/close/volume/
amount/vwap``, one column per base) from the 1-minute trade klines of a frozen, digest-verified AlphaKeel dataset pack, plus
point-in-time funding columns for perpetuals. It is part of the AlphaKeel patch layer (``patches/MANIFEST.md``, group
``leaf-package``); the only upstream edits are the dispatch in ``alpha_bench_tool._load_universe_panel``, the CLI choice
list and the tool's parameter description.

Spec (``alphakeel:`` followed by a path to a JSON file, or by the JSON object itself)::

    {
      "schema": "vibe-trading.alphakeel-universe/1",            # optional; checked when present
      "pack_dir": "packs/pk-...",                               # an exported pack (lock.json + content/), no network
      "pack_id": "pk-...",                                      # or: open a frozen pack through the research service
                                                                # or neither: freeze one for the period (service)
      "instruments": [{"venue": "binance", "market": "perp", "symbol": "BTCUSDT", "base": "BTC"}, ...],
      "interval": "1d",                                         # 1d (default) | 4h | 1h, UTC-aligned bars
      "bases": ["BTC", "ETH"],                                  # optional allow-list (Strategy IR universe.bases rule)
      "excluded_bases": ["LUNA"],                               # optional (IR universe.excluded_bases rule)
      "ir": "strategy.json",                                    # optional: take bases/excluded_bases from this IR
      "funding": true,                                          # funding columns for perps (default true)
      "accept_partial": false                                   # freeze mode only (default false: a gap is an error)
    }

Relative paths are resolved against the spec file's directory (the working directory for an inline object).

Point in time. A row is labelled with its bar's **open** time (like every Vibe-Trading panel) and holds only what is
visible at the bar's close (``open + interval - 1 ms``, the ``close_time_ms`` of its last minute): OHLCV from complete
buckets only (every minute present, ``close_time_ms == t + 59 999`` checked); a funding settlement enters the row of the
bar during which it becomes visible, ``available_at_ms`` or -- without it -- settlement time + the lock's
``funding.assumed_delay_ms`` (at least 60 s, same rule as ``alphakeel_research.packfile.Pit``). The bench's forward
return (close of the next bar over this close) is realised strictly after that. Funding columns:

* ``funding_rate``: the last official settlement rate visible at the bar close (per settlement, not normalised);
* ``funding_interval_hours``: that settlement's interval (NaN when the dataset does not state it);
* ``funding_sum``: the sum of the rates that became visible during the bar (0.0 when none).

The roster is the declared instrument list (one instrument per base): it is chosen by the researcher, not a
point-in-time index membership, so ``_meta.survivorship_bias`` is ``True``. A base listed during the period is NaN before
its first complete bar.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, Callable

import pandas as pd

PREFIX = "alphakeel:"
SPEC_SCHEMA = "vibe-trading.alphakeel-universe/1"
_KEYS = {"schema", "pack_dir", "pack_id", "instruments", "interval", "bases", "excluded_bases", "ir", "funding", "accept_partial"}
_INTERVALS = {"1h": 60, "4h": 240, "1d": 1440}
_MIN_MS = 60_000
_DAY_MS = 86_400_000
_KLINE_SPAN_MS = 59_999
_SETTLEMENT_KINDS = ("regular", "special")
_FIELDS = ("open", "high", "low", "close", "volume", "amount")

#: Injected by tests; production builds an ``alphakeel_research.client.Client`` from the environment.
client_factory: Callable[[], Any] | None = None


class UniverseChoices(list):
    """The CLI's benchmark-universe choice list: its names, plus any ``alphakeel:<spec>`` (argparse checks ``in``)."""

    def __contains__(self, value: object) -> bool:
        return (isinstance(value, str) and value.startswith(PREFIX)) or super().__contains__(value)


def is_alphakeel_universe(universe: object) -> bool:
    return isinstance(universe, str) and universe.startswith(PREFIX)


# ---------------------------------------------------------------------------------------------------------------------
# spec
# ---------------------------------------------------------------------------------------------------------------------

def _ascii_upper(s: str) -> str:
    from alphakeel_research import ir as IR

    return IR.ascii_upper(s)


def _base_list(value: Any, field: str) -> list[str]:
    """Same rule as the Strategy IR (README §1, rule 3): a list of non-empty, upper-case, distinct strings."""
    if not isinstance(value, list) or not all(isinstance(b, str) and b and b == _ascii_upper(b) for b in value):
        raise ValueError(f"alphakeel universe: {field} must be a list of non-empty upper-case strings")
    if len(set(value)) != len(value):
        raise ValueError(f"alphakeel universe: {field} has duplicates")
    return list(value)


def parse_spec(universe: str) -> tuple[dict, Path]:
    """``(spec, base_dir)`` from ``alphakeel:<path>`` or ``alphakeel:{json}``; every problem is a ``ValueError``."""
    if not is_alphakeel_universe(universe):
        raise ValueError(f"not an alphakeel universe: {universe!r}")
    rest = universe[len(PREFIX):].strip()
    if not rest:
        raise ValueError("alphakeel universe: give a spec file or an inline JSON object after 'alphakeel:'")
    if rest.startswith("{"):
        text, base_dir = rest, Path.cwd()
    else:
        path = Path(rest).expanduser()
        if not path.is_file():
            raise ValueError(f"alphakeel universe: spec file {path} does not exist")
        text, base_dir = path.read_text(encoding="utf-8"), path.resolve().parent
    try:
        spec = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"alphakeel universe: the spec is not JSON ({exc})") from None
    if not isinstance(spec, dict):
        raise ValueError("alphakeel universe: the spec must be a JSON object")
    unknown = sorted(set(spec) - _KEYS)
    if unknown:
        raise ValueError(f"alphakeel universe: unknown spec keys {unknown} (known: {sorted(_KEYS)})")
    if "schema" in spec and spec["schema"] != SPEC_SCHEMA:
        raise ValueError(f"alphakeel universe: schema must be {SPEC_SCHEMA!r}")
    if spec.get("pack_dir") is not None and spec.get("pack_id") is not None:
        raise ValueError("alphakeel universe: give pack_dir or pack_id, not both")
    if spec.get("interval", "1d") not in _INTERVALS:
        raise ValueError(f"alphakeel universe: interval must be one of {sorted(_INTERVALS)}")
    for k in ("funding", "accept_partial"):
        if k in spec and not isinstance(spec[k], bool):
            raise ValueError(f"alphakeel universe: {k} must be true or false")
    insts = spec.get("instruments")
    if not isinstance(insts, list) or not insts:
        raise ValueError("alphakeel universe: instruments must be a non-empty list of {venue, market, symbol, base}")
    for d in insts:
        if not isinstance(d, dict) or not all(isinstance(d.get(k), str) and d.get(k) for k in ("venue", "market", "symbol", "base")):
            raise ValueError("alphakeel universe: every instrument needs string venue, market, symbol and base "
                             "(dataset packs leave base null in their instruments table, so declare it)")
        if d["market"] not in ("perp", "spot"):
            raise ValueError(f"alphakeel universe: market must be perp or spot, got {d['market']!r}")
    return spec, base_dir


def bases_filter(spec: dict, base_dir: Path) -> dict:
    """``{"allow": [...] | None, "excluded": [...], "ir_strategy_id": ... | None}``: the spec's own lists combined with
    the IR's ``universe.bases`` / ``excluded_bases`` (allow-lists intersect, exclusions add up), so a factor is researched
    on exactly the bases the strategy may enter."""
    allow: set[str] | None = set(_base_list(spec["bases"], "bases")) if spec.get("bases") is not None else None
    if allow is not None and not allow:
        raise ValueError("alphakeel universe: bases, when given, must not be empty")
    excluded = set(_base_list(spec.get("excluded_bases", []), "excluded_bases"))
    sid = None
    if spec.get("ir") is not None:
        from alphakeel_research import ir as IR

        p = Path(spec["ir"]).expanduser()
        p = p if p.is_absolute() else base_dir / p
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"alphakeel universe: cannot read the IR {p}: {exc}") from None
        try:
            loaded = IR.load(text)
        except IR.IrError as exc:
            raise ValueError(f"alphakeel universe: the IR {p} is invalid: {exc.code}: {exc.message}") from None
        u = loaded.universe
        if u.get("bases") is not None:
            allow = set(u["bases"]) if allow is None else allow & set(u["bases"])
        excluded |= set(u.get("excluded_bases", []))
        sid = loaded.strategy_id
    return {"allow": None if allow is None else sorted(allow), "excluded": sorted(excluded), "ir_strategy_id": sid}


def roster(spec: dict, filt: dict) -> list[dict]:
    """The declared instruments that pass the bases filter, one per base, sorted by base."""
    out: dict[str, dict] = {}
    for d in spec["instruments"]:
        base = _ascii_upper(d["base"])
        if (filt["allow"] is not None and base not in filt["allow"]) or base in filt["excluded"]:
            continue
        if base in out:
            raise ValueError(f"alphakeel universe: two instruments for base {base} ({out[base]['venue']}:{out[base]['symbol']} "
                             f"and {d['venue']}:{d['symbol']}); a panel column is one base -- keep one")
        out[base] = {"venue": d["venue"], "market": d["market"], "symbol": d["symbol"], "base": base}
    if len(out) < 2:
        raise ValueError(f"alphakeel universe: {len(out)} base(s) left after the bases filter; a cross-sectional IC needs at least two")
    return [out[b] for b in sorted(out)]


# ---------------------------------------------------------------------------------------------------------------------
# the pack
# ---------------------------------------------------------------------------------------------------------------------

def _client():
    if client_factory is not None:
        return client_factory()
    from alphakeel_research.client import Client
    from src.config.accessor import get_env_value

    url = get_env_value("ALPHAKEEL_RESEARCH_URL", "").strip()
    tok = get_env_value("ALPHAKEEL_RESEARCH_TOKEN", "").strip()
    tok_file = get_env_value("ALPHAKEEL_RESEARCH_TOKEN_FILE", "").strip()
    if not tok and tok_file and Path(tok_file).expanduser().is_file():
        tok = Path(tok_file).expanduser().read_text(encoding="utf-8").strip()
    if not url or not tok:
        raise RuntimeError("alphakeel universe: pack_id / freeze mode needs ALPHAKEEL_RESEARCH_URL and ALPHAKEEL_RESEARCH_TOKEN "
                           "(or _TOKEN_FILE); use pack_dir for an exported pack")
    return Client(url, tok)


def _refs(insts: list[dict]) -> list[dict]:
    return [{"venue": d["venue"], "market": d["market"], "symbol": d["symbol"]} for d in insts]


def open_pack(spec: dict, base_dir: Path, insts: list[dict], start_ms: int, end_ms: int, *, funding: bool):
    """``(pack, service_info)``: the exported directory, the named pack, or a dataset pack frozen for exactly
    ``[start_ms, end_ms)`` with ``kline_1m`` (trade) and, for perps, the funding dataset."""
    from alphakeel_research.errors import ApiError
    from alphakeel_research.packfile import Pack

    if spec.get("pack_dir") is not None:
        p = Path(spec["pack_dir"]).expanduser()
        p = p if p.is_absolute() else base_dir / p
        if not (p / "lock.json").is_file():
            raise ValueError(f"alphakeel universe: {p} is not an exported pack (no lock.json)")
        return Pack.from_directory(p), {"used": False, "pack_dir": str(p)}
    from alphakeel_research import workflow
    from alphakeel_research.client import Client, key_for

    client = _client()
    ok = False
    try:
        who = client.check()
        if spec.get("pack_id") is not None:
            pack = Pack.open(client, spec["pack_id"], workflow.DEFAULT_CACHE)
        else:
            perps = funding and any(d["market"] == "perp" for d in insts)
            req = Client.dataset_pack_request(start_ms=start_ms, end_ms=end_ms, instruments=sorted(_refs(insts), key=lambda r: (r["venue"], r["market"], r["symbol"])),
                                              funding_dataset=None if perps else "none", market_tables=["kline_1m"], price_kinds=["trade"],
                                              accept_partial=bool(spec.get("accept_partial", False)))
            # "latest at acceptance" request: keyed by content + UTC day so a re-run after new data was published refreezes
            # (same rule as ir_backtest.backtest)
            day = time.strftime("%Y-%m-%d", time.gmtime())
            pack = workflow.freeze_pack(client, req, key=key_for("pack", {"request": req, "utc_day": day}))
            lock_req = pack.lock.get("request") or {}
            if lock_req.get("window") != req["window"] or lock_req.get("instruments") != req["instruments"]:
                raise ValueError("alphakeel universe: the service returned a pack that does not match the request; refusing to use it")
        ok = True
    except ApiError as exc:
        raise RuntimeError(f"alphakeel universe: {exc.code}: {exc.message}") from exc
    finally:
        if not ok and callable(getattr(client, "close", None)):
            client.close()
    return pack, {"used": True, "credential": who.get("credential"), "holdout_days": who.get("holdout_days")}


def check_pack(pack, insts: list[dict], start_ms: int, end_ms: int, *, funding: bool) -> None:
    """The pack must be a versioned dataset pack with trade ``kline_1m`` for every instrument over the whole period (and
    the funding dataset when perp funding is wanted); anything else is refused, never filled from another source."""
    lock = pack.lock
    req = lock.get("request") or {}
    if not pack.is_dataset:
        raise ValueError("alphakeel universe: the pack is a scan pack; this universe needs a dataset pack")
    ds = req.get("dataset") or {}
    if "kline_1m" not in (ds.get("market_tables") or []) or "trade" not in (ds.get("price_kinds") or []) or pack.dataset_version("market") is None:
        raise ValueError("alphakeel universe: the pack holds no versioned kline_1m (trade) market dataset")
    w = req.get("window") or {}
    if not (w.get("start_ms", math.inf) <= start_ms and end_ms <= w.get("end_ms", -math.inf)):
        raise ValueError(f"alphakeel universe: the pack window [{w.get('start_ms')}, {w.get('end_ms')}) does not cover the period "
                         f"[{start_ms}, {end_ms}); choose a period inside it")
    have = {(r["venue"], r["market"], r["symbol"]) for r in (req.get("instruments") or [])}
    missing = [f"{d['venue']}:{d['market']}:{d['symbol']}" for d in insts if (d["venue"], d["market"], d["symbol"]) not in have]
    if missing:
        raise ValueError(f"alphakeel universe: the pack does not hold {missing}")
    if funding and any(d["market"] == "perp" for d in insts) and pack.dataset_version("funding") is None:
        raise ValueError("alphakeel universe: the pack holds no funding dataset; set \"funding\": false to build OHLCV only")


# ---------------------------------------------------------------------------------------------------------------------
# panel
# ---------------------------------------------------------------------------------------------------------------------

def _bars(pack, insts: list[dict], start_ms: int, end_ms: int, minutes: int) -> dict[str, dict[int, list[float]]]:
    """One pass over the pack's ``kline_1m`` objects per (venue, market): complete ``minutes`` buckets per base, as
    ``{base: {bucket_open_ms: [open, high, low, close, volume_base, volume_quote]}}``."""
    from alphakeel_research.errors import EvidenceError

    span = minutes * _MIN_MS
    out: dict[str, dict[int, list[float]]] = {d["base"]: {} for d in insts}
    for venue, market in sorted({(d["venue"], d["market"]) for d in insts}):
        by_symbol = {d["symbol"]: d["base"] for d in insts if d["venue"] == venue and d["market"] == market}
        legacy = f"market/kline_1m/{venue}/part-"
        if any(o["role"] == "market" and o["name"].startswith(legacy) for o in pack.lock["objects"]):
            raise ValueError(f"alphakeel universe: the pack stores {venue} spot and perp klines in one object (old layout); rebuild it")
        tag = f"market/kline_1m/{venue}/{market}/"
        acc: dict[str, list] = {}  # base -> [bucket, o, h, l, c, v, q, n]
        last: dict[str, int] = {}

        def flush(base: str) -> None:
            a = acc.pop(base, None)
            if a is not None and a[7] == minutes:
                out[base][a[0]] = a[1:7]

        for o in sorted((o for o in pack.lock["objects"] if o["role"] == "market" and o["name"].startswith(tag)), key=lambda o: o["name"]):
            if (o["last_ms"] or 0) < start_ms or (o["first_ms"] or 0) >= end_ms:
                continue
            for r in pack._read(o["name"]):
                t, sym = r[0], r[1]
                if sym not in by_symbol or not (start_ms <= t < end_ms) or r[10] != "trade":
                    continue
                if r[2] != t + _KLINE_SPAN_MS:  # the bar is visible only from its close: a wrong close time would leak it early
                    raise EvidenceError("evidence.reference", f"kline at {t} of {venue}:{sym} has close_time_ms {r[2]}, not t + {_KLINE_SPAN_MS}")
                base = by_symbol[sym]
                if base in last and t <= last[base]:
                    raise EvidenceError("evidence.reference", f"klines of {venue}:{market}:{sym} are not in strict time order at {t}")
                last[base] = t
                b = t - t % span
                a = acc.get(base)
                if a is None or a[0] != b:
                    flush(base)
                    op, hi, lo, cl = float(r[3]), float(r[4]), float(r[5]), float(r[6])
                    acc[base] = [b, op, hi, lo, cl, float(r[7]), float(r[8]), 1]
                else:
                    a[2] = max(a[2], float(r[4]))
                    a[3] = min(a[3], float(r[5]))
                    a[4] = float(r[6])
                    a[5] += float(r[7])
                    a[6] += float(r[8])
                    a[7] += 1
            pack._mem.pop(o["name"], None)  # bounded memory: one decoded object at a time
        for base in list(acc):
            flush(base)
    return out


def _assumed_delay_ms(pack) -> int:
    from alphakeel_research.packfile import MIN_ASSUMED_DELAY_MS

    f = pack.lock.get("funding")
    d = f.get("assumed_delay_ms") if isinstance(f, dict) else None
    if not isinstance(d, int) or isinstance(d, bool) or d < MIN_ASSUMED_DELAY_MS:
        raise ValueError(f"alphakeel universe: the lock's funding.assumed_delay_ms ({d}) is missing or below {MIN_ASSUMED_DELAY_MS}; "
                         "settlements without available_at could be seen at the settlement instant")
    return d


def _funding(pack, inst: dict, index: pd.DatetimeIndex, start_ms: int, end_ms: int, span: int, delay: int) -> tuple[list, list, list]:
    """Per bar of ``index``: (last visible rate, its interval hours, sum of rates that became visible in the bar)."""
    from alphakeel_research.packfile import Inst

    lo = int(pack.lock["request"]["window"]["start_ms"])
    rows = [s for s in pack.settlements(Inst(inst["venue"], inst["market"], inst["symbol"]), lo, end_ms) if s.event_kind in _SETTLEMENT_KINDS]
    vis = sorted(((s.available_at_ms if s.available_at_ms is not None else s.t + delay), s.t, s) for s in rows)
    rate, ivh, summ = [], [], []
    j, cur, cur_iv = 0, math.nan, math.nan
    for ts in index:
        bar_open = int(ts.value // 1_000_000)
        visible_by = bar_open + span - 1  # the bar's close time (its last minute's close_time_ms)
        s_sum = 0.0
        while j < len(vis) and vis[j][0] <= visible_by:
            s = vis[j][2]
            if vis[j][0] >= bar_open:
                s_sum += float(s.rate)
            cur = float(s.rate)
            cur_iv = s.interval_seconds / 3600 if isinstance(s.interval_seconds, int) and not isinstance(s.interval_seconds, bool) else math.nan
            j += 1
        rate.append(cur)
        ivh.append(cur_iv)
        summ.append(s_sum)
    return rate, ivh, summ


def build_panel(pack, insts: list[dict], start_ms: int, end_ms: int, *, minutes: int, funding: bool) -> dict[str, Any]:
    """The wide panel (fields -> DataFrame indexed by bar open, one column per base) over ``[start_ms, end_ms)``."""
    span = minutes * _MIN_MS
    bars = _bars(pack, insts, start_ms, end_ms, minutes)
    first = start_ms + (-start_ms) % span
    index = pd.to_datetime(list(range(first, end_ms - span + 1, span)), unit="ms")
    index.name = "trade_date"
    bases = [d["base"] for d in insts]
    panel: dict[str, Any] = {}
    for k, field in enumerate(_FIELDS):
        cols = {b: pd.Series({pd.Timestamp(t, unit="ms"): v[k] for t, v in bars[b].items()}, dtype=float) for b in bases}
        panel[field] = pd.DataFrame(cols, index=index, columns=bases).astype(float)
    vol = panel["volume"]
    panel["vwap"] = (panel["amount"] / vol.where(vol > 0)).astype(float)
    perps = [d for d in insts if d["market"] == "perp"]
    if funding and perps:
        delay = _assumed_delay_ms(pack)
        f_rate, f_iv, f_sum = (pd.DataFrame(math.nan, index=index, columns=bases) for _ in range(3))
        for d in perps:
            r, iv, sm = _funding(pack, d, index, start_ms, end_ms, span, delay)
            f_rate[d["base"]], f_iv[d["base"]], f_sum[d["base"]] = r, iv, sm
        listed = panel["close"].notna()
        panel["funding_rate"] = f_rate.where(listed)
        panel["funding_interval_hours"] = f_iv.where(listed)
        panel["funding_sum"] = f_sum.where(listed)
    return panel


def load_alphakeel_panel(universe: str, start: str, end: str) -> dict[str, Any]:
    """Entry point of ``alpha_bench_tool._load_universe_panel`` for ``alphakeel:<spec>``; ``start``/``end`` are the
    period's first and last UTC days (inclusive), so the window is ``[start 00:00, end + 1 day)``."""
    spec, base_dir = parse_spec(universe)
    minutes = _INTERVALS[spec.get("interval", "1d")]
    funding = bool(spec.get("funding", True))
    filt = bases_filter(spec, base_dir)
    insts = roster(spec, filt)
    start_ms = int(pd.Timestamp(start).tz_localize(None).normalize().value // 1_000_000)
    end_ms = int(pd.Timestamp(end).tz_localize(None).normalize().value // 1_000_000) + _DAY_MS
    pack, service = open_pack(spec, base_dir, insts, start_ms, end_ms, funding=funding)
    try:
        check_pack(pack, insts, start_ms, end_ms, funding=funding)
        panel = build_panel(pack, insts, start_ms, end_ms, minutes=minutes, funding=funding)
    finally:
        close = getattr(pack.client, "close", None)
        if callable(close):
            close()
    close_df = panel["close"]
    empty = [b for b in close_df.columns if close_df[b].isna().all()]
    if close_df.shape[1] - len(empty) < 2:
        raise ValueError(f"alphakeel universe: fewer than two bases have a complete bar in the period (empty: {empty})")
    lock = pack.lock
    allow = filt["allow"]
    panel["_meta"] = {
        "universe": "alphakeel",
        "market": "crypto",
        "source": "alphakeel_dataset_pack",
        "pack_id": lock["pack_id"],
        "pack_sha256": lock["pack_sha256"],
        "dataset_versions": {d["name"]: d["dataset_version"] for d in pack.datasets()},
        "accept_partial": bool((lock.get("request") or {}).get("accept_partial")),
        "research_service": service,
        "interval": spec.get("interval", "1d"),
        "bar": {"index_label": "bar_open_time_utc", "visible_at": "bar close (open + interval - 1 ms)",
                "complete_bars_only": True, "price_kind": "trade", "volume": "base units", "amount": "quote units",
                "vwap": "amount / volume"},
        "funding": None if not (funding and any(d["market"] == "perp" for d in insts)) else {
            "source": "official settlements", "visibility": "available_at_ms, else settlement time + lock assumed_delay_ms",
            "assumed_delay_ms": _assumed_delay_ms(pack), "columns": ["funding_rate", "funding_interval_hours", "funding_sum"]},
        "instruments": insts,
        "bases": [d["base"] for d in insts],
        "bases_without_bars": empty,
        "bases_filter": filt,
        # allowed bases (spec / IR) that no declared instrument provides: the research does not cover them
        "allowed_bases_without_instrument": [] if allow is None else sorted(set(allow) - {d["base"] for d in insts}),
        # the roster is the researcher's declared list, not a point-in-time index membership
        "survivorship_bias": True,
        "pit_membership": False,
        "degraded": bool((lock.get("request") or {}).get("accept_partial")),
    }
    return panel
