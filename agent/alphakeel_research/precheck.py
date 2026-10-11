"""Research-side precheck of AlphaKeel's G1 gate (docs/45 §3) on a ``robustness.json``.

The items, their thresholds and their order are AlphaKeel's ``validation/protocol.json`` (exported to
``contract/validation/protocol.json``, pinned by ``contract/PIN``); this module evaluates only the ``validated`` items
marked ``research_side: true``. The semantics are fixed by the shared vector ``contract/validation/vectors/g1-precheck.json``
that AlphaKeel's own verdict code passes as well: decimals compare as ``Decimal`` (never through a float round trip of a
decimal string), ratio thresholds ``{num, den}`` compare with integers, and a missing field is a FAIL with a note (never
a pass by omission). Only a statement of what the evidence shows: promotion is a human decision.

Result per item: ``{"id", "label", "result": "PASS" | "FAIL" | "N/A", "observed", "note"}``.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from . import contract
from .errors import ApiError

PROTOCOL_FILE = "validation/protocol.json"
#: the battery's own DSR recomputation may differ from the reported one by at most this (floating point only)
DSR_TOLERANCE = 1e-6
#: relative tolerance of the V[SR] floor recomputed from daily_returns (protocol v2, 1.6)
VAR_FLOOR_TOLERANCE = 1e-9
#: 1.9 (research-side addition, audit 2 P1-7): a robust range narrower than this relative half-width on either side tests nothing
MIN_ROBUST_REL_WIDTH = Decimal("0.2")
SYNTHETIC_VIEW = "kline_close_synthetic_bbo"
GRADE_ORDER = ("A", "B", "C", "D", "F")

_MISSING = object()


def protocol_sha256() -> str:
    """sha256 of the packaged protocol, verified against ``contract/PIN`` (``contract.local_pin()`` re-hashes every pinned file)."""
    pin = contract.local_pin()
    if PROTOCOL_FILE not in pin:
        raise ApiError("contract.pin_mismatch", f"{PROTOCOL_FILE} is not in this client's contract PIN")
    return pin[PROTOCOL_FILE]


def load_protocol() -> dict:
    """The packaged validation protocol; refuses a file that does not match ``contract/PIN``."""
    sha = protocol_sha256()
    path = Path(contract.__file__).parent / "contract" / PROTOCOL_FILE
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != sha:
        raise ApiError("contract.pin_mismatch", f"{PROTOCOL_FILE} does not match this client's PIN")
    return json.loads(raw.decode("utf-8"))


def research_items(protocol: dict) -> list[dict]:
    return [i for i in protocol["stages"]["validated"]["items"] if i.get("research_side") is True]


def _get(doc: Any, *path: str) -> Any:
    cur = doc
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return _MISSING
        cur = cur[k]
    return cur


def _dec(x: Any, kind: str = "any") -> Decimal | None:
    """A decimal from a JSON value of the given ``kind`` -- AlphaKeel's verdict is strict on JSON types and so is this:
    ``"str"`` a decimal string (money, written by the battery as a string), ``"num"`` a JSON number (int or float, never a
    bool or a string), ``"int"`` a JSON integer (never a bool or a float), ``"any"`` any of those (legacy callers).
    None for anything else (and for junk / non-finite values)."""
    if x is None or isinstance(x, bool):
        return None
    if kind == "str" and not isinstance(x, str):
        return None
    if kind == "num" and not isinstance(x, (int, float)):
        return None
    if kind == "int" and not isinstance(x, int):
        return None
    try:
        v = Decimal(str(x))
    except InvalidOperation:
        return None
    return v if v.is_finite() else None


def _res(ok: bool | None, observed: Any, note: str | None = None) -> tuple[str, Any, str | None]:
    return ("PASS" if ok else "FAIL"), observed, note


_KIND_WORDS = {"str": "a decimal string", "num": "a JSON number", "int": "a JSON integer"}


def _need(doc: Any, *path: str, kind: str = "any") -> tuple[Decimal | None, str | None]:
    v = _get(doc, *path)
    where = ".".join(path)
    if v is _MISSING or v is None:
        return None, f"{where} missing"
    d = _dec(v, kind)
    if d is not None:
        return d, None
    return None, f"{where} has the wrong JSON type (type: must be {_KIND_WORDS.get(kind, 'a number')})" if kind != "any" else f"{where} is not a number"


def _show(d: Decimal | None) -> str | None:
    return None if d is None else format(d.normalize(), "f")


def _ge(doc, path, thr, kind="any", observed_fmt=_show):
    d, note = _need(doc, *path, kind=kind)
    return ("FAIL", None, note) if d is None else _res(d >= Decimal(thr), observed_fmt(d))


def _gt(doc, path, thr, kind="any"):
    d, note = _need(doc, *path, kind=kind)
    return ("FAIL", None, note) if d is None else _res(d > Decimal(thr), _show(d))


def _le(doc, path, thr, kind="any"):
    d, note = _need(doc, *path, kind=kind)
    return ("FAIL", None, note) if d is None else _res(d <= Decimal(thr), _show(d))


def _dsr(rb: dict, t: dict, other: int, trials_floor: int = 0):
    from backtest import alphakeel_metrics as M

    inp = _get(rb, "statistics", "dsr_inputs")
    if not isinstance(inp, dict):
        return "FAIL", None, "statistics.dsr_inputs missing"
    try:
        s, g3, g4, te, var, trials = (inp[k] for k in ("sharpe_daily", "skewness", "kurtosis_non_excess", "effective_days", "var_sr_used", "trials"))
        reported = _get(rb, "statistics", "alphakeel_metrics", "dsr")
        if reported is _MISSING or reported is None:
            return "FAIL", None, "statistics.alphakeel_metrics.dsr missing"
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (s, g3, g4, te, var, reported)):
            return "FAIL", None, "statistics.dsr_inputs has the wrong JSON type (type: every statistic must be a JSON number)"
        if isinstance(trials, bool) or not isinstance(trials, int):
            return "FAIL", None, "statistics.dsr_inputs.trials has the wrong JSON type (type: must be a JSON integer)"
        if trials < 1:
            return "FAIL", None, "statistics.dsr_inputs.trials must be >= 1"
        own = M.psr(s, M.sr0(var, int(trials)), te, g3, g4)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return "FAIL", None, "statistics.dsr_inputs is malformed"
    if own is None or abs(own - reported) > DSR_TOLERANCE:
        return "FAIL", None, (f"dsr_inputs do not reproduce the reported DSR ({reported} vs {own}): the inputs are not trusted")
    # protocol v2: V[SR] floor from the daily returns (Mertens asymptotic variance of the Sharpe estimator) and a trial-count floor
    rets = _get(inp, "daily_returns")
    if rets is _MISSING or not isinstance(rets, list) or len(rets) < 3 or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in rets):
        return "FAIL", None, "statistics.dsr_inputs.daily_returns missing: the V[SR] floor cannot be recomputed"
    try:
        s_r, mo, te_r = M.sharpe(rets), M.moments(rets), M.effective_sample_size(rets)
        if s_r is None or mo is None or te_r <= 1:
            return "FAIL", None, "statistics.dsr_inputs.daily_returns has no variance: the V[SR] floor cannot be recomputed"
        floor_var = (1 - mo[0] * s_r + (mo[1] - 1) / 4 * s_r * s_r) / (te_r - 1)
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return "FAIL", None, "statistics.dsr_inputs.daily_returns is malformed"
    if var < floor_var * (1 - VAR_FLOOR_TOLERANCE):
        return "FAIL", None, f"var_sr below floor: var_sr_used {var} < {floor_var} recomputed from daily_returns"
    n = max(int(trials), int(trials_floor)) + other
    value = M.psr(s, M.sr0(var, n), te, g3, g4)
    if value is None:
        return "FAIL", None, "DSR is not defined for these inputs"
    return _res(Decimal(repr(value)) >= Decimal(t["min_dsr"]), round(value, 6),
                f"trials {max(int(trials), int(trials_floor))} + {other} other registered candidates (floor {int(trials_floor)})")


def _pbo(rb: dict, t: dict):
    variants = _get(rb, "statistics", "pbo", "variants")
    if variants is _MISSING or variants is None:
        return "FAIL", None, "statistics.pbo.variants missing"
    if isinstance(variants, bool) or not isinstance(variants, int):
        return "FAIL", None, "statistics.pbo.variants has the wrong JSON type (type: must be a JSON integer)"
    if variants < int(t["applies_min_variants"]):
        return "N/A", None, f"{variants} variants < {t['applies_min_variants']}: PBO is not applicable"
    return _le(rb, ("statistics", "pbo", "value"), t["max_pbo"], "num")


def narrow_ranges(rb: dict) -> list[str]:
    """Parameters whose ``robust`` range is narrower than +-MIN_ROBUST_REL_WIDTH of the value (``parameters.ranges`` of
    robustness.json; absent in older files = not checked). A zero value has no relative width: its range must just be non-empty."""
    ranges = _get(rb, "parameters", "ranges")
    if not isinstance(ranges, list):
        return []
    out = []
    for r in ranges:
        v, lo, hi = (_dec(r.get(k), "str") if isinstance(r, dict) else None for k in ("value", "lo", "hi"))
        name = r.get("name") if isinstance(r, dict) else None
        if v is None or lo is None or hi is None:
            out.append(f"{name}: malformed range")
        elif v == 0:
            if not hi > lo:
                out.append(f"{name}: empty range")
        elif (v - lo) / abs(v) < MIN_ROBUST_REL_WIDTH or (hi - v) / abs(v) < MIN_ROBUST_REL_WIDTH:
            out.append(f"{name}: [{_show(lo)}, {_show(hi)}] around {_show(v)}")
    return out


def _neighbours(rb: dict, t: dict):
    nb = _get(rb, "parameters", "neighbours")
    if not isinstance(nb, list):
        return "FAIL", None, "parameters.neighbours missing"
    if len(nb) < int(t["min_neighbours"]):
        return "FAIL", len(nb), f"fewer than {t['min_neighbours']} parameter neighbours"
    raw = [x.get("net") if isinstance(x, dict) else None for x in nb]
    if any(r is not None and not isinstance(r, str) for r in raw):
        return "FAIL", None, "a parameter neighbour net has the wrong JSON type (type: must be a decimal string)"
    nets = [_dec(r, "str") for r in raw]
    if any(n is None for n in nets):
        return "FAIL", None, "a parameter neighbour has no net"
    bad = sum(1 for n in nets if not n > Decimal(t["every_net_gt"]))
    narrow = narrow_ranges(rb)
    if narrow:
        return "FAIL", f"{len(nets) - bad} of {len(nets)} positive", (
            f"robust range too narrow (less than +-{MIN_ROBUST_REL_WIDTH * 100:.0f}% of the value on a side): {', '.join(narrow)}; "
            "the neighbour test checks nothing -- widen the range or drop the parameter claim")
    return _res(bad == 0, f"{len(nets) - bad} of {len(nets)} positive", None)


def _walk_forward(rb: dict, t: dict):
    wf = _get(rb, "walk_forward")
    if not isinstance(wf, dict) or wf.get("computable") is not True:
        reason = wf.get("reason") if isinstance(wf, dict) else None
        return "FAIL", None, f"walk-forward not computable{': ' + str(reason) if reason else ''}"
    folds = wf.get("folds")
    if not isinstance(folds, list):
        return "FAIL", None, "walk_forward.folds missing"
    days, nets = [], []
    for f in folds:
        d = _get(f, "test", "days")
        tn = _get(f, "test_net")
        if (d is not _MISSING and d is not None and (isinstance(d, bool) or not isinstance(d, int))) or \
                (tn is not _MISSING and tn is not None and not isinstance(tn, str)):
            return "FAIL", None, "a walk-forward fold has the wrong JSON type (type: test.days must be a JSON integer, test_net a decimal string)"
        n = _dec(None if tn is _MISSING else tn, "str")
        if d is _MISSING or d is None or n is None:
            return "FAIL", None, "a walk-forward fold has no test days / test net"
        days.append(d)
        nets.append(n)
    raw_eff = wf.get("efficiency")
    if raw_eff is not None and not isinstance(raw_eff, str):
        return "FAIL", None, "walk_forward.efficiency has the wrong JSON type (type: must be a decimal string or null)"
    eff = _dec(raw_eff, "str")
    positive = sum(1 for n in nets if n > 0)
    num, den = t["min_positive_folds"]["num"], t["min_positive_folds"]["den"]
    obs = f"{positive} of {len(folds)} folds positive, efficiency {_show(eff) if eff is not None else 'n/a'}"
    if len(folds) < int(t["min_folds"]):
        return "FAIL", obs, f"fewer than {t['min_folds']} folds"
    if min(days) < int(t["min_test_days"]):
        return "FAIL", obs, f"a test segment is shorter than {t['min_test_days']} days"
    if positive * den < len(folds) * num:
        return "FAIL", obs, f"fewer than {num}/{den} of the folds are positive"
    if eff is None:
        return "FAIL", obs, "efficiency is null (non-positive training net per day)"
    return _res(eff >= Decimal(t["min_efficiency"]), obs, None)


def _filter(rb: dict, t: dict):
    tested = _get(rb, "rating", "facts", "filter_tested")
    if tested is _MISSING:
        return "FAIL", None, "rating.facts.filter_tested missing"
    if tested is None or not isinstance(tested, bool):
        return "FAIL", None, "rating.facts.filter_tested has the wrong JSON type (type: must be a boolean, not null)"
    if tested is not True:
        if _get(rb, "rating", "facts", "filter_required") is True:
            return "FAIL", None, "the IR restricts its universe (excluded_bases / a narrower bases list) but its filter was not permutation-tested"
        return "N/A", None, "no filter under test in the IR"
    p = _get(rb, "rating", "facts", "filter_p")
    if p is _MISSING or p is None:
        return "FAIL", None, "the filter is in the IR but has no permutation p-value"
    return _le(rb, ("rating", "facts", "filter_p"), t["max_p"], "str")


def _grade(rb: dict, t: dict):
    g = _get(rb, "rating", "grade")
    if g not in GRADE_ORDER:
        return "FAIL", None, "rating.grade missing"
    sv = _get(rb, "rating", "facts", "synthetic_view")
    if not isinstance(sv, bool):
        return "FAIL", g, "rating.facts.synthetic_view missing: the synthetic-quote cap cannot be checked"
    view = _get(rb, "data_view", "view")
    synthetic = sv or (isinstance(view, str) and ("synthetic" in view or view == SYNTHETIC_VIEW))
    if synthetic and GRADE_ORDER.index(g) < GRADE_ORDER.index("B"):
        return "FAIL", g, "cap violated: a synthetic-quote view is capped at B"
    return _res(GRADE_ORDER.index(g) <= GRADE_ORDER.index(t["min_grade"]), g, None)


def _coverage(rb: dict, t: dict):
    v = _get(rb, "rating", "facts", "coverage_partial")
    if not isinstance(v, bool):
        return "FAIL", None, "rating.facts.coverage_partial missing"
    return _res(v is False, v, "a pack frozen with accept_partial has data gaps" if v else None)


def _item(rb: dict, item: dict, other: int, trials_floor: int = 0) -> tuple[str, Any, str | None]:
    i, t = item["id"], item["threshold"]
    facts = ("rating", "facts")
    if i == "1.1":
        return _ge(rb, (*facts, "days"), t["min_days"], "int")
    if i == "1.2":
        return _ge(rb, (*facts, "trades_closed"), t["min_trades"], "int")
    if i == "1.3":
        return _gt(rb, (*facts, "base_net"), t["net_gt"], "str")
    if i == "1.4":
        return _gt(rb, ("cost_stress", "1.5", "net"), t["net_gt"], "str")
    if i == "1.5":
        if _get(rb, "cost_stress", "basis_stress_x2") is _MISSING:
            return "FAIL", None, "basis stress x2 was not run (basis_stress 0 or too large)"
        return _gt(rb, ("cost_stress", "basis_stress_x2", "net"), t["net_gt"], "str")
    if i == "1.6":
        return _dsr(rb, t, other, trials_floor)
    if i == "1.7":
        return _pbo(rb, t)
    if i == "1.8":
        return _gt(rb, ("statistics", "bootstrap", "ci_lower"), t["lower_gt"], "num")
    if i == "1.9":
        return _neighbours(rb, t)
    if i == "1.10":
        return _le(rb, ("statistics", "concentration", "top_coin_share"), t["max_share"], "str")
    if i == "1.11":
        return _walk_forward(rb, t)
    if i == "1.12":
        return _filter(rb, t)
    if i == "1.13":
        return _grade(rb, t)
    if i == "1.16":
        return _coverage(rb, t)
    return "FAIL", None, f"item {i} is not implemented in this client (protocol newer than the client)"


def evaluate(robustness: dict | None, protocol: dict, other_candidates: int = 0, trials_floor: int = 0) -> list[dict]:
    """The research-side G1 items of ``protocol`` evaluated on ``robustness`` (``robustness.json``), in protocol order.
    ``other_candidates`` is the number of other registered candidates added to the DSR trial count (AlphaKeel's registry
    knows it; a research-side run has 0). ``trials_floor`` is the trial-count floor (IR provenance + battery candidates, or
    the research-service ledger count): the DSR uses max(dsr_inputs.trials, trials_floor) + other_candidates."""
    out = []
    for item in research_items(protocol):
        if robustness is None:
            result, observed, note = "FAIL", None, "robustness.json missing"
        else:
            result, observed, note = _item(robustness, item, int(other_candidates), int(trials_floor))
            if result == "FAIL" and not note:
                note = f"observed {observed}, threshold {json.dumps(item['threshold'], sort_keys=True)}"
        out.append({"id": item["id"], "label": item["label"], "result": result, "observed": observed, "note": note})
    return out
