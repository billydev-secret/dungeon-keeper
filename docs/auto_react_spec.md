# Auto React — Feature Spec

Automatically adds a configured set of emoji reactions to image posts in specific channels. Each rule is per-channel: when a non-bot member posts a message containing an image in a channel with an enabled rule, the bot reacts with every emoji in that rule's list. A frictionless engagement nudge — reactions arrive without anyone lifting a finger, and (via the starboard's reaction thresholds) give visual content a head start.

## Commands

None. The feature has no slash commands or context menus — it is a pure `on_message` listener, configured out-of-band (see Configuration).

## Behaviour

The listener fires on every message and bails early unless all of the following hold:

- Author is not a bot.
- Message is in a guild (DMs ignored).
- Message **contains an image**: an attachment whose `content_type` starts with `image/`, or an embed of type `image`, `gifv`, or `rich` that carries an image or thumbnail.
- The channel has a rule row and that rule's `enabled` flag is set. Lookup is exact on `(guild_id, channel_id)` — threads and forum posts don't inherit their parent channel's rule.

When a rule matches, all of its emojis are added concurrently (`asyncio.gather`). A failing emoji (deleted custom emoji, missing Add Reactions permission, invalid string) is logged as a warning and does **not** block the others — each reaction succeeds or fails independently.

Embeds are inspected as they exist at message-creation time. Link previews that Discord attaches to a message afterwards (via message edit) are not seen, so a bare image URL usually won't trigger a reaction; uploaded attachments always will.

## Configuration

Managed through the web dashboard's admin API (admin scope required); there is no in-Discord configuration.

- Current rules are returned in the `auto_react` section of `GET /api/config` — one entry per channel with `channel_id`, `emojis` (list), and `enabled`.
- `PUT /api/config/auto-react/{channel_id}` with body `{"emojis": [...], "enabled": true}` creates or replaces the channel's rule (upsert; the emoji list is replaced wholesale, not merged).
- `DELETE /api/config/auto-react/{channel_id}` removes the rule.

Emojis are free-form strings — Unicode emoji or full custom-emoji syntax (`<:name:id>`). No validation happens at write time; a bad entry simply fails (with a log warning) when a reaction is attempted.

Note: as of this writing no dashboard **panel** exists for these endpoints — the backend API is complete, but rules must be managed by calling the API directly.

## Stored data

One SQLite table, `auto_react_config` (migration `043_auto_react.sql`):

| Column | Type | Notes |
|---|---|---|
| `guild_id` | INTEGER | Part of primary key |
| `channel_id` | INTEGER | Part of primary key |
| `emojis` | TEXT | Comma-separated emoji list (default `''`) |
| `enabled` | INTEGER | 1 = active, 0 = paused without deleting the rule (default 1) |

No per-message state is stored — nothing records which messages were reacted to.
