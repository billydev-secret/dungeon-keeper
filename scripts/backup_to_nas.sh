#!/usr/bin/env bash
#
# Off-device backup to the Synology NAS.
#
# Closes finding B1 from docs/reviews/2026-08-06-backup-disaster-recovery.md:
# the bot's own backups land in <db-parent>/backups on the *same physical disk*
# as the database, which defends against logical damage but not against disk
# failure. This ships a verified copy to NaturewoodNAS over rsync-on-SSH.
#
# Runs as `ben` from a systemd timer (deploy/dk-nas-backup.{service,timer}) --
# deliberately NOT inside the bot, whose ProtectHome=read-only hardening should
# stay intact.
#
# Config lives in ~/.config/dk-backup/nas.conf (see deploy/README.md).
# Exit non-zero on any failure so the systemd unit goes `failed` and shows up
# in `systemctl --failed` -- that is this job's failure visibility.

set -euo pipefail

CONF="${DK_NAS_CONF:-$HOME/.config/dk-backup/nas.conf}"
if [[ ! -r "$CONF" ]]; then
    echo "FATAL: config not found at $CONF (see deploy/README.md)" >&2
    exit 78  # EX_CONFIG
fi
# shellcheck source=/dev/null
source "$CONF"

: "${NAS_HOST:?NAS_HOST must be set in $CONF}"
: "${NAS_USER:?NAS_USER must be set in $CONF}"
: "${NAS_DB_DIR:?NAS_DB_DIR must be set in $CONF}"
: "${NAS_SECRET_DIR:?NAS_SECRET_DIR must be set in $CONF}"
: "${GPG_PASSPHRASE_FILE:?GPG_PASSPHRASE_FILE must be set in $CONF}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
DK_ROOT="${DK_ROOT:-/home/ben/discord-bots/dungeon-keeper}"
LOCAL_BACKUP_DIR="$DK_ROOT/backups"

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new)
ssh_nas() { ssh "${SSH_OPTS[@]}" "$NAS_USER@$NAS_HOST" "$@"; }

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
die() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] FATAL: $*" >&2; exit 1; }

# --- 1. reachability -------------------------------------------------------
log "Checking $NAS_USER@$NAS_HOST ..."
ssh_nas true || die "cannot reach the NAS over SSH (key not authorised, or NAS down)"

# --- 2. pick the newest LOCAL backup and verify it before shipping ---------
# Never push a corrupt file off-device: a bad backup that overwrites a good one
# on the NAS is worse than no backup at all.
newest="$(find "$LOCAL_BACKUP_DIR" -maxdepth 1 -name 'dungeonkeeper_*.db' -printf '%T@ %p\n' \
          | sort -rn | head -1 | cut -d' ' -f2-)"
[[ -n "$newest" ]] || die "no local backup found in $LOCAL_BACKUP_DIR"

log "Verifying $(basename "$newest") before transfer ..."
check="$(sqlite3 "file:${newest}?mode=ro" 'PRAGMA quick_check;' 2>&1 || true)"
[[ "$check" == "ok" ]] || die "local backup failed integrity check: $check"

rows="$(sqlite3 "file:${newest}?mode=ro" 'SELECT COUNT(*) FROM messages;')"
log "Integrity ok (messages=$rows, $(du -h "$newest" | cut -f1))"

# --- 3. ship the database --------------------------------------------------
ssh_nas "mkdir -p '$NAS_DB_DIR' '$NAS_SECRET_DIR' && chmod 700 '$NAS_SECRET_DIR'"

log "Syncing $(basename "$newest") -> $NAS_HOST:$NAS_DB_DIR/"
rsync -a --partial --inplace --no-perms --no-group \
      -e "ssh ${SSH_OPTS[*]}" \
      "$newest" "$NAS_USER@$NAS_HOST:$NAS_DB_DIR/"

# Verify the far end matches, by size. (A full checksum re-reads 700 MB over
# the wire; rsync already verifies its own transfer with a rolling checksum.)
local_size="$(stat -c%s "$newest")"
remote_size="$(ssh_nas "stat -c%s '$NAS_DB_DIR/$(basename "$newest")'")"
[[ "$local_size" == "$remote_size" ]] \
    || die "size mismatch after transfer: local=$local_size remote=$remote_size"
log "Transfer verified ($local_size bytes)"

# --- 4. ship the things a DB-only restore cannot rebuild -------------------
# .env is the single highest-leverage file here (finding B7): 4 KB, gitignored,
# and losing it means re-issuing the bot token and every API key by hand.
# Encrypted client-side so the NAS never holds plaintext secrets.
[[ -r "$GPG_PASSPHRASE_FILE" ]] || die "GPG passphrase file unreadable: $GPG_PASSPHRASE_FILE"

staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT

log "Encrypting secrets bundle ..."
tar -czf "$staging/secrets.tar.gz" \
    -C "$DK_ROOT" .env \
    -C /etc/systemd/system dungeon-keeper.service cloudflared.service 2>/dev/null \
  || tar -czf "$staging/secrets.tar.gz" -C "$DK_ROOT" .env

gpg --batch --yes --quiet \
    --passphrase-file "$GPG_PASSPHRASE_FILE" \
    --symmetric --cipher-algo AES256 \
    --output "$staging/secrets.tar.gz.gpg" \
    "$staging/secrets.tar.gz"

rsync -a --no-perms --no-group -e "ssh ${SSH_OPTS[*]}" \
      "$staging/secrets.tar.gz.gpg" \
      "$NAS_USER@$NAS_HOST:$NAS_SECRET_DIR/secrets-$(date +%Y%m%d).tar.gz.gpg"
log "Secrets bundle synced (AES256, passphrase from $GPG_PASSPHRASE_FILE)"

# Small unbacked media that a restored DB still points at (finding B7).
#
# This is a MIRROR of current state, not a versioned backup: --delete means a
# file removed locally (by the guess_repo cleanup, or by an erasure) is removed
# here too. That keeps deletions propagating for GDPR, at the cost of not being
# able to recover a file you deleted by mistake. The database copies are the
# real backup; this is 5 MB of accompanying images.
NAS_MEDIA_DIR="${NAS_MEDIA_DIR:-$NAS_DB_DIR/media}"
for dir in guess_cache econ_icon_catalog econ_role_icons; do
    [[ -d "$DK_ROOT/$dir" ]] || continue
    ssh_nas "mkdir -p '$NAS_MEDIA_DIR/$dir'"
    rsync -a --delete --no-perms --no-group -e "ssh ${SSH_OPTS[*]}" \
          "$DK_ROOT/$dir/" "$NAS_USER@$NAS_HOST:$NAS_MEDIA_DIR/$dir/"
done
log "Media directories mirrored"

# --- 5. prune the NAS to the agreed window ---------------------------------
# 14 days. This number is load-bearing: it is what docs/gdpr_runbook.md
# states as the point at which an erasure has propagated to every copy. Change
# it in both places or not at all.
#
# `find -delete` is avoided deliberately: DSM ships a busybox find on some
# versions, where -delete does not exist. print0 | xargs rm is portable, and
# the -maxdepth/-type/-name triple keeps the blast radius to our own files.
prune_old() {  # $1 = directory, $2 = filename glob -> echoes the count removed
    local dir="$1" glob="$2"
    local n
    n="$(ssh_nas "find '$dir' -maxdepth 1 -type f -name '$glob' -mtime +$RETENTION_DAYS | wc -l")"
    if (( n > 0 )); then
        ssh_nas "find '$dir' -maxdepth 1 -type f -name '$glob' -mtime +$RETENTION_DAYS -print0 \
                 | xargs -0 -r rm -f"
    fi
    echo "$n"
}

log "Pruning NAS copies older than ${RETENTION_DAYS}d ..."
pruned="$(prune_old "$NAS_DB_DIR" 'dungeonkeeper_*.db')"
pruned_sec="$(prune_old "$NAS_SECRET_DIR" 'secrets-*.tar.gz.gpg')"
log "Pruned $pruned database copies and $pruned_sec secrets bundles"

kept="$(ssh_nas "ls -1 '$NAS_DB_DIR'/dungeonkeeper_*.db 2>/dev/null | wc -l")"
log "NAS now holds $kept database copies (window: ${RETENTION_DAYS}d)"

# --- 6. leave a local breadcrumb so staleness is detectable ----------------
mkdir -p "$(dirname "${STATUS_FILE:=$HOME/.local/state/dk-backup/last-nas-sync}")"
date -Iseconds > "$STATUS_FILE"

log "Off-device backup complete."
