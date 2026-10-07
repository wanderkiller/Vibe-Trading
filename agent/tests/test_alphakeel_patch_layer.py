"""The local patch layer (patches/) is consistent with the working tree.

Runs ``patches/check_patches.py`` in-process: every anchor of every patch group holds, every file the manifest lists
exists, and every file changed since ``patches/BASE`` belongs to a group. See patches/MANIFEST.md and UPGRADE.md.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PATCHES = ROOT / "patches"

pytestmark = pytest.mark.skipif(not (PATCHES / "check_patches.py").is_file(), reason="no patches/ directory")


def _checker():
    spec = importlib.util.spec_from_file_location("check_patches", PATCHES / "check_patches.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(mod)
    return mod


def test_all_anchors_hold_on_the_working_tree():
    mod = _checker()
    results = mod.run_checks(ROOT)
    failed = [f"{r.status} {r.group} {r.kind} {r.file}: {r.anchor} {r.detail}" for r in results if r.failed]
    assert not failed, "\n".join(failed)
    kinds = {r.kind for r in results}
    assert {"upstream", "hook", "preimage", "derived", "file"} <= kinds


def test_manifest_lists_existing_files_and_matches_anchors_json():
    manifest = (PATCHES / "MANIFEST.md").read_text(encoding="utf-8")
    # File lists in the manifest are bullet items "- `path`" (directories end with "/").
    listed = re.findall(r"(?m)^\s*- `([^`]+)`\s*$", manifest)
    assert listed, "no file bullets found in MANIFEST.md"
    missing = [p for p in listed if not (ROOT / p).exists()]
    assert not missing, f"MANIFEST.md lists missing paths: {missing}"
    doc = json.loads((PATCHES / "anchors.json").read_text(encoding="utf-8"))
    in_json = {f for g in doc["groups"] for f in g["files"]}
    assert set(listed) == in_json, (
        f"only in MANIFEST.md: {sorted(set(listed) - in_json)}; only in anchors.json: {sorted(in_json - set(listed))}")
    for g in doc["groups"]:
        assert f"`{g['id']}`" in manifest, f"group {g['id']} has no MANIFEST.md section"


def test_base_is_a_commit_hash():
    base = (PATCHES / "BASE").read_text(encoding="utf-8").split()[0]
    assert re.fullmatch(r"[0-9a-f]{40}", base)


def test_checker_flags_a_regressed_preimage(tmp_path):
    """A conflict resolved to upstream's code (preimage back, hook gone) fails the check."""
    mod = _checker()
    (tmp_path / "patches").mkdir()
    (tmp_path / "patches" / "BASE").write_text("0" * 40 + "\n")
    (tmp_path / "f.py").write_text("old_code()\n")
    doc = {"groups": [{"id": "g", "title": "t", "files": ["f.py"], "anchors": [
        {"file": "f.py", "kind": "preimage", "literal": "old_code()"},
        {"file": "f.py", "kind": "hook", "literal": "new_code()"},
    ]}]}
    (tmp_path / "patches" / "anchors.json").write_text(json.dumps(doc))
    statuses = sorted(r.status for r in mod.check_anchors(doc, tmp_path, None))
    assert statuses == ["MISS", "REGRESSED"]
