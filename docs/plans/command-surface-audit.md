# Command surface audit — 2026-07-28

Audit of every slash command in `src/bot_modules` against CLAUDE.md's rule that
configuration and reporting live on the web dashboard, and Discord is for member
self-service and in-the-moment mod actions.

**Method.** All command declarations were extracted by AST walk (not regex), then
each deletion candidate was traced to a concrete dashboard route *and* panel
before being recommended. Two candidates failed that trace and were kept — see
"Kept after verification".

**Count:** 190 commands before, **177** after — 9 removed as dashboard
duplicates, then 4 more when the `/dm_*` set folded into a panel (`f24bcf87`).

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

## Done — 13 commands removed

### Round 1 — dashboard duplicates (9)

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

### Round 2 — the `/dm_*` sprawl (4)

`/dm_help`, `/dm_set_mode`, `/dm_status`, `/dm_revoke` → one ephemeral
`DmSettingsView` behind a **My DM Settings** button on the existing request panel
(`f24bcf87`). Four top-level commands for one feature is the case CLAUDE.md's
"prefer one ephemeral panel over a sprawl of subcommands" rule names directly.

This is the reference implementation for the Gap 1 work below — route posts the
panel, cog auto-posts it on boot, no command. Two things it taught:

- **A panel that becomes the only route to something needs the boot-time
  autopost.** Otherwise a guild whose admin never pressed "post panel" has no
  surface at all. `place_or_refresh` edits in place, so restarts refresh rather
  than stack duplicates.
- **Deleting a command means auditing every surface that names it.** Five still
  advertised the old commands after the fold — the panel embed, its footer, the
  acceptance DM, the request DM, and `/help` — four of them member-facing, and
  the request DM is read outside the server where there is no panel in sight.
  Caught by review, in `4d2e208f`.

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

**Recommended next step — and yes, all of them can move.** Checked 2026-07-28.

Correcting an earlier overstatement in this document: **six** of the commands
above post a panel, not eight. `/quote-role` creates a mentionable role,
`/risky reset_state` clears in-channel state, and `/games config
game-status|game-end` inspect or force-close a game — those are in-the-moment
actions that happen to be admin-gated, not panel-posters, and each needs its own
decision.

Two proven web→bot bridges already exist and either works here:

- **Call a cog method** — `bot.get_cog("DmPermsCog").post_panel(...)`
  (`config.py:3779`). Needs the cog to expose one.
- **Call a module-level function** — `post_or_update_booster_panel(db_path,
  guild, channel)` (`config.py:2884`). Cleaner; no cog lookup.

Both reach Discord through `ctx.bot`, and both already do the channel-exists /
is-a-text-channel / bot-can-post-here checks a route needs. Per command:

| Command | Where the posting lives | Effort |
|---|---|---|
| `/bank post-guide` | `EconomyCog.guide_panel` — a `StickyPanel` (`economy_cog.py:1618`) | Trivial |
| `/bank post-leaderboard` | `EconomyCog.leaderboard_panel` (`:1624`) | Trivial |
| `/bank post-shop` | `EconomyCog.shop_panel` (`:1632`) | Trivial |
| `/voice-admin post-panel` | `VoiceMasterCog.panel` — a `StickyPanel` (`voice_master_cog.py:149`) | Trivial |
| `/guess prompt` | `_repost_prompt(bot, channel, guild_id)` — already module-level (`guess_cog.py:1323`) | Trivial |
| `/ticket panel` | Inline in the command: build embed → `channel.send` → record the id | Small extraction first |

Four of the six are `StickyPanel` instances hanging off a cog, and
`place_or_refresh` takes exactly `(guild, channel)` — everything a route already
has. That makes a **single generic route** viable rather than six near-identical
ones: `POST /api/panels/{panel_key}/post {channel_id}`, backed by a registry
mapping `panel_key` → `(cog name, attribute)`. `/guess prompt` needs a one-line
adapter; `/ticket panel` needs its send lifted out of the command body first.

The DM panel (`f24bcf87`) is the worked example of the whole shape: route posts
it, cog auto-posts it on boot, no command. Note the lesson from that one — if a
panel becomes the only route to something, it needs the boot-time autopost too,
or a guild whose admin never pressed the button has no surface at all.

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

## Gap 3 — redundant commands (~10, no dashboard work needed)

A different axis from the rest of this document. Everything above is about
commands being in the *wrong place*; these are commands that shouldn't exist as
separate commands at all. None of them needs a panel built first, which makes
this the cheapest list here. Verified by reading the implementations 2026-07-28.

**Verified duplicates**

| Commands | Finding |
|---|---|
| `/support` · `/games support` | Same description verbatim, both post the support-server link. `support_cog.py:26` sends plain text, `games_help_cog.py:26` sends an embed. Drop one |
| `/games end` · `/games config game-end` | Both call `_teardown_active_game` on the channel's active game and send `build_force_end_embed`. `/games end` additionally allows the host and asks for confirmation; the config one is mod-only and skips it. That is an *option* on one command, not two commands |

**"Collapse controls" violations** — CLAUDE.md: *"One dial with a few states
beats several overlapping toggles."* In each pair the two callbacks are
byte-identical except for one boolean handed to a shared helper:

| Commands | Differ only by |
|---|---|
| `/risky start` · `/risky start_no_ping` | `ping=True/False` + `skip_min_game_time` into `_start_game`; identical signatures |
| `/ffa` · `/ffa_banner` | `banner=False/True` into `start_ffa`; identical signatures (`kind`, `tags`, `prompt`) |
| `/away on` · `/away off` | A two-state toggle split across two commands |

Three commands recoverable from six, no capability lost. Note `/risky start` had
7 uses in the usage window — the pair is live, so a resignature is visible to
members.

**Sprawl worth collapsing into a panel**

- `/dm_help`, `/dm_set_mode`, `/dm_status`, `/dm_revoke` — **done** in `f24bcf87`;
  see that commit for the shape.
- `/trusted add|remove|list` + `/blocked add|remove|list` — six commands for two
  lists, alongside Voice Control's existing panel.

**Judgment calls, not recommendations**

- **Dev tooling is live in production.** `/fill` adds fake players to a lobby and
  `/answer` submits fake answers for them, both gated only on `manage_guild` — so
  any admin can inject fake players into a real game. `/reload_cog` and
  `/spotify_authorize` are also registered.
- **`/invite`** posts a bot-invite link with a full permission set. Correct for a
  public bot; dead weight for a private community one. Depends on intent for the
  bot, which this audit doesn't presume.

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
