# Event Echo — Feature Spec

## What it is

When something worth joining starts somewhere in the server, Event Echo posts
a small embed in main chat with a link that takes you straight to it. The
problem it solves is structural: games run in `🎲│games`, `🎲│cat-bot` and the
Gamebot channel, but people sit in `💛│the-meadow` — the busiest channel in the
server by ~30%. A game can open, fill and finish without anyone in the main
room knowing it happened.

It is a signpost, not a notification system. It never pings.

## Product principles

1. **Silence is the feature.** Echoes post with `AllowedMentions.none()` and
   contain no mention of any kind. There is no ping setting, deliberately —
   the events this watches are frequent, and a role ping on each would turn
   the busiest channel in the server into something people mute. If a ping is
   ever wanted it has to be designed as opt-in, not exposed as a flag.
2. **Only the start, only if joinable.** An echo pointing at a finished game
   is a dead link and worse than nothing. Results are never echoed.
3. **Skipping beats queueing.** A game that arrives inside a cooldown is
   dropped, not held. Announcing it later means announcing something stale.
4. **Rate limits are the design, not a setting.** The whole feature is
   cooldown arithmetic wrapped around three event sources.

## Configuration

One key, `event_echo_channel_id`, on **Config → Event Echo** (admin-gated).
Unset means the feature is off — there is no separate enable toggle to drift
out of step with the channel.

It is deliberately *not* `denizen_announce_channel_id`, which points at the
same channel today but is legacy role-grant plumbing (see
[role_grant_spec.md](role_grant_spec.md)); either can be moved without
dragging the other along.

There are no slash commands. Cooldown windows are constants in
`event_echo_logic`, not settings — see "Not yet built" below.

## Sources

| Source | Trigger | `echo_key` | Dedupe `ref` |
|---|---|---|---|
| `party_game` | Any row in `games_active_games` with a posted lobby | game type (`mfk`, `story`, …) | `game_id` |
| `gamebot` | A Gamebot lobby embed for Cards Against Humanity | sub-game (`cah`) | message id |
| `discord_event` | A native Discord event going `scheduled → active` | `discord_event` | event id |

**Party games** are swept by `event_echo_loop` every 15s rather than hooked at
each game's lobby post. Not because hooking would mean 28 call sites — those
funnel through one `update_game_message`, and `end_game`'s `bot=` kwarg is
precedent for threading a side effect into a shared manager function. The real
reason is that `update_game_message` isn't the only path: `games_ffa_cog` and
`games_photo_cog` pass `message_id=` straight to `create_game` and never call
it, so a hook there would silently miss them. A sweep sees whatever ended up
in the table however it got there. It also picks up **scheduled games** for
free (same launch path). The cost is that a game opening and finishing inside
one tick is never echoed, which is the right trade.

The sweep is deliberately **unfiltered by state**. The six lobby games sit in
`joining`, most others in `open`, and `wyr` / `nhie` / `price` are created
straight into `playing` — all three schedulable, so an enumerated state list
silently excluded them. Presence in `games_active_games` already means live;
freshness and the per-game dedupe bound the rest.

The guild comes from the game's **own channel**, never from `ctx.guild_id`:
the table has no `guild_id` column, so a lobby opened in any other guild the
bot is in would otherwise be announced here with a jump link whose guild
segment points at the wrong server. `game_manager.end_game` resolves it the
same way, for the same reason.

**Cards Against Humanity** rides the `on_message` listener
`games_external_cog` already runs over Gamebot for economy payouts, so it costs
a branch on a path that was running anyway rather than a second watcher to
keep in sync with Gamebot's wording. Gamebot posts "Loading…" and edits the
real embed in, so the lobby is usually only visible on the edit; both paths
reach the echo and the message-id dedupe collapses them to one post.
Connect 4 and Anagrams are recognised by the same parser but not echoed —
two-player and quickfire games don't warrant main chat.

**Discord events** fire on the `scheduled → active` transition specifically,
not on `status == active`, so an event created already-live or updated while
live doesn't re-post. `Intents.default()` already carries
`guild_scheduled_events` (it isn't privileged), so no intent change was needed.

## Rate limiting

Two windows, both of which must pass:

- **Per type** — 60 minutes. The same kind of game at most hourly.
- **Global floor** — 10 minutes. Nothing at all within 10 minutes of the last
  echo, whatever it was.

The global floor is the one that matters. With ~20 party-game types, per-type
alone permits twenty posts in a minute with every one inside its own window.

A third bound, **freshness** (10 minutes), only bites after downtime: on
restart the sweep sees every currently-open game at once, and without it the
bot would announce a batch of games that opened while it was down.

Ceiling is ~6 echoes/hour; the realistic rate on observed game volume is 3–5 a
day.

## Storage

`event_echo_log` (migration 141) — one row per *considered* echo.

The non-obvious column is `suppressed`. A game the cooldown rejects still gets
a row, flagged, because the poll loop re-offers that same open lobby every
tick: without the row it would post the moment its cooldown expired, by which
point the game is an hour old and probably over. Recording the refusal makes
"not echoed" a decision taken once, at open.

Cooldown reads therefore filter on `suppressed = 0` — a refusal must not push
the next window out, or one busy minute would cascade into a window that keeps
receding and the feature would go quiet permanently.

Claims are taken *before* the send, so a crash between the two loses an echo
rather than repeating one — but a send we *know* failed is released back to
`suppressed = 1`, or an unreachable destination would burn both cooldowns on
behalf of a message nobody ever saw. The row stays (flagged) rather than being
deleted, so the sweep doesn't retry a dead channel every 15 seconds.

The claim runs under `open_db_immediate`, not the default deferred
transaction: it is a read-then-write on the cooldown window and all three
sources reach it concurrently, so under a deferred transaction two overlapping
claims could both read "nothing echoed yet" and both post inside the floor.

Rows are pruned after 24h.

## Member experience

In main chat:

> **🎲 Marry, Fornicate, Kiss is starting**
> A game is open in #🎲│games.
> **[Jump in →]**

No ping, no notification, no buttons. One accent-coloured embed
(`resolve_accent_color`), footer naming the host where one is known (cache
lookup only — a footer isn't worth an API round trip per game).

Copy varies by source: a native Discord event gets 📅 and "It's happening"
rather than 🎲 and "A game is open". An `external` event carries a location
string and no channel, so the `in <#…>` clause is dropped entirely — anything
else there renders as a mention Discord can't resolve.

## Tests

- `tests/test_event_echo_logic.py` — the cooldown arithmetic, freshness, the
  jump-link format, and that no builder output can contain a mention.
- `tests/test_event_echo_service.py` — the store (dedupe, per-guild scoping,
  suppressed rows not extending windows, prune), the destination gate, and the
  three ways an echo is correctly refused.
- `tests/test_embed_accent_contract.py` — the `event_echo.game_starting` row.

## Not yet built / Roadmap

- **Opt-in ping role.** Considered and deliberately declined for v1 (2026-07-28)
  — silent first, watch the real rate, add a self-assign role later if the
  echoes turn out to be too easy to miss. If added, it must allow-list exactly
  that role per `embed_style_guide.md`.
- **Dashboard-tunable cooldowns.** The two windows are constants. Making them
  settings means reading them from config in `decide()` instead of from the
  module constants.
- **Casino and Cat Bot.** Out of scope by decision, not by omission — both fire
  far too often to echo.
- **Live-updating summary message.** One "games running now" post edited in
  place, instead of one post per game. Rejected for v1: edit loops are the
  shape that caused a repost storm in a live channel on 2026-07-26.
