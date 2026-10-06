"""Which data sources a run can be audited against.

A source is *auditable* when every row the run read can be tied to a content-addressed, versioned dataset that someone
else can re-read bit for bit: ``alphakeel_b2`` (frozen AlphaKeel packs, verified on read, tagged with the dataset
versions) and ``alphakeel_pack`` (an exported pack directory). A live pull straight from an exchange through ccxt is
not: the exchange can revise, truncate or rate-limit history, nothing pins the response, and a re-run next week may see
different rows. Those results stay usable for exploration, but the run card says so instead of implying otherwise.

Loaders running in the same process as the run record per-source provenance here (``record_provenance``); the run card
reads it back (``data_audit``). Nothing in this module touches the network.
"""

from __future__ import annotations

from typing import Any, Iterable

#: source name -> why a run on it cannot be audited. ccxt crypto data is pulled live from an exchange.
NON_AUDITABLE_SOURCES: dict[str, str] = {
    "ccxt": (
        "live pull from an exchange through ccxt: the response is not pinned to a dataset version and cannot be "
        "re-read bit for bit; use source alphakeel_b2 for an auditable crypto run"
    ),
}
AUDITABLE_SOURCES: frozenset[str] = frozenset({"alphakeel_b2", "alphakeel_pack"})
_MAX_ENTRIES_PER_SOURCE = 64
_PROVENANCE: dict[str, list[dict[str, Any]]] = {}


def record_provenance(source: str, entry: dict[str, Any]) -> None:
    """Remember one read of ``source`` (e.g. pack id and dataset versions). Bounded; duplicates are dropped."""
    lst = _PROVENANCE.setdefault(source, [])
    if entry not in lst and len(lst) < _MAX_ENTRIES_PER_SOURCE:
        lst.append(entry)


def reset_provenance() -> None:
    _PROVENANCE.clear()


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
    }


def non_auditable_warning(audit: dict[str, Any]) -> str | None:
    non = audit.get("non_auditable_sources") or []
    if not non:
        return None
    return "non-auditable data: " + "; ".join(f"{n} ({NON_AUDITABLE_SOURCES.get(n, 'not pinned')})" for n in non)
