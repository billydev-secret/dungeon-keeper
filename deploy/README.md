# Deployment

systemd units for the bot, captured from the live host. `dungeon-keeper.service`
references this file; before now it did not exist, and the only copy of the unit
lived at `/etc/systemd/system/` — untracked, so a host rebuild would have lost
the hardening block. Keep these in sync: **if you edit the installed unit, copy
it back here in the same commit.**

| File | Purpose |
|---|---|
| `dungeon-keeper.service` | The bot + dashboard. The heavy one. |
| `discord-bots.target` | Grouping target — start/stop/restart every bot at once. |
| `dungeon-keeper-watchdog.service` | DMs the owner if the bot goes down. |

## Install

```bash
sudo cp deploy/dungeon-keeper.service deploy/discord-bots.target \
        deploy/dungeon-keeper-watchdog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now discord-bots.target dungeon-keeper-watchdog
journalctl -u dungeon-keeper -f
```

**Paths are hardcoded to `/home/ben/discord-bots/dungeon-keeper`.** On a new host,
change these four lines in `dungeon-keeper.service` (plus `ExecStart` in the
watchdog unit) and the `User=`/`Group=` if the account differs:

- `WorkingDirectory=`
- `Environment=PYTHONPATH=<repo>/src`
- `ExecStart=<repo>/.venv/bin/python -m dungeonkeeper`
- `ReadWritePaths=`

`ReadWritePaths` must point at the repo root. The unit runs
`ProtectSystem=strict` + `ProtectHome=read-only`, so the repo is the *only*
writable location — the DB, `backups/`, `log.txt`, the HuggingFace cache, and
matplotlib's config dir all live there for that reason. Narrowing it breaks
transcription and graph rendering.

## Dependencies

```bash
python -m venv .venv                       # Python 3.14 (prod: 3.14.6, Fedora 44)
.venv/bin/pip install -r requirements.lock
```

`requirements.lock` deliberately **excludes `llama-cpp-python`** — it is the only
dependency that builds from source (no manylinux wheel for cp314), needing
gcc/g++ and cmake plus several minutes. Install it only if this host runs
in-process inference:

```bash
.venv/bin/pip install -r requirements-local-llm.lock   # only if LLAMA_SERVER_URL is unset
```

Hosts pointing `LLAMA_SERVER_URL` at a llama.cpp `llama-server` elsewhere on the
LAN never import `llama_cpp` at all, so they skip the toolchain entirely.

Also needed on the host:

- **Java 17+** for Lavalink, or music silently degrades to "currently
  unavailable". The bot spawns it as a child process and finds it via
  `shutil.which("java")` → `$JAVA_HOME` → `/usr/lib/jvm/*/bin/java`.
- `python scripts/setup_lavalink.py` — `Lavalink.jar` and its plugins are
  gitignored.

## Moving to a new host

`git clone` gets you almost none of the runtime state — it is nearly all
gitignored. Copy these across explicitly:

| Path | Notes |
|---|---|
| `.env` | Secrets. `SESSION_SECRET` must survive or every dashboard session is invalidated. |
| `dungeonkeeper.db` | Stop the service first so the WAL is checkpointed. |
| `econ_role_icons/`, `econ_icon_catalog/`, `quote_borders/` | Per-guild uploads. |
| `src/web_server/static/doc-images/` | Uploaded doc images. |
| `.cache/huggingface/` | Whisper models. Optional — re-downloadable from the dashboard widget. |
| `models/` | GGUF weights. Optional — re-downloaded from HuggingFace on first boot, but that is ~2 GB and `TimeoutStartSec=180` may not cover it. |

Size the disk for **≥30 GB**: the repo is ~10 GB today and `backups/` grows with
the DB (5 retained full copies).

Because the dashboard is reached through the cloudflared tunnel, moving hosts
needs **no DNS change, no port forwarding, and no firewall work** — run the same
`cloudflared` unit on the new box. Keep `DASHBOARD_BASE_URL` unchanged and the
Discord/Spotify OAuth redirect URIs keep working untouched.

## Off-device backup (NAS)

The bot backs itself up every 6 hours into `backups/` — on the **same physical
disk** as the database. That covers a bad migration; it does not cover a dead
SSD. `scripts/backup_to_nas.sh` ships a verified copy to the Synology
(NaturewoodNAS, `192.168.174.3`) over SSH, on a daily timer.

> **Transport is a plain `ssh 'cat > file'` pipe — not rsync, not scp.**
> Both were tried against the live NAS on 2026-08-07 and both are blocked by
> DSM defaults:
>
> | | Result |
> |---|---|
> | `rsync` over SSH | DSM ships a **setuid-root rsync that refuses `--server` for non-root** — `Permission denied, please try again.` from the far end. DSM 7 also disables root SSH, so there is no account to run it as. |
> | `scp` / `sftp` | DSM's sshd has **no sftp subsystem** — `subsystem request failed on channel 0`. Modern `scp` speaks SFTP, so it fails too. |
>
> Either could be unblocked with a DSM service checkbox, but a DSM update that
> reset one would silently break the backup. The pipe depends on nothing but
> sshd. Use `ssh … 'cat …'` in anything you write against this NAS — **reaching
> for `scp` will not work.**

It runs as its own unit rather than inside the bot, so `dungeon-keeper.service`
keeps its `ProtectHome=read-only` hardening.

**Retention is 14 days**, and that number is load-bearing: it is what
`docs/gdpr_runbook.md` states as the point at which an erasure has
propagated to every copy. Change it in both places or not at all.

### Install

```bash
# 1. Authorise this machine against the NAS (asks for the DSM password once).
#    The DSM account must be in the administrators group -- DSM only allows
#    SSH for admins. Enable it in DSM: Control Panel > Terminal & SNMP > SSH.
#
#    FIRST enable DSM's User Home service:
#      Control Panel > User & Group > Advanced > "Enable user home service"
#    Without it there is no /var/services/homes/<user>, so there is nowhere to
#    put authorized_keys and ssh-copy-id fails AFTER accepting your password
#    with "Could not chdir to home directory ... mkdir: cannot create
#    directory '.ssh': Permission denied". That reads like a permissions bug
#    and is not one.
ssh-copy-id -i ~/.ssh/id_ed25519.pub admin@192.168.174.3

#    If the key still is not accepted, DSM's strict mode checks are the cause
#    (ssh-copy-id does not set these itself):
#      ssh admin@192.168.174.3 'chmod 700 ~ ~/.ssh && chmod 600 ~/.ssh/authorized_keys'

# 2. Config + passphrase.
mkdir -p ~/.config/dk-backup ~/.local/state/dk-backup
cp deploy/nas-backup.conf.example ~/.config/dk-backup/nas.conf
chmod 600 ~/.config/dk-backup/nas.conf
$EDITOR ~/.config/dk-backup/nas.conf        # fill in NAS_USER and the two paths

openssl rand -base64 32 > ~/.config/dk-backup/env-passphrase
chmod 600 ~/.config/dk-backup/env-passphrase
```

> **Record that passphrase in your password manager before going further.**
> It lives on the machine the backup exists to survive. If the disk dies and
> the passphrase dies with it, the encrypted secrets bundle on the NAS cannot
> be opened — at exactly the moment you need it.

```bash
# 3. Dry run, watching the output.
./scripts/backup_to_nas.sh

# 4. Install the timer.
sudo cp deploy/dk-nas-backup.service deploy/dk-nas-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dk-nas-backup.timer
systemctl list-timers dk-nas-backup.timer
```

### What it does

1. Refuses to run if the NAS is unreachable (exits non-zero → the unit shows in
   `systemctl --failed`).
2. Picks the newest local backup and runs `PRAGMA quick_check` **before**
   transferring — a corrupt copy overwriting a good one on the NAS would be
   worse than no backup.
3. Pipes it over SSH to `<name>.partial`, compares **sha256 on both ends**, and
   only then renames it into place — so a truncated transfer can never occupy
   the real filename on the NAS. A byte-identical copy already present is
   skipped, making a same-day re-run cheap instead of another 790 MB.
4. tars `.env` plus the `dungeon-keeper`/`cloudflared` unit files, encrypts with
   AES256, and syncs that to a separate `chmod 700` directory. These are the
   things a database-only restore cannot rebuild — note that `cloudflared.service`
   carries the tunnel token in its `ExecStart`, so the tunnel is covered by this
   bundle and needs no separate handling.
5. Mirrors `guess_cache/`, `econ_icon_catalog/`, `econ_role_icons/` — the small
   media a restored DB still has rows pointing at. This is a mirror of current
   state, not versioned: deletions propagate.
6. Prunes both the database copies and the secrets bundles past 14 days, and
   sweeps `.partial` files older than a day (a run killed mid-transfer would
   otherwise strand ~790 MB on the NAS forever).
7. Keeps both NAS directories at `chmod 700` and every file it writes at `600`,
   re-asserting them on each run. The share itself is world-writable by DSM
   default, and the database copy is **not** encrypted at rest the way the
   secrets bundle is — mode is the only thing limiting who on the LAN can read
   646k rows of member message content.

### Restoring from it

See [`docs/disaster_recovery_runbook.md`](../docs/disaster_recovery_runbook.md).
The short version:

```bash
# List what is on the NAS (the db dir is 700, so run as the same DSM account).
ssh admin@192.168.174.3 'ls -la /volume1/Storage/botbackups/db'

# Pull a copy back. NOT scp -- see the transport note above; it will fail.
ssh admin@192.168.174.3 'cat /volume1/Storage/botbackups/db/dungeonkeeper_YYYYMMDD_HHMMSS.db' \
    > dungeonkeeper_restored.db

# Prove it survived the round trip before trusting it.
sqlite3 "file:dungeonkeeper_restored.db?mode=ro" 'PRAGMA quick_check;'

# Secrets: .env + the systemd units (incl. the cloudflared tunnel token).
ssh admin@192.168.174.3 'cat /volume1/Storage/botbackups/secrets/secrets-YYYYMMDD.tar.gz.gpg' \
    > secrets.tar.gz.gpg
gpg --decrypt --output secrets.tar.gz secrets.tar.gz.gpg   # prompts for the passphrase
tar -xzf secrets.tar.gz
```

The NAS has its own `sqlite3`, so you can check a copy **without pulling
790 MB first** — useful when you are deciding which backup to restore:

```bash
ssh admin@192.168.174.3 \
  'sqlite3 "file:/volume1/Storage/botbackups/db/dungeonkeeper_YYYYMMDD_HHMMSS.db?mode=ro" \
   "PRAGMA quick_check; SELECT COUNT(*) FROM messages;"'
```

## Notes

- The dashboard binds loopback-only by default. `server.py` force-reverts a
  non-loopback bind when auth is `OpenAuth`, so you cannot accidentally expose
  an unauthenticated admin panel.
- `_client_ip()` trusts the `CF-Connecting-IP` header, which is safe **only**
  because the origin is reachable exclusively through the tunnel. If you ever
  put the bot behind an ALB or nginx, fix that first or the per-IP rate limiter
  can be defeated by a spoofed header.
- The bot's own `backups/` sit beside the DB on the same disk, which defends
  against logical damage but not disk failure. The off-device copy that closes
  that gap is the NAS timer below.
