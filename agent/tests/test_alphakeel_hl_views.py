"""Dataset view of the 2026-10-18 HL / equity increments (design docs/plans/2026-10-18-hl-onchain-arb-integration.md section 4.3):
coarse 1h / 1d bars, the history fields from official settlements, borrow and listing fields, the NYSE-window equity leg priced
by index klines, and ``equity_perp`` pair enumeration. Every check of the point-in-time view (``ir_views.dataset_frame_legs``)
is also run through the streaming cursor (``ir_backtest.DatasetCursor``), the second implementation.

Offline: packs are built with the helpers of test_alphakeel_ir_backtest (the service's object formats).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from alphakeel_research import ir_backtest, ir_calendar, ir_views, packfile
from alphakeel_research.errors import ApiError
from alphakeel_research.packfile import Pack
from tests.test_alphakeel_ir_backtest import FEES, H, MIN, _obj, build, export_dir, settle

HOUR = 3_600_000
DAY = 86_400_000
BNID = "binance:linear:USDT:{}"


def utc(y, mo, d, h=0, mi=0, s=0) -> int:
    return int(dt.datetime(y, mo, d, h, mi, s, tzinfo=dt.timezone.utc).timestamp() * 1000)


def bar_rows(symbol, lo, hi, span, *, kind="trade", close="100", vq="10"):
    """Bars [lo, hi) of ``span`` ms: close constant, volume_quote constant."""
    return [[t, symbol, t + span - 1, close, close, close, close, "1", vq, 1, kind] for t in range(lo, hi, span)]


def extend(built, objs):
    """Add market objects to a pack built by ``build``: ``objs`` = [(table, venue, market, rows)] (any table, own schema id)."""
    lock, blobs, contents = built
    for table, venue, market, rows in objs:
        o, stored, content = _obj(f"market/{table}/{venue}/{market}/part-00000.jsonl.zst", "market",
                                  f"alphakeel.rows.market-{table.replace('_', '-')}/1",
                                  {"schema": f"alphakeel.rows.market-{table.replace('_', '-')}/1", "table": table, "columns": ["t", "symbol"]}, rows)
        lock["objects"].append(o)
        blobs[o["name"]] = stored
        contents[o["content_sha256"]] = content
    lock["objects"].sort(key=lambda o: o["name"])
    digest = packfile.pack_digest(lock)
    lock["pack_sha256"], lock["pack_id"] = digest, f"pk-{digest[:24]}"
    return lock, blobs, contents


BTC = {"venue": "binance", "market": "perp", "symbol": "BTCUSDT", "base": "BTC", "quote": "USDT"}
HYPE = {"venue": "hyperliquid", "market": "perp", "symbol": "HYPE", "base": "HYPE", "quote": "USDC"}
NVDA_PERP = {"venue": "binance", "market": "perp", "symbol": "NVDAUSDT", "base": "NVDA", "quote": "USDT"}
NVDA = {"venue": "ibkr", "market": "equity", "symbol": "NVDA", "base": "NVDA", "quote": "USDT"}
EQ_FEES = {**FEES, "binance": {"perp": "0.0005"}, "ibkr": {"equity": "0.0003"}}


def legs_of(pack, insts, now, **kw):
    return ir_views.dataset_frame_legs(pack, insts, now, fees=kw.pop("fees", FEES), **kw)


def cursor_legs(tmp_path, pack, insts, nows, lo, hi, fees=FEES, borrow=None):
    cur = ir_backtest.DatasetCursor(pack, insts, lo + DAY, hi, fees=fees, work=tmp_path / "w", borrow=borrow)
    out = {}
    try:
        for now in nows:
            cur.advance(now)
            out[now] = cur.legs()
    finally:
        cur.close()
    return out


def same_both_ways(tmp_path, pack, insts, nows, lo, hi, **kw):
    """The Pit view and the streaming cursor give the same legs at every ``now``; returns the Pit legs by time."""
    fees = kw.get("fees", FEES)
    borrow = kw.get("borrow")
    got = cursor_legs(tmp_path, pack, insts, nows, lo, hi, fees, borrow)
    for now in nows:
        want = ir_views.dataset_frame_legs(pack, insts, now, fees=fees, borrow=borrow)
        assert got[now] == want, now
    return {now: ir_views.dataset_frame_legs(pack, insts, now, fees=fees, borrow=borrow) for now in nows}


# --- coarse bars -----------------------------------------------------------------------------------------------------------

NOW = utc(2026, 3, 2, 12, 30)  # a Monday, mid-hour
H0 = NOW - 30 * MIN  # 12:00, the hour grid of 1h bars
D0 = utc(2026, 3, 2)  # the day grid of 1d bars
LO = NOW - 40 * DAY  # the pack window starts well before the 30-day history windows
ST = {"binance": [settle(BNID.format("BTCUSDT"), NOW - 3 * HOUR, "0.0001")]}


def coarse_pack(tmp, *, m1=None, h1=None, d1=None, st=None, extra=()):
    kl = {("binance", "BTCUSDT"): m1} if m1 else {}
    built = build([BTC], kl, st or ST, lo=LO, hi=NOW + DAY)
    objs = []
    if h1:
        objs.append(("kline_1h", "binance", "perp", h1))
    if d1:
        objs.append(("kline_1d", "binance", "perp", d1))
    objs.extend(extra)
    return Pack.from_directory(export_dir(tmp, extend(built, objs)))


def test_a_leg_without_fresh_1m_bars_uses_the_1h_series_and_its_24h_volume(tmp_path):
    h1 = bar_rows("BTCUSDT", H0 - 2 * DAY, H0, HOUR, close="123.5", vq="10")  # last bar closes at 11:59:59.999
    pack = coarse_pack(tmp_path, h1=h1)
    (leg,) = legs_of(pack, [BTC], NOW)
    v = leg["view"]
    assert (v["bid"], v["ask"], v["bbo"], v["quote_ms"], v["funding_px"]) == ("123.5", "123.5", True, H0 - 1, "123.5")
    assert v["volume_quote"] == "240"  # 24 hourly bars x 10
    # 65 minutes is the limit of the last closed 1h bar, 65 minutes and 1 ms is not
    edge = H0 - 1 + 65 * MIN
    assert legs_of(pack, [BTC], edge)[0]["view"]["quote_ms"] == H0 - 1
    assert legs_of(pack, [BTC], edge + 1) == []
    # 23 bars in the 24 h: the volume is unknown, not a sum of 23
    short = coarse_pack(tmp_path / "s", h1=bar_rows("BTCUSDT", H0 - 23 * HOUR, H0, HOUR))
    assert legs_of(short, [BTC], NOW)[0]["view"]["volume_quote"] is None


def test_the_finest_fresh_series_wins_and_1d_is_the_last_resort(tmp_path):
    m1 = bar_rows("BTCUSDT", NOW - 20 * MIN, NOW - 10 * MIN, MIN, close="101")  # last closed 12:19:59.999, 10 min old: stale
    h1 = bar_rows("BTCUSDT", H0 - DAY, H0, HOUR, close="102")
    d1 = bar_rows("BTCUSDT", D0 - 3 * DAY, D0, DAY, close="103", vq="777")
    pack = coarse_pack(tmp_path, m1=m1, h1=h1, d1=d1)
    assert legs_of(pack, [BTC], NOW)[0]["view"]["bid"] == "102"  # 1m is older than 5 minutes: the 1h bar
    assert legs_of(pack, [BTC], NOW - 10 * MIN + 4 * MIN)[0]["view"]["bid"] == "101"  # 1m is 4 minutes old: fresh again
    # only a 1d series: fresh for 1 day + 5 minutes after the bar closed
    only_d = coarse_pack(tmp_path / "d", d1=bar_rows("BTCUSDT", D0 - 3 * DAY, D0, DAY, close="103", vq="777"))
    closed = D0 - 1  # the last bar covers the day before D0
    assert legs_of(only_d, [BTC], closed + DAY + 5 * MIN)[0]["view"]["volume_quote"] == "777"  # one daily bar
    assert legs_of(only_d, [BTC], closed + DAY + 5 * MIN + 1) == []
    assert legs_of(only_d, [BTC], closed + DAY + 5 * MIN)[0]["view"]["quote_ms"] == closed


def test_without_the_new_tables_the_view_is_the_old_one_with_no_new_fields(tmp_path):
    m1 = bar_rows("BTCUSDT", NOW - 2 * DAY, NOW, MIN, close="100")
    pack = coarse_pack(tmp_path, m1=m1)
    (leg,) = legs_of(pack, [BTC], NOW)
    assert set(leg["view"]) == {"venue", "symbol", "market", "quote_ccy", "bid", "ask", "bbo", "funding_rate", "interval_hours",
                                "next_funding_ms", "funding_px", "volume_quote", "taker_fee", "quote_ms"}
    assert leg["view"]["volume_quote"] == "14400"  # 1440 x 10
    same_both_ways(tmp_path, pack, [BTC], [LO + DAY, NOW - 7 * MIN, NOW], LO, NOW + DAY)


def test_the_streaming_cursor_reads_the_coarse_series_like_the_point_in_time_view(tmp_path):
    m1 = bar_rows("BTCUSDT", NOW - 20 * MIN, NOW - 10 * MIN, MIN, close="101")
    h1 = bar_rows("BTCUSDT", H0 - 2 * DAY, H0, HOUR, close="102")
    d1 = bar_rows("BTCUSDT", D0 - 3 * DAY, D0, DAY, close="103")
    pack = coarse_pack(tmp_path, m1=m1, h1=h1, d1=d1)
    nows = [NOW - 15 * MIN, NOW - 6 * MIN, NOW - 5 * MIN, NOW, NOW + 36 * MIN + 1, NOW + 40 * MIN]
    got = same_both_ways(tmp_path, pack, [BTC], nows, LO, NOW + DAY)
    assert [bool(got[n]) for n in nows] == [True] * 4 + [True, True]
    cur = ir_backtest.DatasetCursor(pack, [BTC], LO + DAY, NOW + DAY, fees=FEES, work=tmp_path / "w2")
    try:
        names = {t for t, _ in [(a["table"], a["instrument"]["symbol"]) for a in cur.inputs(pack)]}
    finally:
        cur.close()
    assert {"kline_1m", "kline_1h", "kline_1d"} <= names  # run-manifest.inputs carries the coarse chains


# --- history fields --------------------------------------------------------------------------------------------------------

def settlements_8h(count, now=NOW, rate=lambda k: "0.0001"):
    """``count`` 8-hour settlements of BTCUSDT, the newest 10 minutes before ``now`` (k = 0), ``rate(k)`` for the k-th newest."""
    return {"binance": [settle(BNID.format("BTCUSDT"), now - 10 * MIN - k * 8 * HOUR, rate(k)) for k in reversed(range(count))]}


def fresh_1m():
    return bar_rows("BTCUSDT", NOW - 12 * MIN, NOW + 12 * MIN, MIN)


def test_history_fields_are_means_of_hourly_rates_over_visible_settlements_with_coverage(tmp_path):
    # rates by age: the newest 21 (the 7 d window) are 0.0008 / -0.0004 alternating; older ones 0.0016
    rate = lambda k: ("0.0008" if k % 2 == 0 else "-0.0004") if k < 21 else "0.0016"  # noqa: E731
    pack = coarse_pack(tmp_path, m1=fresh_1m(), st=settlements_8h(90, rate=rate))
    v = legs_of(pack, [BTC], NOW)[0]["view"]
    seven = [Decimal(rate(k)) / 8 for k in range(21)]
    thirty = [Decimal(rate(k)) / 8 for k in range(90)]  # t = NOW - 10 min - 8 h k >= NOW - 720 h  <=>  k <= 89
    tiny = Decimal("1e-25")  # the view carries 60 significant digits, this test's own sums 28
    assert abs(Decimal(v["funding_mean_7d"]) - sum(seven) / 21) < tiny
    assert abs(Decimal(v["funding_mean_30d"]) - sum(thirty) / 90) < tiny
    assert abs(Decimal(v["funding_pos_share_30d"]) - Decimal(sum(1 for k in range(90) if Decimal(rate(k)) > 0)) / 90) < tiny
    same_both_ways(tmp_path, pack, [BTC], [NOW - 5 * MIN, NOW], LO, NOW + DAY)


@pytest.mark.parametrize("count,seven,thirty", [(81, True, True), (80, True, False), (19, True, False), (18, False, False)])
def test_history_fields_need_090_of_the_window_hours_covered(tmp_path, count, seven, thirty):
    # 8 h each: 19 settlements = 152 h >= 151.2 h (0.9 x 168), 18 = 144 h; 81 = 648 h >= 648 h (0.9 x 720), 80 = 640 h
    pack = coarse_pack(tmp_path, m1=fresh_1m(), st=settlements_8h(count))
    v = legs_of(pack, [BTC], NOW)[0]["view"]
    assert ("funding_mean_7d" in v) is seven
    assert ("funding_mean_30d" in v) is thirty and ("funding_pos_share_30d" in v) is thirty
    same_both_ways(tmp_path, pack, [BTC], [NOW - 5 * MIN, NOW], LO, NOW + DAY)


def test_history_counts_only_visible_settlements_inside_the_window_and_infers_missing_intervals(tmp_path):
    # the settlement 30 s ago is not visible yet (delay 60 s); one 7 d + 1 s old is outside the 7 d window
    rows = [settle(BNID.format("BTCUSDT"), NOW - 30_000, "0.5")]
    rows += settlements_8h(21)["binance"]
    rows.append(settle(BNID.format("BTCUSDT"), NOW - 7 * DAY - 1000, "0.9"))
    pack = coarse_pack(tmp_path, m1=fresh_1m(), st={"binance": sorted(rows, key=lambda r: r[0])})
    v = legs_of(pack, [BTC], NOW)[0]["view"]
    assert Decimal(v["funding_mean_7d"]) == Decimal("0.0001") / 8  # 21 rows of 0.0001 / 8 h; 0.5 and 0.9 are not in
    # an interval the dataset leaves null is inferred from the gap to the previous settlement (8 h here)
    blank = [r[:4] + [None] + r[5:] for r in settlements_8h(21)["binance"]]
    pack2 = coarse_pack(tmp_path / "b", m1=fresh_1m(), st={"binance": blank})
    assert legs_of(pack2, [BTC], NOW)[0]["view"]["funding_mean_7d"] == legs_of(pack, [BTC], NOW)[0]["view"]["funding_mean_7d"]


def test_listed_ms_and_borrow_from_the_packs_tables_else_constants_else_missing(tmp_path):
    meta = [[LO, "BTCUSDT", "0.1", "0.001", "0.001", "5", NOW - 40 * DAY, None, None]]
    borrow = [[NOW - 2 * HOUR, "lend:USDT", "0.05", "0.5", "1000", "400", "2000", None],
              [NOW - HOUR, "lend:USDT", "0.07", "0.6", "1000", "500", "2000", NOW - 30 * MIN],  # read at 11:30, known from then
              [NOW - 5 * MIN, "lend:USDT", "0.09", "0.9", "1000", "900", "2000", NOW + 10 * MIN]]  # not known yet
    wide = bar_rows("BTCUSDT", NOW - 4 * HOUR, NOW + 12 * MIN, MIN)
    pack = coarse_pack(tmp_path, m1=wide, extra=[("instrument_meta", "binance", "perp", meta), ("borrow_lend", "binance", "lend", borrow)])
    v = legs_of(pack, [BTC], NOW)[0]["view"]
    assert v["listed_ms"] == NOW - 40 * DAY
    assert (v["borrow_apr"], v["borrow_util"], v["borrow_cap_used"]) == ("0.07", "0.6", "0.25")
    assert legs_of(pack, [BTC], NOW + 10 * MIN)[0]["view"]["borrow_apr"] == "0.09"
    got = same_both_ways(tmp_path, pack, [BTC], [NOW - 150 * MIN, NOW - 90 * MIN, NOW - 45 * MIN, NOW, NOW + 10 * MIN], LO, NOW + DAY)
    assert "borrow_apr" not in got[NOW - 150 * MIN][0]["view"] and got[NOW - 90 * MIN][0]["view"]["borrow_apr"] == "0.05"
    # a listing time after the decision time is not known yet; a snapshot dated after it is not known either
    future = [[LO, "BTCUSDT", "0.1", "0.001", "0.001", "5", NOW + HOUR, None, None]]
    later = [[NOW + HOUR, "BTCUSDT", "0.1", "0.001", "0.001", "5", NOW - DAY, None, None]]
    for k, rows in (("a", future), ("b", later)):
        p2 = coarse_pack(tmp_path / k, m1=wide, extra=[("instrument_meta", "binance", "perp", rows)])
        got2 = same_both_ways(tmp_path / k, p2, [BTC], [NOW], LO, NOW + DAY)
        assert "listed_ms" not in got2[NOW][0]["view"]

    # no table: the request's constants, util / cap_used optional; no constants: nothing
    plain = coarse_pack(tmp_path / "p", m1=fresh_1m())
    assert "borrow_apr" not in legs_of(plain, [BTC], NOW)[0]["view"] and "listed_ms" not in legs_of(plain, [BTC], NOW)[0]["view"]
    consts = {"binance": {"apr": "0.05", "util": 0.8}}
    v = legs_of(plain, [BTC], NOW, borrow=consts)[0]["view"]
    assert (v["borrow_apr"], v["borrow_util"], "borrow_cap_used" in v) == ("0.05", "0.8", False)
    same_both_ways(tmp_path / "c", plain, [BTC], [NOW], LO, NOW + DAY, borrow=consts)
    with pytest.raises(ApiError):
        ir_views.require_borrow({"binance": {"util": "0.8"}})  # apr is required
    with pytest.raises(ApiError):
        ir_views.require_borrow({"nowhere": {"apr": "0.05"}})


def test_borrow_fields_are_for_perp_legs_only(tmp_path):
    spot = {"venue": "binance", "market": "spot", "symbol": "BTCUSDT-SPOT", "base": "BTC", "quote": "USDT"}
    kl = {("binance", "BTCUSDT"): fresh_1m(), ("binance", "BTCUSDT-SPOT"): bar_rows("BTCUSDT-SPOT", NOW - 12 * MIN, NOW + MIN, MIN)}
    pack = Pack.from_directory(export_dir(tmp_path, build([BTC, spot], kl, ST, lo=LO, hi=NOW + DAY)))
    fees = {"binance": {"perp": "0.0005", "spot": "0.001"}}
    legs = {x["view"]["market"]: x["view"] for x in legs_of(pack, [BTC, spot], NOW, fees=fees, borrow={"binance": {"apr": "0.05"}})}
    assert legs["perp"]["borrow_apr"] == "0.05" and "borrow_apr" not in legs["spot"]


# --- the equity leg --------------------------------------------------------------------------------------------------------

MON = utc(2026, 3, 2)  # EST: 10:00 ET = 15:00Z, 16:00 ET = 21:00Z


def equity_pack(tmp, day_ms, *, index_close="410.25", trade_close="400", hours=(13, 23)):
    lo, hi = day_ms - DAY, day_ms + DAY
    first, last = day_ms + hours[0] * HOUR, day_ms + hours[1] * HOUR
    rows = bar_rows("NVDAUSDT", first, last, MIN, close=trade_close) + bar_rows("NVDAUSDT", first, last, MIN, kind="index", close=index_close)
    built = build([NVDA_PERP], {("binance", "NVDAUSDT"): rows}, {"binance": [settle(BNID.format("NVDAUSDT"), day_ms + 8 * HOUR, "0.0001")]},
                  lo=lo, hi=hi)
    return Pack.from_directory(export_dir(tmp, built)), lo, hi


def equity_present(pack, now, fees=EQ_FEES):
    return any(x["view"]["market"] == "equity" for x in legs_of(pack, [NVDA_PERP, NVDA], now, fees=fees))


def test_the_equity_leg_is_priced_by_the_index_bar_inside_the_nyse_signal_window(tmp_path):
    pack, lo, hi = equity_pack(tmp_path, MON)
    legs = {x["view"]["market"]: x for x in legs_of(pack, [NVDA_PERP, NVDA], MON + 15 * HOUR, fees=EQ_FEES)}
    eq, perp = legs["equity"], legs["perp"]
    assert (eq["view"]["bid"], eq["view"]["ask"], eq["view"]["bbo"], eq["base"]) == ("410.25", "410.25", True, "NVDA")
    assert (eq["view"]["venue"], eq["view"]["symbol"], eq["view"]["quote_ccy"], eq["view"]["taker_fee"]) == ("ibkr", "NVDA", "USDT", "0.0003")
    assert eq["view"]["funding_rate"] is None and eq["view"]["funding_px"] is None and eq["view"]["quote_ms"] == MON + 15 * HOUR - 1
    assert perp["view"]["bid"] == "400"  # the perp leg is priced by its trade bars, the equity leg by the index bars
    # the equity leg is never given the perp-only fields
    assert not {"funding_mean_7d", "borrow_apr"} & set(eq["view"])
    # the equity leg needs fees.ibkr.equity (as a spot leg needs a spot fee)
    assert not equity_present(pack, MON + 15 * HOUR, fees={**EQ_FEES, "ibkr": {}})
    assert not equity_present(pack, MON + 15 * HOUR, fees={"binance": {"perp": "0.0005"}})


def test_equity_session_boundaries_holiday_and_early_close(tmp_path):
    # Monday 2026-03-02 (EST, UTC-5): window 10:00 ET = 15:00Z .. 15:30 ET = 20:30Z, both ends inclusive
    pack, lo, hi = equity_pack(tmp_path, MON)
    assert not equity_present(pack, MON + 15 * HOUR - 1000)       # 09:59:59 ET, pre-market
    assert not equity_present(pack, MON + 14 * HOUR + 30 * MIN)   # 09:30 ET, the open: before the window
    assert equity_present(pack, MON + 15 * HOUR)                  # 10:00:00 ET
    assert equity_present(pack, MON + 20 * HOUR + 30 * MIN)       # 15:30:00 ET, 30 minutes before the close
    assert not equity_present(pack, MON + 20 * HOUR + 30 * MIN + 1000)
    assert not equity_present(pack, MON + 21 * HOUR + 5 * MIN)    # after the close
    same_both_ways(tmp_path, pack, [NVDA_PERP, NVDA], [MON + 15 * HOUR - 1000, MON + 15 * HOUR, MON + 20 * HOUR + 30 * MIN,
                                                       MON + 20 * HOUR + 31 * MIN], lo, hi, fees=EQ_FEES)
    # daylight time: Monday 2026-03-09 is EDT (UTC-4): 10:00 ET = 14:00Z
    edt, lo2, hi2 = equity_pack(tmp_path / "edt", utc(2026, 3, 9), hours=(12, 22))
    assert not equity_present(edt, utc(2026, 3, 9, 13, 59)) and equity_present(edt, utc(2026, 3, 9, 14, 0))
    # 2026-02-16 (Presidents' Day) and a Saturday: closed
    hol, _, _ = equity_pack(tmp_path / "hol", utc(2026, 2, 16))
    assert not equity_present(hol, utc(2026, 2, 16, 16))
    sat, _, _ = equity_pack(tmp_path / "sat", utc(2026, 2, 28))
    assert not equity_present(sat, utc(2026, 2, 28, 16))
    # 2026-11-27 closes at 13:00 ET (EST): the window ends 12:30 ET = 17:30Z
    early_day = utc(2026, 11, 27)
    early, _, _ = equity_pack(tmp_path / "early", early_day, hours=(13, 22))
    assert equity_present(early, early_day + 17 * HOUR + 30 * MIN) and not equity_present(early, early_day + 17 * HOUR + 31 * MIN)
    normal, _, _ = equity_pack(tmp_path / "normal", utc(2026, 11, 30), hours=(13, 22))  # the Monday after: a normal day
    assert equity_present(normal, utc(2026, 11, 30, 17, 31)) and equity_present(normal, utc(2026, 11, 30, 20, 30))


def test_ir_calendar_matches_the_paper_monitors_tables():
    assert ir_calendar.in_window(utc(2026, 3, 2, 15, 0)) and not ir_calendar.in_window(utc(2026, 3, 2, 14, 59, 59))
    assert ir_calendar.window_bounds(dt.date(2026, 11, 27)) == (utc(2026, 11, 27, 15), utc(2026, 11, 27, 17, 30))
    assert ir_calendar.window_bounds(dt.date(2026, 7, 3)) is None and ir_calendar.window_bounds(dt.date(2026, 7, 4)) is None


def test_the_equity_leg_needs_a_reference_perp_and_uses_the_declared_reference(tmp_path):
    pack, lo, hi = equity_pack(tmp_path, MON)
    with pytest.raises(ValueError):
        legs_of(pack, [NVDA], MON + 15 * HOUR, fees=EQ_FEES)  # no declared binance perp of base NVDA
    named = {**NVDA, "base": "NVDA2", "reference": {"venue": "binance", "market": "perp", "symbol": "NVDAUSDT"}}
    legs = legs_of(pack, [NVDA_PERP, named], MON + 15 * HOUR, fees=EQ_FEES)
    assert {x["base"] for x in legs} == {"NVDA", "NVDA2"}
    with pytest.raises(ApiError):
        ir_backtest.DatasetCursor(pack, [NVDA], lo + DAY, hi, fees=EQ_FEES, work=tmp_path / "w")
    # quote defaults to the reference's when the instrument does not declare one
    bare = {"venue": "ibkr", "market": "equity", "symbol": "NVDA"}
    assert {x["view"]["quote_ccy"] for x in legs_of(pack, [NVDA_PERP, bare], MON + 15 * HOUR, fees=EQ_FEES)} == {"USDT"}


# --- pair enumeration ------------------------------------------------------------------------------------------------------

def test_equity_perp_pairs_are_enumerated_in_both_directions_with_the_equity_prefix(tmp_path):
    pack, lo, hi = equity_pack(tmp_path, MON)
    pairs = ir_views.dataset_frame_pairs(pack, [NVDA_PERP, NVDA], MON + 15 * HOUR, fees=EQ_FEES)
    assert [(p["id"], p["kind"]) for p in pairs] == [
        ("equity:NVDA:binance:NVDAUSDT>ibkr:NVDA", "equity_perp"), ("equity:NVDA:ibkr:NVDA>binance:NVDAUSDT", "equity_perp")]
    long_eq = pairs[1]
    assert (long_eq["long"]["market"], long_eq["short"]["market"]) == ("equity", "perp")
    # outside the window there is no equity leg and no pair; a different base or quote does not pair
    assert ir_views.dataset_frame_pairs(pack, [NVDA_PERP, NVDA], MON + 14 * HOUR, fees=EQ_FEES) == []
    legs = legs_of(pack, [NVDA_PERP, NVDA], MON + 15 * HOUR, fees=EQ_FEES)
    other = [dict(x, base="AAPL") if x["view"]["market"] == "equity" else x for x in legs]
    assert ir_views.pairs_of_legs(other) == []
    usdc = [dict(x, view=dict(x["view"], quote_ccy="USDC")) if x["view"]["market"] == "equity" else x for x in legs]
    assert ir_views.pairs_of_legs(usdc) == []
    # the pair is decided by an equity_perp IR: the features read the perp leg's fields and the index price
    from alphakeel_research import ir
    from tests.test_alphakeel_ir import _carry_doc

    doc = _carry_doc()
    doc["universe"].update(shape="equity_perp", pairs=[["ibkr", "binance"], ["binance", "ibkr"]])
    doc["entry"].update(conditions=[{"feature": "entry_edge", "op": ">=", "value": "-1"}], rank_by="entry_edge")
    out = ir.decide(doc, {"pairs": pairs}, {"positions": [], "cooldowns": {}}, MON + 15 * HOUR)
    assert [e["pair_id"] for e in out["entries"]] == ["equity:NVDA:binance:NVDAUSDT>ibkr:NVDA"]  # perp long / equity short: (410.25 - 400) / 400 > 0
    assert out["entries"][0]["features"]["funding_apr"] is not None


def test_universe_instruments_accepts_equity_for_equity_perp_only_and_the_request_omits_it():
    from tests.test_alphakeel_ir import _carry_doc

    doc = _carry_doc()
    doc["universe"].update(shape="equity_perp", pairs=[["ibkr", "binance"], ["binance", "ibkr"]])
    kept = ir_backtest.universe_instruments(doc, [NVDA, NVDA_PERP, BTC])
    assert [(d["venue"], d["market"]) for d in kept] == [("binance", "perp"), ("binance", "perp"), ("ibkr", "equity")]
    req = ir_backtest.dataset_request(kept, MON, MON + DAY)
    assert all(i["venue"] != "ibkr" for i in req["instruments"]) and req["dataset"]["price_kinds"] == ["trade", "index"]
    plain = ir_backtest.dataset_request([BTC], MON, MON + DAY)
    assert plain["dataset"] == {"market_tables": ["kline_1m"], "price_kinds": ["trade"]}
    cross = _carry_doc()
    assert all(d["market"] == "perp" for d in ir_backtest.universe_instruments(cross, [NVDA, NVDA_PERP, BTC]))


def test_a_margin_ir_runs_and_the_card_says_it_is_booked_as_isolated():
    from tests.test_alphakeel_ir import _carry_doc

    doc = _carry_doc()
    assert ir_backtest.margin_warnings(doc) == []
    doc["universe"].update(shape="spot_perp", pairs=[["hyperliquid", "hyperliquid"]])
    doc["sizing"]["margin"] = {"mode": "portfolio", "reduce_above": "0.6", "reduce_to": "0.4"}
    (w,) = ir_backtest.margin_warnings(doc)
    assert "isolated" in w and "AlphaKeel research service conservative profile" in w
    assert "portfolio margin (sizing.margin) and borrow interest" in ir_backtest.SIM_CONVENTIONS["not_modelled"][-1]


def test_an_equity_perp_ir_runs_end_to_end_on_the_dataset_view(tmp_path):
    from alphakeel_research import ir
    from tests.test_alphakeel_ir import _carry_doc

    pack, lo, hi = equity_pack(tmp_path / "pack", MON)
    doc = _carry_doc(max_hold_hours=1, max_loss="500", take_profit=None, conditions=[])
    doc["universe"].update(shape="equity_perp", pairs=[["ibkr", "binance"], ["binance", "ibkr"]])
    doc["entry"].update(conditions=[{"feature": "entry_edge", "op": ">=", "value": "0.01"}], rank_by="entry_edge", cooldown_minutes=600)
    doc["sizing"].update(notional_per_leg="1000", max_open=1)
    doc.pop("strategy_id", None)
    assert ir.load(doc)
    start = MON + 15 * HOUR
    card = ir_backtest.run(doc, pack, [NVDA, NVDA_PERP], start_ms=start, end_ms=start + 3 * HOUR, out_dir=tmp_path / "run", step_minutes=5,
                           fees=EQ_FEES)
    # index 410.25 vs trade 400: the perp is the long leg, the equity (ibkr) the short leg; entered once, closed after 1 h
    assert card["counts"]["opened"] == 1 and card["counts"]["closed"] == 1
    trade = [__import__("json").loads(x) for x in (tmp_path / "run" / "trades.jsonl").read_text().splitlines()][0]
    assert (trade["long"]["instrument"]["venue"], trade["short"]["instrument"]["venue"]) == ("binance", "ibkr")
    assert trade["pair_id"] == "equity:NVDA:binance:NVDAUSDT>ibkr:NVDA" and trade["short"]["entry_fee"] == "0.3076875"  # 2.5 x 410.25 x 0.0003
    assert any("equity leg (ibkr)" in a for a in card["approximations"]) and card["warnings"] == []


def test_precheck_scales_and_literals_cover_the_2026_10_18_increments():
    from alphakeel_research import precheck
    from tests.test_alphakeel_ir import _carry_doc

    doc = _carry_doc()
    doc["universe"].update(shape="spot_perp", pairs=[["hyperliquid", "hyperliquid"]])
    doc["exit"]["conditions"] = [{"feature": "funding_apr_7d", "op": "<", "value": "0", "min_hold_hours": 6}]
    doc["sizing"]["margin"] = {"mode": "portfolio", "reduce_above": "0.6", "reduce_to": "0.4"}
    lits = precheck.numeric_literals(doc)
    paths = [p for p, _ in lits]
    assert "exit.conditions[0].min_hold_hours" in paths and paths[-2:] == ["sizing.margin.reduce_above", "sizing.margin.reduce_to"]
    assert paths.index("exit.conditions[0].value") + 1 == paths.index("exit.conditions[0].min_hold_hours")
    scales = {f: precheck._zero_scale(dict(doc, entry=dict(doc["entry"], conditions=[{"feature": f, "op": ">=", "value": "0"}])),
                                      "entry.conditions[0].value") for f in
              ("funding_apr_7d", "funding_apr_30d", "entry_edge", "borrow_apr", "borrow_utilization", "borrow_cap_used",
               "funding_pos_share_30d", "listing_age_hours")}
    assert scales == {"funding_apr_7d": 1, "funding_apr_30d": 1, "entry_edge": Decimal("0.0001"), "borrow_apr": Decimal("0.0001"),
                      "borrow_utilization": Decimal("0.0001"), "borrow_cap_used": Decimal("0.0001"),
                      "funding_pos_share_30d": Decimal("0.01"), "listing_age_hours": 1}
