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
1. Report every variant backtested in this turn in one pipe table: name, window, trade_count, net_usdt, max_drawdown, evidence_status. Its figures are `observed` with that variant's `evidence_dir` as ref. Variants from earlier turns are listed in the system result card; do not restate their figures.
2. The first line states the fee basis (`assumptions.fees_source` and the rates). A fee rate is an input you or the tool chose: declare it `count`, never `observed`. Fees, funding and PnL amounts paid or earned (`fees_usdt`, `funding_usdt`, `price_pnl_usdt`, `net_usdt`) are `observed` with the variant's `evidence_dir` as ref, quoted as single values, not as formulas.
3. Report `evidence_status` verbatim. NEVER present an `insufficient` or `failed_screen` variant as a recommendation, and never describe a losing variant's drawdown as controlled: a loss is reported as a loss.
4. An IR parameter you set (a threshold, `exit.max_loss`, `max_hold_hours`) and a G1 item id (`1.2`) are `count`. Write a money parameter without a currency mark beside the number (`exit.max_loss = 100`), since a currency-marked count is checked as an observed price.
5. A figure for a setting you did not backtest (another fee tier, size or window) is `proposed` and named untested.
6. The system appends a result card built from the trial ledger after your answer; do not contradict it, and do not write a card, table or section of your own that calls itself system-generated."""


def routing_block(registry: Any) -> str:
    """:data:`ROUTING_BLOCK` when ``alphakeel_research`` is registered in *registry*, else ``""``. Never raises."""
    try:
        has = getattr(registry, "has", None)
        if callable(has):
            return ROUTING_BLOCK if has(TOOL) else ""
        return ROUTING_BLOCK if TOOL in getattr(registry, "_tools", {}) else ""
    except Exception:  # noqa: BLE001 - fail-safe omission, never raise
        return ""
