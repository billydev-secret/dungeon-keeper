# Implementation plan — QA Tracker (volunteer testing crew + currency rewards)

**Status:** stages 0–4 shipped (0–3 merged 2026-07-16, migration 077; stage-4
auto-archive sweep landed after); stage-4 polish + the bounty idea still open.

Replaces the plain-text `#testing-queue` mirror with interactive **QA cards**:
one embed per test entry, Pass / Fail / Blocked buttons, verdicts recorded in
SQLite, testers paid in economy currency, admin oversight on the dashboard.
Audience: volunteer members who hold a configurable QA-crew role — this is
member self-service in Discord; **all admin knobs live on the web dashboard**
(working-agreement rule; no admin slash commands).

Decisions locked with Billy 2026-07-16:

- **Instant pay + admin void.** Every first verdict on a test pays coins
  immediately (daily cap). A bogus verdict is voided from the dashboard, which
  claws the coins back.
- **1 pass verifies.** The first Passed verdict turns a card green. Later
  verdicts still record (and pay) until the card is archived.
- **Cards live in the existing `#testing-queue` channel** — volunteers get
  access to it; no new channel.

Commits reference stages as `QA Tracker (stage N): …`. Each stage: built in a
worktree, `scripts/gate.py` green, `docs/INDEX.md` updated in the same commit,
merged to main for live testing before the next stage starts. QA cards post
automatically from the commit's Testing: section (TESTING_QUEUE.md retired
2026-07-18 — see the addendum below). Stages 1–2 only go live after a bot restart (user pushes that
button).

## Layout

```
src/migrations/077_qa_tracker.sql            # qa_tests, qa_verdicts, settings defaults
src/bot_modules/services/qa_service.py       # CRUD, settings loader, status math,
                                             #   payout (apply_credit) + void (apply_debit)
src/bot_modules/cogs/qa_cog.py               # DynamicItem buttons, fail modal, thread notes
scripts/post_testing_docs.py                 # hook grows: insert qa_tests row + post card
src/web_server/routes/qa.py                  # board + config + void APIs (admin-gated)
src/web_server/static/js/panels/qa-tracker.js  # status board + config (model: mod-tickets.js)
tests/test_qa_service.py  tests/test_qa_cog.py  tests/web/test_qa_routes.py
```

## Data model (migration `077_qa_tracker.sql`)

```sql
qa_tests(
  id INTEGER PRIMARY KEY,
  guild_id INTEGER NOT NULL,
  entry_key TEXT NOT NULL,          -- poster's entry_key(): heading minus trailing parens
  title TEXT NOT NULL,
  body_md TEXT NOT NULL,            -- the checklist body (commit's Testing: section, or
                                     --   a checklist doc's ### block)
  commit_sha TEXT, commit_subject TEXT,
  channel_id INTEGER, message_id INTEGER,  -- the posted card
  thread_id INTEGER,                -- created lazily on first fail/blocked note
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','passed','failed','blocked','archived')),
  verified_by INTEGER, verified_at TEXT,   -- first passer
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX ... ON qa_tests(guild_id, entry_key, commit_sha);

qa_verdicts(
  id INTEGER PRIMARY KEY,
  test_id INTEGER NOT NULL REFERENCES qa_tests(id),
  guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
  verdict TEXT NOT NULL CHECK (verdict IN ('pass','fail','blocked')),
  note TEXT,                        -- required for fail, optional for blocked
  paid_amount INTEGER NOT NULL DEFAULT 0,   -- coins minted for this verdict (0 = unpaid)
  voided_by INTEGER, voided_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE (test_id, user_id)         -- one verdict per tester per test; re-click = update
);
```

The `UNIQUE(test_id, user_id)` constraint is the payment race-anchor, following
the economy's established `INSERT OR IGNORE` → `rowcount > 0` dedup pattern
(`econ_logins`, `econ_qotd_rewards`): pay only when the INSERT lands, never on
verdict *updates*. Daily cap = `COUNT(*)` of paid verdicts per user per
guild-local day (same `local_day` helper the economy uses).

Settings ride the existing config KV table under a `qa_` prefix (loader
modeled on `EconSettings`, `economy_service.py:28`):
`qa_role_id` (QA-crew role; 0 = admins only), `qa_channel_id` (defaults to the
current `#testing-queue` id), `qa_reward` (default **15**, between QOTD 10 and
game-win 20), `qa_daily_cap` (default **4** paid verdicts/day), `qa_enabled`.

## Status math (in `qa_service.py`, pure function)

Precedence: any un-voided **fail** ⇒ `failed` (red) · else any **blocked** ⇒
`blocked` (amber) · else any **pass** ⇒ `passed` (green, stamped
`verified_by/at` from the first passer) · else `pending` (gray). `archived` is
admin-only (dashboard) and terminal. Red/green here is semantic — the
`resolve_accent_color` convention explicitly keeps status colors.

## Economy integration

- Payout: `apply_credit(conn, guild, user, settings.qa_reward, "qa_reward",
  meta={"test_id": …, "verdict": …})` inside the same transaction as the
  verdict INSERT. Add `qa_reward` to `FAUCET_GROUPS`
  (`src/bot_modules/economy/metrics.py:26`) or the dashboard income-mix stats
  silently misattribute it.
- **Void = the economy's first clawback.** No negative-adjustment path exists
  today (`/bank grant` and the web grant are credit-only). Void calls
  `apply_debit(…, kind="qa_void", meta={"verdict_id": …})` for
  `min(balance, paid_amount)` — `apply_debit` refuses to go negative, so a
  spent-down wallet claws back what's there and records the shortfall in
  `meta`. `qa_void` is a debit kind, **not** added to `FAUCET_GROUPS`.
  Voiding also marks the verdict row and recomputes the card status.
- Rewards will appear in the economy register-channel feed automatically once
  that branch merges (it announces from the ledger side).

## Discord surface (`qa_cog.py` + poster changes)

**Card** — one embed per test entry, posted by the post-commit hook:
title = entry heading; description = the authored checklist body (rendered as
plain `•` lines — the boxes are now the buttons' job); footer = short sha +
subject; color = status. Fields show the verdict tally and
"✅ Verified by @name · <t:…:R>" once passed.

**Buttons** — three `discord.ui.DynamicItem[Button]` classes (the
`pen_pals_cog.py:831` pattern), templates `qa:v:(?P<id>\d+):(pass|fail|blocked)`.
Restart-safe, no per-message registration, and — crucially — they work on
messages the **standalone hook script** posted via raw REST, because dynamic
items dispatch purely on `custom_id`. Handler flow:

1. Gate: clicker has `qa_role_id` (or admin). Otherwise ephemeral "join the QA
   crew" nudge.
2. **Fail** → modal requiring a "what went wrong" note; **Blocked** → modal
   with optional note; **Pass** → no modal.
3. Upsert verdict (`INSERT … ON CONFLICT(test_id,user_id) DO UPDATE`); pay on
   fresh insert only, if under the daily cap; commit.
4. Fail/blocked notes post into the card's thread (created lazily on first
   note) so failure detail lives with the test.
5. Re-render the embed (status color, tally, verified-by) and confirm
   ephemerally ("Recorded — +15 🪙" / "Recorded — daily cap reached, no pay").

**Poster/hook** (`scripts/post_testing_docs.py`): for each new queue entry the
hook now (a) INSERTs a `qa_tests` row into the prod DB (stdlib `sqlite3` —
the hook stays dependency-free; DB is WAL so writing beside the live bot is
routine), (b) POSTs the card embed + component rows via REST with the
`qa:v:<id>:<verdict>` custom_ids, (c) stores `message_id` back on the row.
The entry-level ✅ reaction from `f3c345c` is retired (the buttons replace
it; the role-checklist channels stay plain text and never had it). The full
dump (`--only testing-queue --yes`) doubles as the backfill: pending entries
post as cards keyed on the dump's HEAD sha, so a re-run reuses rows instead
of duplicating. Failure containment unchanged: every hook path still exits
0, and a pre-077 DB degrades to the old text messages with a printed hint.

**Sequencing caveat:** cards the hook posts before the next bot restart have
inert buttons (the cog isn't loaded yet). Stage order below puts the cog live
before the hook starts emitting cards.

## Dashboard (`routes/qa.py` + `panels/qa-tracker.js`)

New nav item under **Dev** (`SECTIONS` in `app.js`):
`{ id: "qa-tracker", label: "QA Tracker", module: "./panels/qa-tracker.js" }`
— the Dev section itself is `perms: ["admin"]`-gated, so the item needs no
flag of its own. Backend `require_perms({"admin"})`; UI copied from
`mod-tickets.js` (filter strip + status chips + `data-table`).

- **Board**: tests filterable by status; each row expands to its verdicts
  (who, verdict, note, paid, when) with a jump-link to the Discord card.
- **Moderation**: void a verdict (confirm dialog → clawback → card re-render
  via the bot? No — the route edits the DB and pokes the card through the
  existing bot/web shared-DB seam: the cog re-renders on next interaction,
  and the route also PATCHes the message via REST so the card updates
  immediately). Archive a test (buttons removed, color dimmed).
- **Config**: role picker, channel picker, reward amount, daily cap, enable
  toggle (`config-helpers.js` pickers; unenforced settings are forbidden by
  the working agreement, so every knob here is read by the cog/service).
- **Top testers** mini-table (verdict counts / coins earned) — cheap
  `econ_ledger` GROUP BY, gives the volunteer crew a visible scoreboard.

## Out of scope (explicitly)

- **How members get the QA role** — Discord's own onboarding / existing role
  flows; the tracker only *reads* the role.
- Bug-bounty bonus payouts (revisit after the crew is active).

## Stages

**Stage 0 — schema + service.** Migration 077; `qa_service.py` (settings
loader, CRUD, status math as pure functions, `record_verdict` with
pay-on-insert + cap, `void_verdict` with clawback); `qa_reward` into
`FAUCET_GROUPS`. Unit tests incl. the pay/no-pay race matrix.

**Stage 1 — bot cog.** `qa_cog.py`: dynamic buttons, modals, thread notes,
embed renderer, role gate. Extension registered in `__main__.py`. Fake-driven
tests. *Restart needed after merge; buttons must be live before stage 2.*

**Stage 2 — poster emits cards.** Hook inserts rows + posts cards; the full
dump doubles as the card backfill; reaction path retired entirely. Tests
extend `test_post_testing_docs.py`. Live-test = this stage's own queue entry
arriving as a working card.

**Stage 3 — dashboard** — ✅ shipped. `routes/qa.py` (admin-gated board with
folded verdicts + jump links, void with clawback, archive, settings PUT,
top-testers), `qa-tracker.js` panel, nav entry under **Dev**. Void/archive
re-render the Discord card best-effort through the in-process `ctx.bot`
(archive strips the buttons); a card failure never rolls back the DB. Route
tests in `tests/web/test_qa_routes.py`.

**Stage 4 — polish.** ✅ Archive sweep shipped
(2026-07-18): `qa_archive_sweep_loop` (`qa_cog.py`, registered as a startup
task) polls every 60s for tests `status='passed'` whose `verified_at` is 10+
minutes old (`qa_service.list_stale_passed`) and deletes the card from the
channel — the audit trail (verdicts, payouts) stays in the DB, only the
Discord message goes. Best-effort on the Discord side: a message someone
already deleted, or a channel the bot can no longer see, still gets marked
`archived` (nothing left to clean up); a transient Discord error leaves the
row `passed` for the next sweep to retry. Reuses the existing terminal
`archived` status (same one the dashboard's manual Archive sets) — a swept
card's jump-link in the board will 404 since the message is gone, unlike a
manually-archived card which keeps its (dimmed) message. Bounty idea still
open, revisit with real usage data.

## Addendum — `docs/TESTING_QUEUE.md` retired (2026-07-18)

Once the board became the runtime source of truth (stage 3), the queue
file itself bit-rotted: 1758 lines, almost entirely still under `##
Pending` — nobody was doing the manual Done-archiving step by hand, because
the real verified/pass/fail state already lived in `qa_tests`. The file was
deleted; the post-commit hook now sources a card straight from the
triggering commit's own message instead of diffing a queue file across
commits. See CLAUDE.md's Commits section for the `Testing:` trailer
convention. `post_commit()`/`testing_checklist()` in
`scripts/post_testing_docs.py` carry the new logic; the role checklists
(admin/moderator/user) are unaffected — still dumped via `--only`.

## Addendum — merge commits expand to the branch side (2026-07-30)

Feature work happens in dk-session worktrees, which deliberately carry no
`.env` — so the hook no-ops on every worktree commit, and the `--no-ff`
merge that `/dk-ship` lands in prod (where the hook *does* run) has no
`Testing:` section of its own. Net effect: shipped features posted no card
unless someone ran `--commit <child sha>` by hand.

`post_commit()` now expands a merge commit into the commits it merged
(`first-parent..merge`, `--no-merges`, oldest first) and posts one card per
branch commit that carries a `Testing:` section, keyed on that commit's own
sha and subject. The worktree no-op is kept on purpose: a card posted
mid-development would invite testing a feature that isn't live, and the
ship-time rebase would re-post it under new shas. Plain commits on prod
main behave exactly as before.

**Correction (2026-08-01):** the expansion alone wasn't enough — git runs
`post-commit` only for `git commit`; a clean `git merge` fires `post-merge`
instead, so ship merges still posted nothing (first observed on the
wellness ship, confirmed on the intake-stale-nudge ship). A `post-merge`
hook now invokes the same script with the merge sha (squash merges skipped
— no commit to expand), `install.sh` installs both hooks, and a conflicted
merge finished by `git commit` still lands in `post-commit` — either path
runs the same expansion, deduped by the (guild, entry_key, sha) index.

---

## Stage 5 — one card per feature, not per commit (2026-08-20)

**The problem.** Every behaviour-changing commit posted its own card. In the
30 days to 2026-08-20 that was **442 cards** (756 commits, 442 carrying a
`Testing:` section). The queue held 414 cards all-time, of which **172 were
still pending and only ~12% had ever received a verdict** — 165 of the pending
ones created on or before 2026-07-21 with no verdict at all. The system was
emitting far more than anyone could consume, and the cards it emitted were raw
commit-message text written developer-to-developer: nine-checkbox cards,
compound steps ("add a chore, press Run now, then tick it with Mark Done — the
row shows your name and the time" is three actions and an assertion in one
box), references to internals a tester cannot see (`guess_post` quest, "the two
open spoiled rounds (#384, #388)"), and steps untestable in a sitting ("leave a
chore untouched overnight").

**The key is the branch, and the trigger is teardown.** Keying on the merge
does not work: 184 first-parent merges in 30 days came from only **136 distinct
branches**, because a branch ships repeatedly while work continues on it —
`survivor-review` merged ten times, `backup-disaster-recovery-review` five. The
feature is the branch, and the branch is *finished* at `/dk-ship` teardown, so
that is where its card is written:

- `post_commit()` now **returns without posting for a merge commit**. Plain
  commits landing straight on main are unchanged — they are their own feature
  and post their own card, keyed on the subject as before (~30 of these a
  month).
- `cmd_teardown` calls `post_testing_docs.py --branch <name>`, which finds every
  `Merge branch '<name>'` on main's first-parent history (`branch_merges`, five
  subject shapes: `'x'`, `"x"`, bare, `… into main`, `Merge x: description`),
  expands each through the existing `merged_commits()`, and collects every
  `Testing:` section it landed — deduped, since a rebase replay lands identical
  work under a new sha.
- The card is keyed `entry_key = <branch>` with `commit_sha` = the latest thing
  the branch shipped, so a teardown re-run reuses the row, and a card that has
  already been posted (`message_id` set) is left alone.

**No migration.** The obvious approach — put the merge sha in `commit_sha` —
would defeat in-place identity, and `NULL` is worse: SQLite treats NULLs as
distinct in a unique index, so `ON CONFLICT` never fires on the checklist-doc
path either (a latent duplicate-row bug, untouched here). The branch path does
an explicit lookup instead and leaves the `(guild_id, entry_key, commit_sha)`
index doing exactly what it did.

**The rewrite.** One Claude call (`claude-opus-5`, `effort: low`) turns N
commits' checklists into one card: one action and one observable result per
item, at most 8 items, naming what the tester clicks rather than what the code
calls it, nothing that needs an overnight wait. It is **best-effort by
contract** — no `ANTHROPIC_API_KEY`, a network failure, a non-JSON reply or an
empty result all fall back to the raw concatenated checklists under the
humanised branch name. The call is plain `urllib`: the script runs under bare
system python3 (the git hook has no venv), so the `anthropic` SDK is not
available to it, and it must not use the module's own `request()` helper, which
raises `SystemExit` on failure.

A first pass capped the card at 6 items and told the model to *combine* what
did not fit. It complied by writing seven assertions into one box — recreating
the exact density the stage set out to remove. The cap is now 8 and the
instruction is inverted: never combine unrelated actions; if the notes exceed
the cap, keep the checks most likely to catch a regression a member would
notice and leave the rest out. A card nobody finishes is worse than a short one.

**Ordering.** The card posts *after* teardown kills the tmux window, so no
user-visible step waits on an API round trip; teardown is already detached, so
the call outlives the pane. Failures are contained twice (the poster swallows
its own, `post_qa_card` guards the subprocess) — a card is never the reason a
session fails to tear down.

**Volume.** 442 cards/30d → ~166 (136 branches + ~30 direct commits) from the
re-keying alone. Below that is policy, not machinery: CLAUDE.md now says only
**user-facing** changes earn a `Testing:` section, and documents how to word the
lines. The backlog was cleared with `scripts/archive_stale_qa_cards.py`, which
archives the pending, verdict-free cards created on or before the cutoff and
edits their Discord messages so the buttons die.

**Known gaps.** A session shipped with `--keep` never tears down and so never
posts; run `--branch` by hand. A branch bundling unrelated work (`todo-triage`)
produces one card covering all of it, which is inherent to keying on the branch.
Branch names like `worktree-agent-acee91a4…` make poor card titles, but the
rewrite supplies its own title, so only the fallback path shows the slug.
