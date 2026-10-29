# Local deployment for Vibe-Trading 0.1.18

This fork includes the upstream v0.1.18 release, Gemini thought-signature replay (already in upstream since 0.1.16),
and the deployment files used for the verified
Docker installation.

`Dockerfile.local` follows the upstream Dockerfile and adds Anthropic and
DeepSeek adapters from `requirements-local-providers-lock.txt`. The overlay
pins versions and download hashes. Since 0.1.18 the base lock itself pins
langchain-core 1.6.6 (langchain-anthropic 1.7.2 requires at least 1.6.2), so the
overlay replaces no base pin. The final build runs `pip check`.

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

## Standalone deployment

Deploy with `docker compose build && docker compose up -d` after backing up the image and the named volumes.

Back up the existing image, environment files, and named volumes before
upgrading. Stop the service while archiving its volumes to preserve a consistent
session database. Preserve all volume names and mounts when replacing the
container. To restore a retained image, tag it as the Compose service image and
recreate the service with --no-build; restore data only after separately saving
any newer sessions.

When merging another upstream release, refresh Dockerfile.local from the new
Dockerfile, retain the provider-overlay installation, and review both upstream
dependency locks against the overlay.

Validation for this update: 494 targeted backend tests and all 672 frontend
tests passed, the production image built successfully, and pip check passed.
The Anthropic, DeepSeek, Gemini, and OpenAI SDK adapters initialized and bound
tools successfully. The configured Gemini endpoint also passed two consecutive
streamed tool calls with thought signatures echoed back, followed by its final
answer, both before and after deployment. Other providers were not exercised
with live generation requests. Health/readiness, session database integrity,
configuration hashes, user-skill hashes, and persistent mounts were checked
after deployment.
