#!/usr/bin/env bash
# Thin wrapper — the canonical gate is scripts/gate.py (cross-platform).
set -euo pipefail
exec python3 "$(dirname "$0")/gate.py" "$@"
