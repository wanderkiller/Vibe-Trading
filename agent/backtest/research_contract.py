"""The research contract of a session: what its backtests actually measured (engine, direction, spot/perp, assets).

Pure functions, no I/O. The ledger (``src/tools/alphakeel_research_ledger.py``) keeps the contract per session and the
result card shows every change of it; a change is allowed (runs are never refused) but never silent.
"""

from __future__ import annotations

from typing import Iterable

_QUOTES = ("USDT", "USDC", "BUSD", "FDUSD", "USD")
_PERP_SUFFIXES = ("-PERP", "-SWAP")


def _is_crypto(code: str) -> bool:
    """``is_crypto_code`` on the code and on its part after the last ``:`` (it knows one venue prefix, not ``okx:perp:``)."""
    from backtest.data_audit import is_crypto_code

    text = str(code).strip()
    return is_crypto_code(text) or is_crypto_code(text.rsplit(":", 1)[-1])


def base_asset(code: str) -> str:
    """The base asset of a crypto code (``BTC-USDT``, ``BTC-USDT-PERP``, ``okx:perp:BTC-USDT-SWAP``, ``binance:spot:BTCUSDT``,
    ``ETH/USDT`` -> ``BTC`` / ``BTC`` / ``BTC`` / ``BTC`` / ``ETH``); any other code is returned upper-cased."""
    text = str(code).strip()
    if not _is_crypto(text):
        return text.upper()
    name = text.rsplit(":", 1)[-1].strip().upper()
    for suffix in _PERP_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    if "-" in name or "/" in name:
        return name.replace("/", "-").split("-", 1)[0]
    for quote in _QUOTES:
        if name.endswith(quote) and len(name) > len(quote):
            return name[: -len(quote)]
    return name


def _class_of(code: str) -> str:
    from backtest.engines._market_hooks import is_perp_code

    if is_perp_code(code) or ":PERP:" in str(code).upper():
        return "perp"
    return "spot" if _is_crypto(code) else "cash"


def _combine(classes: Iterable[str]) -> str:
    distinct = set(classes)
    if not distinct:
        return "unknown"
    return next(iter(distinct)) if len(distinct) == 1 else "mixed"


def instrument_class(codes: Iterable[str]) -> str:
    """``perp``, ``spot`` or ``cash`` when every code is of that class, ``mixed`` for several, ``unknown`` without codes."""
    return _combine(_class_of(c) for c in codes)


def contract_of_engine(facts: dict) -> dict:
    """The contract a generic engine run measured (``facts`` of its screen: ``direction``, ``codes``)."""
    codes = facts.get("codes") or []
    return {"engine": "generic", "direction": facts.get("direction") or "unknown",
            "instrument_class": instrument_class(codes), "assets": sorted({base_asset(c) for c in codes})}


def contract_of_ir(card: dict) -> dict:
    """The contract an IR run card measured (``instruments`` rows with ``market`` and ``base``)."""
    insts = [i for i in card.get("instruments") or [] if isinstance(i, dict)]
    classes = [str(i.get("market") or "").lower() for i in insts]
    return {"engine": "ir", "direction": "long_short",
            "instrument_class": _combine(c if c in ("perp", "spot") else "unknown" for c in classes) if classes else "unknown",
            "assets": sorted({str(i["base"]).upper() for i in insts if i.get("base")})}


def contract_diff(old: dict, new: dict) -> list[str]:
    """The fields that changed: ``engine``, ``direction``, ``instrument_class`` when different, ``assets`` when the two
    asset sets are disjoint (an overlap is the same research universe)."""
    fields = [f for f in ("engine", "direction", "instrument_class") if old.get(f) != new.get(f)]
    a, b = set(old.get("assets") or []), set(new.get("assets") or [])
    if not (a & b) and (a or b):
        fields.append("assets")
    return fields
