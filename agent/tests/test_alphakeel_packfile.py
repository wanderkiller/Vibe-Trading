"""PackCache eviction: least recently used first, never recent units, never .part files or locks/."""

from __future__ import annotations

import hashlib
import os
import time

from alphakeel_research import packfile
from alphakeel_research.packfile import PackCache

NOW = 1_800_000_000.0
OLD = NOW - 10 * 3600


def _put(path, n, mtime):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * n)
    os.utime(path, (mtime, mtime))


def test_evict_removes_oldest_first_and_keeps_recent_part_and_locks(tmp_path):
    c = PackCache(tmp_path, max_bytes=10**12)
    _put(c.root / "content" / "a", 100, OLD - 300)
    _put(c.root / "content" / "b", 100, OLD - 200)
    _put(c.root / "content" / "recent", 100, NOW - 60)  # inside the 2 h guard
    _put(c.root / "content" / "z.1.2.part", 100, OLD - 900)
    _put(c.root / "locks" / "l", 100, OLD - 900)
    _put(c.root / "spill" / "k1" / "f", 100, OLD - 250)
    (c.root / "spill" / "k1" / "g").write_bytes(b"y" * 50)
    c.max_bytes = 200
    freed = c.evict(now=NOW)
    # oldest first: a (100), then k1 (150), then b; stops once 200 is met
    assert not (c.root / "content" / "a").exists() and not (c.root / "spill" / "k1").exists()
    assert (c.root / "content" / "b").exists() and freed == 250
    assert (c.root / "content" / "recent").exists()
    assert (c.root / "content" / "z.1.2.part").exists() and (c.root / "locks" / "l").exists()


def test_evict_never_deletes_units_younger_than_the_guard_even_over_the_limit(tmp_path):
    c = PackCache(tmp_path, max_bytes=1)
    _put(c.root / "content" / "r", 100, NOW - 3600)
    assert c.evict(now=NOW) == 0 and (c.root / "content" / "r").exists()


def test_constructor_evicts_with_env_limit_and_bad_env_falls_back(tmp_path, monkeypatch):
    _put(tmp_path / "content" / "old", 100, time.time() - 10 * 3600)
    monkeypatch.setenv(packfile.CACHE_MAX_BYTES_ENV, "10")
    PackCache(tmp_path, evict=True)
    assert not (tmp_path / "content" / "old").exists()
    _put(tmp_path / "content" / "old", 100, time.time() - 10 * 3600)
    monkeypatch.setenv(packfile.CACHE_MAX_BYTES_ENV, "garbage")
    c = PackCache(tmp_path, evict=True)
    assert c.max_bytes == packfile.CACHE_MAX_BYTES_DEFAULT and (tmp_path / "content" / "old").exists()


def test_ensure_hit_refreshes_mtime(tmp_path):
    c = PackCache(tmp_path, max_bytes=10**12)
    data = b"hello\n"
    csha = hashlib.sha256(data).hexdigest()
    _put(c.content_path(csha), 0, OLD)
    c.content_path(csha).write_bytes(data)
    os.utime(c.content_path(csha), (OLD, OLD))
    assert c.ensure(None, "p", {"content_sha256": csha}) == c.content_path(csha)
    assert c.content_path(csha).stat().st_mtime > time.time() - 600


def test_from_directory_never_evicts_an_exported_pack(tmp_path, monkeypatch):
    # an exported pack directory is read-only evidence (maybe the only copy): opening it must not run eviction
    monkeypatch.setattr(packfile, "verify_lock", lambda lock: None)
    monkeypatch.setenv(packfile.CACHE_MAX_BYTES_ENV, "1")
    (tmp_path / "lock.json").write_text('{"pack_id": "p", "objects": []}', encoding="utf-8")
    f = tmp_path / "content" / "old"
    _put(f, 100, time.time() - 3 * 3600)
    packfile.Pack.from_directory(tmp_path)
    assert f.exists()


def test_network_cache_construction_still_evicts(tmp_path):
    f = tmp_path / "content" / "old"
    _put(f, 100, time.time() - 3 * 3600)
    PackCache(tmp_path, max_bytes=1, evict=True)
    assert not f.exists()


def test_exported_pack_read_hit_refreshes_mtime(tmp_path, monkeypatch):
    monkeypatch.setattr(packfile, "verify_lock", lambda lock: None)
    old = time.time() - 10 * 3600
    data = b"hello\n"
    csha = hashlib.sha256(data).hexdigest()
    (tmp_path / "lock.json").write_text('{"pack_id": "p", "objects": []}', encoding="utf-8")
    p = tmp_path / "content" / csha
    _put(p, 0, old)
    p.write_bytes(data)
    os.utime(p, (old, old))
    pack = packfile.Pack.from_directory(tmp_path)
    pack._path({"name": "x", "content_sha256": csha})
    assert p.stat().st_mtime > old + 3600
