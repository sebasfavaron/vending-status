#!/usr/bin/env bash
set -euo pipefail
cd /home/sebas/work/projects/vending-status
if [[ -f /home/sebas/runtime/secrets/ourvend.env ]]; then
  set -a
  source /home/sebas/runtime/secrets/ourvend.env
  set +a
fi
if [[ -f ./.env ]]; then
  set -a
  source ./.env
  set +a
fi
exec python3 app.py
