"""Generic Strategy IR policy: runs any ``alphakeel.strategy-ir/1`` document under ``PolicyHost``.

Parameters: ``{"ir": <IR document>, "qty_dp": <int, default 6>}``.

Each step:
1. reconcile the IR positions with the engine's REAL positions (``ctx["positions"]``): a pair whose legs are all gone is
   closed (its base goes to ``cooldowns`` at this step's time); an entry none of whose legs ever showed up did not fill
   and is dropped without a cooldown;
2. build the frame from the point-in-time reader (``ir_views``, README §4.1a) and call ``ir.decide`` (README §5);
3. exits -> one reduce-only ``limit_ioc`` per engine position of that pair (whatever actually filled, never an
   assumption that both legs did), at the touch (long closes sell at the bid, short closes buy at the ask);
   entries -> two ``limit_ioc`` intents: long leg buys at its ask, short leg sells at its bid, the same quantity on both
   legs: ``notional_per_leg / long.ask`` rounded DOWN to ``qty_dp`` places.

Intent naming (README §5a, the same as AlphaKeel's Rust ``ir_strategy`` run, so the research service's L2 layer can
compare decision by decision): ``intent_id = n<trade>-<L|S>-<open|close>``, ``position_ref = p<trade><L|S>``,
``pair_id = pair-<trade>``. ``trade`` counts from 1 the entries actually submitted, in ``entries`` order; an entry whose
leg has no real top-of-book quote (``bbo = false``) or whose quantity rounds to zero submits nothing and takes no number
(Rust ``paper::open_planned`` refuses the same entries). v1 legs are USDT only, so the "no FX rate" refusal never applies.

``net_now`` of a held pair is the sum of its two legs' ``ctx["positions"][].net_now`` (the engine's per-leg close-now net,
README §5a); it is ``None`` when either leg's value is ``None`` (no quote for that contract in this frame) or either leg
is not (or no longer) an engine position. ``exit.max_loss`` / ``exit.take_profit`` therefore fire exactly when the
engine's state says so.

Only ``assumptions.funding_estimate = "predicted"`` can be served (the pack's scan observations carry the venues'
current/predicted rates); ``last_settled`` is refused at ``initialize`` instead of being silently replaced (README §1).
"""

from decimal import ROUND_DOWN, Decimal

from alphakeel_research import ir as IR
from alphakeel_research import ir_views
from alphakeel_research.errors import Unsupported


def initialize(parameters):
    doc = parameters["ir"]
    loaded = IR.load(doc)
    if loaded["assumptions"]["funding_estimate"] != "predicted":
        raise Unsupported("assumptions.funding_estimate = last_settled cannot be served by the scan-frame view (it holds the "
                          "venues' current/predicted funding rates)")
    qty_dp = parameters.get("qty_dp", 6)
    if isinstance(qty_dp, bool) or not isinstance(qty_dp, int) or not 0 <= qty_dp <= 18:
        raise ValueError("qty_dp must be an integer in 0..=18")
    return {"ir": loaded.doc, "strategy_id": loaded.strategy_id, "qty_dp": qty_dp, "positions": {}, "cooldowns": {}, "next_trade": 1}


def _reconcile(state, engine, t):
    positions, cooldowns = dict(state["positions"]), dict(state["cooldowns"])
    for key in sorted(positions):
        p = dict(positions[key])
        live = [r for r in p["refs"].values() if r in engine]
        if live:
            p["seen"] = True
            positions[key] = p
        elif p["seen"]:
            del positions[key]
            cooldowns[IR.ascii_upper(p["base"])] = t
        else:
            del positions[key]  # the entry never filled on either leg: nothing was held, no cooldown
    return positions, cooldowns


def _price(touch, pit, inst, side):
    q = touch.get((inst["venue"], inst["market"], inst["symbol"]))
    if q is not None:
        return q[0] if side == "bid" else q[1]
    from alphakeel_research.packfile import Inst

    last = pit.latest_quote(Inst(inst["venue"], inst["market"], inst["symbol"]))
    if last is None:
        return None
    return IR.fmt(last.bid if side == "bid" else last.ask)


def _pair_net_now(p, engine):
    """README §5a: the pair's close-now net = the sum of its legs' engine ``net_now``; unknown if either leg's is."""
    total = Decimal(0)
    for leg in ("long", "short"):
        ep = engine.get(p["refs"][leg])
        if ep is None or ep.get("net_now") is None:
            return None
        total += Decimal(ep["net_now"])
    return IR.fmt(total)


def on_step(ctx, state):
    t = ctx["time_ms"]
    engine = {p["position_ref"]: p for p in ctx["positions"]}
    positions, cooldowns = _reconcile(state, engine, t)
    legs = ir_views.frame_legs(ctx.pit, None, t)
    pairs = ir_views.pairs_of_legs(legs)
    touch = ir_views.touch(legs)
    views = [{"id": k, "base": p["base"], "long": {"venue": p["long"]["venue"], "symbol": p["long"]["symbol"]},
              "short": {"venue": p["short"]["venue"], "symbol": p["short"]["symbol"]}, "opened_ms": p["opened_ms"],
              "net_now": _pair_net_now(p, engine)}
             for k, p in sorted(positions.items())]
    decision = IR.decide(IR.Ir(state["ir"]), {"pairs": pairs}, {"positions": views, "cooldowns": cooldowns}, t)

    intents = []
    for x in decision["exits"]:
        p = positions[x["position_id"]]
        for leg in ("long", "short"):
            ref = p["refs"][leg]
            ep = engine.get(ref)
            if ep is None:
                continue
            is_long = ep["side"] == "long"
            px = _price(touch, ctx.pit, ep["instrument"], "bid" if is_long else "ask")
            if px is None:
                continue  # no quote to price the close at: the exit is decided again on the next step
            intents.append({"intent_id": f"n{p['trade']}-{'L' if leg == 'long' else 'S'}-close", "decision_time_ms": t, "instrument": ep["instrument"],
                            "side": "sell" if is_long else "buy", "qty": ep["qty"], "order_type": "limit_ioc", "limit_price": px,
                            "reduce_only": True, "position_ref": ref, "pair_id": x["position_id"]})

    by_id = {p["id"]: p for p in pairs}
    step = Decimal(1).scaleb(-state["qty_dp"])
    n = state["next_trade"]
    for e in decision["entries"]:
        pair = by_id[e["pair_id"]]
        if not (pair["long"]["bbo"] and pair["short"]["bbo"]):
            continue  # no real top-of-book on a leg: not submitted, takes no trade number (README §5a)
        qty = (Decimal(e["notional_per_leg"]) / Decimal(pair["long"]["ask"])).quantize(step, rounding=ROUND_DOWN)
        if qty <= 0:
            continue
        trade, n = n, n + 1
        key, rl, rs = f"pair-{trade}", f"p{trade}L", f"p{trade}S"
        li = {"venue": pair["long"]["venue"], "market": pair["long"]["market"], "symbol": pair["long"]["symbol"]}
        si = {"venue": pair["short"]["venue"], "market": pair["short"]["market"], "symbol": pair["short"]["symbol"]}
        q = IR.fmt(qty)
        intents.append({"intent_id": f"n{trade}-L-open", "decision_time_ms": t, "instrument": li, "side": "buy", "qty": q,
                        "order_type": "limit_ioc", "limit_price": pair["long"]["ask"], "reduce_only": False, "position_ref": rl, "pair_id": key})
        intents.append({"intent_id": f"n{trade}-S-open", "decision_time_ms": t, "instrument": si, "side": "sell", "qty": q,
                        "order_type": "limit_ioc", "limit_price": pair["short"]["bid"], "reduce_only": False, "position_ref": rs, "pair_id": key})
        positions[key] = {"pair_id": pair["id"], "base": pair["base"], "long": li, "short": si, "opened_ms": t,
                          "trade": trade, "refs": {"long": rl, "short": rs}, "seen": False}

    new_state = dict(state, positions=positions, cooldowns=cooldowns, next_trade=n)
    return {"intents": intents, "state": new_state}
