"""Command line: freeze and read data, review a Python backtest, run a policy under both state sources, run native.

    python -m alphakeel_research check
    python -m alphakeel_research freeze   --start-ms N --end-ms N [--venues binance,okx] [--funding none|latest] --out pack.json
    python -m alphakeel_research read     --pack PACK_ID --venue binance --symbol BTCUSDT [--market perp] --start-ms N --end-ms N [--as-of-ms N]
    python -m alphakeel_research review   --pack PACK_ID --intents intents.json [--profile profile.json]
    python -m alphakeel_research policy   --pack PACK_ID --strategy DIR --instruments instruments.json [--profile profile.json]
    python -m alphakeel_research native   --pack PACK_ID --rules rules.json            (or --params handoff.json)
    python -m alphakeel_research inject   --pack PACK_ID --intents intents.json --fault fee|funding-sign
    python -m alphakeel_research inject   --pack PACK_ID --intents intents.json --fault fee|funding-sign
    python -m alphakeel_research compare  --a RUN --b RUN --mode fixed_intent_replay|python_policy|native_strategy

Credential: ALPHAKEEL_RESEARCH_TOKEN or ALPHAKEEL_RESEARCH_TOKEN_FILE; service URL: ALPHAKEEL_RESEARCH_URL.
Every command prints one JSON document with references (ids, hashes, paths) and summaries; full events stay on disk.
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
    p.add_argument("--out")
    p = sub.add_parser("read")
    p.add_argument("--pack", required=True)
    p.add_argument("--venue", required=True)
    p.add_argument("--symbol", required=True)
    p.add_argument("--market", default="perp")
    p.add_argument("--table", default="quotes", choices=["quotes", "observations", "settlements"])
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
    a = ap.parse_args(argv)
    try:
        c = Client(a.url)
        work = Path(a.work) if a.work else Path(tempfile.mkdtemp(prefix="ak-research-"))
        if a.cmd == "check":
            _out(c.check())
        elif a.cmd == "freeze":
            req = {"window": {"start_ms": a.start_ms, "end_ms": a.end_ms}, "warmup_frames": a.warmup_frames, "venues": [v for v in a.venues.split(",") if v],
                   "accept_partial": a.accept_partial}
            if a.funding != "latest":
                req["funding_dataset"] = a.funding
            pack = workflow.freeze_pack(c, req, cache_dir=a.cache)
            lock = pack.lock
            if a.out:
                Path(a.out).write_bytes(canon.canonical_bytes(lock))
            _out({"pack_id": pack.id, "pack_sha256": lock["pack_sha256"], "frames": lock["scans"]["frames"], "windows": lock["windows"],
                  "funding": lock["funding"], "coverage": lock["coverage"], "objects": len(lock["objects"]), "cache": a.cache})
        elif a.cmd == "read":
            pack = Pack.open(c, a.pack, a.cache)
            inst = Inst(a.venue, a.market, a.symbol)
            from .packfile import Pit

            src = Pit(pack, a.as_of_ms) if a.as_of_ms is not None else pack
            rows = getattr(src, a.table)(inst, a.start_ms, a.end_ms)
            _out({"pack_id": pack.id, "table": a.table, "rows": len(rows), "first": [r.__dict__ for r in rows[: a.limit]], "accesses": pack.accesses()})
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
