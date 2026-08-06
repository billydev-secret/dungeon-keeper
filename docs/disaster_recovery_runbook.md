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
| Backups | `/home/ben/discord-bots/dungeon-keeper/backups/` (same disk — see B1) |
| Schedule | every 6h, plus one on every bot start; newest 5 kept |
| Service | `dungeon-keeper.service` (also serves the dashboard) |
| Logs | `journalctl -u dungeon-keeper` (persistent). `log.txt` is **wiped on boot** — don't rely on it. |

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

## What a DB restore does *not* bring back

The database is not the whole system. If the **machine** is lost rather than the
database, these are gone too — none of them are in any backup today (finding B1/B7):

| | Impact |
|---|---|
| `.env` | **Blocks recovery.** Bot token + all API keys. Must be re-issued from the Discord developer portal and each provider. |
| `/etc/systemd/system/*.service` | Unit files for `dungeon-keeper`, `cloudflared`, `tod-mcp`. See `deploy/README.md` to rebuild. |
| Cloudflare tunnel credentials | Dashboard stays unreachable until the tunnel is re-provisioned. |
| `models/` (4.5 GB) | NSFW classifier, whisper, llama. Re-downloadable, slow. |
| `lavalink/Lavalink.jar` + plugins | Music silently fails until re-fetched. |
| `guess_cache/`, `econ_icon_catalog/`, `econ_role_icons/` | 9 live images orphaned; rows survive, images 404. |

`assets/` is in git and recovers with the checkout.

**Until off-device backups exist, a disk failure is not recoverable from
anything on this machine.** That is the open High finding — see B1.
