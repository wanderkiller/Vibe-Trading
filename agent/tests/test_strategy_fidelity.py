"""A blocked backtest is reported, not swapped for another strategy (session 8125728230d5).

The AlphaKeel service had no published market dataset, the agent offered "use the built-in engine now" and ran a
single-asset spot strategy when the user asked for a cross-venue funding carry backtest.
"""

from __future__ import annotations

import json

import pytest

from tests.fixtures.alphakeel_tool_cli import patch_cli

from src.agent.context import ContextBuilder
from src.agent.memory import WorkspaceMemory
from src.agent.tools import ToolRegistry

pytestmark = pytest.mark.unit

_NO_MARKET = "no market dataset is configured or published on this service"


def test_the_prompt_forbids_substituting_a_strategy() -> None:
    prompt = ContextBuilder(ToolRegistry(), WorkspaceMemory()).build_system_prompt("回测一下")
    assert "Backtest the strategy that was asked for, or say you did not." in prompt
    assert "is not that confirmation" in prompt


def test_the_prompt_leaves_research_verdicts_to_the_result_card() -> None:
    prompt = ContextBuilder(ToolRegistry(), WorkspaceMemory()).build_system_prompt("回测一下")
    assert "Research verdicts come from the system result card appended after your answer, not from you" in prompt
    assert "never call a result validated, robust, risk-controlled, market-neutral, statistically significant or ready for live trading." in prompt
    assert "When the card says passed_screen, say only that it passed the basic screen" in prompt
    assert "Describe what actually ran from the card's facts (direction, time in market)" in prompt
    assert "AlphaKeel's G1/G2 gates" in prompt
    assert "Never cite news or events that no tool returned." in prompt
    assert "sign-flip Sharpe test, order-permutation drawdown test" in prompt


def _run(monkeypatch, main) -> dict:
    from src.tools.alphakeel_research_tool import AlphakeelResearchTool

    patch_cli(monkeypatch, main)
    return json.loads(AlphakeelResearchTool().execute(action="freeze", args={
        "start_ms": 1, "end_ms": 2, "market_tables": ["kline_1m"],
        "instruments": [{"venue": "okx", "market": "perp", "symbol": "BTC-USDT-SWAP"}]}))


def test_no_market_dataset_error_carries_the_blocked_instruction(monkeypatch) -> None:
    def printed(argv):
        print(json.dumps({"error": {"code": "data.insufficient", "message": _NO_MARKET, "retryable": False}}))
        return 1

    out = _run(monkeypatch, printed)
    assert out["status"] == "error" and out["next"].startswith("BLOCKED")
    assert "Do NOT replace the requested strategy" in out["next"]

    from alphakeel_research.errors import ApiError

    def raised(argv):  # the child process: cli.main turns an ApiError into its JSON error and exit 1 (cli.py)
        try:
            raise ApiError("data.insufficient", _NO_MARKET)
        except ApiError as e:
            print(json.dumps({"error": {"code": e.code, "message": e.message}}))
            return 1

    out = _run(monkeypatch, raised)
    assert out["result"]["error"]["code"] == "data.insufficient" and out["next"].startswith("BLOCKED")


def test_other_errors_carry_no_blocked_instruction(monkeypatch) -> None:
    def other(argv):
        print(json.dumps({"error": {"code": "data.insufficient", "message": "funding coverage partial"}}))
        return 1

    assert "next" not in _run(monkeypatch, other)
