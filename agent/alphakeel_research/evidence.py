"""Standard evidence for a Python-side run: run-manifest, events, result, execution profile (and policy steps).

The files are written into an isolated run directory and can be uploaded to AlphaKeel for review. Nothing here takes a
result from AlphaKeel: Python's numbers are always Python's own.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import __version__, canon, contract
from .sim import SimResult


def build_manifest(sim: SimResult, *, pack, run_id: str, mode: str, side: str = "python_local", parameters: dict | None = None,
                   strategy: dict | None = None, parent_run_id: str | None = None, seed: int | None = None, sandbox: dict | None = None,
                   dependency_lock_sha256: str = "", dirty: str | None = None, claims: list[str] | None = None) -> tuple[dict, dict]:
    """Return (manifest, result) with the run's identity filled in (result.mode/side/run_id follow the manifest)."""
    result = dict(sim.result)
    result["run_id"] = run_id
    result["mode"] = mode
    result["side"] = side
    params = dict(parameters or {})
    if sandbox:
        params["sandbox"] = sandbox
    manifest = {
        "schema": "alphakeel.run-manifest/1", "run_id": run_id, "parent_run_id": parent_run_id, "mode": mode, "side": side, "origin": "client_uploaded",
        "engine": {"name": "alphakeel_research.sim (independent Decimal simulator)", "version": __version__},
        "builder": {"adapter": f"alphakeel_research/{__version__}", "build": f"alphakeel_research {__version__}", "dirty": dirty,
                    "dependency_lock_sha256": dependency_lock_sha256},
        "strategy": None if strategy is None else {"strategy_id": strategy["strategy_id"], "content_sha256": strategy["content_sha256"]},
        "parameters": params, "data_lock_sha256": pack.lock["pack_sha256"], "pack_id": pack.lock["pack_id"],
        "execution_profile_sha256": canon.canonical_sha256(sim.profile), "seed": seed, "inputs": sim.accesses, "status": "complete",
        "outputs": {"events_sha256": sim.events_sha256, "result_sha256": canon.canonical_sha256(result), "facts_sha256": None},
        "scope": {"compared": [], "claims": claims or ["independent Decimal accounting of the declared intents/decisions on the frozen pack",
                                                      "does not certify the strategy logic or the absence of look-ahead"]},
        "created_ms": int(time.time() * 1000),
    }
    return manifest, result


def write_run_dir(run_dir: str | os.PathLike, manifest: dict, result: dict, sim: SimResult, *, steps: list[dict] | None = None,
                  strategy_manifest: dict | None = None, strategy_bundle: bytes | None = None) -> Path:
    d = Path(run_dir)
    d.mkdir(parents=True, exist_ok=True)
    errs = contract.validate_def("run-manifest", manifest) + contract.validate_def("result", result) + contract.validate_def("execution-profile", sim.profile)
    if errs:
        raise contract.ContractError(errs)
    ev_lines = [json.loads(ln) for ln in sim.events_bytes.split(b"\n") if ln]
    bad = contract.validate_events(ev_lines)
    if bad:
        raise contract.ContractError(bad)
    (d / "run-manifest.json").write_bytes(canon.canonical_bytes(manifest))
    (d / "events.jsonl").write_bytes(sim.events_bytes)
    (d / "result.json").write_bytes(canon.canonical_bytes(result))
    (d / "execution-profile.json").write_bytes(canon.canonical_bytes(sim.profile))
    if steps is not None:
        (d / "steps.json").write_bytes(canon.canonical_bytes(steps))
    if strategy_manifest is not None:
        (d / "strategy-manifest.json").write_bytes(canon.canonical_bytes(strategy_manifest))
    if strategy_bundle is not None:
        (d / "strategy-bundle.tar.gz").write_bytes(strategy_bundle)
    return d


def register(client, run_dir: str | os.PathLike, pack_id: str, *, key: str | None = None) -> dict:
    """Upload the run directory's evidence and register it as an external run (the service re-verifies everything)."""
    d = Path(run_dir)
    up = lambda name: client.upload("evidence", (d / name).read_bytes())["sha256"]  # noqa: E731
    body = {"pack_id": pack_id, "manifest_sha256": up("run-manifest.json"), "result_sha256": up("result.json")}
    if (d / "events.jsonl").exists():
        body["events_sha256"] = up("events.jsonl")
    if (d / "execution-profile.json").exists():
        body["profile_sha256"] = up("execution-profile.json")
    if (d / "steps.json").exists():
        body["steps_sha256"] = up("steps.json")
    return client.register_external(body, key=key)
