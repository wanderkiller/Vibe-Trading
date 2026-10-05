"""Property tests: the independent Decimal simulator against AlphaKeel's engine, plus simulator golds and the HTTP contract.

The differential test draws random (but valid-looking) intent sequences and requires that, whenever the simulator
accepts a sequence, the engine reviewed by the layered comparator agrees on every layer. Only the explicitly listed
input rejections (``EXPECTED_REJECTIONS``: the simulator's ``Unsupported`` cases and the service's documented
invalid/unsupported-input codes) are discarded, never counted as agreement. ``engine.failure``, contract/evidence
errors, interrupted runs or any other code fail the test. Generated / accepted / excluded counts are reported.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, assume, event, given, settings, strategies as st

from alphakeel_research import workflow
from alphakeel_research.client import Client
from alphakeel_research.errors import ApiError
from tests.fixtures.alphakeel_server import start

#: Input rejections that mean "this random sequence is outside what both sides model", not a disagreement. Anything
#: else raised while reviewing a sequence (engine.failure, schema/contract/evidence errors, run.interrupted, ...) fails.
EXPECTED_REJECTIONS = frozenset({
    "capability.unsupported", "capability.order_type", "capability.product", "capability.initial_position",
    "intent.overflow", "intent.non_positive_qty", "intent.reduce_only_needs_position", "intent.limit_price_required",
    "data.quote_missing",
})
#: Raised by the local simulator before anything is sent (e.g. a position_ref reused on another contract or side).
EXPECTED_LOCAL_REJECTIONS = EXPECTED_REJECTIONS | {"request.invalid"}

BIN = {"venue": "binance", "market": "perp", "symbol": "BTCUSDT"}
SPOT = {"venue": "binance", "market": "spot", "symbol": "USDCUSDT"}  # a separate (spot) account, long only
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
        "inst": st.sampled_from([BIN, OKX, SPOT]),
        "side": st.sampled_from(["buy", "sell"]),
        # around the 10 000 per-account balance too: 4999 + 4999 fits, 9950 sits in the maintenance band after a fill
        "qty": st.sampled_from(["1", "10", "33.333", "0.5", "250", "4999", "9950", "9999", "10001", "0.00000001", "0.000000019"]),
        "kind": st.sampled_from(["market", "ioc_cross", "ioc_away"]),
        "reduce": st.booleans(),
        "ref": st.sampled_from([None, "p0", "p1"]),
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
                        "order_type": "market" if r["kind"] == "market" else "limit_ioc", "limit_price": px, "reduce_only": r["reduce"], "position_ref": r["ref"]})
        return out

    return st.lists(one, min_size=1, max_size=5).map(build)


@pytest.mark.parametrize("seed", [0])
def test_simulator_and_engine_agree_on_random_intent_sequences(server, client, pack, tmp_path, seed):
    tally: dict[str, int] = {"generated": 0, "agreed": 0}
    excluded: dict[str, int] = {}

    @settings(max_examples=60, deadline=None, derandomize=True, suppress_health_check=list(HealthCheck))
    @given(intent_lists(server.start_ms))
    def prop(intents):
        tally["generated"] += 1
        try:
            rev = workflow.review_intents(client, pack, intents, cache_run_dir=tmp_path)
        except ApiError as e:  # Unsupported is an ApiError too: same allow-list, same accounting
            # service errors carry an HTTP status or the failed run's id; the simulator's are raised before anything is sent
            side = "service" if (e.status is not None or e.job_id) else "simulator"
            allowed = EXPECTED_REJECTIONS if side == "service" else EXPECTED_LOCAL_REJECTIONS
            if e.code not in allowed:
                raise AssertionError(f"unexpected {side} {e.code} for {json.dumps(intents)}: {e.message}") from e
            excluded[f"{side}:{e.code}"] = excluded.get(f"{side}:{e.code}", 0) + 1
            event(f"excluded {side}:{e.code}")
            assume(False)
        rep = rev["report"]
        assert rep["passed"], json.dumps({"intents": intents, "first": rep["first_difference"], "layers": {k: v["status"] for k, v in rep["layers"].items()}}, indent=1)
        tally["agreed"] += 1
        event("agreed")

    prop()
    summary = {**tally, "excluded": excluded}
    print("differential summary:", json.dumps(summary, sort_keys=True))
    assert tally["agreed"] >= 5, f"too few sequences were accepted by both sides for the property to mean anything: {summary}"


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
