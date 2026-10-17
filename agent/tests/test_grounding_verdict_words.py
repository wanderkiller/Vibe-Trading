"""Verdict words without a passed screen are stated on the result card as no system verdict; the answer is not blocked.

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
        ("无风险收益率 4%", []),
        ("risk-free rate 4%", []),
        ("risk\u2011free rate 4%", []),
        ("the risk-free asset and risk-free return", ["risk-free"]),
        ("该策略无风险", ["无风险"]),
        ("the strategy is risk\u2011free", ["risk-free"]),
        ("the strategy is risk\u2212free", ["risk-free"]),
        ("经过验证的策略", ["经过验证的策略"]),
        ("The edge is verified", ["verified"]),
        ("unverified", []),
        ("收益不错已验证", ["已验证"]),
        ("不仅已验证", ["已验证"]),
        ("不但已验证", ["已验证"]),
        ("尚未验证", []),
        ("It is not yet fully validated.", []),
        ("It is fine, and validated.", ["validated"]),
        ("It's verified.", ["verified"]),
        ("It\u2019s now verified.", ["verified"]),
        ("策略已经过验证", ["已经过验证"]),
        ("策略已经验证", ["已经验证"]),
        ("该结果并非已验证", []),
        ("绝非无风险", []),
        ("Not overfit - validated.", ["validated"]),
        ("Not overfit \u2014 validated.", ["validated"]),
        ("not overfit (validated)", ["validated"]),
        ("不/已验证", ["已验证"]),
        ("It was not tested and validated.", []),
        ("We do not consider this strategy validated.", []),
        ("Sharpe is not bad and it is validated", ["validated"]),
    ],
)
def test_scan_cases(text: str, expected: list[str]) -> None:
    assert _phrases(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "No lookahead was found, so the strategy is validated.",
        "Not only robust but validated.",
        "There is no doubt the edge is validated.",
        "We found no leakage and the results are validated",
        "Sharpe is not bad and the model is validated",
        "It is not just profitable, it is validated",
        "The strategy is production ready.",
        "This is a risk free strategy.",
        "The strategy delivers risk-free returns.",
        "该策略无风险 收益稳定",
        "该策略未来可上实盘",
        "The strategy is verified.",
    ],
)
def test_these_state_a_verdict(text: str) -> None:
    assert _phrases(text) != []


@pytest.mark.parametrize(
    "text",
    [
        "It is not validated.",
        "It is not yet validated.",
        "It has not been validated.",
        "It isn't validated.",
        "It was never verified.",
        "It cannot be considered production ready.",
        "I verified the data has no gaps.",
        "经过验证，数据无缺失",
        "无风险利率为 4%",
        "risk-free rate 4%",
        "无风险收益率 4%",
    ],
)
def test_these_state_no_verdict(text: str) -> None:
    assert _phrases(text) == []


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


_ANSWER = "只有 5 笔交易，风险真正得到控制，策略已验证。"


def test_no_session_means_the_verdict_words_are_card_notes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("src.agent.grounding.policies.current_llm_session_id", lambda: "")
    result = _ledger(tmp_path).validate_final_answer(_ANSWER)
    assert result.valid and result.issues == []
    assert result.card_notes == (("verdict", "风险真正得到控制"), ("verdict", "已验证"))
    assert result.released_text == _ANSWER


def test_a_session_without_a_passed_trial_releases_the_answer_with_card_notes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("src.agent.grounding.policies.current_llm_session_id", lambda: "s1")
    monkeypatch.setattr(arl, "ledger_rows", lambda sid: [])
    monkeypatch.setattr(arl, "engine_rows", lambda sid: [])
    result = _ledger(tmp_path).validate_final_answer("策略已验证，已验证")
    assert result.valid and result.card_notes == (("verdict", "已验证"),)


def test_a_passed_trial_allows_the_verdict(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("src.agent.grounding.policies.current_llm_session_id", lambda: "s1")
    monkeypatch.setattr(arl, "session_has_passed_trial", lambda sid: True)
    result = _ledger(tmp_path).validate_final_answer(_ANSWER)
    assert result.valid and result.card_notes == ()


def test_the_card_states_the_notes(monkeypatch) -> None:
    monkeypatch.setattr(arl, "_ir_section", lambda sid, zh: [])
    monkeypatch.setattr(arl, "_engine_section", lambda sid, zh, first: [])
    notes = (("verdict", "已验证"), ("verdict", "可上实盘"), ("citation", "12.5%"), ("factor", "gtja191_010"))
    zh = arl.render_card("s1", True, notes)
    assert zh == (
        "**答复核对（系统根据本会话记录生成）**\n"
        "- 本会话没有任何版本通过初筛；答复中的「已验证」、「可上实盘」不是系统判定。\n"
        "- 答复标为引用的数字 「12.5%」 在本会话检索到的资料中没有找到。\n"
        "- 答复提到的因子名 「gtja191_010」 在本会话没有被使用或检索到。"
    )
    en = arl.render_card("s1", False, [["verdict", "validated"], ["factor", "[x](y)\n#"]])
    assert en.splitlines()[1] == (
        "- No version in this session passed the screen; these words in the answer are not a system verdict: \u201cvalidated\u201d."
    )
    assert not any(c in en.splitlines()[2] for c in "[]()#") and len(en.splitlines()) == 3
    assert arl.render_card("s1", True, ()) is None and arl.render_card(None, True, notes) is None


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


def test_a_reason_renders_with_full_width_brackets() -> None:
    out = arl._safe_reason("[官方确认](http://x)")
    assert not any(c in out for c in "[]()") and "官方确认" in out
