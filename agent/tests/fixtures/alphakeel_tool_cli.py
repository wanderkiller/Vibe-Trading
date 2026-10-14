"""Test double for the ``alphakeel_research`` child process the agent tool starts (research-truth: one process per call).

``patch_cli(monkeypatch, main)`` makes the tool's child run (``_run_child([python, "-P", "-m", "alphakeel_research",
*argv], timeout)``) call ``main(argv)`` in-process instead, as the CLI would (stdout, stderr, exit code; argparse's
``SystemExit`` becomes the exit code, an uncaught exception a traceback on stderr and exit 1). Only the tool module's
``_run_child`` is replaced; the real one (``Popen`` in its own process group) is exercised by the real-child tests.
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import traceback
from typing import Callable


def patch_run(monkeypatch, run: Callable) -> None:
    """Replace the tool's child run with ``run(cmd, timeout=...) -> CompletedProcess``."""
    from src.tools import alphakeel_research_tool as tool

    monkeypatch.setattr(tool, "_run_child", lambda cmd, timeout: run(cmd, timeout=timeout))


def patch_cli(monkeypatch, main: Callable[[list[str]], int]) -> None:
    def run(cmd, **kwargs):
        assert cmd[1:4] == ["-P", "-m", "alphakeel_research"], cmd
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main(list(cmd[4:]))
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
        except Exception:  # noqa: BLE001 - what the interpreter does with an uncaught exception
            err.write(traceback.format_exc())
            rc = 1
        return subprocess.CompletedProcess(cmd, rc, out.getvalue(), err.getvalue())

    patch_run(monkeypatch, run)
