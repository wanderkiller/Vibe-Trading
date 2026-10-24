"""Read file tool: read file contents from the workspace."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from src.agent.tools import BaseTool
from src.tools.path_utils import safe_path as _safe_path
from src.tools.path_utils import safe_run_dir as _safe_run_dir
from src.tools.path_utils import allowed_file_roots
from src.tools.redaction import redact_internal_paths
from src.providers.session_context import current_llm_session_id
from src.tools.alphakeel_research_ledger import engine_rows, engine_snapshot_dir

_OUTPUT_LIMIT = 50_000

# Subdirectories a SKILL.md links into. A link inside a skill document is
# written relative to that document — `references/foo.md` — because that is the
# form that resolves for a human reading the file on GitHub. This tool roots at
# the bundled skills/ directory, so the same string has to be resolved against
# the skill that owns it or the agent cannot open what the document points at.
# The list is deliberately narrow: only these two names are searched, so an
# arbitrary model-supplied relative path never starts reaching into skills/.
_SKILL_RELATIVE_PREFIXES = ("references/", "scripts/")

# How many entries a directory listing returns.
_LISTING_LIMIT = 200

# What the refusal says, whichever root the path missed. A risk-parity run once
# spent all eight of its no-progress rounds trying spellings of the engine's
# source path (``../../agent/src/backtest``, ``skills/../backtest/engine.py``)
# against one message that said neither where it may read nor what to do.
_READABLE = (
    "read_file opens files under the run directory (a relative path such as "
    "artifacts/metrics.csv or config.json) and the bundled skill documents "
    "(skills/<skill>/...). The engine's source code is not readable here; its "
    "contracts and options are documented in the skills: load_skill(name) lists "
    "a skill's sections, and load_skill(name, section=...) returns one."
)


# `trials/NNNN/<rel>`: the files of the current conversation's generic-backtest run NNNN (the number on the result
# card). Read-only snapshots outside every directory the model can write.
_TRIAL_RE = re.compile(r"^trials/(\d{4})/(.+)$")
_TRIAL_FILES = ("code/signal_engine.py", "config.json", "artifacts/metrics.csv", "screen.json")
_TRIAL_FORMS = "trials/NNNN/" + " | trials/NNNN/".join(_TRIAL_FILES) + " (NNNN = the four-digit trial number on the result card)"


def _trial_error(code: str, message: str) -> str:
    return json.dumps({"status": "error", "error_code": code, "error": message}, ensure_ascii=False)


def _read_trial(file_path: str, limit: Any) -> tuple[Path | None, str | None]:
    """Resolve a ``trials/`` path to a snapshot file, or return an error JSON."""
    match = _TRIAL_RE.fullmatch(file_path)
    if match is None or match.group(2) not in _TRIAL_FILES:
        return None, _trial_error("not_allowed", f"{file_path} is not a readable trial path. Allowed: {_TRIAL_FORMS}.")
    session_id = current_llm_session_id()
    if not session_id:
        return None, _trial_error("no_session", "trials/ reads the current session's engine runs; no session is bound.")
    seq = int(match.group(1))
    path = engine_snapshot_dir(session_id, seq) / match.group(2)
    # a snapshot is readable only when this session's engine ledger has the row it belongs to
    row = next((r for r in reversed(engine_rows(session_id)) if r.get("seq") == seq), None)
    if row is None or not path.is_file():
        runs = engine_snapshot_dir(session_id, seq).parent
        existing = sorted(p.name for p in runs.iterdir() if p.is_dir() and p.name.isdigit()) if runs.is_dir() else []
        listing = ", ".join(existing) if existing else "none"
        return None, _trial_error("not_found", f"{file_path} was not found. Existing trials of this session: {listing}.")
    expected = {"code/signal_engine.py": row.get("code_sha256"), "config.json": row.get("config_sha256"),
                "screen.json": row.get("screen_sha256")}.get(match.group(2))
    if isinstance(expected, str) and expected:
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()  # the ledger hashes the file bytes the same way
        except OSError:
            actual = None
        if actual != expected:
            return None, _trial_error("snapshot_mismatch", f"{file_path} no longer matches the ledger row of trial {seq:04d}; "
                                      "the snapshot was modified after it was recorded and is not readable.")
    return path, None


def _bundled_skills_dir() -> Path:
    """Return the bundled read-only skills root."""
    return Path(__file__).resolve().parents[1] / "skills"


def _skill_relative_matches(file_path: str, skills_dir: Path) -> list[tuple[str, Path]]:
    """Return the bundled skills that carry ``file_path`` inside their directory.

    Args:
        file_path: Skill-relative path as written in a SKILL.md link, e.g.
            ``references/sec_edgar_client.md``.
        skills_dir: Bundled read-only skills root.

    Returns:
        ``(skill_name, path)`` for each owning skill, in skill-name order. Empty
        when no bundled skill carries the path.
    """
    # A link written in a skill document never needs to climb, and `..` would
    # let a path that merely *starts* with an allowed prefix land somewhere
    # else — `references/../SKILL.md` matching all 90 skills, for one. Refusing
    # the segment outright keeps "starts with references/" and "stays under
    # references/" the same statement.
    if ".." in file_path.replace("\\", "/").split("/"):
        return []

    matches: list[tuple[str, Path]] = []
    for skill_dir in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        try:
            candidate = _safe_path(file_path, skill_dir)
        except ValueError:
            # Absolute paths and UNC shares, rejected by the same containment
            # check the skills/ root already applies, enforced one level down
            # so the fallback cannot widen the boundary.
            continue
        if candidate.is_file():
            matches.append((skill_dir.name, candidate))
    return matches


class ReadFileTool(BaseTool):
    """Read file contents with optional line limit."""

    name = "read_file"
    description = "Read a file from the workspace. Returns file contents with optional line limit."
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "File path relative to run_dir or skills/. "
                    "The skills/ prefix always resolves to the bundled skills; "
                    "trials/NNNN/code/signal_engine.py and trials/NNNN/config.json read an earlier generic backtest of this session. "
                    "A references/ or scripts/ path copied out of a SKILL.md "
                    "resolves against the skill that owns it."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Max number of lines to return (default: all)",
            },
        },
        "required": ["path"],
    }
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        """Read a file.

        Args:
            **kwargs: Must include path. Optional limit and run_dir.

        Returns:
            JSON string containing content or an error.
        """
        file_path = kwargs["path"]
        limit = kwargs.get("limit")
        run_dir = kwargs.get("run_dir")

        allowed_roots = []
        if run_dir:
            try:
                allowed_roots.append(_safe_run_dir(str(run_dir)))
            except ValueError as exc:
                return json.dumps(
                    {
                        "status": "error",
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                )
        # Read-only access to skills/
        skills_dir = _bundled_skills_dir()
        if skills_dir.exists():
            allowed_roots.append(skills_dir.resolve())

        # Add configured extra file roots (VIBE_TRADING_ALLOWED_FILE_ROOTS)
        for extra_root in allowed_file_roots():
            if extra_root not in allowed_roots:
                allowed_roots.append(extra_root)

        if file_path.startswith("trials/"):
            trial_path, trial_error = _read_trial(file_path, limit)
            if trial_error is not None:
                return trial_error
            try:
                text = trial_path.read_text(encoding="utf-8")
                if limit and limit > 0:
                    text = "".join(text.splitlines(keepends=True)[:limit])
                if len(text) > _OUTPUT_LIMIT:
                    text = text[:_OUTPUT_LIMIT] + "\n... (truncated)"
                return json.dumps({"status": "ok", "path": str(trial_path), "content": text}, ensure_ascii=False)
            except Exception as exc:
                return json.dumps({"status": "error", "error": redact_internal_paths(str(exc))}, ensure_ascii=False)

        resolved = None
        namespaced = False
        # Whether any root contains the path at all: a path inside a root that
        # does not exist is "not found", one outside every root is refused.
        contained = False

        # `skills/` is a namespace bound to the bundled read-only skills root.
        # Binding the prefix stops a same-named file in run_dir — which the agent
        # itself can write — from shadowing a bundled skill and being loaded as
        # trusted guidance.
        if file_path.startswith("skills/"):
            namespaced = True
            try:
                candidate = _safe_path(file_path[len("skills/") :], skills_dir)
                contained = True
            except ValueError:
                candidate = None
            if candidate is not None and candidate.exists():
                resolved = candidate

        # Unprefixed paths search every allowed root, run_dir first.
        if resolved is None and not namespaced:
            for root in allowed_roots:
                try:
                    candidate = _safe_path(file_path, root)
                    contained = True
                    if candidate.exists():
                        resolved = candidate
                        break
                except ValueError:
                    continue

        # Last resort: a skill-relative link. Tried only after every allowed
        # root has already missed, so nothing that resolves today changes, and
        # it reads out of the read-only skills tree, so run_dir cannot poison
        # it. An ambiguous path is reported rather than guessed — silently
        # picking one of two same-named references would hand the agent a
        # document from the wrong skill and read as a correct answer.
        if (
            resolved is None
            and not namespaced
            and skills_dir.exists()
            and file_path.startswith(_SKILL_RELATIVE_PREFIXES)
        ):
            matches = _skill_relative_matches(file_path, skills_dir)
            if len(matches) == 1:
                resolved = matches[0][1]
            elif len(matches) > 1:
                owners = ", ".join(name for name, _ in matches)
                return json.dumps(
                    {
                        "status": "error",
                        "error": (
                            f"Ambiguous skill-relative path: {file_path} exists in "
                            f"{len(matches)} skills ({owners}). Prefix it with the "
                            f"skill name, e.g. skills/<skill>/{file_path}."
                        ),
                    },
                    ensure_ascii=False,
                )

        if resolved is None:
            if contained:
                error_code = "not_found"
                reason = f"{file_path} was not found."
            else:
                error_code = "outside_readable_roots"
                reason = f"{file_path} is outside what read_file may open."
            return json.dumps(
                {
                    "status": "error",
                    "error_code": error_code,
                    "error": f"{reason} {_READABLE}",
                },
                ensure_ascii=False,
            )

        try:
            if resolved.is_dir():
                # A directory is a question about what is in it.
                entries = sorted(
                    child.name + ("/" if child.is_dir() else "")
                    for child in resolved.iterdir()
                    if not child.name.startswith(".")
                )
                return json.dumps(
                    {
                        "status": "ok",
                        "path": str(resolved),
                        "kind": "directory",
                        "entries": entries[:_LISTING_LIMIT],
                        "truncated": len(entries) > _LISTING_LIMIT,
                    },
                    ensure_ascii=False,
                )

            text = resolved.read_text(encoding="utf-8")
            if limit and limit > 0:
                lines = text.splitlines(keepends=True)
                text = "".join(lines[:limit])
            if len(text) > _OUTPUT_LIMIT:
                text = text[:_OUTPUT_LIMIT] + "\n... (truncated)"
            return json.dumps(
                {
                    "status": "ok",
                    "path": str(resolved),
                    "content": text,
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps(
                {
                    "status": "error",
                    "error": redact_internal_paths(str(exc)),
                },
                ensure_ascii=False,
            )
