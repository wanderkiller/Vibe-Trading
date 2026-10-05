"""Starts AlphaKeel's fixture research server (a Rust example binary) for cross-project tests.

The binary is built by `cargo build --example research_fixture_server` in the AlphaKeel repository. Point
ALPHAKEEL_FIXTURE_SERVER at it, or ALPHAKEEL_REPO at an AlphaKeel checkout (its debug build is used). Without either,
tests are skipped with that reason; with ALPHAKEEL_REQUIRE_FIXTURE=1 (AlphaKeel's cross-project CI) a missing binary
or tool FAILS instead, so a green run there means the cross-project tests really ran.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

def required() -> bool:
    return os.environ.get("ALPHAKEEL_REQUIRE_FIXTURE", "").strip().lower() in {"1", "true", "yes", "on"}


def skip_or_fail(reason: str) -> None:
    """Skip locally; fail where the cross-project run must not silently skip (ALPHAKEEL_REQUIRE_FIXTURE=1)."""
    if required():
        pytest.fail(f"required cross-project prerequisite missing: {reason}", pytrace=False)
    pytest.skip(reason)


def server_binary() -> str | None:
    p = os.environ.get("ALPHAKEEL_FIXTURE_SERVER", "").strip()
    if not p and os.environ.get("ALPHAKEEL_REPO", "").strip():
        p = str(Path(os.environ["ALPHAKEEL_REPO"]) / "target" / "debug" / "examples" / "research_fixture_server")
    return p if p and Path(p).is_file() else None


class Fixture:
    def __init__(self, info: dict, proc: subprocess.Popen):
        self.info = info
        self.proc = proc
        self.url = info["url"]
        self.token = info["token"]
        self.start_ms = info["start_ms"]
        self.end_ms = info["end_ms"]
        self.hour_ms = info["hour_ms"]
        self.dir = Path(info["dir"])

    def stop(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self.proc.kill()


def start(frames: int = 61) -> Fixture:
    binary = server_binary()
    if binary is None:
        skip_or_fail("AlphaKeel fixture server binary not found: build it with `cargo build --example research_fixture_server` "
                     "and set ALPHAKEEL_FIXTURE_SERVER (or ALPHAKEEL_REPO)")
    d = tempfile.mkdtemp(prefix="ak-fixture-")
    proc = subprocess.Popen([binary, "--dir", d, "--frames", str(frames)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("the fixture server did not start")
    return Fixture(json.loads(line), proc)
