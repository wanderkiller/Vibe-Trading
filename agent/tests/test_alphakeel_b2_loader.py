"""The alphakeel_b2 loader, dataset packs in the Python client, hold-out errors, run-card audit marks, sandbox isolation.

Offline: a fake service hands out dataset packs built in this file (the same object formats the AlphaKeel service writes:
header line + JSON rows, zstd, content-addressed). Nothing here touches the network or a real AlphaKeel service; the
Rust side builds the same lock shape and both validate it against the pinned contract.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import textwrap
import types
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
import zstandard

from alphakeel_research import contract, packfile
from alphakeel_research import policy as policy_mod
from alphakeel_research.errors import ApiError
from backtest import data_audit
from backtest.loaders import registry
from backtest.loaders.base import NoAvailableSourceError

H = "ab" * 32
DAY = 86_400_000
START = 1_672_531_200_000  # 2023-01-01T00:00:00Z


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _obj(name: str, role: str, schema_id: str, header: dict, rows: list[list]) -> tuple[dict, bytes]:
    content = b"".join([contract.canonical_bytes(header) + b"\n"] + [contract.canonical_bytes(r) + b"\n" for r in rows])
    stored = zstandard.ZstdCompressor(level=3).compress(content)
    chain = hashlib.sha256()
    for r in rows:
        chain.update(packfile.row_digest(r))
    ts = [r[0] for r in rows if isinstance(r[0], int)]
    return ({"name": name, "role": role, "schema_id": schema_id, "codec": "jsonl+zstd", "sha256": sha(stored), "size": len(stored),
             "content_sha256": sha(content), "content_size": len(content), "rows": len(rows),
             "first_ms": min(ts) if ts else None, "last_ms": max(ts) if ts else None, "rows_sha256": chain.hexdigest()}, stored)


def kline_rows(symbol: str, minutes: int = 1440, kind: str = "trade", start: int = START) -> list[list]:
    out = []
    for i in range(minutes):
        t = start + i * 60_000
        p = Decimal(100) + Decimal(i) / 100
        out.append([t, symbol, t + 59_999, str(p), str(p + 1), str(p - 1), str(p + Decimal("0.5")), "2", str(2 * p), 3, kind])
    return out


def settlement_rows(inst_id: str, n: int = 3) -> list[list]:
    return [[START + i * 8 * 3_600_000, inst_id, "0.0001" if i % 2 == 0 else "-0.00005", "regular", 28800, "official", "USDT", "linear",
             "reconstructed_assumption", None, START + 9 * DAY, "100.5", H, True] for i in range(n)]


def build_pack(*, venue="binance", market="perp", symbol="BTCUSDT", start=START, end=START + DAY, klines: list[list] | None = None,
               settlements: list[list] | None = None, accept_partial: bool = False,
               funding_coverage: dict | None = None) -> tuple[dict, dict[str, bytes]]:
    objs: list[dict] = []
    blobs: dict[str, bytes] = {}
    datasets = []
    sources = []
    tables = []
    if klines is not None:
        o, b = _obj(f"market/kline_1m/{venue}/{market}/part-00000.jsonl.zst", "market", "alphakeel.rows.market-kline-1m/1",
                    {"schema": "alphakeel.rows.market-kline-1m/1", "table": "kline_1m", "columns": ["t", "symbol", "close_time_ms"]}, klines)
        objs.append(o)
        blobs[o["name"]] = b
        tables.append("kline_1m")
        datasets.append({"name": "market", "dataset_version": "mkt-fixture-v1", "snapshot_sha256": H, "schema_version": "1",
                         "visibility": "historical_reconstruction", "tables": ["kline_1m"], "venues": [venue], "watermark_ms": end})
        sources.append({"role": "market", "identity": f"market-dataset:mkt-fixture-v1/venue={venue}/table=kline_1m", "watermark_ms": end,
                        "visibility": "historical_reconstruction"})
    inst_id = f"{venue}:linear:USDT:{symbol}"
    if settlements is not None:
        o, b = _obj(f"settlements/{venue}/part-00000.jsonl.zst", "settlements", "alphakeel.rows.settlements/1",
                    {"schema": "alphakeel.rows.settlements/1", "venue": venue}, settlements)
        objs.append(o)
        blobs[o["name"]] = b
        datasets.append({"name": "funding", "dataset_version": "fund-fixture-v1", "snapshot_sha256": H, "schema_version": "1",
                         "visibility": "historical_reconstruction", "tables": ["settlements"], "venues": [venue], "watermark_ms": end})
        sources.append({"role": "funding_settlement", "identity": f"funding-dataset:fund-fixture-v1/venue={venue}", "watermark_ms": end,
                        "visibility": "historical_reconstruction"})
    inst_row = [venue, market, symbol, None, None, None, None, inst_id if settlements is not None else None,
                "complete" if settlements is not None else "not_applicable", None, None, "not_recorded_in_dataset", None, None, True]
    o, b = _obj("instruments/instruments.jsonl.zst", "instruments", "alphakeel.rows.instruments/1",
                {"schema": "alphakeel.rows.instruments/1", "columns": ["venue"]}, [inst_row])
    objs.append(o)
    blobs[o["name"]] = b
    cov = contract.canonical_bytes({"schema": "alphakeel.coverage/1", "funding": [], "market": []})
    objs.append({"name": "coverage/datasets.json", "role": "coverage", "schema_id": "alphakeel.coverage/1", "codec": "json", "sha256": sha(cov),
                 "size": len(cov), "content_sha256": sha(cov), "content_size": len(cov), "rows": 0, "first_ms": None, "last_ms": None,
                 "rows_sha256": sha(cov)})
    blobs["coverage/datasets.json"] = cov
    objs.sort(key=lambda o: o["name"])
    lock = {
        "schema": "alphakeel.data-lock/1", "pack_id": "pk-pending", "pack_sha256": "0" * 64, "created_ms": 1_700_000_000_000,
        "request": {"window": {"start_ms": start, "end_ms": end}, "warmup_frames": 0, "venues": [venue],
                    "instruments": [{"venue": venue, "market": market, "symbol": symbol}], "accept_partial": accept_partial,
                    "funding_dataset": None if settlements is not None else "none", "include": {"native": False, "tables": True},
                    "dataset": {"market_dataset": None, "market_tables": tables, "price_kinds": ["trade"]}},
        "windows": {"requested": {"start_ms": start, "end_ms": end}, "actual": {"start_ms": start, "end_ms": end}, "warmup": None},
        "funding": ({"dataset_version": "fund-fixture-v1", "snapshot_sha256": H, "boundary_match": "nearest-within-60s-unique-v1",
                     "visibility": "historical_reconstruction", "assumed_delay_ms": 60_000, "conflict_policy": "strict", "watermark_ms": end}
                    if settlements is not None else None),
        "scans": None, "datasets": datasets, "sources": sources,
        "units": {"price": "p", "rate": "r", "quantity": "q", "time": "t", "money": "m"},
        "universe": {"instruments": 1, "selected": 1, "by_venue": {venue: {"perp": 1 if market == "perp" else 0, "spot": 0 if market == "perp" else 1}}, "digest": H},
        "coverage": {"funding": funding_coverage or {"perps": 1, "complete": 1, "partial": 0, "none": 0}, "quotes": {"frames": 0, "frames_all_venues_failed": 0, "rows": 0},
                     "fx": {"points": 0, "frames_without_rate": 0}, "pnl_backtest_complete": False, "reasons": ["dataset pack"]},
        "objects": objs, "transforms": [{"id": "dataset-to-rows", "code_version": "alphakeel-test", "config_sha256": H, "inputs": [H],
                                         "outputs": [o["sha256"] for o in objs if o["role"] != "coverage"]}],
        "limits": ["dataset pack"],
    }
    digest = packfile.pack_digest(lock)
    lock["pack_sha256"] = digest
    lock["pack_id"] = f"pk-{digest[:24]}"
    return lock, blobs


class FakeClient:
    """A service double: serves one pre-built pack per (klines?, funding?) request shape; can inject errors and corruption."""

    def __init__(self, packs: dict[str, tuple[dict, dict[str, bytes]]], *, create_error: ApiError | None = None, corrupt: str | None = None,
                 swap: dict | None = None, pin: dict | None = None):
        self.packs = packs
        self.pin = contract.local_pin() if pin is None else pin
        self.create_error = create_error
        self.corrupt = corrupt
        self.swap = swap
        self.requests: list[dict] = []
        self.closed = False

    # the real connection check runs against this double's whoami/capabilities
    from alphakeel_research.client import Client as _Client

    check = _Client.check

    def whoami(self):
        return {"credential": "tok-fake", "scopes": ["data"], "holdout_days": 60, "holdout_cutoff_ms": 0, "contract_pin": self.pin}

    def capabilities(self):
        return {"schemas": {"supported_major": contract.MAJOR}, "contract": contract.CONTRACT_ID, "modes": [], "limits": {}}

    def create_pack(self, request, *, key=None):
        if self.create_error:
            raise self.create_error
        self.requests.append(request)
        kind = "kline" if request["dataset"].get("market_tables") else "funding"
        if kind == "kline" and request["funding_dataset"] is None and "perp" in self.packs:
            kind = "perp"  # a perpetual's bars come with the settlements of the same pack
        lock = self.swap if self.swap else self.packs[kind][0]
        return {"job_id": "pj-1", "status": "ready", "pack_id": lock["pack_id"], "kind": kind}

    def wait_pack(self, job_id, **kw):  # pragma: no cover - create_pack answers ready
        raise AssertionError("not needed")

    def _find(self, pack_id):
        for lock, blobs in self.packs.values():
            if lock["pack_id"] == pack_id:
                return lock, blobs
        if self.swap and self.swap["pack_id"] == pack_id:
            return self.swap, {}
        raise AssertionError(pack_id)

    def pack_lock(self, pack_id):
        return copy.deepcopy(self._find(pack_id)[0])

    def object_range(self, pack_id, name, start, end, *, identity=False):
        lock, blobs = self._find(pack_id)
        data = blobs[name]
        if self.corrupt == name:
            data = bytes([data[0] ^ 1]) + data[1:]
        return types.SimpleNamespace(content=data[start:end + 1])

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPHAKEEL_B2_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("ALPHAKEEL_B2_VENUE", raising=False)
    data_audit.reset_provenance()
    yield
    data_audit.reset_provenance()


def loader(client):
    registry._ensure_registered()
    cls = registry.LOADER_REGISTRY["alphakeel_b2"]
    ldr = cls()
    ldr._client_factory = lambda: client
    return ldr


def kline_pack(symbol="BTCUSDT", **kw):
    kw.setdefault("market", "spot")
    return build_pack(symbol=symbol, klines=kline_rows(symbol), **kw)


# --- registration ------------------------------------------------------------------------------------------------------


def test_alphakeel_b2_is_registered_explicit_only_and_never_a_fallback(monkeypatch):
    assert "alphakeel_b2" in registry.VALID_SOURCES
    registry._ensure_registered()
    cls = registry.LOADER_REGISTRY["alphakeel_b2"]
    assert registry.is_no_network_fallback_source("alphakeel_b2")
    for k in ("ALPHAKEEL_RESEARCH_URL", "ALPHAKEEL_RESEARCH_TOKEN", "ALPHAKEEL_RESEARCH_TOKEN_FILE"):
        monkeypatch.delenv(k, raising=False)
    assert cls().is_available() is False  # no credential, and no network is touched to find out
    with pytest.raises(NoAvailableSourceError, match="no research-service credential"):
        cls().fetch(["BTC-USDT"], "2023-01-01", "2023-01-01")
    monkeypatch.setenv("ALPHAKEEL_RESEARCH_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("ALPHAKEEL_RESEARCH_TOKEN", "ak_rs_tok-x.y")
    assert cls().is_available() is True
    from backtest import loader_health

    assert cls.requires_auth is True  # a credentialed source: the public-endpoint canary does not apply
    assert not [e for e in loader_health.coverage_errors() if "alphakeel_b2" in e]


def test_codes_are_parsed_explicitly_never_guessed():
    from backtest.loaders.alphakeel_b2_loader import VENUES, parse_code

    assert parse_code("okx:perp:BTC-USDT-SWAP") == ("okx", "perp", "BTC-USDT-SWAP")
    assert parse_code("BTC-USDT") == ("binance", "spot", "BTCUSDT")
    assert parse_code("eth-usdt-perp", "bybit") == ("bybit", "perp", "ETHUSDT")
    with pytest.raises(ValueError, match="explicit form"):
        parse_code("BTC-USDT", "okx")  # okx symbols are not BASEQUOTE
    with pytest.raises(ValueError, match="unknown venue"):
        parse_code("mtgox:spot:BTCUSD")
    with pytest.raises(ValueError, match="perp or spot"):
        parse_code("binance:swap:BTCUSDT")
    with pytest.raises(ValueError, match="needs a perp"):
        parse_code("binance:spot:BTCUSDT", force_market="perp")
    assert len(VENUES) == 9 and set(VENUES) == {"binance", "okx", "bybit", "bitget", "gate", "aster", "hyperliquid", "lighter", "backpack"}


# --- OHLCV from kline_1m ---------------------------------------------------------------------------------------------


def test_ohlcv_comes_from_verified_klines_resampled_and_tagged_with_dataset_versions():
    lock, blobs = kline_pack()
    fc = FakeClient({"kline": (lock, blobs)})
    ldr = loader(fc)
    out = ldr.fetch(["BTC-USDT"], "2023-01-01", "2023-01-01", interval="1m")
    df = out["BTC-USDT"]
    assert len(df) == 1440 and df.index.name == "trade_date" and str(df.index[0]) == "2023-01-01 00:00:00"
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.iloc[0].tolist() == [100.0, 101.0, 99.0, 100.5, 2.0]
    tag = df.attrs["alphakeel_b2"]
    assert tag["pack_id"] == lock["pack_id"] and tag["pack_sha256"] == lock["pack_sha256"] and tag["verified"] is True
    assert tag["datasets"] == [{"name": "market", "dataset_version": "mkt-fixture-v1", "snapshot_sha256": H, "visibility": "historical_reconstruction"}]
    # the request was a strict dataset pack for exactly this instrument and window; klines only, no funding
    req = fc.requests[0]
    assert req["dataset"] == {"market_tables": ["kline_1m"], "price_kinds": ["trade"]} and req["funding_dataset"] == "none"
    assert req["accept_partial"] is False and req["window"] == {"start_ms": START, "end_ms": START + DAY}
    assert req["instruments"] == [{"venue": "binance", "market": "spot", "symbol": "BTCUSDT"}]
    # resampling: complete buckets only; first/high/low/last/sum
    five = loader(FakeClient({"kline": (lock, blobs)})).fetch(["BTC-USDT"], "2023-01-01", "2023-01-01", interval="5m")["BTC-USDT"]
    assert len(five) == 288
    assert five.iloc[0].tolist() == pytest.approx([100.0, 101.04, 99.0, 100.54, 10.0])
    day = loader(FakeClient({"kline": (lock, blobs)})).fetch(["BTC-USDT"], "2023-01-01", "2023-01-01", interval="1D")["BTC-USDT"]
    assert len(day) == 1 and day.iloc[0]["volume"] == 2.0 * 1440 and day.iloc[0]["open"] == 100.0
    # the run card gets the same provenance
    audit = data_audit.data_audit(["alphakeel_b2"])
    assert audit["auditable"] is True and audit["sources"]["alphakeel_b2"]["reads"][0]["pack_id"] == lock["pack_id"]
    assert fc.closed


def test_a_partial_trailing_bucket_is_dropped_not_extrapolated():
    from backtest.loaders.alphakeel_b2_loader import resample_klines

    idx = pd.to_datetime([START + i * 60_000 for i in range(7)], unit="ms")
    df = pd.DataFrame({"open": range(7), "high": range(7), "low": range(7), "close": range(7), "volume": [1.0] * 7}, index=idx)
    out = resample_klines(df, 5)
    assert len(out) == 1 and out["volume"].iloc[0] == 5.0  # minutes 5 and 6 do not make a bucket


def test_every_failure_is_a_source_error_with_the_service_code_and_no_fallback(tmp_path):
    lock, blobs = kline_pack()
    # hold-out refusal is surfaced unchanged
    err = ApiError("data.holdout", "the window overlaps this credential's 60-day hold-out: end_ms must be at most 123")
    with pytest.raises(NoAvailableSourceError, match=r"data\.holdout.*at most 123"):
        loader(FakeClient({}, create_error=err)).fetch(["BTC-USDT"], "2023-01-01", "2023-01-01")
    # market dataset not configured / gap -> structured error, never a different source
    for code in ("data.insufficient", "data.coverage_partial"):
        with pytest.raises(NoAvailableSourceError, match=code.replace(".", r"\.")):
            loader(FakeClient({}, create_error=ApiError(code, "x"))).fetch(["BTC-USDT"], "2023-01-01", "2023-01-01")
    # a corrupted object fails its digest on read
    with pytest.raises(NoAvailableSourceError, match="evidence"):
        loader(FakeClient({"kline": (lock, blobs)}, corrupt="market/kline_1m/binance/spot/part-00000.jsonl.zst")).fetch(["BTC-USDT"], "2023-01-01", "2023-01-01")
    # a pack for another window/instrument is refused
    other, _ = build_pack(symbol="ETHUSDT", market="spot", klines=kline_rows("ETHUSDT"))
    with pytest.raises(NoAvailableSourceError, match="does not match the request"):
        loader(FakeClient({"kline": (lock, blobs)}, swap=other)).fetch(["BTC-USDT"], "2023-01-01", "2023-01-01")
    # a tampered lock is refused
    bad = copy.deepcopy(lock)
    bad["limits"] = ["tampered"]
    with pytest.raises(NoAvailableSourceError, match="evidence.hash_mismatch"):
        loader(FakeClient({"kline": (bad, blobs)})).fetch(["BTC-USDT"], "2023-01-01", "2023-01-01")
    with pytest.raises(NoAvailableSourceError, match="not supported"):
        loader(FakeClient({"kline": (lock, blobs)})).fetch(["BTC-USDT"], "2023-01-01", "2023-01-01", interval="1W")


# --- funding history ---------------------------------------------------------------------------------------------------


def test_funding_history_keeps_exact_decimals_and_the_funding_dataset_version():
    sym = "BTC-USDT-SWAP"
    lock, blobs = build_pack(venue="okx", symbol=sym, settlements=settlement_rows(f"okx:linear:USDT:{sym}"))
    fc = FakeClient({"funding": (lock, blobs)})
    out = loader(fc).fetch_funding(["okx:perp:BTC-USDT-SWAP"], "2023-01-01", "2023-01-01")
    df = out["okx:perp:BTC-USDT-SWAP"]
    assert df["rate"].tolist() == [Decimal("0.0001"), Decimal("-0.00005"), Decimal("0.0001")]
    assert set(df["dataset_version"]) == {"fund-fixture-v1"} and df.index.name == "settled_at"
    assert df.attrs["alphakeel_b2"]["datasets"][0]["name"] == "funding"
    req = fc.requests[0]
    assert req["dataset"] == {} and req["funding_dataset"] is None and req["venues"] == ["okx"]
    with pytest.raises(NoAvailableSourceError, match="needs a perp"):
        loader(fc).fetch_funding(["binance:spot:BTCUSDT"], "2023-01-01", "2023-01-01")


def test_every_venue_of_the_funding_dataset_is_accepted_by_the_loader():
    from backtest.loaders.alphakeel_b2_loader import VENUES, parse_code

    for v in VENUES:
        assert parse_code(f"{v}:perp:X", "binance")[0] == v


# --- the pack file reader: dataset packs, point in time --------------------------------------------------------------


def test_dataset_pack_validates_against_the_contract_and_the_digest_ignores_watermarks():
    lock, _ = kline_pack()
    contract.validate_doc(lock)
    packfile.verify_lock(lock)
    moved = copy.deepcopy(lock)
    moved["datasets"][0]["watermark_ms"] += 12345
    moved["sources"][0]["watermark_ms"] += 1
    assert packfile.pack_digest(moved) == packfile.pack_digest(lock), "watermarks are observations, not content"
    other = copy.deepcopy(lock)
    other["datasets"][0]["dataset_version"] = "mkt-fixture-v2"
    assert packfile.pack_digest(other) != packfile.pack_digest(lock), "the pinned version is content"
    no_scans = copy.deepcopy(lock)
    del no_scans["scans"]
    with pytest.raises(contract.ContractError):
        contract.validate_doc(no_scans)  # scans must be present (null for dataset packs)


def test_pit_hides_unclosed_bars_and_unreceived_bbo(tmp_path):
    klines = kline_rows("BTCUSDT", minutes=10)
    lock, blobs = build_pack(klines=klines)
    cache = packfile.PackCache(tmp_path / "c")
    fc = FakeClient({"kline": (lock, blobs)})
    pack = packfile.Pack(lock, cache, fc)
    inst = packfile.Inst("binance", "perp", "BTCUSDT")
    assert pack.is_dataset and pack.dataset_version("market") == "mkt-fixture-v1"
    assert len(pack.klines(inst, START, START + 600_000)) == 10
    pit = packfile.Pit(pack, as_of_ms=START + 3 * 60_000 + 59_998)  # bar 3 closes at ...59_999: not yet closed
    seen = pit.klines(inst, START)
    assert [k.t for k in seen] == [START, START + 60_000, START + 120_000]
    with pytest.raises(ApiError, match="after as_of"):
        pit.klines(inst, START, START + 10 * 60_000)
    assert pack.accesses()[0]["table"] == "kline_1m" and pack.accesses()[0]["rows"] >= 3
    assert pack.coverage()["schema"] == "alphakeel.coverage/1"


def test_client_builds_dataset_requests_and_reports_the_holdout():
    from alphakeel_research.client import Client

    req = Client.dataset_pack_request(start_ms=1, end_ms=2, instruments=[{"venue": "binance", "market": "perp", "symbol": "BTCUSDT"}],
                                      funding_dataset="none", market_tables=["kline_1m"], price_kinds=["trade", "mark"])
    assert req["dataset"] == {"market_tables": ["kline_1m"], "price_kinds": ["trade", "mark"]} and req["include_native"] is False
    assert req["funding_dataset"] == "none" and req["warmup_frames"] == 0


# --- run card: ccxt is marked non-auditable ------------------------------------------------------------------------------


def test_run_card_marks_ccxt_non_auditable_and_b2_auditable(tmp_path):
    from backtest.run_card import write_run_card

    card = write_run_card(tmp_path / "r1", {"source": "ccxt", "codes": ["BTC-USDT"]}, {"sharpe": 1.0}, data_sources=["ccxt"])
    assert card["data_audit"]["auditable"] is False and card["data_audit"]["non_auditable_sources"] == ["ccxt"]
    assert any("non-auditable" in w for w in card["warnings"])
    md = (tmp_path / "r1" / "run_card.md").read_text(encoding="utf-8")
    assert "NOT AUDITABLE" in md and "ccxt" in md
    data_audit.record_provenance("alphakeel_b2", {"pack_id": "pk-1", "datasets": [{"name": "market", "dataset_version": "mkt-v1"}]})
    card2 = write_run_card(tmp_path / "r2", {"source": "alphakeel_b2"}, {"sharpe": 1.0}, data_sources=["alphakeel_b2"])
    assert card2["data_audit"]["auditable"] is True and not any("non-auditable" in w for w in card2["warnings"])
    assert "market=mkt-v1" in (tmp_path / "r2" / "run_card.md").read_text(encoding="utf-8")
    # mixed: one non-auditable source makes the run non-auditable
    card3 = write_run_card(tmp_path / "r3", {"source": "auto"}, {"sharpe": 1.0}, data_sources=["alphakeel_b2", "ccxt"])
    assert card3["data_audit"]["auditable"] is False


# --- sandbox network isolation ---------------------------------------------------------------------------------------------

_PROBE_POLICY = """
import errno, json, socket
def initialize(parameters):
    return {}
def on_step(ctx, state):
    names = [ln.split(":")[0].strip() for ln in open("/proc/net/dev").read().splitlines()[2:] if ":" in ln]
    s = socket.socket(); s.settimeout(0.5)
    try:
        s.connect(("192.0.2.1", 9)); code = "connected"
    except OSError as e:
        code = errno.errorcode.get(e.errno, str(e.errno))
    return {"intents": [], "state": {"ifaces": sorted(names), "connect": code}}
"""


def _run_probe_policy(tmp_path, **kw):
    script = tmp_path / "p.py"
    script.write_text(textwrap.dedent(_PROBE_POLICY), encoding="utf-8")
    host = policy_mod.PolicyHost(script, call_timeout=20, **kw)
    host.start({})
    try:
        host._call({"op": "step", "context": {"time_ms": 1}})
        sandbox = dict(host.sandbox)
    finally:
        host.close()
    return sandbox


def test_sandbox_reports_network_isolation_truthfully(tmp_path):
    """Whatever this host can do, the report must match what a launch under the chosen prefix really gives."""
    policy_mod._NETNS_CACHE.clear()
    sandbox = _run_probe_policy(tmp_path)
    assert isinstance(sandbox["network_blocked"], bool) and sandbox["network_isolation"]
    prefix, _, report = policy_mod.network_isolation_launcher(policy_mod._sandbox_credentials())
    assert report["network_blocked"] == sandbox["network_blocked"] and bool(prefix) == sandbox["network_blocked"]
    if sandbox["network_blocked"]:
        out = subprocess.run([*prefix, sys.executable, "-c", policy_mod._NETNS_PROBE], capture_output=True)
        assert out.returncode == 0, "claimed isolation must survive an independent run of the probe"
    else:
        assert "not available" in sandbox["network_isolation"] or "not installed" in sandbox["network_isolation"]


def test_a_fake_unshare_cannot_make_the_sandbox_claim_isolation(tmp_path, monkeypatch):
    """A launcher that does nothing (here: a stub `unshare` that just execs the command) fails the probe, so no claim is made."""
    stub = tmp_path / "bin" / "unshare"
    stub.parent.mkdir()
    stub.write_text('#!/bin/sh\nwhile [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"\n', encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub.parent}:{os.environ['PATH']}")
    policy_mod._NETNS_CACHE.clear()
    prefix, drops, report = policy_mod.network_isolation_launcher(None)
    assert prefix == [] and report["network_blocked"] is False
    assert "not available" in report["network_isolation"]
    sb = _run_probe_policy(tmp_path)
    assert sb["network_blocked"] is False
    policy_mod._NETNS_CACHE.clear()


def test_missing_unshare_is_reported_and_require_flag_refuses_to_start(tmp_path, monkeypatch):
    policy_mod._NETNS_CACHE.clear()
    monkeypatch.setattr(policy_mod.shutil, "which", lambda name: None)
    prefix, _, report = policy_mod.network_isolation_launcher(None)
    assert prefix == [] and report == {"network_blocked": False, "network_isolation": "unshare is not installed"}
    assert _run_probe_policy(tmp_path)["network_blocked"] is False
    script = tmp_path / "p.py"
    script.write_text(textwrap.dedent(_PROBE_POLICY), encoding="utf-8")
    with pytest.raises(ApiError, match="network isolation is required"):
        policy_mod.PolicyHost(script, require_network_isolation=True).start({})
    monkeypatch.setenv("ALPHAKEEL_REQUIRE_NETNS", "1")
    with pytest.raises(ApiError, match="network isolation is required"):
        policy_mod.PolicyHost(script).start({})
    policy_mod._NETNS_CACHE.clear()


def test_when_the_host_supports_isolation_the_strategy_really_has_no_network(tmp_path):
    policy_mod._NETNS_CACHE.clear()
    prefix, drops, report = policy_mod.network_isolation_launcher(policy_mod._sandbox_credentials())
    if not prefix:
        pytest.skip("this host cannot create a network namespace: " + report["network_isolation"])
    r = subprocess.run([*prefix, sys.executable, "-c", policy_mod._NETNS_PROBE], capture_output=True)
    assert r.returncode == 0
    sb = _run_probe_policy(tmp_path)
    assert sb["network_blocked"] is True and "verified" in sb["network_isolation"]


def test_direct_okx_and_binance_loaders_are_non_auditable_like_ccxt():
    for src in ("okx", "binance"):
        a = data_audit.data_audit([src])
        assert a["auditable"] is False and a["non_auditable_sources"] == [src]
        assert "re-read bit for bit" in a["sources"][src]["reason"]
        assert data_audit.non_auditable_warning(a).startswith("non-auditable data: " + src)
    mixed = data_audit.data_audit(["alphakeel_b2", "okx"])
    assert mixed["auditable"] is False and mixed["non_auditable_sources"] == ["okx"]
    assert data_audit.data_audit(["alphakeel_b2"])["auditable"] is True


# --- R5 (2026-10-06 review): contract PIN at runtime, perp funding, strict locks, point-in-time view ---------------------


def test_the_loader_refuses_a_service_built_from_another_contract():
    """B-P1-3: the PIN used to be checked only by this test suite; now every client run compares it with the service."""
    lock, blobs = kline_pack()
    other = dict(contract.local_pin())
    other["openapi.json"] = "0" * 64
    with pytest.raises(NoAvailableSourceError, match=r"contract\.pin_mismatch.*openapi\.json"):
        loader(FakeClient({"kline": (lock, blobs)}, pin=other)).fetch(["BTC-USDT"], "2023-01-01", "2023-01-01")

    class Old(FakeClient):  # a service that predates the runtime check reports no digest
        def whoami(self):
            w = super().whoami()
            del w["contract_pin"]
            return w

    with pytest.raises(NoAvailableSourceError, match=r"contract\.pin_mismatch"):
        loader(Old({"kline": (lock, blobs)})).fetch(["BTC-USDT"], "2023-01-01", "2023-01-01")
    # the matching service passes
    assert len(loader(FakeClient({"kline": (lock, blobs)})).fetch(["BTC-USDT"], "2023-01-01", "2023-01-01")["BTC-USDT"]) == 1


def test_perpetual_bars_carry_the_settlements_of_the_same_pack_at_the_real_settlement_times():
    """B-P1-1 (P11): alphakeel_b2 bars had no funding_rate column, so CryptoEngine charged a fixed 0.0001 and a cross-venue
    hedge's legs cancelled. Settlements at 00:00, 08:00, 16:00 attach to the first bar opening at or after them."""
    inst_id = "binance:linear:USDT:BTCUSDT"
    lock, blobs = build_pack(klines=kline_rows("BTCUSDT"), settlements=settlement_rows(inst_id))
    fc = FakeClient({"perp": (lock, blobs)})
    df = loader(fc).fetch(["BTC-USDT-PERP"], "2023-01-01", "2023-01-01", interval="4H")["BTC-USDT-PERP"]
    assert fc.requests[0]["funding_dataset"] is None and fc.requests[0]["dataset"]["market_tables"] == ["kline_1m"]
    assert len(df) == 6
    assert df["funding_settlements"].tolist() == [1, 0, 1, 0, 1, 0]
    assert df["funding_rate"].tolist() == pytest.approx([0.0001, 0.0, -0.00005, 0.0, 0.0001, 0.0])
    assert df["funding_settlement_time"].iloc[2] == pd.Timestamp(START + 8 * 3_600_000, unit="ms")
    assert pd.isna(df["funding_settlement_time"].iloc[1])
    tag = df.attrs["alphakeel_b2"]
    assert tag["funding"]["source"] == "official_settlements" and tag["funding"]["settlements"] == 3
    assert tag["bar"] == {"index_label": "bar_open_time", "close_rule": "last 1-minute close of the bar", "complete_bars_only": True, "minutes": 240}
    # one 1D bar: the three settlements of the day land on the only bar open at or after them → only 00:00 here
    day = loader(FakeClient({"perp": (lock, blobs)})).fetch(["BTC-USDT-PERP"], "2023-01-01", "2023-01-01", interval="1D")["BTC-USDT-PERP"]
    assert day["funding_settlements"].tolist() == [1]


def test_the_engine_charges_the_loader_settlements_once_each_for_a_cross_venue_hedge():
    from backtest.engines.crypto import CryptoEngine
    from backtest.models import Position

    inst_id = "binance:linear:USDT:BTCUSDT"
    lock, blobs = build_pack(klines=kline_rows("BTCUSDT"), settlements=settlement_rows(inst_id))
    df = loader(FakeClient({"perp": (lock, blobs)})).fetch(["BTC-USDT-PERP"], "2023-01-01", "2023-01-01", interval="1H")["BTC-USDT-PERP"]
    eng = CryptoEngine({"initial_cash": 100_000, "leverage": 1.0, "interval": "1H"})
    eng.positions["BTC-USDT-PERP"] = Position("BTC-USDT-PERP", 1, 100.0, pd.Timestamp("2022-12-31"), 10.0, leverage=1.0)
    eng._funding.prepare({"BTC-USDT-PERP": df}, ["BTC-USDT-PERP"])
    before = eng.capital
    for ts in df.index:
        eng.before_rebalance_bar(ts, {"BTC-USDT-PERP": df}, ["BTC-USDT-PERP"])
    # 3 settlements (not 24 × "daily fallback" + slots): 10 × open × rate at the bars opening at 00:00, 08:00, 16:00
    opens = df["open"]
    want = 10 * (opens.iloc[0] * 0.0001 + opens.iloc[8] * -0.00005 + opens.iloc[16] * 0.0001)
    assert before - eng.capital == pytest.approx(want)
    assert eng._funding.settlements == 3


def test_a_lock_with_accept_partial_or_incomplete_funding_coverage_is_not_used():
    """P8: _check_lock checked window and instruments but not the coverage policy the pack was built with."""
    lock, blobs = build_pack(market="spot", klines=kline_rows("BTCUSDT"), accept_partial=True)
    with pytest.raises(NoAvailableSourceError, match="accept_partial"):
        loader(FakeClient({"kline": (lock, blobs)})).fetch(["BTC-USDT"], "2023-01-01", "2023-01-01")
    inst_id = "binance:linear:USDT:BTCUSDT"
    lock, blobs = build_pack(klines=kline_rows("BTCUSDT"), settlements=settlement_rows(inst_id),
                             funding_coverage={"perps": 1, "complete": 0, "partial": 1, "none": 0})
    with pytest.raises(NoAvailableSourceError, match="funding coverage is incomplete"):
        loader(FakeClient({"perp": (lock, blobs)})).fetch(["BTC-USDT-PERP"], "2023-01-01", "2023-01-01")


def test_a_kline_whose_close_time_is_not_t_plus_59999_is_an_evidence_error(tmp_path):
    """P4b: the close time was trusted; an early close_time_ms would expose the bar's close before the minute ended."""
    rows = kline_rows("BTCUSDT")
    rows[5][2] = rows[5][0] + 30_000
    lock, blobs = build_pack(market="spot", klines=rows)
    with pytest.raises(NoAvailableSourceError, match="close_time_ms"):
        loader(FakeClient({"kline": (lock, blobs)})).fetch(["BTC-USDT"], "2023-01-01", "2023-01-01")
    pack = packfile.Pack(lock, packfile.PackCache(tmp_path / "c"), FakeClient({"kline": (lock, blobs)}))
    with pytest.raises(ApiError, match="close_time_ms"):
        packfile.Pit(pack, START + DAY - 1).klines(packfile.Inst("binance", "spot", "BTCUSDT"), START)


class _StubPack:
    """Just enough of ``Pack`` for the point-in-time rules (rows are given directly)."""

    def __init__(self, funding, quotes=(), settlements=(), meta=None):
        self.lock = {"funding": funding}
        self._q, self._s, self._m = list(quotes), list(settlements), meta
        self.id = "pk-stub"

    def quotes(self, inst, start, end):
        return [q for q in self._q if start <= q.t < end]

    def settlements(self, inst, start, end):
        return [x for x in self._s if start <= x.t < end]

    def instrument_meta(self, inst):
        return None if self._m is None else dict(self._m)

    def instrument_meta_history(self, inst):
        return [] if self._m is None else [dict(self._m)]


def _settlement(t, available_at=None):
    return packfile.Settlement(t, "i", Decimal("0.0001"), "regular", 3600, "official", "USDT", "linear", "x", available_at, 0, None, H, True)


FUND = {"dataset_version": "v", "assumed_delay_ms": 60_000}
I = packfile.Inst("binance", "perp", "BTCUSDT")


def test_pit_quotes_respect_the_exchange_timestamp_and_settlements_the_conservative_delay():
    """P6 / P2 / P2c: no 5 s future tolerance for decisions; a settlement without available_at is visible only after the
    lock's delay (>= 60 s); a lock without a funding block or with delay 0 is refused instead of meaning "delay 0"."""
    q = [packfile.Quote(START, Decimal(1), Decimal(1), True, None, None, START, 0),
         packfile.Quote(START + 60_000, Decimal(1), Decimal(1), True, None, None, START + 63_000, 1),
         packfile.Quote(START + 120_000, Decimal(1), Decimal(1), True, None, None, None, 2)]
    pit = packfile.Pit(_StubPack(FUND, quotes=q), START + 120_000)
    assert [x.frame for x in pit.quotes(I, START)] == [0, 1, 2]
    assert [x.frame for x in packfile.Pit(_StubPack(FUND, quotes=q), START + 62_999).quotes(I, START)] == [0]
    s = [_settlement(START), _settlement(START + 3_600_000, available_at=START + 3_605_000)]
    at = lambda t: [x.t for x in packfile.Pit(_StubPack(FUND, settlements=s), t).settlements(I, START - 1)]
    assert at(START + 59_999) == [] and at(START + 60_000) == [START]
    assert at(START + 3_604_999) == [START] and at(START + 3_605_000) == [START, START + 3_600_000]
    with pytest.raises(ApiError, match="below the minimum"):
        packfile.Pit(_StubPack({"dataset_version": "v", "assumed_delay_ms": 0}), START)
    with pytest.raises(ApiError, match="funding_missing|no funding"):
        packfile.Pit(_StubPack(None, settlements=s), START + DAY).settlements(I, START)


def test_pit_instrument_meta_hides_future_listing_and_delisting():
    meta = {"t": START, "symbol": "BTCUSDT", "listed_ms": START + 1000, "delisted_ms": START + 5000, "extra": {}}
    assert packfile.Pit(_StubPack(FUND, meta=meta), START + 999).instrument_meta(I) is None
    mid = packfile.Pit(_StubPack(FUND, meta=meta), START + 1000).instrument_meta(I)
    assert mid["listed_ms"] == START + 1000 and mid["delisted_ms"] is None
    assert packfile.Pit(_StubPack(FUND, meta=meta), START + 5000).instrument_meta(I)["delisted_ms"] == START + 5000


def _market_pack(objects: dict[str, list[list]]) -> packfile.Pack:
    """A dataset pack with the given market objects ({name: rows}) on top of a kline pack, opened from memory."""
    lock, blobs = kline_pack(market="perp")
    lock = copy.deepcopy(lock)
    for name, rows in objects.items():
        o, b = _obj(name, "market", "alphakeel.rows.market-x/1", {"schema": "alphakeel.rows.market-x/1"}, rows)
        lock["objects"].append(o)
        blobs[name] = b
    lock["objects"].sort(key=lambda o: o["name"])
    lock["transforms"][0]["outputs"] = [o["sha256"] for o in lock["objects"] if o["role"] != "coverage"]
    digest = packfile.pack_digest(lock)
    lock["pack_sha256"], lock["pack_id"] = digest, f"pk-{digest[:24]}"
    return packfile.Pack(lock, packfile.PackCache(Path(os.environ.get("PYTEST_TMP", "/tmp")) / f"c-{digest[:8]}"), FakeClient({"x": (lock, blobs)}))


def _bbo_row(t, symbol, px):
    return [t, symbol, t + 5, px, "1", px, "1", t]


def test_spot_and_perp_rows_of_the_same_symbol_are_never_mixed(tmp_path, monkeypatch):
    """Binance spot BTCUSDT and perp BTCUSDT share a symbol; rows carry only the symbol, so the market is in the object path."""
    monkeypatch.setenv("PYTEST_TMP", str(tmp_path))
    pack = _market_pack({"market/bbo/binance/perp/part-00000.jsonl.zst": [_bbo_row(START + 2_000, "BTCUSDT", "100")],
                         "market/bbo/binance/spot/part-00000.jsonl.zst": [_bbo_row(START + 1_000, "BTCUSDT", "99"),
                                                                          _bbo_row(START + 3_000, "BTCUSDT", "99.5")]})
    perp = [b.bid for b in pack.bbo(packfile.Inst("binance", "perp", "BTCUSDT"), START, START + DAY)]
    spot = [b.bid for b in pack.bbo(packfile.Inst("binance", "spot", "BTCUSDT"), START, START + DAY)]
    assert perp == [Decimal("100")] and spot == [Decimal("99"), Decimal("99.5")]
    assert {a["instrument"]["market"]: a["rows"] for a in pack.accesses() if a["table"] == "bbo"} == {"perp": 1, "spot": 2}


def test_a_pack_with_spot_and_perp_rows_in_one_object_is_refused(tmp_path, monkeypatch):
    """The layout before market-separated objects cannot tell the two markets apart: an evidence error, not a guess."""
    monkeypatch.setenv("PYTEST_TMP", str(tmp_path))
    pack = _market_pack({"market/bbo/binance/part-00000.jsonl.zst": [_bbo_row(START + 1_000, "BTCUSDT", "99")]})
    with pytest.raises(packfile.EvidenceError, match="rebuild the pack"):
        pack.bbo(packfile.Inst("binance", "perp", "BTCUSDT"), START, START + DAY)


def _meta_row(t, tick, listed=None, delisted=None):
    return [t, "BTCUSDT", tick, "0.001", "0.001", None, listed, delisted, {"contract_value": "1"}]


def test_pit_instrument_meta_is_the_last_snapshot_known_at_as_of(tmp_path, monkeypatch):
    """A later spec change (tick/lot/contract_value/status) must not leak back to an earlier as_of."""
    monkeypatch.setenv("PYTEST_TMP", str(tmp_path))
    pack = _market_pack({"market/instrument_meta/binance/perp/part-00000.jsonl.zst":
                         [_meta_row(START - DAY, "0.1"), _meta_row(START + DAY, "0.01")]})
    inst = packfile.Inst("binance", "perp", "BTCUSDT")
    assert pack.instrument_meta(inst)["tick_size"] == "0.01"  # the unfiltered pack: latest snapshot of the window
    assert [m["tick_size"] for m in pack.instrument_meta_history(inst)] == ["0.1", "0.01"]
    assert packfile.Pit(pack, START).instrument_meta(inst)["tick_size"] == "0.1"
    assert packfile.Pit(pack, START + DAY - 1).instrument_meta(inst)["tick_size"] == "0.1"
    assert packfile.Pit(pack, START + DAY).instrument_meta(inst)["tick_size"] == "0.01"
    assert packfile.Pit(pack, START - DAY - 1).instrument_meta(inst) is None  # no snapshot known yet


def test_a_lock_without_sources_is_an_evidence_error_not_a_type_error():
    lock, _ = kline_pack()
    del lock["sources"]
    with pytest.raises(packfile.EvidenceError, match="sources"):
        packfile.pack_digest(lock)


def test_a_policy_strategy_gets_only_the_point_in_time_view():
    """B-P2-9: ctx.pack handed the unfiltered pack to the strategy next to ctx.pit."""
    import inspect

    from alphakeel_research import policy_host

    src = inspect.getsource(policy_host)
    assert "ctx.pack" not in src and "ctx.pit = Pit(" in src
    pit = packfile.Pit(_StubPack(FUND), START)
    assert not hasattr(pit, "pack") and pit.pack_id == "pk-stub"
