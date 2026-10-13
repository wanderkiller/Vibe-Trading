#!/usr/bin/env bash
# deploy-vt.sh — the only way the production Vibe-Trading container is rebuilt (local patch layer, group `local-deploy`).
#
# Run it from the production checkout (the directory holding docker-compose.yml) after checking out the commit to
# deploy. It refuses unless every gate holds, in this order:
#   1. the checkout is clean and HEAD is a full commit;
#   2. GitHub Actions workflow "Deploy gate" (.github/workflows/deploy-gate.yml: Linux, Python 3.11 + frontend, the
#      production target; upstream's "CI" matrix no longer gates) has a completed run on exactly that commit with conclusion `success`
#      (`gh run list --commit`); a red, cancelled, pending or missing run refuses;
#   3. HEAD equals AlphaKeel's `ci/vibe-trading.ref` (path given by --ak-ref) and this checkout's
#      `agent/alphakeel_research/contract/PIN` is byte-identical to AlphaKeel's `ci/vibe-trading.pin`
#      (default: next to the ref file; --ak-pin overrides);
#   4. the new image builds (`docker compose build`) while the old container still serves;
#   5. backup to ~/vibe-trading-backups/<ts>/: the old image is kept under the tag `vibe-trading-rollback:<ts>`
#      (image id recorded in image.json), `agent/.env` is copied (never printed), and every named volume of the running
#      container is archived with the service stopped (consistent session database);
#   6. `up -d --no-build --no-deps`, then `/health` on 127.0.0.1:8899 and `python -m alphakeel_research check` inside
#      the container must report `contract_pin: matched` (deploy AlphaKeel first when the pin changed);
#      any failure here retags the rollback image and recreates the old container.
# Every attempt (refused, rolled back or deployed) appends one JSON line to deploy-log.jsonl next to
# DEPLOYMENT.local.md: ts, sha, step, status, image ids, pin digest. Exit status is non-zero unless deployed.
#
# Usage:
#   tools/deploy-vt.sh --ak-ref /home/ubuntu/alphakeel/ci/vibe-trading.ref [--ak-pin PATH] [--repo OWNER/NAME]
#
# This script changes production; run it only with the user's explicit go-ahead (AGENTS.md rule 14).
set -euo pipefail

AK_REF="" AK_PIN="" GH_REPO="wanderkiller/Vibe-Trading"
SERVICE="vibe-trading" HEALTH_URL="http://127.0.0.1:8899/health" HEALTH_WAIT_S=180
while [ $# -gt 0 ]; do
    case "$1" in
        --ak-ref) AK_REF="$2"; shift 2 ;;
        --ak-pin) AK_PIN="$2"; shift 2 ;;
        --repo) GH_REPO="$2"; shift 2 ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        *) echo "deploy-vt: unknown argument $1" >&2; exit 2 ;;
    esac
done
[ -n "$AK_REF" ] || { echo "deploy-vt: --ak-ref <AlphaKeel ci/vibe-trading.ref> is required" >&2; exit 2; }
[ -n "$AK_PIN" ] || AK_PIN="$(dirname "$AK_REF")/vibe-trading.pin"

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
[ -f DEPLOYMENT.local.md ] && [ -f docker-compose.yml ] || { echo "deploy-vt: run from the Vibe-Trading checkout" >&2; exit 2; }
LOG="$ROOT/deploy-log.jsonl"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$HOME/vibe-trading-backups/$TS"
SHA="$(git rev-parse HEAD)"
OLD_IMAGE_ID="" NEW_IMAGE_ID="" ROLLBACK_TAG="vibe-trading-rollback:$TS"
PIN_SHA="$(sha256sum agent/alphakeel_research/contract/PIN 2>/dev/null | cut -d' ' -f1 || true)"

log_line() {  # step status [detail]
    python3 -I -c 'import json,sys
k=["ts","sha","step","status","detail","old_image_id","new_image_id","rollback_tag","pin_sha256","backup_dir"]
print(json.dumps(dict(zip(k,sys.argv[1:])),sort_keys=True))' \
        "$TS" "$SHA" "$1" "$2" "${3:-}" "$OLD_IMAGE_ID" "$NEW_IMAGE_ID" "$ROLLBACK_TAG" "$PIN_SHA" "$BACKUP" >>"$LOG"
}
refuse() {  # step detail — nothing in production was touched
    log_line "$1" refused "$2"
    echo "deploy-vt: REFUSED at $1: $2" >&2
    exit 1
}
rollback() {  # step detail — the new container is up but failed its checks
    echo "deploy-vt: $1 failed ($2); rolling back to $ROLLBACK_TAG" >&2
    docker tag "$ROLLBACK_TAG" "$IMAGE"
    if docker compose up -d --no-build --no-deps "$SERVICE"; then
        log_line "$1" rolled_back "$2"
    else
        log_line "$1" rollback_failed "$2"
    fi
    exit 1
}

# 1. clean checkout
[ -z "$(git status --porcelain --untracked-files=no)" ] || refuse checkout "tracked files are modified"

# 2. CI on this exact commit
CI_JSON="$(gh run list --repo "$GH_REPO" --workflow "Deploy gate" --commit "$SHA" --json status,conclusion,headSha,databaseId --limit 20)" \
    || refuse ci "gh run list failed"
CI_OK="$(printf '%s' "$CI_JSON" | python3 -I -c 'import json,sys
sha=sys.argv[1]; runs=[r for r in json.load(sys.stdin) if r.get("headSha")==sha and r.get("status")=="completed"]
print("success" if runs and runs[0].get("conclusion")=="success" else (runs[0].get("conclusion") if runs else "none"))' "$SHA")"
[ "$CI_OK" = success ] || refuse ci "latest completed Deploy gate run on $SHA: $CI_OK"

# 3. pairing with AlphaKeel
[ -f "$AK_REF" ] || refuse pairing "no ref file $AK_REF"
[ -f "$AK_PIN" ] || refuse pairing "no pin file $AK_PIN"
WANT="$(grep -Ev '^[[:space:]]*(#|$)' "$AK_REF" | head -1 | tr -d '[:space:]')"
[ "$WANT" = "$SHA" ] || refuse pairing "HEAD $SHA != AlphaKeel ref $WANT"
cmp -s agent/alphakeel_research/contract/PIN "$AK_PIN" || refuse pairing "contract/PIN differs from $AK_PIN"

# 4. build the new image while the old container serves
docker compose config --quiet || refuse build "docker compose config failed"
IMAGE="$(docker compose config --images "$SERVICE" | head -1)"
CID="$(docker compose ps -q "$SERVICE" || true)"
[ -n "$CID" ] || refuse build "no running $SERVICE container to back up"
OLD_IMAGE_ID="$(docker inspect --format '{{.Image}}' "$CID")"
docker tag "$OLD_IMAGE_ID" "$ROLLBACK_TAG" || refuse build "cannot tag the old image"
docker compose build "$SERVICE" || refuse build "docker compose build failed"
NEW_IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$IMAGE")"

# 5. backup: image tag + id, env, volumes (service stopped while archiving)
mkdir -p "$BACKUP"
python3 -I -c 'import json,sys; json.dump(dict(zip(["image","old_image_id","rollback_tag","sha_to_deploy"],sys.argv[1:5])),open(sys.argv[5],"w"),indent=1)' \
    "$IMAGE" "$OLD_IMAGE_ID" "$ROLLBACK_TAG" "$SHA" "$BACKUP/image.json"
[ -f agent/.env ] && install -m 600 agent/.env "$BACKUP/agent.env"
VOLUMES="$(docker inspect --format '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}}={{.Destination}}{{"\n"}}{{end}}{{end}}' "$CID")"
docker compose stop "$SERVICE" || refuse backup "cannot stop $SERVICE"
for pair in $VOLUMES; do
    vol="${pair%%=*}"
    if ! docker run --rm --user 0 --entrypoint tar -v "$vol:/data:ro" -v "$BACKUP:/backup" "$ROLLBACK_TAG" \
            czf "/backup/$vol.tgz" -C /data .; then
        docker compose start "$SERVICE" || true
        refuse backup "archiving volume $vol failed (old container restarted)"
    fi
done
log_line backup ok "$(printf '%s' "$VOLUMES" | tr '\n' ' ')"

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
