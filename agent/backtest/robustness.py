"""Tunable-parameter convention of a generic-engine strategy, its neighbourhood sweep and its component ablation (pure).

**Convention.** ``config.json`` declares what the strategy may tune; nothing is declared, nothing changes:

* ``params``: ``{name: number}``, the chosen point. The runner sets ``SignalEngine.PARAMS`` to the class's own ``PARAMS``
  dict (when it has one) updated with these values before the engine is instantiated, so the code reads
  ``self.PARAMS["name"]`` in ``__init__`` or ``generate``.
* ``param_ranges`` (optional): ``{name: {"step": s, "min": a, "max": b}}`` (each key optional). A neighbour is the chosen
  value ``+/- step``; without a step it is ``+/- 20 %`` (an integer value moves by at least 1 and stays an integer).
  A neighbour outside ``[min, max]`` is not run.
* ``components`` (optional): names of switchable signal components. The runner sets ``SignalEngine.COMPONENTS`` to
  ``{name: True}`` (``False`` for a name listed in ``components_off``), so the code reads ``self.COMPONENTS["name"]``.
* ``components_off`` (optional): components switched off in this run (a subset of ``components``).

Names are identifiers (``[A-Za-z_][A-Za-z0-9_]*``, at most 40 characters), at most ``MAX_PARAMS`` parameters and
``MAX_COMPONENTS`` components. ``variants`` lists the runs of the sweep (two neighbours per parameter, one ablation per
component that is on); ``variant_config`` is the config of one. Nothing here reads files, imports strategy code or touches
the network.
"""

from __future__ import annotations

import copy
import math
import re
from typing import Any

MAX_PARAMS = 8
MAX_COMPONENTS = 8
DEFAULT_RELATIVE_STEP = 0.2
DECLARATION_KEYS = ("params", "param_ranges", "components", "components_off")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,39}")
_RANGE_KEYS = frozenset({"step", "min", "max"})


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _check_name(name: Any, what: str) -> None:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError(f"{what} name {name!r} must be an identifier ([A-Za-z_][A-Za-z0-9_]*, at most 40 characters)")


def validate(config: dict) -> None:
    """Raise ``ValueError`` when a declaration key of ``config`` is present with a wrong shape; absent keys are fine."""
    params = config.get("params")
    if params is not None:
        if not isinstance(params, dict) or not params:
            raise ValueError("params must be a non-empty object {name: number}")
        if len(params) > MAX_PARAMS:
            raise ValueError(f"params declares {len(params)} parameters; at most {MAX_PARAMS}")
        for name, value in params.items():
            _check_name(name, "params")
            if not _is_number(value):
                raise ValueError(f"params[{name!r}] must be a finite number, got {value!r}")
    ranges = config.get("param_ranges")
    if ranges is not None:
        if not isinstance(ranges, dict):
            raise ValueError("param_ranges must be an object {name: {step, min, max}}")
        for name, spec in ranges.items():
            if not isinstance(params, dict) or name not in params:
                raise ValueError(f"param_ranges[{name!r}] has no value in params")
            if not isinstance(spec, dict) or set(spec) - _RANGE_KEYS:
                raise ValueError(f"param_ranges[{name!r}] must be an object with keys among step, min, max")
            for key, value in spec.items():
                if not _is_number(value):
                    raise ValueError(f"param_ranges[{name!r}][{key!r}] must be a finite number")
            if "step" in spec and not spec["step"] > 0:
                raise ValueError(f"param_ranges[{name!r}].step must be positive")
            if "min" in spec and "max" in spec and spec["min"] > spec["max"]:
                raise ValueError(f"param_ranges[{name!r}].min is above max")
    components = config.get("components")
    if components is not None:
        if not isinstance(components, list) or not components:
            raise ValueError("components must be a non-empty list of names")
        if len(components) > MAX_COMPONENTS:
            raise ValueError(f"components lists {len(components)} names; at most {MAX_COMPONENTS}")
        for name in components:
            _check_name(name, "components")
        if len(set(components)) != len(components):
            raise ValueError("components lists a name twice")
    off = config.get("components_off")
    if off is not None:
        if not isinstance(off, list):
            raise ValueError("components_off must be a list of names")
        for name in off:
            if not isinstance(components, list) or name not in components:
                raise ValueError(f"components_off names {name!r}, which is not in components")


def declared(config: dict) -> bool:
    """Whether ``config`` declares tunable parameters or switchable components (the sweep runs only then)."""
    return isinstance(config.get("params"), dict) and bool(config["params"]) or (
        isinstance(config.get("components"), list) and bool(config["components"]))


def apply(engine_cls: Any, config: dict) -> None:
    """Set ``engine_cls.PARAMS`` / ``engine_cls.COMPONENTS`` from ``config`` (only the declared ones; validated config)."""
    params = config.get("params")
    if isinstance(params, dict) and params:
        base = getattr(engine_cls, "PARAMS", None)
        engine_cls.PARAMS = {**(base if isinstance(base, dict) else {}), **params}
    components = config.get("components")
    if isinstance(components, list) and components:
        off = set(config.get("components_off") or [])
        base = getattr(engine_cls, "COMPONENTS", None)
        engine_cls.COMPONENTS = {**(base if isinstance(base, dict) else {}), **{c: c not in off for c in components}}


def _round(v: float) -> float:
    return float(f"{v:.10g}")


def _neighbours(value: float | int, spec: dict) -> tuple[list[tuple[str, float | int]], str | None]:
    """``([(side, value), ...], reason)``: the in-range neighbours of ``value`` and why one or both are missing."""
    step = spec.get("step")
    is_int = isinstance(value, int) and (step is None or float(step).is_integer())
    if step is None:
        if value == 0:
            return [], "zero_without_step"
        step = abs(value) * DEFAULT_RELATIVE_STEP
        if is_int:
            step = max(1, int(round(step)))
    elif is_int:
        step = int(step)
    out, dropped = [], False
    for side, cand in (("-", value - step), ("+", value + step)):
        cand = int(cand) if is_int else _round(float(cand))
        if ("min" in spec and cand < spec["min"]) or ("max" in spec and cand > spec["max"]) or cand == value:
            dropped = True
            continue
        out.append((side, cand))
    return out, ("out_of_range" if dropped else None)


def variants(config: dict) -> tuple[list[dict], list[dict]]:
    """``(runs, skipped)`` of the sweep of a validated ``config``.

    ``runs``: ``{"label", "kind": "param", "param", "value", "side": "-"|"+", "base"}`` for each neighbour of each
    parameter, then ``{"label", "kind": "ablation", "component"}`` for each component that is on. ``skipped``:
    ``{"param", "reason"}`` for a parameter with a missing neighbour (``zero_without_step``, ``out_of_range``)."""
    runs: list[dict] = []
    skipped: list[dict] = []
    params = config.get("params") if isinstance(config.get("params"), dict) else {}
    ranges = config.get("param_ranges") if isinstance(config.get("param_ranges"), dict) else {}
    for name, value in params.items():
        found, reason = _neighbours(value, ranges.get(name) or {})
        for side, cand in found:
            runs.append({"label": f"p_{name}_{'minus' if side == '-' else 'plus'}", "kind": "param", "param": name,
                         "value": cand, "side": side, "base": value})
        if reason:
            skipped.append({"param": name, "reason": reason})
    off = set(config.get("components_off") or [])
    for name in config.get("components") or []:
        if name not in off:
            runs.append({"label": f"a_{name}", "kind": "ablation", "component": name})
    return runs, skipped


def variant_config(config: dict, run: dict) -> dict:
    """``config`` with the one change of ``run`` (a parameter moved, or one more component switched off)."""
    out = copy.deepcopy(config)
    out.pop("supersedes_contract", None)  # a sweep run is never a declared strategy change
    if run["kind"] == "param":
        out["params"] = {**out["params"], run["param"]: run["value"]}
    else:
        out["components_off"] = [*(out.get("components_off") or []), run["component"]]
    return out


def neighbourhood_summary(chosen_sharpe: float | None, neighbour_sharpes: list[float | None]) -> dict:
    """``below_half``: neighbours whose Sharpe is below half the chosen point's (a neighbour without a Sharpe counts as
    below); ``share`` = below_half / n; both None when the chosen Sharpe is not positive (half of it is no bar)."""
    n = len(neighbour_sharpes)
    if chosen_sharpe is None or not chosen_sharpe > 0 or not n:
        return {"n": n, "below_half": None, "share": None}
    below = sum(1 for s in neighbour_sharpes if s is None or s < chosen_sharpe / 2)
    return {"n": n, "below_half": below, "share": below / n}
