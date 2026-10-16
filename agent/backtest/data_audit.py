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
#: every active ISO-4217 alphabetic code (incl. the metals XAU XAG XPT XPD and the IMF unit XDR): a pair of two of them is
#: forex / a metal quote, not crypto.
ISO_4217: frozenset[str] = frozenset("""
AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BOV BRL BSD BTN BWP BYN BZD CAD CDF CHE CHF
CHW CLF CLP CNY COP COU CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL
HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD
MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF
SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD USN UYI UYU
UYW UZS VED VES VND VUV WST XAF XAG XAU XCD XDR XOF XPD XPF XPT YER ZAR ZMW ZWG
""".split())
#: ISO-4217 plus the unofficial Iranian quotes exchanges list (rial IRR is ISO; toman/IRT are not).
FIAT_LIKE: frozenset[str] = ISO_4217 | {"IRT", "TMN"}
STABLE_QUOTES: frozenset[str] = frozenset({"USDT", "USDC", "BUSD", "FDUSD", "TUSD", "DAI", "USDP", "PYUSD", "USDE", "USDS",
                                           "EURC", "USD1"})
CRYPTO_QUOTE_ASSETS: frozenset[str] = frozenset({"BTC", "ETH", "BNB", "SOL"})
#: back-compat name: every quote that makes a pair with a crypto-looking base a crypto code.
CRYPTO_QUOTES: frozenset[str] = FIAT_LIKE | STABLE_QUOTES | CRYPTO_QUOTE_ASSETS
CRYPTO_BASES: frozenset[str] = frozenset("""
BTC XBT ETH SOL BNB XRP DOGE ADA TRX TON AVAX LINK DOT LTC BCH SHIB PEPE 1000PEPE WIF BONK UNI ATOM NEAR APT ARB OP SUI SEI
INJ TIA FIL ETC XLM HBAR ICP AAVE MKR LDO CRV RNDR RENDER FET TAO WLD JUP PYTH STX IMX GRT ALGO VET EOS XMR ZEC DASH KAS
ONDO ENA HYPE TRUMP USDT USDC
""".split())
#: BTC/ETH spot and futures funds track the coin ~1:1, so their bars count as crypto data for the hold-out; bare "BTC" is
#: the Grayscale Bitcoin Mini Trust on yfinance; bare ETH is deliberately absent (NYSE: Ethan Allen).
CRYPTO_PROXY_TICKERS: frozenset[str] = frozenset({
    "BTC", "IBIT", "GBTC", "FBTC", "ARKB", "BITB", "HODL", "BTCO", "BRRR", "EZBC", "BTCW", "ETHA", "ETHE", "FETH", "ETHW",
    "ETHV", "CETH", "EZET", "QETH", "BITO", "BTF", "BITX", "BITU", "ETHU", "BITCOIN", "ETHEREUM", "XBT"})
_FOREX_RE = re.compile(r"^([A-Z]{3})[-/_]?([A-Z]{3})$")
_CRYPTO_PAIR_RE = re.compile(r"^([A-Z0-9]{2,15})[-/_]([A-Z0-9]{2,6})$")
#: perpetual / swap names (OKX ``BTC-USDT-SWAP``, ``BTC-PERP``, ``BTC_PERP``): any base with such a suffix is crypto.
_DERIVATIVE_SUFFIX_RE = re.compile(r"^([A-Z0-9]{2,15}(?:[-/_][A-Z]{3,5})?)[-_](?:SWAP|PERP|PERPETUAL)$")
#: dated crypto futures (OKX ``BTC-USDT-251226``, Deribit ``BTC-26DEC25``, Binance COIN-M ``BTCUSD_251226``).
_DATED_FUTURE_RE = re.compile(r"^([A-Z0-9]{2,15}(?:[-/_][A-Z]{3,5})?)[-_](?:\d{6}|\d{1,2}[A-Z]{3}\d{2})$")
#: CME crypto futures roots as Yahoo continuous contracts (``BTC=F``, ``MBT=F``, ``ETH=F``, ``MET=F``): ~1:1 with the coin.
CME_CRYPTO_FUTURE_ROOTS: frozenset[str] = frozenset({"BTC", "MBT", "ETH", "MET", "BFF", "XRP", "MXR", "SOL", "MSL", "SOLM"})
#: an unseparated pair ending in a USD stablecoin, or in USD with a non-ISO base of 3+ letters (6+ characters: longer than
#: any US equity ticker), is a crypto spot/CFD name (``MATICUSD``, ``POLUSDT``, ``AXSUSD``).
_USD_CONCAT_RE = re.compile(r"^([A-Z0-9]{2,15})(USDT|USDC)$|^([A-Z]{3,10})USD$")
_BROKER_SUFFIX_RE = re.compile(r"^(.*)\.[A-Z0-9]{1,3}$")
_MT5_SUFFIX_RE = re.compile(r"^([A-Z0-9]+)[a-z]$")
_MAX_ENTRIES_PER_SOURCE = 64
_PROVENANCE: dict[str, list[dict[str, Any]]] = {}
#: source -> latest end (ms, exclusive) of any data read from it in this process (AlphaKeel protocol 2.5, item 1.14: the
#: research horizon includes every read, also the ones that bypass the research service).
_EXPOSURE_END: dict[str, int] = {}


def _is_concat_pair(upper: str) -> bool:
    """``BASE+QUOTE`` without a separator for a base in ``CRYPTO_BASES`` and a quote in ``CRYPTO_QUOTES`` other than the base."""
    for i in range(1, len(upper)):
        base, quote = upper[:i], upper[i:]
        if base in CRYPTO_BASES and quote in CRYPTO_QUOTES and quote != base:
            return True
    return False


_VENUE_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,15}:([^:]+)$")


def is_crypto_code(code: str) -> bool:
    """The one crypto classifier (hold-out guard, exposure rows, result card). Policy: conservative -- a code that may be
    crypto data (or a ~1:1 proxy of it) is crypto, a plain forex / metal pair or an equity is not. Never raises.

    Normalisation, in order: non-str is False; strip; drop a leading ``local:`` (any case); drop a
    venue prefix (``BINANCE:``, ``X:``); drop a trailing ``=X``; drop
    one trailing lower-case letter after ``[A-Z0-9]+`` (MT5 ``BTCUSDm``); drop a ccxt ``:SETTLE`` suffix (SETTLE a stable or
    fiat quote); upper-case; drop a ``.XX`` broker suffix only when the rest is an unseparated crypto pair (so ``AAPL.US``
    and ``600000.SH`` keep theirs and stay non-crypto). Rules on the result, first match wins:

    1. two ISO-4217 codes (``EUR-USD``, ``EURUSD``, ``GBP/USD``, ``XAU-USD``) -> not crypto;
    2. the runner's ``_detect_market`` says crypto -> crypto;
    0. a Yahoo CME crypto future (``BTC=F``, ``MBT=F``, ``ETH=F``) -> crypto;
    3. a perpetual / swap suffix or a dated-future expiry (``-251226``, ``-26DEC25``) -> crypto;
    3b. ``*USDT`` / ``*USDC``, or ``XXX+USD`` with a non-ISO base of 3+ letters (``MATICUSD``) -> crypto;
    4. a ``CRYPTO_PROXY_TICKERS`` member (``IBIT``, ``GBTC``, bare ``BTC``) or a name starting with ``XBT`` -> crypto;
    5. ``BASE[-/_]QUOTE``: base in ``CRYPTO_BASES``, or quote in fiat / stable / crypto quotes with a non-ISO base -> crypto;
    6. ``BASEQUOTE`` for a base in ``CRYPTO_BASES`` and a quote in ``CRYPTO_QUOTES`` (``BTCCAD``, ``XBTUSD``) -> crypto;
    7. otherwise not crypto (bare ``ETH`` is the NYSE ticker Ethan Allen)."""
    if not isinstance(code, str):
        return False
    try:
        symbol = code.strip()
        if symbol[:6].lower() == "local:":
            symbol = symbol[6:].strip()
        m = _VENUE_PREFIX_RE.match(symbol)  # TradingView / Polygon venue prefix ``BINANCE:BTCUSDT``, ``X:BTCUSD``
        if m:
            symbol = m.group(1).strip()
        if symbol.upper().endswith("=F") and symbol.upper()[:-2] in CME_CRYPTO_FUTURE_ROOTS:
            return True
        if symbol.upper().endswith("=X"):
            symbol = symbol[:-2]
        m = _MT5_SUFFIX_RE.match(symbol)
        if m:
            symbol = m.group(1)
        base, sep, settle = symbol.rpartition(":")
        if sep and settle.upper() in STABLE_QUOTES | FIAT_LIKE:  # ccxt contract symbol ``BTC/USDT:USDT``
            symbol = base
        upper = symbol.upper()
        m = _BROKER_SUFFIX_RE.match(upper)
        if m and _is_concat_pair(m.group(1)):
            upper = m.group(1)
        m = _FOREX_RE.match(upper)
        if m and m.group(1) in ISO_4217 and m.group(2) in ISO_4217:
            return False
        try:
            from backtest.engines._market_hooks import _detect_market

            if _detect_market(upper) == "crypto":
                return True
        except Exception:  # noqa: BLE001 - fall through to the patterns
            pass
        if _DERIVATIVE_SUFFIX_RE.match(upper) or _DATED_FUTURE_RE.match(upper):
            return True
        m = _USD_CONCAT_RE.match(upper)
        if m and (m.group(1) or m.group(3) not in ISO_4217):
            return True
        if upper in CRYPTO_PROXY_TICKERS or upper.startswith("XBT"):
            return True
        m = _CRYPTO_PAIR_RE.match(upper)
        if m:
            pair_base, quote = m.groups()
            if pair_base in CRYPTO_BASES or (quote in CRYPTO_QUOTES and pair_base not in ISO_4217):
                return True
        return _is_concat_pair(upper)
    except Exception:  # noqa: BLE001 - a classifier never raises
        return False


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
