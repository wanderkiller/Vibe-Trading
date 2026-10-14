"""IR backtests report what the code measured (research-truth, session b9aa13a496a4 and replays 3d53fa72a8e9 / a061ea4c35f2).

The model writes the IR and the explanation; the request's defaults (B2), the trial count (B4), the label (B3) and the
figures (B1, observed under the run's ``evidence_dir``) come from code, and a result card built from the trial ledger is
appended after the answer (§5). Every value here is synthetic.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

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
    given = req.plan({"ir": "s.json", "fees": {"okx": {"perp": "0.0002"}}, "start_ms": end - 30 * DAY_MS}, _ir_doc(), _who())
    assert given["fees_source"] == "request" and given["start_ms"] == end - 30 * DAY_MS and given["step_minutes"] == 2


def test_the_row_estimate_refuses_before_freezing_and_names_the_longest_window() -> None:
    from src.tools import alphakeel_ir_request as req

    # 3 instruments x (259,200 + 1,440) minutes = 781,920 rows
    with pytest.raises(ValueError, match=r"the pack would hold ~781920 rows \(limit 500000\): with 3 instruments the window "
                                         r"can be at most 114 days"):
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

    monkeypatch.setattr(cli, "main", fake_main)
    sess = tmp_path / "session"
    sess.mkdir()
    (sess / "strategy.json").write_text(json.dumps(_ir_doc()))
    state.update(session=sess, runs=tmp_path / "runs", sid=ir.load(_ir_doc()).strategy_id)
    return state


def _run_tool(state, dirname: str, card: dict, **args) -> dict:
    from src.tools.alphakeel_research_tool import AlphakeelResearchTool

    state["queue"].append(_write_run(state["runs"], dirname, card))
    return json.loads(AlphakeelResearchTool().execute(action="ir_backtest", args={"ir": "strategy.json", **args},
                                                      run_dir=str(state["session"])))


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

    assert render_card(session["session"], True) is None  # nothing linked yet
    _run_tool(session, "20261014_090950_ir", _card(session["sid"], "carry-high-hurdle", "-138.19", 15))
    _run_tool(session, "20261014_091500_ir", _card("ir-b", "carry-wide", "50", 40, days=120))
    _run_tool(session, "20261014_092000_ir", _card("ir-c", "carry-tight", "-10", 40, days=120))
    _run_tool(session, "20261014_092500_ir", _card(session["sid"], "carry-high-hurdle", "-1", 15))  # a re-run: 3 versions, 4 backtests
    zh = render_card(session["session"], True)
    lines = zh.splitlines()
    assert lines[0] == "---" and lines[1] == "**研究结果（系统根据试验账本生成，非模型撰写）**"
    assert lines[2] == "费率口径：公开基础档 taker（工具默认） — binance 0.05% / bybit 0.055% / okx 0.05% 每笔"
    assert lines[3] == "本会话 3 个版本 / 4 次回测：通过初筛 1，未通过 1，证据不足 2"
    rows = [ln for ln in lines if ln.startswith("| carry")]
    assert rows == [
        "| carry-wide | 2026-01-01→06-29 | 40 | 50.00 | 0.27% | 通过初筛，1.4–1.13 未评估 |",
        "| carry-high-hurdle | 2026-01-01→06-29 | 15 | −1.00 | 0.27% | 证据不足（1.2） |",
        "| carry-tight | 2026-01-01→06-29 | 40 | −10.00 | 0.27% | 未通过初筛（1.3） |",
        "| carry-high-hurdle | 2026-01-01→06-29 | 15 | −138.19 | 0.27% | 证据不足（1.2） |",
    ]
    assert "1.4–1.13（成本压力、DSR、PBO、邻域、走步等）未评估：需要 ir_robustness。" in lines
    hid = json.loads((session["session"] / "ir_hypothesis.json").read_text())["hypothesis_id"]
    assert lines[-1] == f"账本：ir_trials_evidence.jsonl；假设 {hid}"
    en = render_card(session["session"], False)
    assert "This session: 3 versions / 4 backtests: passed screen 1, failed screen 1, insufficient 2" in en
    assert "| carry-tight | 2026-01-01→06-29 | 40 | −10.00 | 0.27% | failed screen (1.3) |" in en


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
