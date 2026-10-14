"""IR backtest trials in upstream's hypothesis registry, and the result card built from it (research-truth B4, §5).

One hypothesis per agent session (``<session run dir>/ir_hypothesis.json`` names it); every successful ``ir_backtest``
is linked to it with ``HypothesisRegistry.link_backtest``. The number of distinct versions (``strategy_id``) linked so
far, this one included, is the run's ``--trials``; ``<session run dir>/ir_trials_evidence.jsonl`` lists them (one line
per version) and is its ``--trials-evidence``. AlphaKeel counts trials in its own research-service ledger anyway (G1 1.6,
every ``ir_backtest`` registers a trial): this ledger is what Vibe-Trading's own ``trials`` / DSR and the result card read.

``HypothesisRegistry._save`` is an unlocked read-modify-write of one global file: create and link run under an
``fcntl.flock`` on ``<hypotheses.json>.lock`` (no lock where there is no ``fcntl``, as ``alphakeel_handoff_routes``).
"""

from __future__ import annotations

import contextlib
import json
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

try:  # POSIX advisory lock; the Windows desktop build has no fcntl
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

HYPOTHESIS_FILE = "ir_hypothesis.json"
TRIALS_EVIDENCE_FILE = "ir_trials_evidence.jsonl"
CARD_MAX_ROWS = 10


def _registry():
    from src.hypotheses.registry import HypothesisRegistry

    return HypothesisRegistry()


@contextlib.contextmanager
def _locked(registry) -> Iterator[None]:
    if fcntl is None:
        yield
        return
    path = Path(f"{registry.path}.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _hypothesis_id(session_run_dir: str | Path) -> str | None:
    try:
        doc = json.loads((Path(session_run_dir) / HYPOTHESIS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    hid = doc.get("hypothesis_id") if isinstance(doc, dict) else None
    return hid if isinstance(hid, str) and hid else None


def session_hypothesis(session_run_dir: str | Path, ir_name: str = "") -> str:
    """The session's hypothesis id; created (status ``testing``) and recorded in ``ir_hypothesis.json`` the first time."""
    hid = _hypothesis_id(session_run_dir)
    if hid is not None:
        return hid
    reg = _registry()
    d = Path(session_run_dir)
    with _locked(reg):
        hyp = reg.create(title=f"IR research {d.name}", thesis=ir_name or d.name, status="testing",
                         skills=["alphakeel-handoff"], data_sources=["alphakeel_b2"])
    d.mkdir(parents=True, exist_ok=True)
    (d / HYPOTHESIS_FILE).write_text(json.dumps({"hypothesis_id": hyp.hypothesis_id}) + "\n", encoding="utf-8")
    return hyp.hypothesis_id


def _run_cards(session_run_dir: str | Path) -> tuple[str | None, list[dict]]:
    hid = _hypothesis_id(session_run_dir)
    if hid is None:
        return None, []
    for h in _registry().list():
        if h.hypothesis_id == hid:
            return hid, [rc for rc in h.run_cards if isinstance(rc, dict)]
    return hid, []


def _parameters(ir_doc: Any) -> dict:
    params = (ir_doc or {}).get("parameters") if isinstance(ir_doc, dict) else None
    return {k: v.get("value") for k, v in params.items() if isinstance(v, dict)} if isinstance(params, dict) else {}


def _ir_of(run_dir: Any) -> dict | None:
    try:
        return json.loads((Path(str(run_dir)) / "ir.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def trials_for(session_run_dir: str | Path, strategy_id: str, *, ir_doc: dict | None = None,
               window: dict | None = None, ir_name: str = "") -> tuple[int, str]:
    """``(count, evidence_path)``: the distinct ``strategy_id`` of the session hypothesis' linked runs plus this one, and
    ``<session>/ir_trials_evidence.jsonl`` with one line per version (strategy_id, parameter values, window, run_dir)."""
    session_hypothesis(session_run_dir, ir_name)
    _, cards = _run_cards(session_run_dir)
    versions: dict[str, dict] = {}
    for rc in cards:  # in link order: the latest run of a version wins
        m = rc.get("metrics") or {}
        sid = m.get("strategy_id")
        if not isinstance(sid, str) or not sid:
            continue
        run_dir = rc.get("backtest_run_dir") or None
        versions[sid] = {"strategy_id": sid, "parameters": _parameters(_ir_of(run_dir)), "window": m.get("window"),
                         "run_dir": run_dir}
    if strategy_id not in versions:
        versions[strategy_id] = {"strategy_id": strategy_id, "parameters": _parameters(ir_doc), "window": window, "run_dir": None}
    path = Path(session_run_dir) / TRIALS_EVIDENCE_FILE
    path.write_text("".join(json.dumps(v, sort_keys=True, ensure_ascii=False) + "\n" for v in versions.values()), encoding="utf-8")
    return len(versions), str(path)


def link(session_run_dir: str | Path, payload: dict) -> str:
    """Link a successful ``ir backtest`` (its CLI result: ``run_dir``, ``run_card``) to the session hypothesis."""
    card_path = Path(str(payload["run_card"]))
    card = json.loads(card_path.read_text(encoding="utf-8"))
    hid = session_hypothesis(session_run_dir, card.get("name") or "")
    scr = card.get("screen") or {}
    metrics = {"strategy_id": card["strategy_id"], "name": card.get("name"), "window": card.get("window"),
               "trade_count": card["counts"]["closed"], "net_usdt": card["amounts"]["equity_end"],
               "fees_usdt": card["amounts"]["fees"], "max_drawdown": card["max_drawdown"]["fraction_of_capital"],
               "evidence_status": scr.get("evidence_status"), "fees_source": card["assumptions"].get("fees_source")}
    reg = _registry()
    with _locked(reg):
        reg.link_backtest(hid, run_card_path=str(card_path), backtest_run_dir=str(payload.get("run_dir") or card_path.parent),
                          metrics=metrics, validation={"screen": scr.get("items") or []})
    return hid


# ---------------------------------------------------------------------------------------------------------------------
# result card (system text appended after the answer; numbers verbatim from the registered metrics)
# ---------------------------------------------------------------------------------------------------------------------

_TEXT = {
    True: {"title": "**研究结果（系统根据试验账本生成，非模型撰写）**", "fees": "费率口径", "per_fill": "每笔",
           "default": "公开基础档 taker（工具默认）", "request": "请求给定费率",
           "summary": "本会话 {n} 个版本 / {m} 次回测：通过初筛 {p}，未通过 {f}，证据不足 {i}",
           "head": "| 版本 | 窗口 | 成交 | 净收益 USDT | 最大回撤 | 判定 |",
           "passed": "通过初筛，1.4–1.13 未评估", "failed": "未通过初筛（{ids}）", "insufficient": "证据不足（{ids}）",
           "more": "另有 {n} 次回测见账本",
           "not_evaluated": "1.4–1.13（成本压力、DSR、PBO、邻域、走步等）未评估：需要 ir_robustness。",
           "ledger": "账本：{file}；假设 {hid}"},
    False: {"title": "**Research result (generated by the system from the trial ledger, not written by the model)**",
            "fees": "Fee basis", "per_fill": "per fill", "default": "public base-tier taker (tool default)",
            "request": "fees given in the request",
            "summary": "This session: {n} versions / {m} backtests: passed screen {p}, failed screen {f}, insufficient {i}",
            "head": "| Version | Window | Trades | Net USDT | Max drawdown | Status |",
            "passed": "passed screen; 1.4–1.13 not evaluated", "failed": "failed screen ({ids})", "insufficient": "insufficient ({ids})",
            "more": "{n} more backtests in the ledger",
            "not_evaluated": "1.4–1.13 (cost stress, DSR, PBO, neighbours, walk-forward, ...) not evaluated: needs ir_robustness.",
            "ledger": "Ledger: {file}; hypothesis {hid}"},
}
_SCREEN_GROUPS = {"insufficient": ("1.1", "1.2"), "failed_screen": ("1.3", "1.16")}


def _dec(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _two(d: Decimal) -> str:
    s = format(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN), "f")
    return s.replace("-", "−")


def fmt_amount(v: Any) -> str:
    """A registered decimal string at 2 decimals (U+2212 minus); ``—`` when missing."""
    d = _dec(v)
    return "—" if d is None else _two(d)


def fmt_fraction(v: Any) -> str:
    """A registered fraction as a percent at 2 decimals."""
    d = _dec(v)
    return "—" if d is None else _two(d * 100) + "%"


def _pct(rate: Any) -> str:
    """A fee rate as an exact percent (``0.00055`` -> ``0.055%``)."""
    d = (_dec(rate) or Decimal(0)) * 100
    return ("0" if d.is_zero() else format(d.normalize(), "f")) + "%"


def _window(w: Any) -> str:
    if not isinstance(w, dict) or not w.get("start_day") or not w.get("end_day"):
        return "—"
    a, b = str(w["start_day"]), str(w["end_day"])
    return f"{a}→{b[5:]}" if a[:4] == b[:4] else f"{a}→{b}"


def _verdict(rc: dict, t: dict) -> str:
    status = (rc.get("metrics") or {}).get("evidence_status")
    if status == "passed_screen":
        return t["passed"]
    if status in _SCREEN_GROUPS:
        items = (rc.get("validation") or {}).get("screen") or []
        ids = [i.get("id") for i in items if isinstance(i, dict) and i.get("result") == "FAIL" and i.get("id") in _SCREEN_GROUPS[status]]
        return t["failed" if status == "failed_screen" else "insufficient"].format(ids=", ".join(ids) or "?")
    return str(status or "—")


def _fee_lines(cards: list[dict], t: dict) -> list[str]:
    """One line per distinct fee basis: its source and every venue's rate, read from the registered run card."""
    seen: list[tuple[str, str]] = []
    for rc in cards:
        src = (rc.get("metrics") or {}).get("fees_source") or ""
        fees = None
        try:
            fees = json.loads(Path(str(rc.get("run_card_path"))).read_text(encoding="utf-8"))["assumptions"]["fees"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        rates = " / ".join(f"{v} {_pct(m['perp'])}"
                           for v, m in sorted(fees.items()) if isinstance(m, dict) and _dec(m.get("perp")) is not None) \
            if isinstance(fees, dict) else ""
        label = t["default"] if src.startswith("public base-tier taker") else (t["request"] if src == "request" else src)
        key = (label, rates)
        if key not in seen:
            seen.append(key)
    return [f"{t['fees']}：{label} — {rates} {t['per_fill']}" if rates else f"{t['fees']}：{label}" for label, rates in seen]


def render_card(session_run_dir: str | Path, zh: bool) -> str | None:
    """The result card of the session's IR backtests, or None (no hypothesis, or nothing linked)."""
    hid, cards = _run_cards(session_run_dir)
    if hid is None or not cards:
        return None
    t = _TEXT[bool(zh)]
    # versions = distinct strategy_id (= trials.count); rows and the status counts are per linked backtest
    versions = {(rc.get("metrics") or {}).get("strategy_id") for rc in cards} - {None}
    counts = {"passed_screen": 0, "failed_screen": 0, "insufficient": 0}
    for rc in cards:
        s = (rc.get("metrics") or {}).get("evidence_status")
        if s in counts:
            counts[s] += 1
    order = sorted(range(len(cards)), key=lambda i: -(_dec((cards[i].get("metrics") or {}).get("net_usdt")) or Decimal("-Infinity")))
    shown = set(order[:CARD_MAX_ROWS]) | {i for i, rc in enumerate(cards) if (rc.get("metrics") or {}).get("evidence_status") == "passed_screen"}
    rows = []
    for i in order:
        if i not in shown:
            continue
        m = cards[i].get("metrics") or {}
        rows.append(f"| {m.get('name') or m.get('strategy_id') or '—'} | {_window(m.get('window'))} | {m.get('trade_count', '—')} | "
                    f"{fmt_amount(m.get('net_usdt'))} | {fmt_fraction(m.get('max_drawdown'))} | {_verdict(cards[i], t)} |")
    sep = "：" if zh else ": "
    fee_lines = [ln.replace("：", sep) for ln in _fee_lines(cards, t)]
    lines = ["---", t["title"], *fee_lines,
             t["summary"].format(n=len(versions), m=len(cards), p=counts["passed_screen"], f=counts["failed_screen"], i=counts["insufficient"]),
             t["head"], "|---|---|---|---|---|---|", *rows]
    if len(cards) > len(rows):
        lines.append(t["more"].format(n=len(cards) - len(rows)))
    lines += [t["not_evaluated"], t["ledger"].format(file=TRIALS_EVIDENCE_FILE, hid=hid)]
    return "\n".join(lines)
