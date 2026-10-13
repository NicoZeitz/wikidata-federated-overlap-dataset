#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

bash fetch-all.sh

uv run build-ground-truth
uv run dataset-builder
uv run create-query-inputs