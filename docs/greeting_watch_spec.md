# Greeting Watch

**Flavor: Reference** — matches current behavior.

## What it does

Catches "good morning" / "hello" style messages in your main chat that go
**unanswered**, so nobody who says hi to the room falls through the cracks. When
a greeting isn't replied to or @mentioned within a configurable window, the bot
**DMs the chosen members** (e.g. admins/greeters) with a jump link — one or
several people can subscribe to the alerts.

There is no Discord command surface — it's configured entirely on the dashboard
(**Config → Greeting Watch**, admin-only), per the project's "config lives on the
web" rule.

## How it works

1. **Detection (live, at ingest).** In `EventsCog.on_message`, for a real member
   message posted in a watched channel, `is_greeting(message.content)` decides
   whether it opens with a greeting token. This must happen live: the default
   `message_storage_level="none"` drops message text before it reaches the DB,
   so a greeting can't be matched after the fact. A match writes a lightweight
   row to `greeting_watch` (ids + timestamp only — **no message text stored**).

   `is_greeting` is a heuristic, not a classifier: it matches a short message
   (≤ 8 words) that *starts* with a hello-ish token — "good morning", "gm",
   "morning", "hello", "hey", "hi", "hiya", "howdy", "good afternoon/evening",
   "yo", "sup", "what's up", "greetings", "hola" — or a check-in phrase
   standing in for one: "good timezone" (a jokey stand-in for "good
   morning/afternoon/evening" in servers spread across timezones) and "how's
   everyone's/your morning/afternoon/evening/day/night/weekend (going)". A
   word boundary keeps "history" / "gaming" / "morningstar" from matching, and
   the check-in phrase requires a plural/2nd-person subject right after
   "how's" so generic questions ("how's this bug possible") don't match. Tune
   the vocabulary in `greeting_watch_service.py` as real misses surface.

   One open watch per (channel, author): a second greeting from the same person
   while the first is still pending is a no-op, so a "gm 🙂 … hey all" double
   post can't queue two alerts.

   **Replies never count**, even when the text opens with a hello-ish token —
   a Discord reply is addressed to whoever it replies to, not the room (e.g.
   "hey, no worries" as a reply shouldn't queue an unanswered-greeting alert).

2. **Verdict (background loop).** `greeting_watch_loop` ticks every 60s. For each
   guild with pending rows, it reads config fresh from the DB (so dashboard
   changes apply without a restart) and picks up greetings whose window has
   closed. "Was it answered?" reads `user_interactions_log` — the ingest path
   already records one edge there for every **reply target** and **@mention**.
   If anyone *other than the greeter* has an edge pointing **to** the greeter
   inside `[greeting_ts, greeting_ts + window]`, it's `acknowledged`; otherwise
   it's `unanswered` and **every** notify member is DMed. Either way the row is
   resolved so it's never re-processed. If the feature is turned off (or every
   notify member cleared) mid-window, still-pending rows are retired as
   `skipped`.

Definition of "answered": a Discord **reply** to the greeter, or a message that
**@mentions** them. A bare "hey!" that neither replies nor tags them can't be
attributed and so reads as unanswered — the practical soft edge of the feature.

## Configuration (Config → Greeting Watch)

| Field | Config key | Meaning |
|---|---|---|
| Enable greeting watch | `greeting_watch_enabled` | Master on/off. |
| Watched channels | `greeting_watch_channel_ids` | CSV of channel ids — your "main chat". Empty = nothing watched. |
| Notify (DM) these members | `greeting_watch_notify_user_ids` | CSV of member ids — everyone listed gets the DM. Empty = no DM sent. Falls back to the legacy single `greeting_watch_notify_user_id` if the CSV is unset. |
| Unanswered window (minutes) | `greeting_watch_window_minutes` | Wait before flagging (default 10). |

`greeting_watch_enabled` and `greeting_watch_channel_ids` are read on the ingest
hot path via the cached `GuildConfig` snapshot (invalidated on save); the loop
reads its own keys straight from the DB each tick.

## Data

Migration `078_greeting_watch.sql`:

```
greeting_watch(guild_id, message_id, channel_id, author_id,
               created_ts, resolved_at, outcome)   PK (guild_id, message_id)
```

`resolved_at IS NULL` = pending; `outcome` ∈ {`acknowledged`, `unanswered`,
`skipped`}. Partial indexes cover the pending sweep and the per-author dedup
lookup. No message content is ever stored.

## Economy hook

A reply/mention landing on a member whose greeting is still **pending** fires
the `greeting_answered` quest trigger for the answerer
(`pending_greetings_for`, wired in `events_cog._econ_work`; occurrence = the
greeting message id, so one hello credits an answerer once). See
`economy_spec.md` §4.5.

## Not built / possible follow-ups

- **Silence variant.** Only the "ignored in a crowd" definition ships (nobody
  acknowledged the greeter). A "channel went dead-silent after the greeting"
  detector could be added off `processed_messages` if wanted.
- **Alert routing.** DMs one or more chosen members. Routing to a mod channel
  or role ping would reuse the `mod_channel_id` / Rules Watch alert patterns.
- **Dashboard log.** Resolved rows carry a verdict but aren't surfaced anywhere;
  a small "recent unanswered greetings" view could live under Reports.
