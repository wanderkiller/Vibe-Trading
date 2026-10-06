"""Write the AlphaKeel hand-off package (``params.json`` + ``evidence/handoff-audit.json``) from a finished research run.

AlphaKeel ``docs/32-vibe-trading-handoff.md`` is the source of truth for the format. What this module enforces, so a
hand-off cannot quietly claim more than the research supports (review B-P1-2, D-P2-8, R2's required fields):

* **Auditable data or "exploratory"**: the run card's ``data_audit.auditable`` must be ``True`` (every source versioned
  and verified — ``alphakeel_b2``/``alphakeel_pack``). Otherwise the package is classified ``exploratory``: the
  classification and the non-auditable sources are written into ``source.data_audit`` and the evidence file, and the
  summary starts with ``EXPLORATORY``. ccxt/okx/binance direct loads are never auditable.
* **Research window** (``source.window``, required): every day of data the research used. When the research read data
  through AlphaKeel's research service, the credential's data horizon (``/whoami`` ``data_horizon``) is the service's own
  record of what it handed out: a window ending before it under-states the data seen and is refused.
* **Research credential** (``source.research_credential``): written when the research used the research service. AlphaKeel
  checks it against the service's data-horizon ledger when it accepts the experiment (experiment start at or after the
  credential's ``review_start_min_ms``, window covering the data handed out). Without it the window is self-reported:
  AlphaKeel runs the comparison but will not name a winner, and the evidence file says so.
* **Trials** (``trials {count, evidence}``, required): how many parameter sets were tried for this result and the file in
  the package that shows it (it must exist). AlphaKeel adds ``count`` to its global trial ledger (DSR's N).
* **AlphaKeel-convention statistics** next to Vibe-Trading's own: UTC-daily returns over an explicit capital base,
  Sharpe/Sortino/PSR/DSR with ``trials.count`` (``backtest.alphakeel_metrics``).
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from .errors import ApiError

SCHEMA = "alphakeel.handoff.params/1"
#: The only rule keys AlphaKeel accepts (docs/32 §3 / the hand-off skill's table).
RULE_KEYS = ("min_net", "max_payback_hours", "horizon_hours", "min_volume", "max_spread", "candidate_limit",
             "notional_per_leg", "max_open", "max_loss", "take_profit", "max_hold_hours", "cooldown_minutes")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID = re.compile(r"^[A-Za-z0-9._-]{3,64}$")


def _bad(msg: str) -> ApiError:
    return ApiError("request.invalid", msg)


def _day_end_ms(day: str) -> int:
    """Exclusive end of a UTC day ``YYYY-MM-DD`` in ms (AlphaKeel reads an end date as the next day's 00:00)."""
    d = dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc) + dt.timedelta(days=1)
    return int(d.timestamp() * 1000)


def classify(run_card: dict) -> dict:
    """``{"auditable": bool, "classification": "auditable"|"exploratory", "sources", "non_auditable_sources", "reasons"}``."""
    audit = run_card.get("data_audit")
    if not isinstance(audit, dict) or not isinstance(audit.get("auditable"), bool):
        raise _bad("the run card has no data_audit.auditable: the data's provenance is unknown")
    ok = audit["auditable"] is True
    non = list(audit.get("non_auditable_sources") or [])
    reasons = [] if ok else [f"non-auditable data source(s): {', '.join(non) or 'unknown'} (no dataset version, not verified by digest)"]
    return {"auditable": ok, "classification": "auditable" if ok else "exploratory",
            "sources": sorted((audit.get("sources") or {}).keys()), "non_auditable_sources": non, "reasons": reasons}


def write_handoff(out_dir: str | Path, *, handoff_id: str, summary: str, rules: dict, run_card: dict,
                  window: dict, trials_count: int, trials_evidence: str, run_id: str | None = None,
                  daily_returns: list[float] | None = None, research_credential: dict | None = None) -> dict:
    """Validate and write ``params.json`` and ``evidence/handoff-audit.json`` into ``out_dir``; returns ``params``.

    ``daily_returns``: UTC-daily returns over the capital base (``backtest.alphakeel_metrics.daily_returns``); they give
    the AlphaKeel-convention statistics in the evidence file. ``research_credential``: ``Client.check()``/``whoami()``
    output when the research used AlphaKeel's research service (its ``data_horizon`` bounds the window).
    """
    out = Path(out_dir)
    if not _ID.match(handoff_id or ""):
        raise _bad("handoff id must be 3–64 letters, digits, dot, dash or underscore")
    if not isinstance(rules, dict) or not rules:
        raise _bad("rules must be a non-empty object")
    for k, v in rules.items():
        if k not in RULE_KEYS:
            raise _bad(f"{k!r} is not an AlphaKeel rule (a factor must be ported into the screener first)")
        if not isinstance(v, dict) or "value" not in v:
            raise _bad(f"rule {k!r} must be {{\"value\": ..., \"robust\": [...]}}")
    start, end = window.get("start"), window.get("end")
    if not (isinstance(start, str) and isinstance(end, str) and _DAY.match(start) and _DAY.match(end) and start <= end):
        raise _bad("window must be {start, end} as YYYY-MM-DD (UTC days, end inclusive) with start <= end")
    if isinstance(trials_count, bool) or not isinstance(trials_count, int) or trials_count < 1:
        raise _bad("trials_count must be an integer >= 1: every parameter set tried for this result counts")
    ev = Path(trials_evidence)
    if ev.is_absolute() or ".." in ev.parts or not (out / ev).is_file():
        raise _bad(f"trials evidence {trials_evidence!r} must be an existing file inside the package")
    if research_credential is not None:
        cred = research_credential.get("credential")
        if not (isinstance(cred, str) and cred and cred == cred.strip()):
            raise _bad("research_credential must be the Client.check()/whoami() output with its credential id")
    horizon = (research_credential or {}).get("data_horizon")
    if horizon is not None and _day_end_ms(end) < int(horizon["max_window_end_ms"]):
        raise _bad(f"the research window ends {end} but this credential received data up to {horizon['max_window_end_ms']} ms "
                   "(research service data horizon): the window must cover every day of data the research used")
    audit = classify(run_card)
    if research_credential is None:
        audit["reasons"].append("no research-service credential: AlphaKeel cannot check the research window against its "
                                "data-horizon ledger, so the comparison will not name a winner")
    if not audit["auditable"] and not summary.startswith("EXPLORATORY"):
        summary = "EXPLORATORY (non-auditable data): " + summary
    from backtest.alphakeel_metrics import alphakeel_metrics

    ak = alphakeel_metrics(daily_returns, trials=trials_count) if daily_returns is not None else None
    source: dict[str, Any] = {"tool": "vibe-trading", "window": {"start": start, "end": end},
                              "data_audit": {k: audit[k] for k in ("auditable", "classification", "sources", "non_auditable_sources")}}
    if run_id:
        source["run_id"] = run_id
    if research_credential is not None:
        source["research_credential"] = research_credential.get("credential")
    params = {"schema": SCHEMA, "id": handoff_id, "strategy": "cross_venue_carry", "summary": summary, "source": source,
              "trials": {"count": trials_count, "evidence": ev.as_posix()}, "rules": rules}
    evidence = {"schema": "vibe-trading.handoff-audit/1", "handoff_id": handoff_id, "classification": audit["classification"],
                "reasons": audit["reasons"], "data_audit": run_card["data_audit"], "window": source["window"],
                "trials": params["trials"], "alphakeel_metrics": ak,
                "vibe_trading_metrics": run_card.get("metrics"), "research_credential": research_credential and {
                    "credential": research_credential.get("credential"), "holdout_days": research_credential.get("holdout_days"),
                    "data_horizon": horizon}}
    (out / "evidence").mkdir(parents=True, exist_ok=True)
    (out / "params.json").write_text(json.dumps(params, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "evidence" / "handoff-audit.json").write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return params
