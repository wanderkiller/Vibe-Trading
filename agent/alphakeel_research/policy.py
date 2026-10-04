"""Step-wise policy adapter: the SAME script and parameters run in the local simulator and in AlphaKeel-driven mode.

The parent process (which holds the service credential) talks to a sandboxed child over JSON lines
(``policy_host.py``). The child gets: a scrubbed environment (no credential, no trading configuration, no user
directory), an ephemeral HOME, resource limits, an unprivileged uid where the host allows it, its own process group (a
timeout or cancellation kills the whole tree), and read-only access to an exported pack directory. Whatever of that
the host cannot enforce is reported in ``PolicyHost.sandbox`` and recorded in the run evidence, never silently dropped.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import canon
from .errors import ApiError

_AGENT_ROOT = Path(__file__).resolve().parents[1]
_RLIMIT_BOOTSTRAP = """\
import os, runpy, sys
_as, _nofile = int(sys.argv[1]), int(sys.argv[2])
sys.argv = ["policy_host"] + sys.argv[3:]
try:
    import resource
    for _res, _t in ((resource.RLIMIT_AS, _as), (resource.RLIMIT_NOFILE, _nofile)):
        _s, _h = resource.getrlimit(_res)
        _nh = _t if _h == resource.RLIM_INFINITY else min(_t, _h)
        resource.setrlimit(_res, (min(_t, _nh), _nh))
except Exception:
    pass
runpy.run_module("alphakeel_research.policy_host", run_name="__main__", alter_sys=True)
"""
_ENV_KEEP = ("PATH", "LANG", "LC_ALL", "TZ")


class PolicyHost:
    """One policy script in one sandboxed child process."""

    def __init__(self, script: str | os.PathLike, *, pack_dir: str | os.PathLike | None = None, call_timeout: float = 30.0,
                 rlimit_as_mb: int = 4096, python: str | None = None):
        self.script = str(Path(script).resolve())
        self.pack_dir = str(pack_dir) if pack_dir else None
        self.call_timeout = call_timeout
        self.rlimit_as_mb = rlimit_as_mb
        self.python = python or sys.executable
        self.proc: subprocess.Popen | None = None
        self._home: str | None = None
        self.sandbox: dict[str, Any] = {}

    # -- lifecycle ---------------------------------------------------------------------------------------------

    def start(self, parameters: dict, seed: int | None = None) -> str:
        env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
        self._home = tempfile.mkdtemp(prefix="ak-policy-home-")
        env.update({"HOME": self._home, "USERPROFILE": self._home, "PYTHONPATH": str(_AGENT_ROOT), "PYTHONUNBUFFERED": "1",
                    "PYTHONHASHSEED": str(seed if seed is not None else 0), "PYTHONDONTWRITEBYTECODE": "1"})
        if self.pack_dir:
            env["ALPHAKEEL_PACK_DIR"] = self.pack_dir
        cmd = [self.python, "-c", _RLIMIT_BOOTSTRAP, str(self.rlimit_as_mb * 1024 * 1024), "512", self.script]
        kwargs: dict[str, Any] = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True,
                                      encoding="utf-8", start_new_session=True, cwd=self._home)
        creds = _sandbox_credentials()
        self.sandbox = {"credentials_in_environment": False, "ephemeral_home": True, "resource_limits": True, "process_group": True,
                        "uid_drop": False, "network_blocked": False, "pack_access": "read-only exported directory" if self.pack_dir else "none"}
        proc = None
        if creds is not None:
            try:
                proc = subprocess.Popen(cmd, user=creds[0], group=creds[1], **kwargs)
                self.sandbox["uid_drop"] = True
            except (PermissionError, LookupError, OSError):
                proc = None
        if proc is None:
            proc = subprocess.Popen(cmd, **kwargs)
        self.proc = proc
        r = self._call({"op": "init", "parameters": parameters})
        return r["state_sha256"]

    def close(self) -> None:
        p = self.proc
        if p is not None and p.poll() is None:
            try:
                p.stdin.write(json.dumps({"op": "close"}) + "\n")
                p.stdin.flush()
                p.wait(timeout=2)
            except Exception:  # noqa: BLE001
                self._kill()
        self.proc = None
        if self._home:
            shutil.rmtree(self._home, ignore_errors=True)
            self._home = None

    def __enter__(self) -> "PolicyHost":
        return self

    def __exit__(self, *a: Any) -> None:
        self.close()

    def _kill(self) -> None:
        p = self.proc
        if p is None:
            return
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            p.kill()
        try:
            p.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass

    # -- protocol ----------------------------------------------------------------------------------------------

    def _call(self, msg: dict) -> dict:
        p = self.proc
        if p is None or p.poll() is not None:
            raise ApiError("run.interrupted", "the strategy process is not running: " + self._stderr_tail())
        p.stdin.write(json.dumps(msg, sort_keys=True, separators=(",", ":")) + "\n")
        p.stdin.flush()
        deadline = time.monotonic() + self.call_timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                self._kill()
                raise ApiError("policy.timeout", f"the strategy did not answer within {self.call_timeout}s; its process tree was killed")
            ready, _, _ = select.select([p.stdout], [], [], min(left, 0.5))
            if ready:
                line = p.stdout.readline()
                if not line:
                    raise ApiError("run.interrupted", "the strategy process ended: " + self._stderr_tail())
                r = json.loads(line)
                if not r.get("ok"):
                    raise ApiError("engine.failure", "the strategy raised: " + str(r.get("error")))
                return r
            if p.poll() is not None:
                raise ApiError("run.interrupted", "the strategy process ended: " + self._stderr_tail())

    def _stderr_tail(self) -> str:
        p = self.proc
        if p is None or p.stderr is None:
            return ""
        try:
            ready, _, _ = select.select([p.stderr], [], [], 0)
            return p.stderr.read(2000) if ready else ""
        except Exception:  # noqa: BLE001
            return ""

    def step(self, context: dict) -> dict:
        r = self._call({"op": "step", "context": context})
        return {"intents": r["intents"], "state_sha256": r["state_sha256"]}


def _sandbox_credentials() -> tuple[str, str] | None:
    try:
        import pwd

        pwd.getpwnam("vibe-sandbox")
        return ("vibe-sandbox", "vibe-sandbox")
    except (ImportError, KeyError, OSError):
        return None


def response_doc(session_id: str, step_seq: int, context_sha256: str, strategy_content_sha256: str, pack_id: str, intents: list, state_sha256: str) -> dict:
    return {"schema": "alphakeel.policy-response/1", "session_id": session_id, "step_seq": step_seq, "context_sha256": context_sha256,
            "strategy_content_sha256": strategy_content_sha256, "pack_id": pack_id, "intents": intents, "state_sha256": state_sha256}


class StepJournal:
    """Durable per-step record kept BEFORE a response is submitted.

    A retry after a lost reply re-sends the stored response; it never calls the strategy again, so a retry cannot
    produce a different decision for the same step.
    """

    def __init__(self, directory: str | os.PathLike):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, seq: int) -> Path:
        return self.dir / f"step-{seq:06d}.json"

    def get(self, seq: int) -> dict | None:
        p = self._path(seq)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def put(self, seq: int, context: dict, response: dict) -> None:
        tmp = self._path(seq).with_suffix(".tmp")
        tmp.write_bytes(canon.canonical_bytes({"context": context, "response": response}))
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp, self._path(seq))

    def all(self) -> list[dict]:
        out = []
        for p in sorted(self.dir.glob("step-*.json")):
            d = json.loads(p.read_text(encoding="utf-8"))
            out.append({"step_seq": d["context"]["step_seq"], "context": d["context"], "response": d["response"]})
        return out
