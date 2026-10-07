#!/usr/bin/env python3
"""Check the local patch layer over upstream HKUDS/Vibe-Trading (standard library only).

Reads ``patches/anchors.json`` (see ``patches/MANIFEST.md``) and checks every anchor:

* working tree (default): upstream anchors and our hooks are present, the upstream code a patch replaces
  (``preimage``) is absent, derived files (``Dockerfile.local``) still equal their upstream source plus the overlay,
  every listed file exists, and every file changed since ``patches/BASE`` belongs to a patch group;
* ``--upstream REF``: the same anchors against ``git show REF:<file>``, so an upgrade can be assessed before
  rebasing — a missing upstream anchor or a changed preimage means the patch must be re-read and re-applied by hand;
  it also lists the upstream commits since BASE that touch each file we modify, and new files of ours that upstream
  now also has;
* ``--tests``: additionally runs the AlphaKeel, crypto-engine and metrics tests (working tree only).

Exit status 0 when everything passes, 1 otherwise. ``run_checks()`` is the in-process API used by
``agent/tests/test_alphakeel_patch_layer.py``.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PATCHES_DIR = Path(__file__).resolve().parent
REPO_ROOT = PATCHES_DIR.parent
TEST_GLOBS = (
    "agent/tests/test_alphakeel_*.py",
    "agent/tests/test_crypto_engine.py",
    "agent/tests/test_metrics*.py",
)
# Fixture switches the AlphaKeel tests read; they reach pytest through the inherited environment.
FIXTURE_ENV = ("ALPHAKEEL_FIXTURE_SERVER", "ALPHAKEEL_REPO", "ALPHAKEEL_REQUIRE_FIXTURE")
FAILING = {"MISS", "REGRESSED", "CHANGED", "DRIFT", "COLLISION", "UNCOVERED", "MISSING"}


@dataclasses.dataclass
class Result:
    group: str
    kind: str
    file: str
    anchor: str
    status: str
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.status in FAILING


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    except FileNotFoundError:  # no git binary: git-based checks report SKIPPED / MISS
        return subprocess.CompletedProcess(args, 127, "", "git not found")


class Source:
    """File contents from the working tree or from a git revision."""

    def __init__(self, root: Path, ref: str | None):
        self.root, self.ref = root, ref
        self._cache: dict[str, str | None] = {}

    def read(self, path: str) -> str | None:
        if path not in self._cache:
            if self.ref is None:
                p = self.root / path
                self._cache[path] = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None
            else:
                r = _git(self.root, "show", f"{self.ref}:{path}")
                self._cache[path] = r.stdout if r.returncode == 0 else None
        return self._cache[path]

    def exists(self, path: str) -> bool:
        if self.ref is None:
            return (self.root / path).exists()
        r = _git(self.root, "cat-file", "-e", f"{self.ref}:{path.rstrip('/')}")
        return r.returncode == 0


def load_anchors(patches_dir: Path = PATCHES_DIR) -> dict:
    return json.loads((patches_dir / "anchors.json").read_text(encoding="utf-8"))


def read_base(patches_dir: Path = PATCHES_DIR) -> str:
    return (patches_dir / "BASE").read_text(encoding="utf-8").split()[0]


def _present(text: str, anchor: dict) -> bool:
    if "regex" in anchor:
        return re.search(anchor["regex"], text) is not None
    return anchor["literal"] in text


def _label(anchor: dict) -> str:
    if anchor["kind"] == "derived":
        return f"= {anchor['source']} + overlay"
    s = anchor.get("literal") if "literal" in anchor else "re:" + anchor["regex"]
    return s.replace("\n", "\\n")


def _strip_overlay(text: str, begin: str, end: str) -> str | None:
    lines = text.splitlines(keepends=True)
    try:
        i = next(n for n, line in enumerate(lines) if begin in line)
        j = next(n for n in range(i, len(lines)) if end in lines[n])
    except StopIteration:
        return None
    k = j + 1
    if k < len(lines) and not lines[k].strip():
        k += 1
    return "".join(lines[:i] + lines[k:])


def _check_derived(anchor: dict, tree: Source, src: Source) -> tuple[str, str]:
    derived = tree.read(anchor["file"])
    source = src.read(anchor["source"])
    if derived is None or source is None:
        return "MISS", "file missing"
    stripped = _strip_overlay(derived, anchor["overlay_begin"], anchor["overlay_end"])
    if stripped is None:
        return "MISS", "overlay block not found"
    if stripped != source:
        return "DRIFT", f"{anchor['file']} minus the overlay differs from {anchor['source']}; refresh it"
    return "OK", ""


def check_anchors(doc: dict, root: Path, ref: str | None) -> list[Result]:
    tree = Source(root, None)
    src = Source(root, ref) if ref else tree
    out: list[Result] = []
    for g in doc["groups"]:
        for a in g["anchors"]:
            kind, file = a["kind"], a["file"]
            if kind == "derived":
                status, detail = _check_derived(a, tree, src)
                out.append(Result(g["id"], kind, file, _label(a), status, detail))
                continue
            text = src.read(file)
            if text is None:
                if ref and kind == "hook":
                    status, detail = "ABSENT", "file not upstream (re-apply)"
                else:
                    status, detail = "MISS", "file missing"
                out.append(Result(g["id"], kind, file, _label(a), status, detail))
                continue
            found = _present(text, a)
            if kind == "upstream":
                status = "OK" if found else "MISS"
                detail = "" if found else "upstream anchor gone: re-read the file and re-derive the hook"
            elif kind == "hook":
                if ref:
                    status = "PRESENT" if found else "ABSENT"
                    detail = "upstream already contains it" if found else "expected: re-applied by the patch"
                else:
                    status, detail = ("OK", "") if found else ("MISS", "hook missing from the working tree")
            elif kind == "preimage":
                if ref:
                    status, detail = ("OK", "") if found else (
                        "CHANGED", "upstream changed the code this patch replaces: re-read before re-applying")
                else:
                    status, detail = ("OK", "") if not found else (
                        "REGRESSED", "replaced upstream code is back (conflict resolved to upstream?)")
            else:
                raise ValueError(f"unknown anchor kind {kind!r}")
            out.append(Result(g["id"], kind, file, _label(a), status, detail))
    return out


def check_files(doc: dict, root: Path, ref: str | None, base: str | None) -> list[Result]:
    """Working tree: every listed path exists. Upstream mode: new files of ours that upstream now also has, and
    the upstream commits since BASE that touch each upstream file we modify (informational)."""
    out: list[Result] = []
    tree = Source(root, None)
    base_src = Source(root, base) if base else None
    up = Source(root, ref) if ref else None
    seen: set[str] = set()
    for g in doc["groups"]:
        for f in g["files"]:
            if f in seen:
                continue
            seen.add(f)
            if ref is None:
                ok = tree.exists(f)
                out.append(Result(g["id"], "file", f, "exists", "OK" if ok else "MISSING"))
                continue
            ours = base_src is not None and not base_src.exists(f)
            if ours or f.endswith("/"):
                if up.exists(f):
                    out.append(Result(g["id"], "file", f, "new file of ours", "COLLISION",
                                      "upstream now has this path too"))
                continue
            r = _git(root, "log", "--format=%h", f"{base}..{ref}", "--", f)
            commits = r.stdout.split() if r.returncode == 0 else []
            if commits:
                out.append(Result(g["id"], "file", f, "upstream commits since BASE", "REREAD",
                                  f"{len(commits)}: {' '.join(commits[:8])}{' …' if len(commits) > 8 else ''}"))
    return out


def check_coverage(doc: dict, root: Path, base: str) -> list[Result]:
    """Every path that differs from BASE (committed, staged, unstaged or untracked) belongs to a patch group."""
    if _git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}").returncode != 0:
        return [Result("-", "coverage", "-", f"BASE {base[:10]}", "SKIPPED", "BASE not in this repository")]
    changed = set(_git(root, "diff", "--name-only", base).stdout.split())
    changed |= set(_git(root, "ls-files", "--others", "--exclude-standard").stdout.split())
    listed = [f for g in doc["groups"] for f in g["files"]]
    out = []
    for path in sorted(changed):
        if not any(path == f or (f.endswith("/") and path.startswith(f)) for f in listed):
            out.append(Result("-", "coverage", path, f"changed since BASE {base[:10]}", "UNCOVERED",
                              "add it to a group in anchors.json and MANIFEST.md"))
    return out


def run_checks(root: Path = REPO_ROOT, ref: str | None = None, patches_dir: Path | None = None) -> list[Result]:
    patches_dir = patches_dir or root / "patches"
    doc = load_anchors(patches_dir)
    base = read_base(patches_dir)
    results = check_anchors(doc, root, ref)
    results += check_files(doc, root, ref, base)
    if ref is None:
        results += check_coverage(doc, root, base)
    return results


def run_tests(root: Path = REPO_ROOT) -> int:
    files = sorted(str(p.relative_to(root)) for pat in TEST_GLOBS for p in root.glob(pat))
    for name in FIXTURE_ENV:
        if os.environ.get(name):
            print(f"passing through {name}={os.environ[name]}")
    cmd = [sys.executable, "-m", "pytest", *files, "-q", "-p", "no:cacheprovider"]
    print("$", " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=root, env=os.environ.copy()).returncode


def print_table(results: list[Result], *, verbose: bool) -> None:
    rows = [r for r in results if verbose or r.status not in {"OK"}]
    if not rows:
        return
    w = [max(len(getattr(r, c)) for r in rows) for c in ("group", "kind", "status")]
    wf = min(48, max(len(r.file) for r in rows))
    for r in rows:
        anchor = r.anchor if len(r.anchor) <= 70 else r.anchor[:67] + "..."
        line = f"{r.status:<{w[2]}}  {r.group:<{w[0]}}  {r.kind:<{w[1]}}  {r.file:<{wf}}  {anchor}"
        print(line + (f"  -- {r.detail}" if r.detail else ""))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--upstream", metavar="REF", help="check anchors against git REF instead of the working tree")
    ap.add_argument("--tests", action="store_true", help="also run the patch-layer test suite (working tree)")
    ap.add_argument("--json", action="store_true", help="print results as JSON")
    ap.add_argument("-q", "--quiet", action="store_true", help="only print non-OK rows")
    args = ap.parse_args(argv)
    if args.upstream:
        r = _git(REPO_ROOT, "rev-parse", "--verify", "--quiet", f"{args.upstream}^{{commit}}")
        if r.returncode != 0:
            print(f"unknown ref {args.upstream!r}; fetch it first", file=sys.stderr)
            return 2
    results = run_checks(REPO_ROOT, args.upstream)
    if args.json:
        print(json.dumps([dataclasses.asdict(r) for r in results], indent=1, ensure_ascii=False))
    else:
        print_table(results, verbose=not args.quiet)
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    where = f"upstream {args.upstream}" if args.upstream else "working tree"
    failed = [r for r in results if r.failed]
    print(f"\n{where}: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
          + (f"  -> {len(failed)} need attention" if failed else "  -> all anchors hold"), file=sys.stderr)
    rc = 1 if failed else 0
    if args.tests:
        if args.upstream:
            print("--tests runs on the working tree only; skipped in --upstream mode", file=sys.stderr)
        elif run_tests(REPO_ROOT) != 0:
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
