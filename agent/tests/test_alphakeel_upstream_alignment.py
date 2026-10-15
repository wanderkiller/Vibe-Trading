"""The research tool and pack cache follow upstream's mechanisms where upstream already solved the same problem
(AK/VT round 1 review, 2026-10-15): write paths through ``path_utils.resolve_safe_path``, results fitted to
``TOOL_RESULT_LIMIT`` as structured JSON, partial output kept on a timeout, one temporary file per cache writer.
Every value here is synthetic.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading

import pytest

from tests.fixtures.alphakeel_tool_cli import patch_cli

pytestmark = pytest.mark.unit


@pytest.fixture()
def run_dir(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    (root / "session").mkdir(parents=True)
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(root))
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_WRITE_ROOTS", str(root))
    return root / "session"


def test_an_out_path_outside_the_write_roots_is_refused_before_the_cli_runs(run_dir, monkeypatch) -> None:
    from src.tools.alphakeel_research_tool import AlphakeelResearchTool

    calls = []
    patch_cli(monkeypatch, lambda argv: calls.append(argv) or 0)
    for out in ("../../private", str(run_dir.parent.parent / "private")):
        res = json.loads(AlphakeelResearchTool().execute(
            action="ir_export", args={"ir": "strategy.json", "run": "r", "out": out}, run_dir=str(run_dir)))
        assert res["status"] == "error" and res["error"].startswith("out: ") and "VIBE_TRADING_ALLOWED_WRITE_ROOTS" in res["error"], res
    assert calls == []
    AlphakeelResearchTool().execute(action="ir_export", args={"ir": "strategy.json", "run": "r", "out": "export"},
                                    run_dir=str(run_dir))
    assert calls and calls[0][calls[0].index("--out") + 1] == str(run_dir / "export")


def test_an_oversized_result_stays_valid_json_with_its_evidence_reference() -> None:
    from src.config.limits import TOOL_RESULT_LIMIT
    from src.tools.alphakeel_research_tool import _dump

    rows = [{"ts_ms": i, "price": "1.2345", "qty": "10"} for i in range(1000)]
    out = {"status": "ok", "evidence_dir": "ir_runs/x",
           "result": {"evidence_status": "passed_screen", "summary": "s", "rows": rows, "notes": "n" * 3000}}
    text = _dump(out, ["unit warning"])
    assert len(text) <= TOOL_RESULT_LIMIT
    env = json.loads(text)
    assert (env["status"], env["evidence_dir"], env["warnings"]) == ("ok", "ir_runs/x", ["unit warning"])
    assert env["result"]["evidence_status"] == "passed_screen" and env["result"]["rows"] == rows[: len(env["result"]["rows"])]
    assert env["omitted"]["result.rows"] == f"{len(env['result']['rows'])} of 1000 records" and env["omitted_note"]


@pytest.mark.skipif(not hasattr(__import__("os"), "killpg"), reason="process groups are POSIX")
def test_a_timeout_returns_and_keeps_the_partial_output(tmp_path, monkeypatch) -> None:
    from src.agent import loop
    from src.tools import alphakeel_research_tool as tool

    monkeypatch.setenv("ALPHAKEEL_RESEARCH_TOKEN", "ak_rs_env-secret")
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path))
    script = ("import sys, time\n"
              "print('freeze: job 7 queued token ak_rs_env-secret', file=sys.stderr, flush=True)\n"
              "time.sleep(30)\n")
    monkeypatch.setattr(tool, "_child_cmd", lambda argv: [sys.executable, "-c", script])
    monkeypatch.setattr(loop, "TOOL_TIMEOUT_SECONDS", 2.0, raising=False)
    text = tool.AlphakeelResearchTool().execute(action="check", run_dir=str(tmp_path))
    out = json.loads(text)
    assert out["error"] == "alphakeel_research timed out after 2.0 s"
    assert out["stderr"].strip() == "freeze: job 7 queued token <redacted>" and "ak_rs_env-secret" not in text
    assert (tmp_path / "logs" / "alphakeel_research_stderr.txt").read_text().strip() == out["stderr"].strip()


def test_two_writers_of_the_same_cached_object_both_succeed(tmp_path, monkeypatch) -> None:
    """Both calls write their temporary file before either replaces (a barrier in ``os.replace``): a shared ``.part``
    path made the second replace fail with ``FileNotFoundError``."""
    import os

    from alphakeel_research import packfile

    raw = b'{"a":1}\n' * 100
    sha = hashlib.sha256(raw).hexdigest()
    obj = {"content_sha256": sha, "sha256": sha, "codec": "jsonl", "size": len(raw), "content_size": len(raw), "name": "x"}

    class Client:
        def object_range(self, pack_id, name, start, end, identity=False):
            return type("R", (), {"content": raw[start:end + 1]})()

    barrier, real = threading.Barrier(2, timeout=10), os.replace

    def replace(src, dst):
        barrier.wait()
        real(src, dst)

    monkeypatch.setattr(packfile.os, "replace", replace)
    cache = packfile.PackCache(tmp_path / "cache")
    results: list = []

    def fill() -> None:
        try:
            results.append(cache.ensure(Client(), "p", obj))
        except Exception as e:  # noqa: BLE001
            results.append(e)

    threads = [threading.Thread(target=fill) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert results == [cache.content_path(sha)] * 2, results
    assert cache.has(sha) and sorted(p.name for p in (tmp_path / "cache" / "content").iterdir()) == [sha]


def test_a_path_argument_that_is_not_a_string_is_refused(run_dir, monkeypatch) -> None:
    """A list went past the hand-off guard and the root check, and ``_csv`` turned it back into the path (review round 1)."""
    from src.tools.alphakeel_research_tool import AlphakeelResearchTool

    monkeypatch.setenv("ALPHAKEEL_HANDOFF_DIR", str(run_dir.parent / "handoff"))
    calls = []
    patch_cli(monkeypatch, lambda argv: calls.append(argv) or 0)
    for action, args in (("ir_export", {"ir": "s.json", "run": "r", "out": [str(run_dir.parent / "handoff" / "sid")]}),
                         ("ir_robustness", {"run": [str(run_dir.parent.parent / "private")]}),
                         ("ir_export", {"ir": "s.json", "run": "r", "out": 7})):
        res = json.loads(AlphakeelResearchTool().execute(action=action, args=args, run_dir=str(run_dir)))
        assert res["status"] == "error" and "must be a path string" in res["error"], res
    assert calls == []


def test_a_sibling_under_the_runs_root_is_allowed_as_upstream_write_file_allows_it(run_dir, monkeypatch) -> None:
    from src.tools.alphakeel_research_tool import AlphakeelResearchTool

    calls = []
    patch_cli(monkeypatch, lambda argv: calls.append(argv) or 0)
    AlphakeelResearchTool().execute(action="ir_export", args={"ir": "s.json", "run": "r", "out": "../other/x"},
                                    run_dir=str(run_dir))
    assert len(calls) == 1


def test_oversized_kept_fields_still_fit_the_budget() -> None:
    from src.config.limits import TOOL_RESULT_LIMIT
    from src.tools.alphakeel_research_tool import _dump

    for out in ({"status": "error", "result": {"error": {"code": "x", "message": "m" * 12000}}},
                {"status": "ok", "evidence_dir": "ir_runs/x", "result": {"summary": {"k": "v" * 18000}}},
                {"status": "ok", "result": {k: "s" * 1499 for k in ("summary", "run_dir", "run_card", "run_id", "strategy_id",
                                                                     "pack_id", "comparison_id")}}):
        text = _dump(out, [])
        assert len(text) <= TOOL_RESULT_LIMIT and json.loads(text)["status"] == out["status"], len(text)


@pytest.mark.skipif(not hasattr(__import__("os"), "killpg"), reason="process groups are POSIX")
def test_timeout_logs_are_written_only_under_an_allowed_run_root(tmp_path, monkeypatch) -> None:
    from src.agent import loop
    from src.tools import alphakeel_research_tool as tool

    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path / "runs"))
    script = "import sys, time\nprint('partial', file=sys.stderr, flush=True)\ntime.sleep(30)\n"
    monkeypatch.setattr(tool, "_child_cmd", lambda argv: [sys.executable, "-c", script])
    monkeypatch.setattr(loop, "TOOL_TIMEOUT_SECONDS", 2.0, raising=False)
    outside = tmp_path / "handoff" / "sid"
    out = json.loads(tool.AlphakeelResearchTool().execute(action="check", run_dir=str(outside)))
    assert out["stderr"].strip() == "partial" and "logs" not in out and not outside.exists(), out
