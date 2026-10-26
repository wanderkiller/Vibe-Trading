"""``GET /runs/{id}/alphakeel`` and ``POST /runs/{id}/alphakeel/export``: the backend of the hand-off button.

An app with only the runs routes and these routes; RUNS_DIR, the hand-off directory and the research client are test
doubles (a fake ``Client`` whose ``check()`` is the credential, never a real service or token).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api_server
from alphakeel_research import cli, precheck
from src.api import alphakeel_handoff_routes as routes
from src.api.runs_routes import register_runs_routes
from tests.test_alphakeel_ir_backtest import START, fees_file
from tests.test_alphakeel_robustness import H6, _ir, _market

CRED = {"credential": "cred-test", "holdout_days": 60, "data_horizon": {"max_window_end_ms": START + 1}}


class FakeClient:
    fail: Exception | None = None

    def __init__(self, url=None):
        self.url = url

    def check(self):
        if FakeClient.fail is not None:
            raise FakeClient.fail
        return dict(CRED)

    def close(self):
        pass


@pytest.fixture()
def env(tmp_path, monkeypatch):
    runs, handoff = tmp_path / "runs", tmp_path / "handoff"
    runs.mkdir()
    token = tmp_path / "token"
    token.write_text("not-a-real-token\n")
    monkeypatch.setattr(api_server, "RUNS_DIR", runs)
    monkeypatch.setenv("ALPHAKEEL_HANDOFF_DIR", str(handoff))
    monkeypatch.setenv("ALPHAKEEL_HANDOFF_HOST_DIR", "/host/handoff")
    monkeypatch.setenv("ALPHAKEEL_RESEARCH_TOKEN_FILE", str(token))
    monkeypatch.setattr(routes, "_client_cls", lambda: FakeClient)
    FakeClient.fail = None
    app = FastAPI()
    register_runs_routes(app)
    routes.register_alphakeel_handoff_routes(app)
    return types_ns(client=TestClient(app, client=("127.0.0.1", 50000)), runs=runs, handoff=handoff, tmp=tmp_path, token=token)


def types_ns(**kw):
    import types

    return types.SimpleNamespace(**kw)


def make_ir_run(env, name="20261010_120000_ir", *, robustness=True, trials=True, capsys=None) -> Path:
    tmp = env.tmp / name
    tmp.mkdir()
    insts, pd = _market(tmp, hi=START + H6)
    (tmp / "ir.json").write_text(json.dumps(_ir()))
    (tmp / "inst.json").write_text(json.dumps(insts))
    run = env.runs / name
    assert cli.main(["ir", "backtest", "--fees", fees_file(tmp), "--no-register", "--ir", str(tmp / "ir.json"), "--start-ms", str(START), "--end-ms", str(START + H6), "--instruments",
                     str(tmp / "inst.json"), "--pack-dir", str(pd), "--step-minutes", "2", *(["--trials", "1"] if trials else []),
                     "--out", str(run)]) == 0
    if robustness:
        assert cli.main(["ir", "robustness", "--no-register", "--run", str(run), "--permutations", "0", "--workers", "1", "--step-minutes", "2"]) == 0
    return run


def test_a_non_ir_run_is_not_offered_and_cannot_be_exported(env):
    (env.runs / "run_20260101_000000").mkdir()
    (env.runs / "run_20260101_000000" / "state.json").write_text('{"status": "success"}')
    r = env.client.get("/runs/run_20260101_000000/alphakeel")
    assert r.status_code == 200 and r.json() == {"ir": False, "strategy_id": None, "name": None, "window": None, "robustness": None,
                                                 "robustness_sha256": None, "precheck": [], "protocol": None, "target": None, "exported": None, "credential": False}
    r = env.client.post("/runs/run_20260101_000000/alphakeel/export")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "ir.not_ir_run"
    assert env.client.get("/runs/nope/alphakeel").status_code == 404
    assert env.client.post("/runs/nope/alphakeel/export").json()["detail"]["code"] == "run.not_found"


def test_run_ids_that_could_escape_the_runs_directory_are_rejected(env):
    for bad in ("a.b", "..x", "a b", "x;y"):
        assert env.client.get(f"/runs/{bad}/alphakeel").status_code == 400, bad
        assert env.client.post(f"/runs/{bad}/alphakeel/export").status_code == 400, bad
    assert not env.handoff.exists()


def test_the_real_app_has_the_routes_behind_the_same_auth():
    paths = {(m, r.path) for r in api_server.app.routes for m in getattr(r, "methods", ())}
    assert ("GET", "/runs/{run_id}/alphakeel") in paths and ("POST", "/runs/{run_id}/alphakeel/export") in paths


def test_ir_run_without_robustness_shows_an_empty_precheck_and_export_is_409(env):
    run = make_ir_run(env, robustness=False)
    body = env.client.get(f"/runs/{run.name}/alphakeel").json()
    assert body["ir"] is True and body["robustness"] is None and body["precheck"] == [] and body["exported"] is None
    assert body["strategy_id"].startswith("ir-") and body["window"] == {"start_day": "2023-01-01", "end_day": "2023-01-01"}
    assert body["target"] == {"container_path": f"{env.handoff}/{body['strategy_id']}/", "host_path": f"/host/handoff/{body['strategy_id']}/"}
    assert body["credential"] is True and body["protocol"] == {"version": precheck.load_protocol()["version"], "sha256": precheck.protocol_sha256()}
    r = env.client.post(f"/runs/{run.name}/alphakeel/export")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "robustness.missing"
    assert not env.handoff.exists() or not any(env.handoff.iterdir())


def test_ir_run_with_robustness_lists_the_precheck_and_exports_idempotently(env):
    run = make_ir_run(env)
    doc = json.loads((run / "robustness" / "robustness.json").read_text())
    body = env.client.get(f"/runs/{run.name}/alphakeel").json()
    assert body["robustness"] == {"grade": doc["rating"]["grade"], "score": doc["rating"]["score"], "caps": [c["id"] for c in doc["rating"]["caps"]],
                                  "schema": "vibe-trading.ir-robustness/2",
                                  "sha256": hashlib.sha256((run / "robustness" / "robustness.json").read_bytes()).hexdigest()}
    # protocol 2.3: the route passes the IR document, so 1.9/1.12/1.13/1.19 are judged as AlphaKeel judges them; 1.6 adds the
    # trials floor and fixed_literals, and 1.18 (decided from the IS pack's data lock) is N/A on the VT side
    want = precheck.evaluate(doc, precheck.load_protocol(), ir=_ir())
    assert len(body["precheck"]) == 16 and [c["id"] for c in body["precheck"]] == [c["id"] for c in want]
    assert {c["id"]: c["result"] for c in body["precheck"] if c["id"] not in ("1.6", "1.18")} == {c["id"]: c["result"] for c in want if c["id"] not in ("1.6", "1.18")}
    assert all(c["result"] == "N/A" for c in body["precheck"] if c["id"] == "1.18")
    sid = body["strategy_id"]
    r = env.client.post(f"/runs/{run.name}/alphakeel/export")
    assert r.status_code == 200, r.text
    out = r.json()
    target = env.handoff / sid
    assert out["strategy_id"] == sid and out["path"] == f"{target}/" and out["grade"] == doc["rating"]["grade"]
    names = [f["name"] for f in out["files"]]
    assert {"strategy.json", "evidence/robustness.json", "evidence/robustness.md", "evidence/robustness-trials.jsonl",
            "evidence/ir-backtest-audit.json", "evidence/trials.jsonl"} <= set(names)
    for f in out["files"]:
        assert hashlib.sha256((target / f["name"]).read_bytes()).hexdigest() == f["sha256"]
    strategy = json.loads((target / "strategy.json").read_text())
    assert strategy["strategy_id"] == sid and strategy["provenance"]["research_credential"] == "cred-test"
    assert strategy["provenance"]["robustness"]["sha256"] == body["robustness"]["sha256"] and strategy["provenance"]["robustness"]["grade"] == out["grade"]
    assert (target / "evidence" / "robustness.json").read_bytes() == (run / "robustness" / "robustness.json").read_bytes()
    assert oct(target.stat().st_mode & 0o7777) in ("0o2775", "0o775")
    marker = json.loads((run / "alphakeel-handoff.json").read_text())
    assert marker == {"path": out["path"], "at": out["at"], "strategy_id": sid, "files": out["files"]}
    assert env.client.get(f"/runs/{run.name}/alphakeel").json()["exported"] == marker
    assert sorted(p.name for p in env.handoff.iterdir()) == [sid]  # no temp or old directories left
    # a second export overwrites: same files, a stale file of the old package is gone
    (target / "stale.txt").write_text("x")
    again = env.client.post(f"/runs/{run.name}/alphakeel/export")
    assert again.status_code == 200 and not (target / "stale.txt").exists()
    assert [f["name"] for f in again.json()["files"]] == names
    assert sorted(p.name for p in env.handoff.iterdir()) == [sid]


def test_export_needs_a_credential_and_a_reachable_service(env, monkeypatch):
    run = make_ir_run(env)
    monkeypatch.delenv("ALPHAKEEL_RESEARCH_TOKEN_FILE")
    assert env.client.get(f"/runs/{run.name}/alphakeel").json()["credential"] is False
    r = env.client.post(f"/runs/{run.name}/alphakeel/export")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "credential.missing"
    monkeypatch.setenv("ALPHAKEEL_RESEARCH_TOKEN_FILE", str(env.tmp / "does-not-exist"))
    assert env.client.get(f"/runs/{run.name}/alphakeel").json()["credential"] is False
    monkeypatch.setenv("ALPHAKEEL_RESEARCH_TOKEN_FILE", str(env.token))
    FakeClient.fail = ConnectionError("connection refused")
    r = env.client.post(f"/runs/{run.name}/alphakeel/export")
    assert r.status_code == 502 and r.json()["detail"]["code"] == "research.unavailable" and "connection refused" in r.json()["detail"]["message"]
    assert not env.handoff.exists() or not any(env.handoff.iterdir())


def test_robustness_of_another_strategy_and_export_errors_use_the_documented_codes(env):
    run = make_ir_run(env)
    rb = json.loads((run / "robustness" / "robustness.json").read_text())
    (run / "robustness" / "robustness.json").write_text(json.dumps({**rb, "strategy_id": "ir-" + "0" * 24}))
    body = env.client.get(f"/runs/{run.name}/alphakeel").json()
    assert body["robustness"] is None and body["precheck"] == []
    r = env.client.post(f"/runs/{run.name}/alphakeel/export")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "robustness.mismatch"
    # an old-schema battery is refused by the export itself (ApiError code passed through as 400)
    (run / "robustness" / "robustness.json").write_text(json.dumps({**rb, "schema": "vibe-trading.ir-robustness/1"}))
    r = env.client.post(f"/runs/{run.name}/alphakeel/export")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "request.invalid" and "ir-robustness/2" in r.json()["detail"]["message"]
    assert not env.handoff.exists() or not any(env.handoff.iterdir())  # a failed export leaves no temp directory
    # a run without trials evidence cannot be exported: the export's own message says how to fix it
    (run / "robustness" / "robustness.json").write_text(json.dumps(rb))
    run2 = make_ir_run(env, "20261010_120001_ir", trials=False)
    r = env.client.post(f"/runs/{run2.name}/alphakeel/export")
    assert r.status_code == 400 and "trials" in r.json()["detail"]["message"]


def test_a_second_export_of_the_same_strategy_is_refused_while_the_first_runs(env):
    import fcntl
    import tempfile

    run = make_ir_run(env)
    sid = env.client.get(f"/runs/{run.name}/alphakeel").json()["strategy_id"]
    lock_dir = Path(tempfile.gettempdir()) / f"alphakeel-handoff-locks-{os.getuid()}"
    lock_dir.mkdir(exist_ok=True)
    with open(lock_dir / f"{sid}.lock", "a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        r = env.client.post(f"/runs/{run.name}/alphakeel/export")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "export.busy"
    assert env.client.post(f"/runs/{run.name}/alphakeel/export").status_code == 200


def test_export_writes_a_manifest_listing_every_file_with_its_hash(env):
    run = make_ir_run(env)
    sid = env.client.get(f"/runs/{run.name}/alphakeel").json()["strategy_id"]
    r = env.client.post(f"/runs/{run.name}/alphakeel/export")
    assert r.status_code == 200, r.text
    target = env.handoff / sid
    m = json.loads((target / "handoff-manifest.json").read_text())
    card = json.loads((run / "run_card.json").read_text())
    assert m["schema"] == "alphakeel.handoff-manifest/1" and m["strategy_id"] == sid and m["run_id"] == card["run_id"]
    assert isinstance(m["exported_at_ms"], int) and m["exported_at_ms"] > 0
    on_disk = sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file() and p.name != "handoff-manifest.json")
    assert [f["path"] for f in m["files"]] == on_disk and "strategy.json" in on_disk
    for f in m["files"]:
        data = (target / f["path"]).read_bytes()
        assert f["sha256"] == hashlib.sha256(data).hexdigest() and f["bytes"] == len(data)
    assert "handoff-manifest.json" in [f["name"] for f in r.json()["files"]]


def test_export_refuses_when_the_robustness_changed_since_it_was_reviewed(env):
    run = make_ir_run(env)
    body = env.client.get(f"/runs/{run.name}/alphakeel").json()
    sha = body["robustness_sha256"]
    assert sha == body["robustness"]["sha256"] == hashlib.sha256((run / "robustness" / "robustness.json").read_bytes()).hexdigest()
    r = env.client.post(f"/runs/{run.name}/alphakeel/export", json={"robustness_sha256": "0" * 64})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "export.changed" and "reopen" in r.json()["detail"]["message"]
    assert not env.handoff.exists() or not any(env.handoff.iterdir())
    assert env.client.post(f"/runs/{run.name}/alphakeel/export", json={"robustness_sha256": sha}).status_code == 200


def test_export_refuses_a_battery_rewritten_between_the_check_and_the_copy(env, monkeypatch):
    run = make_ir_run(env)
    sha = env.client.get(f"/runs/{run.name}/alphakeel").json()["robustness_sha256"]
    from alphakeel_research import ir_backtest

    real = ir_backtest.export

    def racing_export(doc, run_dir, out_dir, **kw):
        f = Path(run_dir) / "robustness" / "robustness.json"
        rb = json.loads(f.read_text())
        rb["notes"] = list(rb.get("notes") or []) + ["rewritten by a concurrent ir robustness"]
        f.write_text(json.dumps(rb))
        return real(doc, run_dir, out_dir, **kw)

    monkeypatch.setattr(ir_backtest, "export", racing_export)
    r = env.client.post(f"/runs/{run.name}/alphakeel/export", json={"robustness_sha256": sha})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "export.changed"
    assert not env.handoff.exists() or not any(env.handoff.iterdir())


def test_export_with_a_battery_over_another_window_is_409_window_mismatch(env):
    run = make_ir_run(env)
    f = run / "robustness" / "robustness.json"
    rb = json.loads(f.read_text())
    rb["inputs"]["is_window"]["end_ms"] += 1
    f.write_text(json.dumps(rb))
    r = env.client.post(f"/runs/{run.name}/alphakeel/export")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "export.window_mismatch" and "window mismatch" in r.json()["detail"]["message"]
    assert not env.handoff.exists() or not any(env.handoff.iterdir())


def test_the_ledger_count_of_the_backtest_registration_reaches_the_1_6_trials_floor(env, monkeypatch):
    # protocol 3.0 trials_include: service_registration.ledger_trials_for_scope (AlphaKeel's 1.6 ledger count) is passed
    # as ledger_trials; a card without it passes nothing
    run = make_ir_run(env)
    seen = []
    real = precheck.evaluate
    monkeypatch.setattr(precheck, "evaluate", lambda *a, **kw: seen.append(kw) or real(*a, **kw))
    env.client.get(f"/runs/{run.name}/alphakeel")
    assert "ledger_trials" not in seen[-1]
    card = json.loads((run / "run_card.json").read_text())
    card["service_registration"] = {"registered": True, "trial_id": "trial-x", "trial_key": "research:x", "ledger_trials_for_scope": 1234}
    (run / "run_card.json").write_text(json.dumps(card))
    env.client.get(f"/runs/{run.name}/alphakeel")
    assert seen[-1]["ledger_trials"] == 1234
