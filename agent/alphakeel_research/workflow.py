"""High-level flows used by the CLI and the Vibe-Trading tool. Each returns plain dicts of references and summaries."""

from __future__ import annotations

import json
import os
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import canon, evidence
from .client import Client, key_for
from .errors import ApiError
from .packfile import Inst, Pack
from .policy import PolicyHost, StepJournal, response_doc
from .sim import Simulator, dp_of

DEFAULT_CACHE = Path(os.environ.get("ALPHAKEEL_RESEARCH_CACHE", Path.home() / ".vibe-trading" / "alphakeel" / "packs"))


def freeze_pack(client: Client, request: dict, *, cache_dir: str | Path | None = None, timeout: float = 900.0) -> Pack:
    """Create (or find, by key) a frozen data pack and open it locally with verified content."""
    job = client.create_pack(request)
    done = client.wait_pack(job["job_id"], timeout=timeout)
    if done["status"] != "ready":
        err = done.get("error") or {}
        raise ApiError(err.get("code", "engine.failure"), err.get("message", f"the pack job ended as {done['status']}"), retryable=bool(err.get("retryable")))
    return Pack.open(client, done["pack_id"], cache_dir or DEFAULT_CACHE)


def intents_doc(intents: list[dict]) -> dict:
    return {"schema": "alphakeel.intents/1", "intents": intents}


def intent_price_dp(intents: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    from decimal import Decimal

    for it in intents:
        if it.get("limit_price") is not None:
            leg = Inst.parse(it["instrument"]).leg()
            out[leg] = max(out.get(leg, 0), dp_of(Decimal(it["limit_price"])))
    return out


def declared_of(intents: list[dict], declared: list[dict] | None) -> list[Inst]:
    seen: dict[Inst, None] = {}
    for it in intents:
        seen.setdefault(Inst.parse(it["instrument"]), None)
    for d in declared or []:
        seen.setdefault(Inst.parse(d), None)
    return list(seen)


def python_replay(pack: Pack, profile: dict, intents: list[dict], *, declared: list[dict] | None = None, run_id: str) -> Any:
    """Independent Python execution and accounting of a fixed intent list (a local, Decimal-exact backtest)."""
    insts = declared_of(intents, declared)
    sim = Simulator(pack, profile, insts, run_id=run_id, intent_price_dp=intent_price_dp(intents))
    by_time: dict[int, list[dict]] = {}
    for it in intents:
        by_time.setdefault(it["decision_time_ms"], []).append(it)
    unknown = set(by_time) - {f.t for f in sim.frames}
    if unknown:
        raise ApiError("intent.not_a_decision_time", f"{sorted(unknown)[0]} is not a scan decision time of this pack")
    out = sim.run(lambda k, t, view: by_time.get(t, []))
    return out


def review_intents(client: Client, pack: Pack, intents: list[dict], *, profile: dict | None = None, declared: list[dict] | None = None,
                   cache_run_dir: str | Path, run_id: str | None = None, wait: float = 300.0) -> dict:
    """Run the intents locally (own accounting), have AlphaKeel execute them independently, register the local evidence
    and ask for the layered comparison. Returns references and the comparison summary."""
    prof_doc = client.render_profile(pack.id, "fixed_intent_replay", profile)["profile"]
    local_id = run_id or f"py-{key_for('replay', {'pack': pack.id, 'intents': intents, 'profile': profile})[:20]}"
    sim = python_replay(pack, prof_doc, intents, declared=declared, run_id=local_id)
    manifest, result = evidence.build_manifest(sim, pack=pack, run_id=local_id, mode="fixed_intent_replay",
                                               parameters={"profile": profile or {}, "instruments": declared or []})
    d = evidence.write_run_dir(Path(cache_run_dir) / local_id, manifest, result, sim)
    reg = evidence.register(client, d, pack.id)
    up = client.upload("intents", canon.canonical_bytes(intents_doc(intents)))
    body = {"mode": "fixed_intent_replay", "pack_id": pack.id, "intents_sha256": up["sha256"], "profile": profile, "instruments": declared or []}
    run = client.create_run({k: v for k, v in body.items() if v is not None})
    done = client.wait_run(run["run_id"], timeout=wait)
    if done["status"] != "complete":
        raise ApiError((done.get("error") or {}).get("code", "engine.failure"), (done.get("error") or {}).get("message", done["status"]),
                       job_id=done.get("run_id"))
    cmp = client.compare(reg["run_id"], done["run_id"], "fixed_intent_replay")
    report = client.wait_comparison(cmp["comparison_id"], timeout=wait)
    return {"local_run": reg["run_id"], "alphakeel_run": done["run_id"], "comparison": cmp["comparison_id"], "status": report["status"],
            "report": report.get("report"), "run_dir": str(d)}


def native_run(client: Client, pack: Pack, rules: dict, *, profile: dict | None = None, wait: float = 900.0) -> dict:
    run = client.create_run({"mode": "native_strategy", "pack_id": pack.id, "rules": rules, **({"profile": profile} if profile else {})})
    done = client.wait_run(run["run_id"], timeout=wait)
    if done["status"] != "complete":
        raise ApiError((done.get("error") or {}).get("code", "engine.failure"), (done.get("error") or {}).get("message", done["status"]),
                       job_id=done.get("run_id"))
    result = json.loads(client.artifact(done["run_id"], "result.json"))
    return {"run_id": done["run_id"], "result": result, "manifest_sha256": done["manifest_sha256"], "facts_sha256": done["facts_sha256"]}


# ---------------------------------------------------------------------------------------------------------------------
# policy: the same strategy locally and under AlphaKeel's actual state
# ---------------------------------------------------------------------------------------------------------------------

def run_policy_local(pack: Pack, profile: dict, strategy_dir: str | Path, strategy_manifest: dict, instruments: list[dict], *, run_id: str,
                     pack_dir: str | Path, call_timeout: float = 30.0) -> tuple[Any, list[dict], dict]:
    """The policy against the LOCAL simulator's state (same context contract as the remote session)."""
    insts = [Inst.parse(i) for i in instruments]
    sim = Simulator(pack, profile, insts, run_id=run_id)
    steps: list[dict] = []
    states: list[str | None] = []
    host = PolicyHost(Path(strategy_dir) / strategy_manifest["entry"]["script"], pack_dir=pack_dir, call_timeout=call_timeout)
    try:
        host.start(strategy_manifest["parameters"], strategy_manifest["seed"])

        def decide(k: int, t: int, view: dict) -> list[dict]:
            out = host.step(view)
            ctx_sha = canon.canonical_sha256(view)
            resp = response_doc(run_id, k, ctx_sha, strategy_manifest["content_sha256"], pack.id, out["intents"], out["state_sha256"])
            steps.append({"step_seq": k, "context": view, "response": resp})
            states.append(out["state_sha256"])
            return out["intents"]

        res = sim.run(decide, policy_steps=True, state_hashes=states)
    finally:
        sandbox = host.sandbox
        host.close()
    return res, steps, sandbox


def run_policy_remote(client: Client, pack: Pack, strategy_dir: str | Path, strategy_manifest: dict, instruments: list[dict], *, profile: dict | None,
                      journal_dir: str | Path, pack_dir: str | Path, step_timeout_ms: int = 60_000, call_timeout: float = 30.0, wait: float = 1800.0,
                      parent_run_id: str | None = None) -> dict:
    """The same policy driven by AlphaKeel's actual simulated state, one persisted step at a time."""
    body = {"mode": "python_policy", "pack_id": pack.id, "strategy_id": strategy_manifest["strategy_id"], "instruments": instruments,
            "seed": strategy_manifest["seed"], "policy": {"step_timeout_ms": step_timeout_ms}}
    if profile:
        body["profile"] = profile
    if parent_run_id:
        body["parent_run_id"] = parent_run_id
    run = client.create_run(body)
    run_id = run["run_id"]
    journal = StepJournal(journal_dir)
    host = PolicyHost(Path(strategy_dir) / strategy_manifest["entry"]["script"], pack_dir=pack_dir, call_timeout=call_timeout)
    deadline = time.monotonic() + wait
    try:
        host.start(strategy_manifest["parameters"], strategy_manifest["seed"])
        while True:
            if time.monotonic() > deadline:
                client.cancel_run(run_id)
                raise ApiError("service.unavailable", "the session did not finish in time and was cancelled", retryable=True)
            st = client.next_step(run_id, 20_000)
            state = st.get("state")
            if state == "terminal":
                break
            if state != "awaiting":
                continue
            seq = st["step_seq"]
            saved = journal.get(seq)
            if saved is None:
                out = host.step(st["context"])
                resp = response_doc(run_id, seq, st["context_sha256"], strategy_manifest["content_sha256"], pack.id, out["intents"], out["state_sha256"])
                journal.put(seq, st["context"], resp)  # persist BEFORE submitting
            else:
                resp = saved["response"]  # a retry re-sends the stored response; the strategy is not called again
            _submit(client, run_id, seq, resp)
    finally:
        sandbox = host.sandbox
        host.close()
    done = client.wait_run(run_id, timeout=120.0)
    return {"run": done, "sandbox": sandbox, "steps": journal.all()}


def _submit(client: Client, run_id: str, seq: int, resp: dict) -> None:
    """Submit; if the reply was lost, ask whether the response was accepted before doing anything else."""
    for attempt in range(4):
        try:
            client.submit_step(run_id, seq, resp)
            return
        except ApiError as e:
            if e.code == "service.unavailable" or (e.retryable and e.code != "policy.not_waiting"):
                try:
                    rec = client.get_step(run_id, seq)
                    if rec.get("state") in ("accepted", "executed") and rec.get("response_sha256") == canon.canonical_sha256(resp):
                        return
                except ApiError:
                    pass
                time.sleep(0.3 * (attempt + 1))
                continue
            raise
    raise ApiError("service.unavailable", f"could not confirm step {seq}", retryable=True)


FAULTS = ("fee", "funding-sign")


def inject_fault(client: Client, pack: Pack, intents: list[dict], fault: str, *, profile: dict | None = None, work: str | Path) -> dict:
    """Corrupt ONE value of a correct local run's evidence (a fee, or a funding sign), re-derive its hashes so the evidence is
    self-consistent, and have the service compare it with the engine's own run. The report must locate the corrupted value,
    both sides' values and the difference; this demonstrates (and regression-tests) the comparison, it is not a backtest."""
    if fault not in FAULTS:
        raise ApiError("request.invalid", f"fault must be one of {FAULTS}", field="fault")
    prof_doc = client.render_profile(pack.id, "fixed_intent_replay", profile)["profile"]
    rid = f"py-inject-{fault}-{int(time.time())}"
    sim = python_replay(pack, prof_doc, intents, run_id=rid)
    if fault == "fee":
        target = next((e for e in sim.events if e["kind"] == "fill"), None)
        if target is None:
            raise ApiError("request.invalid", "the intents produced no fill to corrupt")
        before = target["data"]["fee"]
        target["data"]["fee"] = str(Decimal(before) + Decimal("0.01"))
    else:
        target = next((e for e in sim.events if e["kind"] == "funding"), None)
        if target is None:
            raise ApiError("request.invalid", "the run settled no funding: choose a window that crosses a boundary")
        before = target["data"]["amount"]
        target["data"]["amount"] = str(-Decimal(before))
    body = b"".join(canon.canonical_bytes(e) + b"\n" for e in [{"schema": "alphakeel.events/1", "run_id": rid, "count": len(sim.events)}] + sim.events)
    sim.events_bytes, sim.events_sha256 = body, canon.sha256_hex(body)
    manifest, result = evidence.build_manifest(sim, pack=pack, run_id=rid, mode="fixed_intent_replay", parameters={"profile": profile or {}, "injected_fault": fault})
    result["event_log"]["sha256"] = sim.events_sha256
    manifest["outputs"]["events_sha256"] = sim.events_sha256
    manifest["outputs"]["result_sha256"] = canon.canonical_sha256(result)
    d = evidence.write_run_dir(Path(work) / rid, manifest, result, sim)
    reg = evidence.register(client, d, pack.id)
    up = client.upload("intents", canon.canonical_bytes(intents_doc(intents)))
    run = client.wait_run(client.create_run({"mode": "fixed_intent_replay", "pack_id": pack.id, "intents_sha256": up["sha256"], **({"profile": profile} if profile else {})})["run_id"])
    rep = client.wait_comparison(client.compare(reg["run_id"], run["run_id"], "fixed_intent_replay")["comparison_id"]).get("report") or {}
    return {"injected": {"fault": fault, "was": before,
                         "now": target["data"]["fee" if fault == "fee" else "amount"]},
            "local_run": reg["run_id"], "alphakeel_run": run["run_id"], "passed": rep.get("passed"),
            "layers": {k: v["status"] for k, v in rep.get("layers", {}).items()}, "first_difference": rep.get("first_difference"), "pass_scope": rep.get("pass_scope")}


# ---------------------------------------------------------------------------------------------------------------------
# Strategy IR: the same IR in Python (ir_policy under the local simulator) and in AlphaKeel's Rust evaluator
# ---------------------------------------------------------------------------------------------------------------------

IR_POLICY_DIR = Path(__file__).parent / "ir_policy"


def validate_ir(client: Client, ir_doc: dict, pack_id: str | None = None) -> dict:
    """``POST /validate/ir``: AlphaKeel's own check of the document (and, with a pack, against that pack's scans)."""
    return client.validate_ir(ir_doc, pack_id)


def ir_instruments(pack: Pack, ir_doc: dict) -> list[dict]:
    """The contracts an IR can trade on this pack: selected, of the IR's shape and quote currencies, on its venues, base
    not excluded, with a taker fee in the pack's fee table (README §4.1a); declared to the local simulator up front."""
    from . import ir as IR

    loaded = IR.load(ir_doc)
    u = loaded.universe
    markets = {"cross_perp": {"perp"}, "spot_perp": {"spot", "perp"}}[u["shape"]]
    venues = None if u["pairs"] == "any" else {v for pair in u["pairs"] for v in pair}
    excluded = {IR.ascii_upper(b) for b in u.get("excluded_bases", [])}
    fees = pack.fees().get("fees", {})
    out = []
    for inst, meta in sorted(pack.instruments().items(), key=lambda kv: kv[0].leg()):
        if not meta.selected or inst.market not in markets or meta.quote not in u["quote_ccys"]:
            continue
        if (venues is not None and inst.venue not in venues) or IR.ascii_upper(meta.base) in excluded:
            continue
        if fees.get(inst.venue, {}).get(inst.market) in (None, ""):
            continue
        out.append(inst.ref())
    if not out:
        raise ApiError("data.insufficient", "the pack has no contract this IR could trade")
    return out


def _side_summary(result: dict) -> dict:
    c, a = result.get("counts", {}), result.get("amounts", {})
    closed, still = c.get("positions_closed"), c.get("positions_open_at_end")
    return {"positions_opened": None if closed is None or still is None else closed + still, "positions_closed": closed,
            "positions_open_at_end": still, "intents": c.get("intents"), "fills": c.get("fills"), "realized": a.get("realized")}


def ir_review(client: Client, pack: Pack, ir_doc: dict, *, out_dir: str | Path, profile: dict | None = None, seed: int = 0,
              qty_dp: int = 8, wait: float = 1800.0) -> dict:
    """Reconcile one Strategy IR between Vibe-Trading and AlphaKeel on one frozen pack.

    (a) the generic ``ir_policy`` runs this IR under the LOCAL simulator (the policy flow; evidence in ``out_dir``);
    (b) AlphaKeel runs the same IR with its Rust evaluator (``mode: ir_strategy``) on the same pack;
    (c) the local run is registered as an external ``ir_strategy`` run and the service compares the two layer by layer.
    The execution profile is the one the service renders for ``ir_strategy`` (so L1 is comparable). Returns, and writes as
    ``reconciliation.json``, the references and the comparison output; it approves nothing.
    """
    from . import ir as IR
    from . import strategy as strat

    loaded = IR.load(ir_doc)
    sid, ssha = loaded.strategy_id, canon.canonical_sha256(loaded.definition)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    prof_doc = client.render_profile(pack.id, "ir_strategy", profile)["profile"]
    insts = ir_instruments(pack, loaded.doc)
    params = {"ir": loaded.doc, "qty_dp": qty_dp}
    manifest, bundle = strat.manifest(IR_POLICY_DIR, name="ir_policy", parameters=params, seed=seed)
    pack_dir = pack.export(out / "pack")
    local_id = f"py-{sid}-{int(time.time())}"
    sim, steps, sandbox = run_policy_local(pack, prof_doc, IR_POLICY_DIR, manifest, insts, run_id=local_id, pack_dir=pack_dir)
    man, res = evidence.build_manifest(sim, pack=pack, run_id=local_id, mode="ir_strategy",
                                       parameters={"profile": profile or {}, "ir": loaded.doc, "instruments": insts,
                                                   "policy": {"script": "ir_policy", "content_sha256": manifest["content_sha256"], "qty_dp": qty_dp}},
                                       strategy={"strategy_id": sid, "content_sha256": ssha}, seed=seed, sandbox=sandbox,
                                       claims=["Python Strategy IR implementation (ir_policy) decided under the local Decimal simulator's state",
                                               "does not certify the research backtest the IR came from"])
    run_dir = evidence.write_run_dir(out / local_id, man, res, sim, steps=steps, strategy_manifest=manifest, strategy_bundle=bundle)
    reg = evidence.register(client, run_dir, pack.id)
    body = {"mode": "ir_strategy", "pack_id": pack.id, "ir": loaded.doc}
    if profile:
        body["profile"] = profile
    run = client.wait_run(client.create_run(body)["run_id"], timeout=wait)
    if run["status"] != "complete":
        raise ApiError((run.get("error") or {}).get("code", "engine.failure"), (run.get("error") or {}).get("message", run["status"]),
                       job_id=run.get("run_id"))
    remote = json.loads(client.artifact(run["run_id"], "result.json"))
    cmp = client.compare(reg["run_id"], run["run_id"], "ir_strategy")
    rep = client.wait_comparison(cmp["comparison_id"], timeout=wait).get("report") or {}
    doc = {
        "schema": "vibe-trading.ir-reconciliation/1", "strategy_id": sid, "strategy_sha256": ssha,
        "pack_id": pack.id, "pack_sha256": pack.lock["pack_sha256"],
        "local_run": reg["run_id"], "alphakeel_run": run["run_id"], "comparison": cmp["comparison_id"],
        "alphakeel_result": {"mode": remote.get("mode"), "strategy_id": remote.get("strategy_id"), "strategy_sha256": remote.get("strategy_sha256"),
                             "verification": remote.get("verification")},
        "passed": rep.get("passed"), "layers": {k: v["status"] for k, v in rep.get("layers", {}).items()},
        "first_difference": rep.get("first_difference"), "pass_scope": rep.get("pass_scope"), "notes": rep.get("notes", []),
        "summary": {"python_local": _side_summary(res), "alphakeel": _side_summary(remote)},
        "run_dir": str(run_dir), "sandbox": sandbox,
    }
    (out / "reconciliation.json").write_text(json.dumps(doc, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return doc


# ---------------------------------------------------------------------------------------------------------------------
# Strategy IR research side: long-history dataset backtest and the export AlphaKeel's registry imports
# ---------------------------------------------------------------------------------------------------------------------

def ir_backtest(client: Client | None, ir_doc: dict, instruments: list[dict], *, start_ms: int, end_ms: int, out_dir: str | Path,
                cache_dir: str | Path | None = None, pack_dir: str | Path | None = None, **kw: Any) -> dict:
    """Long-history IR backtest on a dataset pack (``ir_backtest.backtest``); returns the run card. Research evidence only:
    the reconciliation with AlphaKeel's evaluator is :func:`ir_review` on a scan pack."""
    from . import ir_backtest as bt

    return bt.backtest(client, ir_doc, instruments, start_ms=start_ms, end_ms=end_ms, out_dir=out_dir,
                       cache_dir=cache_dir or DEFAULT_CACHE, pack_dir=pack_dir, **kw)


def ir_export(ir_doc: dict, run_dir: str | Path, out_dir: str | Path, *, research_credential: dict | None = None) -> dict:
    """``strategy.json`` (IR + ``strategy_id`` + ``provenance``) and ``evidence/ir-backtest-audit.json`` from an
    ``ir backtest`` run directory (``ir_backtest.export``)."""
    from . import ir_backtest as bt

    return bt.export(ir_doc, run_dir, out_dir, research_credential=research_credential)
