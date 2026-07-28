# Command surface audit — 2026-07-28

Audit of every slash command in `src/bot_modules` against CLAUDE.md's rule that
configuration and reporting live on the web dashboard, and Discord is for member
self-service and in-the-moment mod actions.

**Method.** All command declarations were extracted by AST walk (not regex), then
each deletion candidate was traced to a concrete dashboard route *and* panel
before being recommended. Two candidates failed that trace and were kept — see
"Kept after verification".

**Count:** 190 commands before, 181 after.

**Naming caveat.** Several groups are attached to a parent at *runtime*
(`games.add_command(...)` in each cog's `setup()`, over the shared groups in
`bot_modules/games/command_groups.py`), which a static AST walk cannot see. Every
game command actually lives under `/games` or `/games play` — `/wyr` is
`/games play wyr`, `/track watch` is `/games track watch`. The counts here are
right; flat-looking paths for game commands are not.

Classification used throughout: **member self-service** (keep) / **mod action in
the moment** (keep) / **admin config** (delete, panel should exist) /
**report-or-analytics** (delete, the website has it).

---

## Done — 9 commands removed

Each verified against a live route + panel first.

| Command | Replaced by |
|---|---|
| `/rules-watch digest` | Rules Watch panel, **Digest** tier filter (unlabeled-only by default) |
| `/rules-watch label` | Confirmed / False-positive buttons → `POST /api/rules-watch/events/{id}/label` |
| `/rules-watch stats` | Rules Watch panel, **Stats** tab → `GET /api/rules-watch/stats` |
| `/rules-watch status` | Config → Rules Watch |
| `/warnings` | Moderation → Warnings → `GET /api/moderation/warnings` (returns revoked rows too) |
| `/docs post` | `POST /api/docs/{doc_key}/placements` |
| `/docs sync` | `POST /api/docs/{doc_key}/sync` |
| `/docs unpost` | `DELETE /api/docs/{doc_key}/placements/{channel_id}` |
| `/docs list` | `GET /api/docs` |

`docs_cog.py` was deleted whole. `rules_watch_cog.py` was **not** — it still owns
the "Report Rule Violation" message context menu.

### Answering the question that started this

There are no report commands left in Discord. `reports_cog.py`'s own docstring
records that member activity/role/engagement reports moved to
`src/web_server/routes/reports.py`; the only thing still in that file is
`/quality_leave`, which is leave-of-absence roster management under a misleading
filename. Renaming that cog is a loose end.

---

## Kept after verification

- **`/policy list`** — reads the `policies` table (adopted policies, ordered by
  `passed_at`). The dashboard's Policy Tickets panel reads `policy_tickets` (the
  open/voting/closed proposal workflow). Different tables, and **no web route
  reads `policies`**. This was on the delete list until the trace; removing it
  would have left no way to see what actually passed.
- **`Report Rule Violation`** context menu — creating an event about a message
  you are looking at is mod work, not configuration.
- **`/modinfo`** — in-channel mod lookup; kept by owner's call.
- **Member-facing "reports"** — `/xp_leaderboards`, `/guess leaderboard`,
  `/bump status` look like analytics but serve members, and the dashboard's
  Reports section is gated `perms: ["moderator"]` (`app.js`). Members cannot
  reach the web equivalent, so "the website has it" does not hold for these.

---

## Gap 1 — admin config with no dashboard route (15 commands)

These are admin config by CLAUDE.md's definition, but every one failed its parity
check: the dashboard has no route to do the same thing. **Do not delete before
building the route** — each is currently the only way to perform its action.

Almost all of them are the same shape: *post a sticky panel into a channel*.

| Command | Gap |
|---|---|
| `/bank post-guide` | No route. Cog calls `place_or_refresh` (`economy_cog.py:4243`) |
| `/bank post-leaderboard` | No route (`economy_cog.py:4389`) |
| `/bank post-shop` | No route (`economy_cog.py:4437`) |
| `/voice-admin post-panel` | `POST /voice-master/post-howto` exists but posts the *member how-to embed*, a different thing from the persistent owner-control panel |
| `/ticket panel` | No route |
| `/guess prompt` | No route |
| `/quote-role` | No route |
| `/risky reset_state` | No route |
| `/games config game-status` | No route |
| `/games config game-end` | No route |
| `/inactive panel` | **Load-bearing.** `config-inactive.js:68` and `:265` tell admins to "Run `/inactive panel` in Discord first" — the dashboard depends on it |
| `/inactive sweep` | Half-covered: the dry-run preview exists (`config.py:2187`) and `auto_sweep` runs 6-hourly, but there is no manual "run now" on the web |
| `/setup` | A DM/button config wizard — a textbook violation, but also the only in-Discord path for a fresh server admin who hasn't found the dashboard. Needs a decision, not a reflex |
| `/grant_audit` | Half-covered: `GET /api/reports/grant-audit` serves the panel, but nothing posts the auto-updating Discord card |

**Recommended next step.** One reusable dashboard control — "post this panel to
`#channel`" — collapses eight of these at once. The pattern already exists:
`config.py:2884` (booster roles) and `config.py:3779` (DM perms) both post a
panel by calling into the cog, and `core/sticky.py:351` `place_or_refresh` is the
shared primitive. This is the single highest-value item in this document: the
command surface is not cluttered with reports, it is cluttered with panel-posters.

---

## Gap 2 — features with no dashboard surface at all (13 commands)

Not just missing a route — missing a panel. Reported per the owner's decision to
leave them in place for now.

| Feature | Commands | Notes |
|---|---|---|
| Quality leave | `/quality_leave add\|remove\|list` | `add_leave`/`remove_leave` are called **only** from `reports_cog.py`. The dashboard *reads* leaves (`member_quality_score.py:268`, feeding Quality Score) but has no write path |
| External game tracking | `/games track watch\|status\|disable\|enable\|sample` | `economy-income-sources.js:23` only sets the *payout amount*; nothing configures which channel/bot to watch |
| Hidden channels | `/hidden hide\|restore\|list` | No panel, no route. Also carries an open S3 finding (`docs/reviews/2026-07-22-deep-review.md`): the hide/restore state machine is inline in the cog with zero coverage — a web migration would fix both at once |
| Voice 24/7 | `/247`, `/247_status` | No panel, no route |

`/watch add|remove|list` (personal mod watchlist) also has no panel, but it is
arguably self-service for mods rather than config.

---

## Usage data — and why it can't drive deletions

There is **no command-usage telemetry**. Invocations are only written to the log
(`events_cog.py:1792` → `Command /<name> by <user> …`); nothing lands in a table.
`log.txt` is wiped on every boot (`__main__.py:86`) and rotates at 2 MB, so the
only usable history is journald: `journalctl -u dungeon-keeper`.

Window available at the time of the audit: **2026-07-25 15:33 → 07-28 07:26
(2.7 days)**, containing **144 invocations across 27 distinct commands** — out of
181. Top of the distribution:

| Count | Command |
|---|---|
| 43 | `/bank wallet` |
| 21 | `/bank quests` |
| 10 | `/ask` |
| 9 | `/modinfo` |
| 8 | `/bank shop` |
| 8 | `/purge` |
| 7 | `/risky start` |
| 6 | `/guess submit` |
| 5 | `/bank pay` |

The long tail is 1–4 uses each: `/birthday set`, `/penpals status`, `/grant`,
`/bump status`, `/bio`, `/play`, `/steal_emoji`, `/games track watch`,
`/games hotpotato challenge`, `/bank post-leaderboard`, `/guess prompt`, plus the
context menus (`Quote`, `Jail User`, `Steal Emoji`).

**This is not a deletion signal, and must not be used as one.** Three reasons:

1. **The window is 2.7 days.** Command frequency is not uniform — `/setup` runs
   once per server *ever*, `/jail` only when someone misbehaves, `/quality_leave`
   only when a member goes on leave. Absence over three days is not evidence of
   disuse.
2. **The sample is 144 events.** Anything used monthly is statistically invisible.
3. **journald retention is size-based** (defaults, 141.7 MB in use), not
   time-based, so the window silently shrinks as log volume grows.

What it *is* good for: it corroborates that the nine commands deleted in this
round (`/rules-watch …`, `/warnings`, `/docs …`) saw **zero** use in the window,
which is consistent with them being duplicates of dashboard pages people already
use. That's supporting evidence, not the reason — the reason was verified route +
panel parity.

If usage should actually inform future rounds, the fix is a counter table
(command name, guild, timestamp) written from the existing `on_interaction`
hook — a small change that would make the next audit evidence-driven instead of
inference-driven.

## Regression from round 1 — found, fixed

Deleting `/rules-watch label` lost the ability to record a **corrected rule
number**. The parity check confirmed the panel labels events; it did not check
that it labels them *with the same fields*. The endpoint and `service.upsert_label`
had always accepted `corrected_rule` — `rules-watch.js` simply never sent one.

Fixed by adding a **Correct rule** input beside the label buttons, sent only with
a confirmation (a dismissal has no correct rule). `tests/test_rules_watch_labels.py`
locks the persistence contract, because nothing reads the column back into the UI
where a future break would be visible.

Worth keeping in view as a method note: this is the same failure mode as the
`/policy list` near-miss — *"a panel for this feature exists"* is not the same
check as *"this panel does this thing, with this data."* Two of the three
parity misses in this audit were that exact substitution, and the second one
shipped. A parity claim should name the field, not the feature.

## Coordination note

The sibling `reports-panel-review` session ran a four-commit reports cleanup on
2026-07-27 (`5b4cd71d`, `85b1c40d`, `215084af`, `53ff1be5`). Cross-checked: it
removed only experimental metric panels, and `85b1c40d` *created* the
`inactive-report` panel this audit leans on. No "the website has it" argument
here was invalidated. Worth re-checking if that session deletes further panels —
specifically `quality-score` or `grant-audit`.
