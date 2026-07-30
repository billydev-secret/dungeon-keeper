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
2. **Only things you can still act on.** An echo pointing at a finished game
   is a dead link and worse than nothing. Results are never echoed — a source
   either just opened, or is about to close.
3. **Skipping beats queueing.** A game that arrives inside a cooldown is
   dropped, not held. Announcing it later means announcing something stale.
4. **Rate limits are the design, not a setting.** The whole feature is
   cooldown arithmetic wrapped around nine event sources — except for the
   exempt ones, where the right rate limit turned out to be none at all.
5. **One announcement per event, not two.** A source that already announced
   itself somewhere gives that up when it moves here (2026-07-29): the quest
   flip's own leaderboard-channel post was deleted rather than left running
   alongside its echo, and community tier crossings stopped DMing the host a
   beat sheet. Duplicating news into two places is noise, not coverage — and
   the cost is paid honestly, since the flip's opt-in role ping went with the
   post rather than being carved into principle 1.

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

Three shapes. **"This just started"** — echo it so people can join. **"Last
chance"** — echo it because a deadline is about to pass. **"This just
happened"** — echo it because the server crossed a boundary worth marking;
nobody has to act. The distinction is not cosmetic; see Rate limiting.

| Source | Shape | Trigger | Dedupe `ref` |
|---|---|---|---|
| `party_game` | start | Any row in `games_active_games` with a posted lobby | `game_id` |
| `gamebot` | start | A Gamebot lobby embed for Cards Against Humanity | message id |
| `discord_event` | start | A native Discord event going `scheduled → active` | event id |
| `bounty` | start | An open `econ_bounties` row with a posted card, within the freshness window | bounty id |
| `auction_closing` | **deadline** | An open `econ_auctions` row whose `ends_at` is within the hour | auction id |
| `pools_closing` | **deadline** | An open `casino_pools_rounds` row whose `closes_at` is within the hour | round id |
| `raffle_closing` | **deadline** | The guild's ISO week rolls within the hour (and the raffle is enabled) | ISO week (`2026-W31`) |
| `quest_flip` | **happened** | A new quest period going live at the ISO-week roll | ISO week (`2026-W31`) |
| `community_tier` | **happened** | A community goal crossing a 40% / 70% / 100% tier | `quest_id:tier` |

`echo_key` is the game type for party games, the sub-game for Gamebot, and
the source name for everything else — those fire a handful of times a year,
so there is nothing finer to bucket by.

**The two "happened" sources are pushed, not swept.** `economy_loop` calls
`echo_quest_flip` / `echo_community_tier` at the moment it commits the thing
being announced, the way `events_cog` calls `echo_discord_event`. Both link at
the **leaderboard panel** (`econ_leaderboard_channel_id` /
`econ_leaderboard_message_id`) — the one surface rendering a week's quests and
a goal's progress bar — and both skip entirely when no panel is posted, the
same gate the raffle applies to the shop panel and for the same reason.

Neither passes an origin channel: the quests aren't *in* a channel, and naming
the panel's channel next to a link to the panel is the same fact twice.

Both carry a **`detail` line** the static `lead` can't: the pool size and ⚡
spotlight for the flip, the tier and contributor count for a crossing. That
copy is built by the economy (`economy_loop.flip_echo_detail`,
`quests.tier_echo_line`), not here — Event Echo owns the frame, the feature
owns its own voice. `tier_echo_line` is the "Suggested post" line the host's
beat sheet used to carry, kept deliberately (see commit 723d2533), minus the
goal title the headline already shows and minus the promise of a next tier
when the crossing *is* the last one.

**The four economy sources** are swept together by `econ_candidates`, on one
shared read connection per tick, and each yields an `EchoCandidate` — so the
sweep is one loop rather than a branch per source. Their queries live in the
services that own those tables (`economy_auction_service.closing_auctions`,
`pools_service.closing_rounds`, `economy_bounty_service.recent_bounties`):
Event Echo consumes rows it doesn't shape, so a column rename stays the owning
feature's problem. Those queries also alias their columns to one shared shape
(`id`, `channel_id`, `message_id`, `deadline`), which is what keeps column
names from being threaded through Event Echo as strings.

Auctions, pools and bounties carry their own `guild_id`, so unlike games the
guild comes straight off the row. The raffle has no row, so it is asked per
guild — every gate it checks (enabled, timezone, shop panel) is guild-scoped
config.

Two schema facts the sweeps depend on:

* A pools round stays `status='open'` for hours *after* betting shuts, waiting
  to settle (migration 140). The sweep filters on `closes_at`, not `status` —
  filtering on status alone would post "last call" to a round that stopped
  taking bets before lunch.
* The **raffle is the odd one out**: there is no raffle row at all. Tickets are
  week-scoped and `economy_loop` draws the closed week's winner at the ISO-week
  roll, so both halves of the echo are derived rather than read. *When* comes
  from `economy.logic.next_week_roll_epoch` — guild-local Monday 00:00, from
  the guild's fixed `tz_offset_hours`. It lives in the economy's own logic
  module, next to `local_day_for`/`local_day_bounds`, so there is one
  expression of the week boundary rather than two that can drift. *What to link to* is the **economy shop panel**
  (`econ_shop_channel_id` / `econ_shop_message_id`), because that is where the
  buy-tickets button lives — which makes it the best jump target of any source:
  the reader lands on the button, not on a description of something elsewhere.
  No shop panel configured means no echo, since "the raffle closes soon" with
  nowhere to act is just an alarm — and the check is on the economy's **master
  switch** as well as the raffle's own flag, because `roll_day` returns early
  when the economy is off, so no draw happens even though the raffle flag and
  a previously-posted shop panel both survive. It is deliberately *not* gated on there
  being entrants already — zero tickets sold is when the nudge is worth most.
  It is also the only source that can't be discovered from a row, so it is
  asked once per guild rather than swept.
* An auction's `ends_at` **moves**: a late bid inside the soft-close window
  pushes it out, which is exactly what this echo is trying to cause. The
  per-auction claim means the echo fires once, at the first tick the auction
  is within an hour of its then-current end; a later extension doesn't
  re-trigger it. This is also why the copy renders Discord's `<t:…:R>` rather
  than a baked-in "in 1 hour", which would be wrong by the time it was read.

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

Two windows, both of which must pass — for **start** sources:

- **Per type** — 60 minutes. The same kind of game at most hourly.
- **Global floor** — 10 minutes. Nothing at all within 10 minutes of the last
  echo, whatever it was.

**Deadline and "happened" sources skip both.** Skip-don't-queue is right for a
game start — miss one and another comes along within the hour — and wrong
everywhere the moment *is* the thing: an "auction ends in an hour" dropped
because a party game echoed 8 minutes earlier is simply lost, and so is the
single announcement an ISO week gets. The floor exists to stop ~20 game types
bursting; every exempt source fires on a fixed, bounded schedule instead
(auctions and pools a handful of times a year — 2 auctions in the server's
entire history; the flip once a week; a goal at most three times a period), so
exempting them costs nothing and only ever saves the valuable ones. Exempt
means "can't be crowded out", not "can repeat" — the per-ref claim still holds
each to one echo.

For the two "happened" sources the argument is sharper still: their dedupe
lives **outside** Event Echo. The ISO week for the flip, and
`econ_community_progress.notified_tier` for a crossing — which the hourly beat
pass advances in the same transaction that detects it. Nothing re-offers
either on a later tick, so an echo the floor refused would be gone for good
rather than merely late.

`SourceSpec` therefore carries `exempt` and `retry` as **independent** flags;
one boolean used to imply both. A deadline is exempt *because* it will be
re-offered (`retry=True`); a boundary already crossed is exempt *because* it
won't be (`retry=False`).

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
rather than repeating one — but a send we *know* failed is released, or an
unreachable destination would burn both cooldowns on behalf of a message
nobody ever saw.

How far the release goes depends on the shape, and the two answers are
opposites:

* **Start sources** keep the row, flagged `suppressed = 1`. Their value expires
  in minutes, so a failed lobby echo isn't worth re-attempting — and leaving
  the ref claimed is what stops the sweep hammering an unreachable channel
  every 15 seconds for the life of the lobby.
* **Deadline sources** drop the row entirely, so the next tick tries again.
  Not retrying would defeat the point of exempting them from the cooldowns:
  one 429 on the first tick of the final hour would lose the last call
  outright, with hundreds of usable ticks still inside the window. The
  deadline bounds the retries by itself — once it passes, the sweep stops
  offering the candidate.
* **"Happened" sources** keep the flagged row like a start source, but for the
  opposite reason to a lobby: nothing re-offers them at all, since they are
  pushed once from the event itself. Dropping the row would erase the only
  record that a send was attempted, and buy no later tick to help.

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

A "happened" echo drops the invitation. There is nothing to join, so the call
to action becomes **See the board →**, and the numbers get their own line:

> **📋 This week's quests are up**
> A fresh set of weeklies just landed.
> **8** weeklies in the pool — `/bank quests` shows yours.
> ⚡ **Spotlight:** Answer the QOTD pays **double** all week.
> **[See the board →]**

> **🏁 Community goal: Send Messages**
> Nice work, everyone.
> 🎉 **Tier 2 down** — 72% and climbing, 14 of you have chipped in. Payout
> secured for everyone; next tier's on the board!
> **[See the board →]**

Members who held the opt-in economy game role used to be pinged by the
quest-flip post. They aren't any more — that post is gone, and the echo that
replaced it is silent like every other one.

## Tests

- `tests/test_event_echo_logic.py` — the cooldown arithmetic, freshness, the
  jump-link format, and that no builder output can contain a mention.
- `tests/test_event_echo_service.py` — the store (dedupe, per-guild scoping,
  suppressed rows not extending windows, prune), the destination gate, the
  three ways an echo is correctly refused, and the two "happened" sources
  (per-week and per-`quest_id:tier` dedupe, surviving a game echo seconds
  earlier, staying silent, not retrying a failed send).
- `tests/test_economy_loop.py` — `flip_echo_detail` / `tier_echo_line` copy,
  the leaderboard-panel gate, and `community_hourly_pulse` splitting crossings
  (echoed) from beats (DMed).
- `tests/test_embed_accent_contract.py` — the `event_echo.game_starting` row.

## Not yet built / Roadmap

- **Opt-in ping role.** Considered and deliberately declined for v1 (2026-07-28)
  — silent first, watch the real rate, add a self-assign role later if the
  echoes turn out to be too easy to miss. If added, it must allow-list exactly
  that role per `embed_style_guide.md`. **Re-raised and declined again
  2026-07-29**, when the quest flip moved here: that post pinged the opt-in
  economy game role, and a per-source ping flag was the obvious way to keep
  it. Declined because "silent except one source" is the shape principle 1
  exists to refuse, and because a ping earned by one weekly source is a ping
  the next source will argue for too. The right version is still a
  self-assign role covering the whole feature, decided on observed rate. If it
  lands, the quest flip is the first candidate — that ping had real reach.
- **Dashboard-tunable cooldowns.** The two windows are constants. Making them
  settings means reading them from config in `decide()` instead of from the
  module constants.
- **Casino and Cat Bot.** Out of scope by decision, not by omission — both fire
  far too often to echo.
- **Three sources are dormant in prod** (as of 2026-07-28). `pools_enabled` is
  unset, so the prediction market never opens a round; `econ_bounty_channel_id`
  is `0`, so bounty cards have nowhere to post and there have been zero
  bounties ever; and the raffle has 0 draws and 0 tickets, with rollout planned
  for the week of 2026-08-03. All three echoes are built and tested, but none
  can fire until those features are switched on. Auctions are live — 2 in the
  server's history.
- **New auctions listed.** Only the closing echo was wanted; an "auction
  opened" echo is a `SOURCE_SPECS` row plus a query away if that changes.

### Surveyed and deliberately not added (2026-07-28)

A sweep of every other feature for the echo shape (a live row, a message to
link to, a deadline or an open state). Decision was to stop at six sources and
watch the real rate first — two of the six can't fire yet, so the picture is
incomplete. Recorded so it isn't re-derived:

* **Musical Chairs (`mc_games`) and Hot Potato Group (`hp_group_games`)** —
  the one true gap. Both are open-join lobby games (`host_id`, `state='LOBBY'`,
  `roster`) that the sweep misses only because they live in their own tables
  rather than `games_active_games`. 3 games each, ever. Same shape as what
  already works; adding them needs no new concepts.
* **1v1 duels** — Pressure Cooker (26 games), Hot Potato duel (21), Chicken
  (13), Quickdraw (5): 65 games against ~133 for all party games, so a third
  of game activity that main chat never hears about. Deliberately excluded:
  every one is `challenger_id` → `target_id`, a directed challenge rather than
  something you can join, so echoing them means a new "worth watching"
  category alongside "joinable". Note Pressure Cooker is *not* a group game
  despite its volume — an easy thing to get wrong from the name.
* **Photo Challenge** — plausible deadline echo ("submissions close soon"),
  15 runs, has a card message and a submission window. Not built.
* **QOTD (13), announcements, birthdays, starboard** — all already post
  somewhere visible on their own; an echo would be double-posting.
* **Casino, drops, Cat Bot** — excluded by decision, far too frequent.
- **Live-updating summary message.** One "games running now" post edited in
  place, instead of one post per game. Rejected for v1: edit loops are the
  shape that caused a repost storm in a live channel on 2026-07-26.
