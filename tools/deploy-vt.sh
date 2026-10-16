#!/usr/bin/env bash
# deploy-vt.sh — the only way the production Vibe-Trading container is rebuilt (local patch layer, group `local-deploy`).
#
# Run it from the production checkout (the directory holding docker-compose.yml) after checking out the commit to
# deploy. It refuses unless every gate holds, in this order:
#   1. the checkout is clean -- no modified and no untracked files (an untracked module under agent/ would be copied into
#      the image, which must be exactly the commit CI tested) -- and HEAD is a full commit;
#   2. GitHub Actions workflow "Deploy gate" (.github/workflows/deploy-gate.yml: Linux, Python 3.11 + frontend, the
#      production target; upstream's "CI" matrix no longer gates) has a completed run on exactly that commit with conclusion `success`
#      (`gh run list --commit`); a red, cancelled, pending or missing run refuses;
#   3. HEAD equals AlphaKeel's `ci/vibe-trading.ref` and this checkout's `agent/alphakeel_research/contract/PIN` is
#      byte-identical to AlphaKeel's `ci/vibe-trading.pin`, both read from AlphaKeel's PUBLISHED main: `git fetch origin
#      main` in the AlphaKeel checkout (--ak-repo, default /home/ubuntu/alphakeel), then `git show origin/main:ci/...`
#      (never its working tree, which may be ahead of or behind what is released; audit 2026-10-12 R2-D-1). --ak-ref /
#      --ak-pin override that with files (diagnosis only; --ak-pin defaults to next to --ak-ref) and are refused unless
#      --allow-pair-override is also given (round-3 review: an override must be a deliberate act, not a leftover
#      argument); the log line records `pair_source` (`origin/main@<sha>` or `override:<paths>`);
#   4. the new image builds (`docker compose build`) while the old container still serves; a refusal after the build
#      points the compose image tag back at the old image (a later manual `compose up` must not start an unverified one);
#   5. backup to ~/vibe-trading-backups/<ts>/ (mode 700: the volumes may hold credentials): the old image is kept under the tag `vibe-trading-rollback:<ts>`
#      (image id recorded in image.json), `agent/.env` is copied (never printed), and every named volume of the running
#      container is archived with the service stopped (consistent session database);
#   6. `up -d --no-build --no-deps`, then `/health` on 127.0.0.1:8899 and `python -m alphakeel_research check` inside
#      the container must report `contract_pin: matched` (deploy AlphaKeel first when the pin changed);
#      any failure here retags the rollback image and recreates the old container. Volumes are NOT restored
#      automatically (the new container may already have written to them); DEPLOYMENT.local.md has the manual restore.
#   7. after a successful deploy only: prune old `vibe-trading-rollback:*` tags (keep the newest VT_ROLLBACK_KEEP,
#      default 3, and always this run's tag), then `docker builder prune --filter until=${VT_BUILDER_MAX_AGE:-72h}` (by age: works on BuildKit and the legacy builder, which ignores --keep-storage);
#      failures only warn and never change the exit status (log step `prune`).
# Every attempt (refused, rolled back or deployed) appends one JSON line to deploy-log.jsonl next to
# DEPLOYMENT.local.md: ts, sha, step, status, image ids, pin digest. Exit status is non-zero unless deployed.
#
# Usage:
#   tools/deploy-vt.sh [--ak-repo /home/ubuntu/alphakeel] [--allow-pair-override --ak-ref PATH [--ak-pin PATH]] [--repo OWNER/NAME]
#
# This script changes production; run it only with the user's explicit go-ahead (AGENTS.md rule 14).
set -euo pipefail

AK_REPO="/home/ubuntu/alphakeel" AK_REF="" AK_PIN="" GH_REPO="wanderkiller/Vibe-Trading" ALLOW_PAIR_OVERRIDE=0
SERVICE="vibe-trading" HEALTH_URL="http://127.0.0.1:8899/health" HEALTH_WAIT_S=180
while [ $# -gt 0 ]; do
    case "$1" in
        --ak-repo) AK_REPO="$2"; shift 2 ;;
        --ak-ref) AK_REF="$2"; shift 2 ;;
        --ak-pin) AK_PIN="$2"; shift 2 ;;
        --allow-pair-override) ALLOW_PAIR_OVERRIDE=1; shift ;;
        --repo) GH_REPO="$2"; shift 2 ;;
        -h|--help) sed -n '2,/^set -euo/{/^#/p}' "$0"; exit 0 ;;
        *) echo "deploy-vt: unknown argument $1" >&2; exit 2 ;;
    esac
done
PAIR_OVERRIDE=0
{ [ -n "$AK_REF" ] || [ -n "$AK_PIN" ]; } && PAIR_OVERRIDE=1
[ -n "$AK_REF" ] && [ -z "$AK_PIN" ] && AK_PIN="$(dirname "$AK_REF")/vibe-trading.pin"
PAIR_SOURCE=""

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
[ -f DEPLOYMENT.local.md ] && [ -f docker-compose.yml ] || { echo "deploy-vt: run from the Vibe-Trading checkout" >&2; exit 2; }
LOG="$ROOT/deploy-log.jsonl"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$HOME/vibe-trading-backups/$TS"
SHA="$(git rev-parse HEAD)"
OLD_IMAGE_ID="" NEW_IMAGE_ID="" ROLLBACK_TAG="vibe-trading-rollback:$TS" IMAGE="" BUILT=0
PIN_SHA="$(sha256sum agent/alphakeel_research/contract/PIN 2>/dev/null | cut -d' ' -f1 || true)"

log_line() {  # step status [detail]
    python3 -I -c 'import json,sys
k=["ts","sha","step","status","detail","old_image_id","new_image_id","rollback_tag","pin_sha256","backup_dir","pair_source"]
print(json.dumps(dict(zip(k,sys.argv[1:])),sort_keys=True))' \
        "$TS" "$SHA" "$1" "$2" "${3:-}" "$OLD_IMAGE_ID" "$NEW_IMAGE_ID" "$ROLLBACK_TAG" "$PIN_SHA" "$BACKUP" "$PAIR_SOURCE" >>"$LOG"
}
refuse() {  # step detail — the old container keeps (or resumes) serving the old image
    local detail="$2"
    if [ "$BUILT" = 1 ]; then  # `compose build` moved the service's image tag to the unverified new image: move it back
        if docker tag "$OLD_IMAGE_ID" "$IMAGE"; then
            detail="$detail; image tag $IMAGE restored to $OLD_IMAGE_ID"
        else
            detail="$detail; RESTORING the image tag $IMAGE to $OLD_IMAGE_ID FAILED"
        fi
    fi
    log_line "$1" refused "$detail" || echo "deploy-vt: WARNING: cannot write $LOG" >&2
    echo "deploy-vt: REFUSED at $1: $detail" >&2
    exit 1
}
rollback() {  # step detail — the new container is up but failed its checks
    echo "deploy-vt: $1 failed ($2); rolling back to $ROLLBACK_TAG" >&2
    if ! docker tag "$ROLLBACK_TAG" "$IMAGE"; then
        log_line "$1" rollback_failed "$2; retagging $ROLLBACK_TAG as $IMAGE failed" || true
        exit 1
    fi
    if docker compose up -d --no-build --no-deps "$SERVICE"; then
        log_line "$1" rolled_back "$2; volumes not restored (backup $BACKUP)"
        echo "deploy-vt: volumes were not restored; if the new image migrated them, restore from $BACKUP (DEPLOYMENT.local.md)" >&2
    else
        log_line "$1" rollback_failed "$2"
    fi
    exit 1
}

prune_old() {  # step 7: runs only after a successful deploy; never fails the deploy
    local keep="${VT_ROLLBACK_KEEP:-3}" tag removed="" warn="" out reclaimed n=0
    case "$keep" in ''|*[!0-9]*) keep=3 ;; esac
    while IFS= read -r tag; do
        [ -n "$tag" ] || continue
        n=$((n + 1))
        if [ "$tag" = "$ROLLBACK_TAG" ] || [ "$n" -le "$keep" ]; then continue; fi
        if docker image rm "$tag" >/dev/null 2>&1; then
            echo "deploy-vt: prune: removed $tag"
            removed="$removed $tag"
        else
            echo "deploy-vt: prune: kept $tag (docker refused to remove it)" >&2
        fi
    done < <(docker images --format '{{.Repository}}:{{.Tag}}' vibe-trading-rollback 2>/dev/null \
                 | grep '^vibe-trading-rollback:' | sort -r || true)
    if out="$(docker builder prune -f --filter "until=${VT_BUILDER_MAX_AGE:-72h}" 2>&1)"; then
        reclaimed="$(printf '%s\n' "$out" | grep -i 'total' | tail -1 || true)"
        echo "deploy-vt: prune: builder cache pruned (${reclaimed:-no total reported})"
    else
        warn=1
        reclaimed="builder prune failed"
        echo "deploy-vt: WARNING: docker builder prune failed" >&2
    fi
    log_line prune "${warn:+warn}${warn:-ok}" "removed:${removed:- none}; ${reclaimed:-}" || true
}

# 1. clean checkout: nothing modified and nothing untracked (git-ignored files such as agent/.env are not in the build context
#    by .dockerignore, or are the deployment's own env)
[ -z "$(git status --porcelain)" ] || refuse checkout "the checkout has modified or untracked files (the image must be the tested commit)"

# 2. CI on this exact commit
CI_JSON="$(gh run list --repo "$GH_REPO" --workflow "Deploy gate" --commit "$SHA" --json status,conclusion,headSha,databaseId --limit 20)" \
    || refuse ci "gh run list failed"
CI_OK="$(printf '%s' "$CI_JSON" | python3 -I -c 'import json,sys
sha=sys.argv[1]; runs=[r for r in json.load(sys.stdin) if r.get("headSha")==sha and r.get("status")=="completed"]
print("success" if runs and runs[0].get("conclusion")=="success" else (runs[0].get("conclusion") if runs else "none"))' "$SHA")"
[ "$CI_OK" = success ] || refuse ci "latest completed Deploy gate run on $SHA: $CI_OK"

# 3. pairing with AlphaKeel: its published main, unless overridden by files -- and only on purpose
[ "$PAIR_OVERRIDE" = 0 ] || [ "$ALLOW_PAIR_OVERRIDE" = 1 ] \
    || refuse pairing "--ak-ref/--ak-pin override AlphaKeel's published pair; pass --allow-pair-override to use them (diagnosis only)"
if [ -z "$AK_REF" ] || [ -z "$AK_PIN" ]; then
    PAIR_TMP="$(mktemp -d)"
    trap 'rm -rf "$PAIR_TMP"' EXIT
    git -C "$AK_REPO" fetch --quiet origin main || refuse pairing "git fetch origin main failed in $AK_REPO"
    AK_MAIN="$(git -C "$AK_REPO" rev-parse --verify --quiet 'origin/main^{commit}')" || refuse pairing "no origin/main in $AK_REPO"
    PAIR_SOURCE="origin/main@$AK_MAIN"
    if [ -z "$AK_REF" ]; then
        AK_REF="$PAIR_TMP/vibe-trading.ref"
        git -C "$AK_REPO" show "$AK_MAIN:ci/vibe-trading.ref" >"$AK_REF" || refuse pairing "no ci/vibe-trading.ref on AlphaKeel origin/main"
    fi
    if [ -z "$AK_PIN" ]; then
        AK_PIN="$PAIR_TMP/vibe-trading.pin"
        git -C "$AK_REPO" show "$AK_MAIN:ci/vibe-trading.pin" >"$AK_PIN" || refuse pairing "no ci/vibe-trading.pin on AlphaKeel origin/main"
    fi
fi
case "$AK_REF" in "${PAIR_TMP:-//}"/*) ;; *) PAIR_SOURCE="${PAIR_SOURCE:+$PAIR_SOURCE }override:ref=$AK_REF" ;; esac
case "$AK_PIN" in "${PAIR_TMP:-//}"/*) ;; *) PAIR_SOURCE="${PAIR_SOURCE:+$PAIR_SOURCE }override:pin=$AK_PIN" ;; esac
[ -f "$AK_REF" ] || refuse pairing "no ref file $AK_REF"
[ -f "$AK_PIN" ] || refuse pairing "no pin file $AK_PIN"
WANT="$(grep -Ev '^[[:space:]]*(#|$)' "$AK_REF" | head -1 | tr -d '[:space:]')"
[ "$WANT" = "$SHA" ] || refuse pairing "HEAD $SHA != AlphaKeel ref $WANT"
cmp -s agent/alphakeel_research/contract/PIN "$AK_PIN" || refuse pairing "contract/PIN differs from $AK_PIN"

# 4. build the new image while the old container serves
# every command from here on that can fail ends in `|| refuse ...` (or `|| rollback ...` once the new container is up):
# under `set -e` a bare failure would exit without restoring the image tag or writing the log line
docker compose config --quiet || refuse build "docker compose config failed"
IMAGE="$(docker compose config --images "$SERVICE" | head -1)" || refuse build "docker compose config --images failed"
[ -n "$IMAGE" ] || refuse build "docker compose config names no image for $SERVICE"
CID="$(docker compose ps -q "$SERVICE" || true)"
[ -n "$CID" ] || refuse build "no running $SERVICE container to back up"
OLD_IMAGE_ID="$(docker inspect --format '{{.Image}}' "$CID")" || refuse build "cannot read the running container's image id"
[ -n "$OLD_IMAGE_ID" ] || refuse build "the running container reports no image id"
docker tag "$OLD_IMAGE_ID" "$ROLLBACK_TAG" || refuse build "cannot tag the old image"
BUILT=1
docker compose build "$SERVICE" || refuse build "docker compose build failed"
NEW_IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$IMAGE")" || refuse build "cannot inspect the new image $IMAGE"

# 5. backup: image tag + id, env, volumes (service stopped while archiving)
install -d -m 700 "$HOME/vibe-trading-backups" "$BACKUP" || refuse backup "cannot create the backup directory $BACKUP"
python3 -I -c 'import json,sys; json.dump(dict(zip(["image","old_image_id","rollback_tag","sha_to_deploy"],sys.argv[1:5])),open(sys.argv[5],"w"),indent=1)' \
    "$IMAGE" "$OLD_IMAGE_ID" "$ROLLBACK_TAG" "$SHA" "$BACKUP/image.json" || refuse backup "cannot write $BACKUP/image.json"
if [ -f agent/.env ]; then
    install -m 600 agent/.env "$BACKUP/agent.env" || refuse backup "cannot copy agent/.env into $BACKUP"
fi
VOLUMES="$(docker inspect --format '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}}={{.Destination}}{{"\n"}}{{end}}{{end}}' "$CID")" \
    || refuse backup "cannot list the running container's volumes"
docker compose stop "$SERVICE" || refuse backup "cannot stop $SERVICE"
for pair in $VOLUMES; do
    vol="${pair%%=*}"
    if ! docker run --rm --user 0 --entrypoint tar -v "$vol:/data:ro" -v "$BACKUP:/backup" "$ROLLBACK_TAG" \
            czf "/backup/$vol.tgz" -C /data .; then
        docker compose start "$SERVICE" || true
        refuse backup "archiving volume $vol failed (old container restarted)"
    fi
done
log_line backup ok "$(printf '%s' "$VOLUMES" | tr '\n' ' ')" \
    || { docker compose start "$SERVICE" || true; refuse backup "cannot write the deploy log (old container restarted)"; }

# 6. start the new image and check it
docker compose up -d --no-build --no-deps "$SERVICE" || rollback up "docker compose up failed"
deadline=$(( $(date +%s) + HEALTH_WAIT_S ))
until curl -fsS -o /dev/null "$HEALTH_URL"; do
    [ "$(date +%s)" -lt "$deadline" ] || rollback health "$HEALTH_URL not healthy within ${HEALTH_WAIT_S}s"
    sleep 3
done
CHECK="$(docker compose exec -T "$SERVICE" python -m alphakeel_research check 2>&1)" \
    || rollback contract "alphakeel_research check failed: $(printf '%s' "$CHECK" | tail -1)"
printf '%s' "$CHECK" | grep -q '"contract_pin": *"matched"' || rollback contract "contract_pin is not matched"

log_line deploy deployed ""
echo "deploy-vt: deployed $SHA (image $NEW_IMAGE_ID; rollback tag $ROLLBACK_TAG; backup $BACKUP)"

# 7. housekeeping after success (best effort)
prune_old || echo "deploy-vt: WARNING: prune step failed" >&2
