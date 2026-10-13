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
                                          --fees fees.json [--fees-source LABEL] [--trials N [--trials-evidence FILE]] [--capital-base X]
                                          [--accept-partial] [--pack-dir DIR] [--verbose] [--out DIR]
                                          (--out defaults to a new Vibe-Trading run directory RUNS_DIR/<YYYYMMDD_HHMMSS>_ir/, which
                                           also gets ir.json, instruments.json, req.json, review_report.json, state.json)
                                          (long-history research backtest on a dataset pack: 1m klines + official
                                           settlements, synthetic touch; research evidence, NOT reconciliation input)
    python -m alphakeel_research ir crosscheck --ir strategy.json --run DIR [--timeout-s 7200]
                                          (the same dataset pack, window, step, fees and instruments as the `ir backtest` run
                                           in DIR, run by AlphaKeel's engine (ir_strategy + dataset_view); trade-by-trade
                                           comparison written to DIR/alphakeel-check.json)
    python -m alphakeel_research ir export   --ir strategy.json --run DIR [--research-credential] [--robustness DIR] --out DIR
                                          (robustness defaults to DIR/robustness when present; evidence/robustness.* is copied)
                                          (strategy.json with strategy_id + provenance for AlphaKeel's registry)
    python -m alphakeel_research ir robustness --ir strategy.json --pack-dir DIR --instruments inst.json --is-start-ms N --is-end-ms N
                                          [--oos-start-ms N --oos-end-ms N [--oos-pack-dir DIR]] [--fees fees.json] [--filter filter.json]
                                          [--permutations 999] [--seed 7] [--permutation-seed N] [--workers 2] [--step-minutes 5] [--capital-base X]
                                          [--prior-trials N] [--wf-folds 3] [--wf-test-days 30] [--wf-min-train-days 30] --out DIR
    python -m alphakeel_research ir robustness --run DIR [--oos-start-ms N --oos-end-ms N ...]
                                          (DIR = an `ir backtest` run: --out DIR/robustness, --ir DIR/ir.json, --instruments
                                           DIR/instruments.json, the IS window and --pack-dir from its run card; explicit
                                           arguments must agree with the run card)
                                          (reliability battery on an exported dataset pack: cost stress, parameter neighbours,
                                           sub-periods, OOS, filter permutation test, honest trial count; grade A-F with the
                                           rule table; writes DIR/robustness.json, robustness.md, trials.jsonl; no network)

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


def _run_card(run_dir: str) -> dict:
    from . import ir_backtest

    f = Path(run_dir) / "run_card.json"
    try:
        card = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ApiError("request.invalid", f"{f} is not readable: --run needs an `ir backtest` run directory", field="run") from None
    if card.get("schema") != ir_backtest.RUN_CARD_SCHEMA:
        raise ApiError("request.invalid", f"{f} is not a {ir_backtest.RUN_CARD_SCHEMA} document", field="run")
    return card


def _robustness_inputs(a) -> tuple[str, str, str, tuple[int, int], str]:
    """(ir, instruments, pack_dir, IS window, out) of ``ir robustness``: explicit arguments, or -- with ``--run`` -- the
    run directory's files and run card; an explicit argument that disagrees with the run card is an error."""
    from . import ir as IR

    if not a.run:
        missing = [f for f, v in (("--ir", a.ir), ("--pack-dir", a.pack_dir), ("--instruments", a.instruments), ("--is-start-ms", a.is_start_ms),
                                  ("--is-end-ms", a.is_end_ms), ("--out", a.out)) if v is None]
        if missing:
            raise ApiError("request.invalid", f"ir robustness needs {', '.join(missing)} (or --run DIR)", field="run")
        return a.ir, a.instruments, a.pack_dir, (a.is_start_ms, a.is_end_ms), a.out
    run = Path(a.run)
    card = _run_card(a.run)
    ir_path = a.ir or str(run / "ir.json")
    if not Path(ir_path).is_file():
        raise ApiError("request.invalid", f"{ir_path} does not exist: --run needs the run directory's ir.json (or --ir)", field="ir")
    sid = IR.load(_load(ir_path)).strategy_id
    if sid != card["strategy_id"]:
        raise ApiError("request.invalid", f"the IR is {sid}, the run card is for {card['strategy_id']}", field="ir")
    w = card["window"]
    window = (w["start_ms"], w["end_ms"])
    for flag, given, want in (("--is-start-ms", a.is_start_ms, window[0]), ("--is-end-ms", a.is_end_ms, window[1])):
        if given is not None and given != want:
            raise ApiError("request.invalid", f"{flag} {given} differs from the run card's window ({want})", field="is_window")
    pack_dir = a.pack_dir or card.get("pack_dir")
    if not pack_dir:
        raise ApiError("request.invalid", "the run card records no pack_dir (the pack came from the service): give --pack-dir", field="pack_dir")
    inst = a.instruments or str(run / "instruments.json")
    if not Path(inst).is_file():
        raise ApiError("request.invalid", f"{inst} does not exist: give --instruments", field="instruments")
    return ir_path, inst, pack_dir, window, a.out or str(run / "robustness")


def _robustness_fees(a) -> tuple[dict | None, str | None]:
    """``(fee table, its source)`` of ``ir robustness``: ``--fees FILE``, else the ``--run`` card's table -- refused when that
    card ran on the old implicit default (``fees_source`` ``"default"`` or absent: cards written before the fee table was
    required carry ``0.0005`` on every venue; audit 2026-10-12 R2-D-4 a)."""
    if a.fees:
        return _load(a.fees), a.fees
    if not a.run:
        return None, None
    assumptions = _run_card(a.run).get("assumptions") or {}
    src = assumptions.get("fees_source")
    if not assumptions.get("fees") or src in (None, "", "default"):
        raise ApiError("request.invalid", f"the run card in {a.run} has no explicit fee table (fees_source {src!r}: the old implicit "
                       "0.0005 default); give --fees", field="fees")
    return assumptions["fees"], f"run_card:{Path(a.run) / 'run_card.json'}"


def _battery(robustness, registry, ir_path, pack, inst_path, window, oos, a, trials_floor) -> dict:
    from decimal import Decimal

    fees, fees_source = _robustness_fees(a)
    return robustness.battery(_load(ir_path), pack, _load(inst_path), is_window=window, oos_window=oos,
                              oos_pack=Pack.from_directory(a.oos_pack_dir) if a.oos_pack_dir else None, out_dir=a.out,
                              fees=fees, fees_source=fees_source,
                              filter_spec=_load(a.filter), permutations=a.permutations, seed=a.seed,
                              permutation_seed=a.permutation_seed,
                              step_minutes=a.step_minutes, capital_base=Decimal(a.capital_base) if a.capital_base else None,
                              workers=a.workers, prior_trials=a.prior_trials, wf_folds=a.wf_folds, wf_test_days=a.wf_test_days,
                              wf_min_train_days=a.wf_min_train_days, prior_trials_floor=trials_floor, registry=registry)


def _ir_research(a) -> int:
    """``ir backtest`` / ``ir export``: the service is only contacted when it is needed (freezing, credential)."""
    from decimal import Decimal

    if a.ir_cmd == "robustness":
        from . import robustness

        if (a.oos_start_ms is None) != (a.oos_end_ms is None):
            raise ApiError("request.invalid", "--oos-start-ms and --oos-end-ms go together", field="oos_window")
        ir_path, inst_path, pack_dir, window, out_dir = _robustness_inputs(a)
        pack = Pack.from_directory(pack_dir)
        if a.run and pack.id != _run_card(a.run)["pack"]["pack_id"]:
            raise ApiError("request.invalid", f"--pack-dir is pack {pack.id}, the run used {_run_card(a.run)['pack']['pack_id']}", field="pack_dir")
        oos = None if a.oos_start_ms is None else (a.oos_start_ms, a.oos_end_ms)
        a.out = out_dir
        # prior_trials is never below the IR run card's trials.count (audit 2 P1-1 b): the battery uses the larger and says so
        floor = ((_run_card(a.run).get("trials") or {}).get("count") or 0) if a.run else 0
        # every candidate run is registered in the research service's trial ledger (audit 2 P1-1 c); --no-register opts out, visibly
        reg = None if a.no_register else Client(a.url)
        try:
            doc = _battery(robustness, reg, ir_path, pack, inst_path, window, oos, a, floor)
        finally:
            if reg is not None:
                reg.close()
        for n in doc["notes"]:
            if n.startswith("warning:"):
                print(n, file=sys.stderr)
        r = doc["rating"]
        _out({"strategy_id": doc["strategy_id"], "grade": r["grade"], "score": r["score"], "caps": [c["id"] for c in r["caps"]],
              "reasons": r["reasons"], "trials": doc["trials"]["count"], "runs": len(doc["runs"]),
              "service_runs": doc["service_runs"]["ids"],
              "report": str(Path(a.out) / "robustness.md"), "json": str(Path(a.out) / "robustness.json"),
              "note": "research evidence on the synthetic-touch dataset view; not an approval"})
        return 0
    if a.ir_cmd == "crosscheck":
        from . import ir_crosscheck

        c = Client(a.url)
        try:
            c.check()
            doc = ir_crosscheck.alphakeel_check(c, _load(a.ir), a.run, timeout=a.timeout_s)
        finally:
            c.close()
        _out({k: doc.get(k) for k in ("passed", "alphakeel_run_id", "alphakeel_status", "comparison", "totals", "error")})
        return 0 if doc.get("passed") else 1
    if a.ir_cmd == "backtest":
        if not a.out:
            from . import ir_backtest

            a.out = str(ir_backtest.new_run_dir())
        c = None if (a.pack_dir and a.no_register) else Client(a.url)
        try:
            card = workflow.ir_backtest(c, _load(a.ir), _load(a.instruments), start_ms=a.start_ms, end_ms=a.end_ms, out_dir=a.out,
                                        cache_dir=a.cache, pack_dir=a.pack_dir, register=not a.no_register,
                                        step_minutes=a.step_minutes, fees=_load(a.fees),
                                        fees_source=a.fees_source or a.fees, trials=a.trials, trials_evidence=a.trials_evidence,
                                        capital_base=Decimal(a.capital_base) if a.capital_base else None,
                                        accept_partial=a.accept_partial, verbose=a.verbose)
        finally:
            if c is not None:
                c.close()
        _out({"evidence_status": card["screen"]["evidence_status"], "screen": card["screen"], "run_id": card["run_id"], "strategy_id": card["strategy_id"], "pack_id": card["pack"]["pack_id"],
              "window": card["window"], "view": card["view"], "counts": card["counts"], "amounts": card["amounts"],
              "max_drawdown": card["max_drawdown"], "trials": card["trials"], "assumptions": card["assumptions"],
              "service_registration": card.get("service_registration"),
              "run_dir": str(a.out), "run_card": str(Path(a.out) / "run_card.json"), "note": "research evidence (dataset view approximations in the run card); not reconciliation input"})
        return 0
    cred = None
    if a.research_credential:
        c = Client(a.url)
        try:
            cred = c.check()
        finally:
            c.close()
    r = workflow.ir_export(_load(a.ir), a.run, a.out, research_credential=cred, robustness_dir=a.robustness, reconciliation=a.reconciliation,
                           data_exposure_end_ms=a.data_exposure_end_ms)
    for w in r.get("warnings", []):
        print(f"warning: {w}", file=sys.stderr)
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
    q.add_argument("--fees", required=True, help="JSON {venue: {perp: rate, spot: rate}} (required: there is no default fee)")
    q.add_argument("--fees-source", help="label of the fee table in the run card (default: the --fees value)")
    q.add_argument("--trials", type=int)
    q.add_argument("--trials-evidence")
    q.add_argument("--capital-base", help="capital base of the daily returns (default notional_per_leg x 2 x max_open)")
    q.add_argument("--accept-partial", action="store_true")
    q.add_argument("--pack-dir", help="an exported dataset pack directory instead of freezing one through the service")
    q.add_argument("--no-register", action="store_true", help="do not register this run in the research service's trial ledger (recorded in the run card)")
    q.add_argument("--verbose", action="store_true", help="write every rejected pair into decisions.jsonl")
    q.add_argument("--out", help="output directory (default: a new Vibe-Trading run directory RUNS_DIR/<YYYYMMDD_HHMMSS>_ir/)")
    q = irs.add_parser("robustness", help="reliability battery + confidence grade on an exported dataset pack (data offline; each candidate run is registered with the research service)")
    q.add_argument("--run", help="an `ir backtest` run directory: defaults --out (DIR/robustness), --ir, --instruments, the IS window and --pack-dir")
    q.add_argument("--ir")
    q.add_argument("--pack-dir", help="exported dataset pack directory (lock.json + content/)")
    q.add_argument("--instruments", help="JSON list of {venue, market, symbol, base?, quote?}")
    q.add_argument("--is-start-ms", type=int)
    q.add_argument("--is-end-ms", type=int)
    q.add_argument("--oos-start-ms", type=int)
    q.add_argument("--oos-end-ms", type=int)
    q.add_argument("--oos-pack-dir", help="exported pack for the OOS window (default: --pack-dir)")
    q.add_argument("--fees", help="JSON {venue: {perp: rate, spot: rate}} (default: the --run card's explicit fee table; otherwise required)")
    q.add_argument("--filter", help='JSON {"kind": "excluded_bases", "bases": ["XYZ", ...]}: the filter under test')
    q.add_argument("--permutations", type=int, default=999, help="protocol 2.5 (1.12): AlphaKeel requires >= 999")
    q.add_argument("--seed", type=int, default=7, help="bootstrap seed")
    q.add_argument("--permutation-seed", type=int, default=None,
                   help="default (and required by AlphaKeel 1.12): derived from the strategy id")
    q.add_argument("--workers", type=int, default=2)
    q.add_argument("--step-minutes", type=int, default=5)
    q.add_argument("--capital-base", help="capital base shared by every variant (default notional_per_leg x 2 x max_open of the IR)")
    q.add_argument("--prior-trials", type=int, default=0, help="variants tried BEFORE this battery (self-declared; added to the DSR count; "
                   "never below the `ir backtest` run card's trials.count: the larger is used, with a warning)")
    q.add_argument("--no-register", action="store_true", help="do not register the candidate runs in the research service's trial ledger "
                   "(recorded in robustness.json service_runs.registered = false; AlphaKeel's DSR then relies on its own trial floor)")
    q.add_argument("--wf-folds", type=int, default=3, help="anchored walk-forward folds (test segments = the last folds x test-days days of the IS window)")
    q.add_argument("--wf-test-days", type=int, default=30)
    q.add_argument("--wf-min-train-days", type=int, default=30, help="shorter first training segment: walk-forward not computable")
    q.add_argument("--out", help="output directory (required without --run)")
    q = irs.add_parser("crosscheck", help="the same dataset backtest run by AlphaKeel's engine, compared trade by trade")
    q.add_argument("--ir", required=True)
    q.add_argument("--run", required=True, help="the `ir backtest` output directory")
    q.add_argument("--timeout-s", type=float, default=7200.0)
    q = irs.add_parser("export")
    q.add_argument("--ir", required=True)
    q.add_argument("--run", required=True, help="the `ir backtest` output directory")
    q.add_argument("--research-credential", action="store_true", help="record the research-service credential (Client.check())")
    q.add_argument("--data-exposure-end-ms", type=int, help="latest end (ms, exclusive) of data the research read in other "
                   "processes (the conversation's exposure file); folded into provenance.data_exposure_end_ms")
    q.add_argument("--robustness", help="directory with robustness.json/.md/trials.jsonl (default: RUN/robustness when present)")
    q.add_argument("--reconciliation", help="reconciliation.json of `ir review` / `ir crosscheck` (default: RUN/reconciliation.json when present); "
                   "its pack_id must equal the robustness in-sample pack")
    q.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "ir" and a.ir_cmd in ("backtest", "export", "robustness", "crosscheck"):
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
            held = pack.instruments()
            if inst not in held:  # not "0 rows": the agent took silence for missing data and froze probe packs (session 77963ca6bc9e)
                near = sorted(held, key=lambda i: (i.symbol != inst.symbol, i.venue != inst.venue, i.leg()))
                shown = ", ".join(f"{i.venue} {i.market} {i.symbol}" for i in near[:30]) + (f" (+{len(near) - 30} more)" if len(near) > 30 else "")
                raise ApiError("request.invalid", f"read: {inst.venue} {inst.market} {inst.symbol} is not in pack {pack.id}; the pack holds: "
                               f"{shown or 'no instruments'}; ir_backtest freezes its own pack", field="symbol")
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
