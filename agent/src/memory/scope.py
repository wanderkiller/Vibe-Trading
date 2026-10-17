"""Asset scope of recalled memories.

Pure. An auto-recalled project memory about one instrument (a QQQ research note) must not ride into a conversation about
other instruments, where the model took it for something the user had done there. The scope of a message is the set of
upper-case ticker-like tokens it names; a project memory is in scope when it shares one of them, or names none.
"""

from __future__ import annotations

import re
from typing import Any

# ``re.ASCII``: "QQQ分批进场" must yield QQQ, so a CJK character is not a word character here.
_TOKEN = re.compile(r"\b[A-Z][A-Z0-9]{1,5}(?:[-/.][A-Z0-9]{2,5})?\b", re.ASCII)
_PART_SPLIT = re.compile(r"[-/.]")

#: Upper-case words that are not instruments.
_STOP = frozenset(
    "IR API OOS IS DSR PBO SPA CSCV USD USDT USDC CNY HKD ETF OHLC OHLCV CSV JSON AI LLM PNL APR APY MDD CAGR KPI UTC "
    "ID OK VS TP SL Q1 Q2 Q3 Q4 G1 G2 AK VT PERP SWAP".split()
)

#: How much of a memory body is read for its own tokens.
_BODY_CHARS = 2000


def asset_tokens(text: str) -> frozenset[str]:
    """Ticker-like tokens of ``text`` (``BTC-USDT`` gives ``BTC-USDT`` and ``BTC``; the quote currency is a stop word)."""
    found: set[str] = set()
    for match in _TOKEN.finditer(text or ""):
        token = match.group(0)
        for candidate in (token, *_PART_SPLIT.split(token)):
            if candidate and candidate not in _STOP:
                found.add(candidate)
    return frozenset(found)


def memory_in_scope(entry: Any, scope_tokens: frozenset[str]) -> bool:
    """Whether a recalled ``entry`` may be shown for a message whose asset tokens are ``scope_tokens``.

    Only ``project`` memories are scoped (a preference or a feedback note holds for every asset). A project memory with no
    asset token of its own is in scope.
    """
    if str(getattr(entry, "memory_type", "") or "").lower() != "project":
        return True
    keywords = " ".join(str(k) for k in (getattr(entry, "keywords", None) or ()))
    own = asset_tokens(f"{getattr(entry, 'title', '')} {keywords} {str(getattr(entry, 'body', ''))[:_BODY_CHARS]}")
    return not own or bool(own & scope_tokens)
