"""Strategy artifacts: the original script(s), entry points, parameters and digests, replayable in a clean directory.

A strategy bundle is a deterministic tar.gz (sorted paths, zero mtimes). The manifest's ``content_sha256`` covers every
file path and content, so "the same strategy" is defined by bytes, not by a name. Scripts always run on the
Vibe-Trading side; AlphaKeel stores the bundle as evidence and never executes it.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import platform
import tarfile
from pathlib import Path

from . import canon
from .errors import EvidenceError

MAX_FILES = 200
MAX_FILE_BYTES = 1 << 20


def _files(directory: Path) -> list[Path]:
    out = [p for p in sorted(directory.rglob("*")) if p.is_file() and not any(part.startswith(".") or part == "__pycache__" for part in p.relative_to(directory).parts)]
    if not out or len(out) > MAX_FILES:
        raise EvidenceError("evidence.size", f"a strategy bundle has 1–{MAX_FILES} files (found {len(out)})")
    for p in out:
        if p.stat().st_size > MAX_FILE_BYTES:
            raise EvidenceError("evidence.size", f"{p.name} is larger than {MAX_FILE_BYTES} bytes")
    return out


def make_bundle(directory: str | Path) -> tuple[bytes, list[dict]]:
    d = Path(directory)
    files = _files(d)
    listing = []
    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for p in files:
            rel = p.relative_to(d).as_posix()
            data = p.read_bytes()
            listing.append({"path": rel, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
            ti = tarfile.TarInfo(rel)
            ti.size = len(data)
            ti.mtime = 0
            ti.mode = 0o644
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            tar.addfile(ti, io.BytesIO(data))
    return raw.getvalue(), listing


def content_sha256(listing: list[dict]) -> str:
    return canon.canonical_sha256([{"path": f["path"], "sha256": f["sha256"]} for f in sorted(listing, key=lambda f: f["path"])])


def manifest(directory: str | Path, *, entry: str = "strategy.py", name: str, contract_kind: str = "policy", parameters: dict | None = None,
             requires_fields: list[str] | None = None, history_len: int = 0, modes: list[str] | None = None, profile_ids: list[str] | None = None,
             dependency_lock: str | Path | None = None, seed: int | None = None) -> tuple[dict, bytes]:
    bundle, listing = make_bundle(directory)
    csha = content_sha256(listing)
    if entry not in {f["path"] for f in listing}:
        raise EvidenceError("evidence.reference", f"entry script {entry!r} is not in the bundle")
    lock_sha = hashlib.sha256(Path(dependency_lock).read_bytes()).hexdigest() if dependency_lock else ""
    m = {
        "schema": "alphakeel.strategy-manifest/1", "strategy_id": f"st-{csha[:24]}", "name": name, "content_sha256": csha, "contract": contract_kind,
        "entry": {"script": entry, **({"initialize": "initialize", "on_step": "on_step"} if contract_kind == "policy" else {})},
        "parameters": parameters or {}, "requires": {"fields": requires_fields or ["quotes"], "history_len": history_len},
        "supports": {"modes": modes or (["python_policy", "fixed_intent_replay"] if contract_kind == "policy" else ["fixed_intent_replay"]),
                     "profile_ids": profile_ids or ["ak-top-of-book-ioc-taker-v1"]},
        "files": listing, "dependency_lock_sha256": lock_sha, "interpreter": f"CPython {platform.python_version()}", "seed": seed,
    }
    return m, bundle


def extract_bundle(bundle: bytes, dest: str | Path, expect_content_sha256: str | None = None) -> Path:
    """Replay a stored strategy in a clean directory; every file's digest is re-checked."""
    d = Path(dest)
    d.mkdir(parents=True, exist_ok=True)
    listing = []
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tar:
        for ti in tar.getmembers():
            if not ti.isfile() or ti.name.startswith("/") or ".." in Path(ti.name).parts:
                raise EvidenceError("evidence.path", f"unsafe path in the bundle: {ti.name!r}")
            data = tar.extractfile(ti).read()
            target = d / ti.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            listing.append({"path": ti.name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
    if expect_content_sha256 and content_sha256(listing) != expect_content_sha256:
        raise EvidenceError("evidence.hash_mismatch", "the extracted bundle does not match the manifest's content_sha256")
    return d
