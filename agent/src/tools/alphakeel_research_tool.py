"""Agent tool: research against AlphaKeel's frozen data and engine through its service API.

Thin wrapper over the ``alphakeel_research`` command line (the same code a standalone script uses). It returns
references (pack/run/comparison ids, digests, file paths) and short summaries, never full event logs: those stay on disk
under the returned ``run_dir``. No orders are placed anywhere; the service credential is read from the environment
(``ALPHAKEEL_RESEARCH_TOKEN`` / ``_TOKEN_FILE``) and never appears in arguments or results.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from src.agent.tools import BaseTool
from src.config.accessor import get_env_value
from src.tools.alphakeel_ir_request import IGNORED_NOTE
from src.tools.alphakeel_research_ledger import NO_SESSION_WARNING

_ACTIONS = ("check", "freeze", "read", "review", "policy", "native", "compare", "ir_validate", "ir_review", "ir_backtest", "ir_robustness", "ir_crosscheck", "ir_export")
#: ``read`` tables: scan packs have quotes/observations/settlements; dataset packs have klines/bbo/instrument_meta (and settlements).
_MAX_OUT = 20_000
#: Added to an error that means the service has no market data at all: the strategy cannot be backtested here, and
#: the agent must say so instead of switching to another strategy or engine (session 8125728230d5).
_MARKET_BLOCKED_NEXT = (
    "BLOCKED: this AlphaKeel service has no published market dataset, so no dataset pack can carry kline_1m/bbo/"
    "instrument_meta for any window and ir_backtest cannot price the legs. This is a platform data gap, not a "
    "symbol or argument problem; retrying other windows or symbols will not help. Tell the user exactly this. "
    "Do NOT replace the requested strategy with a different one (for example a single-asset backtest with the "
    "built-in engine). What still works for the same strategy: a funding-only estimate from the settlements already "
    "in a pack (read table=settlements for each leg), labelled 'funding-only: no price, basis, spread or slippage "
    "PnL; not an ir_backtest'. Any different strategy needs the user's confirmation by name."
)


#: ``ir_validate`` with local problems: they are all returned at once and AlphaKeel is not called (session b9aa13a496a4).
_IR_PROBLEMS_HINT = (
    "All problems are listed at once: fix every one, then validate again. A complete valid template is at the start of "
    'load_skill(name="alphakeel-handoff", section="0. Strategy logic: hand over a Strategy IR (primary path)").'
)


def _read_ir(path: Any) -> tuple[Any, str | None]:
    """The IR document at ``path`` (``ir_validate`` / ``ir_backtest`` take a path), or the reason it cannot be read."""
    try:
        with open(str(path), encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return None, f"ir: cannot read {path}: {e.__class__.__name__}: {e}"
    try:
        return json.loads(text), None
    except (ValueError, RecursionError) as e:  # RecursionError: nesting deeper than the parser's stack
        return None, f"ir: {path} is not JSON: {e.__class__.__name__}: {str(e)[:500]}"


#: arguments that are file or directory paths. The agent writes them relative to its run directory (write_file resolves
#: against run_dir, which every tool call receives), while the CLI would resolve them against the process cwd.
_RELATIVE_PATH_KEYS = ("ir", "fees", "instruments", "filter", "trials_evidence", "pack_dir", "oos_pack_dir", "run", "out",
                       "robustness", "reconciliation", "intents", "strategy", "profile", "params", "rules", "parameters")


def _resolve_paths(args: dict[str, Any], run_dir: Any) -> dict[str, Any]:
    """``args`` with ``~`` expanded and every relative path made absolute under ``run_dir`` (inline JSON strings and
    absolute paths stay)."""
    if not isinstance(run_dir, str) or not run_dir.strip():
        return args
    out = dict(args)
    for k in _RELATIVE_PATH_KEYS:
        v = out.get(k)
        if not isinstance(v, str) or not v.strip() or v.lstrip().startswith(("{", "[")):
            continue  # not a path, or inline JSON
        v = os.path.expanduser(v)
        out[k] = v if os.path.isabs(v) else os.path.join(run_dir, v)
    return out


def _dump(out: dict, warnings: list[str]) -> str:
    """The envelope as JSON, cut to ``_MAX_OUT``; unit warnings ride at the top level, also on a truncated envelope."""
    if warnings:
        out = {**out, "warnings": warnings}
    body = json.dumps(out, ensure_ascii=False, default=str)
    if len(body) <= _MAX_OUT:
        return body
    cut = {"status": out["status"], "truncated": True, "head": body[:_MAX_OUT]}
    return json.dumps({**cut, "warnings": warnings} if warnings else cut)


def _csv(v: Any) -> str:
    """A list (or already comma separated string) of names as the CLI's comma list."""
    return ",".join(str(x).strip() for x in v if str(x).strip()) if isinstance(v, (list, tuple)) else str(v)


#: where ``ir export`` may NOT write from the agent: AlphaKeel's hand-off directory (same variable the run page's button uses)
_HANDOFF_DEFAULT = "/home/vibe/handoff"


def _handoff_dir() -> str:
    return get_env_value("ALPHAKEEL_HANDOFF_DIR") or _HANDOFF_DEFAULT


def _under_handoff(path: str) -> bool:
    root = os.path.realpath(_handoff_dir())
    p = os.path.realpath(path)
    return p == root or p.startswith(root.rstrip(os.sep) + os.sep)


def _instruments_file(v: Any, prefix: str = "ak-instruments-", key: str = "instruments") -> str:
    """``instruments`` (or ``filter``, ``fees``) is a path to a JSON file, or the JSON itself, which is written to a temp
    file. A string that starts with ``{`` or ``[`` is that JSON serialized (models often send objects as strings)."""
    if isinstance(v, str) and v.strip().startswith(("{", "[")):
        try:
            v = json.loads(v)
        except ValueError as e:
            raise ValueError(f"{key}: not valid JSON: {e}") from None
    if isinstance(v, (list, dict)):
        fd, path = tempfile.mkstemp(prefix=prefix, suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(v, f)
        return path
    return str(v)


#: the planned ``ir_backtest`` request always carries fees (the request's or the tool default,
#: ``alphakeel_ir_request.DEFAULT_PUBLIC_TAKER``); ``_argv`` refuses one without.
_FEES_NEEDED = ("needs fees: {venue: {perp: rate}} with a decimal-string taker rate for every venue in universe.pairs (or in "
                "instruments when pairs is \"any\"; no default; unless the user gives a fee tier use each venue's public "
                "base-tier perp taker fee and say so)")


#: path-valued arguments a command writes under (``out``; ``run``: ``ir robustness`` writes DIR/robustness; ``robustness``: the
#: battery directory an export copies from / a re-run writes to). None of them may point into the hand-off directory.
_PATH_KEYS = ("out", "robustness", "run")


def _handoff_guard(action: str, a: dict[str, Any]) -> None:
    """No path-valued argument may point into AlphaKeel's hand-off directory."""
    for k in _PATH_KEYS:
        v = a.get(k)
        if isinstance(v, (str, os.PathLike)) and str(v) != "" and _under_handoff(str(v)):
            raise ValueError(f"{action}: {k} points into AlphaKeel's hand-off directory, which is not an agent action. Hand-off is the "
                             "user's decision: the user presses 'Hand off to AlphaKeel' on the run page (docs/45 section 7). Use another "
                             "directory, or tell the user the run is ready for review.")


def _argv(action: str, a: dict[str, Any]) -> list[str]:
    _handoff_guard(action, a)
    out: list[str] = action.split("_", 1) if action.startswith("ir_") else [action]

    def add(flag: str, key: str, required: bool = False) -> None:
        v = a.get(key)
        if v is None or v == "":
            if required:
                raise ValueError(f"{action} needs {key}")
            return
        if isinstance(v, bool):
            kind = ('funding dataset version: omit it (latest), "none", or a version id' if key == "funding"
                    else "number, string or list, as the action's argument describes")
            raise ValueError(f"{action}: {key} must be a {kind}, not a boolean" if key != "funding"
                             else f"{action}: funding is the {kind}; it is not a boolean")
        if isinstance(v, dict):
            raise ValueError(f"{action}: {key} must be a scalar")
        out.extend([flag, _csv(v) if isinstance(v, (list, tuple)) else str(v)])

    def fees(required: bool) -> None:
        """``fees`` is a JSON object (written to a temp file) or a path to one."""
        v = a.get("fees")
        if v is None or v == "" or v == {} or isinstance(v, bool):
            if required or isinstance(v, bool):
                raise ValueError(f"{action} {_FEES_NEEDED}")
            return
        out.extend(["--fees", _instruments_file(v, "ak-fees-", "fees")])

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
        # ``a`` is the request planned by ``alphakeel_ir_request.plan`` (research-truth B2): window, instruments, step,
        # fees and their source derived by code; trials from the session's trial ledger.
        add("--ir", "ir", True), add("--start-ms", "start_ms", True), add("--end-ms", "end_ms", True)
        if a.get("instruments") in (None, "", []):
            raise ValueError("ir_backtest needs instruments")
        out.extend(["--instruments", _instruments_file(a["instruments"])])
        add("--out", "out")  # optional: the CLI creates a Vibe-Trading run directory
        for f, k in (("--step-minutes", "step_minutes"), ("--trials", "trials"), ("--trials-evidence", "trials_evidence"),
                     ("--capital-base", "capital_base"), ("--pack-dir", "pack_dir"), ("--fees-source", "fees_source")):
            add(f, k)
        out.extend(f for f, k in (("--accept-partial", "accept_partial"), ("--verbose", "verbose")) if a.get(k))
        fees(True)
    elif action == "ir_robustness":
        if not a.get("run") and not a.get("ir"):
            raise ValueError("ir_robustness needs run (an ir_backtest run directory) or ir")
        add("--run", "run"), add("--ir", "ir"), add("--pack-dir", "pack_dir")
        if a.get("instruments") not in (None, "", []):
            out.extend(["--instruments", _instruments_file(a["instruments"])])
        for f, k in (("--is-start-ms", "is_start_ms"), ("--is-end-ms", "is_end_ms"), ("--oos-start-ms", "oos_start_ms"),
                     ("--oos-end-ms", "oos_end_ms"), ("--oos-pack-dir", "oos_pack_dir")):
            add(f, k)
        fees(False)  # optional: the --run card's fee table is the default
        if a.get("filter") not in (None, "", {}):
            out.extend(["--filter", _instruments_file(a["filter"], "ak-filter-", "filter")])
        for f, k in (("--permutations", "permutations"), ("--seed", "seed"), ("--permutation-seed", "permutation_seed"),
                     ("--workers", "workers"), ("--step-minutes", "step_minutes"),
                     ("--capital-base", "capital_base"), ("--prior-trials", "prior_trials"), ("--wf-folds", "wf_folds"),
                     ("--wf-test-days", "wf_test_days"), ("--wf-min-train-days", "wf_min_train_days"), ("--out", "out")):
            add(f, k)
    elif action == "ir_crosscheck":
        add("--ir", "ir", True), add("--run", "run", True), add("--timeout-s", "timeout_s")
    elif action == "ir_export":
        add("--ir", "ir", True), add("--run", "run", True), add("--out", "out", True), add("--robustness", "robustness"), add("--reconciliation", "reconciliation")
        if _under_handoff(str(a["out"])):
            raise ValueError("ir_export: writing into AlphaKeel's hand-off directory is not an agent action. Hand-off is the user's "
                             "decision: the user presses 'Hand off to AlphaKeel' on the run page (docs/45 section 7). Export to another "
                             "directory if you need the package, or tell the user the run is ready for review.")
        if a.get("research_credential"):
            out.append("--research-credential")
    return out


def _tool_timeout_seconds() -> float | None:
    """The child process bound: the agent loop's tool timeout (its module-level override, else
    ``VIBE_TRADING_TOOL_TIMEOUT_SECONDS``); a non-positive value disables it."""
    from src.agent.loop import _tool_timeout_seconds as loop_tool_timeout

    configured = float(loop_tool_timeout())
    return configured if configured > 0 else None


def _child_cmd(argv: list[str]) -> list[str]:
    """``python -P -m alphakeel_research ARGV``: ``-P`` (Python >= 3.11) keeps the cwd and script dir off ``sys.path``,
    so a module the model wrote (json.py, decimal.py ...) is never imported by the child, which holds the credential."""
    return [sys.executable, "-P", "-m", "alphakeel_research", *argv]


def _run_child(cmd: list[str], timeout: float | None) -> subprocess.CompletedProcess:
    """Run the CLI in its own process group (cwd None: relative paths are already absolute, ``_resolve_paths``); on
    timeout the whole group is killed (grandchildren included) and ``TimeoutExpired`` is raised."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ.copy(), cwd=None,
                            start_new_session=True)
    done = False
    try:
        out, err = proc.communicate(timeout=timeout)
        done = True
    finally:
        if not done:  # timeout or any other exception: the group goes, the original exception propagates
            if hasattr(os, "killpg"):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:  # pragma: no cover - Windows
                proc.kill()
            try:
                proc.communicate(timeout=5)
            except Exception:  # noqa: BLE001 - give up reading rather than block
                pass
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _redact(text: str) -> str:
    """``text`` with the research-service credential (``ALPHAKEEL_RESEARCH_TOKEN``, the content of
    ``ALPHAKEEL_RESEARCH_TOKEN_FILE``) replaced by ``<redacted>``."""
    secrets = []
    tok = os.environ.get("ALPHAKEEL_RESEARCH_TOKEN")
    if tok:
        secrets.append(tok)
    path = os.environ.get("ALPHAKEEL_RESEARCH_TOKEN_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                secrets.append(content)
        except (OSError, UnicodeDecodeError):
            pass
    for secret in secrets:
        text = text.replace(secret, "<redacted>")
    return text


def _json_or_none(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _market_blocked(code: Any, message: Any) -> bool:
    return code == "data.insufficient" and "no market dataset" in str(message or "")


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


# ---------------------------------------------------------------------------------------------------------------------
# ir_backtest: request planned by code, evidence copy, trial ledger, code-made label (research-truth B1-B4)
# ---------------------------------------------------------------------------------------------------------------------

def _check_out(out: Any, run_dir: Any) -> None:
    """``ir_backtest`` ``out`` (already resolved): never inside ``<run_dir>/ir_runs/`` (the evidence copies), never a
    path whose last segment is empty, ``.`` or ``..``, never a directory that already holds a run card (the ledger's
    sha256 of the earlier run would break)."""
    if out in (None, ""):
        return
    text = str(out)
    last = re.split(r"[\\/]", text)[-1]
    inside = False
    if isinstance(run_dir, str) and run_dir.strip():
        root = os.path.realpath(os.path.join(run_dir, "ir_runs"))
        p = os.path.realpath(text)
        inside = p == root or p.startswith(root.rstrip(os.sep) + os.sep)
    if last in ("", ".", "..") or inside:
        raise ValueError("out must not be inside ir_runs/ or end in . or ..")
    if os.path.isfile(os.path.join(text, "run_card.json")):
        raise ValueError("out already holds a run card: use a new out directory or omit out")


def _session_id(tool: Any) -> str | None:
    v = str(getattr(tool, "_default_session_id", None) or "").strip()
    return v or None


def _plan_ir_backtest(args: dict[str, Any], run_dir: Any, session_id: str | None, warnings: list[str]) -> dict[str, Any]:
    """The CLI request of an ``ir_backtest``, in this order: arguments parsed, hand-off guard and ``out`` check, one
    ``check()`` + ``alphakeel_ir_request.plan``, then the conversation's trial ledger (``trials`` / ``trials_evidence``).
    Nothing is registered or written when an earlier step fails. Raises ``ValueError`` or the service's ``ApiError``."""
    from alphakeel_research import ir

    from src.tools import alphakeel_ir_request as req

    a = req.parse_args(args)  # every argument problem at once, before anything is read or asked
    _handoff_guard("ir_backtest", args)
    _check_out(args.get("out"), run_dir)
    doc, unreadable = _read_ir(a.ir)
    if unreadable is not None:
        raise ValueError(unreadable)
    try:
        loaded = ir.load(doc)
    except Exception as e:  # noqa: BLE001 - IrError: the IR does not validate
        raise ValueError(f"ir: {getattr(e, 'message', None) or e} (run ir_validate first)") from None
    planned = req.plan(args, loaded.doc, req.service_check())
    if session_id is None:
        warnings.append(NO_SESSION_WARNING)
    elif not (isinstance(run_dir, str) and run_dir.strip()):
        warnings.append("no run directory: trial ledger skipped")
    else:
        try:
            from src.tools import alphakeel_research_ledger as ledger

            count, evidence = ledger.trials_for(session_id, run_dir, loaded.strategy_id, ir_doc=loaded.doc,
                                                window={"start_ms": planned["start_ms"], "end_ms": planned["end_ms"]},
                                                ir_name=loaded.doc.get("name") or "")
            planned["trials"], planned["trials_evidence"] = count, evidence
        except Exception as e:  # noqa: BLE001 - the ledger never fails the backtest
            warnings.append(f"trial ledger: {e.__class__.__name__}: {str(e)[:300]}")
    return planned


def _copy_evidence(result: dict, run_dir: Any) -> str | None:
    """Copy the run's ``run_card.json`` and ``artifacts/metrics.csv`` to ``<run_dir>/ir_runs/<run dir name>[-N]/`` (a
    new directory: ``-2``, ``-3``... when the name is taken); returns it relative to ``run_dir`` (``evidence_dir``), or
    None without a run directory."""
    if not (isinstance(run_dir, str) and run_dir.strip()):
        return None
    src = Path(str(result["run_dir"])).resolve()
    base = Path(run_dir) / "ir_runs"
    name, n = src.name, 1
    while (base / name).exists():
        n += 1
        name = f"{src.name}-{n}"
    dst = base / name
    dst.mkdir(parents=True)
    for f in ("run_card.json", "artifacts/metrics.csv"):
        if (src / f).is_file():
            (dst / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src / f, dst / f)
    return f"ir_runs/{name}"


_SCREEN_FAIL_TEXT = {"1.1": "1.1 window {o} d < {t}", "1.2": "1.2 {o} closed trades < {t}", "1.3": "1.3 net {o} USDT <= {t}",
                     "1.16": "1.16 coverage partial"}


def _summary(result: dict) -> str:
    """One code-made line: the label, every failed screen item, closed trades, window, fee basis."""
    scr = result.get("screen") or {}
    items = {i.get("id"): i for i in scr.get("items") or [] if isinstance(i, dict)}
    parts = []
    for i in items.values():
        if i.get("result") == "FAIL":
            thr = next(iter((i.get("threshold") or {}).values()), "?")
            parts.append(_SCREEN_FAIL_TEXT.get(i["id"], i["id"] + " FAIL").format(o="missing" if i.get("observed") is None else i["observed"], t=thr))
    closed = (result.get("counts") or {}).get("closed")
    days = (items.get("1.1") or {}).get("observed")
    fees_source = (result.get("assumptions") or {}).get("fees_source")
    parts += [f"{closed} closed trades", f"window {days} d", f"fees {fees_source}"]
    return f"{result.get('evidence_status')}: " + "; ".join(parts)


def _finish_ir_backtest(out: dict, planned: dict, run_dir: Any, session_id: str | None, warnings: list[str]) -> None:
    """A finished ``ir_backtest`` envelope: ``evidence_status`` first and a ``summary`` in ``result``, ``evidence_dir``
    and ``ignored_args`` on the envelope, the run appended to the conversation's private ledger."""
    result = out.get("result")
    if planned.get("ignored_args"):
        out["ignored_args"] = planned["ignored_args"]
        out["ignored_args_note"] = IGNORED_NOTE
    if out["status"] != "ok" or not isinstance(result, dict) or not result.get("run_dir"):
        return
    if "evidence_status" in result:
        out["result"] = {"evidence_status": result["evidence_status"], "summary": _summary(result),
                         **{k: v for k, v in result.items() if k != "evidence_status"}}
    rel = None
    try:
        rel = _copy_evidence(result, run_dir)
        if rel is not None:
            out["evidence_dir"] = rel
    except OSError as e:
        warnings.append(f"evidence copy: {e.__class__.__name__}: {str(e)[:300]}")
    if session_id is not None and isinstance(run_dir, str) and run_dir.strip():
        try:
            from src.tools import alphakeel_research_ledger as ledger

            ledger.record(session_id, run_dir, result, rel)
        except Exception as e:  # noqa: BLE001 - the ledger never fails the backtest
            warnings.append(f"trial ledger: {e.__class__.__name__}: {str(e)[:300]}")


class AlphakeelResearchTool(BaseTool):
    """Freeze AlphaKeel data, review a Python backtest, run a policy or the native strategy, compare results."""

    name = "alphakeel_research"
    description = (
        "Use AlphaKeel's frozen market data and its Rust/Nautilus engine through the research service: "
        "'freeze' a data pack for a time window (a scan pack, or with args.instruments a dataset pack: funding settlements for "
        "any window; market_tables kline_1m (trade/mark/index) for the published history; instrument_meta, bbo and depth20 are "
        "live captures covering only recent days, so never request them for a historical window), 'read' point-in-time rows (quotes, settlements, klines, bbo, instrument_meta), 'review' a Python backtest's fixed intents "
        "against the engine (layered L0-L4 comparison with the first difference), run a Python 'policy' under both a "
        "local simulator and the engine's real state, run the 'native' strategy, or 'compare' two runs. Strategy IR "
        "(strategy.json): 'ir_validate' with AlphaKeel, 'ir_review' (Python vs AlphaKeel's Rust evaluator on a scan pack, "
        "layered reconciliation), 'ir_backtest' (long-history research backtest on a dataset pack: 1m klines + official "
        "settlements, synthetic touch; research evidence, not reconciliation; it freezes its own pack (kline_1m trade + "
        "settlements), so do not 'freeze' before it. It needs only args.ir; start_ms/end_ms are optional (default: the 180 "
        "days before the hold-out cutoff); instruments, step, fees (public base-tier taker unless args.fees is given) and "
        "the trial count are derived from the IR and the session's trial ledger. It returns evidence_status "
        "(insufficient / failed_screen / passed_screen) and evidence_dir (the ref of its observed figures)), 'ir_robustness' (reliability battery + grade on the run's exported pack: cost stress, parameter neighbours, "
        "anchored walk-forward, OOS, filter permutation test; args.run = the ir_backtest run directory), 'ir_crosscheck' (the same "
        "backtest run by AlphaKeel's engine, compared trade by trade), 'ir_export' (strategy.json with strategy_id "
        "and provenance for AlphaKeel's registry, from an ir_backtest run; carries the robustness evidence). You cannot export into "
        "AlphaKeel's hand-off directory: hand-off is the user's button on the run page. Files for "
        "intents/strategy/profile are paths on disk. Returns ids, digests and summaries only; a pass never certifies "
        "strategy logic. Relative file paths in args resolve against the run directory. Needs ALPHAKEEL_RESEARCH_URL and a service token in the environment."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(_ACTIONS)},
            "args": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "start_ms": {"type": "integer", "description": "epoch milliseconds"},
                    "end_ms": {"type": "integer", "description": "epoch milliseconds"},
                    "as_of_ms": {"type": "integer", "description": "epoch milliseconds"},
                    "funding": {"type": "string", "description": 'funding dataset version: omit for latest, "none" to drop funding'},
                    "venues": {"type": "array", "items": {"type": "string"}},
                    "instruments": {
                        "anyOf": [
                            {"type": "array", "items": {"type": "object", "properties": {
                                "venue": {"type": "string"}, "market": {"type": "string"}, "symbol": {"type": "string"}}}},
                            {"type": "string"},
                        ],
                        "description": "freeze/read/review/policy/ir_robustness: symbol is the venue-native contract id, e.g. okx perp "
                                       "BTC-USDT-SWAP, binance perp BTCUSDT; a spot-style BTC-USDT on a perp finds no settlements. "
                                       "Not for ir_backtest (derived from the IR).",
                    },
                    "market_tables": {
                        "type": "array", "items": {"type": "string", "enum": ["kline_1m", "bbo", "depth20", "instrument_meta"]},
                        "description": "without market_tables a dataset pack holds funding settlements only (any window) and cannot drive an "
                                       "ir_backtest PnL. kline_1m (trade/mark/index) covers the published history; instrument_meta, "
                                       "bbo and depth20 are live captures covering only recent days: never request them for a "
                                       "historical window. ir_backtest freezes its own pack (kline_1m trade + settlements): do not "
                                       "freeze before it.",
                    },
                    "fees": {
                        "anyOf": [{"type": "object"}, {"type": "string"}],
                        "description": "{venue: {perp: rate}} with decimal-string taker rates (or a JSON file path); ir_backtest: "
                                       "optional, only when the user gives a fee tier (default: each venue's public base-tier taker)",
                    },
                    "pack": {"type": "string"},
                    "accept_partial": {"type": "boolean"},
                },
                "description": "Arguments of the action (see the alphakeel_research CLI): start_ms, end_ms, venues, funding, instruments (list of {venue, market, symbol} or a JSON file path), market_tables, price_kinds, market_dataset, accept_partial, pack, venue, market, symbol, table, price_kind, as_of_ms, intents, strategy, instruments, parameters, seed, profile, rules, params, a, b, mode; IR: ir, out, run, fees, capital_base, pack_dir, verbose, research_credential; ir_backtest: ir (required), start_ms, end_ms (optional), fees (optional), accept_partial, capital_base, pack_dir, verbose -- instruments, step_minutes, trials, trials_evidence are derived and ignored if given; robustness (export: directory with robustness.json); ir_robustness: run, ir, instruments, pack_dir, is_start_ms, is_end_ms, oos_start_ms, oos_end_ms, oos_pack_dir, filter ({kind: excluded_bases, bases: [...]} or a JSON file), permutations, seed, permutation_seed, workers, step_minutes, prior_trials, wf_folds, wf_test_days, wf_min_train_days; ir_crosscheck: ir, run, timeout_s.",
            },
        },
        "required": ["action"],
    }
    repeatable = True
    is_readonly = False

    def __init__(self, default_session_id: str | None = None, event_callback: Any = None) -> None:
        """``default_session_id``: the conversation, injected by the host (``build_registry``); the IR trial ledger and
        result card are per conversation. The model never chooses it."""
        self._default_session_id = default_session_id
        self._event_callback = event_callback

    def execute(self, **kwargs: Any) -> str:
        """Every exit of ``_execute`` is redacted (the research-service credential never reaches the model)."""
        return _redact(self._execute(**kwargs))

    def _execute(self, **kwargs: Any) -> str:
        action = str(kwargs.get("action", "")).strip()
        args = kwargs.get("args") or {}
        if action not in _ACTIONS or not isinstance(args, dict):
            return json.dumps({"status": "error", "error": f"action must be one of {list(_ACTIONS)} and args an object"})
        session_run_dir = kwargs.get("run_dir")
        args = _resolve_paths(args, session_run_dir)  # before _argv: the hand-off guard checks the resolved path
        warnings: list[str] = []
        planned: dict[str, Any] | None = None
        try:
            from alphakeel_research import ir
            from alphakeel_research.errors import ApiError

            if action == "ir_backtest":
                try:
                    planned = _plan_ir_backtest(args, session_run_dir, _session_id(self), warnings)
                except ApiError as e:
                    return json.dumps({"status": "error", "error": e.message, "code": e.code,
                                       "request_id": getattr(e, "request_id", None)})
                argv = _argv(action, planned)
            else:
                argv = _argv(action, args)
        except ImportError as e:
            return json.dumps({"status": "error", "error": f"alphakeel_research is not installed: {e}"})
        except ValueError as e:
            return json.dumps({"status": "error", "error": str(e)})
        # Strategy IR: unit warnings (never blocking); ir_validate first runs the local checker, which lists every
        # problem at once, and asks AlphaKeel (authoritative) only when it finds none.
        if action in ("ir_validate", "ir_backtest"):
            doc, unreadable = _read_ir(args["ir"])
            if unreadable is None:
                warnings = ir.unit_warnings(doc) + warnings
            if action == "ir_validate":
                if unreadable is not None:
                    return json.dumps({"status": "error", "error": unreadable})
                found = ir.problems(doc)
                if found:
                    return _dump({"status": "error", "result": {"valid": False, "source": "local", "problems": found,
                                                                 "hint": _IR_PROBLEMS_HINT}}, warnings)
        # A child process per call, as upstream's backtest tool (src/core/runner.py): redirect_stdout is process-wide, so
        # two conversations running at once read each other's output (research-truth, replay session 4cada46ec0b4).
        timeout = _tool_timeout_seconds()
        try:
            proc = _run_child(_child_cmd(argv), timeout)
        except subprocess.TimeoutExpired:
            err = {"status": "error", "error": f"alphakeel_research timed out after {timeout} s"}
            return json.dumps({**err, "warnings": warnings} if warnings else err)
        except Exception as e:  # noqa: BLE001 - surfaced as a clean tool error, never with credentials
            err = {"status": "error", "error": _redact(f"{e.__class__.__name__}: {str(e)[:500]}")}
            return json.dumps({**err, "warnings": warnings} if warnings else err)
        text, rc = proc.stdout or "", proc.returncode
        if rc != 0 and _json_or_none(text) is None:
            stderr = _redact(proc.stderr or "")
            if rc == 2:  # argparse: its message is on stderr and echoes only argv
                msg = f"bad arguments (exit 2): {stderr[-1000:].strip()}"
            elif rc < 0:
                msg = f"alphakeel_research was killed by signal {-rc}"
            else:
                last = next((ln.strip() for ln in reversed(stderr.splitlines()) if ln.strip()), "")
                msg = f"alphakeel_research failed (exit {rc}): {last[:500]}"
            err = {"status": "error", "error": msg}
            return json.dumps({**err, "warnings": warnings} if warnings else err)
        try:
            result: Any = json.loads(text)
        except ValueError:
            result = (_ir_review_summary(args) if action == "ir_review" and rc == 0 else None) or {"output": text[:_MAX_OUT]}
        out = {"status": "ok" if rc == 0 else "error", "result": result}
        e = result.get("error") if isinstance(result, dict) else None
        if rc != 0 and isinstance(e, dict) and _market_blocked(e.get("code"), e.get("message")):
            out["next"] = _MARKET_BLOCKED_NEXT
        if planned is not None:
            _finish_ir_backtest(out, planned, session_run_dir, _session_id(self), warnings)
        return _dump(out, warnings)
