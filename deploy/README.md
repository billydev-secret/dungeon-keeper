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
(NaturewoodNAS, `192.168.174.3`) over rsync-on-SSH, on a daily timer.

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
ssh-copy-id -i ~/.ssh/id_ed25519.pub <nas-user>@192.168.174.3

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
3. rsyncs it, then verifies the far-end size matches.
4. tars `.env` plus the `dungeon-keeper`/`cloudflared` unit files, encrypts with
   AES256, and syncs that to a separate `chmod 700` directory. These are the
   three things a database-only restore cannot rebuild.
5. Mirrors `guess_cache/`, `econ_icon_catalog/`, `econ_role_icons/` — the small
   media a restored DB still has rows pointing at. This is a mirror of current
   state, not versioned: deletions propagate.
6. Prunes both the database copies and the secrets bundles past 14 days.

### Restoring from it

See [`docs/disaster_recovery_runbook.md`](../docs/disaster_recovery_runbook.md).
The short version:

```bash
scp <nas-user>@192.168.174.3:<NAS_DB_DIR>/dungeonkeeper_YYYYMMDD_HHMMSS.db .
gpg --decrypt --output secrets.tar.gz secrets-YYYYMMDD.tar.gz.gpg
tar -xzf secrets.tar.gz
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
