"""Research contract: pure functions (backtest.research_contract)."""

from __future__ import annotations

import pytest

from backtest.research_contract import (
    base_asset,
    contract_diff,
    contract_of_engine,
    contract_of_ir,
    instrument_class,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("code,base", [
    ("BTC-USDT", "BTC"),
    ("BTC-USDT-PERP", "BTC"),
    ("okx:perp:BTC-USDT-SWAP", "BTC"),
    ("binance:spot:BTCUSDT", "BTC"),
    ("ETH/USDT", "ETH"),
    ("eth-usdt", "ETH"),
    ("AAPL.US", "AAPL.US"),
    ("600000.sh", "600000.SH"),
])
def test_base_asset(code, base) -> None:
    assert base_asset(code) == base


def test_instrument_class() -> None:
    assert instrument_class(["BTC-USDT", "ETH-USDT"]) == "spot"
    assert instrument_class(["BTC-USDT-PERP", "okx:perp:ETH-USDT-SWAP"]) == "perp"
    assert instrument_class(["AAPL.US"]) == "cash"
    assert instrument_class(["BTC-USDT", "BTC-USDT-PERP"]) == "mixed"
    assert instrument_class([]) == "unknown"


def _ir(*bases: str, market: str = "perp") -> dict:
    return {"instruments": [{"venue": "binance", "market": market, "symbol": f"{b}USDT", "base": b} for b in bases]}


def test_contract_of_ir_and_engine() -> None:
    assert contract_of_ir(_ir("SOL", "BTC", "ETH", "BTC")) == {
        "engine": "ir", "direction": "long_short", "instrument_class": "perp", "assets": ["BTC", "ETH", "SOL"]}
    mixed = {"instruments": [*_ir("BTC")["instruments"], *_ir("BTC", market="spot")["instruments"]]}
    assert contract_of_ir(mixed)["instrument_class"] == "mixed"
    assert contract_of_engine({"direction": "long_only", "codes": ["ETH-USDT", "BTC-USDT"]}) == {
        "engine": "generic", "direction": "long_only", "instrument_class": "spot", "assets": ["BTC", "ETH"]}
    assert contract_of_engine({})["direction"] == "unknown"


def test_contract_diff_for_the_f9941f6d027f_sequence() -> None:
    carry = contract_of_ir(_ir("BTC", "ETH", "SOL"))
    trend = contract_of_engine({"direction": "long_only", "codes": ["BTC-USDT", "ETH-USDT", "SOL-USDT"]})
    pairs = contract_of_engine({"direction": "long_short", "codes": ["ETH-USDT", "BTC-USDT"]})
    assert contract_diff(carry, trend) == ["engine", "direction", "instrument_class"]
    assert contract_diff(trend, pairs) == ["direction"]
    assert contract_diff(trend, trend) == []


def test_contract_diff_assets_only_when_disjoint() -> None:
    a = contract_of_engine({"direction": "long_only", "codes": ["BTC-USDT"]})
    assert contract_diff(a, contract_of_engine({"direction": "long_only", "codes": ["BTC-USDT", "ETH-USDT"]})) == []
    assert contract_diff(a, contract_of_engine({"direction": "long_only", "codes": ["SOL-USDT"]})) == ["assets"]
