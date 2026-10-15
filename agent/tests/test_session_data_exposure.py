"""Data read by one tool call counts in the conversation's research horizon at ``ir_export`` (AlphaKeel protocol 2.5,
G1 1.14; VT AGENTS.md rule 7).

Every tool call that reads data runs in a process of its own (the backtest tool's engine child, the research tool's CLI
child), so ``backtest.data_audit``'s in-process record dies with it. The agent process folds each read into the
conversation's exposure file (``<sessions dir>/<session_id>/data_exposure.jsonl``) and ``ir_export`` passes the latest end
to the export. Every value here is synthetic.
"""

from __future__ import annotations

import json
import subprocess

import pandas as pd
import pytest

from tests.fixtures.alphakeel_tool_cli import patch_run

pytestmark = pytest.mark.unit

SESSION = "sess-exposure"


@pytest.fixture()
def session(tmp_path, monkeypatch):
    """A bound conversation with its sessions dir under ``tmp_path`` (as the agent loop binds it for a run)."""
    from backtest import data_audit
    from src.providers.session_context import bind_llm_session_id, reset_llm_session_id
    from src.tools import alphakeel_research_ledger

    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path))
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_WRITE_ROOTS", str(tmp_path))
    monkeypatch.setattr(alphakeel_research_ledger, "_sessions_dir", lambda: tmp_path / "sessions")
    data_audit.reset_provenance()
    token = bind_llm_session_id(SESSION)
    try:
        yield tmp_path
    finally:
        reset_llm_session_id(token)
        data_audit.reset_provenance()


def _okx_run_dir(root, end_date: str):
    rd = root / "run"
    (rd / "code").mkdir(parents=True)
    (rd / "code" / "signal_engine.py").write_text("class SignalEngine:\n    pass\n")
    (rd / "config.json").write_text(json.dumps({"source": "okx", "codes": ["BTC-USDT"], "start_date": "2026-01-01",
                                                "end_date": end_date}))
    return rd


def _export_argv(root, monkeypatch) -> list[str]:
    from src.tools.alphakeel_research_tool import AlphakeelResearchTool

    seen = {}

    def run(cmd, timeout=None):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"strategy_id": "s"}), "")

    patch_run(monkeypatch, run)
    out = json.loads(AlphakeelResearchTool(default_session_id=SESSION).execute(
        action="ir_export", run_dir=str(root / "run"),
        args={"ir": str(root / "ir.json"), "run": str(root / "irbt"), "out": str(root / "export")}))
    assert out["status"] == "ok", out
    return seen["cmd"]


def test_a_direct_exchange_read_by_the_backtest_tool_reaches_ir_export(session, monkeypatch) -> None:
    """The regression: an okx pull in the backtest tool's child was missing from the export's data_exposure_end_ms."""
    from backtest import data_audit
    from src.core.runner import RunResult
    from src.tools import backtest_tool

    card_end = int(pd.Timestamp("2026-01-08").timestamp() * 1000)

    class ChildRunner:  # the engine child: its record of the read survives only in the run card it writes
        def __init__(self, timeout=None):
            self.timeout = timeout

        def execute(self, entry, run_path, cwd=None, cli_args=None):
            (run_path / "run_card.json").write_text(json.dumps({"data_audit": {"data_exposure_end_ms": card_end}}))
            return RunResult(success=False, exit_code=1, stdout="", stderr="", artifacts={})

    monkeypatch.setattr(backtest_tool, "Runner", ChildRunner)
    rd = _okx_run_dir(session, "2026-01-10")
    backtest_tool.run_backtest(str(rd))
    data_audit.reset_provenance()  # ir_export runs in a CLI child of its own: nothing of this process reaches it

    argv = _export_argv(session, monkeypatch)
    asked_end = int(pd.Timestamp("2026-01-11").timestamp() * 1000)  # the config's end_date, inclusive: the later one
    assert argv[argv.index("--data-exposure-end-ms") + 1] == str(asked_end), argv
    rows = [json.loads(x) for x in (session / "sessions" / SESSION / "data_exposure.jsonl").read_text().splitlines()]
    assert rows and rows[-1]["source"] == "okx" and rows[-1]["via"] == "backtest:run"


def test_an_in_process_read_is_recorded_for_the_bound_session_only(session) -> None:
    from backtest import data_audit
    from src.providers.session_context import bind_llm_session_id, reset_llm_session_id
    from src.tools import alphakeel_research_ledger as ledger

    data_audit.record_exposure("binance", 1_700_000_000_000)
    assert ledger.exposure_end_ms(SESSION) == 1_700_000_000_000
    token = bind_llm_session_id("")
    try:
        data_audit.record_exposure("binance", 1_700_100_000_000)  # no conversation bound: this process only
    finally:
        reset_llm_session_id(token)
    assert ledger.exposure_end_ms(SESSION) == 1_700_000_000_000
    assert data_audit.data_exposure_end_ms() == 1_700_100_000_000


def test_ir_export_without_reads_passes_no_exposure(session, monkeypatch) -> None:
    assert "--data-exposure-end-ms" not in _export_argv(session, monkeypatch)


# --- the agent loop's tool threads keep the bound conversation ----------------------------------------------------------

class _ReadonlyRegistry:
    """A readonly tool whose body records a data read in-process, as the market data tool's loaders do."""

    class _Tool:
        is_readonly = True

    def get(self, tool_name: str) -> object:
        return self._Tool()

    def execute(self, tool_name: str, args: dict) -> str:
        from backtest import data_audit

        data_audit.record_exposure("okx", 1_700_000_000_000)
        return '{"status":"ok"}'


def _agent(registry=None):
    from types import SimpleNamespace

    from src.agent.loop import AgentLoop

    return AgentLoop(registry=registry or _ReadonlyRegistry(), llm=SimpleNamespace(), max_iterations=1)  # type: ignore[arg-type]


def test_a_readonly_tool_thread_records_reads_for_the_bound_session(session) -> None:
    """Readonly tools run in a worker thread; a new thread starts with an empty context, which lost the session."""
    from src.tools import alphakeel_research_ledger as ledger

    result, _ = _agent()._invoke_tool("market_data", {}, call_id="c1")
    assert json.loads(result)["status"] == "ok"
    assert ledger.exposure_end_ms(SESSION) == 1_700_000_000_000


def test_parallel_readonly_tools_run_in_the_bound_session(session, monkeypatch) -> None:
    """Consecutive readonly tool calls run in a thread pool; each worker must see the conversation the loop bound."""
    from types import SimpleNamespace

    from src.providers.session_context import current_llm_session_id

    agent = _agent()
    seen: list[str] = []

    def invoke(name, args, call_id=None):
        seen.append(current_llm_session_id())
        return '{"status":"ok"}', 0

    monkeypatch.setattr(agent, "_invoke_tool", invoke)
    monkeypatch.setattr(agent, "_finalize_tool_result", lambda *a, **k: None)
    calls = [SimpleNamespace(name="market_data", arguments={}, id=f"c{i}") for i in range(3)]
    agent._execute_parallel(calls, None, [], SimpleNamespace(write=lambda row: None), [], 0)  # type: ignore[arg-type]
    assert seen == [SESSION] * 3
