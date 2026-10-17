"""Parameter neighbourhood and component ablation of a generic-engine backtest (``backtest/robustness.py``).

A strategy declares ``params`` / ``param_ranges`` / ``components`` in config.json; the runner sets them on
``SignalEngine.PARAMS`` / ``COMPONENTS``, a second runner pass (``--robustness``) runs every neighbour and every ablation on
the same data, each sweep run is a trial of the session, and the result card carries system-written lines. Every value here
is synthetic; nothing touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backtest import robustness as rb
from tests.test_engine_screen import SESSION, _make_run

pytestmark = pytest.mark.unit

DECLARED = {"params": {"fast": 10, "slow": 30}, "components": ["trend", "vol"]}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    from src.tools import alphakeel_ir_request, alphakeel_research_ledger

    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path))
    monkeypatch.setattr(alphakeel_research_ledger, "_sessions_dir", lambda: tmp_path / "sessions")
    monkeypatch.setattr(alphakeel_ir_request, "service_check", lambda: {"holdout_cutoff_ms": 1_787_000_000_000})
    alphakeel_research_ledger._FALLBACK_MEMO.clear()


# --- the convention (pure) ------------------------------------------------------------------------------------------------

def test_neighbours_are_one_step_or_twenty_percent_and_respect_bounds() -> None:
    runs, skipped = rb.variants({"params": {"n": 10, "k": 2, "x": 0.5, "z": 0, "s": 14, "b": 3},
                                 "param_ranges": {"s": {"step": 1}, "b": {"step": 1, "min": 3}}})
    got = {(r["param"], r["side"]): r["value"] for r in runs}
    assert got[("n", "-")] == 8 and got[("n", "+")] == 12 and isinstance(got[("n", "+")], int)
    assert got[("k", "-")] == 1 and got[("k", "+")] == 3  # an integer always moves by at least 1
    assert got[("x", "-")] == 0.4 and got[("x", "+")] == 0.6
    assert got[("s", "-")] == 13 and got[("s", "+")] == 15
    assert ("b", "-") not in got and got[("b", "+")] == 4
    assert {"param": "z", "reason": "zero_without_step"} in skipped and {"param": "b", "reason": "out_of_range"} in skipped
    assert all(r["kind"] == "param" for r in runs)


def test_ablation_switches_off_each_component_that_is_on() -> None:
    config = {"components": ["a", "b", "c"], "components_off": ["b"]}
    runs, _ = rb.variants(config)
    assert [r["component"] for r in runs] == ["a", "c"]
    assert rb.variant_config(config, runs[1])["components_off"] == ["b", "c"]
    moved = rb.variant_config({**DECLARED, "supersedes_contract": "a reason that is long"},
                              {"kind": "param", "param": "fast", "value": 8})
    assert moved["params"] == {"fast": 8, "slow": 30} and "supersedes_contract" not in moved


@pytest.mark.parametrize("bad", [
    {"params": {}}, {"params": {"fast": True}}, {"params": {"fast": "10"}}, {"params": {"1x": 1}},
    {"params": {"fast": float("nan")}}, {"params": {f"p{i}": i for i in range(9)}},
    {"params": {"fast": 1}, "param_ranges": {"slow": {"step": 1}}},
    {"params": {"fast": 1}, "param_ranges": {"fast": {"stride": 1}}},
    {"params": {"fast": 1}, "param_ranges": {"fast": {"step": 0}}},
    {"params": {"fast": 1}, "param_ranges": {"fast": {"min": 5, "max": 1}}},
    {"components": []}, {"components": ["a", "a"]}, {"components": ["a b"]}, {"components_off": ["a"]},
    {"components": ["a"], "components_off": ["b"]},
])
def test_a_malformed_declaration_is_refused(bad) -> None:
    with pytest.raises(ValueError):
        rb.validate(bad)


def test_a_config_without_the_convention_is_unchanged() -> None:
    from backtest.runner import BacktestConfigSchema

    plain = {"codes": ["AAPL.US"], "start_date": "2024-01-01", "end_date": "2024-06-01", "source": "yfinance",
             "optimizer_params": {"lookback": 60}}
    BacktestConfigSchema(**plain)
    assert not rb.declared(plain) and rb.variants(plain) == ([], [])

    class Engine:
        PARAMS = {"keep": 1}

    rb.apply(Engine, plain)
    assert Engine.PARAMS == {"keep": 1} and not hasattr(Engine, "COMPONENTS")
    with pytest.raises(ValueError, match="params"):
        BacktestConfigSchema(**{**plain, "params": {"fast": "x"}})


def test_apply_overlays_the_class_defaults() -> None:
    class Engine:
        PARAMS = {"fast": 5, "other": 2}

    rb.apply(Engine, {**DECLARED, "components_off": ["vol"]})
    assert Engine.PARAMS == {"fast": 10, "other": 2, "slow": 30}
    assert Engine.COMPONENTS == {"trend": True, "vol": False}


def test_neighbourhood_share_counts_missing_sharpes_and_needs_a_positive_point() -> None:
    assert rb.neighbourhood_summary(1.0, [0.4, 0.6, None, 1.2]) == {"n": 4, "below_half": 2, "share": 0.5}
    assert rb.neighbourhood_summary(-0.2, [0.1])["share"] is None
    assert rb.neighbourhood_summary(None, [0.1])["share"] is None


def test_the_runner_security_check_accepts_the_convention(tmp_path) -> None:
    from backtest.runner import _validate_signal_engine_source

    path = tmp_path / "signal_engine.py"
    path.write_text("class SignalEngine:\n    PARAMS = {'fast': 10}\n    COMPONENTS = {'trend': True}\n\n"
                    "    def generate(self, data_map):\n        n = self.PARAMS['fast']\n"
                    "        return {} if self.COMPONENTS['trend'] else {'n': n}\n")
    _validate_signal_engine_source(path)


# --- the runner's sweep pass ----------------------------------------------------------------------------------------------

def test_the_sweep_pass_runs_every_variant_on_fresh_engines_and_survives_a_failure(tmp_path, monkeypatch) -> None:
    from backtest import runner

    seen = []

    class Market:
        def run_backtest(self, config, loader, signal_engine, run_dir, bars_per_year=None):
            seen.append((dict(signal_engine.PARAMS), dict(signal_engine.COMPONENTS), config["params"]))
            if signal_engine.PARAMS["fast"] == 12:
                raise RuntimeError("boom")
            (Path(run_dir) / "artifacts").mkdir(parents=True)
            (Path(run_dir) / "artifacts" / "metrics.csv").write_text("final_value\n1\n")

    class Engine:
        PARAMS = {"fast": 1}

        def generate(self, data_map):
            return {}

    monkeypatch.setattr(runner, "_create_market_engine", lambda *a: Market())
    rd = tmp_path / "run"
    (rd / "code").mkdir(parents=True)
    (rd / "code" / "signal_engine.py").write_text("# code\n")
    (rd / "robustness" / "stale").mkdir(parents=True)
    declared = {"codes": ["X"], **DECLARED}
    runner._run_robustness(rd, declared, {**declared, "_run_card_x": 1}, Engine, None, "yfinance", ["X"], 252)
    manifest = json.loads((rd / "robustness" / "manifest.json").read_text())
    status = {e["label"]: e["status"] for e in manifest["runs"]}
    assert status == {"p_fast_minus": "ok", "p_fast_plus": "failed", "p_slow_minus": "ok", "p_slow_plus": "ok",
                      "a_trend": "ok", "a_vol": "ok"}
    assert "boom" in next(e["error"] for e in manifest["runs"] if e["label"] == "p_fast_plus")
    assert not (rd / "robustness" / "stale").exists()
    assert seen[0][0] == {"fast": 8, "slow": 30} and seen[4][1] == {"trend": False, "vol": True}
    vconf = json.loads((rd / "robustness" / "a_vol" / "config.json").read_text())
    assert vconf == rb.variant_config(declared, {"kind": "ablation", "component": "vol"}) and "_run_card_x" not in vconf
    assert (rd / "robustness" / "a_vol" / "code" / "signal_engine.py").read_text() == "# code\n"


# --- ledger, trials and card ----------------------------------------------------------------------------------------------

#: per sweep label: (drift, seed) of its synthetic result; the parent is drift 0.002, seed 1
SWEEP = {"p_fast_minus": (0.0, 2), "p_fast_plus": (0.004, 5), "p_slow_minus": (0.002, 1), "p_slow_plus": (0.002, 1),
         "a_trend": (-0.001, 3), "a_vol": (0.0, 4)}


def _write_sweep(rd: Path, *, failed: tuple[str, ...] = ()) -> None:
    config = json.loads((rd / "config.json").read_text())
    runs, skipped = rb.variants(config)
    manifest = []
    for run in runs:
        drift, seed = SWEEP[run["label"]]
        vdir = _make_run(rd / "robustness", run["label"], drift=drift, seed=seed)
        (vdir / "config.json").write_text(json.dumps(rb.variant_config(config, run)))
        manifest.append({**run, "status": "failed" if run["label"] in failed else "ok", "error": "boom"})
    (rd / "robustness" / "manifest.json").write_text(json.dumps({"runs": manifest, "skipped": skipped}))


def _parent(tmp_path, **kw) -> tuple[Path, dict]:
    from src.tools import alphakeel_research_ledger as L

    rd = _make_run(tmp_path, config_extra=DECLARED, **kw)
    return rd, L.record_engine_run(SESSION, rd)


def test_every_sweep_run_is_a_trial_and_the_card_shows_the_neighbourhood(tmp_path) -> None:
    from src.tools import alphakeel_research_ledger as L

    rd, first = _parent(tmp_path)
    assert first["trials"] == 1
    _write_sweep(rd)
    doc = L.record_robustness(SESSION, rd, first["seq"])
    assert [r["status"] for r in doc["runs"]] == ["ok"] * 6
    # the two slow neighbours reproduce the chosen point bit for bit: one trial, not three
    assert L.trial_count(SESSION) == 5
    current = L.current_engine_screen(SESSION, first["seq"])
    assert current["facts"]["trials"] == 5
    card = L.render_card(SESSION, zh=False)
    assert "parameter neighbourhood (system sweep of 4 neighbours" in card
    assert "1/4 neighbours have a Sharpe below half the chosen point's -- the result depends on the exact parameter" in card
    assert "  - fast=10: 8 → Sharpe" in card and "12 → Sharpe" in card
    assert "  - slow=30: 24 → identical to the chosen point; 36 → identical to the chosen point (both neighbours" in card
    assert "component ablation" in card and "trend → Sharpe" in card and "below half the chosen point" in card
    assert "removing any one component halves the Sharpe" in card
    assert "(the 6 sweep backtests above count in this session's trials by distinct result" in card
    assert "(6 of them are system parameter sweeps / component ablations" in card
    table = [ln for ln in card.splitlines() if ln.startswith("| ") and ln[2].isdigit()]
    assert len(table) == 1 and table[0].startswith("| 1 |")
    # a sweep row identical to the chosen point does not take its place; a sweep row that is best says what it is
    assert "Multiple comparison (5 distinct versions" in card and "the best is #3 (a system sweep of #1)" in card
    assert "- #1: Sharpe first 75% / last 25%" in card
    zh = L.render_card(SESSION, zh=True)
    assert "参数邻域（系统扫描 4 个邻点" in zh and "组件消融" in zh and "代码可能没有读取 self.PARAMS[\"slow\"]" in zh


def test_a_failed_tampered_or_misconfigured_sweep_run_is_not_shown_as_a_result(tmp_path) -> None:
    from src.tools import alphakeel_research_ledger as L

    rd, first = _parent(tmp_path)
    _write_sweep(rd, failed=("a_vol",))
    (rd / "robustness" / "a_trend" / "config.json").write_text(json.dumps({**DECLARED, "params": {"fast": 99, "slow": 30}}))
    doc = L.record_robustness(SESSION, rd, first["seq"])
    status = {r["label"]: (r["status"], r.get("error")) for r in doc["runs"]}
    assert status["a_vol"] == ("failed", "boom")
    assert status["a_trend"] == ("failed", "config.json is not the planned sweep config")
    row = next(r for r in L.engine_rows(SESSION) if (r.get("sweep") or {}).get("label") == "p_fast_minus")
    (Path(row["snapshot_dir"]) / "returns.json").write_text("[0.5]")
    card = L.render_card(SESSION, zh=False)
    assert "8 → snapshot does not match the record" in card
    assert "trend → backtest failed" in card and "vol → backtest failed" in card
    assert "(2 sweep backtests failed and are not counted)" in card
    assert "removing any one component" not in card


def test_a_sweep_run_never_counts_as_a_passed_version(tmp_path) -> None:
    from src.tools import alphakeel_research_ledger as L

    rd, first = _parent(tmp_path, trades=3)  # the chosen point is insufficient
    config = json.loads((rd / "config.json").read_text())
    run = rb.variants(config)[0][0]
    vdir = _make_run(rd / "robustness", run["label"], drift=0.004, seed=7)
    (vdir / "config.json").write_text(json.dumps(rb.variant_config(config, run)))
    (rd / "robustness" / "manifest.json").write_text(json.dumps({"runs": [{**run, "status": "ok"}]}))
    L.record_robustness(SESSION, rd, first["seq"])
    sweep_row = next(r for r in L.engine_rows(SESSION) if r.get("sweep"))
    assert L.verified_engine_screen(sweep_row)["evidence_status"] == "passed_screen"
    assert L.session_has_passed_trial(SESSION) is False
    assert L.contract_history(SESSION) == [] or all(h.get("seq") != sweep_row["seq"] for h in L.contract_history(SESSION))


def test_without_the_convention_nothing_is_swept_or_shown(tmp_path) -> None:
    from src.tools import alphakeel_research_ledger as L

    rd = _make_run(tmp_path)
    first = L.record_engine_run(SESSION, rd)
    assert L.record_robustness(SESSION, rd, first["seq"]) is None
    card = L.render_card(SESSION, zh=False)
    assert "neighbourhood" not in card and "ablation" not in card and "sweep" not in card


# --- the tool -------------------------------------------------------------------------------------------------------------

def _fake_runner(calls: list):
    from src.core.runner import RunResult

    class FakeRunner:
        def __init__(self, timeout=None):
            self.timeout = timeout

        def execute(self, entry, run_path, *, cwd=None, cli_args=None):
            calls.append(list(cli_args or []))
            logs = Path(run_path) / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            sweep = "--robustness" in (cli_args or [])
            (logs / "runner_stdout.txt").write_text("sweep" if sweep else "main")
            if sweep:
                _write_sweep(Path(run_path))
            return RunResult(success=True, exit_code=0, stdout="", stderr="", artifacts={})

    return FakeRunner


@pytest.mark.parametrize("declared", [True, False])
def test_the_tool_runs_the_sweep_only_for_a_declared_strategy(tmp_path, monkeypatch, declared) -> None:
    from src.providers.session_context import bind_llm_session_id, reset_llm_session_id
    from src.tools import backtest_tool

    calls: list = []
    monkeypatch.setattr(backtest_tool, "Runner", _fake_runner(calls))
    rd = _make_run(tmp_path, source="yfinance", codes=("AAPL.US",), config_extra=DECLARED if declared else None)
    token = bind_llm_session_id(SESSION)
    try:
        out = json.loads(backtest_tool.run_backtest(str(rd)))
    finally:
        reset_llm_session_id(token)
    if not declared:
        assert len(calls) == 1 and "robustness" not in out
        return
    assert calls[1] == [str(rd), "--robustness"]
    sweep = out["robustness"]
    assert sweep["status"] == "ok" and sweep["runs"] == 6 and sweep["recorded"] == 6 and sweep["trials"] == 5
    assert any("parameter neighbourhood" in ln for ln in sweep["lines"])
    assert out["research_screen"]["trials"] == 5 and out["research_screen"]["facts"]["trials"] == 5
    assert (rd / "logs" / "runner_stdout.txt").read_text() == "main"
    assert (rd / "robustness" / "logs" / "runner_stdout.txt").read_text() == "sweep"


# --- the real engine, end to end ------------------------------------------------------------------------------------------

REAL_CODE = '''class SignalEngine:
    PARAMS = {"fast": 5, "slow": 20}
    COMPONENTS = {"trend": True}

    def generate(self, data_map):
        out = {}
        for code, df in data_map.items():
            c = df["close"]
            sig = (c.rolling(self.PARAMS["fast"]).mean() > c.rolling(self.PARAMS["slow"]).mean()).astype(float)
            if self.COMPONENTS["trend"]:
                sig = sig * (c > c.rolling(60).mean()).astype(float)
            out[code] = sig
        return out
'''


def test_the_sweep_pass_on_the_real_engine_reaches_the_card(tmp_path, monkeypatch) -> None:
    import numpy as np
    import pandas as pd

    from backtest import runner
    from src.tools import alphakeel_research_ledger as L

    idx = pd.bdate_range("2024-01-02", periods=300)
    close = 100 * np.exp(np.cumsum(np.random.default_rng(0).normal(0.0005, 0.015, 300)))
    frame = pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1e6}, index=idx)

    class Loader:
        name = "yahoo"

        def fetch(self, codes, start_date, end_date, **kw):
            return {c: frame for c in codes}

    monkeypatch.setattr(runner, "_get_loader", lambda source: Loader)
    rd = tmp_path / "run"
    (rd / "code").mkdir(parents=True)
    (rd / "code" / "signal_engine.py").write_text(REAL_CODE)
    (rd / "config.json").write_text(json.dumps({"source": "yfinance", "codes": ["AAPL.US"], "start_date": "2024-01-02",
                                                "end_date": "2025-02-20", "params": {"fast": 10, "slow": 30},
                                                "components": ["trend"]}))
    runner.main(rd)
    runner.main(rd, robustness=True)
    first = L.record_engine_run(SESSION, rd)
    doc = L.record_robustness(SESSION, rd, first["seq"])
    assert [r["status"] for r in doc["runs"]] == ["ok"] * 5
    keys = {r["trial_key"] for r in L.engine_rows(SESSION)}
    assert len(keys) == 6  # every neighbour and the ablation changed the result: the code reads PARAMS and COMPONENTS
    lines = L.robustness_lines(SESSION, zh=False)
    assert lines[0].startswith("- #1 parameter neighbourhood (system sweep of 4 neighbours")
    assert "identical to the chosen point" not in "\n".join(lines) and "trend → Sharpe" in lines[-2]
