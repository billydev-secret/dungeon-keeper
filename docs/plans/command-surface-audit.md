# Command surface audit — 2026-07-28

Audit of every slash command in `src/bot_modules` against CLAUDE.md's rule that
configuration and reporting live on the web dashboard, and Discord is for member
self-service and in-the-moment mod actions.

**Method.** All command declarations were extracted by AST walk (not regex), then
each deletion candidate was traced to a concrete dashboard route *and* panel
before being recommended. Two candidates failed that trace and were kept — see
"Kept after verification".

**Count:** 190 commands before, **165** after — 9 dashboard duplicates, 4 more
when the `/dm_*` set folded into a panel (`f24bcf87`), 6 in the Gap 3 redundancy
pass (`912f4613`), and 6 panel-posters replaced by one dashboard route.

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

## Done — 25 commands removed

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

## Gap 1 — admin config with no dashboard route (was 15; 12 done, 2 staying, 1 open)

These were admin config by CLAUDE.md's definition, and every one failed its
parity check: the dashboard had no route to do the same thing. **Do not delete
before building the route** — each is otherwise the only way to perform its
action.

The six that were the same shape — *post a sticky panel into a channel* — are
**done**: they collapsed into one route and one page, ~~struck through~~ below.
`/quote-role` and `/spotify_authorize` are done too, by different means. Seven
remain.

| Command | Gap |
|---|---|
| ~~`/bank post-guide`~~ | **Done** — Config → Channel Panels |
| ~~`/bank post-leaderboard`~~ | **Done** — Config → Channel Panels |
| ~~`/bank post-shop`~~ | **Done** — Config → Channel Panels |
| ~~`/voice-admin post-panel`~~ | **Done** — Config → Channel Panels. (Not the same as `POST /voice-master/post-howto`, which posts the member how-to embed) |
| ~~`/ticket panel`~~ | **Done** — Config → Channel Panels |
| ~~`/guess prompt`~~ | **Done** — Config → Channel Panels |
| ~~`/quote-role`~~ | **Done** — removed; the trigger matches the role by name, so an admin creates it by hand |
| `/risky reset_state` | **Staying in Discord.** Clears stuck in-memory state in the channel you're standing in — in-the-moment recovery, admin-gated for safety rather than because it's configuration |
| `/games config game-status` | **Staying in Discord.** "What game is running in *this* channel" — building a dashboard view to answer a question about the channel you're already looking at would be worse than the command |
| ~~`/games config game-end`~~ | **Done** — absorbed into `/games end force:true` in the Gap 3 pass (`912f4613`) |
| `/inactive panel` | **Load-bearing.** `config-inactive.js:68` and `:265` tell admins to "Run `/inactive panel` in Discord first" — the dashboard depends on it |
| `/inactive sweep` | Half-covered: the dry-run preview exists (`config.py:2187`) and `auto_sweep` runs 6-hourly, but there is no manual "run now" on the web |
| ~~`/setup`~~ | **Done — deleted.** All six settings it walked through (`mod_role_ids`, `admin_role_ids`, `jail_category_id`, `ticket_category_id`, `log_channel_id`, `transcript_channel_id`) were already on Config → Moderation, and it created nothing in the server: `@Jailed` is created lazily on the first jail (`jail/apply.py:194`), not by the wizard. So it configured nothing exclusive — the "only path for an admin who hasn't found the dashboard" argument doesn't survive the check, since they'd have to find `/setup` too. `setup_cog.py`, the wizard views, its step table, and its two embeds all went with it |
| ~~`/grant_audit`~~ | **Done** — Config → Channel Panels. I first filed this as a half-covered *report*; it isn't. Its own docstring described the economy-leaderboard pattern (post, edit in place, move, kept fresh by an hourly loop), so it was a panel-poster with two options all along. Those options are why `PanelSpec` grew a declared `options` schema — the registry now describes what a panel needs, instead of a panel with settings growing its own endpoint |

### How the six moved — the pattern for anything similar

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
has. Rather than six near-identical routes, each cog now exposes one uniform
`async post_*(guild, channel)` method and
`bot_modules/services/panel_registry.py` maps a key to it. The route
(`POST /api/panels/{key}/post`) owns only the plumbing the six commands each
repeated: resolve guild, resolve channel, check the bot can post there, call the
cog, report what happened. `GET /api/panels` feeds the **Config → Channel
Panels** page.

Three things worth carrying to any similar job:

- **Two panels own their destination.** Voice Control posts into its configured
  control channel and Guess Who into the configured Guess channel, because their
  buttons drive a flow the cog only looks for in that one place. Honouring a
  picked channel would strand the buttons somewhere nothing reads. The route
  ignores `channel_id` for these and the API flags them so the UI hides the
  picker.
- **These do *not* get the boot-time autopost the DM panel needed.** That one
  became the only route to a member's DM settings; these six all sit alongside
  commands that still work (`/ticket open`, `/guess submit`, `/voice …`,
  `/bank wallet`), so a guild that never posts one loses discoverability, not
  capability. Posting into someone's channel unasked is the bigger imposition.
  Worth stating because "the DM panel does it" is the obvious wrong inference.
- **The registry resolves by name at request time**, so nothing at import time
  catches a rename — a stale entry would first appear as a 503 when an admin
  presses Post. `tests/test_panel_registry.py` is the compile-time check the
  dynamic lookup doesn't get.

## Gap 2 — features with no dashboard surface at all — DONE 2026-07-28

Not just missing a route — missing a panel. All four resolved, and the useful
part is that only *one* of them wanted a panel.

Three ways this list resolves, and only one of them is "build a panel":

- **Delete it** when the feature isn't earning its place. Quality leave had zero
  prod rows; voice 24/7 had one live channel and was dropped anyway, knowingly.
  For an unused feature the honest answer is removal, not a dashboard.
- **Keep it in Discord** when it is genuinely in-the-moment mod work. Hiding a
  channel happens while you are looking at the channel.
- **Build the panel** when it is real configuration someone has to sit down and
  do — external game tracking, where you pick a channel and a bot to watch.

| Feature | Commands | Notes |
|---|---|---|
| ~~Quality leave~~ | ~~`/quality_leave add\|remove\|list`~~ | **Done — feature deleted, not migrated.** No panel was built: prod had zero rows in `quality_score_leaves` and the dashboard never surfaced the status, so the whole leave-of-absence concept went with the commands (CRUD, `STATUS_LEAVE`, the scoring exemption). Deleting only the commands would have left a read path with no writer and any future row unremovable. The empty table stays — it was created lazily, has no migration, and dropping it for zero rows isn't worth a destructive migration. This also finally removed `reports_cog.py`, the misleadingly-named file that started this audit |
| ~~External game tracking~~ | ~~`/games track watch\|status\|disable\|enable\|sample`~~ | **Done — panel built.** Games → External Tracking (`routes/games_external.py`, `games-external.js`). The only one of the four that was real sit-down configuration, so the only one that earned a panel. Two things the panel does better than the commands: `disable`/`enable` needed a `bot` argument and refused when several were tracked, which a toggle-per-row makes impossible; and it needed a new `/api/meta/bots` endpoint because `/meta/members` deliberately filters bots *out* |
| Hidden channels | `/hidden hide\|restore\|list` | **Staying in Discord** (owner's call, 2026-07-28) — hiding a channel is an in-the-moment mod action taken while looking at the channel, not configuration. The open S3 finding stands on its own (`docs/reviews/2026-07-22-deep-review.md`): the hide/restore state machine is inline in the cog with zero coverage, and is worth extracting to `hidden_channels/logic.py` regardless of where the surface lives |
| ~~Voice 24/7~~ | ~~`/247`, `/247_status`~~ | **Done — feature deleted.** Unlike quality leave this one was *live*: one prod channel was pinned always-on with a Spotify autoplay playlist. Removed knowingly. Autoplay went with it, never having been independent — it only ran for an always-on channel. Also gone: the startup rejoin, the idle-disconnect exemption, `music_settings.py`, and the 24/7 embed/format helpers. The empty `music_channel_settings` table stays |

`/watch add|remove|list` (personal mod watchlist) also has no panel, but it is
arguably self-service for mods rather than config.

---

## Gap 3 — redundant commands — DONE 2026-07-28

A different axis from the rest of this document. Everything above is about
commands being in the *wrong place*; these are commands that shouldn't exist as
separate commands at all. None of them needs a panel built first, which makes
this the cheapest list here. Verified by reading the implementations 2026-07-28,
and worked in `912f4613` — six commands removed, 177 → 171.

**Verified duplicates** — both removed.

| Commands | Finding |
|---|---|
| `/support` · `/games support` | Same description verbatim, both post the support-server link. `support_cog.py:26` sends plain text, `games_help_cog.py:26` sends an embed. Drop one |
| `/games end` · `/games config game-end` | Both call `_teardown_active_game` on the channel's active game and send `build_force_end_embed`. `/games end` additionally allows the host and asks for confirmation; the config one is mod-only and skips it. That is an *option* on one command, not two commands |

**"Collapse controls" violations** — CLAUDE.md: *"One dial with a few states
beats several overlapping toggles."* In each pair the two callbacks are
byte-identical except for one boolean handed to a shared helper. Both collapsed
into an option on the surviving command.

| Commands | Differ only by |
|---|---|
| `/risky start` · `/risky start_no_ping` | `ping=True/False` + `skip_min_game_time` into `_start_game`; identical signatures |
| `/away on` · `/away off` | A two-state toggle split across two commands |

**`/ffa_banner` was on this list and should not have been.** The callbacks *are*
byte-identical but for `banner`, which is what put it here — and that reading
missed everything else that references the name. It is a first-class game type:
own emoji, display name "Truth or Dare Card", its own `launch_banner` in the
scheduler registry (`bot.game_launchers`), a place in the schedulable list, and
its own prompt-tag mapping. Every other game type has a matching
`/games play <type>` command, so collapsing this one into an option would break
that pattern for no gain. Left alone — two collapses, not three.

That makes three near-misses in this audit of the same shape (`/policy list`,
the corrected-rule regression, this): reading the implementation is not the same
as checking what *else* points at it.

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
