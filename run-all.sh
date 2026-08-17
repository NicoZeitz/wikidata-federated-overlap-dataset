#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

bash fetch-all.sh
python3 scripts/build_ground_truth.py
python3 scripts/build_dataset.py