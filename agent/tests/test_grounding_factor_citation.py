"""Factor-library names must be library-sourced; a cited figure needs a retrieval this session made.

Regression of session f9941f6d027f: hand-written rules carried zoo names ("GTJA-191 Alpha 010", "Kakushadze Alpha 017"), and a
figure was declared cited from "彭博社" although no retrieval tool had been called. Every value here is synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agent.grounding.ledger import GroundingLedger

pytestmark = pytest.mark.unit

_CITED = "彭博社报道该基金规模增长 12.5%。\n\n```figures\n12.5% | cited | 彭博社 |\n```"


def _ledger(run_dir: Path, **kwargs) -> GroundingLedger:
    run_dir.mkdir(parents=True, exist_ok=True)
    return GroundingLedger(run_dir=run_dir, user_message="总结一下", **kwargs)


def _ingest(ledger: GroundingLedger, tool: str, payload: dict, call_id: str = "c1", success: bool = True) -> None:
    ledger.ingest_tool_result(
        tool_name=tool, arguments={}, result=json.dumps(payload), call_id=call_id, success=success
    )


def _codes(ledger: GroundingLedger, text: str) -> list[tuple[str, str]]:
    return [(i["code"], i["reason"]) for i in ledger.validate_final_answer(text).issues]


def _factor(issues: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [i for i in issues if i[0].startswith("factor_")]


def test_a_zoo_name_needs_a_factor_tool_result(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    answer = "规则 gtja191_010 的 IC 为正。"
    assert _factor(_codes(ledger, answer)) == [("factor_unsourced", "factor_never_returned")]
    _ingest(ledger, "alpha_zoo", {"items": [{"id": "gtja191_010", "zoo": "gtja191"}]})
    assert _factor(_codes(ledger, answer)) == []
    # the paper-style spelling resolves to the same id
    assert _factor(_codes(ledger, "GTJA-191 Alpha 010 表现不错")) == []
    assert _factor(_codes(ledger, "gtja191_011 也行")) == [("factor_unsourced", "factor_never_returned")]


def test_a_failed_or_unrelated_call_does_not_source_a_name(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    _ingest(ledger, "alpha_zoo", {"items": [{"id": "gtja191_010"}]}, success=False)
    _ingest(ledger, "web_search", {"results": [{"title": "gtja191_010 explained"}]}, call_id="c2")
    assert _factor(_codes(ledger, "gtja191_010"))


def test_a_backtest_that_ran_the_factor_through_zoo_factor_sources_it(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    _ingest(ledger, "backtest", {"status": "ok", "research_screen": {"facts": {"zoo_factors_used": ["gtja191_010"]}}})
    assert _factor(_codes(ledger, "gtja191_010")) == []


def test_an_id_the_library_does_not_have_is_unknown_and_a_real_one_is_unsourced(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    unknown = ledger.validate_final_answer("Kakushadze Alpha 999 的规则").issues
    assert [(i["code"], i["reason"]) for i in unknown] == [("factor_unknown", "not_in_factor_library")]
    assert unknown[0]["message"] == (
        "Kakushadze Alpha 999 is not a factor in alpha_zoo; describe the rule in your own words, "
        "without a factor-library name."
    )
    assert unknown[0]["span"] == [0, len("Kakushadze Alpha 999 的规则")] and unknown[0]["symbol"] is None
    real = ledger.validate_final_answer("Kakushadze Alpha 017 的规则").issues
    assert [(i["code"], i["reason"]) for i in real] == [("factor_unsourced", "factor_never_returned")]
    assert real[0]["message"].startswith("Kakushadze Alpha 017 was not returned by any factor tool in this session;")


def test_a_name_inside_the_figures_block_is_not_read(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    text = "规模约 12 个。\n\n```figures\n12 | count | gtja191_010 |\n```"
    assert _factor(_codes(ledger, text)) == []


def test_a_factor_issue_is_not_redactable(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    text = "gtja191_010 的收益是 3.2%。"
    result = ledger.validate_final_answer(text)
    assert any(i["code"] == "factor_unsourced" for i in result.issues)
    assert ledger.redacted_release(text, result) is None
    prompt = ledger.correction_prompt(result)
    assert "gtja191_010 was not returned by any factor tool" in prompt and "plain words" in prompt


def test_a_cited_figure_needs_a_retrieval_this_session(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    issues = ledger.validate_final_answer(_CITED).issues
    assert [(i["code"], i["reason"]) for i in issues] == [("numeric_claim_conflict", "citation_not_retrieved")]
    assert "retrieved no document, page, news or paper" in issues[0]["message"]
    _ingest(ledger, "web_search", {"results": [{"title": "彭博社 fund flows up 12.5%", "url": "https://example.test/a"}]})
    assert ledger._retrieval_calls == 1
    assert ledger.validate_final_answer(_CITED).issues == []


def test_a_failed_retrieval_does_not_count(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    _ingest(ledger, "web_search", {"error": "down"}, success=False)
    assert ledger._retrieval_calls == 0
    assert [i["reason"] for i in ledger.validate_final_answer(_CITED).issues] == ["citation_not_retrieved"]


def test_citation_without_source_still_comes_first(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    text = "规模增长 12.5%。\n\n```figures\n12.5% | cited | |\n```"
    assert [i["reason"] for i in ledger.validate_final_answer(text).issues] == ["citation_without_source"]


def test_factor_ids_and_retrieval_count_carry_over_from_prior_turns(tmp_path: Path) -> None:
    first_copy = tmp_path / "sessions" / "s1" / "grounding" / "run-1.json"
    first = _ledger(tmp_path / "run-1", session_copy=first_copy)
    _ingest(first, "alpha_bench", {"rows": [{"id": "gtja191_010"}, {"id": "alpha101_017"}]})
    _ingest(first, "read_url", {"text": "AUM grew 12.5%"}, call_id="c2")
    _ingest(first, "get_stock_news", {"items": [{"title": "n 12.5%"}]}, call_id="c3")
    assert first_copy.is_file()
    payload = json.loads(first_copy.read_text(encoding="utf-8"))
    assert payload["factor_ids"] == ["alpha101_017", "gtja191_010"] and payload["retrieval_calls"] == 2

    later = _ledger(tmp_path / "run-2", prior_ledgers=[first_copy])
    assert later._factor_ids == {"gtja191_010", "alpha101_017"} and later._retrieval_calls == 2
    assert _factor(_codes(later, "Kakushadze Alpha 017 和 gtja191_010")) == []
    assert later.validate_final_answer(_CITED).issues == []

    old = tmp_path / "sessions" / "s1" / "grounding" / "old.json"
    old.write_text(json.dumps({"schema_version": 1, "evidence": []}), encoding="utf-8")
    assert _ledger(tmp_path / "run-3", prior_ledgers=[old])._retrieval_calls == 0
    both = _ledger(tmp_path / "run-4", prior_ledgers=[first_copy, first_copy])
    assert both._retrieval_calls == 4


def _cited_value(value: str) -> str:
    return f"彭博社报道增长 {value}。\n\n```figures\n{value} | cited | 彭博社 |\n```"


def test_load_skill_is_no_retrieval(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    _ingest(ledger, "load_skill", {"name": "backtest-guide", "text": "Sharpe above 12.5 is rare"})
    assert ledger._retrieval_calls == 0 and ledger._retrieved_numbers == []
    assert [i["reason"] for i in ledger.validate_final_answer(_CITED).issues] == ["citation_not_retrieved"]


def test_a_retrieval_without_the_value_does_not_back_the_figure(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    _ingest(ledger, "web_search", {"results": [{"title": "fund flows", "text": "AUM 3,400 and 7.1%"}]})
    issues = ledger.validate_final_answer(_CITED).issues
    assert [(i["code"], i["reason"]) for i in issues] == [("numeric_claim_conflict", "citation_value_not_retrieved")]
    assert "appears in no document, page, news or paper" in issues[0]["message"]
    assert ledger.redacted_release(_CITED, ledger.validate_final_answer(_CITED)) is not None
    assert {3400.0, 7.1} <= set(ledger._retrieved_numbers)


def test_a_retrieved_percent_backs_the_figure_as_a_percent_or_a_fraction(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    _ingest(ledger, "web_search", {"results": [{"text": "growth was 4.2% last year"}]})
    assert ledger.validate_final_answer(_cited_value("4.2")).issues == []
    assert ledger.validate_final_answer(_cited_value("0.042")).issues == []
    assert [i["reason"] for i in ledger.validate_final_answer(_cited_value("5.3")).issues] == [
        "citation_value_not_retrieved"
    ]


def test_retrieved_numbers_persist_and_are_capped(tmp_path: Path) -> None:
    session_copy = tmp_path / "sessions" / "s1" / "grounding" / "run-1.json"
    first = _ledger(tmp_path / "run-1", session_copy=session_copy)
    _ingest(first, "read_url", {"text": " ".join(str(n) for n in range(1, 6001))})
    assert len(first._retrieved_numbers) == 5000 and first._retrieved_numbers[0] == 1.0
    assert first._retrieved_numbers[-1] == 5000.0
    later = _ledger(tmp_path / "run-2", prior_ledgers=[session_copy])
    assert later._retrieved_numbers == first._retrieved_numbers
    assert later.validate_final_answer(_cited_value("4999")).issues == []
    assert [i["reason"] for i in later.validate_final_answer(_cited_value("765432.1")).issues] == [
        "citation_value_not_retrieved"
    ]
