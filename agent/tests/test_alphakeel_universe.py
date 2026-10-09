"""``alphakeel:<spec>`` alpha-bench universe: a crypto cross-section built from an AlphaKeel dataset pack.

Offline: the dataset packs are built with the service-format helpers of ``test_alphakeel_ir_backtest`` and opened from an
exported directory (or through a service double for the freeze path). Expected numbers are computed by hand in each test.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import types

import pandas as pd
import pytest

from src.factors import alphakeel_universe as AU
from src.tools.alpha_bench_tool import _load_universe_panel, run_alpha_bench
from tests.test_alphakeel_ir_backtest import _carry_doc, build, export_dir, settle

MIN = 60_000
HOUR = 3_600_000
DAY = 86_400_000
LO = 1_672_531_200_000  # 2023-01-01T00:00:00Z
HI = LO + 2 * DAY
PERIOD = "2023-01-01/2023-01-02"
BASES = ["ADA", "BTC", "DOGE", "ETH", "SOL", "XRP"]


def inst(base, venue="binance", market="perp"):
    return {"venue": venue, "market": market, "symbol": f"{base}USDT", "base": base, "quote": "USDT"}


def price(base_i: int, t: int) -> float:
    """A deterministic, base-specific minute path (no constant cross-section)."""
    k = (t - LO) // MIN
    return round(10.0 * (base_i + 1) + ((k * (base_i + 3)) % 17) * 0.01 + (k // 60) * 0.001 * (base_i - 2), 6)


def kline_rows(symbol, base_i, lo=LO, hi=HI, *, skip=(), vol=None):
    out = []
    for t in range(lo, hi, MIN):
        if t in skip:
            continue
        p = price(base_i, t)
        o, c = p, round(p + 0.003, 6)
        h, low = round(max(o, c) + 0.002, 6), round(min(o, c) - 0.001, 6)
        v = vol(t) if vol else str(1 + base_i)
        out.append([t, symbol, t + 59_999, str(o), str(h), str(low), str(c), v, str(round(float(v) * c, 8)), 3, "trade"])
    return out


def make_pack(tmp_path, bases=BASES, *, settlements=None, klines=None, lo=LO, hi=HI, name="pack"):
    insts = [inst(b) for b in bases]
    kl = klines if klines is not None else {("binance", f"{b}USDT"): kline_rows(f"{b}USDT", i) for i, b in enumerate(bases)}
    built = build(insts, kl, {"binance": settlements or []}, lo=lo, hi=hi)
    d = export_dir(tmp_path / name, built)
    return d, insts, built


def spec_file(tmp_path, pack_dir, insts, **kw):
    doc = {"schema": AU.SPEC_SCHEMA, "pack_dir": str(pack_dir), "instruments": insts, **kw}
    p = tmp_path / f"universe-{len(list(tmp_path.glob('universe-*.json')))}.json"
    p.write_text(json.dumps(doc))
    return f"alphakeel:{p}"


# --- panel values ------------------------------------------------------------------------------------------------------

def test_hourly_bars_are_complete_utc_buckets_labelled_by_open_with_quote_amount_and_true_vwap(tmp_path):
    hole = LO + 5 * HOUR + 7 * MIN  # one missing minute: that hour of BTC is not a bar
    kl = {("binance", f"{b}USDT"): kline_rows(f"{b}USDT", i, skip=(hole,) if b == "BTC" else ()) for i, b in enumerate(BASES[:3])}
    d, insts, _ = make_pack(tmp_path, BASES[:3], klines=kl)
    panel = _load_universe_panel(spec_file(tmp_path, d, insts, interval="1h", funding=False), PERIOD)
    close = panel["close"]
    assert list(close.columns) == ["ADA", "BTC", "DOGE"] and len(close) == 48
    assert close.index[0] == pd.Timestamp("2023-01-01 00:00") and close.index[-1] == pd.Timestamp("2023-01-02 23:00")
    # hand-built ADA (i=0) bar of 02:00: open = first minute's open, close = last minute's close, H/L over the hour
    t0 = LO + 2 * HOUR
    mins = [price(0, t0 + m * MIN) for m in range(60)]
    row = pd.Timestamp("2023-01-01 02:00")
    assert panel["open"].at[row, "ADA"] == pytest.approx(mins[0])
    assert panel["close"].at[row, "ADA"] == pytest.approx(round(mins[-1] + 0.003, 6))
    assert panel["high"].at[row, "ADA"] == pytest.approx(max(round(max(p, p + 0.003) + 0.002, 6) for p in mins))
    assert panel["low"].at[row, "ADA"] == pytest.approx(min(round(min(p, p + 0.003) - 0.001, 6) for p in mins))
    assert panel["volume"].at[row, "ADA"] == pytest.approx(60.0)
    amount = sum(round(1.0 * round(p + 0.003, 6), 8) for p in mins)
    assert panel["amount"].at[row, "ADA"] == pytest.approx(amount)
    assert panel["vwap"].at[row, "ADA"] == pytest.approx(amount / 60.0)
    assert math.isnan(close.at[pd.Timestamp("2023-01-01 05:00"), "BTC"])  # incomplete bucket: dropped, never filled
    assert close["BTC"].notna().sum() == 47
    assert "funding_rate" not in panel
    meta = panel["_meta"]
    assert meta["universe"] == "alphakeel" and meta["interval"] == "1h" and meta["survivorship_bias"] is True
    assert meta["pack_id"].startswith("pk-") and meta["dataset_versions"] == {"market": "mkt-fixture-v1", "funding": "fund-fixture-v1"}


def test_funding_enters_the_row_of_the_bar_during_which_it_becomes_visible(tmp_path):
    iid = "binance:linear:USDT:BTCUSDT"
    st = [
        settle(iid, LO - 8 * HOUR, "0.0001"),                                 # before the period: initial state only
        settle(iid, LO + 8 * HOUR, "0.0002", available_at=LO + 11 * HOUR),    # published 3 h late: hour 11, not 8
        settle(iid, LO + 16 * HOUR, "-0.0003"),                               # no available_at: + 60 s assumed delay
        settle(iid, LO + DAY - 30_000, "0.0004"),                             # 23:59:30 + 60 s -> visible on day 2
    ]
    d, insts, _ = make_pack(tmp_path, BASES[:2], settlements=st, lo=LO - DAY)  # the pack's window starts a day early
    hourly = _load_universe_panel(spec_file(tmp_path, d, insts, interval="1h"), PERIOD)
    fr, fs = hourly["funding_rate"]["BTC"], hourly["funding_sum"]["BTC"]

    def at(h):
        return pd.Timestamp(LO + h * HOUR, unit="ms")

    assert fr[at(0)] == pytest.approx(0.0001) and fs[at(0)] == 0.0
    assert fr[at(10)] == pytest.approx(0.0001) and fr[at(11)] == pytest.approx(0.0002) and fs[at(11)] == pytest.approx(0.0002)
    assert fs[at(8)] == 0.0
    assert fr[at(16)] == pytest.approx(-0.0003) and fs[at(16)] == pytest.approx(-0.0003)  # visible 16:01, inside bar 16:00
    assert fr[at(23)] == pytest.approx(-0.0003) and fr[at(24)] == pytest.approx(0.0004)
    assert hourly["funding_interval_hours"]["BTC"][at(11)] == 8.0
    assert hourly["funding_rate"]["ADA"].isna().all()  # no settlements for ADA in the pack
    daily = _load_universe_panel(spec_file(tmp_path, d, insts, interval="1d"), PERIOD)
    d1, d2 = pd.Timestamp("2023-01-01"), pd.Timestamp("2023-01-02")
    assert daily["funding_sum"].at[d1, "BTC"] == pytest.approx(0.0002 - 0.0003)  # the 23:59:30 one is not visible on day 1
    assert daily["funding_sum"].at[d2, "BTC"] == pytest.approx(0.0004)
    assert daily["funding_rate"].at[d1, "BTC"] == pytest.approx(-0.0003)
    assert daily["_meta"]["funding"]["assumed_delay_ms"] == 60_000


def test_no_look_ahead_rows_closed_before_a_cut_do_not_change_when_later_data_is_removed(tmp_path):
    """A pack holding only what was visible at ``cut`` gives the same rows for every bar that closed by ``cut``."""
    iid = "binance:linear:USDT:BTCUSDT"
    st = [settle(iid, LO + h * HOUR, f"0.000{h % 7 + 1}", available_at=LO + h * HOUR + 2 * HOUR) for h in range(0, 48, 8)]
    cut = LO + 30 * HOUR + 30 * MIN - 1
    st.append(settle(iid, LO + 29 * HOUR, "0.0009", available_at=cut + 1))  # settled inside a closed bar, published after the cut
    st.sort(key=lambda r: r[0])
    full, insts, _ = make_pack(tmp_path, BASES[:3], settlements=st, name="full")
    kl = {("binance", f"{b}USDT"): [r for r in kline_rows(f"{b}USDT", i) if r[2] <= cut] for i, b in enumerate(BASES[:3])}
    trunc, _, _ = make_pack(tmp_path, BASES[:3], settlements=[s for s in st if s[9] <= cut], klines=kl, name="trunc")
    a = _load_universe_panel(spec_file(tmp_path, full, insts, interval="1h"), PERIOD)
    b = _load_universe_panel(spec_file(tmp_path, trunc, insts, interval="1h"), PERIOD)
    closed = a["close"].index[a["close"].index + pd.Timedelta(hours=1) - pd.Timedelta(milliseconds=1) <= pd.Timestamp(cut, unit="ms")]
    assert len(closed) == 30
    for field in ("open", "high", "low", "close", "volume", "amount", "vwap", "funding_rate", "funding_sum", "funding_interval_hours"):
        pd.testing.assert_frame_equal(a[field].loc[closed], b[field].loc[closed], check_freq=False)
    assert b["close"].loc[pd.Timestamp(LO + 30 * HOUR, unit="ms")].isna().all()  # the half-done 30:00 bar is not a bar


# --- bases filter (Strategy IR universe.bases / excluded_bases) --------------------------------------------------------

def test_bases_filter_follows_the_ir_rule_and_intersects_with_the_strategy(tmp_path):
    d, insts, _ = make_pack(tmp_path, BASES[:4])
    doc = _carry_doc()
    doc.pop("strategy_id", None)
    doc["universe"]["bases"] = ["BTC", "ETH", "DOGE", "LINK"]
    doc["universe"]["excluded_bases"] = ["DOGE"]
    (tmp_path / "strategy.json").write_text(json.dumps(doc))
    panel = _load_universe_panel(spec_file(tmp_path, d, insts, interval="1h", ir=str(tmp_path / "strategy.json"),
                                           bases=["ADA", "BTC", "ETH", "LINK"]), PERIOD)
    assert list(panel["close"].columns) == ["BTC", "ETH"]
    f = panel["_meta"]["bases_filter"]
    assert f["allow"] == ["BTC", "ETH", "LINK"] and f["excluded"] == ["DOGE"] and f["ir_strategy_id"].startswith("ir-")
    assert panel["_meta"]["allowed_bases_without_instrument"] == ["LINK"]
    with pytest.raises(ValueError, match="upper-case"):
        _load_universe_panel(spec_file(tmp_path, d, insts, bases=["btc", "ETH"]), PERIOD)
    with pytest.raises(ValueError, match="at least two"):
        _load_universe_panel(spec_file(tmp_path, d, insts, bases=["BTC"]), PERIOD)
    with pytest.raises(ValueError, match="two instruments for base BTC"):
        _load_universe_panel(spec_file(tmp_path, d, insts + [inst("BTC", "okx")]), PERIOD)


# --- refusals ----------------------------------------------------------------------------------------------------------

def test_refuses_a_period_outside_the_pack_an_undeclared_instrument_and_bad_specs(tmp_path):
    d, insts, _ = make_pack(tmp_path, BASES[:3])
    with pytest.raises(ValueError, match="does not cover the period"):
        _load_universe_panel(spec_file(tmp_path, d, insts), "2023-01-01/2023-01-03")
    with pytest.raises(ValueError, match="does not hold"):
        _load_universe_panel(spec_file(tmp_path, d, insts + [inst("XRP")]), PERIOD)
    with pytest.raises(ValueError, match="unknown spec keys"):
        _load_universe_panel(spec_file(tmp_path, d, insts, basis=["BTC"]), PERIOD)
    with pytest.raises(ValueError, match="declare it"):
        _load_universe_panel(spec_file(tmp_path, d, [{k: v for k, v in i.items() if k != "base"} for i in insts]), PERIOD)
    with pytest.raises(ValueError, match="interval"):
        _load_universe_panel(spec_file(tmp_path, d, insts, interval="5m"), PERIOD)
    with pytest.raises(ValueError, match="does not exist"):
        _load_universe_panel("alphakeel:/nonexistent/universe.json", PERIOD)


def test_inline_json_spec_and_cli_choice(tmp_path):
    d, insts, _ = make_pack(tmp_path, BASES[:2])
    inline = "alphakeel:" + json.dumps({"pack_dir": str(d), "instruments": insts, "interval": "4h", "funding": False})
    assert len(_load_universe_panel(inline, PERIOD)["close"]) == 12
    from src.factors import cli_handlers

    root = argparse.ArgumentParser()
    cli_handlers.add_subparser(root.add_subparsers(dest="cmd"))
    ns = root.parse_args(["alpha", "bench", "--zoo", "academic", "--universe", "alphakeel:/x/u.json", "--period", PERIOD])
    assert ns.universe == "alphakeel:/x/u.json"
    assert root.parse_args(["alpha", "compare", "a", "b", "--universe", "sp500"]).universe == "sp500"
    with pytest.raises(SystemExit):
        root.parse_args(["alpha", "bench", "--universe", "nasdaq"])


# --- freeze through the research service ---------------------------------------------------------------------------------

def test_freeze_mode_asks_for_exactly_the_period_and_refuses_a_swapped_pack(tmp_path, monkeypatch):
    _d, insts, built = make_pack(tmp_path, BASES[:2], settlements=[settle("binance:linear:USDT:BTCUSDT", LO + 8 * HOUR, "0.0001")])
    lock, blobs, _ = built

    class FakeClient:
        requests: list = []

        def __init__(self, lock):
            self.lock = lock

        def check(self):
            return {"credential": "tok-fake", "holdout_days": 60}

        def create_pack(self, request, *, key=None):
            FakeClient.requests.append(request)
            return {"job_id": "pj-1", "status": "queued"}

        def wait_pack(self, job_id, **kw):
            return {"status": "ready", "pack_id": self.lock["pack_id"]}

        def pack_lock(self, pack_id):
            return copy.deepcopy(self.lock)

        def object_range(self, pack_id, name, start, end, *, identity=False):
            return types.SimpleNamespace(content=blobs[name][start:end + 1])

    from alphakeel_research import workflow

    monkeypatch.setattr(workflow, "DEFAULT_CACHE", tmp_path / "cache")
    monkeypatch.setattr(AU, "client_factory", lambda: FakeClient(lock))
    spec = "alphakeel:" + json.dumps({"instruments": list(reversed(insts)), "interval": "1d"})
    panel = _load_universe_panel(spec, PERIOD)
    req = FakeClient.requests[0]
    assert req["window"] == {"start_ms": LO, "end_ms": HI} and req["funding_dataset"] is None and req["accept_partial"] is False
    assert req["dataset"] == {"market_tables": ["kline_1m"], "price_kinds": ["trade"]}
    assert req["instruments"] == [{"venue": "binance", "market": "perp", "symbol": s} for s in ("ADAUSDT", "BTCUSDT")]
    assert panel["_meta"]["research_service"] == {"used": True, "credential": "tok-fake", "holdout_days": 60}
    assert panel["funding_sum"].at[pd.Timestamp("2023-01-01"), "BTC"] == pytest.approx(0.0001)
    with pytest.raises(ValueError, match="does not match the request"):
        _load_universe_panel(spec, "2023-01-02/2023-01-02")


# --- end to end: an alpha-zoo factor benched on the crypto cross-section --------------------------------------------------

def test_alpha_bench_runs_an_alpha_zoo_factor_on_the_pack_cross_section(tmp_path):
    def vol_of(i):
        return lambda t: str(1 + i + ((t - LO) // MIN) % (5 + i))

    kl = {("binance", f"{b}USDT"): kline_rows(f"{b}USDT", i, vol=vol_of(i)) for i, b in enumerate(BASES)}
    d, insts, _ = make_pack(tmp_path, BASES, klines=kl)
    env = run_alpha_bench(alpha_id="academic_illiq", universe=spec_file(tmp_path, d, insts, interval="1h"), period=PERIOD,
                          output_dir=str(tmp_path / "reports"))
    assert env["status"] == "ok", env
    assert env["n_alphas_tested"] == 1 and env["top"][0]["id"] == "academic_illiq" and env["top"][0]["ic_count"] > 0
