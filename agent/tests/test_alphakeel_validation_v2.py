"""Research side of AlphaKeel's validation protocol v2 (docs/45) and the research-side overfitting fixes (audit 2026-10-11 2/3):
shared precheck vectors, prior-trials floor, handoff consistency, run registration with the research service, the forced
filter test, the robust-range width floor and the point-in-time truncation of ``Pit.instruments``."""

from __future__ import annotations

import copy
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from alphakeel_research import cli, contract, ir_backtest, ir_crosscheck, precheck, robustness, workflow
from alphakeel_research.errors import ApiError
from alphakeel_research.packfile import Inst, Instrument, Pack, Pit
from tests.test_alphakeel_ir_backtest import FEES, _fake_robustness, _run, golden_ir
from tests.test_alphakeel_robustness import H6, START, _ir, _market

CONTRACT = Path(contract.__file__).parent / "contract"
VECTOR = json.loads((CONTRACT / "validation" / "vectors" / "g1-precheck.json").read_text(encoding="utf-8"))
PIN = contract.local_pin()


def test_the_contract_pin_is_the_v2_protocol_and_vectors():
    # the digests are the contract itself: they must equal the packaged files (the PIN is exported from AlphaKeel, never edited)
    for rel in ("validation/protocol.json", "validation/vectors/g1-precheck.json"):
        assert PIN[rel] == hashlib.sha256((Path(contract.__file__).parent / "contract" / rel).read_bytes()).hexdigest()
    p = precheck.load_protocol()
    assert p["version"] == "2.5"
    # an optional informational item (3.11, not research-side) never reaches the research-side list
    assert "3.11" not in {i["id"] for i in precheck.research_items(p)}
    stripped = copy.deepcopy(p)
    for stage in stripped["stages"].values():
        stage["items"] = [i for i in stage["items"] if i["id"] != "3.11"]
    assert [i["id"] for i in precheck.research_items(stripped)] == [i["id"] for i in precheck.research_items(p)]


@pytest.mark.parametrize("case", VECTOR["cases"], ids=[c["name"] for c in VECTOR["cases"]])
def test_precheck_vectors_v2(case):
    forced = precheck.ir_filter_required({"universe": case["ir_universe"]}) if "ir_universe" in case else case.get("filter_required_by_ir", False)
    got = precheck.evaluate(case["robustness"], precheck.load_protocol(), case["other_registered_candidates"], case.get("trials_floor", 0),
                            filter_required_by_ir=forced, ak_synthetic=case.get("ak_synthetic"))
    assert {g["id"]: g["result"] for g in got} == case["expected"], case["why"]
    assert all(g["note"] for g in got if g["result"] == "FAIL")


def test_the_trials_floor_and_the_synthetic_cap_are_applied():
    case = next(c for c in VECTOR["cases"] if c["name"] == "all_pass")
    p = precheck.load_protocol()
    by = lambda rb, **kw: {g["id"]: g for g in precheck.evaluate(rb, p, 0, **kw)}  # noqa: E731
    assert by(case["robustness"])["1.6"]["result"] == "PASS"
    assert by(case["robustness"], trials_floor=500)["1.6"]["result"] == "FAIL"
    rb = copy.deepcopy(case["robustness"])
    rb["data_view"] = {"view": "kline_close_synthetic_bbo"}
    rb["rating"]["grade"] = "A"
    assert by(rb)["1.13"]["result"] == "FAIL" and "cap violated" in by(rb)["1.13"]["note"]


# --- 3: the battery's trial count is never below the run card's ------------------------------------------------------------

def test_prior_trials_floor(tmp_path, capsys):
    insts, pd = _market(tmp_path)
    pack = Pack.from_directory(pd)
    kw = dict(is_window=(START, START + H6), permutations=0, seed=3, step_minutes=2, workers=1)
    low = robustness.battery(_ir(), pack, insts, fees=FEES, out_dir=tmp_path / "a", prior_trials=2, prior_trials_floor=9, **kw)
    ok = robustness.battery(_ir(), pack, insts, fees=FEES, out_dir=tmp_path / "b", prior_trials=12, prior_trials_floor=9, **kw)
    n = low["trials"]["distinct_candidate_variants"]
    assert low["trials"]["count"] == n + 9 and low["trials"]["prior_trials_used"] == 9 and low["trials"]["prior_trials_declared"] == 2
    assert any(x.startswith("warning:") and "below the IR run card's trials.count 9" in x for x in low["notes"])
    assert ok["trials"]["count"] == n + 12 and not any("below the IR run card" in x for x in ok["notes"])
    # the CLI reads the floor from the run card and says so on stderr
    run = _run(tmp_path / "r", "--trials", "3", "--trials-evidence", str(_evidence(tmp_path)))
    capsys.readouterr()
    assert cli.main(["ir", "robustness", "--no-register", "--run", str(run), "--permutations", "0", "--workers", "1", "--step-minutes", "2",
                     "--prior-trials", "1"]) == 0
    cap = capsys.readouterr()
    assert "below the IR run card's trials.count 3" in cap.err
    doc = json.loads((run / "robustness" / "robustness.json").read_text())
    assert doc["trials"]["prior_trials_used"] == 3


def _evidence(tmp_path: Path) -> Path:
    f = tmp_path / "trials-ev.jsonl"
    f.write_text("".join(json.dumps({"trial": i}) + "\n" for i in range(3)))
    return f


# --- 4: the export refuses inconsistent evidence ---------------------------------------------------------------------------

def test_export_rejects_trial_mismatch(tmp_path):
    run = _run(tmp_path, "--trials", "1")
    card = json.loads((run / "run_card.json").read_text())
    sid, pid, w = card["strategy_id"], card["pack"]["pack_id"], card["window"]
    inputs = {"is_window": {"start_ms": w["start_ms"], "end_ms": w["end_ms"]}, "oos_window": None, "pack_id": pid}

    def go(name, *, rob_inputs=inputs, variants=1, rows=1, reconciliation=None):
        d = tmp_path / name
        _fake_robustness(d, sid, inputs=rob_inputs)
        doc = json.loads((d / "robustness.json").read_text())
        doc["trials"] = {"distinct_candidate_variants": variants}
        (d / "robustness.json").write_text(json.dumps(doc))
        (d / "trials.jsonl").write_text("".join(json.dumps({"seq": i, "counted_trial": True}) + "\n" for i in range(rows)) + '{"seq": 99, "counted_trial": false}\n')
        return workflow.ir_export(golden_ir(), run, tmp_path / f"o-{name}", robustness_dir=d, reconciliation=reconciliation)

    assert go("ok")["provenance"]["robustness"]["grade"] == "C"
    with pytest.raises(ApiError, match="trials ledger mismatch"):
        go("rows", variants=1, rows=2)
    with pytest.raises(ApiError, match="trials ledger mismatch"):
        go("variants", variants=3, rows=1)
    with pytest.raises(ApiError, match="no inputs.pack_id"):
        go("nopack", rob_inputs={k: v for k, v in inputs.items() if k != "pack_id"})
    for name, rec, match in (("rec-none", {"layers": {}}, "has no pack_id"), ("rec-other", {"pack_id": "pk-other"}, "same pack")):
        f = tmp_path / f"{name}.json"
        f.write_text(json.dumps(rec))
        with pytest.raises(ApiError, match=match):
            go(name, reconciliation=f)
    good = tmp_path / "rec-good.json"
    good.write_text(json.dumps({"pack_id": pid}))
    assert go("rec-ok", reconciliation=good)["strategy_id"] == sid
    # the default <run>/reconciliation.json (what `ir crosscheck` writes) is checked too
    (run / "reconciliation.json").write_text(json.dumps({"pack_id": "pk-other"}))
    with pytest.raises(ApiError, match="same pack"):
        go("rec-default")


def test_a_real_battery_ledger_has_one_counted_row_per_variant(tmp_path):
    insts, pd = _market(tmp_path)
    out = tmp_path / "rb"
    doc = robustness.battery(_ir(), Pack.from_directory(pd), insts, is_window=(START, START + H6), fees=FEES, out_dir=out, permutations=0, step_minutes=2, workers=1)
    rows = [json.loads(x) for x in (out / "trials.jsonl").read_text().splitlines()]
    assert sum(r["counted_trial"] for r in rows) == doc["trials"]["distinct_candidate_variants"] > 0
    assert doc["inputs"]["pack_id"]


# --- 5: runs are registered with the research service ----------------------------------------------------------------------

class FakeService:
    def __init__(self, fail_first_with_queue_full: int = 0, fail_with: ApiError | None = None,
                 check_fails_with: ApiError | None = None):
        self.checks: list[str] = []
        self.check_fails_with = check_fails_with
        self.bodies: list[dict] = []
        self.keys: list[str] = []
        self.queue_full = fail_first_with_queue_full
        self.fail_with = fail_with
        self.trial_bodies: list[dict] = []
        self.trial_keys: list[str] = []

    def check(self):
        self.checks.append("check")
        if self.check_fails_with is not None:
            raise self.check_fails_with
        return {"contract_pin": "matched"}

    def record_trial(self, body, *, key=None):
        if self.fail_with is not None:
            raise self.fail_with
        self.trial_bodies.append(body)
        self.trial_keys.append(key)
        return {"trial_id": f"trial-{len(self.trial_bodies):04d}", "trial_key": "research:x", "request_sha256": "x", "recorded": True}

    def create_run(self, body, *, key=None):
        if self.fail_with is not None:
            raise self.fail_with
        if self.queue_full > 0:
            self.queue_full -= 1
            raise ApiError("resource.queue_full", "the run queue is full")
        self.bodies.append(body)
        self.keys.append(key)
        return {"run_id": f"run-{len(self.bodies):04d}"}

    def wait_run(self, run_id, *, timeout=0):
        return {"status": "complete"}

    def artifact(self, run_id, name, *, stored=False):
        return json.dumps({"fill_model": "conservative", "approximations": ["sqrt_impact_modeled_depth"], "capital_cost": {"annual": "0.05"},
                           "amounts": {"close_now": "10.5", "net_of_capital_cost": "9.25"}}).encode()


def test_runs_registered_with_service(tmp_path, monkeypatch):
    monkeypatch.setattr(ir_crosscheck.time, "sleep", lambda s: None)
    insts, pd = _market(tmp_path)
    pack = Pack.from_directory(pd)
    svc = FakeService(fail_first_with_queue_full=2)
    doc = robustness.battery(_ir(), pack, insts, is_window=(START, START + H6), fees=FEES, out_dir=tmp_path / "rb", permutations=0, step_minutes=2,
                             workers=1, registry=svc)
    n = doc["trials"]["distinct_candidate_variants"]
    assert len(svc.bodies) == n == len(set(svc.keys)) and all(b["mode"] == "ir_strategy" and b["pack_id"] == pack.id for b in svc.bodies)
    assert all(b["dataset_view"]["view"] == "kline_close_synthetic_bbo" for b in svc.bodies)
    sr = doc["service_runs"]
    assert sr["registered"] is True and sr["ids"] == [f"run-{i:04d}" for i in range(1, n + 1)]
    assert {r["strategy_id"] for r in sr["runs"]} == set(doc["trials"]["candidate_strategy_ids"])
    cons = sr["conservative"]
    assert sr["service_conservative_net"] == "9.25" and cons["fill_model"] == "conservative" and cons["net_basis"] == "net_of_capital_cost"
    assert Decimal(cons["difference"]) == Decimal("9.25") - Decimal(cons["local_net"])
    assert any("conservative run of the same IR" in x for x in doc["notes"])
    assert svc.trial_bodies == []  # the battery still uses POST /runs
    assert all("profile" not in b for b in svc.bodies)  # the registered runs use the server default (conservative)
    on_disk = json.loads((tmp_path / "rb" / "robustness.json").read_text())
    assert on_disk["service_runs"]["ids"] == sr["ids"]
    # no client: said so, in the document and in the notes
    bare = robustness.battery(_ir(), pack, insts, is_window=(START, START + H6), fees=FEES, out_dir=tmp_path / "rb2", permutations=0, step_minutes=2, workers=1)
    assert bare["service_runs"]["registered"] is False and any("not registered" in x for x in bare["notes"])
    # a registration that fails stops the battery before robustness.json is written
    bad = FakeService(fail_with=ApiError("internal.error", "ledger write failed"))
    with pytest.raises(ApiError, match="ledger write failed"):
        robustness.battery(_ir(), pack, insts, is_window=(START, START + H6), fees=FEES, out_dir=tmp_path / "rb3", permutations=0, step_minutes=2,
                           workers=1, registry=bad)
    assert not (tmp_path / "rb3" / "robustness.json").exists()


def test_ir_backtest_registers_its_trial_ledger_only(tmp_path):
    from tests.test_alphakeel_ir_backtest import BN, OK, START as S0, END, export_dir, golden_built

    pd = export_dir(tmp_path, golden_built())
    svc = FakeService()
    card = ir_backtest.backtest(svc, golden_ir(), [BN, OK], start_ms=S0, end_ms=END, out_dir=tmp_path / "r1", pack_dir=pd, register=True, trials=1, fees=FEES)
    assert svc.bodies == [] and len(svc.trial_bodies) == 1
    tb = svc.trial_bodies[0]
    assert tb["mode"] == "ir_strategy" and "profile" not in tb and tb["dataset_view"]["view"] == "kline_close_synthetic_bbo"
    _, want_key = ir_crosscheck.mirror_request(tb["ir"], card, key_salt=ir_crosscheck.trial_key_salt())
    assert svc.trial_keys == [want_key]
    expect = {"registered": True, "trial_id": "trial-0001", "trial_key": "research:x",
              "ledger": "research-service trial ledger (POST /trials, ledger only, not executed)"}
    assert card["service_registration"] == expect
    assert svc.checks == ["check"]
    badsvc = FakeService(check_fails_with=ApiError("contract.pin_mismatch", "x"))
    with pytest.raises(ApiError, match="x"):
        ir_backtest.backtest(badsvc, golden_ir(), [BN, OK], start_ms=S0, end_ms=END, out_dir=tmp_path / "r4", pack_dir=pd, register=True, trials=1, fees=FEES)
    assert badsvc.trial_bodies == []
    assert json.loads((tmp_path / "r4" / "state.json").read_text())["status"] == "failed"
    assert json.loads((tmp_path / "r1" / "run_card.json").read_text())["service_registration"] == expect
    off = ir_backtest.backtest(None, golden_ir(), [BN, OK], start_ms=S0, end_ms=END, out_dir=tmp_path / "r2", pack_dir=pd, trials=1, fees=FEES)
    assert off["service_registration"]["registered"] is False
    with pytest.raises(ApiError, match="needs the research-service client"):
        ir_backtest.backtest(None, golden_ir(), [BN, OK], start_ms=S0, end_ms=END, out_dir=tmp_path / "r3", pack_dir=pd, register=True, trials=1, fees=FEES)
    assert json.loads((tmp_path / "r3" / "state.json").read_text())["status"] == "failed"


# --- 6: the test surface is not the researcher's to choose -----------------------------------------------------------------

def test_forced_filter_permutation(tmp_path):
    insts, pd = _market(tmp_path)
    pack = Pack.from_directory(pd)
    kw = dict(is_window=(START, START + H6), permutations=3, seed=5, step_minutes=2, workers=1)
    p = precheck.load_protocol()
    # excluded_bases in the IR, no --filter: the filter is tested anyway
    doc_ir = _ir()
    doc_ir["universe"]["excluded_bases"] = ["ETH"]
    d = robustness.battery(doc_ir, pack, insts, fees=FEES, out_dir=tmp_path / "a", **kw)
    f = d["rating"]["facts"]
    assert f["filter_tested"] is True and f["filter_required"] is True and f["filter_p"] is not None
    assert d["filter"]["bases"] == ["ETH"] and any("forced" in x for x in d["notes"])
    assert {g["id"]: g for g in precheck.evaluate(d, p)}["1.12"]["result"] in ("PASS", "FAIL")
    # an allow-list narrower than the coins the battery had data for: the dropped coins are the tested filter
    allow = _ir()
    allow["universe"]["bases"] = ["BTC", "SOL"]
    e = robustness.battery(allow, pack, insts, fees=FEES, out_dir=tmp_path / "b", **kw)
    assert e["rating"]["facts"]["filter_tested"] is True and e["filter"]["bases"] == ["ETH"]
    assert e["filter"]["permutation"]["pool"] == ["BTC", "ETH", "SOL"] and any("universe.bases" in x for x in e["notes"])
    # a given --filter cannot replace the IR's own restriction
    g = robustness.battery(doc_ir, pack, insts, fees=FEES, out_dir=tmp_path / "c", filter_spec={"kind": "excluded_bases", "bases": ["SOL"]}, **kw)
    assert g["filter"]["bases"] == ["ETH"] and any("is not tested" in x for x in g["notes"])
    # an IR with no restriction and no filter keeps 1.12 not applicable
    free = robustness.battery(_ir(), pack, insts, fees=FEES, out_dir=tmp_path / "d", **kw)
    assert free["rating"]["facts"]["filter_required"] is False and {x["id"]: x for x in precheck.evaluate(free, p)}["1.12"]["result"] == "N/A"
    # precheck: a restricted IR whose filter was not tested is a FAIL, not N/A
    tampered = copy.deepcopy(d)
    tampered["rating"]["facts"]["filter_tested"] = False
    got = {x["id"]: x for x in precheck.evaluate(tampered, p)}["1.12"]
    assert got["result"] == "FAIL" and "not permutation-tested" in got["note"]


def test_robust_width_floor(tmp_path):
    case = next(c for c in VECTOR["cases"] if c["name"] == "all_pass")
    p = precheck.load_protocol()

    def one(value, lo, hi):
        rb = copy.deepcopy(case["robustness"])
        rb["parameters"] = {**rb.get("parameters", {}), "ranges": [{"name": "hold", "path": "exit.max_hold_hours", "value": value, "lo": lo, "hi": hi}]}
        # protocol 2.5 (1.9 both_ends): the ranged parameter needs its lo and hi neighbours
        rb["parameters"]["neighbours"] = [{"parameter": "hold", "side": "lo", "net": "1.0"}, {"parameter": "hold", "side": "hi", "net": "2.0"}]
        return {g["id"]: g for g in precheck.evaluate(rb, p)}["1.9"]

    assert one("10", "8", "12")["result"] == "PASS"  # exactly +-20% passes
    narrow = one("10", "9.9999", "10.0001")
    assert narrow["result"] == "FAIL" and "too narrow" in narrow["note"] and "hold" in narrow["note"]
    assert one("10", "8", "11.9")["result"] == "FAIL"  # one side short
    assert one("0", "-1", "1")["result"] == "PASS" and one("0", "0", "0")["result"] == "FAIL"
    # the battery writes the ranges and warns about a narrow one
    insts, pd = _market(tmp_path)
    d = robustness.battery(_ir(), Pack.from_directory(pd), insts, is_window=(START, START + H6), fees=FEES, out_dir=tmp_path / "rb", permutations=0, step_minutes=2, workers=1)
    assert d["parameters"]["ranges"] == [{"name": "hold", "path": "exit.max_hold_hours", "value": "1", "lo": "1", "hi": "2"}]
    assert any(x.startswith("warning: robust range too narrow") for x in d["notes"])
    assert {g["id"]: g for g in precheck.evaluate(d, p)}["1.9"]["result"] == "FAIL"


# --- 8: Pit.instruments is point in time -----------------------------------------------------------------------------------

def test_pit_instruments_truncated_at_as_of():
    def inst(sym, first, last):
        return Instrument("binance", "perp", sym, sym[:3], "USDT", "USDT", "linear", None, "ok", None, Decimal(1), "ok", first, last, True)

    rows = {Inst("binance", "perp", s): inst(s, a, b) for s, a, b in (("BTCUSDT", 100, 900), ("ETHUSDT", 400, 900), ("SOLUSDT", 100, 300))}
    pit = Pit(SimpleNamespace(lock={}, id="pk-x", instruments=lambda: rows), 350)
    got = pit.instruments()
    assert set(k.symbol for k in got) == {"BTCUSDT", "SOLUSDT"}  # ETH first seen at 400 > as_of: does not exist yet
    assert got[Inst("binance", "perp", "BTCUSDT")].last_seen_ms == 350  # cut at as_of
    assert got[Inst("binance", "perp", "SOLUSDT")].last_seen_ms == 300  # already earlier: unchanged
    assert rows[Inst("binance", "perp", "BTCUSDT")].last_seen_ms == 900  # the pack's own rows are untouched
    assert len(Pit(SimpleNamespace(lock={}, id="pk-x", instruments=lambda: rows), 100).instruments()) == 2
    assert Pit(SimpleNamespace(lock={}, id="pk-x", instruments=lambda: rows), 99).instruments() == {}


# --- reconciliation compares engines on the one-level fill model; L1 is the server's profile ---------------------------------

def test_crosscheck_requests_top_of_book(tmp_path):
    fill = {"fill_model": "top_of_book", "spread_floor_bps": "0", "capital_cost_annual": "0"}
    assert ir_crosscheck.RECONCILIATION_FILL == fill
    assert ir_crosscheck.with_reconciliation_fill({"leverage_perp": "3"}) == {"leverage_perp": "3", **fill}
    assert ir_crosscheck.with_reconciliation_fill({"spread_floor_bps": 0, "capital_cost_annual": "0.0"}) == fill
    for bad in ({"fill_model": "conservative"}, {"spread_floor_bps": "2"}, {"capital_cost_annual": "0.05"}):
        with pytest.raises(ApiError, match="contradicts"):
            ir_crosscheck.with_reconciliation_fill(bad)
    run = _run(tmp_path)

    class Svc(FakeService):
        def wait_run(self, run_id, *, timeout=0):
            return {"status": "failed", "error": {"code": "x"}, "created_ms": 0}

    svc = Svc()
    doc = ir_crosscheck.alphakeel_check(svc, golden_ir(), run)
    assert doc["passed"] is False and svc.bodies[0]["profile"] == fill and svc.bodies[0]["mode"] == "ir_strategy"
    # registration (the conservative default) is a different request
    reg = FakeService()
    ir_crosscheck.register_trial(reg, golden_ir(), json.loads((run / "run_card.json").read_text()))
    assert "profile" not in reg.bodies[0] and reg.keys[0] != svc.keys[0]


def test_profile_summary_from_server(tmp_path):
    """``ir review`` asks the server to render the profile of the top_of_book run and uses that document; it builds none itself."""
    pytest.importorskip("alphakeel_research.ir")
    seen = {}

    class Stop(Exception):
        pass

    class Svc:
        def render_profile(self, pack_id, mode, profile=None):
            seen.update(pack_id=pack_id, mode=mode, profile=profile)
            raise Stop

    insts, pd = _market(tmp_path)
    pack = Pack.from_directory(pd)
    with pytest.raises(Stop):
        workflow.ir_review(Svc(), pack, _ir(), out_dir=tmp_path / "rv")
    assert seen["mode"] == "ir_strategy" and seen["pack_id"] == pack.id
    assert {k: seen["profile"][k] for k in ir_crosscheck.RECONCILIATION_FILL} == ir_crosscheck.RECONCILIATION_FILL
    with pytest.raises(ApiError, match="contradicts"):
        workflow.ir_review(Svc(), pack, _ir(), out_dir=tmp_path / "rv2", profile={"fill_model": "conservative"})


def test_protocol_22_ak_side_inputs_come_from_the_ir_and_the_pack_not_from_the_battery():
    p = precheck.load_protocol()
    case = next(c for c in VECTOR["cases"] if c["name"] == "filter_forced_by_ir_tested")
    ir = {"universe": {"excluded_bases": ["BTC"]}}
    # protocol 2.3 (review round 2, B): any bases allow-list forces the filter, whatever the battery traded
    assert precheck.ir_filter_required(ir) and not precheck.ir_filter_required({"universe": {}})
    assert precheck.ir_filter_required({"universe": {"bases": ["eth"]}}) and not precheck.ir_filter_required({"universe": {"bases": None}})
    assert precheck.pack_is_synthetic(None) and precheck.pack_is_synthetic({"selection": "pit_membership"})
    assert not precheck.pack_is_synthetic({"selection": "screener_live"}) and precheck.pack_is_synthetic({"selection": "screener_live", "dataset_pack": True})
    rb = copy.deepcopy(case["robustness"])
    rb["rating"]["facts"]["filter_tested"] = False
    got = {g["id"]: g for g in precheck.evaluate(rb, p, 0, ir=ir, pack_universe={"selection": "screener_live"})}
    assert got["1.12"]["result"] == "FAIL"  # forced by the IR even though the battery says filter_required false


def test_register_trial_key_includes_assumptions_version(tmp_path, monkeypatch):
    """Review round 2, E: the trial-registration key depends on the run-assumption version and the protocol version, so a run
    made by an older service build (which AlphaKeel refuses to bind) is not handed back for the same body forever."""
    run = _run(tmp_path)
    card = json.loads((run / "run_card.json").read_text())

    def key():
        svc = FakeService()
        ir_crosscheck.register_trial(svc, golden_ir(), card)
        return svc.keys[0]

    base = key()
    assert key() == base  # deterministic
    monkeypatch.setattr(ir_crosscheck, "ASSUMPTIONS_VERSION", "run-assumptions/99")
    assert key() != base
    monkeypatch.undo()
    real = precheck.load_protocol
    monkeypatch.setattr(precheck, "load_protocol", lambda: {**real(), "version": "9.9"})
    assert key() != base
    # the reconciliation (cross-check) key stays body-only
    body, plain = ir_crosscheck.mirror_request(golden_ir(), card)
    assert plain == ir_crosscheck.mirror_request(golden_ir(), card, key_salt="")[1] != ir_crosscheck.mirror_request(golden_ir(), card, key_salt="x")[1]


def test_mirror_request_ignores_a_top_level_strategy_id_in_the_ir(tmp_path):
    card = {"pack": {"pack_id": "pk-x"}, "window": {"start_ms": 1, "end_ms": 2}, "view": "kline_close_synthetic_bbo", "step_minutes": 5,
            "assumptions": {"fees": {"okx": {"perp": "0.0005"}}},
            "instruments": [{"venue": "okx", "market": "perp", "symbol": "BTC-USDT-SWAP", "base": "BTC", "quote": "USDT"}]}
    plain = golden_ir()
    plain.pop("strategy_id", None)
    with_id = {**plain, "strategy_id": "st-whatever"}
    for salt in ("", "salt"):
        assert ir_crosscheck.mirror_request(plain, card, key_salt=salt) == ir_crosscheck.mirror_request(with_id, card, key_salt=salt)
    assert "strategy_id" not in ir_crosscheck.mirror_request(with_id, card)[0]["ir"] and "strategy_id" in with_id
