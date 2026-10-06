"""Fixture policy that depends on execution feedback: if the large opening order was refused (denied by the engine, or not
submitted by the same-instant margin check), retry with a smaller one."""

BIN = {"venue": "binance", "market": "perp", "symbol": "BTCUSDT"}


def initialize(parameters):
    return {"big": parameters["big"], "small": parameters["small"], "phase": "try_big", "close_at_ms": parameters["close_at_ms"], "attempts": 0}


def on_step(ctx, state):
    t = ctx["time_ms"]
    st = dict(state)
    intents = []
    denied = [r for r in ctx["last_results"] if r["status"] in ("denied", "rejected", "not_submitted")]
    if st["phase"] == "try_big":
        intents = [{"intent_id": "i-big", "decision_time_ms": t, "instrument": BIN, "side": "buy", "qty": st["big"], "order_type": "limit_ioc",
                    "limit_price": "1", "reduce_only": False, "position_ref": "big"}]
        st["phase"] = "wait_big"
        st["attempts"] += 1
    elif st["phase"] == "wait_big":
        if denied:
            intents = [{"intent_id": "i-small", "decision_time_ms": t, "instrument": BIN, "side": "buy", "qty": st["small"], "order_type": "limit_ioc",
                        "limit_price": "1", "reduce_only": False, "position_ref": "small"}]
            st["phase"] = "holding_small"
            st["attempts"] += 1
        else:
            st["phase"] = "holding_big"
    elif t == st["close_at_ms"]:
        for p in ctx["positions"]:
            intents.append({"intent_id": f"i-close-{p['position_ref']}", "decision_time_ms": t, "instrument": p["instrument"], "side": "sell", "qty": p["qty"],
                            "order_type": "limit_ioc", "limit_price": "0.999", "reduce_only": True, "position_ref": p["position_ref"]})
    return {"intents": intents, "state": st}
