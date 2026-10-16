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
              final_override: float | None = None, codes: tuple[str, ...] = ("BTC-USDT",), config_extra: dict | None = None,
              ohlcv: bool = True) -> Path:
    rd = root / name
    (rd / "code").mkdir(parents=True, exist_ok=True)
    (rd / "artifacts").mkdir(parents=True, exist_ok=True)
    config = {"source": source, "codes": list(codes), "start_date": "2024-01-01", "initial_cash": 1_000_000}
    if end_date:
        config["end_date"] = end_date
    config.update(config_extra or {})
    (rd / "config.json").write_text(json.dumps(config))
    (rd / "code" / "signal_engine.py").write_text(code)
    rng = random.Random(seed)
    eq, rows, pos = 1_000_000.0, ["timestamp,ret,equity"], ["timestamp,BTC-USDT"]
    for d in range(days):
        eq *= 1 + drift + rng.gauss(0, 0.005)
        ts = f"{(datetime.date(2024, 1, 1) + datetime.timedelta(days=d)).isoformat()} 16:00:00"
        rows.append(f"{ts},0.0,{eq}")
        pos.append(f"{ts},{weight if d % 2 == 0 else 0.0}")
    if ohlcv:  # full coverage of the window: one daily bar per equity row
        bars = ["trade_date,open,high,low,close,volume"] + [r.split(",")[0] + ",1,1,1,1,1" for r in rows[1:]]
        for code in codes:
            (rd / "artifacts" / f"ohlcv_{code}.csv").write_text("\n".join(bars) + "\n")
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
    alphakeel_research_ledger._FALLBACK_MEMO.clear()


def _items(screen: dict) -> dict:
    return {i["id"]: i["result"] for i in screen["items"]}


def test_a_few_trades_is_insufficient(tmp_path) -> None:
    screen = screen_engine_run(_make_run(tmp_path, trades=5), trials=1)
    assert screen["evidence_status"] == "insufficient"
    assert _items(screen)["1.2"] == "FAIL" and _items(screen)["1.1"] == "PASS"
    assert screen["not_evaluated"][0] == "1.4" and "ir_robustness" in screen["next"]
    facts = screen["facts"]
    assert facts["direction"] == "long_only" and facts["codes"] == ["BTC-USDT"] and facts["funding_paid"] == "12.5"
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
    _make_run(tmp_path, code="# v2\n", seed=2)  # same run dir, edited code, different result: a new trial
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
    assert "通用引擎回测 9 次 / 9 个不同结果（按 metrics 去重）：" in zh
    assert "通用引擎结果不能直接上实盘：须交接 Strategy IR，通过 ir_robustness 与 AlphaKeel G1/G2。" in zh
    assert "| # | 标的 / 来源 | 窗口天数 | 成交 | 净收益率 | DSR（试验数） | 方向 / 在场时间 | 判定 |" in zh
    assert "数据不可审计" not in zh  # no run card in the synthetic dirs: nothing claimed
    body = [ln for ln in zh.splitlines() if ln.startswith("| ") and "BTC-USDT / okx" in ln]
    assert len(body) == 9  # the latest 10 rows cover all 9
    snaps = {r["snapshot_dir"] for r in L.engine_rows(SESSION)}
    assert len(snaps) == 9
    assert "Generic engine backtests 9 / 9 distinct results (by metrics)" in L.render_card(SESSION, False)
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

    equity = {"codes": ["AAPL.US"]}
    assert backtest_tool._holdout_refusal({"source": "yfinance", "end_date": "2999-01-01", **equity}, SESSION) is None
    assert backtest_tool._holdout_refusal({"source": "yfinance", **equity}, SESSION) is None
    assert backtest_tool._holdout_refusal({"source": "tushare", "codes": ["000001.SZ"], "end_date": "2999-01-01"}, SESSION) is None


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
    rd = _make_run(tmp_path, source="yfinance", codes=("AAPL.US",))
    no_session = json.loads(backtest_tool.run_backtest(str(rd)))["research_screen"]
    assert no_session["warning"] == "no session id: trials not counted" and no_session["evidence_status"] in ("failed_screen", "passed_screen")
    token = bind_llm_session_id(SESSION)
    try:
        out = json.loads(backtest_tool.run_backtest(str(rd)))["research_screen"]
    finally:
        reset_llm_session_id(token)
    assert out["seq"] == 1 and out["trials"] == 1 and {"id", "result", "observed"} == set(out["items"][0])
    assert out["facts"]["direction"] == "long_only" and "ir_robustness" in out["next"]
    assert out["provisional"] == ("1.6 and the status are recomputed on the result card as this session's trial count grows; "
                                  "quote the card, not this field")
    # no session + a screen warning: both warnings are kept
    cfg = json.loads((rd / "config.json").read_text())
    cfg.pop("initial_cash")
    cfg["initial_capital"] = 500_000
    (rd / "config.json").write_text(json.dumps(cfg))
    both = json.loads(backtest_tool.run_backtest(str(rd)))["research_screen"]["warning"]
    assert "ignores initial_capital" in both and both.endswith("; no session id: trials not counted")


# --- card warning ---------------------------------------------------------------------------------------------------------

def test_the_card_warns_when_the_session_read_holdout_data(tmp_path) -> None:
    from src.tools import alphakeel_research_ledger as L

    L.record_engine_run(SESSION, _make_run(tmp_path))
    assert "hold-out" not in L.render_card(SESSION, False).split("Engine results")[-1]
    L.record_exposure(SESSION, "okx", CUTOFF_DAY_MS + 14 * DAY_MS, "backtest:run")
    zh, en = L.render_card(SESSION, True), L.render_card(SESSION, False)
    assert "⚠ 本会话读取过 AlphaKeel hold-out 区间的数据（截至 2026-08-30，hold-out 起点 2026-08-17）" in zh
    assert "cannot be handed to AlphaKeel (G1 1.14)" in en


# --- review round 1 regressions (each failed on 804ce936) --------------------------------------------------------------------

@pytest.mark.parametrize("source,codes", [
    ("okx", ["BTC-USDT"]), ("auto", ["BTC-USDT"]), ("yfinance", ["BTC-USDT"]), ("yfinance", ["BTC-USD"]), ("yahoo", ["BTC-USD"]),
    ("local", ["local:BTC-USDT"]), ("tushare", ["AAPL.US", "ETH/USDT"]), ("auto", ["BTCUSDT"]),
])
def test_holdout_guard_keys_on_crypto_codes_not_only_the_source(source, codes, tmp_path) -> None:
    from src.tools import backtest_tool

    msg = backtest_tool._holdout_refusal({"source": source, "codes": codes, "end_date": "2026-08-30"}, SESSION)
    assert msg is not None and "reaches into the hold-out" in msg and "2026-08-16" in msg
    assert backtest_tool._holdout_refusal({"source": source, "codes": codes}, SESSION) is not None  # no end_date either
    assert backtest_tool._holdout_refusal({"source": source, "codes": codes, "end_date": "2026-08-10"}, SESSION) is None


def test_a_yfinance_crypto_config_is_refused_before_the_engine_runs(tmp_path, monkeypatch) -> None:
    out = _guarded_run(tmp_path, monkeypatch, source="yfinance", end_date="2026-08-30")  # codes ["BTC-USDT"]
    assert out["error_code"] == "holdout" and not out["_called"]


def test_equity_codes_on_yfinance_are_never_refused(tmp_path) -> None:
    from src.tools import backtest_tool

    for codes in (["AAPL.US"], ["AAPL", "MSFT.US"], ["000001.SZ"]):
        assert backtest_tool._holdout_refusal({"source": "yfinance", "codes": codes, "end_date": "2999-01-01"}, SESSION) is None


@pytest.mark.parametrize("bad", ["2026-08-10T00:00:00", "2026-08-10 00:00:00", "2026-08-10T00:00:00+08:00", "2026-08-10+08:00",
                                 "20260810", "2026/08/10", "2026-8-10", "2026-02-30", " 2026-08-10", 20260810])
def test_end_date_must_be_a_plain_iso_date(bad) -> None:
    from src.tools import backtest_tool

    msg = backtest_tool._holdout_refusal({"source": "okx", "codes": ["BTC-USDT"], "end_date": bad}, SESSION)
    assert msg is not None and "YYYY-MM-DD" in msg and "reaches into" not in msg


def test_card_warning_does_not_need_a_cached_cutoff_and_counts_only_crypto_exposure(tmp_path, monkeypatch) -> None:
    from src.tools import alphakeel_ir_request, alphakeel_research_ledger as L

    sdir = tmp_path / "sessions" / SESSION
    equity = _make_run(tmp_path, "eq", source="yfinance", codes=("AAPL.US",))
    L.record_engine_run(SESSION, equity)
    far = CUTOFF_DAY_MS + 30 * DAY_MS
    L.record_exposure(SESSION, "yfinance", far, "backtest:eq", crypto=False)  # an equity run read data past the crypto cutoff: not hold-out data
    assert not (sdir / L.HOLDOUT_FILE).exists()
    assert L.crypto_exposure_end_ms(SESSION) is None and "hold-out" not in L.render_card(SESSION, False)
    crypto = _make_run(tmp_path, "cr", source="yfinance", codes=("BTC-USD",), seed=7)
    L.record_engine_run(SESSION, crypto)
    L.record_exposure(SESSION, "yfinance", far, "backtest:cr", crypto=True)
    assert L.crypto_exposure_end_ms(SESSION) == far
    assert "⚠" in L.render_card(SESSION, False) and not (sdir / L.HOLDOUT_FILE).exists()  # the card makes no service call
    L.holdout_cutoff(SESSION)  # the backtest guard asks the service and caches the answer in the session
    assert "hold-out starts 2026-08-17" in L.render_card(SESSION, False)
    # a card never asks the service: with no cache and no memo the 60-day fallback applies
    L._FALLBACK_MEMO.clear()
    (sdir / L.HOLDOUT_FILE).unlink(missing_ok=True)
    asked = []
    monkeypatch.setattr(alphakeel_ir_request, "service_check", lambda: asked.append(1) or {"holdout_cutoff_ms": CUTOFF_MS})
    L.record_exposure(SESSION, "okx", 9_999_999_999_999, "backtest:other")
    assert "⚠" in L.render_card(SESSION, True) and L._day(L._fallback_cutoff_ms()) in L.render_card(SESSION, False)
    assert not asked and not (sdir / L.HOLDOUT_FILE).exists() and SESSION not in L._FALLBACK_MEMO


def test_engine_capital_base_is_initial_cash_with_the_engine_default(tmp_path) -> None:
    rd = _make_run(tmp_path, final_override=1_200_000.0)
    cfg = json.loads((rd / "config.json").read_text())
    cfg.pop("initial_cash")
    cfg["initial_capital"] = 500_000  # the engine ignores this key and starts from 1_000_000
    (rd / "config.json").write_text(json.dumps(cfg))
    screen = screen_engine_run(rd, trials=1)
    net = [i for i in screen["items"] if i["id"] == "1.3"][0]["observed"]
    assert net == "200000" and screen["facts"]["config_initial_capital_ignored"] is True
    assert screen["facts"]["capital_base"] == "1000000" and "initial_capital" in screen["warning"]
    cfg["initial_cash"] = 2_000_000
    (rd / "config.json").write_text(json.dumps(cfg))
    again = screen_engine_run(rd, trials=1)
    assert "config_initial_capital_ignored" not in again["facts"] and again["facts"]["capital_base"] == "2000000"
    assert [i for i in again["items"] if i["id"] == "1.3"][0]["observed"] == "-800000"


def test_trial_identity_is_the_engine_result_not_the_code_or_config(tmp_path) -> None:
    from src.tools import alphakeel_research_ledger as L

    code = "import json\nPARAMS = json.load(open('params.json'))  # parameters live outside code and config\n"
    keys = []
    for n, drift in enumerate((0.001, 0.002, 0.003)):
        rd = _make_run(tmp_path, code=code, drift=drift, seed=3)
        (rd / "params.json").write_text(json.dumps({"fast": 5 + n}))
        keys.append(L.record_engine_run(SESSION, rd))
    assert [k["trials"] for k in keys] == [1, 2, 3]
    rows = L.engine_rows(SESSION)
    assert len({r["trial_key"] for r in rows}) == 3 and len({r["input_key"] for r in rows}) == 1  # same code + config, 3 results
    rd = _make_run(tmp_path, code=code, drift=0.003, seed=3)  # an identical rerun
    assert L.record_engine_run(SESSION, rd)["trials"] == 3
    rows = L.engine_rows(SESSION)
    assert rows[-1]["trial_key"] == rows[2]["trial_key"] and len({r["trial_key"] for r in rows}) == 3


def test_a_stored_verdict_is_recomputed_with_the_sessions_current_trial_count(tmp_path) -> None:
    from src.tools import alphakeel_research_ledger as L

    first = L.record_engine_run(SESSION, _make_run(tmp_path, drift=0.0005, seed=1))
    assert first["trials"] == 1 and first["evidence_status"] == "passed_screen"
    assert "passed screen 1, failed screen 0" in L.render_card(SESSION, False)
    for n in range(30):
        L.record_engine_run(SESSION, _make_run(tmp_path, drift=0.0005, seed=100 + n))
    assert len({r["trial_key"] for r in L.engine_rows(SESSION)}) == 31
    assert "passed screen 0, failed screen 31" in L.render_card(SESSION, False)  # row 1 is no longer a pass
    row = L.engine_rows(SESSION)[0]
    now = L._current_screen(SESSION, row, L.verified_engine_screen(row))
    assert now["evidence_status"] == "failed_screen" and now["facts"]["trials"] == 31 and now["facts"]["dsr"] < 0.95
    assert {i["id"]: i["result"] for i in now["items"]}["1.6"] == "FAIL"
    # the frozen screen file (and so the run-time tool result) still says what it said then
    frozen = json.loads((Path(L.engine_rows(SESSION)[0]["snapshot_dir"]) / "screen.json").read_text())
    assert frozen["evidence_status"] == "passed_screen" and frozen["facts"]["trials"] == 1
    # a changed returns series is reported, not trusted
    (Path(L.engine_rows(SESSION)[0]["snapshot_dir"]) / "returns.json").write_text("[0.1]")
    assert L._current_screen(SESSION, row, L.verified_engine_screen(row)) is None
    assert "快照已改动或缺失" in L.render_card(SESSION, True)


def test_ir_trials_count_includes_the_engine_trials(tmp_path, monkeypatch) -> None:
    from src.hypotheses.registry import HypothesisRegistry
    from src.tools import alphakeel_research_ledger as L

    monkeypatch.setattr(L, "_registry", lambda: HypothesisRegistry(tmp_path / "hypotheses.json"))
    for n in range(10):
        L.record_engine_run(SESSION, _make_run(tmp_path, seed=n))
    count, evidence = L.trials_for(SESSION, tmp_path / "turn", "ir-strategy-1", ir_doc={"parameters": {}}, window=None)
    assert count == 11
    lines = [json.loads(ln) for ln in Path(evidence).read_text().splitlines()]
    engine = [ln for ln in lines if ln["strategy_id"].startswith("engine:")]
    assert len(lines) == 11 and len(engine) == 10 and all(ln["parameters"] == {} and ln["window"] is None and ln["run_dir"] for ln in engine)
    assert L.trial_count(SESSION) == 10  # no IR run recorded in the ledger yet; the run asking is added by trials_for itself


def test_coverage_item_1_16_and_the_not_evaluated_list(tmp_path) -> None:
    rd = _make_run(tmp_path, ohlcv=False)
    (rd / "config.json").write_text(json.dumps({"source": "yfinance", "codes": [], "start_date": "2024-01-01", "initial_cash": 1_000_000}))
    absent = screen_engine_run(rd, trials=1)  # no code to check and no run card verdict: not evaluated
    assert "1.16" in absent["not_evaluated"] and "1.16" not in _items(absent)
    assert absent["not_evaluated"] == ["1.4", "1.5", "1.7", "1.9", "1.10", "1.11", "1.12", "1.13", "1.14", "1.15", "1.16", "1.17", "1.18", "1.19"]
    assert not set(absent["not_evaluated"]) & set(_items(absent))
    rd = _make_run(tmp_path, "withcard")
    assert _items(screen_engine_run(rd, trials=1))["1.16"] == "PASS"  # evaluated from the ohlcv artifacts
    (rd / "run_card.json").write_text(json.dumps({"data_audit": {"coverage_partial": False}}))
    ok = screen_engine_run(rd, trials=1)
    assert _items(ok)["1.16"] == "PASS" and "1.16" not in ok["not_evaluated"]
    (rd / "run_card.json").write_text(json.dumps({"data_audit": {"coverage_partial": True}}))
    bad = screen_engine_run(rd, trials=1)
    assert _items(bad)["1.16"] == "FAIL" and bad["evidence_status"] == "failed_screen"
    (rd / "run_card.json").write_text(json.dumps({"data_audit": {"coverage_partial": "no"}}))
    assert _items(screen_engine_run(rd, trials=1))["1.16"] == "PASS"  # not a bool: the screen evaluates it itself


def test_coverage_1_16_is_evaluated_from_the_ohlcv_files_when_the_card_has_no_verdict(tmp_path) -> None:
    rd = _make_run(tmp_path, "cov", codes=("BTC-USDT", "ETH-USDT"), end_date="2024-07-18")  # 200 days: ends 2024-07-18
    ok = screen_engine_run(rd, trials=1)
    assert _items(ok)["1.16"] == "PASS" and "1.16" not in ok["not_evaluated"]
    eth = rd / "artifacts" / "ohlcv_ETH-USDT.csv"
    saved = eth.read_text()
    eth.unlink()  # a code with no ohlcv file
    missing = screen_engine_run(rd, trials=1)
    assert _items(missing)["1.16"] == "FAIL" and missing["evidence_status"] == "failed_screen" and "1.16" not in missing["not_evaluated"]
    eth.write_text("trade_date,open,high,low,close,volume\n")  # empty
    assert _items(screen_engine_run(rd, trials=1))["1.16"] == "FAIL"
    lines = saved.splitlines()
    eth.write_text("\n".join([lines[0], *lines[11:]]) + "\n")  # first bar 10 days after start_date
    late = screen_engine_run(rd, trials=1)
    assert _items(late)["1.16"] == "FAIL" and late["evidence_status"] == "failed_screen"
    eth.write_text("\n".join([lines[0], *lines[7:]]) + "\n")  # first bar 6 days after: within the 7-day tolerance
    assert _items(screen_engine_run(rd, trials=1))["1.16"] == "PASS"
    eth.write_text("\n".join(lines[:-12]) + "\n")  # last bar 12 days before end_date
    assert _items(screen_engine_run(rd, trials=1))["1.16"] == "FAIL"
    eth.write_text(saved)
    assert _items(screen_engine_run(rd, trials=1))["1.16"] == "PASS"


def test_money_figures_in_the_screen_are_decimal_strings(tmp_path) -> None:
    from src.tools import alphakeel_research_ledger as L

    rd = _make_run(tmp_path)
    (rd / "artifacts" / "metrics.csv").write_text(
        "final_value,trade_count,benchmark_return,excess_return,funding_paid\n1100000.5,40,0.1,0.2,99791.55084632568\n")
    screen = screen_engine_run(rd, trials=1)
    assert screen["facts"]["funding_paid"] == "99791.55084632568" and screen["facts"]["capital_base"] == "1000000"
    assert [i for i in screen["items"] if i["id"] == "1.3"][0]["observed"] == "100000.5"
    L.record_engine_run(SESSION, rd)
    stored = (Path(L.engine_rows(SESSION)[0]["snapshot_dir"]) / "screen.json").read_text()
    assert '"funding_paid": "99791.55084632568"' in stored


def test_card_shows_net_return_on_the_capital_base_and_the_g1_basis(tmp_path) -> None:
    from src.tools import alphakeel_research_ledger as L

    L.record_engine_run(SESSION, _make_run(tmp_path, final_override=1_250_000.0, trades=10))
    zh, en = L.render_card(SESSION, True), L.render_card(SESSION, False)
    assert "初筛口径：AlphaKeel G1" in zh and "Screen basis: AlphaKeel G1" in en
    assert "净收益率" in zh and "Net return" in en and "| 25.00% |" in zh
    assert "1.4" not in L._ids_text([]) and L._ids_text(["1.4", "1.5", "1.7", "1.9", "1.10", "1.11"]) == "1.4, 1.5, 1.7, 1.9–1.11"
    passed = L.record_engine_run("sess-pass", _make_run(tmp_path, "p", drift=0.0005, seed=1))
    assert passed["evidence_status"] == "passed_screen"
    assert "通过初筛，1.4, 1.5, 1.7, 1.9–1.15, 1.17–1.19 未评估" in L.render_card("sess-pass", True)


# --- review round 2: one crypto classifier, session-wide hold-out guard, no upgrade at render ------------------------------

@pytest.mark.parametrize("code,crypto", [
    ("BTC-USDC", True), ("BTC-EUR", True), ("ETH-JPY", True), ("BTC-IRT", True), ("BTC-TMN", True), ("BTCUSDT", True),
    ("btc-usdt", True), ("local:BTC-IRT", True), ("LOCAL: ETH-USD", True), ("EUR-USD", True), ("1000PEPE-USDT", True),
    ("BTC-USDT-SWAP", True), ("BTC-PERP", True), ("BTC/USDT:USDT", True), ("BTCUSD", True), ("XBTUSD", True),
    ("local:ETH-USDT-SWAP", True),
    ("AAPL.US", False), ("600000.SH", False), ("BRK-B", False), ("AAPL", False), ("local:MYDATA", False), ("BTC", False),
    ("ETH", False), ("EURUSD", False),
])
def test_is_crypto_code(code, crypto) -> None:
    from backtest.data_audit import is_crypto_code

    assert is_crypto_code(code) is crypto


def test_crypto_sources_are_the_static_set_plus_registered_crypto_only_loaders() -> None:
    from backtest.data_audit import STATIC_CRYPTO_SOURCES, crypto_sources

    got = crypto_sources()
    assert STATIC_CRYPTO_SOURCES <= got and {"nobitex", "wallex"} & got and not {"yfinance", "local", "auto", "tushare"} & got


@pytest.mark.parametrize("source,code", [
    ("yfinance", "BTC-USDC"), ("yfinance", "BTC-EUR"), ("yfinance", "ETH-JPY"), ("nobitex", "BTC-IRT"), ("wallex", "BTC-TMN"),
    ("yfinance", "local:BTC-IRT"),
])
def test_the_guard_refuses_every_crypto_code_and_crypto_only_source(source, code) -> None:
    from backtest.data_audit import crypto_sources
    from src.tools import backtest_tool

    if source in ("nobitex", "wallex") and source not in crypto_sources():
        pytest.skip(f"loader {source} is not registered as crypto-only in this build")
    msg = backtest_tool._holdout_refusal({"source": source, "codes": [code], "end_date": "2026-08-30"}, SESSION)
    assert msg is not None and "reaches into the hold-out" in msg


def test_equity_on_yfinance_is_never_refused_even_after_a_crypto_read() -> None:
    from src.tools import alphakeel_research_ledger as L, backtest_tool

    L.record_exposure(SESSION, "okx", CUTOFF_DAY_MS + 14 * DAY_MS, "x", crypto=True)
    for code in ("AAPL.US", "600000.SH", "BRK-B"):
        assert backtest_tool._holdout_refusal({"source": "yfinance", "codes": [code], "end_date": "2999-01-01"}, SESSION) is None


def test_a_renamed_local_file_is_refused_only_after_a_crypto_read_past_the_cutoff() -> None:
    from src.tools import alphakeel_research_ledger as L, backtest_tool

    def refusal(**kw):
        return backtest_tool._holdout_refusal({"source": "local", "codes": ["MYDATA"], "end_date": "2026-08-30", **kw}, SESSION)

    assert refusal() is None
    L.record_exposure(SESSION, "yfinance", CUTOFF_DAY_MS + 14 * DAY_MS, "backtest:eq", crypto=False)  # an equity read
    L.record_exposure(SESSION, "okx", CUTOFF_DAY_MS - 5 * DAY_MS, "backtest:old", crypto=True)  # crypto, before the cutoff
    assert refusal() is None
    L.record_exposure(SESSION, "okx", CUTOFF_DAY_MS + 14 * DAY_MS, "backtest:cr", crypto=True)
    assert "reaches into the hold-out" in refusal()
    assert "reaches into the hold-out" in refusal(source="yfinance", codes=["local:MYDATA"])
    assert refusal(end_date="2026-08-10") is None  # ending before the cutoff is fine
    assert "explicit end_date" in refusal(end_date=None)
    assert backtest_tool._holdout_refusal({"source": "local", "codes": ["MYDATA"], "end_date": "2026-08-30"}, "other-session") is None
    assert backtest_tool._holdout_refusal({"source": "yfinance", "codes": ["AAPL.US"], "end_date": "2026-08-30"}, SESSION) is None


def _variant(rd: Path, drift: float, vol: float, seed: int, days: int = 400) -> None:
    (rd / "code").mkdir(parents=True, exist_ok=True)
    (rd / "artifacts").mkdir(parents=True, exist_ok=True)
    (rd / "config.json").write_text(json.dumps({"source": "yfinance", "codes": ["AAPL"], "start_date": "2024-01-01",
                                                "end_date": "2025-03-01", "initial_cash": 1_000_000}))
    (rd / "code" / "signal_engine.py").write_text(f"# {seed}\n")
    rng, eq, rows, pos, bars = random.Random(seed), 1e6, ["timestamp,ret,equity"], ["timestamp,AAPL"], ["trade_date,open,high,low,close,volume"]
    for d in range(days):
        eq += 1e6 * (drift + rng.gauss(0, vol))
        ts = (datetime.date(2024, 1, 1) + datetime.timedelta(days=d)).isoformat()
        rows.append(f"{ts},0,{eq}")
        pos.append(f"{ts},0.5")
        bars.append(f"{ts},1,1,1,1,1")
    (rd / "artifacts" / "equity.csv").write_text("\n".join(rows) + "\n")
    (rd / "artifacts" / "positions.csv").write_text("\n".join(pos) + "\n")
    (rd / "artifacts" / "ohlcv_AAPL.csv").write_text("\n".join(bars) + "\n")
    (rd / "artifacts" / "metrics.csv").write_text(f"final_value,trade_count\n{eq},50\n")


def test_a_record_time_failure_is_never_upgraded_at_render(tmp_path) -> None:
    """Regression (review/upgrade.py): 150 near-zero-Sharpe variants shrank the cross-trial Sharpe variance, so the DSR of an
    earlier failed run rose above 0.95 on the card while its tool result said failed_screen."""
    from src.tools import alphakeel_research_ledger as L

    rd = tmp_path / "up"
    _variant(rd, 0.006, 0.01, 1)
    L.record_engine_run(SESSION, rd)  # a wide-Sharpe variant pair makes the trial variance large at record time
    _variant(rd, -0.004, 0.01, 2)
    L.record_engine_run(SESSION, rd)
    _variant(rd, 0.004, 0.01, 3)
    rec = L.record_engine_run(SESSION, rd)
    assert rec["evidence_status"] == "failed_screen" and {i["id"]: i["result"] for i in rec["items"]}["1.6"] == "FAIL"
    assert rec["inputs"]["var_sr_used"] > 0
    for k in range(150):
        _variant(rd, 0.00001 * (k % 7), 0.01, 100 + k)
        L.record_engine_run(SESSION, rd)
    row = L.engine_rows(SESSION)[2]
    now = L._current_screen(SESSION, row, L.verified_engine_screen(row))
    assert now["evidence_status"] == "failed_screen" and {i["id"]: i["result"] for i in now["items"]}["1.6"] == "FAIL"
    # the stored variance is a floor: the recomputed DSR is the one with the larger variance
    from backtest import alphakeel_metrics as am

    rets = L.verified_engine_returns(row)
    n = L.trial_count(SESSION)
    cur = am.dsr_inputs(rets, n, L.variant_sharpes_for(SESSION, row["trial_key"]))
    assert cur["var_sr_used"] < rec["inputs"]["var_sr_used"]  # the premise: the variance did shrink
    assert "| 3 |" not in "\n".join(ln for ln in L.render_card(SESSION, False).splitlines() if "passed screen;" in ln)


def test_rescreen_never_improves_a_stored_screen_and_may_worsen_it(tmp_path) -> None:
    from backtest.engine_screen import rescreen

    rd = _make_run(tmp_path, drift=0.0005, seed=1)
    stored = screen_engine_run(rd, trials=1, include_returns=True)
    returns = stored.pop("daily_returns")
    assert stored["evidence_status"] == "passed_screen"
    worse = rescreen(stored, returns, trials=50, variant_sharpes=[0.1, 0.3, 0.2])
    assert worse["evidence_status"] == "failed_screen" and worse["facts"]["trials"] == 50
    failing = {**worse, "inputs": worse.get("inputs") or stored["inputs"]}
    assert rescreen(failing, returns, trials=1) is failing  # a better recomputation keeps the stored failed screen
