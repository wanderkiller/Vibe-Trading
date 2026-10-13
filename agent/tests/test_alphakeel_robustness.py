"""``alphakeel_research.robustness``: the reliability battery and its rating rules.

Offline: the dataset packs are built with the fixture builders of ``test_alphakeel_ir_backtest`` (service object formats,
exported directory, no network). Pure pieces (rating caps, p-value, parameter paths, filter accounting) are tested on
hand-made inputs; the battery end to end on a three-coin fixture market.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from alphakeel_research import cli, ir, ir_backtest, robustness
from alphakeel_research.errors import ApiError
from alphakeel_research.packfile import Pack
from backtest import alphakeel_metrics as M
from tests.test_alphakeel_ir import _carry_doc
from tests.test_alphakeel_ir_backtest import DAY, FEES, LO, MIN, START, bars, build, export_dir, fees_file, settle

# --- rating rules ----------------------------------------------------------------------------------------------------------

GOOD = {"base_net": "100", "oos_net": "40", "cost_x2_net": "50", "coverage_partial": False, "trades_closed": 200, "days": 365,
        "funding_unpriced_fraction": "0", "filter_tested": True, "filter_p": "0.01", "synthetic_view": False, "dsr": 0.99,
        "psr": 0.999, "pbo": 0.05, "bootstrap_lower": 0.0001, "segments_positive_fraction": "1", "has_parameters": True,
        "neighbours_positive_fraction": "1", "top_coin_share": "0.2", "oos_over_is_net_per_day": "0.8",
        "walk_forward_computable": True, "walk_forward_folds": 3, "walk_forward_positive_folds": 3, "walk_forward_efficiency": "0.8"}


def test_all_components_pass_without_caps_is_a_and_the_dataset_view_caps_at_b():
    r = robustness.rate(dict(GOOD))
    assert (r["grade"], r["passed"], r["applicable"], r["caps"]) == ("A", 10, 10, [])
    r = robustness.rate({**GOOD, "synthetic_view": True})
    assert r["grade"] == "B" and [c["id"] for c in r["caps"]] == ["synthetic_view"]


@pytest.mark.parametrize("change,cap,grade", [
    ({"base_net": "0"}, "base_net_nonpositive", "F"),
    ({"base_net": "-5"}, "base_net_nonpositive", "F"),
    ({"oos_net": "0"}, "oos_net_nonpositive", "D"),
    ({"cost_x2_net": "-1"}, "cost_x2_nonpositive", "D"),
    ({"oos_net": None, "oos_over_is_net_per_day": None}, "no_oos", "C"),
    ({"coverage_partial": True}, "coverage_partial", "C"),
    ({"trades_closed": 29}, "few_trades", "C"),
    ({"days": 59}, "short_window", "C"),
    ({"funding_unpriced_fraction": "0.02"}, "funding_unpriced", "C"),
    ({"filter_p": "0.2"}, "filter_not_better_than_random", "C"),
    ({"filter_p": None}, "filter_not_better_than_random", "C"),
    ({"synthetic_view": True}, "synthetic_view", "B"),
    ({"walk_forward_positive_folds": 1}, "walk_forward_unstable", "C"),
])
def test_each_cap_is_a_ceiling(change, cap, grade):
    r = robustness.rate({**GOOD, **change})
    assert cap in [c["id"] for c in r["caps"]]
    assert r["grade"] == grade or robustness.GRADES.index(r["grade"]) > robustness.GRADES.index(grade)
    assert any(cap in x for x in r["reasons"])
    # the boundary itself does not trigger: 30 trades / 60 days / 1% unpriced / p = 0.10
    ok = robustness.rate({**GOOD, "trades_closed": 30, "days": 60, "funding_unpriced_fraction": "0.01", "filter_p": "0.05",
                          "walk_forward_positive_folds": 2})
    assert ok["caps"] == []


def test_score_grades_and_not_computable_components_count_as_failed():
    # 10 applicable, 8 pass -> 0.8 -> B; PBO and DSR missing = not demonstrated
    r = robustness.rate({**GOOD, "pbo": None, "dsr": None})
    assert (r["passed"], r["applicable"], r["score_grade"], r["grade"]) == (8, 10, "B", "B")
    assert {c["id"]: c["result"] for c in r["components"]}["pbo"] == "not_computable"
    # no parameters and no filter: those components are not applicable (and the filter cap cannot apply)
    r = robustness.rate({**GOOD, "has_parameters": False, "filter_tested": False, "filter_p": None})
    assert r["applicable"] == 8 and r["grade"] == "A"
    # everything failing but a positive base: D, never better
    bad = {**GOOD, "dsr": 0.1, "psr": 0.2, "pbo": 0.9, "bootstrap_lower": -1, "segments_positive_fraction": "0.25",
           "neighbours_positive_fraction": "0", "filter_p": "0.04", "top_coin_share": "0.9", "oos_over_is_net_per_day": "0.1"}
    assert robustness.rate(bad)["grade"] == "D"


def test_the_rule_table_is_in_the_docstring_and_in_the_output():
    for rule in robustness.RULES["caps"] + robustness.RULES["components"]:
        assert rule["id"] in robustness.__doc__, rule["id"]
    r = robustness.rate(dict(GOOD))
    assert r["rules"] == robustness.RULES and r["facts"] == GOOD


# --- permutation p-value ---------------------------------------------------------------------------------------------------

def test_permutation_p_value_is_add_one_and_counts_ties():
    D = Decimal
    assert robustness.permutation_p_value(D(5), [D(1), D(5), D(7), D(2)]) == D(3) / D(5)  # (1 + 2) / (1 + 4)
    assert robustness.permutation_p_value(D(10), [D(1)] * 19) == D("0.05")  # the best possible with 19 draws
    assert robustness.permutation_p_value(D(-1), [D(0)] * 9) == 1
    assert robustness.permutation_p_value(D(1), []) is None


# --- parameter paths -------------------------------------------------------------------------------------------------------

def _param_doc():
    doc = _carry_doc(max_hold_hours=4, max_loss="500", take_profit=None, conditions=[{"feature": "funding_hourly_net", "op": "<", "value": "0"}])
    doc["entry"]["conditions"] = [{"feature": "funding_hourly_net", "op": ">=", "value": "0.00001"},
                                  {"feature": "spread", "op": "between", "value": ["0", "0.01"]}]
    doc["parameters"] = {"min_f": {"value": "0.00001", "robust": ["0", "0.0001"], "path": "entry.conditions[0].value"},
                         "spread_lo": {"value": "0", "robust": ["0", "0.005"], "path": "entry.conditions[1].value[0]"},
                         "hold": {"value": "4", "robust": ["2", "8"], "path": "exit.max_hold_hours"}}
    ir.load(doc)
    return doc


def test_set_path_and_neighbour_doc_edit_the_literal_and_the_parameter_value():
    doc = _param_doc()
    base_id = ir.load(doc).strategy_id
    n = robustness.neighbour_doc(doc, "min_f", "0.0001")
    assert n["entry"]["conditions"][0]["value"] == "0.0001" and n["parameters"]["min_f"]["value"] == "0.0001"
    assert ir.load(n).strategy_id != base_id and doc["entry"]["conditions"][0]["value"] == "0.00001"  # input untouched
    n = robustness.neighbour_doc(doc, "spread_lo", "0.005")
    assert n["entry"]["conditions"][1]["value"] == ["0.005", "0.01"]
    n = robustness.neighbour_doc(doc, "hold", "8")
    assert n["exit"]["max_hold_hours"] == 8 and isinstance(n["exit"]["max_hold_hours"], int)
    with pytest.raises(ValueError):
        robustness.neighbour_doc(doc, "hold", "2.5")
    with pytest.raises(KeyError):
        robustness.set_path(doc, "entry.conditions[9].value", "1")
    with pytest.raises(KeyError):
        robustness.set_path(doc, "universe.excluded_bases[0]", "X")  # only entry/exit/sizing/assumptions
    # a neighbour that breaks a cross-field rule (between lo > hi) is not a valid IR
    with pytest.raises(ir.IrError):
        robustness.neighbour_doc({**doc, "parameters": {**doc["parameters"], "spread_lo": {"value": "0", "robust": ["0", "0.02"],
                                                                                           "path": "entry.conditions[1].value[0]"}}},
                                 "spread_lo", "0.02")


def test_scale_fees_multiplies_every_rate_exactly():
    assert robustness.scale_fees({"binance": {"perp": "0.0004", "spot": "0.001"}}, "1.5") == {"binance": {"perp": "0.0006", "spot": "0.0015"}}
    with pytest.raises(ApiError, match="explicit fee table"):  # no default table to scale (D2-13)
        robustness.scale_fees(None, "2")


# --- filter accounting -----------------------------------------------------------------------------------------------------

def _tr(n, base, opened, realized, price, funding, fee_each, closed=True):
    leg = {"price_pnl": price if closed else None, "funding": funding, "entry_fee": fee_each, "exit_fee": fee_each if closed else "0"}
    zero = {"price_pnl": "0" if closed else None, "funding": "0", "entry_fee": "0", "exit_fee": "0"}
    return {"trade": n, "pair_id": f"cross:{base}:a>b", "base": base, "opened_ms": opened, "closed_ms": opened + 1 if closed else None,
            "status": "closed" if closed else "open", "realized": realized if closed else None, "long": leg, "short": zero}


def test_filter_accounting_splits_selection_knock_on_and_residual():
    # unfiltered: BTC +5, ETH -7 (price -6, funding +1, fees 2 x 1), SOL +3, ETH open; filtered: BTC +5 (same), SOL +3 at
    # another time (knock-on: the old SOL trade is removed, a new one added), XRP +1 (added)
    off = [_tr(1, "BTC", 0, "5", "7", "0", "1"), _tr(2, "ETH", 0, "-7", "-6", "1", "1"), _tr(3, "SOL", 10, "3", "5", "0", "1"),
           _tr(4, "ETH", 20, None, None, "0", "1", closed=False)]
    on = [_tr(1, "BTC", 0, "5", "7", "0", "1"), _tr(2, "SOL", 12, "3", "5", "0", "1"), _tr(3, "XRP", 13, "1", "3", "0", "1")]
    a = robustness.filter_accounting(off, on, ["ETH"], Decimal("1.5"), Decimal("9"))
    assert a["improvement"] == "7.5"
    assert (a["blocked"]["count"], a["blocked"]["closed"], a["blocked"]["open"], a["blocked"]["net"]) == (2, 1, 1, "-7")
    assert a["blocked"]["net_by_base"] == {"ETH": "-7"}
    assert (a["selection_contribution"], a["price_impact"], a["funding_impact"], a["cost_savings"]) == ("7", "6", "-1", "2")
    assert Decimal(a["price_impact"]) + Decimal(a["funding_impact"]) + Decimal(a["cost_savings"]) == Decimal(a["selection_contribution"])
    assert (a["knock_on_added"]["count"], a["knock_on_added"]["net"]) == (2, "4")
    assert (a["knock_on_removed"]["count"], a["knock_on_removed"]["net"]) == (1, "3")
    # 7.5 = 7 + 4 - 3 + residual
    assert a["residual_open_positions"] == "-0.5"


def test_segment_and_month_nets_telescope_to_the_final_equity():
    days = ["2023-01-30", "2023-01-31", "2023-02-01", "2023-02-02", "2023-02-03"]
    eq = [Decimal(x) for x in ("1", "-1", "2", "4", "3")]
    segs = robustness.segment_nets(days, eq, 2)
    assert [s["days"] for s in segs] == [2, 3] and [s["net"] for s in segs] == ["-1", "4"]
    assert robustness.month_nets(days, eq) == [{"month": "2023-01", "net": "-1"}, {"month": "2023-02", "net": "4"}]


def test_stationary_bootstrap_is_seeded_and_brackets_the_mean():
    x = [0.001 * ((i * 7) % 5 - 1) for i in range(200)]
    a = M.stationary_bootstrap_mean_ci(x, seed=3, resamples=300)
    assert a == M.stationary_bootstrap_mean_ci(x, seed=3, resamples=300)
    assert a["ci_lower"] <= a["mean"] <= a["ci_upper"] and a["block_days"] == M.block_length(200)
    assert M.stationary_bootstrap_mean_ci([1.0], seed=1) is None


# --- the battery end to end ------------------------------------------------------------------------------------------------

H6 = 6 * 3_600_000
HI = START + 2 * H6
COINS = {"BTC": ("BTCUSDT", "BTC-USDT-SWAP", 100, "0.01"), "ETH": ("ETHUSDT", "ETH-USDT-SWAP", 50, "-0.01"),
         "SOL": ("SOLUSDT", "SOL-USDT-SWAP", 20, "0.005")}


def _trend(base, slope):
    """Binance price base + slope per minute: the long Binance leg of BTC and SOL gains, ETH's loses on every trade."""
    def f(t):
        return ir.fmt(Decimal(base) + Decimal(slope) * ((t - LO) // MIN))
    return f


def _market(tmp_path, hi=HI):
    insts, kl, st = [], {}, {"binance": [], "okx": []}
    for b, (bn, ok, px, slope) in COINS.items():
        insts += [{"venue": "binance", "market": "perp", "symbol": bn, "base": b, "quote": "USDT"},
                  {"venue": "okx", "market": "perp", "symbol": ok, "base": b, "quote": "USDT"}]
        kl[("binance", bn)] = bars(bn, _trend(px, slope), hi=hi)
        kl[("okx", ok)] = bars(ok, lambda t, px=px: str(px), hi=hi)
        for k in range(-1, 4):  # 8-hourly settlements: Binance pays longs, OKX pays shorts -> long Binance / short OKX
            st["binance"].append(settle(f"binance:linear:USDT:{bn}", START + k * 8 * 3_600_000, "-0.0001"))
            st["okx"].append(settle(f"okx:linear:USDT:{ok}", START + k * 8 * 3_600_000, "0.0003"))
    insts.sort(key=lambda d: (d["venue"], d["market"], d["symbol"]))  # the request's order (`ir_backtest.universe_instruments`)
    pd = export_dir(tmp_path, build(insts, kl, st, lo=LO, hi=hi))
    return insts, pd


def _ir():
    doc = _carry_doc(max_hold_hours=1, max_loss="500", take_profit=None, conditions=[])
    doc["entry"]["cooldown_minutes"] = 30
    doc["sizing"]["max_open"] = 3
    doc["universe"]["excluded_bases"] = []
    doc["parameters"] = {"hold": {"value": "1", "robust": ["1", "2"], "path": "exit.max_hold_hours"}}
    doc.pop("strategy_id", None)
    ir.load(doc)
    return doc


def test_battery_end_to_end_counts_trials_honestly_and_tests_the_filter(tmp_path):
    insts, pd = _market(tmp_path)
    pack = Pack.from_directory(pd)
    out = tmp_path / "rb"
    d = _ir()
    d["entry"]["rank_by"] = "net_conservative"  # reads basis_stress: basis-x2 is a real stress (else not_applicable)
    doc = robustness.battery(d, pack, insts, is_window=(START, START + H6), oos_window=(START + H6, HI), fees=FEES, out_dir=out,
                             filter_spec={"kind": "excluded_bases", "bases": ["ETH"]}, permutations=4, seed=11, step_minutes=2,
                             workers=1)
    assert doc["schema"] == "vibe-trading.ir-robustness/2" and (out / "robustness.md").is_file() and (out / "trials.jsonl").is_file()
    labels = {r["label"]: r for r in doc["runs"]}
    assert {"base", "cost-x1.5", "cost-x2", "basis-x2", "param-hold-hi", "oos", "filter-off", "filter-on"} <= set(labels)
    assert "param-hold-lo" not in labels and doc["parameters"]["skipped"][0]["reason"] == "equals the base value"
    # filter-off is the base IR itself (no excluded bases): run once, referenced
    assert labels["filter-off"]["same_as"] == "base" and labels["filter-off"]["strategy_id"] == labels["base"]["strategy_id"]
    # honest trial count: distinct candidate IRs = base, basis-x2, param-hold-hi, filter-on (fee stress = same id; perms not counted)
    assert doc["trials"]["count"] == 4 and doc["trials"]["permutation_draws_not_counted"] == 4
    assert doc["statistics"]["alphakeel_metrics"]["trials"]["count"] == 4
    trials = [json.loads(x) for x in (out / "trials.jsonl").read_text().splitlines()]
    assert len(trials) == len(doc["runs"]) and {t["strategy_id"] for t in trials if t["counted_trial"]} == set(doc["trials"]["candidate_strategy_ids"])
    # the base daily series is the run card's: the same Sharpe
    base_card = json.loads((out / "runs" / "base" / "run_card.json").read_text())
    assert doc["statistics"]["alphakeel_metrics"]["sharpe_daily"] == base_card["alphakeel_metrics"]["sharpe_daily"]
    assert base_card["assumptions"]["capital_base"] == doc["inputs"]["capital_base"] == "6000"
    # every variant shares the capital base (a parameter could change notional or max_open)
    hold = json.loads((out / "runs" / "param-hold-hi" / "run_card.json").read_text())
    assert hold["assumptions"]["capital_base"] == "6000" and hold["assumptions"]["capital_base_source"] == "given"
    # cost stress: fees scale and the net falls by exactly the extra fees when trades are identical
    c2 = json.loads((out / "runs" / "cost-x2" / "run_card.json").read_text())
    assert c2["assumptions"]["fees"]["okx"] == {"perp": "0.001"}
    if c2["counts"]["closed"] == base_card["counts"]["closed"]:
        fills = 4 * (c2["counts"]["closed"] + c2["counts"]["open_at_end"])  # each fill's fee is rounded to 1e-8 on its own
        assert abs(Decimal(c2["amounts"]["fees"]) - 2 * Decimal(base_card["amounts"]["fees"])) <= fills * Decimal("0.00000001")
    # the filter: ETH (its long Binance leg loses on every trade) is blocked
    f = doc["filter"]
    off = [json.loads(x) for x in (out / "runs" / "base" / "trades.jsonl").read_text().splitlines()]
    eth = [t for t in off if t["base"] == "ETH"]
    assert f["blocked"]["count"] == len(eth) > 0
    assert Decimal(f["blocked"]["net"]) == sum(Decimal(t["realized"]) for t in eth if t["status"] == "closed")
    assert Decimal(f["improvement"]) == Decimal(f["selection_contribution"]) + Decimal(f["knock_on_added"]["net"]) \
        - Decimal(f["knock_on_removed"]["net"]) + Decimal(f["residual_open_positions"])
    assert Decimal(f["selection_contribution"]) > 0 and all(t["base"] != "ETH" for t in
                                                             (json.loads(x) for x in (out / "runs" / "filter-on" / "trades.jsonl").read_text().splitlines()))
    pm = f["permutation"]
    assert pm["pool"] == ["BTC", "ETH", "SOL"] and len(pm["draws_detail"]) == 4
    imps = [Decimal(d["improvement"]) for d in pm["draws_detail"]]
    assert Decimal(pm["p_value"]) == robustness.permutation_p_value(Decimal(f["improvement"]), imps)
    # OOS and sub-periods present; the grade obeys the caps (synthetic view, < 60 days, < 30 trades)
    assert doc["oos"]["days"] >= 1 and doc["subperiods"]["segments"]
    caps = {c["id"] for c in doc["rating"]["caps"]}
    assert {"synthetic_view", "short_window", "few_trades"} <= caps and doc["rating"]["grade"] in ("C", "D", "F")
    # deterministic: the same seed draws the same sets
    again = robustness.battery(d, pack, insts, is_window=(START, START + H6), fees=FEES, out_dir=tmp_path / "rb2",
                               filter_spec={"kind": "excluded_bases", "bases": ["ETH"]}, permutations=4, seed=11, step_minutes=2, workers=1)
    assert [d["bases"] for d in again["filter"]["permutation"]["draws_detail"]] == [d["bases"] for d in pm["draws_detail"]]
    assert "no_oos" in {c["id"] for c in again["rating"]["caps"]}


def test_parameters_and_basis_stress_inert_on_the_dataset_view_are_skipped_not_passed(tmp_path):
    """The dataset view makes quote_age_ms / leg_skew_ms / spread / price_estimated constant, and basis_stress only moves
    stress / net_conservative: neighbours of such a parameter, or basis-x2 of an IR that reads neither, rerun the base
    strategy. They are skipped with the reason (not counted as passing neighbours, PBO candidates or trials)."""
    insts, pd = _market(tmp_path)
    pack = Pack.from_directory(pd)
    d = _ir()
    d["entry"]["conditions"] = [{"feature": "quote_age_ms", "op": "<=", "value": "5000"}]
    d["parameters"]["max_age"] = {"value": "5000", "robust": ["4000", "6000"], "path": "entry.conditions[0].value"}
    assert d["entry"]["rank_by"] == "funding_hourly_net" and Decimal(d["assumptions"]["basis_stress"]) > 0
    ir.load(d)
    out = tmp_path / "rb"
    doc = robustness.battery(d, pack, insts, is_window=(START, START + H6), fees=FEES, out_dir=out, permutations=0, step_minutes=2,
                             workers=1)
    labels = {r["label"] for r in doc["runs"]}
    assert "param-hold-hi" in labels and not {"param-max_age-lo", "param-max_age-hi", "basis-x2"} & labels
    skipped = {(s["parameter"], s["side"]): s["reason"] for s in doc["parameters"]["skipped"]}
    assert skipped[("max_age", "lo")].startswith("inert on the dataset view: quote_age_ms") and ("max_age", "hi") in skipped
    assert [x["parameter"] for x in doc["parameters"]["inert"]] == ["max_age"]
    assert [x["parameter"] for x in doc["parameters"]["neighbours"]] == ["hold"]
    b = doc["cost_stress"]["basis_stress_x2"]
    assert b["result"] == "not_applicable" and b["net"] is None and b["over_base"] is None
    assert doc["trials"]["distinct_candidate_variants"] == 2  # base and param-hold-hi only
    from alphakeel_research import precheck
    assert {"max_age:hi", "max_age:lo"} <= set(precheck.missing_neighbour_sides(doc))  # G1 1.9 does not pass an untested parameter
    card = json.loads((out / "runs" / "base" / "run_card.json").read_text())
    assert any(w.startswith("parameter max_age (entry.conditions[0].value) is inert") for w in card["warnings"])
    assert "not applicable" in (out / "robustness.md").read_text()


def test_battery_cli_with_process_workers_matches_in_process(tmp_path, capsys):
    insts, pd = _market(tmp_path)
    (tmp_path / "ir.json").write_text(json.dumps(_ir()))
    (tmp_path / "inst.json").write_text(json.dumps(insts))
    out = tmp_path / "cli"
    rc = cli.main(["ir", "robustness", "--no-register", "--fees", fees_file(tmp_path), "--ir", str(tmp_path / "ir.json"), "--pack-dir", str(pd), "--instruments", str(tmp_path / "inst.json"),
                   "--is-start-ms", str(START), "--is-end-ms", str(START + H6), "--permutations", "0", "--workers", "2",
                   "--step-minutes", "2", "--out", str(out)])
    assert rc == 0
    printed = json.loads(capsys.readouterr().out)
    doc = json.loads((out / "robustness.json").read_text())
    assert printed["grade"] == doc["rating"]["grade"] and doc["inputs"]["execution"] == "process pool (spawn)"
    ser = robustness.battery(_ir(), Pack.from_directory(pd), insts, is_window=(START, START + H6), fees=FEES, out_dir=tmp_path / "ser",
                             permutations=0, step_minutes=2, workers=1)
    assert [(r["label"], r["net"]) for r in ser["runs"]] == [(r["label"], r["net"]) for r in doc["runs"]]
    assert ser["rating"]["grade"] == doc["rating"]["grade"]
    md = (out / "robustness.md").read_text()
    assert "Not tested" in md and "latency" in md and "Rule table" in md


def test_battery_refuses_bad_inputs(tmp_path):
    insts, pd = _market(tmp_path)
    pack = Pack.from_directory(pd)
    with pytest.raises(ApiError, match="filter"):
        robustness.battery(_ir(), pack, insts, is_window=(START, START + H6), fees=FEES, out_dir=tmp_path / "x", filter_spec={"kind": "x"}, workers=1)
    with pytest.raises(ApiError, match="filter"):
        robustness.battery(_ir(), pack, insts, is_window=(START, START + H6), fees=FEES, out_dir=tmp_path / "x",
                           filter_spec={"kind": "excluded_bases", "bases": ["eth"]}, workers=1)
    with pytest.raises(ApiError, match="overlaps"):
        robustness.battery(_ir(), pack, insts, is_window=(START, START + H6), oos_window=(START + H6 - DAY // 24, HI),
                           fees=FEES, out_dir=tmp_path / "x", workers=1)
    with pytest.raises(ApiError, match="permutations"):
        robustness.battery(_ir(), pack, insts, is_window=(START, START + H6), fees=FEES, out_dir=tmp_path / "x", permutations=-1, workers=1)
    assert ir_backtest.DAY_MS == DAY and MIN == 60_000


# --- anchored walk-forward -----------------------------------------------------------------------------------------------

def test_walk_forward_rating_cap_and_component():
    r = robustness.rate({**GOOD, "walk_forward_positive_folds": 2})  # 2 of 3 = exactly 2/3: no cap
    assert "walk_forward_unstable" not in [c["id"] for c in r["caps"]]
    r = robustness.rate({**GOOD, "walk_forward_positive_folds": 1})
    assert [c["id"] for c in r["caps"]] == ["walk_forward_unstable"] and r["grade"] == "C"
    comp = lambda f: {c["id"]: c["result"] for c in robustness.rate(f)["components"]}["walk_forward_efficiency"]  # noqa: E731
    assert comp({**GOOD, "walk_forward_efficiency": "0.5"}) == "pass"
    assert comp({**GOOD, "walk_forward_efficiency": "0.49999999"}) == "fail"
    assert comp({**GOOD, "walk_forward_efficiency": None}) == "not_computable"
    # not computable: no cap, but the component counts as failed (like every other one)
    nc = robustness.rate({**GOOD, "walk_forward_computable": False, "walk_forward_folds": 0, "walk_forward_positive_folds": None})
    assert "walk_forward_unstable" not in [c["id"] for c in nc["caps"]] and (nc["passed"], nc["applicable"]) == (9, 10)
    assert {c["id"]: c["result"] for c in nc["components"]}["walk_forward_efficiency"] == "not_computable"


def test_walk_forward_plan_folds_are_the_last_days_of_the_is_window_and_anchored():
    p = robustness.walk_forward_plan((0, 120 * DAY), 3, 30, 30)
    assert p["computable"] and [(f["train"], f["test"]) for f in p["folds"]] == [
        ((0, 30 * DAY), (30 * DAY, 60 * DAY)), ((0, 60 * DAY), (60 * DAY, 90 * DAY)), ((0, 90 * DAY), (90 * DAY, 120 * DAY))]
    q = robustness.walk_forward_plan((0, 119 * DAY), 3, 30, 30)  # first training segment 29 days
    assert not q["computable"] and "wf_min_train_days" in q["reason"]
    assert not robustness.walk_forward_plan((0, 500 * DAY), 0, 30, 30)["computable"]
    assert not robustness.walk_forward_plan((0, 500 * DAY), 3, True, 30)["computable"]


def test_walk_forward_grid_cartesian_then_one_at_a_time():
    doc = _param_doc()
    grid, kind = robustness.walk_forward_grid(doc)  # 3 x 2 x 3 = 18 points, first = all base
    assert kind == "cartesian" and len(grid) == 18 and grid[0]["parameters"] == {"hold": "4", "min_f": "0.00001", "spread_lo": "0"}
    assert grid[0]["doc"] is doc and all(g["doc"] is not None for g in grid)
    assert grid[1]["parameters"]["spread_lo"] == "0.005" and grid[1]["doc"]["entry"]["conditions"][1]["value"] == ["0.005", "0.01"]
    doc["parameters"]["spread_hi"] = {"value": "0.01", "robust": ["0.006", "0.02"], "path": "entry.conditions[1].value[1]"}
    ir.load(doc)
    grid, kind = robustness.walk_forward_grid(doc)  # 54 > 27: base + (2 + 1 + 2 + 2) one-at-a-time points
    assert kind == "one_at_a_time" and len(grid) == 8
    assert sum(1 for g in grid[1:] if g["parameters"] != grid[0]["parameters"]) == 7
    assert all(sum(1 for k in g["parameters"] if g["parameters"][k] != grid[0]["parameters"][k]) <= 1 for g in grid)
    # a point that is not a valid IR (spread_lo above spread_hi) is skipped and recorded, not fatal
    doc["parameters"]["spread_hi"]["robust"] = ["0", "0.02"]
    doc["parameters"]["spread_lo"]["robust"] = ["0", "0.012"]
    doc["parameters"]["hold"]["robust"] = ["4", "4"]
    doc["parameters"]["min_f"]["robust"] = ["0.00001", "0.00001"]
    bad, _ = robustness.walk_forward_grid(doc)
    assert [g["parameters"]["spread_lo"] + "/" + g["parameters"]["spread_hi"] for g in bad if g["skipped"]] == ["0.012/0.01", "0.012/0"]


def test_dsr_inputs_reproduce_dsr_exactly():
    x = [0.001 * ((i * 7) % 11 - 4) + 0.0002 * i / 100 for i in range(120)]
    vs = [0.05, 0.12, -0.03, 0.2]
    i = M.dsr_inputs(x, 9, vs)
    assert set(i) == {"sharpe_daily", "skewness", "kurtosis_non_excess", "effective_days", "var_sr_used", "trials"} and i["trials"] == 9
    # the battery publishes the series itself (JSON round trip): sharpe / skew / kurtosis recompute from it with the same functions
    pub = json.loads(json.dumps({**i, "daily_returns": [r for r in x]}))
    assert pub["daily_returns"] == x and M.sharpe(pub["daily_returns"]) == i["sharpe_daily"]
    assert M.moments(pub["daily_returns"]) == (i["skewness"], i["kurtosis_non_excess"])
    assert M.effective_sample_size(pub["daily_returns"]) == i["effective_days"]
    assert M.psr(i["sharpe_daily"], M.sr0(i["var_sr_used"], 9), i["effective_days"], i["skewness"], i["kurtosis_non_excess"]) == M.dsr(x, 9, vs)
    assert i["var_sr_used"] >= M.statistics.variance(vs) and M.dsr_inputs([0.0] * 10, 3) is None and M.dsr([0.0] * 10, 3) is None


WF_DAYS = 4
WF_HI = START + WF_DAYS * DAY
WF_COINS = {"BTC": ("BTCUSDT", "BTC-USDT-SWAP", 100, "0.002"), "ETH": ("ETHUSDT", "ETH-USDT-SWAP", 200, "-0.002")}


def _wf_market(tmp_path):
    insts, kl, st = [], {}, {"binance": [], "okx": []}
    for b, (bn, ok, px, slope) in WF_COINS.items():
        insts += [{"venue": "binance", "market": "perp", "symbol": bn, "base": b, "quote": "USDT"},
                  {"venue": "okx", "market": "perp", "symbol": ok, "base": b, "quote": "USDT"}]
        kl[("binance", bn)] = bars(bn, _trend(px, slope), hi=WF_HI)
        kl[("okx", ok)] = bars(ok, lambda t, px=px: str(px), hi=WF_HI)
        for k in range(-1, WF_DAYS * 3 + 1):
            st["binance"].append(settle(f"binance:linear:USDT:{bn}", START + k * 8 * 3_600_000, "-0.0001"))
            st["okx"].append(settle(f"okx:linear:USDT:{ok}", START + k * 8 * 3_600_000, "0.0003"))
    return insts, export_dir(tmp_path, build(insts, kl, st, lo=LO, hi=WF_HI))


def test_walk_forward_battery_vector_fixed_seed(tmp_path):
    insts, pd = _wf_market(tmp_path)
    out = tmp_path / "wf"
    doc = robustness.battery(_ir(), Pack.from_directory(pd), insts, is_window=(START, WF_HI), fees=FEES, out_dir=out, permutations=0, seed=5,
                             step_minutes=10, workers=1, wf_folds=2, wf_test_days=1, wf_min_train_days=1)
    assert doc["schema"] == "vibe-trading.ir-robustness/2"
    wf = doc["walk_forward"]
    assert wf["computable"] and wf["config"] == {"folds": 2, "test_days": 1, "min_train_days": 1, "grid": "cartesian", "grid_size": 2}
    # fold boundaries: the last 2 UTC days are the test segments, training is anchored at the IS start
    assert [(f["k"], f["train"]["start_ms"], f["train"]["end_ms"], f["train"]["days"], f["test"]["start_ms"], f["test"]["end_ms"], f["test"]["days"])
            for f in wf["folds"]] == [(1, START, START + 2 * DAY, 2, START + 2 * DAY, START + 3 * DAY, 1),
                                      (2, START, START + 3 * DAY, 3, START + 3 * DAY, START + 4 * DAY, 1)]
    nets = lambda lbl: Decimal(json.loads((out / "runs" / lbl / "run_card.json").read_text())["amounts"]["equity_end"])  # noqa: E731
    for f in wf["folds"]:
        k = f["k"]
        a, b = nets(f"wf{k}-train-g01"), nets(f"wf{k}-train-g02")  # hold = 1 (base), hold = 2
        sel_hold, sel_net = ("1", a) if a >= b else ("2", b)  # a tie keeps the base point
        assert f["selected"]["parameters"] == {"hold": sel_hold} and Decimal(f["selected"]["train_net"]) == sel_net == max(a, b)
        assert Decimal(f["test_net"]) == nets(f"wf{k}-test")
        assert f["skipped"] == []
    tn = sum(Decimal(f["test_net"]) for f in wf["folds"])
    rn = sum(Decimal(f["selected"]["train_net"]) for f in wf["folds"])
    assert wf["positive_folds"] == sum(1 for f in wf["folds"] if Decimal(f["test_net"]) > 0)
    # the fixture is a losing one: every fold's training net and test net are negative, so there is no efficiency (rn <= 0)
    assert rn < 0 and tn < 0 and wf["efficiency"] is None and wf["positive_folds"] == 0
    assert [f["test_net"] for f in wf["folds"]] == ["-29.07568689476741", "-29.74226251601734"]
    assert [(f["selected"]["parameters"], f["selected"]["train_net"]) for f in wf["folds"]] == [
        ({"hold": "2"}, "-56.36477881531186"), ({"hold": "2"}, "-80.68876955046182")]
    facts = doc["rating"]["facts"]
    assert (facts["walk_forward_computable"], facts["walk_forward_folds"], facts["walk_forward_efficiency"]) == (True, 2, wf["efficiency"])
    # the training grids are runs in trials.jsonl but never DSR candidates
    trials = [json.loads(x) for x in (out / "trials.jsonl").read_text().splitlines()]
    w = [t for t in trials if t["role"] == "walk_forward"]
    assert len(w) == 6 and not any(t["counted_trial"] for t in w) and doc["trials"]["walk_forward_runs_not_counted"] == 6
    di = doc["statistics"]["dsr_inputs"]
    assert M.sharpe(di["daily_returns"]) == di["sharpe_daily"] and M.moments(di["daily_returns"]) == (di["skewness"], di["kurtosis_non_excess"])
    # base, param-hold-hi; basis-x2 is not applicable (the IR reads no stress feature: it would rerun the base strategy)
    assert doc["trials"]["count"] == 2 and doc["trials"]["count"] == doc["statistics"]["dsr_inputs"]["trials"]
    assert doc["cost_stress"]["basis_stress_x2"]["result"] == "not_applicable"
    # dsr_inputs reproduce the reported DSR; the walk-forward table is rendered
    i = doc["statistics"]["dsr_inputs"]
    assert M.psr(i["sharpe_daily"], M.sr0(i["var_sr_used"], i["trials"]), i["effective_days"], i["skewness"],
                 i["kurtosis_non_excess"]) == doc["statistics"]["alphakeel_metrics"]["dsr"]
    assert "## Walk-forward (anchored)" in (out / "robustness.md").read_text()


def test_walk_forward_not_computable_on_a_short_window(tmp_path):
    insts, pd = _market(tmp_path)
    doc = robustness.battery(_ir(), Pack.from_directory(pd), insts, is_window=(START, START + H6), fees=FEES, out_dir=tmp_path / "rb", permutations=0,
                             step_minutes=2, workers=1)
    wf = doc["walk_forward"]
    assert wf["computable"] is False and "wf_min_train_days" in wf["reason"] and wf["config"] == {"folds": 3, "test_days": 30, "min_train_days": 30}
    comp = {c["id"]: c["result"] for c in doc["rating"]["components"]}
    assert comp["walk_forward_efficiency"] == "not_computable" and "walk_forward_unstable" not in [c["id"] for c in doc["rating"]["caps"]]
    assert "Not computable" in (tmp_path / "rb" / "robustness.md").read_text()


# --- `ir robustness --run`: the battery inside a VT run directory, then export picks it up ----------------------------------------

def test_robustness_run_defaults_come_from_the_run_and_export_carries_the_evidence(tmp_path, capsys):
    from alphakeel_research import workflow

    insts, pd = _market(tmp_path, hi=START + H6)  # `ir backtest` wants a pack for exactly its window
    (tmp_path / "ir.json").write_text(json.dumps(_ir()))
    (tmp_path / "inst.json").write_text(json.dumps(insts))
    run = tmp_path / "run"
    assert cli.main(["ir", "backtest", "--fees", fees_file(tmp_path), "--no-register", "--ir", str(tmp_path / "ir.json"), "--start-ms", str(START), "--end-ms", str(START + H6), "--instruments",
                     str(tmp_path / "inst.json"), "--pack-dir", str(pd), "--step-minutes", "2", "--trials", "1", "--out", str(run)]) == 0
    capsys.readouterr()
    base = ["ir", "robustness", "--no-register", "--run", str(run), "--permutations", "0", "--workers", "1", "--step-minutes", "2"]
    # explicit arguments must agree with the run card
    assert cli.main([*base, "--is-end-ms", str(START + H6 - 1)]) == 1
    assert "differs from the run card" in capsys.readouterr().out
    other = _ir()
    other["entry"]["cooldown_minutes"] = 45
    (tmp_path / "other.json").write_text(json.dumps(other))
    assert cli.main([*base, "--ir", str(tmp_path / "other.json")]) == 1
    assert "run card is for" in capsys.readouterr().out
    assert cli.main(["ir", "robustness", "--no-register", "--ir", str(tmp_path / "ir.json")]) == 1  # no --run: the old arguments are required
    assert "--pack-dir" in capsys.readouterr().out
    assert cli.main([*base, "--is-start-ms", str(START), "--is-end-ms", str(START + H6)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["json"] == str(run / "robustness" / "robustness.json")
    doc = json.loads((run / "robustness" / "robustness.json").read_text())
    assert doc["inputs"]["is_window"] == {"start_ms": START, "end_ms": START + H6} and doc["inputs"]["pack_id"] == Pack.from_directory(pd).id
    assert doc["inputs"]["fees_source"] == f"run_card:{run / 'run_card.json'}"  # R2-D-4 (a): where the inherited table came from
    # export with no flags finds RUN/robustness
    exp = tmp_path / "exp"
    r = workflow.ir_export(_ir(), run, exp)
    pr = r["provenance"]["robustness"]
    assert pr["grade"] == doc["rating"]["grade"] and pr["schema"] == robustness.SCHEMA
    assert (exp / "evidence" / "robustness.json").read_bytes() == (run / "robustness" / "robustness.json").read_bytes()
    assert (exp / "evidence" / "robustness-trials.jsonl").read_bytes() == (run / "robustness" / "trials.jsonl").read_bytes()
    assert not any(w.startswith("no robustness") for w in r["warnings"])
    # R2-D-4 (a): a card written on the old implicit default fee (fees_source "default" or none) is not inherited
    card_path = run / "run_card.json"
    card = json.loads(card_path.read_text())
    for src in ("default", None):
        card["assumptions"]["fees_source"] = src
        card_path.write_text(json.dumps(card))
        assert cli.main([*base, "--out", str(tmp_path / "rb-old")]) == 1
        assert "give --fees" in capsys.readouterr().out
        assert not (tmp_path / "rb-old" / "robustness.json").exists()
