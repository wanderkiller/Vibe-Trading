"""Grounding evidence carries across the turns of one conversation.

A figure a tool returned in an earlier turn (alphakeel_research fees) is still
observed evidence in a later turn; per-turn quotes are not. Every value here is
synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agent.grounding.ledger import GroundingLedger
from src.agent.loop import _prior_ledgers

pytestmark = pytest.mark.unit

_ANSWER = (
    "手续费合计 4534.98。\n\n```figures\n4534.98 | observed | fees | alphakeel_research\n```"
)


def _ledger(run_dir: Path, **kwargs) -> GroundingLedger:
    run_dir.mkdir(parents=True, exist_ok=True)
    return GroundingLedger(run_dir=run_dir, user_message="总结一下", **kwargs)


def _ingest(ledger: GroundingLedger, tool: str, payload: dict, call_id: str) -> None:
    ledger.ingest_tool_result(
        tool_name=tool, arguments={}, result=json.dumps(payload), call_id=call_id, success=True
    )


def _copy(tmp_path: Path, name: str) -> Path:
    """The session-store ledger file of run ``name`` (outside any run dir)."""
    return tmp_path / "sessions" / "s1" / "grounding" / f"{name}.json"


def _write_ledger(path: Path, evidence: list[dict] | None = None, **extra) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "evidence": evidence or [], **extra}), encoding="utf-8")
    return path


def _record(**kw) -> dict:
    base = {"artifact": None, "call_id": "c1", "currency": None, "currency_conversion": None, "field": "fees_usdt",
            "scope": None, "source": "backtest", "status": "observed", "symbol": None, "timestamp": None,
            "tool": "alphakeel_research", "value": 4534.98312158, "venue": None}
    return {**base, **kw}


@pytest.fixture()
def first(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run-1"
    ledger = _ledger(run_dir, session_copy=_copy(tmp_path, "run-1"))
    _ingest(ledger, "alphakeel_research", {"status": "ok", "fees": 4534.98312158}, "call-a")
    _ingest(ledger, "get_market_data", {"AAPL.US": {"close": 187.25, "date": "2026-01-02"}}, "call-q")
    ledger.persist()
    return _copy(tmp_path, "run-1")


def _fee_issues(result) -> list[dict]:
    return [
        i for i in result.issues
        if i.get("code") in {"numeric_claim_conflict", "numeric_claim_unavailable"}
        and i.get("value") is not None
    ]


def test_a_figure_from_an_earlier_turn_is_observed_in_a_later_one(tmp_path: Path, first: Path) -> None:
    later = _ledger(tmp_path / "run-2", prior_ledgers=[first])
    assert _fee_issues(later.validate_final_answer(_ANSWER)) == []


def test_without_prior_ledgers_the_same_figure_is_rejected(tmp_path: Path, first: Path) -> None:
    alone = _ledger(tmp_path / "run-2")
    assert _fee_issues(alone.validate_final_answer(_ANSWER))


def test_quotes_are_per_turn_and_never_carried(tmp_path: Path, first: Path) -> None:
    later = _ledger(tmp_path / "run-2", prior_ledgers=[first])
    tools = {record.tool for record in later._evidence}
    assert "alphakeel_research" in tools
    assert "get_market_data" not in tools


def test_carried_records_are_not_carried_twice(tmp_path: Path, first: Path) -> None:
    second = _copy(tmp_path, "run-2")
    _ledger(tmp_path / "run-2", prior_ledgers=[first], session_copy=second).persist()
    third = _ledger(tmp_path / "run-3", prior_ledgers=[first, second])
    ids = [r.call_id for r in third._evidence if r.tool == "alphakeel_research"]
    assert ids and all(i.count("prior:") == 1 for i in ids)
    assert len(ids) == len(set(ids))
    assert all(r.artifact is None and r.scope is None for r in third._evidence if r.call_id.startswith("prior:"))


def test_unreadable_prior_ledgers_are_skipped(tmp_path: Path) -> None:
    broken = _copy(tmp_path, "broken")
    broken.parent.mkdir(parents=True)
    broken.write_text("{not json", encoding="utf-8")
    foreign = _write_ledger(_copy(tmp_path, "foreign"), [{"call_id": "x"}])
    foreign.write_text(json.dumps({"schema_version": 2, "evidence": [{"call_id": "x"}]}), encoding="utf-8")
    ledger = _ledger(tmp_path / "run-2", prior_ledgers=[broken, foreign, _copy(tmp_path, "missing")])
    assert ledger._evidence == [] and ledger._analysis_metrics == []


def test_analysis_metrics_are_carried_with_run_relative_paths_removed(tmp_path: Path) -> None:
    prior = _write_ledger(
        _copy(tmp_path, "run-1"),
        [_record(call_id="bt1", tool="backtest", field="sharpe", scope="ir_runs/x_ir", value=0.69)],
        analysis_evidence=[
            {"metric": "sharpe", "value": 0.69, "tool": "backtest", "call_id": "bt1",
             "field": "sharpe", "artifact": "rp/metrics.csv", "scope": "rp"},
            {"metric": "sharpe", "value": 0.5, "tool": "backtest", "call_id": "prior:old:bt0",
             "field": "sharpe"},
            {"metric": "sharpe", "value": 0.4, "tool": "backtest", "call_id": "bt-engine", "field": "sharpe"},
            {"metric": "ic", "value": 0.07, "tool": "factor_analysis", "call_id": "fa1", "field": "ic"},
        ],
    )
    ledger = _ledger(tmp_path / "run-2", prior_ledgers=[prior])
    assert {(e["call_id"], e["value"]) for e in ledger._analysis_metrics} == {
        ("prior:run-1:bt1", 0.69), ("prior:run-1:fa1", 0.07)}
    entry = next(e for e in ledger._analysis_metrics if e["tool"] == "backtest")
    assert entry["artifact"] is None and entry["scope"] is None


def test_a_planted_run_dir_ledger_is_ignored(tmp_path: Path) -> None:
    """The model's write_file can write under the runs root; only the session store is read back."""
    run_dir = tmp_path / "run-1"
    planted = _write_ledger(run_dir / "artifacts" / "grounding_evidence.json", [_record(value=99999.12)])
    later = _ledger(tmp_path / "run-2", prior_ledgers=[])
    assert later._evidence == []
    answer = "手续费合计 99999.12。\n\n```figures\n99999.12 | observed | fees | alphakeel_research\n```"
    assert _fee_issues(later.validate_final_answer(answer))
    assert planted.is_file()


def test_persist_writes_the_same_payload_to_the_session_copy(tmp_path: Path) -> None:
    copy = _copy(tmp_path, "run-1")
    ledger = _ledger(tmp_path / "run-1", session_copy=copy)
    _ingest(ledger, "alphakeel_research", {"status": "ok", "fees": 12.5}, "call-a")
    own = json.loads((tmp_path / "run-1" / "artifacts" / "grounding_evidence.json").read_text(encoding="utf-8"))
    assert json.loads(copy.read_text(encoding="utf-8")) == own and own["evidence"]
    assert not list(copy.parent.glob("*.tmp"))


def test_bash_and_ohlc_price_records_are_not_carried(tmp_path: Path) -> None:
    prior = _write_ledger(_copy(tmp_path, "run-1"), [
        _record(call_id="b1", tool="bash", field="fees_usdt", value=111.11),
        _record(call_id="p1", tool="read_file", field="close", value=187.25),
        _record(call_id="p2", tool="read_file", field="data.quote[0].last_price", value=188.5),
        _record(call_id="ok", tool="alphakeel_research", field="fees_usdt", value=4534.98312158),
    ])
    later = _ledger(tmp_path / "run-2", prior_ledgers=[prior])
    assert [r.call_id for r in later._evidence] == ["prior:run-1:ok"]


def test_engine_backtest_records_are_per_turn_but_ir_ones_are_carried(tmp_path: Path) -> None:
    prior = _write_ledger(
        _copy(tmp_path, "run-1"),
        [
            _record(call_id="eng", tool="backtest", field="final_equity", scope="runs/abc", value=123456.7),
            _record(call_id="root", tool="backtest", field="final_equity", scope=".", value=222222.2),
            _record(call_id="ir", tool="backtest", field="fees_usdt", scope="ir_runs/x_ir", value=4534.98312158),
        ],
        analysis_evidence=[
            {"metric": "sharpe", "value": 0.4, "tool": "backtest", "call_id": "eng", "field": "sharpe"},
            {"metric": "sharpe", "value": 0.9, "tool": "backtest", "call_id": "ir", "field": "sharpe"},
        ],
        analysis_completed=[{"call_id": "eng", "tool": "backtest"}, {"call_id": "ir", "tool": "alphakeel_research"}],
    )
    later = _ledger(tmp_path / "run-2", prior_ledgers=[prior])
    assert [r.call_id for r in later._evidence] == ["prior:run-1:ir"]
    assert [e["call_id"] for e in later._analysis_metrics] == ["prior:run-1:ir"]
    assert later._prior_call_tools == {"prior:run-1:eng": "backtest", "prior:run-1:ir": "alphakeel_research"}


def _attempt(sessions: Path, session: str, attempt_id: str, status: str, created: str, run_dir: Path) -> None:
    path = sessions / session / "attempts" / attempt_id
    path.mkdir(parents=True)
    (path / "attempt.json").write_text(
        json.dumps({"attempt_id": attempt_id, "status": status, "created_at": created, "run_dir": str(run_dir)}),
        encoding="utf-8",
    )


def test_prior_ledgers_are_completed_attempts_in_creation_order(tmp_path: Path, monkeypatch) -> None:
    import src.agent.loop as loop

    sessions = tmp_path / "sessions"
    monkeypatch.setattr(loop, "SESSIONS_DIR", sessions)
    dirs = {name: tmp_path / "runs" / name for name in ("a", "b", "c", "d", "cur", "nofile")}
    for name in ("a", "b", "c", "d", "cur"):
        _write_ledger(sessions / "s1" / "grounding" / f"{name}.json")
    _attempt(sessions, "s1", "x2", "completed", "2026-01-02T00:00:00", dirs["b"])
    _attempt(sessions, "s1", "x1", "completed", "2026-01-01T00:00:00", dirs["a"])
    _attempt(sessions, "s1", "x3", "failed", "2026-01-03T00:00:00", dirs["c"])
    _attempt(sessions, "s1", "x4", "completed", "2026-01-04T00:00:00", dirs["nofile"])
    _attempt(sessions, "s1", "x5", "completed", "2026-01-05T00:00:00", dirs["cur"])
    _attempt(sessions, "s1", "x6", "completed", "2026-01-06T00:00:00", dirs["d"])
    g = sessions / "s1" / "grounding"
    assert _prior_ledgers("s1", dirs["cur"]) == [g / "a.json", g / "b.json", g / "d.json"]
    assert _prior_ledgers("s1", dirs["cur"], limit=2) == [g / "b.json", g / "d.json"]
    assert _prior_ledgers("missing", dirs["cur"]) == []
    assert _prior_ledgers(None, dirs["cur"]) == []


def _ir_run_ledger(path: Path) -> Path:
    """A persisted turn shaped like the audited session's (f9941f6d027f, turn 3): the IR run's metrics copy is recorded
    as ``backtest`` output of the ``alphakeel_research`` call that produced it."""
    record = _record(call_id="call_118306", tool="backtest", scope="ir_runs/x_ir",
                     artifact="ir_runs/x_ir/artifacts/metrics.csv")
    return _write_ledger(
        path, [record], analysis_evidence=[],
        analysis_completed=[{"call_id": "call_118306", "tool": "alphakeel_research", "recorded_at": "x"}],
    )


def test_an_ir_figure_cited_by_the_tool_that_ran_it_is_observed_in_a_later_turn(tmp_path: Path) -> None:
    """Regression (audited session, turn 4): "4534.98 | observed | fees | alphakeel_research" was rejected as
    "declared observed from alphakeel_research, whose results do not contain it" and released as (略※)."""
    prior = _ir_run_ledger(_copy(tmp_path, "run-ir"))
    later = _ledger(tmp_path / "run-2", prior_ledgers=[prior])
    assert _fee_issues(later.validate_final_answer(_ANSWER)) == []


def test_a_carried_call_names_only_its_own_tool(tmp_path: Path) -> None:
    prior = _ir_run_ledger(_copy(tmp_path, "run-ir"))
    later = _ledger(tmp_path / "run-2", prior_ledgers=[prior])
    answer = "手续费合计 4534.98。\n\n```figures\n4534.98 | observed | fees | alpha_zoo\n```"
    assert _fee_issues(later.validate_final_answer(answer))
