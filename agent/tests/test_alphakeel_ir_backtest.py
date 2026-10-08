"""Strategy IR research side: README §5a alignment of ir_policy, the dataset view, the long-history IR backtest and the
``ir export`` document AlphaKeel's registry imports.

Offline: dataset packs are built in this file in the service's object formats (header line + JSON rows, zstd,
content-addressed lock that validates against the pinned contract), opened from an exported directory or through a
service double. Gold numbers are hand-calculated in the test that uses them.
"""

from __future__ import annotations

import copy
import hashlib
import json
import types
from decimal import Decimal
from pathlib import Path

import pytest
import zstandard

from alphakeel_research import cli, contract, ir, ir_backtest, ir_views, packfile, workflow
from alphakeel_research.errors import ApiError
from alphakeel_research.packfile import Inst, Pack, Pit
from tests.test_alphakeel_ir import T, _carry_doc, _ctx, _fake_pit, _policy

H = "ab" * 32
DAY = 86_400_000
MIN = 60_000
START = 1_672_531_200_000  # 2023-01-01T00:00:00Z: first decision instant
END = START + 2 * 3_600_000
LO = START - DAY  # pack window start (warm-up day)
BN = {"venue": "binance", "market": "perp", "symbol": "BTCUSDT", "base": "BTC", "quote": "USDT"}
OK = {"venue": "okx", "market": "perp", "symbol": "BTC-USDT-SWAP", "base": "BTC", "quote": "USDT"}
BS = {"venue": "binance", "market": "spot", "symbol": "BTCUSDT-SPOT", "base": "BTC", "quote": "USDT"}


# --- a dataset pack in the service's formats -------------------------------------------------------------------------------

def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _obj(name, role, schema_id, header, rows):
    content = b"".join([contract.canonical_bytes(header) + b"\n"] + [contract.canonical_bytes(r) + b"\n" for r in rows])
    stored = zstandard.ZstdCompressor(level=3).compress(content)
    chain = hashlib.sha256()
    for r in rows:
        chain.update(packfile.row_digest(r))
    ts = [r[0] for r in rows if isinstance(r[0], int)]
    return ({"name": name, "role": role, "schema_id": schema_id, "codec": "jsonl+zstd", "sha256": _sha(stored), "size": len(stored),
             "content_sha256": _sha(content), "content_size": len(content), "rows": len(rows),
             "first_ms": min(ts) if ts else None, "last_ms": max(ts) if ts else None, "rows_sha256": chain.hexdigest()}, stored, content)


def bars(symbol, price_at, lo=LO, hi=END, vq="1000"):
    """1m trade bars [lo, hi): close = price_at(t), volume_quote constant."""
    out = []
    for t in range(lo, hi, MIN):
        p = price_at(t)
        out.append([t, symbol, t + 59_999, p, p, p, p, "10", vq, 5, "trade"])
    return out


def settle(inst_id, t, rate, hours=8, mark=None, available_at=None):
    return [t, inst_id, rate, "regular", hours * 3600, "official", "USDT", "linear", "reconstructed_assumption", available_at,
            t + 120_000, mark, H, True]


def build(instruments, klines: dict, settlements: dict, *, lo=LO, hi=END):
    """``klines``: {(venue, symbol): rows}; ``settlements``: {venue: rows}. Instruments rows leave base/quote null (as the
    service's dataset packs do)."""
    objs, blobs, contents = [], {}, {}

    def add(o):
        objs.append(o[0])
        blobs[o[0]["name"]] = o[1]
        contents[o[0]["content_sha256"]] = o[2]

    venues = sorted({d["venue"] for d in instruments})
    market_of = {(d["venue"], d["symbol"]): d["market"] for d in instruments}
    for v in venues:
        for m in ("perp", "spot"):  # the service writes one object per (table, venue, market)
            rows = [r for (vv, s), rs in sorted(klines.items()) if vv == v and market_of.get((vv, s), "perp") == m for r in rs]
            if rows:
                add(_obj(f"market/kline_1m/{v}/{m}/part-00000.jsonl.zst", "market", "alphakeel.rows.market-kline-1m/1",
                         {"schema": "alphakeel.rows.market-kline-1m/1", "table": "kline_1m", "columns": ["t", "symbol", "close_time_ms"]}, rows))
        if settlements.get(v):
            add(_obj(f"settlements/{v}/part-00000.jsonl.zst", "settlements", "alphakeel.rows.settlements/1",
                     {"schema": "alphakeel.rows.settlements/1", "venue": v}, settlements[v]))
    irows = [[d["venue"], d["market"], d["symbol"], None, None, None, None,
              f"{d['venue']}:linear:USDT:{d['symbol']}" if d["market"] == "perp" else None,
              "complete" if d["market"] == "perp" else "not_applicable", None, None, "not_recorded_in_dataset", None, None, True] for d in instruments]
    add(_obj("instruments/instruments.jsonl.zst", "instruments", "alphakeel.rows.instruments/1",
             {"schema": "alphakeel.rows.instruments/1", "columns": ["venue"]}, irows))
    cov = contract.canonical_bytes({"schema": "alphakeel.coverage/1", "funding": [], "market": []})
    objs.append({"name": "coverage/datasets.json", "role": "coverage", "schema_id": "alphakeel.coverage/1", "codec": "json", "sha256": _sha(cov),
                 "size": len(cov), "content_sha256": _sha(cov), "content_size": len(cov), "rows": 0, "first_ms": None, "last_ms": None,
                 "rows_sha256": _sha(cov)})
    blobs["coverage/datasets.json"] = cov
    contents[_sha(cov)] = cov
    objs.sort(key=lambda o: o["name"])
    refs = [{"venue": d["venue"], "market": d["market"], "symbol": d["symbol"]} for d in instruments]
    perps = sum(d["market"] == "perp" for d in instruments)
    datasets = [{"name": "market", "dataset_version": "mkt-fixture-v1", "snapshot_sha256": H, "schema_version": "1",
                 "visibility": "historical_reconstruction", "tables": ["kline_1m"], "venues": venues, "watermark_ms": hi},
                {"name": "funding", "dataset_version": "fund-fixture-v1", "snapshot_sha256": H, "schema_version": "1",
                 "visibility": "historical_reconstruction", "tables": ["settlements"], "venues": venues, "watermark_ms": hi}]
    sources = [{"role": "market", "identity": f"market-dataset:mkt-fixture-v1/venue={v}/table=kline_1m", "watermark_ms": hi,
                "visibility": "historical_reconstruction"} for v in venues]
    sources += [{"role": "funding_settlement", "identity": f"funding-dataset:fund-fixture-v1/venue={v}", "watermark_ms": hi,
                 "visibility": "historical_reconstruction"} for v in venues]
    lock = {
        "schema": "alphakeel.data-lock/1", "pack_id": "pk-pending", "pack_sha256": "0" * 64, "created_ms": 1_700_000_000_000,
        "request": {"window": {"start_ms": lo, "end_ms": hi}, "warmup_frames": 0, "venues": venues, "instruments": refs, "accept_partial": False,
                    "funding_dataset": None, "include": {"native": False, "tables": True},
                    "dataset": {"market_dataset": None, "market_tables": ["kline_1m"], "price_kinds": ["trade"]}},
        "windows": {"requested": {"start_ms": lo, "end_ms": hi}, "actual": {"start_ms": lo, "end_ms": hi}, "warmup": None},
        "funding": {"dataset_version": "fund-fixture-v1", "snapshot_sha256": H, "boundary_match": "nearest-within-60s-unique-v1",
                    "visibility": "historical_reconstruction", "assumed_delay_ms": 60_000, "conflict_policy": "strict", "watermark_ms": hi},
        "scans": None, "datasets": datasets, "sources": sources,
        "units": {"price": "p", "rate": "r", "quantity": "q", "time": "t", "money": "m"},
        "universe": {"instruments": len(refs), "selected": len(refs),
                     "by_venue": {v: {"perp": sum(1 for d in instruments if d["venue"] == v and d["market"] == "perp"),
                                      "spot": sum(1 for d in instruments if d["venue"] == v and d["market"] == "spot")} for v in venues}, "digest": H},
        "coverage": {"funding": {"perps": perps, "complete": perps, "partial": 0, "none": 0}, "quotes": {"frames": 0, "frames_all_venues_failed": 0, "rows": 0},
                     "fx": {"points": 0, "frames_without_rate": 0}, "pnl_backtest_complete": False, "reasons": ["dataset pack"]},
        "objects": objs, "transforms": [{"id": "dataset-to-rows", "code_version": "alphakeel-test", "config_sha256": H, "inputs": [H],
                                         "outputs": [o["sha256"] for o in objs if o["role"] != "coverage"]}],
        "limits": ["dataset pack"],
    }
    digest = packfile.pack_digest(lock)
    lock["pack_sha256"], lock["pack_id"] = digest, f"pk-{digest[:24]}"
    return lock, blobs, contents


def export_dir(tmp: Path, built) -> Path:
    lock, _blobs, contents = built
    d = tmp / "pack"
    (d / "content").mkdir(parents=True, exist_ok=True)
    for sha, c in contents.items():
        (d / "content" / sha).write_bytes(c)
    (d / "lock.json").write_text(json.dumps(lock))
    return d


# the golden market: Binance 100 then 101 from START+40m; OKX 100.2 then 100.5 from START+40m
def p_bn(t):
    return "101" if t >= START + 40 * MIN else "100"


def p_ok(t):
    return "100.5" if t >= START + 40 * MIN else "100.2"


def golden_built(**kw):
    kl = {("binance", "BTCUSDT"): bars("BTCUSDT", p_bn), ("okx", "BTC-USDT-SWAP"): bars("BTC-USDT-SWAP", p_ok)}
    st = {"binance": [settle("binance:linear:USDT:BTCUSDT", START - 8 * 3_600_000, "-0.0001"),
                      settle("binance:linear:USDT:BTCUSDT", START + 30 * MIN, "0.0001")],
          "okx": [settle("okx:linear:USDT:BTC-USDT-SWAP", START - 8 * 3_600_000, "0.0003"),
                  settle("okx:linear:USDT:BTC-USDT-SWAP", START + 30 * MIN, "0.0002", mark="999")]}
    return build([BN, OK], kl, st, **kw)


def golden_ir() -> dict:
    doc = _carry_doc(max_hold_hours=1, max_loss="500", take_profit=None, conditions=[])
    doc["entry"]["cooldown_minutes"] = 600
    doc["sizing"]["notional_per_leg"] = "1000"
    doc["sizing"]["max_open"] = 1
    doc.pop("strategy_id", None)  # provenance (required) stays: the input's placeholder; export replaces it
    assert ir.load(doc)
    return doc


# --- (1) README §5a alignment of ir_policy --------------------------------------------------------------------------------

def test_entry_qty_is_half_even_to_eight_places():
    assert ir_views.QTY_DP == 8
    assert ir_views.entry_qty("1000", "3") == Decimal("333.33333333")
    assert ir_views.entry_qty("1000", "100.01") == Decimal("9.99900010")  # rounding down to 6 would be 9.999000
    assert ir_views.entry_qty("0.000000025", "1") == Decimal("0.00000002")  # tie -> even
    assert ir_views.entry_qty("0.000000035", "1") == Decimal("0.00000004")
    assert ir_views.entry_qty("0.000000004", "1") == 0
    pol = _policy()
    st = pol.initialize({"ir": _carry_doc()})
    assert st["qty_dp"] == 8
    ints = pol.on_step(_ctx(T, [], _fake_pit()), st)["intents"]
    assert [(i["intent_id"], i["qty"]) for i in ints] == [("n1-L-open", "9.9990001"), ("n1-S-open", "9.9990001")]


def _frame_at(t, *, okx_bbo=True, okx_missing=False):
    from alphakeel_research.packfile import Observation, Quote

    pit = _fake_pit()
    pit.as_of_ms = t
    ok = Inst("okx", "perp", "BTC-USDT-SWAP")
    for inst, rows in pit.rows.items():
        rows[:] = [Quote(q.t + (t - T), q.bid, q.ask, q.bbo if inst != ok else okx_bbo, q.mark, q.volume_quote,
                         None if q.quote_ts is None else q.quote_ts + (t - T), q.frame + 100) for q in rows]
    for rows in pit.obs.values():
        rows[:] = [Observation(o.t + (t - T), o.funding_rate, o.interval_hours, o.next_funding_ms, o.funding_px, o.funding_px_kind, o.frame + 100) for o in rows]
    if okx_missing:
        pit.rows[ok] = []
    pit.quotes = lambda inst, start, end=None: [q for q in pit.rows.get(inst, []) if start <= q.t < (end if end is not None else t + 1)]
    pit.observations = lambda inst, start, end=None: [o for o in pit.obs.get(inst, []) if start <= o.t < (end if end is not None else t + 1)]
    return pit


def test_no_exit_while_a_leg_has_no_tradable_quote_in_this_frame():
    pol = _policy()
    doc = _carry_doc(max_hold_hours=1, conditions=[])
    st = pol.on_step(_ctx(T, [], _fake_pit()), pol.initialize({"ir": doc}))["state"]
    held = [{"position_ref": "p1L", "instrument": {"venue": "binance", "market": "perp", "symbol": "BTCUSDT"}, "side": "long",
             "qty": "9.9990001", "avg_entry": "100.01", "net_now": "0"},
            {"position_ref": "p1S", "instrument": {"venue": "okx", "market": "perp", "symbol": "BTC-USDT-SWAP"}, "side": "short",
             "qty": "9.9990001", "avg_entry": "100.02", "net_now": None}]
    t = T + 2 * 3_600_000  # max_hold is due
    # OKX quoted without a real top of book: hold (max_hold waits too), even for the Binance leg that is quoted
    out = pol.on_step(_ctx(t, held, _frame_at(t, okx_bbo=False)), st)
    assert out["intents"] == [] and set(out["state"]["positions"]) == {"pair-1"}
    # OKX not in this frame at all (an older quote exists in no frame here; it would never be used)
    out = pol.on_step(_ctx(t + 60_000, held, _frame_at(t + 60_000, okx_missing=True)), out["state"])
    assert out["intents"] == []
    # both legs quoted again: both closes at this frame's touch
    out = pol.on_step(_ctx(t + 120_000, held, _frame_at(t + 120_000)), out["state"])
    assert [(i["intent_id"], i["side"], i["limit_price"]) for i in out["intents"]] == [("n1-L-close", "sell", "100"), ("n1-S-close", "buy", "100.03")]


# --- (2) the dataset view --------------------------------------------------------------------------------------------------

def _view_pack():
    kl = {("binance", "BTCUSDT"): bars("BTCUSDT", p_bn, vq="2"), ("okx", "BTC-USDT-SWAP"): bars("BTC-USDT-SWAP", p_ok, vq="3")}
    st = {"binance": [settle("binance:linear:USDT:BTCUSDT", START - 4 * 3_600_000, "0.0001", hours=4),
                      settle("binance:linear:USDT:BTCUSDT", START, "0.00015", hours=4, available_at=START + 90_000)],
          "okx": [settle("okx:linear:USDT:BTC-USDT-SWAP", START - 8 * 3_600_000, "-0.0002")]}
    return build([BN, OK], kl, st)


def test_dataset_view_touch_funding_volume_interval_and_synthetic_bbo(tmp_path):
    pack = Pack.from_directory(export_dir(tmp_path, _view_pack()))
    now = START + 60_000
    pairs = ir_views.dataset_frame_pairs(pack, [BN, OK], now)
    assert [p["id"] for p in pairs] == ["cross:BTC:binance:BTCUSDT>okx:BTC-USDT-SWAP", "cross:BTC:okx:BTC-USDT-SWAP>binance:BTCUSDT"]
    lg, sh = pairs[0]["long"], pairs[0]["short"]
    # touch = close of the last bar closed at now (bar START, closes START+59999), bid = ask, bbo synthetic
    assert (lg["bid"], lg["ask"], lg["bbo"], lg["funding_px"], lg["quote_ms"]) == ("100", "100", True, "100", START + 59_999)
    assert (sh["bid"], sh["ask"]) == ("100.2", "100.2")
    # last settled visible at now: binance's START settlement is only available at START+90s -> the earlier one, 4 h interval
    assert (lg["funding_rate"], lg["interval_hours"], lg["next_funding_ms"]) == ("0.0001", 4, START)
    assert (sh["funding_rate"], sh["interval_hours"], sh["next_funding_ms"]) == ("-0.0002", 8, START)
    # volume: 1440 bars x 2 (binance), x 3 (okx)
    assert lg["volume_quote"] == "2880" and sh["volume_quote"] == "4320"
    assert lg["taker_fee"] == "0.0005" and lg["quote_ccy"] == "USDT" and pairs[0]["base"] == "BTC"
    # once the START settlement is visible it is the rate
    later = ir_views.dataset_frame_pairs(pack, [BN, OK], START + 120_000)
    assert later[0]["long"]["funding_rate"] == "0.00015" and later[0]["long"]["next_funding_ms"] == START + 4 * 3_600_000
    # fewer than 1440 bars in the 24 h: volume unknown (not 0)
    early = ir_views.dataset_frame_legs(pack, [BN], LO + 20 * 3_600_000 + 5 * MIN)  # 1205 bars so far
    assert early[0]["view"]["volume_quote"] is None
    # the Pit form must be at the decision time; other views are refused
    with pytest.raises(ValueError):
        ir_views.dataset_frame_pairs(Pit(pack, now + 1), [BN, OK], now)
    with pytest.raises(ValueError):
        ir_views.dataset_frame_pairs(pack, [BN, OK], now, view="bbo")


def test_dataset_view_leaves_out_unknown_intervals_stale_bars_and_spot_without_klines_or_fee(tmp_path):
    kl = {("binance", "BTCUSDT"): bars("BTCUSDT", p_bn), ("okx", "BTC-USDT-SWAP"): bars("BTC-USDT-SWAP", p_ok, hi=START - 10 * MIN)}
    st = {"binance": [settle("binance:linear:USDT:BTCUSDT", START - 3_600_000, "0.0001")],
          "okx": [settle("okx:linear:USDT:BTC-USDT-SWAP", START - 3_600_000, "0.0001", hours=0)]}
    pack = Pack.from_directory(export_dir(tmp_path, build([BN, OK], kl, st)))
    legs = ir_views.dataset_frame_legs(pack, [BN, OK], START)
    assert [x["view"]["venue"] for x in legs] == ["binance"]  # okx: bars stale (> 5 min) and its interval is unknown
    # okx with fresh bars but an unknown interval: still out
    kl[("okx", "BTC-USDT-SWAP")] = bars("BTC-USDT-SWAP", p_ok)
    pack = Pack.from_directory(export_dir(tmp_path / "b", build([BN, OK], kl, st)))
    assert [x["view"]["venue"] for x in ir_views.dataset_frame_legs(pack, [BN, OK], START)] == ["binance"]
    # okx's latest settlement has an unusable interval: the leg is out (an older settlement is not used instead)
    # spot: a spot leg needs klines and a spot fee; with both, the same-venue spot_perp pair appears
    spot_kl = {("binance", "BTCUSDT"): bars("BTCUSDT", p_bn), ("binance", "BTCUSDT-SPOT"): bars("BTCUSDT-SPOT", lambda t: "99.9")}
    pack_s = Pack.from_directory(export_dir(tmp_path / "s", build([BN, BS], spot_kl, st)))
    assert ir_views.dataset_frame_pairs(pack_s, [BN, BS], START) == []  # default fees have no spot rate
    fees = {"binance": {"perp": "0.0005", "spot": "0.001"}}
    pairs = ir_views.dataset_frame_pairs(pack_s, [BN, BS], START, fees=fees)
    assert [p["id"] for p in pairs] == ["spot:BTC:binance:BTCUSDT-SPOT>binance:BTCUSDT"] and pairs[0]["long"]["taker_fee"] == "0.001"
    assert pairs[0]["long"]["funding_rate"] is None and pairs[0]["long"]["funding_px"] is None and pairs[0]["long"]["bid"] == "99.9"
    # no spot klines: no spot leg, so no spot_perp pair (just cross_perp, here none)
    pack_n = Pack.from_directory(export_dir(tmp_path / "n", build([BN, BS], {("binance", "BTCUSDT"): bars("BTCUSDT", p_bn)}, st)))
    assert ir_views.dataset_frame_pairs(pack_n, [BN, BS], START, fees=fees) == []


def test_the_streaming_cursor_builds_the_same_legs_as_the_point_in_time_view(tmp_path):
    pack = Pack.from_directory(export_dir(tmp_path, _view_pack()))
    cur = ir_backtest.DatasetCursor(pack, [BN, OK], START, END, fees=None, work=tmp_path / "w")
    try:
        for now in [LO + 10 * MIN, START - 1, START, START + 60_000, START + 61_000, START + 90_000, START + 3 * 3_600_000 // 3, END - 1]:
            cur.advance(now)
            assert cur.legs() == ir_views.dataset_frame_legs(pack, [BN, OK], now), now
    finally:
        cur.close()


# --- (3) simulator gold and the backtest end to end ------------------------------------------------------------------------

def test_simulator_golden_one_pair_one_settlement_one_close(tmp_path):
    """Hand calculation (notional 1000 per leg, taker 0.0005):
    entry at START: long Binance at its ask 100 -> qty 1000/100 = 10; short OKX sells at its bid 100.2
      fees 10 x 100 x 0.0005 = 0.5 and 10 x 100.2 x 0.0005 = 0.501
    settlement at START+30m (both venues): mark = close of the last bar closed by then (100 / 100.2; OKX's own settlement
      mark 999 is NOT used while a bar is there)
      long pays 10 x 100 x 0.0001 = 0.1; short receives 10 x 100.2 x 0.0002 = 0.2004
    max_hold (1 h) at START+60m, both legs quoted: long sells at 101 (+10), short buys at 100.5 (-3)
      fees 10 x 101 x 0.0005 = 0.505 and 10 x 100.5 x 0.0005 = 0.5025
    realized = 10 - 3 + (-0.1 + 0.2004) - (0.5 + 0.501 + 0.505 + 0.5025) = 7 + 0.1004 - 2.0085 = 5.0919
    """
    pack = Pack.from_directory(export_dir(tmp_path, golden_built()))
    out = tmp_path / "bt"
    card = ir_backtest.run(golden_ir(), pack, [BN, OK], start_ms=START, end_ms=END, out_dir=out, step_minutes=2)
    assert card["amounts"]["realized"] == "5.0919"
    assert card["amounts"]["price_pnl_closed"] == "7" and card["amounts"]["funding"] == "0.1004" and card["amounts"]["fees"] == "2.0085"
    assert card["counts"]["opened"] == 1 and card["counts"]["closed"] == 1 and card["counts"]["open_at_end"] == 0
    assert card["counts"]["funding_events"] == 2 and card["amounts"]["equity_end"] == "5.0919"
    trades = [json.loads(x) for x in (out / "trades.jsonl").read_text().splitlines()]
    assert len(trades) == 1
    t = trades[0]
    assert (t["trade"], t["pair_key"], t["opened_ms"], t["closed_ms"], t["exit_reason"]) == (1, "pair-1", START, START + 60 * MIN, "max_hold")
    assert (t["long"]["qty"], t["long"]["entry_price"], t["long"]["exit_price"], t["long"]["funding"]) == ("10", "100", "101", "-0.1")
    assert (t["short"]["entry_price"], t["short"]["exit_price"], t["short"]["funding"], t["realized"]) == ("100.2", "100.5", "0.2004", "5.0919")
    dec = [json.loads(x) for x in (out / "decisions.jsonl").read_text().splitlines()]
    assert dec[0]["entries"][0]["intents"] == ["n1-L-open", "n1-S-open"] and dec[30]["exits"][0]["intents"] == ["n1-L-close", "n1-S-close"]
    assert "rejected_detail" not in dec[0] and len(dec) == 60  # 2 h / 2 min, every instant has data
    # cooldown 600 min: no re-entry after the close; the close is reconciled one instant later like ir_policy
    assert all(not d["entries"] for d in dec[1:])
    eq = [json.loads(x) for x in (out / "equity.jsonl").read_text().splitlines()]
    assert eq[0]["equity"] == "-2.002"  # close-now right after entry: entry fees 0.5 + 0.501, exit fees at the touch the same (spread 0)
    assert eq[-1]["equity"] == "5.0919"
    assert Decimal(card["max_drawdown"]["amount"]) >= 0


def test_ir_backtest_cli_end_to_end_on_an_exported_pack_writes_the_run_card(tmp_path):
    pd = export_dir(tmp_path, golden_built())
    (tmp_path / "ir.json").write_text(json.dumps(golden_ir()))
    (tmp_path / "inst.json").write_text(json.dumps([OK, BN, {**BS, "venue": "bybit"}]))  # bybit spot: not in the universe
    (tmp_path / "trials.csv").write_text("trial,min_net\n1,0\n2,0.0001\n3,0.0002\n")
    out = tmp_path / "run"
    rc = cli.main(["ir", "backtest", "--ir", str(tmp_path / "ir.json"), "--start-ms", str(START), "--end-ms", str(END), "--instruments",
                   str(tmp_path / "inst.json"), "--pack-dir", str(pd), "--trials", "3", "--trials-evidence", str(tmp_path / "trials.csv"),
                   "--verbose", "--out", str(out)])
    assert rc == 0
    card = json.loads((out / "run_card.json").read_text())
    assert card["schema"] == "vibe-trading.ir-backtest-run-card/1" and card["strategy_id"] == ir.load(golden_ir()).strategy_id
    assert card["view"] == "kline_close_synthetic_bbo" and any("synthetic" in a for a in card["approximations"])
    assert card["pack"]["dataset_versions"] == {"market": "mkt-fixture-v1", "funding": "fund-fixture-v1"}
    assert card["data_audit"]["auditable"] is True and set(card["data_audit"]["sources"]) == {"alphakeel_b2"}
    from alphakeel_research import handoff

    assert handoff.classify(card)["classification"] == "auditable"
    assert card["assumptions"]["fees_source"] == "default" and card["assumptions"]["fees"]["okx"] == {"perp": "0.0005"}
    assert card["assumptions"]["capital_base"] == "2000"
    assert card["trials"]["count"] == 3 and (out / card["trials"]["evidence"]).is_file()
    assert card["alphakeel_metrics"] is not None and card["amounts"]["realized"] == "5.0919"
    assert {i["table"] for i in card["inputs"]} == {"kline_1m", "settlements"}
    assert card["window"] == {"start_ms": START, "end_ms": END, "start_day": "2023-01-01", "end_day": "2023-01-01"}
    assert card["data_window"]["start_day"] == "2022-12-31" and card["research_service"]["used"] is False
    dec = json.loads((out / "decisions.jsonl").read_text().splitlines()[1])
    assert "rejected_detail" in dec
    assert not (out / ".work").exists()


def test_ir_backtest_freezes_through_the_service_and_refuses_a_swapped_pack(tmp_path):
    built = golden_built()
    lock, blobs, _ = built

    class FakeClient:
        def __init__(self, lock):
            self.lock, self.requests = lock, []

        def check(self):
            return {"credential": "tok-fake", "data_horizon": None, "holdout_days": 60}

        def create_pack(self, request, *, key=None):
            self.requests.append(request)
            return {"job_id": "pj-1", "status": "ready"}

        def wait_pack(self, job_id, **kw):
            return {"status": "ready", "pack_id": self.lock["pack_id"]}

        def pack_lock(self, pack_id):
            return copy.deepcopy(self.lock)

        def object_range(self, pack_id, name, start, end, *, identity=False):
            return types.SimpleNamespace(content=blobs[name][start:end + 1])

    fc = FakeClient(lock)
    card = workflow.ir_backtest(fc, golden_ir(), [BN, OK], start_ms=START, end_ms=END, out_dir=tmp_path / "o", cache_dir=tmp_path / "c")
    req = fc.requests[0]
    assert req["window"] == {"start_ms": LO, "end_ms": END} and req["dataset"] == {"market_tables": ["kline_1m"], "price_kinds": ["trade"]}
    assert req["instruments"] == [{"venue": "binance", "market": "perp", "symbol": "BTCUSDT"}, {"venue": "okx", "market": "perp", "symbol": "BTC-USDT-SWAP"}]
    assert req["funding_dataset"] is None and req["accept_partial"] is False
    assert card["research_service"] == {"used": True, "credential": "tok-fake", "data_horizon": None, "holdout_days": 60}
    # the service answers with a pack for another window: refused, not used
    with pytest.raises(ApiError):
        workflow.ir_backtest(fc, golden_ir(), [BN, OK], start_ms=START + 60_000, end_ms=END, out_dir=tmp_path / "o2", cache_dir=tmp_path / "c")


# --- (4) ir export ---------------------------------------------------------------------------------------------------------

def _run(tmp_path, *trials_args):
    pd = export_dir(tmp_path, golden_built())
    (tmp_path / "ir.json").write_text(json.dumps(golden_ir()))
    (tmp_path / "inst.json").write_text(json.dumps([BN, OK]))
    out = tmp_path / "run"
    assert cli.main(["ir", "backtest", "--ir", str(tmp_path / "ir.json"), "--start-ms", str(START), "--end-ms", str(END), "--instruments",
                     str(tmp_path / "inst.json"), "--pack-dir", str(pd), *trials_args, "--out", str(out)]) == 0
    return out


def test_ir_export_round_trips_with_the_same_strategy_id_and_full_provenance(tmp_path, capsys):
    run = _run(tmp_path, "--trials", "1")
    capsys.readouterr()
    exp = tmp_path / "export"
    assert cli.main(["ir", "export", "--ir", str(tmp_path / "ir.json"), "--run", str(run), "--out", str(exp)]) == 0
    printed = json.loads(capsys.readouterr().out)
    doc = json.loads((exp / "strategy.json").read_text())
    sid = ir.load(golden_ir()).strategy_id
    assert doc["strategy_id"] == sid == ir.load(doc).strategy_id == printed["strategy_id"]
    # the definition is untouched: removing the export's keys gives the input back
    assert {k: v for k, v in doc.items() if k not in ("strategy_id", "provenance")} == {k: v for k, v in golden_ir().items() if k != "provenance"}
    p = doc["provenance"]
    assert p["tool"] == "vibe-trading" and p["run_id"].startswith("irbt-") and p["window"] == {"start": "2023-01-01", "end": "2023-01-01"}
    assert p["trials"] == {"count": 1, "evidence": "evidence/trials.jsonl"} and (exp / p["trials"]["evidence"]).is_file()
    assert p["classification"] == "auditable" and p["data_audit"]["auditable"] is True and p["data_audit"]["sources"] == ["alphakeel_b2"]
    assert p["data_view"] == "kline_close_synthetic_bbo" and p["approximations"] and "research_credential" not in p
    assert p["backtest"]["audit"] == "evidence/ir-backtest-audit.json"
    audit = json.loads((exp / "evidence" / "ir-backtest-audit.json").read_text())
    assert audit["strategy_id"] == sid and audit["run_card"]["run_id"] == p["run_id"] and audit["alphakeel_metrics"] is not None
    assert any("no research-service credential" in r for r in audit["reasons"])
    # every float-free decimal stays canonical; the document is a valid IR wherever it goes
    assert ir.validate(doc) == []


def test_ir_export_with_a_research_credential_applies_the_data_horizon_rule(tmp_path):
    run = _run(tmp_path, "--trials", "1")
    cred = {"credential": "cred-1", "holdout_days": 60, "data_horizon": {"max_window_end_ms": START + DAY}}
    r = workflow.ir_export(golden_ir(), run, tmp_path / "e1", research_credential=cred)
    assert r["provenance"]["research_credential"] == "cred-1"
    # the credential received data beyond the backtest window: the window under-states the data seen -> refused
    late = {**cred, "data_horizon": {"max_window_end_ms": START + 3 * DAY}}
    with pytest.raises(ApiError, match="data horizon"):
        workflow.ir_export(golden_ir(), run, tmp_path / "e2", research_credential=late)


def test_ir_export_refuses_missing_trials_evidence_and_another_strategy(tmp_path):
    run = _run(tmp_path)  # no --trials
    with pytest.raises(ApiError, match="trials"):
        workflow.ir_export(golden_ir(), run, tmp_path / "e")
    run2 = _run(tmp_path / "x", "--trials", "1")
    (run2 / "trials.jsonl").write_text("tampered\n")
    with pytest.raises(ApiError, match="trials evidence"):
        workflow.ir_export(golden_ir(), run2, tmp_path / "e2")
    other = golden_ir()
    other["name"] = "another"
    with pytest.raises(ApiError, match="run card is for"):
        workflow.ir_export(other, run2, tmp_path / "e3")
    # trials > 1 without the file that lists them is refused at backtest time
    with pytest.raises(ApiError, match="trials-evidence"):
        ir_backtest.run(golden_ir(), Pack.from_directory(export_dir(tmp_path / "y", golden_built())), [BN, OK], start_ms=START,
                        end_ms=END, out_dir=tmp_path / "z", trials=5)


# --- (5) the agent tool's argv ---------------------------------------------------------------------------------------------

def test_agent_tool_maps_the_ir_actions_to_the_cli():
    from src.tools.alphakeel_research_tool import _ACTIONS, _argv

    assert {"ir_validate", "ir_review", "ir_backtest", "ir_export"} <= set(_ACTIONS)
    assert _argv("ir_validate", {"ir": "s.json", "pack": "pk-1"}) == ["ir", "validate", "--ir", "s.json", "--pack", "pk-1"]
    assert _argv("ir_review", {"ir": "s.json", "pack": "pk-1", "out": "d", "seed": 1}) == [
        "ir", "review", "--pack", "pk-1", "--ir", "s.json", "--out", "d", "--seed", "1"]
    argv = _argv("ir_backtest", {"ir": "s.json", "start_ms": 1, "end_ms": 2, "instruments": "i.json", "out": "d", "trials": 3,
                                 "trials_evidence": "t.csv", "step_minutes": 5, "verbose": True})
    assert argv[:2] == ["ir", "backtest"] and "--verbose" in argv and argv[argv.index("--step-minutes") + 1] == "5"
    assert _argv("ir_export", {"ir": "s.json", "run": "r", "out": "e", "research_credential": True}) == [
        "ir", "export", "--ir", "s.json", "--run", "r", "--out", "e", "--research-credential"]
    with pytest.raises(ValueError):
        _argv("ir_backtest", {"ir": "s.json"})


def test_the_exported_document_is_accepted_by_alphakeels_rust_validator(tmp_path):
    """AlphaKeel's own check (``POST /validate/ir``, the registry importer's ``Ir::parse`` + ``validate``) of the export."""
    from alphakeel_research.client import Client
    from tests.fixtures.alphakeel_server import start

    run = _run(tmp_path, "--trials", "1")
    workflow.ir_export(golden_ir(), run, tmp_path / "exp")
    doc = json.loads((tmp_path / "exp" / "strategy.json").read_text())
    server = start()
    client = Client(server.url, server.token)
    try:
        v = workflow.validate_ir(client, doc)
    finally:
        client.close()
        server.stop()
    assert v["valid"] is True and v["problems"] == [] and v["strategy_id"] == doc["strategy_id"]
