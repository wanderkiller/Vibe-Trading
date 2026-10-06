"""alphakeel.research/1 contract helpers: canonical JSON, hashes, and validation by the generated models.

The models (``models.py``) are generated from the OpenAPI that AlphaKeel exports from its Rust DTOs, so there is one
source of truth. This module adds only what the generator cannot: canonical JSON (the identity of an artifact),
the float ban, cross-line event-log checks, and mapping of pydantic errors to the contract's stable error codes
(the shared vectors compare codes, never library message text).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from . import canon as _canon
from . import models

CONTRACT_ID = "alphakeel.research/1"
MAJOR = 1


def local_pin() -> dict[str, str]:
    """The contract files this client was built against: ``{relative path: sha256}`` from ``contract/PIN``.

    Every pinned file is re-hashed: a client whose packaged contract files do not match their own PIN refuses to run
    (it would otherwise validate against something other than what it claims to speak).
    """
    import hashlib
    from pathlib import Path

    root = Path(__file__).parent / "contract"
    out: dict[str, str] = {}
    for line in (root / "PIN").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        digest, name = line.split(maxsplit=1)
        actual = hashlib.sha256((root / name).read_bytes()).hexdigest()
        if actual != digest:
            from .errors import ApiError

            raise ApiError("contract.pin_mismatch", f"the packaged contract file {name} does not match this client's PIN")
        out[name] = digest
    return out


@dataclass(frozen=True)
class Violation:
    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.code} at {self.path}: {self.message}"


class ContractError(ValueError):
    def __init__(self, violations: list[Violation]):
        self.violations = violations
        super().__init__("; ".join(str(v) for v in violations[:3]))

    @property
    def code(self) -> str:
        return self.violations[0].code


# ---------------------------------------------------------------------------------------------------------------
# canonical JSON (byte-identical with the Rust implementation; verified by the shared vectors)
# ---------------------------------------------------------------------------------------------------------------


def _reject_floats(v: Any, path: str, out: list[Violation]) -> None:
    if isinstance(v, float):
        out.append(Violation("schema.float", path or "/", "floating-point numbers are not allowed"))
    elif isinstance(v, list):
        for i, x in enumerate(v):
            _reject_floats(x, f"{path}/{i}", out)
    elif isinstance(v, dict):
        for k, x in v.items():
            _reject_floats(x, f"{path}/{k}", out)


def canonical_bytes(doc: Any) -> bytes:
    """Sorted keys, no whitespace, UTF-8 verbatim, integers only."""
    try:
        return _canon.canonical_bytes(doc)
    except _canon.FloatError as e:
        raise ContractError([Violation("schema.float", e.path, "floating-point numbers are not allowed")]) from e


sha256_hex = _canon.sha256_hex


def canonical_sha256(doc: Any) -> str:
    return sha256_hex(canonical_bytes(doc))


# ---------------------------------------------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------------------------------------------

KINDS = {
    "data-lock": models.DataLock,
    "execution-profile": models.ExecutionProfile,
    "strategy-manifest": models.StrategyManifest,
    "run-manifest": models.RunManifest,
    "result": models.RunResult,
    "comparison": models.Comparison,
    "intents": models.Intents,
    "policy-context": models.PolicyContext,
    "policy-response": models.PolicyResponse,
    "event": models.Event,
    "events-header": models.EventsHeader,
    "intent": models.Intent,
    "difference": models.Difference,
    "error": models.ErrorBody,
    "adjustment": models.Adjustment,
}

EVENT_DATA = {
    "decision": models.EventDataDecision,
    "intent": models.EventDataIntent,
    "order_result": models.EventDataOrderResult,
    "fill": models.EventDataFill,
    "fee": models.EventDataFee,
    "funding": models.EventDataFunding,
    "position": models.EventDataPosition,
    "balance": models.EventDataBalance,
    "equity": models.EventDataEquity,
}

_TYPE_ERRORS = {
    "string_type", "int_type", "bool_type", "dict_type", "list_type", "model_type", "model_attributes_type",
    "int_parsing", "int_from_float", "bool_parsing", "none_required", "is_instance_of", "json_type",
}


def _path(loc: tuple) -> str:
    parts = []
    for p in loc:
        # union branches show up as class names (e.g. "DataLockFunding"); fields are snake_case, indexes are ints
        if isinstance(p, str) and (
            (p[:1].isupper() and any(c.islower() for c in p)) or p.startswith("function-") or p in ("json-or-python", "lax", "strict")
        ):
            continue
        parts.append(str(p))
    return "/" + "/".join(parts) if parts else "/"


def _code(err: dict) -> str:
    t = err["type"]
    inp = err.get("input")
    if isinstance(inp, float):
        return "schema.float"
    if t == "missing":
        return "schema.required"
    if t == "extra_forbidden":
        return "schema.additional"
    if t == "string_pattern_mismatch":
        return "schema.format"
    if t in ("enum", "literal_error"):
        return "schema.enum"
    if t in ("too_short", "too_long", "string_too_short", "string_too_long"):
        return "schema.length"
    if t in ("greater_than_equal", "less_than_equal", "greater_than", "less_than"):
        return "schema.range"
    if t in _TYPE_ERRORS:
        return "schema.type"
    if t == "value_error":
        return "schema.enum"  # the only custom validator is the false-only boolean
    return "schema.type"


def _from_pydantic(e: ValidationError) -> list[Violation]:
    out: list[Violation] = []
    seen = set()
    for err in e.errors(include_url=False):
        v = Violation(_code(err), _path(err["loc"]), str(err.get("msg", "")))
        if (v.code, v.path) not in seen:
            seen.add((v.code, v.path))
            out.append(v)
    return out


def validate_def(kind: str, doc: Any) -> list[Violation]:
    """Validate ``doc`` against the model of ``kind``. Strict: no type coercion (``"1"`` is not an integer)."""
    model = KINDS.get(kind)
    if model is None:
        return [Violation("contract.unknown_kind", "/", f"unknown definition {kind}")]
    floats: list[Violation] = []
    _reject_floats(doc, "", floats)
    try:
        text = json.dumps(doc, allow_nan=False)
    except ValueError:
        return [Violation("schema.float", "/", "non-finite number")]
    try:
        model.model_validate_json(text, strict=True)
        out: list[Violation] = []
    except ValidationError as e:
        out = _from_pydantic(e)
    for f in floats:
        if not any(o.code == "schema.float" and o.path == f.path for o in out):
            out.append(f)
    return out


def parse_schema_id(doc: Any) -> tuple[str, int]:
    s = doc.get("schema") if isinstance(doc, dict) else None
    if not isinstance(s, str):
        raise ContractError([Violation("contract.missing_schema", "/schema", "the document has no schema field")])
    if not s.startswith("alphakeel."):
        raise ContractError([Violation("contract.unknown_kind", "/schema", f"{s} is not an alphakeel schema")])
    kind, sep, major = s[len("alphakeel."):].rpartition("/")
    if not sep or not major.isdigit():
        raise ContractError([Violation("contract.unknown_kind", "/schema", f"{s} has no major version")])
    return kind, int(major)


ARTIFACT_KINDS = ("data-lock", "execution-profile", "strategy-manifest", "run-manifest", "result", "comparison",
                  "intents", "policy-context", "policy-response")


def validate_doc(doc: Any) -> str:
    """Validate a versioned artifact by its own ``schema`` field. Unknown kinds and majors are rejected first."""
    kind, major = parse_schema_id(doc)
    if kind not in ARTIFACT_KINDS:
        raise ContractError([Violation("contract.unknown_kind", "/schema", f"unknown artifact kind {kind}")])
    if major != MAJOR:
        raise ContractError([Violation(
            "contract.unknown_major", "/schema", f"{kind} major version {major} is not supported (this client speaks {MAJOR})")])
    v = validate_def(kind, doc)
    if v:
        raise ContractError(v)
    return kind


def validate_event(ev: Any) -> list[Violation]:
    out = validate_def("event", ev)
    kind = ev.get("kind") if isinstance(ev, dict) else None
    model = EVENT_DATA.get(kind)
    if model is not None and isinstance(ev.get("data"), dict):
        for v in _validate_model(model, ev["data"]):
            out.append(Violation(v.code, "/data" + ("" if v.path == "/" else v.path), v.message))
    return out


def _validate_model(model: type[BaseModel], doc: Any) -> list[Violation]:
    floats: list[Violation] = []
    _reject_floats(doc, "", floats)
    try:
        model.model_validate_json(json.dumps(doc), strict=True)
        out: list[Violation] = []
    except ValidationError as e:
        out = _from_pydantic(e)
    for f in floats:
        if not any(o.code == "schema.float" and o.path == f.path for o in out):
            out.append(f)
    return out


def validate_events(lines: list[Any]) -> list[Violation]:
    """Event log = header + events: run_id consistent, event_id unique, (time_ns, seq) strictly increasing."""
    if not lines:
        return [Violation("evidence.schema", "/", "the event log is empty (a header line is required)")]
    head, events = lines[0], lines[1:]
    out = validate_def("events-header", head)
    run_id = head.get("run_id") if isinstance(head, dict) else None
    if isinstance(head, dict) and head.get("count") != len(events):
        out.append(Violation("evidence.schema", "/count", "header count does not match the number of events"))
    ids: set[str] = set()
    last: tuple[int, int] | None = None
    for i, e in enumerate(events):
        p = f"/events/{i}"
        for v in validate_event(e):
            out.append(Violation(v.code, p + ("" if v.path == "/" else v.path), v.message))
        if not isinstance(e, dict):
            continue
        if e.get("run_id") != run_id:
            out.append(Violation("evidence.reference", f"{p}/run_id", "event belongs to another run"))
        eid = e.get("event_id")
        if isinstance(eid, str):
            if eid in ids:
                out.append(Violation("evidence.reference", f"{p}/event_id", "duplicate event_id"))
            ids.add(eid)
        ns, seq = e.get("time_ns"), e.get("seq")
        if isinstance(ns, str) and ns.isdigit() and isinstance(seq, int):
            cur = (int(ns), seq)
            if last is not None and cur <= last:
                out.append(Violation("evidence.schema", p, "events must be strictly ordered by (time_ns, seq)"))
            last = cur
        if len(out) > 200:
            break
    return out
