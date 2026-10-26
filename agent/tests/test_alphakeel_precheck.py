"""``alphakeel_research.precheck``: AlphaKeel's research-side G1 items on a ``robustness.json``.

The shared vector ``contract/validation/vectors/g1-precheck.json`` is the specification (AlphaKeel's own verdict code runs
the same cases); every case must match exactly. Then a ``robustness.json`` from a real battery run checks that every item is
produced and that the results agree with the document's own facts.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from alphakeel_research import contract, precheck, robustness
from alphakeel_research.errors import ApiError
from alphakeel_research.packfile import Pack
from tests.test_alphakeel_ir_backtest import FEES
from tests.test_alphakeel_robustness import H6, START, _ir, _market, _wf_market, WF_HI

CONTRACT = Path(contract.__file__).parent / "contract"
VECTOR = json.loads((CONTRACT / "validation" / "vectors" / "g1-precheck.json").read_text(encoding="utf-8"))
ORDER = ["1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7", "1.8", "1.9", "1.10", "1.11", "1.12", "1.13", "1.16"]


def test_the_packaged_protocol_is_pinned_and_lists_the_research_side_items_in_order():
    p = precheck.load_protocol()
    assert p["schema"] == "alphakeel.validation-protocol/1" and [i["id"] for i in precheck.research_items(p)] == ORDER
    assert precheck.protocol_sha256() == contract.local_pin()["validation/protocol.json"]
    assert set(contract.local_pin()) >= {"validation/protocol.json", "validation/vectors/g1-precheck.json"}


def test_a_protocol_file_that_differs_from_the_pin_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(contract, "local_pin", lambda: {"validation/protocol.json": "0" * 64})
    with pytest.raises(ApiError) as e:
        precheck.load_protocol()
    assert e.value.code == "contract.pin_mismatch"


@pytest.mark.parametrize("case", VECTOR["cases"], ids=[c["name"] for c in VECTOR["cases"]])
def test_every_shared_vector_case_matches_exactly(case):
    # a case may give the IR universe instead of the flag: the flag is then derived by ir_filter_required, as AlphaKeel does (protocol 2.3)
    forced = precheck.ir_filter_required({"universe": case["ir_universe"]}) if "ir_universe" in case else case.get("filter_required_by_ir", False)
    got = precheck.evaluate(case["robustness"], precheck.load_protocol(), case["other_registered_candidates"], case.get("trials_floor", 0),
                            filter_required_by_ir=forced, ak_synthetic=case.get("ak_synthetic"))
    assert [g["id"] for g in got] == list(case["expected"]), case["why"]
    assert {g["id"]: g["result"] for g in got} == case["expected"], case["why"]
    assert all(g["label"] for g in got)
    assert all(g["note"] for g in got if g["result"] == "FAIL")  # a FAIL always says why


@pytest.mark.parametrize("c", VECTOR["derivations"]["filter_required_by_ir"], ids=lambda c: c["name"])
def test_ir_filter_required_matches_the_shared_derivation_vectors(c):
    # review round 2, B: the derivation function itself is under test, not only its result fed into evaluate()
    assert precheck.ir_filter_required({"universe": c["universe"]}) is c["expected"]


@pytest.mark.parametrize("c", VECTOR["derivations"]["pack_is_synthetic"], ids=lambda c: c["name"])
def test_pack_is_synthetic_matches_the_shared_derivation_vectors(c):
    assert precheck.pack_is_synthetic(c["pack_universe"]) is c["expected"]


def test_missing_robustness_fails_every_item_and_a_missing_field_is_a_fail_not_a_pass():
    p = precheck.load_protocol()
    got = precheck.evaluate(None, p)
    assert [g["id"] for g in got] == ORDER and {g["result"] for g in got} == {"FAIL"} and {g["note"] for g in got} == {"robustness.json missing"}
    case = copy.deepcopy(VECTOR["cases"][0]["robustness"])
    del case["rating"]["facts"]["days"]
    del case["statistics"]["pbo"]
    got = {g["id"]: g for g in precheck.evaluate(case, p)}
    assert got["1.1"]["result"] == "FAIL" and "missing" in got["1.1"]["note"] and got["1.7"]["result"] == "FAIL"
    assert {g["result"] for g in precheck.evaluate({}, p)} == {"FAIL"}


def test_battery_output_has_every_item_and_agrees_with_its_own_facts(tmp_path):
    insts, pd = _market(tmp_path)
    doc = robustness.battery(_ir(), Pack.from_directory(pd), insts, is_window=(START, START + H6), fees=FEES, out_dir=tmp_path / "rb", permutations=0,
                             step_minutes=2, workers=1)
    doc = json.loads((tmp_path / "rb" / "robustness.json").read_text())  # the file the route reads
    got = {g["id"]: g for g in precheck.evaluate(doc, precheck.load_protocol())}
    assert list(got) == ORDER
    f = doc["rating"]["facts"]
    assert got["1.1"]["result"] == "FAIL" and f["days"] < 90  # a 6-hour window
    assert got["1.2"]["result"] == "FAIL" and f["entries_distinct"] < 30  # protocol 3.0 counts distinct entries
    rows = [json.loads(x) for x in (tmp_path / "rb" / "runs" / "base" / "trades.jsonl").read_text().splitlines() if x.strip()]
    assert f["entries_distinct"] == sum(1 for r in rows if r["status"] == "closed")  # a 6-hour window: no open entry reaches 7 days
    assert doc["statistics"]["bootstrap"] is None or doc["statistics"]["bootstrap"].get("block_rule") == "max_politis_white_median_hold"
    assert got["1.3"]["result"] == ("PASS" if Decimal(f["base_net"]) > 0 else "FAIL")
    assert got["1.4"]["result"] == ("PASS" if Decimal(doc["cost_stress"]["1.5"]["net"]) > 0 else "FAIL")
    bs = doc["cost_stress"]["basis_stress_x2"]  # not_applicable (net null) when the IR reads neither stress feature: 1.5 fails
    assert bs["result"] in ("run", "not_applicable") and (bs["net"] is None) == (bs["result"] == "not_applicable")
    assert got["1.5"]["result"] == ("FAIL" if bs["net"] is None else "PASS" if Decimal(bs["net"]) > 0 else "FAIL")
    # a 6-hour window is one UTC day: the DSR is undefined, dsr_inputs is null and 1.6 fails (the next test covers a real DSR)
    assert doc["statistics"]["dsr_inputs"] is None and doc["statistics"]["alphakeel_metrics"]["dsr"] is None
    assert got["1.6"]["result"] == "FAIL" and "dsr_inputs missing" in got["1.6"]["note"]
    assert got["1.7"]["result"] == "N/A"  # fewer than 8 variants
    assert got["1.11"]["result"] == "FAIL" and "not computable" in got["1.11"]["note"]
    assert got["1.12"]["result"] == "N/A"  # no filter under test
    assert got["1.13"]["result"] == ("PASS" if doc["rating"]["grade"] in ("A", "B") else "FAIL")
    assert got["1.16"]["result"] == "PASS" and f["coverage_partial"] is False


def test_walk_forward_result_of_a_real_battery_feeds_item_1_11(tmp_path):
    insts, pd = _wf_market(tmp_path)
    doc = robustness.battery(_ir(), Pack.from_directory(pd), insts, is_window=(START, WF_HI), fees=FEES, out_dir=tmp_path / "wf", permutations=0,
                             step_minutes=10, workers=1, wf_folds=2, wf_test_days=1, wf_min_train_days=1)
    got = {g["id"]: g for g in precheck.evaluate(json.loads((tmp_path / "wf" / "robustness.json").read_text()), precheck.load_protocol())}
    assert got["1.11"]["result"] == "FAIL"  # 2 folds < the protocol's 3, whatever the nets
    assert "fewer than 3 folds" in got["1.11"]["note"] and doc["walk_forward"]["computable"]
    # four days of returns: the DSR is defined; the inputs reproduce it, and more registered candidates can only lower it
    i = doc["statistics"]["dsr_inputs"]
    assert i["trials"] == doc["trials"]["count"] and got["1.6"]["note"].startswith(f"trials {i['trials']} + 0")
    again = {g["id"]: g for g in precheck.evaluate(doc, precheck.load_protocol(), 40)}
    assert again["1.6"]["observed"] <= got["1.6"]["observed"] if got["1.6"]["observed"] is not None else True


def _mutate(path, value, *, filter_on=False):
    rb = copy.deepcopy(VECTOR["cases"][0]["robustness"])
    f = rb["rating"]["facts"]
    if filter_on:
        f["filter_tested"], f["filter_p"] = True, "0.01"
        # protocol 2.5 (1.12): a tested filter carries its permutation draws and the seed derived from the strategy id
        rb["filter"] = {"permutation": {"draws": 999, "seed": precheck.permutation_seed(rb["strategy_id"])}}
    cur = rb
    for k in path[:-1]:
        cur = cur[k]
    cur[path[-1]] = value
    return rb


WRONG_TYPES = [  # (item, path, wrong value, needs a filter under test)
    ("1.1", ("rating", "facts", "days"), 120.0, False), ("1.1", ("rating", "facts", "days"), True, False), ("1.1", ("rating", "facts", "days"), "120", False),
    ("1.2", ("rating", "facts", "entries_distinct"), 45.0, False), ("1.2", ("rating", "facts", "entries_distinct"), "45", False),
    ("1.3", ("rating", "facts", "base_net"), 152.3, False), ("1.3", ("rating", "facts", "base_net"), True, False),
    ("1.4", ("cost_stress", "1.5", "net"), 98.1, False), ("1.5", ("cost_stress", "basis_stress_x2", "net"), 120, False),
    ("1.6", ("statistics", "dsr_inputs", "trials"), 7.0, False), ("1.6", ("statistics", "dsr_inputs", "trials"), True, False),
    ("1.6", ("statistics", "dsr_inputs", "trials"), "7", False), ("1.6", ("statistics", "dsr_inputs", "trials"), 0, False),
    ("1.6", ("statistics", "dsr_inputs", "sharpe_daily"), "0.3", False), ("1.6", ("statistics", "alphakeel_metrics", "dsr"), "0.9585088157772101", False),
    ("1.7", ("statistics", "pbo", "variants"), 9.0, False), ("1.7", ("statistics", "pbo", "value"), "0.1", False),
    ("1.7", ("statistics", "pbo", "value"), False, False),
    ("1.8", ("statistics", "bootstrap", "ci_lower"), "0.0004", False), ("1.8", ("statistics", "bootstrap", "ci_lower"), True, False),
    ("1.9", ("parameters", "neighbours"), [{"net": 50}, {"net": "12.5"}], False),
    ("1.10", ("statistics", "concentration", "top_coin_share"), 0.31, False),
    ("1.11", ("walk_forward", "efficiency"), 0.62, False),
    ("1.11", ("walk_forward", "folds"), [{"k": 1, "test": {"days": 30.0}, "test_net": "10"}] * 3, False),
    ("1.11", ("walk_forward", "folds"), [{"k": 1, "test": {"days": 30}, "test_net": 10}] * 3, False),
    ("1.12", ("rating", "facts", "filter_tested"), None, False),
    ("1.12", ("rating", "facts", "filter_p"), 0.01, True),
]


@pytest.mark.parametrize("item,path,value,filter_on", WRONG_TYPES, ids=[f"{i}-{'.'.join(p)}-{v!r}"[:70] for i, p, v, _ in WRONG_TYPES])
def test_wrong_json_types_fail(item, path, value, filter_on):
    p = precheck.load_protocol()
    ok = {g["id"]: g for g in precheck.evaluate(_mutate(path, _mutate_value(path, filter_on), filter_on=filter_on), p)}
    assert ok[item]["result"] == ("N/A" if path[-1] == "filter_tested" else "PASS"), ok[item]  # the right type passes: only the type is under test
    got = {g["id"]: g for g in precheck.evaluate(_mutate(path, value, filter_on=filter_on), p)}
    assert got[item]["result"] == "FAIL" and got[item]["note"], got[item]
    if not (item == "1.6" and path[-1] == "trials" and value == 0):
        assert "type" in got[item]["note"]


def _mutate_value(path, filter_on):
    cur = VECTOR["cases"][0]["robustness"]
    for k in path:
        cur = cur[k]
    return "0.01" if filter_on and path[-1] == "filter_p" else cur


# ---- protocol 2.1: 1.9 width, 1.18 universe, 1.19 literals (the shared vector's ir_cases) ----

@pytest.mark.parametrize("case", VECTOR["ir_cases"], ids=[c["name"] for c in VECTOR["ir_cases"]])
def test_every_shared_ir_case_matches_exactly(case):
    extra = precheck.ir_items(case["ir"], case["pack_universe"], precheck.load_protocol(), "pk")
    got = {i["id"]: i["result"] for i in extra["items"]}
    exp = case["expected"]
    assert got["1.18"] == exp["1.18"], (case["why"], extra["items"])
    assert got["1.19"] == exp["1.19"], (case["why"], extra["items"])
    assert ("FAIL" if extra["robust_width_problems"] else "PASS") == exp["robust_width"], (case["why"], extra["robust_width_problems"])
    assert extra["grade_cap"] == exp["grade_cap"], case["why"]
    assert all(i["note"] for i in extra["items"] if i["result"] == "FAIL")


def test_evaluate_without_an_ir_is_unchanged_and_with_one_appends_1_18_and_1_19():
    case = VECTOR["cases"][0]
    plain = precheck.evaluate(case["robustness"], precheck.load_protocol(), case["other_registered_candidates"])
    assert {g["id"] for g in plain}.isdisjoint({"1.18", "1.19"})
    ir_case = VECTOR["ir_cases"][0]
    full = precheck.evaluate(case["robustness"], precheck.load_protocol(), case["other_registered_candidates"],
                             ir=ir_case["ir"], pack_universe=ir_case["pack_universe"])
    assert [g["id"] for g in full][-2:] == ["1.18", "1.19"]
    assert [g["id"] for g in full][:-2] == [g["id"] for g in plain]


@pytest.mark.parametrize("c", VECTOR["derivations"]["permutation_seed"], ids=lambda c: c["strategy_id"])
def test_permutation_seed_matches_the_shared_derivation_vectors(c):
    # protocol 2.5 (1.12, D4-5): the seed is derived from the strategy id exactly as AlphaKeel derives it
    assert precheck.permutation_seed(c["strategy_id"]) == c["expected"]


# --- protocol 3.0 (AlphaKeel WP14a) -----------------------------------------------------------------------------------

def _protocol_with(item_id: str, threshold: dict) -> dict:
    p = copy.deepcopy(precheck.load_protocol())
    for item in p["stages"]["validated"]["items"]:
        if item["id"] == item_id:
            item["threshold"] = threshold
    return p


def _by_id(rb: dict, protocol: dict, **kw) -> dict:
    return {g["id"]: g for g in precheck.evaluate(rb, protocol, 0, 0, **kw)}


def test_1_2_branches_on_the_threshold_key_not_on_the_version():
    rb = copy.deepcopy(VECTOR["cases"][0]["robustness"])
    rb["rating"]["facts"].update(trades_closed=31, entries_distinct=25)
    v3 = precheck.load_protocol()
    assert "min_entries" in next(i for i in v3["stages"]["validated"]["items"] if i["id"] == "1.2")["threshold"]
    assert _by_id(rb, v3)["1.2"]["result"] == "FAIL"  # 25 distinct entries < 30, whatever trades_closed says
    # a 2.8-or-earlier copy (min_trades) is judged on trades_closed exactly as before, even inside a "3.0" document
    old = _protocol_with("1.2", {"min_trades": "30"})
    assert old["version"] == "3.0" and _by_id(rb, old)["1.2"]["result"] == "PASS"
    # 3.0 key, fact missing: FAIL with a note, never the closed count instead
    del rb["rating"]["facts"]["entries_distinct"]
    got = _by_id(rb, v3)["1.2"]
    assert got["result"] == "FAIL" and "entries_distinct missing" in got["note"] and "never the closed-trade count" in got["note"]
    assert _by_id(rb, old)["1.2"]["result"] == "PASS"  # the old copy never needed it
    unknown = _by_id(rb, _protocol_with("1.2", {"min_something": "30"}))["1.2"]
    assert unknown["result"] == "FAIL" and "unknown 1.2 threshold keys" in unknown["note"]


def test_1_8_block_too_long_fails_with_the_cap_and_an_old_block_rule_is_noted():
    rb = copy.deepcopy(VECTOR["cases"][0]["robustness"])
    p = precheck.load_protocol()
    got = _by_id(rb, p)["1.8"]
    assert got["result"] == "PASS" and "not computed with the protocol 3.0 block" in got["note"]
    rb["statistics"]["bootstrap"] = {"ci_lower": None, "block_rule": "max_politis_white_median_hold",
                                     "block_too_long": {"median_hold_days": 21, "days": 60, "block_cap": 20}}
    got = _by_id(rb, p)["1.8"]
    assert got["result"] == "FAIL" and got["observed"] == {"median_hold_days": 21, "days": 60, "block_cap": 20}
    assert "cannot cover the holding period" in got["note"]
    rb["statistics"]["bootstrap"] = {"ci_lower": 0.0004, "block_rule": "max_politis_white_median_hold"}
    got = _by_id(rb, p)["1.8"]
    assert got["result"] == "PASS" and got["note"] is None
    # the old copy (no block key) reads only ci_lower, as before
    assert _by_id(rb, _protocol_with("1.8", {"lower_gt": "0"}))["1.8"]["note"] is None


def test_1_6_trials_include_is_named_in_the_note_and_the_ledger_count_is_a_floor():
    rb = copy.deepcopy(VECTOR["cases"][0]["robustness"])
    p = precheck.load_protocol()
    got = _by_id(rb, p)["1.6"]
    assert got["result"] == "PASS" and "trials_include targets_strategy,summary_reads" in got["note"] and "NOT in this local trial count" in got["note"]
    big = _by_id(rb, p, ledger_trials=10_000)["1.6"]
    assert big["result"] == "FAIL" and "ledger count 10000 used as a floor" in big["note"]  # the ledger's submissions and reads count
    thr = dict(next(i for i in p["stages"]["validated"]["items"] if i["id"] == "1.6")["threshold"], trials_include="targets_strategy,factor_trials")
    odd = _by_id(rb, _protocol_with("1.6", thr))["1.6"]
    assert "unknown trials_include source(s) factor_trials" in odd["note"]
    thr.pop("trials_include")
    assert "trials_include" not in (_by_id(rb, _protocol_with("1.6", thr))["1.6"]["note"] or "")  # an old copy: no note
