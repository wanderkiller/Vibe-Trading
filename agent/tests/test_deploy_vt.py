"""``tools/deploy-vt.sh`` release gate (patch group ``local-deploy``; AlphaKeel audit 2026-10-12 R2-D-7).

The script runs in a throw-away git checkout with ``gh`` and ``docker`` stubbed on ``PATH``: nothing touches a real
container, registry or the user's home. Each refusal must happen before the first ``docker`` call that changes anything,
and be the last line of ``deploy-log.jsonl``.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "deploy-vt.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None, reason="needs bash and git")

GH = """#!/usr/bin/env bash
printf '%s' "$GH_STUB_JSON"
"""

# docker: every call is recorded; `compose stop` fails (the backup step refuses) so the run never reaches `up`
DOCKER = """#!/usr/bin/env bash
echo "$*" >> "$DOCKER_CALLS"
# DOCKER_FAIL: a call that starts with it fails (exit 1), to reach a refusal path
call="$*"
if [ -n "${DOCKER_FAIL:-}" ] && [ "${call#"$DOCKER_FAIL"}" != "$call" ]; then exit 1; fi
case "$*" in
  "compose config --quiet") exit 0 ;;
  "compose config --images vibe-trading") echo "vibe-trading:local" ;;
  "compose ps -q vibe-trading") echo "cid-old" ;;
  "inspect --format {{.Image}} cid-old") echo "sha256:old" ;;
  "image inspect --format {{.Id}} vibe-trading:local") echo "sha256:new" ;;
  "compose build vibe-trading") exit 0 ;;
  tag*) exit 0 ;;
  inspect*) echo "" ;;
  "compose stop vibe-trading") exit 1 ;;
  *) exit 1 ;;
esac
"""


def _exe(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def checkout(tmp_path):
    repo = tmp_path / "vt"
    (repo / "tools").mkdir(parents=True)
    (repo / "agent" / "alphakeel_research" / "contract").mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "tools" / "deploy-vt.sh")
    (repo / "DEPLOYMENT.local.md").write_text("deploy\n")
    (repo / "docker-compose.yml").write_text("services: {}\n")
    (repo / "agent" / "alphakeel_research" / "contract" / "PIN").write_text("pin-1\n")
    (repo / ".gitignore").write_text("/deploy-log.jsonl\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "fixture")
    sha = _git(repo, "rev-parse", "HEAD")
    ak = tmp_path / "ak"
    ak.mkdir()
    (ak / "vibe-trading.ref").write_text(f"# the paired commit\n{sha}\n")
    (ak / "vibe-trading.pin").write_text("pin-1\n")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    _exe(stubs / "gh", GH)
    _exe(stubs / "docker", DOCKER)
    home = tmp_path / "home"
    home.mkdir()
    return {"repo": repo, "sha": sha, "ak_ref": ak / "vibe-trading.ref", "stubs": stubs, "home": home, "calls": tmp_path / "docker-calls"}


def _run(c, *, ci: str = "success", ref: Path | None = None, args: list[str] | None = None, docker_fail: str = ""):
    runs = [{"status": "completed", "conclusion": ci, "headSha": c["sha"], "databaseId": 1}]
    env = {**os.environ, "PATH": f"{c['stubs']}:{os.environ['PATH']}", "HOME": str(c["home"]), "GH_STUB_JSON": json.dumps(runs),
           "DOCKER_CALLS": str(c["calls"]), "DOCKER_FAIL": docker_fail}
    argv = args if args is not None else []
    p = subprocess.run(["bash", "tools/deploy-vt.sh", *argv], cwd=c["repo"], env=env, capture_output=True, text=True, timeout=60)
    log = [json.loads(x) for x in (c["repo"] / "deploy-log.jsonl").read_text().splitlines()]
    calls = c["calls"].read_text().splitlines() if c["calls"].exists() else []
    return p, log[-1], calls


def test_deploy_vt_refuses_red_ci(checkout):
    p, last, calls = _run(checkout, ci="failure")
    assert p.returncode == 1 and "REFUSED at ci" in p.stderr
    assert (last["step"], last["status"], last["sha"]) == ("ci", "refused", checkout["sha"]) and "failure" in last["detail"]
    assert calls == []  # nothing built, tagged or stopped


def test_deploy_vt_refuses_untracked_files(checkout):
    (checkout["repo"] / "agent" / "stray_module.py").write_text("x = 1\n")
    p, last, calls = _run(checkout)
    assert p.returncode == 1 and (last["step"], last["status"]) == ("checkout", "refused") and "untracked" in last["detail"]
    assert calls == []


def test_deploy_vt_restores_the_image_tag_when_refused_after_the_build(checkout):
    p, last, calls = _run(checkout)
    assert p.returncode == 1 and (last["step"], last["status"]) == ("backup", "refused")
    assert "image tag vibe-trading:local restored to sha256:old" in last["detail"]
    build = calls.index("compose build vibe-trading")
    assert "tag sha256:old vibe-trading:local" in calls[build:]  # the compose tag points at the old image again
    assert not any(c.startswith("compose up") for c in calls)
    backup = Path(last["backup_dir"])
    assert backup.is_dir() and stat.S_IMODE(backup.stat().st_mode) == 0o700


def test_deploy_vt_is_standalone(checkout):
    p, last, calls = _run(checkout)
    assert (last["step"], last["status"]) == ("backup", "refused"), p.stderr
    assert last["pair_source"] == "standalone"
    assert "compose build vibe-trading" in calls


@pytest.mark.parametrize("fail, step, what", [
    ("image inspect", "build", "cannot inspect the new image"),
    ("inspect --format {{range", "backup", "cannot list the running container's volumes"),
    (None, "backup", "cannot create the backup directory"),
])
def test_deploy_vt_failures_after_the_build_refuse_and_restore_the_image_tag(checkout, fail, step, what):
    """Round-3 review P2: every failure between the build and the service stop goes through refuse(): one log line and
    the compose tag back on the old image (a bare `set -e` exit did neither)."""
    if fail is None:  # the backup root is a file: `install -d` fails
        (checkout["home"] / "vibe-trading-backups").write_text("not a directory\n")
    p, last, calls = _run(checkout, docker_fail=fail or "")
    assert p.returncode == 1 and (last["step"], last["status"]) == (step, "refused"), p.stderr
    assert what in last["detail"] and "image tag vibe-trading:local restored to sha256:old" in last["detail"]
    build = calls.index("compose build vibe-trading")
    assert "tag sha256:old vibe-trading:local" in calls[build:]
    assert not any(c.startswith(("compose stop", "compose up")) for c in calls)
