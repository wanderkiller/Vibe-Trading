#!/usr/bin/env bash
# Standalone production deploy: requires a clean checkout and a successful
# Deploy gate for HEAD, backs up the running image, environment and volumes,
# checks /health, and rolls back the image if startup fails. Run only with
# explicit user authorization. Bootstrap from retirement is documented in
# DEPLOYMENT.local.md; this script requires an existing running container.
set -euo pipefail

GH_REPO="wanderkiller/Vibe-Trading"
SERVICE="vibe-trading" HEALTH_URL="http://127.0.0.1:8899/health" HEALTH_WAIT_S=180
while [ $# -gt 0 ]; do
    case "$1" in
        --repo) GH_REPO="$2"; shift 2 ;;
        -h|--help) sed -n '2,/^set -euo/{/^#/p}' "$0"; exit 0 ;;
        *) echo "deploy-vt: unknown argument $1" >&2; exit 2 ;;
    esac
done
PAIR_SOURCE="standalone"

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
log_line deploy deployed ""
echo "deploy-vt: deployed $SHA (image $NEW_IMAGE_ID; rollback tag $ROLLBACK_TAG; backup $BACKUP)"

# 7. housekeeping after success (best effort)
prune_old || echo "deploy-vt: WARNING: prune step failed" >&2
