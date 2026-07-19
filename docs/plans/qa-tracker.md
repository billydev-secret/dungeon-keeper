# Implementation plan — QA Tracker (volunteer testing crew + currency rewards)

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
worktree, `scripts/gate.py` green, `docs/INDEX.md` + `docs/TESTING_QUEUE.md`
updated in the same commit, merged to main for live testing before the next
stage starts. Stages 1–2 only go live after a bot restart (user pushes that
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
