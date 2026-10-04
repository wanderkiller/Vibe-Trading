"""Fixture policy: open a hedged pair on the first step, close whatever the engine says is open at `close_at_ms`."""

BIN = {"venue": "binance", "market": "perp", "symbol": "BTCUSDT"}
OKX = {"venue": "okx", "market": "perp", "symbol": "BTC-USDT-SWAP"}


def initialize(parameters):
    return {"qty": parameters["qty"], "close_at_ms": parameters["close_at_ms"], "opened": False}


def on_step(ctx, state):
    t = ctx["time_ms"]
    intents = []
    if not state["opened"]:
        intents = [
            {"intent_id": "i-open-l", "decision_time_ms": t, "instrument": BIN, "side": "buy", "qty": state["qty"], "order_type": "limit_ioc",
             "limit_price": "1", "reduce_only": False, "position_ref": "pl", "pair_id": "pair-1"},
            {"intent_id": "i-open-s", "decision_time_ms": t, "instrument": OKX, "side": "sell", "qty": state["qty"], "order_type": "limit_ioc",
             "limit_price": "1", "reduce_only": False, "position_ref": "ps", "pair_id": "pair-1"},
        ]
        state = dict(state, opened=True)
    elif t == state["close_at_ms"]:
        for p in ctx["positions"]:  # the engine's real positions, not an assumption that both legs filled
            long = p["side"] == "long"
            intents.append({"intent_id": f"i-close-{p['position_ref']}", "decision_time_ms": t, "instrument": p["instrument"], "side": "sell" if long else "buy",
                            "qty": p["qty"], "order_type": "limit_ioc", "limit_price": "0.999" if long else "1.001", "reduce_only": True,
                            "position_ref": p["position_ref"], "pair_id": "pair-1"})
    return {"intents": intents, "state": state}
