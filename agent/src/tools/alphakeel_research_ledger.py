"""IR backtest trials of one conversation, and the result card built from them (research-truth B4, §5).

Scope is the conversation (``session_id``), not a turn: every turn gets a new run directory. The tool's private ledger
is ``<sessions dir>/<session_id>/ir_ledger.jsonl`` -- outside the agent's write roots, so the model cannot write it --
with one line per successful ``ir_backtest`` (``strategy_id``, ``name``, the absolute ``run_card_path`` and its
``run_card_sha256``, ``evidence_dir``, the turn's ``run_dir``, ``ts``). The run's ``--trials`` is the number of distinct
``strategy_id`` in it (this one included); ``<turn run dir>/ir_trials_evidence.jsonl`` lists them (one line per version)
and is its ``--trials-evidence``. AlphaKeel counts trials in its own research-service ledger anyway (G1 1.6).

Every run is also linked to one upstream ``HypothesisRegistry`` hypothesis per conversation (spec B4; the id is kept
in ``<sessions dir>/<session_id>/ir_hypothesis.json``); the registry is not a source of counts or of the card.

The card reads only the private ledger and re-reads every run card, checking its sha256: a run card that was changed
or is missing is not shown (and counted in one line). Read-modify-writes run under ``fcntl.flock`` on
``<file>.lock`` (no lock where there is no ``fcntl``, as ``alphakeel_handoff_routes``).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

try:  # POSIX advisory lock; the Windows desktop build has no fcntl
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

LEDGER_FILE = "ir_ledger.jsonl"
HYPOTHESIS_FILE = "ir_hypothesis.json"
TRIALS_EVIDENCE_FILE = "ir_trials_evidence.jsonl"
CARD_MAX_ROWS = 10
NO_SESSION_WARNING = "no session id: trial ledger and result card skipped"


def _sessions_dir() -> Path:
    from src.config.paths import get_sessions_dir

    return Path(get_sessions_dir())


def _registry():
    from src.hypotheses.registry import HypothesisRegistry

    return HypothesisRegistry()


@contextlib.contextmanager
def _flock(path: Path) -> Iterator[None]:
    """Exclusive advisory lock on ``<path>.lock`` (none without fcntl)."""
    if fcntl is None:
        yield
        return
    lock = Path(f"{path}.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _fsync_dir(path: Path) -> None:
    """Make a new directory entry durable (POSIX; a no-op where directories cannot be opened)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _session_dir(session_id: str) -> Path:
    return _sessions_dir() / session_id


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ledger_rows(session_id: str) -> list[dict]:
    """The private ledger of ``session_id`` in append order."""
    path = _session_dir(session_id) / LEDGER_FILE
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and isinstance(row.get("strategy_id"), str):
                        rows.append(row)
    except OSError:
        return []
    return rows


def verified_card(row: dict) -> dict | None:
    """The run card a ledger row names, re-read; None when it is missing or its sha256 differs from the row's."""
    try:
        path = Path(str(row["run_card_path"]))
        if _sha256(path) != row.get("run_card_sha256"):
            return None
        card = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return card if isinstance(card, dict) else None


def session_hypothesis(session_id: str, ir_name: str = "") -> str:
    """The conversation's hypothesis id; created (status ``testing``) the first time."""
    path = _session_dir(session_id) / HYPOTHESIS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with _flock(path):
        try:
            hid = json.loads(path.read_text(encoding="utf-8")).get("hypothesis_id")
        except (OSError, ValueError, AttributeError):
            hid = None
        if isinstance(hid, str) and hid:
            return hid
        reg = _registry()
        with _flock(Path(reg.path)):
            hyp = reg.create(title=f"IR research {session_id}", thesis=ir_name or session_id, status="testing",
                             skills=["alphakeel-handoff"], data_sources=["alphakeel_b2"])
        path.write_text(json.dumps({"hypothesis_id": hyp.hypothesis_id}) + "\n", encoding="utf-8")
        return hyp.hypothesis_id


def _parameters(ir_doc: Any) -> dict:
    params = (ir_doc or {}).get("parameters") if isinstance(ir_doc, dict) else None
    return {k: v.get("value") for k, v in params.items() if isinstance(v, dict)} if isinstance(params, dict) else {}


def _json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def trials_for(session_id: str, run_dir: str | Path, strategy_id: str, *, ir_doc: dict | None = None,
               window: dict | None = None, ir_name: str = "") -> tuple[int, str]:
    """``(count, evidence_path)``: the distinct ``strategy_id`` in the conversation's private ledger plus this one, and
    ``<run_dir>/ir_trials_evidence.jsonl`` with one line per version (strategy_id, parameter values, window, run_dir)."""
    session_hypothesis(session_id, ir_name)
    versions: dict[str, dict] = {}
    for row in ledger_rows(session_id):  # in append order: the latest run of a version wins
        card_path = Path(str(row.get("run_card_path")))
        card = _json_file(card_path) or {}
        versions[row["strategy_id"]] = {"strategy_id": row["strategy_id"], "parameters": _parameters(_json_file(card_path.parent / "ir.json")),
                                        "window": card.get("window"), "run_dir": str(card_path.parent)}
    if strategy_id not in versions:
        versions[strategy_id] = {"strategy_id": strategy_id, "parameters": _parameters(ir_doc), "window": window, "run_dir": None}
    path = Path(run_dir) / TRIALS_EVIDENCE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(v, sort_keys=True, ensure_ascii=False) + "\n" for v in versions.values()), encoding="utf-8")
    return len(versions), str(path)


def record(session_id: str, run_dir: str | Path, result: dict, evidence_dir: str | None) -> None:
    """After a successful ``ir backtest`` (its CLI result: ``run_dir``, ``run_card``): append the private ledger row, then
    link the run to the conversation's hypothesis (``HypothesisRegistry.link_backtest``)."""
    card_path = Path(str(result["run_card"])).resolve()
    card = json.loads(card_path.read_text(encoding="utf-8"))
    row = {"strategy_id": card["strategy_id"], "name": card.get("name"), "run_card_path": str(card_path),
           "run_card_sha256": _sha256(card_path), "evidence_dir": evidence_dir, "run_dir": str(run_dir),
           "ts": int(time.time() * 1000)}
    ledger = _session_dir(session_id) / LEDGER_FILE
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with _flock(ledger):
        created = not ledger.exists()
        with open(ledger, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
            # the row is a trial for the DSR floor: durable before the call returns (upstream TraceWriter.write: flush +
            # fsync per record, the directory entry too when the file is new)
            f.flush()
            with contextlib.suppress(OSError):  # a filesystem without fsync: flush-only, as upstream
                os.fsync(f.fileno())
        if created:
            _fsync_dir(ledger.parent)
    hid = session_hypothesis(session_id, card.get("name") or "")
    scr = card.get("screen") or {}
    metrics = {"strategy_id": card["strategy_id"], "name": card.get("name"), "window": card.get("window"),
               "trade_count": card["counts"]["closed"], "net_usdt": card["amounts"]["equity_end"],
               "fees_usdt": card["amounts"]["fees"], "max_drawdown": card["max_drawdown"]["fraction_of_capital"],
               "evidence_status": scr.get("evidence_status"), "fees_source": card["assumptions"].get("fees_source")}
    reg = _registry()
    with _flock(Path(reg.path)):
        reg.link_backtest(hid, run_card_path=str(card_path), backtest_run_dir=str(result.get("run_dir") or card_path.parent),
                          metrics=metrics, validation={"screen": scr.get("items") or []})


# ---------------------------------------------------------------------------------------------------------------------
# result card (system text appended after the answer; every figure re-read from the verified run cards)
# ---------------------------------------------------------------------------------------------------------------------

_TEXT = {
    True: {"title": "**研究结果（系统根据试验账本生成，非模型撰写）**", "fees": "费率口径：", "per_fill": "每笔",
           "default": "公开基础档 taker（工具默认）", "request": "请求给定费率",
           "summary": "本会话 {n} 个版本 / {m} 次回测：通过初筛 {p}，未通过 {f}，证据不足 {i}",
           "head": "| 版本 | 窗口 | 成交 | 净收益 USDT | 手续费 USDT | 资金费 USDT | 最大回撤 | 判定 |",
           "passed": "通过初筛，1.4–1.13 未评估", "failed": "未通过初筛（{ids}）", "insufficient": "证据不足（{ids}）",
           "more": "另有 {n} 次回测见账本", "tampered": "，已改动 {n}", "tampered_row": "run card 已改动或缺失",
           "not_evaluated": "1.4–1.13（成本压力、DSR、PBO、邻域、走步等）未评估：需要 ir_robustness。",
           "ledger": "账本：{file}"},
    False: {"title": "**Research result (generated by the system from the trial ledger, not written by the model)**",
            "fees": "Fee basis: ", "per_fill": "per fill", "default": "public base-tier taker (tool default)",
            "request": "fees given in the request",
            "summary": "This session: {n} versions / {m} backtests: passed screen {p}, failed screen {f}, insufficient {i}",
            "head": "| Version | Window | Trades | Net USDT | Fees USDT | Funding USDT | Max drawdown | Status |",
            "passed": "passed screen; 1.4–1.13 not evaluated", "failed": "failed screen ({ids})", "insufficient": "insufficient ({ids})",
            "more": "{n} more backtests in the ledger", "tampered": ", modified {n}", "tampered_row": "run card modified or missing",
            "not_evaluated": "1.4–1.13 (cost stress, DSR, PBO, neighbours, walk-forward, ...) not evaluated: needs ir_robustness.",
            "ledger": "Ledger: {file}"},
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
    """A decimal at 2 decimals (U+2212 minus); ``—`` when missing."""
    d = _dec(v)
    return "—" if d is None else _two(d)


def fmt_fraction(v: Any) -> str:
    """A fraction as a percent at 2 decimals."""
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


def _status(card: dict) -> Any:
    return (card.get("screen") or {}).get("evidence_status")


def _verdict(card: dict, t: dict) -> str:
    status = _status(card)
    if status == "passed_screen":
        return t["passed"]
    if status in _SCREEN_GROUPS:
        items = (card.get("screen") or {}).get("items") or []
        ids = [i.get("id") for i in items if isinstance(i, dict) and i.get("result") == "FAIL" and i.get("id") in _SCREEN_GROUPS[status]]
        return t["failed" if status == "failed_screen" else "insufficient"].format(ids=", ".join(ids) or "?")
    return str(status or "—")


def _fee_lines(cards: list[dict], t: dict) -> list[str]:
    """One line per distinct fee basis: its source and every venue's rate, from the run cards."""
    seen: list[tuple[str, str]] = []
    for card in cards:
        a = card.get("assumptions") or {}
        src, fees = str(a.get("fees_source") or ""), a.get("fees")
        rates = " / ".join(f"{v} {_pct(m['perp'])}" for v, m in sorted(fees.items())
                           if isinstance(m, dict) and _dec(m.get("perp")) is not None) if isinstance(fees, dict) else ""
        label = t["default"] if src.startswith("public base-tier taker") else (t["request"] if src == "request" else src)
        if (label, rates) not in seen:
            seen.append((label, rates))
    return [f"{t['fees']}{label} — {rates} {t['per_fill']}" if rates else f"{t['fees']}{label}" for label, rates in seen]


def render_card(session_id: str | None, zh: bool) -> str | None:
    """The result card of the conversation's IR backtests, or None (no session id, or nothing in the private ledger).

    Header N (distinct ``strategy_id``) and M (backtests) count every private ledger row, as ``trials.count`` does. A row
    whose run card was changed or is missing keeps its line (name and ``strategy_id`` from the ledger, ``—`` for every
    figure, the status says so), sorts last, is always shown and counts in no screen class."""
    if not session_id:
        return None
    rows_all = ledger_rows(session_id)
    if not rows_all:
        return None
    t = _TEXT[bool(zh)]
    entries = [(row, verified_card(row)) for row in rows_all]
    cards = [c for _, c in entries if c is not None]
    bad = sum(1 for _, c in entries if c is None)
    counts = {"passed_screen": 0, "failed_screen": 0, "insufficient": 0}
    for c in cards:
        if _status(c) in counts:
            counts[_status(c)] += 1
    versions = {row["strategy_id"] for row in rows_all}

    def key(i: int):
        c = entries[i][1]
        if c is None:
            return (1, Decimal(0))
        d = _dec((c.get("amounts") or {}).get("equity_end"))
        return (0, -(Decimal("-Infinity") if d is None else d))

    order = sorted(range(len(entries)), key=key)
    always = {i for i, (_, c) in enumerate(entries) if c is None or _status(c) == "passed_screen"}
    shown = set(order[:CARD_MAX_ROWS]) | always
    rows = []
    for i in order:
        if i not in shown:
            continue
        row, c = entries[i]
        if c is None:
            name = f"{row.get('name') or '—'} ({str(row.get('strategy_id') or '')[:8]})"
            rows.append(f"| {name} | — | — | — | — | — | — | {t['tampered_row']} |")
            continue
        name = f"{c.get('name') or '—'} ({str(c.get('strategy_id') or '')[:8]})"
        rows.append(f"| {name} | {_window(c.get('window'))} | {(c.get('counts') or {}).get('closed', '—')} | "
                    f"{fmt_amount((c.get('amounts') or {}).get('equity_end'))} | "
                    f"{fmt_amount((c.get('amounts') or {}).get('fees'))} | {fmt_amount((c.get('amounts') or {}).get('funding'))} | "
                    f"{fmt_fraction((c.get('max_drawdown') or {}).get('fraction_of_capital'))} | {_verdict(c, t)} |")
    summary = t["summary"].format(n=len(versions), m=len(entries), p=counts["passed_screen"], f=counts["failed_screen"],
                                  i=counts["insufficient"]) + (t["tampered"].format(n=bad) if bad else "")
    lines = ["---", t["title"], *_fee_lines(cards, t), summary, t["head"], "|---|---|---|---|---|---|---|---|", *rows]
    if len(entries) > len(rows):
        lines.append(t["more"].format(n=len(entries) - len(rows)))
    lines += [t["not_evaluated"], t["ledger"].format(file=TRIALS_EVIDENCE_FILE)]
    return "\n".join(lines)
