"""Command line: freeze and read data, review a Python backtest, run a policy under both state sources, run native.

    python -m alphakeel_research check
    python -m alphakeel_research freeze   --start-ms N --end-ms N [--venues binance,okx] [--funding none|latest] --out pack.json
    python -m alphakeel_research freeze   --start-ms N --end-ms N --instruments inst.json [--market-tables kline_1m,bbo] [--price-kinds trade]
                                          (dataset pack: funding history / market data for ANY window, no scan frames; the
                                           most recent hold-out days are refused with data.holdout)
    python -m alphakeel_research read     --pack PACK_ID --venue binance --symbol BTCUSDT [--market perp] --start-ms N --end-ms N [--as-of-ms N]
    python -m alphakeel_research review   --pack PACK_ID --intents intents.json [--profile profile.json]
    python -m alphakeel_research policy   --pack PACK_ID --strategy DIR --instruments instruments.json [--profile profile.json]
    python -m alphakeel_research native   --pack PACK_ID --rules rules.json            (or --params handoff.json)
    python -m alphakeel_research inject   --pack PACK_ID --intents intents.json --fault fee|funding-sign
    python -m alphakeel_research inject   --pack PACK_ID --intents intents.json --fault fee|funding-sign
    python -m alphakeel_research compare  --a RUN --b RUN --mode fixed_intent_replay|python_policy|native_strategy|ir_strategy
    python -m alphakeel_research ir validate --ir strategy.json [--pack PACK_ID]
    python -m alphakeel_research ir review   --pack PACK_ID --ir strategy.json [--seed N] [--profile profile.json] --out DIR
                                          (the IR in Python under the local simulator AND in AlphaKeel's Rust evaluator,
                                           compared layer by layer; prints the comparison, writes DIR/reconciliation.json)
    python -m alphakeel_research ir backtest --ir strategy.json --start-ms N --end-ms N --instruments inst.json [--step-minutes 2]
                                          [--fees fees.json] [--trials N [--trials-evidence FILE]] [--capital-base X]
                                          [--accept-partial] [--pack-dir DIR] [--verbose] --out DIR
                                          (long-history research backtest on a dataset pack: 1m klines + official
                                           settlements, synthetic touch; research evidence, NOT reconciliation input)
    python -m alphakeel_research ir export   --ir strategy.json --run DIR [--research-credential] --out DIR
                                          (strategy.json with strategy_id + provenance for AlphaKeel's registry)

Credential: ALPHAKEEL_RESEARCH_TOKEN or ALPHAKEEL_RESEARCH_TOKEN_FILE; service URL: ALPHAKEEL_RESEARCH_URL.
Every command prints one JSON document with references (ids, hashes, paths) and summaries; full events stay on disk
(except `ir review`, which prints a plain-text report; its JSON is reconciliation.json).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from . import canon, workflow
from .client import Client
from .errors import ApiError
from .packfile import Inst, Pack


def _out(obj) -> None:
    json.dump(obj, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")


def _load(path: str | None):
    return None if not path else json.loads(Path(path).read_text(encoding="utf-8"))


IR_FOOTER = "Decision (paper or not) is yours; this report does not approve anything."


def ir_report(r: dict) -> str:
    """Plain-text rendering of ``workflow.ir_review``: only the comparison's own output, both sides' summaries, the
    references, and the fixed footer. No verdict or recommendation of its own."""
    lines = [f"Strategy IR reconciliation  {r['strategy_id']}  (definition sha256 {r['strategy_sha256']})",
             f"pack {r['pack_id']}  (sha256 {r['pack_sha256']})",
             f"python run {r['local_run']}   alphakeel run {r['alphakeel_run']} (mode {r['alphakeel_result']['mode']})   comparison {r['comparison']}",
             "", "layer  status"]
    lines += [f"{k:<6} {v}" for k, v in sorted(r["layers"].items())]
    lines += ["", "first_difference: " + json.dumps(r["first_difference"], sort_keys=True, default=str),
              "pass_scope: " + str(r["pass_scope"]), "",
              f"{'':<14}{'opened':>8}{'closed':>8}{'open_at_end':>13}  realized"]
    for name, key in (("python_local", "python_local"), ("alphakeel", "alphakeel")):
        s = r["summary"][key]
        lines.append(f"{name:<14}{str(s['positions_opened']):>8}{str(s['positions_closed']):>8}{str(s['positions_open_at_end']):>13}  {s['realized']}")
    lines += ["", f"written: {Path(r['run_dir']).parent / 'reconciliation.json'}", "", IR_FOOTER]
    return "\n".join(lines) + "\n"


def _ir_research(a) -> int:
    """``ir backtest`` / ``ir export``: the service is only contacted when it is needed (freezing, credential)."""
    from decimal import Decimal

    if a.ir_cmd == "backtest":
        c = None if a.pack_dir else Client(a.url)
        try:
            card = workflow.ir_backtest(c, _load(a.ir), _load(a.instruments), start_ms=a.start_ms, end_ms=a.end_ms, out_dir=a.out,
                                        cache_dir=a.cache, pack_dir=a.pack_dir, step_minutes=a.step_minutes, fees=_load(a.fees),
                                        fees_source=a.fees or "default", trials=a.trials, trials_evidence=a.trials_evidence,
                                        capital_base=Decimal(a.capital_base) if a.capital_base else None,
                                        accept_partial=a.accept_partial, verbose=a.verbose)
        finally:
            if c is not None:
                c.close()
        _out({"run_id": card["run_id"], "strategy_id": card["strategy_id"], "pack_id": card["pack"]["pack_id"],
              "window": card["window"], "view": card["view"], "counts": card["counts"], "amounts": card["amounts"],
              "max_drawdown": card["max_drawdown"], "trials": card["trials"], "assumptions": card["assumptions"],
              "run_card": str(Path(a.out) / "run_card.json"), "note": "research evidence (dataset view approximations in the run card); not reconciliation input"})
        return 0
    cred = None
    if a.research_credential:
        c = Client(a.url)
        try:
            cred = c.check()
        finally:
            c.close()
    r = workflow.ir_export(_load(a.ir), a.run, a.out, research_credential=cred)
    _out(r)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="alphakeel_research", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url")
    ap.add_argument("--cache", default=str(workflow.DEFAULT_CACHE))
    ap.add_argument("--work", default=None, help="directory for run evidence (default: a temp dir)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    p = sub.add_parser("freeze")
    p.add_argument("--start-ms", type=int, required=True)
    p.add_argument("--end-ms", type=int, required=True)
    p.add_argument("--venues", default="")
    p.add_argument("--warmup-frames", type=int, default=0)
    p.add_argument("--funding", default="latest", help="latest (default), none, or a dataset version")
    p.add_argument("--accept-partial", action="store_true")
    p.add_argument("--instruments", help="JSON list of {venue, market, symbol}: makes this a dataset pack (required for one)")
    p.add_argument("--market-tables", default="", help="dataset pack: comma list of kline_1m,bbo,depth20,instrument_meta")
    p.add_argument("--market-dataset", help="dataset pack: market dataset version (default: latest at acceptance)")
    p.add_argument("--price-kinds", default="", help="dataset pack: comma list of trade,mark,index for kline_1m (default trade)")
    p.add_argument("--out")
    p = sub.add_parser("read")
    p.add_argument("--pack", required=True)
    p.add_argument("--venue", required=True)
    p.add_argument("--symbol", required=True)
    p.add_argument("--market", default="perp")
    p.add_argument("--table", default="quotes", choices=["quotes", "observations", "settlements", "klines", "bbo", "instrument_meta"])
    p.add_argument("--price-kind", default="trade", choices=["trade", "mark", "index"], help="klines only (dataset packs)")
    p.add_argument("--start-ms", type=int, required=True)
    p.add_argument("--end-ms", type=int, required=True)
    p.add_argument("--as-of-ms", type=int)
    p.add_argument("--limit", type=int, default=5)
    p = sub.add_parser("review")
    p.add_argument("--pack", required=True)
    p.add_argument("--intents", required=True)
    p.add_argument("--profile")
    p.add_argument("--instruments")
    p = sub.add_parser("policy")
    p.add_argument("--pack", required=True)
    p.add_argument("--strategy", required=True)
    p.add_argument("--instruments", required=True)
    p.add_argument("--profile")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--parameters")
    p = sub.add_parser("inject", help="corrupt one value of a correct run's evidence and show how the comparison locates it")
    p.add_argument("--pack", required=True)
    p.add_argument("--intents", required=True)
    p.add_argument("--fault", required=True, choices=list(workflow.FAULTS))
    p.add_argument("--profile")
    p = sub.add_parser("native")
    p.add_argument("--pack", required=True)
    p.add_argument("--rules")
    p.add_argument("--params")
    p.add_argument("--profile")
    p = sub.add_parser("compare")
    p.add_argument("--a", required=True)
    p.add_argument("--b", required=True)
    p.add_argument("--mode", required=True)
    p = sub.add_parser("ir", help="Strategy IR: validate with AlphaKeel, reconcile Python vs AlphaKeel's Rust evaluator")
    irs = p.add_subparsers(dest="ir_cmd", required=True)
    q = irs.add_parser("validate")
    q.add_argument("--ir", required=True)
    q.add_argument("--pack")
    q = irs.add_parser("review")
    q.add_argument("--pack", required=True)
    q.add_argument("--ir", required=True)
    q.add_argument("--seed", type=int, default=0)
    q.add_argument("--profile")
    q.add_argument("--out", required=True)
    q = irs.add_parser("backtest")
    q.add_argument("--ir", required=True)
    q.add_argument("--start-ms", type=int, required=True)
    q.add_argument("--end-ms", type=int, required=True)
    q.add_argument("--instruments", required=True, help="JSON list of {venue, market, symbol, base?, quote?}")
    q.add_argument("--step-minutes", type=int, default=2)
    q.add_argument("--fees", help="JSON {venue: {perp: rate, spot: rate}} (default: 0.0005 perp taker on every venue)")
    q.add_argument("--trials", type=int)
    q.add_argument("--trials-evidence")
    q.add_argument("--capital-base", help="capital base of the daily returns (default notional_per_leg x 2 x max_open)")
    q.add_argument("--accept-partial", action="store_true")
    q.add_argument("--pack-dir", help="an exported dataset pack directory instead of freezing one through the service")
    q.add_argument("--verbose", action="store_true", help="write every rejected pair into decisions.jsonl")
    q.add_argument("--out", required=True)
    q = irs.add_parser("export")
    q.add_argument("--ir", required=True)
    q.add_argument("--run", required=True, help="the `ir backtest` output directory")
    q.add_argument("--research-credential", action="store_true", help="record the research-service credential (Client.check())")
    q.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "ir" and a.ir_cmd in ("backtest", "export"):
            return _ir_research(a)
        c = Client(a.url)
        work = Path(a.work) if a.work else Path(tempfile.mkdtemp(prefix="ak-research-"))
        if a.cmd == "check":
            _out(c.check())
        elif a.cmd == "freeze":
            req = {"window": {"start_ms": a.start_ms, "end_ms": a.end_ms}, "warmup_frames": a.warmup_frames, "venues": [v for v in a.venues.split(",") if v],
                   "accept_partial": a.accept_partial}
            if a.funding != "latest":
                req["funding_dataset"] = a.funding
            if a.instruments:
                req = c.dataset_pack_request(start_ms=a.start_ms, end_ms=a.end_ms, instruments=_load(a.instruments),
                                             funding_dataset=None if a.funding == "latest" else a.funding,
                                             market_tables=[t for t in a.market_tables.split(",") if t],
                                             price_kinds=[t for t in a.price_kinds.split(",") if t] or None,
                                             market_dataset=a.market_dataset, accept_partial=a.accept_partial)
            pack = workflow.freeze_pack(c, req, cache_dir=a.cache)
            lock = pack.lock
            if a.out:
                Path(a.out).write_bytes(canon.canonical_bytes(lock))
            _out({"pack_id": pack.id, "pack_sha256": lock["pack_sha256"], "frames": (lock["scans"] or {}).get("frames"), "windows": lock["windows"],
                  "funding": lock["funding"], "datasets": lock.get("datasets"), "coverage": lock["coverage"], "objects": len(lock["objects"]), "cache": a.cache})
        elif a.cmd == "read":
            pack = Pack.open(c, a.pack, a.cache)
            inst = Inst(a.venue, a.market, a.symbol)
            from .packfile import Pit

            src = Pit(pack, a.as_of_ms) if a.as_of_ms is not None else pack
            if a.table == "instrument_meta":  # spec snapshots (t = snapshot day); with --as-of-ms only the one known then
                if a.as_of_ms is not None:
                    meta = src.instrument_meta(inst)
                    rows = [meta] if meta else []
                else:
                    rows = pack.instrument_meta_history(inst)
            elif a.table == "klines":
                rows = src.klines(inst, a.start_ms, a.end_ms, price_kind=a.price_kind)
            else:
                rows = getattr(src, a.table)(inst, a.start_ms, a.end_ms)
            _out({"pack_id": pack.id, "table": a.table, "rows": len(rows),
                  "first": [r if isinstance(r, dict) else r.__dict__ for r in rows[: a.limit]], "accesses": pack.accesses()})
        elif a.cmd == "review":
            pack = Pack.open(c, a.pack, a.cache)
            intents = _load(a.intents)
            intents = intents["intents"] if isinstance(intents, dict) else intents
            r = workflow.review_intents(c, pack, intents, profile=_load(a.profile), declared=_load(a.instruments), cache_run_dir=work)
            rep = r["report"] or {}
            _out({"local_run": r["local_run"], "alphakeel_run": r["alphakeel_run"], "comparison": r["comparison"], "passed": rep.get("passed"),
                  "layers": {k: v["status"] for k, v in rep.get("layers", {}).items()}, "first_difference": rep.get("first_difference"), "pass_scope": rep.get("pass_scope"), "run_dir": r["run_dir"]})
        elif a.cmd == "policy":
            from . import policy_flow

            pack = Pack.open(c, a.pack, a.cache)
            r = policy_flow.run_both(c, pack, a.strategy, _load(a.instruments), profile=_load(a.profile), seed=a.seed, parameters=_load(a.parameters), work=work)
            _out(r)
        elif a.cmd == "inject":
            pack = Pack.open(c, a.pack, a.cache)
            intents = _load(a.intents)
            intents = intents["intents"] if isinstance(intents, dict) else intents
            _out(workflow.inject_fault(c, pack, intents, a.fault, profile=_load(a.profile), work=work))
        elif a.cmd == "native":
            pack = Pack.open(c, a.pack, a.cache)
            rules = _load(a.rules)
            if a.params:
                v = c.validate_params(_load(a.params), pack.id)
                if not v["valid"]:
                    _out({"valid": False, "problems": v["problems"]})
                    return 2
                rules = v["rules"]
            r = workflow.native_run(c, pack, rules, profile=_load(a.profile))
            _out({"run_id": r["run_id"], "amounts": r["result"]["amounts"], "verification": r["result"]["verification"], "counts": r["result"]["counts"],
                  "facts_sha256": r["facts_sha256"]})
        elif a.cmd == "ir" and a.ir_cmd == "validate":
            v = workflow.validate_ir(c, _load(a.ir), a.pack)
            _out(v)
            return 0 if v["valid"] else 2
        elif a.cmd == "ir":
            pack = Pack.open(c, a.pack, a.cache)
            r = workflow.ir_review(c, pack, _load(a.ir), out_dir=a.out, profile=_load(a.profile), seed=a.seed)
            sys.stdout.write(ir_report(r))
        elif a.cmd == "compare":
            cmp = c.compare(a.a, a.b, a.mode)
            rep = c.wait_comparison(cmp["comparison_id"])
            _out({"comparison": cmp["comparison_id"], "status": rep["status"], "passed": (rep.get("report") or {}).get("passed"),
                  "layers": {k: v["status"] for k, v in (rep.get("report") or {}).get("layers", {}).items()}, "first_difference": (rep.get("report") or {}).get("first_difference")})
        return 0
    except ApiError as e:
        _out({"error": {"code": e.code, "message": e.message, "field": e.field, "retryable": e.retryable, "request_id": e.request_id}})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
