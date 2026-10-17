"""``read_file`` ``trials/NNNN/...`` reads the current conversation's engine-run snapshots (never run_dir files).

Regression: in a later turn the agent could not read the previous turn's strategy code (read_file is scoped to the
current run_dir), re-implemented it, and the same parameters gave a different result.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.providers.session_context import bind_llm_session_id, reset_llm_session_id
from src.tools import alphakeel_research_ledger as L
from src.tools.read_file_tool import ReadFileTool

pytestmark = pytest.mark.unit

SESSION = "sess-trials"


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path))
    monkeypatch.setattr(L, "_sessions_dir", lambda: tmp_path / "sessions")
    run = tmp_path / "run"
    run.mkdir()
    return run


def _snapshot(seq: int, code: str = "# strategy\nline2\n", *, row: bool = True) -> Path:
    snap = L.engine_snapshot_dir(SESSION, seq)
    (snap / "code").mkdir(parents=True)
    (snap / "code" / "signal_engine.py").write_text(code, encoding="utf-8")
    (snap / "config.json").write_text(json.dumps({"codes": ["BTC-USDT-PERP"]}), encoding="utf-8")
    if row:  # the engine ledger row record_engine_run appends, with the same file hashes
        L._append_durable(L._session_dir(SESSION) / L.ENGINE_LEDGER_FILE, {
            "seq": seq, "trial_key": f"key{seq}", "snapshot_dir": str(snap),
            "code_sha256": L._sha256(snap / "code" / "signal_engine.py"), "config_sha256": L._sha256(snap / "config.json")})
    return snap


def _read(run: Path, path: str, **kw) -> dict:
    return json.loads(ReadFileTool().execute(path=path, run_dir=str(run), **kw))


@pytest.fixture()
def bound():
    token = bind_llm_session_id(SESSION)
    yield
    reset_llm_session_id(token)


def test_engine_snapshot_dir_is_four_digit_under_engine_runs(env) -> None:
    assert L.engine_snapshot_dir(SESSION, 7) == env.parent / "sessions" / SESSION / L.ENGINE_RUNS_DIR / "0007"


def test_reads_code_and_config_of_an_existing_snapshot(env, bound) -> None:
    _snapshot(1)
    code = _read(env, "trials/0001/code/signal_engine.py")
    assert code["status"] == "ok" and code["content"] == "# strategy\nline2\n"
    assert _read(env, "trials/0001/code/signal_engine.py", limit=1)["content"] == "# strategy\n"
    assert json.loads(_read(env, "trials/0001/config.json")["content"]) == {"codes": ["BTC-USDT-PERP"]}


def test_no_bound_session_is_no_session(env) -> None:
    out = _read(env, "trials/0001/config.json")
    assert out["status"] == "error" and out["error_code"] == "no_session"
    assert "no session is bound" in out["error"]


@pytest.mark.parametrize("path", ["trials/0001/../x", "trials/1/config.json", "trials/00001/config.json",
                                  "trials/0001/other.py", "trials/0001/code/../config.json", "trials/", "trials/0001"])
def test_other_forms_are_not_allowed(env, bound, path) -> None:
    _snapshot(1)
    out = _read(env, path)
    assert out["status"] == "error" and out["error_code"] == "not_allowed"
    assert "trials/NNNN/code/signal_engine.py" in out["error"]


def test_not_found_lists_the_existing_trials(env, bound) -> None:
    _snapshot(1)
    _snapshot(3)
    out = _read(env, "trials/0002/config.json")
    assert out["error_code"] == "not_found" and "0001, 0003" in out["error"]
    assert _read(env, "trials/0001/screen.json")["error_code"] == "not_found"  # allowed form, file absent


def test_not_found_when_the_session_has_no_trials(env, bound) -> None:
    assert "none" in _read(env, "trials/0001/config.json")["error"]


def test_a_run_dir_file_named_trials_does_not_shadow_the_namespace(env, bound) -> None:
    (env / "trials" / "0001" / "code").mkdir(parents=True)
    (env / "trials" / "0001" / "code" / "signal_engine.py").write_text("# forged\n", encoding="utf-8")
    (env / "trials" / "0001" / "config.json").write_text("{}", encoding="utf-8")
    out = _read(env, "trials/0001/config.json")
    assert out["error_code"] == "not_found"  # the forged file is not read; no snapshot exists
    _snapshot(1)
    assert _read(env, "trials/0001/code/signal_engine.py")["content"] == "# strategy\nline2\n"
    assert _read(env, "trials/0001/config.json")["content"] != "{}"


def test_a_tampered_snapshot_is_a_snapshot_mismatch(env, bound) -> None:
    snap = _snapshot(1)
    (snap / "code" / "signal_engine.py").write_text("# forged\n", encoding="utf-8")
    out = _read(env, "trials/0001/code/signal_engine.py")
    assert out["status"] == "error" and out["error_code"] == "snapshot_mismatch"
    assert _read(env, "trials/0001/config.json")["status"] == "ok"  # only the changed file is refused
    (snap / "config.json").write_text("{}", encoding="utf-8")
    assert _read(env, "trials/0001/config.json")["error_code"] == "snapshot_mismatch"


def test_a_tampered_screen_json_is_a_snapshot_mismatch(env, bound) -> None:
    snap = _snapshot(1)
    screen = snap / "screen.json"
    screen.write_text('{"evidence_status": "failed_screen"}', encoding="utf-8")
    # a row with no screen hash (an older ledger) leaves screen.json readable
    assert _read(env, "trials/0001/screen.json")["status"] == "ok"
    L._append_durable(L._session_dir(SESSION) / L.ENGINE_LEDGER_FILE, {
        "seq": 1, "trial_key": "key1", "snapshot_dir": str(snap), "screen_sha256": L._sha256(screen)})
    assert _read(env, "trials/0001/screen.json")["status"] == "ok"
    screen.write_text('{"evidence_status": "passed_screen"}', encoding="utf-8")
    out = _read(env, "trials/0001/screen.json")
    assert out["status"] == "error" and out["error_code"] == "snapshot_mismatch"


def test_a_snapshot_without_a_ledger_row_is_not_found(env, bound) -> None:
    _snapshot(1, row=False)
    out = _read(env, "trials/0001/code/signal_engine.py")
    assert out["status"] == "error" and out["error_code"] == "not_found"
    _snapshot(2)
    assert _read(env, "trials/0002/config.json")["status"] == "ok"
