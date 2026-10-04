"""Starts AlphaKeel's fixture research server (a Rust example binary) for cross-project tests.

The binary is built by `cargo build --example research_fixture_server` in the AlphaKeel repository. Point
ALPHAKEEL_FIXTURE_SERVER at it (default: the sibling checkout's debug build). Tests are skipped, with that reason,
when it is not available; the cross-project evidence in AlphaKeel's docs records the runs where it was.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

DEFAULT = "/home/ubuntu/alphakeel/target/debug/examples/research_fixture_server"


def server_binary() -> str | None:
    p = os.environ.get("ALPHAKEEL_FIXTURE_SERVER", DEFAULT)
    return p if Path(p).exists() else None


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
        pytest.skip("AlphaKeel fixture server binary not built (cargo build --example research_fixture_server)")
    d = tempfile.mkdtemp(prefix="ak-fixture-")
    proc = subprocess.Popen([binary, "--dir", d, "--frames", str(frames)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("the fixture server did not start")
    return Fixture(json.loads(line), proc)
