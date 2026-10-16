"""Code-made screen of one generic-engine ``backtest`` run against the AlphaKeel G1 items it can already answer.

``screen_engine_run`` reads the run directory's artifacts only (config.json, artifacts/metrics.csv, equity.csv,
positions.csv, optional run_card.json) and never raises: an unreadable artifact gives ``evidence_status`` ``unavailable``.
Thresholds are the ``validated`` items of ``alphakeel_research.precheck.load_protocol()`` (as ``alphakeel_research.screen``
reads them); 1.4, 1.5, 1.7, 1.9-1.14 are not evaluated and an engine result can never go live by itself.

``evidence_status``: ``insufficient`` (1.1 or 1.2 fails), ``failed_screen`` (1.3, 1.6 or 1.8 fails), ``passed_screen``.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Sequence

NOT_EVALUATED = ["1.4", "1.5", "1.7", "1.9", "1.10", "1.11", "1.12", "1.13", "1.14"]
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


def _compare(observed: Any, threshold: Any, op: str) -> str:
    o, t = _dec(observed), _dec(threshold)
    if o is None or t is None:
        return "FAIL"
    return "PASS" if (o >= t if op == "ge" else o > t) else "FAIL"


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
                      protocol: dict | None = None) -> dict:
    """Screen one engine run directory; see the module docstring. Never raises."""
    try:
        return _screen(Path(run_dir), trials=trials, variant_sharpes=variant_sharpes, protocol=protocol)
    except Exception as exc:  # noqa: BLE001 - a screen failure must never break the backtest tool
        return {"evidence_status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}


def _screen(run_dir: Path, *, trials: int, variant_sharpes: Sequence[float], protocol: dict | None) -> dict:
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
    initial = _num(config.get("initial_capital"))
    if initial is None:
        initial = _num(config.get("initial_cash"))
    if initial is None:
        initial = float(eq.iloc[0])
    window_days = (ts.max().normalize() - ts.min().normalize()).days + 1
    trade_count = mval("trade_count")
    closed_trades = None if trade_count is None else int(trade_count)
    final_value = mval("final_value")
    net = None if final_value is None else Decimal(str(final_value)) - Decimal(str(initial))

    returns = am.daily_returns(eq, capital_base=initial, initial_equity=initial)
    stats = am.alphakeel_metrics(returns, trials=trials, variant_sharpes=variant_sharpes)
    ci = am.stationary_bootstrap_mean_ci(returns, seed=0)
    ci_lower = None if ci is None else ci.get("ci_lower")

    protocol = protocol if protocol is not None else precheck.load_protocol()
    by_id = {i["id"]: i for i in protocol["stages"]["validated"]["items"]}
    spec = (("1.1", window_days, "min_days", "ge"), ("1.2", closed_trades, "min_trades", "ge"),
            ("1.3", None if net is None else str(net), "net_gt", "gt"), ("1.6", stats.get("dsr"), "min_dsr", "ge"),
            ("1.8", ci_lower, "lower_gt", "gt"))
    items = []
    for item_id, observed, key, op in spec:
        p = by_id[item_id]
        thr = p["threshold"][key]
        items.append({"id": item_id, "label": p["label"], "result": _compare(observed, thr, op), "observed": observed,
                      "threshold": {key: thr}})
    res = {i["id"]: i["result"] for i in items}
    if res["1.1"] == "FAIL" or res["1.2"] == "FAIL":
        status = "insufficient"
    elif any(res[k] == "FAIL" for k in ("1.3", "1.6", "1.8")):
        status = "failed_screen"
    else:
        status = "passed_screen"

    pos = positions.drop(columns=["timestamp"]).apply(pd.to_numeric, errors="coerce").fillna(0.0)
    audit = card.get("data_audit") if isinstance(card.get("data_audit"), dict) else {}
    codes = config.get("codes")
    facts: dict[str, Any] = {
        "codes": list(codes) if isinstance(codes, list) else [],
        "source": config.get("source"),
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
        facts["funding_paid"] = funding
    return {"evidence_status": status, "items": items, "facts": facts, "not_evaluated": list(NOT_EVALUATED), "next": NEXT}
