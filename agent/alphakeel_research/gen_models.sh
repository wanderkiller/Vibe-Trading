#!/usr/bin/env bash
# Regenerate models.py from the pinned OpenAPI (Rust DTOs are the single source; never edit models.py by hand).
#   pip install 'datamodel-code-generator==0.83.0' ruff      # development dependencies only
# Generated outside the repository so the formatter does not pick up project-specific configuration.
set -euo pipefail
cd "$(dirname "$0")"
tmp="$(mktemp -d)"
datamodel-codegen --input contract/openapi.json --input-file-type openapi --output "$tmp/models.py" \
  --output-model-type pydantic_v2.BaseModel --target-python-version 3.11 --use-annotated \
  --disable-timestamp --use-standard-collections --formatters ruff-format --custom-file-header \
  "# GENERATED from contract/openapi.json by gen_models.sh (datamodel-code-generator). Do not edit."
cp "$tmp/models.py" models.py
rm -rf "$tmp"
