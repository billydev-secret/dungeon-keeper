#!/usr/bin/env bash
# Full pytest regression suite. Run before pushing / opening a PR.
set -euo pipefail
cd "$(dirname "$0")/.."
exec ./.venv/Scripts/python.exe -m pytest "$@"
