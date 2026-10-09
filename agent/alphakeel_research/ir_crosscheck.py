"""Cross-engine check of a long-history IR backtest (``ir backtest --alphakeel-check``).

The VT dataset backtest (``ir_backtest.run``: Python Decimal simulator on the ``kline_close_synthetic_bbo`` view) and
AlphaKeel's engine (``ir_strategy`` run with a ``dataset_view`` on the SAME dataset pack: the Rust IR evaluator decides
on frames synthesised by the same view rules and NautilusTrader executes, funding from the pack's official settlements)
are two independent implementations of one strategy on one input. This module submits the AlphaKeel run with exactly
the parameters of a finished VT run (pack, decision window, step, fee table, declared instruments), fetches its result
and event log, and compares trade by trade:

* the set of trades (``n<k>`` numbering, README §5a) and, per trade, the pair, open/close decision times, quantity,
  entry/exit prices, fees and funding per leg (``position_ref`` ``p<k>L|S``), realised net;
* the totals (realised, price P&L, funding, fees).

Amounts must agree to ``TOLERANCE`` per amount (both sides round each amount to 1e-8). A difference is a finding, not a
failure to hide: the report lists the first ones. The engine fills IOC at the synthetic touch, so on this view the two
sides should agree exactly; known structural differences (the engine marks funding at the last FRAME's price, the
simulator at the last 1m bar's close -- equal when frames sit on the settlement grid) are stated in the report.
"""

from __future__ import annotations

import hashlib
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import canon

SCHEMA = "vibe-trading.ir-crosscheck/1"
TOLERANCE = Decimal("0.00000002")
NOTES = [
    "both sides use the kline_close_synthetic_bbo view on the same frozen dataset pack: same input, independent code",
    "AlphaKeel marks a funding settlement at the last frame's mark at or before it; the simulator at the last 1m bar's "
    "close; they are the same price when decision frames sit on the settlement grid (start aligned, step divides the interval)",
    "the engine checks same-instant cumulative initial margin (profile leverage) and may refuse an entry the simulator, "
    "which models no margin, makes: such a trade appears on one side only",
]


def view_of(card: dict) -> dict:
    """The ``dataset_view`` of the AlphaKeel run that mirrors a VT run card."""
    venues = sorted({i["venue"] for i in card["instruments"]})
    fees = card["assumptions"]["fees"]
    return {
        "view": card["view"], "start_ms": card["window"]["start_ms"], "end_ms": card["window"]["end_ms"],
        "step_minutes": card["step_minutes"],
        "fees": {v: {"perp": fees[v]["perp"]} for v in venues},
        "instruments": [{k: i[k] for k in ("venue", "market", "symbol", "base", "quote")} for i in card["instruments"]],
    }


def _jsonl(b: bytes) -> list[dict]:
    return [json.loads(x) for x in b.decode().splitlines() if x.strip()]


def ak_trades(events: list[dict]) -> dict[int, dict]:
    """``trade -> {legs: {L|S: {...}}, opened_ms, closed_ms}`` (decision times of the fills) from AlphaKeel's event log (intent ids ``n<k>-L|S-open|close``)."""
    out: dict[int, dict] = {}
    for e in events:
        kind, iid, pref = e.get("kind"), e.get("intent_id"), e.get("position_ref")
        if kind == "fill" and iid and iid.startswith("n"):
            n_s, leg, act = iid[1:].split("-")
            t = out.setdefault(int(n_s), {"legs": {}})
            lg = t["legs"].setdefault(leg, {"funding": Decimal(0), "fees": Decimal(0)})
            d = e["data"]
            lg["instrument"] = e.get("instrument")
            lg["fees"] += Decimal(d["fee"])
            if act == "open":
                lg["qty"], lg["entry_price"] = Decimal(d["qty"]), Decimal(d["price"])
                t.setdefault("opened_ms", int(e["time_ms"]))
            else:
                lg["exit_price"] = Decimal(d["price"])
                t["closed_ms"] = int(e["time_ms"])
        elif kind == "funding" and pref and pref.startswith("p"):
            n, leg = int(pref[1:-1]), pref[-1]
            lg = out.setdefault(n, {"legs": {}})["legs"].setdefault(leg, {"funding": Decimal(0), "fees": Decimal(0)})
            lg["funding"] += Decimal(e["data"]["amount"])
    return out


def vt_trades(run_dir: Path) -> dict[int, dict]:
    out = {}
    for t in (json.loads(x) for x in (run_dir / "trades.jsonl").read_text().splitlines() if x.strip()):
        legs = {}
        for k, leg in (("L", t["long"]), ("S", t["short"])):
            legs[k] = {"instrument": leg["instrument"], "qty": Decimal(leg["qty"]), "entry_price": Decimal(leg["entry_price"]),
                       "exit_price": None if leg["exit_price"] is None else Decimal(leg["exit_price"]),
                       "fees": Decimal(leg["entry_fee"]) + Decimal(leg["exit_fee"]), "funding": Decimal(leg["funding"])}
        out[t["trade"]] = {"legs": legs, "opened_ms": t["opened_ms"], "closed_ms": t["closed_ms"]}
    return out


def compare(vt: dict[int, dict], ak: dict[int, dict]) -> dict:
    diffs: list[dict] = []
    matched = 0
    for n in sorted(set(vt) | set(ak)):
        a, b = vt.get(n), ak.get(n)
        if a is None or b is None:
            diffs.append({"trade": n, "field": "presence", "vt": a is not None, "alphakeel": b is not None})
            continue
        ok = True
        ak_open = b.get("opened_ms")
        if ak_open != a["opened_ms"]:
            ok = False
            diffs.append({"trade": n, "field": "opened_ms", "vt": a["opened_ms"], "alphakeel": ak_open})
        ak_close = b.get("closed_ms")
        if ak_close != a["closed_ms"]:
            ok = False
            diffs.append({"trade": n, "field": "closed_ms", "vt": a["closed_ms"], "alphakeel": ak_close})
        for leg in ("L", "S"):
            x, y = a["legs"].get(leg, {}), b["legs"].get(leg, {})
            for f in ("qty", "entry_price", "exit_price", "fees", "funding"):
                u, v = x.get(f), y.get(f)
                if u is None and v is None:
                    continue
                if u is None or v is None or abs(u - v) > TOLERANCE:
                    ok = False
                    diffs.append({"trade": n, "leg": leg, "field": f, "vt": None if u is None else str(u),
                                  "alphakeel": None if v is None else str(v)})
        matched += ok
    return {"trades_vt": len(vt), "trades_alphakeel": len(ak), "trades_matched": matched,
            "differences": len(diffs), "first_differences": diffs[:30]}


def alphakeel_check(client: Any, ir_doc: dict, run_dir: str | Path, *, timeout: float = 7200.0) -> dict:
    """Run the mirrored AlphaKeel ``ir_strategy`` + ``dataset_view`` run and compare; writes ``alphakeel-check.json``."""
    run_dir = Path(run_dir)
    card = json.loads((run_dir / "run_card.json").read_text())
    body = {"mode": "ir_strategy", "pack_id": card["pack"]["pack_id"], "ir": ir_doc, "dataset_view": view_of(card)}
    key = "vt-xcheck-" + hashlib.sha256(canon.canonical_bytes(body)).hexdigest()[:32]
    submitted = int(time.time() * 1000)
    run = client.create_run(body, key=key)
    st = client.wait_run(run["run_id"], timeout=timeout)
    # The key is content-derived: after a failed run (e.g. on an older service build) it would return that failed run
    # forever. A run that had already ended before this call is retried once as its child (``parent_run_id``).
    if st.get("status") in ("failed", "interrupted", "cancelled") and int(st.get("created_ms") or submitted) < submitted:
        child = {**body, "parent_run_id": run["run_id"]}
        run = client.create_run(child, key=key + "-retry-" + run["run_id"][-12:])
        st = client.wait_run(run["run_id"], timeout=timeout)
    doc: dict = {"schema": SCHEMA, "vt_run_id": card["run_id"], "strategy_id": card["strategy_id"],
                 "alphakeel_run_id": run["run_id"], "alphakeel_status": st.get("status"), "notes": NOTES}
    if st.get("status") != "complete":
        doc.update(passed=False, error=st.get("error"))
    else:
        res = json.loads(client.artifact(run["run_id"], "result.json"))
        ev = _jsonl(client.artifact(run["run_id"], "events.jsonl"))
        cmp = compare(vt_trades(run_dir), ak_trades(ev))
        # like for like: AlphaKeel's result amounts cover CLOSED trades (positions still open at the end are valued in
        # equity, not in realized/fees/funding); the run card's fees/funding also include the open positions'.
        closed = [json.loads(x) for x in (run_dir / "trades.jsonl").read_text().splitlines() if x.strip()]
        closed = [t for t in closed if t["status"] == "closed"]
        legs = [t[k] for t in closed for k in ("long", "short")]
        vt_tot = {"realized": card["amounts"]["realized"],
                  "funding": str(sum((Decimal(x["funding"]) for x in legs), Decimal(0))),
                  "fees": str(sum((Decimal(x["entry_fee"]) + Decimal(x["exit_fee"]) for x in legs), Decimal(0)))}
        tot = {k: {"vt": vt_tot[k], "alphakeel": res["amounts"][k], "scope": "closed trades"} for k in ("realized", "funding", "fees")}
        tot_ok = all(abs(Decimal(v["vt"]) - Decimal(v["alphakeel"])) <= TOLERANCE * max(1, cmp["trades_vt"] * 6) for v in tot.values())
        doc.update(comparison=cmp, totals=tot, alphakeel_verification=res.get("verification"),
                   alphakeel_dataset_view=res.get("dataset_view", {}).get("instants"),
                   passed=cmp["differences"] == 0 and tot_ok)
    (run_dir / "alphakeel-check.json").write_text(json.dumps(doc, indent=2, sort_keys=True, default=str) + "\n")
    if "comparison" in doc:
        rec = reconciliation_doc(doc, card, res)
        (run_dir / "reconciliation.json").write_text(json.dumps(rec, indent=2, sort_keys=True, default=str) + "\n")
    return doc


PASS_SCOPE = ("dataset view kline_close_synthetic_bbo: the Python simulator and AlphaKeel's engine (Rust IR evaluator + "
              "NautilusTrader) agree trade by trade on the same frozen dataset pack; synthetic touch (spread 0, no depth), "
              "last-settled funding as the decision rate -- not a scan-frame reconciliation; the strategy is not certified")


def reconciliation_doc(check: dict, card: dict, res: dict) -> dict:
    """The cross-check as a ``vibe-trading.ir-reconciliation/1`` file (``arb strategy reconcile --from-file``): L0 = same
    pack and view, L2 = the trade set and decision times, L3 = per-leg quantities, prices, fees and funding, L4 = closed
    totals. Same honesty as ``ir review``: AlphaKeel's registry records it as research-supplied, never as a verdict."""
    cmp = check["comparison"]
    timing = [d for d in cmp["first_differences"] if d.get("field") in ("presence", "opened_ms", "closed_ms")]
    amounts = [d for d in cmp["first_differences"] if d not in timing]
    tot_ok = all(abs(Decimal(v["vt"]) - Decimal(v["alphakeel"])) <= TOLERANCE * max(1, cmp["trades_vt"] * 6)
                 for v in check["totals"].values())
    st = lambda bad: "mismatch" if bad else "matched"  # noqa: E731
    return {
        "schema": "vibe-trading.ir-reconciliation/1",
        "comparison": "xchk-" + hashlib.sha256(f"{card['run_id']}|{check['alphakeel_run_id']}".encode()).hexdigest()[:24],
        "strategy_id": card["strategy_id"], "pack_id": card["pack"]["pack_id"],
        "local_run": card["run_id"], "alphakeel_run": check["alphakeel_run_id"],
        "layers": {"L0": "matched", "L1": "not_applicable", "L2": st(timing or cmp["trades_vt"] != cmp["trades_alphakeel"]),
                   "L3": st(amounts), "L4": st(not tot_ok)},
        "first_difference": (cmp["first_differences"] or [None])[0],
        "passed": bool(check.get("passed")), "pass_scope": PASS_SCOPE,
        "notes": NOTES + [f"trades: {cmp['trades_vt']} (vt) / {cmp['trades_alphakeel']} (alphakeel), matched {cmp['trades_matched']}"],
        "summary": {
            "python_local": {"run_id": card["run_id"], "mode": "ir_backtest_dataset_view", "side": "python_local",
                             "strategy_id": card["strategy_id"], "amounts": check["totals"] and {k: v["vt"] for k, v in check["totals"].items()},
                             "counts": card["counts"]},
            "alphakeel": {"run_id": check["alphakeel_run_id"], "mode": "ir_strategy", "side": "alphakeel_engine",
                          "strategy_id": res.get("strategy_id"), "amounts": res.get("amounts"), "counts": res.get("counts"),
                          "verification": res.get("verification")},
        },
        "source": "ir crosscheck",
    }
