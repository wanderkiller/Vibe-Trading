"""Agent tool: research against AlphaKeel's frozen data and engine through its service API.

Thin wrapper over the ``alphakeel_research`` command line (the same code a standalone script uses). It returns
references (pack/run/comparison ids, digests, file paths) and short summaries, never full event logs: those stay on disk
under the returned ``run_dir``. No orders are placed anywhere; the service credential is read from the environment
(``ALPHAKEEL_RESEARCH_TOKEN`` / ``_TOKEN_FILE``) and never appears in arguments or results.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
from typing import Any

from src.agent.tools import BaseTool

_ACTIONS = ("check", "freeze", "read", "review", "policy", "native", "compare", "ir_validate", "ir_review", "ir_backtest", "ir_export")
#: ``read`` tables: scan packs have quotes/observations/settlements; dataset packs have klines/bbo/instrument_meta (and settlements).
_MAX_OUT = 20_000


def _csv(v: Any) -> str:
    """A list (or already comma separated string) of names as the CLI's comma list."""
    return ",".join(str(x).strip() for x in v if str(x).strip()) if isinstance(v, (list, tuple)) else str(v)


def _instruments_file(v: Any) -> str:
    """``instruments`` is a path to a JSON file, or the list itself (``[{venue, market, symbol}]``), which is written to a temp file."""
    if isinstance(v, (list, dict)):
        fd, path = tempfile.mkstemp(prefix="ak-instruments-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(v, f)
        return path
    return str(v)


def _argv(action: str, a: dict[str, Any]) -> list[str]:
    out: list[str] = action.split("_", 1) if action.startswith("ir_") else [action]

    def add(flag: str, key: str, required: bool = False) -> None:
        v = a.get(key)
        if v is None or v == "":
            if required:
                raise ValueError(f"{action} needs {key}")
            return
        out.extend([flag, str(v)])

    if action == "freeze":
        add("--start-ms", "start_ms", True), add("--end-ms", "end_ms", True), add("--venues", "venues"), add("--funding", "funding")
        add("--warmup-frames", "warmup_frames")
        # Dataset pack (no scan frames; any window): funding history and/or market tables for an explicit instrument list.
        if a.get("instruments") not in (None, "", []):
            out.extend(["--instruments", _instruments_file(a["instruments"])])
            if a.get("market_tables"):
                out.extend(["--market-tables", _csv(a["market_tables"])])
            if a.get("price_kinds"):
                out.extend(["--price-kinds", _csv(a["price_kinds"])])
            add("--market-dataset", "market_dataset")
        elif any(a.get(k) for k in ("market_tables", "price_kinds", "market_dataset")):
            raise ValueError("freeze: market_tables / price_kinds / market_dataset need instruments (a dataset pack)")
        if a.get("accept_partial"):
            out.append("--accept-partial")
    elif action == "read":
        for f, k in (("--pack", "pack"), ("--venue", "venue"), ("--symbol", "symbol"), ("--start-ms", "start_ms"), ("--end-ms", "end_ms")):
            add(f, k, True)
        add("--market", "market"), add("--table", "table"), add("--as-of-ms", "as_of_ms"), add("--limit", "limit")
        add("--price-kind", "price_kind")
    elif action == "review":
        add("--pack", "pack", True), add("--intents", "intents", True), add("--profile", "profile"), add("--instruments", "instruments")
    elif action == "policy":
        for f, k in (("--pack", "pack"), ("--strategy", "strategy"), ("--instruments", "instruments")):
            add(f, k, True)
        add("--profile", "profile"), add("--seed", "seed"), add("--parameters", "parameters")
    elif action == "native":
        add("--pack", "pack", True), add("--rules", "rules"), add("--params", "params"), add("--profile", "profile")
    elif action == "compare":
        add("--a", "a", True), add("--b", "b", True), add("--mode", "mode", True)
    # Strategy IR: the document is a path (strategy.json); outputs go to the given directory
    elif action == "ir_validate":
        add("--ir", "ir", True), add("--pack", "pack")
    elif action == "ir_review":
        add("--pack", "pack", True), add("--ir", "ir", True), add("--out", "out", True), add("--seed", "seed"), add("--profile", "profile")
    elif action == "ir_backtest":
        add("--ir", "ir", True), add("--start-ms", "start_ms", True), add("--end-ms", "end_ms", True)
        if a.get("instruments") in (None, "", []):
            raise ValueError("ir_backtest needs instruments")
        out.extend(["--instruments", _instruments_file(a["instruments"])])
        add("--out", "out", True)
        for f, k in (("--step-minutes", "step_minutes"), ("--fees", "fees"), ("--trials", "trials"), ("--trials-evidence", "trials_evidence"),
                     ("--capital-base", "capital_base"), ("--pack-dir", "pack_dir")):
            add(f, k)
        out.extend(f for f, k in (("--accept-partial", "accept_partial"), ("--verbose", "verbose")) if a.get(k))
    elif action == "ir_export":
        add("--ir", "ir", True), add("--run", "run", True), add("--out", "out", True)
        if a.get("research_credential"):
            out.append("--research-credential")
    return out


def _ir_review_summary(args: dict[str, Any]) -> dict | None:
    """``ir review`` prints a text report; the tool returns the references and the comparison from reconciliation.json."""
    path = os.path.join(str(args.get("out")), "reconciliation.json")
    try:
        with open(path, encoding="utf-8") as f:
            r = json.load(f)
    except (OSError, ValueError):
        return None
    keep = ("strategy_id", "pack_id", "local_run", "alphakeel_run", "comparison", "passed", "layers", "first_difference", "pass_scope", "summary")
    return {**{k: r.get(k) for k in keep}, "reconciliation": path}


class AlphakeelResearchTool(BaseTool):
    """Freeze AlphaKeel data, review a Python backtest, run a policy or the native strategy, compare results."""

    name = "alphakeel_research"
    description = (
        "Use AlphaKeel's frozen market data and its Rust/Nautilus engine through the research service: "
        "'freeze' a data pack for a time window (a scan pack, or with args.instruments a dataset pack of funding history and "
        "market_tables kline_1m/bbo/depth20/instrument_meta for ANY window), 'read' point-in-time rows (quotes, settlements, klines, bbo, instrument_meta), 'review' a Python backtest's fixed intents "
        "against the engine (layered L0-L4 comparison with the first difference), run a Python 'policy' under both a "
        "local simulator and the engine's real state, run the 'native' strategy, or 'compare' two runs. Strategy IR "
        "(strategy.json): 'ir_validate' with AlphaKeel, 'ir_review' (Python vs AlphaKeel's Rust evaluator on a scan pack, "
        "layered reconciliation), 'ir_backtest' (long-history research backtest on a dataset pack: 1m klines + official "
        "settlements, synthetic touch; research evidence, not reconciliation), 'ir_export' (strategy.json with strategy_id "
        "and provenance for AlphaKeel's registry, from an ir_backtest run). Files for "
        "intents/strategy/profile are paths on disk. Returns ids, digests and summaries only; a pass never certifies "
        "strategy logic. Needs ALPHAKEEL_RESEARCH_URL and a service token in the environment."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(_ACTIONS)},
            "args": {"type": "object", "description": "Arguments of the action (see the alphakeel_research CLI): start_ms, end_ms, venues, funding, instruments (list of {venue, market, symbol} or a JSON file path), market_tables, price_kinds, market_dataset, accept_partial, pack, venue, market, symbol, table, price_kind, as_of_ms, intents, strategy, instruments, parameters, seed, profile, rules, params, a, b, mode; IR: ir, out, run, step_minutes, fees, trials, trials_evidence, capital_base, pack_dir, verbose, research_credential."},
        },
        "required": ["action"],
    }
    repeatable = True
    is_readonly = False

    def execute(self, **kwargs: Any) -> str:
        action = str(kwargs.get("action", "")).strip()
        args = kwargs.get("args") or {}
        if action not in _ACTIONS or not isinstance(args, dict):
            return json.dumps({"status": "error", "error": f"action must be one of {list(_ACTIONS)} and args an object"})
        try:
            from alphakeel_research import cli
            from alphakeel_research.errors import ApiError

            argv = _argv(action, args)
        except ImportError as e:
            return json.dumps({"status": "error", "error": f"alphakeel_research is not installed: {e}"})
        except ValueError as e:
            return json.dumps({"status": "error", "error": str(e)})
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = cli.main(argv)
        except ApiError as e:
            return json.dumps({"status": "error", "error": e.message, "code": e.code, "request_id": getattr(e, "request_id", None)})
        except SystemExit as e:  # argparse
            return json.dumps({"status": "error", "error": f"bad arguments (exit {e.code})"})
        except Exception as e:  # noqa: BLE001 - surfaced as a clean tool error, never with credentials
            return json.dumps({"status": "error", "error": f"{e.__class__.__name__}: {str(e)[:500]}"})
        text = buf.getvalue()
        try:
            result: Any = json.loads(text)
        except ValueError:
            result = (_ir_review_summary(args) if action == "ir_review" and rc == 0 else None) or {"output": text[:_MAX_OUT]}
        body = json.dumps({"status": "ok" if rc == 0 else "error", "result": result}, ensure_ascii=False, default=str)
        return body if len(body) <= _MAX_OUT else json.dumps({"status": "ok" if rc == 0 else "error", "truncated": True, "head": body[:_MAX_OUT]})
