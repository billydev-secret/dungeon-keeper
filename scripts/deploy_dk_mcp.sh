#!/usr/bin/env bash
# Deploy the read-only docs/source MCP server to /opt/dk-mcp.
#
# Source of truth is src/dk_mcp/ in this repo -- it lives here so it is
# versioned alongside the docs it serves, type-checked by pyright, and covered
# by scripts/gate.py. It RUNS from /opt so a network-facing process is not
# executing out of the production checkout.
#
# This script never touches the running service. Restarting is Billy's call:
#   sudo systemctl restart dk-mcp
#
# Usage:  ./scripts/deploy_dk_mcp.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET=/opt/dk-mcp
ENV_FILE="$TARGET/dk-mcp.env"

if [[ ! -d $TARGET ]]; then
  cat >&2 <<MSG
$TARGET does not exist. Create it once, as root:

  sudo install -d -o "$USER" -g "$(id -gn)" $TARGET

then re-run this script.
MSG
  exit 1
fi

if [[ ! -w $TARGET ]]; then
  echo "$TARGET is not writable by $USER — check its ownership." >&2
  exit 1
fi

echo "==> syncing dk_mcp package"
rsync -a --delete \
  --exclude '__pycache__' \
  "$REPO/src/dk_mcp/" "$TARGET/dk_mcp/"
cp "$REPO/requirements-mcp.lock" "$TARGET/requirements-mcp.lock"

echo "==> building venv"
if [[ ! -x $TARGET/.venv/bin/python ]]; then
  python3 -m venv "$TARGET/.venv"
fi
"$TARGET/.venv/bin/python" -m pip install --quiet --upgrade pip
"$TARGET/.venv/bin/python" -m pip install --quiet -r "$TARGET/requirements-mcp.lock"

# The endpoint is unauthenticated; this random path IS the shared secret. It is
# generated once and then left alone -- rotating it silently would break the
# claude.ai connector with a 404 and no clue why.
if [[ ! -f $ENV_FILE ]]; then
  echo "==> generating $ENV_FILE (first run)"
  secret="/mcp-$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  umask 077
  cat > "$ENV_FILE" <<ENVEOF
DK_REPO_ROOT=/home/ben/discord-bots/dungeon-keeper
DK_MCP_HOST=127.0.0.1
DK_MCP_PORT=8322
DK_MCP_PATH=$secret
DK_MCP_ALLOWED_HOSTS=dkmcp.billy-bots.com
ENVEOF
fi
chmod 600 "$ENV_FILE"

echo
echo "Deployed to $TARGET."
echo "Connector URL:  https://dkmcp.billy-bots.com$(grep '^DK_MCP_PATH=' "$ENV_FILE" | cut -d= -f2-)"
echo

# On a first deploy the unit does not exist yet, and telling someone to restart
# a unit systemd has never heard of just produces "Unit dk-mcp.service not
# found." Print whichever step is actually next.
if [[ -f /etc/systemd/system/dk-mcp.service ]]; then
  echo "Not restarted — run that yourself when you're ready:"
  echo "  sudo systemctl restart dk-mcp"
else
  echo "The systemd unit is not installed yet. Next:"
  echo "  sudo install -m 644 $REPO/deploy/dk-mcp.service /etc/systemd/system/dk-mcp.service"
  echo "  sudo systemctl daemon-reload"
  echo "  sudo systemctl enable --now dk-mcp"
  echo
  echo "Then point a Cloudflare Tunnel hostname at it:"
  echo "  dkmcp.billy-bots.com -> http://127.0.0.1:8322"
fi
