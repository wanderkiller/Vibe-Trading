"""Strategy IR v1 (alphakeel.strategy-ir/1): the Python side runs the SAME shared vectors as AlphaKeel's Rust crate.

Pinned copy of the contract: agent/alphakeel_research/contract/strategy-ir/ (README.md is the source of truth).
"""

from __future__ import annotations

import copy
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from alphakeel_research import ir, ir_views
from alphakeel_research.packfile import Inst, Instrument, Observation, Quote

ROOT = Path(__file__).resolve().parents[1] / "alphakeel_research"
PIN_DIR = ROOT / "contract" / "strategy-ir"
EXAMPLE = ROOT / "examples" / "ir" / "strategy.json"
POLICY_DIR = ROOT / "ir_policy"


def _cases(name: str) -> list[dict]:
    doc = json.loads((PIN_DIR / "vectors" / name).read_text(encoding="utf-8"))
    assert doc["schema"] == "alphakeel.strategy-ir/1"
    return doc["cases"]


def _base_doc() -> dict:
    return copy.deepcopy(_cases("canonical.json")[0]["doc"])


# --- pin ---------------------------------------------------------------------------------------------------------------

def test_the_strategy_ir_contract_files_are_covered_by_the_one_research_contract_pin():
    # exported together with the research contract (tools/export-research-contract.sh): one PIN, the same keys as the
    # service's /whoami contract_pin, so Client.check() refuses a service built from another IR spec
    assert not (PIN_DIR / "PIN").exists()
    text = (ROOT / "contract" / "PIN").read_text()
    pinned = {line.split(None, 1)[1]: line.split(None, 1)[0] for line in text.splitlines() if line and not line.startswith("#")}
    names = {"README.md", "schema.json", "vectors/canonical.json", "vectors/decisions.json", "vectors/features.json"}
    assert {k for k in pinned if k.startswith("strategy-ir/")} == {f"strategy-ir/{n}" for n in names}
    for n in names:
        assert hashlib.sha256((PIN_DIR / n).read_bytes()).hexdigest() == pinned[f"strategy-ir/{n}"], n


# --- (a) shared vectors ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("case", _cases("features.json"), ids=lambda c: c["name"])
def test_feature_vectors(case):
    if "error" in case:
        with pytest.raises(ir.PairInvalid) as e:
            ir.compute_features(case["pair"], case["assumptions"], case["now_ms"])
        assert e.value.code == case["error"]
    else:
        got = ir.compute_features(case["pair"], case["assumptions"], case["now_ms"]).to_json()
        assert got == case["expected"]


@pytest.mark.parametrize("case", _cases("canonical.json"), ids=lambda c: c["name"])
def test_canonical_vectors(case):
    loaded = ir.load(json.dumps(case["doc"]))
    assert loaded.canonical_definition() == case["definition_canonical"]
    assert loaded.strategy_id == case["strategy_id"] == ir.strategy_id(case["doc"])


def test_canonical_vector_relations():
    cases = _cases("canonical.json")  # same_id_as / different_id_from are case indexes
    for c in cases:
        if "same_id_as" in c:
            assert ir.strategy_id(c["doc"]) == ir.strategy_id(cases[c["same_id_as"]]["doc"]), c["name"]
        if "different_id_from" in c:
            assert ir.strategy_id(c["doc"]) != ir.strategy_id(cases[c["different_id_from"]]["doc"]), c["name"]


@pytest.mark.parametrize("case", _cases("decisions.json"), ids=lambda c: c["name"])
def test_decision_vectors(case):
    got = ir.decide(case["ir"], case["frame"], case["state"], case["now_ms"])
    # JSON round trip: the decision is plain JSON with exactly the vector's shape and order
    assert json.loads(json.dumps(got)) == case["expected"]
    assert list(got) == ["exits", "entries", "rejected"]


# --- (b) schema ----------------------------------------------------------------------------------------------------------

def _ir_documents() -> list[tuple[str, dict]]:
    docs = [(f"canonical:{c['name']}", c["doc"]) for c in _cases("canonical.json")]
    docs += [(f"decisions:{c['name']}", c["ir"]) for c in _cases("decisions.json")]
    docs.append(("example", json.loads(EXAMPLE.read_text())))
    return docs


@pytest.mark.parametrize("name,doc", _ir_documents(), ids=lambda x: x if isinstance(x, str) else "")
def test_every_vector_document_satisfies_the_json_schema(name, doc):
    jsonschema = pytest.importorskip("jsonschema", reason="jsonschema is not installed in this environment")
    schema = json.loads((PIN_DIR / "schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(doc)
    assert ir.validate(doc) == []


# --- (c) validation errors -----------------------------------------------------------------------------------------------

def _set(doc: dict, path: list, value) -> dict:
    d = copy.deepcopy(doc)
    cur = d
    for k in path[:-1]:
        cur = cur[k]
    cur[path[-1]] = value
    return d


def _del(doc: dict, path: list) -> dict:
    d = copy.deepcopy(doc)
    cur = d
    for k in path[:-1]:
        cur = cur[k]
    del cur[path[-1]]
    return d


BAD = [
    ("unknown top-level field", lambda d: _set(d, ["features"], []), ir.E_SHAPE),
    ("unknown nested field", lambda d: _set(d, ["exit", "trailing"], "1"), ir.E_SHAPE),
    ("unknown feature", lambda d: _set(d, ["entry", "rank_by"], "zscore"), ir.E_SHAPE),
    ("unknown operator", lambda d: _set(d, ["entry", "conditions", 0, "op"], "=="), ir.E_SHAPE),
    ("unknown venue", lambda d: _set(d, ["universe", "pairs"], [["binance", "kraken"]]), ir.E_SHAPE),
    ("trailing zero decimal", lambda d: _set(d, ["exit", "max_loss"], "20.0"), ir.E_SHAPE),
    ("0.10", lambda d: _set(d, ["entry", "conditions", 0, "value"], "0.10"), ir.E_SHAPE),
    ("-0", lambda d: _set(d, ["entry", "conditions", 0, "value"], "-0"), ir.E_SHAPE),
    ("exponent", lambda d: _set(d, ["sizing", "notional_per_leg"], "1e3"), ir.E_SHAPE),
    ("leading plus", lambda d: _set(d, ["sizing", "notional_per_leg"], "+1000"), ir.E_SHAPE),
    ("JSON number for a decimal", lambda d: _set(d, ["sizing", "notional_per_leg"], 1000), ir.E_SHAPE),
    ("bool for a count", lambda d: _set(d, ["sizing", "max_open"], True), ir.E_SHAPE),
    ("missing nullable take_profit", lambda d: _del(d, ["exit", "take_profit"]), ir.E_SHAPE),
    ("missing nullable leverage", lambda d: _del(d, ["sizing", "leverage"]), ir.E_SHAPE),
    ("missing provenance", lambda d: _del(d, ["provenance"]), ir.E_SHAPE),
    ("schema constant", lambda d: _set(d, ["schema"], "alphakeel.strategy-ir/2"), ir.E_SCHEMA),
    ("kind mismatch", lambda d: _set(d, ["kind"], "single_venue_trend"), ir.E_KIND),
    ("empty name", lambda d: _set(d, ["name"], "  "), ir.E_INVALID),
    ("cross_perp on one venue", lambda d: _set(d, ["universe", "pairs"], [["okx", "okx"]]), ir.E_INVALID),
    ("spot_perp across venues", lambda d: _set(_set(d, ["universe", "shape"], "spot_perp"), ["universe", "pairs"], [["okx", "binance"]]), ir.E_INVALID),
    ("duplicate venue pair", lambda d: _set(d, ["universe", "pairs"], [["okx", "binance"], ["okx", "binance"]]), ir.E_INVALID),
    ("empty pair list", lambda d: _set(d, ["universe", "pairs"], []), ir.E_INVALID),
    ("lower-case base", lambda d: _set(d, ["universe", "excluded_bases"], ["luna"]), ir.E_INVALID),
    ("duplicate quote ccy", lambda d: _set(d, ["universe", "quote_ccys"], ["USDT", "USDT"]), ir.E_INVALID),
    ("basis_stress 1", lambda d: _set(d, ["assumptions", "basis_stress"], "1"), ir.E_INVALID),
    ("horizon 0", lambda d: _set(d, ["assumptions", "horizon_hours"], 0), ir.E_INVALID),
    ("between with lo > hi", lambda d: _set(d, ["entry", "conditions", 0], {"feature": "funding_apr", "op": "between", "value": ["2", "1"]}), ir.E_INVALID),
    ("between with one value", lambda d: _set(d, ["entry", "conditions", 0], {"feature": "funding_apr", "op": "between", "value": "1"}), ir.E_INVALID),
    ("interval without between", lambda d: _set(d, ["exit", "conditions", 0], {"feature": "funding_apr", "op": ">=", "value": ["1", "2"]}), ir.E_INVALID),
    ("candidate_limit 201", lambda d: _set(d, ["entry", "candidate_limit"], 201), ir.E_INVALID),
    ("one_per_base false", lambda d: _set(d, ["entry", "one_per_base"], False), ir.E_INVALID),
    ("max_loss 0", lambda d: _set(d, ["exit", "max_loss"], "0"), ir.E_INVALID),
    ("negative take_profit", lambda d: _set(d, ["exit", "take_profit"], "-1"), ir.E_INVALID),
    ("max_open 51", lambda d: _set(d, ["sizing", "max_open"], 51), ir.E_INVALID),
    ("robust outside", lambda d: _set(d, ["parameters", "min_net", "robust"], ["0.001", "0.002"]), ir.E_INVALID),
    ("provenance without tool", lambda d: _set(d, ["provenance"], {"window": {"start": "a", "end": "b"}}), ir.E_INVALID),
    ("provenance window end not a string", lambda d: _set(d, ["provenance", "window", "end"], 3), ir.E_INVALID),
    ("parameter path into provenance", lambda d: _set(d, ["parameters", "min_net", "path"], "provenance.window.start"), ir.E_PARAMETER_PATH),
    ("parameter path out of range", lambda d: _set(d, ["parameters", "min_net", "path"], "entry.conditions[3].value"), ir.E_PARAMETER_PATH),
    ("parameter path at a feature name", lambda d: _set(d, ["parameters", "min_net", "path"], "entry.rank_by"), ir.E_PARAMETER_PATH),
    ("parameter value mismatch", lambda d: _set(d, ["parameters", "min_net", "path"], "exit.max_loss"), ir.E_PARAMETER_MISMATCH),
    ("declared id mismatch", lambda d: _set(d, ["strategy_id"], "ir-000000000000000000000000"), ir.E_STRATEGY_ID),
]


@pytest.mark.parametrize("name,mutate,code", BAD, ids=[b[0] for b in BAD])
def test_invalid_documents_are_refused_with_their_rule_code(name, mutate, code):
    doc = mutate(_base_doc())
    with pytest.raises(ir.IrError) as e:
        ir.load(doc)
    assert e.value.code == code, e.value.message
    problems = ir.validate(doc)
    assert len(problems) == 1 and problems[0].startswith(code + ":")


def test_text_input_refuses_floats_duplicates_and_non_json_but_provenance_is_free():
    text = json.dumps(_base_doc())
    with pytest.raises(ir.IrError) as e:
        ir.load(text.replace('"max_open": 3', '"max_open": 3.0'))
    assert e.value.code == ir.E_SHAPE
    with pytest.raises(ir.IrError) as e:
        ir.load(text[:-1] + ', "name": "again"}')
    assert e.value.code == ir.E_SHAPE and "duplicate" in e.value.message
    with pytest.raises(ir.IrError):
        ir.load(text.replace('"max_open": 3', '"max_open": NaN'))
    doc = _base_doc()
    doc["provenance"]["score"] = 0.42  # free content, not part of the definition
    assert ir.load(json.dumps(doc)).strategy_id == ir.load(_base_doc()).strategy_id


def test_a_parameter_may_point_at_an_integer_and_at_an_interval_bound():
    doc = _base_doc()
    doc["entry"]["conditions"].append({"feature": "funding_apr", "op": "between", "value": ["0.5", "2"]})
    doc["parameters"] = {"apr_lo": {"value": "0.5", "robust": ["0.3", "0.8"], "path": "entry.conditions[1].value[0]"},
                         "hold": {"value": "72", "robust": ["48", "96"], "path": "exit.max_hold_hours"}}
    assert ir.validate(doc) == []


# --- (d) strategy_id -------------------------------------------------------------------------------------------------------

def test_strategy_id_ignores_key_order_provenance_and_the_declared_id():
    doc = _base_doc()
    sid = ir.strategy_id(doc)
    reordered = json.loads(json.dumps(doc, sort_keys=True))
    reordered = {k: reordered[k] for k in reversed(list(reordered))}
    reordered["exit"] = {k: reordered["exit"][k] for k in reversed(list(reordered["exit"]))}
    assert ir.strategy_id(reordered) == sid
    other = _set(doc, ["provenance"], {"tool": "someone-else", "window": {"start": "x", "end": "y"}, "trials": {"count": 999}})
    assert ir.strategy_id(other) == sid
    declared = _set(doc, ["strategy_id"], sid)
    assert ir.load(declared).strategy_id == sid
    assert ir.strategy_id(_set(doc, ["sizing", "max_open"], 4)) != sid
    assert ir.strategy_id(_del(doc, ["universe", "quote_ccys"])) == sid  # the default is inserted before hashing


# --- ir_views (README §4.1a) on an in-memory point-in-time reader -------------------------------------------------------

T = 1_700_000_000_000


class FakePit:
    """The parts of ``packfile.Pit`` the views read, over in-memory rows (frame 7 at T; frame 6 one minute earlier)."""

    def __init__(self, rows: dict, obs: dict, instruments: dict, fees: dict):
        self.rows, self.obs, self.insts, self.fee_doc = rows, obs, instruments, fees
        self.as_of_ms = T

    def instruments(self):
        return self.insts

    def fees(self):
        return self.fee_doc

    def quotes(self, inst, start, end=None):
        return [q for q in self.rows.get(inst, []) if start <= q.t < (end if end is not None else T + 1)]

    def observations(self, inst, start, end=None):
        return [o for o in self.obs.get(inst, []) if start <= o.t < (end if end is not None else T + 1)]

    def latest_quote(self, inst):
        rows = self.quotes(inst, 0)
        return rows[-1] if rows else None


def _inst(venue, market, symbol, base, quote="USDT", selected=True):
    return Instrument(venue, market, symbol, base, quote, quote, "linear", None, "ok", None, None, "ok", 0, 0, selected)


def _q(t, bid, ask, frame, mark=None, quote_ts=None, bbo=True):
    return Quote(t, Decimal(bid), Decimal(ask), bbo, None if mark is None else Decimal(mark), Decimal("1000000"), quote_ts, frame)


def _o(t, rate, hours, frame, fpx=None):
    return Observation(t, Decimal(rate), hours, None, None if fpx is None else Decimal(fpx), "mark", frame)


def _fake_pit() -> FakePit:
    bn, ok, by = Inst("binance", "perp", "BTCUSDT"), Inst("okx", "perp", "BTC-USDT-SWAP"), Inst("bybit", "perp", "BTCUSDT")
    bs, bc = Inst("binance", "spot", "BTCUSDT"), Inst("binance", "perp", "BTCUSDC")
    old, ex, nofee = Inst("okx", "perp", "ETH-USDT-SWAP"), Inst("binance", "perp", "ETHUSDT"), Inst("gate", "perp", "BTC_USDT")
    unsel = Inst("bybit", "perp", "ETHUSDT")
    insts = {bn: _inst("binance", "perp", "BTCUSDT", "BTC"), ok: _inst("okx", "perp", "BTC-USDT-SWAP", "BTC"),
             by: _inst("bybit", "perp", "BTCUSDT", "BTC"), bs: _inst("binance", "spot", "BTCUSDT", "BTC"),
             bc: _inst("binance", "perp", "BTCUSDC", "BTC", quote="USDC"), old: _inst("okx", "perp", "ETH-USDT-SWAP", "ETH"),
             ex: _inst("binance", "perp", "ETHUSDT", "ETH"), nofee: _inst("gate", "perp", "BTC_USDT", "BTC"),
             unsel: _inst("bybit", "perp", "ETHUSDT", "ETH", selected=False)}
    rows = {bn: [_q(T, "100", "100.01", 7, mark="100.005", quote_ts=T - 1000)], ok: [_q(T, "100.02", "100.03", 7, quote_ts=T - 500)],
            by: [_q(T, "99.99", "100", 7)], bs: [_q(T, "99.98", "100", 7, quote_ts=T - 10)], bc: [_q(T, "100", "100.01", 7)],
            old: [_q(T - 60_000, "2000", "2001", 6)], ex: [_q(T, "2000", "2000.5", 7)], nofee: [_q(T, "100", "100.01", 7)],
            unsel: [_q(T, "2000", "2001", 7)]}
    obs = {bn: [_o(T, "0.0001", 8, 7)], ok: [_o(T - 60_000, "0.0009", 8, 6), _o(T, "0.0006", 8, 7, fpx="100.025")],
           old: [_o(T, "0.0001", 8, 7)]}
    fees = {"fees": {v: {"perp": "0.0005", "spot": "0.001"} for v in ("binance", "okx", "bybit")}}
    return FakePit(rows, obs, insts, fees)


def test_frame_pairs_follow_the_enumeration_rule():
    pairs = ir_views.scan_frame_pairs(_fake_pit(), 7, T)
    ids = [p["id"] for p in pairs]
    assert ids == sorted(ids)
    assert ids == [
        "cross:BTC:binance:BTCUSDT>bybit:BTCUSDT",
        "cross:BTC:binance:BTCUSDT>okx:BTC-USDT-SWAP",
        "cross:BTC:bybit:BTCUSDT>binance:BTCUSDT",
        "cross:BTC:bybit:BTCUSDT>okx:BTC-USDT-SWAP",
        "cross:BTC:okx:BTC-USDT-SWAP>binance:BTCUSDT",
        "cross:BTC:okx:BTC-USDT-SWAP>bybit:BTCUSDT",
        # same venue + spot long + perp short; the USDC perp has no USDT partner; ETH has one leg in this frame
        "spot:BTC:binance:BTCUSDT>binance:BTCUSDT",
    ]
    p = next(x for x in pairs if x["id"] == "cross:BTC:binance:BTCUSDT>okx:BTC-USDT-SWAP")
    # funding from the SAME frame's observation; funding_px from the observation, else the quote's mark
    assert p["short"]["funding_rate"] == "0.0006" and p["short"]["funding_px"] == "100.025"
    assert p["long"]["funding_px"] == "100.005" and p["long"]["taker_fee"] == "0.0005" and p["long"]["quote_ms"] == T - 1000
    by = next(x for x in pairs if x["id"] == "cross:BTC:bybit:BTCUSDT>okx:BTC-USDT-SWAP")
    # a perp without an observation in this frame has no funding fields: economics unavailable, never zero
    assert by["long"]["funding_rate"] is None and by["long"]["interval_hours"] is None and by["long"]["funding_px"] is None
    f = ir.compute_features(by, {"horizon_hours": 8, "basis_stress": "0", "funding_estimate": "predicted"}, T)
    assert f["net_conservative"] is None and f["quote_age_ms"] is None
    spot = pairs[-1]
    assert spot["kind"] == "spot_perp" and spot["long"]["market"] == "spot" and spot["long"]["taker_fee"] == "0.001"
    # identified by time when the pack frame number is not known
    assert ir_views.scan_frame_pairs(_fake_pit(), None, T) == pairs


# --- the generic policy on the in-memory reader --------------------------------------------------------------------------

def _policy():
    import importlib.util

    spec = importlib.util.spec_from_file_location("ir_policy_under_test", POLICY_DIR / "strategy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Ctx(dict):
    pit = None


def _ctx(t, positions, pit):
    c = Ctx({"time_ms": t, "positions": positions})
    c.pit = pit
    return c


def test_the_policy_enters_both_legs_and_exits_only_what_the_engine_holds():
    pol = _policy()
    doc = _base_doc()
    doc["universe"]["pairs"] = [["binance", "okx"]]
    doc["entry"]["conditions"] = [{"feature": "funding_hourly_net", "op": ">", "value": "0"}]
    doc["entry"]["rank_by"] = "funding_hourly_net"
    doc["parameters"] = {}
    doc["exit"]["max_hold_hours"] = 1
    state = pol.initialize({"ir": doc, "qty_dp": 3})
    pit = _fake_pit()
    out = pol.on_step(_ctx(T, [], pit), state)
    ints = out["intents"]
    assert [(i["intent_id"], i["instrument"]["venue"], i["side"], i["qty"], i["limit_price"], i["position_ref"], i["pair_id"]) for i in ints] == [
        ("n1-L-open", "binance", "buy", "9.999", "100.01", "p1L", "pair-1"),
        ("n1-S-open", "okx", "sell", "9.999", "100.02", "p1S", "pair-1")]  # 1000/100.01 down to 3 dp; README §5a names
    assert all(i["order_type"] == "limit_ioc" and i["reduce_only"] is False and i["decision_time_ms"] == T for i in ints)
    st = out["state"]
    json.dumps(st)
    # only the long leg filled; one hour later max_hold fires and the close is for that leg only, at the bid
    pit2 = _fake_pit()
    for rows in pit2.rows.values():
        rows[:] = [Quote(q.t + 3_600_000, q.bid, q.ask, q.bbo, q.mark, q.volume_quote, q.quote_ts, q.frame + 60) for q in rows]
    for rows in pit2.obs.values():
        rows[:] = [Observation(o.t + 3_600_000, o.funding_rate, o.interval_hours, o.next_funding_ms, o.funding_px, o.funding_px_kind, o.frame + 60) for o in rows]
    held = [{"position_ref": "p1L", "instrument": {"venue": "binance", "market": "perp", "symbol": "BTCUSDT"}, "side": "long", "qty": "9.999",
             "avg_entry": "100.01"}]
    t2 = T + 3_600_000
    out2 = pol.on_step(_ctx(t2, held, pit2), st)
    assert [(i["intent_id"], i["side"], i["qty"], i["limit_price"], i["reduce_only"], i["position_ref"], i["pair_id"]) for i in out2["intents"]] == [
        ("n1-L-close", "sell", "9.999", "100", True, "p1L", "pair-1")]
    # the close filled: the pair is gone from the engine -> closed, base cools down from this step
    out3 = pol.on_step(_ctx(t2 + 60_000, [], pit2), out2["state"])
    assert out3["state"]["positions"] == {} and out3["state"]["cooldowns"] == {"BTC": t2}  # README §5: from the close decision (AlphaKeel closed_ms), not this later step


def test_an_entry_that_never_filled_is_dropped_without_a_cooldown():
    pol = _policy()
    doc = _base_doc()
    doc["entry"]["conditions"] = []
    doc["entry"]["rank_by"] = "funding_hourly_net"
    doc["parameters"] = {}
    st = pol.initialize({"ir": doc})
    out = pol.on_step(_ctx(T, [], _fake_pit()), st)
    assert len(out["intents"]) == 2 and len(out["state"]["positions"]) == 1
    out2 = pol.on_step(_ctx(T, [], _fake_pit()), out["state"])
    assert out2["state"]["cooldowns"] == {} and out2["state"]["next_trade"] == 3  # re-entered as pair-2


def _carry_doc(**exit_):
    doc = _base_doc()
    doc["universe"]["pairs"] = [["binance", "okx"]]
    doc["entry"]["conditions"] = []
    doc["entry"]["rank_by"] = "funding_hourly_net"
    doc["parameters"] = {}
    doc["exit"].update(exit_)
    return doc


def test_an_entry_with_an_estimated_leg_price_is_not_submitted_and_takes_no_trade_number():
    pol = _policy()
    st = pol.initialize({"ir": _carry_doc()})
    pit = _fake_pit()
    ok = Inst("okx", "perp", "BTC-USDT-SWAP")
    pit.rows[ok] = [Quote(q.t, q.bid, q.ask, False, q.mark, q.volume_quote, q.quote_ts, q.frame) for q in pit.rows[ok]]
    out = pol.on_step(_ctx(T, [], pit), st)
    assert out["intents"] == [] and out["state"]["positions"] == {} and out["state"]["next_trade"] == 1
    # the next submitted entry is trade 1 (Rust paper::open_planned refuses the same entry and opens nothing)
    out2 = pol.on_step(_ctx(T, [], _fake_pit()), out["state"])
    assert [i["intent_id"] for i in out2["intents"]] == ["n1-L-open", "n1-S-open"] and out2["state"]["next_trade"] == 2


def _held(net_l, net_s):
    return [{"position_ref": "p1L", "instrument": {"venue": "binance", "market": "perp", "symbol": "BTCUSDT"}, "side": "long", "qty": "9.999",
             "avg_entry": "100.01", "net_now": net_l},
            {"position_ref": "p1S", "instrument": {"venue": "okx", "market": "perp", "symbol": "BTC-USDT-SWAP"}, "side": "short", "qty": "9.999",
             "avg_entry": "100.02", "net_now": net_s}]


def test_pair_net_now_is_the_sum_of_the_engines_leg_values_and_unknown_if_either_leg_is():
    pol = _policy()
    # max_loss 20: the pair closes when its close-now net is <= -20 (README §5); exit conditions off, max_hold far away
    doc = _carry_doc(max_loss="20", conditions=[], max_hold_hours=72)
    out = pol.on_step(_ctx(T, [], _fake_pit()), pol.initialize({"ir": doc, "qty_dp": 3}))
    st = out["state"]
    t = T + 60_000
    pit = _fake_pit()
    for rows in pit.rows.values():
        rows[:] = [Quote(q.t + 60_000, q.bid, q.ask, q.bbo, q.mark, q.volume_quote, q.quote_ts, q.frame + 1) for q in rows]
    for rows in pit.obs.values():
        rows[:] = [Observation(o.t + 60_000, o.funding_rate, o.interval_hours, o.next_funding_ms, o.funding_px, o.funding_px_kind, o.frame + 1) for o in rows]
    assert pol._pair_net_now(st["positions"]["pair-1"], {p["position_ref"]: p for p in _held("-12.5", "-7.5")}) == "-20"
    closes = pol.on_step(_ctx(t, _held("-12.5", "-7.5"), pit), st)["intents"]
    assert [i["intent_id"] for i in closes] == ["n1-L-close", "n1-S-close"]
    assert pol.on_step(_ctx(t, _held("-12.5", "-7.4"), pit), st)["intents"] == []  # -19.9: held
    # one leg's value unknown (no quote for it in this frame) or one leg not held: the pair's net_now is unknown -> no max_loss
    assert pol._pair_net_now(st["positions"]["pair-1"], {p["position_ref"]: p for p in _held("-30", None)}) is None
    assert pol.on_step(_ctx(t, _held("-30", None), pit), st)["intents"] == []
    only_long = _held("-30", "0")[:1]
    assert pol._pair_net_now(st["positions"]["pair-1"], {p["position_ref"]: p for p in only_long}) is None


def test_the_policy_refuses_a_funding_source_it_cannot_serve():
    pol = _policy()
    doc = _set(_base_doc(), ["assumptions", "funding_estimate"], "last_settled")
    with pytest.raises(Exception) as e:
        pol.initialize({"ir": doc})
    assert getattr(e.value, "code", "") == "capability.unsupported"


# --- the local simulator's policy context carries the engine's per-leg net_now (README §5a) -----------------------------

def _sim_with(pos, quote, fee="0.0005"):
    from alphakeel_research import sim as S

    bn = Inst("binance", "perp", "BTCUSDT")
    s = S.Simulator.__new__(S.Simulator)
    s.pos, s.fee_rate, s.last_quote = {pos.ref: pos}, {bn: Decimal(fee)}, {} if quote is None else {bn: quote}
    s.accounts, s.run_id, s.profile = {}, "py-x", {"decision_clock": {"history_len": 0}}
    return s, bn


class _F:
    t, seq = T, 1


def test_sim_net_now_reproduces_the_rust_hand_value_and_its_null_rule():
    from alphakeel_research import sim as S

    bn = Inst("binance", "perp", "BTCUSDT")
    # AlphaKeel tests/research_service.rs: long 10 @ 1 (entry fee 10 x 1 x 0.0005), this frame's bid 0.999, no funding yet:
    # 10 x (0.999 - 1) + 0 - 0.005 - 10 x 0.999 x 0.0005 = -0.019995
    p = S.Pos("p1", bn, True, "USDT", net=Decimal(10), entry_qty=Decimal(10), entry_notional=Decimal(10), fees=Decimal("0.005"),
              fees_buy=Decimal("0.005"))
    sim, _ = _sim_with(p, (Decimal("0.999"), Decimal("1.001"), 3))
    view = sim._view(3, _F, [])
    assert view["positions"] == [{"position_ref": "p1", "instrument": bn.ref(), "side": "long", "qty": "10", "avg_entry": "1",
                                  "net_now": "-0.019995"}]
    # the quote is from an earlier frame (the engine's quote is not stamped with this decision time): unknown, not stale-valued
    assert sim._view(4, _F, [])["positions"][0]["net_now"] is None
    # short leg: qty x (avg - ask) + settled funding - entry-direction (sell) fees only - exit taker fee; a reducing buy's fee
    # is not an entry fee. 4 x (2 - 2.01) + 0.3 - 0.004 - 4 x 2.01 x 0.0005 = -0.04 + 0.3 - 0.004 - 0.00402 = 0.25198
    q = S.Pos("p2", bn, False, "USDT", net=Decimal(-4), entry_qty=Decimal(5), entry_notional=Decimal(10), fees=Decimal("0.006"),
              fees_sell=Decimal("0.004"), fees_buy=Decimal("0.002"), funding=Decimal("0.3"))
    sim, _ = _sim_with(q, (Decimal("2"), Decimal("2.01"), 0))
    assert sim._view(0, _F, [])["positions"][0]["net_now"] == "0.25198"
    # normalised decimal text like the service (no trailing zeros, no exponent)
    r = S.Pos("p3", bn, True, "USDT", net=Decimal(1), entry_qty=Decimal(1), entry_notional=Decimal("0.5"))
    sim, _ = _sim_with(r, (Decimal("1.5"), Decimal("1.6"), 0), fee="0")
    assert sim._view(0, _F, [])["positions"][0]["net_now"] == "1"


# --- (e) the example IR under the local simulator, on AlphaKeel's fixture pack --------------------------------------------

def test_the_example_ir_runs_locally_on_the_fixture_pack_and_writes_evidence(tmp_path):
    from alphakeel_research import evidence, workflow
    from alphakeel_research import strategy as strat
    from alphakeel_research.client import Client
    from tests.fixtures.alphakeel_server import start

    server = start()
    client = Client(server.url, server.token)
    try:
        pack = workflow.freeze_pack(client, {"window": {"start_ms": server.start_ms, "end_ms": server.end_ms}}, cache_dir=tmp_path / "cache")
        prof = client.render_profile(pack.id, "python_policy")["profile"]
        pack_dir = pack.export(tmp_path / "pack")  # downloads and verifies every object while the client is open
        doc = json.loads(EXAMPLE.read_text())
        params = {"ir": doc, "qty_dp": 6}
        man, bundle = strat.manifest(POLICY_DIR, name="ir_policy", parameters=params, seed=1)
        insts = [{"venue": "binance", "market": "perp", "symbol": "BTCUSDT"}, {"venue": "okx", "market": "perp", "symbol": "BTC-USDT-SWAP"}]
        sim, steps, sandbox = workflow.run_policy_local(pack, prof, POLICY_DIR, man, insts, run_id="py-ir-example", pack_dir=pack_dir)
        m, res = evidence.build_manifest(sim, pack=pack, run_id="py-ir-example", mode="python_policy",
                                         parameters={"profile": {}, "instruments": insts}, strategy=man, seed=1, sandbox=sandbox)
        d = evidence.write_run_dir(tmp_path / "run", m, res, sim, steps=steps, strategy_manifest=man, strategy_bundle=bundle)
    finally:
        client.close()
        server.stop()
    intents = [i for s in steps for i in s["response"]["intents"]]
    opens = [i for i in intents if not i["reduce_only"]]
    closes = [i for i in intents if i["reduce_only"]]
    assert len(opens) >= 2 and {i["side"] for i in opens} == {"buy", "sell"} and len({i["pair_id"] for i in opens}) >= 1
    first = opens[:2]
    assert first[0]["pair_id"] == first[1]["pair_id"] and first[0]["qty"] == first[1]["qty"]
    assert len(closes) >= 1
    assert {"run-manifest.json", "result.json", "events.jsonl", "steps.json", "strategy-manifest.json"} <= {p.name for p in d.iterdir()}
    assert res["counts"]["positions_closed"] == 2 and res["counts"]["positions_open_at_end"] == 0
    # same round trip as the hand-calculated carry_policy gold of the e2e tests: 100 per leg at the touch, held one hour
    assert res["amounts"]["realized"] == "1.6"
