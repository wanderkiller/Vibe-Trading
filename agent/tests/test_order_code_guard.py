"""Order-placing code is refused by write_file and edit_file.

Regression of session f9941f6d027f: the agent wrote exchange order code ("改 dry_run=False 即可实盘") although this repository
never places orders. Every value here is synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.tools.edit_file_tool import EditFileTool
from src.tools.order_code_guard import order_code_refusal
from src.tools.write_file_tool import WriteFileTool

pytestmark = pytest.mark.unit

_ORDER_SNIPPETS = [
    "ex.create_order('BTC/USDT', 'limit', 'buy', 1, 100)",
    "ex.create_limit_buy_order('BTC/USDT', 1, 100)",
    "ex.create_market_sell_order('BTC/USDT', 1)",
    "ex.create_stop_order('BTC/USDT', 1)",
    "client.place_order(symbol='BTCUSDT')",
    "ex.privatePostOrderCreate({})",
    "ex.privatePostApiV5TradeOrder({})",
    "requests.post(base + '/api/v3/order', data=p)",
    "requests.post(base + '/fapi/v1/order', data=p)",
    "post('/api/v5/trade/order', body)",
    "post('/v5/order/create', body)",
    "await trading_place_order(args)",
    "broker.submit_order(order)",
    "ex.createOrder(s, 'market', 'buy', 1)",
    "ex.CREATE_ORDER('BTC/USDT', 'market', 'buy', 1)",
    "getattr(ex, 'create_order')(s, 'market', 'buy', 1)",
    "client.futures_create_order(symbol='BTCUSDT')",
    "ex.create_market_buy_order(s, 1)",
    "ex.create_post_only_order(s, 1)",
    "o = ex.order_market_buy(symbol=s, quantity=1)",
    "o = client.order_limit_sell(symbol=s)",
    "ex.placeOrder(s)",
    "getattr(ex, 'place_order')",
    "ex.submitOrder(s)",
    "ex.fapiPrivatePostOrder({})",
    "ex.private_post_order({})",
    "ex.privatePostTradeOrder({})",
    "requests.post('https://x/sapi/v1/margin/order')",
    "requests.post('https://x/papi/v1/um/order')",
    "requests.post('https://x/dapi/v1/order')",
    "requests.post('https://x/api/v3/order/test')",
]
_ALLOWED = [
    "import ccxt\nex = ccxt.okx()\nbars = ex.fetch_ohlcv('BTC/USDT:USDT', '1h', limit=500)\n",
    "class SignalEngine:\n    def generate(self, data_map):\n        return {c: df['close'].pct_change().rolling(20).mean().gt(0).astype(float) for c, df in data_map.items()}\n",
    "# no orders here: this only backtests\nreorder = sorted(items)\n",
    "df.sort_values(order)",
    "reorder_columns(df)",
    "order_book_depth = 5",
    "url = base + '/api/v3/depth'",
]


@pytest.fixture(autouse=True)
def _roots(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path))


@pytest.mark.parametrize("snippet", _ORDER_SNIPPETS)
def test_each_order_pattern_is_refused(snippet: str) -> None:
    assert order_code_refusal(snippet) is not None


@pytest.mark.parametrize("snippet", _ALLOWED)
def test_research_code_is_allowed(snippet: str) -> None:
    assert order_code_refusal(snippet) is None


def test_write_file_refuses_and_writes_nothing(tmp_path: Path) -> None:
    body = json.loads(WriteFileTool().execute(path="live.py", content="ex.create_order('BTC/USDT', 'market', 'buy', 1)\n", run_dir=str(tmp_path)))
    assert body["status"] == "error" and body["error_code"] == "order_code_refused"
    assert body["error"].startswith("Vibe-Trading research code does not place orders")
    assert not (tmp_path / "live.py").exists()
    ok = json.loads(WriteFileTool().execute(path="bt.py", content=_ALLOWED[0], run_dir=str(tmp_path)))
    assert ok["status"] == "ok" and (tmp_path / "bt.py").is_file()


def test_edit_file_refuses_and_leaves_the_file_unchanged(tmp_path: Path) -> None:
    target = tmp_path / "signal_engine.py"
    target.write_text("DRY_RUN = True\n", encoding="utf-8")
    body = json.loads(EditFileTool().execute(path="signal_engine.py", old_text="DRY_RUN = True", new_text="DRY_RUN = False\nex.create_order(s)", run_dir=str(tmp_path)))
    assert body["status"] == "error" and body["error_code"] == "order_code_refused"
    assert target.read_text(encoding="utf-8") == "DRY_RUN = True\n"
    ok = json.loads(EditFileTool().execute(path="signal_engine.py", old_text="True", new_text="False", run_dir=str(tmp_path)))
    assert ok["status"] == "ok" and target.read_text(encoding="utf-8") == "DRY_RUN = False\n"


def test_edit_file_checks_the_whole_replaced_content(tmp_path: Path) -> None:
    """An edit may complete order code out of pieces already in the file; new_text alone looks harmless."""
    target = tmp_path / "signal_engine.py"
    target.write_text("ex.foo(s)\n", encoding="utf-8")
    body = json.loads(EditFileTool().execute(path="signal_engine.py", old_text="foo", new_text="create_order", run_dir=str(tmp_path)))
    assert body["status"] == "error" and body["error_code"] == "order_code_refused"
    assert target.read_text(encoding="utf-8") == "ex.foo(s)\n"
