"""``ir_backtest`` request: only the IR is required; everything else has a default or is derived (research-truth B2).

Mirrors upstream's ``BacktestConfigSchema``: a pydantic model of the arguments the model may give (every error listed at
once), defaults filled in by code. What the IR already determines -- instruments, step, trials -- is never taken from the
model: ``instruments`` / ``step_minutes`` / ``trials`` / ``trials_evidence`` it sends are ignored and reported back in
``ignored_args``.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

DAY_MS = 86_400_000
MINUTE_MS = 60_000
#: default research window: the ``DEFAULT_WINDOW_DAYS`` days before the credential's hold-out cutoff (UTC day aligned)
DEFAULT_WINDOW_DAYS = 180

#: Each venue's public non-VIP perp taker fee, checked 2026-10-14; AlphaKeel G1 1.17 compares it with the account's fees
#: again. Used only when the request gives no ``fees`` (the leaf package keeps "no default fee": the default lives here,
#: with its source label in the run card).
DEFAULT_PUBLIC_TAKER = {"binance": {"perp": "0.0005"}, "okx": {"perp": "0.0005"}, "bybit": {"perp": "0.00055"}}
DEFAULT_FEES_SOURCE = "public base-tier taker (tool default, user decision 2026-10-14)"
REQUEST_FEES_SOURCE = "request"

#: venue-native perp contract id per base: the venues with 1m kline history in AlphaKeel's market dataset
VENUE_PERP_SYMBOL = {"binance": "{b}USDT", "okx": "{b}-USDT-SWAP", "bybit": "{b}USDT"}
SHAPE_MARKETS = {"cross_perp": ("perp",)}

STEP_CHOICES = (1, 2, 5, 10, 15, 30, 60)
#: mirrors AlphaKeel ``crates/research-service/src/dataset_view.rs`` ``MAX_FRAMES`` (decision instants per run)
MAX_DECISION_INSTANTS = 25_000

#: model arguments the tool computes itself (from the IR and the trial ledger)
DERIVED_ARGS = ("instruments", "step_minutes", "trials", "trials_evidence")
IGNORED_NOTE = "computed from the IR and the trial ledger"


class IrBacktestArgs(BaseModel):
    """The arguments of ``alphakeel_research(action="ir_backtest")``; unknown keys are kept (``extra="allow"``)."""

    model_config = ConfigDict(extra="allow")

    ir: str
    start_ms: int | None = None
    end_ms: int | None = None
    fees: dict | str | None = None
    accept_partial: bool = False
    capital_base: str | int | float | None = None
    pack_dir: str | None = None
    verbose: bool = False


def parse_args(args: dict[str, Any]) -> IrBacktestArgs:
    """``args`` as :class:`IrBacktestArgs`; a ``ValueError`` listing every problem at once."""
    try:
        return IrBacktestArgs.model_validate(args)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(x) for x in err['loc']) or 'args'}: {err['msg']}" for err in e.errors())
        raise ValueError(f"ir_backtest arguments: {problems}") from None


def ignored_args(args: dict[str, Any]) -> list[str]:
    """The derived arguments the model sent anyway (they are not used)."""
    return [k for k in DERIVED_ARGS if args.get(k) not in (None, "", [], {})]


def utc_day_floor(ms: int) -> int:
    return ms - ms % DAY_MS


def window(a: IrBacktestArgs, holdout_cutoff_ms: Any) -> tuple[int, int]:
    """``(start_ms, end_ms)``: the given ones, else end = the hold-out cutoff aligned to its UTC day and start = end - 180 d."""
    end = a.end_ms
    if end is None:
        if isinstance(holdout_cutoff_ms, bool) or not isinstance(holdout_cutoff_ms, int):
            raise ValueError("ir_backtest: the service reports no holdout_cutoff_ms, so there is no default window: pass end_ms")
        end = utc_day_floor(holdout_cutoff_ms)
    start = a.start_ms if a.start_ms is not None else end - DEFAULT_WINDOW_DAYS * DAY_MS
    if not end > start:
        raise ValueError("ir_backtest: end_ms must be after start_ms")
    return start, end


def instruments(ir_doc: dict) -> list[dict]:
    """``universe.bases`` x the venues of ``universe.pairs`` x the shape's markets, venue-native perp symbols."""
    u = ir_doc.get("universe") or {}
    bases = u.get("bases")
    if not isinstance(bases, list) or not bases:
        raise ValueError("ir_backtest needs universe.bases as an explicit list")
    pairs = u.get("pairs")
    markets = SHAPE_MARKETS.get(u.get("shape"))
    if not isinstance(pairs, list) or markets is None:
        # research-truth §2 (user decision 2026-10-14): explicit pairs + cross_perp only
        raise ValueError("ir_backtest supports only an explicit universe.pairs list (not \"any\") with shape cross_perp: "
                         "write the venue pairs out and use cross_perp")
    venues: list[str] = []
    for pair in pairs:
        for v in pair:
            if v not in venues:
                venues.append(v)
    for v in venues:
        if v not in VENUE_PERP_SYMBOL:
            raise ValueError(f"no 1m kline history for {v}: the dataset backtest covers {', '.join(VENUE_PERP_SYMBOL)}")
    return [{"venue": v, "market": m, "symbol": VENUE_PERP_SYMBOL[v].format(b=b), "base": b, "quote": "USDT"}
            for v in venues for b in bases for m in markets]


def step_minutes(start_ms: int, end_ms: int) -> int:
    """The smallest of :data:`STEP_CHOICES` that keeps the run within :data:`MAX_DECISION_INSTANTS`."""
    minutes = (end_ms - start_ms) / MINUTE_MS
    for s in STEP_CHOICES:
        if math.ceil(minutes / s) <= MAX_DECISION_INSTANTS:
            return s
    longest = MAX_DECISION_INSTANTS * STEP_CHOICES[-1] // 1440
    raise ValueError(f"ir_backtest: the window is too long: at {STEP_CHOICES[-1]}-minute steps it may span at most {longest} days")


def row_estimate(n_instruments: int, window_minutes: int) -> int:
    """Pack rows: per instrument one 1m kline per window minute plus the warm-up day, and settlements at most hourly."""
    return n_instruments * (window_minutes + 1440 + math.ceil(window_minutes / 60))


def check_rows(n_instruments: int, start_ms: int, end_ms: int, max_rows: Any) -> None:
    """Refuse before freezing a pack the service would refuse (:func:`row_estimate` over ``max_rows``)."""
    if isinstance(max_rows, bool) or not isinstance(max_rows, int):
        return
    estimate = row_estimate(n_instruments, (end_ms - start_ms) // MINUTE_MS)
    if estimate > max_rows:
        # the longest whole-day window with row_estimate <= max_rows: n * (1464 * D + 1440) <= max_rows
        days = max((max_rows // n_instruments - 1440) // 1464, 0)
        raise ValueError(f"the pack would hold ~{estimate} rows (limit {max_rows}): with {n_instruments} instruments the window "
                         f"can be at most {days} days")


def _fee_table(v: Any) -> dict | None:
    """The fee table a request gives (object, JSON string or readable JSON file), or None when it cannot be read here
    (the CLI reports that)."""
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        text = v.strip()
        if not text.startswith(("{", "[")):
            try:
                with open(os.path.expanduser(text), encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                return None
        try:
            doc = json.loads(text)
        except ValueError:
            return None
        return doc if isinstance(doc, dict) else None
    return None


def fees(a: IrBacktestArgs, venues: list[str]) -> tuple[Any, str]:
    """The fee table and its source label: the request's (every derived venue needs a perp rate), else
    :data:`DEFAULT_PUBLIC_TAKER` cut to the derived venues."""
    if a.fees in (None, "", {}):
        return {v: dict(DEFAULT_PUBLIC_TAKER[v]) for v in venues if v in DEFAULT_PUBLIC_TAKER}, DEFAULT_FEES_SOURCE
    table = _fee_table(a.fees)
    if table is not None:
        missing = [v for v in venues if not (isinstance(table.get(v), dict) and table[v].get("perp") not in (None, ""))]
        if missing:
            raise ValueError(f"fees has no perp rate for {', '.join(missing)}: give a rate for every venue in universe.pairs, "
                             "or omit fees to use the public base-tier default")
    return a.fees, REQUEST_FEES_SOURCE


def plan(args: dict[str, Any], ir_doc: dict, who: dict) -> dict[str, Any]:
    """Everything ``ir backtest`` is called with, from the model's ``args``, the IR and ``check()`` (``who``).

    Returns the CLI arguments (``ir``, ``start_ms``, ``end_ms``, ``instruments``, ``step_minutes``, ``fees``,
    ``fees_source`` and the pass-through ones) plus ``ignored_args``. ``trials`` / ``trials_evidence`` are added by the
    caller from the trial ledger."""
    a = parse_args(args)
    start, end = window(a, (who or {}).get("holdout_cutoff_ms"))
    insts = instruments(ir_doc)
    check_rows(len(insts), start, end, ((who or {}).get("datasets") or {}).get("max_rows"))
    venues = list(dict.fromkeys(d["venue"] for d in insts))
    fee_table, source = fees(a, venues)
    out: dict[str, Any] = {"ir": a.ir, "start_ms": start, "end_ms": end, "instruments": insts,
                           "step_minutes": step_minutes(start, end), "fees": fee_table, "fees_source": source,
                           "accept_partial": a.accept_partial, "verbose": a.verbose}
    for k in ("capital_base", "pack_dir"):
        if getattr(a, k) not in (None, ""):
            out[k] = getattr(a, k)
    if args.get("out") not in (None, ""):  # passed through as before (user decision 2026-10-14)
        out["out"] = args["out"]
    out["ignored_args"] = ignored_args(args)
    return out


def service_check() -> dict:
    """``check()`` of the research service (credential, hold-out cutoff, dataset limits): one call per ``ir_backtest``."""
    from alphakeel_research.client import Client

    c = Client()
    try:
        return c.check()
    finally:
        c.close()
