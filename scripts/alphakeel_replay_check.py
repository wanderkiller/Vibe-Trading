#!/usr/bin/env python3
"""Machine acceptance of an IR research session (research-truth spec §8). Read-only.

    python scripts/alphakeel_replay_check.py <session_id> [--sessions-dir DIR]

Run inside the Vibe-Trading container (or anywhere the session's trace, its private IR ledger and the run cards it
names are at the recorded paths). Prints one ``PASS`` / ``FAIL`` line per check and exits 1 when any fails:

1. no ``ir_backtest`` failed on a request error: the failure envelope's error code / message names ``request.invalid``,
   ``resource.rows``, ``step_minutes`` or ``base/quote``, or is the planner's row-limit refusal
2. at most one ``ir_validate`` failed on the IR's structure
3. every successful ``ir_backtest`` has ``evidence_dir``, and its (sha256-verified) run card's ``trials.count`` is the
   number of distinct ``strategy_id`` in the conversation's private ledger up to and including that run
4. the final answer ends with the result card exactly as the private ledger and the verified run cards render it now
   (rows, verdict column and header counts)
5. no ``answer_rejected`` issue rejected a number whose absolute value, at 2 decimals, is a non-zero value of the 13
   ``metrics.csv`` columns of an evidence copy of this conversation's verified run cards
6. no separate ``freeze`` call
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

AGENT = Path(__file__).resolve().parents[1] / "agent"
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

PARAM_ERROR_TOKENS = ("request.invalid", "resource.rows", "step_minutes", "base/quote", "the pack would hold ~")
STRUCTURE_CODES = ("ir.shape", "ir.invalid", "ir.schema", "ir.kind", "ir.parameter_path", "ir.parameter_mismatch")


def _default_root() -> Path:
    try:
        from src.config.paths import get_runtime_root

        return Path(get_runtime_root())
    except Exception:  # noqa: BLE001 - standalone
        import os

        home = os.environ.get("VIBE_TRADING_HOME")
        return Path(home).expanduser() if home else Path.home() / ".vibe-trading"


def _text(entry: dict, trace_dir: Path, field: str) -> str:
    """A trace entry's text field, inline or from its sidecar (``<field>_path``)."""
    if isinstance(entry.get(field), str):
        return entry[field]
    rel = entry.get(f"{field}_path")
    if rel:
        try:
            return (trace_dir / rel).read_text(encoding="utf-8")
        except OSError:
            pass
    return str(entry.get(f"{field}_preview") or entry.get("preview") or "")


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def load_trace(trace_dir: Path) -> list[dict]:
    out = []
    with open(trace_dir / "trace.jsonl", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                doc = _json(line)
                if isinstance(doc, dict):
                    out.append(doc)
    return out


def research_calls(entries: list[dict], trace_dir: Path) -> list[dict]:
    """``alphakeel_research`` calls in order: ``{call_id, action, args, status, payload, text}``."""
    calls: dict[str, dict] = {}
    order: list[str] = []
    for e in entries:
        if e.get("tool") != "alphakeel_research" or not e.get("call_id"):
            continue
        cid = e["call_id"]
        if e.get("type") == "tool_call":
            args = e.get("args") or {}
            calls[cid] = {"call_id": cid, "action": str(args.get("action") or ""), "args": args, "status": None,
                          "payload": None, "text": ""}
            order.append(cid)
        elif e.get("type") == "tool_result" and cid in calls:
            text = _text(e, trace_dir, "result")
            calls[cid].update(status=e.get("status"), text=text, payload=_json(text))
    return [calls[c] for c in order]


def _failed(call: dict) -> bool:
    p = call["payload"]
    return call["status"] == "error" or (isinstance(p, dict) and p.get("status") == "error")


def _ok(call: dict) -> bool:
    p = call["payload"]
    return isinstance(p, dict) and p.get("status") == "ok" and call["status"] != "error"


def final_answer(entries: list[dict], trace_dir: Path) -> str | None:
    answers = [e for e in entries if e.get("type") == "answer"]
    return _text(answers[-1], trace_dir, "content") if answers else None


def _dec(v: Any) -> Decimal | None:
    try:
        d = Decimal(str(v).replace("−", "-").replace(",", "").rstrip("%"))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


# --- checks ------------------------------------------------------------------------------------------------------------

def _error_text(call: dict) -> str:
    """The failure envelope's error code and message (not the rest of the envelope, e.g. ``ignored_args``)."""
    p = call["payload"] if isinstance(call["payload"], dict) else None
    if p is None:
        return call["text"]
    parts = [str(p.get("code") or ""), str(p.get("error") or "")]
    r = p.get("result")
    e = r.get("error") if isinstance(r, dict) else None
    if isinstance(e, dict):
        parts += [str(e.get("code") or ""), str(e.get("message") or "")]
    elif e is not None:
        parts.append(str(e))
    return " ".join(parts)


def check_1(calls: list[dict]) -> tuple[bool, str]:
    bad = [c["call_id"] for c in calls if c["action"] == "ir_backtest" and _failed(c)
           and any(t in _error_text(c) for t in PARAM_ERROR_TOKENS)]
    return not bad, f"{len(bad)} ir_backtest request errors {bad}"


def check_2(calls: list[dict]) -> tuple[bool, str]:
    def structural(c: dict) -> bool:
        p = c["payload"] if isinstance(c["payload"], dict) else {}
        r = p.get("result") if isinstance(p.get("result"), dict) else {}
        return r.get("source") == "local" or any(k in _error_text(c) for k in STRUCTURE_CODES)

    bad = [c["call_id"] for c in calls if c["action"] == "ir_validate" and _failed(c) and structural(c)]
    return len(bad) <= 1, f"{len(bad)} structural ir_validate failures (limit 1) {bad}"


def check_3(calls: list[dict], session_id: str) -> tuple[bool, str]:
    from src.tools import alphakeel_research_ledger as L

    rows = L.ledger_rows(session_id)
    index = {str(Path(str(r.get("run_card_path")))): i for i, r in enumerate(rows)}
    problems = []
    n = 0
    for c in calls:
        if c["action"] != "ir_backtest" or not _ok(c):
            continue
        n += 1
        p = c["payload"]
        r = p.get("result") if isinstance(p.get("result"), dict) else {}
        if not isinstance(p.get("evidence_dir"), str):
            problems.append(f"{c['call_id']}: no evidence_dir")
        key = str(Path(str(r.get("run_card"))).resolve()) if r.get("run_card") else None
        i = index.get(key) if key else None
        if i is None:
            problems.append(f"{c['call_id']}: not in the private ledger")
            continue
        card = L.verified_card(rows[i])
        if card is None:
            problems.append(f"{c['call_id']}: run card modified or missing")
            continue
        want = len({row["strategy_id"] for row in rows[: i + 1]})
        count = (card.get("trials") or {}).get("count")
        if count != want:
            problems.append(f"{c['call_id']}: trials.count {count!r} != {want} versions")
    return n > 0 and not problems, f"{n} successful ir_backtest; " + ("; ".join(problems) or "all consistent")


def check_4(answer: str | None, session_id: str) -> tuple[bool, str]:
    from src.tools.alphakeel_research_ledger import render_card

    if not answer:
        return False, "no final answer"
    cards = [c for c in (render_card(session_id, True), render_card(session_id, False)) if c]
    if not cards:
        return False, "the private ledger renders no card"
    if any(answer.rstrip().endswith(c) for c in cards):
        return True, "the answer ends with the card the ledger and the verified run cards render"
    return False, "the answer does not end with the card the ledger and the verified run cards render (rows, verdicts, counts)"


def _two(d: Decimal) -> str:
    return format(abs(d).quantize(Decimal("0.01")), "f")


def check_5(entries: list[dict], session_id: str) -> tuple[bool, str]:
    from src.tools import alphakeel_research_ledger as L

    from alphakeel_research.ir_backtest import METRICS_COLUMNS

    registered = set()
    for row in L.ledger_rows(session_id):
        if L.verified_card(row) is None or not row.get("evidence_dir") or not row.get("run_dir"):
            continue
        path = Path(str(row["run_dir"])) / str(row["evidence_dir"]) / "artifacts" / "metrics.csv"
        try:
            head, values = path.read_text(encoding="utf-8").splitlines()[:2]
        except (OSError, ValueError):
            continue
        cells = dict(zip(head.split(","), values.split(",")))
        for col in METRICS_COLUMNS:
            d = _dec(cells.get(col, ""))
            if d is not None and not d.is_zero():
                registered.add(_two(d))
    bad = []
    for e in entries:
        if e.get("type") != "answer_rejected":
            continue
        for issue in e.get("issues") or []:
            if not isinstance(issue, dict) or issue.get("value") is None:
                continue
            d = _dec(issue["value"])
            if d is not None and _two(d) in registered:
                bad.append(f"{issue['value']} ({issue.get('reason')})")
    return not bad, f"{len(bad)} rejected registered figures {bad}"


def check_6(calls: list[dict]) -> tuple[bool, str]:
    n = sum(1 for c in calls if c["action"] == "freeze")
    return n == 0, f"{n} freeze calls"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("session_id")
    ap.add_argument("--sessions-dir", type=Path, help="default: the runtime sessions directory")
    a = ap.parse_args(argv)
    sessions = a.sessions_dir or _default_root() / "sessions"
    from src.tools import alphakeel_research_ledger as L

    L._sessions_dir = lambda: sessions  # the private ledger lives beside the trace
    trace_dir = sessions / a.session_id
    entries = load_trace(trace_dir)
    calls = research_calls(entries, trace_dir)
    results = [("1", "ir_backtest request errors = 0", *check_1(calls)),
               ("2", "structural ir_validate failures <= 1", *check_2(calls)),
               ("3", "evidence_dir and trials.count per ir_backtest", *check_3(calls, a.session_id)),
               ("4", "final answer ends with the ledger's result card", *check_4(final_answer(entries, trace_dir), a.session_id)),
               ("5", "no rejected registered figure", *check_5(entries, a.session_id)),
               ("6", "no separate freeze", *check_6(calls))]
    print(f"session {a.session_id}: trace {trace_dir}, {len(L.ledger_rows(a.session_id))} private ledger rows")
    for n, title, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'} {n} {title}: {detail}")
    return 0 if all(r[2] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
