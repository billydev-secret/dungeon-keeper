# Sticky-panel extraction — plan

**Status:** Group A **done** (2026-07-26). Groups B and C below are outstanding.

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

### Group B — close, one hook each (outstanding)

| Site | What it needs | What it gains |
|---|---|---|
| `whisper_cog` launcher | nothing — it is simply *behind* the family | it has **no debounce and no id cache**, so it does a threaded DB read *and* a full delete+send per message |
| `confessions_cog` launcher | an `after_place` hook for its component-based duplicate sweep (already implemented on `StickyPanel`) | it has **no throttle at all** and costs ≥5 REST calls per repost, the worst in the codebase; its config reads are **synchronous sqlite on the event loop** |
| `dm_perms_cog` panel | nothing, if a trailing-edge debounce may replace its leading-edge 2s cooldown — that is a **user-visible timing change** and needs a decision | drops a `history(limit=1)` probe; `set_panel_settings` currently runs sync on the event loop |
| `pen_pals_cog` panel | nothing | fixes a real bug: `channel.send` is unguarded *after* the old panel is deleted, so a failed send permanently orphans the panel. Also has **no `cog_unload`**, leaking coroutines |
| `guess_cog` prompt | `StickyPanel` would have to accept `VoiceChannel`/`Thread`, not just `TextChannel` | gains the per-guild lock it currently lacks (manual reposts can race the debounced one) |

Five of these do a config/DB read on the hot path for **every message in the
guild**. Migrating them is a bigger win than the line count suggests.

### Group C — genuinely out of family (leave alone)

- **Casino hub** — a different *algorithm*: in-flight guard rather than
  cancel-and-rearm, a polling loop that holds off up to 300s so the panel can't
  move mid-bet, a `last_message_id` short-circuit instead of the predicate, and
  `ensure_panel` doubling as repaint *and* teardown. Would need five hooks
  nobody else uses.
- **AMA bottom bar** — per-*game-instance* state, not per-guild. The handle is a
  live `discord.Message` on a `bot.active_views` object, the id lives in a games
  payload blob, and `_suppress_resend` is set by unrelated rotation flows. There
  is no `(guild_id) -> (channel_id, message_id)` pair to hand a `load_ids`
  callback.
- **Bump-tracker widget** — reposts on a *domain event* (a detected bump), never
  because it was buried. No burial predicate exists. A refreshing widget that
  occasionally relocates, not a sticky panel.
- **Voice Master panel** — manual `/voice-admin post-panel` only. No listener,
  no repost, and it doesn't delete its predecessor (reposting stacks
  duplicates — arguably its own small bug).

## The shape that landed

Composition, not a mixin — `economy_cog` owns three panels in one cog, so a
mixin can't express it.

```python
class StickyPanel:
    def __init__(self, name, bot, *,
                 load_ids: Callable[[int], tuple[int, int]],   # sync, run in a thread
                 save_ids: Callable[[int, int, int], None],    # sync, run in a thread
                 build: Callable[[Guild], Awaitable[PanelContent]],
                 after_place=None, delay=6.0, cache_ttl=300.0):
    async def place(self, guild, target) -> Message | None
    async def unpost(self, guild) -> bool
    async def refresh(self, guild_id) -> bool
    async def on_message(self, message) -> None
    def set_known_guilds(self, guild_ids) -> None
    def cancel_all(self) -> None
```

`PanelContent(embed, view, signature=None)` is what `build` returns. Supplying
a `signature` opts into the unchanged-content gate; omit it and every refresh
edits. `retry` is a set the owner's loop can drain to re-attempt failed edits.

Each cog holds a `StickyPanel`, forwards `on_message` from a listener and
`cancel_all()` from `cog_unload`, and says nothing about locks or debouncing.

## What Group A actually saved

The economy cog lost ~500 lines of panel machinery and three separate
`on_message` listeners. Beyond the line count, its panels picked up four
behaviours they did not have and would not have got on their own:
post-before-delete, `HTTPException` instead of `Forbidden`-only, single-REST-
call edits, and (where a signature is supplied) skipping no-op edits entirely.

**This means Group A is a behaviour change for the economy panels, not a pure
refactor** — worth verifying on the live server rather than trusting tests
alone.

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
