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

``<sessions dir>/<session_id>/data_exposure.jsonl`` is the conversation's data exposure (AlphaKeel protocol 2.5, G1 1.14):
one line per read (``source``, ``end_ms`` exclusive, ``via``, ``ts``), appended by ``backtest.data_audit.record_exposure``
in the agent process -- in-process tools directly, the backtest tool for its child -- because every tool call that reads
data runs in a process of its own and the exporting CLI child cannot see their in-process records. ``ir_export`` passes
the latest end to the export (``provenance.data_exposure_end_ms``).

The generic ``backtest`` tool has its own private trial ledger, ``<session dir>/engine_ledger.jsonl``: one row per
successful engine run, with a snapshot of the run (code, config, metrics, the code-made screen) under
``<session dir>/engine_runs/<NNNN>/`` because the next backtest overwrites the same run dir. The card shows these rows
too, from the snapshots only (sha256 of ``screen.json`` checked). ``alphakeel_holdout.json`` caches the research
service's hold-out cutoff: the backtest tool refuses crypto data that reaches into the hold-out.
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
EXPOSURE_FILE = "data_exposure.jsonl"
ENGINE_LEDGER_FILE = "engine_ledger.jsonl"
ENGINE_RUNS_DIR = "engine_runs"
DEFAULT_HOLDOUT_DAYS = 60
HOLDOUT_FILE = "alphakeel_holdout.json"
HOLDOUT_SOURCES = frozenset({"okx", "binance", "ccxt", "alphakeel_b2", "alphakeel_pack"})
HOLDOUT_CACHE_SECONDS = 6 * 3600
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


def _append_durable(path: Path, row: dict) -> None:
    """Append one JSON line under the file lock, durable before returning (upstream TraceWriter.write: flush + fsync per
    record, the directory entry too when the file is new)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _flock(path):
        created = not path.exists()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
            f.flush()
            with contextlib.suppress(OSError):  # a filesystem without fsync: flush-only, as upstream
                os.fsync(f.fileno())
        if created:
            _fsync_dir(path.parent)


def record_exposure(session_id: str, source: str, end_ms: int, via: str) -> None:
    """Append one data read of the conversation (``source`` returned data up to ``end_ms``, exclusive) to its exposure
    file. Called by ``backtest.data_audit.record_exposure`` for the session bound in the agent process."""
    row = {"source": str(source), "end_ms": int(end_ms), "via": str(via), "ts": int(time.time() * 1000)}
    _append_durable(_session_dir(session_id) / EXPOSURE_FILE, row)


def exposure_end_ms(session_id: str) -> int | None:
    """The latest ``end_ms`` in the conversation's exposure file, or None (no file, no readable line)."""
    best = None
    try:
        with open(_session_dir(session_id) / EXPOSURE_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    v = json.loads(line).get("end_ms")
                except (ValueError, AttributeError):
                    continue
                if isinstance(v, int) and not isinstance(v, bool) and (best is None or v > best):
                    best = v
    except OSError:
        return None
    return best


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
    # the row is a trial for the DSR floor: durable before the call returns
    _append_durable(_session_dir(session_id) / LEDGER_FILE, row)
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
# generic-engine trial ledger (``backtest`` tool) and the hold-out cutoff
# ---------------------------------------------------------------------------------------------------------------------

def engine_rows(session_id: str) -> list[dict]:
    """The private engine ledger of ``session_id`` in append order."""
    rows = []
    try:
        with open(_session_dir(session_id) / ENGINE_LEDGER_FILE, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and isinstance(row.get("trial_key"), str):
                        rows.append(row)
    except OSError:
        return []
    return rows


def _trial_key(run_dir: Path) -> str:
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    code = (run_dir / "code" / "signal_engine.py").read_bytes()
    return hashlib.sha256(code + b"\0" + json.dumps(config, sort_keys=True).encode("utf-8")).hexdigest()


def record_engine_run(session_id: str, run_dir: Path) -> dict:
    """After a successful generic ``backtest``: count the trial, screen the run, snapshot it and append the private row.

    ``trials`` = distinct (code, config) trial keys in the engine ledger (this one included) + distinct IR
    ``strategy_id`` of the conversation. Returns the screen dict plus ``trials`` and ``seq``."""
    from backtest.engine_screen import screen_engine_run

    run_dir = Path(run_dir)
    key = _trial_key(run_dir)
    prior = engine_rows(session_id)
    keys = {r["trial_key"] for r in prior} | {key}
    trials = len(keys) + len({r["strategy_id"] for r in ledger_rows(session_id)})
    latest: dict[str, dict] = {}
    for r in prior:  # append order: the latest row of a trial key wins
        latest[r["trial_key"]] = r
    sharpes = [float(r["sharpe_daily"]) for k, r in latest.items()
               if k != key and isinstance(r.get("sharpe_daily"), (int, float)) and not isinstance(r.get("sharpe_daily"), bool)]
    screen = screen_engine_run(run_dir, trials=trials, variant_sharpes=sharpes)
    runs = _session_dir(session_id) / ENGINE_RUNS_DIR
    seq = len(prior) + 1
    while True:
        snap = runs / f"{seq:04d}"
        try:
            snap.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            seq += 1
    for rel in ("code/signal_engine.py", "config.json", "artifacts/metrics.csv"):
        src = run_dir / rel
        if src.is_file():
            (snap / rel).parent.mkdir(parents=True, exist_ok=True)
            (snap / rel).write_bytes(src.read_bytes())
    screen_path = snap / "screen.json"
    screen_path.write_text(json.dumps(screen, sort_keys=True, ensure_ascii=False, indent=2), encoding="utf-8")
    sharpe = (screen.get("facts") or {}).get("sharpe_daily")
    row = {"seq": seq, "trial_key": key, "run_dir": str(run_dir), "snapshot_dir": str(snap),
           "screen_sha256": _sha256(screen_path), "sharpe_daily": sharpe if isinstance(sharpe, (int, float)) else None,
           "ts": int(time.time() * 1000)}
    _append_durable(_session_dir(session_id) / ENGINE_LEDGER_FILE, row)
    return {**screen, "trials": trials, "seq": seq}


def verified_engine_screen(row: dict) -> dict | None:
    """The snapshot ``screen.json`` an engine ledger row names, re-read; None when missing or its sha256 differs."""
    try:
        path = Path(str(row["snapshot_dir"])) / "screen.json"
        if _sha256(path) != row.get("screen_sha256"):
            return None
        screen = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return screen if isinstance(screen, dict) else None


def _fallback_cutoff_ms(now_ms: int | None = None) -> int:
    now = int(time.time() * 1000) if now_ms is None else now_ms
    day = 86_400_000
    return (now // day) * day - DEFAULT_HOLDOUT_DAYS * day


def cached_holdout_cutoff_ms(session_id: str | None, *, max_age_seconds: float | None = None) -> int | None:
    """The cutoff in the session's cache file (no network), or None; ``max_age_seconds`` bounds the cache age."""
    if not session_id:
        return None
    try:
        data = json.loads((_session_dir(session_id) / HOLDOUT_FILE).read_text(encoding="utf-8"))
        cutoff, ts = data.get("cutoff_ms"), data.get("ts")
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(cutoff, bool) or not isinstance(cutoff, int):
        return None
    if max_age_seconds is not None:
        if isinstance(ts, bool) or not isinstance(ts, (int, float)) or time.time() * 1000 - ts > max_age_seconds * 1000:
            return None
    return cutoff


def holdout_cutoff(session_id: str | None) -> dict:
    """``{"cutoff_ms", "source"}``: AlphaKeel's hold-out start. ``source`` is ``service`` (the research service's
    ``holdout_cutoff_ms``, cached per session for 6 h) or ``fallback_60d`` (UTC day floor of now minus 60 days, used
    when the service cannot be asked). Only a service answer is cached, and only with a session id."""
    cached = cached_holdout_cutoff_ms(session_id, max_age_seconds=HOLDOUT_CACHE_SECONDS)
    if cached is not None:
        return {"cutoff_ms": cached, "source": "service"}
    try:
        from src.tools.alphakeel_ir_request import service_check

        v = service_check().get("holdout_cutoff_ms")
        if isinstance(v, bool) or not isinstance(v, int):
            raise TypeError("holdout_cutoff_ms is not an int")
    except Exception:  # noqa: BLE001 - no answer from the service: the conservative fallback applies
        return {"cutoff_ms": _fallback_cutoff_ms(), "source": "fallback_60d"}
    if session_id:
        path = _session_dir(session_id) / HOLDOUT_FILE
        with contextlib.suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
            with _flock(path):
                path.write_text(json.dumps({"cutoff_ms": v, "ts": int(time.time() * 1000)}), encoding="utf-8")
    return {"cutoff_ms": v, "source": "service"}


# ---------------------------------------------------------------------------------------------------------------------
# result card (system text appended after the answer; every figure re-read from the verified run cards)
# ---------------------------------------------------------------------------------------------------------------------

_TEXT = {
    True: {"title": "**研究结果（系统根据试验账本生成，非模型撰写）**", "fees": "费率口径：", "per_fill": "每笔",
           "default": "公开基础档 taker（工具默认）", "request": "请求给定费率",
           "summary": "本会话 IR {n} 个版本 / {m} 次回测：通过初筛 {p}，未通过 {f}，证据不足 {i}",
           "head": "| 版本 | 窗口 | 成交 | 净收益 USDT | 手续费 USDT | 资金费 USDT | 最大回撤 | 判定 |",
           "passed": "通过初筛，1.4–1.13 未评估", "failed": "未通过初筛（{ids}）", "insufficient": "证据不足（{ids}）",
           "more": "另有 {n} 次回测见账本", "tampered": "，已改动 {n}", "tampered_row": "run card 已改动或缺失",
           "not_evaluated": "1.4–1.13（成本压力、DSR、PBO、邻域、走步等）未评估：需要 ir_robustness。",
           "ledger": "账本：{file}"},
    False: {"title": "**Research result (generated by the system from the trial ledger, not written by the model)**",
            "fees": "Fee basis: ", "per_fill": "per fill", "default": "public base-tier taker (tool default)",
            "request": "fees given in the request",
            "summary": "This session's IR: {n} versions / {m} backtests: passed screen {p}, failed screen {f}, insufficient {i}",
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


def _ir_section(session_id: str, zh: bool) -> list[str]:
    """The IR part of the result card (no lines when the private IR ledger is empty).

    Header N (distinct ``strategy_id``) and M (backtests) count every private ledger row, as ``trials.count`` does. A row
    whose run card was changed or is missing keeps its line (name and ``strategy_id`` from the ledger, ``—`` for every
    figure, the status says so), sorts last, is always shown and counts in no screen class."""
    rows_all = ledger_rows(session_id)
    if not rows_all:
        return []
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
    return lines


_ENGINE_TEXT = {
    True: {"title": "**通用引擎回测（backtest 工具；系统根据试验账本生成，非模型撰写）**",
           "summary": "通用引擎回测 {m} 次 / {n} 个不同版本（代码+配置）：通过初筛 {p}，未通过 {f}，证据不足 {i}",
           "head": "| # | 标的 / 来源 | 窗口天数 | 成交 | 净收益 | DSR（试验数） | 方向 / 在场时间 | 判定 |",
           "passed": "通过初筛，1.4–1.14 未评估", "failed": "未通过初筛（{ids}）", "insufficient": "证据不足（{ids}）",
           "unavailable": "初筛不可用", "tampered": "，已改动 {n}", "tampered_row": "快照已改动或缺失",
           "more": "另有 {n} 次回测见账本",
           "no_live": "通用引擎结果不能直接上实盘：须交接 Strategy IR，通过 ir_robustness 与 AlphaKeel G1/G2。",
           "non_auditable": "数据不可审计（{srcs}）：实时拉取的数据无法逐位复现，不能作为 AlphaKeel 证据。",
           "holdout": "⚠ 本会话读取过 AlphaKeel hold-out 区间的数据（截至 {day}，hold-out 起点 {cutoff_day}）：本会话的研究不能交接 AlphaKeel（G1 1.14）。",
           "long_only": "仅多", "short_only": "仅空", "long_short": "多空", "flat": "空仓"},
    False: {"title": "**Generic engine backtests (`backtest` tool; generated by the system from the trial ledger, not written by the model)**",
            "summary": "Generic engine backtests {m} / {n} distinct versions (code + config): passed screen {p}, failed screen {f}, insufficient {i}",
            "head": "| # | Symbols / source | Window days | Trades | Net | DSR (trials) | Direction / time in market | Status |",
            "passed": "passed screen; 1.4–1.14 not evaluated", "failed": "failed screen ({ids})", "insufficient": "insufficient ({ids})",
            "unavailable": "screen unavailable", "tampered": ", modified {n}", "tampered_row": "snapshot modified or missing",
            "more": "{n} more backtests in the ledger",
            "no_live": "Engine results cannot go live directly: hand over a Strategy IR, pass ir_robustness and AlphaKeel G1/G2.",
            "non_auditable": "Data not auditable ({srcs}): live-pulled data cannot be re-read bit for bit and is not AlphaKeel evidence.",
            "holdout": "⚠ This session read data inside AlphaKeel's hold-out window (up to {day}, hold-out starts {cutoff_day}): its research cannot be handed to AlphaKeel (G1 1.14).",
            "long_only": "long only", "short_only": "short only", "long_short": "long/short", "flat": "flat"},
}
_ENGINE_GROUPS = {"insufficient": ("1.1", "1.2"), "failed_screen": ("1.3", "1.6", "1.8")}


def _item_observed(screen: dict, item_id: str) -> Any:
    for i in screen.get("items") or []:
        if isinstance(i, dict) and i.get("id") == item_id:
            return i.get("observed")
    return None


def _engine_verdict(screen: dict, t: dict) -> str:
    status = screen.get("evidence_status")
    if status == "passed_screen":
        return t["passed"]
    if status in _ENGINE_GROUPS:
        ids = [i.get("id") for i in screen.get("items") or []
               if isinstance(i, dict) and i.get("result") == "FAIL" and i.get("id") in _ENGINE_GROUPS[status]]
        return t["failed" if status == "failed_screen" else "insufficient"].format(ids=", ".join(ids) or "?")
    return t["unavailable"] if status == "unavailable" else str(status or "—")


def _day(ms: int) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc).strftime("%Y-%m-%d")


def _engine_section(session_id: str, zh: bool, first: bool) -> list[str]:
    rows_all = engine_rows(session_id)
    if not rows_all:
        return []
    t = _ENGINE_TEXT[bool(zh)]
    entries = [(row, verified_engine_screen(row)) for row in rows_all]
    screens = [s for _, s in entries if s is not None]
    bad = sum(1 for _, s in entries if s is None)
    counts = {"passed_screen": 0, "failed_screen": 0, "insufficient": 0}
    for s in screens:
        if s.get("evidence_status") in counts:
            counts[s["evidence_status"]] += 1
    shown = set(range(max(0, len(entries) - CARD_MAX_ROWS), len(entries)))
    shown |= {i for i, (_, s) in enumerate(entries) if s is None or s.get("evidence_status") == "passed_screen"}
    rows = []
    for i in sorted(shown):
        row, s = entries[i]
        seq = row.get("seq", i + 1)
        if s is None:
            rows.append(f"| {seq} | — | — | — | — | — | — | {t['tampered_row']} |")
            continue
        facts = s.get("facts") or {}
        label = ",".join(str(c) for c in facts.get("codes") or []) or "—"
        direction = t.get(str(facts.get("direction")), str(facts.get("direction") or "—"))
        rows.append(f"| {seq} | {label} / {facts.get('source') or '—'} | {_item_observed(s, '1.1') if _item_observed(s, '1.1') is not None else '—'} | "
                    f"{_item_observed(s, '1.2') if _item_observed(s, '1.2') is not None else '—'} | {fmt_amount(_item_observed(s, '1.3'))} | "
                    f"{fmt_amount(facts.get('dsr'))} ({facts.get('trials', '—')}) | {direction} / {fmt_fraction(facts.get('time_in_market'))} | "
                    f"{_engine_verdict(s, t)} |")
    summary = t["summary"].format(m=len(entries), n=len({r["trial_key"] for r in rows_all}), p=counts["passed_screen"],
                                  f=counts["failed_screen"], i=counts["insufficient"]) + (t["tampered"].format(n=bad) if bad else "")
    lines = (["---"] if first else []) + [t["title"], summary, t["head"], "|---|---|---|---|---|---|---|---|", *rows]
    if len(entries) > len(rows):
        lines.append(t["more"].format(n=len(entries) - len(rows)))
    lines.append(t["no_live"])
    non = sorted({str(x) for s in screens for x in (s.get("facts") or {}).get("non_auditable_sources") or []})
    if non:
        lines.append(t["non_auditable"].format(srcs=", ".join(non)))
    return lines


def _holdout_warning(session_id: str, zh: bool) -> list[str]:
    exposure = exposure_end_ms(session_id)
    cutoff = cached_holdout_cutoff_ms(session_id)
    if exposure is None or cutoff is None or exposure <= cutoff:
        return []
    return [_ENGINE_TEXT[bool(zh)]["holdout"].format(day=_day(exposure - 1), cutoff_day=_day(cutoff))]


def render_card(session_id: str | None, zh: bool) -> str | None:
    """The result card of the conversation: its IR backtests (``ir_ledger.jsonl``), then its generic engine backtests
    (``engine_ledger.jsonl``), then a warning when it read hold-out data. None without a session id or when both
    ledgers are empty."""
    if not session_id:
        return None
    ir = _ir_section(session_id, zh)
    engine = _engine_section(session_id, zh, first=not ir)
    if not ir and not engine:
        return None
    return "\n".join([*ir, *engine, *_holdout_warning(session_id, zh)])
