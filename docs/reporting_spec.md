# Reporting — Feature Spec

Reporting is the analytics backbone of the dashboard. Four small services — interaction tracking, incident detection, invite attribution, and the member quality score — produce the data; the dashboard renders it as charts and tables. The Discord surface is nearly empty: one mod slash group for leave-of-absence management, plus an unrelated `/invite` command that returns the bot ' install URL.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/quality_leave add member:<m> days:<30|60|90>` | Slash | Mod | Mark a member on leave; quality reports treat them as `Leave of Absence` until the term ends |
| `/quality_leave remove member:<m>` | Slash | Mod | Clear an active leave row |
| `/quality_leave list` | Slash | Mod (ephemeral) | Show the active leave roster with remaining days |
| `/invite` | Slash | Everyone | Returns the bot ' OAuth install URL. Unrelated to invite attribution |
| Dashboard report tiles | Web | Admin | Read-only analytics surfaces — see Behavior |
| Message Review panel | Web | Mod | Filter and inspect past messages by author, channel, content, sentiment, and reply chain — see Behavior |
| Cache clear | Web | Admin | Drop every cached report payload for the active guild |

The bot needs **Manage Server** to read invite codes for attribution. When missing, invite attribution silently degrades to "no inviter detected" — no other report is blocked.

## Behavior

### Dashboard report tiles

Every report is admin-only, GET-only (cache clear is the single POST), and read through a per-route cache keyed by guild + parameters. Most tiles use a 60-second TTL; heavier tiles that scan the message archive (quality score, time-to-level, interaction heatmap, dropoff, chilling-effect) use 5 to 10 minutes. The cache only invalidates on TTL expiry or explicit clear — there are no realtime pushes.

Day-bucketed charts roll over at the guild ' local 6 am, not midnight. Names on every row are resolved live from the guild cache when the bot is online and fall back to the historical name archive when offline; some tiles (role listings, guild-wide inactivity) return a service-unavailable error when the bot is offline since they depend on live role membership.

Tiles group into a few areas:

- **Activity** — message rate, message cadence by hour/day/week, role-growth, channel comparisons, top voice users, reaction givers/receivers, burst sessions, session-burst around a single member, generic activity (messages or XP) with user/channel/bot exclusions.
- **Membership health** — join-time histogram, cohort retention, NSFW-channel activity grouped by recorded gender, members inactive ≥ N days, the oldest members without the NSFW role, message-rate-drops, dropoff.
- **Greeter performance** — greeter response time and missed joins, derived from the configured greeter chat channel and welcome / leave audit.
- **XP** — top-N leaderboard for a window, days-to-level-5 histogram, and a generalised days-to-level-N report (level 2–100). Source data is owned by [[xp-spec]].
- **Interaction graph** — force-directed network of replies and mentions, plus an animated adjacency-matrix heatmap by day or week.
- **Invite effectiveness** — per-inviter table of active invitees joined through them.
- **Quality score** — the Member Quality Score table (described below).
- **Chilling effect** — members whose arrival in a channel correlates with others going quiet.

### Message Review

A mod investigation panel in the dashboard's Moderation section. Filter past messages by:

- **Author** — multi-select chips. Picking two authors returns messages from either (OR), not both.
- **Channel** — multi-select chips, same OR semantics.
- **Content** — free-text search.
- **Mentions** — single member.
- **Reply to** — single member (find replies to that member's messages).
- **Sentiment / emotion** — optional filter to the badges that decorate each row.
- **Date range**.

Each result row shows author + channel name, content (truncated), timestamp, sentiment and emotion badges, and a jump link to the original Discord message. Pagination + sort by timestamp work as you'd expect.

Mods can also issue a **natural-language query** ("messages from alice or bob in #general about cake last week"); the AI parses it into author / channel / content / date filters and pre-populates the chips. The mod can tweak the chips and re-run.

A separate **Export** button downloads the current result set as a CSV. Both the panel and the export are mod-gated; admin isn't required.

### Incident detection

A per-process velocity tracker keeps a 10-minute sliding window of message rate per guild. Against a 30-day baseline (mean + standard deviation per hour-of-day × day-of-week, refreshed every 15 minutes), a velocity spike fires when the current rate is at least mean + 3·stddev **and** above 5 messages per minute. Severity is `critical` past 1.5× the threshold, otherwise `warning`. The same guild can ' t emit another velocity incident within 5 minutes.

A join raid fires when at least 3 accounts younger than 7 days join within a 2-minute window. Severity is always `critical`. Incidents are stored for the health-metrics tiles to read.

### Invite attribution

The bot caches the current `uses` count for every guild invite at startup and refreshes per join. When a member joins, the bot diffs the live invite list against the cache; the first code whose `uses` ticked up is recorded as the inviter on that join. If two joins land in the same window, only one inviter is detected — the rest record without an attribution. Re-joins after a leave never overwrite the original inviter.

### Member quality score

A whole-server score in `[0, 1]` computed over a rolling 90-day window from four sub-scores:

1. **Engagement Given (40%)** — average percentile of reaction-rate and reply-ratio (replies under 5 characters don ' t count). Multiplied by an initiative multiplier (0.85× to 1.10×) based on what fraction of pair interactions the member started. Anti-gaming: serial reactions to the same author on the same day get half credit after 5 and zero after 10.
2. **Consistency & Recency (25%)** — 60% recency (exponential decay since last seen) + 40% consistency (active weeks divided by min of weeks-in-window or weeks-since-join, so newcomers aren ' t penalised for short tenure).
3. **Content Resonance (20%)** — mean reactions + replies received per "post" (an attachment or a non-reply conversation starter). Non-posters get the neutral percentile 0.5.
4. **Posting Activity (15%)** — daily-capped attachments + conversation starters per active day. Non-posters get a percentile floor of 0.25.

Status precedence: `Leave of Absence` (active leave row) → `Onboarding` (under 7 days tenure) → `Insufficient Data` (under 7 active days) → `Active`. Onboarding / insufficient / leave rows are scored 0 and sort to the bottom.

Tenure buffer adds 30 days at 6 months and 60 days at 12 months to the inactivity threshold, surfaced on each row so reviewers can see why a long-tenured quiet member isn ' t flagged.

## Permissions

- `/quality_leave *` — Mod role, re-checked inside each handler. The Discord default-perms flag (Manage Server) is a UI hint, not the gate.
- `/invite` — open to everyone.
- All dashboard report routes — admin tier. There is no read-only or mod-only tier on report endpoints.
- Message Review panel + its export — mod tier. Mods who have Discord's Manage Messages permission qualify automatically.
- Bot-side: no Discord permissions are required to read reports. Invite-cache refresh needs **Manage Server**; a missing perm is a soft degrade, not an error.

## User-visible errors

| When | The user sees |
|---|---|
| `/quality_leave *` invoked by non-mod | "You don ' t have permission to use this command." |
| `/quality_leave list` in DM | "This command only works in a server." |
| Generalised time-to-level requested with level outside 2–100 | HTTP 400 |
| Greeter-response asked for a period with no resolvable greeters | HTTP 404 "No greeter response data found for the selected period." |
| Role-listing or guild-wide inactivity tile while bot is offline | HTTP 503 "Guild not available." |
| Other report tile while bot is offline | Tile renders with archive-only names (no error) |
| A report tile worker raises | HTTP 500; the cache entry is not stored so the next request retries fresh |

## Non-goals

- **No realtime dashboard updates.** Every tile is poll-driven; cache TTL is the freshness floor.
- **No per-channel quality scores.** Quality score is server-wide; per-channel rollups belong to other tiles.
- **No write endpoints on report tiles.** Only the cache-clear endpoint is non-GET, and it never touches data tables.
- **No precise invite tracking.** Concurrent joins race the cache diff and may mis-attribute or fail to attribute.
- **No historical baseline retention.** The 30-day rolling baseline overwrites older numbers; there is no audit of how baselines drifted.
- **No incident review UI.** Velocity spikes and raid attempts are stored but only surfaced through health tiles — there is no dedicated dashboard list or slash command.
- **No automatic expiry of leave-of-absence rows.** They are re-classified as `Active` on the next score run; nothing deletes them.

## Configuration

Reporting reads guild config but owns very little of its own. The dashboard reads:

- The XP-excluded channel list (passed through to activity-by-XP).
- The recorded-bots list (lets activity tiles include or exclude their messages).
- Greeter role, greeter chat channel, welcome / leave / join-leave log channels — for the greeter-response tile.
- The NSFW role — for the oldest-SFW-members tile.

The only configuration owned by reporting itself is the leave-of-absence roster managed via `/quality_leave`.

## Stored data

Per-guild and per-user: a directed interaction tally between every (from, to) pair plus an append-only interaction log (with the source message id) so day-windowed graphs can be reconstructed; per-join invite-attribution rows (one per invitee, never overwritten); an append-only incident log for velocity spikes and raid attempts; a per-hour-of-day × day-of-week baseline of message velocity, refreshed in the background; and the leave-of-absence roster. No filesystem cache — chart payloads are JSON returned through the per-route memory cache. The velocity tracker and invite cache are per-process in-memory state and rebuild from the database on restart.
