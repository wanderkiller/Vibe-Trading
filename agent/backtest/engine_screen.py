"""Code-made screen of one generic-engine ``backtest`` run against the AlphaKeel G1 items it can already answer.

``screen_engine_run`` reads the run directory's artifacts only (config.json, artifacts/metrics.csv, equity.csv,
positions.csv, optional run_card.json) and never raises: an unreadable artifact gives ``evidence_status`` ``unavailable``.
Thresholds are the ``validated`` items of ``alphakeel_research.precheck.load_protocol()`` and the comparisons are
``precheck._ge`` / ``_gt`` (as ``alphakeel_research.screen`` reads them). Evaluated: 1.1, 1.2, 1.3, 1.6, 1.8 and, when the run card
carries ``data_audit.coverage_partial`` (a bool), 1.16. ``not_evaluated`` is every other protocol item; an engine result can
never go live by itself.

``evidence_status``: ``insufficient`` (1.1 or 1.2 fails), ``failed_screen`` (1.3, 1.6, 1.8 or 1.16 fails), ``passed_screen``.

The capital base is the engine's own: ``config["initial_cash"]``, default ``DEFAULT_INITIAL_CASH`` (``BaseEngine.__init__``).
``initial_capital`` is not read by the engine; a config that sets only it is flagged (``facts.config_initial_capital_ignored``).
Item 1.6 depends on the session's trial count, which grows: ``rescreen`` recomputes it from the stored daily returns.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Sequence

#: ``BaseEngine.__init__``: ``self.initial_capital = config.get("initial_cash", 1_000_000)``
DEFAULT_INITIAL_CASH = 1_000_000
NEXT = ("engine results cannot go live: hand over a Strategy IR, pass ir_robustness, "
        "then AlphaKeel G1 (validated) and G2 (paper)")


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _dec(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def engine_initial_cash(config: dict) -> Decimal | None:
    """The capital base the engine uses for ``config``: exactly ``config.get("initial_cash", 1_000_000)``; None when that is
    not a positive finite number (the engine config schema rejects it too)."""
    d = _dec(config.get("initial_cash", DEFAULT_INITIAL_CASH))
    return d if d is not None and d > 0 else None


def _status_of(res: dict) -> str:
    if res.get("1.1") == "FAIL" or res.get("1.2") == "FAIL":
        return "insufficient"
    if any(res.get(k) == "FAIL" for k in ("1.3", "1.6", "1.8", "1.16")):
        return "failed_screen"
    return "passed_screen"


def _id_key(item_id: str) -> tuple:
    return tuple(int(p) if p.isdigit() else 0 for p in item_id.split("."))


def _direction(pos: Any) -> str:
    vals = pos.to_numpy(dtype=float) if pos.shape[1] else []
    has_long = bool((vals > 0).any()) if len(vals) else False
    has_short = bool((vals < 0).any()) if len(vals) else False
    if has_long and has_short:
        return "long_short"
    if has_long:
        return "long_only"
    if has_short:
        return "short_only"
    return "flat"


def screen_engine_run(run_dir: Path, *, trials: int, variant_sharpes: Sequence[float] = (),
                      protocol: dict | None = None, include_returns: bool = False) -> dict:
    """Screen one engine run directory; see the module docstring. Never raises. ``include_returns`` adds the daily return
    series (``daily_returns``) the screen was computed from, for ``rescreen``."""
    try:
        return _screen(Path(run_dir), trials=trials, variant_sharpes=variant_sharpes, protocol=protocol,
                       include_returns=include_returns)
    except Exception as exc:  # noqa: BLE001 - a screen failure must never break the backtest tool
        return {"evidence_status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}


def rescreen(screen: dict, returns: Sequence[float], *, trials: int, variant_sharpes: Sequence[float] = ()) -> dict:
    """``screen`` (a stored ``screen_engine_run`` result) with item 1.6, ``facts.dsr`` / ``facts.trials`` and
    ``evidence_status`` recomputed for ``trials`` and ``variant_sharpes`` from the stored daily ``returns``. Never raises;
    a screen that cannot be recomputed becomes ``unavailable`` (never the stale verdict)."""
    try:
        from alphakeel_research import precheck
        from backtest import alphakeel_metrics as am

        items = [dict(i) for i in screen["items"]]
        k = next(n for n, i in enumerate(items) if i["id"] == "1.6")
        dsr = am.dsr(list(returns), int(trials), list(variant_sharpes))
        result, observed, _ = precheck._ge({"dsr": dsr}, ("dsr",), items[k]["threshold"]["min_dsr"], "num", observed_fmt=float)
        items[k] = {**items[k], "result": result, "observed": observed}
        facts = {**(screen.get("facts") or {}), "dsr": dsr, "trials": int(trials)}
        return {**screen, "items": items, "facts": facts, "evidence_status": _status_of({i["id"]: i["result"] for i in items})}
    except Exception as exc:  # noqa: BLE001
        return {**screen, "evidence_status": "unavailable", "error": f"rescreen failed: {type(exc).__name__}: {exc}"}


def _screen(run_dir: Path, *, trials: int, variant_sharpes: Sequence[float], protocol: dict | None, include_returns: bool) -> dict:
    import pandas as pd

    from alphakeel_research import precheck
    from backtest import alphakeel_metrics as am

    art = run_dir / "artifacts"
    try:
        config = _read_json(run_dir / "config.json")
        metrics = pd.read_csv(art / "metrics.csv")
        equity = pd.read_csv(art / "equity.csv")
        positions = pd.read_csv(art / "positions.csv")
    except (OSError, ValueError, pd.errors.PandasError) as exc:
        return {"evidence_status": "unavailable", "error": f"unreadable artifact: {type(exc).__name__}: {exc}"}
    if not isinstance(config, dict):
        return {"evidence_status": "unavailable", "error": "config.json is not an object"}
    if metrics.empty or equity.empty or "timestamp" not in equity or "equity" not in equity or "timestamp" not in positions:
        return {"evidence_status": "unavailable", "error": "metrics.csv/equity.csv/positions.csv missing rows or columns"}
    card: dict = {}
    try:
        card = _read_json(run_dir / "run_card.json")
        card = card if isinstance(card, dict) else {}
    except (OSError, ValueError):
        card = {}

    m = metrics.iloc[0]

    def mval(name: str) -> float | None:
        return _num(m[name]) if name in metrics.columns else None

    ts = pd.to_datetime(equity["timestamp"])
    eq = pd.Series(equity["equity"].to_numpy(dtype=float), index=pd.DatetimeIndex(ts))
    capital = engine_initial_cash(config)
    if capital is None:
        return {"evidence_status": "unavailable", "error": "config initial_cash is not a positive number"}
    initial = float(capital)
    capital_ignored = "initial_capital" in config and "initial_cash" not in config
    window_days = (ts.max().normalize() - ts.min().normalize()).days + 1
    trade_count = mval("trade_count")
    closed_trades = None if trade_count is None else int(trade_count)
    final_value = mval("final_value")
    net = None if final_value is None else Decimal(str(final_value)) - capital

    returns = am.daily_returns(eq, capital_base=initial, initial_equity=initial)
    stats = am.alphakeel_metrics(returns, trials=trials, variant_sharpes=variant_sharpes)
    ci = am.stationary_bootstrap_mean_ci(returns, seed=0)
    ci_lower = None if ci is None else ci.get("ci_lower")

    protocol = protocol if protocol is not None else precheck.load_protocol()
    by_id = {i["id"]: i for i in protocol["stages"]["validated"]["items"]}
    doc = {"days": window_days, "trades": closed_trades, "net": None if net is None else str(net), "dsr": stats.get("dsr"),
           "ci": ci_lower}
    audit = card.get("data_audit") if isinstance(card.get("data_audit"), dict) else {}

    def thr(item_id: str, key: str) -> str:
        return by_id[item_id]["threshold"][key]

    checks: list[tuple[str, str, Any, str]] = [  # (id, threshold key, observed, result)
        ("1.1", "min_days", *_pair(precheck._ge(doc, ("days",), thr("1.1", "min_days"), "int", observed_fmt=int))),
        ("1.2", "min_trades", *_pair(precheck._ge(doc, ("trades",), thr("1.2", "min_trades"), "int", observed_fmt=int))),
        ("1.3", "net_gt", *_pair(precheck._gt(doc, ("net",), thr("1.3", "net_gt"), "str"))),
        ("1.6", "min_dsr", *_pair(precheck._ge(doc, ("dsr",), thr("1.6", "min_dsr"), "num", observed_fmt=float))),
        ("1.8", "lower_gt", ci_lower, precheck._gt(doc, ("ci",), thr("1.8", "lower_gt"), "num")[0]),
    ]
    coverage = audit.get("coverage_partial")
    if isinstance(coverage, bool) and "1.16" in by_id:
        checks.append(("1.16", "coverage_partial", coverage,
                       "PASS" if str(coverage).lower() == thr("1.16", "coverage_partial") else "FAIL"))
    items = [{"id": i, "label": by_id[i]["label"], "result": result, "observed": observed, "threshold": {key: thr(i, key)}}
             for i, key, observed, result in checks]
    res = {i["id"]: i["result"] for i in items}
    status = _status_of(res)
    not_evaluated = sorted((i for i in by_id if i not in res), key=_id_key)

    pos = positions.drop(columns=["timestamp"]).apply(pd.to_numeric, errors="coerce").fillna(0.0)
    codes = config.get("codes")
    facts: dict[str, Any] = {
        "codes": list(codes) if isinstance(codes, list) else [],
        "source": config.get("source"),
        "capital_base": str(Decimal(str(config.get("initial_cash", DEFAULT_INITIAL_CASH)))),
        "direction": _direction(pos),
        "time_in_market": float((pos != 0).any(axis=1).mean()) if len(pos) else None,
        "avg_gross_exposure": float(pos.abs().sum(axis=1).mean()) if len(pos) else None,
        "sharpe_daily": stats.get("sharpe_daily"),
        "dsr": stats.get("dsr"),
        "psr_vs_zero": stats.get("psr_vs_zero"),
        "trials": int(trials),
        "benchmark_return": mval("benchmark_return"),
        "excess_return": mval("excess_return"),
        "data_auditable": audit.get("auditable"),
        "non_auditable_sources": list(audit.get("non_auditable_sources") or []),
    }
    funding = mval("funding_paid")
    if funding is not None:
        facts["funding_paid"] = str(Decimal(str(funding)))
    out = {"evidence_status": status, "items": items, "facts": facts, "not_evaluated": not_evaluated, "next": NEXT}
    if capital_ignored:
        facts["config_initial_capital_ignored"] = True
        out["warning"] = (f"config has initial_capital but no initial_cash: the engine ignores initial_capital and starts from "
                          f"{DEFAULT_INITIAL_CASH}; returns and net are computed against that base")
    if include_returns:
        out["daily_returns"] = [float(r) for r in returns]
    return out


def _pair(r: tuple) -> tuple[Any, str]:
    """``precheck`` ``(result, observed, note)`` as ``(observed, result)``."""
    return r[1], r[0]
