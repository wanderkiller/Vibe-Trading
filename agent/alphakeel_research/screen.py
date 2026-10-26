"""Code-made screen of one ``ir backtest`` run card against AlphaKeel's G1 items it can already answer (research-truth B3).

The label is decided here, never by the model (as upstream ``strategy_discovery`` labels its evidence): G1 1.1, 1.2, 1.3
and 1.16 read straight off the run card; their thresholds are the ``validated`` items of ``precheck.load_protocol()`` and
the comparison is ``precheck``'s (``Decimal``, a missing or mistyped field is a FAIL). 1.4-1.13 need the robustness
battery (``ir robustness``) and are listed as not evaluated: ``passed_screen`` is never a pass of G1.

1.2 follows the protocol copy's threshold key: ``min_entries`` (protocol 3.0) reads ``counts.entries_distinct``, and a card
written before it falls back to ``counts.closed`` with the item's ``basis`` = ``precheck.LEGACY_ENTRIES_BASIS`` (旧口径);
``min_trades`` (<= 2.8) reads ``counts.closed``.

``evidence_status``: ``insufficient`` (1.1 or 1.2 fails: window too short or too few entries, 0 included),
``failed_screen`` (1.1 and 1.2 pass, 1.3 or 1.16 fails; 1.3 is ``N/A`` when no position was ever opened: neither FAIL nor PASS), ``passed_screen`` (all four pass; 1.4-1.13 not evaluated).
"""

from __future__ import annotations

from typing import Any

from . import precheck

#: (G1 item, run card path, threshold key, precheck JSON kind)
SCREEN_ITEMS = (("1.1", ("metrics", "days"), "min_days", "int"),
                ("1.2", ("counts", "closed"), "min_trades", "int"),  # protocol 3.0 copies: min_entries, see _entries
                ("1.3", ("amounts", "equity_end"), "net_gt", "str"),
                ("1.16", ("data_audit", "coverage_partial"), "coverage_partial", None))
NOT_EVALUATED = tuple(f"1.{i}" for i in range(4, 14))
PASSED_SCREEN_TEXT = "passed screen; 1.4-1.13 not evaluated"


def _coverage(card: dict, threshold: str) -> tuple[str, Any]:
    v = precheck._get(card, "data_audit", "coverage_partial")
    if not isinstance(v, bool):
        return "FAIL", None
    return ("PASS" if str(v).lower() == threshold else "FAIL"), v


def _entries(card: dict, threshold: dict) -> tuple[str, Any, str, str, dict]:
    """1.2 by threshold key: ``(result, observed, key, threshold, extra)``."""
    if "min_entries" in threshold:
        thr = threshold["min_entries"]
        n, basis = precheck.entries_count(precheck._get(card, "counts"))
        if n is None:
            return "FAIL", None, "min_entries", thr, {"basis": basis, "note": "counts.entries_distinct / counts.closed missing"}
        extra = {"basis": basis}
        if basis != "entries_distinct":
            extra["note"] = "旧口径: the run card has no counts.entries_distinct (re-run ir backtest); closed trades stand in for distinct entries"
        result, observed, _ = precheck._ge({"n": n}, ("n",), thr, "int")
        return result, observed, "min_entries", thr, extra
    thr = threshold["min_trades"]
    result, observed, _ = precheck._ge(card, ("counts", "closed"), thr, "int")
    return result, observed, "min_trades", thr, {}


def screen(card: dict, protocol: dict | None = None) -> dict:
    """``{"evidence_status", "items", "not_evaluated", "next"}`` for an ``ir backtest`` run card."""
    protocol = protocol if protocol is not None else precheck.load_protocol()
    by_id = {i["id"]: i for i in protocol["stages"]["validated"]["items"]}
    items = []
    for item_id, path, key, kind in SCREEN_ITEMS:
        p = by_id[item_id]
        extra: dict = {}
        if item_id == "1.2":
            result, observed, key, thr, extra = _entries(card, p["threshold"])
            items.append({"id": item_id, "label": p["label"], "result": result, "observed": observed, "threshold": {key: thr}, **extra})
            continue
        thr = p["threshold"][key]
        if item_id == "1.16":
            result, observed = _coverage(card, thr)
        elif item_id == "1.3":
            result, observed, _ = precheck._gt(card, path, thr, kind)
            opened = precheck._get(card, "counts", "opened")
            if opened == 0 and not isinstance(opened, bool):
                result, extra = "N/A", {"note": "0 closed trades"}  # nothing was ever opened: net > 0 is not a finding (1.2 judges the count)
        else:
            result, observed, _ = precheck._ge(card, path, thr, kind)
        items.append({"id": item_id, "label": p["label"], "result": result, "observed": observed, "threshold": {key: thr}, **extra})
    res = {i["id"]: i["result"] for i in items}
    if res["1.1"] == "FAIL" or res["1.2"] == "FAIL":
        status = "insufficient"
    elif res["1.3"] == "FAIL" or res["1.16"] == "FAIL":
        status = "failed_screen"
    else:
        status = "passed_screen"
    return {"evidence_status": status, "items": items, "not_evaluated": list(NOT_EVALUATED), "next": "ir_robustness"}
