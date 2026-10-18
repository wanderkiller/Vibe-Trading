"""Strategy IR v1 (``alphakeel.strategy-ir/1``): load, validate, ``strategy_id``, features and the decision function.

The contract is ``contract/strategy-ir/README.md`` (pinned copy of AlphaKeel's docs/contracts/strategy-ir); the shared
gold vectors next to it are run by tests/test_alphakeel_ir.py. This is an independent implementation of that text, not a
translation of the Rust crate or of the vector generator.

Standard library + ``decimal`` only: this module is imported inside the policy sandbox (``ir_policy/strategy.py``).

Documents and views are plain JSON values (dicts, lists, strings, ints, bools, None). Decimal inputs are decimal strings;
outputs use the canonical decimal spelling (no exponent, no trailing zeros, ``0`` instead of ``-0``). JSON floats fail
the shape check everywhere except inside ``provenance``, which is free content outside the definition (README §1).
"""

from __future__ import annotations

import copy
import json
import re
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from typing import Any

from . import canon
from .errors import ApiError

SCHEMA = "alphakeel.strategy-ir/1"
KIND = "cross_venue_carry"
VENUES = ("binance", "okx", "bybit", "bitget", "gate", "aster", "hyperliquid", "lighter", "backpack", "ibkr")
FEATURES = ("funding_hourly_net", "funding_apr", "funding", "basis", "spread", "fees", "stress", "net_expected",
            "net_conservative", "volume_min", "quote_age_ms", "leg_skew_ms", "price_estimated",
            "funding_apr_7d", "funding_apr_30d", "funding_pos_share_30d", "borrow_apr", "borrow_utilization",
            "borrow_cap_used", "entry_edge", "listing_age_hours")
OPS = (">=", "<=", ">", "<", "between")
SHAPES = ("cross_perp", "spot_perp", "equity_perp")
IBKR = "ibkr"  # the equity leg's venue name; only equity_perp pairs may name it (README §1, §3)
FUNDING_ESTIMATES = ("predicted", "last_settled")
U32_MAX = 4294967295
MAX_CANDIDATE_LIMIT = 200
MAX_OPEN_LIMIT = 50
HOUR_MS = 3_600_000
MINUTE_MS = 60_000

# Error codes (README §3 lists the rules, not code strings; these follow the rule groups, one code per group).
E_SHAPE = "ir.shape"                      # §3.1 JSON/shape: syntax, unknown field/feature/operator/venue, types, decimal spelling
E_SCHEMA = "ir.schema"                    # §3.2 schema constant
E_KIND = "ir.kind"                        # §3.2 kind
E_INVALID = "ir.invalid"                  # §3.3, §3.4, §3.6 ranges, pair/shape, upper case, conditions, provenance
E_PARAMETER_PATH = "ir.parameter_path"    # §3.5 path does not resolve to a number
E_PARAMETER_MISMATCH = "ir.parameter_mismatch"  # §3.5 value differs from the literal at path
E_STRATEGY_ID = "ir.strategy_id_mismatch"  # §3.7 declared id differs from the computed one

_DEC_RE = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]*[1-9])?$")
_PATH_RE = re.compile(r"^(entry|exit|sizing|assumptions)(\[[0-9]+\])*(\.[a-z_]+(\[[0-9]+\])*)*$")
_SEG_RE = re.compile(r"([a-z_]+)((?:\[[0-9]+\])*)")
# Exact enough for every v1 formula (products and quotients of quotes, rates and fees); final values are rounded to
# at most 10 places, and division results are carried with this many significant digits.
_CTX = Context(prec=60, rounding=ROUND_HALF_EVEN, Emax=999999, Emin=-999999,
               traps=[InvalidOperation])


class IrError(ApiError):
    """An IR document that breaks README §1–§3. ``field`` is the JSON path of the offending value when known."""

    def __init__(self, code: str, message: str, *, field: str | None = None):
        super().__init__(code, message, field=field)


class PairInvalid(ValueError):
    """README §4.1 "组合不合法": the whole pair is refused (``code`` is ``quote_ccy_mismatch`` or ``shape_mismatch``)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------------------------------------------------------------
# decimals
# ---------------------------------------------------------------------------------------------------------------------

def is_canonical_decimal(s: Any) -> bool:
    return isinstance(s, str) and s != "-0" and _DEC_RE.match(s) is not None


def fmt(d: Decimal) -> str:
    """Canonical decimal text: no exponent, no trailing zeros, ``0`` for any zero."""
    if d.is_zero():
        return "0"
    with localcontext(_CTX):
        n = d.normalize()
    s = format(n, "f")
    return s


def _dec(v: Any, what: str) -> Decimal:
    """A view number: decimal string (or int / Decimal); floats and other spellings are refused."""
    if isinstance(v, bool) or v is None or isinstance(v, float):
        raise ValueError(f"{what}: not a decimal: {v!r}")
    if isinstance(v, Decimal):
        if not v.is_finite():
            raise ValueError(f"{what}: not finite")
        return v
    if isinstance(v, int):
        return Decimal(v)
    if isinstance(v, str):
        try:
            d = Decimal(v)
        except InvalidOperation:
            raise ValueError(f"{what}: not a decimal: {v!r}") from None
        if not d.is_finite():
            raise ValueError(f"{what}: not finite")
        return d
    raise ValueError(f"{what}: not a decimal: {v!r}")


def _opt_dec(v: Any, what: str) -> Decimal | None:
    return None if v is None else _dec(v, what)


def _opt_int(v: Any, what: str) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"{what}: not an integer: {v!r}")
    return v


def _round(d: Decimal, places: int) -> Decimal:
    with localcontext(_CTX):
        return d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)


# ---------------------------------------------------------------------------------------------------------------------
# loading and shape (§1, §3.1)
# ---------------------------------------------------------------------------------------------------------------------

def _no_dupes(pairs: list[tuple[str, Any]]) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise IrError(E_SHAPE, f"duplicate key {k!r}")
        out[k] = v
    return out


def _no_const(s: str) -> Any:
    raise IrError(E_SHAPE, f"{s} is not JSON")


def _parse_text(text: str | bytes) -> Any:
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    try:
        return json.loads(text, object_pairs_hook=_no_dupes, parse_constant=_no_const)
    except json.JSONDecodeError as e:
        raise IrError(E_SHAPE, f"not JSON: {e}") from None


def _shape(msg: str, path: str) -> IrError:
    return IrError(E_SHAPE, f"{path}: {msg}", field=path)


class _Sink:
    """Where checks report. ``collect=False`` (:func:`_check`, pinned to the Rust validator): the first problem raises.
    ``collect=True`` (:func:`problems`): every problem is recorded and the caller goes on with the next independent check."""

    def __init__(self, collect: bool):
        self.collect = collect
        self.items: list[IrError] = []

    def ok(self, fn: Any, *args: Any) -> bool:
        """Run one check; False (collect mode) when it found problems. An :func:`_obj` error carries all of its fields."""
        try:
            fn(*args)
        except IrError as e:
            if not self.collect:
                raise
            self.items.extend(getattr(e, "all", None) or [e])
            return False
        return True

    def clean(self, fn: Any, *args: Any) -> bool:
        """Run a section check that reports through this sink; True when it added no problem."""
        n = len(self.items)
        fn(*args, self)
        return len(self.items) == n


def _obj(v: Any, path: str, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> dict:
    """An object with exactly these fields. Raises the first unknown (else missing) field; ``.all`` lists every one."""
    if not isinstance(v, dict):
        raise _shape("must be an object", path)
    found = [_shape(f"unknown field {k!r}", f"{path}.{k}" if path else k) for k in v if k not in required and k not in optional]
    found += [_shape("missing field", f"{path}.{k}" if path else k) for k in required if k not in v]
    if found:
        found[0].all = found
        raise found[0]
    return v


def _s_str(v: Any, path: str) -> None:
    if not isinstance(v, str):
        raise _shape("must be a string", path)


def _s_u32(v: Any, path: str) -> None:
    if isinstance(v, bool) or not isinstance(v, int) or not (0 <= v <= U32_MAX):
        raise _shape("must be an integer in 0..=4294967295", path)


def _s_dec(v: Any, path: str) -> None:
    if not is_canonical_decimal(v):
        raise _shape(f"must be a canonical decimal string, got {v!r}", path)


def _s_dec_or_null(v: Any, path: str) -> None:
    if v is not None:
        _s_dec(v, path)


def _s_bool(v: Any, path: str) -> None:
    if not isinstance(v, bool):
        raise _shape("must be a boolean", path)


def _s_enum(v: Any, allowed: tuple[str, ...], path: str) -> None:
    if not isinstance(v, str) or v not in allowed:
        raise _shape(f"unknown value {v!r}", path)


def _s_strings(v: Any, path: str) -> None:
    if not isinstance(v, list):
        raise _shape("must be a list of strings", path)
    for i, x in enumerate(v):
        _s_str(x, f"{path}[{i}]")


def _s_pairs(pairs: Any, path: str) -> None:
    if pairs != "any":
        if not isinstance(pairs, list):
            raise _shape('must be "any" or a list of [long_venue, short_venue]', path)
        for i, pr in enumerate(pairs):
            if not isinstance(pr, list) or len(pr) != 2:
                raise _shape("must be [long_venue, short_venue]", f"{path}[{i}]")
            for j, x in enumerate(pr):
                _s_enum(x, VENUES, f"{path}[{i}][{j}]")


def _s_robust(r: Any, path: str) -> None:
    if not isinstance(r, list) or len(r) != 2:
        raise _shape("must be [lo, hi]", path)
    for j, v in enumerate(r):
        _s_dec(v, f"{path}[{j}]")


def _s_object(v: Any, path: str) -> None:
    if not isinstance(v, dict):
        raise _shape("must be an object", path)


def _s_conditions(v: Any, path: str) -> None:
    if not isinstance(v, list):
        raise _shape("must be a list", path)
    for i, c in enumerate(v):
        p = f"{path}[{i}]"
        _obj(c, p, ("feature", "op", "value"), ("min_hold_hours",))
        _s_enum(c["feature"], FEATURES, f"{p}.feature")
        _s_enum(c["op"], OPS, f"{p}.op")
        val = c["value"]
        if isinstance(val, list):
            if len(val) != 2:
                raise _shape("an interval is [lo, hi]", f"{p}.value")
            for j, x in enumerate(val):
                _s_dec(x, f"{p}.value[{j}]")
        else:
            _s_dec(val, f"{p}.value")
        if "min_hold_hours" in c:
            _s_u32(c["min_hold_hours"], f"{p}.min_hold_hours")


MARGIN_MODES = ("portfolio",)


def _s_margin(v: Any, path: str) -> None:
    """README §1 ``sizing.margin`` (optional): ``{mode: "portfolio", reduce_above, reduce_to}``, decimal strings."""
    _obj(v, path, ("mode", "reduce_above", "reduce_to"))
    _s_enum(v["mode"], MARGIN_MODES, f"{path}.mode")
    _s_dec(v["reduce_above"], f"{path}.reduce_above")
    _s_dec(v["reduce_to"], f"{path}.reduce_to")


# Per object: required fields, optional fields, and the check of each field in the order the first-error path runs them.
_SECTIONS: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[tuple[str, Any, tuple], ...]]] = {
    "universe": (("pairs", "shape", "excluded_bases"), ("quote_ccys", "bases"), (
        ("pairs", _s_pairs, ()), ("shape", _s_enum, (SHAPES,)), ("excluded_bases", _s_strings, ()),
        ("quote_ccys", _s_strings, ()), ("bases", _s_strings, ()))),
    "assumptions": (("horizon_hours", "basis_stress", "funding_estimate"), (), (
        ("horizon_hours", _s_u32, ()), ("basis_stress", _s_dec, ()), ("funding_estimate", _s_enum, (FUNDING_ESTIMATES,)))),
    "entry": (("conditions", "rank_by", "candidate_limit", "cooldown_minutes", "one_per_base"), (), (
        ("conditions", _s_conditions, ()), ("rank_by", _s_enum, (FEATURES,)), ("candidate_limit", _s_u32, ()),
        ("cooldown_minutes", _s_u32, ()), ("one_per_base", _s_bool, ()))),
    "exit": (("max_hold_hours", "max_loss", "take_profit", "conditions"), (), (
        ("max_hold_hours", _s_u32, ()), ("max_loss", _s_dec, ()), ("take_profit", _s_dec_or_null, ()),
        ("conditions", _s_conditions, ()))),
    "sizing": (("notional_per_leg", "max_open", "leverage"), ("margin",), (
        ("notional_per_leg", _s_dec, ()), ("max_open", _s_u32, ()), ("leverage", _s_dec_or_null, ()),
        ("margin", _s_margin, ()))),
}
_TOP_REQUIRED = ("schema", "name", "kind", "universe", "assumptions", "entry", "exit", "sizing", "parameters", "provenance")


def _shape_fields(v: Any, path: str, required: tuple[str, ...], optional: tuple[str, ...], checks: tuple, k: _Sink) -> None:
    """One object: its field set, then each present field (collect mode goes on past a missing or unknown field)."""
    if not k.ok(_obj, v, path, required, optional) and not isinstance(v, dict):
        return
    for key, fn, extra in checks:
        if key in v:
            k.ok(fn, v[key], *extra, f"{path}.{key}")


def _shape_section(name: str, v: Any, k: _Sink) -> None:
    required, optional, checks = _SECTIONS[name]
    _shape_fields(v, name, required, optional, checks, k)


def _shape_parameter(name: str, p: Any, k: _Sink) -> None:
    pp = f"parameters.{name}"
    if not k.ok(_obj, p, pp, ("value", "robust", "path")) and not isinstance(p, dict):
        return
    if "value" in p:
        k.ok(_s_dec, p["value"], f"{pp}.value")
    if "robust" in p:
        k.ok(_s_robust, p["robust"], f"{pp}.robust")
    if "path" in p:
        k.ok(_s_str, p["path"], f"{pp}.path")


def _shape_parameters(ps: Any, k: _Sink) -> None:
    if not k.ok(_s_object, ps, "parameters"):
        return
    for name, p in ps.items():
        _shape_parameter(name, p, k)


MAKER_MODELS = ("M1", "M2")
MAKER_TIMEOUT_MIN_S = 120
MAKER_TIMEOUT_MAX_S = 3600
MAKER_PARTICIPATION_MAX = Decimal("0.25")
MAKER_PARTICIPATION_DEFAULT = "0.05"
_LEGS = ("long", "short")


def _s_taker(v: Any, path: str) -> None:
    if v != "taker":
        raise _shape('must be "taker"', path)


def _shape_maker(v: Any, path: str, k: _Sink) -> None:
    _shape_fields(v, path, ("model", "timeout_s"), ("participation", "chase"), (
        ("model", _s_enum, (MAKER_MODELS,)), ("timeout_s", _s_u32, ()), ("participation", _s_dec, ()),
        ("chase", _s_taker, ())), k)


def _shape_leg(v: Any, path: str, k: _Sink) -> None:
    if v == "taker":
        return
    if not isinstance(v, dict):
        k.ok(_s_leg_kind, v, path)
        return
    if not k.ok(_obj, v, path, ("maker",)):
        return
    _shape_maker(v["maker"], f"{path}.maker", k)


def _s_leg_kind(v: Any, path: str) -> None:
    raise _shape('must be "taker" or {"maker": {...}}', path)


def _shape_entry_execution(v: Any, path: str, k: _Sink) -> None:
    if not k.ok(_obj, v, path, (), _LEGS) and not isinstance(v, dict):
        return
    for leg in _LEGS:
        if leg in v:
            _shape_leg(v[leg], f"{path}.{leg}", k)


def _shape_execution(v: Any, k: _Sink) -> None:
    """README §1 ``execution`` (optional): ``{entry?: {long?, short?}, exit?: "taker"}``; a leg is "taker" or {"maker": ...}."""
    if not k.ok(_obj, v, "execution", (), ("entry", "exit")) and not isinstance(v, dict):
        return
    if "entry" in v:
        _shape_entry_execution(v["entry"], "execution.entry", k)
    if "exit" in v:
        k.ok(_s_taker, v["exit"], "execution.exit")


def _shape_doc(doc: Any, k: _Sink) -> dict[str, bool]:
    """§3.1 for the whole document; returns which top-level values passed (collect mode checks each independently)."""
    passed: dict[str, bool] = {}
    if not k.ok(_obj, doc, "", _TOP_REQUIRED, ("strategy_id", "execution")) and not isinstance(doc, dict):
        return passed
    for key in ("schema", "strategy_id", "name", "kind"):
        if key in doc:
            passed[key] = k.ok(_s_str, doc[key], key)
    for key in ("universe", "assumptions", "entry", "exit", "sizing"):
        if key in doc:
            passed[key] = k.clean(_shape_section, key, doc[key])
    if "parameters" in doc:
        passed["parameters"] = k.clean(_shape_parameters, doc["parameters"])
    if "execution" in doc:
        passed["execution"] = k.clean(_shape_execution, doc["execution"])
    return passed


def _check_shape(doc: Any) -> None:
    _shape_doc(doc, _Sink(collect=False))


# ---------------------------------------------------------------------------------------------------------------------
# validation (§3)
# ---------------------------------------------------------------------------------------------------------------------

def _invalid(path: str, msg: str) -> IrError:
    return IrError(E_INVALID, f"{path}: {msg}", field=path)


def _D(s: str) -> Decimal:
    return Decimal(s)


def _check_conditions(conds: list, path: str, *, min_hold: bool = False) -> None:
    """§3.4. ``min_hold_hours`` is for exit conditions only (>= 1); an entry condition that carries it is refused."""
    for i, c in enumerate(conds):
        p = f"{path}[{i}]"
        if "min_hold_hours" in c:
            if not min_hold:
                raise _invalid(f"{p}.min_hold_hours", "only exit conditions take min_hold_hours")
            if c["min_hold_hours"] < 1:
                raise _invalid(f"{p}.min_hold_hours", "must be >= 1")
        ranged = isinstance(c["value"], list)
        if c["op"] == "between":
            if not ranged:
                raise _invalid(p, "the value of between is [lo, hi]")
            if _D(c["value"][0]) > _D(c["value"][1]):
                raise _invalid(p, "between needs lo <= hi")
        elif ranged:
            raise _invalid(p, "only between takes an interval")


def ascii_upper(s: str) -> str:
    return "".join(chr(ord(c) - 32) if "a" <= c <= "z" else c for c in s)


def _upper(s: str) -> bool:
    return s != "" and s == ascii_upper(s)


def _resolve(doc: dict, path: str) -> Any:
    """``seg(.seg)*`` with ``seg = name([index])*``; the first name is entry/exit/sizing/assumptions. Missing → KeyError."""
    if not _PATH_RE.match(path):
        raise KeyError(path)
    cur: Any = doc
    for seg in path.split("."):
        m = _SEG_RE.fullmatch(seg)
        if m is None:
            raise KeyError(path)
        if not isinstance(cur, dict) or m.group(1) not in cur:
            raise KeyError(path)
        cur = cur[m.group(1)]
        for idx in re.findall(r"\[([0-9]+)\]", m.group(2)):
            if not isinstance(cur, list) or int(idx) >= len(cur):
                raise KeyError(path)
            cur = cur[int(idx)]
    return cur


def _check_parameter(definition: dict, name: str, p: dict) -> None:
    path = f"parameters.{name}"
    if name.strip() == "":
        raise _invalid("parameters", "a parameter name cannot be empty")
    lo, hi = (_D(x) for x in p["robust"])
    value = _D(p["value"])
    if not (lo <= value <= hi):
        raise _invalid(path, "robust must satisfy lo <= value <= hi")
    bad = IrError(E_PARAMETER_PATH, f"parameter {name}: path {p['path']} does not point at a number in "
                  "entry/exit/sizing/assumptions", field=f"{path}.path")
    try:
        target = _resolve(definition, p["path"])
    except KeyError:
        raise bad from None
    if isinstance(target, str) and is_canonical_decimal(target):
        found = _D(target)
    elif isinstance(target, int) and not isinstance(target, bool):
        found = Decimal(target)
    else:
        raise bad
    if found != value:
        raise IrError(E_PARAMETER_MISMATCH, f"parameter {name}: value {p['value']} differs from {json.dumps(target)} at "
                      f"{p['path']}", field=f"{path}.value")


def _check_parameters(doc: dict, k: _Sink) -> None:
    definition = _definition(doc)
    for name in sorted(doc["parameters"]):
        k.ok(_check_parameter, definition, name, doc["parameters"][name])


def maker_leg(doc: Any) -> tuple[str, dict] | None:
    """The maker entry leg of an IR document: ``("long" | "short", spec)`` with ``participation`` and ``chase`` defaults
    filled, or None when every leg is taker (``execution`` absent, ``{}`` or all ``"taker"``). Expects a valid shape."""
    if isinstance(doc, Ir):
        doc = doc.doc
    ex = doc.get("execution") if isinstance(doc, dict) else None
    entry = ex.get("entry") if isinstance(ex, dict) else None
    if not isinstance(entry, dict):
        return None
    for leg in _LEGS:
        v = entry.get(leg)
        if isinstance(v, dict) and isinstance(v.get("maker"), dict):
            spec = dict(v["maker"])
            spec.setdefault("participation", MAKER_PARTICIPATION_DEFAULT)
            spec.setdefault("chase", "taker")
            return leg, spec
    return None


def maker_request_check(ir_doc: Any, fees: Any, step_minutes: int | None) -> tuple[str, str] | None:
    """``(field, message)`` when a request with this IR, fee table and step cannot run a maker entry leg, else None
    (always None for an IR without one). One definition for the tool path and the CLI path:

    * ``step_minutes <= 2``: AlphaKeel refuses a maker entry leg on such a step;
    * a maker leg on a spot leg (``spot_perp``: the long leg is spot): VT sends no spot maker fees;
    * a fee row without ``maker.perp`` (only a fee table that is a mapping can be inspected; a path or JSON text is read,
      and reported, elsewhere)."""
    m = maker_leg(ir_doc)
    if m is None:
        return None
    leg = m[0]
    shape = ((ir_doc.doc if isinstance(ir_doc, Ir) else ir_doc).get("universe") or {}).get("shape")
    if shape == "spot_perp" and leg == "long":
        return "ir", ("a maker entry leg on the long leg of a spot_perp IR is a spot maker leg: VT sends no spot maker fees, so it "
                      "is refused; make the short (perp) leg the maker leg or use taker")
    if isinstance(step_minutes, int) and step_minutes <= 2:
        return "step_minutes", ("AlphaKeel refuses a maker entry leg with step_minutes <= 2: use step_minutes >= 5 "
                                "(CLI: --step-minutes 5 or more; ir_backtest: a shorter window gives a coarser step) or drop the maker leg")
    if isinstance(fees, dict):
        missing = [v for v, row in fees.items() if not (isinstance(row, dict) and isinstance(row.get("maker"), dict)
                                                         and row["maker"].get("perp") not in (None, ""))]
        if missing:
            return "fees", (f"fees has no maker.perp rate for {', '.join(missing)}: the IR has a maker entry leg (execution), "
                            "so give a maker rate for every venue in universe.pairs (fees.<venue>.maker.perp)")
    return None


def maker_request_problem(ir_doc: Any, fees: Any, step_minutes: int | None) -> str | None:
    """The message of :func:`maker_request_check`, or None."""
    got = maker_request_check(ir_doc, fees, step_minutes)
    return None if got is None else got[1]


def _check_execution(doc: dict, k: _Sink) -> None:
    """README §3 rule 5b: at most one maker leg; timeout_s in [120, 3600]; participation in (0, 0.25]."""
    entry = (doc.get("execution") or {}).get("entry") or {}
    makers = [leg for leg in _LEGS if isinstance(entry.get(leg), dict)]
    if len(makers) > 1:
        k.ok(_raise, _invalid("execution.entry", "at most one leg can be maker"))
        return
    for leg in makers:
        m = entry[leg]["maker"]
        p = f"execution.entry.{leg}.maker"
        if not (MAKER_TIMEOUT_MIN_S <= m["timeout_s"] <= MAKER_TIMEOUT_MAX_S):
            k.ok(_raise, _invalid(f"{p}.timeout_s", f"must be in {MAKER_TIMEOUT_MIN_S}..={MAKER_TIMEOUT_MAX_S}"))
        if "participation" in m and not (0 < _D(m["participation"]) <= MAKER_PARTICIPATION_MAX):
            k.ok(_raise, _invalid(f"{p}.participation", f"must be in (0, {MAKER_PARTICIPATION_MAX}]"))


def _raise(e: IrError) -> None:
    raise e


def _check_provenance(p: Any, k: _Sink) -> None:
    if not k.ok(_require, isinstance(p, dict), "provenance", "must be an object"):
        return
    t = p.get("tool")
    k.ok(_require, isinstance(t, str) and t != "", "provenance.tool", "required non-empty string")
    w = p.get("window")
    if not k.ok(_require, isinstance(w, dict), "provenance.window", "required object {start, end}"):
        return
    for key in ("start", "end"):
        k.ok(_require, isinstance(w.get(key), str), f"provenance.window.{key}", "required string")


def _require(cond: bool, path: str, msg: str) -> None:
    if not cond:
        raise _invalid(path, msg)


def _check_schema(v: str) -> None:
    if v != SCHEMA:
        raise IrError(E_SCHEMA, f"schema must be {SCHEMA}, got {v!r}", field="schema")


def _check_kind(v: str) -> None:
    if v != KIND:
        raise IrError(E_KIND, f"kind accepts only {KIND} in v1, got {v!r}", field="kind")


def _check_name(v: str) -> None:
    if v.strip() == "":
        raise _invalid("name", "cannot be empty")


def _check_universe(u: dict) -> None:
    if isinstance(u["pairs"], list):
        if not u["pairs"]:
            raise _invalid("universe.pairs", 'the list cannot be empty (use "any")')
        seen = set()
        for i, (a, b) in enumerate(u["pairs"]):
            if u["shape"] == "cross_perp" and a == b:
                raise _invalid(f"universe.pairs[{i}]", "cross_perp needs two different venues")
            if u["shape"] == "spot_perp" and a != b:
                raise _invalid(f"universe.pairs[{i}]", "spot_perp needs one venue")
            if u["shape"] == "equity_perp" and (a == b or (a == IBKR) == (b == IBKR)):
                raise _invalid(f"universe.pairs[{i}]", "equity_perp needs two different venues, exactly one of them ibkr")
            if u["shape"] != "equity_perp" and IBKR in (a, b):
                raise _invalid(f"universe.pairs[{i}]", "ibkr can only be a venue of an equity_perp pair")
            if (a, b) in seen:
                raise _invalid(f"universe.pairs[{i}]", "duplicate")
            seen.add((a, b))
    for i, b in enumerate(u["excluded_bases"]):
        if not _upper(b):
            raise _invalid(f"universe.excluded_bases[{i}]", "must be non-empty upper case")
    if "bases" in u:  # optional base allow-list (README §1); absent = any base
        if not u["bases"]:
            raise _invalid("universe.bases", "cannot be empty when given")
        seen_b: set = set()
        for i, b in enumerate(u["bases"]):
            if not _upper(b) or b in seen_b:
                raise _invalid(f"universe.bases[{i}]", "must be non-empty upper case and unique")
            seen_b.add(b)
    qs = u.get("quote_ccys", ["USDT"])
    if not qs:
        raise _invalid("universe.quote_ccys", "cannot be empty")
    seen_q: set = set()
    for i, q in enumerate(qs):
        if not _upper(q) or q in seen_q:
            raise _invalid(f"universe.quote_ccys[{i}]", "must be non-empty upper case and unique")
        seen_q.add(q)


def _check_assumptions(a: dict) -> None:
    if a["horizon_hours"] < 1:
        raise _invalid("assumptions.horizon_hours", "must be >= 1")
    bs = _D(a["basis_stress"])
    if bs < 0 or bs >= 1:
        raise _invalid("assumptions.basis_stress", "must be in [0, 1)")


def _check_entry(e: dict) -> None:
    _check_conditions(e["conditions"], "entry.conditions")
    if not (1 <= e["candidate_limit"] <= MAX_CANDIDATE_LIMIT):
        raise _invalid("entry.candidate_limit", f"must be in 1..={MAX_CANDIDATE_LIMIT}")
    if e["one_per_base"] is not True:
        raise _invalid("entry.one_per_base", "is fixed to true in v1")


def _check_exit(x: dict) -> None:
    if x["max_hold_hours"] < 1:
        raise _invalid("exit.max_hold_hours", "must be >= 1")
    if _D(x["max_loss"]) <= 0:
        raise _invalid("exit.max_loss", "must be > 0")
    if x["take_profit"] is not None and _D(x["take_profit"]) <= 0:
        raise _invalid("exit.take_profit", "must be > 0 or null")
    _check_conditions(x["conditions"], "exit.conditions", min_hold=True)


def _check_sizing(s: dict, shape: Any = None) -> None:
    if _D(s["notional_per_leg"]) <= 0:
        raise _invalid("sizing.notional_per_leg", "must be > 0")
    if not (1 <= s["max_open"] <= MAX_OPEN_LIMIT):
        raise _invalid("sizing.max_open", f"must be in 1..={MAX_OPEN_LIMIT}")
    if s["leverage"] is not None and _D(s["leverage"]) <= 0:
        raise _invalid("sizing.leverage", "must be > 0 or null")
    m = s.get("margin")
    if m is not None:
        if shape in SHAPES and shape != "spot_perp":
            raise _invalid("sizing.margin", "portfolio margin is only for spot_perp")
        above, to = _D(m["reduce_above"]), _D(m["reduce_to"])
        if to <= 0 or to >= above:
            raise _invalid("sizing.margin.reduce_to", "must satisfy 0 < reduce_to < reduce_above")
        if above >= Decimal("0.95"):
            raise _invalid("sizing.margin.reduce_above", "must be < 0.95")


def _check_strategy_id(doc: dict) -> None:
    computed = _strategy_id(doc)
    if doc["strategy_id"] != computed:
        raise IrError(E_STRATEGY_ID, f"declared strategy_id {doc['strategy_id']} differs from the computed {computed}",
                      field="strategy_id")


# Semantic checks of one top-level value (§3.2-§3.4), in the order the first-error path runs them.
_SEMANTIC = (("schema", _check_schema), ("kind", _check_kind), ("name", _check_name), ("universe", _check_universe),
             ("assumptions", _check_assumptions), ("entry", _check_entry), ("exit", _check_exit), ("sizing", _check_sizing))


def _run_checks(doc: Any, k: _Sink) -> None:
    """Every §3 rule. A value's semantic checks run only if its shape passed; the cross-section ones (parameter paths,
    strategy_id) only if the whole shape passed. With a raising sink this is exactly the first-error order."""
    passed = _shape_doc(doc, k)
    shape_ok = not k.items
    for key, fn in _SEMANTIC:
        if passed.get(key):
            if key == "sizing":
                u = doc["universe"] if passed.get("universe") else None
                k.ok(fn, doc[key], u.get("shape") if isinstance(u, dict) else None)
            else:
                k.ok(fn, doc[key])
    if shape_ok:
        _check_parameters(doc, k)
    if passed.get("execution"):
        _check_execution(doc, k)
    if isinstance(doc, dict) and "provenance" in doc:
        _check_provenance(doc["provenance"], k)
    if shape_ok and "strategy_id" in doc:
        k.ok(_check_strategy_id, doc)


def _check(doc: Any) -> None:
    _run_checks(doc, _Sink(collect=False))


def validate(doc: Any) -> list[str]:
    """Problems of an IR document (``[]`` = valid). Validation stops at the first broken rule, so at most one entry,
    spelled ``"<code>: <message>"``. Use :func:`load` to get the :class:`IrError` itself."""
    if isinstance(doc, Ir):
        doc = doc.doc
    try:
        _check(doc)
    except IrError as e:
        return [f"{e.code}: {e.message}"]
    return []


def problems(doc: Any) -> list[dict]:
    """Every problem of an IR document at once, as ``{"code", "field", "message"}`` (``[]`` = valid). Same rules and
    messages as :func:`validate`, but independent values are checked independently and an object reports all of its
    missing and unknown fields. AlphaKeel's validator stays authoritative; this is the agent's fast first pass."""
    if isinstance(doc, Ir):
        doc = doc.doc
    k = _Sink(collect=True)
    _run_checks(doc, k)
    return [{"code": e.code, "field": e.field, "message": e.message} for e in k.items]


def unit_warnings(doc: Any) -> list[str]:
    """Non-blocking unit hints. ``exit.max_loss`` and ``exit.take_profit`` are quote-currency amounts (README §1), not
    fractions of the notional; a value below 0.1% of one leg is almost certainly a fraction copied from params.json."""
    if isinstance(doc, Ir):
        doc = doc.doc
    try:
        _check_shape(doc)
    except IrError:
        return []
    n = doc["sizing"]["notional_per_leg"]
    out = []
    for key, what in (("max_loss", "closes at a loss of"), ("take_profit", "takes profit at")):
        m = doc["exit"][key]
        if m is None or not (Decimal(0) < Decimal(m) < Decimal(n) * Decimal("0.001")):
            continue
        with localcontext(Context(prec=6)):
            pct = Decimal(m) * 100 / Decimal(n)
        out.append(f"exit.{key} is an amount in the quote currency (USDT), not a fraction: {key} {m} on notional_per_leg {n} "
                   f"{what} {m} USDT ({fmt(pct)}% of one leg). params.json's {key} is a fraction; the IR's is not.")
    return out


# ---------------------------------------------------------------------------------------------------------------------
# canonical form and strategy_id (§2)
# ---------------------------------------------------------------------------------------------------------------------

def _definition(doc: dict) -> dict:
    d = {k: copy.deepcopy(v) for k, v in doc.items() if k not in ("provenance", "strategy_id")}
    if isinstance(d.get("universe"), dict) and "quote_ccys" not in d["universe"]:
        d["universe"]["quote_ccys"] = ["USDT"]
    if "execution" in d:
        m = maker_leg(doc)
        if m is None:
            del d["execution"]  # all taker: the same definition (and id) as no execution (README §2)
        else:
            leg, spec = m
            ent = {name: "taker" for name in _LEGS}
            ent[leg] = {"maker": spec}
            d["execution"] = {"entry": ent, "exit": "taker"}
    return d


def _strategy_id(doc: dict) -> str:
    return "ir-" + canon.canonical_sha256(_definition(doc))[:24]


class Ir:
    """A validated IR document. ``doc`` is the document as given (no defaults inserted); ``definition`` adds them."""

    __slots__ = ("doc",)

    def __init__(self, doc: dict):
        self.doc = doc

    @property
    def definition(self) -> dict:
        return _definition(self.doc)

    def canonical_definition(self) -> str:
        return canon.canonical_bytes(self.definition).decode("utf-8")

    @property
    def strategy_id(self) -> str:
        return _strategy_id(self.doc)

    # convenient views (read-only by convention)
    @property
    def universe(self) -> dict:
        u = dict(self.doc["universe"])
        u.setdefault("quote_ccys", ["USDT"])
        return u

    def __getitem__(self, k: str) -> Any:
        return self.doc[k]


def load(text_or_dict: str | bytes | dict | Ir) -> Ir:
    """Parse (if text) and validate. Raises :class:`IrError` with the §3 rule group as ``code``."""
    if isinstance(text_or_dict, Ir):
        return text_or_dict
    doc = _parse_text(text_or_dict) if isinstance(text_or_dict, (str, bytes)) else copy.deepcopy(text_or_dict)
    _check(doc)
    return Ir(doc)


def strategy_id(ir: Ir | dict | str) -> str:
    return load(ir).strategy_id


# ---------------------------------------------------------------------------------------------------------------------
# features (§4)
# ---------------------------------------------------------------------------------------------------------------------

class Features(dict):
    """The 21 features of one pair, keyed by name. Decimal features are ``Decimal`` (rounded as §4.2 says), the
    millisecond features and ``listing_age_hours`` ``int``, ``price_estimated`` ``bool``; unavailable is ``None`` (never 0)."""

    def value(self, name: str) -> Decimal | None:
        """The comparable value (§1: booleans as 0/1, milliseconds as decimals); ``None`` = unavailable."""
        v = self[name]
        if v is None:
            return None
        if isinstance(v, bool):
            return Decimal(1 if v else 0)
        if isinstance(v, int):
            return Decimal(v)
        return v

    def to_json(self) -> dict:
        out = {}
        for k in FEATURES:
            v = self[k]
            out[k] = fmt(v) if isinstance(v, Decimal) else v
        return out


def _leg(p: dict, side: str) -> dict:
    leg = p.get(side)
    if not isinstance(leg, dict):
        raise ValueError(f"pair {p.get('id')!r}: missing {side} leg")
    w = f"{p.get('id')}.{side}"
    if leg.get("market") not in ("perp", "spot", "equity"):
        raise ValueError(f"{w}.market: {leg.get('market')!r}")
    return {
        "venue": leg["venue"], "symbol": leg["symbol"], "market": leg["market"], "quote_ccy": leg["quote_ccy"],
        "bid": _dec(leg["bid"], f"{w}.bid"), "ask": _dec(leg["ask"], f"{w}.ask"), "bbo": leg["bbo"] is True,
        "funding_rate": _opt_dec(leg.get("funding_rate"), f"{w}.funding_rate"),
        "interval_hours": _opt_int(leg.get("interval_hours"), f"{w}.interval_hours"),
        "funding_px": _opt_dec(leg.get("funding_px"), f"{w}.funding_px"),
        "volume_quote": _opt_dec(leg.get("volume_quote"), f"{w}.volume_quote"),
        "taker_fee": _dec(leg["taker_fee"], f"{w}.taker_fee"),
        "quote_ms": _opt_int(leg.get("quote_ms"), f"{w}.quote_ms"),
        "funding_mean_7d": _opt_dec(leg.get("funding_mean_7d"), f"{w}.funding_mean_7d"),
        "funding_mean_30d": _opt_dec(leg.get("funding_mean_30d"), f"{w}.funding_mean_30d"),
        "funding_pos_share_30d": _opt_dec(leg.get("funding_pos_share_30d"), f"{w}.funding_pos_share_30d"),
        "borrow_apr": _opt_dec(leg.get("borrow_apr"), f"{w}.borrow_apr"),
        "borrow_util": _opt_dec(leg.get("borrow_util"), f"{w}.borrow_util"),
        "borrow_cap_used": _opt_dec(leg.get("borrow_cap_used"), f"{w}.borrow_cap_used"),
        "listed_ms": _opt_int(leg.get("listed_ms"), f"{w}.listed_ms"),
    }


def _horizon(assumptions: dict) -> tuple[int, Decimal]:
    h = assumptions["horizon_hours"]
    if isinstance(h, bool) or not isinstance(h, int) or h < 1:
        raise ValueError("assumptions.horizon_hours must be an integer >= 1")
    return h, _dec(assumptions["basis_stress"], "assumptions.basis_stress")


def _fund(x: dict, h: int) -> Decimal | None:
    """Funding of one leg over the horizon, per 1 base unit; None = unavailable."""
    if x["market"] in ("spot", "equity"):
        return Decimal(0)
    rate, interval = x["funding_rate"], x["interval_hours"]
    if rate is None or interval is None or interval == 0:
        return None
    fpx = x["funding_px"] if x["funding_px"] is not None else (x["bid"] + x["ask"]) / 2
    return ((rate * fpx) * h) / interval


def compute_features(pair: dict, assumptions: dict, now_ms: int) -> Features:
    """§4.2 for one pair view (JSON shape of §4.1). Raises :class:`PairInvalid` for an invalid pair (§4.1)."""
    L, S = _leg(pair, "long"), _leg(pair, "short")
    if L["quote_ccy"] != S["quote_ccy"]:
        raise PairInvalid("quote_ccy_mismatch")
    kind = pair["kind"]
    if kind == "cross_perp":
        ok = L["market"] == "perp" and S["market"] == "perp" and L["venue"] != S["venue"]
    elif kind == "spot_perp":
        ok = L["market"] == "spot" and S["market"] == "perp" and L["venue"] == S["venue"]
    elif kind == "equity_perp":
        ok = ({L["market"], S["market"]} == {"equity", "perp"}) and L["venue"] != S["venue"]
    else:
        raise ValueError(f"pair {pair.get('id')!r}: unknown kind {kind!r}")
    if not ok:
        raise PairInvalid("shape_mismatch")
    h, bs = _horizon(assumptions)
    f = Features({k: None for k in FEATURES})
    with localcontext(_CTX):
        fl, fs = _fund(L, h), _fund(S, h)
        n = L["ask"]
        if fl is not None and fs is not None and n != 0:
            fees_q = (L["ask"] * L["taker_fee"] + S["bid"] * S["taker_fee"]) + (L["bid"] * L["taker_fee"] + S["ask"] * S["taker_fee"])
            spread_q = (L["ask"] - L["bid"]) + (S["ask"] - S["bid"])
            stress_q = n * bs
            funding_q = fs - fl
            basis_q = (S["bid"] + S["ask"]) / 2 - (L["bid"] + L["ask"]) / 2
            kept = funding_q * Decimal("0.5") if funding_q > 0 else funding_q
            cons_q = kept - (fees_q + spread_q + stress_q)
            exp_q = (funding_q + basis_q) - (fees_q + spread_q)
            hourly = (funding_q / h) / n
            f["funding_hourly_net"] = _round(hourly, 10)
            f["funding_apr"] = _round(hourly * 8760, 6)
            f["funding"] = _round(funding_q / n, 8)
            f["basis"] = _round(basis_q / n, 8)
            f["spread"] = _round(spread_q / n, 8)
            f["fees"] = _round(fees_q / n, 8)
            f["stress"] = _round(stress_q / n, 8)
            f["net_expected"] = _round(exp_q / n, 8)
            f["net_conservative"] = _round(cons_q / n, 8)
        # 2026-10-18 features: each is independent of the economics group and of the others
        for name, key in (("funding_apr_7d", "funding_mean_7d"), ("funding_apr_30d", "funding_mean_30d")):
            if all(x[key] is not None for x in (L, S) if x["market"] == "perp"):
                m = {id(x): (x[key] if x["market"] == "perp" else Decimal(0)) for x in (L, S)}
                f[name] = _round((m[id(S)] - m[id(L)]) * 8760, 6)
        perps = [x for x in (L, S) if x["market"] == "perp"]
        if kind != "cross_perp" and len(perps) == 1:
            pl = perps[0]
            for name, key, places in (("funding_pos_share_30d", "funding_pos_share_30d", 4), ("borrow_apr", "borrow_apr", 8),
                                      ("borrow_utilization", "borrow_util", 8), ("borrow_cap_used", "borrow_cap_used", 8)):
                if pl[key] is not None:
                    f[name] = _round(pl[key], places)
        if L["ask"] != 0:
            f["entry_edge"] = _round((S["bid"] - L["ask"]) / L["ask"], 8)
        if L["volume_quote"] is not None and S["volume_quote"] is not None:
            f["volume_min"] = min(L["volume_quote"], S["volume_quote"])
    listed = [x["listed_ms"] for x in (L, S) if x["listed_ms"] is not None]
    if listed:
        f["listing_age_hours"] = max(0, now_ms - max(listed)) // HOUR_MS
    if L["quote_ms"] is not None and S["quote_ms"] is not None:
        f["quote_age_ms"] = max(max(0, now_ms - L["quote_ms"]), max(0, now_ms - S["quote_ms"]))
        f["leg_skew_ms"] = abs(L["quote_ms"] - S["quote_ms"])
    f["price_estimated"] = not (L["bbo"] and S["bbo"])
    return f


# ---------------------------------------------------------------------------------------------------------------------
# decision (§5)
# ---------------------------------------------------------------------------------------------------------------------

def _holds(cond: dict, v: Decimal) -> bool:
    op, val = cond["op"], cond["value"]
    if op == "between":
        return _D(val[0]) <= v <= _D(val[1])
    x = _D(val)
    return {">=": v >= x, "<=": v <= x, ">": v > x, "<": v < x}[op]


def _legref(leg: dict) -> dict:
    return {"venue": leg["venue"], "symbol": leg["symbol"]}


def _same_leg(a: dict, b: dict) -> bool:
    return a["venue"] == b["venue"] and a["symbol"] == b["symbol"]


def _exit_reason(ir: Ir, pairs: list[dict], pos: dict, now_ms: int) -> str | None:
    x = ir.doc["exit"]
    if now_ms - pos["opened_ms"] >= x["max_hold_hours"] * HOUR_MS:
        return "max_hold"
    net = _opt_dec(pos.get("net_now"), f"position {pos['id']}.net_now")
    if net is not None and net <= -_D(x["max_loss"]):
        return "max_loss"
    if x["take_profit"] is not None and net is not None and net >= _D(x["take_profit"]):
        return "take_profit"
    pair = next((p for p in pairs if _same_leg(p["long"], pos["long"]) and _same_leg(p["short"], pos["short"])), None)
    if pair is None:
        return None
    try:
        f = compute_features(pair, ir.doc["assumptions"], now_ms)
    except PairInvalid:
        return None
    held_ms = now_ms - pos["opened_ms"]
    for c in x["conditions"]:
        mh = c.get("min_hold_hours")
        if mh is not None and held_ms < mh * HOUR_MS:
            continue  # README §5 exit step 4: this condition does not take part yet
        v = f.value(c["feature"])
        if v is not None and _holds(c, v):
            return f"condition:{c['feature']}"
    return None


def _admit(ir: Ir, p: dict, cooldowns: dict, open_bases: set, now_ms: int) -> tuple[Features | None, str | None]:
    u = ir.universe
    e = ir.doc["entry"]
    base = ascii_upper(p["base"])
    if p["kind"] != u["shape"]:
        return None, "universe:shape"
    if u["pairs"] != "any" and [p["long"]["venue"], p["short"]["venue"]] not in u["pairs"]:
        return None, "universe:pair"
    if "bases" in u and not any(ascii_upper(b) == base for b in u["bases"]):
        return None, "universe:base"
    if any(ascii_upper(b) == base for b in u["excluded_bases"]):
        return None, "universe:excluded_base"
    try:
        f = compute_features(p, ir.doc["assumptions"], now_ms)
    except PairInvalid as err:
        return None, f"invalid:{err.code}"
    if p["long"]["quote_ccy"] not in u["quote_ccys"]:
        return None, "universe:quote_ccy"
    for c in e["conditions"]:
        v = f.value(c["feature"])
        if v is None:
            return None, f"unavailable:{c['feature']}"
        if not _holds(c, v):
            return None, f"entry:{c['feature']}"
    if f.value(e["rank_by"]) is None:
        return None, f"unavailable:{e['rank_by']}"
    if base in open_bases:
        return None, "open_base"
    cool_ms = e["cooldown_minutes"] * MINUTE_MS
    if any(ascii_upper(str(b)) == base and int(last) + cool_ms > now_ms for b, last in cooldowns.items()):
        return None, "cooldown"
    return f, None


def decide(ir: Ir | dict, frame: dict | list, state: dict, now_ms: int) -> dict:
    """One decision instant (§5). ``frame`` is ``{"pairs": [PairView…]}`` (or the list itself), ``state`` is
    ``{"positions": [PositionView…], "cooldowns": {base: last_close_ms}}``. Returns the Decision as JSON values:
    ``{"exits": [...], "entries": [...], "rejected": [...]}`` in the §5 order. Pure: same inputs, same output."""
    ir = load(ir)
    pairs = frame["pairs"] if isinstance(frame, dict) else list(frame)
    positions = list(state.get("positions") or [])
    cooldowns = dict(state.get("cooldowns") or {})

    exits = []
    for pos in sorted(positions, key=lambda q: q["id"]):
        r = _exit_reason(ir, pairs, pos, now_ms)
        if r is not None:
            exits.append({"position_id": pos["id"], "reason": r})

    # positions decided to exit at this instant still count as open (base and max_open) until the engine confirms the close
    open_bases = {ascii_upper(q["base"]) for q in positions}
    rejected: list[dict] = []
    ranked: list[tuple[dict, Features]] = []
    for p in pairs:
        f, why = _admit(ir, p, cooldowns, open_bases, now_ms)
        if why is not None:
            rejected.append({"pair_id": p["id"], "reason": why})
        else:
            ranked.append((p, f))
    key = ir.doc["entry"]["rank_by"]
    ranked.sort(key=lambda pf: pf[0]["id"])
    ranked.sort(key=lambda pf: pf[1].value(key), reverse=True)  # stable: ties keep pair_id ascending

    seen: set = set()
    kept = []
    for p, f in ranked:
        b = ascii_upper(p["base"])
        if b in seen:
            rejected.append({"pair_id": p["id"], "reason": "duplicate_base"})
        else:
            seen.add(b)
            kept.append((p, f))
    limit = ir.doc["entry"]["candidate_limit"]
    slots = max(0, ir.doc["sizing"]["max_open"] - len(positions))
    entries = []
    for i, (p, f) in enumerate(kept):
        if i >= limit:
            rejected.append({"pair_id": p["id"], "reason": "candidate_limit"})
        elif i >= slots:
            rejected.append({"pair_id": p["id"], "reason": "max_open"})
        else:
            entries.append({"pair_id": p["id"], "base": p["base"], "long": _legref(p["long"]), "short": _legref(p["short"]),
                            "notional_per_leg": ir.doc["sizing"]["notional_per_leg"], "features": f.to_json()})
    rejected.sort(key=lambda r: r["pair_id"])
    return {"exits": exits, "entries": entries, "rejected": rejected}
