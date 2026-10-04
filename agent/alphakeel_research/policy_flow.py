"""One command that runs the same policy under both state sources and compares them (local simulator vs AlphaKeel)."""

from __future__ import annotations

import time
from pathlib import Path

from . import evidence, strategy as strat, workflow
from .client import Client
from .errors import ApiError
from .packfile import Pack


def run_both(client: Client, pack: Pack, strategy_dir: str, instruments: list[dict], *, profile: dict | None, seed: int, parameters: dict | None,
             work: Path, step_timeout_ms: int = 60_000) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    manifest, bundle = strat.manifest(strategy_dir, name=Path(strategy_dir).name, parameters=parameters or {}, seed=seed)
    up = client.upload("strategy-bundle", bundle)
    client.register_strategy(manifest, up["sha256"])
    prof_doc = client.render_profile(pack.id, "python_policy", profile)["profile"]
    pack_dir = pack.export(work / "pack")
    local_id = f"py-{manifest['content_sha256'][:16]}-{int(time.time())}"
    sim, steps, sandbox = workflow.run_policy_local(pack, prof_doc, strategy_dir, manifest, instruments, run_id=local_id, pack_dir=pack_dir)
    man, res = evidence.build_manifest(sim, pack=pack, run_id=local_id, mode="python_policy", parameters={"profile": profile or {}, "instruments": instruments},
                                       strategy=manifest, seed=seed, sandbox=sandbox)
    d = evidence.write_run_dir(work / local_id, man, res, sim, steps=steps, strategy_manifest=manifest, strategy_bundle=bundle)
    reg = evidence.register(client, d, pack.id)
    remote = workflow.run_policy_remote(client, pack, strategy_dir, manifest, instruments, profile=profile, journal_dir=work / "journal", pack_dir=pack_dir,
                                        step_timeout_ms=step_timeout_ms)
    run = remote["run"]
    if run["status"] != "complete":
        raise ApiError((run.get("error") or {}).get("code", "engine.failure"), (run.get("error") or {}).get("message", run["status"]))
    cmp = client.compare(reg["run_id"], run["run_id"], "python_policy")
    rep = client.wait_comparison(cmp["comparison_id"]).get("report") or {}
    return {"strategy_id": manifest["strategy_id"], "local_run": reg["run_id"], "alphakeel_run": run["run_id"], "comparison": cmp["comparison_id"],
            "passed": rep.get("passed"), "layers": {k: v["status"] for k, v in rep.get("layers", {}).items()}, "first_difference": rep.get("first_difference"),
            "pass_scope": rep.get("pass_scope"), "sandbox": sandbox, "run_dir": str(d)}
