"""Generic-engine ``backtest`` runs get a code-made screen, a private trial ledger, a result card and a hold-out guard.

Regression of an audited session: ten generic backtests (nine in one run dir with edited code) were invisible to the
IR-only result card, the model called a 5-trade, Sharpe-0.10 result "validated", and OKX data past AlphaKeel's hold-out
cutoff was read. Every value here is synthetic; nothing touches the network.
"""

from __future__ import annotations

import datetime
import json
import random
from pathlib import Path

import pytest

from backtest.engine_screen import screen_engine_run

pytestmark = pytest.mark.unit

SESSION = "sess-engine"
DAY_MS = 86_400_000
CUTOFF_MS = 1_787_000_000_000  # 2026-08-17 21:33 UTC, not day aligned
CUTOFF_DAY_MS = CUTOFF_MS - CUTOFF_MS % DAY_MS


def _make_run(root: Path, name: str = "run", *, trades: int = 40, drift: float = 0.002, days: int = 200, code: str = "# v1\n",
              seed: int = 1, source: str = "yfinance", end_date: str | None = None, weight: float = 0.5,
              final_override: float | None = None) -> Path:
    rd = root / name
    (rd / "code").mkdir(parents=True, exist_ok=True)
    (rd / "artifacts").mkdir(parents=True, exist_ok=True)
    config = {"source": source, "codes": ["BTC-USDT"], "start_date": "2024-01-01", "initial_cash": 1_000_000}
    if end_date:
        config["end_date"] = end_date
    (rd / "config.json").write_text(json.dumps(config))
    (rd / "code" / "signal_engine.py").write_text(code)
    rng = random.Random(seed)
    eq, rows, pos = 1_000_000.0, ["timestamp,ret,equity"], ["timestamp,BTC-USDT"]
    for d in range(days):
        eq *= 1 + drift + rng.gauss(0, 0.005)
        ts = f"{(datetime.date(2024, 1, 1) + datetime.timedelta(days=d)).isoformat()} 16:00:00"
        rows.append(f"{ts},0.0,{eq}")
        pos.append(f"{ts},{weight if d % 2 == 0 else 0.0}")
    (rd / "artifacts" / "equity.csv").write_text("\n".join(rows) + "\n")
    (rd / "artifacts" / "positions.csv").write_text("\n".join(pos) + "\n")
    final = eq if final_override is None else final_override
    (rd / "artifacts" / "metrics.csv").write_text(
        f"final_value,trade_count,benchmark_return,excess_return,funding_paid\n{final},{trades},0.1,0.2,12.5\n")
    return rd


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    from src.tools import alphakeel_ir_request, alphakeel_research_ledger

    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path))
    monkeypatch.setattr(alphakeel_research_ledger, "_sessions_dir", lambda: tmp_path / "sessions")
    monkeypatch.setattr(alphakeel_ir_request, "service_check", lambda: {"holdout_cutoff_ms": CUTOFF_MS})


def _items(screen: dict) -> dict:
    return {i["id"]: i["result"] for i in screen["items"]}


def test_a_few_trades_is_insufficient(tmp_path) -> None:
    screen = screen_engine_run(_make_run(tmp_path, trades=5), trials=1)
    assert screen["evidence_status"] == "insufficient"
    assert _items(screen)["1.2"] == "FAIL" and _items(screen)["1.1"] == "PASS"
    assert screen["not_evaluated"][0] == "1.4" and "ir_robustness" in screen["next"]
    facts = screen["facts"]
    assert facts["direction"] == "long_only" and facts["codes"] == ["BTC-USDT"] and facts["funding_paid"] == 12.5
    assert 0.45 < facts["time_in_market"] < 0.55 and 0.2 < facts["avg_gross_exposure"] < 0.3


def test_strong_drift_passes_the_basics_and_more_trials_lower_the_dsr(tmp_path) -> None:
    rd = _make_run(tmp_path, trades=40)
    one = screen_engine_run(rd, trials=1)
    many = screen_engine_run(rd, trials=50, variant_sharpes=[0.1, 0.3, 0.2])
    assert [_items(one)[k] for k in ("1.1", "1.2", "1.3")] == ["PASS"] * 3
    assert many["facts"]["dsr"] < one["facts"]["dsr"] and many["facts"]["trials"] == 50


def test_missing_artifacts_are_unavailable_not_an_exception(tmp_path) -> None:
    out = screen_engine_run(tmp_path / "nope", trials=1)
    assert out["evidence_status"] == "unavailable" and out["error"]


def test_trial_counting_and_snapshots(tmp_path) -> None:
    from src.tools import alphakeel_research_ledger as L

    rd = _make_run(tmp_path, code="# v1\n")
    assert L.record_engine_run(SESSION, rd)["trials"] == 1
    (rd / "code" / "signal_engine.py").write_text("# v2\n")
    second = L.record_engine_run(SESSION, rd)
    assert second["trials"] == 2 and second["seq"] == 2
    L.record_engine_run(SESSION, rd)  # same code + config again: a second row, not a new trial
    rows = L.engine_rows(SESSION)
    assert [r["seq"] for r in rows] == [1, 2, 3] and len({r["trial_key"] for r in rows}) == 2
    assert (Path(rows[0]["snapshot_dir"]) / "code" / "signal_engine.py").read_text() == "# v1\n"
    assert (Path(rows[2]["snapshot_dir"]) / "code" / "signal_engine.py").read_text() == "# v2\n"
    assert L.record_engine_run(SESSION, rd)["trials"] == 2


def test_the_audited_session_nine_backtests_in_one_run_dir_show_on_the_card(tmp_path) -> None:
    from src.tools import alphakeel_research_ledger as L

    assert L.render_card(SESSION, True) is None
    for n in range(9):
        rd = _make_run(tmp_path, code=f"# edit {n}\n", seed=n, trades=5 + n, source="okx", end_date="2026-08-10")
        L.record_engine_run(SESSION, rd)
    zh = L.render_card(SESSION, True)
    assert "通用引擎回测 9 次 / 9 个不同版本（代码+配置）：" in zh
    assert "通用引擎结果不能直接上实盘：须交接 Strategy IR，通过 ir_robustness 与 AlphaKeel G1/G2。" in zh
    assert "| # | 标的 / 来源 | 窗口天数 | 成交 | 净收益 | DSR（试验数） | 方向 / 在场时间 | 判定 |" in zh
    assert "数据不可审计" not in zh  # no run card in the synthetic dirs: nothing claimed
    body = [ln for ln in zh.splitlines() if ln.startswith("| ") and "BTC-USDT / okx" in ln]
    assert len(body) == 9  # the latest 10 rows cover all 9
    snaps = {r["snapshot_dir"] for r in L.engine_rows(SESSION)}
    assert len(snaps) == 9
    assert "Generic engine backtests 9 / 9 distinct versions" in L.render_card(SESSION, False)
    # a modified snapshot is reported, not trusted
    victim = Path(L.engine_rows(SESSION)[0]["snapshot_dir"]) / "screen.json"
    victim.write_text(victim.read_text().replace("insufficient", "passed_screen"))
    assert "快照已改动或缺失" in L.render_card(SESSION, True)


def test_ir_only_and_engine_only_cards_both_render(tmp_path) -> None:
    from src.tools import alphakeel_research_ledger as L

    L.record_engine_run(SESSION, _make_run(tmp_path))
    card = L.render_card(SESSION, False)
    assert card is not None and card.startswith("---\n**Generic engine backtests")


# --- hold-out guard ------------------------------------------------------------------------------------------------------

def _guarded_run(tmp_path, monkeypatch, **kw) -> dict:
    from src.tools import backtest_tool

    called = []

    class Boom:
        def __init__(self, timeout=None):
            self.timeout = timeout

        def execute(self, *a, **k):
            called.append(1)
            raise AssertionError("the engine must not run")

    monkeypatch.setattr(backtest_tool, "Runner", Boom)
    rd = _make_run(tmp_path, **kw)
    out = json.loads(backtest_tool.run_backtest(str(rd)))
    out["_called"] = bool(called)
    return out


def test_okx_end_date_inside_the_holdout_is_refused_without_running_the_engine(tmp_path, monkeypatch) -> None:
    out = _guarded_run(tmp_path, monkeypatch, source="okx", end_date="2026-08-30")
    assert out["status"] == "error" and out["error_code"] == "holdout" and not out["_called"]
    from src.tools import alphakeel_research_ledger as L

    last_allowed = L._day(CUTOFF_DAY_MS - DAY_MS)
    assert "2026-08-30" in out["error"] and last_allowed in out["error"] and "service" in out["error"]


def test_okx_without_end_date_is_refused(tmp_path, monkeypatch) -> None:
    out = _guarded_run(tmp_path, monkeypatch, source="okx")
    assert out["error_code"] == "holdout" and "explicit end_date" in out["error"] and not out["_called"]


def test_end_date_on_the_last_allowed_day_passes_the_guard(tmp_path, monkeypatch) -> None:
    from src.tools import alphakeel_research_ledger as L, backtest_tool

    last_allowed = L._day(CUTOFF_DAY_MS - DAY_MS)
    assert backtest_tool._holdout_refusal({"source": "okx", "end_date": last_allowed}, SESSION) is None
    assert backtest_tool._holdout_refusal({"source": "okx", "end_date": L._day(CUTOFF_DAY_MS)}, SESSION) is not None


def test_a_failing_service_falls_back_to_60_days(tmp_path, monkeypatch) -> None:
    from src.tools import alphakeel_ir_request, alphakeel_research_ledger as L

    def boom():
        raise RuntimeError("service down")

    monkeypatch.setattr(alphakeel_ir_request, "service_check", boom)
    got = L.holdout_cutoff(SESSION)
    assert got["source"] == "fallback_60d" and got["cutoff_ms"] % DAY_MS == 0
    assert not (tmp_path / "sessions" / SESSION / L.HOLDOUT_FILE).exists()  # a fallback is never cached
    out = _guarded_run(tmp_path, monkeypatch, source="okx", end_date="2999-01-01")
    assert out["error_code"] == "holdout" and "fallback_60d" in out["error"]


def test_the_service_answer_is_cached_per_session(tmp_path, monkeypatch) -> None:
    from src.tools import alphakeel_ir_request, alphakeel_research_ledger as L

    assert L.holdout_cutoff(SESSION) == {"cutoff_ms": CUTOFF_MS, "source": "service"}
    monkeypatch.setattr(alphakeel_ir_request, "service_check", lambda: {"holdout_cutoff_ms": 1})
    assert L.holdout_cutoff(SESSION)["cutoff_ms"] == CUTOFF_MS  # fresh cache wins
    assert L.holdout_cutoff(None)["cutoff_ms"] == 1 and not (tmp_path / "sessions" / "None").exists()


def test_other_sources_are_never_refused(tmp_path, monkeypatch) -> None:
    from src.tools import backtest_tool

    assert backtest_tool._holdout_refusal({"source": "yfinance", "end_date": "2999-01-01"}, SESSION) is None
    assert backtest_tool._holdout_refusal({"source": "yfinance"}, SESSION) is None


def test_the_tool_result_carries_the_research_screen(tmp_path, monkeypatch) -> None:
    from src.core.runner import RunResult
    from src.providers.session_context import bind_llm_session_id, reset_llm_session_id
    from src.tools import backtest_tool

    class Ok:
        def __init__(self, timeout=None):
            self.timeout = timeout

        def execute(self, *a, **k):
            return RunResult(success=True, exit_code=0, stdout="", stderr="", artifacts={})

    monkeypatch.setattr(backtest_tool, "Runner", Ok)
    rd = _make_run(tmp_path, source="yfinance")
    no_session = json.loads(backtest_tool.run_backtest(str(rd)))["research_screen"]
    assert no_session["warning"] == "no session id: trials not counted" and no_session["evidence_status"] in ("failed_screen", "passed_screen")
    token = bind_llm_session_id(SESSION)
    try:
        out = json.loads(backtest_tool.run_backtest(str(rd)))["research_screen"]
    finally:
        reset_llm_session_id(token)
    assert out["seq"] == 1 and out["trials"] == 1 and {"id", "result", "observed"} == set(out["items"][0])
    assert out["facts"]["direction"] == "long_only" and "ir_robustness" in out["next"]


# --- card warning ---------------------------------------------------------------------------------------------------------

def test_the_card_warns_when_the_session_read_holdout_data(tmp_path) -> None:
    from src.tools import alphakeel_research_ledger as L

    L.holdout_cutoff(SESSION)  # caches the service cutoff
    L.record_engine_run(SESSION, _make_run(tmp_path))
    assert "hold-out" not in L.render_card(SESSION, False).split("Engine results")[-1]
    L.record_exposure(SESSION, "okx", CUTOFF_DAY_MS + 14 * DAY_MS, "backtest:run")
    zh, en = L.render_card(SESSION, True), L.render_card(SESSION, False)
    assert "⚠ 本会话读取过 AlphaKeel hold-out 区间的数据（截至 2026-08-30，hold-out 起点 2026-08-17）" in zh
    assert "cannot be handed to AlphaKeel (G1 1.14)" in en
