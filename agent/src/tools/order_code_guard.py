"""Refuse order-placing code: this repository researches and backtests, it never places orders.

Pure. Live execution goes through the AlphaKeel hand-off (Strategy IR); code that places an order on an exchange
(ccxt ``create_order``, REST order endpoints, ``place_order`` helpers) is not written here, even with a dry-run switch.
"""

from __future__ import annotations

import json
import re

_PATTERNS = tuple(
    re.compile(p)
    for p in (
        r"\.create_order\s*\(",
        r"\bcreate_(?:market|limit|stop)_(?:buy_|sell_)?order\s*\(",
        r"\bplace_order\s*\(",
        r"\bprivatePost\w*Order\w*\s*\(",
        r"/api/v3/order\b",
        r"/fapi/v1/order\b",
        r"/api/v5/trade/order\b",
        r"/v5/order/create\b",
        r"\btrading_place_order\b",
        r"\bsubmit_order\s*\(",
    )
)

REFUSAL = (
    "Vibe-Trading research code does not place orders; live execution goes through the AlphaKeel hand-off "
    "(Strategy IR). Write the research/backtest part only."
)


def order_code_refusal(text: str) -> str | None:
    """The refusal message when ``text`` contains order-placing code, else None."""
    if text and any(p.search(text) for p in _PATTERNS):
        return REFUSAL
    return None


def refusal_json(message: str) -> str:
    """The tool-result JSON of a refused write."""
    return json.dumps({"status": "error", "error_code": "order_code_refused", "error": message}, ensure_ascii=False)
