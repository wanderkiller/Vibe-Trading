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


def test_the_tool_builds_dataset_pack_freeze_and_read_arguments():
    from src.tools.alphakeel_research_tool import AlphakeelResearchTool, _argv

    inst = [{"venue": "okx", "market": "perp", "symbol": "BTC-USDT-SWAP"}]
    argv = _argv("freeze", {"start_ms": 1, "end_ms": 2, "instruments": inst, "market_tables": ["kline_1m", "bbo"], "price_kinds": ["trade", "mark"],
                            "market_dataset": "mkt-v1", "funding": "none", "accept_partial": True})
    assert argv[:5] == ["freeze", "--start-ms", "1", "--end-ms", "2"]
    path = argv[argv.index("--instruments") + 1]
    assert json.load(open(path)) == inst  # the list is written to a file the CLI can load
    assert argv[argv.index("--market-tables") + 1] == "kline_1m,bbo" and argv[argv.index("--price-kinds") + 1] == "trade,mark"
    assert argv[argv.index("--market-dataset") + 1] == "mkt-v1" and "--accept-partial" in argv and argv[argv.index("--funding") + 1] == "none"
    # a path is passed through; a scan-pack freeze has no dataset flags
    assert _argv("freeze", {"start_ms": 1, "end_ms": 2, "instruments": "/x/inst.json"})[-2:] == ["--instruments", "/x/inst.json"]
    assert "--instruments" not in _argv("freeze", {"start_ms": 1, "end_ms": 2})
    # market options without instruments are a clear error, not a silently ignored scan-pack freeze
    out = json.loads(AlphakeelResearchTool().execute(action="freeze", args={"start_ms": 1, "end_ms": 2, "market_tables": ["bbo"]}))
    assert out["status"] == "error" and "instruments" in out["error"]
    rd = _argv("read", {"pack": "p", "venue": "okx", "symbol": "S", "start_ms": 1, "end_ms": 2, "table": "klines", "price_kind": "mark", "market": "perp"})
    assert rd[rd.index("--table") + 1] == "klines" and rd[rd.index("--price-kind") + 1] == "mark"
