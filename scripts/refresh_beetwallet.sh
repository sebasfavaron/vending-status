#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 scripts/fetch_beetwallet_operations.py "$@"
python3 scripts/build_beetwallet_summary.py
printf '\nListo:\n- %s\n- %s\n' "$(pwd)/data/beetwallet_operations.json" "$(pwd)/data/beetwallet_summary.json"
