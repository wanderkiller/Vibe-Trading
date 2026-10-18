"""Review fixes of the HL on-chain integration (2026-10-18): view needs refused on scan frames and without a source in the dataset
run, the 31-day history lower bound, duplicate settlements by visibility, the holiday-table range, the equity leg's volume, the
6-hour borrow age and the economics-free history features."""

from __future__ import annotations

from decimal import Decimal

import pytest

from alphakeel_research import ir, ir_backtest, ir_calendar, ir_views
from alphakeel_research.errors import ApiError, Unsupported
from alphakeel_research.packfile import Inst, Pack, Pit
from tests.test_alphakeel_hl_views import (BNID, BTC, DAY, EQ_FEES, HOUR, LO, MIN, NOW, NVDA, NVDA_PERP, ST, bar_rows, build, coarse_pack,
                                           equity_pack, export_dir, extend, fresh_1m, legs_of, same_both_ways, settle, utc)
from tests.test_alphakeel_ir import _carry_doc, _policy

FEES = {"binance": {"perp": "0.0005"}}


def _cond(feature, **kw):
    return {"feature": feature, "op": ">=", "value": "0", **kw}


def _doc(**kw):
    doc = _carry_doc()
    doc.pop("strategy_id", None)
    return doc


# --- P1-1: scan frames refuse what they cannot serve ---------------------------------------------------------------------

def _entry(f):
    d = _doc()
    d["entry"]["conditions"] = [_cond(f)]
    return d


def _rank(f):
    d = _doc()
    d["entry"]["rank_by"] = f
    return d


def _exit(f):
    d = _doc()
    d["exit"]["conditions"] = [_cond(f)]
    return d


def _margin():
    d = _doc()
    d["universe"].update(shape="spot_perp", pairs=[["hyperliquid", "hyperliquid"]])
    d["sizing"]["margin"] = {"mode": "portfolio", "reduce_above": "0.6", "reduce_to": "0.4"}
    return d


def _equity():
    d = _doc()
    d["universe"].update(shape="equity_perp", pairs=[["ibkr", "binance"]])
    return d


@pytest.mark.parametrize("make", [lambda: _entry("funding_apr_7d"), lambda: _entry("funding_apr_30d"), lambda: _entry("funding_pos_share_30d"),
                                  lambda: _rank("borrow_apr"), lambda: _exit("borrow_utilization"), lambda: _entry("borrow_cap_used"),
                                  lambda: _exit("listing_age_hours"), _margin, _equity])
def test_scan_frame_policy_refuses_ir_needing_history_borrow_listing_margin_or_equity(make):
    with pytest.raises(Unsupported) as e:
        _policy().initialize({"ir": make()})
    assert e.value.code == "capability.unsupported"


def test_scan_frame_policy_still_loads_an_ir_without_those_needs():
    assert _policy().initialize({"ir": _doc()})["strategy_id"].startswith("ir-")


# --- P1-2: the dataset run needs a source for each view need -------------------------------------------------------------------

def _cursor(tmp_path, pack, needs, start, borrow=None, insts=(BTC,), fees=FEES, end=None):
    cur = ir_backtest.DatasetCursor(pack, list(insts), start, end or start + DAY, fees=fees, work=tmp_path / "w", borrow=borrow, needs=frozenset(needs))
    cur.close()


def test_borrow_need_needs_a_borrow_lend_table_or_a_constant(tmp_path):
    pack = coarse_pack(tmp_path / "a", m1=fresh_1m())
    with pytest.raises(ApiError) as e:
        _cursor(tmp_path, pack, {"borrow"}, NOW - 20 * DAY)
    assert e.value.code == "request.invalid"
    _cursor(tmp_path / "c", pack, {"borrow"}, NOW - 20 * DAY, borrow={"binance": {"apr": "0.05"}})
    with pytest.raises(ApiError):
        _cursor(tmp_path / "d", pack, {"borrow"}, NOW - 20 * DAY, borrow={"okx": {"apr": "0.05"}})  # a constant for another venue
    table = [[NOW - HOUR, "lend:USDT", "0.07", "0.6", "1000", "500", "2000", None]]
    withtable = coarse_pack(tmp_path / "b", m1=fresh_1m(), extra=[("borrow_lend", "binance", "lend", table)])
    _cursor(tmp_path / "e", withtable, {"borrow"}, NOW - 20 * DAY)
    _cursor(tmp_path / "f", pack, set(), NOW - 20 * DAY)  # no need, no check


def test_listing_need_needs_an_instrument_meta_listing_time(tmp_path):
    pack = coarse_pack(tmp_path / "a", m1=fresh_1m())
    with pytest.raises(ApiError):
        _cursor(tmp_path, pack, {"listing"}, NOW - 20 * DAY)
    nolisting = [[LO, "BTCUSDT", "0.1", "0.001", "0.001", "5", None, None, None]]
    p2 = coarse_pack(tmp_path / "b", m1=fresh_1m(), extra=[("instrument_meta", "binance", "perp", nolisting)])
    with pytest.raises(ApiError):
        _cursor(tmp_path / "c", p2, {"listing"}, NOW - 20 * DAY)
    listed = [[LO, "BTCUSDT", "0.1", "0.001", "0.001", "5", NOW - 40 * DAY, None, None]]
    p3 = coarse_pack(tmp_path / "d", m1=fresh_1m(), extra=[("instrument_meta", "binance", "perp", listed)])
    _cursor(tmp_path / "e", p3, {"listing"}, NOW - 20 * DAY)


def test_history_need_needs_the_pack_to_reach_31_days_before_the_first_decision(tmp_path):
    short = Pack.from_directory(export_dir(tmp_path / "s", build([BTC], {("binance", "BTCUSDT"): fresh_1m()}, ST, lo=NOW - 3 * DAY, hi=NOW + DAY)))
    with pytest.raises(ApiError) as e:
        _cursor(tmp_path / "x", short, {"history"}, NOW - 2 * DAY)
    assert e.value.code == "request.invalid"
    _cursor(tmp_path / "y", short, set(), NOW - 2 * DAY)
    # the same through run(): a history IR on a one-day-warm-up pack is refused before any work
    doc = _doc()
    doc["entry"]["conditions"] = [_cond("funding_apr_7d")]
    with pytest.raises(ApiError):
        ir_backtest.run(doc, short, [BTC], start_ms=NOW - 2 * DAY, end_ms=NOW - DAY, out_dir=tmp_path / "run", fees=FEES)
    long_pack = coarse_pack(tmp_path / "l", m1=fresh_1m())
    _cursor(tmp_path / "z", long_pack, {"history"}, NOW - 5 * DAY)


def test_dataset_request_moves_the_window_start_31_days_only_for_history_irs():
    plain = ir_backtest.dataset_request([BTC], NOW, NOW + DAY)
    hist = ir_backtest.dataset_request([BTC], NOW, NOW + DAY, history=True)
    assert plain["window"]["start_ms"] == NOW - ir_backtest.WARMUP_MS
    assert hist["window"]["start_ms"] == NOW - 31 * DAY and {k: v for k, v in hist.items() if k != "window"} == {k: v for k, v in plain.items() if k != "window"}
    assert ir_backtest.dataset_request([BTC], NOW, NOW + DAY, history=False) == plain
    assert ir.view_needs(_entry("funding_apr_30d")) == {"history"} and ir.view_needs(_doc()) == frozenset()


# --- P2-3 / P2-4: duplicate settlements, lower bound ---------------------------------------------------------------------------

def test_a_duplicate_settlement_time_takes_the_later_visible_row_in_both_views(tmp_path):
    t = NOW - 3 * HOUR
    first = settle(BNID.format("BTCUSDT"), t, "0.0001", available_at=t + 2 * MIN)
    later = settle(BNID.format("BTCUSDT"), t, "0.0009", available_at=t + 5 * MIN)
    pack = coarse_pack(tmp_path, m1=bar_rows("BTCUSDT", t - 12 * MIN, t + 30 * MIN, MIN), st={"binance": [later, first]})  # file order: later first
    got = same_both_ways(tmp_path / "w", pack, [BTC], [t + 3 * MIN, t + 6 * MIN], LO, NOW + DAY)
    assert got[t + 3 * MIN][0]["view"]["funding_rate"] == "0.0001" and got[t + 6 * MIN][0]["view"]["funding_rate"] == "0.0009"


def test_the_settlement_lower_bound_is_the_later_of_the_pack_start_and_31_days(tmp_path):
    pack = coarse_pack(tmp_path, m1=fresh_1m(), st=ST)
    pit = Pit(pack, NOW)
    assert pit.window_start_ms() == LO
    # a settlement 35 days old is outside 31 days even though the pack (40 days) holds it: not in the window fields either way
    old = settle(BNID.format("BTCUSDT"), NOW - 35 * DAY, "0.5")
    p2 = coarse_pack(tmp_path / "b", m1=fresh_1m(), st={"binance": [old] + ST["binance"]})
    assert all(s.t >= NOW - 31 * DAY for s in ir_views.visible_settlements(Pit(p2, NOW), BTC_INST, NOW))
    same_both_ways(tmp_path / "w", p2, [BTC], [NOW], LO, NOW + DAY)


BTC_INST = Inst("binance", "perp", "BTCUSDT")


# --- P2-5: calendar range ----------------------------------------------------------------------------------------------------

def test_equity_runs_outside_the_holiday_tables_are_refused(tmp_path):
    ir_calendar.require_coverage(utc(2024, 11, 28, 15), utc(2027, 3, 26, 20))  # the edges are covered
    for a, b in ((utc(2024, 11, 27, 15), utc(2024, 11, 28, 15)), (utc(2027, 3, 26, 15), utc(2027, 3, 27, 15))):
        with pytest.raises(ApiError) as e:
            ir_calendar.require_coverage(a, b)
        assert e.value.code == "request.invalid" and "2024-11-28" in str(e.value) and "2027-03-26" in str(e.value)
    day = utc(2027, 4, 6)
    pack, lo, hi = equity_pack(tmp_path, day)
    with pytest.raises(ApiError):
        _cursor(tmp_path / "x", pack, set(), day + 14 * HOUR, insts=(NVDA_PERP, NVDA), fees=EQ_FEES, end=day + 22 * HOUR)
    inside = utc(2026, 3, 2)
    pack2, lo, hi = equity_pack(tmp_path / "ok", inside)
    _cursor(tmp_path / "y", pack2, set(), inside + 14 * HOUR, insts=(NVDA_PERP, NVDA), fees=EQ_FEES, end=inside + 22 * HOUR)


# --- P2-7: equity volume -------------------------------------------------------------------------------------------------------

def test_the_equity_leg_has_no_volume_in_either_view(tmp_path):
    day = utc(2026, 3, 2)
    pack, lo, hi = equity_pack(tmp_path, day, hours=(-12, 23))  # 35 h of bars: the perp has its 24 h volume
    now = day + 15 * HOUR
    legs = {x["view"]["market"]: x["view"] for x in legs_of(pack, [NVDA_PERP, NVDA], now, fees=EQ_FEES)}
    assert legs["equity"]["volume_quote"] is None and legs["perp"]["volume_quote"] is not None
    cur = ir_backtest.DatasetCursor(pack, [NVDA_PERP, NVDA], day + 14 * HOUR, hi, fees=EQ_FEES, work=tmp_path / "w")
    try:
        cur.advance(now)
        assert {x["view"]["market"]: x["view"]["volume_quote"] for x in cur.legs()}["equity"] is None
    finally:
        cur.close()


# --- P2-8: borrow age, pos-share denominator, history without economics -----------------------------------------------------------------

def test_a_borrow_reading_older_than_6_hours_is_missing_in_both_views(tmp_path):
    table = [[NOW - 7 * HOUR, "lend:USDT", "0.05", "0.5", "1000", "400", "2000", None],
             [NOW - 5 * HOUR, "lend:USDT", "0.07", "0.6", "1000", "500", "2000", None]]
    wide = bar_rows("BTCUSDT", NOW - 9 * HOUR, NOW + 3 * HOUR, MIN)
    st = {"binance": [settle(BNID.format("BTCUSDT"), NOW - 8 * HOUR, "0.0001")]}
    pack = coarse_pack(tmp_path, m1=wide, st=st, extra=[("borrow_lend", "binance", "lend", table)])
    nows = [NOW - 5 * HOUR - 10 * MIN, NOW - 5 * HOUR + 10 * MIN, NOW + HOUR - 10 * MIN, NOW + HOUR + 10 * MIN]
    got = same_both_ways(tmp_path / "w", pack, [BTC], nows, LO, NOW + DAY, borrow={"binance": {"apr": "0.01"}})
    views = [got[t][0]["view"] for t in nows]
    assert views[0]["borrow_apr"] == "0.05" and views[1]["borrow_apr"] == "0.07"
    assert views[2]["borrow_apr"] == "0.07"  # read 5 h 50 min ago: still current
    assert "borrow_apr" not in views[3]       # read 6 h 10 min ago: stale, and the constant does not replace it


def test_history_fields_pos_share_counts_only_settlements_with_a_usable_interval():
    rows = [(1_000_000 + k * 28_800_000, Decimal("0.0001") if k % 2 == 0 else Decimal("-0.0001"), 28800) for k in range(100)]
    now = rows[-1][0] + 3_600_000
    rows.insert(10, (rows[9][0] + 1000, Decimal("0.9"), 5000))  # an interval the view cannot use: out of the mean and of the share
    f, _ = ir_views.funding_history_fields(rows, now)
    win = [r for r in rows if now - 30 * DAY <= r[0] < now and r[2] == 28800]
    assert Decimal(f["funding_pos_share_30d"]) == Decimal(sum(1 for r in win if r[1] > 0)) / len(win)


def test_a_perp_pair_without_funding_rate_but_with_funding_mean_has_history_apr_and_no_economics():
    def leg(venue, mean):
        return {"venue": venue, "symbol": "BTC", "market": "perp", "quote_ccy": "USDT", "bid": "100", "ask": "100", "bbo": True,
                "funding_rate": None, "interval_hours": None, "funding_px": "100", "volume_quote": "1000", "taker_fee": "0.0005",
                "quote_ms": 1, "funding_mean_7d": mean, "funding_mean_30d": mean}
    pair = {"id": "x", "base": "BTC", "kind": "cross_perp", "long": leg("binance", "0.00001"), "short": leg("okx", "0.00003")}
    f = ir.compute_features(pair, {"horizon_hours": 8, "basis_stress": "0.001", "funding_estimate": "predicted"}, 2)
    assert f["funding_apr_7d"] == (Decimal("0.00003") - Decimal("0.00001")) * 8760 and f["funding_apr_30d"] == f["funding_apr_7d"]
    assert f["funding_apr"] is None and f["net_expected"] is None and f["net_conservative"] is None
