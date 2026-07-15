# Bump Tracker — Feature Spec

Reminds a role when server-listing-site "bump" cooldowns (Disboard-style) expire. Each guild configures a set of sites, each with its own cooldown. Bumps are recorded manually with `/bump log` or auto-detected from the listing bot's confirmation message; when a cooldown expires the bot pings the configured role in the configured channel. A persistent "Bump Tracker" widget embed in that channel shows live per-site status.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/bump log name:<site>` | Slash | Manage Server (group default) | Record a bump for a site; resets its cooldown timer |
| `/bump status` | Slash | Manage Server (group default) | Show current per-site cooldown status (ephemeral embed) |

The `name` argument autocompletes against the guild's configured sites (case-insensitive substring match, max 25 choices).

## Behaviour

### Background loop
A startup task ticks every 60 seconds. For each guild with the feature enabled and a channel configured:

- Any site whose cooldown has expired and hasn't been notified yet triggers a single channel message: `@role Site1, Site2 is/are ready to bump!` (role mention omitted if no role is set). The site is then marked notified, so the ping fires once per bump cycle.
- The widget embed is refreshed after a ping, and otherwise at most every 5 minutes.

### Widget
An embed titled "Bump Tracker" listing each site as either `✅ Ready to bump!` or `⏰ Xh Ym remaining`, with the footer "Use /bump log <site> after bumping to reset the timer." Colour follows the guild accent (blurple fallback). When nothing new was posted to the channel the widget is edited in place (avoids the unread indicator); after a ping or auto-detected bump the old widget is deleted and re-sent so it stays at the bottom of the channel. The new message ID is persisted.

A site with no logged bump counts as ready.

### `/bump log`
Validates the site name against the guild's configured sites, upserts the bump timestamp (clearing the notified flag), confirms ephemerally, then refreshes the widget in place (if a channel is configured).

### `/bump status`
Renders the same widget embed ephemerally to the invoker. No writes.

### Auto-detection
Sites may carry a detector: a listing bot's user ID plus an optional text pattern. When any bot posts in the configured channel, the message is matched against each detector site — author must equal `detector_bot_id`, and if a pattern is set it must appear (case-insensitive) in the message content or any embed description. First match wins: the bump is logged automatically and the widget is force-resent to the bottom of the channel.

## User-visible errors

| When | The user sees |
|---|---|
| `/bump log` with an unknown site | "No site named **{name}** found." |
| `/bump status` with no sites configured | "No sites configured. Add sites from the web dashboard." |
| Widget shown with no sites configured | Embed body: "No sites configured. Add sites from the web dashboard." |

Failures to send the ping or widget (missing channel, HTTP errors) are logged server-side and silently skipped.

## Configuration

All configuration lives in the web dashboard (`PUT /config/bump-tracker` and `/config/bump-tracker/sites/...`); there are no config slash commands.

- **Channel** — where pings and the widget are posted; the feature is inactive until set.
- **Role** — pinged when a site becomes ready (optional).
- **Enabled** — master toggle (default on).
- **Per site**: name, cooldown in seconds, optional detector bot ID and detector pattern.

The `/bump` command group defaults to requiring **Manage Server**.

## Stored data

SQLite, migrations `044_bump_tracker.sql` and `047_bump_tracker_detector.sql`:

- `bump_tracker_config` — per guild: `channel_id`, `role_id`, `widget_message_id`, `enabled`.
- `bump_tracker_sites` — per (guild, site): `cooldown_seconds`, `detector_bot_id`, `detector_pattern`.
- `bump_tracker_log` — per (guild, site): last `bumped_at` (unix timestamp) and `notified` flag. Only the latest bump per site is kept — no history. Removing a site also deletes its log row.
