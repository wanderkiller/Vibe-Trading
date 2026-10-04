"""The alphakeel_pack loader refuses OHLCV explicitly; the alphakeel_research tool returns references and clean errors."""

from __future__ import annotations

import json

import pytest

from backtest.loaders.base import NoAvailableSourceError
from backtest.loaders import registry


def test_alphakeel_pack_is_a_registered_explicit_only_source_that_never_fabricates_bars(monkeypatch, tmp_path):
    assert "alphakeel_pack" in registry.VALID_SOURCES
    registry._ensure_registered()
    cls = registry.LOADER_REGISTRY["alphakeel_pack"]
    assert registry.is_no_network_fallback_source("alphakeel_pack")
    monkeypatch.delenv("ALPHAKEEL_PACK_DIR", raising=False)
    assert cls().is_available() is False
    with pytest.raises(NoAvailableSourceError, match="not OHLCV"):
        cls().fetch(["BTCUSDT"], "2026-01-01", "2026-01-02")
    (tmp_path / "lock.json").write_text("{}")
    monkeypatch.setenv("ALPHAKEEL_PACK_DIR", str(tmp_path))
    assert cls().is_available() is True
    with pytest.raises(NoAvailableSourceError):
        cls().fetch(["BTCUSDT"], "2026-01-01", "2026-01-02")  # still no bars, even with a pack configured


def test_the_tool_validates_arguments_and_never_leaks_the_credential(monkeypatch):
    from src.tools.alphakeel_research_tool import AlphakeelResearchTool, _argv

    t = AlphakeelResearchTool()
    assert json.loads(t.execute(action="trade"))["status"] == "error"
    assert "start_ms" in json.loads(t.execute(action="freeze", args={}))["error"]
    assert _argv("compare", {"a": "r1", "b": "r2", "mode": "python_policy"}) == ["compare", "--a", "r1", "--b", "r2", "--mode", "python_policy"]
    monkeypatch.setenv("ALPHAKEEL_RESEARCH_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("ALPHAKEEL_RESEARCH_TOKEN", "ak_rs_tok-secret.supersecretvalue")
    out = t.execute(action="check")
    assert "supersecretvalue" not in out and json.loads(out)["status"] == "error"


def test_the_tool_is_auto_registered_with_the_agent_tools():
    from src.agent.tools import BaseTool

    import src.tools.alphakeel_research_tool  # noqa: F401

    assert "alphakeel_research" in {c.name for c in BaseTool.__subclasses__() if getattr(c, "name", None)}
