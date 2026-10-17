"""IR backtest trials of one conversation, and the result card built from them (research-truth B4, §5).

Scope is the conversation (``session_id``), not a turn: every turn gets a new run directory. The tool's private ledger
is ``<sessions dir>/<session_id>/ir_ledger.jsonl`` -- outside the agent's write roots, so the model cannot write it --
with one line per successful ``ir_backtest`` (``strategy_id``, ``name``, the absolute ``run_card_path`` and its
``run_card_sha256``, ``evidence_dir``, the turn's ``run_dir``, ``ts``). The run's ``--trials`` is the number of distinct
``strategy_id`` in it (this one included); ``<turn run dir>/ir_trials_evidence.jsonl`` lists them (one line per version)
and is its ``--trials-evidence``. AlphaKeel counts trials in its own research-service ledger anyway (G1 1.6).

Every run is also linked to one upstream ``HypothesisRegistry`` hypothesis per conversation (spec B4; the id is kept
in ``<sessions dir>/<session_id>/ir_hypothesis.json``); the registry is not a source of counts or of the card.

The card reads only the private ledger and re-reads every run card, checking its sha256: a run card that was changed
or is missing is not shown (and counted in one line). Read-modify-writes run under ``fcntl.flock`` on
``<file>.lock`` (no lock where there is no ``fcntl``, as ``alphakeel_handoff_routes``).

``<sessions dir>/<session_id>/data_exposure.jsonl`` is the conversation's data exposure (AlphaKeel protocol 2.5, G1 1.14):
one line per read (``source``, ``end_ms`` exclusive, ``via``, ``ts``), appended by ``backtest.data_audit.record_exposure``
in the agent process -- in-process tools directly, the backtest tool for its child -- because every tool call that reads
data runs in a process of its own and the exporting CLI child cannot see their in-process records. ``ir_export`` passes
the latest end to the export (``provenance.data_exposure_end_ms``).

The generic ``backtest`` tool has its own private trial ledger, ``<session dir>/engine_ledger.jsonl``: one row per
successful engine run, with a snapshot of the run (code, config, metrics, the daily returns, the code-made screen) under
``<session dir>/engine_runs/<NNNN>/`` because the next backtest overwrites the same run dir. A trial is identified by
``trial_key`` = sha256 of the engine-written ``artifacts/metrics.csv`` (results the model cannot steer; ``input_key`` is the
old code+config hash, kept for information). The card shows these rows too, from the snapshots only (sha256 of
``screen.json`` and ``returns.json`` checked); the DSR item and the verdict are recomputed at render time with the
session's current trial count, since the count grows after a run. ``alphakeel_holdout.json`` caches the research service's
hold-out cutoff: the backtest tool refuses crypto data that reaches into the hold-out.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import time
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

try:  # POSIX advisory lock; the Windows desktop build has no fcntl
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

LEDGER_FILE = "ir_ledger.jsonl"
HYPOTHESIS_FILE = "ir_hypothesis.json"
TRIALS_EVIDENCE_FILE = "ir_trials_evidence.jsonl"
EXPOSURE_FILE = "data_exposure.jsonl"
ENGINE_LEDGER_FILE = "engine_ledger.jsonl"
ENGINE_RUNS_DIR = "engine_runs"
RETURNS_FILE = "returns.json"
RETURNS_DATES_FILE = "returns_dates.json"
CONTRACT_FILE = "research_contract.json"
ROBUSTNESS_FILE = "robustness.jsonl"
DEFAULT_HOLDOUT_DAYS = 60
HOLDOUT_FILE = "alphakeel_holdout.json"
HOLDOUT_CACHE_SECONDS = 6 * 3600
HOLDOUT_FALLBACK_MEMO_SECONDS = 300
_FALLBACK_MEMO: dict[str | None, tuple[float, int]] = {}  # session id -> (monotonic time, cutoff_ms); memory only
CARD_MAX_ROWS = 10
NO_SESSION_WARNING = "no session id: trial ledger and result card skipped"


def _sessions_dir() -> Path:
    from src.config.paths import get_sessions_dir

    return Path(get_sessions_dir())


def _registry():
    from src.hypotheses.registry import HypothesisRegistry

    return HypothesisRegistry()


@contextlib.contextmanager
def _flock(path: Path) -> Iterator[None]:
    """Exclusive advisory lock on ``<path>.lock`` (none without fcntl)."""
    if fcntl is None:
        yield
        return
    lock = Path(f"{path}.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _fsync_dir(path: Path) -> None:
    """Make a new directory entry durable (POSIX; a no-op where directories cannot be opened)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _session_dir(session_id: str) -> Path:
    return _sessions_dir() / session_id


def engine_snapshot_dir(session_id: str, seq: int) -> Path:
    """The snapshot directory of engine run ``seq`` of the conversation (what ``trials/NNNN/`` reads)."""
    return _session_dir(session_id) / ENGINE_RUNS_DIR / f"{seq:04d}"


def _append_durable(path: Path, row: dict) -> None:
    """Append one JSON line under the file lock, durable before returning (upstream TraceWriter.write: flush + fsync per
    record, the directory entry too when the file is new)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _flock(path):
        created = not path.exists()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
            f.flush()
            with contextlib.suppress(OSError):  # a filesystem without fsync: flush-only, as upstream
                os.fsync(f.fileno())
        if created:
            _fsync_dir(path.parent)


def record_exposure(session_id: str, source: str, end_ms: int, via: str, crypto: bool | None = None) -> None:
    """Append one data read of the conversation (``source`` returned data up to ``end_ms``, exclusive) to its exposure
    file. Called by ``backtest.data_audit.record_exposure`` for the session bound in the agent process. ``crypto`` (None =
    unknown, the field is then omitted) says the read was crypto data; rows without it stay readable."""
    row = {"source": str(source), "end_ms": int(end_ms), "via": str(via), "ts": int(time.time() * 1000)}
    if crypto is not None:
        row["crypto"] = bool(crypto)
    _append_durable(_session_dir(session_id) / EXPOSURE_FILE, row)


def exposure_end_ms(session_id: str) -> int | None:
    """The latest ``end_ms`` in the conversation's exposure file, or None (no file, no readable line)."""
    best = None
    try:
        with open(_session_dir(session_id) / EXPOSURE_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    v = json.loads(line).get("end_ms")
                except (ValueError, AttributeError):
                    continue
                if isinstance(v, int) and not isinstance(v, bool) and (best is None or v > best):
                    best = v
    except OSError:
        return None
    return best


def crypto_exposure_end_ms(session_id: str) -> int | None:
    """The latest ``end_ms`` of the conversation's *crypto* data reads, or None. A row counts when its ``crypto`` field is
    True or, for a row without the field (written before it existed), when its source is in
    ``backtest.data_audit.crypto_sources()``. Equity reads are not hold-out data."""
    from backtest.data_audit import crypto_sources

    sources = crypto_sources()
    best = None
    try:
        with open(_session_dir(session_id) / EXPOSURE_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                    v, flag = row.get("end_ms"), row.get("crypto", None)
                    source = row.get("source")
                except (ValueError, AttributeError):
                    continue
                if not isinstance(v, int) or isinstance(v, bool) or (best is not None and v <= best):
                    continue
                if flag is True or (not isinstance(flag, bool) and source in sources):
                    best = v
    except OSError:
        return None
    return best


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_contract_file(session_id: str) -> dict | None:
    """``research_contract.json`` of the session as ``{"active": dict, "history": list}``; None when missing or unreadable."""
    try:
        data = json.loads((_session_dir(session_id) / CONTRACT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("active"), dict):
        return None
    history = data.get("history")
    return {"active": data["active"], "history": [h for h in history if isinstance(h, dict)] if isinstance(history, list) else []}


def _write_json_atomic(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(doc, sort_keys=True, ensure_ascii=False, indent=2))
        f.flush()
        with contextlib.suppress(OSError):
            os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


_REASON_MAX = 200
_REASON_ESCAPES = str.maketrans({"|": "｜", "*": "＊", "#": "＃", "`": "｀", "<": "＜", ">": "＞",
                                    "[": "［", "]": "］", "(": "（", ")": "）"})


def _collapse(text: Any) -> str:
    return " ".join(text.split()) if isinstance(text, str) else ""


def _safe_reason(text: Any) -> str:
    """A free-text reason as one line of plain text for the system-generated card: whitespace (newlines too) collapsed to single
    spaces, the markdown characters ``| * # ` < > [ ] ( )`` replaced by their full-width forms, cut to 200 characters with ``…``.
    Idempotent, so the card applies it again to reasons stored before it existed."""
    out = _collapse(text).translate(_REASON_ESCAPES)
    return out if len(out) <= _REASON_MAX else out[: _REASON_MAX - 1] + "…"


def update_contract(session_id: str, contract: dict, *, seq: int | None = None, strategy_id: str | None = None,
                    reason: str | None = None) -> dict | None:
    """Compare what a backtest measured (``contract``) with the session's active research contract.

    No contract yet: ``contract`` becomes the active one, None. Same strategy (``contract_diff`` empty): None, the active
    contract is kept. Otherwise a history entry is appended (``declared`` = a ``reason`` of at least 10 characters), the
    contract becomes the active one and the entry is returned. A run is never refused; the card shows the change."""
    from backtest.research_contract import contract_diff

    path = _session_dir(session_id) / CONTRACT_FILE
    with _flock(path):
        current = _read_contract_file(session_id)
        if current is None:
            _write_json_atomic(path, {"active": contract, "history": []})
            return None
        fields = contract_diff(current["active"], contract)
        if not fields:
            known = contract.get("interval")  # a contract file from before ``interval`` learns it, no change is recorded
            if known not in (None, "unknown") and current["active"].get("interval") in (None, "unknown"):
                _write_json_atomic(path, {"active": {**current["active"], "interval": known}, "history": current["history"]})
            return None
        text = _safe_reason(reason)
        entry = {"seq": seq, "strategy_id": strategy_id, "from": current["active"], "to": contract, "fields": fields,
                 "declared": len(_collapse(reason)) >= 10, "reason": text or None, "ts": int(time.time() * 1000)}
        _write_json_atomic(path, {"active": contract, "history": [*current["history"], entry]})
        return entry


def contract_history(session_id: str) -> list[dict]:
    """The session's recorded contract changes in order."""
    current = _read_contract_file(session_id)
    return current["history"] if current else []


def ledger_rows(session_id: str) -> list[dict]:
    """The private ledger of ``session_id`` in append order."""
    path = _session_dir(session_id) / LEDGER_FILE
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and isinstance(row.get("strategy_id"), str):
                        rows.append(row)
    except OSError:
        return []
    return rows


def verified_card(row: dict) -> dict | None:
    """The run card a ledger row names, re-read; None when it is missing or its sha256 differs from the row's."""
    try:
        path = Path(str(row["run_card_path"]))
        if _sha256(path) != row.get("run_card_sha256"):
            return None
        card = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return card if isinstance(card, dict) else None


def session_hypothesis(session_id: str, ir_name: str = "") -> str:
    """The conversation's hypothesis id; created (status ``testing``) the first time."""
    path = _session_dir(session_id) / HYPOTHESIS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with _flock(path):
        try:
            hid = json.loads(path.read_text(encoding="utf-8")).get("hypothesis_id")
        except (OSError, ValueError, AttributeError):
            hid = None
        if isinstance(hid, str) and hid:
            return hid
        reg = _registry()
        with _flock(Path(reg.path)):
            hyp = reg.create(title=f"IR research {session_id}", thesis=ir_name or session_id, status="testing",
                             skills=["alphakeel-handoff"], data_sources=["alphakeel_b2"])
        path.write_text(json.dumps({"hypothesis_id": hyp.hypothesis_id}) + "\n", encoding="utf-8")
        return hyp.hypothesis_id


def _parameters(ir_doc: Any) -> dict:
    params = (ir_doc or {}).get("parameters") if isinstance(ir_doc, dict) else None
    return {k: v.get("value") for k, v in params.items() if isinstance(v, dict)} if isinstance(params, dict) else {}


def _json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def trials_for(session_id: str, run_dir: str | Path, strategy_id: str, *, ir_doc: dict | None = None,
               window: dict | None = None, ir_name: str = "") -> tuple[int, str]:
    """``(count, evidence_path)``: the distinct ``strategy_id`` in the conversation's private ledger plus this one plus the
    distinct generic-engine trial keys, and ``<run_dir>/ir_trials_evidence.jsonl`` with one line per version / engine trial
    (strategy_id -- ``engine:<trial_key[:24]>`` for an engine trial --, parameter values, window, run_dir). The research
    service takes the evidence file as an opaque, hashed file (no line schema), so engine lines share it."""
    session_hypothesis(session_id, ir_name)
    versions: dict[str, dict] = {}
    for row in ledger_rows(session_id):  # in append order: the latest run of a version wins
        card_path = Path(str(row.get("run_card_path")))
        card = _json_file(card_path) or {}
        versions[row["strategy_id"]] = {"strategy_id": row["strategy_id"], "parameters": _parameters(_json_file(card_path.parent / "ir.json")),
                                        "window": card.get("window"), "run_dir": str(card_path.parent)}
    if strategy_id not in versions:
        versions[strategy_id] = {"strategy_id": strategy_id, "parameters": _parameters(ir_doc), "window": window, "run_dir": None}
    for key, row in _latest_engine_rows(session_id).items():  # one trial per distinct engine result
        sid = f"engine:{key[:24]}"
        versions[sid] = {"strategy_id": sid, "parameters": {}, "window": None, "run_dir": row.get("snapshot_dir")}
    path = Path(run_dir) / TRIALS_EVIDENCE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(v, sort_keys=True, ensure_ascii=False) + "\n" for v in versions.values()), encoding="utf-8")
    return len(versions), str(path)


def record(session_id: str, run_dir: str | Path, result: dict, evidence_dir: str | None, *,
           supersedes: str | None = None) -> None:
    """After a successful ``ir backtest`` (its CLI result: ``run_dir``, ``run_card``): append the private ledger row, then
    link the run to the conversation's hypothesis (``HypothesisRegistry.link_backtest``)."""
    card_path = Path(str(result["run_card"])).resolve()
    card = json.loads(card_path.read_text(encoding="utf-8"))
    row = {"strategy_id": card["strategy_id"], "name": card.get("name"), "run_card_path": str(card_path),
           "run_card_sha256": _sha256(card_path), "evidence_dir": evidence_dir, "run_dir": str(run_dir),
           "ts": int(time.time() * 1000)}
    from backtest.research_contract import contract_of_ir

    try:  # the contract never fails the backtest; a change is on the row and on the card
        row["contract_change"] = update_contract(session_id, contract_of_ir(card), strategy_id=card["strategy_id"], reason=supersedes)
    except Exception:  # noqa: BLE001
        row["contract_change"] = None
    # the row is a trial for the DSR floor: durable before the call returns
    _append_durable(_session_dir(session_id) / LEDGER_FILE, row)
    hid = session_hypothesis(session_id, card.get("name") or "")
    scr = card.get("screen") or {}
    metrics = {"strategy_id": card["strategy_id"], "name": card.get("name"), "window": card.get("window"),
               "trade_count": card["counts"]["closed"], "net_usdt": card["amounts"]["equity_end"],
               "fees_usdt": card["amounts"]["fees"], "max_drawdown": card["max_drawdown"]["fraction_of_capital"],
               "evidence_status": scr.get("evidence_status"), "fees_source": card["assumptions"].get("fees_source")}
    reg = _registry()
    with _flock(Path(reg.path)):
        reg.link_backtest(hid, run_card_path=str(card_path), backtest_run_dir=str(result.get("run_dir") or card_path.parent),
                          metrics=metrics, validation={"screen": scr.get("items") or []})


# ---------------------------------------------------------------------------------------------------------------------
# generic-engine trial ledger (``backtest`` tool) and the hold-out cutoff
# ---------------------------------------------------------------------------------------------------------------------

def engine_rows(session_id: str) -> list[dict]:
    """The private engine ledger of ``session_id`` in append order."""
    rows = []
    try:
        with open(_session_dir(session_id) / ENGINE_LEDGER_FILE, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and isinstance(row.get("trial_key"), str):
                        rows.append(row)
    except OSError:
        return []
    return rows


def _latest_engine_rows(session_id: str) -> dict[str, dict]:
    """``trial_key`` -> its latest engine ledger row (append order: the latest row of a key wins, except that a system
    sweep row never replaces a row the model ran with the same result)."""
    latest: dict[str, dict] = {}
    for r in engine_rows(session_id):
        held = latest.get(r["trial_key"])
        if r.get("sweep") and held is not None and not held.get("sweep"):
            continue
        latest[r["trial_key"]] = r
    return latest


def trial_count(session_id: str, extra_key: str | None = None) -> int:
    """The conversation's trials: distinct engine trial keys (plus ``extra_key``, the run being recorded) + distinct IR
    ``strategy_id``. One function for record time and render time."""
    keys = set(_latest_engine_rows(session_id)) | ({extra_key} if extra_key else set())
    return len(keys) + len({r["strategy_id"] for r in ledger_rows(session_id)})


def variant_sharpes_for(session_id: str, key: str | None) -> list[float]:
    """The daily Sharpes of the other engine trials (latest row per key, ``key`` itself excluded). System sweep rows count
    in the trial number but not here: near-identical neighbours would shrink the cross-trial variance of the DSR."""
    out = []
    for k, r in _latest_engine_rows(session_id).items():
        v = r.get("sharpe_daily")
        if k != key and not r.get("sweep") and isinstance(v, (int, float)) and not isinstance(v, bool):
            out.append(float(v))
    return out


def _input_key(run_dir: Path) -> str:
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    code = (run_dir / "code" / "signal_engine.py").read_bytes()
    return hashlib.sha256(code + b"\0" + json.dumps(config, sort_keys=True).encode("utf-8")).hexdigest()


def _trial_key(run_dir: Path) -> str:
    """sha256 of the engine-written ``artifacts/metrics.csv``: the trial is what the engine produced, not what the model
    wrote into code or config (a signal engine can read its parameters from any file). Only a run without metrics falls
    back to the input hash (such a run is not screened anyway)."""
    try:
        return hashlib.sha256((run_dir / "artifacts" / "metrics.csv").read_bytes()).hexdigest()
    except OSError:
        return _input_key(run_dir)


def _supersedes_of(run_dir: Path) -> str | None:
    """``supersedes_contract`` (a string) of the run's ``config.json``, else None."""
    try:
        value = json.loads((run_dir / "config.json").read_text(encoding="utf-8")).get("supersedes_contract")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) else None


def _interval_of(run_dir: Path) -> str:
    """``interval`` (a string) of the run's ``config.json``; ``1D`` when absent, as ``backtest/runner.py`` defaults it."""
    try:
        value = json.loads((run_dir / "config.json").read_text(encoding="utf-8")).get("interval", "1D")
    except (OSError, ValueError, AttributeError):
        return "1D"
    return value if isinstance(value, str) and value.strip() else "1D"


def _drift_message(change: dict) -> str:
    fields = ", ".join(change.get("fields") or [])
    if change.get("declared"):
        return f"Declared strategy change ({fields}): {change.get('reason')}"
    return (f"This run is a different strategy from the session's research contract (changed: {fields}). Tell the user the "
            "strategy changed and why. To declare the change, set config.json \"supersedes_contract\": \"<reason, at least 10 "
            "characters>\" and rerun.")


def record_engine_run(session_id: str, run_dir: Path, *, sweep: dict | None = None) -> dict:
    """After a successful generic ``backtest``: count the trial, screen the run, snapshot it and append the private row.

    ``trials`` = ``trial_count`` (distinct engine ``trial_key`` values, this one included, + distinct IR ``strategy_id`` of
    the conversation). Returns the screen dict plus ``trials`` and ``seq``. ``sweep`` (``{"parent_seq", "label"}``) marks
    a system sweep run of ``record_robustness``: it is a trial like any other row, never touches the research contract,
    is not a row of the card's table and never makes ``session_has_passed_trial`` true."""
    from backtest.engine_screen import screen_engine_run

    run_dir = Path(run_dir)
    key = _trial_key(run_dir)
    try:
        input_key = _input_key(run_dir)
    except (OSError, ValueError):
        input_key = None
    prior = engine_rows(session_id)
    trials = trial_count(session_id, key)
    screen = screen_engine_run(run_dir, trials=trials, variant_sharpes=variant_sharpes_for(session_id, key), include_returns=True)
    returns = screen.pop("daily_returns", None)
    dates = screen.pop("daily_dates", None)
    runs = _session_dir(session_id) / ENGINE_RUNS_DIR
    seq = len(prior) + 1
    while True:
        snap = runs / f"{seq:04d}"
        try:
            snap.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            seq += 1
    for rel in ("code/signal_engine.py", "config.json", "artifacts/metrics.csv"):
        src = run_dir / rel
        if src.is_file():
            (snap / rel).parent.mkdir(parents=True, exist_ok=True)
            (snap / rel).write_bytes(src.read_bytes())
    returns_path = snap / RETURNS_FILE
    returns_path.write_text(json.dumps(returns if isinstance(returns, list) else []), encoding="utf-8")
    dates_path = snap / RETURNS_DATES_FILE
    dates_path.write_text(json.dumps(dates if isinstance(dates, list) else []), encoding="utf-8")
    screen_path = snap / "screen.json"
    screen_path.write_text(json.dumps(screen, sort_keys=True, ensure_ascii=False, indent=2), encoding="utf-8")
    sharpe = (screen.get("facts") or {}).get("sharpe_daily")
    from backtest.research_contract import contract_of_engine

    facts = screen.get("facts") or {}
    change = None
    if sweep is None and screen.get("evidence_status") != "unavailable" and facts.get("codes"):  # a run that measured nothing is not a contract
        try:  # the contract never fails the backtest; a change is on the row, on the card and in the returned dict
            change = update_contract(session_id, contract_of_engine(facts, interval=_interval_of(run_dir)), seq=seq,
                                     reason=_supersedes_of(run_dir))
        except Exception:  # noqa: BLE001
            change = None
    code_snap, config_snap = snap / "code" / "signal_engine.py", snap / "config.json"
    row = {"seq": seq, "trial_key": key, "input_key": input_key, "run_dir": str(run_dir), "snapshot_dir": str(snap),
           "code_sha256": _sha256(code_snap) if code_snap.is_file() else None,
           "config_sha256": _sha256(config_snap) if config_snap.is_file() else None,
           "screen_sha256": _sha256(screen_path), "returns_sha256": _sha256(returns_path),
           "returns_dates_sha256": _sha256(dates_path),
           "sharpe_daily": sharpe if isinstance(sharpe, (int, float)) else None, "ts": int(time.time() * 1000),
           "contract_change": change}
    if sweep is not None:
        row["sweep"] = {"parent_seq": sweep.get("parent_seq"), "label": sweep.get("label")}
    _append_durable(_session_dir(session_id) / ENGINE_LEDGER_FILE, row)
    out = {**screen, "trials": trials, "seq": seq,
           "trial_files": [f"trials/{seq:04d}/code/signal_engine.py", f"trials/{seq:04d}/config.json"]}
    if change is not None:
        out["contract_drift"] = {**change, "message": _drift_message(change)}
    return out


def _same_json(path: Path, doc: dict) -> bool:
    try:
        return json.loads(path.read_text(encoding="utf-8")) == doc
    except (OSError, ValueError):
        return False


def record_robustness(session_id: str, run_dir: Path, parent_seq: int) -> dict | None:
    """After the sweep pass of ``backtest/runner.py --robustness`` over ``run_dir`` (engine row ``parent_seq``): record
    every sweep run as an engine row (``record_engine_run(..., sweep=...)``, so it counts as a trial) and append the
    sweep to ``robustness.jsonl``: ``parent_seq``, ``runs`` (the plan ``robustness.variants`` recomputes from the run's
    ``config.json``, each with ``status`` ``ok`` + ``seq``, ``failed`` + ``error`` or ``missing``) and ``skipped``. A run
    is recorded (a trial) whenever its metrics exist, its ``config.json`` is the planned variant config and its
    ``code/signal_engine.py`` is the run's code; it is ``ok`` when the manifest also says so, else ``failed`` with its
    ``seq`` (a result that exists is counted even when the run then failed). None when the config declares nothing."""
    from backtest import robustness as rb

    run_dir = Path(run_dir)
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    rb.validate(config)
    if not rb.declared(config):
        return None
    planned, skipped = rb.variants(config)
    manifest = _json_file(run_dir / "robustness" / "manifest.json")
    entries = manifest.get("runs") if isinstance(manifest, dict) and isinstance(manifest.get("runs"), list) else []
    by_label = {e.get("label"): e for e in entries if isinstance(e, dict)}
    code_sha = _sha256(run_dir / "code" / "signal_engine.py")
    runs = []
    for run in planned:
        entry: dict[str, Any] = {**run, "status": "missing", "seq": None}
        reported = by_label.get(run["label"]) if isinstance(by_label.get(run["label"]), dict) else None
        reported_ok = reported is not None and reported.get("status") == "ok"
        if reported is not None and not reported_ok:
            entry.update(status="failed", error=_collapse(reported.get("error"))[:300])
        vdir = run_dir / "robustness" / run["label"]
        if (vdir / "artifacts" / "metrics.csv").is_file():
            code = vdir / "code" / "signal_engine.py"
            if not _same_json(vdir / "config.json", rb.variant_config(config, run)):
                entry.update(status="failed", error="config.json is not the planned sweep config")
            elif not code.is_file() or _sha256(code) != code_sha:
                entry.update(status="failed", error="code/signal_engine.py is not the run's code")
            else:
                rec = record_engine_run(session_id, vdir, sweep={"parent_seq": parent_seq, "label": run["label"]})
                entry.update(status="ok" if reported_ok else "failed", seq=rec.get("seq"))
        elif reported_ok:
            entry.update(status="failed", error="no artifacts/metrics.csv")
        runs.append(entry)
    doc = {"parent_seq": parent_seq, "runs": runs, "skipped": skipped, "ts": int(time.time() * 1000)}
    _append_durable(_session_dir(session_id) / ROBUSTNESS_FILE, doc)
    return doc


def robustness_records(session_id: str) -> list[dict]:
    """The sweeps of ``robustness.jsonl`` in append order."""
    out = []
    try:
        with open(_session_dir(session_id) / ROBUSTNESS_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    doc = json.loads(line)
                except ValueError:
                    continue
                if isinstance(doc, dict) and isinstance(doc.get("parent_seq"), int) and isinstance(doc.get("runs"), list):
                    out.append(doc)
    except OSError:
        return []
    return out


def current_engine_screen(session_id: str, seq: Any) -> dict | None:
    """The current screen (``_current_screen``) of engine row ``seq``; None without such a row."""
    for row in engine_rows(session_id):
        if row.get("seq") == seq:
            return _current_screen(session_id, row, verified_engine_screen(row))
    return None


def verified_engine_returns(row: dict) -> list[float] | None:
    """The snapshot ``returns.json`` an engine ledger row names, re-read; None when missing, mistyped or its sha256 differs."""
    try:
        path = Path(str(row["snapshot_dir"])) / RETURNS_FILE
        if _sha256(path) != row.get("returns_sha256"):
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(data, list) or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in data):
        return None
    return [float(x) for x in data]


def verified_engine_dates(row: dict) -> list[str] | None:
    """The snapshot ``returns_dates.json`` (ISO ``YYYY-MM-DD``, one per daily return) an engine ledger row names, re-read;
    None for a legacy row without dates, or when the file is missing, mistyped or its sha256 differs."""
    try:
        path = Path(str(row["snapshot_dir"])) / RETURNS_DATES_FILE
        if not row.get("returns_dates_sha256") or _sha256(path) != row.get("returns_dates_sha256"):
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(data, list) or not all(isinstance(x, str) and len(x) == 10 for x in data):
        return None
    return list(data)


def verified_engine_screen(row: dict) -> dict | None:
    """The snapshot ``screen.json`` an engine ledger row names, re-read; None when missing or its sha256 differs."""
    try:
        path = Path(str(row["snapshot_dir"])) / "screen.json"
        if _sha256(path) != row.get("screen_sha256"):
            return None
        screen = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return screen if isinstance(screen, dict) else None


def _fallback_cutoff_ms(now_ms: int | None = None) -> int:
    now = int(time.time() * 1000) if now_ms is None else now_ms
    day = 86_400_000
    return (now // day) * day - DEFAULT_HOLDOUT_DAYS * day


def cached_holdout_cutoff_ms(session_id: str | None, *, max_age_seconds: float | None = None) -> int | None:
    """The cutoff in the session's cache file (no network), or None; ``max_age_seconds`` bounds the cache age."""
    if not session_id:
        return None
    try:
        data = json.loads((_session_dir(session_id) / HOLDOUT_FILE).read_text(encoding="utf-8"))
        cutoff, ts = data.get("cutoff_ms"), data.get("ts")
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(cutoff, bool) or not isinstance(cutoff, int):
        return None
    if max_age_seconds is not None:
        if isinstance(ts, bool) or not isinstance(ts, (int, float)) or time.time() * 1000 - ts > max_age_seconds * 1000:
            return None
    return cutoff


def holdout_cutoff(session_id: str | None) -> dict:
    """``{"cutoff_ms", "source"}``: AlphaKeel's hold-out start. ``source`` is ``service`` (the research service's
    ``holdout_cutoff_ms``, cached per session for 6 h) or ``fallback_60d`` (UTC day floor of now minus 60 days, used
    when the service cannot be asked). Only a service answer is cached in a file, and only with a session id; a fallback
    answer is remembered in memory for ``HOLDOUT_FALLBACK_MEMO_SECONDS`` so a down service is not asked on every call."""
    cached = cached_holdout_cutoff_ms(session_id, max_age_seconds=HOLDOUT_CACHE_SECONDS)
    if cached is not None:
        return {"cutoff_ms": cached, "source": "service"}
    memo = _FALLBACK_MEMO.get(session_id)
    if memo is not None and time.monotonic() - memo[0] < HOLDOUT_FALLBACK_MEMO_SECONDS:
        return {"cutoff_ms": memo[1], "source": "fallback_60d"}
    try:
        from src.tools.alphakeel_ir_request import service_check

        v = service_check().get("holdout_cutoff_ms")
        if isinstance(v, bool) or not isinstance(v, int):
            raise TypeError("holdout_cutoff_ms is not an int")
    except Exception:  # noqa: BLE001 - no answer from the service: the conservative fallback applies
        fallback = _fallback_cutoff_ms()
        _FALLBACK_MEMO[session_id] = (time.monotonic(), fallback)
        return {"cutoff_ms": fallback, "source": "fallback_60d"}
    _FALLBACK_MEMO.pop(session_id, None)
    if session_id:
        path = _session_dir(session_id) / HOLDOUT_FILE
        with contextlib.suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
            with _flock(path):
                path.write_text(json.dumps({"cutoff_ms": v, "ts": int(time.time() * 1000)}), encoding="utf-8")
    return {"cutoff_ms": v, "source": "service"}


# ---------------------------------------------------------------------------------------------------------------------
# result card (system text appended after the answer; every figure re-read from the verified run cards)
# ---------------------------------------------------------------------------------------------------------------------

_TEXT = {
    True: {"title": "**研究结果（系统根据试验账本生成，非模型撰写）**", "fees": "费率口径：", "per_fill": "每笔",
           "default": "公开基础档 taker（工具默认）", "request": "请求给定费率",
           "summary": "本会话 IR {n} 个版本 / {m} 次回测：通过初筛 {p}，未通过 {f}，证据不足 {i}",
           "head": "| 版本 | 窗口 | 成交 | 净收益 USDT | 价差盈亏 USDT | 手续费 USDT | 资金费 USDT | 最大回撤 | 判定 |",
           "passed": "通过初筛，1.4–1.13 未评估", "failed": "未通过初筛（{ids}）", "insufficient": "证据不足（{ids}）",
           "more": "另有 {n} 次回测见账本", "tampered": "，已改动 {n}", "tampered_row": "run card 已改动或缺失",
           "not_evaluated": "1.4–1.13（成本压力、DSR、PBO、邻域、走步等）未评估：需要 ir_robustness。",
           "ledger": "账本：{file}"},
    False: {"title": "**Research result (generated by the system from the trial ledger, not written by the model)**",
            "fees": "Fee basis: ", "per_fill": "per fill", "default": "public base-tier taker (tool default)",
            "request": "fees given in the request",
            "summary": "This session's IR: {n} versions / {m} backtests: passed screen {p}, failed screen {f}, insufficient {i}",
            "head": "| Version | Window | Trades | Net USDT | Price PnL USDT | Fees USDT | Funding USDT | Max drawdown | Status |",
            "passed": "passed screen; 1.4–1.13 not evaluated", "failed": "failed screen ({ids})", "insufficient": "insufficient ({ids})",
            "more": "{n} more backtests in the ledger", "tampered": ", modified {n}", "tampered_row": "run card modified or missing",
            "not_evaluated": "1.4–1.13 (cost stress, DSR, PBO, neighbours, walk-forward, ...) not evaluated: needs ir_robustness.",
            "ledger": "Ledger: {file}"},
}
_SCREEN_GROUPS = {"insufficient": ("1.1", "1.2"), "failed_screen": ("1.3", "1.16")}
_CLASS_ORDER = ("passed_screen", "failed_screen", "insufficient")


def _dec(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _two(d: Decimal) -> str:
    s = format(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN), "f")
    return s.replace("-", "−")


def fmt_amount(v: Any) -> str:
    """A decimal at 2 decimals (U+2212 minus); ``—`` when missing."""
    d = _dec(v)
    return "—" if d is None else _two(d)


def fmt_fraction(v: Any) -> str:
    """A fraction as a percent at 2 decimals."""
    d = _dec(v)
    return "—" if d is None else _two(d * 100) + "%"


def _pct(rate: Any) -> str:
    """A fee rate as an exact percent (``0.00055`` -> ``0.055%``)."""
    d = (_dec(rate) or Decimal(0)) * 100
    return ("0" if d.is_zero() else format(d.normalize(), "f")) + "%"


def _window(w: Any) -> str:
    if not isinstance(w, dict) or not w.get("start_day") or not w.get("end_day"):
        return "—"
    a, b = str(w["start_day"]), str(w["end_day"])
    return f"{a}→{b[5:]}" if a[:4] == b[:4] else f"{a}→{b}"


def _status(card: dict) -> Any:
    return (card.get("screen") or {}).get("evidence_status")


def _verdict(card: dict, t: dict) -> str:
    status = _status(card)
    if status == "passed_screen":
        return t["passed"]
    if status in _SCREEN_GROUPS:
        items = (card.get("screen") or {}).get("items") or []
        ids = [i.get("id") for i in items if isinstance(i, dict) and i.get("result") == "FAIL" and i.get("id") in _SCREEN_GROUPS[status]]
        return t["failed" if status == "failed_screen" else "insufficient"].format(ids=", ".join(ids) or "?")
    return str(status or "—")


def _maker_note(row: dict) -> str:
    """`` (maker 0.02%)`` when the venue's fee row carries a maker perp rate, else nothing."""
    maker = row.get("maker")
    rate = maker.get("perp") if isinstance(maker, dict) else None
    return f" (maker {_pct(rate)})" if _dec(rate) is not None else ""


def _fee_lines(cards: list[dict], t: dict) -> list[str]:
    """One line per distinct fee basis: its source and every venue's rate, from the run cards."""
    seen: list[tuple[str, str]] = []
    for card in cards:
        a = card.get("assumptions") or {}
        src, fees = str(a.get("fees_source") or ""), a.get("fees")
        rates = " / ".join(f"{v} {_pct(m['perp'])}{_maker_note(m)}" for v, m in sorted(fees.items())
                           if isinstance(m, dict) and _dec(m.get("perp")) is not None) if isinstance(fees, dict) else ""
        label = t["default"] if src.startswith("public base-tier taker") else (t["request"] if src == "request" else src)
        if (label, rates) not in seen:
            seen.append((label, rates))
    return [f"{t['fees']}{label} — {rates} {t['per_fill']}" if rates else f"{t['fees']}{label}" for label, rates in seen]


def _ir_section(session_id: str, zh: bool) -> list[str]:
    """The IR part of the result card (no lines when the private IR ledger is empty).

    Header N (distinct ``strategy_id``) and M (backtests) count every private ledger row, as ``trials.count`` does. A row
    whose run card was changed or is missing keeps its line (name and ``strategy_id`` from the ledger, ``—`` for every
    figure, the status says so), sorts last, is always shown and counts in no screen class.

    Rows sort by screen class (passed, failed, insufficient, other), then net descending. Every row whose sample is large
    enough to be screened (passed or failed, i.e. 1.1/1.2 held) is always shown, so a large-sample loss is never cut in
    favour of small-sample winners (2026-10-17 research session 90b12bd25f7a: the 288-trade run fell off a top-10-by-net
    card). ``价差盈亏`` is ``amounts.price_pnl_closed``, the gross the fees are weighed against."""
    rows_all = ledger_rows(session_id)
    if not rows_all:
        return []
    t = _TEXT[bool(zh)]
    entries = [(row, verified_card(row)) for row in rows_all]
    cards = [c for _, c in entries if c is not None]
    bad = sum(1 for _, c in entries if c is None)
    counts = {"passed_screen": 0, "failed_screen": 0, "insufficient": 0}
    for c in cards:
        if _status(c) in counts:
            counts[_status(c)] += 1
    versions = {row["strategy_id"] for row in rows_all}

    def key(i: int):
        c = entries[i][1]
        if c is None:
            return (len(_CLASS_ORDER) + 1, Decimal(0))
        d = _dec((c.get("amounts") or {}).get("equity_end"))
        rank = _CLASS_ORDER.index(_status(c)) if _status(c) in _CLASS_ORDER else len(_CLASS_ORDER)
        return (rank, -(Decimal("-Infinity") if d is None else d))

    order = sorted(range(len(entries)), key=key)
    always = {i for i, (_, c) in enumerate(entries) if c is None or _status(c) in ("passed_screen", "failed_screen")}
    shown = set(order[:CARD_MAX_ROWS]) | always
    rows = []
    for i in order:
        if i not in shown:
            continue
        row, c = entries[i]
        if c is None:
            name = f"{row.get('name') or '—'} ({str(row.get('strategy_id') or '')[:8]})"
            rows.append(f"| {name} | — | — | — | — | — | — | — | {t['tampered_row']} |")
            continue
        name = f"{c.get('name') or '—'} ({str(c.get('strategy_id') or '')[:8]})"
        rows.append(f"| {name} | {_window(c.get('window'))} | {(c.get('counts') or {}).get('closed', '—')} | "
                    f"{fmt_amount((c.get('amounts') or {}).get('equity_end'))} | "
                    f"{fmt_amount((c.get('amounts') or {}).get('price_pnl_closed'))} | "
                    f"{fmt_amount((c.get('amounts') or {}).get('fees'))} | {fmt_amount((c.get('amounts') or {}).get('funding'))} | "
                    f"{fmt_fraction((c.get('max_drawdown') or {}).get('fraction_of_capital'))} | {_verdict(c, t)}{_undeclared_mark(row, _CONTRACT_TEXT[bool(zh)])} |")
    summary = t["summary"].format(n=len(versions), m=len(entries), p=counts["passed_screen"], f=counts["failed_screen"],
                                  i=counts["insufficient"]) + (t["tampered"].format(n=bad) if bad else "")
    lines = ["---", t["title"], *_fee_lines(cards, t), summary, t["head"], "|---|---|---|---|---|---|---|---|---|", *rows]
    if len(entries) > len(rows):
        lines.append(t["more"].format(n=len(entries) - len(rows)))
    lines += [t["not_evaluated"], t["ledger"].format(file=TRIALS_EVIDENCE_FILE)]
    return lines


_ENGINE_TEXT = {
    True: {"title": "**通用引擎回测（backtest 工具；系统根据试验账本生成，非模型撰写）· 初筛口径：AlphaKeel G1**",
           "summary": "通用引擎回测 {m} 次 / {n} 个不同结果（按 metrics 去重）：通过初筛 {p}，未通过 {f}，证据不足 {i}",
           "head": "| # | 代码 | 标的 / 来源 | 窗口天数 | 往返笔数 | 净收益率 | DSR（试验数） | 方向 / 在场时间 | 判定 |",
           "passed": "通过初筛，{ne} 未评估", "failed": "未通过初筛（{ids}）", "insufficient": "证据不足（{ids}）",
           "unavailable": "初筛不可用", "tampered": "，已改动 {n}", "tampered_row": "快照已改动或缺失", "code_new": "新",
           "more": "另有 {n} 次回测见账本",
           "zoo_unused": "- #{seq}：代码里写了 {ids} 的名字，但没有调用因子库实现（zoo_factor）；这些规则是手写的，不是因子库因子",
           "zoo_used": "- #{seq} 调用因子库：{ids}",
           "mt": "- 多重比较（{k} 个不同版本{cap}，共同 {t} 天）：表现最好的是 #{seq}；Hansen SPA p={p:.3f}（平稳自助法 {n} 次，平均块长 {b:g}）{luck}。参考信息，不改变判定。",
           "mt_cap": "，仅取最近 {m} 个", "mt_luck": "——无法和运气区分",
           "pbo": "- 回测过拟合概率 PBO={pbo:.2f}（CSCV，S={S}，{n} 种切分）。",
           "split": "- #{seq}：前 75% / 后 25% 的 Sharpe {is_s} / {oos_s}；平稳自助法 Sharpe 95% 区间 [{lo}, {hi}]。",
           "mt_skipped": "（{n} 个旧版本没有日期，未计入）",
           "mt_mismatch": "（{n} 个版本的收益快照与记录不符，未计入）",
           "mt_short": "（{n} 个版本回测不足 60 天，未计入多重比较）",
           "sweeps": "（其中 {n} 次是系统参数扫描 / 组件消融，计入试验数，不在下表）", "sep": "；", "mt_sweep_of": "（#{p} 的系统扫描）",
           "rb_head": "- #{seq} 参数邻域（系统扫描 {n} 个邻点，每个参数 ±1 档或 ±20%）：本点 Sharpe {s}、往返 {rt}；{share}。参考信息，不改变判定。",
           "rb_share": "{b}/{n} 个邻点的 Sharpe 低于本点的一半{flag}", "rb_share_na": "本点 Sharpe 不为正，不计算低于一半的比例",
           "rb_spike": "——结果依赖精确的参数取值（参数尖峰）",
           "rb_param": "  - {name}={base}：{parts}{note}", "rb_point": "Sharpe {s}（往返 {rt}）",
           "rb_same": "结果与本点完全相同", "rb_failed": "回测失败", "rb_mismatch": "快照与记录不符",
           "rb_unwired": "（两个邻点都与本点完全相同：代码可能没有读取 self.PARAMS[\"{name}\"]）",
           "rb_zero_without_step": "  - {name}：取值为 0 且没有 step，未扫描",
           "rb_out_of_range": "  - {name}：有邻点超出 min/max，未运行",
           "rb_ablation": "- #{seq} 组件消融（系统逐个关闭）：{parts}{flag}。参考信息，不改变判定。",
           "rb_delta": "{point}，变化 {d:+.2f}", "rb_below": "，低于本点一半",
           "rb_all_needed": "——去掉任何一个组件 Sharpe 都低于一半：结果依赖全部组件的叠加，是过拟合迹象",
           "rb_failed_note": "（{n} 次扫描回测失败；已有结果的仍计入试验数）",
           "mt_sweep_capped": "（{n} 个较早的系统扫描版本超出上限，未计入多重比较；仍计入试验数）",
           "rb_trials": "（以上 {n} 次扫描回测按不同结果计入本会话的试验数：DSR、SPA/PBO）",
           "no_live": "通用引擎结果不能直接上实盘：须交接 Strategy IR，通过 ir_robustness 与 AlphaKeel G1/G2。",
           "non_auditable": "数据不可审计（{srcs}）：实时拉取的数据无法逐位复现，不能作为 AlphaKeel 证据。",
           "holdout": "⚠ 本会话读取过 AlphaKeel hold-out 区间的数据（截至 {day}，hold-out 起点 {cutoff_day}）：本会话的研究不能交接 AlphaKeel（G1 1.14）。",
           "long_only": "仅多", "short_only": "仅空", "long_short": "多空", "flat": "空仓"},
    False: {"title": "**Generic engine backtests (`backtest` tool; generated by the system from the trial ledger, not written by the model) · Screen basis: AlphaKeel G1**",
            "summary": "Generic engine backtests {m} / {n} distinct results (by metrics): passed screen {p}, failed screen {f}, insufficient {i}",
            "head": "| # | code | Symbols / source | Window days | Round trips | Net return | DSR (trials) | Direction / time in market | Status |",
            "passed": "passed screen; {ne} not evaluated", "failed": "failed screen ({ids})", "insufficient": "insufficient ({ids})",
            "unavailable": "screen unavailable", "tampered": ", modified {n}", "tampered_row": "snapshot modified or missing", "code_new": "new",
            "more": "{n} more backtests in the ledger",
            "zoo_unused": "- #{seq}: the code names {ids} but does not call the factor-library implementation (zoo_factor); these rules are hand-written, not library factors",
            "zoo_used": "- #{seq} uses library factors: {ids}",
            "mt": "- Multiple comparison ({k} distinct versions{cap}, {t} common days): the best is #{seq}; Hansen SPA p={p:.3f} (stationary bootstrap, {n} resamples, mean block {b:g}){luck}. Reference only; it does not change the verdict.",
            "mt_cap": ", latest {m} only", "mt_luck": " -- cannot be told apart from luck",
            "pbo": "- Probability of backtest overfitting PBO={pbo:.2f} (CSCV, S={S}, {n} splits).",
            "split": "- #{seq}: Sharpe first 75% / last 25% {is_s} / {oos_s}; stationary-bootstrap 95% Sharpe interval [{lo}, {hi}].",
            "mt_skipped": "({n} older versions have no dates and are not included)",
            "mt_mismatch": "({n} versions' return snapshots do not match the record and are excluded)",
            "mt_short": "({n} versions have fewer than 60 days and are not in the multiple-comparison test)",
            "sweeps": " ({n} of them are system parameter sweeps / component ablations: counted as trials, not in the table)", "sep": "; ", "mt_sweep_of": " (a system sweep of #{p})",
            "rb_head": "- #{seq} parameter neighbourhood (system sweep of {n} neighbours, each parameter +/-1 step or +/-20%): chosen point Sharpe {s}, {rt} round trips; {share}. Reference only; it does not change the verdict.",
            "rb_share": "{b}/{n} neighbours have a Sharpe below half the chosen point's{flag}", "rb_share_na": "the chosen Sharpe is not positive, so no below-half share",
            "rb_spike": " -- the result depends on the exact parameter values (a parameter spike)",
            "rb_param": "  - {name}={base}: {parts}{note}", "rb_point": "Sharpe {s} ({rt} round trips)",
            "rb_same": "identical to the chosen point", "rb_failed": "backtest failed", "rb_mismatch": "snapshot does not match the record",
            "rb_unwired": " (both neighbours are identical to the chosen point: the code may not read self.PARAMS[\"{name}\"])",
            "rb_zero_without_step": "  - {name}: value 0 without a step, not swept",
            "rb_out_of_range": "  - {name}: a neighbour is outside min/max and was not run",
            "rb_ablation": "- #{seq} component ablation (system switches each off in turn): {parts}{flag}. Reference only; it does not change the verdict.",
            "rb_delta": "{point}, change {d:+.2f}", "rb_below": ", below half the chosen point",
            "rb_all_needed": " -- removing any one component halves the Sharpe: the result needs every component stacked, a sign of overfitting",
            "rb_failed_note": "({n} sweep backtests failed; those that left a result still count as trials)",
            "mt_sweep_capped": "({n} older system sweep versions are over the cap and not in the multiple-comparison test; they still count as trials)",
            "rb_trials": "(the {n} sweep backtests above count in this session's trials by distinct result: DSR, SPA/PBO)",
            "no_live": "Engine results cannot go live directly: hand over a Strategy IR, pass ir_robustness and AlphaKeel G1/G2.",
            "non_auditable": "Data not auditable ({srcs}): live-pulled data cannot be re-read bit for bit and is not AlphaKeel evidence.",
            "holdout": "⚠ This session read data inside AlphaKeel's hold-out window (up to {day}, hold-out starts {cutoff_day}): its research cannot be handed to AlphaKeel (G1 1.14).",
            "long_only": "long only", "short_only": "short only", "long_short": "long/short", "flat": "flat"},
}
_ENGINE_GROUPS = {"insufficient": ("1.1", "1.2"), "failed_screen": ("1.3", "1.6", "1.8", "1.16")}


def _item_observed(screen: dict, item_id: str) -> Any:
    for i in screen.get("items") or []:
        if isinstance(i, dict) and i.get("id") == item_id:
            return i.get("observed")
    return None


def _ids_text(ids: Any) -> str:
    """Item ids as text, runs of three or more consecutive ids as a range: ``1.4, 1.5, 1.7, 1.9–1.15``."""
    nums = []
    for x in ids if isinstance(ids, list) else []:
        head, _, tail = str(x).partition(".")
        if head == "1" and tail.isdigit():
            nums.append(int(tail))
    nums = sorted(set(nums))
    parts, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        parts += [f"1.{nums[i]}–1.{nums[j]}"] if j - i >= 2 else [f"1.{n}" for n in nums[i:j + 1]]
        i = j + 1
    return ", ".join(parts) or "—"


def _engine_verdict(screen: dict, t: dict) -> str:
    status = screen.get("evidence_status")
    if status == "passed_screen":
        return t["passed"].format(ne=_ids_text(screen.get("not_evaluated")))
    if status in _ENGINE_GROUPS:
        ids = [i.get("id") for i in screen.get("items") or []
               if isinstance(i, dict) and i.get("result") == "FAIL" and i.get("id") in _ENGINE_GROUPS[status]]
        return t["failed" if status == "failed_screen" else "insufficient"].format(ids=", ".join(ids) or "?")
    return t["unavailable"] if status == "unavailable" else str(status or "—")


def _day(ms: int) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc).strftime("%Y-%m-%d")


def _current_screen(session_id: str, row: dict, screen: dict | None) -> dict | None:
    """The row's stored screen with item 1.6 (DSR) and the verdict recomputed for the session's CURRENT trial count and
    variant Sharpes from the snapshot's verified daily returns; None (shown as modified) when the screen or the returns
    cannot be verified. The tool result keeps the screen as computed at run time."""
    if screen is None or screen.get("evidence_status") == "unavailable":
        return screen
    returns = verified_engine_returns(row)
    if returns is None:
        return None
    from backtest.engine_screen import rescreen

    return rescreen(screen, returns, trials=trial_count(session_id), variant_sharpes=variant_sharpes_for(session_id, row.get("trial_key")))


def session_has_passed_trial(session_id: str | None) -> bool:
    """Whether any backtest of the conversation passed the screen: an IR row whose verified run card says
    ``passed_screen``, or a generic-engine row whose current screen (DSR recomputed for today's trial count) does.
    False without a session id."""
    if not session_id:
        return False
    for row in ledger_rows(session_id):
        card = verified_card(row)
        if card is not None and _status(card) == "passed_screen":
            return True
    for row in engine_rows(session_id):
        if row.get("sweep"):  # a system sweep run is not a version the model chose
            continue
        screen = _current_screen(session_id, row, verified_engine_screen(row))
        if screen is not None and screen.get("evidence_status") == "passed_screen":
            return True
    return False


def _net_return(screen: dict) -> str:
    net, base = _dec(_item_observed(screen, "1.3")), _dec((screen.get("facts") or {}).get("capital_base"))
    return "—" if net is None or base is None or base <= 0 else fmt_fraction(net / base)


MT_MAX_VERSIONS = 20
MT_MAX_SWEEP_VERSIONS = 20  # sweep rows are versions too, on top of the model's own and never instead of them
MT_MIN_VERSIONS = 3
MT_PBO_MIN_VERSIONS = 8
MT_MIN_DAYS = 60
MT_N_BOOT = 2000
MT_PBO_S = 16


def _periods_per_year(row: dict) -> int:
    """365 when every code of the row's verified screen is crypto, else 252."""
    from backtest.research_contract import _is_crypto

    codes = ((verified_engine_screen(row) or {}).get("facts") or {}).get("codes")
    codes = [c for c in codes if isinstance(c, str) and c] if isinstance(codes, list) else []
    return 365 if codes and all(_is_crypto(c) for c in codes) else 252


def _fmt2(v: Any) -> str:
    return "—" if not isinstance(v, (int, float)) or isinstance(v, bool) else f"{v:.2f}"


def _multiple_testing_lines(session_id: str, t: dict) -> list[str]:
    """Card lines computed at render time from the verified dated daily returns of the latest row of each distinct trial key:
    Hansen SPA over the versions (>= 3 and >= 60 common days), PBO (>= 8 versions), the latest row's 75/25 Sharpe split and
    bootstrap interval, and a note on legacy rows without dates. Seeds are fixed, so the card renders identically every time.
    Reference information; it never changes a verdict. Never raises."""
    try:
        return _multiple_testing_lines_unsafe(session_id, t)
    except Exception:  # noqa: BLE001 - the card never fails on a statistic
        return []


def _multiple_testing_lines_unsafe(session_id: str, t: dict) -> list[str]:
    import numpy as np

    from backtest import multiple_testing as mt

    rows = sorted(_latest_engine_rows(session_id).values(), key=lambda r: r.get("seq") if isinstance(r.get("seq"), int) else 0)
    usable: list[tuple[dict, list[str], list[float]]] = []
    skipped = mismatch = short = 0
    for row in rows:
        dates, returns = verified_engine_dates(row), verified_engine_returns(row)
        if dates is None and not row.get("returns_dates_sha256"):  # recorded before the dates were snapshotted
            skipped += 1
            continue
        if dates is None or returns is None or len(dates) != len(returns) or len(set(dates)) != len(dates):
            mismatch += 1
            continue
        usable.append((row, dates, returns))
    long_enough = [u for u in usable if len(u[2]) >= MT_MIN_DAYS]  # a short or empty run would empty the date intersection
    short = len(usable) - len(long_enough)
    lines: list[str] = []
    model = [u for u in long_enough if not u[0].get("sweep")]
    swept = [u for u in long_enough if u[0].get("sweep")]
    capped = len(model) > MT_MAX_VERSIONS
    sweep_left_out = max(0, len(swept) - MT_MAX_SWEEP_VERSIONS)
    keep = {id(u) for u in model[-MT_MAX_VERSIONS:]} | {id(u) for u in swept[len(swept) - min(len(swept), MT_MAX_SWEEP_VERSIONS):]}
    versions = [u for u in long_enough if id(u) in keep]
    if len(versions) >= MT_MIN_VERSIONS:
        common = sorted(set.intersection(*(set(d) for _, d, _ in versions)))
        if len(common) >= MT_MIN_DAYS:
            by_day = [dict(zip(d, r)) for _, d, r in versions]
            matrix = np.column_stack([[m[day] for day in common] for m in by_day])
            spa = mt.spa_test(matrix, n_boot=MT_N_BOOT, seed=0)
            best = versions[spa["best_index"]][0]
            sweep_of = (best.get("sweep") or {}).get("parent_seq")
            lines.append(t["mt"].format(
                k=len(versions), cap=t["mt_cap"].format(m=MT_MAX_VERSIONS) if capped else "", t=len(common),
                seq=f"{best.get('seq')}{t['mt_sweep_of'].format(p=sweep_of) if sweep_of is not None else ''}", p=spa["p_consistent"], n=spa["n_boot"], b=spa["mean_block"],
                luck=t["mt_luck"] if spa["p_consistent"] >= 0.05 else ""))
            if len(versions) >= MT_PBO_MIN_VERSIONS:
                pbo = mt.pbo_cscv(matrix, S=MT_PBO_S)
                if pbo is not None:
                    lines.append(t["pbo"].format(pbo=pbo["pbo"], S=pbo["S"], n=pbo["n_combinations"]))
    for row, _, returns in reversed(usable):
        if len(returns) >= MT_MIN_DAYS and not row.get("sweep"):  # the latest version the model chose
            ppy = _periods_per_year(row)
            split = mt.split_sharpe(returns, periods_per_year=ppy)
            ci = mt.block_bootstrap_sharpe_ci(returns, periods_per_year=ppy, n_boot=MT_N_BOOT, seed=0) or {}
            lines.append(t["split"].format(seq=row.get("seq"), is_s=_fmt2(split["is_sharpe"]), oos_s=_fmt2(split["oos_sharpe"]),
                                           lo=_fmt2(ci.get("ci_lower")), hi=_fmt2(ci.get("ci_upper"))))
            break
    for key, n in (("mt_skipped", skipped), ("mt_mismatch", mismatch), ("mt_short", short), ("mt_sweep_capped", sweep_left_out)):
        if n:
            lines.append(t[key].format(n=n))
    return lines


_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,39}")


def _num_text(v: Any) -> str:
    return f"{v:.10g}" if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else "?"


def _sweep_point(row: dict | None, ppy: int) -> dict | None:
    """``{"sharpe", "round_trips", "trial_key"}`` of an engine row from its verified returns and screen; None when either
    does not verify."""
    from backtest import multiple_testing as mt

    if row is None:
        return None
    returns, screen = verified_engine_returns(row), verified_engine_screen(row)
    if returns is None or screen is None:
        return None
    rt = _item_observed(screen, "1.2")
    return {"sharpe": mt.annual_sharpe(returns, ppy), "round_trips": rt if isinstance(rt, int) and not isinstance(rt, bool) else None,
            "trial_key": row.get("trial_key")}


def robustness_lines(session_id: str, zh: bool) -> list[str]:
    """Card lines of the latest sweep (``robustness.jsonl``) whose chosen point verifies: the parameter neighbourhood
    (each neighbour's annualised Sharpe and round trips, the share of neighbours below half the chosen Sharpe) and the
    component ablation (each component's Sharpe change). Computed at render time from the verified snapshots of the sweep
    rows; nothing when the session has no sweep (no strategy declared the convention). Never raises."""
    try:
        return _robustness_lines_unsafe(session_id, _ENGINE_TEXT[bool(zh)])
    except Exception:  # noqa: BLE001 - the card never fails on a statistic
        return []


def _robustness_lines_unsafe(session_id: str, t: dict) -> list[str]:
    from backtest import robustness as rb

    by_seq = {r.get("seq"): r for r in engine_rows(session_id)}
    for doc in reversed(robustness_records(session_id)):
        parent = by_seq.get(doc["parent_seq"])
        if parent is None or parent.get("sweep"):
            continue
        ppy = _periods_per_year(parent)
        chosen = _sweep_point(parent, ppy)
        if chosen is None:
            continue
        break
    else:
        return []
    seq = doc["parent_seq"]
    params: dict[str, list[tuple[dict, dict | None]]] = {}
    ablations: list[tuple[dict, dict | None]] = []
    failed = 0
    for run in doc["runs"]:
        if not isinstance(run, dict):
            continue
        name = run.get("param") if run.get("kind") == "param" else run.get("component")
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            continue
        point = None
        if run.get("status") == "ok":
            vrow = by_seq.get(run.get("seq"))
            ok = isinstance(vrow, dict) and vrow.get("sweep") == {"parent_seq": seq, "label": run.get("label")}
            point = _sweep_point(vrow, ppy) if ok else None
            if point is None:
                point = {"mismatch": True}
        else:
            failed += 1
        (params.setdefault(name, []) if run.get("kind") == "param" else ablations).append((run, point))

    def outcome(point: dict | None) -> str:
        if point is None:
            return t["rb_failed"]
        if point.get("mismatch"):
            return t["rb_mismatch"]
        if point["trial_key"] == chosen["trial_key"]:
            return t["rb_same"]
        return t["rb_point"].format(s=_fmt2(point["sharpe"]), rt=point["round_trips"] if point["round_trips"] is not None else "—")

    lines: list[str] = []
    measured = lambda pts: [p for _, p in pts if p is not None and not p.get("mismatch")]  # noqa: E731
    if params:
        neighbours = [p for pts in params.values() for p in measured(pts)]
        summary = rb.neighbourhood_summary(chosen["sharpe"], [p["sharpe"] for p in neighbours])
        if summary["share"] is None:
            share = t["rb_share_na"]
        else:
            share = t["rb_share"].format(b=summary["below_half"], n=summary["n"],
                                         flag=t["rb_spike"] if summary["below_half"] else "")
        lines.append(t["rb_head"].format(seq=seq, n=sum(len(v) for v in params.values()), s=_fmt2(chosen["sharpe"]),
                                         rt=chosen["round_trips"] if chosen["round_trips"] is not None else "—", share=share))
        for name, pts in params.items():
            base = _num_text(pts[0][0].get("base"))
            parts = [f"{_num_text(run.get('value'))} → {outcome(p)}" for run, p in pts]
            unwired = len(pts) == 2 and all(p is not None and not p.get("mismatch") and p["trial_key"] == chosen["trial_key"] for _, p in pts)
            lines.append(t["rb_param"].format(name=name, base=base, parts=t["sep"].join(parts),
                                              note=t["rb_unwired"].format(name=name) if unwired else ""))
    for item in doc.get("skipped") or []:
        name = item.get("param") if isinstance(item, dict) else None
        if isinstance(name, str) and _NAME_RE.fullmatch(name) and item.get("reason") in ("zero_without_step", "out_of_range"):
            lines.append(t["rb_" + item["reason"]].format(name=name))
    if ablations:
        parts = []
        collapsed = 0
        for run, p in ablations:
            text = outcome(p)
            if p is not None and not p.get("mismatch") and p["trial_key"] != chosen["trial_key"]:
                if p["sharpe"] is not None and chosen["sharpe"] is not None:
                    text = t["rb_delta"].format(point=text, d=p["sharpe"] - chosen["sharpe"])
                if chosen["sharpe"] is not None and chosen["sharpe"] > 0 and (p["sharpe"] is None or p["sharpe"] < chosen["sharpe"] / 2):
                    text += t["rb_below"]
                    collapsed += 1
            parts.append(f"{run['component']} → {text}")
        lines.append(t["rb_ablation"].format(seq=seq, parts=t["sep"].join(parts),
                                             flag=t["rb_all_needed"] if len(ablations) >= 2 and collapsed == len(ablations) else ""))
    if failed:
        lines.append(t["rb_failed_note"].format(n=failed))
    lines.append(t["rb_trials"].format(n=sum(1 for r in doc["runs"] if isinstance(r, dict) and isinstance(r.get("seq"), int))))
    return lines


def _engine_section(session_id: str, zh: bool, first: bool) -> list[str]:
    rows_all = engine_rows(session_id)
    if not rows_all:
        return []
    t = _ENGINE_TEXT[bool(zh)]
    sweeps = sum(1 for row in rows_all if row.get("sweep"))
    entries = [(row, _current_screen(session_id, row, verified_engine_screen(row))) for row in rows_all if not row.get("sweep")]
    screens = [s for _, s in entries if s is not None]
    bad = sum(1 for _, s in entries if s is None)
    counts = {"passed_screen": 0, "failed_screen": 0, "insufficient": 0}
    for s in screens:
        if s.get("evidence_status") in counts:
            counts[s["evidence_status"]] += 1
    shown = set(range(max(0, len(entries) - CARD_MAX_ROWS), len(entries)))
    shown |= {i for i, (_, s) in enumerate(entries) if s is None or s.get("evidence_status") == "passed_screen"}
    first_seq_of_code: dict[str, Any] = {}
    code_marks: list[str] = []
    for i, (row, _) in enumerate(entries):
        digest = row.get("code_sha256")
        if not isinstance(digest, str) or not digest:
            code_marks.append("—")
            continue
        earlier = first_seq_of_code.get(digest)
        if earlier is None:
            first_seq_of_code[digest] = row.get("seq", i + 1)
            code_marks.append(t["code_new"])
        else:
            code_marks.append(f"= #{earlier}")
    rows = []
    for i in sorted(shown):
        row, s = entries[i]
        seq = row.get("seq", i + 1)
        if s is None:
            rows.append(f"| {seq} | {code_marks[i]} | — | — | — | — | — | — | {t['tampered_row']} |")
            continue
        facts = s.get("facts") or {}
        label = ",".join(str(c) for c in facts.get("codes") or []) or "—"
        direction = t.get(str(facts.get("direction")), str(facts.get("direction") or "—"))
        rows.append(f"| {seq} | {code_marks[i]} | {label} / {facts.get('source') or '—'} | {_item_observed(s, '1.1') if _item_observed(s, '1.1') is not None else '—'} | "
                    f"{_item_observed(s, '1.2') if _item_observed(s, '1.2') is not None else '—'} | {_net_return(s)} | "
                    f"{fmt_amount(facts.get('dsr'))} ({facts.get('trials', '—')}) | {direction} / {fmt_fraction(facts.get('time_in_market'))} | "
                    f"{_engine_verdict(s, t)}{_undeclared_mark(row, _CONTRACT_TEXT[bool(zh)])} |")
    summary = t["summary"].format(m=len(rows_all), n=len({r["trial_key"] for r in rows_all}), p=counts["passed_screen"],
                                  f=counts["failed_screen"], i=counts["insufficient"]) + (t["tampered"].format(n=bad) if bad else "") + (
        t["sweeps"].format(n=sweeps) if sweeps else "")
    lines = (["---"] if first else []) + [t["title"], summary, t["head"], "|---|---|---|---|---|---|---|---|---|", *rows]
    if len(entries) > len(rows):
        lines.append(t["more"].format(n=len(entries) - len(rows)))
    for i in sorted(shown):
        row, s = entries[i]
        facts = (s or {}).get("facts") or {}
        seq = row.get("seq", i + 1)
        for key, text in (("zoo_names_in_code_not_used", "zoo_unused"), ("zoo_factors_used", "zoo_used")):
            ids = facts.get(key)
            if isinstance(ids, list) and ids:
                lines.append(t[text].format(seq=seq, ids=", ".join(str(x) for x in ids)))
    lines.extend(_multiple_testing_lines(session_id, t))
    lines.extend(robustness_lines(session_id, zh))
    lines.append(t["no_live"])
    non = sorted({str(x) for s in screens for x in (s.get("facts") or {}).get("non_auditable_sources") or []})
    if non:
        lines.append(t["non_auditable"].format(srcs=", ".join(non)))
    return lines


_CONTRACT_TEXT = {
    True: {"title": "**研究合同变更（系统根据回测实测生成）**", "line": "- #{ref}：{fields} 由 {old} 变为 {new}；{status}",
           "declared": "已声明：{reason}", "undeclared": "未声明——这一版与之前的结果不是同一策略，不能直接比较",
           "mark": "（未声明变更）", "sep": "、", "labels": {"interval": "周期"}},
    False: {"title": "**Research contract changes (measured by the system)**", "line": "- #{ref}: {fields} changed from {old} to {new}; {status}",
            "declared": "declared: {reason}", "undeclared": "undeclared — this version is not the same strategy as the earlier results and cannot be compared with them",
            "mark": " (undeclared strategy change)", "sep": ", ", "labels": {"interval": "bar interval"}},
}


def _contract_values(contract: Any, fields: list[str], labels: dict | None = None) -> str:
    contract = contract if isinstance(contract, dict) else {}
    parts = []
    for f in fields:
        v = contract.get(f)
        parts.append(f"{(labels or {}).get(f, f)}={','.join(str(x) for x in v) if isinstance(v, list) else v if v is not None else '—'}")
    return ", ".join(parts)


def _undeclared_mark(row: dict, t: dict) -> str:
    change = row.get("contract_change")
    return t["mark"] if isinstance(change, dict) and change.get("declared") is False else ""


def _contract_section(session_id: str, zh: bool) -> list[str]:
    history = contract_history(session_id)
    if not history:
        return []
    t = _CONTRACT_TEXT[bool(zh)]
    lines = [t["title"]]
    for h in history:
        fields = [str(f) for f in h.get("fields") or []]
        ref = h.get("seq") if h.get("seq") is not None else str(h.get("strategy_id") or "?")[:8]
        status = t["declared"].format(reason=_safe_reason(h.get("reason"))) if h.get("declared") else t["undeclared"]
        names = t["sep"].join(t["labels"].get(f, f) for f in fields)
        lines.append(t["line"].format(ref=ref, fields=names, old=_contract_values(h.get("from"), fields, t["labels"]),
                                      new=_contract_values(h.get("to"), fields, t["labels"]), status=status))
    return lines


def _holdout_warning(session_id: str, zh: bool) -> list[str]:
    exposure = crypto_exposure_end_ms(session_id)
    if exposure is None:
        return []
    try:  # no network on the card: the session's cache file, else the in-memory memo, else the 60-day fallback
        cutoff = cached_holdout_cutoff_ms(session_id, max_age_seconds=HOLDOUT_CACHE_SECONDS)
        if cutoff is None:
            memo = _FALLBACK_MEMO.get(session_id)
            cutoff = memo[1] if memo is not None else _fallback_cutoff_ms()
    except Exception:  # noqa: BLE001 - the card never fails on the cutoff
        cutoff = _fallback_cutoff_ms()
    if exposure <= cutoff:
        return []
    return [_ENGINE_TEXT[bool(zh)]["holdout"].format(day=_day(exposure - 1), cutoff_day=_day(cutoff))]


def render_card(session_id: str | None, zh: bool) -> str | None:
    """The result card of the conversation: its IR backtests (``ir_ledger.jsonl``), then its generic engine backtests
    (``engine_ledger.jsonl``), then the research contract changes (``research_contract.json``), then a warning when it read hold-out data. None without a session id or when both
    ledgers are empty."""
    if not session_id:
        return None
    ir = _ir_section(session_id, zh)
    engine = _engine_section(session_id, zh, first=not ir)
    if not ir and not engine:
        return None
    return "\n".join([*ir, *engine, *_contract_section(session_id, zh), *_holdout_warning(session_id, zh)])
