"""Routing text for AlphaKeel IR backtests in the Task-Routing prompt section (research-truth B3).

As upstream ``src.strategy_discovery.guard``: the block is emitted only when ``alphakeel_research`` is registered, never
raises, and imports nothing heavy (the context builder imports it eagerly). Upstream's At-risk / Sub-optimal / Healthy
attribution layers are not copied: they read ``trades.csv`` / ``equity.csv`` / ``validation.json``, which an IR run does
not have.
"""

from __future__ import annotations

from typing import Any

TOOL = "alphakeel_research"

ROUTING_BLOCK: str = """**AlphaKeel IR backtest** — after `alphakeel_research(action="ir_backtest")`:
1. Report every variant tried this session in one pipe table: name, window, trade_count, net_usdt, max_drawdown, evidence_status. Its figures are `observed` with that variant's `evidence_dir` as ref.
2. The first line states the fee basis (`assumptions.fees_source` and the rates).
3. Report `evidence_status` verbatim. NEVER present an `insufficient` or `failed_screen` variant as a recommendation, and never describe a losing variant's drawdown as controlled: a loss is reported as a loss.
4. A figure for a setting you did not backtest (another fee tier, size or window) is `proposed` and named untested.
5. The system appends a result card built from the trial ledger after your answer; do not contradict it."""


def routing_block(registry: Any) -> str:
    """:data:`ROUTING_BLOCK` when ``alphakeel_research`` is registered in *registry*, else ``""``. Never raises."""
    try:
        has = getattr(registry, "has", None)
        if callable(has):
            return ROUTING_BLOCK if has(TOOL) else ""
        return ROUTING_BLOCK if TOOL in getattr(registry, "_tools", {}) else ""
    except Exception:  # noqa: BLE001 - fail-safe omission, never raise
        return ""
