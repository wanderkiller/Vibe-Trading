"""G3 alignment of the Vibe-Trading dataset view and G1 precheck with AlphaKeel's research service (AK commit f63d4c5:
``dataset_view.rs``, ``dataset_pack.rs``, ``exec.rs``, ``verdict_facts.rs``, ``strategy-registry/src/verdict.rs``). Where AlphaKeel has a Rust
test of the same scenario the expected numbers are copied from it (named per test); the others state AlphaKeel's rule in the comment."""

from __future__ import annotations

import copy
from decimal import Decimal
from unittest import mock

import pytest

from alphakeel_research import ir_backtest, ir_views, precheck
from alphakeel_research.errors import ApiError
from alphakeel_research.packfile import Inst, Pack, Pit
from tests.test_alphakeel_hl_views import (BNID, BTC, DAY, HOUR, LO, MIN, NOW, ST, bar_rows, build, coarse_pack, export_dir, extend, fresh_1m,
                                           legs_of, same_both_ways, settle)
from tests.test_alphakeel_hl_review_fixes import FEES
from tests.test_alphakeel_ir import _carry_doc
from tests.test_alphakeel_ir_backtest import BN, BS, END, START, bars

BTC_INST = Inst("binance", "perp", "BTCUSDT")


def _cursor_legs(tmp_path, pack, nows, **kw):
    cur = ir_backtest.DatasetCursor(pack, [BTC], NOW - 5 * DAY, NOW + DAY, fees=FEES, work=tmp_path / "w", **kw)
    out = {}
    try:
        for now in nows:
            cur.advance(now)
            out[now] = cur.legs()
    finally:
        cur.close()
    return out


# --- 1. borrow_cap -------------------------------------------------------------------------------------------------------------
# AK ``DatasetView::parse_borrow_cap`` / ``borrow_rows``: cap_used = total borrowed / the reading's cap when that is > 0, else / the
# declared ``borrow_cap.<venue>.<TOKEN>`` (token upper-cased), else missing. Both are positive decimals; a bad shape is request.invalid.

@pytest.mark.parametrize("cap_cell, declared, want", [
    (None, {"binance": {"usdt": "4000"}}, "0.125"),    # no cap in the reading: the declared one (500 / 4000)
    ("0", {"binance": {"USDT": "2000"}}, "0.25"),      # a zero cap is no cap
    ("2000", {"binance": {"USDT": "5000"}}, "0.25"),   # the reading's own cap beats the declared one
    (None, None, None),                                # neither: missing
    (None, {"binance": {"USDC": "4000"}}, None),       # declared for another token
])
def test_g3_borrow_cap_declares_the_global_cap_where_the_reading_has_none(tmp_path, cap_cell, declared, want):
    table = [[NOW - HOUR, "lend:USDT", "0.07", "0.6", "1000", "500", cap_cell, None]]
    pack = coarse_pack(tmp_path / "p", m1=fresh_1m(), extra=[("borrow_lend", "binance", "lend", table)])
    (leg,) = legs_of(pack, [BTC], NOW, borrow_cap=declared)
    assert leg["view"].get("borrow_cap_used") == want
    assert leg["view"]["borrow_apr"] == "0.07"
    # the streaming cursor gives the same leg
    assert _cursor_legs(tmp_path, pack, [NOW], borrow_cap=declared)[NOW] == [leg]


@pytest.mark.parametrize("bad", [[], "x", {"nowhere": {"USDT": "1"}}, {"binance": {}}, {"binance": []}, {"binance": {"USDT": "0"}},
                                 {"binance": {"USDT": "-5"}}, {"binance": {"USDT": "abc"}}, {"binance": {"USDT": True}}, {"binance": {"USDT": None}}])
def test_g3_borrow_cap_with_a_bad_shape_is_request_invalid(bad):
    with pytest.raises(ApiError) as e:
        ir_views.require_borrow_cap(bad)
    assert (e.value.code, e.value.field) == ("request.invalid", "borrow_cap")


def test_g3_borrow_cap_is_part_of_the_run_identity_only_when_given():
    base = ir_backtest.run_id_of("ir-aaaaaaaaaaaaaaaaaaaaaaaa", "pk-x", 1, 2, 2, {}, Decimal(1))
    assert ir_backtest.run_id_of("ir-aaaaaaaaaaaaaaaaaaaaaaaa", "pk-x", 1, 2, 2, {}, Decimal(1), None, None) == base
    assert ir_backtest.run_id_of("ir-aaaaaaaaaaaaaaaaaaaaaaaa", "pk-x", 1, 2, 2, {}, Decimal(1), None, {"binance": {"USDT": "4000"}}) != base
    assert ir_views.require_borrow_cap({"binance": {"usdt": "4000"}}) == {"binance": {"USDT": Decimal("4000")}}


# --- 2. lend pools: request symbol "USDC", rows "lend:USDC" ------------------------------------------------------------------------
# AK ``dataset_pack::normalize`` (test r4_lend_pools_and_the_coarse_tables_are_validated_together): the pool is asked for as
# ``{venue, market: "lend", symbol: <quote currency>}`` beside the contracts, ``borrow_lend`` joins the tables, and the pack's rows
# carry the symbol ``lend:<CCY>``.

HYPE = {"venue": "hyperliquid", "market": "perp", "symbol": "HYPE", "base": "HYPE", "quote": "USDC"}


def test_g3_the_pool_is_requested_as_market_lend_with_the_bare_currency_and_read_back_as_lend_ccy(tmp_path):
    req = ir_backtest.dataset_request([HYPE], NOW, NOW + DAY, borrow_pools=True)
    assert req["instruments"] == [{"venue": "hyperliquid", "market": "perp", "symbol": "HYPE"},
                                  {"venue": "hyperliquid", "market": "lend", "symbol": "USDC"}]
    assert req["dataset"]["market_tables"] == ["kline_1m", "borrow_lend"]
    plain = ir_backtest.dataset_request([HYPE], NOW, NOW + DAY)
    assert plain["instruments"] == [{"venue": "hyperliquid", "market": "perp", "symbol": "HYPE"}] and plain["dataset"]["market_tables"] == ["kline_1m"]
    # a perp without a declared quote cannot name its pool
    with pytest.raises(ApiError) as e:
        ir_backtest.dataset_request([{k: v for k, v in HYPE.items() if k != "quote"}], NOW, NOW + DAY, borrow_pools=True)
    assert (e.value.code, e.value.field) == ("request.invalid", "instruments")
    # two perps on one venue and currency share one pool
    two = ir_backtest.dataset_request([HYPE, {**HYPE, "symbol": "ETH", "base": "ETH"}], NOW, NOW + DAY, borrow_pools=True)
    assert [i for i in two["instruments"] if i["market"] == "lend"] == [{"venue": "hyperliquid", "market": "lend", "symbol": "USDC"}]
    # the rows of the pack: object market/borrow_lend/<venue>/lend/, symbol "lend:USDC" -- read through the (venue, ccy) pair
    assert ir_views.LEND_MARKET == "lend" and Pack.borrow_inst("hyperliquid", "USDC") == Inst("hyperliquid", "lend", "lend:USDC")
    table = [[NOW - 2 * HOUR, "lend:USDC", "0.04", "0.5", "1000", "400", None, None], [NOW - HOUR, "lend:USDC", "0.06", "0.6", "1000", "500", None, None]]
    kl = {("binance", "BTCUSDT"): fresh_1m()}
    pack = Pack.from_directory(export_dir(tmp_path, extend(build([BTC], kl, ST, lo=LO, hi=NOW + DAY), [("borrow_lend", "binance", "lend", table)])))
    assert [(t, b.apr) for t, b in pack.borrow_lend("binance", "USDC", 0, NOW + 1)] == [(NOW - 2 * HOUR, Decimal("0.04")), (NOW - HOUR, Decimal("0.06"))]
    assert pack.borrow_lend("binance", "USDT", 0, NOW + 1) == []  # another currency's pool: no rows
    assert Pit(pack, NOW).borrow_lend("binance", "USDC").apr == Decimal("0.06")


def test_g3_pools_are_asked_for_only_when_the_ir_reads_borrow_and_a_perp_venue_has_no_constant():
    doc = _carry_doc()
    doc["entry"]["conditions"] = [{"feature": "borrow_apr", "op": ">=", "value": "0"}]
    assert ir_backtest.needs_pools(doc, [BN], None) is True
    assert ir_backtest.needs_pools(doc, [BN], {"binance": {"apr": "0.05"}}) is False
    assert ir_backtest.needs_pools(doc, [BN], {"okx": {"apr": "0.05"}}) is True
    assert ir_backtest.needs_pools(_carry_doc(), [BN], None) is False


# --- 3. extra view fields only for what the IR uses -------------------------------------------------------------------------------
# AK ``Leg::quote``: history / borrow / listing fields are computed (and their tables read, ``captures_with`` accesses) only when the
# IR's ``ViewCapabilities`` ask for them; an IR that uses none gets the plain frame.

def _full_pack(tmp_path):
    table = [[NOW - HOUR, "lend:USDT", "0.07", "0.6", "1000", "500", "2000", None]]
    meta = [[LO, "BTCUSDT", "0.1", "0.001", "0.001", "5", NOW - 90 * DAY, None, None]]
    st = {"binance": [settle(BNID.format("BTCUSDT"), NOW - k * 8 * HOUR, "0.0001") for k in range(1, 100)]}
    return coarse_pack(tmp_path, m1=fresh_1m(), st=st, extra=[("borrow_lend", "binance", "lend", table), ("instrument_meta", "binance", "perp", meta)])


@pytest.mark.parametrize("needs, fields", [
    (frozenset(), set()),
    (frozenset({"history"}), {"funding_mean_7d", "funding_mean_30d", "funding_pos_share_30d"}),
    (frozenset({"borrow"}), {"borrow_apr", "borrow_util", "borrow_cap_used"}),
    (frozenset({"listing"}), {"listed_ms"}),
    (frozenset({"history", "borrow", "listing"}), {"funding_mean_7d", "funding_mean_30d", "funding_pos_share_30d", "borrow_apr", "borrow_util",
                                                    "borrow_cap_used", "listed_ms"}),
])
def test_g3_only_the_view_fields_the_ir_uses_are_computed(tmp_path, needs, fields):
    pack = _full_pack(tmp_path / "p")
    extras = {"funding_mean_7d", "funding_mean_30d", "funding_pos_share_30d", "borrow_apr", "borrow_util", "borrow_cap_used", "listed_ms"}
    (leg,) = legs_of(pack, [BTC], NOW, needs=needs)
    assert {k for k in leg["view"] if k in extras} == fields
    (streamed,) = _cursor_legs(tmp_path, pack, [NOW], needs=needs)[NOW]
    assert streamed == leg


def test_g3_the_tables_of_unused_view_fields_are_not_read(tmp_path):
    pack = _full_pack(tmp_path / "p")
    boom = AssertionError("read a table the IR does not use")
    with mock.patch.object(Pack, "borrow_lend", side_effect=boom), mock.patch.object(Pack, "instrument_meta_history", side_effect=boom):
        got = _cursor_legs(tmp_path, pack, [NOW], needs=frozenset())
    assert len(got[NOW]) == 1 and "borrow_apr" not in got[NOW][0]["view"]
    # with the need the same table is read
    with mock.patch.object(Pack, "borrow_lend", side_effect=boom):
        with pytest.raises(AssertionError):
            _cursor_legs(tmp_path / "x", pack, [NOW], needs=frozenset({"borrow"}))
    # a source the IR needs but the pack lacks is the same request.invalid as in AlphaKeel ``check_sources``
    bare = coarse_pack(tmp_path / "bare", m1=fresh_1m())
    with pytest.raises(ApiError) as e:
        _cursor_legs(tmp_path / "y", bare, [NOW], needs=frozenset({"borrow"}))
    assert e.value.code == "request.invalid"


# --- 4. settlement rows are limited to t <= now ------------------------------------------------------------------------------------
# AK ``Hist::near`` ranges over ``t - 24h ..= t``: a settlement whose time is still ahead is not the latest one, however early it
# became visible; the history windows count only t < now.

def test_g3_a_settlement_ahead_of_now_is_not_the_latest_even_when_already_visible(tmp_path):
    early = settle(BNID.format("BTCUSDT"), NOW + 10 * MIN, "0.9000", available_at=NOW - MIN)  # visible before its own time
    pack = coarse_pack(tmp_path / "p", m1=fresh_1m(), st={"binance": ST["binance"] + [early]})
    got = same_both_ways(tmp_path / "w", pack, [BTC], [NOW, NOW + 11 * MIN], LO, NOW + DAY, )
    assert got[NOW][0]["view"]["funding_rate"] == "0.0001"          # the settlement of NOW - 3 h, not the one ahead
    for needs in (None, frozenset({"history"})):
        assert _cursor_legs(tmp_path / str(needs), pack, [NOW], needs=needs)[NOW][0]["view"]["funding_rate"] == "0.0001"
    assert [s.t for s in ir_views.visible_settlements(Pit(pack, NOW), BTC_INST, NOW)][-1] == NOW - 3 * HOUR


# --- 5. portfolio-margin borrow rate -----------------------------------------------------------------------------------------------
# AK ``margin_environment``: the constant ``borrow.<venue>.apr`` (source "constant"), else the highest reading of the pool of the
# venue's first declared perp inside the decision window [start, end) (source "table_max"), else request.invalid. The run records
# them as ``borrow_apr`` / ``borrow_apr_source`` (AK ``run_assumptions``).

def _margin_ir():
    doc = _carry_doc(max_hold_hours=1, max_loss="500", take_profit=None, conditions=[])
    doc["entry"]["cooldown_minutes"] = 600
    doc["sizing"].update(notional_per_leg="1000", max_open=1)
    doc["universe"].update(shape="spot_perp", pairs=[["binance", "binance"]])
    doc["sizing"]["margin"] = {"mode": "portfolio", "reduce_above": "0.6", "reduce_to": "0.4"}
    doc.pop("strategy_id", None)
    return doc


def _margin_pack(tmp_path, table):
    kl = {("binance", "BTCUSDT"): bars("BTCUSDT", lambda t: "100"), ("binance", "BTCUSDT-SPOT"): bars("BTCUSDT-SPOT", lambda t: "99.9")}
    st = {"binance": [settle("binance:linear:USDT:BTCUSDT", START - 8 * HOUR, "0.0001"), settle("binance:linear:USDT:BTCUSDT", START + 30 * MIN, "0.0001")]}
    built = build([BN, BS], kl, st)
    if table is not None:
        built = extend(built, [("borrow_lend", "binance", "lend", table)])
    return Pack.from_directory(export_dir(tmp_path, built))


def _readings(*cells):
    return [[t, "lend:USDT", apr, "0.5", "1000", "400", "2000", None] for t, apr in cells]


def test_g3_the_margin_borrow_apr_is_the_constant_else_the_highest_reading_in_the_window(tmp_path):
    table = _readings((START - 3 * HOUR, "0.50"), (START + 10 * MIN, "0.03"), (START + 40 * MIN, "0.09"), (START + 50 * MIN, "0.05"), (END, "0.99"))
    pack = _margin_pack(tmp_path, table)
    ident = {Inst("binance", "perp", "BTCUSDT"): ("BTC", "USDT")}
    # the window is [START, END): the reading before it and the one at END do not count
    assert ir_views.margin_borrow_apr(pack, [BN], ident, {}, "binance", START, END) == (Decimal("0.09"), "table_max")
    assert ir_views.margin_borrow_apr(pack, [BN], ident, {"binance": {"borrow_apr": "0.07"}}, "binance", START, END) == (Decimal("0.07"), "constant")
    # a constant for another venue does not count; no reading inside the window: request.invalid
    with pytest.raises(ApiError) as e:
        ir_views.margin_borrow_apr(pack, [BN], ident, {"okx": {"borrow_apr": "0.07"}}, "binance", START - 5 * HOUR, START - 4 * HOUR)
    assert (e.value.code, e.value.field) == ("request.invalid", "borrow")
    with pytest.raises(ApiError) as e:
        ir_views.margin_borrow_apr(_margin_pack(tmp_path / "n", None), [BN], ident, {}, "binance", START, END)
    assert e.value.code == "request.invalid"


def test_g3_a_margin_run_records_borrow_apr_and_its_source_and_refuses_a_run_without_one(tmp_path):
    doc = _margin_ir()
    table = _readings((START + 10 * MIN, "0.03"), (START + 40 * MIN, "0.09"))
    card = ir_backtest.run(doc, _margin_pack(tmp_path / "a", table), [BN, BS], start_ms=START, end_ms=END, out_dir=tmp_path / "ra",
                           fees={"binance": {"perp": "0.0005", "spot": "0.001"}})
    assert (card["assumptions"]["borrow_apr"], card["assumptions"]["borrow_apr_source"]) == ("0.09", "table_max")
    card = ir_backtest.run(doc, _margin_pack(tmp_path / "b", table), [BN, BS], start_ms=START, end_ms=END, out_dir=tmp_path / "rb",
                           fees={"binance": {"perp": "0.0005", "spot": "0.001"}}, borrow={"binance": {"apr": "0.07"}})
    assert (card["assumptions"]["borrow_apr"], card["assumptions"]["borrow_apr_source"]) == ("0.07", "constant")
    with pytest.raises(ApiError) as e:
        ir_backtest.run(doc, _margin_pack(tmp_path / "c", None), [BN, BS], start_ms=START, end_ms=END, out_dir=tmp_path / "rc",
                        fees={"binance": {"perp": "0.0005", "spot": "0.001"}})
    assert e.value.code == "request.invalid"
    # an IR without sizing.margin records neither
    plain = _margin_ir()
    plain["sizing"].pop("margin")
    card = ir_backtest.run(plain, _margin_pack(tmp_path / "d", None), [BN, BS], start_ms=START, end_ms=END, out_dir=tmp_path / "rd",
                           fees={"binance": {"perp": "0.0005", "spot": "0.001"}})
    assert "borrow_apr_source" not in card["assumptions"]


# --- 6. history reach ---------------------------------------------------------------------------------------------------------------
# AK ``check_sources``: the pack's window start must not be later than the first decision minus 31 days, else request.invalid
# (field dataset_view.start_ms); the pack is not moved back for it -- the requested window carries the 31 days.

def test_g3_the_pack_funding_start_is_not_moved_back_and_a_short_pack_is_request_invalid(tmp_path):
    req = ir_backtest.dataset_request([BTC], NOW, NOW + DAY)
    assert req["window"]["start_ms"] == NOW - ir_backtest.WARMUP_MS
    with pytest.raises(TypeError):
        ir_backtest.dataset_request([BTC], NOW, NOW + DAY, history=True)  # no history switch any more
    pack = coarse_pack(tmp_path / "p", m1=fresh_1m())  # window starts 40 days before NOW
    for start, ok in ((NOW, True), (LO + 31 * DAY, True), (LO + 31 * DAY - 1, False), (LO + DAY, False)):
        if ok:
            ir_backtest.DatasetCursor(pack, [BTC], start, NOW + DAY, fees=FEES, work=tmp_path / f"w{start}", needs=frozenset({"history"})).close()
            continue
        with pytest.raises(ApiError) as e:
            ir_backtest.DatasetCursor(pack, [BTC], start, NOW + DAY, fees=FEES, work=tmp_path / f"w{start}", needs=frozenset({"history"}))
        assert (e.value.code, e.value.field) == ("request.invalid", "dataset_view.start_ms")


# --- 7. G1 / 1.14 borrow floor of a portfolio-margin IR ---------------------------------------------------------------------------------
# AK ``borrow_floor_max`` / ``borrow_series_problem`` (tests r4_an_any_pairs_ir_checks_the_borrow_floor_of_a_venue_only_the_run_used,
# r4_borrow_series_validity_names_the_failed_condition, borrow_floor_uses_protocol_lookback_days_and_shared_venue_list).

D = 86_400_000
H = 3_600_000
NOW_B = 100 * D
IR_ANY = {"universe": {"shape": "spot_perp", "pairs": "any"}, "sizing": {"margin": {"mode": "portfolio"}}}


def _dense(apr_pct, now=NOW_B, step=3 * H, days=30):
    return [(now - k * step, Decimal(apr_pct) / 100) for k in range(0, days * 8 + 1)]


def test_g3_with_any_pairs_the_borrow_floor_covers_the_venues_of_the_research_inputs_and_of_the_run():
    protocol = precheck.load_protocol()
    series = {"binance": _dense(3), "bybit": _dense(30)}
    rob = {"inputs": {"fees": {"binance": {"perp": "0.0005"}}}}
    floor = lambda ir, **kw: precheck.borrow_floor_max(protocol, ir, rob, series, NOW_B, **kw)  # noqa: E731
    assert floor(IR_ANY) == (Decimal("0.05"), None)                                       # research inputs name binance only
    assert floor(IR_ANY, run_venues=["bybit"]) == (Decimal("0.30"), None)                 # the run also used bybit (30%)
    assert floor(IR_ANY, run_venues=["binance", "ibkr"]) == (Decimal("0.05"), None)       # ibkr is no borrow venue
    floor_none, why = floor(IR_ANY, run_venues=["okx"])                                   # a venue without a series cannot be waved through
    assert floor_none is None and "okx: no borrow-lend series observed; cannot set the borrow floor" in why
    listed = {"universe": {"shape": "spot_perp", "pairs": [["binance", "binance"]]}, "sizing": {"margin": {"mode": "portfolio"}}}
    assert floor(listed, run_venues=["bybit"]) == (Decimal("0.05"), None)                 # a listed IR ignores the run's extra venues
    assert precheck.borrow_floor_max(protocol, {"universe": {"shape": "spot_perp", "pairs": "any"}, "sizing": {}}, rob, series, NOW_B) == (None, None)
    none, why = precheck.borrow_floor_max(protocol, IR_ANY, None, series, NOW_B)          # "any" and no fee table: the venues cannot be told
    assert none is None and "cannot tell which venues" in why


def test_g3_the_borrow_series_is_valid_only_when_fresh_gap_free_and_90_percent_covered():
    now, d = 100 * 24 * H, Decimal("0.05")
    dense = [(now - k * 3 * H, d) for k in range(0, 30 * 8 + 1)]
    assert precheck.borrow_series_problem(dense, now, 30) is None
    assert "no borrow-lend point" in precheck.borrow_series_problem([], now, 30)
    assert "cover" in precheck.borrow_series_problem([(now, d)], now, 30)
    assert "old (max 6h)" in precheck.borrow_series_problem([(t - 7 * H, x) for t, x in dense], now, 30)
    gapped = [(t, x) for t, x in dense if not (now - 5 * 24 * H <= t < now - 4 * 24 * H)]
    assert "gap" in precheck.borrow_series_problem(gapped, now, 30)
    assert precheck.borrow_series_problem([(now - k * 6 * H, d) for k in range(0, 30 * 4 + 1)], now, 30) is None  # one reading per 6 h: exactly enough
    # the latest reading exactly 6 h old is still current; one millisecond more is not
    assert precheck.borrow_series_problem([(t - 6 * H, x) for t, x in dense], now, 30) is None
    assert "old (max 6h)" in precheck.borrow_series_problem([(t - 6 * H - 1, x) for t, x in dense], now, 30)


def test_g3_the_borrow_lookback_days_come_from_the_protocol_and_a_20_day_old_peak_counts_in_30_days_only():
    series = {"binance": [(t, Decimal("0.20") if t == NOW_B - 20 * D else Decimal("0.03")) for t, _ in _dense(3)]}
    rob = {"inputs": {"fees": {"binance": {"perp": "0.0005"}}}}

    def with_days(days):
        p = copy.deepcopy(precheck.load_protocol())
        for stage in p["stages"].values():
            for i in stage["items"]:
                if i["id"] == "1.14":
                    i["threshold"]["borrow_lookback_days"] = days
        return precheck.borrow_floor_max(p, IR_ANY, rob, series, NOW_B)

    assert with_days("30") == (Decimal("0.20"), None)
    assert with_days("10") == (Decimal("0.05"), None)  # the 20% peak is outside 10 days: the observed maximum 3% is below the protocol floor
    for bad in ("0", "-3", "2.5", "x"):
        floor, why = with_days(bad)
        assert floor is None and "borrow_lookback_days" in why
    assert precheck.load_protocol()["version"] == "2.7"
    thr = precheck._item_threshold(precheck.load_protocol(), "1.14")
    assert (thr["borrow_apr_floor"], thr["borrow_lookback_days"]) == ("0.05", "30")


# --- 8. duplicate settlements -------------------------------------------------------------------------------------------------------------
# AK ``r4_a_duplicated_settlement_time_keeps_the_later_visible_row_whatever_the_input_order`` (t0 = 40 d; rows (t0, visible +20 s,
# 0.003), (t0 - 1 h, visible +5 s, 0.001), (t0, visible +10 s, 0.002)): ordered by (visible, t), one row per settlement time, the
# later-visible one wins; at t0 + 15 s the earlier-visible 0.002 is in force, at t0 + 25 s 0.003, the previous settlement is t0 - 1 h.

@pytest.mark.parametrize("reverse", [False, True])
def test_g3_a_duplicated_settlement_time_keeps_the_later_visible_row_whatever_the_input_order(tmp_path, reverse):
    t0 = 40 * DAY
    sid = BNID.format("BTCUSDT")
    rows = [settle(sid, t0, "0.003", hours=1, available_at=t0 + 20_000), settle(sid, t0 - HOUR, "0.001", hours=1, available_at=t0 - HOUR + 5_000),
            settle(sid, t0, "0.002", hours=1, available_at=t0 + 10_000)]
    if reverse:
        rows.reverse()
    bars_ = bar_rows("BTCUSDT", t0 - 12 * MIN, t0 + 12 * MIN, MIN)
    built = build([BTC], {("binance", "BTCUSDT"): bars_}, {"binance": rows}, lo=t0 - 2 * DAY, hi=t0 + DAY)
    pack = Pack.from_directory(export_dir(tmp_path / "p", built))
    at = [t0 + 15_000, t0 + 25_000]
    cur = ir_backtest.DatasetCursor(pack, [BTC], t0 - DAY, t0 + DAY, fees=FEES, work=tmp_path / "w")
    try:
        for now, rate in zip(at, ("0.002", "0.003")):
            cur.advance(now)
            (streamed,) = cur.legs()
            (pit,) = legs_of(pack, [BTC], now)
            assert streamed == pit
            assert pit["view"]["funding_rate"] == rate, now
            vis = ir_views.visible_settlements(Pit(pack, now), BTC_INST, now)
            assert [s.t for s in vis] == [t0 - HOUR, t0] and vis[-1].rate == Decimal(rate)  # one row per settlement time
    finally:
        cur.close()
