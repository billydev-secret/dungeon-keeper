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

If the bot loses **Manage Messages** mid-sweep, the current sweep stops, the rule remains active, and the next tick will retry. Mods see no in-channel notice — failures only appear in the bot's operator logs.

### Startup catch-up

When the bot boots, every rule runs a one-shot pass over its channel's recent history. Anything past the `max_age` cutoff is deleted; anything younger is queued so the next live sweep can age it out. **Pinned messages are skipped during the startup pass.** A media-only rule additionally skips text-only messages on both the delete and the queue paths. The live sweep doesn't re-check pin state — see Non-goals.

## Permissions

- **User-side**: dashboard admin only.
- **Bot-side**: **Manage Messages** in every channel with a rule. **Read Message History** is required for the startup catch-up to walk channel history.

## User-visible errors

None. Auto-delete has no member-facing surface, so members never see error messages. Dashboard validation errors surface as standard HTTP 400 responses in the admin UI.

## Non-goals

- **No slash command.** Configuration is admin-only by design.
- **No min/max enforcement on the configured values.** The dashboard surfaces sensible floors but the API accepts whatever it's sent — a malformed config can produce an aggressive rule.
- **No edit-tracking.** A message's age is its creation time. Editing doesn't reset the timer.
- **No "preserve pins" toggle in the live sweep.** Pin a message after the queue picks it up and it'll still get deleted on the next tick. Mod policy: don't pin in auto-delete channels.
- **No per-author exclusion.** Bot messages age out the same as member messages.
- **No retry queue / failure surface.** A permission failure just retries on the next tick; there's no in-Discord notice that anything went wrong.
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
- **Tracked messages** — the pending-deletion queue. Transient — rows are deleted as soon as they're swept (or skipped if Discord already deleted the message). A wiped tracked-messages table is not catastrophic: the next startup catch-up rebuilds it from channel history.

No per-user data. No filesystem cache. Live tracking is wired through the bot's message listeners (see [[events-spec]]).
