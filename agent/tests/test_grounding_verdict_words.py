"""Verdict words need a passed screen.

Regression of session f9941f6d027f: a 5-round-trip, Sharpe 0.10 result was called "已验证" / "风险真正得到控制" / "稳健正收益" while no
trial passed the screen. Every value here is synthetic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agent.grounding.ledger import GroundingLedger
from src.agent.grounding.verdict_words import scan_verdict_claims
from src.tools import alphakeel_research_ledger as arl

pytestmark = pytest.mark.unit


def _phrases(text: str) -> list[str]:
    return [p for p, _ in scan_verdict_claims(text)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("策略已验证", ["已验证"]),
        ("这个版本可上线", ["可上线"]),
        ("风险真正得到控制，稳健正收益", ["风险真正得到控制", "稳健正收益"]),
        ("validated", ["validated"]),
        ("It is production-ready and Risk-Free.", ["production-ready", "Risk-Free"]),
        ("未验证", []),
        ("尚未验证通过", []),
        ("不能上实盘", []),
        ("不可以上实盘，也不可上线", []),
        ("The edge is not validated", []),
        ("this isn't validated yet", []),
        ("No. Validated results only", ["Validated"]),
        ("表现不错，已验证", ["已验证"]),
        ("invalidated", []),
        ("无风险利率为 4%", []),
        ("无风险 收益率 4%", []),
        ("risk-free rate 4%", []),
        ("risk\u2011free rate 4%", []),
        ("the risk-free asset and risk-free return", []),
        ("该策略无风险", ["无风险"]),
        ("the strategy is risk\u2011free", ["risk-free"]),
        ("the strategy is risk\u2212free", ["risk-free"]),
        ("经过验证的结论", ["经过验证"]),
        ("The edge is verified", ["verified"]),
        ("unverified", []),
        ("收益不错已验证", ["已验证"]),
        ("不仅已验证", ["已验证"]),
        ("不但已验证", ["已验证"]),
        ("尚未验证", []),
        ("It is not yet fully, completely and thoroughly validated.", []),
        ("It is fine, and validated.", ["validated"]),
    ],
)
def test_scan_cases(text: str, expected: list[str]) -> None:
    assert _phrases(text) == expected


def test_span_points_at_the_phrase() -> None:
    text = "结论：策略已验证。"
    (phrase, (start, end)), = scan_verdict_claims(text)
    assert text[start:end] == phrase == "已验证"


def test_a_model_written_card_title_does_not_disable_the_gate() -> None:
    """The real result card is appended after grounding, so a draft never contains it: a title is no skip marker."""
    assert _phrases("**研究结果汇总**\n该策略已验证，可上实盘。") == ["已验证", "可上实盘"]
    assert _phrases("**Research results**\nThe strategy is validated and ready for live.") == [
        "validated",
        "ready for live",
    ]
    assert _phrases("answer\n\n---\n**Generic engine backtests**\nvalidated") == ["validated"]
    assert _phrases("已验证\n\n---\n" + arl._TEXT[True]["title"] + "\n已验证") == ["已验证", "已验证"]


def test_the_figures_block_is_not_read() -> None:
    block = "5 笔\n\n```figures\n5 | count | 已验证的笔数 |\n```"
    assert _phrases(block) == []


def _ledger(tmp_path: Path) -> GroundingLedger:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return GroundingLedger(run_dir=run_dir, user_message="总结一下")


_ANSWER = "夏普 0.10，风险真正得到控制，策略已验证。"


def test_no_session_means_verdict_unsupported(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("src.agent.grounding.policies.current_llm_session_id", lambda: "")
    result = _ledger(tmp_path).validate_final_answer(_ANSWER)
    verdicts = [i for i in result.issues if i["code"] == "verdict_unsupported"]
    assert [i["reason"] for i in verdicts] == ["no_passed_trial", "no_passed_trial"]
    assert verdicts[0]["message"] == (
        '"风险真正得到控制" states a research verdict, but no backtest of this session passed the screen; '
        "describe the result without that verdict (the result card carries the verdict)."
    )
    assert [i["claim"] for i in verdicts] == [_ANSWER, _ANSWER]


def test_a_session_without_a_passed_trial_is_unsupported_and_not_redactable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("src.agent.grounding.policies.current_llm_session_id", lambda: "s1")
    monkeypatch.setattr(arl, "ledger_rows", lambda sid: [])
    monkeypatch.setattr(arl, "engine_rows", lambda sid: [])
    ledger = _ledger(tmp_path)
    result = ledger.validate_final_answer("策略已验证")
    assert [i["code"] for i in result.issues] == ["verdict_unsupported"]
    assert ledger.redacted_release("策略已验证", result) is None
    assert "do not call the result validated" in ledger.correction_prompt(result)


def test_a_passed_trial_allows_the_verdict(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("src.agent.grounding.policies.current_llm_session_id", lambda: "s1")
    monkeypatch.setattr(arl, "session_has_passed_trial", lambda sid: True)
    issues = _ledger(tmp_path).validate_final_answer(_ANSWER).issues
    assert [i for i in issues if i["code"] == "verdict_unsupported"] == []


def test_session_has_passed_trial_reads_both_ledgers(monkeypatch) -> None:
    assert arl.session_has_passed_trial(None) is False and arl.session_has_passed_trial("") is False
    ir = {"strategy_id": "a"}
    monkeypatch.setattr(arl, "ledger_rows", lambda sid: [ir])
    monkeypatch.setattr(arl, "engine_rows", lambda sid: [])
    monkeypatch.setattr(arl, "verified_card", lambda row: {"screen": {"evidence_status": "failed_screen"}})
    assert arl.session_has_passed_trial("s1") is False
    monkeypatch.setattr(arl, "verified_card", lambda row: {"screen": {"evidence_status": "passed_screen"}})
    assert arl.session_has_passed_trial("s1") is True
    monkeypatch.setattr(arl, "verified_card", lambda row: None)
    monkeypatch.setattr(arl, "engine_rows", lambda sid: [{"trial_key": "k"}])
    monkeypatch.setattr(arl, "verified_engine_screen", lambda row: {})
    monkeypatch.setattr(arl, "_current_screen", lambda sid, row, screen: {"evidence_status": "passed_screen"})
    assert arl.session_has_passed_trial("s1") is True
    monkeypatch.setattr(arl, "_current_screen", lambda sid, row, screen: None)
    assert arl.session_has_passed_trial("s1") is False
