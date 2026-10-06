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
    """Random intent sequences that stay mostly inside what both sides model (review F-P2-3: 294 of 504 generated sequences
    were discarded as reduce_only_needs_position, so only 60 were compared). A reduce-only intent is generated only against
    an earlier opening intent of the sequence — same contract and position_ref, opposite side, a later frame, at most the
    opened quantity; with no open position the row becomes an opening intent. A position_ref is never re-bound to another
    contract/side, and spot is opened long only. Rejections can still happen (margin, an IOC that does not cross) and are
    accounted for, never counted as agreement."""
    one = st.fixed_dictionaries({
        "k": st.integers(0, 59),
        "inst": st.sampled_from([BIN, OKX, SPOT]),
        "side": st.sampled_from(["buy", "sell"]),
        # around the 10 000 per-account balance too: 4999 + 4999 fits, 9950 sits in the maintenance band after a fill
        "qty": st.sampled_from(["1", "10", "33.333", "0.5", "250", "4999", "9950", "9999", "10001", "0.00000001", "0.000000019"]),
        "kind": st.sampled_from(["market", "ioc_cross", "ioc_away"]),
        "reduce": st.booleans(),
        "pick": st.integers(0, 7),
        "ref": st.sampled_from([None, "p0", "p1"]),
    })

    def price(kind, side):
        if kind == "ioc_cross":
            return "1.5" if side == "buy" else "0.5"
        if kind == "ioc_away":
            return "0.5" if side == "buy" else "1.5"
        return None

    def build(rows):
        out = []
        bound: dict[str, tuple[str, str]] = {}  # position_ref -> (instrument key, side)
        held: list[dict] = []  # opening intents that can be reduced: {inst, side, qty, ref, k}
        for i, r in enumerate(sorted(rows, key=lambda r: r["k"])):
            k, inst, side, qty, kind, ref = r["k"], r["inst"], r["side"], r["qty"], r["kind"], r["ref"]
            candidates = [h for h in held if h["k"] < k]
            if r["reduce"] and candidates:
                h = candidates[r["pick"] % len(candidates)]
                q = min(Decimal(qty), h["qty"])
                h["qty"] -= q
                if h["qty"] <= 0:
                    held.remove(h)
                rside = "sell" if h["side"] == "buy" else "buy"
                out.append({"intent_id": f"i{i}", "decision_time_ms": start_ms + 60_000 * k, "instrument": h["inst"], "side": rside,
                            "qty": format(q, "f"), "order_type": "market" if kind == "market" else "limit_ioc",
                            "limit_price": price(kind, rside), "reduce_only": True, "position_ref": h["ref"]})
                continue
            if inst is SPOT:
                side = "buy"  # spot cannot be sold short
            key = json.dumps(inst, sort_keys=True)
            if ref is not None and bound.setdefault(ref, (key, side)) != (key, side):
                ref = None
            out.append({"intent_id": f"i{i}", "decision_time_ms": start_ms + 60_000 * k, "instrument": inst, "side": side, "qty": qty,
                        "order_type": "market" if kind == "market" else "limit_ioc", "limit_price": price(kind, side),
                        "reduce_only": False, "position_ref": ref})
            if kind != "ioc_away" and (ref is not None or inst is SPOT):
                held.append({"inst": inst, "side": side, "qty": Decimal(qty), "ref": ref, "k": k})
        return out

    return st.lists(one, min_size=1, max_size=5).map(build)


@pytest.mark.parametrize("seed", [0])
def test_simulator_and_engine_agree_on_random_intent_sequences(server, client, pack, tmp_path, seed):
    tally: dict[str, int] = {"generated": 0, "agreed": 0}
    excluded: dict[str, int] = {}

    @settings(max_examples=320, deadline=None, derandomize=True, suppress_health_check=list(HealthCheck))
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
    # The property means something only if most generated sequences are actually compared (review F-P2-3: 60 of 504).
    assert tally["agreed"] >= 200, f"too few sequences were accepted by both sides for the property to mean anything: {summary}"


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


def _statuses(rev):
    """order_result status per intent id from the local (Python) evidence of a review."""
    out = {}
    for line in (open(f"{rev['run_dir']}/events.jsonl", encoding="utf-8").read().splitlines()[1:]):
        e = json.loads(line)
        if e["kind"] == "order_result":
            out[e["intent_id"]] = (e["data"]["status"], e["data"]["reason_code"], e["order_id"])
    return out


def _open(iid, t, inst, side, qty, ref):
    return {"intent_id": iid, "decision_time_ms": t, "instrument": inst, "side": side, "qty": qty, "order_type": "market",
            "limit_price": None, "reduce_only": False, "position_ref": ref}


def test_same_instant_opening_orders_are_checked_cumulatively_per_account_like_the_engine(server, client, pack, tmp_path):
    t = server.start_ms
    # 10 000 per venue account. Leverage 1: 6000 fits, the next 5000 (cumulative 11 000) is not submitted and is not counted,
    # so a later 4000 (6000 + 4000 = 10 000) still fits; the OKX account is separate and unaffected.
    intents = [_open("a", t, BIN, "buy", "6000", "pa"), _open("b", t, BIN, "buy", "5000", "pb"), _open("c", t, BIN, "buy", "4000", "pc"),
               _open("d", t, OKX, "sell", "6000", "pd")]
    rev = workflow.review_intents(client, pack, intents, profile={"leverage_perp": "1"}, cache_run_dir=tmp_path)
    assert rev["report"]["passed"], json.dumps(rev["report"]["first_difference"], indent=1)
    st = _statuses(rev)
    assert st["a"][0] == "filled" and st["c"][0] == "filled" and st["d"][0] == "filled"
    assert st["b"] == ("not_submitted", "not_submitted", None), "a refused order never reached the engine: no order id"


def test_perpetual_initial_margin_is_notional_over_leverage_in_the_same_instant_check(server, client, pack, tmp_path):
    t = server.start_ms
    # leverage 2: 6000 / 2 + 5000 / 2 = 5500 <= 10 000, so both fit (the engine's own per-order check still sees 6000 and 5000 each)
    intents = [_open("a", t, BIN, "buy", "6000", "pa"), _open("b", t, BIN, "buy", "5000", "pb")]
    rev = workflow.review_intents(client, pack, intents, profile={"leverage_perp": "2"}, cache_run_dir=tmp_path)
    assert rev["report"]["passed"], json.dumps(rev["report"]["first_difference"], indent=1)
    assert {k: v[0] for k, v in _statuses(rev).items()} == {"a": "filled", "b": "filled"}


def test_the_profile_is_v2_and_a_document_without_the_field_is_the_retired_per_order_rule(client, pack):
    prof = client.render_profile(pack.id, "fixed_intent_replay")["profile"]
    assert prof["profile_id"] == "ak-top-of-book-ioc-taker-v2"
    assert prof["account"]["same_instant_margin"] == "cumulative_initial_margin"
    from alphakeel_research.sim import Simulator
    from alphakeel_research.packfile import Inst

    legacy = json.loads(json.dumps(prof))
    del legacy["account"]["same_instant_margin"]
    assert Simulator(pack, legacy, [Inst.parse(BIN)], run_id="legacy").margin_rule == "per_order_engine"
    assert Simulator(pack, prof, [Inst.parse(BIN)], run_id="v2").margin_rule == "cumulative_initial_margin"
