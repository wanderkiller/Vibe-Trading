"""IR backtests report what the code measured (research-truth, session b9aa13a496a4 and replays 3d53fa72a8e9 / a061ea4c35f2).

The model writes the IR and the explanation; the request's defaults (B2), the trial count (B4), the label (B3) and the
figures (B1, observed under the run's ``evidence_dir``) come from code, and a result card built from the trial ledger is
appended after the answer (§5). Every value here is synthetic.
"""

from __future__ import annotations

import copy
import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from tests.fixtures.alphakeel_tool_cli import patch_cli

from alphakeel_research import ir_backtest, precheck
from alphakeel_research.screen import screen

pytestmark = pytest.mark.unit

DAY_MS = 86_400_000
CUTOFF_MS = 1_782_000_000_000 + 12_345  # not UTC day aligned
_EXAMPLE = Path(ir_backtest.__file__).resolve().parent / "examples" / "ir" / "strategy.json"


def _ir_doc(**universe) -> dict:
    doc = json.loads(_EXAMPLE.read_text())
    doc["universe"].update({"pairs": [["binance", "okx"], ["bybit", "okx"]], "bases": ["BTC"], **universe})
    return doc


def _who(max_rows: int = 3_000_000) -> dict:
    return {"holdout_cutoff_ms": CUTOFF_MS, "datasets": {"max_rows": max_rows}}


# --- B2: the request ---------------------------------------------------------------------------------------------------

def test_only_ir_is_needed_and_everything_else_is_derived() -> None:
    from src.tools import alphakeel_ir_request as req

    p = req.plan({"ir": "strategy.json"}, _ir_doc(), _who())
    end = CUTOFF_MS - CUTOFF_MS % DAY_MS
    assert (p["start_ms"], p["end_ms"]) == (end - 180 * DAY_MS, end)
    assert p["instruments"] == [
        {"venue": "binance", "market": "perp", "symbol": "BTCUSDT", "base": "BTC", "quote": "USDT"},
        {"venue": "okx", "market": "perp", "symbol": "BTC-USDT-SWAP", "base": "BTC", "quote": "USDT"},
        {"venue": "bybit", "market": "perp", "symbol": "BTCUSDT", "base": "BTC", "quote": "USDT"},
    ]
    assert p["step_minutes"] == 15  # 180 d: 10-minute steps are 25,920 instants > 25,000
    assert p["fees"] == req.DEFAULT_PUBLIC_TAKER and p["fees_source"] == req.DEFAULT_FEES_SOURCE
    assert p["ignored_args"] == []
    given = req.plan({"ir": "s.json", "fees": {v: {"perp": "0.0002"} for v in ("binance", "okx", "bybit")},
                      "start_ms": end - 30 * DAY_MS}, _ir_doc(), _who())
    assert given["fees_source"] == "request" and given["start_ms"] == end - 30 * DAY_MS and given["step_minutes"] == 2


def test_the_row_estimate_refuses_before_freezing_and_names_the_longest_window() -> None:
    from src.tools import alphakeel_ir_request as req

    # 3 instruments x (259,200 + 1,440 + 4,320) = 794,880 rows
    with pytest.raises(ValueError, match=r"the pack would hold ~794880 rows \(limit 500000\): with 3 instruments the window "
                                         r"can be at most 112 days"):
        req.plan({"ir": "s.json"}, _ir_doc(), _who(max_rows=500_000))


def test_model_supplied_derived_args_are_ignored_not_refused() -> None:
    from src.tools import alphakeel_ir_request as req

    p = req.plan({"ir": "s.json", "instruments": [{"venue": "okx"}], "trials": 9, "step_minutes": 1}, _ir_doc(), _who())
    assert p["ignored_args"] == ["instruments", "step_minutes", "trials"]
    assert p["step_minutes"] == 15 and len(p["instruments"]) == 3


def test_request_errors() -> None:
    from src.tools import alphakeel_ir_request as req

    with pytest.raises(ValueError, match="ir_backtest needs universe.bases as an explicit list"):
        req.plan({"ir": "s.json"}, _ir_doc(bases=None), _who())
    with pytest.raises(ValueError, match="no 1m kline history for hyperliquid: the dataset backtest covers binance, okx, bybit"):
        req.plan({"ir": "s.json"}, _ir_doc(pairs=[["binance", "hyperliquid"]]), _who())
    for universe in ({"pairs": "any"}, {"shape": "spot_perp"}):
        with pytest.raises(ValueError, match=r"supports only an explicit universe.pairs list \(not \"any\"\) with shape cross_perp"):
            req.plan({"ir": "s.json"}, _ir_doc(**universe), _who())
    with pytest.raises(ValueError) as e:
        req.plan({"start_ms": "x", "verbose": "maybe"}, _ir_doc(), _who())
    assert "ir:" in str(e.value) and "start_ms:" in str(e.value) and "verbose:" in str(e.value)  # all at once


# --- B3: the label -----------------------------------------------------------------------------------------------------

def _screen_card(net: str, closed: int, days: int, partial: bool = False) -> dict:
    return {"metrics": {"days": days}, "counts": {"closed": closed}, "amounts": {"equity_end": net},
            "data_audit": {"coverage_partial": partial}}


def test_screen_labels_from_the_protocol_thresholds() -> None:
    lost = screen(_screen_card("-138.19", 15, 180))
    assert lost["evidence_status"] == "insufficient"
    assert [i["id"] for i in lost["items"] if i["result"] == "FAIL"] == ["1.2", "1.3"]
    assert lost["not_evaluated"] == [f"1.{i}" for i in range(4, 14)] and lost["next"] == "ir_robustness"
    assert screen(_screen_card("50", 40, 120))["evidence_status"] == "passed_screen"
    assert screen(_screen_card("-10", 40, 120))["evidence_status"] == "failed_screen"
    assert screen(_screen_card("50", 40, 120, partial=True))["evidence_status"] == "failed_screen"
    assert screen(_screen_card("50", 0, 120))["evidence_status"] == "insufficient"
    # a missing field is a FAIL, never a pass by omission
    assert screen({"metrics": {"days": 120}, "counts": {"closed": 40}})["evidence_status"] == "failed_screen"

    protocol = copy.deepcopy(precheck.load_protocol())
    for item in protocol["stages"]["validated"]["items"]:
        if item["id"] == "1.2":
            item["threshold"]["min_trades"] = "10"
    assert screen(_screen_card("-138.19", 15, 180), protocol)["evidence_status"] == "failed_screen"


# --- B1 + B3 + B4 through the tool, then grounding ---------------------------------------------------------------------

def _card(sid: str, name: str, net: str, closed: int, days: int = 180, fees_source: str = "") -> dict:
    from src.tools.alphakeel_ir_request import DEFAULT_FEES_SOURCE, DEFAULT_PUBLIC_TAKER

    card = {
        "strategy_id": sid, "name": name, "run_id": f"irbt-{name}",
        "window": {"start_ms": 0, "end_ms": days * DAY_MS, "start_day": "2026-01-01", "end_day": "2026-06-29"},
        "counts": {"closed": closed},
        "amounts": {"equity_end": net, "fees": "300.43", "funding": "190.11", "price_pnl_closed": "-27.87",
                    "realized": net, "ccy": "USDT"},
        "max_drawdown": {"amount": "54.2", "fraction_of_capital": "0.00271"},
        "assumptions": {"fees": DEFAULT_PUBLIC_TAKER, "fees_source": fees_source or DEFAULT_FEES_SOURCE, "capital_base": "20000"},
        "metrics": {"days": days}, "alphakeel_metrics": {"sharpe_annualised": -1.25},
        "data_audit": {"coverage_partial": False}, "trials": None,
    }
    card["screen"] = screen(card)
    return card


def _write_run(runs: Path, dirname: str, card: dict) -> Path:
    rd = runs / dirname
    (rd / "artifacts").mkdir(parents=True)
    (rd / "run_card.json").write_text(json.dumps(card))
    row = ir_backtest.metrics_row(rd, card)
    (rd / "artifacts" / "metrics.csv").write_text(",".join(ir_backtest.METRICS_COLUMNS) + "\n"
                                                  + ",".join(row[c] for c in ir_backtest.METRICS_COLUMNS) + "\n")
    (rd / "ir.json").write_text(json.dumps({**_ir_doc(), "strategy_id": card["strategy_id"]}))
    return rd


@pytest.fixture()
def session(tmp_path, monkeypatch):
    """A session run dir, a temp hypothesis registry, the service check stubbed, and a fake ``ir backtest`` CLI that
    prints the result of the next queued run card."""
    from alphakeel_research import cli, ir
    from src.hypotheses.registry import HypothesisRegistry
    from src.tools import alphakeel_ir_request, alphakeel_research_ledger

    monkeypatch.setattr(alphakeel_ir_request, "service_check", lambda: _who())
    monkeypatch.setattr(alphakeel_research_ledger, "_registry", lambda: HypothesisRegistry(tmp_path / "hypotheses.json"))
    monkeypatch.setattr(alphakeel_research_ledger, "_sessions_dir", lambda: tmp_path / "sessions")
    state = {"queue": [], "argv": []}

    def fake_main(argv):
        state["argv"].append(argv)
        rd = state["queue"].pop(0)
        card = json.loads((rd / "run_card.json").read_text())
        print(json.dumps({"evidence_status": card["screen"]["evidence_status"], "screen": card["screen"], "run_id": card["run_id"],
                          "strategy_id": card["strategy_id"], "window": card["window"], "counts": card["counts"],
                          "amounts": card["amounts"], "max_drawdown": card["max_drawdown"], "assumptions": card["assumptions"],
                          "run_dir": str(rd), "run_card": str(rd / "run_card.json")}))
        return 0

    patch_cli(monkeypatch, fake_main)
    sess = tmp_path / "session"
    sess.mkdir()
    (sess / "strategy.json").write_text(json.dumps(_ir_doc()))
    state.update(session=sess, runs=tmp_path / "runs", sid=ir.load(_ir_doc()).strategy_id, tmp=tmp_path)
    return state


SESSION_ID = "sess-1"


def _run_tool(state, dirname: str, card: dict, *, run_dir: Path | None = None, session_id: str | None = SESSION_ID,
              **args) -> dict:
    """One ``ir_backtest`` tool call of conversation ``session_id`` in the turn run directory ``run_dir``."""
    from src.tools.alphakeel_research_tool import AlphakeelResearchTool

    rd = run_dir or state["session"]
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "strategy.json").write_text(json.dumps(_ir_doc()))
    state["queue"].append(_write_run(state["runs"], dirname, card))
    return json.loads(AlphakeelResearchTool(default_session_id=session_id).execute(
        action="ir_backtest", args={"ir": "strategy.json", **args}, run_dir=str(rd)))


def test_ir_backtest_envelope_carries_label_evidence_dir_and_ledger_trials(session) -> None:
    out = _run_tool(session, "20261014_090950_ir", _card(session["sid"], "carry-high-hurdle", "-138.19", 15),
                    instruments=[{"venue": "okx"}], trials=4)
    assert out["status"] == "ok", out
    assert list(out["result"])[:2] == ["evidence_status", "summary"]
    assert out["result"]["evidence_status"] == "insufficient"
    assert out["result"]["summary"] == ("insufficient: 1.2 15 closed trades < 30; 1.3 net -138.19 USDT <= 0; 15 closed trades; "
                                        "window 180 d; fees public base-tier taker (tool default, user decision 2026-10-14)")
    assert out["evidence_dir"] == "ir_runs/20261014_090950_ir"
    assert out["ignored_args"] == ["instruments", "trials"] and out["ignored_args_note"] == "computed from the IR and the trial ledger"
    ev = session["session"] / "ir_runs" / "20261014_090950_ir"
    assert (ev / "run_card.json").is_file() and (ev / "artifacts" / "metrics.csv").is_file()
    argv = session["argv"][0]
    assert argv[argv.index("--trials") + 1] == "1" and argv[argv.index("--step-minutes") + 1] == "15"
    assert argv[argv.index("--fees-source") + 1].startswith("public base-tier taker")
    lines = (session["session"] / "ir_trials_evidence.jsonl").read_text().splitlines()
    assert [json.loads(x)["strategy_id"] for x in lines] == [session["sid"]]
    assert json.loads(lines[0])["parameters"] == {"min_net": "0.001", "max_hold_hours": "1"}

    # a second version counts 2; re-running the first does not add a third
    _run_tool(session, "20261014_091500_ir", _card("ir-second", "v2", "12.5", 40))
    _run_tool(session, "20261014_092000_ir", _card(session["sid"], "carry-high-hurdle", "-1", 15))
    assert [a[a.index("--trials") + 1] for a in session["argv"]] == ["1", "1", "2"]
    assert "--trials-evidence" in session["argv"][2]


def test_ir_backtest_figures_are_observed_under_evidence_dir(session) -> None:
    from src.agent.grounding import GroundingLedger

    raw = _run_tool(session, "20261014_090950_ir", _card(session["sid"], "carry-high-hurdle", "-138.19", 15))
    ledger = GroundingLedger(run_dir=session["session"], user_message="回测这个跨所资金费率策略")
    ledger.ingest_tool_result(tool_name="alphakeel_research", arguments={"action": "ir_backtest", "args": {"ir": "strategy.json"}},
                              result=json.dumps(raw), call_id="bt-1", success=True)

    def declared(value: str):
        return ledger.validate_final_answer(f"净收益 {value}。\n\n```figures\n{value} | observed | 净收益 | "
                                            f"ir_runs/20261014_090950_ir\n```")

    r = declared("-138.19")
    assert r.valid, json.dumps(r.issues, ensure_ascii=False, indent=1)
    assert declared("300.43").valid  # fees_usdt
    assert not declared("-200.00").valid


def test_usdt_marked_ir_backtest_figures_are_observed_under_evidence_dir(session) -> None:
    """Spec §7.3 wording: "净收益 -138.19 USDT" (a currency-marked figure). Grounding answers a currency-marked figure only
    from money fields (``_is_price_kind``): the IR backtest's ``*_usdt`` columns are in ``_AMOUNT_FIELDS``."""
    from src.agent.grounding import GroundingLedger

    raw = _run_tool(session, "20261014_090950_ir", _card(session["sid"], "carry-high-hurdle", "-138.19", 15))
    ledger = GroundingLedger(run_dir=session["session"], user_message="回测这个跨所资金费率策略")
    ledger.ingest_tool_result(tool_name="alphakeel_research", arguments={"action": "ir_backtest", "args": {"ir": "strategy.json"}},
                              result=json.dumps(raw), call_id="bt-1", success=True)
    r = ledger.validate_final_answer("净收益 -138.19 USDT。\n\n```figures\n-138.19 | observed | 净收益 | ir_runs/20261014_090950_ir\n```")
    assert r.valid, json.dumps(r.issues, ensure_ascii=False)
    rejected = ledger.validate_final_answer("净收益 -200.00 USDT。\n\n```figures\n-200.00 | observed | 净收益 | "
                                            "ir_runs/20261014_090950_ir\n```")
    assert not rejected.valid
    fees = ledger.validate_final_answer("手续费 300.43 USDT。\n\n```figures\n300.43 | observed | 手续费 | ir_runs/20261014_090950_ir\n```")
    assert fees.valid, json.dumps(fees.issues, ensure_ascii=False)


def test_an_ir_backtest_result_without_evidence_dir_is_no_backtest_evidence(tmp_path) -> None:
    """Regression (today's behaviour): an envelope without evidence_dir records nothing, whatever lies on disk."""
    from src.agent.grounding import GroundingLedger

    card = _card("ir-x", "x", "-138.19", 15)
    _write_run(tmp_path / "ir_runs", "x", card)
    ledger = GroundingLedger(run_dir=tmp_path, user_message="回测")
    ledger.ingest_tool_result(tool_name="alphakeel_research", arguments={"action": "ir_backtest", "args": {}},
                              result=json.dumps({"status": "ok", "result": {"run_dir": str(tmp_path / "ir_runs" / "x")}}),
                              call_id="bt-1", success=True)
    r = ledger.validate_final_answer("净收益 -138.19 USDT。\n\n```figures\n-138.19 | observed | 净收益 | ir_runs/x\n```")
    assert not r.valid


# --- §5: the result card -----------------------------------------------------------------------------------------------

def test_result_card_rows_order_and_language(session) -> None:
    from src.tools.alphakeel_research_ledger import render_card

    assert render_card(SESSION_ID, True) is None  # nothing recorded yet
    s8 = session["sid"][:8]
    _run_tool(session, "20261014_090950_ir", _card(session["sid"], "carry-high-hurdle", "-138.19", 15))
    _run_tool(session, "20261014_091500_ir", _card("ir-bbbbbbbbbb", "carry-wide", "50", 40, days=120))
    _run_tool(session, "20261014_092000_ir", _card("ir-cccccccccc", "carry-tight", "-10", 40, days=120))
    _run_tool(session, "20261014_092500_ir", _card(session["sid"], "carry-high-hurdle", "-1", 15))  # a re-run: 3 versions, 4 backtests
    zh = render_card(SESSION_ID, True)
    lines = zh.splitlines()
    assert lines[0] == "---" and lines[1] == "**研究结果（系统根据试验账本生成，非模型撰写）**"
    assert lines[2] == "费率口径：公开基础档 taker（工具默认） — binance 0.05% / bybit 0.055% / okx 0.05% 每笔"
    assert lines[3] == "本会话 3 个版本 / 4 次回测：通过初筛 1，未通过 1，证据不足 2"
    rows = [ln for ln in lines if ln.startswith("| carry")]
    assert rows == [
        "| carry-wide (ir-bbbbb) | 2026-01-01→06-29 | 40 | 50.00 | 300.43 | 190.11 | 0.27% | 通过初筛，1.4–1.13 未评估 |",
        f"| carry-high-hurdle ({s8}) | 2026-01-01→06-29 | 15 | −1.00 | 300.43 | 190.11 | 0.27% | 证据不足（1.2） |",
        "| carry-tight (ir-ccccc) | 2026-01-01→06-29 | 40 | −10.00 | 300.43 | 190.11 | 0.27% | 未通过初筛（1.3） |",
        f"| carry-high-hurdle ({s8}) | 2026-01-01→06-29 | 15 | −138.19 | 300.43 | 190.11 | 0.27% | 证据不足（1.2） |",
    ]
    assert "1.4–1.13（成本压力、DSR、PBO、邻域、走步等）未评估：需要 ir_robustness。" in lines
    assert lines[-1] == "账本：ir_trials_evidence.jsonl"  # no hypothesis id handed to the model
    assert "hyp_" not in zh
    en = render_card(SESSION_ID, False)
    assert "This session: 3 versions / 4 backtests: passed screen 1, failed screen 1, insufficient 2" in en
    assert "| carry-tight (ir-ccccc) | 2026-01-01→06-29 | 40 | −10.00 | 300.43 | 190.11 | 0.27% | failed screen (1.3) |" in en
    assert en.splitlines()[-1] == "Ledger: ir_trials_evidence.jsonl"


# --- review round 1 ------------------------------------------------------------------------------------------------------

def test_p1_1_one_conversation_two_turns_counts_both_versions(session) -> None:
    """Every turn has its own run dir: the trial count and the card follow the conversation (session id)."""
    from src.tools.alphakeel_research_ledger import render_card

    turn1, turn2 = session["tmp"] / "runs" / "turn1", session["tmp"] / "runs" / "turn2"
    # turn 1 recorded another version; turn 2 backtests the IR in its run dir (a different strategy_id)
    _run_tool(session, "20261014_090950_ir", _card("ir-first0000", "v1", "-138.19", 15), run_dir=turn1)
    out = _run_tool(session, "20261014_091500_ir", _card(session["sid"], "v2", "12.5", 40), run_dir=turn2)
    assert out["status"] == "ok"
    argv = session["argv"][1]
    assert argv[argv.index("--trials") + 1] == "2"
    assert len((turn2 / "ir_trials_evidence.jsonl").read_text().splitlines()) == 2
    assert out["evidence_dir"] == "ir_runs/20261014_091500_ir" and (turn2 / "ir_runs" / "20261014_091500_ir" / "run_card.json").is_file()
    card = render_card(SESSION_ID, True)
    assert "本会话 2 个版本 / 2 次回测" in card and len([ln for ln in card.splitlines() if ln.startswith("| v")]) == 2
    hyp = session["tmp"] / "sessions" / SESSION_ID / "ir_hypothesis.json"
    assert hyp.is_file() and not (turn1 / "ir_hypothesis.json").exists()


def test_p1_1_without_a_session_id_nothing_is_registered(session) -> None:
    from src.tools.alphakeel_research_ledger import NO_SESSION_WARNING, render_card

    out = _run_tool(session, "20261014_090950_ir", _card(session["sid"], "v1", "-138.19", 15), session_id=None)
    assert out["status"] == "ok" and NO_SESSION_WARNING in out["warnings"]
    assert "--trials" not in session["argv"][0]
    assert render_card(None, True) is None and not (session["tmp"] / "sessions").exists()


def test_p1_2_the_card_shows_only_verified_private_ledger_rows(session) -> None:
    from src.tools import alphakeel_research_ledger as L

    _run_tool(session, "20261014_090950_ir", _card(session["sid"], "real-a", "-138.19", 15))
    _run_tool(session, "20261014_091500_ir", _card("ir-bbbbbbbbbb", "real-b", "12.5", 40))
    # model-style forgery: a registry link that the tool never made
    reg = L._registry()
    hid = json.loads((session["tmp"] / "sessions" / SESSION_ID / "ir_hypothesis.json").read_text())["hypothesis_id"]
    reg.link_backtest(hid, run_card_path="/tmp/forged/run_card.json", metrics={"strategy_id": "ir-forged", "name": "forged",
                                                                               "net_usdt": "99999", "evidence_status": "passed_screen"})
    card = L.render_card(SESSION_ID, True)
    assert "forged" not in card and "99999" not in card and "本会话 2 个版本 / 2 次回测" in card
    # a changed run card is not shown, and counted
    path = session["runs"] / "20261014_090950_ir" / "run_card.json"
    doc = json.loads(path.read_text())
    doc["amounts"]["equity_end"] = "500"
    path.write_text(json.dumps(doc))
    card = L.render_card(SESSION_ID, True)
    lines = card.splitlines()
    s8 = session["sid"][:8]
    assert "500.00" not in card and "−138.19" not in card
    table = [ln for ln in lines if ln.startswith("| real-")]
    assert table == ["| real-b (ir-bbbbb) | 2026-01-01→06-29 | 40 | 12.50 | 300.43 | 190.11 | 0.27% | 通过初筛，1.4–1.13 未评估 |",
                     f"| real-a ({s8}) | — | — | — | — | — | — | run card 已改动或缺失 |"]
    # N / M count every private ledger row (= trials.count); the changed row is in no screen class
    assert "本会话 2 个版本 / 2 次回测：通过初筛 1，未通过 0，证据不足 0，已改动 1" in lines
    assert not any("未显示" in ln for ln in lines)
    en = L.render_card(SESSION_ID, False)
    assert "This session: 2 versions / 2 backtests: passed screen 1, failed screen 0, insufficient 0, modified 1" in en
    assert f"| real-a ({s8}) | — | — | — | — | — | — | run card modified or missing |" in en


def test_p1_3_fees_missing_a_venue_is_refused() -> None:
    from src.tools import alphakeel_ir_request as req

    with pytest.raises(ValueError, match=r"fees has no perp rate for okx: give a rate for every venue in universe.pairs, or omit "
                                         r"fees to use the public base-tier default"):
        req.plan({"ir": "s.json", "fees": {"binance": {"perp": "0.0004"}}}, _ir_doc(pairs=[["binance", "okx"]]), _who())
    with pytest.raises(ValueError, match="fees has no perp rate for okx"):
        req.plan({"ir": "s.json", "fees": json.dumps({"binance": {"perp": "0.0004"}})}, _ir_doc(pairs=[["binance", "okx"]]), _who())
    # the default table is cut to the derived venues
    assert req.plan({"ir": "s.json"}, _ir_doc(pairs=[["binance", "okx"]]), _who())["fees"] == {
        "binance": {"perp": "0.0005"}, "okx": {"perp": "0.0005"}}


def test_p2_1_evidence_dirs_never_overwrite_and_out_is_checked(session) -> None:
    first = _run_tool(session, "same_ir", _card(session["sid"], "v1", "-1", 15))
    session["runs"] = session["tmp"] / "runs2"
    second = _run_tool(session, "same_ir", _card(session["sid"], "v1", "-2", 15))
    assert (first["evidence_dir"], second["evidence_dir"]) == ("ir_runs/same_ir", "ir_runs/same_ir-2")
    assert json.loads((session["session"] / "ir_runs" / "same_ir" / "run_card.json").read_text())["amounts"]["equity_end"] == "-1"
    for bad in ("ir_runs/x", "out/..", "out/.", "out/"):
        before = list(session["argv"])
        out = _run_tool(session, f"never-{bad.replace('/', '_')}", _card(session["sid"], "v1", "-1", 15), out=bad)
        session["queue"].clear()
        assert out == {"status": "error", "error": "out must not be inside ir_runs/ or end in . or .."}, out
        assert session["argv"] == before


def test_p2_2_zero_net_sorts_as_zero(session) -> None:
    from src.tools.alphakeel_research_ledger import render_card

    _run_tool(session, "a_ir", _card("ir-aaaaaaaaaa", "neg", "-5", 40))
    _run_tool(session, "b_ir", _card("ir-bbbbbbbbbb", "zero", "0", 40))
    _run_tool(session, "c_ir", _card("ir-cccccccccc", "pos", "3", 40))
    names = [ln.split(" ")[1] for ln in render_card(SESSION_ID, False).splitlines() if ln.startswith("| ") and "(ir-" in ln]
    assert names == ["pos", "zero", "neg"]


def test_p2_5_row_estimate_counts_hourly_settlements() -> None:
    from src.tools import alphakeel_ir_request as req

    assert req.row_estimate(3, 180 * 1440) == 3 * (259_200 + 1440 + 4320)
    with pytest.raises(ValueError, match=r"at most (\d+) days") as e:
        req.check_rows(3, 0, 180 * DAY_MS, 500_000)
    days = int(re.search(r"at most (\d+) days", str(e.value)).group(1))
    assert req.row_estimate(3, days * 1440) <= 500_000 < req.row_estimate(3, (days + 1) * 1440)


def test_routing_block_only_when_the_tool_is_registered() -> None:
    from src.tools.alphakeel_research_guard import ROUTING_BLOCK, routing_block

    class Reg:
        def __init__(self, names):
            self._tools = dict.fromkeys(names)

    assert routing_block(Reg(["alphakeel_research"])) == ROUTING_BLOCK
    assert routing_block(Reg(["backtest"])) == ""
    assert routing_block(object()) == ""


def test_metrics_csv_uses_upstream_columns_as_decimal_text(tmp_path) -> None:
    card = _card("ir-x", "x", "-138.19", 15)
    row = ir_backtest.metrics_row(tmp_path, card)
    assert row["final_value"] == "19861.81" and row["total_return"] == "-0.0069095"
    assert row["max_drawdown"] == "-0.00271" and row["trade_count"] == "15" and row["win_rate"] == "0"
    assert row["annual_return"] == ir_backtest._s(ir_backtest.q8(Decimal("-0.0069095") * 365 / 180))
    assert row["net_usdt"] == "-138.19" and row["fees_usdt"] == "300.43" and row["sharpe"] == "-1.25"


# --- review round 2 ------------------------------------------------------------------------------------------------------

def test_r2_out_holding_a_run_card_is_refused_before_anything_is_registered(session) -> None:
    from src.tools import alphakeel_research_ledger as L

    _run_tool(session, "20261014_090950_ir", _card(session["sid"], "v1", "-1", 15))
    rows, hyp = L.ledger_rows(SESSION_ID), (session["tmp"] / "sessions" / SESSION_ID / "ir_hypothesis.json").read_text()
    before = list(session["argv"])
    out = _run_tool(session, "unused_ir", _card(session["sid"], "v1", "-1", 15), out=str(session["runs"] / "20261014_090950_ir"))
    session["queue"].clear()
    assert out == {"status": "error", "error": "out already holds a run card: use a new out directory or omit out"}
    assert session["argv"] == before and L.ledger_rows(SESSION_ID) == rows
    assert (session["tmp"] / "sessions" / SESSION_ID / "ir_hypothesis.json").read_text() == hyp


def test_r2_untouched_header_counts_with_a_changed_card(session) -> None:
    from src.tools import alphakeel_research_ledger as L

    _run_tool(session, "a_ir", _card("ir-aaaaaaaaaa", "va", "-5", 40))
    _run_tool(session, "b_ir", _card("ir-bbbbbbbbbb", "vb", "3", 40))
    before = L.render_card(SESSION_ID, True).splitlines()[3]
    assert before == "本会话 2 个版本 / 2 次回测：通过初筛 1，未通过 1，证据不足 0"
    (session["runs"] / "b_ir" / "run_card.json").unlink()  # missing counts as changed
    lines = L.render_card(SESSION_ID, True).splitlines()
    assert lines[3] == "本会话 2 个版本 / 2 次回测：通过初筛 0，未通过 1，证据不足 0，已改动 1"
    table = [ln for ln in lines if ln.startswith("| v")]
    assert table[-1] == "| vb (ir-bbbbb) | — | — | — | — | — | — | run card 已改动或缺失 |"


def test_r2_build_registry_survives_a_failing_alphakeel_import(monkeypatch, caplog) -> None:
    import sys

    from src.tools import build_registry

    monkeypatch.setitem(sys.modules, "src.tools.alphakeel_research_tool", None)  # the import raises ImportError
    with caplog.at_level("WARNING"):
        registry = build_registry()
    assert registry is not None
    assert any("alphakeel_research is not session-injected" in r.getMessage() for r in caplog.records)


# --- replay fixes: one child process per call, fee roles, card columns ------------------------------------------------

def test_two_conversations_at_once_each_get_their_own_cli_output(session, monkeypatch) -> None:
    """Replay session 4cada46ec0b4: with contextlib.redirect_stdout (process-wide) a concurrent conversation's CLI output
    landed in the other's buffer, so one got ``{"output": ""}`` and both ledgers recorded the same run. Each call now runs
    its own child process and reads only that process's stdout."""
    import subprocess
    import threading

    from src.tools import alphakeel_research_ledger as L
    from src.tools.alphakeel_research_tool import AlphakeelResearchTool
    from tests.fixtures.alphakeel_tool_cli import patch_run

    runs = {}
    for who in ("A", "B"):
        rd = session["tmp"] / f"turn-{who}"
        rd.mkdir()
        (rd / "strategy.json").write_text(json.dumps(_ir_doc()))
        runs[who] = (rd, _write_run(session["tmp"] / f"runs-{who}", f"run_{who}_ir", _card(session["sid"], f"conv-{who}", "-1", 15)))
    barrier = threading.Barrier(2)

    def fake_run(cmd, **kwargs):
        ir_path = cmd[cmd.index("--ir") + 1]
        who = "A" if "turn-A" in ir_path else "B"
        barrier.wait(timeout=10)  # both children "run" at the same time
        rd = runs[who][1]
        card = json.loads((rd / "run_card.json").read_text())
        stdout = json.dumps({"evidence_status": card["screen"]["evidence_status"], "screen": card["screen"],
                             "strategy_id": card["strategy_id"], "counts": card["counts"], "assumptions": card["assumptions"],
                             "run_dir": str(rd), "run_card": str(rd / "run_card.json")})
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    patch_run(monkeypatch, fake_run)
    results = {}

    def call(who):
        tool = AlphakeelResearchTool(default_session_id=f"sess-{who}")
        results[who] = json.loads(tool.execute(action="ir_backtest", args={"ir": "strategy.json"}, run_dir=str(runs[who][0])))

    threads = [threading.Thread(target=call, args=(w,)) for w in ("A", "B")]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=30)
    for who in ("A", "B"):
        assert results[who]["status"] == "ok", results[who]
        assert results[who]["result"]["run_dir"] == str(runs[who][1])
        rows = L.ledger_rows(f"sess-{who}")
        assert [r["run_card_path"] for r in rows] == [str((runs[who][1] / "run_card.json").resolve())]


def test_fee_rate_is_count_and_fee_amount_is_observed(session) -> None:
    from src.agent.grounding import GroundingLedger

    card = _card(session["sid"], "carry", "-138.19", 15)
    card["amounts"]["fees"] = "930.61"
    raw = _run_tool(session, "20261014_090950_ir", card)
    ledger = GroundingLedger(run_dir=session["session"], user_message="回测这个跨所资金费率策略")
    ledger.ingest_tool_result(tool_name="alphakeel_research", arguments={"action": "ir_backtest", "args": {"ir": "strategy.json"}},
                              result=json.dumps(raw), call_id="bt-1", success=True)
    r = ledger.validate_final_answer(
        "费率口径：公开基础档 taker，费率 0.0005；手续费 930.61 USDT。\n\n```figures\n0.0005 | count | 费率（工具默认）\n"
        "930.61 | observed | 手续费 | ir_runs/20261014_090950_ir\n```")
    assert r.valid, json.dumps(r.issues, ensure_ascii=False)


def test_card_has_fee_and_funding_columns(session) -> None:
    from src.tools.alphakeel_research_ledger import render_card

    _run_tool(session, "a_ir", _card("ir-aaaaaaaaaa", "va", "-5", 40))
    zh, en = render_card(SESSION_ID, True).splitlines(), render_card(SESSION_ID, False).splitlines()
    assert "| 版本 | 窗口 | 成交 | 净收益 USDT | 手续费 USDT | 资金费 USDT | 最大回撤 | 判定 |" in zh
    assert "| Version | Window | Trades | Net USDT | Fees USDT | Funding USDT | Max drawdown | Status |" in en
    assert "| va (ir-aaaaa) | 2026-01-01→06-29 | 40 | −5.00 | 300.43 | 190.11 | 0.27% | 未通过初筛（1.3） |" in zh


def test_the_child_never_imports_modules_from_the_run_dir(tmp_path, monkeypatch) -> None:
    """The child holds the service credential: a ``json.py`` the model wrote into its run directory must not be imported.
    Real child process (``check`` against a closed local port ends without network)."""
    import os
    import subprocess

    from src.tools.alphakeel_research_tool import AlphakeelResearchTool
    from tests.fixtures.alphakeel_tool_cli import patch_run

    agent = str(Path(__file__).resolve().parents[1])
    monkeypatch.setenv("PYTHONPATH", agent + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""))
    monkeypatch.setenv("ALPHAKEEL_RESEARCH_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("ALPHAKEEL_RESEARCH_TOKEN", "ak_rs_test-token")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "json.py").write_text("raise SystemExit(99)\n")
    seen = {}
    real = subprocess.run

    def spy(cmd, **kwargs):
        seen.update(cmd=cmd, cwd=kwargs.get("cwd"))
        return real(cmd, **kwargs)

    patch_run(monkeypatch, spy)
    out = json.loads(AlphakeelResearchTool().execute(action="check", run_dir=str(run_dir)))
    assert seen["cwd"] is None and seen["cmd"][1:4] == ["-P", "-m", "alphakeel_research"]
    assert out["status"] == "error" and isinstance(out.get("result"), dict) and "error" in out["result"], out
