"""Backtest execution tool: validates config.json + signal_engine.py and runs the built-in engine."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from backtest.loaders.registry import VALID_SOURCES
from src.agent.progress import emit_progress
from src.agent.tools import BaseTool
from src.config.accessor import get_env_config
from src.config.limits import TOOL_RESULT_LIMIT
from src.core.runner import Runner
from src.core.state import RunStateStore
from src.tools.backtest_summary import collect_ohlcv_paths, try_build_backtest_summary
from src.tools.path_utils import safe_run_dir


def _backtest_timeout_seconds() -> float | None:
    """Return the configured backtest subprocess timeout.

    The backtest is a write-style tool, so the agent-loop timeout does not
    cancel it.  The subprocess still needs its own bound, which follows the
    same ``VIBE_TRADING_TOOL_TIMEOUT_SECONDS`` setting used by the loop.  A
    non-positive value keeps the historical "disabled" semantics.

    Returns:
        Positive timeout in seconds, or ``None`` to disable the bound.
    """
    configured = float(get_env_config().agent_tuning.vibe_trading_tool_timeout_seconds)
    return configured if configured > 0 else None


def _timeout_output(value: Any) -> str:
    """Normalize ``TimeoutExpired`` output for persistence and JSON."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _persist_timeout_output(run_path: Path, exc: subprocess.TimeoutExpired) -> dict[str, str]:
    """Persist partial subprocess output after a timeout.

    ``subprocess.run`` exposes captured output on ``TimeoutExpired`` when pipes
    are used.  Preserve it before returning so a timed-out run remains
    diagnosable instead of appearing to have produced nothing.
    """
    output = {
        "stdout": _timeout_output(getattr(exc, "stdout", None)),
        "stderr": _timeout_output(getattr(exc, "stderr", None)),
    }
    log_dir = run_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "runner_stdout.txt").write_text(output["stdout"], encoding="utf-8")
    (log_dir / "runner_stderr.txt").write_text(output["stderr"], encoding="utf-8")
    return output


def _note_child_exposure(run_path: Path, config: dict[str, Any]) -> None:
    """Fold the engine child's data exposure into this process (``backtest.data_audit.record_exposure``, which also writes
    the bound conversation's exposure file): the child's own record dies with it. The later of the run card's
    ``data_audit.data_exposure_end_ms`` and, for a source whose reads are recorded (``NON_AUDITABLE_SOURCES``: live
    exchange pulls), the end the config asked for -- a run that failed or timed out after its fetch wrote no card (or
    left an earlier run's) but still saw that data. Never raises."""
    try:
        import pandas as pd

        from backtest.data_audit import NON_AUDITABLE_SOURCES, record_exposure

        source = str(config.get("source") or "")
        ends = []
        try:
            card = json.loads((run_path / "run_card.json").read_text(encoding="utf-8"))
            ends.append((card.get("data_audit") or {}).get("data_exposure_end_ms"))
        except (OSError, ValueError, AttributeError):
            pass
        if source in NON_AUDITABLE_SOURCES and config.get("end_date"):
            ends.append(int((pd.Timestamp(str(config["end_date"])) + pd.Timedelta(days=1)).timestamp() * 1000))
        ends = [e for e in ends if isinstance(e, int) and not isinstance(e, bool)]
        if ends:
            record_exposure(source or "backtest", max(ends), via=f"backtest:{run_path.name}")
    except Exception:  # noqa: BLE001 - provenance bookkeeping must not break the tool result
        logging.getLogger(__name__).warning("backtest data exposure not recorded for %s", run_path, exc_info=True)


def _holdout_refusal(config: dict[str, Any], session_id: str | None) -> str | None:
    """None when the config may run; else the refusal message. A crypto/AlphaKeel source must end before AlphaKeel's
    hold-out cutoff (AGENTS.md rule 7: research never reads the hold-out). Any failure here refuses against the fallback
    cutoff -- the guard is never skipped."""
    from src.tools import alphakeel_research_ledger as L

    if config.get("source") not in L.HOLDOUT_SOURCES:
        return None
    day_ms = 86_400_000
    try:
        cutoff = L.holdout_cutoff(session_id)
        cutoff_ms, origin = int(cutoff["cutoff_ms"]), str(cutoff["source"])
    except Exception:  # noqa: BLE001 - never skip the guard
        logging.getLogger(__name__).warning("hold-out cutoff failed; using the %d-day fallback", L.DEFAULT_HOLDOUT_DAYS, exc_info=True)
        cutoff_ms, origin = L._fallback_cutoff_ms(), "fallback_60d"
    last_allowed = L._day(cutoff_ms - day_ms)
    end_date = config.get("end_date")
    tail = ("The hold-out is AlphaKeel's out-of-sample window that research never reads: "
            f"the hold-out starts {L._day(cutoff_ms)} (cutoff from {origin}); last allowed end_date is {last_allowed}.")
    if not end_date:
        return f"a crypto backtest needs an explicit end_date on or before {last_allowed}. {tail}"
    try:
        import pandas as pd

        end_ms = int((pd.Timestamp(str(end_date)).tz_localize(None) + pd.Timedelta(days=1)).tz_localize("UTC").timestamp() * 1000)
    except Exception:  # noqa: BLE001 - an unreadable end_date cannot be shown to be safe
        return f"end_date {end_date!r} is not an ISO date (YYYY-MM-DD) and cannot be checked. {tail}"
    if end_ms <= cutoff_ms:
        return None
    return f"end_date {end_date} reaches into the hold-out. {tail}"


def _research_screen(run_path: Path) -> dict[str, Any]:
    """The code-made screen of a finished run, reduced for the tool result; never raises."""
    from backtest.engine_screen import screen_engine_run
    from src.providers.session_context import current_llm_session_id

    session_id = current_llm_session_id()
    try:
        if session_id:
            from src.tools.alphakeel_research_ledger import record_engine_run

            screen = record_engine_run(session_id, run_path)
        else:
            screen = {**screen_engine_run(run_path, trials=1), "trials": 1, "warning": "no session id: trials not counted"}
    except Exception as exc:  # noqa: BLE001 - the screen never turns a successful run into an error
        logging.getLogger(__name__).warning("engine run not recorded for %s", run_path, exc_info=True)
        return {"evidence_status": "unavailable", "error": str(exc)}
    if screen.get("evidence_status") == "unavailable":
        return {k: screen[k] for k in ("evidence_status", "error", "trials", "seq", "warning") if k in screen}
    out: dict[str, Any] = {
        "evidence_status": screen.get("evidence_status"),
        "items": [{"id": i.get("id"), "result": i.get("result"), "observed": i.get("observed")} for i in screen.get("items") or []],
        "facts": screen.get("facts"), "trials": screen.get("trials"), "seq": screen.get("seq"),
        "next": screen.get("next"), "not_evaluated": screen.get("not_evaluated"),
    }
    if screen.get("warning"):
        out["warning"] = screen["warning"]
    return {k: v for k, v in out.items() if v is not None}


def run_backtest(run_dir: str) -> str:
    """Run backtest: validate config.json + signal_engine.py, invoke built-in engine.

    Args:
        run_dir: Path to the run directory.

    Returns:
        JSON-formatted execution result.
    """
    emit_progress("validate", message="validating run_dir and config")
    try:
        run_path = safe_run_dir(run_dir)
    except ValueError as exc:
        return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)

    config_path = run_path / "config.json"
    if not config_path.exists():
        return json.dumps(
            {
                "status": "error",
                "error": f"config.json not found in {run_path}",
                "hint": "config.json belongs at the root of run_dir.",
            },
            ensure_ascii=False,
        )

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return json.dumps({"status": "error", "error": f"config.json parse error: {e}"}, ensure_ascii=False)

    if "source" not in config:
        return json.dumps({"status": "error", "error": "config.json missing 'source' field (tushare/okx/yfinance)"}, ensure_ascii=False)

    if config["source"] not in VALID_SOURCES:
        return json.dumps({"status": "error", "error": f"source must be one of {VALID_SOURCES}, got: {config['source']}"}, ensure_ascii=False)

    signal_path = run_path / "code" / "signal_engine.py"
    if not signal_path.exists():
        return json.dumps(
            {
                "status": "error",
                "error": f"code/signal_engine.py not found in {run_path}",
                "hint": "signal_engine.py belongs in code/ inside run_dir.",
            },
            ensure_ascii=False,
        )

    from src.providers.session_context import current_llm_session_id

    try:
        refusal = _holdout_refusal(config, current_llm_session_id() or None)
    except Exception:  # noqa: BLE001 - never skip the guard
        logging.getLogger(__name__).warning("hold-out guard failed", exc_info=True)
        refusal = "the hold-out guard failed; fix end_date and retry (research never reads AlphaKeel's hold-out)"
    if refusal:
        return json.dumps({"status": "error", "error_code": "holdout", "error": refusal, "run_dir": run_dir}, ensure_ascii=False)

    agent_root = Path(__file__).resolve().parents[2]
    entry_script = agent_root / "backtest" / "runner.py"

    source = config.get("source", "?")
    emit_progress(
        "simulate",
        message=f"running backtest engine (source={source})",
    )
    runner = Runner(timeout=_backtest_timeout_seconds())
    try:
        result = runner.execute(
            entry_script,
            run_path,
            cwd=agent_root,
            cli_args=[str(run_path)],
        )
    except subprocess.TimeoutExpired as exc:
        _note_child_exposure(run_path, config)
        # The lifecycle block below is unreachable on a timeout, so record the
        # failure here — otherwise the run is indistinguishable from never-run
        # (the evidence gate fail-closes either way, but the reason is lost).
        timeout_output = _persist_timeout_output(run_path, exc)
        timeout_label = f"{runner.timeout}s" if runner.timeout is not None else "the configured limit"
        reason = f"backtest engine timed out after {timeout_label}"
        RunStateStore().mark_failure(run_path, reason)
        response = {
            "status": "error",
            "error": reason,
            "run_dir": run_dir,
        }
        if timeout_output["stdout"]:
            response["stdout"] = timeout_output["stdout"][-2000:]
        if timeout_output["stderr"]:
            response["stderr"] = timeout_output["stderr"][-2000:]
        return json.dumps(response, ensure_ascii=False)

    # Record lifecycle status so tool-driven runs are ingestible by the
    # evidence pipeline: refresh_strategy_evidence fail-closes without
    # state.json, which previously only the runtime loop wrote (#1412).
    # Same contract as the runtime — success, or failure with a reason.
    _note_child_exposure(run_path, config)
    state_store = RunStateStore()
    if result.success:
        state_store.mark_success(run_path)
    else:
        state_store.mark_failure(run_path, f"backtest engine exited with code {result.exit_code}")

    emit_progress("finalize", message="collecting artifacts")
    artifacts_found = {name: str(path) for name, path in result.artifacts.items()}
    envelope: dict[str, Any] = {
        "status": "ok" if result.success else "error",
        "exit_code": result.exit_code,
        "stdout": result.stdout[-2000:] if len(result.stdout) > 2000 else result.stdout,
        "stderr": result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr,
        "artifacts": artifacts_found,
        "run_dir": run_dir,
    }
    if result.success:
        # Small envelopes retain the legacy fields unchanged. Oversized
        # envelopes are fitted below with explicit omission notices. A missing
        # or corrupt run card never turns a successful run into an error.
        ohlcv_paths = collect_ohlcv_paths(run_path)
        artifacts_found["ohlcv"] = ohlcv_paths
        summary = try_build_backtest_summary(run_path, ohlcv_paths)
        if summary is not None:
            envelope["summary"] = summary
        envelope["research_screen"] = _research_screen(run_path)
    return _serialize_result(envelope)


def _serialize_result(envelope: dict[str, Any]) -> str:
    """Keep structured results usable through the agent's delivery cap."""
    def encode() -> str:
        return json.dumps(envelope, ensure_ascii=False)

    if len(encode()) <= TOOL_RESULT_LIMIT:
        return encode()
    for key in ("stdout", "stderr"):
        if envelope.get(key):
            envelope[key] = ""
            envelope["logs_omitted"] = "Read the run logs for full stdout/stderr."
    summary = envelope.get("summary")
    if isinstance(summary, dict):
        points = summary.get("equity_preview", [])
        while len(encode()) > TOOL_RESULT_LIMIT and len(points) > 2:
            count = max(2, len(points) // 2)
            points = [points[i * (len(points) - 1) // (count - 1)] for i in range(count)]
            summary["equity_preview"] = points
        if len(encode()) > TOOL_RESULT_LIMIT:
            envelope.pop("summary")
            envelope["summary_omitted"] = "Summary exceeds the result budget; inspect run_card.json locally or use read_run_artifact in meta mode."
    if len(encode()) > TOOL_RESULT_LIMIT:
        envelope["artifacts"] = {}
        envelope["artifacts_omitted"] = "Artifact paths exceed the result budget; inspect the run directory with read_file."
    screen = envelope.get("research_screen")
    if len(encode()) > TOOL_RESULT_LIMIT and isinstance(screen, dict):  # last resort: keep status, facts and counts
        for key in ("not_evaluated", "next", "items"):
            screen.pop(key, None)
    return encode()


class BacktestTool(BaseTool):
    """Backtest execution tool."""

    name = "backtest"
    description = "Run backtest: validate config.json + signal_engine.py, invoke built-in engine."
    parameters = {
        "type": "object",
        "properties": {
            "run_dir": {"type": "string", "description": "Path to the run directory"},
        },
        "required": ["run_dir"],
    }
    repeatable = True
    is_readonly = False

    def execute(self, **kwargs) -> str:
        """Execute backtest."""
        return run_backtest(kwargs["run_dir"])
