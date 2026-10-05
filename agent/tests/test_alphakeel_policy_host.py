"""PolicyHost I/O bounds: no strategy output pattern can hold a call past ``call_timeout``.

Offline; no AlphaKeel service. Each case is bounded by an external SIGALRM watchdog so a regression fails the test
instead of hanging the suite.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from alphakeel_research import policy as policy_mod
from alphakeel_research.errors import ApiError
from alphakeel_research.policy import PolicyHost

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups and select() on pipes")


@contextmanager
def watchdog(seconds: float):
    def fire(*_):
        raise AssertionError(f"PolicyHost call outlived its deadline: external {seconds}s watchdog fired")

    old = signal.signal(signal.SIGALRM, fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def _policy(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "policy.py"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def _raw_host(tmp_path: Path, child_code: str, timeout: float) -> PolicyHost:
    """A host wired to an arbitrary child, to exercise the parent's frame reader against raw protocol bytes."""
    host = PolicyHost(tmp_path / "unused.py", call_timeout=timeout)
    host.proc = subprocess.Popen([sys.executable, "-c", child_code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, bufsize=0, start_new_session=True)
    for f in (host.proc.stdin, host.proc.stdout, host.proc.stderr):
        os.set_blocking(f.fileno(), False)
    return host


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split()[2] != "Z"
    except OSError:
        return True


def test_a_half_line_written_to_descriptor_1_cannot_outlive_the_timeout(tmp_path):
    # The audit's reproducer: before the fix this blocked in readline() until an outer watchdog intervened.
    p = _policy(tmp_path, """
        import os, time
        def initialize(parameters):
            return {}
        def on_step(context, state):
            os.write(1, b'{')
            time.sleep(30)
            return {'intents': [], 'state': state}
    """)
    with PolicyHost(p, call_timeout=5) as host:
        host.start({})
        host.call_timeout = 0.3
        t0 = time.monotonic()
        with watchdog(3), pytest.raises(ApiError) as e:
            host.step({"time_ms": 0})
        assert e.value.code == "policy.timeout"
        assert time.monotonic() - t0 < 2
        # Descriptor 1 is stderr inside the child: the stray byte is diagnostic output, not a protocol frame.
        assert "{" in host._stderr_tail()


def test_a_half_frame_on_the_protocol_channel_cannot_outlive_the_timeout(tmp_path):
    host = _raw_host(tmp_path, "import os,sys,time; sys.stdin.readline(); os.write(1, b'{\"ok\": tr'); time.sleep(30)", 0.3)
    try:
        t0 = time.monotonic()
        with watchdog(3), pytest.raises(ApiError) as e:
            host._call({"op": "step"})
        assert e.value.code == "policy.timeout"
        assert time.monotonic() - t0 < 2
        assert host.proc.poll() is not None, "the process was killed"
    finally:
        host._kill()


def test_an_oversized_frame_is_refused_without_buffering_it_all(tmp_path, monkeypatch):
    monkeypatch.setattr(policy_mod, "MAX_FRAME_BYTES", 1 << 20)
    host = _raw_host(tmp_path, "import os,sys,time; sys.stdin.readline(); os.write(1, b'x' * (4 << 20)); time.sleep(30)", 10)
    try:
        with watchdog(8), pytest.raises(ApiError) as e:
            host._call({"op": "step"})
        assert e.value.code == "engine.failure" and "protocol frame over" in e.value.message
        assert len(host._out) <= (1 << 20) + 65536 + 1
    finally:
        host._kill()


def test_a_malformed_frame_is_a_failure_not_a_crash(tmp_path):
    host = _raw_host(tmp_path, "import os,sys,time; sys.stdin.readline(); os.write(1, b'not json\\n'); time.sleep(30)", 5)
    try:
        with watchdog(4), pytest.raises(ApiError) as e:
            host._call({"op": "step"})
        assert e.value.code == "engine.failure" and "malformed" in e.value.message
    finally:
        host._kill()


def test_a_flood_of_stderr_is_drained_and_the_call_still_succeeds(tmp_path):
    # 4 MiB of stderr is far beyond a pipe buffer: without draining, the child blocks on write and the call times out.
    p = _policy(tmp_path, """
        import os, sys
        def initialize(parameters):
            return {}
        def on_step(context, state):
            for _ in range(64):
                os.write(2, b'e' * 65536)
            print('and via print', file=sys.stderr)
            return {'intents': [], 'state': {'n': 1}}
    """)
    with PolicyHost(p, call_timeout=10) as host, watchdog(15):
        host.start({})
        out = host.step({"time_ms": 0})
        assert out["intents"] == []
        assert len(host._err) <= policy_mod.STDERR_TAIL_BYTES
        assert host._stderr_tail().endswith("and via print\n")


def test_a_child_that_never_reads_stdin_cannot_block_the_write(tmp_path):
    host = _raw_host(tmp_path, "import time; time.sleep(30)", 0.5)
    try:
        t0 = time.monotonic()
        with watchdog(4), pytest.raises(ApiError) as e:
            host._call({"op": "step", "context": {"blob": "x" * (8 << 20)}})
        assert e.value.code == "policy.timeout"
        assert time.monotonic() - t0 < 3
    finally:
        host._kill()


def test_a_timeout_reaps_the_whole_process_tree(tmp_path):
    marker = tmp_path / "grandchild.pid"
    p = _policy(tmp_path, f"""
        import subprocess, time
        def initialize(parameters):
            return {{}}
        def on_step(context, state):
            g = subprocess.Popen(['sleep', '60'])
            open({str(marker)!r}, 'w').write(str(g.pid))
            time.sleep(60)
            return {{'intents': [], 'state': state}}
    """)
    with PolicyHost(p, call_timeout=5) as host:
        host.start({})
        child = host.proc.pid
        host.call_timeout = 1.0
        with watchdog(6), pytest.raises(ApiError) as e:
            host.step({"time_ms": 0})
        assert e.value.code == "policy.timeout"
    grandchild = int(marker.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and (_alive(child) or _alive(grandchild)):
        time.sleep(0.05)
    assert not _alive(child) and not _alive(grandchild)


def test_a_normal_session_still_works_and_close_is_clean(tmp_path):
    p = _policy(tmp_path, """
        def initialize(parameters):
            return {'k': parameters['k']}
        def on_step(context, state):
            print('noise on stdout goes to stderr')
            return {'intents': [{'id': str(context['time_ms'])}], 'state': state}
    """)
    with watchdog(10):
        host = PolicyHost(p, call_timeout=5)
        sha = host.start({"k": 3})
        assert len(sha) == 64
        for t in range(3):
            assert host.step({"time_ms": t})["intents"] == [{"id": str(t)}]
        proc = host.proc
        host.close()
        assert proc.poll() is not None
