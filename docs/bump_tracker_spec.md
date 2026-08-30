# Bump Tracker — Feature Spec

Reminds a role when server-listing-site "bump" cooldowns (Disboard-style) expire. Each guild configures a set of sites, each with its own cooldown. Bumps are recorded manually with `/bump log` or auto-detected from the listing bot's confirmation message; when a cooldown expires the bot pings the configured role in the configured channel. A persistent "Bump Tracker" widget embed in that channel shows live per-site status.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/bump log name:<site>` | Slash | Manage Server (group default) | Record a bump for a site; resets its cooldown timer |
| `/bump status` | Slash | Manage Server (group default) | Show current per-site cooldown status (ephemeral embed) |

The `name` argument autocompletes against the guild's configured sites (case-insensitive substring match, max 25 choices).

## Behavior

### Background loop
A startup task ticks every 60 seconds. For each guild with the feature enabled and a channel configured:

- Any site whose cooldown has expired and hasn't been notified yet triggers a single channel message: `@role Site1, Site2 is/are ready to bump!` (role mention omitted if no role is set). The site is then marked notified, so the ping fires once per bump cycle.
- The widget embed is refreshed after a ping, and otherwise at most every 5 minutes.

### Widget
An embed titled "Bump Tracker" listing each site as either `✅ Ready to bump!` or `⏰ Xh Ym remaining`, with the footer "Use /bump log <site> after bumping to reset the timer." Color follows the guild accent (blurple fallback). When nothing new was posted to the channel the widget is edited in place (avoids the unread indicator); after a ping or auto-detected bump the old widget is deleted and re-sent so it stays at the bottom of the channel. The new message ID is persisted.

A site with no logged bump counts as ready.

### `/bump log`
Validates the site name against the guild's configured sites, upserts the bump timestamp (clearing the notified flag), confirms ephemerally, then refreshes the widget in place — only when the guild is enabled *and* has a channel (`should_post_widget`). With reminders switched off the bump is still recorded and nothing is written to the channel.

### `/bump status`
Renders the same widget embed ephemerally to the invoker. No writes.

### Auto-detection
Sites may carry a detector: a listing bot's user ID, an optional success pattern (`detector_pattern`) and an optional failure pattern (`failure_pattern`). Eligibility is `should_detect`: the message must be in the configured channel, which is the only channel watched — but **not** gated on `enabled`, because the panel's switch promises bumps keep being recorded while reminders are off. The message is then matched against each detector site — author must equal `detector_bot_id`, then the message text is classified (`bump_tracker/detector_logic.py`):

1. If `failure_pattern` is set and appears in the text, the message is a **refused** bump. It is logged server-side and otherwise ignored — no bump recorded, no widget refresh, cooldown keeps running off the last real bump.
2. Otherwise, if `detector_pattern` is empty **or** appears in the text, it is a **successful** bump: logged automatically, and the widget force-resent to the bottom of the channel when reminders are on.
3. Otherwise the site does not claim the message, and the next detector site is tried.

The failure check runs first because refusals routinely echo the success wording ("you can bump again…"), and because every guild configured before this existed uses an empty `detector_pattern`, which matches anything the bot posts.

Matching is case-insensitive and substring-based, against everything readable on the message:

- plain `content`
- every embed's title, description, footer, author name, and field names/values
- **Components V2** text — nested `text_display` components, walked depth-first through `container` / `section` children and section accessories

Components V2 matters because several listing bots deliver an empty `content`, zero embeds, and all their visible text in components — DH Bump for both outcomes, and Discadia for its successes. Before this was read, no pattern could match those messages at all.

**Ephemeral replies are never seen.** DISBOARD answers a rejected bump with an ephemeral interaction response, which Discord does not deliver to other bots. There is nothing to detect and no failure pattern to configure; a refused DISBOARD bump simply leaves the timer untouched, which is already correct.

## User-visible errors

| When | The user sees |
|---|---|
| `/bump log` with an unknown site | "No site named **{name}** found." |
| `/bump status` with no sites configured | "No sites configured. Add sites from the web dashboard." |
| Widget shown with no sites configured | Embed body: "No sites configured. Add sites from the web dashboard." |

Failures to send the ping or widget (missing channel, HTTP errors) are logged server-side and silently skipped.

## Configuration

All configuration lives in the web dashboard — **Config → Server → Bump Tracker**
(`panels/config-bump-tracker.js`, backed by `PUT /config/bump-tracker` and
`/config/bump-tracker/sites/...`); there are no config slash commands.

The panel shipped 2026-07-23. Before that the endpoints existed but nothing
called them, so the only way to set the feature up was editing the database by
hand — which is how the live guilds were configured. The panel covers the full
API surface: reminder channel and ping role, the master toggle, per-site
cooldown and detector, adding and removing sites, and recording a bump. It also
shows each site's live status and counts down to the next one becoming ready.

- **Channel** — where pings and the widget are posted; the feature is inactive until set.
- **Role** — pinged when a site becomes ready (optional).
- **Enabled** ("Send Bump Reminders") — governs the pings and the live widget only (default on). Recording continues either way: `/bump log` and auto-detection both keep writing bump rows while it is off, which is what the panel hint says and what the loop, the manual path and the detector now all agree on.
- **Per site**: name, cooldown in seconds, and a **Detection** cell holding the optional detector bot ID, "Text when bumped" (`detector_pattern`) and "Text when refused" (`failure_pattern`).

Until 2026-07-28 the panel sent `detector_pattern: ""` on every site save, so it
could neither show nor set a pattern, and editing a site's cooldown or bot ID
silently wiped one that had been set by hand. Both patterns are now editable and
round-trip through the panel.

The `/bump` command group defaults to requiring **Manage Server**.

## Stored data

SQLite, migrations `044_bump_tracker.sql`, `047_bump_tracker_detector.sql` and
`137_bump_tracker_failure.sql`:

- `bump_tracker_config` — per guild: `channel_id`, `role_id`, `widget_message_id`, `enabled`.
- `bump_tracker_sites` — per (guild, site): `cooldown_seconds`, `detector_bot_id`, `detector_pattern`, `failure_pattern`.

- `bump_tracker_log` — per (guild, site): last `bumped_at` (unix timestamp) and `notified` flag. Only the latest bump per site is kept — no history. Removing a site also deletes its log row.

Migration 137 also back-fills `failure_pattern` for every existing row whose
`detector_bot_id` is Discadia, Discodus or DH Bump — all three announce refusals
publicly, and every such row was configured with an empty `detector_pattern`
(match anything), so their refusals were being recorded as bumps. Only the veto
is seeded, never `detector_pattern`: a wrong veto degrades to the old behavior,
whereas a wrong success pattern would stop detection outright.
