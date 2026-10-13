# Local deployment for Vibe-Trading 0.1.16

This fork includes the upstream v0.1.16 release, the mobile navigation patches,
Gemini thought-signature replay, and the deployment files used for the verified
Docker installation.

`Dockerfile.local` follows the upstream Dockerfile and adds Anthropic and
DeepSeek adapters from `requirements-local-providers-lock.txt`. The overlay
pins versions and download hashes. Its langchain-core 1.6.3 pin intentionally
replaces the base lock's 1.6.1 because langchain-anthropic 1.7.2 requires at
least 1.6.2. The final build runs `pip check`.

`docker-compose.override.yml` selects that Dockerfile, host networking, and a
127.0.0.1:8899 listener. The host's reverse proxy must forward to that listener.
The override allows loopback proxy identity to be preserved for forwarded
requests. Compose automatically loads this override next to docker-compose.yml.

Provide credentials locally using the configuration paths documented in the
upstream README. Persistent user configuration at ~/.vibe-trading/.env takes
precedence over agent/.env for values not already supplied by the environment.
The Docker user home and its configuration are retained by the vibe-home
volume. Environment files, credentials, transcripts, and runtime data are not
included in the repository.

Build and start the configured service only through `tools/deploy-vt.sh` (and only with the user's explicit
go-ahead), from this checkout with the commit to deploy checked out:

```bash
tools/deploy-vt.sh --ak-ref /home/ubuntu/alphakeel/ci/vibe-trading.ref   # --ak-pin defaults to ci/vibe-trading.pin next to it
```

It refuses (exit 1; a refusal after the build points the compose image tag back at the old image) unless the checkout is
clean (no modified and no untracked files), GitHub workflow `CI` succeeded on exactly this commit,
the commit equals AlphaKeel's `ci/vibe-trading.ref` and `agent/alphakeel_research/contract/PIN` equals AlphaKeel's
`ci/vibe-trading.pin`. It then builds the new image while the old container serves, keeps the old image as
`vibe-trading-rollback:<ts>`, copies `agent/.env` and archives every named volume (service stopped) into
`~/vibe-trading-backups/<ts>/` (mode 700: the volumes may hold credentials), starts the new image, and requires `/health` plus `python -m alphakeel_research check`
in the container to report `contract_pin: matched`; on failure it re-tags the rollback image and recreates the old
container. Every attempt appends one JSON line (sha, step, status, old/new image id, PIN digest, backup dir) to
`deploy-log.jsonl` next to this file (git-ignored). When the contract PIN changed, deploy AlphaKeel first: the
in-container check talks to the running research service. Manual rollback to an earlier deployment:
`docker tag vibe-trading-rollback:<ts> "$(docker compose config --images vibe-trading)"` then
`docker compose up -d --no-build --no-deps vibe-trading`.
After a successful deploy only the most recent `VT_ROLLBACK_KEEP` (default 3) rollback images are kept, including this one; the volume backups under `~/vibe-trading-backups/<ts>` are all kept, so the image for an older backup may no longer exist - then restore from the current image plus the volume backup. Builds go through BuildKit via the `docker-buildx` package (installed on the host 2026-10-16; without it `docker builder prune` falls back to the legacy path and ignores `--keep-storage`); the post-deploy prune drops builder cache older than `VT_BUILDER_MAX_AGE` (default 72h).

The automatic rollback swaps the image only, not the volumes. When the new image migrated a volume (session database,
user home) the old image may not read it: restore that volume from the same backup, with the service stopped, after
saving anything newer you want to keep (`<vol>` = the volume name, `<ts>` = the backup directory):

```bash
docker compose stop vibe-trading
docker run --rm --user 0 --entrypoint sh -v <vol>:/data -v ~/vibe-trading-backups/<ts>:/backup:ro vibe-trading-rollback:<ts> \
    -c 'find /data -mindepth 1 -delete && tar xzf /backup/<vol>.tgz -C /data'
docker compose up -d --no-build --no-deps vibe-trading
```

Back up the existing image, environment files, and named volumes before
upgrading. Stop the service while archiving its volumes to preserve a consistent
session database. Preserve all volume names and mounts when replacing the
container. To restore a retained image, tag it as the Compose service image and
recreate the service with --no-build; restore data only after separately saving
any newer sessions.

When merging another upstream release, refresh Dockerfile.local from the new
Dockerfile, retain the provider-overlay installation, and review both upstream
dependency locks against the overlay. Preserve the mobile navigation patches
and add their menu/close labels to any newly introduced locales.

Validation for this update: 494 targeted backend tests and all 672 frontend
tests passed, the production image built successfully, and pip check passed.
The Anthropic, DeepSeek, Gemini, and OpenAI SDK adapters initialized and bound
tools successfully. The configured Gemini endpoint also passed two consecutive
streamed tool calls with thought signatures echoed back, followed by its final
answer, both before and after deployment. Other providers were not exercised
with live generation requests. Health/readiness, session database integrity,
configuration hashes, user-skill hashes, and persistent mounts were checked
after deployment.

AlphaKeel research client: `agent/alphakeel_research/` (installed with the project, httpx/pydantic/zstandard/duckdb are
already in the locks) reaches AlphaKeel's research service at http://127.0.0.1:8731 over the host network. Put the service
credential in a file readable by the container user and set `ALPHAKEEL_RESEARCH_TOKEN_FILE` (see agent/.env.example); the
credential is created on the AlphaKeel host with `arb research-service token create`. To turn the integration off, run
`arb research-service disable` on the AlphaKeel host or remove the variables; no Vibe-Trading data is touched.

Patch layer: every local change over upstream (this overlay, the mobile patches, the AlphaKeel client and its hooks in
upstream files) is listed by group in `patches/MANIFEST.md`, with the upstream base in `patches/BASE`. Before merging
another upstream release, follow `patches/UPGRADE.md`: `python patches/check_patches.py --upstream <tag>` assesses the
release before any rebase, and `python patches/check_patches.py --tests` must pass on the result.
