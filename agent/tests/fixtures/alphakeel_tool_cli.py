"""Test double for the ``alphakeel_research`` child process the agent tool starts (research-truth: one process per call).

``patch_cli(monkeypatch, main)`` makes the tool's ``subprocess.run([python, "-m", "alphakeel_research", *argv])`` call
``main(argv)`` in-process instead, as the CLI would (stdout, stderr, exit code; argparse's ``SystemExit`` becomes the
exit code). Only the tool module's ``subprocess`` is replaced.
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import types
from typing import Callable


def patch_run(monkeypatch, run: Callable) -> None:
    """Replace the tool's ``subprocess.run`` with ``run(cmd, **kwargs) -> CompletedProcess``."""
    from src.tools import alphakeel_research_tool as tool

    monkeypatch.setattr(tool, "subprocess", types.SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired,
                                                                   CompletedProcess=subprocess.CompletedProcess))


def patch_cli(monkeypatch, main: Callable[[list[str]], int]) -> None:
    def run(cmd, **kwargs):
        assert cmd[1:4] == ["-P", "-m", "alphakeel_research"], cmd
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main(list(cmd[4:]))
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
        return subprocess.CompletedProcess(cmd, rc, out.getvalue(), err.getvalue())

    patch_run(monkeypatch, run)
