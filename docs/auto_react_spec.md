# Auto React — Feature Spec

Automatically adds a configured set of emoji reactions to image posts in specific channels. Each rule is per-channel: when a non-bot member posts a message containing an image in a channel with an enabled rule, the bot reacts with every emoji in that rule's list. A frictionless engagement nudge — reactions arrive without anyone lifting a finger, and (via the starboard's reaction thresholds) give visual content a head start.

## Commands

None. The feature has no slash commands or context menus — it is a pure `on_message` listener, configured out-of-band (see Configuration).

## Behavior

The listener fires on every message and bails early unless all of the following hold:

- Author is not a bot.
- Message is in a guild (DMs ignored).
- Message **contains an image**: an attachment whose `content_type` starts with `image/`, or an embed of type `image`, `gifv`, or `rich` that carries an image or thumbnail.
- The channel has a rule row and that rule's `enabled` flag is set. Lookup is exact on `(guild_id, channel_id)` — threads and forum posts don't inherit their parent channel's rule.

When a rule matches, all of its emojis are added concurrently (`asyncio.gather`). A failing emoji (deleted custom emoji, missing Add Reactions permission, invalid string) is logged as a warning and does **not** block the others — each reaction succeeds or fails independently.

Embeds are inspected as they exist at message-creation time. Link previews that Discord attaches to a message afterwards (via message edit) are not seen, so a bare image URL usually won't trigger a reaction; uploaded attachments always will.

### Tipping rules

A rule with `tips_enabled` behaves differently, because in a tipping channel the emoji the bot places are **live payment buttons** ([[economy-spec]]) rather than decoration. Emoji are therefore only placed on a post that qualified:

1. The channel must be **`is_nsfw()`** — Discord's own age gate is the rail, and the classifier only narrows within it. A tipping rule on a non-age-gated channel places nothing at all.
2. **Attachments only.** Embeds are never classified (their images live on arbitrary external hosts — see [[nsfw-classifier-spec]]), so a tipping rule places nothing on an embed-only post, unlike a plain rule.
3. At least one image attachment must not be ruled out by the classifier.

Step 3 **fails open**: an image the classifier couldn't read still gets emoji. Only a confident "read it, not explicit" withholds them. The asymmetry is deliberate — the cost of being wrong is a poster silently losing tips they can neither see nor appeal, so a CDN hiccup must not cause it.

The age gate is checked *before* classifying, so a misconfigured rule costs no downloads and records no metrics.

When emoji are placed, a **placement receipt** is recorded. Only emoji the bot itself placed on that specific message can be tipped; see [[economy-spec]] for why that matters. Emoji that failed to attach are excluded from the receipt, so nothing unpayable is recorded as tippable. A receipt failure is logged loudly but never breaks the listener — the post simply isn't tippable.

Rules **without** `tips_enabled` are entirely unaffected by any of the above: same embeds, same channels, no classification, no receipts.

## Configuration

Managed through the web dashboard's admin API (admin scope required); there is no in-Discord configuration.

- Current rules are returned in the `auto_react` section of `GET /api/config` — one entry per channel with `channel_id`, `emojis` (list), `enabled`, and `tips_enabled`.
- `PUT /api/config/auto-react/{channel_id}` with body `{"emojis": [...], "enabled": true, "tips_enabled": false}` creates or replaces the channel's rule (upsert; the emoji list is replaced wholesale, not merged). `tips_enabled` defaults to false — turning a channel into a tipping channel is always an explicit act.
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
| `tips_enabled` | INTEGER | 1 = the placed emoji are live tip buttons (default 0, migration `142_auto_react_tips.sql`) |

`auto_react_placements` (migration `142_auto_react_tips.sql`) records what the bot placed on a tipping post — `guild_id`, `channel_id`, `message_id`, `author_id` (the tip recipient, resolved at placement time), the emoji actually attached, and a timestamp. Plain non-tipping rules write nothing here, so for them it remains true that no per-message state is stored.

The receipt exists rather than checking `reaction.me` on the message because that would cost an API round trip on every reaction event at ~1,050 events/day, and this table doubles as the record of which posts qualified.
