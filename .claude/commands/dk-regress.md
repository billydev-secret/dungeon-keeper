---
description: Full pre-release check on main — every test, both lint layers, the type check, the browser sweep, and a design scan of what is about to go live
argument-hint: [--no-standards] [--no-browser] [--since REF]
allowed-tools: Bash(git:*), Bash(python:*), Bash(python3:*), Bash(npm:*), Bash(npx:*), Bash(systemctl:*), Bash(sqlite3:*), Bash(ls:*), Agent, Read, Grep, Glob
---
The last check before a restart puts new code in front of members. `/dk-ship` is
per-feature and deliberately fast; this is the opposite — slow, whole-repo, and run
once when a batch of merges is ready to go live.

**This command never restarts anything.** It reports go/no-go; Billy presses the
button.

Args in `$ARGUMENTS`: `--no-standards` skips the design scan, `--no-browser` skips the
panel sweep (use when there is no browser installed), `--since REF` overrides the
auto-detected deployed commit.

Run from the **prod checkout**, on `main`. Steps, in order, stopping with a clear
report on any failure — but finish the cheap diagnostics (steps 1-2, 6) even if a
later stage fails, because a go/no-go is more useful complete than early.

1. **Guard.** `git rev-parse --abbrev-ref HEAD` must be `main`, and
   `git status --porcelain -uno` must be empty. Untracked files are fine. If a
   session worktree is mid-ship the merge lock may be held — say so rather than
   racing it.

2. **Work out what is actually about to ship.** The running bot is whatever was
   deployed at the last restart, so the delta is everything since that commit:

       T=$(systemctl show dungeon-keeper --property=ActiveEnterTimestamp --value)
       DEPLOYED=$(git rev-list -1 --before="$T" main)

   Report the commit, its date, and `git rev-list --count $DEPLOYED..main`. This is
   the range the standards scan reviews and the number that tells the reader how big
   this release is. `--since REF` overrides it. If the service is not running or the
   timestamp is empty, say so and fall back to the `last-full-gate` tag.

3. **The full gate, with both heavy checks forced on:**

       python scripts/gate.py --pyright --browser

   That is ruff, pyright, the whole pytest suite, and a sweep of **every** panel at
   every viewport. It is the slow one — allow ~15 minutes and expect pyright alone to
   take six. Run it **solo**: a parallel full run alongside other sessions can exhaust
   the tmpfs quota and spray hundreds of bogus sqlite errors. If sessions are gating,
   say so and offer to wait rather than starting it into contention.

   `--no-browser` drops the sweep for a machine without Chromium.

4. **The JS lint layer**, which pytest does not cover and CI treats as blocking:

       npm install --no-save
       npx eslint src/web_server/static/js
       npx stylelint "src/web_server/static/**/*.css"

   Seven eslint warnings is the known-clean baseline, not a failure. `stylelint`
   takes `--fix` for the mechanical ones.

5. **Standards scan** (skip with `--no-standards`). Launch the **`standards-review`**
   agent over `$DEPLOYED..main` — the design and reuse rules no test can express.
   It reports and never edits. Relay its findings verbatim; a finding here is not
   automatically a blocker, but it is the user's call, not yours.

   A release-sized range is much larger than a feature branch, so expect it to take
   longer than the same scan does in `/dk-ship` and to have more to say.

6. **What the restart will actually do to the database**, by rehearsing it:

       python scripts/migration_dryrun.py

   Migrations run at boot, so a migration that passes against a fresh test schema
   and fails against production's actual columns takes the bot down at the moment
   new code reaches members. This snapshots the live database (via sqlite3's backup
   API — a `cp` of a WAL database is very often malformed), applies everything
   `schema_version` has not recorded, and reports. The live file is never written.

   It flags destructive statements (`DROP TABLE`, `DROP COLUMN`, `DELETE FROM`) and
   prints the `.backup` line to run before restarting when it finds one. A
   destructive migration is not a blocker — several are deliberate — but it is the
   difference between restarting with a backup and restarting without one.

7. **Dials that do nothing:**

       python scripts/config_key_audit.py

   Read-only. Lists config keys stored in the database that no code reads — a
   branch whose reader never merged, or a feature deleted while its rows outlived
   it. Each is a dial someone can set, and change nothing by setting. Keys built as
   a prefix plus a settings-dataclass field are understood, so this reports about a
   dozen rather than the hundred-odd a naive string search claims.

8. **Report go / no-go.** One short verdict, then the detail: how many commits are
   shipping, what failed if anything, the pending migrations and whether any is
   destructive, any dead config keys, and any standards findings. Say plainly whether anything found is a
   blocker or a note.

   Close by reminding the user that **they** restart, and that a batch this size is
   worth restarting when they can watch it rather than last thing at night.
