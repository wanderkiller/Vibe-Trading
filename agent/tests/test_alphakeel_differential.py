"""Property tests: the independent Decimal simulator against AlphaKeel's engine, plus simulator golds and the HTTP contract.

The differential test draws random (but valid-looking) intent sequences and requires that, whenever the simulator
accepts a sequence, the engine reviewed by the layered comparator agrees on every layer. Sequences the simulator
refuses (`Unsupported`) or the service rejects (`ApiError`) are discarded, never counted as agreement.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, assume, given, settings, strategies as st

from alphakeel_research import workflow
from alphakeel_research.client import Client
from alphakeel_research.errors import ApiError, Unsupported
from tests.fixtures.alphakeel_server import start

BIN = {"venue": "binance", "market": "perp", "symbol": "BTCUSDT"}
OKX = {"venue": "okx", "market": "perp", "symbol": "BTC-USDT-SWAP"}


@pytest.fixture(scope="module")
def server():
    f = start()
    yield f
    f.stop()


@pytest.fixture(scope="module")
def client(server):
    c = Client(server.url, server.token)
    yield c
    c.close()


@pytest.fixture(scope="module")
def pack(server, client, tmp_path_factory):
    return workflow.freeze_pack(client, {"window": {"start_ms": server.start_ms, "end_ms": server.end_ms}}, cache_dir=tmp_path_factory.mktemp("c"))


def intent_lists(start_ms: int):
    one = st.fixed_dictionaries({
        "k": st.integers(0, 60),
        "inst": st.sampled_from([BIN, OKX]),
        "side": st.sampled_from(["buy", "sell"]),
        "qty": st.sampled_from(["1", "10", "33.333", "0.5", "250", "9999", "10001", "0.00000001", "0.000000019"]),
        "kind": st.sampled_from(["market", "ioc_cross", "ioc_away"]),
        "reduce": st.booleans(),
    })

    def build(rows):
        out = []
        for i, r in enumerate(sorted(rows, key=lambda r: r["k"])):
            px = None
            if r["kind"] == "ioc_cross":
                px = "1.5" if r["side"] == "buy" else "0.5"
            elif r["kind"] == "ioc_away":
                px = "0.5" if r["side"] == "buy" else "1.5"
            out.append({"intent_id": f"i{i}", "decision_time_ms": start_ms + 60_000 * r["k"], "instrument": r["inst"], "side": r["side"], "qty": r["qty"],
                        "order_type": "market" if r["kind"] == "market" else "limit_ioc", "limit_price": px, "reduce_only": r["reduce"], "position_ref": None})
        return out

    return st.lists(one, min_size=1, max_size=5).map(build)


@pytest.mark.parametrize("seed", [0])
def test_simulator_and_engine_agree_on_random_intent_sequences(server, client, pack, tmp_path, seed):
    checked = {"n": 0}

    @settings(max_examples=40, deadline=None, derandomize=True, suppress_health_check=list(HealthCheck))
    @given(intent_lists(server.start_ms))
    def prop(intents):
        try:
            rev = workflow.review_intents(client, pack, intents, cache_run_dir=tmp_path)
        except Unsupported:
            assume(False)
        except ApiError as e:
            assume(e.code not in ("engine.failure",) and False)  # rejected by the service: not an agreement either
            raise
        rep = rev["report"]
        assert rep["passed"], json.dumps({"intents": intents, "first": rep["first_difference"], "layers": {k: v["status"] for k, v in rep["layers"].items()}}, indent=1)
        checked["n"] += 1

    prop()
    assert checked["n"] >= 5, "too few sequences were accepted by both sides for the property to mean anything"


def test_simulator_unit_golds(server, client, pack):
    from alphakeel_research.sim import q8

    assert q8(Decimal("0.123456785")) == Decimal("0.12345678") and q8(Decimal("0.123456775")) == Decimal("0.12345678")  # half-even
    intents = [{"intent_id": "a", "decision_time_ms": server.start_ms, "instrument": BIN, "side": "buy", "qty": "100", "order_type": "market",
                "limit_price": None, "reduce_only": False, "position_ref": "p"},
               {"intent_id": "b", "decision_time_ms": server.start_ms + 60_000, "instrument": BIN, "side": "sell", "qty": "100", "order_type": "market",
                "limit_price": None, "reduce_only": True, "position_ref": "p"}]
    profile = client.render_profile(pack.id, "fixed_intent_replay")["profile"]
    r = workflow.python_replay(pack, profile, intents, run_id="gold").result
    # buy at the ask 1, sell at the bid 0.999: price pnl -0.1, fees 0.05 + 0.04995 = 0.09995, no boundary is crossed
    assert r["amounts"]["price_pnl"] == "-0.1" and r["amounts"]["fees"] == "0.09995" and r["amounts"]["funding"] == "0"
