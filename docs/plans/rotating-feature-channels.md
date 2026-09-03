# Rotating feature channels — design (exploration)

Status: **Stage 1 built 2026-08-29** (rotation, announcement, quest coupling,
dashboard panel); **Part 4 built 2026-08-30** (a room's game ends and starts
with the flip; scheduled games skip a hidden room). Stage 3 (pausing new
submissions while hidden) not started. Billy's ask: rotate confessions /
whisper / guess-who, announce the day's feature in main chat.

## Decisions already taken

| Question | Answer |
|---|---|
| Featured vs hidden | **Hidden, one at a time** — only the featured channel is visible |
| Cadence | **Daily** (not weekly) |
| Announcement destination | **Dashboard dial**, default 💛│the-meadow |
| What "hidden" means | **Out of sight, still running** — as a per-channel checkmark table |
| Flip timing / order | Fixed cycle; **flip locked to midnight**, **announce hour configurable** (default 09:00) — see Part 2 |
| Featured quest slot | **Reserve a slot**; setup pins capped at `n-1` |
| Pool membership | **Any channel is selectable** — no whitelist. Seeded with the five below |

## Measured starting state (TGM, 30 days to 2026-08-29)

| channel | messages | authors | auto-delete |
|---|---|---|---|
| 🤫│whisper `1503124772425437184` | 1,056 | 35 | none |
| 🤷│guess-who `1502760619269427292` | 375 | 26 | 30 days, swept daily |
| 🤐│confessions `1469771843320811602` | 96 | 1 (bot; anonymous by design) | 30 days, swept daily |

Announcement target 💛│the-meadow `1469491363287531553`: 32,663 msgs / 133 authors
(~1,090 a day).

All three are alive. Rotation here manufactures scarcity on working surfaces; it
does not revive neglect. Cost accepted knowingly.

## Mechanism

### Visibility: flip in place, do not move categories

`bot_modules/hidden_channels/` already snapshots overwrites + placement and
restores them verbatim, but `hidden_channels_cog.hide` **moves the channel into a
"Hidden Channels" category** and restores position with a second `channel.edit`.
That is right for an indefinite hide and wrong for a daily one: it reshuffles
everyone's channel list twice a day and doubles the audit-log entries.

Reuse the **pure, unit-tested `hidden_channels/overwrites.py`**
(`serialize_overwrites` / `rebuild_overwrites`) and skip the cog's category path:

* **hide** — serialize current overwrites into `stored_overwrites`, then one
  `channel.edit(overwrites=…)` denying `view_channel` to `@everyone`. Channel
  stays in its home category at its position.
* **show** — `rebuild_overwrites(stored)` and apply; clear the snapshot.

One API edit per channel per transition. Position never moves.

**Mod visibility:** the flip only denies `@everyone`. A role holding an explicit
`view_channel=True` still sees the room while hidden — so mods keep eyes on the
hidden channels. Proposed default; flag if not wanted.

### Scheduling: exactly-once per day

Loop `feature_rotation_loop`, registered beside `scheduled_games_loop` at
`src/dungeonkeeper/__main__.py:370`. Two separate daily actions: the **flip** at
local midnight (locked to the quest board's day boundary — see Part 2) and the
**announcement** at `announce_hour`.

`last_flip_date` stores `YYYY-MM-DD` in the configured tz. A pass claims the day
atomically —
`UPDATE … SET last_flip_date=? WHERE guild_id=? AND last_flip_date<?` — and only
acts on `rowcount == 1`. Same shape as `announcements_service.claim_scheduled`
and `survivor/tasks.py`'s per-week guard; a restart mid-flip cannot double-post.

Timezone follows the announcements convention: integer offset, **no DST**.

### Announcement: the rotation loop posts it directly

Timed Announcements **cannot express recurrence**. `announcements` carries a
single `post_at` and a one-shot `draft → scheduled → sent` claim
(`announcements_service.py:47,258`). Driving a daily rotation through it would
mean inserting a fresh row every day forever. The rotation loop posts its own
embed instead — colour from `safe_resolve_accent(bot, guild)`, per `core/branding`.

## Schema (migration 192 — re-check against main immediately before committing)

```sql
CREATE TABLE feature_rotation_config (
    guild_id            INTEGER PRIMARY KEY,
    enabled             INTEGER NOT NULL DEFAULT 0,
    announce_hour       INTEGER NOT NULL DEFAULT 9,
    rooms_per_day       INTEGER NOT NULL DEFAULT 1,
    announce_channel_id INTEGER NOT NULL DEFAULT 0,
    current_position    INTEGER NOT NULL DEFAULT 0,
    last_flip_date      TEXT    NOT NULL DEFAULT '',
    last_announce_id    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE feature_rotation_pool (
    guild_id          INTEGER NOT NULL,
    channel_id        INTEGER NOT NULL,
    position          INTEGER NOT NULL DEFAULT 0,
    label             TEXT    NOT NULL DEFAULT '',
    blurb             TEXT    NOT NULL DEFAULT '',
    in_rotation       INTEGER NOT NULL DEFAULT 1,
    hide_when_off     INTEGER NOT NULL DEFAULT 1,
    pause_when_off    INTEGER NOT NULL DEFAULT 0,
    announce          INTEGER NOT NULL DEFAULT 1,
    stored_overwrites TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (guild_id, channel_id)
);
```

**No per-user data** — neither table names a member, so no `data_register.md` row
and no `SUBJECT_ID_COLUMNS` addition is required. (Confirm at review.)

## Dashboard panel — the checkmark table

Route id `feature-rotation` (bare feature name, per CLAUDE.md), filed beside the
channel tooling in `docs/dashboard_ia.md`. Admin config lives here, not in Discord.

```
Daily feature rotation                        [ On ]

Announce at [ 09:00 ] (server time, UTC−7)   in [ 💛│the-meadow ▾ ]
Rooms flip at midnight, with the quest board.

  #  Channel          In rot.  Hide when off  Pause new  Announce
  1  🤫│whisper          ☑          ☑             ☐          ☑
  2  🤷│guess-who        ☑          ☑             ☐          ☑
  3  🤐│confessions      ☑          ☑             ☐          ☑
                                      [ + add channel ]

  Today: 🤫│whisper   ·   Tomorrow: 🤷│guess-who
```

`Hide when off` on + `Pause new` off == "out of sight, still running", the agreed
default. Each row can differ.

## Risks — resolved and open

**Resolved**

* *No-contact.* Hiding is guild-wide and identical for every member, so it
  discloses nothing about any pairing; every refusal path is unchanged and stays
  indistinguishable from an ordinary outcome. No interaction with
  `docs/no_contact_spec.md`.
* *Stranded in-flight state.* With "still running", nothing is stranded. The 26
  currently-open guess rounds (aged 2 → 106 days) keep resolving. A round posted
  while its channel is hidden simply surfaces on the next featured day — content
  accumulates and is revealed, which suits the event framing.
* *Whisper.* Play is ephemeral panels (`whisper_cog` answers `ephemeral=True`);
  `cfg.channel_id` is only a feed. Hiding the feed does not touch the 30-day
  age-lock (`whisper_service.py:14`) or stop a whisper being sent or answered.
* *Content expiring unseen.* Auto-delete is **30 days**, not 7. A room is
  featured ~10 times within its content's life.

**Open**

1. **Notification papercut.** A member @-mentioned in a hidden channel gets a
   notification that opens nothing. Options: accept it; set `pause_when_off` for
   the pinging feature; or suppress mentions while hidden. *Needs Billy's call.*
2. **Audit-log noise.** 2–4 channel edits a day, in the log that memory records
   as the server's only durable history.
3. **Concurrent overwrite edits.** An admin editing a hidden channel's
   permissions has them clobbered on restore — inherited from `hidden_channels`,
   not new.

## Staging

1. **Core** — migration, quest exclusion + featured pin (Part 2), `feature_rotation/logic.py` (pure: next position,
   `is_flip_due`, announcement copy), `services/feature_rotation_service.py`,
   the loop, in-place hide/show. Dashboard panel with the checkmark table.
2. **Announcement** — the daily embed.
3. **`pause_when_off`** — an accepting-new gate in each of the three features.
   Only if wanted; the agreed default leaves the column unticked.

## Obligations

* Logic-layer tests in the same commit: flip-due boundary at the configured hour,
  the exactly-once day claim (two passes, one flip), cycle wrap, a disabled/empty
  pool, `hide_when_off` off, and hide→show overwrite round-trip.
* `manual.html` in the same commit — this changes what members can see day to day.
* `docs/INDEX.md` classification for this doc.

---

# Part 2 — quests match the open room

Added at Billy's request: "make sure quests are assigned that match the open
room of the day", and "those things could work together".

## How the board actually works (this constrains everything)

The personal board is a **pure function of `(pool, user, period_idx, n)`** —
there is no stored assignment table (`quests.assigned_quest_ids`, docstring at
`quests.py:677`). The pool is shuffled per member and walked `n`-at-a-time by
`period_index`, which for daily is `date.toordinal(local_day)` — so it advances
at **local midnight**.

`_frozen_board_pool` (`economy_quests_service.py:864`) snapshots the live pool
and board size **the first time any member's board of that cadence is read in
the period**, into `econ_quest_pool_snapshots`, precisely so a mid-period edit
can't reshuffle everyone's board. A rotation-aware pool filter must therefore be
applied *before* that snapshot — which the daily cadence gives for free, since a
fresh snapshot is taken each day anyway.

**Prod sizing (TGM has no override, so defaults apply):** daily board = **2**
slots (`PERSONAL_BOARD_SIZE`), `MAX_SETUP_PINS = 2`.

## Which quests actually break when a room is hidden

Seven quest event keys touch the three features. "Out of sight, still running"
holds for most of them, because their entry point is a slash command or an
ephemeral panel rather than the channel:

| key | entry point | works while hidden? |
|---|---|---|
| `whisper` | ephemeral panel | ✅ |
| `whisper_guess` | ephemeral inbox | ✅ |
| `confession` | `/confess` (`confessions_cog.py:901`) | ✅ |
| `guess_post` | `/guess submit` (`guess_cog.py:1918`) | ✅ |
| `guess` | button on the round message, in channel | ❌ |
| `guess_win` | same | ❌ |
| `confession_reply` | button on the confession message, in channel | ❌ |

**4 of 7 survive hiding; 3 do not.** Only those three need excluding from the
pool on days their channel is hidden — a small perturbation, not a partition.

Caveat to record: dropping ids changes `m = len(pool)`, and the draw window is
`start = (index * n) % m`, so a daily-varying pool size slightly degrades the
"repeats spaced ~floor(m/n) periods apart" property. Three ids out of the daily
pool is a small enough perturbation to accept; it should be a comment, not a
surprise later.

## The timing collision — and its clean resolution

The board's day rolls at **local midnight** and freezes its pool there. The
agreed rotation flip hour defaults to **09:00**. Left as-is:

* the pool snapshot taken at 00:00 would reflect **yesterday's** featured room
  for the whole day, and
* between 00:00 and 09:00 the open room and the board would disagree outright.

**Resolution: split the one hour dial into two.**

* **Flip hour — locked to the quest day boundary (00:00 local, UTC−7).** Room,
  board and economy day all turn over together. Nothing to reconcile. "Local"
  is the guild's shared `tz_offset_hours` config key, the one birthdays, jail
  and reports already read — the rotation deliberately has no offset of its
  own, since two dials could be set apart and the flip would then fire hours
  away from the boundary the board froze its pool on.
* **Announce hour — configurable, default 09:00.** The announcement lands when
  main chat is awake and still says something true: "today's room is X".

This honours both earlier decisions (configurable timing; announcement seen by a
live room) and removes the mismatch entirely. The visibility change at midnight
is silent, which is fine — the announcement is what does the telling.

## Coupling design

**Exclusion.** Filter the three channel-bound quest ids out of the cadence pool
for any channel hidden that day, before the snapshot is frozen.

**Featured pin.** The day's room contributes one pinned board slot, drawn with
the *same* per-member walk the setup pins use —
`assigned_quest_ids(featured_room_quest_ids, user_id, index, 1)` — so which of
that room's quests a member gets rotates rather than repeating.

This reuses the existing pin machinery at `economy_quests_service.py:859`
wholesale. It also inherits that code's hard-won lesson: pinning shipped
unbounded and swamped every board, which is why `MAX_SETUP_PINS = 2` exists. The
featured pin must be capped at **1** and must never be exempt from that ceiling.

**The slot-budget problem (needs Billy's call).** Daily board = 2, setup pins may
already claim both. A new member with pending setups has no room for a featured
pin. Options:

1. *Reserve one slot for the featured pin*, capping setup pins at `n-1`. A
   member with pending setups gets 1 setup + 1 featured. Cost: setup quests
   surface in ~4 days instead of ~2.
2. *Featured pin fills only a leftover slot.* Costs nothing, but new members —
   the ones the rotation would most help orient — never see it.
3. *Raise the daily board to 3.* Best UX, but it is an economy change (more
   quests, more payout) and wants a look against the 2026-07-30 retune before
   anyone touches it.

**Decided: (1)** — reserve one slot for the featured pin, capping setup pins at
`n-1`. (3) stays a follow-up, only if the payout maths shows headroom against the
2026-07-30 retune.

## Extra obligations for Part 2

* Logic-layer tests: the exclusion filter (hidden + channel-bound ⇒ dropped;
  hidden + panel-driven ⇒ kept), the featured pin drawn and capped at 1, pin
  precedence against setup pins at `n = 2`, and a day with rotation disabled
  producing byte-identical boards to today's.
* A rotation-aware pool must not change `econ_quest_pool_snapshots` semantics —
  the snapshot stays the frozen truth for the period.


---

# Part 3 — the pool

## Rule: any channel is selectable

The dashboard picker offers **every** text channel; nothing is hard-blocked.
`feature_rotation_pool` is already keyed on a bare `(guild_id, channel_id)`, so
this needs no schema change — only that the panel's picker isn't filtered.

The unsuitability analysis below is **advisory copy in the panel**, not
enforcement. Billy's call, every time.

## Seeded pool (5)

| channel | id | 30d msgs / authors | quest triggers |
|---|---|---|---|
| 🤫│whisper | `1503124772425437184` | 1,054 / 35 | `whisper`, `whisper_guess` |
| 🤷│guess-who | `1502760619269427292` | 363 / 26 | `guess`, `guess_win`, `guess_post` |
| 🤐│confessions | `1469771843320811602` | 98 / bot | `confession`, `confession_reply` |
| 🎲│risky-rolls | `1471642282771087400` | 1,143 / 39 | `risky_roll` |
| 🙋‍♂️│ama | `1524091654238109747` | 331 / 9 | `ama_ask`, `ama_answer` |

## `rooms_per_day` — why the pool size needs a companion dial

With one room open at a time, pool size is a visibility divisor: 3 rooms ⇒ each
open ~10 days a month, 5 ⇒ ~6, 7 ⇒ ~4. Whisper carries 1,054 messages across 35
authors a month; at ~4 days a month that is decommissioning, not scarcity.

`rooms_per_day` (default 1) decouples the two. **A 5-room pool wants 2**, which
puts every room back to ~12 days a month. The cycle walks `rooms_per_day`
channels forward each day.

Knock-on for Part 2: the featured pin draws from the union of *today's* featured
rooms' quest ids, still capped at one pinned slot.

## Advisory notes for the panel (guidance, never a block)

Channels where hiding has a known cost, worth surfacing next to the picker:

* **🔝│bumpatorium** — bumping runs on a ~2h Disboard cadence; a hidden day
  breaks the cycle.
* **🎰│the-casino**, **💹│the-prediction-market** — live bets and settlement
  windows; hiding mid-market strands them.
* **🎵│music** — the now-playing card is edited in place and would strand.
* **🎲│cat-bot**, **🎲│co-ordle** — third-party bots spawn on their own schedule;
  hiding breaks `cat_catch`.
* **💜│big-feelings** — support surface.
* **🏈│nfl-survivor-league** — seasonal, Sept 10 deadline.
* **🔥│flash-channel** — `econ_theme_channel_id`; Flash Themes just shipped.
* **💛│the-meadow** — main chat, and the announcement destination. Putting the
  announce channel in the pool must at minimum warn loudly.
* Feeds, staff rooms, onboarding, and per-user `bio-*` / `penpals-*` / `jail-*` /
  `ticket-*` channels — structurally unsuitable.

**🫦│photo-challenge** (`1513286402920419501`, 643 / 42, silent 14 days) is
recorded as the strongest *optional* addition: the one room where rotation would
revive rather than restrict. Not seeded; one click to add.

## Guard worth having

If the announce channel is itself in the pool, the announcement would post into a
room nobody can see on the days it's hidden. Cheapest fix: on flip, never hide
the configured announce channel, and say so in the panel.


---

# Part 4 — the games end and start with the flip

Added at Billy's request: *"on the channel rotation, can the games be ended and
started when the channel is moved"*. Built 2026-08-30, migration 197.

Stage 1 shipped the rotation as a **pure visibility operation** — `hide_room` /
`show_room` deny and restore `view_channel` and touch no game state at all.
This part gives a pool row the option of owning a game's lifecycle too.

## What "the games" turned out to mean

There is no uniform game object across the five seeded rooms, and asking the
question room by room collapsed the work rather than expanding it:

| room | is there a game to start and end? |
|---|---|
| 🤫│whisper | no — a continuous stream of ephemeral panels, no round |
| 🤐│confessions | no — same, `/confess` submissions |
| 🤷│guess-who | rounds exist, but they belong to individual members and stay open until solved (26 open, aged 2–106 days). Force-resolving someone else's round is not a lifecycle, it's a deletion |
| 🎲│risky-rolls | **yes** — `bot.game_launchers["risky_roll"]` to start, its own `auto_close_round` to resolve. Rounds live in memory, not `games_active_games` |
| 🙋‍♂️│ama | **yes** — `bot.game_launchers["ama"]` to start, `AMAView` to close |

**Decided (Billy):** scope is AMA and risky-rolls only. A generic
`start()`/`end()` hook across five features that don't share a lifecycle was the
trap; the two that are really games already had matched machinery.

## The two halves

### 1. Rotation-driven start/end — `launch_game` on a pool row

Migration 197 adds `launch_game` (a key from `GAME_NAMES`, `''` for none) and
`launch_options` (the launcher's options dict as JSON, the same
`SCHEDULE_OPTION_SCHEMA` fields `games_scheduled.options` carries). Setting it
means both halves, deliberately symmetric: started on the room's featured day,
ended on every day it isn't. The picker offers **AMA and Risky Rolls** — the
two rooms with a real session to open and close.

The flip runs **end → hide → show → start**, inside the existing `claim_flip`
guard so a restart mid-flip can't double-fire:

* the end goes first, while the outgoing room is still visible, so its recap
  lands where members can still read it;
* the start goes last, after the show, so the game's first message posts into a
  channel that is already open.

`plan_games` keys the end on **"not featured today"**, not on "this flip hid
it". Two reasons, both load-bearing: a room with `hide_when_off` unticked never
appears in the hide plan yet still stops being the featured room, and the whole
module derives the day from an ordinal precisely so a bot that was offline for
three days returns to the right room — an end that required having *observed*
yesterday's flip would give that property up.

**Ending is not one mechanism, because the two games keep their state in
different places.** `GamePlan.end` therefore carries `(channel_id, game_key)`,
not a bare channel id, and `end_room_game` tries three things:

1. a **registered channel closer**, `bot.game_channel_closers[game_key]` — the
   counterpart to the existing `game_busy_checks`, for a game whose rounds live
   outside `games_active_games`. Risky Rolls keeps its in `rr_state.active_games`,
   so no table lookup can see them; its closer routes through `auto_close_round`,
   the round's real resolution, so a winner is picked, the no-contact gate is
   consulted, and the prompts go out. Closing early is the same event as the
   timer running out, just sooner — and the pending auto-close task is cancelled
   so it can't fire later on a resolved round.
2. a view exposing **`close_now`** for a game in that table — the game's own
   completion site. AMA grew one wrapping `_do_close`, so the recap embed posts
   and the roster is paid as if the host had pressed the button.
3. **`force_end_active_game`** otherwise — the `/games end` path, which archives
   and pays but posts no recap. The after-a-restart case, where the row outlived
   its view.

`end_game`'s DELETE is the exactly-once claim, so racing the 24-hour expiry
sweep costs at worst a missing recap, never a double payout.

Two economy details worth recording. The launch is hosted by **nobody**
(`host_id=0`, which reaches `pay_game_rewards` as `host_id=None`), so an
auto-launched game pays no host bounty — a real member there would collect one
every featured day for hosting nothing. And a launch is **refused** rather than
stomping a game already running in the room, checking both
`games_active_games` and the game's registered busy-check.

### 2. Scheduled games skip a hidden room

Measured in prod before designing this: 🎲│risky-rolls **already auto-launches
twice daily** from `games_scheduled` — id 4 at 05:13 (350-minute window) and id
6 at 12:53 (240-minute window), both pinging role `1472628220338766016`. Two
consequences killed the obvious design:

* a rotation launch at 00:00 would be a **third** daily round, still open at
  05:13, pushing schedule #4 into `skipped_active`;
* a risky-roll round never survives to midnight under those windows, so "end at
  midnight" would be a no-op for that room anyway.

**Decided (Billy):** gate the existing schedules — and, on a second pass,
**also offer Risky Rolls in the room picker**. `_process_due` now skips a slot
whose channel the rotation currently has hidden, recording `skipped_hidden` and
rolling a recurring row to its next slot.

The two mechanisms **stack rather than collide**: a room set to launch Risky
Rolls opens its own round at midnight, and schedules #4 and #6 still fire on
that room's open day (they are only silenced on its hidden days). They cannot
double-launch — the rotation refuses to launch onto a running round via the
registered busy-check, and the scheduler skips a hidden room — but three rounds
on the featured day is a real possibility, so the panel says so next to the
dial and the schedules are worth retiring by hand if one round is what's wanted.
Because the announcement is downstream of a successful launch, that one check
also suppresses the role ping — which closes **open risk #1** from Part 1 ("a
member @-mentioned in a hidden channel gets a notification that opens
nothing"), for scheduled games at least.

The gate reads the *observed* `hidden_at`, not a re-derived plan, so a guild
with the rotation off or a channel outside the pool is never gated. It fails
**open**: a wrong `False` costs one game posted into a hidden room, a wrong
`True` costs a game that silently never runs.

## Decisions taken

| Question | Answer |
|---|---|
| Which rooms | **AMA and risky-rolls only** — the other three have no lifecycle to run; both are offered in the picker |
| When | **Both at midnight**, with the flip ordered end → hide → show → start |
| Where the result is seen | **In-room only** — the recap stays where the game puts it; the morning announcement is unchanged |
| Mid-flight round when the room rotates out | **Forced resolution** through the game's own close path (recap + payout), not a void |
| Risky-rolls | **Gate its existing schedules** on hidden days, *and* offer it in the picker (revised — the first pass gated only) |
| AMA mode/format | Panel dials, defaulting to `/ama`'s own defaults (unfiltered, hot seat) |

## Not done, deliberately

* **`pause_when_off`** (stage 3) is still absent, column and all. This part
  ends and starts games; it does not gate submissions, and CLAUDE.md forbids
  shipping the toggle before the enforcement.
* **The picker is still an allow-list**, `("ama", "risky_roll")`, not every
  schedulable game — the rest have no room in the pool that owns them. The route
  drops an unsupported key *and its options*, so a stale save can't leave a dial
  looking set while doing nothing.

## Tests

Pure (`test_feature_rotation_logic.py`): start/end membership across the cycle,
a room that never hides still being ended, a room taken out of rotation still
being ended, two featured rooms both starting, `resolve_day` carrying the plan,
and the lenient launch-options round trip. Storage
(`test_feature_rotation_store.py`): the column round trip, blank-not-null for
pre-197 rows, and `is_hidden_by_rotation` including the two never-gated cases.
Service (`tests/cogs/test_feature_rotation_games.py`): the closer preference and
its fallback, a raising closer not stopping the other rooms, host id 0, the
no-stomp and busy-check refusals, the **end → hide → show → start** ordering,
the day claim covering the new work, the registered-closer path (including a
game the table cannot see, a raising closer still letting the table path run,
and one game's closer never being used for another), and the AMA `close_now`
and Risky Rolls registration wiring assertions. Risky Rolls' own closer
(`tests/cogs/test_risky_roll_cog.py`): resolving rather than dropping, leaving
other channels alone, reporting nothing closed, and cancelling the pending
auto-close timer.
Scheduler (`tests/cogs/test_scheduled_games_loop.py`): hidden skips and
advances, no role ping, visible launches, no-rotation guilds unaffected, a
one-off staying due, and a read failure failing open.
