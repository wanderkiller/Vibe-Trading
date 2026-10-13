"""ir_validate reports every IR problem at once, locally, before AlphaKeel is asked (session b9aa13a496a4).

The agent spent ~20 ir_validate calls guessing the Strategy IR shape: AlphaKeel's validator stops at the first serde
error. ``ir.problems`` lists every missing/unknown field of every section in one call, and the tool returns them
without calling the service. It also wrote ``exit.max_loss: "0.01"`` (a fraction) where the IR wants a USDT amount.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alphakeel_research import ir

pytestmark = pytest.mark.unit

_EXAMPLE = Path(ir.__file__).resolve().parent / "examples" / "ir" / "strategy.json"
_SECTIONS = ("universe", "assumptions", "entry", "exit", "sizing", "parameters", "provenance")


def _empty_sections() -> dict:
    return {"schema": ir.SCHEMA, "name": "x", "kind": ir.KIND, **{k: {} for k in _SECTIONS}}


def _fields(problems: list[dict]) -> set:
    return {p["field"] for p in problems}


def test_every_missing_top_level_field_in_one_call() -> None:
    found = ir.problems({"schema": ir.SCHEMA, "kind": ir.KIND})
    assert {"name", *_SECTIONS} <= _fields(found)
    assert all(p["code"] == ir.E_SHAPE and p["message"].endswith("missing field") for p in found)


def test_every_section_reports_all_its_missing_fields() -> None:
    fields = _fields(ir.problems(_empty_sections()))
    want = {
        "universe.pairs", "universe.shape", "universe.excluded_bases",
        "assumptions.horizon_hours", "assumptions.basis_stress", "assumptions.funding_estimate",
        "entry.conditions", "entry.rank_by", "entry.candidate_limit", "entry.cooldown_minutes", "entry.one_per_base",
        "exit.max_hold_hours", "exit.max_loss", "exit.take_profit", "exit.conditions",
        "sizing.notional_per_leg", "sizing.max_open", "sizing.leverage",
        "provenance.tool", "provenance.window",
    }
    assert want <= fields, sorted(want - fields)


def test_an_error_in_one_section_does_not_hide_another() -> None:
    doc = json.loads(_EXAMPLE.read_text())
    doc["universe"]["pairs"] = "binance-okx"
    doc["exit"]["max_loss_pct"] = "0.01"
    doc["sizing"]["max_open"] = 0  # semantic, in a section whose shape passed: still reported
    fields = _fields(ir.problems(doc))
    assert {"universe.pairs", "exit.max_loss_pct", "sizing.max_open"} <= fields
    # first-error path unchanged: the first problem only
    assert ir.validate(doc) == ["ir.shape: universe.pairs: must be \"any\" or a list of [long_venue, short_venue]"]


def test_the_example_is_valid_both_ways() -> None:
    doc = json.loads(_EXAMPLE.read_text())
    assert ir.problems(doc) == []
    assert ir.validate(doc) == []
    assert ir.unit_warnings(doc) == []


def test_first_problem_is_the_first_error_path_result() -> None:
    """problems() runs the same checks in the same order, so its first entry is what validate()/load() report."""
    from tests.test_alphakeel_ir import BAD, _base_doc, _cases

    for name, mutate, code in BAD:
        doc = mutate(_base_doc())
        first = ir.problems(doc)[0]
        assert ir.validate(doc) == [f"{first['code']}: {first['message']}"] and first["code"] == code, name
    for case in _cases("canonical.json"):
        assert ir.problems(case["doc"]) == [], case["name"]


def _forbid_cli(monkeypatch) -> None:
    from alphakeel_research import cli

    def called(argv):
        raise AssertionError(f"AlphaKeel was called: {argv}")

    monkeypatch.setattr(cli, "main", called)


def test_tool_returns_all_local_problems_without_calling_alphakeel(monkeypatch, tmp_path) -> None:
    from src.tools.alphakeel_research_tool import AlphakeelResearchTool

    _forbid_cli(monkeypatch)
    path = tmp_path / "strategy.json"
    path.write_text(json.dumps(_empty_sections()))
    out = json.loads(AlphakeelResearchTool().execute(action="ir_validate", args={"ir": str(path)}))
    assert out["status"] == "error"
    r = out["result"]
    assert r["valid"] is False and r["source"] == "local"
    assert r["problems"] == ir.problems(_empty_sections()) and len(r["problems"]) >= 20
    assert "All problems are listed at once" in r["hint"]

    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    out = json.loads(AlphakeelResearchTool().execute(action="ir_validate", args={"ir": str(bad)}))
    assert out["status"] == "error" and "not JSON" in out["error"]


def _with_loss(max_loss: str, notional: str) -> dict:
    doc = json.loads(_EXAMPLE.read_text())
    doc["exit"]["max_loss"], doc["sizing"]["notional_per_leg"] = max_loss, notional
    return doc


def test_max_loss_fraction_warns_and_reaches_the_envelope(monkeypatch, tmp_path) -> None:
    (w,) = ir.unit_warnings(_with_loss("0.01", "10000"))
    assert "max_loss 0.01 on notional_per_leg 10000 closes at a loss of 0.01 USDT (0.0001% of one leg)" in w
    assert ir.unit_warnings(_with_loss("100", "10000")) == []

    from alphakeel_research import cli
    from src.tools.alphakeel_research_tool import AlphakeelResearchTool

    seen = []

    def valid(argv):
        seen.append(argv)
        print(json.dumps({"valid": True, "problems": [], "strategy_id": "ir-000000000000000000000000"}))
        return 0

    monkeypatch.setattr(cli, "main", valid)
    path = tmp_path / "strategy.json"
    path.write_text(json.dumps(_with_loss("0.01", "10000")))
    out = json.loads(AlphakeelResearchTool().execute(action="ir_validate", args={"ir": str(path)}))
    assert seen and out["status"] == "ok" and out["result"]["valid"] is True
    assert out["warnings"] == [w]

    path.write_text(json.dumps(_with_loss("100", "10000")))
    assert "warnings" not in json.loads(AlphakeelResearchTool().execute(action="ir_validate", args={"ir": str(path)}))


def test_export_keeps_the_input_provenance_fixed_literals(tmp_path) -> None:
    """``ir export`` rebuilt provenance from scratch and dropped ``fixed_literals``, which AlphaKeel's G1 1.19 and
    precheck.py read from the exported strategy.json."""
    from alphakeel_research import workflow
    from tests.test_alphakeel_ir_backtest import _run, golden_ir

    run = _run(tmp_path, "--trials", "1")
    doc = golden_ir()
    doc["provenance"] = {**doc["provenance"], "fixed_literals": {"entry.candidate_limit": "3"}}
    workflow.ir_export(doc, run, tmp_path / "export")
    written = json.loads((tmp_path / "export" / "strategy.json").read_text())
    assert written["provenance"]["fixed_literals"] == {"entry.candidate_limit": "3"}
    assert written["provenance"]["tool"] == "vibe-trading"
