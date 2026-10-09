"""Strategy-reliability battery and confidence rating for Strategy IR research backtests (``ir robustness``).

Why: a backtest that got prettier after a change (a filter, a threshold) proves nothing by itself. The battery reruns the
WHOLE system (``ir_backtest.run``) on an already-opened dataset pack -- no network, no re-freezing -- under variations
that a real edge should survive and a lucky one should not, records every variant it ran, and grades the result with an
explicit rule table a reader can recompute from ``robustness.json``.

Runs (each in ``out_dir/runs/<label>/``, independent runs in ``workers`` processes; identical runs are run once):

* ``base``             the IR as given on the IS window;
* ``cost-x1.5|x2``     same IR, every taker fee of the effective fee table multiplied (Decimal);
* ``basis-x2``         IR copy with ``assumptions.basis_stress`` doubled (a different IR: a different strategy_id);
* ``param-<n>-lo|hi``  for every ``parameters`` entry, the literal at its README §3 rule 5 ``path`` (and the parameter's
                       ``value``) set to ``robust[0]`` / ``robust[1]``; each neighbour is its own IR;
* ``oos``              the same IR on the OOS window (another pack allowed), when given;
* ``filter-off|on``    when a filter is under test (``{"kind": "excluded_bases", "bases": [...]}``): the IR without and
                       with those bases in ``universe.excluded_bases``;
* ``perm-NNNN``        Monte Carlo permutation test of the filter: ``permutations`` random exclusion sets of the same size,
                       drawn (seeded) from the bases that actually traded in ``filter-off``; draws are preferred whose
                       blocked-trade count in ``filter-off`` is within max(1, 25%) of the real filter's (comparable trade
                       count; if fewer than one such set exists every set of that size is eligible and this is reported).
                       Each draw is a FULL rerun (re-selection and knock-on effects included, not a reshuffle of trades).
                       p = (1 + #{random improvement >= real improvement}) / (1 + N), improvement = net(on) - net(off).

Trial count (DSR): the number of DISTINCT IR definitions (strategy_id) run on the IS window as candidates -- base,
basis stress, parameter neighbours, filter off/on -- plus ``prior_trials`` the caller declares for variants tried before
the battery (self-declared, reported as such). Permutation draws are recorded in ``trials.jsonl`` but NOT counted: they
are null-model draws nobody selects among, and counting them would make the DSR (and the grade) fall as the permutation
count rises, i.e. reward running fewer permutations. Fee stresses share the base strategy_id and are not new variants.

Statistics (base IS daily returns = UTC-daily equity change / one capital base shared by every variant, AlphaKeel
convention, ``backtest.alphakeel_metrics``): Sharpe (x sqrt 365), PSR vs 0, DSR (honest trial count, variants' per-day
Sharpe dispersion), PBO by CSCV over the candidate variants' daily returns (>= 4 variants, S = min(10, 2*floor(days/10))
blocks, S >= 4), stationary-bootstrap 95% CI of the mean daily return (seeded, block = max(2, ceil(n^(1/3)))), effective
sample size and the t-stat on it, trade count, per-coin concentration of closed-trade net, max drawdown, net decomposition
(price / funding / fees / open-position mark) from the run card amounts. Money is Decimal; floats only inside statistics.

Rating rule table (``RULES``; written into robustness.json and robustness.md)

Caps -- each triggered cap is a ceiling on the grade:

  ===========================  ===================================================================  =====
  id                           condition                                                            max
  ===========================  ===================================================================  =====
  base_net_nonpositive         base IS net (equity_end) <= 0                                        F
  oos_net_nonpositive          OOS window run and its net <= 0                                      D
  cost_x2_nonpositive          net with every taker fee x2 <= 0                                     D
  no_oos                       no OOS window was run                                                C
  coverage_partial             any run's pack was frozen with accept_partial (data gaps)            C
  few_trades                   fewer than 30 closed trades in the base run                          C
  short_window                 fewer than 60 days in the base IS window                             C
  funding_unpriced             more than 1% of funding settlements on open legs could not be priced C
  filter_not_better_than_random a filter is under test and its permutation p-value > 0.10            C
  synthetic_view               the data view is the synthetic-touch dataset view                    B
  ===========================  ===================================================================  =====

Score components -- applicable ones only; an applicable component that cannot be computed counts as FAILED ("not
demonstrated"):

  ======================  ===========================================================  ===========================
  id                      pass when                                                    applicable
  ======================  ===========================================================  ===========================
  dsr                     DSR >= 0.95                                                  always
  psr                     PSR(0) >= 0.95                                               always
  pbo                     PBO <= 0.2                                                   always
  bootstrap_ci            stationary-bootstrap 95% CI lower bound of mean daily > 0    always
  segments                >= 75% of the k equal sub-periods have positive net          always
  parameter_neighbours    >= 75% of parameter neighbours have positive net             the IR has parameters
  filter_permutation      filter permutation p-value <= 0.05                           a filter is under test
  concentration           top coin's share of closed-trade net <= 50%                  always
  oos_degradation         OOS net per day >= 50% of IS net per day                     an OOS window was run
  ======================  ===========================================================  ===========================

  score = passed / applicable; score grade A >= 0.9, B >= 0.75, C >= 0.5, otherwise D.
  grade = the worst of the score grade and every triggered cap (order A > B > C > D > F).

So A is unreachable on the dataset view (synthetic touch): A needs evidence this battery cannot produce (real top of
book, predicted funding, the engine's execution profile -- ``ir review`` / AlphaKeel validation). The OOS degradation
component is an addition to the reviewer's list: an IS edge that halves out of sample is the usual sign of selection.

What this battery does NOT test (also in every report): latency, order-book depth / slippage beyond the spread-0
synthetic touch, partial fills, margin / leverage / liquidation (and AlphaKeel's runtime liq_buffer guard), the real BBO
and predicted funding rate a live scan sees, venue outages and API errors, capacity, regimes outside the packs' windows,
research done before the battery (only ``prior_trials`` declares it), and Python-vs-Rust reconciliation.
"""

from __future__ import annotations

import concurrent.futures as cf
import copy
import json
import math
import multiprocessing
import random
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import ir as IR
from . import ir_backtest, ir_views
from .errors import ApiError
from .packfile import Pack, PackCache

SCHEMA = "vibe-trading.ir-robustness/1"
COST_MULTIPLIERS = ("1.5", "2")
BASIS_MULTIPLIER = "2"
SEGMENTS = 4
BOOTSTRAP_RESAMPLES = 2000
PBO_MAX_BLOCKS = 10
TRADE_MATCH_TOLERANCE = Decimal("0.25")
GRADES = ("A", "B", "C", "D", "F")

RULES = {
    "caps": [
        {"id": "base_net_nonpositive", "condition": "base IS net (equity_end) <= 0", "max_grade": "F"},
        {"id": "oos_net_nonpositive", "condition": "OOS window run and its net <= 0", "max_grade": "D"},
        {"id": "cost_x2_nonpositive", "condition": "net with every taker fee x2 <= 0", "max_grade": "D"},
        {"id": "no_oos", "condition": "no OOS window was run", "max_grade": "C"},
        {"id": "coverage_partial", "condition": "any run's pack was frozen with accept_partial (data gaps)", "max_grade": "C"},
        {"id": "few_trades", "condition": "fewer than 30 closed trades in the base run", "max_grade": "C"},
        {"id": "short_window", "condition": "fewer than 60 days in the base IS window", "max_grade": "C"},
        {"id": "funding_unpriced", "condition": "more than 1% of funding settlements on open legs could not be priced", "max_grade": "C"},
        {"id": "filter_not_better_than_random", "condition": "a filter is under test and its permutation p-value > 0.10", "max_grade": "C"},
        {"id": "synthetic_view", "condition": "the data view is the synthetic-touch dataset view", "max_grade": "B"},
    ],
    "components": [
        {"id": "dsr", "pass_when": "DSR >= 0.95", "applicable": "always"},
        {"id": "psr", "pass_when": "PSR(0) >= 0.95", "applicable": "always"},
        {"id": "pbo", "pass_when": "PBO <= 0.2", "applicable": "always"},
        {"id": "bootstrap_ci", "pass_when": "stationary-bootstrap 95% CI lower bound of the mean daily return > 0", "applicable": "always"},
        {"id": "segments", "pass_when": ">= 75% of the k equal sub-periods have positive net", "applicable": "always"},
        {"id": "parameter_neighbours", "pass_when": ">= 75% of parameter neighbours have positive net", "applicable": "the IR has parameters"},
        {"id": "filter_permutation", "pass_when": "filter permutation p-value <= 0.05", "applicable": "a filter is under test"},
        {"id": "concentration", "pass_when": "top coin's share of closed-trade net <= 50%", "applicable": "always"},
        {"id": "oos_degradation", "pass_when": "OOS net per day >= 50% of IS net per day", "applicable": "an OOS window was run"},
    ],
    "score_to_grade": [{"min_score": 0.9, "grade": "A"}, {"min_score": 0.75, "grade": "B"}, {"min_score": 0.5, "grade": "C"},
                       {"min_score": 0.0, "grade": "D"}],
    "combine": "grade = the worst of the score grade and every triggered cap's max_grade; an applicable component that "
               "cannot be computed counts as failed",
}

NOT_TESTED = [
    "latency between decision and fill",
    "order-book depth and slippage (the dataset view's touch is the last 1m close, spread 0)",
    "partial fills",
    "margin, leverage and liquidation (including AlphaKeel's runtime liq_buffer guard)",
    "the real top of book and the predicted funding rate a live scan sees (the view uses the last settled rate)",
    "venue outages, API errors and order rejections",
    "capacity / market impact of the position size",
    "regimes outside the packs' windows",
    "research done before this battery (only the declared prior_trials enter the trial count)",
    "Python vs AlphaKeel Rust reconciliation (`ir review` on a scan pack)",
]


# ---------------------------------------------------------------------------------------------------------------------
# small pure helpers (tested directly)
# ---------------------------------------------------------------------------------------------------------------------

def _d(x: Any) -> Decimal | None:
    return None if x is None else Decimal(str(x))


def _s(x: Decimal | None) -> str | None:
    return None if x is None else IR.fmt(x)


def _ratio(a: Decimal | None, b: Decimal | None) -> str | None:
    if a is None or b is None or b == 0:
        return None
    return _s(ir_backtest.q8(a / b))


def permutation_p_value(real: Decimal, randoms: list[Decimal]) -> Decimal | None:
    """(1 + #{random >= real}) / (1 + N): the add-one Monte Carlo p-value (never 0). None without draws."""
    if not randoms:
        return None
    hits = sum(1 for r in randoms if r >= real)
    return Decimal(1 + hits) / Decimal(1 + len(randoms))


_TOKEN = re.compile(r"\[([0-9]+)\]")


def set_path(doc: dict, path: str, literal: Any) -> None:
    """Replace the literal at a README §3 rule 5 path (``seg(.seg)*``, ``seg = name([index])*``) in place; KeyError when
    the path does not resolve (same grammar and resolution as ``ir._resolve``)."""
    IR._resolve(doc, path)  # existence and grammar: the validator's own resolution
    steps: list[Any] = []
    for seg in path.split("."):
        m = IR._SEG_RE.fullmatch(seg)
        steps.append(m.group(1))
        steps.extend(int(i) for i in _TOKEN.findall(m.group(2)))
    cur: Any = doc
    for k in steps[:-1]:
        cur = cur[k]
    cur[steps[-1]] = literal


def neighbour_doc(ir_doc: dict, name: str, new_value: str) -> dict:
    """The IR with parameter ``name`` moved to ``new_value`` (canonical decimal string): the literal at its path and the
    parameter's own ``value`` change together (rule 5 keeps them equal); integers stay integers. Raises IR.IrError /
    ValueError when the result is not a valid IR (e.g. a non-integral value for an integer literal)."""
    doc = copy.deepcopy(ir_doc)
    doc.pop("strategy_id", None)
    p = doc["parameters"][name]
    target = IR._resolve(IR._definition(doc), p["path"])
    v = Decimal(new_value)
    if isinstance(target, int) and not isinstance(target, bool):
        if v != v.to_integral_value():
            raise ValueError(f"parameter {name}: {new_value} is not an integer but {p['path']} is")
        literal: Any = int(v)
    else:
        literal = IR.fmt(v)
    set_path(doc, p["path"], literal)
    p["value"] = IR.fmt(v)
    IR.load(doc)
    return doc


def scale_fees(fees: dict | None, multiplier: str) -> dict:
    """Every rate of the effective fee table (``ir_views.default_fees()`` when None) times ``multiplier`` (Decimal)."""
    base = ir_views.default_fees() if fees is None else fees
    m = Decimal(multiplier)
    return {v: {mk: IR.fmt(Decimal(str(r)) * m) for mk, r in row.items() if r not in (None, "")}
            for v, row in sorted(base.items()) if isinstance(row, dict)}


def with_excluded(ir_doc: dict, bases: set[str] | list[str]) -> dict:
    doc = copy.deepcopy(ir_doc)
    doc.pop("strategy_id", None)
    doc["universe"]["excluded_bases"] = sorted({IR.ascii_upper(b) for b in bases})
    return doc


def trade_parts(t: dict) -> dict:
    """Decimal decomposition of one ``trades.jsonl`` record: net = price + funding - fees (closed trades only)."""
    legs = (t["long"], t["short"])
    closed = t["status"] == "closed"
    price = sum((Decimal(x["price_pnl"]) for x in legs), Decimal(0)) if closed else None
    funding = sum((Decimal(x["funding"]) for x in legs), Decimal(0))
    fees = sum((Decimal(x["entry_fee"]) + Decimal(x["exit_fee"]) for x in legs), Decimal(0))
    return {"closed": closed, "price": price, "funding": funding, "fees": fees, "net": Decimal(t["realized"]) if closed else None}


def _trade_key(t: dict) -> tuple:
    return (t["pair_id"], t["opened_ms"], t["closed_ms"], t["status"])


def _sum_parts(trades: list[dict]) -> dict:
    closed = [trade_parts(t) for t in trades if t["status"] == "closed"]
    z = Decimal(0)
    return {"count": len(trades), "closed": len(closed), "open": len(trades) - len(closed),
            "net": _s(sum((p["net"] for p in closed), z)), "price": _s(sum((p["price"] for p in closed), z)),
            "funding": _s(sum((p["funding"] for p in closed), z)), "fees": _s(sum((p["fees"] for p in closed), z))}


def filter_accounting(off_trades: list[dict], on_trades: list[dict], bases: list[str], net_off: Decimal, net_on: Decimal) -> dict:
    """Where a filter's improvement comes from (closed trades; open-at-end positions fall into ``residual``):

    * blocked   = unfiltered trades whose base is excluded by the filter; selection contribution = -their net, split into
                  price impact (-price), funding impact (-funding) and cost savings (+fees);
    * knock-on  = trades only in the filtered run (capacity freed by the blocked trades: ``added``) and unfiltered trades
                  of other bases missing from it (``removed``); identical trades (same pair, open and close times) cancel;
    * residual  = improvement - selection - added + removed (open positions' marks).
    """
    excl = {IR.ascii_upper(b) for b in bases}
    on_keys = {_trade_key(t) for t in on_trades}
    off_keys = {_trade_key(t) for t in off_trades}
    blocked = [t for t in off_trades if IR.ascii_upper(t["base"]) in excl]
    removed = [t for t in off_trades if IR.ascii_upper(t["base"]) not in excl and _trade_key(t) not in on_keys]
    added = [t for t in on_trades if _trade_key(t) not in off_keys]
    b, a, r = _sum_parts(blocked), _sum_parts(added), _sum_parts(removed)
    by_base: dict[str, Decimal] = {}
    for t in blocked:
        if t["status"] == "closed":
            by_base[t["base"]] = by_base.get(t["base"], Decimal(0)) + Decimal(t["realized"])
    improvement = net_on - net_off
    selection = -Decimal(b["net"])
    residual = improvement - selection - Decimal(a["net"]) + Decimal(r["net"])
    return {"bases": sorted(excl), "improvement": _s(improvement), "net_unfiltered": _s(net_off), "net_filtered": _s(net_on),
            "trades_unfiltered": len(off_trades), "trades_filtered": len(on_trades),
            "blocked": {**b, "net_by_base": {k: _s(v) for k, v in sorted(by_base.items())}},
            "selection_contribution": _s(selection), "price_impact": _s(-Decimal(b["price"])),
            "funding_impact": _s(-Decimal(b["funding"])), "cost_savings": b["fees"],
            "knock_on_added": a, "knock_on_removed": r, "residual_open_positions": _s(residual)}


def segment_nets(days: list[str], equity: list[Decimal], k: int) -> list[dict]:
    """Net of ``k`` equal (by day count) consecutive sub-periods of the daily equity curve (starts at 0)."""
    n = len(days)
    k = max(1, min(k, n))
    out, prev = [], Decimal(0)
    for i in range(k):
        lo, hi = round(i * n / k), round((i + 1) * n / k)
        if hi <= lo:
            continue
        net = equity[hi - 1] - prev
        prev = equity[hi - 1]
        out.append({"from": days[lo], "to": days[hi - 1], "days": hi - lo, "net": _s(net)})
    return out


def month_nets(days: list[str], equity: list[Decimal]) -> list[dict]:
    out: dict[str, Decimal] = {}
    prev = Decimal(0)
    last: dict[str, Decimal] = {}
    for d, e in zip(days, equity):
        last[d[:7]] = e
    for m in sorted(last):
        out[m] = last[m] - prev
        prev = last[m]
    return [{"month": m, "net": _s(v)} for m, v in out.items()]


def _positive_fraction(nets: list[str | None]) -> str | None:
    xs = [Decimal(x) for x in nets if x is not None]
    return None if not xs else _s(ir_backtest.q8(Decimal(sum(1 for x in xs if x > 0)) / Decimal(len(xs))))


# ---------------------------------------------------------------------------------------------------------------------
# the rating
# ---------------------------------------------------------------------------------------------------------------------

def _worse(a: str, b: str) -> str:
    return a if GRADES.index(a) >= GRADES.index(b) else b


def _ge(x, t) -> bool | None:
    return None if x is None else Decimal(str(x)) >= Decimal(str(t))


def _le(x, t) -> bool | None:
    return None if x is None else Decimal(str(x)) <= Decimal(str(t))


def rate(f: dict) -> dict:
    """Grade from the battery's facts (keys below; see ``RULES``). Every number used is echoed back in ``facts``."""
    caps_hit = {
        "base_net_nonpositive": Decimal(f["base_net"]) <= 0,
        "oos_net_nonpositive": f.get("oos_net") is not None and Decimal(f["oos_net"]) <= 0,
        "cost_x2_nonpositive": f.get("cost_x2_net") is not None and Decimal(f["cost_x2_net"]) <= 0,
        "no_oos": f.get("oos_net") is None,
        "coverage_partial": bool(f.get("coverage_partial")),
        "few_trades": int(f.get("trades_closed") or 0) < 30,
        "short_window": int(f.get("days") or 0) < 60,
        "funding_unpriced": f.get("funding_unpriced_fraction") is not None and Decimal(str(f["funding_unpriced_fraction"])) > Decimal("0.01"),
        "filter_not_better_than_random": bool(f.get("filter_tested")) and (f.get("filter_p") is None or Decimal(str(f["filter_p"])) > Decimal("0.1")),
        "synthetic_view": bool(f.get("synthetic_view")),
    }
    comp_val = {
        "dsr": (True, _ge(f.get("dsr"), "0.95")),
        "psr": (True, _ge(f.get("psr"), "0.95")),
        "pbo": (True, _le(f.get("pbo"), "0.2")),
        "bootstrap_ci": (True, None if f.get("bootstrap_lower") is None else Decimal(str(f["bootstrap_lower"])) > 0),
        "segments": (True, _ge(f.get("segments_positive_fraction"), "0.75")),
        "parameter_neighbours": (bool(f.get("has_parameters")), _ge(f.get("neighbours_positive_fraction"), "0.75")),
        "filter_permutation": (bool(f.get("filter_tested")), _le(f.get("filter_p"), "0.05")),
        "concentration": (True, _le(f.get("top_coin_share"), "0.5")),
        "oos_degradation": (f.get("oos_net") is not None, _ge(f.get("oos_over_is_net_per_day"), "0.5")),
    }
    components = []
    for c in RULES["components"]:
        applicable, ok = comp_val[c["id"]]
        components.append({"id": c["id"], "pass_when": c["pass_when"], "applicable": applicable,
                           "result": None if not applicable else ("pass" if ok else ("not_computable" if ok is None else "fail"))})
    app = [c for c in components if c["applicable"]]
    passed = sum(1 for c in app if c["result"] == "pass")
    score = passed / len(app) if app else 0.0
    score_grade = next(r["grade"] for r in RULES["score_to_grade"] if score >= r["min_score"])
    grade = score_grade
    caps, reasons = [], []
    for c in RULES["caps"]:
        if caps_hit[c["id"]]:
            caps.append({"id": c["id"], "condition": c["condition"], "max_grade": c["max_grade"]})
            grade = _worse(grade, c["max_grade"])
            reasons.append(f"cap {c['id']} ({c['condition']}): at most {c['max_grade']}")
    for c in app:
        if c["result"] != "pass":
            reasons.append(f"component {c['id']} {c['result']} ({c['pass_when']})")
    return {"grade": grade, "score": round(score, 6), "score_grade": score_grade, "passed": passed, "applicable": len(app),
            "caps": caps, "components": components, "reasons": reasons, "facts": f, "rules": RULES}


# ---------------------------------------------------------------------------------------------------------------------
# running variants
# ---------------------------------------------------------------------------------------------------------------------

def _run_job(job: dict) -> dict:
    """Worker entry (top level: picklable). Reopens the pack from its lock and local content (no client: no network)."""
    pack = job.pop("pack", None)
    if pack is None:
        pack = Pack(job.pop("lock"), PackCache(job.pop("cache_root")), None)
    return ir_backtest.run(job.pop("doc"), pack, job.pop("instruments"), **job)


def daily_equity(run_dir: Path, start_ms: int, end_ms: int) -> tuple[list[str], list[Decimal]]:
    """UTC days of the window and each day's last equity, carried over days without a point: the same series
    ``ir_backtest.run`` builds its daily returns from (run card ``alphakeel_metrics``)."""
    last: dict[str, Decimal] = {}
    with open(run_dir / "equity.jsonl", encoding="utf-8") as fh:
        for ln in fh:
            d = json.loads(ln)
            last[ir_backtest.utc_day(d["t"])] = Decimal(d["equity"])
    days, eq, prev = [], [], Decimal(0)
    day = start_ms - start_ms % ir_backtest.DAY_MS
    while day < end_ms:
        k = ir_backtest.utc_day(day)
        prev = last.get(k, prev)
        days.append(k)
        eq.append(prev)
        day += ir_backtest.DAY_MS
    return days, eq


def _returns(days: list[str], eq: list[Decimal], capital: Decimal) -> list[float]:
    import pandas as pd

    from backtest.alphakeel_metrics import daily_returns

    return daily_returns(pd.Series([float(e) for e in eq], index=pd.to_datetime(days)), float(capital), 0.0)


def _trades(run_dir: Path) -> list[dict]:
    with open(run_dir / "trades.jsonl", encoding="utf-8") as fh:
        return [json.loads(ln) for ln in fh if ln.strip()]


class _Runner:
    """Runs jobs once each (key = ``ir_backtest.run_id_of``), in a spawn process pool or in this process."""

    def __init__(self, out: Path, packs: dict[str, Pack], instruments: list[dict], windows: dict, step: int, capital: Decimal,
                 workers: int):
        self.out, self.packs, self.instruments, self.windows = out, packs, instruments, windows
        self.step, self.capital, self.workers = step, capital, workers
        self.done: dict[str, dict] = {}  # key -> {label, run_dir, card}
        self.records: list[dict] = []
        self.serial_reason = None
        if workers > 1 and any(p.client is not None for p in packs.values()):
            self.serial_reason = "a pack is service-backed (not an exported directory): runs in this process"

    def job(self, label: str, role: str, doc: dict, *, window: str = "is", fees: dict | None = None, fees_source: str = "base",
            fee_multiplier: str = "1") -> dict:
        sid = IR.load(doc).strategy_id
        start, end = self.windows[window]
        pack = self.packs[window]
        key = ir_backtest.run_id_of(sid, pack.id, start, end, self.step, fees, self.capital)
        return {"label": label, "role": role, "window": window, "strategy_id": sid, "key": key, "fee_multiplier": fee_multiplier,
                "args": {"doc": doc, "instruments": self.instruments, "start_ms": start, "end_ms": end, "step_minutes": self.step,
                         "fees": fees, "fees_source": fees_source, "capital_base": self.capital}}

    def execute(self, jobs: list[dict]) -> None:
        todo: dict[str, dict] = {}
        for j in jobs:
            if j["key"] not in self.done and j["key"] not in todo:
                todo[j["key"]] = j
        for j in todo.values():
            j["args"]["out_dir"] = str(self.out / "runs" / j["label"])
        cards: dict[str, dict] = {}
        if self.workers <= 1 or self.serial_reason or len(todo) <= 1:
            for k, j in todo.items():
                cards[k] = _run_job({**j["args"], "pack": self.packs[j["window"]]})
        else:
            ctx = multiprocessing.get_context("spawn")
            with cf.ProcessPoolExecutor(max_workers=min(self.workers, len(todo)), mp_context=ctx) as ex:
                futs = {k: ex.submit(_run_job, {**j["args"], "lock": self.packs[j["window"]].lock,
                                                "cache_root": str(self.packs[j["window"]].cache.root)}) for k, j in todo.items()}
                for k in todo:  # submission order: deterministic, and the first failure propagates
                    cards[k] = futs[k].result()
        for k, j in todo.items():
            self.done[k] = {"label": j["label"], "run_dir": Path(j["args"]["out_dir"]), "card": cards[k]}
        for j in jobs:
            d = self.done[j["key"]]
            card = d["card"]
            self.records.append({"label": j["label"], "role": j["role"], "window": j["window"], "strategy_id": j["strategy_id"],
                                 "run_id": card["run_id"], "fee_multiplier": j["fee_multiplier"],
                                 "run_dir": str(d["run_dir"].relative_to(self.out)),
                                 "same_as": None if d["label"] == j["label"] else d["label"],
                                 "net": card["amounts"]["equity_end"], "sharpe_annualised": card["alphakeel_metrics"].get("sharpe_annualised"),
                                 "trades_closed": card["counts"]["closed"]})

    def result(self, job: dict) -> dict:
        return self.done[job["key"]]


# ---------------------------------------------------------------------------------------------------------------------
# the battery
# ---------------------------------------------------------------------------------------------------------------------

def _window(w) -> tuple[int, int]:
    if isinstance(w, dict):
        return int(w["start_ms"]), int(w["end_ms"])
    a, b = w
    return int(a), int(b)


def _check_filter(spec: dict | None) -> list[str] | None:
    if spec is None:
        return None
    bases = spec.get("bases") if isinstance(spec, dict) else None
    if not isinstance(spec, dict) or spec.get("kind") != "excluded_bases" or not isinstance(bases, list) or not bases \
            or not all(isinstance(b, str) and b and b == IR.ascii_upper(b) for b in bases) or len(set(bases)) != len(bases):
        raise ApiError("request.invalid", 'filter must be {"kind": "excluded_bases", "bases": ["UPPER", ...]} (non-empty, distinct)',
                       field="filter")
    return sorted(bases)


def _draw_sets(pool: list[str], k: int, off_trades: list[dict], real_blocked: int, n: int, seed: int) -> tuple[list[list[str]], bool]:
    """``n`` seeded random exclusion sets of size ``k`` from ``pool``, preferring sets whose blocked-trade count in the
    unfiltered run is within max(1, 25%) of the real filter's. Returns (sets, matched)."""
    count = {}
    for t in off_trades:
        count[IR.ascii_upper(t["base"])] = count.get(IR.ascii_upper(t["base"]), 0) + 1
    tol = max(Decimal(1), TRADE_MATCH_TOLERANCE * real_blocked)
    rng = random.Random(seed)

    def ok(s):
        return abs(Decimal(sum(count.get(b, 0) for b in s) - real_blocked)) <= tol

    # whether any matched set exists: exhaustive when small, else by the draws themselves
    matched_possible = None
    if math.comb(len(pool), k) <= 20_000:
        import itertools

        matched_possible = any(ok(s) for s in itertools.combinations(pool, k))
    out, matched = [], matched_possible is not False
    for _ in range(n):
        s = sorted(rng.sample(pool, k))
        if matched:
            tries = 0
            while not ok(s) and tries < 1000:
                s = sorted(rng.sample(pool, k))
                tries += 1
            if not ok(s):
                matched = False  # the space is too large to enumerate and no match was found: fall back for all draws
        out.append(s)
    if not matched:  # redraw unconstrained from the same seed so the result does not depend on how far matching got
        rng = random.Random(seed)
        out = [sorted(rng.sample(pool, k)) for _ in range(n)]
    return out, matched


def battery(ir_doc: dict, pack: Pack, instruments: list[dict], *, is_window, oos_window=None, oos_pack: Pack | None = None,
            out_dir: str | Path, fees: dict | None = None, filter_spec: dict | None = None, permutations: int = 30, seed: int = 7,
            step_minutes: int = 5, capital_base: Decimal | str | None = None, workers: int = 2, prior_trials: int = 0,
            segments: int = SEGMENTS) -> dict:
    """Run the battery (module docstring) and write ``robustness.json``, ``robustness.md`` and ``trials.jsonl`` into
    ``out_dir``. Returns the robustness document."""
    from backtest import alphakeel_metrics as M

    loaded = IR.load(ir_doc)
    base_doc = copy.deepcopy(loaded.doc)
    base_doc.pop("strategy_id", None)
    sid = loaded.strategy_id
    if isinstance(permutations, bool) or not isinstance(permutations, int) or permutations < 0:
        raise ApiError("request.invalid", "permutations must be an integer >= 0", field="permutations")
    if isinstance(prior_trials, bool) or not isinstance(prior_trials, int) or prior_trials < 0:
        raise ApiError("request.invalid", "prior_trials must be an integer >= 0", field="prior_trials")
    if not isinstance(workers, int) or workers < 1:
        raise ApiError("request.invalid", "workers must be an integer >= 1", field="workers")
    bases_f = _check_filter(filter_spec)
    windows = {"is": _window(is_window)}
    packs = {"is": pack}
    if oos_window is not None:
        windows["oos"] = _window(oos_window)
        packs["oos"] = oos_pack or pack
        if windows["oos"][0] < windows["is"][1] and windows["oos"][1] > windows["is"][0]:
            raise ApiError("request.invalid", "the OOS window overlaps the IS window: it would not be out of sample", field="oos_window")
    sizing = base_doc["sizing"]
    capital = Decimal(str(capital_base)) if capital_base is not None else Decimal(sizing["notional_per_leg"]) * 2 * sizing["max_open"]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    R = _Runner(out, packs, instruments, windows, step_minutes, capital, workers)
    notes: list[str] = []

    # --- phase 1: everything independent ---------------------------------------------------------------------------
    base = R.job("base", "base", base_doc, fees=fees)
    jobs = [base]
    eff_fees = ir_views.default_fees() if fees is None else fees
    cost = {m: R.job(f"cost-x{m}", "cost_stress", base_doc, fees=scale_fees(eff_fees, m), fees_source=f"base x{m}", fee_multiplier=m)
            for m in COST_MULTIPLIERS}
    jobs += cost.values()
    bs = Decimal(base_doc["assumptions"]["basis_stress"])
    basis_job = None
    if bs > 0 and bs * Decimal(BASIS_MULTIPLIER) < 1:
        bdoc = copy.deepcopy(base_doc)
        bdoc["assumptions"]["basis_stress"] = IR.fmt(bs * Decimal(BASIS_MULTIPLIER))
        basis_job = R.job("basis-x2", "basis_stress", bdoc, fees=fees)
        jobs.append(basis_job)
    else:
        notes.append(f"basis_stress x{BASIS_MULTIPLIER} not run: basis_stress is {base_doc['assumptions']['basis_stress']} "
                     "(0 has no stress to double; the doubled value must stay below 1)")
    neigh: list[tuple[dict, dict]] = []
    skipped_params: list[dict] = []
    safe = re.compile(r"[^A-Za-z0-9_.-]")
    for name in sorted(base_doc["parameters"]):
        p = base_doc["parameters"][name]
        for side, v in (("lo", p["robust"][0]), ("hi", p["robust"][1])):
            if Decimal(v) == Decimal(p["value"]):
                skipped_params.append({"parameter": name, "side": side, "value": v, "reason": "equals the base value"})
                continue
            try:
                ndoc = neighbour_doc(base_doc, name, v)
            except (IR.IrError, ValueError, KeyError) as e:
                skipped_params.append({"parameter": name, "side": side, "value": v, "reason": f"not a valid IR: {e}"})
                continue
            j = R.job(f"param-{safe.sub('_', name)}-{side}", "parameter", ndoc, fees=fees)
            neigh.append(({"parameter": name, "side": side, "value": v, "path": p["path"]}, j))
            jobs.append(j)
    oos_job = R.job("oos", "oos", base_doc, window="oos", fees=fees) if "oos" in windows else None
    if oos_job:
        jobs.append(oos_job)
    f_off = f_on = None
    if bases_f is not None:
        excl = set(base_doc["universe"]["excluded_bases"])
        f_off = R.job("filter-off", "filter", with_excluded(base_doc, excl - set(bases_f)), fees=fees)
        f_on = R.job("filter-on", "filter", with_excluded(base_doc, excl | set(bases_f)), fees=fees)
        jobs += [f_off, f_on]
    R.execute(jobs)

    def net(j) -> Decimal:
        return Decimal(R.result(j)["card"]["amounts"]["equity_end"])

    def series(j):
        d = R.result(j)
        w = windows[j["window"]]
        days, eq = daily_equity(d["run_dir"], *w)
        return days, eq, _returns(days, eq, capital)

    # --- phase 2: the filter's permutation test ----------------------------------------------------------------------
    filt = None
    if bases_f is not None:
        off_tr, on_tr = _trades(R.result(f_off)["run_dir"]), _trades(R.result(f_on)["run_dir"])
        filt = filter_accounting(off_tr, on_tr, bases_f, net(f_off), net(f_on))
        real = Decimal(filt["improvement"])
        pool = sorted({IR.ascii_upper(t["base"]) for t in off_tr})
        k = len(bases_f)
        perm: dict[str, Any] = {"draws": permutations, "set_size": k, "pool": pool, "seed": seed}
        if permutations == 0:
            perm["p_value"] = None
            perm["note"] = "permutations = 0: not run"
        elif len(pool) < k:
            perm["p_value"] = None
            perm["note"] = f"only {len(pool)} bases traded in the unfiltered run: no random set of size {k}"
        else:
            sets, matched = _draw_sets(pool, k, off_tr, filt["blocked"]["count"], permutations, seed)
            excl_off = set(base_doc["universe"]["excluded_bases"]) - set(bases_f)
            pjobs = [R.job(f"perm-{i + 1:04d}", "permutation", with_excluded(base_doc, excl_off | set(s)), fees=fees) for i, s in enumerate(sets)]
            R.execute(pjobs)
            imps = [net(j) - net(f_off) for j in pjobs]
            pv = permutation_p_value(real, imps)
            srt = sorted(imps)
            perm.update({"matched_trade_count": matched, "p_value": _s(pv),
                         "random_at_least_real": sum(1 for x in imps if x >= real),
                         "random_improvement": {"min": _s(srt[0]), "median": _s(srt[len(srt) // 2]), "max": _s(srt[-1])},
                         "real_improvement": _s(real),
                         "draws_detail": [{"label": j["label"], "bases": s, "strategy_id": j["strategy_id"], "improvement": _s(x),
                                           "trades_closed": R.result(j)["card"]["counts"]["closed"]}
                                          for j, s, x in zip(pjobs, sets, imps)],
                         "formula": "p = (1 + #{random improvement >= real improvement}) / (1 + N)"})
            if not matched:
                notes.append("no random exclusion set of the filter's size blocks a comparable number of trades: the "
                             "permutation draws are unconstrained by trade count")
        filt["permutation"] = perm

    # --- statistics ---------------------------------------------------------------------------------------------------
    base_card = R.result(base)["card"]
    b_days, b_eq, b_ret = series(base)
    candidates: dict[str, dict] = {}
    for j in [base, basis_job, *[j for _m, j in neigh], f_off, f_on]:
        if j is not None and j["strategy_id"] not in candidates:
            candidates[j["strategy_id"]] = j
    n_trials = len(candidates) + prior_trials
    var_ret = {s: (series(j)[2] if s != sid else b_ret) for s, j in candidates.items()}
    var_sharpe = [x for x in (M.sharpe(r) for r in var_ret.values()) if x is not None]
    ak = M.alphakeel_metrics(b_ret, trials=n_trials, variant_sharpes=var_sharpe)
    pbo, s_blocks = None, min(PBO_MAX_BLOCKS, 2 * (len(b_ret) // 10))
    if len(var_ret) >= 4 and s_blocks >= 4:
        pbo = M.pbo_cscv(list(var_ret.values()), s_blocks)
    boot = M.stationary_bootstrap_mean_ci(b_ret, seed=seed, resamples=BOOTSTRAP_RESAMPLES)
    t_eff = None
    if len(b_ret) >= 2 and ak["effective_days"]:
        import statistics as st

        sd = st.stdev(b_ret)
        t_eff = None if sd == 0 else ak["mean_daily_return"] / (sd / math.sqrt(ak["effective_days"]))
    base_trades = _trades(R.result(base)["run_dir"])
    by_coin: dict[str, Decimal] = {}
    for t in base_trades:
        if t["status"] == "closed":
            by_coin[t["base"]] = by_coin.get(t["base"], Decimal(0)) + Decimal(t["realized"])
    closed_net = sum(by_coin.values(), Decimal(0))
    top = max(by_coin.items(), key=lambda kv: (kv[1], kv[0])) if by_coin else None
    top_share = _ratio(top[1], closed_net) if top and closed_net > 0 else None
    am = base_card["amounts"]
    b_net = Decimal(am["equity_end"])
    decomposition = {"net": am["equity_end"], "price_closed": am["price_pnl_closed"], "funding": am["funding"], "fees": _s(-Decimal(am["fees"])),
                     "open_positions_mark": _s(b_net - (Decimal(am["price_pnl_closed"]) + Decimal(am["funding"]) - Decimal(am["fees"]))),
                     "identity": "net = price_closed + funding + fees(negative) + open_positions_mark"}
    segs = segment_nets(b_days, b_eq, segments)
    months = month_nets(b_days, b_eq)
    seg_frac = _positive_fraction([s["net"] for s in segs])
    worst_seg = min(segs, key=lambda s: Decimal(s["net"])) if segs else None
    cost_doc = {m: {"net": _s(net(j)), "over_base": _ratio(net(j), b_net), "trades_closed": R.result(j)["card"]["counts"]["closed"],
                    "fees": R.result(j)["card"]["amounts"]["fees"]} for m, j in cost.items()}
    if basis_job:
        cost_doc["basis_stress_x2"] = {"net": _s(net(basis_job)), "over_base": _ratio(net(basis_job), b_net),
                                       "trades_closed": R.result(basis_job)["card"]["counts"]["closed"], "strategy_id": basis_job["strategy_id"]}
    nb = [{**m, "strategy_id": j["strategy_id"], "net": _s(net(j)), "positive": net(j) > 0,
           "sharpe_annualised": R.result(j)["card"]["alphakeel_metrics"].get("sharpe_annualised"),
           "trades_closed": R.result(j)["card"]["counts"]["closed"]} for m, j in neigh]
    worst_nb = min((Decimal(x["net"]) for x in nb), default=None)
    params_doc = {"neighbours": nb, "skipped": skipped_params, "positive_fraction": _positive_fraction([x["net"] for x in nb]),
                  "worst_net": _s(worst_nb), "worst_over_base": _ratio(worst_nb, b_net)}
    oos_doc = None
    if oos_job:
        oc = R.result(oos_job)["card"]
        o_days, _o_eq, o_ret = series(oos_job)
        o_ak = M.alphakeel_metrics(o_ret, trials=1)
        is_npd = b_net / len(b_days) if b_days else None
        oos_npd = Decimal(oc["amounts"]["equity_end"]) / len(o_days) if o_days else None
        oos_doc = {"window": {"start_ms": windows["oos"][0], "end_ms": windows["oos"][1]}, "pack_id": packs["oos"].id,
                   "net": oc["amounts"]["equity_end"], "days": len(o_days), "trades_closed": oc["counts"]["closed"],
                   "net_per_day": _s(ir_backtest.q8(oos_npd)) if oos_npd is not None else None,
                   "is_net_per_day": _s(ir_backtest.q8(is_npd)) if is_npd is not None else None,
                   "oos_over_is_net_per_day": _ratio(oos_npd, is_npd) if is_npd and is_npd > 0 else None,
                   "sharpe_annualised": o_ak["sharpe_annualised"], "is_sharpe_annualised": ak["sharpe_annualised"],
                   "max_drawdown": oc["max_drawdown"], "coverage_partial": bool(oc["data_audit"].get("coverage_partial", oc["pack"].get("accept_partial")))}
    counts = base_card["counts"]
    fu = counts.get("funding_unpriced", 0)
    fu_frac = None if (fu + counts.get("funding_events", 0)) == 0 else _ratio(Decimal(fu), Decimal(fu + counts["funding_events"]))
    all_cards = [d["card"] for d in R.done.values()]
    facts = {
        "base_net": _s(b_net), "oos_net": oos_doc["net"] if oos_doc else None, "cost_x2_net": cost_doc["2"]["net"],
        "coverage_partial": any(bool(c["data_audit"].get("coverage_partial", c["pack"].get("accept_partial"))) for c in all_cards),
        "trades_closed": counts["closed"], "days": len(b_days), "funding_unpriced_fraction": fu_frac,
        "filter_tested": bases_f is not None, "filter_p": (filt or {}).get("permutation", {}).get("p_value") if filt else None,
        "synthetic_view": base_card["view"] == ir_views.DATASET_VIEW, "dsr": ak["dsr"], "psr": ak["psr_vs_zero"], "pbo": pbo,
        "bootstrap_lower": None if boot is None else boot["ci_lower"], "segments_positive_fraction": seg_frac,
        "has_parameters": bool(base_doc["parameters"]), "neighbours_positive_fraction": params_doc["positive_fraction"],
        "top_coin_share": top_share, "oos_over_is_net_per_day": oos_doc["oos_over_is_net_per_day"] if oos_doc else None,
    }
    rating = rate(facts)

    # --- trials.jsonl and the documents --------------------------------------------------------------------------------
    counted = set(candidates)
    with open(out / "trials.jsonl", "w", encoding="utf-8") as fh:
        for i, r in enumerate(R.records):
            fh.write(json.dumps({**r, "seq": i + 1, "counted_trial": r["window"] == "is" and r["role"] != "permutation" and r["strategy_id"] in counted},
                                sort_keys=True) + "\n")
    doc = {
        "schema": SCHEMA, "strategy_id": sid, "name": base_doc["name"],
        "inputs": {"is_window": {"start_ms": windows["is"][0], "end_ms": windows["is"][1]}, "pack_id": pack.id,
                   "oos_window": oos_doc["window"] if oos_doc else None, "oos_pack_id": packs["oos"].id if oos_doc else None,
                   "fees": eff_fees, "fees_given": fees is not None, "filter": filter_spec, "permutations": permutations, "seed": seed,
                   "step_minutes": step_minutes, "capital_base": _s(capital), "workers": workers,
                   "execution": R.serial_reason or ("process pool (spawn)" if workers > 1 else "in process"), "segments": segments},
        "trials": {"count": n_trials, "distinct_candidate_variants": len(candidates), "prior_trials_declared": prior_trials,
                   "candidate_strategy_ids": sorted(candidates), "permutation_draws_not_counted": sum(1 for r in R.records if r["role"] == "permutation"),
                   "evidence": "trials.jsonl",
                   "rule": "distinct IR definitions run on the IS window as candidates (base, basis stress, parameter neighbours, "
                           "filter off/on) + declared prior trials; permutation draws are null-model draws and are not counted"},
        "runs": R.records,
        "statistics": {"alphakeel_metrics": ak, "pbo": {"value": pbo, "variants": len(var_ret), "blocks": s_blocks if pbo is not None else None,
                                                        "note": None if pbo is not None else "needs >= 4 candidate variants and >= 20 days"},
                       "bootstrap": boot, "t_stat_effective": t_eff, "trades_closed": counts["closed"], "open_at_end": counts["open_at_end"],
                       "max_drawdown": base_card["max_drawdown"], "net_decomposition": decomposition,
                       "concentration": {"net_by_coin": {k2: _s(v) for k2, v in sorted(by_coin.items())}, "closed_net": _s(closed_net),
                                         "top_coin": top[0] if top else None, "top_coin_share": top_share},
                       "funding_unpriced_fraction": fu_frac},
        "cost_stress": cost_doc, "parameters": params_doc,
        "subperiods": {"segments": segs, "positive_fraction": seg_frac, "worst": worst_seg, "months": months,
                       "months_positive_fraction": _positive_fraction([m["net"] for m in months])},
        "oos": oos_doc, "filter": filt, "rating": rating, "notes": notes, "not_tested": NOT_TESTED,
        "data_view": {"view": base_card["view"], "approximations": base_card["approximations"]},
    }
    (out / "robustness.json").write_text(json.dumps(doc, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    (out / "robustness.md").write_text(render_markdown(doc), encoding="utf-8")
    return doc


# ---------------------------------------------------------------------------------------------------------------------
# the human-readable report
# ---------------------------------------------------------------------------------------------------------------------

def _f(x, nd=4) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, bool):
        return "yes" if x else "no"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def _table(head: list[str], rows: list[list]) -> list[str]:
    return ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] + ["| " + " | ".join(_f(c) for c in r) + " |" for r in rows]


def render_markdown(doc: dict) -> str:
    r = doc["rating"]
    st = doc["statistics"]
    ak = st["alphakeel_metrics"]
    L = [f"# Robustness report: {doc['name']} ({doc['strategy_id']})", "",
         f"**Grade {r['grade']}** (score {r['passed']}/{r['applicable']} = {r['score']:.2f} -> {r['score_grade']}, then caps). "
         "This is research evidence on a synthetic-touch dataset view, not an approval; promotion is a human decision.", "",
         "## Why this grade", ""]
    L += [f"- {x}" for x in r["reasons"]] or ["- every applicable component passed and no cap applies"]
    L += ["", "## Base run (IS window)", ""]
    d = st["net_decomposition"]
    L += _table(["net", "price (closed)", "funding", "fees", "open positions mark", "closed trades", "days", "max drawdown"],
                [[d["net"], d["price_closed"], d["funding"], d["fees"], d["open_positions_mark"], st["trades_closed"], ak["days"],
                  st["max_drawdown"]["amount"]]])
    b = st["bootstrap"] or {}
    L += ["", "## Statistics (daily returns on capital " + doc["inputs"]["capital_base"] + ")", ""]
    L += _table(["statistic", "value", "note"], [
        ["Sharpe (annualised, 365)", ak["sharpe_annualised"], ""],
        ["PSR vs 0", ak["psr_vs_zero"], "probability the true Sharpe > 0"],
        ["DSR", ak["dsr"], f"trial count {doc['trials']['count']} (honest: variants actually run)"],
        ["PBO (CSCV)", st["pbo"]["value"], st["pbo"]["note"] or f"{st['pbo']['variants']} variants, {st['pbo']['blocks']} blocks"],
        ["mean daily return 95% CI", f"[{_f(b.get('ci_lower'), 6)}, {_f(b.get('ci_upper'), 6)}]", f"stationary bootstrap, block {_f(b.get('block_days'))} days"],
        ["effective days", ak["effective_days"], "Newey-West"], ["t-stat (effective n)", st["t_stat_effective"], ""],
        ["top coin share of closed net", st["concentration"]["top_coin_share"], str(st["concentration"]["top_coin"])],
    ])
    L += ["", "## Cost stress", ""]
    L += _table(["variant", "net", "net / base", "closed trades"],
                [[f"taker fee x{k}" if k in COST_MULTIPLIERS else k, v["net"], v["over_base"], v["trades_closed"]] for k, v in doc["cost_stress"].items()])
    p = doc["parameters"]
    L += ["", "## Parameter neighbours", ""]
    if p["neighbours"]:
        L += _table(["parameter", "side", "value", "net", "Sharpe", "closed trades"],
                    [[x["parameter"], x["side"], x["value"], x["net"], x["sharpe_annualised"], x["trades_closed"]] for x in p["neighbours"]])
        L += ["", f"Positive neighbours: {_f(p['positive_fraction'])}; worst / base net: {_f(p['worst_over_base'])}."]
    else:
        L += ["The IR declares no parameters with a robust range different from the value: nothing perturbed."]
    for s in p["skipped"]:
        L.append(f"- skipped {s['parameter']} {s['side']} = {s['value']}: {s['reason']}")
    sp = doc["subperiods"]
    L += ["", "## Sub-period stability", ""]
    L += _table(["from", "to", "days", "net"], [[s["from"], s["to"], s["days"], s["net"]] for s in sp["segments"]])
    L += ["", f"Positive segments: {_f(sp['positive_fraction'])}; positive months: {_f(sp['months_positive_fraction'])} of {len(sp['months'])}."]
    o = doc["oos"]
    L += ["", "## Out of sample", ""]
    if o:
        L += _table(["", "net", "net per day", "Sharpe", "closed trades"],
                    [["IS", d["net"], o["is_net_per_day"], o["is_sharpe_annualised"], st["trades_closed"]],
                     ["OOS", o["net"], o["net_per_day"], o["sharpe_annualised"], o["trades_closed"]]])
        L += ["", f"OOS / IS net per day: {_f(o['oos_over_is_net_per_day'])}."]
    else:
        L += ["No OOS window was given: the result is in-sample only (capped at C)."]
    fl = doc["filter"]
    if fl:
        pm = fl["permutation"]
        L += ["", f"## Filter test: exclude {', '.join(fl['bases'])}", ""]
        L += _table(["", "net", "trades"], [["without filter", fl["net_unfiltered"], fl["trades_unfiltered"]],
                                           ["with filter", fl["net_filtered"], fl["trades_filtered"]]])
        bl = fl["blocked"]
        L += ["", f"Improvement {fl['improvement']} = selection {fl['selection_contribution']} (price {fl['price_impact']}, funding "
              f"{fl['funding_impact']}, cost savings {fl['cost_savings']}) + knock-on added {fl['knock_on_added']['net']} "
              f"- knock-on removed {fl['knock_on_removed']['net']} + open positions {fl['residual_open_positions']}.",
              f"Blocked trades: {bl['count']} ({bl['closed']} closed), their net {bl['net']}.", "",
              f"Permutation test: {pm.get('draws')} random exclusion sets of {pm.get('set_size')} bases (full reruns); "
              f"p-value {_f(pm.get('p_value'))}; random improvements >= real: {_f(pm.get('random_at_least_real'))}. "
              + (pm.get("note") or ("Draws matched on blocked-trade count." if pm.get("matched_trade_count") else
                                    "Draws NOT matched on trade count (no comparable set)."))]
    L += ["", "## Trials", "", f"DSR trial count {doc['trials']['count']}: {doc['trials']['rule']}. Every run is listed in "
          "`trials.jsonl`.", "", "## Rule table (recompute the grade)", ""]
    L += _table(["cap", "condition", "max grade", "triggered"],
                [[c["id"], c["condition"], c["max_grade"], any(x["id"] == c["id"] for x in r["caps"])] for c in RULES["caps"]])
    L += [""]
    L += _table(["component", "pass when", "applicable", "result"],
                [[c["id"], c["pass_when"], c["applicable"], c["result"] or "-"] for c in r["components"]])
    L += ["", "Score grade: A >= 0.9, B >= 0.75, C >= 0.5, else D. " + RULES["combine"] + ".", "", "## Not tested", ""]
    L += [f"- {x}" for x in doc["not_tested"]]
    L += [f"- notes: {n}" for n in doc["notes"]]
    L += ["", "## Data view approximations", ""] + [f"- {a}" for a in doc["data_view"]["approximations"]]
    return "\n".join(L) + "\n"
