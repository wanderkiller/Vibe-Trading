"""Auto-recalled project memories are scoped by asset.

Regression of session f9941f6d027f: an unrelated QQQ research memory was injected into a crypto funding-arb conversation and the
model referred to it as if the user had done it there. Every value here is synthetic.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.agent.context import ContextBuilder
from src.agent.memory import WorkspaceMemory
from src.agent.tools import ToolRegistry
from src.memory.scope import asset_tokens, memory_in_scope

pytestmark = pytest.mark.unit

_NO_TICKER = "所以你才要去alphazoo里引入一些因子进来啊"


def _entry(memory_type: str, title: str = "QQQ分批进场回测", body: str = "QQQ 分 3 批进场，持有 20 日", keywords=("QQQ",)):
    return SimpleNamespace(title=title, memory_type=memory_type, body=body, keywords=keywords)


def test_asset_tokens() -> None:
    assert asset_tokens("QQQ分批进场") == {"QQQ"}
    assert asset_tokens("BTC-USDT-PERP") == {"BTC-USDT", "BTC"}
    assert asset_tokens(_NO_TICKER) == frozenset()
    assert asset_tokens("ETH/BTC 配对") == {"ETH/BTC", "ETH", "BTC"}
    assert asset_tokens("BTCUSDT funding") == {"BTCUSDT", "BTC"}
    assert asset_tokens("binance:perp:BTCUSDT") == {"BTCUSDT", "BTC"}
    assert asset_tokens("ETHFDUSD") == {"ETHFDUSD", "ETH"}
    assert asset_tokens("btc carry") == {"BTC"}
    assert asset_tokens("继续比特币的研究") == {"BTC"}
    assert asset_tokens("Ethereum and solana, not a solution") == {"ETH", "SOL"}
    assert asset_tokens("RSI MACD SMA EMA ATR KDJ BOLL VWAP TWAP OBV ADX CCI MFI ROC DMI SAR ROI") == frozenset()
    assert asset_tokens("OOS 的 DSR 与 PBO，USDT 计价") == frozenset()


def test_project_memory_scope() -> None:
    project = _entry("project")
    assert not memory_in_scope(project, asset_tokens(_NO_TICKER))
    assert not memory_in_scope(project, asset_tokens("BTC-USDT-PERP 的资金费套利"))
    assert memory_in_scope(project, asset_tokens("像 QQQ 那样做"))
    # a project memory naming no asset is in scope everywhere
    assert memory_in_scope(_entry("project", title="回测流程", body="先看样本外再看全样本", keywords=()), frozenset())


@pytest.mark.parametrize("message", ["BTCUSDT funding", "btc carry", "继续比特币的研究"])
def test_a_named_asset_matches_its_ticker_memory(message: str) -> None:
    memory = _entry("project", title="BTC funding carry", body="funding carry on BTC perps", keywords=())
    assert memory_in_scope(memory, asset_tokens(message))


def test_indicator_names_are_not_assets() -> None:
    memory = _entry("project", title="QQQ 技术指标", body="RSI and MACD on QQQ", keywords=())
    assert not memory_in_scope(memory, asset_tokens("RSI on SPY"))


@pytest.mark.parametrize("memory_type", ["feedback", "user", "preference"])
def test_non_project_memory_is_always_in_scope(memory_type: str) -> None:
    assert memory_in_scope(_entry(memory_type), asset_tokens(_NO_TICKER))


class _Persistent:
    snapshot = None

    def __init__(self, *entries) -> None:
        self._entries = list(entries)
        self.max_results = None

    def find_relevant(self, query: str, max_results: int = 3):
        self.max_results = max_results
        return self._entries[:max_results]


def _user_content(message: str, *entries) -> str:
    return _build(message, _Persistent(*entries))


def _build(message: str, persistent: _Persistent) -> str:
    builder = ContextBuilder(ToolRegistry(), WorkspaceMemory(), persistent_memory=persistent)
    return builder.build_messages(message)[-1]["content"]


def test_context_drops_an_out_of_scope_project_memory() -> None:
    assert "<recalled-memories" not in _user_content(_NO_TICKER, _entry("project"))
    assert "<recalled-memories" not in _user_content("BTC-USDT-PERP 的资金费套利", _entry("project"))


def test_context_keeps_in_scope_and_non_project_memories_with_the_note() -> None:
    user = _user_content("像 QQQ 那样做", _entry("project"), _entry("feedback", title="简短回答", body="答案要短", keywords=()))
    assert '<recalled-memories note="background from earlier sessions; not evidence for this session' in user
    assert "QQQ分批进场回测" in user and "简短回答" in user and user.rstrip().endswith("像 QQQ 那样做")
    only_feedback = _user_content(_NO_TICKER, _entry("project"), _entry("feedback", title="简短回答", body="答案要短", keywords=()))
    assert "简短回答" in only_feedback and "QQQ分批进场回测" not in only_feedback


def test_a_fourth_in_scope_memory_is_found_behind_three_out_of_scope_ones() -> None:
    out = [_entry("project", title=f"QQQ 笔记 {i}") for i in range(3)]
    inside = _entry("project", title="BTC 笔记", body="BTC 资金费", keywords=("BTC",))
    persistent = _Persistent(*out, inside)
    user = _build("BTC 的资金费怎么样", persistent)
    assert persistent.max_results == 10
    assert "BTC 笔记" in user and "QQQ 笔记" not in user


def test_at_most_three_in_scope_memories_are_injected() -> None:
    entries = [_entry("feedback", title=f"反馈{i}", body="短", keywords=()) for i in range(5)]
    user = _user_content(_NO_TICKER, *entries)
    assert "反馈0" in user and "反馈2" in user and "反馈3" not in user
