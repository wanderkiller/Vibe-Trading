"""Refuse order-placing code: this repository researches and backtests, it never places orders.

Pure. Live execution goes through the AlphaKeel hand-off (Strategy IR); code that places an order on an exchange
(ccxt ``create_order`` / ``createOrder``, REST order endpoints, ``place_order`` helpers) is not written here, even with a dry-run switch.
"""

from __future__ import annotations

import json
import re

# Case-insensitive, and not tied to a call parenthesis: a method name is also written as a string for ``getattr``.
# String concatenation ("/api/v3/" + "order") and the bash tool are not covered (best effort for file-writing tools).
# Research identifiers that merely contain "order" (order book, order flow, order ids, sort order) are removed first.
_RESEARCH_IDENTIFIER = re.compile(
    r"\w*(?:orderbook|order_book|order_flow|orderflow|order_ids|sort_order)\w*", re.IGNORECASE
)
_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\w*create\w*orders?(?:ws)?\b",  # createOrder, create_order, futures_create_order, create_market_buy_order
        r"\border_(?:market|limit)_(?:buy|sell)\w*",
        r"(?<![a-z])place_?orders?\b",  # place_order, placeOrder, trading_place_order (not "replace_order")
        r"(?<![a-z])submit_?orders?\b",
        r"private_?post\w*order",  # fapiPrivatePostOrder, private_post_order, privatePostTradeOrder
        r"\b(?:new_?order|edit_?order|batch_?orders?)\w*\s*\(",  # new_order(...), newOrder(...), editOrder(id), batch_orders(...)
        r"[\"'](?:new_?order|edit_?order|batch_?orders?)\w*[\"']",  # getattr(client, "new_order")
        r"/(?:api|fapi|dapi|sapi|papi)/v\d+/(?:\w+/)*order\b",
        r"/api/v5/trade/order\b",
        r"/v5/order/create\b",
        r"\btrading_place_order\b",
    )
) + (
    # camelCase after a lower-case letter: fooPlaceOrder, fooSubmitOrder
    re.compile(r"[a-z0-9](?:Place|Submit)_?Orders?\b"),
)

REFUSAL = (
    "Vibe-Trading research code does not place orders; live execution goes through the AlphaKeel hand-off "
    "(Strategy IR). Write the research/backtest part only."
)


def order_code_count(text: str) -> int:
    """How many order-placing spots ``text`` contains (every pattern's matches, research identifiers aside)."""
    if not text:
        return 0
    text = _RESEARCH_IDENTIFIER.sub(" ", text)
    return sum(len(p.findall(text)) for p in _PATTERNS)


def order_code_refusal(text: str) -> str | None:
    """The refusal message when ``text`` contains order-placing code, else None."""
    return REFUSAL if order_code_count(text) else None


def refusal_json(message: str) -> str:
    """The tool-result JSON of a refused write."""
    return json.dumps({"status": "error", "error_code": "order_code_refused", "error": message}, ensure_ascii=False)
