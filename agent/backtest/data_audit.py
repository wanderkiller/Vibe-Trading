"""Which data sources a run can be audited against.

A source is *auditable* when every row the run read can be tied to a content-addressed, versioned dataset that someone
else can re-read bit for bit: ``alphakeel_b2`` (frozen AlphaKeel packs, verified on read, tagged with the dataset
versions) and ``alphakeel_pack`` (an exported pack directory). A live pull straight from an exchange (through ccxt, or the direct ``okx`` / ``binance`` loaders) is
not: the exchange can revise, truncate or rate-limit history, nothing pins the response, and a re-run next week may see
different rows. Those results stay usable for exploration, but the run card says so instead of implying otherwise.

Loaders running in the same process as the run record per-source provenance here (``record_provenance``); the run card
reads it back (``data_audit``). Data exposure (``record_exposure``) also goes to the bound conversation's exposure file
(``src.tools.alphakeel_research_ledger``). Nothing in this module touches the network.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

#: source name -> why a run on it cannot be audited. ccxt crypto data is pulled live from an exchange.
NON_AUDITABLE_SOURCES: dict[str, str] = {
    "ccxt": (
        "live pull from an exchange through ccxt: the response is not pinned to a dataset version and cannot be "
        "re-read bit for bit; use source alphakeel_b2 for an auditable crypto run"
    ),
    "okx": (
        "live pull straight from the OKX public API: the response is not pinned to a dataset version and cannot be "
        "re-read bit for bit; use source alphakeel_b2 for an auditable crypto run"
    ),
    "binance": (
        "live pull straight from the Binance public API: the response is not pinned to a dataset version and cannot be "
        "re-read bit for bit; use source alphakeel_b2 for an auditable crypto run"
    ),
}
AUDITABLE_SOURCES: frozenset[str] = frozenset({"alphakeel_b2", "alphakeel_pack"})
#: sources that are crypto by construction (live exchange pulls and the AlphaKeel packs); ``crypto_sources`` adds every
#: registered loader declaring ``markets == {"crypto"}``.
STATIC_CRYPTO_SOURCES: frozenset[str] = frozenset({"okx", "binance", "ccxt", "alphakeel_b2", "alphakeel_pack"})
#: quote currencies that make ``BASE-QUOTE`` / ``BASE/QUOTE`` / ``BASE_QUOTE`` a crypto code (forex pairs such as EUR-USD
#: are treated as crypto on purpose: the hold-out guard is conservative).
CRYPTO_QUOTES: frozenset[str] = frozenset({"USDT", "USDC", "USD", "BUSD", "FDUSD", "TUSD", "DAI", "EUR", "GBP", "JPY", "KRW",
                                           "TRY", "BRL", "AUD", "IRT", "TMN", "BTC", "ETH", "BNB"})
_CRYPTO_PAIR_RE = re.compile(r"^([A-Z0-9]{2,15})[-/_]([A-Z]{3,5})$")
#: perpetual / swap names (OKX ``BTC-USDT-SWAP``, ``BTC-PERP``, ``BTC_PERP``): any base with such a suffix is crypto.
_DERIVATIVE_SUFFIX_RE = re.compile(r"^([A-Z0-9]{2,15}(?:[-/_][A-Z]{3,5})?)[-_](?:SWAP|PERP|PERPETUAL)$")
#: unseparated pairs of a major base (``BTCUSD``, ``XBTUSD``, ``ETHUSDT``); bare tickers stay unclassified (ETH is an
#: NYSE ticker too).
_CONCAT_MAJOR_RE = re.compile(r"^(BTC|XBT|ETH|SOL|BNB|XRP|DOGE|ADA|LTC|TRX|AVAX|LINK|DOT|TON)"
                              r"(USDT|USDC|USD|BUSD|FDUSD|EUR|JPY|KRW|TRY|BTC|ETH)$")
_MAX_ENTRIES_PER_SOURCE = 64
_PROVENANCE: dict[str, list[dict[str, Any]]] = {}
#: source -> latest end (ms, exclusive) of any data read from it in this process (AlphaKeel protocol 2.5, item 1.14: the
#: research horizon includes every read, also the ones that bypass the research service).
_EXPOSURE_END: dict[str, int] = {}


def is_crypto_code(code: str) -> bool:
    """The one crypto classifier (hold-out guard, exposure rows, result card). A leading ``local:`` (any case) is stripped;
    crypto when the runner's own ``_detect_market`` says so, or the upper-cased code (a ccxt ``:SETTLE`` suffix dropped) is
    ``BASE[-/_]QUOTE`` with a quote in ``CRYPTO_QUOTES``, a perpetual/swap name, an unseparated major pair or starts with
    ``XBT``. Bare tickers (``BTC``, ``ETH``) are not classified. Never raises."""
    if not isinstance(code, str):
        return False
    symbol = code.strip()
    if symbol[:6].lower() == "local:":
        symbol = symbol[6:].strip()
    try:
        from backtest.engines._market_hooks import _detect_market

        if _detect_market(symbol) == "crypto":
            return True
    except Exception:  # noqa: BLE001 - fall through to the pair pattern
        pass
    upper = symbol.upper()
    base, sep, settle = upper.rpartition(":")
    if sep and settle in CRYPTO_QUOTES:  # ccxt contract symbol ``BTC/USDT:USDT``
        upper = base
    if _DERIVATIVE_SUFFIX_RE.match(upper) or _CONCAT_MAJOR_RE.match(upper) or upper.startswith("XBT"):
        return True
    m = _CRYPTO_PAIR_RE.match(upper)
    return bool(m) and m.group(2) in CRYPTO_QUOTES


def crypto_sources() -> frozenset[str]:
    """``STATIC_CRYPTO_SOURCES`` plus every registered loader whose ``markets`` is exactly ``{"crypto"}``. Never raises:
    on any failure the static set is returned."""
    try:
        from backtest.loaders import registry

        registry._ensure_registered()
        found = {name for name, cls in dict(registry.LOADER_REGISTRY).items()
                 if getattr(cls, "markets", None) == {"crypto"}}
        return STATIC_CRYPTO_SOURCES | frozenset(found)
    except Exception:  # noqa: BLE001
        return STATIC_CRYPTO_SOURCES


def record_provenance(source: str, entry: dict[str, Any]) -> None:
    """Remember one read of ``source`` (e.g. pack id and dataset versions). Bounded; duplicates are dropped."""
    lst = _PROVENANCE.setdefault(source, [])
    if entry not in lst and len(lst) < _MAX_ENTRIES_PER_SOURCE:
        lst.append(entry)


def record_exposure(source: str, end_ms: int, via: str = "in-process", crypto: bool | None = None) -> None:
    """Remember that ``source`` returned data up to ``end_ms`` (exclusive; capped at now: a future end shows nothing yet).

    Also appended to the exposure file of the conversation bound in this context (``src.providers.session_context``; the
    agent loop binds it for a run), because ``ir_export`` runs in a CLI child of its own and sees none of this process's
    records. Without a bound session (a backtest child, a CLI) only this process remembers it. ``crypto`` (None = unknown)
    is written into the session row, always True for a source in ``crypto_sources()``. Never raises."""
    import time

    end = min(int(end_ms), int(time.time() * 1000))
    if end > _EXPOSURE_END.get(source, -1):
        _EXPOSURE_END[source] = end
    try:
        from src.providers.session_context import current_llm_session_id
    except ImportError:  # a process without the agent package (a standalone loader)
        return
    try:
        session_id = current_llm_session_id()
        if session_id:
            from src.tools.alphakeel_research_ledger import record_exposure as record_session_exposure

            if crypto is not None:
                crypto = bool(crypto) or source in crypto_sources()
            record_session_exposure(session_id, source, end, via, crypto=crypto)
    except Exception:  # noqa: BLE001 - provenance bookkeeping must not break a data read
        import logging

        logging.getLogger(__name__).warning("data exposure of %s not recorded for the session", source, exc_info=True)


def data_exposure_end_ms() -> int | None:
    """Latest end of any data read in this process (all sources), or None. Exported as the IR's
    ``provenance.data_exposure_end_ms`` (AlphaKeel G1 1.14 counts it in the research horizon)."""
    return max(_EXPOSURE_END.values(), default=None)


def reset_provenance() -> None:
    _PROVENANCE.clear()
    _EXPOSURE_END.clear()


def data_audit(sources: Iterable[str]) -> dict[str, Any]:
    """The run card's ``data_audit`` block for the sources a run used."""
    names = sorted({str(s) for s in sources if str(s).strip()})
    per_source: dict[str, dict[str, Any]] = {}
    for name in names:
        if name in NON_AUDITABLE_SOURCES:
            per_source[name] = {"auditable": False, "reason": NON_AUDITABLE_SOURCES[name]}
        elif name in AUDITABLE_SOURCES:
            per_source[name] = {"auditable": True, "reads": list(_PROVENANCE.get(name, []))}
        else:
            per_source[name] = {"auditable": None, "reason": "no audit policy is defined for this source"}
    non = [n for n, v in per_source.items() if v["auditable"] is False]
    return {
        "schema_version": 1,
        "auditable": bool(names) and all(v["auditable"] is True for v in per_source.values()),
        "non_auditable_sources": non,
        "sources": per_source,
        "data_exposure_end_ms": data_exposure_end_ms(),
    }


def non_auditable_warning(audit: dict[str, Any]) -> str | None:
    non = audit.get("non_auditable_sources") or []
    if not non:
        return None
    return "non-auditable data: " + "; ".join(f"{n} ({NON_AUDITABLE_SOURCES.get(n, 'not pinned')})" for n in non)
