"""The AlphaKeel hand-off writer (review B-P1-2, D-P2-8; R2 requires source.window and trials)."""

from __future__ import annotations

import json

import pytest

from alphakeel_research.errors import ApiError
from alphakeel_research.handoff import write_handoff

RULES = {"max_loss": {"value": "0.015", "robust": ["0.01", "0.02"]}, "max_hold_hours": {"value": 48, "robust": [36, 72]}}
B2_CARD = {"data_audit": {"auditable": True, "sources": {"alphakeel_b2": {"auditable": True}}, "non_auditable_sources": []},
           "metrics": {"sharpe": 2.7}}
CCXT_CARD = {"data_audit": {"auditable": False, "sources": {"ccxt": {"auditable": False}}, "non_auditable_sources": ["ccxt"]},
             "metrics": {"sharpe": 3.1}}


def _pkg(tmp_path):
    (tmp_path / "evidence").mkdir()
    (tmp_path / "evidence" / "sweep.csv").write_text("variant,sharpe\n", encoding="utf-8")
    return tmp_path


def _write(tmp_path, **kw):
    args = dict(handoff_id="vt-test-0001", summary="carry stop", rules=RULES, run_card=B2_CARD,
                window={"start": "2024-01-01", "end": "2026-06-30"}, trials_count=480, trials_evidence="evidence/sweep.csv",
                run_id="run-1", daily_returns=[0.01, -0.02, 0.03, 0.0, 0.02])
    args.update(kw)
    return write_handoff(_pkg(tmp_path) if not (tmp_path / "evidence").exists() else tmp_path, **args)


def test_an_auditable_run_writes_window_trials_and_alphakeel_statistics(tmp_path):
    p = _write(tmp_path)
    assert p["schema"] == "alphakeel.handoff.params/1" and p["strategy"] == "cross_venue_carry"
    assert p["source"]["window"] == {"start": "2024-01-01", "end": "2026-06-30"}
    assert p["trials"] == {"count": 480, "evidence": "evidence/sweep.csv"}
    assert p["source"]["data_audit"]["classification"] == "auditable" and not p["summary"].startswith("EXPLORATORY")
    assert json.loads((tmp_path / "params.json").read_text()) == p
    ev = json.loads((tmp_path / "evidence" / "handoff-audit.json").read_text())
    ak = ev["alphakeel_metrics"]
    assert ak["trials"] == {"count": 480} and ak["sortino_annualised"] == pytest.approx(17.08800749063506, abs=1e-10)
    assert ev["vibe_trading_metrics"] == {"sharpe": 2.7}  # kept side by side, not mixed


def test_a_non_auditable_data_source_makes_the_handoff_exploratory(tmp_path):
    p = _write(tmp_path, run_card=CCXT_CARD)
    assert p["source"]["data_audit"] == {"auditable": False, "classification": "exploratory", "sources": ["ccxt"],
                                         "non_auditable_sources": ["ccxt"]}
    assert p["summary"].startswith("EXPLORATORY (non-auditable data)")
    ev = json.loads((tmp_path / "evidence" / "handoff-audit.json").read_text())
    assert ev["classification"] == "exploratory" and "ccxt" in ev["reasons"][0]
    with pytest.raises(ApiError, match="data_audit"):
        _write(tmp_path, run_card={"metrics": {}})  # unknown provenance is not "auditable"


@pytest.mark.parametrize("kw, match", [
    ({"trials_count": 0}, "trials_count"),
    ({"trials_evidence": "evidence/missing.csv"}, "must be an existing file"),
    ({"trials_evidence": "../outside.csv"}, "inside the package"),
    ({"window": {"start": "2024-01-01"}}, "window"),
    ({"window": {"start": "2026-07-01", "end": "2026-06-30"}}, "window"),
    ({"rules": {"funding_zscore": {"value": "2"}}}, "not an AlphaKeel rule"),
    ({"handoff_id": "x"}, "handoff id"),
])
def test_required_fields_are_checked_not_defaulted(tmp_path, kw, match):
    with pytest.raises(ApiError, match=match):
        _write(tmp_path, **kw)


def test_the_window_must_cover_the_data_the_research_service_handed_out(tmp_path):
    who = {"credential": "tok-1", "holdout_days": 60,
           "data_horizon": {"max_window_end_ms": 1_782_864_000_000, "max_cutoff_ms": 1_782_864_000_000, "packs": 3}}  # 2026-07-01
    p = _write(tmp_path, research_credential=who)  # window ends 2026-06-30 inclusive = 2026-07-01 00:00 exclusive
    assert p["source"]["research_credential"] == "tok-1"
    with pytest.raises(ApiError, match="must cover every day"):
        _write(tmp_path, research_credential=who, window={"start": "2024-01-01", "end": "2026-06-29"})


def test_without_a_research_credential_the_window_is_self_reported_and_the_evidence_says_so(tmp_path):
    _write(tmp_path)
    ev = json.loads((tmp_path / "evidence" / "handoff-audit.json").read_text())
    assert any("cannot check the research window" in r for r in ev["reasons"]), ev["reasons"]
    with pytest.raises(ApiError, match="credential id"):
        _write(tmp_path, research_credential={"holdout_days": 60})  # no id: AlphaKeel could not check anything
    with pytest.raises(ApiError, match="credential id"):
        _write(tmp_path, research_credential={"credential": " tok-1"})
