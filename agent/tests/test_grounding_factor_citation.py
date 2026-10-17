"""Factor-library names the session's factor tools never returned, and cited figures no retrieval of this session backs,
are listed on the result card (card notes); neither makes the answer invalid.

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


def _ingest(
    ledger: GroundingLedger, tool: str, payload: dict, call_id: str = "c1", success: bool = True, arguments: dict | None = None
) -> None:
    ledger.ingest_tool_result(
        tool_name=tool, arguments=arguments or {}, result=json.dumps(payload), call_id=call_id, success=success
    )


def _codes(ledger: GroundingLedger, text: str) -> list[tuple[str, str]]:
    return [(i["code"], i["reason"]) for i in ledger.validate_final_answer(text).issues]


def _notes(ledger: GroundingLedger, text: str, kind: str) -> list[str]:
    """The card notes of ``kind`` the answer gets; the free-text heuristics never raise an issue."""
    result = ledger.validate_final_answer(text)
    assert result.valid and result.issues == []
    return [note for k, note in result.card_notes if k == kind]


def _factor(ledger: GroundingLedger, text: str) -> list[str]:
    return _notes(ledger, text, "factor")


def _kinds(ledger: GroundingLedger, text: str) -> list[str]:
    """Issue codes, then card-note kinds, of the answer."""
    result = ledger.validate_final_answer(text)
    return [i["code"] for i in result.issues] + [kind for kind, _ in result.card_notes]


def _clean(ledger: GroundingLedger, text: str) -> bool:
    result = ledger.validate_final_answer(text)
    return result.issues == [] and result.card_notes == ()


def test_a_zoo_name_needs_a_factor_tool_result(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    answer = "规则 gtja191_010 的 IC 为正。"
    assert _factor(ledger, answer) == ["gtja191_010"]
    _ingest(ledger, "alpha_zoo", {"items": [{"id": "gtja191_010", "zoo": "gtja191"}]})
    assert _factor(ledger, answer) == []
    # the paper-style spelling resolves to the same id
    assert _factor(ledger, "GTJA-191 Alpha 010 表现不错") == []
    assert _factor(ledger, "gtja191_011 也行") == ["gtja191_011"]


def test_a_failed_or_unrelated_call_does_not_source_a_name(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    _ingest(ledger, "alpha_zoo", {"items": [{"id": "gtja191_010"}]}, success=False)
    _ingest(ledger, "web_search", {"results": [{"title": "gtja191_010 explained"}]}, call_id="c2")
    assert _factor(ledger, "gtja191_010") == ["gtja191_010"]


def test_a_backtest_that_ran_the_factor_through_zoo_factor_sources_it(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    _ingest(ledger, "backtest", {"status": "ok", "research_screen": {"facts": {"zoo_factors_used": ["gtja191_010"]}}})
    assert _factor(ledger, "gtja191_010") == []


def test_an_id_the_library_does_not_have_is_unknown_and_a_real_one_is_unsourced(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    assert _factor(ledger, "Kakushadze Alpha 999 的规则") == ["Kakushadze Alpha 999"]
    assert _factor(ledger, "Kakushadze Alpha 017 的规则") == ["Kakushadze Alpha 017"]


def test_a_name_inside_the_figures_block_is_not_read(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    text = "规模约 12 个。\n\n```figures\n12 | count | gtja191_010 |\n```"
    assert _factor(ledger, text) == []


def test_a_factor_name_never_blocks_and_a_repeated_name_is_noted_once(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    result = ledger.validate_final_answer("gtja191_010 很好，gtja191_010 也稳。")
    assert result.valid and result.issues == [] and result.card_notes == (("factor", "gtja191_010"),)


def test_a_cited_figure_needs_a_retrieval_this_session(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    assert _notes(ledger, _CITED, "citation") == ["12.5%"]
    _ingest(ledger, "web_search", {"results": [{"title": "彭博社 fund flows up 12.5%", "url": "https://example.test/a"}]})
    assert ledger._retrieval_calls == 1
    assert _clean(ledger, _CITED)


def test_a_failed_retrieval_does_not_count(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    _ingest(ledger, "web_search", {"error": "down"}, success=False)
    assert ledger._retrieval_calls == 0
    assert _kinds(ledger, _CITED) == ["citation"]


def test_citation_without_source_still_comes_first(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    text = "规模增长 12.5%。\n\n```figures\n12.5% | cited | |\n```"
    assert [i["reason"] for i in ledger.validate_final_answer(text).issues] == ["citation_without_source"]


def test_factor_ids_and_retrieval_count_carry_over_from_prior_turns(tmp_path: Path) -> None:
    first_copy = tmp_path / "sessions" / "s1" / "grounding" / "run-1.json"
    first = _ledger(tmp_path / "run-1", session_copy=first_copy)
    _ingest(first, "alpha_bench", {"rows": [{"id": "gtja191_010"}, {"id": "alpha101_017"}]})
    _ingest(first, "read_url", {"content": "AUM grew 12.5%"}, call_id="c2")
    _ingest(first, "get_stock_news", {"items": [{"title": "n 12.5%"}]}, call_id="c3")
    assert first_copy.is_file()
    payload = json.loads(first_copy.read_text(encoding="utf-8"))
    assert payload["factor_ids"] == ["alpha101_017", "gtja191_010"] and payload["retrieval_calls"] == 2

    later = _ledger(tmp_path / "run-2", prior_ledgers=[first_copy])
    assert later._factor_ids == {"gtja191_010", "alpha101_017"} and later._retrieval_calls == 2
    assert _factor(later, "Kakushadze Alpha 017 和 gtja191_010") == []
    assert _clean(later, _CITED)

    old = tmp_path / "sessions" / "s1" / "grounding" / "old.json"
    old.write_text(json.dumps({"schema_version": 1, "evidence": []}), encoding="utf-8")
    assert _ledger(tmp_path / "run-3", prior_ledgers=[old])._retrieval_calls == 0
    # the same ledger given twice is one set of calls, not two
    both = _ledger(tmp_path / "run-4", prior_ledgers=[first_copy, first_copy])
    assert both._retrieval_calls == 2


def _cited_value(value: str) -> str:
    return f"彭博社报道增长 {value}。\n\n```figures\n{value} | cited | 彭博社 |\n```"


def test_load_skill_is_no_retrieval(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    _ingest(ledger, "load_skill", {"name": "backtest-guide", "text": "Sharpe above 12.5 is rare"})
    assert ledger._retrieval_calls == 0 and ledger._retrieval_records == []
    assert _kinds(ledger, _CITED) == ["citation"]


def test_a_retrieval_without_the_value_does_not_back_the_figure(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    _ingest(ledger, "web_search", {"results": [{"title": "fund flows", "snippet": "AUM 3,400 and 7.1%"}]})
    assert _notes(ledger, _CITED, "citation") == ["12.5%"]
    assert [[3400.0, False], [7.1, True]] == ledger._retrieval_records[0]["numbers"]


def test_a_retrieved_percent_backs_the_figure_as_a_percent_or_a_fraction(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    _ingest(ledger, "web_search", {"results": [{"snippet": "growth was 4.2% last year"}]})
    assert _clean(ledger, _cited_value("4.2"))
    assert _clean(ledger, _cited_value("0.042"))
    assert _kinds(ledger, _cited_value("5.3")) == ["citation"]


def test_retrieval_records_persist_and_are_capped_per_call(tmp_path: Path) -> None:
    session_copy = tmp_path / "sessions" / "s1" / "grounding" / "run-1.json"
    first = _ledger(tmp_path / "run-1", session_copy=session_copy)
    _ingest(first, "read_url", {"content": " ".join(str(n) for n in range(1, 6001))})
    (record,) = first._retrieval_records
    assert len(record["numbers"]) == 2000 and record["numbers"][0] == [1.0, False] and record["numbers"][-1] == [2000.0, False]
    persisted = json.loads(session_copy.read_text(encoding="utf-8"))
    assert "retrieved_numbers" not in persisted and persisted["retrieval_records"][0]["call_id"] == "c1"
    later = _ledger(tmp_path / "run-2", prior_ledgers=[session_copy])
    assert later._retrieval_records[0]["numbers"] == record["numbers"]
    assert _clean(later, _cited_value("1999.0"))
    assert _kinds(later, _cited_value("4999.5")) == ["citation"]


def test_an_old_flat_retrieved_numbers_list_is_ignored(tmp_path: Path) -> None:
    old = tmp_path / "sessions" / "s1" / "grounding" / "old.json"
    old.parent.mkdir(parents=True)
    old.write_text(
        json.dumps({"schema_version": 1, "retrieval_calls": 3, "retrieved_numbers": [12.5], "evidence": []}), encoding="utf-8"
    )
    later = _ledger(tmp_path / "run", prior_ledgers=[old])
    assert later._retrieval_calls == 0
    assert _kinds(later, _CITED) == ["citation"]


_QUERY = "彭博社 基金规模 增长 12.5%"


def _search(ledger: GroundingLedger, snippet: str, call_id: str = "c1", tool: str = "web_search") -> None:
    args = {"query": _QUERY, "limit": 5}
    _ingest(
        ledger,
        tool,
        {"query": _QUERY, "mode": "search", "results": [{"title": "fund flows", "snippet": snippet}]},
        call_id=call_id,
        arguments=args,
    )


def test_a_call_that_only_echoes_its_query_does_not_back_the_figure(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    _search(ledger, "AUM rose sharply last quarter")
    assert _kinds(ledger, _CITED) == ["citation"]
    other = _ledger(tmp_path / "run-2")
    _search(other, "AUM rose 12.5% last quarter")
    assert _clean(other, _CITED)


def test_a_plain_text_result_loses_the_calls_own_arguments(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    ledger.ingest_tool_result(
        tool_name="read_url", arguments={"url": "https://example.test/12.5", "length": 5000},
        result="searched https://example.test/12.5 length 5000: nothing relevant", call_id="c1", success=True,
    )
    assert _kinds(ledger, _CITED) == ["citation"]
    other = _ledger(tmp_path / "run-2")
    other.ingest_tool_result(
        tool_name="read_url", arguments={"url": "https://example.test/a", "length": 5},
        result="AUM rose 12.5% (length 5)", call_id="c1", success=True,
    )
    assert _clean(other, _CITED)


def test_sentiment_retrieves_nothing(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    _ingest(ledger, "sentiment", {"mode": "sentiment_score", "text": _QUERY, "score": 0.1}, arguments={"text": _QUERY})
    assert ledger._retrieval_calls == 0
    assert _kinds(ledger, _CITED) == ["citation"]


def test_the_newest_of_many_calls_is_kept(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions" / "s1" / "grounding"
    copies: list[Path] = []
    ledger = None
    for turn in range(1, 6):
        copy = sessions / f"run-{turn}.json"
        ledger = _ledger(tmp_path / f"run-{turn}", prior_ledgers=list(copies), session_copy=copy)
        _ingest(ledger, "read_url", {"content": " ".join(str(turn * 10000 + n) for n in range(1000))})
        copies.append(copy)
    assert ledger is not None and ledger._retrieval_calls == 5
    assert _clean(ledger, _cited_value("50500.0"))
    assert _clean(ledger, _cited_value("10500.0"))


def test_only_the_sixty_newest_records_are_kept(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    for n in range(70):
        _ingest(ledger, "read_url", {"content": f"value {n + 1000}"}, call_id=f"c{n}")
    assert ledger._retrieval_calls == 60
    assert ledger._retrieval_records[0]["call_id"] == "c10" and ledger._retrieval_records[-1]["call_id"] == "c69"


def _numbers(text: str) -> list[tuple[float, bool]]:
    from src.agent.grounding.ledger import _numbers_in

    return _numbers_in(text)


def test_a_thousands_comma_joins_only_groups_of_three() -> None:
    assert _numbers("5,10,15") == [(5.0, False), (10.0, False), (15.0, False)]
    assert _numbers("1,234,567 and 4.2% and -12.5%") == [(1234567.0, False), (4.2, True), (12.5, True)]


def _backs(tmp_path: Path, source: str, value: str, name: str = "r") -> bool:
    ledger = _ledger(tmp_path / name)
    _ingest(ledger, "read_url", {"content": source})
    return _clean(ledger, _cited_value(value))


def test_matching_is_tight(tmp_path: Path) -> None:
    assert not _backs(tmp_path, "volume 137 shares", "1.37", "a")
    assert _backs(tmp_path, "growth 4.2% yoy", "4.2", "b") and _backs(tmp_path, "growth 4.2% yoy", "0.042", "c")
    assert _backs(tmp_path, "down -12.5% yoy", "12.5", "d")
    # half of the last written digit: a source 12.54 backs 12.5 but not 12.50 -> 12.54 is within 0.005 of 12.54 only
    assert _backs(tmp_path, "rate 12.54", "12.5", "e") and not _backs(tmp_path, "rate 12.54", "12.50", "f")
    assert not _backs(tmp_path, "rate 12.6", "12.5", "g")


def test_a_decimal_fraction_backs_a_percent_figure(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    _ingest(ledger, "read_url", {"content": "share 0.125 of assets"})
    assert _clean(ledger, _CITED)


def test_a_random_page_backs_few_fabricated_sharpe_values(tmp_path: Path) -> None:
    import random

    rng = random.Random(20261017)
    page = (
        [f"{rng.uniform(1, 1000):.2f}" for _ in range(700)]
        + [str(rng.randint(1000, 5_000_000)) for _ in range(250)]
        + [f"{rng.uniform(0.5, 30):.1f}%" for _ in range(50)]
    )
    rng.shuffle(page)
    ledger = _ledger(tmp_path / "run")
    _ingest(ledger, "read_url", {"content": " ".join(page)})
    backed = sum(
        1 for _ in range(200) if _clean(ledger, _cited_value(f"{rng.uniform(0.30, 3.00):.2f}"))
    )
    assert backed / 200 < 0.05


def test_every_retrieval_record_backs_a_figure_whatever_source_the_note_names(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    _ingest(ledger, "web_search", {"results": [{"title": "彭博社 fund report", "snippet": "AUM up 12.5%"}]}, call_id="c1")
    _ingest(ledger, "web_search", {"results": [{"title": "other blog", "snippet": "AUM up 99.9%"}]}, call_id="c2")
    assert _clean(ledger, _cited_value("12.5"))
    assert _clean(ledger, _cited_value("99.9"))
    assert _notes(ledger, _cited_value("77.7"), "citation") == ["77.7"]


def test_a_rewritten_echo_of_the_call_backs_nothing(tmp_path: Path) -> None:
    # the url, a trailing space and the arXiv search_query are the call's own input, not a document body
    ledger = _ledger(tmp_path / "run")
    _ingest(
        ledger, "read_url", {"url": "https://example.test/report/12.5/", "content": "nothing relevant"},
        arguments={"url": "https://example.test/report/12.5"},
    )
    _ingest(ledger, "web_search", {"results": [{"snippet": "about fund 12.5% growth"}]}, call_id="c2",
            arguments={"query": "fund 12.5% growth "})
    _ingest(ledger, "research_papers", {"search_query": "all:fund AND all:12.5", "papers": []}, call_id="c3",
            arguments={"query": "fund 12.5"})
    _ingest(ledger, "web_search", {"results": [{"snippet": "fund+12.5%25+growth"}]}, call_id="c4",
            arguments={"query": "fund 12.5% growth"})
    assert _kinds(ledger, _CITED) == ["citation"]


def test_dates_and_clock_times_back_no_figure(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    _ingest(ledger, "read_url", {"content": "published 2024-12-05 10:30, revised 2025年3月8日 and 4月12号"})
    for value in ("12%", "5%", "30%", "3%", "8%", "4%"):
        assert _notes(ledger, _cited_value(value), "citation") == [value], value


def test_a_document_the_model_wrote_is_no_source(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "run")
    note = tmp_path / "run" / "notes.md"
    _ingest(ledger, "write_file", {"status": "ok", "path": str(note)}, arguments={"path": str(note), "content": "AUM 12.5%"})
    _ingest(ledger, "read_document", {"text": "AUM 12.5%"}, call_id="c2", arguments={"file_path": str(note)})
    assert ledger._retrieval_calls == 0
    assert _kinds(ledger, _CITED) == ["citation"]
