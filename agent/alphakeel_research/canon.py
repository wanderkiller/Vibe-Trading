"""Canonical JSON and hashes: standard library only (this module is imported inside the strategy sandbox).

Rules (byte-identical with the Rust implementation, checked by the shared vectors): keys sorted by code point, no
whitespace, UTF-8 verbatim, integers only. Floats are refused anywhere: money, price, rate and quantity are decimal
strings, nanoseconds are digit strings.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


class FloatError(ValueError):
    def __init__(self, path: str):
        super().__init__(f"floating-point numbers are not allowed (at {path or '/'})")
        self.path = path or "/"


def _check(v: Any, path: str) -> None:
    if isinstance(v, float):
        raise FloatError(path)
    if isinstance(v, list):
        for i, x in enumerate(v):
            _check(x, f"{path}/{i}")
    elif isinstance(v, dict):
        for k, x in v.items():
            _check(x, f"{path}/{k}")


def canonical_bytes(doc: Any) -> bytes:
    _check(doc, "")
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def canonical_sha256(doc: Any) -> str:
    return sha256_hex(canonical_bytes(doc))
