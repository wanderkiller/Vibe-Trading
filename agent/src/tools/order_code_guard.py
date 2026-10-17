"""Refuse order-placing code: this repository researches and backtests, it never places orders.

Pure. Live execution goes through the AlphaKeel hand-off (Strategy IR); code that places an order on an exchange
(ccxt ``create_order`` / ``createOrder``, REST order endpoints, ``place_order`` helpers) is not written here, even with a dry-run switch.
"""

from __future__ import annotations

import json
import re

# Case-insensitive, and not tied to a call parenthesis: a method name is also written as a string for ``getattr``.
# String concatenation ("/api/v3/" + "order") and the bash tool are not covered (best effort for file-writing tools).
_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\w*create\w*order",  # createOrder, create_order, futures_create_order, create_market_buy_order
        r"order_(?:market|limit)_\w+",
        r"\w*place_?order",  # place_order, placeOrder, trading_place_order
        r"\w*submit_?order",
        r"\w*private_?post\w*order",  # fapiPrivatePostOrder, private_post_order, privatePostTradeOrder
        r"/(?:api|fapi|dapi|sapi|papi)/v\d+/(?:\w+/)*order\b",
        r"/api/v5/trade/order\b",
        r"/v5/order/create\b",
        r"\btrading_place_order\b",
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
