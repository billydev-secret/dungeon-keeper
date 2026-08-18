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
# `install -t <dir>`, not `cp src... <dir>` -- see the note under "Off-device
# backup" below. A cp whose trailing destination is lost to a truncated paste
# overwrites one source unit with another instead of installing anything.
sudo install -m 644 -t /etc/systemd/system/ \
     deploy/dungeon-keeper.service deploy/discord-bots.target \
     deploy/dungeon-keeper-watchdog.service
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
#
# `install -t <dir>` and not `cp a b <dir>`: with cp, the destination is the
# LAST argument, so a command truncated on paste ("cp deploy/x.service
# deploy/x.timer") silently copies the service OVER the timer instead of
# installing either. That is not hypothetical -- it happened on 2026-08-11 and
# produced "Unknown section 'Service'. Ignoring. / Timer unit lacks value
# setting. Refusing." from a .timer that was byte-identical to the .service.
# With -t the destination is named up front and truncation is harmless.
sudo install -m 644 -t /etc/systemd/system/ \
     deploy/dk-nas-backup.service deploy/dk-nas-backup.timer
sudo systemctl daemon-reload
sudo systemctl enable --now dk-nas-backup.timer
systemctl list-timers dk-nas-backup.timer

# 5. VERIFY THROUGH SYSTEMD, not just from your shell. Step 3 passing proves
#    nothing about step 4 -- see "The install trap" below. This is the check
#    that matters:
sudo systemctl start dk-nas-backup.service
systemctl status dk-nas-backup.service     # must end "status=0/SUCCESS"

# If the timer ever loads as `bad-setting`, check the two files differ:
#   diff deploy/dk-nas-backup.service deploy/dk-nas-backup.timer
#   git checkout -- deploy/dk-nas-backup.timer    # restore if clobbered
```

### The install trap: a shell dry run does not prove the unit works

The timer was installed on 2026-08-07, the dry run passed, and **it never ran
once**. Every firing died instantly:

```
dk-nas-backup.service: Unable to locate executable
  '/home/ben/.../scripts/backup_to_nas.sh': Permission denied
Failed at step EXEC ... status=203/EXEC
```

The script is `0755`, owned by `ben`, and runs perfectly from a login shell.
Nothing is wrong with its permissions. **SELinux** is: files under the repo are
labelled `user_home_t`, and the system manager may not `execve` a `user_home_t`
file. The denial is `dontaudit`'d, so `ausearch -m AVC` shows *nothing* — there
is no evidence pointing at SELinux anywhere in the error.

`dungeon-keeper.service` dodges this by accident: its `ExecStart` is
`.venv/bin/python`, and `.venv/bin/*` matches a policy rule that labels it
`bin_t`. `scripts/*.sh` gets no such rule.

The fix, already in `dk-nas-backup.service`, is to run the script **through
bash** — `ExecStart=/usr/bin/bash <script>`. `bash` is `shell_exec_t` and may
be executed; it then only needs to *read* the script, which is allowed.
`chcon -t bin_t` on the script also works but is the wrong fix: a git checkout
or an editor save writes a new file that inherits the directory default again,
silently re-breaking the timer at the next edit.

**Any future unit that runs something out of this repo needs the same
treatment**, and needs verifying with `systemctl start`, not `./script.sh`.
Note that `systemd-run --user` does *not* reproduce the problem — a user
manager runs in a different SELinux domain — so it is not a valid substitute.

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

## DK MCP server

A read-only MCP server exposing this repo's specs and source to claude.ai, so
feature specs are developed against how the bot actually works. Source lives in
`src/dk_mcp/` (versioned with the docs it serves, covered by `scripts/gate.py`);
it runs from `/opt/dk-mcp`, so a network-facing process is not executing out of
the production checkout. Spec: `docs/dk_mcp_server.md`.

Install:

```bash
# 1. Create the deploy directory once, as root.
sudo install -d -o ben -g ben /opt/dk-mcp

# 2. Sync the package, build its venv, and generate the endpoint secret.
#    Re-run this after any change to src/dk_mcp/. It never restarts anything.
./scripts/deploy_dk_mcp.sh

# 3. Install the unit.
sudo install -m 644 deploy/dk-mcp.service /etc/systemd/system/dk-mcp.service
sudo systemctl daemon-reload
sudo systemctl enable --now dk-mcp

# 4. Point a Cloudflare Tunnel hostname at it (Cloudflare dashboard):
#      dkmcp.billy-bots.com  ->  http://127.0.0.1:8322
```

The connector URL is printed by the deploy script; it is
`https://dkmcp.billy-bots.com` plus the random path in `/opt/dk-mcp/dk-mcp.env`.
Add it in claude.ai as a custom connector.

**That random path is the only credential.** The connector is unauthenticated,
so the path is a shared secret: it lives in a 0600 `EnvironmentFile` rather than
in the unit's `Environment=`, because `systemctl show` prints `Environment=` to
any local user without sudo. Don't paste it anywhere it will be logged, and
don't rotate it casually — the connector then 404s with no explanation.

Verifying the sandbox after install (worth doing once, since the unit's mount
namespace is the backstop for the application's own path allowlist):

```bash
# The service must be able to read docs/ and src/ ...
sudo systemd-run --uid=ben --property=JoinsNamespaceOf=dk-mcp.service \
  --pty /bin/ls /home/ben/discord-bots/dungeon-keeper/

# ... and .env, dungeonkeeper.db and .git must not exist in its namespace at
# all. `ls` above should show only docs, src and CLAUDE.md.
```

Note `systemd-run --user` is *not* a substitute for testing this: user-manager
uid remapping makes root-owned files read as `nobody` and produces false
failures. Test against the real system unit or not at all.

Ports: 8322 (8321 belongs to the Truth-or-Dare server at `/opt/tod`).
Logs: `journalctl -u dk-mcp -f`.

### The tunnel ingress rule has to be exactly `http://127.0.0.1:8322`

Not `https://`, and not `localhost`. Both were wrong on the first setup and
each fails differently:

- `https://` — the origin speaks plain HTTP, so cloudflared's TLS handshake
  gets plain text back: `tls: first record does not look like a TLS handshake`.
- `localhost` — resolves to `::1` first, and the service binds IPv4 only:
  `dial tcp [::1]:8322: connect: connection refused`.

Both surface publicly as a Cloudflare **502**, and claude.ai reports *that* as
"Couldn't register with the sign-in service" — because a 502 body is not a
valid MCP response, so the client falls back to OAuth discovery. The auth error
is a symptom; check the origin first.

Diagnose from the origin outwards, in this order:

```bash
# 1. Is the service up and listening?
systemctl is-active dk-mcp && ss -ltn | grep 8322

# 2. Does it answer MCP locally? (expect status=200)
set -a; . /opt/dk-mcp/dk-mcp.env; set +a
curl -s -o /dev/null -w 'status=%{http_code}\n' -X POST "http://127.0.0.1:8322${DK_MCP_PATH}" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"p","version":"1"}}}'

# 3. If that is 200 and the public URL is not, it is the tunnel. cloudflared
#    logs the rule it actually used, including the scheme and host:
journalctl -u cloudflared -n 50 | grep -i originService
```

Note that step 3 prints request paths, so the secret endpoint path ends up in
cloudflared's journal. That is another reason to treat it as rotatable rather
than permanent.
