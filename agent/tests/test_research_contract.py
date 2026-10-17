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
        "engine": "ir", "direction": "long_short", "instrument_class": "perp", "assets": ["BTC", "ETH", "SOL"],
        "interval": "unknown"}
    mixed = {"instruments": [*_ir("BTC")["instruments"], *_ir("BTC", market="spot")["instruments"]]}
    assert contract_of_ir(mixed)["instrument_class"] == "mixed"
    assert contract_of_engine({"direction": "long_only", "codes": ["ETH-USDT", "BTC-USDT"]}) == {
        "engine": "generic", "direction": "long_only", "instrument_class": "spot", "assets": ["BTC", "ETH"],
        "interval": "unknown"}
    assert contract_of_engine({})["direction"] == "unknown"


def test_contract_diff_for_the_f9941f6d027f_sequence() -> None:
    carry = contract_of_ir(_ir("BTC", "ETH", "SOL"))
    trend = contract_of_engine({"direction": "long_only", "codes": ["BTC-USDT", "ETH-USDT", "SOL-USDT"]})
    pairs = contract_of_engine({"direction": "long_short", "codes": ["ETH-USDT", "BTC-USDT"]})
    assert contract_diff(carry, trend) == ["engine", "direction", "instrument_class"]
    assert contract_diff(trend, pairs) == ["direction", "assets"]  # {BTC, ETH, SOL} -> {BTC, ETH}
    assert contract_diff(trend, trend) == []


def test_contract_diff_assets_when_the_sets_differ() -> None:
    a = contract_of_engine({"direction": "long_only", "codes": ["BTC-USDT"]})
    assert contract_diff(a, contract_of_engine({"direction": "long_only", "codes": ["BTC-USDT", "ETH-USDT"]})) == ["assets"]
    assert contract_diff(a, contract_of_engine({"direction": "long_only", "codes": ["SOL-USDT"]})) == ["assets"]
    assert contract_diff(a, contract_of_engine({"direction": "long_only", "codes": ["BTC-USDT-PERP"]})) == ["instrument_class"]


def test_contract_diff_flags_any_difference_of_non_empty_asset_sets() -> None:
    btc = contract_of_engine({"direction": "long_only", "codes": ["BTC-USDT"]})
    both = contract_of_engine({"direction": "long_only", "codes": ["BTC-USDT", "ETH-USDT"]})
    assert contract_diff(btc, both) == ["assets"] and contract_diff(both, btc) == ["assets"]
    assert contract_diff(btc, {**btc, "assets": []}) == []  # no assets named: nothing to compare


def test_contract_diff_interval() -> None:
    facts = {"direction": "long_only", "codes": ["BTC-USDT"]}
    daily, minute = contract_of_engine(facts, interval="1D"), contract_of_engine(facts, interval="1m")
    assert daily["interval"] == "1D" and contract_diff(daily, minute) == ["interval"]
    assert contract_diff(daily, contract_of_engine(facts)) == []  # unknown on either side is not a change
    old = {k: v for k, v in daily.items() if k != "interval"}  # a contract file from before the field
    assert contract_diff(old, daily) == [] and contract_diff(old, minute) == []
    assert contract_of_ir({"interval": "4H", "instruments": []})["interval"] == "4H"
    assert contract_of_ir({"instruments": []})["interval"] == "unknown"


def test_contract_of_ir_reads_1m_from_the_view_and_the_window_days_from_the_card() -> None:
    card = {**_ir("BTC"), "view": {"kind": "dataset"}, "window": {"start_day": "2026-01-01", "end_day": "2026-06-29"}}
    c = contract_of_ir(card)
    assert c["interval"] == "1m" and c["window_days"] == 179 and "window" not in c
    assert "window_days" not in contract_of_ir(_ir("BTC"))


def test_contract_diff_window_days_ignores_a_drifting_start_and_end() -> None:
    def card(start: str, end: str) -> dict:
        return {**_ir("BTC"), "window": {"start_day": start, "end_day": end}}

    a, same_len = contract_of_ir(card("2026-01-01", "2026-06-30")), contract_of_ir(card("2026-01-02", "2026-07-01"))
    shorter = contract_of_ir(card("2026-04-01", "2026-06-30"))
    assert contract_diff(a, same_len) == []  # the default window moved with the hold-out cutoff: same strategy
    assert contract_diff(contract_of_ir(card("2026-01-01", "2026-06-30")), contract_of_ir(card("2026-01-01", "2026-06-29"))) == ["window_days"]
    assert contract_diff(a, shorter) == ["window_days"]
    assert contract_diff(a, contract_of_ir(_ir("BTC"))) == []  # one side has no window: not a change
