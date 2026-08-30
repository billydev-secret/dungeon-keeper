# Rotating feature channels — design (exploration)

Status: **design, awaiting sign-off**. Nothing built. Billy's ask: rotate
confessions / whisper / guess-who, announce the day's feature in main chat.

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
