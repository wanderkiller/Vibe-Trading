"""alphakeel.research/1: the Python side runs the SAME shared vectors as AlphaKeel's Rust tests.

Pinned copy of the contract lives in agent/alphakeel_research/contract/ (exported by AlphaKeel's
tools/export-research-contract.sh); models.py is generated from its openapi.json.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from alphakeel_research import contract as C

ROOT = Path(__file__).resolve().parents[1] / "alphakeel_research"
PIN_DIR = ROOT / "contract"


def _load(name: str):
    return json.loads((PIN_DIR / "vectors" / name).read_text(encoding="utf-8"))


def test_the_pinned_contract_files_match_their_pin():
    lines = [x for x in (PIN_DIR / "PIN").read_text().splitlines() if x and not x.startswith("#")]
    assert len(lines) >= 5
    for line in lines:
        digest, name = line.split(None, 1)
        assert hashlib.sha256((PIN_DIR / name).read_bytes()).hexdigest() == digest, name


@pytest.mark.skipif(shutil.which("datamodel-codegen") is None, reason="datamodel-code-generator is a dev dependency")
def test_models_are_the_current_generation_of_the_pinned_openapi(tmp_path):
    out = tmp_path / "models.py"
    subprocess.run(
        ["datamodel-codegen", "--input", str(PIN_DIR / "openapi.json"), "--input-file-type", "openapi", "--output", str(out),
         "--output-model-type", "pydantic_v2.BaseModel", "--target-python-version", "3.11", "--use-annotated",
         "--disable-timestamp", "--use-standard-collections", "--formatters", "ruff-format", "--custom-file-header",
         "# GENERATED from contract/openapi.json by gen_models.sh (datamodel-code-generator). Do not edit."],
        check=True, capture_output=True,
    )
    assert out.read_text() == (ROOT / "models.py").read_text(), "run agent/alphakeel_research/gen_models.sh"


def test_canonical_json_and_hash_match_the_shared_vectors_byte_for_byte():
    cases = _load("canonical.json")["cases"]
    assert len(cases) >= 6
    for c in cases:
        b = C.canonical_bytes(c["doc"])
        assert b.decode("utf-8") == c["canonical"], c["name"]
        assert C.sha256_hex(b) == c["sha256"], c["name"]


def test_floats_cannot_be_canonicalised_or_validated():
    with pytest.raises(C.ContractError) as e:
        C.canonical_bytes({"a": [1, 2.5]})
    assert e.value.code == "schema.float"
    assert e.value.violations[0].path == "/a/1"


def _verdict(c):
    kind = c["kind"]
    if c.get("by_schema_field"):
        try:
            C.validate_doc(c["doc"])
            return []
        except C.ContractError as e:
            return e.violations
    if kind == "event":
        return C.validate_event(c["doc"])
    if kind == "events-log":
        return C.validate_events(c["doc"])
    return C.validate_def(kind, c["doc"])


def test_shared_contract_vectors_give_the_expected_verdicts():
    cases = _load("contract-artifacts.json")["cases"]
    valid = invalid = 0
    for c in cases:
        name = c["name"]
        got = _verdict(c)
        if c["valid"]:
            valid += 1
            assert not got, f"{name} should be valid: {got}"
        else:
            invalid += 1
            assert got, f"{name} should be rejected"
            codes = {v.code for v in got}
            for want in c["errors"]:
                assert want in codes, f"{name}: expected {want}, got {got}"
            if "path" in c:
                assert any(v.path == c["path"] for v in got), f"{name}: expected path {c['path']}, got {got}"
    assert len(cases) >= 40 and valid >= 12 and invalid >= 25, (len(cases), valid, invalid)


def test_unknown_major_and_kind_are_rejected_before_field_checks():
    for doc, code in [
        ({"schema": "alphakeel.run-manifest/2"}, "contract.unknown_major"),
        ({"schema": "alphakeel.sandwich/1"}, "contract.unknown_kind"),
        ({"hello": 1}, "contract.missing_schema"),
        ({"schema": "other.thing/1"}, "contract.unknown_kind"),
    ]:
        with pytest.raises(C.ContractError) as e:
            C.validate_doc(doc)
        assert e.value.code == code


def test_strict_validation_does_not_coerce_types():
    base = next(c for c in _load("contract-artifacts.json")["cases"] if c["name"] == "intent: market order has no limit price")["doc"]
    assert C.validate_def("intent", base) == []
    bad = dict(base, decision_time_ms="1700000000000")
    assert any(v.code == "schema.type" for v in C.validate_def("intent", bad))
    bad = dict(base, reduce_only="false")
    assert any(v.code == "schema.type" for v in C.validate_def("intent", bad))
