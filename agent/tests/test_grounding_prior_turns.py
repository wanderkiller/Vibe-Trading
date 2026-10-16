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
from src.agent.loop import _prior_run_dirs

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


@pytest.fixture()
def first(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run-1"
    ledger = _ledger(run_dir)
    _ingest(ledger, "alphakeel_research", {"status": "ok", "fees": 4534.98312158}, "call-a")
    _ingest(ledger, "get_market_data", {"AAPL.US": {"close": 187.25, "date": "2026-01-02"}}, "call-q")
    ledger.persist()
    return run_dir


def _fee_issues(result) -> list[dict]:
    return [
        i for i in result.issues
        if i.get("code") in {"numeric_claim_conflict", "numeric_claim_unavailable"}
        and i.get("value") is not None
    ]


def test_a_figure_from_an_earlier_turn_is_observed_in_a_later_one(tmp_path: Path, first: Path) -> None:
    later = _ledger(tmp_path / "run-2", prior_run_dirs=[first])
    assert _fee_issues(later.validate_final_answer(_ANSWER)) == []


def test_without_prior_run_dirs_the_same_figure_is_rejected(tmp_path: Path, first: Path) -> None:
    alone = _ledger(tmp_path / "run-2")
    assert _fee_issues(alone.validate_final_answer(_ANSWER))


def test_quotes_are_per_turn_and_never_carried(tmp_path: Path, first: Path) -> None:
    later = _ledger(tmp_path / "run-2", prior_run_dirs=[first])
    tools = {record.tool for record in later._evidence}
    assert "alphakeel_research" in tools
    assert "get_market_data" not in tools


def test_carried_records_are_not_carried_twice(tmp_path: Path, first: Path) -> None:
    second = tmp_path / "run-2"
    _ledger(second, prior_run_dirs=[first]).persist()
    third = _ledger(tmp_path / "run-3", prior_run_dirs=[first, second])
    ids = [r.call_id for r in third._evidence if r.tool == "alphakeel_research"]
    assert ids and all(i.count("prior:") == 1 for i in ids)
    assert len(ids) == len(set(ids))
    assert all(r.artifact is None and r.scope is None for r in third._evidence if r.call_id.startswith("prior:"))


def test_unreadable_prior_ledgers_are_skipped(tmp_path: Path) -> None:
    broken = tmp_path / "broken"
    (broken / "artifacts").mkdir(parents=True)
    (broken / "artifacts" / "grounding_evidence.json").write_text("{not json", encoding="utf-8")
    foreign = tmp_path / "foreign"
    (foreign / "artifacts").mkdir(parents=True)
    (foreign / "artifacts" / "grounding_evidence.json").write_text(
        json.dumps({"schema_version": 2, "evidence": [{"call_id": "x"}]}), encoding="utf-8"
    )
    ledger = _ledger(tmp_path / "run-2", prior_run_dirs=[broken, foreign, tmp_path / "missing"])
    assert ledger._evidence == [] and ledger._analysis_metrics == []


def test_analysis_metrics_are_carried_with_run_relative_paths_removed(tmp_path: Path) -> None:
    prior = tmp_path / "run-1"
    (prior / "artifacts").mkdir(parents=True)
    (prior / "artifacts" / "grounding_evidence.json").write_text(
        json.dumps({
            "schema_version": 1,
            "evidence": [],
            "analysis_evidence": [
                {"metric": "sharpe", "value": 0.69, "tool": "backtest", "call_id": "bt1",
                 "field": "sharpe", "artifact": "rp/metrics.csv", "scope": "rp"},
                {"metric": "sharpe", "value": 0.5, "tool": "backtest", "call_id": "prior:old:bt0",
                 "field": "sharpe"},
            ],
        }),
        encoding="utf-8",
    )
    ledger = _ledger(tmp_path / "run-2", prior_run_dirs=[prior])
    assert len(ledger._analysis_metrics) == 1
    entry = ledger._analysis_metrics[0]
    assert entry["call_id"] == "prior:run-1:bt1" and entry["tool"] == "backtest" and entry["value"] == 0.69
    assert entry["artifact"] is None and entry["scope"] is None
    assert ledger._names_session_source("backtest")


def _attempt(sessions: Path, session: str, attempt_id: str, status: str, created: str, run_dir: Path) -> None:
    path = sessions / session / "attempts" / attempt_id
    path.mkdir(parents=True)
    (path / "attempt.json").write_text(
        json.dumps({"attempt_id": attempt_id, "status": status, "created_at": created, "run_dir": str(run_dir)}),
        encoding="utf-8",
    )


def test_prior_run_dirs_are_completed_attempts_in_creation_order(tmp_path: Path, monkeypatch) -> None:
    import src.agent.loop as loop

    sessions = tmp_path / "sessions"
    monkeypatch.setattr(loop, "SESSIONS_DIR", sessions)
    dirs = {name: tmp_path / name for name in ("a", "b", "c", "d", "cur")}
    for d in dirs.values():
        d.mkdir()
    _attempt(sessions, "s1", "x2", "completed", "2026-01-02T00:00:00", dirs["b"])
    _attempt(sessions, "s1", "x1", "completed", "2026-01-01T00:00:00", dirs["a"])
    _attempt(sessions, "s1", "x3", "failed", "2026-01-03T00:00:00", dirs["c"])
    _attempt(sessions, "s1", "x4", "completed", "2026-01-04T00:00:00", tmp_path / "gone")
    _attempt(sessions, "s1", "x5", "completed", "2026-01-05T00:00:00", dirs["cur"])
    _attempt(sessions, "s1", "x6", "completed", "2026-01-06T00:00:00", dirs["d"])
    assert _prior_run_dirs("s1", dirs["cur"]) == [dirs["a"], dirs["b"], dirs["d"]]
    assert _prior_run_dirs("s1", dirs["cur"], limit=2) == [dirs["b"], dirs["d"]]
    assert _prior_run_dirs("missing", dirs["cur"]) == []
    assert _prior_run_dirs(None, dirs["cur"]) == []


def _ir_run_ledger(run_dir: Path) -> None:
    """A persisted turn shaped like the audited session's (f9941f6d027f, turn 3): the IR run's metrics copy is recorded
    as ``backtest`` output of the ``alphakeel_research`` call that produced it."""
    (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    record = {"artifact": "ir_runs/x_ir/artifacts/metrics.csv", "call_id": "call_118306", "currency": None,
              "currency_conversion": None, "field": "fees_usdt", "scope": "ir_runs/x_ir", "source": "backtest",
              "status": "observed", "symbol": None, "timestamp": None, "tool": "backtest", "value": 4534.98312158,
              "venue": None}
    (run_dir / "artifacts" / "grounding_evidence.json").write_text(json.dumps({
        "schema_version": 1, "evidence": [record], "analysis_evidence": [],
        "analysis_completed": [{"call_id": "call_118306", "tool": "alphakeel_research", "recorded_at": "x"}],
    }), encoding="utf-8")


def test_an_ir_figure_cited_by_the_tool_that_ran_it_is_observed_in_a_later_turn(tmp_path: Path) -> None:
    """Regression (audited session, turn 4): "4534.98 | observed | fees | alphakeel_research" was rejected as
    "declared observed from alphakeel_research, whose results do not contain it" and released as (略※)."""
    prior = tmp_path / "run-ir"
    _ir_run_ledger(prior)
    later = _ledger(tmp_path / "run-2", prior_run_dirs=[prior])
    assert _fee_issues(later.validate_final_answer(_ANSWER)) == []


def test_a_carried_call_names_only_its_own_tool(tmp_path: Path) -> None:
    prior = tmp_path / "run-ir"
    _ir_run_ledger(prior)
    later = _ledger(tmp_path / "run-2", prior_run_dirs=[prior])
    answer = "手续费合计 4534.98。\n\n```figures\n4534.98 | observed | fees | alpha_zoo\n```"
    assert _fee_issues(later.validate_final_answer(answer))
