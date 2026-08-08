# Disaster recovery runbook

**The document you want at 3am.** Restore the Dungeon Keeper database from a
backup, in order, with the commands.

Verified end-to-end on 2026-08-06 against the live 746 MB database — see
[reviews/2026-08-06-backup-disaster-recovery.md](reviews/2026-08-06-backup-disaster-recovery.md)
for the drill and its timings.

- **RTO ≈ 3–5 minutes.** Almost all of it is bot startup (the ML stack;
  `TimeoutStartSec=180`). Copying and migrating the data takes under a second.
- **RPO up to 6 hours** — the backup interval. If `dungeonkeeper.db` is still
  readable, RPO is ~0: prefer step 1 over restoring.

## Where everything is

| | |
|---|---|
| Live DB | `/home/ben/discord-bots/dungeon-keeper/dungeonkeeper.db` |
| Local backups | `/home/ben/discord-bots/dungeon-keeper/backups/` — **same disk as the DB** |
| Schedule | every 6h (skipped if one was taken in the last 3h); newest 5 kept, nothing younger than 48h pruned |
| **Off-device backups** | NaturewoodNAS `192.168.174.3`, daily at 04:30, **14-day window** |
| Service | `dungeon-keeper.service` (also serves the dashboard) |
| Logs | `journalctl -u dungeon-keeper` (persistent). `log.txt` is **wiped on boot** — don't rely on it. |

**Which copy do you need?** If the disk is alive, use `backups/` — it is local
and newer. If the machine or disk is gone, everything comes from the NAS.

---

## 0. Stop, before you touch anything

**Do not restore if you have not confirmed the live DB is actually unusable.**
A restore throws away every write since the backup — up to 6 hours of messages,
XP, and economy activity. Most "the bot is broken" incidents are not data
damage.

```bash
sqlite3 "file:/home/ben/discord-bots/dungeon-keeper/dungeonkeeper.db?mode=ro" \
  "PRAGMA quick_check;"        # expect: ok   (~15-25s on 746 MB)
```

- **`ok`** → the data is fine. This is not a restore. Check
  `journalctl -u dungeon-keeper -n 200` and fix the process instead.
- **Anything else, or the file is missing/unreadable** → continue.

## 1. Preserve the evidence first

Never overwrite the damaged database. You may need it, and it is the only copy
of the most recent writes.

```bash
cd /home/ben/discord-bots/dungeon-keeper
sudo systemctl stop dungeon-keeper

# keep the corpse — including -wal/-shm, which hold the newest writes
mkdir -p /home/ben/dk-incident-$(date +%Y%m%d_%H%M)
mv dungeonkeeper.db dungeonkeeper.db-wal dungeonkeeper.db-shm \
   /home/ben/dk-incident-$(date +%Y%m%d_%H%M)/ 2>/dev/null
```

The `-wal` file matters: it can contain committed transactions not yet in the
main file, and it is often recoverable even when the main file is not.

## 2. Pick a backup — and verify it before you trust it

```bash
ls -lt /home/ben/discord-bots/dungeon-keeper/backups/*.db
```

Newest first. **Do not blindly take the top entry** — if a backup failed
part-way, a truncated file keeps the real name and the newest mtime (finding
B4). Always check it:

```bash
BAK=/home/ben/discord-bots/dungeon-keeper/backups/dungeonkeeper_YYYYMMDD_HHMMSS.db
sqlite3 "file:$BAK?mode=ro" "PRAGMA quick_check;"   # must print: ok
sqlite3 "file:$BAK?mode=ro" "SELECT COUNT(*) FROM messages;"
```

If it is not `ok`, or the row count is wildly low, move to the next one down.
Also note `dungeonkeeper-pre-tod-backfill-20260729.db` is a **hand-made snapshot
from 2026-07-29** that the rotation never prunes — usable, but very old, and it
predates any erasure run since that date.

## 3. Restore

```bash
cd /home/ben/discord-bots/dungeon-keeper
cp "$BAK" dungeonkeeper.db      # instant: XFS reflink on this box
```

Do **not** copy any `-wal`/`-shm` from the backup directory — a backup-API file
is self-contained, and stale siblings are what makes a database come out
malformed.

## 4. Bring the schema forward

The backup was taken under whatever schema was live then; the code you are about
to run may be newer. The migration chain is idempotent and takes ~0.02s.

```bash
cd /home/ben/discord-bots/dungeon-keeper
PYTHONPATH=src .venv/bin/python -c \
  "from migrations import apply_migrations_sync; \
   apply_migrations_sync('dungeonkeeper.db'); print('migrations OK')"
```

## 5. Sanity-check before starting the bot

```bash
sqlite3 "file:dungeonkeeper.db?mode=ro" "
  PRAGMA foreign_key_check;
  SELECT 'tables',     COUNT(*) FROM sqlite_master WHERE type='table';
  SELECT 'migrations', COUNT(*) FROM schema_version;
  SELECT 'messages',   COUNT(*) FROM messages;
  SELECT 'config',     COUNT(*) FROM config;
"
```

Reference values from 2026-08-05 (they only grow): 279 tables, 169 migrations,
~635k messages, ~523 config rows. `foreign_key_check` must return nothing.

## 6. Start, and watch it

```bash
sudo systemctl start dungeon-keeper
journalctl -u dungeon-keeper -f
```

Wait for the bot to report ready (can take ~1–3 minutes — the ML stack). Then
confirm in Discord that a slash command responds and the dashboard loads.

## 7. After the incident

- [ ] Force a fresh backup so the restored state has a copy — restarting the bot
      already takes one on startup; confirm it in `backups/`.
- [ ] Note in `docs/reviews/` what was lost (everything between the backup
      timestamp and the incident).
- [ ] **If a GDPR erasure ran after the backup was taken, re-run it** — the
      restore reinstated that user's rows. See
      [gdpr_runbook.md](gdpr_runbook.md) step 2.
- [ ] Keep `/home/ben/dk-incident-*/` until you are certain it is not needed,
      then delete it — it is a full copy of user data.

---

## Taking a backup by hand

Before a risky migration or bulk update. This is exactly what the service does:

```bash
cd /home/ben/discord-bots/dungeon-keeper
PYTHONPATH=src .venv/bin/python -c "
from pathlib import Path
from bot_modules.services.db_backup import _run_backup
_run_backup(Path('dungeonkeeper.db'), Path('snapshots'), retention_count=999)
"
```

Write ad-hoc snapshots to **`snapshots/`, not `backups/`** — anything dropped in
`backups/` under a non-standard name is never pruned and lingers forever
(finding B5). Delete hand-made snapshots when the change is confirmed good.

Never `cp` the live DB while the bot is running: with WAL enabled the copy comes
out malformed. Use the backup API above, or a read-only URI connection:

```python
import sqlite3
src = sqlite3.connect("file:dungeonkeeper.db?mode=ro", uri=True)
src.backup(sqlite3.connect("snapshots/manual.db"))
```

---

## Total machine loss — rebuilding from the NAS

When the disk or the whole box is gone. Everything below comes off
NaturewoodNAS (`192.168.174.3`); nothing is needed from the dead machine.

> **`scp` does not work against this NAS.** DSM's sshd has no sftp subsystem
> (`subsystem request failed on channel 0`), and modern `scp` speaks SFTP.
> `rsync` fails too — DSM's setuid rsync refuses `--server` for a non-root uid.
> Pull files with an `ssh 'cat …'` pipe, as below. Verified against the live
> NAS 2026-08-07; do not "fix" these commands back to `scp`.

```bash
# 1. Fresh box: clone the repo and install per deploy/README.md.
git clone <repo> dungeon-keeper && cd dungeon-keeper

# 2. See what is there, and pick a copy. (The db dir is chmod 700 — use the
#    same DSM account the backup ran as.)
ssh admin@192.168.174.3 'ls -la /volume1/Storage/botbackups/db'

#    Check it BEFORE pulling 790 MB — the NAS has its own sqlite3.
ssh admin@192.168.174.3 \
  'sqlite3 "file:/volume1/Storage/botbackups/db/dungeonkeeper_YYYYMMDD_HHMMSS.db?mode=ro" \
   "PRAGMA quick_check; SELECT COUNT(*) FROM messages;"'      # expect: ok

# 3. Pull it down, then re-check locally (this catches a bad transfer, which
#    the check in step 2 cannot).
ssh admin@192.168.174.3 \
  'cat /volume1/Storage/botbackups/db/dungeonkeeper_YYYYMMDD_HHMMSS.db' > dungeonkeeper.db
sqlite3 "file:dungeonkeeper.db?mode=ro" "PRAGMA quick_check;"   # expect: ok

# 4. Recover the secrets bundle — .env plus the systemd units.
ssh admin@192.168.174.3 \
  'cat /volume1/Storage/botbackups/secrets/secrets-YYYYMMDD.tar.gz.gpg' > secrets.tar.gz.gpg
gpg --decrypt --output secrets.tar.gz secrets.tar.gz.gpg
tar -xzf secrets.tar.gz          # -> .env, dungeon-keeper.service, cloudflared.service
sudo cp dungeon-keeper.service cloudflared.service /etc/systemd/system/

#    cloudflared.service carries the tunnel token in its ExecStart, so the
#    dashboard tunnel comes back with this file — nothing to re-provision.

# 5. Bring the schema forward, then start (steps 4-6 above).
```

**The GPG passphrase is not on the NAS** — by design, and it was never on the
dead machine's disk alone if you followed the install note. It is in your
password manager. Without it the secrets bundle is unopenable and every
credential must be re-issued by hand.

### Still not backed up (deliberately)

| | Why it's fine |
|---|---|
| `models/` (4.5 GB) | Re-downloads from HuggingFace on first boot. Slow, not lost. Note `TimeoutStartSec=180` may not cover the first run. |
| `lavalink/Lavalink.jar` + plugins | Re-fetched per `deploy/README.md`. Music is silent until then. |
| ~~Cloudflare tunnel credentials~~ | **Covered — this row is obsolete.** Checked 2026-08-07: there is no `/etc/cloudflared/` and no `~/.cloudflared/`. The tunnel runs token-only, with the credential embedded as `--token <jwt>` in `ExecStart` of `cloudflared.service` — which the secrets bundle already carries. No re-provisioning needed; restoring the unit file restores the tunnel. |
| `assets/` | In git; recovers with the checkout. |
| `Discord Messages/` | Export archive, re-exportable. |

`guess_cache/`, `econ_icon_catalog/` and `econ_role_icons/` **are** mirrored to
the NAS, but as current state rather than versioned history — restoring a
14-day-old database may leave a handful of image rows pointing at files the
mirror no longer has. Rows survive; images 404. See finding B7.
