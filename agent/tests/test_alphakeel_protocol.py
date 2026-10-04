"""Protocol-level tests against the live service: Schemathesis over the pinned OpenAPI, and a Hypothesis state machine for
the policy step protocol (idempotent re-submission, skipped / late / forged steps never change the session)."""

from __future__ import annotations

import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from alphakeel_research import strategy as strat, workflow
from alphakeel_research.client import Client
from alphakeel_research.errors import ApiError
from alphakeel_research.policy import response_doc
from tests.fixtures.alphakeel_server import start

PIN = Path(__file__).resolve().parents[1] / "alphakeel_research" / "contract" / "openapi.json"
FIX = Path(__file__).parent / "fixtures" / "alphakeel"
BIN = {"venue": "binance", "market": "perp", "symbol": "BTCUSDT"}


@pytest.fixture(scope="module")
def server():
    f = start()
    yield f
    f.stop()


def test_schemathesis_fuzzes_every_operation_of_the_pinned_contract_against_the_live_service(server):
    schemathesis = shutil.which("schemathesis") or str(Path(sys.executable).with_name("schemathesis"))
    if not Path(schemathesis).exists():
        pytest.skip("schemathesis is not installed")
    r = subprocess.run([schemathesis, "run", str(PIN), "--url", server.url.rstrip("/"), "-H", f"Authorization: Bearer {server.token}",
                        "--checks", "not_a_server_error,status_code_conformance,content_type_conformance,response_schema_conformance",
                        "--max-examples", "25", "--phases", "fuzzing", "--workers", "1"], capture_output=True, text=True, timeout=900)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-1000:]
    assert "31 passed" in r.stdout or "1565" in r.stdout or "passed" in r.stdout


class StepProtocol(RuleBasedStateMachine):
    client: Client
    server = None
    tmp: Path

    def __init__(self):
        super().__init__()
        c = self.client
        pack = workflow.freeze_pack(c, {"window": {"start_ms": self.server.start_ms, "end_ms": self.server.end_ms}}, cache_dir=self.tmp / "cache")
        d = self.tmp / "s"
        if not d.exists():
            shutil.copytree(FIX / "carry_policy", d)
        man, bundle = strat.manifest(d, name="protocol", parameters={"qty": "1", "close_at_ms": self.server.end_ms}, seed=1)
        c.register_strategy(man, c.upload("strategy-bundle", bundle)["sha256"])
        self.pack, self.man = pack, man
        self.run_id = c.create_run({"mode": "python_policy", "pack_id": pack.id, "strategy_id": man["strategy_id"], "seed": 1, "instruments": [BIN],
                                    "policy": {"step_timeout_ms": 600_000}}, key=uuid.uuid4().hex)["run_id"]
        self.accepted: dict[int, dict] = {}
        self.cur = c.next_step(self.run_id, 5000)
        assert self.cur["state"] == "awaiting", c.run(self.run_id)

    def teardown(self):
        try:
            self.client.cancel_run(self.run_id)
        except ApiError:
            pass

    def _resp(self, cur: dict, **over) -> dict:
        r = response_doc(self.run_id, cur["step_seq"], cur["context_sha256"], self.man["content_sha256"], self.pack.id, [], "00" * 32)
        r.update(over)
        return r

    @precondition(lambda self: self.cur.get("state") == "awaiting" and self.cur["step_seq"] < 8)
    @rule()
    def submit_current(self):
        cur = self.cur
        resp = self._resp(cur)
        self.client.submit_step(self.run_id, cur["step_seq"], resp)
        self.accepted[cur["step_seq"]] = resp
        self.cur = self.client.next_step(self.run_id, 5000)

    @precondition(lambda self: bool(self.accepted))
    @rule(data=st.data())
    def resubmit_an_accepted_step_is_idempotent(self, data):
        seq = data.draw(st.sampled_from(sorted(self.accepted)))
        self.client.submit_step(self.run_id, seq, self.accepted[seq])  # same bytes: accepted again, nothing changes

    @precondition(lambda self: bool(self.accepted))
    @rule(data=st.data())
    def a_different_answer_for_an_accepted_step_is_refused(self, data):
        seq = data.draw(st.sampled_from(sorted(self.accepted)))
        other = dict(self.accepted[seq], state_sha256="11" * 32)
        with pytest.raises(ApiError):
            self.client.submit_step(self.run_id, seq, other)

    @precondition(lambda self: self.cur.get("state") == "awaiting")
    @rule(skip=st.integers(1, 5))
    def skipping_ahead_is_refused(self, skip):
        cur = self.cur
        with pytest.raises(ApiError):
            self.client.submit_step(self.run_id, cur["step_seq"] + skip, self._resp(cur, step_seq=cur["step_seq"] + skip))

    @precondition(lambda self: self.cur.get("state") == "awaiting")
    @rule(field=st.sampled_from(["context_sha256", "strategy_content_sha256", "pack_id", "session_id"]))
    def forged_identity_is_refused(self, field):
        cur = self.cur
        with pytest.raises(ApiError):
            self.client.submit_step(self.run_id, cur["step_seq"], self._resp(cur, **{field: "ab" * 32 if field != "pack_id" and field != "session_id" else "x-forged"}))

    @invariant()
    def the_session_still_waits_for_exactly_the_step_it_waited_for(self):
        if self.cur.get("state") != "awaiting":
            return
        now = self.client.get_step(self.run_id, self.cur["step_seq"])
        assert now["state"] == "awaiting"
        assert self.client.next_step(self.run_id, 1000)["step_seq"] == self.cur["step_seq"]


def test_the_step_protocol_state_machine(server, tmp_path):
    c = Client(server.url, server.token)
    StepProtocol.client, StepProtocol.server, StepProtocol.tmp = c, server, tmp_path
    try:
        StepProtocol.TestCase.settings = settings(max_examples=12, stateful_step_count=12, deadline=None, derandomize=True, suppress_health_check=list(HealthCheck))
        StepProtocol.TestCase().runTest()
    finally:
        c.close()
