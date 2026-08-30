# Auto-Delete — Feature Spec

Per-channel "delete messages older than X, sweep every Y" rules configured from the web dashboard. There is no slash command — the feature is admin-only, configured from the web, and runs as a background sweep plus a startup catch-up so messages still age out even if the bot was offline.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| Auto-delete panel | Web (dashboard) | Admin | Create / update / remove a per-channel rule (`max_age`, `sweep_interval`) |

There are no slash commands or context menus. Members never see the configuration surface.

## Behavior

### Setting a rule

An admin opens the dashboard's auto-delete panel, picks a channel, picks a max message age (e.g. 7 days), and picks a sweep interval (e.g. 1 hour). Saving the rule activates it immediately — the next sweep tick that lands after the configured interval starts deleting eligible messages.

An optional **media-only** toggle narrows the rule to messages that carry an attachment (images/videos/files); text-only messages are left alone. "Media" means a Discord attachment — link-preview embeds, stickers, and pasted image URLs that merely unfurl don't count. The default is off (delete everything). Because the sweep is queue-driven and can't re-inspect a message's attachments at delete time, **toggling media-only on an existing rule clears that channel's tracked queue** so the sweep can never delete a message that no longer matches; the queue rebuilds from live tracking and the next startup catch-up. Editing only the age/interval leaves the queue intact.

Removing a rule clears the rule and discards every tracked message for that channel.

### Live tracking

Every new message in a rule-channel is recorded against the channel's rule (queued for future sweep). Under a media-only rule, only messages carrying an attachment are queued. Deletes — single or bulk — clear the message from the queue so the bot doesn't try to re-delete tombstones. Backfilled messages on bot startup follow the same path.

### The sweep

Once per minute the bot walks every active rule. A rule that's been "due" since the last sweep deletes every queued message older than its `max_age`. Messages younger than 13 days are deleted in bulk; older messages are deleted one at a time (Discord's bulk API rejects messages older than 14 days). The bot paces itself to stay under Discord's per-channel rate limits.

If the bot loses **Manage Messages** mid-sweep, the current sweep stops, the rule remains active, and the next tick will retry. A permission gap is channel-wide rather than a verdict on any one message, so it costs no retry attempts. Mods see no in-channel notice — failures only appear in the bot's operator logs.

### When a delete fails

Any other HTTP error from Discord (a 429, a 500, a 400) costs the message **one attempt** and parks it behind a backoff of 1m → 5m → 15m → 1h → 6h; the rest of the queue drains around it in the meantime. Six tries across ~7.4 hours is the whole budget. Exhausting it **abandons** the message: an `ERROR` with the Discord status and code (plus a traceback) lands in the log, the operator gets one DM per abandoned message, and nothing retries it again. The queue row is kept as the record of what's stuck — it's filtered out of the due query by its attempt count, and it clears itself when the message is eventually deleted by any means.

Before this, a single transient error untracked the messages it failed on. Because the sweep is queue-driven and the startup catch-up only reaches back to `last_run_ts - max_age`, those messages became invisible to every future sweep — three were stranded in #🔥│flash-channel on 2026-08-13. A `NotFound` still leaves the queue cleanly: the message is already gone. A 404 on a multi-message bulk request doesn't say *which* id is stale, so those ids are retried individually in the same sweep rather than dropped together.

The startup scan walks channel history rather than the queue, so a delete it fails is handed to the queue with a fresh budget instead of being counted and forgotten.

### Startup catch-up

When the bot boots, every rule runs a one-shot pass over its channel's recent history. Anything past the `max_age` cutoff is deleted; anything younger is queued so the next live sweep can age it out. **Pinned messages are skipped during the startup pass.** A media-only rule additionally skips text-only messages on both the delete and the queue paths. The live sweep skips pins too — see Pinned messages.

### Pinned messages

**A pinned message is never deleted, by either path.** The startup scan has always had this — it walks `Message` objects and skips `message.pinned` — but the live sweep deletes by bare message id and so had no idea. It was written up as a non-goal with a mod policy attached ("don't pin in auto-delete channels"), and that held right up until a feature started pinning things people had paid for: a purchased Flash Theme card is posted and pinned into `#🔥│flash-channel` for a 24-hour run, and that channel carries a one-hour rule, so the sweep took the card down 3,630 seconds after it went up (2026-08-29).

So the sweep now reads the channel's pins **once per pass** — one request, capped at Discord's own 50-pin limit — and subtracts those ids from every batch. Two consequences worth stating:

- **Pinned messages stay queued rather than being dropped.** Unpinning hands the message straight back to the next sweep, which is what the mod who unpins it expects. The filter runs against the ids coming back from `pop_due_auto_delete_message_ids` rather than inside the query, since the pinned set comes from Discord and not the DB; the rows staying put is also what ends the drain loop when a channel's whole backlog is pinned.
- **A pass that cannot read the pins deletes nothing.** `_pinned_message_ids` returns `None` (not an empty set) when Discord refuses, and the sweep returns early without charging anyone an attempt. The cost is a minute's delay; the cost of guessing the other way is a member's money.

## Permissions

- **User-side**: dashboard admin only.
- **Bot-side**: **Manage Messages** in every channel with a rule. **Read Message History** is required for the startup catch-up to walk channel history, and for the live sweep to read a channel's pins.

## User-visible errors

None. Auto-delete has no member-facing surface, so members never see error messages. Dashboard validation errors surface as standard HTTP 400 responses in the admin UI.

## Non-goals

- **No slash command.** Configuration is admin-only by design.
- **No upper bound or sanity range on the configured values.** The API enforces only the same floor the panel does — age and interval must both be at least 1 second, rejected with a 422 below that, since a 0 makes the rule due every tick with every tracked message eligible. Above that floor it accepts whatever it's sent, so an aggressively short (but positive) age is still an admin's own choice.
- **No edit-tracking.** A message's age is its creation time. Editing doesn't reset the timer.
- **No "preserve pins" *toggle*.** Pins are exempt unconditionally (see Pinned messages); there is no per-rule switch to make a sweep delete them anyway.
- **No per-author exclusion.** Bot messages age out the same as member messages.
- **No in-Discord failure surface for mods.** Failures retry on a bounded schedule (see The sweep) and a give-up DMs the bot operator, but nothing is posted in-channel and no dashboard panel lists stuck messages — `SELECT * FROM auto_delete_messages WHERE attempts >= 6` is the report.
- **No audit log of what was deleted.** The deletion is destructive — there's no "what was here" recovery.
- **No coordination with other features.** A starboarded message that ages out leaves the starboard repost intact but its jump-link dies. See [[starboard-spec]].

## Configuration

Per rule (one rule per channel):

| Setting | Purpose |
|---|---|
| `max_age` | Messages older than this are deleted on the next due sweep |
| `sweep_interval` | How often the rule fires |
| `media_only` | When on, only messages with an attachment are eligible (default off) |

No global config keys. Rules are per-guild, per-channel.

## Stored data

Two per-guild tables:

- **Rules** — one row per (guild, channel) with the max-age, interval, last-run timestamp, and the `media_only` flag.
- **Tracked messages** — the pending-deletion queue, one row per pending message plus its retry state (`attempts`, `next_attempt_ts`). Mostly transient: rows are deleted as soon as they're swept (or skipped if Discord already deleted the message). The exception is an abandoned message, whose row is kept — inert, and the only record of what the sweep couldn't delete. A wiped tracked-messages table is not catastrophic: the next startup catch-up rebuilds it from channel history.

No per-user data. No filesystem cache. Live tracking is wired through the bot's message listeners (see [[events-spec]]).
