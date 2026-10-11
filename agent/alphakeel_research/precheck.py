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
import re
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

#: Numeric literals that need no declaration (G1 1.19, protocol 2.1; AlphaKeel ``strategy_ir::ir::EXEMPT_LITERALS``):
#: 0 and 1 are structural constants (a zero threshold, a boolean), 24 / 60 / 1000 / 3600 are time-unit conversions
#: (hours per day, minutes per hour, milliseconds per second, seconds per hour) -- none of them is a tuned number.
EXEMPT_LITERALS = (0, 1, 24, 60, 1000, 3600)
#: Survivorship annotation of a data lock, best to worst (AlphaKeel ``SURVIVORSHIP_ORDER``); unknown values rank worst.
SURVIVORSHIP_ORDER = ("includes_delisted", "includes_delisted_from_archive", "partial_from_archive", "survivors_before_first_seen",
                      "survivors_only_unobserved")
#: G1 grade cap when the research universe does not exclude survivor bias (protocol 2.1 copies only; 2.2 fails instead).
UNIVERSE_GRADE_CAP = "C"

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


def _cost_net(rb: dict, t: dict, path: tuple[str, ...]):
    """1.3-1.5: the local (one-level-fill) net > net_gt and, from protocol 2.2 (``service_net_gt``), the research service's
    conservative net -- adjusted by (stress net - base net) for 1.4 / 1.5 -- > service_net_gt. The service net is
    ``service_runs.service_conservative_net`` (AlphaKeel checks it against the service's own run records)."""
    res, obs, note = _gt(rb, path, t["net_gt"], "str")
    if "service_net_gt" not in t or (res == "FAIL" and obs is None):  # no service gate, or the local figure is missing / mistyped
        return res, obs, note
    local, base = _dec(_get(rb, *path), "str"), _dec(_get(rb, "rating", "facts", "base_net"), "str")
    cons = _dec(_get(rb, "service_runs", "service_conservative_net"), "str")
    if local is None or base is None or cons is None:
        return "FAIL", obs, ("service-side conservative net unavailable (not registered, not verified against the research service, or "
                             "base_net missing): the local one-level-fill figure alone does not count")
    adj = cons + (local - base)
    ok = local > Decimal(t["net_gt"]) and adj > Decimal(t["service_net_gt"])
    return _res(ok, {"local": _show(local), "service_conservative_adjusted": _show(adj)}, None if ok else
                f"local net {_show(local)}, service-side conservative net (adjusted) {_show(adj)}")


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


def _filter(rb: dict, t: dict, required_by_ir: bool = False):
    tested = _get(rb, "rating", "facts", "filter_tested")
    if tested is _MISSING:
        return "FAIL", None, "rating.facts.filter_tested missing"
    if tested is None or not isinstance(tested, bool):
        return "FAIL", None, "rating.facts.filter_tested has the wrong JSON type (type: must be a boolean, not null)"
    if tested is not True:
        if required_by_ir or _get(rb, "rating", "facts", "filter_required") is True:
            return "FAIL", None, "the IR restricts its universe (excluded_bases / a narrower bases list) but its filter was not permutation-tested"
        return "N/A", None, "no filter under test in the IR"
    p = _get(rb, "rating", "facts", "filter_p")
    if p is _MISSING or p is None:
        return "FAIL", None, "the filter is in the IR but has no permutation p-value"
    return _le(rb, ("rating", "facts", "filter_p"), t["max_p"], "str")


def _grade(rb: dict, t: dict, ak_synthetic: bool | None = None):
    g = _get(rb, "rating", "grade")
    if g not in GRADE_ORDER:
        return "FAIL", None, "rating.grade missing"
    sv = _get(rb, "rating", "facts", "synthetic_view")
    if not isinstance(sv, bool):
        return "FAIL", g, "rating.facts.synthetic_view missing: the synthetic-quote cap cannot be checked"
    view = _get(rb, "data_view", "view")
    synthetic = sv or (isinstance(view, str) and ("synthetic" in view or view == SYNTHETIC_VIEW)) or ak_synthetic is True
    if synthetic and GRADE_ORDER.index(g) < GRADE_ORDER.index("B"):
        return "FAIL", g, "cap violated: a synthetic-quote view is capped at B"
    return _res(GRADE_ORDER.index(g) <= GRADE_ORDER.index(t["min_grade"]), g, None)


def _coverage(rb: dict, t: dict):
    v = _get(rb, "rating", "facts", "coverage_partial")
    if not isinstance(v, bool):
        return "FAIL", None, "rating.facts.coverage_partial missing"
    return _res(v is False, v, "a pack frozen with accept_partial has data gaps" if v else None)


def _item(rb: dict, item: dict, other: int, trials_floor: int = 0, filter_required_by_ir: bool = False,
          ak_synthetic: bool | None = None) -> tuple[str, Any, str | None]:
    i, t = item["id"], item["threshold"]
    facts = ("rating", "facts")
    if i == "1.1":
        return _ge(rb, (*facts, "days"), t["min_days"], "int")
    if i == "1.2":
        return _ge(rb, (*facts, "trades_closed"), t["min_trades"], "int")
    if i == "1.3":
        return _cost_net(rb, t, (*facts, "base_net"))
    if i == "1.4":
        return _cost_net(rb, t, ("cost_stress", "1.5", "net"))
    if i == "1.5":
        if _get(rb, "cost_stress", "basis_stress_x2") is _MISSING:
            return "FAIL", None, "basis stress x2 was not run (basis_stress 0 or too large)"
        return _cost_net(rb, t, ("cost_stress", "basis_stress_x2", "net"))
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
        return _filter(rb, t, filter_required_by_ir and t.get("ir_restrictions") == "forced")
    if i == "1.13":
        return _grade(rb, t, ak_synthetic if t.get("synthetic_source") == "data_lock" else None)
    if i == "1.16":
        return _coverage(rb, t)
    return "FAIL", None, f"item {i} is not implemented in this client (protocol newer than the client)"


# --------------------------------------------------------------------------------------------------------------------
# protocol 2.1: AlphaKeel-side items on the IR and on the universe facts of the IS pack (1.9 width, 1.18, 1.19)
# --------------------------------------------------------------------------------------------------------------------

def survivorship_rank(s: Any) -> int:
    return SURVIVORSHIP_ORDER.index(s) if s in SURVIVORSHIP_ORDER else len(SURVIVORSHIP_ORDER)


def worse_survivorship(a: str, b: str) -> str:
    return b if survivorship_rank(b) > survivorship_rank(a) else a


def numeric_literals(doc: dict) -> list[tuple[str, Decimal]]:
    """Every numeric literal of the entry / exit expressions, in document order (AlphaKeel ``numeric_literals``): the condition
    values (a ``between`` range contributes ``value[0]`` and ``value[1]``), ``entry.candidate_limit``, ``entry.cooldown_minutes``,
    ``exit.max_hold_hours``, ``exit.max_loss`` and ``exit.take_profit`` (when not null). Paths use the ``parameters.*.path`` grammar.
    ``sizing`` and ``assumptions`` are cost / capital assumptions, not entry or exit expressions."""
    out: list[tuple[str, Decimal]] = []

    def conds(prefix: str, cs: list) -> None:
        for i, c in enumerate(cs):
            v = c["value"]
            if isinstance(v, list):
                out.append((f"{prefix}.conditions[{i}].value[0]", Decimal(v[0])))
                out.append((f"{prefix}.conditions[{i}].value[1]", Decimal(v[1])))
            else:
                out.append((f"{prefix}.conditions[{i}].value", Decimal(v)))

    e, x = doc["entry"], doc["exit"]
    conds("entry", e["conditions"])
    out.append(("entry.candidate_limit", Decimal(e["candidate_limit"])))
    out.append(("entry.cooldown_minutes", Decimal(e["cooldown_minutes"])))
    conds("exit", x["conditions"])
    out.append(("exit.max_hold_hours", Decimal(x["max_hold_hours"])))
    out.append(("exit.max_loss", Decimal(x["max_loss"])))
    if x.get("take_profit") is not None:
        out.append(("exit.take_profit", Decimal(x["take_profit"])))
    return out


def _zero_scale(doc: dict, path: str) -> Decimal | None:
    """AlphaKeel's judging scale for a parameter whose value is 0 (the "allowed range width"); None = unknown position."""
    m = re.fullmatch(r"(entry|exit)\.conditions\[([0-9]+)\]\.value(\[[01]\])?", path)
    if m:
        try:
            feature = doc[m.group(1)]["conditions"][int(m.group(2))]["feature"]
        except (KeyError, IndexError, TypeError):
            return None
        table = {"funding_hourly_net": "0.001", "funding_apr": "1", "funding": "0.01", "basis": "0.01", "spread": "0.01", "fees": "0.01",
                 "stress": "0.01", "net_expected": "0.01", "net_conservative": "0.01", "volume_min": "1000000", "quote_age_ms": "60000",
                 "leg_skew_ms": "60000", "price_estimated": "1"}
        return Decimal(table[feature]) if feature in table else None
    return {"entry.cooldown_minutes": Decimal("1440"), "assumptions.basis_stress": Decimal("0.01")}.get(path)


def robust_width_problems(doc: dict, per_side: bool = True) -> list[str]:
    """1.9, AlphaKeel's own check of every parameter's ``robust`` interval: each side deviates >= 20% of |value| (protocol 2.2;
    ``per_side=False`` is the 2.1 total-width rule ``(hi - lo) / (2 |value|) >= 0.2``); for value 0,
    ``hi - lo >= 0.2 x`` the allowed range width of the position. Empty = all fine."""
    bad: list[str] = []
    params = doc.get("parameters")
    if not isinstance(params, dict):
        return bad
    for name in sorted(params):
        p = params[name]
        robust = p.get("robust") if isinstance(p.get("robust"), list) and len(p["robust"]) == 2 else [None, None]
        v, lo, hi = _dec(p.get("value"), "str"), _dec(robust[0], "str"), _dec(robust[1], "str")
        if v is None or lo is None or hi is None:
            bad.append(f"{name}: value/robust are not decimal strings")
            continue
        width = hi - lo
        if v == 0:
            scale = _zero_scale(doc, str(p.get("path", "")))
            if scale is None:
                bad.append(f"{name}: value 0 at {p.get('path')}: no allowed range width is known to judge the robust interval")
            elif width < MIN_ROBUST_REL_WIDTH * scale:
                bad.append(f"{name}: value 0, robust width {_show(width)} below 0.2 x the allowed range width {_show(scale)}")
        elif per_side:
            need = MIN_ROBUST_REL_WIDTH * abs(v)
            if v - lo < need or hi - v < need:
                bad.append(f"{name}: robust [{_show(lo)}, {_show(hi)}] around {_show(v)} deviates by less than 20% of the value on a side "
                           f"(low side {_show((v - lo) / abs(v))}, high side {_show((hi - v) / abs(v))})")
        elif width < MIN_ROBUST_REL_WIDTH * 2 * abs(v):
            bad.append(f"{name}: robust [{_show(lo)}, {_show(hi)}] around {_show(v)} is narrower than +-20% (relative half width {_show(width / (2 * abs(v)))})")
    return bad


def literal_exempt_at(doc: dict, path: str, value: Decimal) -> bool:
    """1.19 (protocol 2.2; AlphaKeel ``strategy_ir::ir::literal_exempt_at``): exemption by *position*, not by value: a condition
    comparison value of 0, the comparison value 1 of a ``price_estimated`` condition, ``entry.cooldown_minutes`` 0."""
    if path == "entry.cooldown_minutes":
        return value == 0
    m = re.fullmatch(r"(entry|exit)\.conditions\[([0-9]+)\](\.value(?:\[[01]\])?)", path)
    if not m:
        return False
    try:
        feature = doc[m.group(1)]["conditions"][int(m.group(2))]["feature"]
    except (KeyError, IndexError, TypeError):
        return False
    return value == 0 or (value == 1 and m.group(3) == ".value" and feature == "price_estimated")


def literal_report(doc: dict, by_position: bool = True) -> dict:
    """1.19: classify every entry / exit numeric literal as declared (a ``parameters.*.path``), listed in
    ``provenance.fixed_literals`` with an equal value, exempt (:data:`EXEMPT_LITERALS`) or undeclared; also report listed
    values that differ and listed paths that point at no literal."""
    literals = numeric_literals(doc)
    fixed = (doc.get("provenance") or {}).get("fixed_literals")
    fixed = fixed if isinstance(fixed, dict) else {}
    declared_paths = {p.get("path") for p in (doc.get("parameters") or {}).values()}
    rep = {"total": len(literals), "declared": 0, "exempt": 0, "fixed": 0, "undeclared": [], "mismatched": [], "stale": []}
    for path, value in literals:
        if path in declared_paths:
            rep["declared"] += 1
        elif path in fixed:
            listed = fixed[path]
            if _dec(listed, "any") == value:
                rep["fixed"] += 1
            else:
                rep["mismatched"].append(f"{path}: listed {json.dumps(listed)}, IR has {value}")
        elif literal_exempt_at(doc, path, value) if by_position else any(value == Decimal(x) for x in EXEMPT_LITERALS):
            rep["exempt"] += 1
        else:
            rep["undeclared"].append(f"{path} = {value}")
    paths = {p for p, _ in literals}
    rep["stale"] = [k for k in fixed if k not in paths]
    return rep


def universe_verdict(pu: dict | None, pack_id: str | None = None, strict: bool = True) -> tuple[str, Any, str | None, str | None]:
    """1.18 on the universe facts of the robustness IS pack (``{"selection", "top_n", "survivorship"}``; None = unknown):
    ``(result, observed, note, grade_cap)``. screener_live -> PASS; pit_membership with includes_delisted* -> PASS;
    pit_membership with survivors_* or declared with includes_delisted* -> PASS with grade cap C; declared with survivors_* -> FAIL;
    a missing annotation (old pack) -> FAIL, re-pack."""
    if pu is None:
        return "FAIL", {"pack_id": pack_id}, "universe annotation unavailable: no data lock for the IS pack, or it was not reachable (需重新打包)", None
    sel, surv = pu.get("selection"), pu.get("survivorship")
    obs = {"pack_id": pack_id, "selection": sel, "top_n": pu.get("top_n"), "survivorship": surv}
    if sel is None:
        return "FAIL", obs, "the IS pack's data lock has no universe.selection (built before the survivorship annotation): 需重新打包", None
    if sel == "screener_live":
        return "PASS", obs, "scan pack: the universe is what the screener saw listed at scan time", None
    if surv is None:
        return "FAIL", obs, "the IS pack's data lock has no universe.survivorship: 需重新打包", None
    delisted = survivorship_rank(surv) < 2
    if strict:  # protocol 2.2: a survivor side fails outright; only a pit_membership pack that includes delisted contracts passes
        if (sel, delisted) == ("pit_membership", True):
            return "PASS", obs, None, None
        if sel == "pit_membership":
            return "FAIL", obs, (f"the evidence universe does not exclude survivor bias: survivorship {surv} (survivors_* / partial_from_archive: the "
                                 "point-in-time membership or the archive of delisted contracts is incomplete for this window); rebuild after the data is repaired, or use a scan pack"), None
        if sel == "declared":
            return "FAIL", obs, f"declared universe (survivorship {surv}): the instrument list is the caller's own, not point in time; rebuild the pack with selection pit_membership", None
        return "FAIL", obs, f"unknown universe.selection {sel!r}", None
    if (sel, delisted) == ("pit_membership", True):
        return "PASS", obs, None, None
    if (sel, delisted) in (("pit_membership", False), ("declared", True)):
        return "PASS", obs, f"G1 grade capped at {UNIVERSE_GRADE_CAP}: selection {sel} with survivorship {surv} does not exclude survivor bias", UNIVERSE_GRADE_CAP
    if (sel, delisted) == ("declared", False):
        return "FAIL", obs, f"declared universe with survivorship {surv}: the evidence universe is today's survivors; rebuild the pack with selection pit_membership", None
    return "FAIL", obs, f"unknown universe.selection {sel!r}", None


def ir_items(doc: dict, pack_universe: dict | None, protocol: dict, pack_id: str | None = None) -> dict:
    """The AlphaKeel-side items that need the IR / the IS pack's universe: ``{"items": [1.18, 1.19 results the protocol has],
    "robust_width_problems": [...], "grade_cap": "C" | None}``. Protocol copies without 1.18 / 1.19 (before 2.1) yield no items."""
    have = {i["id"]: i for i in protocol["stages"]["validated"]["items"]}
    items, cap = [], None
    if "1.18" in have:
        result, observed, note, cap = universe_verdict(pack_universe, pack_id, "grade_cap" not in have["1.18"]["threshold"])
        items.append({"id": "1.18", "label": have["1.18"]["label"], "result": result, "observed": observed, "note": note})
    if "1.19" in have:
        try:
            r = literal_report(doc, "exempt" in have["1.19"]["threshold"] and "position" in str(have["1.19"]["threshold"]["exempt"]))
            ok = not (r["undeclared"] or r["mismatched"] or r["stale"])
            why = "; ".join(x for x in (
                f"undeclared literal(s): {'; '.join(r['undeclared'])}" if r["undeclared"] else "",
                f"fixed_literals value differs from the IR: {'; '.join(r['mismatched'])}" if r["mismatched"] else "",
                f"fixed_literals path(s) point at no literal: {', '.join(r['stale'])}" if r["stale"] else "") if x)
            items.append({"id": "1.19", "label": have["1.19"]["label"], "result": "PASS" if ok else "FAIL", "observed": r, "note": why or None})
        except (KeyError, TypeError, ValueError, InvalidOperation) as e:
            items.append({"id": "1.19", "label": have["1.19"]["label"], "result": "FAIL", "observed": None,
                          "note": f"the IR cannot be read for the literal scan: {e}"})
    side = "min_side_rel_width" in next((i["threshold"] for i in protocol["stages"]["validated"]["items"] if i["id"] == "1.9"), {})
    return {"items": items, "robust_width_problems": robust_width_problems(doc, side), "grade_cap": cap}


_UNSET: Any = object()


def ir_filter_required(ir: dict, traded: set[str]) -> bool:
    """1.12 (protocol 2.2, AlphaKeel ``ir_filter_required``): the IR restricts its universe -- ``universe.excluded_bases`` is not
    empty, or ``universe.bases`` is an allow-list and the battery traded a coin outside it -- so the permutation test is mandatory."""
    u = ir.get("universe") or {}
    if u.get("excluded_bases"):
        return True
    allow = u.get("bases")
    if isinstance(allow, list):
        allowed = {str(x).upper() for x in allow}
        return any(c.upper() not in allowed for c in traded)
    return False


def pack_is_synthetic(pu: dict | None) -> bool:
    """1.13 (AlphaKeel ``pack_is_synthetic``): the IS pack is a dataset pack (synthetic quotes) unless its data lock says
    ``screener_live``; an unreachable data lock counts as synthetic."""
    if pu is None:
        return True
    dp = pu.get("dataset_pack")
    return bool(dp) if isinstance(dp, bool) else pu.get("selection") != "screener_live"


def evaluate(robustness: dict | None, protocol: dict, other_candidates: int = 0, trials_floor: int = 0, *,
             ir: dict | None = None, pack_universe: Any = _UNSET, filter_required_by_ir: bool | None = None,
             ak_synthetic: bool | None = None) -> list[dict]:
    """The research-side G1 items of ``protocol`` evaluated on ``robustness`` (``robustness.json``), in protocol order.
    ``other_candidates`` is the number of other registered candidates added to the DSR trial count (AlphaKeel's registry
    knows it; a research-side run has 0). ``trials_floor`` is the trial-count floor (IR provenance + battery candidates, or
    the research-service ledger count): the DSR uses max(dsr_inputs.trials, trials_floor) + other_candidates."""
    out = []
    if ir is not None and isinstance(robustness, dict):  # AK derives these from the IR and the IS pack's data lock, not from the battery's own flags
        if filter_required_by_ir is None:
            traded = set((_get(robustness, "statistics", "concentration", "net_by_coin") or {}) if isinstance(_get(robustness, "statistics", "concentration", "net_by_coin"), dict) else ())
            filter_required_by_ir = ir_filter_required(ir, traded)
        if ak_synthetic is None and pack_universe is not _UNSET:
            ak_synthetic = pack_is_synthetic(pack_universe)
    for item in research_items(protocol):
        if robustness is None:
            result, observed, note = "FAIL", None, "robustness.json missing"
        else:
            result, observed, note = _item(robustness, item, int(other_candidates), int(trials_floor), bool(filter_required_by_ir), ak_synthetic)
            if result == "FAIL" and not note:
                note = f"observed {observed}, threshold {json.dumps(item['threshold'], sort_keys=True)}"
        out.append({"id": item["id"], "label": item["label"], "result": result, "observed": observed, "note": note})
    if ir is not None:
        # protocol 2.1, AlphaKeel-side: 1.9 also checks the robust widths of the IR itself, 1.13 takes the universe cap,
        # 1.18 / 1.19 are appended (they are not research_side items).
        extra = ir_items(ir, None if pack_universe is _UNSET else pack_universe, protocol,
                         (robustness or {}).get("inputs", {}).get("pack_id") if isinstance(robustness, dict) else None)
        for o in out:
            if o["id"] == "1.9" and extra["robust_width_problems"]:
                o["result"] = "FAIL"
                msg = "robust interval too narrow (AK check): " + "; ".join(extra["robust_width_problems"])
                o["note"] = f"{o['note']}; {msg}" if o["note"] else msg
            if o["id"] == "1.13" and extra["grade_cap"] and isinstance(robustness, dict):
                g = _get(robustness, "rating", "grade")
                over = g in GRADE_ORDER and GRADE_ORDER.index(g) < GRADE_ORDER.index(extra["grade_cap"])
                msg = (f"cap violated: the research universe (1.18) caps the G1 grade at {extra['grade_cap']}" if over
                       else f"grade capped at {extra['grade_cap']} by the research universe (1.18)")
                if over:
                    o["result"] = "FAIL"
                o["note"] = f"{o['note']}; {msg}" if o["note"] else msg
        out.extend(extra["items"])
    return out
