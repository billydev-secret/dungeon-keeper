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
| B1 | **High** | No off-device backup; DB + all copies + `.env` on one disk | **Open — needs a destination decision** |
| B2 | **High** | Retention counted in files; restarts collapse the window (observed 18.3h, not 24h) | Fixed |
| B3 | Medium | Backup failure logs and is never surfaced to anyone | **Open — needs an alert-surface decision** |
| B4 | Medium | A partial backup keeps the real filename and sorts newest | Fixed |
| B5 | Medium | Ad-hoc snapshots never prune → unbounded erasure exposure (extends G5) | Documented; no auto-delete by design |
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

### What still needs you

- **B1** is the one that matters and it needs a decision I can't make: there is
  one physical disk in this box, so "off-device" means picking a destination
  (external drive, another host, or object storage). Once that exists it wants
  a separate systemd timer running as `ben` — not the bot, whose
  `ProtectHome=read-only` hardening should stay intact — copying `backups/`
  plus `.env`, encrypted. **`.env` is the urgent half**: 4 KB, gitignored, and
  its loss means re-issuing every credential.
- **B3** needs a choice of surface. Cheapest useful version: record
  `last_backup_ok_at` in `config` and have the health panel flag it when it is
  older than 2× the interval.

**Nothing found in WAL correctness** — the service uses the online backup API
correctly, and the artifact it produces was proven restorable end-to-end.
**Nothing found in backup reliability** — zero failures in 30 days, runtimes of
2.5–12.7s.

Suggested order: B1 and B7's three small files together (one timer, off-device,
encrypted) → B2's age floor → B5 + B6 (directory split + runbook text, both
doc-sized) → B4's atomic rename → B3's staleness signal.

Restore procedure: **[`docs/disaster_recovery_runbook.md`](../disaster_recovery_runbook.md)**.

---

*Method note: all timings measured on a read-only snapshot in
`/home/ben/discord-bots/dk-sessions/restore-drill/` (since removed). The live
database was opened `mode=ro` only; nothing was restarted.*
