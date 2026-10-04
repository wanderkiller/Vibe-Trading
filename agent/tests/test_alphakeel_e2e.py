"""Cross-project end-to-end tests: this Python client against AlphaKeel's real HTTP service (separate process).

Data are the offline scripted scans of AlphaKeel's fixture server (61 scans, 60 s apart, one funding boundary at +1 h with
an official settlement of -2%). Gold numbers are hand-calculated:
  round trip, long Binance 100 @ 1 -> 0.999 and short OKX 100 @ 1 -> 1.001, taker fees .05 + .05 + .04995 + .05005 = 0.2,
  price pnl -0.2, official funding on the long leg 100 x 1 x 0.02 = +2  =>  realized +1.6.
"""

from __future__ import annotations

import json
import os
import time
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from alphakeel_research import canon, contract, evidence, packfile, workflow
from alphakeel_research import strategy as strat
from alphakeel_research.client import Client, key_for
from alphakeel_research.errors import ApiError, FutureRead
from alphakeel_research.packfile import Inst, Pack, Pit
from alphakeel_research.policy import PolicyHost
from tests.fixtures.alphakeel_server import start

FIX = Path(__file__).parent / "fixtures" / "alphakeel"
BIN = {"venue": "binance", "market": "perp", "symbol": "BTCUSDT"}
OKX = {"venue": "okx", "market": "perp", "symbol": "BTC-USDT-SWAP"}


@pytest.fixture(scope="module")
def server():
    f = start()
    yield f
    f.stop()


@pytest.fixture(scope="module")
def client(server):
    c = Client(server.url, server.token)
    yield c
    c.close()


@pytest.fixture(scope="module")
def pack(server, client, tmp_path_factory):
    req = {"window": {"start_ms": server.start_ms, "end_ms": server.end_ms}}
    return workflow.freeze_pack(client, req, cache_dir=tmp_path_factory.mktemp("cache"))


def it(iid, t, inst, side, qty, price, reduce=False, ref=None, order_type="limit_ioc"):
    return {"intent_id": iid, "decision_time_ms": t, "instrument": inst, "side": side, "qty": qty, "order_type": order_type,
            "limit_price": None if order_type == "market" else price, "reduce_only": reduce, "position_ref": ref}


def round_trip(server):
    t0, t1 = server.start_ms, server.start_ms + server.hour_ms
    return [it("i-open-l", t0, BIN, "buy", "100", "1", ref="pl"), it("i-open-s", t0, OKX, "sell", "100", "1", ref="ps"),
            it("i-close-l", t1, BIN, "sell", "100", "0.999", True, "pl"), it("i-close-s", t1, OKX, "buy", "100", "1.001", True, "ps")]


# ------------------------------------------------------------------------------------------------------ data + loader

def test_connection_check_negotiates_the_contract_and_never_shows_the_credential(server, client):
    info = client.check()
    assert info["contract"] == "alphakeel.research/1"
    assert set(info["modes"]) == {"native_strategy", "fixed_intent_replay", "python_policy"}
    assert server.token not in repr(client) and server.token not in json.dumps(info)
    bad = Client(server.url, "ak_rs_tok-000000000000.ffff")
    with pytest.raises(ApiError) as e:
        bad.whoami()
    assert e.value.code == "auth.invalid" and e.value.status == 401 and e.value.request_id


def test_a_frozen_pack_is_downloaded_verified_and_read_with_exact_decimals(server, client, pack):
    lock = pack.lock
    assert lock["scans"]["frames"] == 61
    assert lock["funding"]["boundary_match"] == "nearest-within-60s-unique-v1"
    assert lock["coverage"]["pnl_backtest_complete"] is False  # OKX has no official settlements in the fixture dataset
    q = pack.quotes(Inst("binance", "perp", "BTCUSDT"), server.start_ms, server.end_ms)
    assert len(q) == 61 and q[0].bid == Decimal("0.999") and q[0].ask == Decimal("1") and isinstance(q[0].bid, Decimal)
    o = pack.observations(Inst("binance", "perp", "BTCUSDT"), server.start_ms, server.start_ms + 1)[0]
    assert o.funding_rate == Decimal("-0.01") and o.interval_hours == 1 and o.funding_px_kind == "mark"
    s = pack.settlements(Inst("binance", "perp", "BTCUSDT"), server.start_ms - 60_000, server.end_ms + 60_000)
    assert [x.rate for x in s] == [Decimal("-0.02")] and s[0].t == server.start_ms + server.hour_ms + 7 and s[0].event_kind == "regular"
    inst = pack.instruments()[Inst("okx", "perp", "BTC-USDT-SWAP")]
    assert inst.multiplier is None and inst.multiplier_status == "not_recorded_by_scanner"  # not invented
    fx = pack.fx(server.start_ms, server.end_ms)
    assert fx[0][1] == Decimal("1.0005")
    # the same read through the service gives the same rows (the service slices the same frozen pack)
    sl = client.slice(pack.id, table="quotes", venue="binance", market="perp", symbol="BTCUSDT", start_ms=server.start_ms, end_ms=server.start_ms + 120_001, limit=10)
    assert [r[2] for r in sl["rows"]] == ["0.999", "0.999", "0.999"]


def test_the_standard_reader_refuses_future_values_and_hides_unavailable_settlements(server, pack):
    pit = Pit(pack, server.start_ms + 120_000)
    assert len(pit.quotes(Inst("binance", "perp", "BTCUSDT"), server.start_ms)) == 3
    with pytest.raises(FutureRead) as e:
        pit.quotes(Inst("binance", "perp", "BTCUSDT"), server.start_ms, server.start_ms + 600_000)
    assert e.value.code == "data.future_read"
    assert pit.settlements(Inst("binance", "perp", "BTCUSDT"), server.start_ms - 60_000) == []  # not settled yet at as_of
    late = Pit(pack, server.end_ms + 600_000)
    assert len(late.settlements(Inst("binance", "perp", "BTCUSDT"), server.start_ms)) == 1


def test_reads_are_logged_as_actual_inputs_with_recomputable_chains(server, pack):
    pack.log.ranges.clear()
    pack.quotes(Inst("binance", "perp", "BTCUSDT"), server.start_ms, server.start_ms + 300_000)
    acc = pack.accesses()
    assert [a["table"] for a in acc] == ["quotes"] and acc[0]["rows"] == 5
    again = pack.accesses()
    assert acc == again
    ch = packfile.Chain()
    for r in pack._inst_rows("quotes", Inst("binance", "perp", "BTCUSDT"), server.start_ms, server.start_ms + 240_001):
        ch.push(r[0], r)
    assert ch.hexdigest() == acc[0]["chain_sha256"]


def test_cache_hits_need_verified_bytes_and_a_corrupt_cache_is_downloaded_again(server, client, tmp_path):
    p1 = Pack.open(client, workflow.freeze_pack(client, {"window": {"start_ms": server.start_ms, "end_ms": server.end_ms}}, cache_dir=tmp_path).id, tmp_path)
    obj = next(o for o in p1.lock["objects"] if o["role"] == "quotes")
    path = p1._path(obj)
    assert p1.cache.has(obj["content_sha256"])
    good = path.read_bytes()
    path.write_bytes(good[:-1] + b"X")                      # corrupt the cache
    assert not p1.cache.has(obj["content_sha256"])
    assert p1._path(obj).read_bytes() == good               # re-downloaded and verified
    (tmp_path / "content" / (obj["content_sha256"] + ".part")).write_bytes(b"half")  # a partial file is never a hit
    assert p1.cache.has(obj["content_sha256"])


def test_without_zstandard_the_service_decompresses_and_the_client_still_verifies(server, client, tmp_path, monkeypatch):
    monkeypatch.setattr(packfile, "_zstd", None)
    p = Pack.open(client, workflow.freeze_pack(client, {"window": {"start_ms": server.start_ms, "end_ms": server.end_ms}}, cache_dir=tmp_path).id, tmp_path / "fresh")
    assert len(p.quotes(Inst("binance", "perp", "BTCUSDT"), server.start_ms, server.end_ms)) == 61


def test_duckdb_slices_keep_decimals_exact_and_locate_rows(server, pack):
    con = pack.duckdb([Inst("binance", "perp", "BTCUSDT"), Inst("okx", "perp", "BTC-USDT-SWAP")], server.start_ms, server.start_ms + 300_000)
    assert con.sql("select typeof(bid) from quotes limit 1").fetchone()[0] == "VARCHAR"
    rows = con.sql("select symbol, bid, ask from quotes where t = {} order by symbol".format(server.start_ms)).fetchall()
    assert rows == [("BTC-USDT-SWAP", "1", "1.001"), ("BTCUSDT", "0.999", "1")]
    assert sorted(Decimal(r[1]) for r in rows) == [Decimal("0.999"), Decimal("1")]
    # a join over the observation table finds the contract whose predicted funding is negative
    neg = con.sql("select q.symbol from quotes q join observations o on o.t = q.t and o.symbol = q.symbol where q.t = {} and o.funding_rate like '-%'".format(server.start_ms)).fetchall()
    assert neg == [("BTCUSDT",)]


# ------------------------------------------------------------------------------------------------------ replay review

def test_python_backtest_is_reviewed_by_the_engine_and_the_hand_gold_holds(server, client, pack, tmp_path):
    intents = round_trip(server)
    prof = client.render_profile(pack.id, "fixed_intent_replay")["profile"]
    sim = workflow.python_replay(pack, prof, intents, run_id="py-gold")
    r = sim.result
    assert r["amounts"]["price_pnl"] == "-0.2" and r["amounts"]["fees"] == "0.2" and r["amounts"]["funding"] == "2" and r["amounts"]["realized"] == "1.6"
    assert r["by_currency"]["USDT"] == {"cash_flow": "-0.2", "fees": "0.2", "funding": "2"}
    ev = [e["data"] for e in sim.events if e["kind"] == "funding"]
    assert ev[0]["rate_source"] == "official_settlement" and ev[0]["rate"] == "-0.02" and ev[0]["amount"] == "2"
    rev = workflow.review_intents(client, pack, intents, cache_run_dir=tmp_path)
    rep = rev["report"]
    assert rep["passed"] is True, json.dumps(rep["layers"], indent=1) + json.dumps(rep["first_difference"])
    assert [rep["layers"][k]["status"] for k in ("L0", "L1", "L2", "L3", "L4")] == ["matched"] * 5
    assert "strategy logic is not certified" in rep["pass_scope"]
    # the engine produced its own facts: the numbers agree because both are right, not because one was copied
    res = json.loads(client.artifact(rev["alphakeel_run"], "result.json"))
    assert res["amounts"]["realized"] == "1.6" and res["verification"]["native_rule_ledger"] == "not_applicable"


def test_a_fee_error_and_a_funding_sign_error_injected_into_the_evidence_are_located(server, client, pack, tmp_path):
    intents = round_trip(server)
    prof = client.render_profile(pack.id, "fixed_intent_replay")["profile"]
    sim = workflow.python_replay(pack, prof, intents, run_id="py-inject")
    manifest, result = evidence.build_manifest(sim, pack=pack, run_id="py-inject", mode="fixed_intent_replay")
    # reference run in the engine
    up = client.upload("intents", canon.canonical_bytes(workflow.intents_doc(intents)))
    run = client.wait_run(client.create_run({"mode": "fixed_intent_replay", "pack_id": pack.id, "intents_sha256": up["sha256"]})["run_id"])
    # 1) fee error: the first fill's fee 0.05 -> 0.06
    sim.events[next(i for i, e in enumerate(sim.events) if e["kind"] == "fill")]["data"]["fee"] = "0.06"
    body = b"".join(canon.canonical_bytes(e) + b"\n" for e in [{"schema": "alphakeel.events/1", "run_id": "py-inject", "count": len(sim.events)}] + sim.events)
    sim.events_bytes = body
    sim.events_sha256 = canon.sha256_hex(body)
    result["event_log"]["sha256"] = sim.events_sha256
    manifest["outputs"]["events_sha256"] = sim.events_sha256
    manifest["outputs"]["result_sha256"] = canon.canonical_sha256(result)
    d = evidence.write_run_dir(tmp_path / "fee", manifest, result, sim)
    reg = evidence.register(client, d, pack.id, key="fee-injection-0123456789")
    rep = client.wait_comparison(client.compare(reg["run_id"], run["run_id"], "fixed_intent_replay")["comparison_id"])["report"]
    assert rep["passed"] is False
    first = rep["first_difference"]
    assert (first["layer"], first["class"]) == ("L3", "fee")
    assert first["a"] == "0.06" and first["b"] == "0.05" and Decimal(first["delta"]) == Decimal("0.01")
    assert rep["layers"]["L0"]["status"] == "matched" and rep["layers"]["L1"]["status"] == "matched"   # inputs and profile are fine: not an input problem
    # 2) funding sign error
    sim2 = workflow.python_replay(pack, prof, intents, run_id="py-sign")
    for e in sim2.events:
        if e["kind"] == "funding":
            e["data"]["amount"] = "-2"
    body = b"".join(canon.canonical_bytes(e) + b"\n" for e in [{"schema": "alphakeel.events/1", "run_id": "py-sign", "count": len(sim2.events)}] + sim2.events)
    sim2.events_bytes, sim2.events_sha256 = body, canon.sha256_hex(body)
    m2, r2 = evidence.build_manifest(sim2, pack=pack, run_id="py-sign", mode="fixed_intent_replay")
    r2["event_log"]["sha256"] = sim2.events_sha256
    m2["outputs"]["events_sha256"] = sim2.events_sha256
    m2["outputs"]["result_sha256"] = canon.canonical_sha256(r2)
    reg2 = evidence.register(client, evidence.write_run_dir(tmp_path / "sign", m2, r2, sim2), pack.id, key="sign-injection-0123456789")
    rep2 = client.wait_comparison(client.compare(reg2["run_id"], run["run_id"], "fixed_intent_replay")["comparison_id"])["report"]
    assert rep2["passed"] is False and rep2["first_difference"]["class"] == "funding"
    assert rep2["first_difference"]["a"] == "-2" and rep2["first_difference"]["b"] == "2"
    assert rep2["first_difference"]["key"].endswith(":amount")


def test_forged_hashes_and_altered_inputs_are_rejected_or_reported_by_the_service(server, client, pack, tmp_path):
    intents = round_trip(server)
    prof = client.render_profile(pack.id, "fixed_intent_replay")["profile"]
    sim = workflow.python_replay(pack, prof, intents, run_id="py-forge")
    manifest, result = evidence.build_manifest(sim, pack=pack, run_id="py-forge", mode="fixed_intent_replay")
    d = evidence.write_run_dir(tmp_path / "ok", manifest, result, sim)
    # a self-reported events hash that is not the hash of the uploaded events
    bad = json.loads((d / "run-manifest.json").read_text())
    bad["outputs"]["events_sha256"] = "00" * 32
    (d / "run-manifest.json").write_bytes(canon.canonical_bytes(bad))
    with pytest.raises(ApiError) as e:
        evidence.register(client, d, pack.id, key="forge1-0123456789abcdef")
    assert e.value.code == "evidence.hash_mismatch"
    # an altered input digest registers (the evidence is self-consistent) but is flagged and fails L0 when compared
    good = json.loads(json.dumps(manifest))
    good["inputs"][0]["chain_sha256"] = "ee" * 32
    (d / "run-manifest.json").write_bytes(canon.canonical_bytes(good))
    reg = evidence.register(client, d, pack.id, key="forge2-0123456789abcdef")
    assert len(reg["artifacts"]["registration"]["input_check"]["mismatched"]) == 1
    up = client.upload("intents", canon.canonical_bytes(workflow.intents_doc(intents)))
    run = client.wait_run(client.create_run({"mode": "fixed_intent_replay", "pack_id": pack.id, "intents_sha256": up["sha256"]})["run_id"])
    rep = client.wait_comparison(client.compare(reg["run_id"], run["run_id"], "fixed_intent_replay")["comparison_id"])["report"]
    assert rep["layers"]["L0"]["status"] == "mismatch" and rep["passed"] is False and rep["layers"]["L3"]["status"] == "not_comparable"


# ------------------------------------------------------------------------------------------------------ policy

def policy_dir_manifest(path: Path, parameters: dict, seed: int = 7):
    return strat.manifest(path, name=path.name, parameters=parameters, seed=seed)


def test_the_same_policy_runs_locally_and_under_alphakeels_state_and_the_comparison_passes(server, client, pack, tmp_path):
    from alphakeel_research import policy_flow

    t_close = server.start_ms + server.hour_ms
    params = {"qty": "100", "close_at_ms": t_close}
    d = tmp_path / "carry_policy"
    import shutil

    shutil.copytree(FIX / "carry_policy", d)
    r = policy_flow.run_both(client, pack, str(d), [BIN, OKX], profile=None, seed=7, parameters=params, work=tmp_path / "work")
    assert r["passed"] is True, json.dumps(r, indent=1)
    assert [r["layers"][k] for k in ("L0", "L1", "L2", "L3", "L4")] == ["matched"] * 5
    assert r["sandbox"]["credentials_in_environment"] is False and r["sandbox"]["resource_limits"] is True
    res = json.loads(client.artifact(r["alphakeel_run"], "result.json"))
    assert res["mode"] == "python_policy" and res["amounts"]["realized"] == "1.6"
    # every step is recorded on the server: state, response and what the engine did with it
    first = client.get_step(r["alphakeel_run"], 0)
    assert first["state"] == "executed" and len(first["results"]) == 2
    lineage = client.lineage(r["alphakeel_run"])
    assert lineage["pack_id"] == pack.id and len(lineage["comparisons"]) == 1


def test_a_policy_that_reacts_to_a_denied_order_gets_the_engines_real_state_in_both_modes(server, client, pack, tmp_path):
    from alphakeel_research import policy_flow
    import shutil

    d = tmp_path / "retry_policy"
    shutil.copytree(FIX / "retry_policy", d)
    t_close = server.start_ms + server.hour_ms
    # 150 USDT per venue: the big order (200) is denied by the engine, the small one (100) fills.
    r = policy_flow.run_both(client, pack, str(d), [BIN], profile={"starting_balance_per_venue": "150"}, seed=1,
                             parameters={"big": "200", "small": "100", "close_at_ms": t_close}, work=tmp_path / "work")
    assert r["passed"] is True, json.dumps(r, indent=1)
    s1 = client.get_step(r["alphakeel_run"], 1)
    statuses = {x["intent_id"]: x["status"] for x in s1["context"]["last_results"]}
    assert statuses == {"i-big": "denied"} and s1["context"]["positions"] == []
    s2 = client.get_step(r["alphakeel_run"], 2)
    assert [p["position_ref"] for p in s2["context"]["positions"]] == ["small"]


def test_a_lost_reply_is_recovered_by_asking_not_by_calling_the_strategy_again(server, client, pack, tmp_path, monkeypatch):
    import shutil

    d = tmp_path / "carry_policy"
    shutil.copytree(FIX / "carry_policy", d)
    marker = tmp_path / "calls.log"
    s = (d / "strategy.py").read_text().replace("def on_step(ctx, state):", f"def on_step(ctx, state):\n    open({str(marker)!r}, 'a').write(str(ctx['step_seq']) + '\\n')")
    (d / "strategy.py").write_text(s)
    man, bundle = policy_dir_manifest(d, {"qty": "100", "close_at_ms": server.start_ms + server.hour_ms})
    client.register_strategy(man, client.upload("strategy-bundle", bundle)["sha256"])
    real = client.submit_step
    state = {"dropped": 0}

    def flaky(run_id, seq, resp):
        out = real(run_id, seq, resp)      # the server accepts it …
        if seq == 0 and state["dropped"] == 0:
            state["dropped"] += 1
            raise ApiError("service.unavailable", "the reply was lost", retryable=True)  # … but the reply never arrives
        return out

    monkeypatch.setattr(client, "submit_step", flaky)
    pack_dir = pack.export(tmp_path / "pk")
    out = workflow.run_policy_remote(client, pack, d, man, [BIN, OKX], profile=None, journal_dir=tmp_path / "journal", pack_dir=pack_dir)
    assert out["run"]["status"] == "complete"
    assert state["dropped"] == 1
    calls = marker.read_text().split()
    assert calls.count("0") == 1, "the strategy was called once for step 0 even though its submission was retried"
    assert len(calls) == 61
    # a second client run with the same journal re-sends the stored response (no new decision is generated)
    saved = json.loads((tmp_path / "journal" / "step-000000.json").read_text())
    assert saved["response"]["step_seq"] == 0 and saved["response"]["intents"][0]["intent_id"] == "i-open-l"


def test_a_silent_client_interrupts_the_session_and_a_child_run_continues_from_the_same_pack(server, client, pack, tmp_path):
    import shutil

    d = tmp_path / "carry_policy"
    shutil.copytree(FIX / "carry_policy", d)
    man, bundle = policy_dir_manifest(d, {"qty": "100", "close_at_ms": server.start_ms + server.hour_ms})
    client.register_strategy(man, client.upload("strategy-bundle", bundle)["sha256"])
    run = client.create_run({"mode": "python_policy", "pack_id": pack.id, "strategy_id": man["strategy_id"], "seed": 7, "instruments": [BIN],
                             "policy": {"step_timeout_ms": 1000}})
    assert client.next_step(run["run_id"], 5000)["state"] == "awaiting"
    done = client.wait_run(run["run_id"], timeout=30)       # we never answer: not an empty decision
    assert done["status"] == "interrupted" and done["error"]["code"] == "policy.timeout"
    assert client.get_step(run["run_id"], 0)["state"] == "awaiting" and client.get_step(run["run_id"], 0).get("response") is None
    out = workflow.run_policy_remote(client, pack, d, man, [BIN, OKX], profile=None, journal_dir=tmp_path / "j2", pack_dir=pack.export(tmp_path / "pk"), parent_run_id=run["run_id"])
    assert out["run"]["status"] == "complete" and out["run"]["parent_run_id"] == run["run_id"]
    assert client.lineage(out["run"]["run_id"])["ancestors"][1]["run_id"] == run["run_id"]


def test_strategy_runs_in_a_sandbox_without_credentials_and_a_hang_kills_the_process_tree(server, pack, tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHAKEEL_RESEARCH_TOKEN", "must-not-leak")
    monkeypatch.setenv("SOME_API_KEY", "must-not-leak")
    pack_dir = pack.export(tmp_path / "pk")
    with PolicyHost(FIX / "probe_policy" / "strategy.py", pack_dir=pack_dir) as h:
        h.start({})                      # raises inside the child if a credential-like variable or the real home is visible
        assert h.step({"time_ms": server.start_ms, "step_seq": 0})["intents"] == []
        assert h.sandbox["credentials_in_environment"] is False and h.sandbox["ephemeral_home"] is True
    pidfile = tmp_path / "child.pid"
    h = PolicyHost(FIX / "sleepy_policy" / "strategy.py", call_timeout=0.8)
    try:
        h.start({"pidfile": str(pidfile)})
        with pytest.raises(ApiError) as e:
            h.step({"time_ms": 1, "step_seq": 0})
        assert e.value.code == "policy.timeout"
    finally:
        h.close()
    child = int(pidfile.read_text())
    time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)                # the grandchild is gone with its process group


def test_policy_state_must_be_exact_json_and_intents_must_be_an_explicit_list(tmp_path):
    bad = tmp_path / "bad_state"
    bad.mkdir()
    (bad / "strategy.py").write_text("def initialize(p):\n    return {'x': 1.5}\n\ndef on_step(c, s):\n    return {'intents': [], 'state': s}\n")
    with pytest.raises(ApiError) as e:
        PolicyHost(bad / "strategy.py").start({})
    assert e.value.code == "engine.failure" and "floating-point" in e.value.message
    none = tmp_path / "no_list"
    none.mkdir()
    (none / "strategy.py").write_text("def initialize(p):\n    return {}\n\ndef on_step(c, s):\n    return {'intents': None, 'state': s}\n")
    with PolicyHost(none / "strategy.py") as h:
        h.start({})
        with pytest.raises(ApiError) as e2:
            h.step({"time_ms": 1})
        assert "explicit" in e2.value.message or "list" in e2.value.message


def test_strategy_bundles_replay_in_a_clean_directory_with_every_digest_checked(tmp_path):
    import shutil

    d = tmp_path / "s"
    shutil.copytree(FIX / "carry_policy", d)
    man, bundle = strat.manifest(d, name="carry", parameters={"qty": "100", "close_at_ms": 1}, seed=3)
    assert contract.validate_doc(man) == "strategy-manifest"
    assert strat.make_bundle(d)[0] == bundle           # deterministic bytes
    out = strat.extract_bundle(bundle, tmp_path / "clean", man["content_sha256"])
    assert (out / "strategy.py").read_text() == (d / "strategy.py").read_text()
    with pytest.raises(Exception):
        strat.extract_bundle(bundle, tmp_path / "clean2", "00" * 32)


# ------------------------------------------------------------------------------------------------------ native + params

def test_native_strategy_runs_through_the_service_and_params_are_validated_like_the_gui(server, client, pack):
    good = {"schema": "alphakeel.handoff.params/1", "id": "vt-e2e-0001", "strategy": "cross_venue_carry", "summary": "fixture",
            "source": {"tool": "vibe-trading"}, "rules": {"max_loss": {"value": "0.015"}, "max_hold_hours": {"value": 48}}}
    v = client.validate_params(good, pack.id)
    assert v["valid"] is True and v["rules"]["max_loss"] == "0.015"
    bad = dict(good, strategy="momentum", rules={"alpha": {"value": "1"}})
    vb = client.validate_params(bad, pack.id)
    assert vb["valid"] is False and any("not an AlphaKeel rule" in p for p in vb["problems"])
    r = workflow.native_run(client, pack, v["rules"], profile={"funding": {"mode": "estimate"}})
    assert r["result"]["mode"] == "native_strategy"
    assert r["result"]["verification"]["native_rule_ledger"] in ("matched", "mismatch")
    assert r["result"]["execution_ok"] is True
    # same rules, same pack, same profile: the engine facts are identical
    again = workflow.native_run(client, pack, v["rules"], profile={"funding": {"mode": "estimate"}})
    assert again["facts_sha256"] == r["facts_sha256"]


# ------------------------------------------------------------------------------------------------------ client rules

def test_only_safe_requests_are_retried_and_errors_keep_their_structure():
    calls = {"get": 0, "post": 0, "post_key": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            calls["get"] += 1
            return httpx.Response(503, json={"error": {"code": "service.unavailable", "message": "busy", "retryable": True, "request_id": "r1"}}) if calls["get"] < 3 else httpx.Response(200, json={"ok": True})
        if req.headers.get("idempotency-key"):
            calls["post_key"] += 1
            if calls["post_key"] < 2:
                raise httpx.ConnectError("boom")
            return httpx.Response(202, json={"job_id": "j"})
        calls["post"] += 1
        raise httpx.ConnectError("boom")

    c = Client("http://x", "ak_rs_tok-1.s", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    assert c.get("/whoami") == {"ok": True} and calls["get"] == 3
    assert c.post("/runs", {"a": 1}, key="k" * 20) == {"job_id": "j"} and calls["post_key"] == 2
    with pytest.raises(ApiError) as e:
        c.post("/runs", {"a": 1})                 # no idempotency key: a blind retry could run the job twice
    assert e.value.code == "service.unavailable" and calls["post"] == 1 and e.value.retryable
    assert key_for("run", {"a": 1}) == key_for("run", {"a": 1}) != key_for("run", {"a": 2})
    c2 = Client("http://x", "ak_rs_tok-1.s", transport=httpx.MockTransport(lambda r: httpx.Response(422, json={"error": {"code": "request.invalid", "message": "bad", "field": "window", "retryable": False, "request_id": "r9"}})))
    with pytest.raises(ApiError) as e2:
        c2.get("/coverage")
    assert (e2.value.code, e2.value.field, e2.value.request_id, e2.value.status) == ("request.invalid", "window", "r9", 422)


def test_a_service_with_another_contract_major_is_refused(monkeypatch):
    def handler(req):
        if req.url.path.endswith("/whoami"):
            return httpx.Response(200, json={"credential": "t", "scopes": []})
        return httpx.Response(200, json={"schemas": {"supported_major": 2}, "contract": "alphakeel.research/2", "modes": [], "limits": {}})

    c = Client("http://x", "ak_rs_tok-1.s", transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError) as e:
        c.check()
    assert e.value.code == "contract.unknown_major"
