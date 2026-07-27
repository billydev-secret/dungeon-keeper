# Sticky-panel extraction — plan

**Status:** Groups A and B **done** (2026-07-26). Group C stays out by design;
one group-B site (guess) is still outstanding.

`bot_modules/core/sticky.py` now holds `StickyPanel` — the shared locks,
debounce, id cache, post-before-delete placer, signature gate and listener —
covered by `tests/test_core_sticky.py`. The economy guide / shop / leaderboard
panels and the todo board all run on it.

## The problem

Discord has no reorder API, so a panel that must stay at the bottom of a
channel keeps its place by deleting itself and re-posting whenever a member
posts beneath it. Before this change, four features implemented that
identically — each with its own per-guild lock map, debounce-task map, TTL
cache of `(channel_id, message_id)`, `on_message` listener, and
delete-and-repost placer, roughly 150–165 lines apiece.

The copies had **diverged in ways that mattered**, which was the real argument
for extracting:

| | economy guide / shop / leaderboard | todo board |
|---|---|---|
| Order of operations | delete old → send new | **send new → delete old** |
| Send failure | `except discord.Forbidden` | `except discord.HTTPException` |
| Unchanged-content skip | none | `board_signature` gate |
| Edit / delete calls | `fetch_message()` + `.edit()`/`.delete()` | `get_partial_message()` |

The todo copy was better on all four rows, and none of those improvements would
have reached the economy panels on their own. Worse, the "take the lock
*before* re-reading the stored ids" fix
(`docs/reviews/2026-07-23-novel-hunt.md`, finding S3) had to be applied three
separate times in `economy_cog.py` and was then re-derived a fourth time in the
todo board from a docstring.

## The full survey — 12 sites

An exhaustive sweep (2026-07-26) found **twelve** places that repost or bump a
message in response to channel activity, not the four this doc originally
listed. Grouped by how well they fit a shared abstraction:

### Group A — migrated ✅

Structurally identical; only "where the ids live" and "what it looks like"
differ.

- `economy_cog` guide panel
- `economy_cog` shop panel
- `economy_cog` leaderboard / quest board
- `cogs/todo_cog.py` todo board

All four now construct a `StickyPanel`. The economy panels gained
post-before-delete, the wider `HTTPException` catch, and single-REST-call
edits in the process. The economy cog's three `on_message` listeners collapsed
into one that forwards to all three panels.

### Group B — migrated ✅ (except guess)

| Site | What it needs | What it gains |
|---|---|---|
| `whisper_cog` launcher | nothing — it is simply *behind* the family | it has **no debounce and no id cache**, so it does a threaded DB read *and* a full delete+send per message |
| `confessions_cog` launcher | an `after_place` hook for its component-based duplicate sweep (already implemented on `StickyPanel`) | it has **no throttle at all** and costs ≥5 REST calls per repost, the worst in the codebase; its config reads are **synchronous sqlite on the event loop** |
| `dm_perms_cog` panel | nothing, if a trailing-edge debounce may replace its leading-edge 2s cooldown — that is a **user-visible timing change** and needs a decision | drops a `history(limit=1)` probe; `set_panel_settings` currently runs sync on the event loop |
| `pen_pals_cog` panel | nothing | fixes a real bug: `channel.send` is unguarded *after* the old panel is deleted, so a failed send permanently orphans the panel. Also has **no `cog_unload`**, leaking coroutines |
| `guess_cog` prompt | **still outstanding** — `StickyPanel` would have to accept `VoiceChannel`/`Thread`, not just `TextChannel` | would gain the per-guild lock it currently lacks (manual reposts can race the debounced one) |

`pen_pals`, `dm_perms` and `voice_master` are migrated. `whisper` and
`confessions` are not yet done and remain the two worst offenders on the hot
path. `guess` is blocked on widening the channel type.

**Behaviour changes that shipped with these:**

- **dm_perms** moved from a leading-edge 2s cooldown to the shared
  trailing-edge debounce, so the panel settles after the channel falls quiet
  rather than jumping on the first message of every burst. Its
  `history(limit=1)` probe and its sync-on-the-event-loop
  `set_panel_settings` both went away.
- **pen_pals** had a real bug: `channel.send` was unguarded *after* the old
  panel was deleted, so a failed send permanently orphaned the panel row. It
  also had no `cog_unload`, leaking a coroutine per reload. Both fixed by the
  migration.
- **voice_master** was **manual-only** and did not delete its predecessor, so
  re-running `/voice-admin post-panel` stacked duplicates. It is now sticky and
  replaces the old panel. Needed a new config key,
  `voice_master_panel_channel_id`.
- **casino** kept its hold-off semantics via the new `hold` hook, and the rule
  changed slightly on request: it now blocks while a round is live **and for
  60s after one settles** (previously: while live, capped at 300s), so players
  reading a result aren't chasing the panel up the channel.

### The `hold` hook

Added to `StickyPanel` for casino: an async predicate that answers "not yet".
While it returns True the restick waits, re-checking every `hold_poll` seconds
up to `hold_max`, then re-sticks anyway — a hold that never clears would
otherwise bury the panel permanently, which is worse than moving it at an
awkward moment. It gates the *sticky repost* only; an explicit `place` (an
admin reposting deliberately) is always honoured.

### `restick_on_bot`, and why the panel must never chase itself

`StickyPanel.on_message` ignores bot authors by default. Casino opts out
(`restick_on_bot=True`) because the casino is what buries its own hub: round
results, big-win broadcasts and jackpot celebrations all land in the hub
channel, and a round settling with nobody typing left the hub — the only entry
point — stranded above the result.

That opt-in reopens the self-loop the bot filter was preventing, since the
panel's own repost is itself a bot message in the panel's own channel. The
first attempt at protection was the message-id skip in `should_restick`, with
`place()` recording the new id immediately after `send()` so the id would be
cached before the gateway event for that repost arrived. **That is a race, and
it usually loses** — the `MESSAGE_CREATE` frame is dispatched while `place()`
is still awaiting the HTTP response, so the cache still holds the *old* panel
id and the repost is waved through as if a member had posted.

It shipped and looped in prod: the casino hub reposted itself every ~6 seconds
(exactly `DEFAULT_DELAY`) in bursts of five to seven, each burst ending only
when the race happened to be won.

The first fix was the at-the-bottom guard: **a panel that is already the
channel's last message is never re-sticked.** Nothing is buried, so there is
nothing to move. It reads `channel.last_message_id`, which discord.py maintains
from the gateway, so it costs no API call. The message-id skip stays as a cheap
way to avoid arming a doomed debounce, but it is an optimisation, not the
protection.

The guard applies to every panel, not just the opted-in one: any restick that
would repost a panel already at the bottom was a wasted delete-and-send.

**That guard shipped and the loop continued** — same ~6s cadence, now
unbroken for hours and surviving a restart. The guard was sound; it was being
fed a stored id that could no longer advance. `schedule_restick` cancel-and-arms
on every trigger, and the task it cancels may be the one *inside* `place()`,
parked in `send()`. Discord has already accepted the message at that point, so
the panel posts — but everything after the send (`_remember`, the old-panel
delete, `save_ids`) never runs. Each iteration therefore left a live panel
nobody held the id for, froze the stored id on a long-dead message, and left the
previous panel in place; the next restick compared `last_message_id` against
that frozen id, saw a mismatch, and posted again. Self-sustaining, and immune to
a restart: boot's `place_or_refresh` found the stale id still pointing at a real
(buried) message and edited it in place, so the very next bot message re-armed
the cycle. Prod evidence: `casino_panel_message_id` unchanged across ~200
panel posts, none of which deleted its predecessor.

So a placement is now **atomic to its caller's cancellation** — `place()` runs
the work as a shielded task, and a cancel stops the *waiting*, never the work.
The at-the-bottom check moved inside the placement lock as `only_if_buried`
(passed by the restick path, never by an explicit post), where it is decided
against ids read under that lock: a restick that queued behind another placement
now sees that placement's result instead of stacking a second panel on top of
it. Cancelling the debounce still cancels a *pending* repost, which is all the
coalescing ever needed.

Rejected alongside it: suppressing resticks for bot messages that arrive while a
placement is in flight. It only saves one debounce tick and a DB read once
placements are atomic, and it would swallow a genuine round result posted
concurrently with a repost.

One accepted edge: if the panel is hand-deleted *while it is the last message*,
`last_message_id` still points at it and the next restick no-ops. The next
message in the channel moves `last_message_id` on and the repost heals it —
and that message is the restick trigger anyway.

### Group C — genuinely out of family (leave alone)

- ~~**Casino hub**~~ — **migrated** after all, once `StickyPanel` grew the
  `hold` hook. Its `ensure_panel` still owns boot, teardown and the in-place
  repaint; only the *sticky repost* path routes through the shared placer.
- **AMA bottom bar** — per-*game-instance* state, not per-guild. The handle is a
  live `discord.Message` on a `bot.active_views` object, the id lives in a games
  payload blob, and `_suppress_resend` is set by unrelated rotation flows. There
  is no `(guild_id) -> (channel_id, message_id)` pair to hand a `load_ids`
  callback.
- **Bump-tracker widget** — reposts on a *domain event* (a detected bump), never
  because it was buried. No burial predicate exists. A refreshing widget that
  occasionally relocates, not a sticky panel.
- ~~**Voice Master panel**~~ — **migrated**; it is now automatic and no longer
  stacks duplicates.

## The shape that landed

Composition, not a mixin — `economy_cog` owns three panels in one cog, so a
mixin can't express it.

```python
class StickyPanel:
    def __init__(self, name, bot, *,
                 load_ids: Callable[[int], tuple[int, int]],   # sync, run in a thread
                 save_ids: Callable[[int, int, int], None],    # sync, run in a thread
                 build: Callable[[Guild], Awaitable[PanelContent]],
                 hold=None, hold_poll=15.0, hold_max=600.0,
                 delay=6.0, cache_ttl=300.0):
    async def place(self, guild, target) -> Message | None   # ignores `hold`
    async def place_or_refresh(self, guild, target)          # what commands want
    async def unpost(self, guild) -> bool
    async def refresh(self, guild_id) -> bool
    async def on_message(self, message) -> None
    def set_known_guilds(self, guild_ids) -> None
    def cancel_all(self) -> None
```

`PanelContent(embed, view, signature=None)` is what `build` returns. Supplying
a `signature` opts into the unchanged-content gate; omit it and every refresh
edits. `place_or_refresh` is what a "post the panel" command should call — it
edits in place when the panel is already in the target channel (so re-running
after a re-brand doesn't hop it to the bottom) and posts fresh otherwise. All
five command/route call sites use it; none re-derives its own embed. `take_retries()` drains the guilds whose last edit failed, for an owner with a
periodic loop; owners without one never call it. `set_known_guilds(ids)` is the
opt-in fast path — every migrated cog now publishes it, so a guild with no panel
costs a set lookup rather than a DB read on every message.

Each cog holds a `StickyPanel`, forwards `on_message` from a listener and
`cancel_all()` from `cog_unload`, and says nothing about locks or debouncing.

## What the migration actually saved

The economy cog lost ~500 lines of panel machinery and three separate
`on_message` listeners. Beyond the line count, its panels picked up four
behaviours they did not have and would not have got on their own:
post-before-delete, `HTTPException` instead of `Forbidden`-only, single-REST-
call edits, and (where a signature is supplied) skipping no-op edits entirely.

**This means the migration is a behaviour change for the economy panels, not a
pure refactor** — worth verifying on the live server rather than trusting tests
alone. The same applies to every group-B site: see the behaviour-change list
above.

Nine of the twelve surveyed sites now share one implementation. `whisper` and
`confessions` are the remaining migratable ones; `guess` needs the channel-type
widening first.

## Related, separately deferred

- **Schedule validation** exists three times: `routes/scheduled_games.py`
  `_validate`, `routes/photo_challenge.py` `_validate_schedule`, and
  `services/todo_recurring_service.py` `validate`. The newest is at the better
  altitude (service layer, domain error) — the other two raise `HTTPException`
  from what is business logic. A shared `services/recurrence.py` exporting
  `normalize_days`, `validate_schedule`, `describe_cadence` and `WEEKDAY_NAMES`
  would collapse them.
- **JS weekday/time helpers** duplicated across `panels/games-scheduling.js`,
  `panels/photo-challenge.js` and `panels/todo.js` (`WEEKDAYS`,
  `timeToMinutes`/`minutesToTime`, the weekday-checkbox row). Todo additionally
  renders its cadence label server-side, which is the better pattern for the
  other two to adopt.
- **Channel-resolution ladder** (bot → guild → 503 → `get_channel` → 400)
  appears in `routes/chat_revive.py` (`_require_channel`), `routes/config.py`
  and now `routes/todo.py`. Belongs in `web_server/helpers.py`, returning
  `(guild, channel)`.
