# Sticky-panel extraction — plan

**Status:** proposed, not started. Filed 2026-07-26 when the todo board became
the fourth copy of this pattern.

## The problem

Discord has no reorder API, so a panel that must stay at the bottom of a
channel keeps its place by deleting itself and re-posting whenever a member
posts beneath it. Four features now implement that identically, each with its
own per-guild lock map, debounce-task map, TTL cache of
`(channel_id, message_id)`, `on_message` listener, and delete-and-repost
placer — roughly 150–165 lines apiece.

The copies have already **diverged in ways that matter**, which is the real
argument for extracting:

| | economy guide / shop / leaderboard | todo board |
|---|---|---|
| Order of operations | delete old → send new | **send new → delete old** |
| Send failure | `except discord.Forbidden` | `except discord.HTTPException` |
| Unchanged-content skip | none | `board_signature` gate |
| Edit / delete calls | `fetch_message()` + `.edit()`/`.delete()` | `get_partial_message()` |

The todo copy is the better one on all four rows, and none of those
improvements will reach the economy panels on their own. Worse, the "take the
lock *before* re-reading the stored ids" fix (`docs/reviews/2026-07-23-novel-hunt.md`,
finding S3) had to be applied three separate times in `economy_cog.py` and was
then re-derived a fourth time here from a docstring.

## In scope — four call sites

Structurally identical; only "how do I load/save the ids" and "how do I build
the embed" differ.

- `economy_cog` guide panel — `_place_guide_panel` and friends
- `economy_cog` shop panel — `_place_shop_panel` and friends
- `economy_cog` leaderboard panel — `_place_leaderboard_panel` and friends
- `cogs/todo_cog.py` todo board — `place_board` and friends

## Explicitly out of family — do **not** force these in

- **Casino hub** (`cogs/casino/cog.py`) — different algorithm, not different
  parameters: an in-flight guard instead of cancel-and-rearm, a polling loop
  with `RESTICK_ROUND_HOLD_SECONDS` to avoid moving the panel mid-round, and a
  `channel.last_message_id` short-circuit instead of `should_restick_guide`.
- **Guess prompt** (`cogs/guess_cog.py`) — no lock, no TTL cache, no persisted
  message id, re-reads config per message.
- **AMA bottom bar** (`cogs/games_ama_cog.py`) — `_suppress_resend` is a
  per-*game-instance* flag on a live object, not per-guild; it edits an
  in-memory `self._bottom_msg` rather than ids from a table.

Generalising across these would produce a lowest-common-denominator abstraction
with hooks nobody else uses.

## Proposed shape

Composition, not a mixin — `economy_cog` owns three panels in one cog, so a
mixin can't express it.

```python
class StickyPanel:
    def __init__(self, ctx, bot, name, *,
                 load_ids: Callable[[int], tuple[int, int]],      # sync, run in a thread
                 save_ids: Callable[[int, int, int], None],
                 build: Callable[[Guild], Awaitable[tuple[Embed, View]]],
                 signature: Callable[[int], Awaitable[Hashable]] | None = None,
                 delay: float = 6.0, ttl: float = 300.0):
    async def place(self, guild, target) -> Message | None
    async def unpost(self, guild) -> bool
    async def refresh(self, guild_id) -> bool
    async def on_message(self, message) -> None
    def cancel_all(self) -> None
```

Each cog holds `self._panel = StickyPanel(...)`, forwards one `on_message`
listener and `cog_unload`, and keeps `should_restick_guide`
(`core/sticky.py`) inside the helper.

## Expected payoff

~620 lines across four copies → one ~200-line helper plus ~25 lines of wiring
each: **roughly −320 lines, and one place to fix the next race** instead of
four. Every call site inherits post-before-delete, the wider exception catch,
the signature skip, and single-call edits.

## Why not now

The value is almost entirely inside `economy_cog.py` — a ~5,000-line file with
an open review docket. Rewriting three of its panels from a todo-board branch
is exactly the uninvited scope-expansion CLAUDE.md warns against, and it would
drag the whole economy suite onto the gate for a change that ships no user-
visible behaviour.

## First step, already done

`should_restick_guide` moved from `bot_modules/economy/guide.py` to
`bot_modules/core/sticky.py` (re-exported from `guide` so economy call sites
are untouched). A moderation feature importing the economy package to decide
whether to re-stick a panel was the wrong dependency, and `core/sticky.py` is
where the helper above will live.

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
