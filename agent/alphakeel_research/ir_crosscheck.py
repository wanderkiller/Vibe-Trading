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
sides should agree exactly. Funding is priced by one rule on both sides (the settlement's published mark, AlphaKeel F04);
the known structural differences are stated in the report (``NOTES``).
"""

from __future__ import annotations

import hashlib
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import canon
from .errors import ApiError

SCHEMA = "vibe-trading.ir-crosscheck/1"
TOLERANCE = Decimal("0.00000002")
NOTES = [
    "both sides use the kline_close_synthetic_bbo view on the same frozen dataset pack: same input, independent code",
    "funding settlements are booked at the settlement row's published mark price on both sides (AlphaKeel F04, docs/37 "
    "'settlement notional price'; ir_views.settlement_price); only a row without one falls back to the last price at or "
    "before it (AlphaKeel: the last frame's mark; the simulator: the last 1m close within 5 minutes), the same price when "
    "decision frames sit on the settlement grid (start aligned, step divides the interval)",
    "the engine checks same-instant cumulative initial margin (profile leverage) and may refuse an entry the simulator, "
    "which models no margin, makes: AlphaKeel then voids that entry (no position, no cooldown, its number skipped; "
    "Strategy IR README §5a) and the trade appears on the simulator's side only",
    "trade-by-trade here, not the service's /comparisons: that endpoint compares two service runs by their event logs "
    "under one execution profile; the simulator is a local run that models neither margin nor the profile and writes no "
    "event log. L0 uses the same definition as /comparisons for dataset-view runs (run-manifest.inputs chains)",
    "quantities: both sides decide with the IR quantity (8 places) and execute it truncated to the engine's exact-accounting "
    "precision (Strategy IR README §5a; the simulator applies the same cap), so the quantity of every leg must be equal",
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


# L0 of the comparison: the tables a dataset-view run reads (AlphaKeel ``dataset_view::captures``: per declared contract
# its ``kline_1m`` rows from the pack window's start and its official settlements). The SAME definition as the service's
# ``/comparisons`` L0 for two dataset-view runs (Strategy IR README §5a): ``run-manifest.inputs`` entries, one per
# (table, contract), equal in range, row count and row-digest chain.
L0_TABLES = ("kline_1m", "settlements")


def compare_inputs(vt_inputs: list[dict], ak_inputs: list[dict]) -> dict:
    """``run-manifest.inputs`` of both sides restricted to ``L0_TABLES``; every (table, contract) must be on both sides
    with the same ``first_ms``/``last_ms``/``rows``/``chain_sha256``."""
    def index(xs: list[dict]) -> dict[str, dict]:
        return {f"{a['table']} {json.dumps(a.get('instrument'), sort_keys=True)}": a for a in xs
                if a["table"] in L0_TABLES and a.get("rows")}  # an empty read (AlphaKeel records it, VT does not) is no input

    vt, ak = index(vt_inputs), index(ak_inputs)
    diffs = []
    for key in sorted(set(vt) | set(ak)):
        a, b = vt.get(key), ak.get(key)
        if a is None or b is None:
            diffs.append({"input": key, "field": "presence", "vt": a is not None, "alphakeel": b is not None})
            continue
        for f in ("first_ms", "last_ms", "rows", "chain_sha256"):
            if a.get(f) != b.get(f):
                diffs.append({"input": key, "field": f, "vt": a.get(f), "alphakeel": b.get(f)})
    return {"status": "mismatch" if diffs or not vt else "matched", "inputs": len(vt), "differences": diffs[:30]}


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


#: Reconciliation (``ir crosscheck`` / ``ir review``, L0-L4) tests that the two engines AGREE, not that fills are realistic:
#: it asks AlphaKeel for the one-level full-fill model with no synthetic spread and no capital cost, the Python simulator's own
#: model (docs/37 4b). Trial registration and the battery's registered runs use the server default (conservative) instead.
RECONCILIATION_FILL = {"fill_model": "top_of_book", "spread_floor_bps": "0", "capital_cost_annual": "0"}


def with_reconciliation_fill(profile: dict | None) -> dict:
    """``profile`` plus :data:`RECONCILIATION_FILL`; a given value that contradicts it is refused (the comparison would not be like for like)."""
    for k, v in RECONCILIATION_FILL.items():
        given = (profile or {}).get(k)
        if given is not None and (str(given) != v if k == "fill_model" else Decimal(str(given)) != Decimal(v)):
            raise ApiError("request.invalid", f"reconciliation compares the engines on the one-level full-fill model: profile.{k} = {given!r} "
                           f"contradicts {v!r}", field=f"profile.{k}")
    return {**(profile or {}), **RECONCILIATION_FILL}


#: Version of the service's run-assumption record this client expects (``result.run_assumptions``: v3 = protocol 2.3, with
#: ``fees_perp_taker`` / ``leverage_perp`` / ``funding_mode``). It is part of the trial-registration idempotency key, so a run
#: made by an older service build (no ``run_assumptions``, 2 bp spread floor) is never handed back for the same body
#: (review round 2, E: AlphaKeel refuses to bind such a run, and the content-derived key would return it forever).
ASSUMPTIONS_VERSION = "run-assumptions/3"


def trial_key_salt() -> str:
    """What the trial-registration key depends on besides the request body: the run-assumption version and the packaged protocol version."""
    from . import precheck

    return f"{ASSUMPTIONS_VERSION}|protocol {precheck.load_protocol()['version']}"


def mirror_request(ir_doc: dict, card: dict, *, profile: dict | None = None, key_salt: str = "") -> tuple[dict, str]:
    """(body, idempotency key) of the AlphaKeel ``ir_strategy`` + ``dataset_view`` run that mirrors a VT run card: the
    cross-check passes ``profile`` = :data:`RECONCILIATION_FILL`, trial registration none (the server's conservative default).
    ``key_salt`` is mixed into the key (trial registration passes :func:`trial_key_salt`; the reconciliation key stays body-only)."""
    body = {"mode": "ir_strategy", "pack_id": card["pack"]["pack_id"], "ir": ir_doc, "dataset_view": view_of(card)}
    if profile is not None:
        body["profile"] = profile
    digest = hashlib.sha256(canon.canonical_bytes(body) + (b"\n" + key_salt.encode() if key_salt else b"")).hexdigest()[:32]
    return body, "vt-xcheck-" + digest


def register_trial(client: Any, ir_doc: dict, card: dict, *, queue_wait_s: float = 600.0, poll_s: float = 5.0) -> str:
    """Register one research run in the research service's trial ledger and return the service run id. The service writes a
    ``research_run`` ledger row for every ``POST /runs`` it accepts (docs/29 s14, ``record_trial``; the same request digest is
    recorded once) and refuses the run when the row cannot be written, so creating the mirrored ``ir_strategy`` run IS the
    registration; this does not wait for the run to finish. A full run queue is waited out; any other error propagates (a trial
    that could not be registered must not be used as evidence)."""
    body, key = mirror_request(ir_doc, card, key_salt=trial_key_salt())
    deadline = time.monotonic() + queue_wait_s
    while True:
        try:
            return str(client.create_run(body, key=key)["run_id"])
        except ApiError as e:
            if e.code != "resource.queue_full" or time.monotonic() >= deadline:
                raise
            time.sleep(poll_s)


def alphakeel_check(client: Any, ir_doc: dict, run_dir: str | Path, *, timeout: float = 7200.0) -> dict:
    """Run the mirrored AlphaKeel ``ir_strategy`` + ``dataset_view`` run and compare; writes ``alphakeel-check.json``."""
    run_dir = Path(run_dir)
    card = json.loads((run_dir / "run_card.json").read_text())
    body, key = mirror_request(ir_doc, card, profile=with_reconciliation_fill(None))
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
        manifest = json.loads(client.artifact(run["run_id"], "run-manifest.json"))
        l0 = compare_inputs(card["inputs"], manifest.get("inputs") or [])
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
        doc.update(comparison=cmp, totals=tot, inputs=l0, alphakeel_verification=res.get("verification"),
                   alphakeel_dataset_view=res.get("dataset_view", {}).get("instants"),
                   passed=cmp["differences"] == 0 and tot_ok and l0["status"] == "matched")
    (run_dir / "alphakeel-check.json").write_text(json.dumps(doc, indent=2, sort_keys=True, default=str) + "\n")
    if "comparison" in doc:
        rec = reconciliation_doc(doc, card, res)
        (run_dir / "reconciliation.json").write_text(json.dumps(rec, indent=2, sort_keys=True, default=str) + "\n")
    return doc


PASS_SCOPE = ("dataset view kline_close_synthetic_bbo: the Python simulator and AlphaKeel's engine (Rust IR evaluator + "
              "NautilusTrader) agree trade by trade on the same frozen dataset pack; synthetic touch (spread 0, no depth), "
              "last-settled funding as the decision rate -- not a scan-frame reconciliation; the strategy is not certified")


def reconciliation_doc(check: dict, card: dict, res: dict) -> dict:
    """The cross-check as a ``vibe-trading.ir-reconciliation/1`` file (``arb strategy reconcile --from-file``): L0 = the
    same input rows (``compare_inputs``: both sides' ``run-manifest.inputs`` chains of klines and settlements), L2 = the trade set and decision times, L3 = per-leg quantities, prices, fees and funding, L4 = closed
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
        "layers": {"L0": check["inputs"]["status"], "L1": "not_applicable", "L2": st(timing or cmp["trades_vt"] != cmp["trades_alphakeel"]),
                   "L3": st(amounts), "L4": st(not tot_ok)},
        "first_difference": (check["inputs"]["differences"] or cmp["first_differences"] or [None])[0],
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
