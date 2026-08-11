# Backup & disaster recovery review — 2026-08-06

Lane: backup and disaster recovery. Scope: `src/bot_modules/services/db_backup.py`,
its wiring in `src/dungeonkeeper/__main__.py`, the prod `backups/` directory, the
state that lives outside the DB, and the erasure interaction flagged as **G5** in
`docs/reviews/2026-08-05-gdpr-register.md`.

**Headline: the backup mechanism is correct and its output is provably
restorable. The backup *strategy* is not a disaster-recovery strategy** — every
copy lives on one physical disk, and the retention window is counted in files
rather than hours, so a normal deploy evening can quietly erase most of it.

Everything below marked *verified* was confirmed by running it against a
read-only snapshot of the live DB, or by reading the code, or by querying
`journalctl`. Nothing was written to the live database and nothing was restarted.

---

## What was proven to work

### The restore drill (verified end-to-end)

I restored **the service's own newest artifact** —
`backups/dungeonkeeper_20260805_174401.db`, written by `_run_backup` at 17:44 —
not a re-creation of it. Full sequence, into
`/home/ben/discord-bots/dk-sessions/restore-drill/` (never over prod):

| Step | Result | Elapsed |
|---|---|---|
| `cp` backup into place | 712 MB | **0.006s** (XFS reflink, `/dev/sda3`) |
| `PRAGMA quick_check` | `ok` | 13–25s |
| `PRAGMA foreign_key_check` | no violations | <0.1s |
| Migration chain (`apply_migrations_sync`) | 167 → 169, applied 150 + 151 cleanly | **0.02s** |
| Representative reads (10 features) | all correct | <1s |

Structure: 279 tables, `journal_mode=wal`, page size 4096. Feature reads on the
restored copy: 634,653 `messages`, 1,020,453 `xp_events`, 582 `member_xp`,
1,493 `casino_blackjack_hands`, 523 `config`, 156 `pen_pals_questions`,
124 `pen_pals_sessions`, 106 `dm_consent_pairs`, 19 `anon_audit_log`,
12 `confession_threads`, 4 `econ_icon_catalog`.

The migration chain running *forward* off a backup is the important result: a
backup taken under an older schema (prod is at 149; main is at 151) catches up
in 0.02s, so restoring an older backup onto newer code is not a blocker.

**Measured RTO ≈ 3–5 minutes, dominated entirely by bot startup**
(`TimeoutStartSec=180` in the unit — the ML stack load), not by data movement.
**RPO is up to 6 hours** (the backup interval), or near-zero when the live DB
file is still readable and only the process is broken.

### WAL correctness (verified — this is a PASS, not a finding)

`db_backup.py:75-83` uses `sqlite3.Connection.backup()`, the online backup API.
It does **not** `cp` a live WAL database, so the known-malformed failure mode
does not apply. Confirmed twice over: by reading the code, and by
`quick_check` returning `ok` on the service's real artifact above.

Backups are also fast and non-disruptive in practice — journald shows
2.5s / 2.8s / 4.0s / 12.7s for the last four runs, and **zero failures in 30
days** (`journalctl -u dungeon-keeper -S "30 days ago" | grep -ci "backup failed"`
→ 0).

---

## Findings

### B1 — No off-device backup at all. *(High)*

Every copy of the data is on **one physical disk**:

```
$ findmnt -no FSTYPE,SOURCE,TARGET /      →  xfs /dev/sda3 /
$ lsblk /dev/sda                          →  238.5G  TWSC TSC10N256-H6Q10S (single disk)
```

`backups/` is `db_path.parent / "backups"` (`db_backup.py:37`) — the same
directory tree, same filesystem, same device as `dungeonkeeper.db`. There is no
`restic`, `borg`, `rclone`, or `duplicity` installed; `crontab -l` is empty and
`systemctl list-timers` shows no backup timer. `rsync` exists but nothing
invokes it. **Verified.**

Stated plainly: *this is not a backup against disk failure.* It protects against
logical damage only — a bad migration, a bad bulk UPDATE, an accidental
`DELETE`. It does not protect against SSD failure, filesystem corruption, or
`rm -rf` of the repo directory. Any of those loses 746 MB / 635k messages /
1.02M xp events / all economy and casino state with no recovery path.

Severity High because the loss is total and unrecoverable, and because the
system currently *reads* as backed-up — five healthy files in `backups/` invite
exactly the wrong conclusion.

**Compounding: `.env` has no backup anywhere.** It is gitignored
(`.gitignore:1`), so it is not in git and it is not in the DB backup. It holds
the Discord bot token and every API key. A disk loss means re-issuing all
credentials from scratch, with no record of what was in there.

**Fix.** Push one copy off the device. Minimal version, no new dependency:

```bash
# after each backup, or on a systemd timer
rsync -a --delete /home/ben/discord-bots/dungeon-keeper/backups/ /mnt/<other-disk>/dk-backups/
```

Note the unit constraint: `dungeon-keeper.service` sets `ProtectHome=read-only`
with `ReadWritePaths=/home/ben/discord-bots/dungeon-keeper`, so the *bot* cannot
write outside that tree without a unit edit. Cleanest is a **separate systemd
timer** running as `ben` that copies `backups/` (plus `.env`, encrypted) to
off-device storage — that keeps the bot's hardening intact. Encrypt whatever
leaves the machine: these files contain message content for 455,133 rows.

### B2 — Retention is counted in files, not time, and every restart burns a slot. *(High)*

`DEFAULT_RETENTION_COUNT = 5` at a 6-hour interval *looks like* a 24-hour
window. It is not, because `db_backup_loop` runs a backup **immediately** on
start (the `while` body precedes the `sleep` — `db_backup.py:47-56`), so every
bot restart consumes one of the five slots.

Observed reality (**verified** from journald and `ls`):

- **20 restarts in the last 14 days** (`grep -c "DB backup loop started"`) ≈ 1.4/day.
- The five files on disk right now span **08-04 23:24 → 08-05 17:44 = 18.3
  hours**, not 24.
- Two of them are **26 minutes apart** (23:24 and 23:50) — a restart at 23:50
  spent a slot duplicating a backup taken minutes earlier.

The failure case is sharp: a deploy or debugging evening with five restarts
inside an hour leaves all five slots holding near-identical copies, and the
oldest recoverable state is *minutes* old. Any logical corruption introduced
before that burst is then unrecoverable — and nothing warns that the window just
collapsed. Given B1 leaves these five files as the only copies in existence,
this is High rather than Medium. Tonight, with five review lanes and a pending
restart, is precisely the shape of evening that triggers it.

**Fix.** Make retention time-based, or hybrid — keep by count *and* floor by
age, so restart-driven backups can never evict a backup younger than the
intended window:

```python
def _prune_old_backups(backup_dir, stem, retention_count, min_age_hours=48.0):
    now = time.time()
    backups = sorted(backup_dir.glob(f"{stem}_*.db"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[retention_count:]:
        if (now - old.stat().st_mtime) < min_age_hours * 3600:
            continue  # inside the guaranteed window — keep it
        old.unlink(missing_ok=True)
```

Cheap alternative that also helps: skip the startup backup when the newest
existing backup is younger than, say, half the interval. Disk is not the
constraint — 212 GB free, and each backup is ~712 MB.

### B3 — A failing backup is invisible to everyone. *(Medium)*

`db_backup.py:52-55`:

```python
except Exception:
    _log.exception("Database backup failed")
```

This `except` sits **inside** the `while` loop, so a failure never propagates to
`AppContext._resilient_task` (`app_context.py:258-291`). That supervisor would
otherwise log a crash and escalate to "exceeded max restarts". As written, a
backup that fails every 6 hours forever produces only a recurring ERROR line and
no other signal. There is no Discord alert, no dashboard surface, no health
check. **Verified by reading both call paths.**

Where the ERROR lands: journald (persistent — `/var/log/journal` exists, so it
survives reboot) and `log.txt`, which is **wiped on every boot**
(`__main__.py:89`) and rotates at ~2 MB with `backupCount=1`. So the durable
record is journald only, and only if someone thinks to look.

One genuine mitigation, worth stating because it bounds the damage: pruning
happens *after* a successful copy (`db_backup.py:95`), so a failing backup does
not delete old ones. Failure is fail-safe for retention. The risk is not
sudden loss — it is that the newest backup silently stops advancing and ages out
of usefulness while everything looks fine.

**Fix.** Track consecutive failures and surface them. The bot already has admin
notification surfaces; the smallest useful version is to record
`last_backup_ok_at` in `config` and have the health/analytics panel flag it when
it is older than 2× the interval. Alternatively, let the exception escape after
N consecutive failures so `_resilient_task` treats it as a crashing loop.

### B4 — A partial backup is indistinguishable from a good one, and sorts newest. *(Medium)*

`_run_backup` writes directly to the final filename (`db_backup.py:69-83`). If
`src.backup(dst)` raises part-way — disk full, I/O error — the destination file
still exists, holds a truncated database, and carries the **newest mtime** in the
directory. `run_backup_now` returns `backups[-1]` sorted by mtime
(`db_backup.py:122-123`), i.e. it would hand back exactly that corrupt file.
More to the point, an operator at 3am running `ls -t backups/ | head -1` gets
it too.

**Verified by reading the code; not empirically triggered** (no backup has ever
failed in prod, per B3's journald check), so this is a latent trap rather than an
observed one.

**Fix.** Write to a temp name and rename only on success — `os.replace` is atomic
within a filesystem, so a partial file never occupies the real name:

```python
tmp_path = backup_path.with_suffix(".db.partial")
src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
try:
    dst = sqlite3.connect(str(tmp_path))
    try:
        src.backup(dst)
    finally:
        dst.close()
finally:
    src.close()
os.replace(tmp_path, backup_path)   # atomic
```

Two side benefits shown above: opening the source with `mode=ro` removes any
chance of the backup path touching the live DB, and `.partial` leftovers are
self-identifying and outside the `{stem}_*.db` prune glob.

### B5 — Ad-hoc backups never age out, so the erasure window is unbounded. *(Medium — extends register G5)*

G5 in `docs/reviews/2026-08-05-gdpr-register.md` already records that backups
retain erased users and asks for a documented retention window. **Net-new here:
that window cannot be honoured today, because the prune glob silently skips
hand-made copies.**

`_prune_old_backups` globs `f"{stem}_*.db"` → `dungeonkeeper_*.db`
(`db_backup.py:105`). The directory contains:

```
dungeonkeeper-pre-tod-backfill-20260729.db    681 MB, 8 days old
```

The separator is a **hyphen**, not an underscore, so it does not match and is
**never pruned**. Confirmed directly:

```python
fnmatch("dungeonkeeper-pre-tod-backfill-20260729.db", "dungeonkeeper_*.db")  # False
fnmatch("dungeonkeeper_20260805_174401.db",           "dungeonkeeper_*.db")  # True
```

So a complete, indefinitely-retained, pre-erasure copy of the entire database —
including message content for ~455k rows — sits in the backup directory outside
every retention policy. Any `purge_user_data` run since 2026-07-29 is not
reflected in it, and nothing will ever remove it.

**Fix.** Three parts, all small:
1. Move ad-hoc/pre-migration snapshots **out** of `backups/` (e.g. a sibling
   `snapshots/` directory) so the automated policy governs one directory only.
2. Add an age-based sweep for anything in `backups/` regardless of name shape.
3. Add a line to the erasure runbook: after a purge, delete or re-purge ad-hoc
   snapshots by hand — they are the copies the automated window does not reach.

**Recommended retention window: 48 hours**, with B2's age floor enforcing it.
That is enough to catch a bad migration through a full day-night cycle, and it
bounds the erasure exposure to a window that can be stated to a requester in
writing ("erasure completes across all copies within 48 hours"). If off-device
copies land per B1, they need the same window and the same statement.

### B6 — The erasure runbook misdescribes the backup schedule. *(Low)*

`docs/gdpr_runbook.md` (then named `gdpr_erasure_runbook.md`) says:

> **Backups** — nightly backups retain pre-erasure copies until they age out…

Backups are **6-hourly, not nightly** (`DEFAULT_INTERVAL_HOURS = 6`, confirmed by
the journald line `DB backup loop started: every 6.0h, keeping 5 backups`). The
retention window is never named, so an operator answering "when is this erasure
complete everywhere?" has nothing to quote. The surrounding advice ("do not
restore past backups over an erasure without re-running step 2") is correct and
worth keeping.

**Fix.** Replace with the measured numbers and add the ad-hoc-snapshot caveat
from B5. Corrected text is in the new runbook (below) and should be mirrored
into step 5.

### B7 — A DB-only restore orphans on-disk media. *(Low)*

Partially noted already in `docs/reviews/2026-08-05-image-guard-guess.md:75` for
the guess files; recorded here with counts and the full inventory.

`guess_rounds.original_path` / `crop_path` and `econ_icon_catalog.image_path`
store filesystem paths, and those directories are gitignored (`.gitignore:30`
`guess_cache/`) and not in the DB backup. Measured against the restored copy:

| Reference | Live files that a DB-only restore would orphan |
|---|---|
| `guess_rounds.original_path` | **5** present (18 already missing — expected, per the guess G2 cleanup) |
| `econ_icon_catalog.image_path` | **4** present |

Small in absolute terms, and both degrade gracefully rather than crashing (the
row survives, the image 404s). Recorded so the class is on the map.

**Full inventory of state outside the DB**, with what a DB-only restore loses:

| Path | Size | In git? | Consequence of loss |
|---|---|---|---|
| `.env` | 4 KB | **No** (gitignored) | **Fatal** — bot token + all API keys. Re-issue from scratch. |
| `models/` | 4.5 GB | No | ML stack (NSFW classifier, whisper, llama). Re-downloadable, slow. |
| `lavalink/` | 105 MB | Jar + plugins gitignored | Music dies until the jar is re-fetched. |
| `assets/` | 17 MB | Yes | Recoverable from git. Note: the remote test runner already lacks `border.png`. |
| `guess_cache/` | 5.2 MB | **No** | 5 live guess originals orphaned (above). |
| `econ_icon_catalog/`, `econ_role_icons/` | 96 KB | No | 4 shop icons orphaned. |
| `/etc/systemd/system/*.service` | — | **No** | Unit files for `dungeon-keeper`, `cloudflared`, `tod-mcp` etc. live only in `/etc`. `deploy/` documents install but the live edits are not tracked. |
| Cloudflare tunnel credentials | — | **No** | Dashboard unreachable until the tunnel is re-provisioned. |

The three that actually block a rebuild are `.env`, the systemd units, and the
tunnel config. All three are tiny. Backing them up costs nothing and is the
single highest-leverage change alongside B1.

### B8 — `db_backup.py` documents environment configuration that does not exist. *(Low)*

`db_backup.py:20` — `# Defaults — configurable via environment variables`.
Nothing in the module reads `os.environ`, and the call site
(`__main__.py:408`) passes neither `interval_hours` nor `retention_count`:

```python
bot.startup_task_factories.append(lambda: db_backup_loop(bot, db_path))
```

So both values are hardcoded and the comment misleads anyone trying to retune
the window. **Fix.** Either delete the comment, or honour it —
`float(os.getenv("DB_BACKUP_INTERVAL_HOURS", 6))` /
`int(os.getenv("DB_BACKUP_RETENTION", 5))`. Given B2 wants a tunable window,
honouring it is the better half.

### B9 — Pruning leaves orphaned `-shm` / `-wal` siblings. *(Info)*

`backups/` currently holds `dungeonkeeper_20260728_072319.db-shm` (32 KB) and
`…-wal` (0 B) whose `.db` was pruned eight days ago — the glob matches `.db`
only. Harmless and tiny, but they make the directory listing confusing at 3am,
which is when it will be read. **Fix.** Unlink the `-shm`/`-wal` siblings
alongside the `.db` in `_prune_old_backups`.

### B10 — `run_backup_now` is dead code with an edge-case crash. *(Info)*

`db_backup.py:114-123` has **no callers** anywhere in `src/` or `scripts/`
(verified by grep). If it were called with `retention_count=0` it would prune
every backup and then raise `IndexError` on `backups[-1]`. Not reachable today.
**Fix.** Delete it, or keep it as the documented manual-backup entry point and
guard the empty case — the runbook below assumes the latter and calls
`_run_backup` directly instead.

---

## Summary

| ID | Severity | Finding | Status |
|---|---|---|---|
| B1 | **High** | No off-device backup; DB + all copies + `.env` on one disk | **Fixed and live** — installed 2026-08-07, first copy verified on the NAS |
| B2 | **High** | Retention counted in files; restarts collapse the window (observed 18.3h, not 24h) | Fixed |
| B3 | Medium | Backup failure logs and is never surfaced to anyone | **Fixed 2026-08-11** — outcome markers + dashboard surface |
| B4 | Medium | A partial backup keeps the real filename and sorts newest | Fixed |
| B5 | Medium | Ad-hoc snapshots never prune → unbounded erasure exposure (extends G5) | Documented; no auto-delete by design. **The one live offender was deleted 2026-08-11** |
| B6 | Low | Erasure runbook says "nightly"; backups are 6-hourly, window unnamed | Fixed |
| B7 | Low | DB-only restore orphans 9 media files; `.env`/units/tunnel unbacked | Documented; rides with B1 |
| B8 | Low | Docstring promises env configuration that does not exist | Fixed |
| B9 | Info | Prune leaves `-shm`/`-wal` orphans | Fixed |
| B10 | Info | `run_backup_now` unused; `retention_count=0` → `IndexError` | Fixed |

### What was fixed here

`db_backup.py`, covered by the new `tests/test_db_backup.py` (28 tests — the
service previously had **none**):

- **B2** — two guards. A backup is now *skipped* when the newest one is younger
  than half the interval, so a restart burst no longer creates near-duplicates
  at all (the root cause); and `_prune_old_backups` grew a `min_age_hours`
  floor (default 48h) so nothing inside the intended window can be evicted
  regardless of count. `run_backup_now` deliberately ignores the skip gap — a
  manual backup is always taken.
- **B4** — the copy is built as `<name>.db.partial` and `os.replace`d into
  position only on success, so a failed copy can never occupy the real
  filename. The source is now opened `mode=ro`, removing any possibility of
  this path writing to the live database. Verified the defect empirically
  against the pre-fix module before fixing: a failing copy left
  `dungeonkeeper_20260806_091015.db` stranded at the real name as the newest
  entry.
- **B8** — `DB_BACKUP_INTERVAL_HOURS`, `DB_BACKUP_RETENTION`, and
  `DB_BACKUP_MIN_AGE_HOURS` are now honoured, with non-numeric and
  non-positive values logged and ignored rather than crashing the loop.
- **B9** — `-wal`/`-shm` siblings are unlinked alongside the `.db`.
- **B10** — `run_backup_now` returns a real path or raises, instead of
  `IndexError`.

**B5 deliberately got no code change.** Auto-deleting files the operator
created by hand is the wrong default; the fix is the directory split plus the
runbook guidance, both of which landed.

### B1 — the off-device backup

Destination chosen: the **Synology NAS on the LAN**, found at `192.168.174.3`
("NaturewoodNAS", MAC OUI `90:09:d0` = Synology, DSM on 5000/5001, SSH + SMB +
NFS open, rsync daemon closed). Destination `/volume1/Storage/botbackups`,
21 TB free.

**Installed and verified 2026-08-07.** The plan said rsync over SSH; the NAS
refused it, and the transport had to change. What was learned installing it is
worth recording, because all three failures look like misconfiguration and are
not:

| Attempt | Result on the live NAS |
|---|---|
| `ssh-copy-id` | Password accepted, then `Could not chdir to home directory /var/services/homes/admin` → `mkdir: cannot create directory '.ssh': Permission denied`. **DSM's User Home service was off**, so no account had a home directory to hold `authorized_keys`. One DSM checkbox. |
| `rsync` over SSH | `Permission denied, please try again.` from the far end. DSM ships a **setuid-root rsync that refuses `--server` mode for a non-root uid**; DSM 7 also disables root SSH, so there is no account to run it as. |
| `scp` / `sftp` | `subsystem request failed on channel 0` — **DSM's sshd has no sftp subsystem**, and modern `scp` speaks SFTP. |
| `ssh 'cat > file'` | **Works.** |

So the transport is a plain SSH pipe. Both blocked options could be unblocked
with DSM service checkboxes, but a DSM update that reset one would silently
break the backup; the pipe depends on nothing but sshd. The cost is delta
transfer and resume — neither of which matters much for a SQLite file that
changes throughout, over a LAN that moves it in 8 seconds.

What rsync *was* silently providing is end-to-end verification, so the script
now does that explicitly, and more strongly than before:

- `scripts/backup_to_nas.sh` — verifies the newest local backup with
  `quick_check` *before* shipping it (a corrupt copy overwriting a good one on
  the NAS would be worse than no backup); pipes it to `<name>.partial`,
  compares **sha256 on both ends**, and renames into place only on a match —
  the same discipline B4 imposed locally, now applied to the far end. Then an
  AES256 bundle of `.env` + the systemd units, a mirror of the small media
  directories, a 14-day prune, and a sweep of stale `.partial` files (a run
  killed mid-transfer would otherwise strand ~790 MB forever).
- `deploy/dk-nas-backup.{service,timer}` — daily at 04:30, `Persistent=true` so
  a run missed while the box was off happens at next boot. Runs as `ben` in its
  own unit so the bot keeps `ProtectHome=read-only`.
- `deploy/nas-backup.conf.example` — now carries the verified values.

**Measured first run: 12.4s end to end**, 790 MB at roughly 100 MB/s. The NAS
copy was then checked *on the NAS* — `quick_check` → `ok`, 646,044 messages —
and the secrets bundle pulled back and decrypted, yielding a byte-identical
`.env` plus both unit files.

**One thing the plan got wrong about privacy.** B1 said "encrypt whatever
leaves the machine"; the script encrypts the secrets bundle but *not* the
database. The share is world-writable by DSM default, so the first run landed
755 MB holding 646k rows of member message content as `-rwxrwxrwx`. Both NAS
directories are now `chmod 700` and every file written `600`, re-asserted on
each run so an older copy heals rather than staying exposed. That is
mode-based, not cryptographic: **anyone with the `admin` DSM account can still
read the database.** Encrypting the DB at rest was not done — it would mean
holding a second passphrase whose loss costs the whole backup, and the threat
it addresses (another LAN account) is smaller than that risk. Recorded as a
deliberate choice, not an oversight.

Retention on the NAS is **14 days**, chosen deliberately against the G5
trade-off: long enough to catch corruption nobody noticed for a fortnight,
short enough to state in writing to an erasure requester. That number now
appears in `gdpr_runbook.md` and in the config, and the two must move
together.

The GPG round-trip was tested end-to-end with a stand-in `.env` (encrypt → tar
→ decrypt → compare) before shipping, because a secrets bundle that cannot be
opened is worse than one that was never made. **The passphrase must live in a
password manager, not only on the machine the backup exists to survive** — that
warning is in the config template and the deploy README.

### What still needs you

- ~~**Install B1**~~ — done 2026-08-07, verified end to end. The one thing only
  you can do remains: **record the GPG passphrase**
  (`~/.config/dk-backup/env-passphrase`) **in a password manager.** It exists
  today only on the disk this backup exists to survive, which makes the secrets
  bundle unopenable in precisely the disaster it was built for.
- ~~**B3** needs a choice of surface.~~ **Fixed 2026-08-11** — see below.
- ~~**The Cloudflare tunnel credentials** are the one thing still outside the
  backup.~~ **Checked 2026-08-07 — already covered.** There is no
  `/etc/cloudflared/` and no `~/.cloudflared/` on this host: the tunnel is run
  token-only, with the credential embedded as `--token <jwt>` in
  `ExecStart` of `/etc/systemd/system/cloudflared.service`. That unit file is
  already inside the encrypted secrets bundle, so the tunnel restores with it.
  Nothing further to fold in — but note the consequence: **the secrets bundle
  now holds the tunnel credential as well as the bot token**, which is exactly
  why it is AES256'd before it leaves the machine.

**Nothing found in WAL correctness** — the service uses the online backup API
correctly, and the artifact it produces was proven restorable end-to-end.
**Nothing found in backup reliability** — zero failures in 30 days, runtimes of
2.5–12.7s.

### B3 — the staleness signal (2026-08-11), and the incident that justified it

Closed the way the finding recommended, plus the off-device half:

- `db_backup.py` writes three global `config` markers — `backup_last_ok_at`,
  `backup_consecutive_failures`, `backup_last_error`. Both writers are
  best-effort and swallow their own exceptions: the likeliest cause of a backup
  failing is a sick database, which is also the likeliest cause of *recording*
  that failure failing, and neither may take the loop down. A deliberate skip
  (B2's restart guard) counts as success — the mechanism working, not failing.
- `assess_backup_health()` is pure and holds every threshold: local stale at
  **2× the interval** (12h), off-device stale at 2× its daily timer (48h), a
  failure streak that is a warning at 1–2 and an error at 3+. An *unconfigured*
  off-device backup reports nothing — a warning that fires on every dev box is
  a warning nobody reads on the one machine that matters.
- Surfaced twice: the home dashboard's **Configuration Problems** card (the
  existing "set up but silently broken" idiom — it rides that endpoint rather
  than a new widget, because an existing layout would never gain a new widget
  on its own), and a **Backups** table on **System Stats** with per-copy age.
  The card keeps reporting while the bot is disconnected, since the check reads
  only the filesystem and the DB — and a bot that is down is exactly when its
  backup loop has stopped.

**The incident.** Wiring this up is what found that **B1's off-device backup
had never run through its timer — not once since it was installed on 08-07.**
Every firing died at `203/EXEC`:

> `Unable to locate executable '…/scripts/backup_to_nas.sh': Permission denied`

The script is 0755 and runs fine from a shell. The cause is **SELinux**: repo
files are `user_home_t` and the system manager cannot `execve` one. The denial
is `dontaudit`'d, so `ausearch -m AVC` shows nothing at all. `ExecStart` now
goes through `/usr/bin/bash`, which is immune to the relabelling a `chcon` fix
would suffer on the next edit. Full write-up in `deploy/README.md`.

Two things worth keeping from it. The install verification was
`./scripts/backup_to_nas.sh` — a shell dry run, which cannot detect this class
of failure; the README now verifies with `systemctl start`. And the review's
own note that "the NAS job *does* have failure visibility already — it exits
non-zero, so a failed run shows in `systemctl --failed`" was **true and
useless**: it sat in `systemctl --failed` for four days and told nobody. That
is precisely the argument B3 was making, demonstrated on B1's own fix.

Also closed while in there: the NAS `db/media` directory was `drwxrwxrwx`
(DSM's share default, inherited by `mkdir -p` and preserved through the swap)
while `db/` and `secrets/` were correctly 700 — so the `guess_cache` mirror,
member-submitted photos, was readable by every account on the LAN. The script
now creates and re-asserts `700` on the media directory and strips group/other
from each mirrored tree before swapping it in.

Suggested order: B1 and B7's three small files together (one timer, off-device,
encrypted) → B2's age floor → B5 + B6 (directory split + runbook text, both
doc-sized) → B4's atomic rename → B3's staleness signal.

Restore procedure: **[`docs/disaster_recovery_runbook.md`](../disaster_recovery_runbook.md)**.

---

*Method note: all timings measured on a read-only snapshot in
`/home/ben/discord-bots/dk-sessions/restore-drill/` (since removed). The live
database was opened `mode=ro` only; nothing was restarted.*
