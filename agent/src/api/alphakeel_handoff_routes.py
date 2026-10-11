"""Hand-off of an IR run to AlphaKeel: the backend of the "Hand off to AlphaKeel" button on the run page.

* ``GET  /runs/{run_id}/alphakeel``         what the run is (IR run or not), its robustness grade and the research-side G1
                                            precheck (``alphakeel_research.precheck``), where an export would land, whether it
                                            was exported before, whether a research credential is configured.
* ``POST /runs/{run_id}/alphakeel/export``  ``ir_backtest.export`` into ``<ALPHAKEEL_HANDOFF_DIR>/<strategy_id>/`` (the
                                            directory AlphaKeel's console reads). Only the user's button calls it: the agent
                                            tool refuses to export there (docs/45 section 7).

Errors are ``{"detail": {"code", "message"}}``. Nothing here talks to an exchange or changes AlphaKeel; the hand-off is a
directory of files. Local patch layer: group ``alphakeel-handoff`` (``patches/MANIFEST.md``).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

AuthDep = Callable[..., Awaitable[Any] | Any]

DEFAULT_HANDOFF_DIR = "/home/vibe/handoff"
DEFAULT_HANDOFF_HOST_DIR = "/home/ubuntu/handoff"
MARKER = "alphakeel-handoff.json"
SID_RE = r"^ir-[0-9a-f]{24}$"


def handoff_dir() -> Path:
    """Where exports land, as the container sees it (``ALPHAKEEL_HANDOFF_DIR``)."""
    return Path(os.environ.get("ALPHAKEEL_HANDOFF_DIR") or DEFAULT_HANDOFF_DIR)


def handoff_host_dir() -> str:
    """The same directory on the host (``ALPHAKEEL_HANDOFF_HOST_DIR``), shown to the user; never opened here."""
    return os.environ.get("ALPHAKEEL_HANDOFF_HOST_DIR") or DEFAULT_HANDOFF_HOST_DIR


def _client_cls():
    """The research-service client class (lazy: the leaf package is optional at import time; tests replace this)."""
    from alphakeel_research.client import Client

    return Client


def credential_configured() -> bool:
    f = os.environ.get("ALPHAKEEL_RESEARCH_TOKEN_FILE")
    return bool(f) and os.path.isfile(f) and os.access(f, os.R_OK)


def _err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ir_run(run_dir: Path) -> tuple[dict, dict, str] | None:
    """(run card, IR document, strategy_id) when ``run_dir`` is an ``ir backtest`` run (IR run-card schema + ``ir.json``
    that is the card's strategy), else None."""
    from alphakeel_research import ir as IR
    from alphakeel_research import ir_backtest

    card = _read_json(run_dir / "run_card.json")
    doc = _read_json(run_dir / "ir.json")
    if not isinstance(card, dict) or not isinstance(doc, dict) or card.get("schema") != ir_backtest.RUN_CARD_SCHEMA:
        return None
    try:
        sid = IR.load(doc).strategy_id
    except Exception:  # noqa: BLE001 - not a valid IR: not an IR run
        return None
    return (card, doc, sid) if card.get("strategy_id") == sid else None


def _robustness(run_dir: Path, sid: str) -> tuple[dict, str] | None:
    """(robustness.json, its sha256) of this run's battery when it is for ``sid``; None when absent or for another strategy."""
    f = run_dir / "robustness" / "robustness.json"
    doc = _read_json(f)
    if not isinstance(doc, dict) or doc.get("strategy_id") != sid:
        return None
    return doc, _sha256(f)


def _target(sid: str) -> dict:
    return {"container_path": f"{str(handoff_dir()).rstrip('/')}/{sid}/", "host_path": f"{handoff_host_dir().rstrip('/')}/{sid}/"}


def describe(run_dir: Path) -> dict:
    """The body of ``GET /runs/{run_id}/alphakeel`` (blocking: reads files; the route runs it in the threadpool)."""
    from alphakeel_research import precheck

    found = ir_run(run_dir)
    if found is None:
        return {"ir": False, "strategy_id": None, "name": None, "window": None, "robustness": None, "robustness_sha256": None, "precheck": [],
                "protocol": None, "target": None, "exported": None, "credential": False}
    card, _doc, sid = found
    protocol = precheck.load_protocol()
    rob = _robustness(run_dir, sid)
    summary = None
    checks: list[dict] = []
    if rob is not None:
        doc, sha = rob
        rating = doc.get("rating") or {}
        summary = {"grade": rating.get("grade"), "score": rating.get("score"), "caps": [c.get("id") for c in rating.get("caps") or []],
                   "schema": doc.get("schema"), "sha256": sha}
        # AlphaKeel's 1.6 trial floor: IR provenance.trials.count + the battery's candidates (it adds the service ledger on its side)
        tri, brt = card.get("trials") or {}, doc.get("trials") or {}
        floor = (tri.get("count") if type(tri.get("count")) is int else 0) + (brt.get("distinct_candidate_variants") if type(brt.get("distinct_candidate_variants")) is int else 0)
        checks = precheck.evaluate(doc, protocol, 0, floor)
    marker = _read_json(run_dir / MARKER)
    exported = None
    if isinstance(marker, dict):
        exported = {k: marker.get(k) for k in ("path", "at", "strategy_id", "files")}
    w = card.get("window") or {}
    return {"ir": True, "strategy_id": sid, "name": card.get("name"), "window": {"start_day": w.get("start_day"), "end_day": w.get("end_day")},
            "robustness": summary, "robustness_sha256": summary["sha256"] if summary else None, "precheck": checks, "protocol": {"version": protocol["version"], "sha256": precheck.protocol_sha256()},
            "target": _target(sid), "exported": exported, "credential": credential_configured()}


def _files_of(d: Path) -> list[dict]:
    return [{"name": p.relative_to(d).as_posix(), "sha256": _sha256(p)} for p in sorted(d.rglob("*")) if p.is_file()]


def _replace_dir(tmp: Path, target: Path) -> None:
    """Put ``tmp`` at ``target``. A directory cannot be renamed over a non-empty one, so an existing package is moved aside
    first and removed after the new one is in place (a reader sees the old package, a moment of none, then the new one)."""
    old = None
    if target.exists():
        old = target.with_name(f".{target.name}.old-{os.getpid()}")
        shutil.rmtree(old, ignore_errors=True)
        os.rename(target, old)
    try:
        os.rename(tmp, target)
    except OSError:
        if old is not None:
            os.rename(old, target)
        raise
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)


MANIFEST = "handoff-manifest.json"
MANIFEST_SCHEMA = "alphakeel.handoff-manifest/1"


def _write_manifest(tmp: Path, sid: str, run_id: Any) -> None:
    """``handoff-manifest.json`` in ``tmp``: every other regular file with its sha256 and size, sorted by path."""
    files = []
    for p in sorted(tmp.rglob("*"), key=lambda q: q.relative_to(tmp).as_posix()):
        if p.is_file() and not p.is_symlink() and p.relative_to(tmp).as_posix() != MANIFEST:
            data = p.read_bytes()
            files.append({"path": p.relative_to(tmp).as_posix(), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    manifest = {"schema": MANIFEST_SCHEMA, "strategy_id": sid, "run_id": run_id,
                "exported_at_ms": int(datetime.now(timezone.utc).timestamp() * 1000), "files": files}
    (tmp / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def export_run(run_dir: Path, expected_robustness_sha256: str | None = None) -> dict:
    """The body of ``POST /runs/{run_id}/alphakeel/export`` (blocking). Raises ``HTTPException`` with the documented codes.
    ``expected_robustness_sha256``: the hash of robustness.json the user reviewed; a different one under the lock is 409."""
    from alphakeel_research import ir_backtest
    from alphakeel_research.errors import ApiError

    found = ir_run(run_dir)
    if found is None:
        raise _err(409, "ir.not_ir_run", "this run is not an `ir backtest` run (no IR run card / ir.json)")
    card, doc, sid = found
    rb_file = run_dir / "robustness" / "robustness.json"
    if not rb_file.is_file():
        raise _err(409, "robustness.missing", "no robustness.json in this run: run `ir robustness --run` first")
    rb = _read_json(rb_file)
    if isinstance(rb, dict) and rb.get("strategy_id") not in (None, sid):
        raise _err(409, "robustness.mismatch", f"robustness.json is for {rb.get('strategy_id')}, this run is {sid}")
    if not credential_configured():
        raise _err(409, "credential.missing", "no research credential (ALPHAKEEL_RESEARCH_TOKEN_FILE): the export records it in the provenance")
    client = _client_cls()(None)
    try:
        cred = client.check()
    except Exception as e:  # noqa: BLE001 - any failure to reach / accept the service is "unavailable"
        raise _err(502, "research.unavailable", f"AlphaKeel's research service did not answer the credential check: {getattr(e, 'message', None) or e}") from e
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    root = handoff_dir()
    root.mkdir(parents=True, exist_ok=True)
    lock_dir = Path(tempfile.gettempdir()) / f"alphakeel-handoff-locks-{os.getuid()}"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with open(lock_dir / f"{sid}.lock", "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise _err(409, "export.busy", f"another export of {sid} is running") from None
        if expected_robustness_sha256 is not None and expected_robustness_sha256 != _sha256(rb_file):
            raise _err(409, "export.changed", "robustness changed since you reviewed it; reopen")
        target = root / sid
        tmp = Path(tempfile.mkdtemp(prefix=f".{sid}.tmp-", dir=root))
        try:
            try:
                ir_backtest.export(doc, run_dir, tmp, research_credential=cred, robustness_dir=run_dir / "robustness")
            except ir_backtest.WindowMismatch as e:
                raise _err(409, "export.window_mismatch", e.message) from e
            except ApiError as e:
                raise _err(400, e.code, e.message) from e
            # `ir robustness` does not take this lock: the battery may have been rewritten between the check above and the
            # copy. What is handed off must be the file the user reviewed.
            copied = tmp / "evidence" / "robustness.json"
            if expected_robustness_sha256 is not None and (not copied.is_file() or _sha256(copied) != expected_robustness_sha256):
                raise _err(409, "export.changed", "robustness changed since you reviewed it; reopen")
            _write_manifest(tmp, sid, card.get("run_id"))
            os.chmod(tmp, 0o2775)  # mkdtemp is 0700; the console on the host reads the package (group of the shared directory)
            files = _files_of(tmp)
            _replace_dir(tmp, target)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    grade = (rb or {}).get("rating", {}).get("grade") if isinstance(rb, dict) else None
    marker = {"path": f"{str(target).rstrip('/')}/", "at": at, "strategy_id": sid, "files": files}
    m_tmp = run_dir / f".{MARKER}.tmp"
    m_tmp.write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(m_tmp, run_dir / MARKER)
    return {"strategy_id": sid, "path": marker["path"], "files": files, "at": at, "grade": grade}


def register_alphakeel_handoff_routes(app: FastAPI, require_auth: AuthDep | None = None) -> None:
    """Mount the hand-off routes onto ``app`` (same auth dependency, run-id validation and RUNS_DIR as the runs routes,
    resolved late from the host ``api_server`` module like ``register_runs_routes``)."""
    host = sys.modules.get("api_server") or sys.modules.get("agent.api_server")
    if host is None:
        raise RuntimeError("register_alphakeel_handoff_routes: api_server module not in sys.modules; import api_server first")
    if require_auth is None:
        require_auth = host.require_auth

    def _run_dir(run_id: str) -> Path:
        h = sys.modules.get("api_server") or sys.modules.get("agent.api_server")
        h._validate_path_param(run_id, "run_id")
        d = Path(h.RUNS_DIR) / run_id
        if not d.is_dir():
            raise _err(404, "run.not_found", f"Run {run_id} not found")
        return d

    @app.get("/runs/{run_id}/alphakeel", dependencies=[Depends(require_auth)])
    async def get_run_alphakeel(run_id: str):
        """Hand-off state of a run: IR run?, robustness grade, research-side G1 precheck, target path, earlier export."""
        return await run_in_threadpool(describe, _run_dir(run_id))

    @app.post("/runs/{run_id}/alphakeel/export", dependencies=[Depends(require_auth)])
    async def export_run_alphakeel(run_id: str, request: Request):
        """Export the IR run (strategy, evidence incl. robustness) to the hand-off directory. Idempotent overwrite.
        Optional JSON body ``{"robustness_sha256": hex}``: the robustness the user reviewed (409 export.changed if it differs)."""
        raw = await request.body()
        sha = None
        if raw.strip():
            try:
                body = json.loads(raw)
            except ValueError:
                raise _err(400, "request.invalid", "the body must be JSON") from None
            if not isinstance(body, dict) or not (body.get("robustness_sha256") is None or isinstance(body.get("robustness_sha256"), str)):
                raise _err(400, "request.invalid", "robustness_sha256 must be a string")
            sha = body.get("robustness_sha256")
        d = _run_dir(run_id)
        return await run_in_threadpool(export_run, d, sha)
