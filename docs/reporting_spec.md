# Reporting — Feature Spec

Reporting is the analytics backbone of the dashboard. A handful of small services — interaction tracking, voice-follow capture, incident detection, invite attribution, and the member quality score — produce the data; the dashboard renders it as charts and tables. The Discord surface is empty apart from an unrelated `/invite` command that returns the bot's install URL — the `/quality_leave` group, and the leave-of-absence concept it managed, were removed 2026-07-28.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/invite` | Slash | Everyone | Returns the bot's OAuth install URL. Unrelated to invite attribution |
| Dashboard report tiles | Web | Admin | Read-only analytics surfaces — see Behavior |
| Message Review panel | Web | Mod | Filter and inspect past messages by author, channel, content, sentiment, and reply chain — see Behavior |
| Cache clear | Web | Admin | Drop every cached report payload for the active guild |

The bot needs **Manage Server** to read invite codes for attribution. When missing, invite attribution degrades to "no inviter detected" and **logs a warning on every refresh and every unattributed join** — no other report is blocked. The collector (2026-07 rework, `invite_tracker.py`) diffs cached invite snapshots on each join: it drains join bursts one use at a time, attributes consumed single-use invites via their disappearance (Discord deletes them before the use count is ever observable), and keeps the cache current through `on_invite_create` / `on_invite_delete`. Attribution misses (vanity URL joins, diff races) are logged, never silent.

## Behavior

### Dashboard report tiles

Every report is admin-only, GET-only (cache clear is the single POST), and read through a per-route cache keyed by guild + parameters. Most tiles use a 60-second TTL; heavier tiles that scan the message archive (quality score, time-to-level, activity drop-off) use 5 to 10 minutes. An hourly background warmer additionally precomputes the default-parameter view of the heavy tiles (including the quality score) so a cold page load rarely pays the compute; non-default parameter combinations still compute on demand. The cache only invalidates on TTL expiry or explicit clear — there are no realtime pushes.

Day-bucketed charts roll over at the guild's local 6 am, not midnight. Names on every row are resolved live from the guild cache when the bot is online and fall back to the historical name archive when offline; some tiles (role listings, guild-wide inactivity) return a service-unavailable error when the bot is offline since they depend on live role membership.

The **Community Health** panel (`/api/health/*`) has its own cache — the DB-backed `health_metrics_cache` table, 15-minute TTL, keyed by `(guild_id, metric_key)`. As of 2026-08 every surface uses it, not just the grid: the sentiment feed and the sentiment outliers joined their sibling tiles, and the deep-dive endpoints (dau-mau, heatmap, channel-health, gini, social-graph, sentiment, sentiment-feed, newcomer-funnel, cohort-retention, mod-workload, mod-engagement) now cache under a `deep:` key prefix — a deep dive returns a superset of its tile (per-channel breakdowns, full graphs, 50-row feeds), so the two shapes must never share a key. Every parameter that changes the population is part of the key (`+bots` for `include_bots`, `|days=N` for the mod-engagement window). **Deep dives are therefore up to 15 minutes stale**, where they used to recompute per click. Display names are the exception: they are resolved after the cache read and never stored, so a rename shows up on the next request rather than after the TTL. `clear_cache(conn, guild_id)` drops tile and deep-dive entries together.

**A degraded payload is never cached.** `mod-workload`, `mod-engagement`, `newcomer-funnel` and `cohort-retention` derive their `mod_ids` / `recent_joins` inputs from the **live** guild object. While the bot is mid-startup, the gateway member cache is cold, or the bot isn't in the guild, that member list is empty and those metrics compute to zeroes. The zeroed payload is still returned (a blank tile beats an error), but it is not written to `health_metrics_cache` — otherwise a few seconds of startup would be served as fact for the next quarter of an hour. `_guild_extras` reports this as `degraded` (no non-bot member visible; every real guild has at least an owner) and `_cache_unless_degraded` is the only writer for those four metrics, on both the tiles and deep-dive paths. Because the cache is read before compute, an existing good value keeps being served through the whole degraded window. Metrics that read only the database (dau-mau, heatmap, gini, social-graph, sentiment, sentiment-feed, channel-health) are unaffected and cache as normal.

Sentiment reads go against the `messages` table's own `sentiment` / `emotion` columns (indexed `(guild_id, sentiment)`), not the `message_sentiment` side table — the two are written by the same code paths and carry identical data, but only `messages` is indexed for the queries the panel actually runs. As of 2026-08 this covers **every** reader on the health hot path, `compute_sentiment` in `health_metrics.py` included. The side table is still written (ingest + backfill) and still read by `reports_data.py` and the CLI backfill in `dungeonkeeper/__main__.py`; nothing has been dropped. One trap: the old join supplied `sentiment IS NOT NULL` implicitly, since a `message_sentiment` row only exists for a scored message. `AVG` skips NULLs on its own, but `COUNT(*)` does not, so every query reading `messages` must state the predicate or unscored messages inflate `scored_count`, the per-channel counts, and the negative-spike `HAVING cnt >= 3`.

**Bots are excluded from every metric by default** (2026-07). Bot traffic was ~21% of stored messages, so counting it made every message-volume number wrong by up to a fifth; the worst single case was a channel that was 99% one bot. `bot_filter_clause()` in `bot_modules/core/bot_exclusion.py` is the single source of truth — a `NOT IN (SELECT user_id FROM known_users WHERE guild_id=? AND is_bot=1)` fragment appended to a query's `WHERE`. Every affected route takes `include_bots: bool = False` to opt back in; the health cache namespaces bot-inclusive payloads under a separate key (`cache_key()`) so the two variants can't poison each other.

**A channel metric counts channels, not thread ids** (2026-08). Every per-channel aggregate groups `messages` by `channel_id`, which for a message posted in a thread is the *thread's* own id — so threads showed up as rows in views meant to answer "which channels are alive": 243 of the 367 rows in a 30-day window were not channels. `services/channel_rollup.build_resolver()` is the single source of truth, and every channel-grouped surface folds through it: `channel-comparison`, `channel-health`, the heatmap's per-channel breakdown, and the homepage top-five. Its rules, in order — an ephemeral bot-made channel is dropped; a current guild channel counts as itself; a thread counts toward its parent; anything else is dropped.

Threads are **attributed, not excluded**: the conversation belongs to the channel it started in, so the parent's volume, unique authors, Gini, depth and last-activity all include its threads. That is why the merged figures are recombined in Python from per-`(channel, author)` rows rather than taken from SQL — summing distinct-author counts would double-count anyone active in both, and averaging two averages would weight a three-message thread like its thousand-message parent. Ordering and any `LIMIT` must be applied *after* folding.

The ephemeral families are identified from their own registries, never by name-guessing: `pen_pals_sessions` (which keeps closed rows, so even a long-deleted pairing stays recognisable), `voice_master_channels`, and `jails`. The bios wizard is the sole exception — it keeps no registry, so its `bio-<user id>` rooms are matched by name.

Nothing in the schema distinguished a thread before this, so `known_channels` gained `parent_id` / `is_thread` (migration 163), written at ingest from discord.py's `Thread.parent_id` — an attribute no other channel type has, so a text channel can never be rolled into its category. History was recovered in two passes: the migration's offline self-join (a thread started from a message takes that message's id, so the parent is recoverable wherever the starter was archived — about a fifth of them), then `scripts/backfill_thread_parents.py` walking the live guild's active and archived threads. **Whatever neither pass reached is dropped, not shown** — roughly 3% of a 30-day window's messages sit in deleted threads and removed channels. Channel *names* are unaffected: they still resolve from the guild cache first, `known_channels` second.

One degraded mode: with no guild cache (bot offline, guild uncached, or a channel list still empty because the gateway hasn't filled it — every real guild has at least one channel, so empty means "don't know", never "none") the resolver cannot check which ids are current, so it classifies from the database alone and **keeps** what it cannot rule out — a stale row beats an empty panel reading as a broken report. Threads still reach their parents; ids known to be threads with no parent still drop.

`known_users` is the only source consulted. `/api/reports/activity` previously scanned live `guild.members` for `.bot` plus a `guild_config` allowlist — that missed bots which had left the server, and it is gone. Authors with no `known_users` row count as human: in prod all 40 such accounts are departed members (39 of 40 have XP rows, and no bot has ever earned XP). XP was already human-only, so XP totals are unaffected.

Not filtered, deliberately: moderation paths (`rules_watch/*`, `ai_moderation_service`) that analyse message content, the ingest layer (`message_store`), privacy deletion, and the sentiment backfill writer. `games_external` and `bump_tracker` exist to read bot messages and are untouched. Mod workload/engagement need no filter — they are already scoped to a mod-id list.

Tiles group into a few areas:

- **Activity** — the merged **Channels** panel (per-channel health status/score over 30 days plus a windowed comparison of messages / XP / sentiment / trend — both `/api/health/channel-health` and `/api/reports/channel-comparison` feed it), top voice users, generic activity (messages or XP) with user/channel exclusions and a **Show Bots** opt-in. (The finer-grained message-rate/cadence/burst experiments were removed in the 2026-07 reports cleanup.)
- **Membership health** — join-time histogram, cohort retention, NSFW-channel activity grouped by recorded gender, activity drop-off profiles, and the merged **Inactive Report** (one member list over last-activity data: scoped to everyone / role holders / role non-holders, filtered by idle days — 0 lists the whole scope oldest-first — optionally measured within one channel; logic in `inactive_report_service.py`).
- **Greeter performance** — greeter response time and missed joins, derived from the configured greeter chat channel and welcome / leave audit.
- **XP** — top-N leaderboard for a window, days-to-level-5 histogram, and a generalised days-to-level-N report (level 2–100). Source data is owned by [[xp-spec]].
- **Interaction graph** — force-directed network of replies and mentions, read by two panels off the one `/reports/interaction-graph` endpoint: **Interactions** (`interaction-graph`) renders it as sortable tables plus a bar chart, and **Connection Graph** (`connection-graph`, restored 2026-08-26 after the 2026-07 cleanup removed it) renders the canvas map and the `include_metrics=1` block — clustering coefficient, density, reciprocity, avg path length, small-world quotient, bridge users, isolates and the cross-cluster matrix — none of which the tables surface. Any interaction touching a bot on either endpoint is excluded, so a member replying to a bot never reads as a one-sided relationship — the exclusion is applied in the queries (`get_interaction_graph_data`) so the interaction-graph tables and the Health **Social Graph** metrics share it. Recorded bots (see State) still have their raw interactions logged; they're just filtered out at report time.
- **Invite effectiveness** — per-inviter table of active invitees joined through them.
- **Quality score** — the Member Quality Score table (described below).
- **One-Sided Attention** — lopsided, unreciprocated attention between member pairs, for moderator review (described below).

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

**Regex search runs under rails.** A pattern can't be pushed into SQL, so it is matched row by row inside the bot's own process — and CPython's regex engine holds the GIL for the whole of a single match, so one catastrophic pattern would stall the Discord gateway, not just the request. Four limits apply, and each one that trips returns a 400 explaining which:

- **Pattern shape.** A repeat nested inside a repeated group (`(a+)+`, `(x?)*`, `([a-z]+)*`) or a repeated group whose branches overlap (`(a|ab)*`) is refused outright — those are the exponential-backtracking shapes. So are patterns over 300 characters, more than 12 unbounded repeats, or a `{n,m}` bound above 200. Ordinary patterns (`(cat|dog)+`, `\d{3}-\d{4}`, `https?://\S+`) are unaffected.
- **Match input** is capped at 4096 characters — Discord's own message ceiling, so nothing real is lost.
- **Rows scanned** are capped at 50,000 and **matches retained** at 5,000. Results are streamed in batches and only matches are kept, rather than pulling every message in the guild into memory first.
- **Wall clock** is capped at 5 seconds across the whole scan; blowing it returns "narrow your filters" instead of a partial answer.

When a row or match cap stops the scan early the response carries `truncated: true`, so the reported total is a floor rather than the whole answer.

### Incident detection

A per-process velocity tracker keeps a 10-minute sliding window of message rate per guild. Against a 30-day baseline (mean + standard deviation per hour-of-day × day-of-week, refreshed every 15 minutes), a velocity spike fires when the current rate is at least mean + 3·stddev **and** above 5 messages per minute. Severity is `critical` past 1.5× the threshold, otherwise `warning`. The same guild can't emit another velocity incident within 5 minutes.

A join raid fires when at least 3 accounts younger than 7 days join within a 2-minute window. Severity is always `critical`. Incidents are stored for the health-metrics tiles to read.

### Invite attribution

The bot caches the current `uses` count for every guild invite at startup and refreshes per join. When a member joins, the bot diffs the live invite list against the cache; the first code whose `uses` ticked up is recorded as the inviter on that join. If two joins land in the same window, only one inviter is detected — the rest record without an attribution. Re-joins after a leave never overwrite the original inviter.

### Member quality score

A whole-server score in `[0, 1]` computed over a rolling 90-day window from four sub-scores:

1. **Engagement Given (40%)** — average percentile of reaction-rate and reply-ratio (replies under 5 characters don't count). Multiplied by an initiative multiplier (0.85× to 1.10×) based on what fraction of pair interactions the member started. Anti-gaming: serial reactions to the same author on the same day get half credit after 5 and zero after 10.
2. **Consistency & Recency (25%)** — 60% recency (exponential decay since last seen) + 40% consistency (active weeks divided by min of weeks-in-window or weeks-since-join, so newcomers aren't penalised for short tenure).
3. **Content Resonance (20%)** — mean reactions + replies received per "post" (an attachment or a non-reply conversation starter). Non-posters get the neutral percentile 0.5.
4. **Posting Activity (15%)** — daily-capped attachments + conversation starters per active day. Non-posters get a percentile floor of 0.25.

Status precedence: `Onboarding` (under 7 days tenure) → `Insufficient Data` (under 7 active days) → `Active`. Onboarding and insufficient rows are scored 0 and sort to the bottom. A `Leave of Absence` status existed until 2026-07-28; it was removed along with `/quality_leave`, its only writer, having never been used in production.

Tenure buffer adds 30 days at 6 months and 60 days at 12 months to the inactivity threshold, surfaced on each row so reviewers can see why a long-tenured quiet member isn't flagged.

### One-sided (unreciprocated) attention

A moderator-review report (**Reports → People**, mod-gated) that surfaces candidate member **pairs** where one person (the *initiator*) directs sustained, lopsided attention at another (the *target*) who does not reciprocate. It is triage for a human to glance at — explicitly *a tip, not a verdict* — and never drives automated action. The window is configurable (default 30 days, clamped 7–180); rows return at most 100.

Three directed signals are unioned over the window, each weighted by how strongly it reads as pursuit:

- **replies + mentions** — `user_interactions_log`, weight 1.0.
- **reactions** — `reaction_log`, weight 0.5 (the weakest single cue).
- **voice-follows** — `voice_follow_log`, weight 2.0 (joining a voice channel the target is *already* in — the strongest "showing up where they are" shape). Capture is direction-aware and noise-guarded in `voice_follow.py`: joining an empty channel records nothing, joining a crowd (> 6 already present) is treated as a party not pursuit, and leave/rejoin flapping into the same channel is debounced within 10 minutes.

Reactions and voice-follows are **live-forward only** (no historical backfill), so early on a report is text-dominated — expected, not a bug.

**Gating.** A pair surfaces only when combined weighted volume in both directions clears a **volume floor** (15) *and* directional **asymmetry** — `w(A→B) / [w(A→B)+w(B→A)]` — reaches the **asymmetry cut** (0.85). Volume alone isn't diagnostic (mutual friends are high-volume too); the separators are direction, fixation, and escalation after the target goes quiet.

**Evidence, not a score.** Rather than collapse everything into one number (which would acquire authority it hasn't earned — the COMPAS anchoring failure), each flagged pair exposes its components as chips: percent one-directional, whether the target *ever* responded in-window, an escalation ratio (initiator's contact rate after vs. before the target's last reciprocal action), attention concentration and distinct-target count (Herfindahl index), voice-follow count, and the biggest burst (most events within a 10-minute span). Benign-reading **cautions** are attached alongside — e.g. a small social circle (few distinct targets may just be a quiet user with one friend), cooling contact (escalation < 1), or mostly-reactions (reads as ordinary support). Ordering is transparent — never-reciprocated pairs first, then escalating ones, then asymmetry, then volume — never a hidden rank.

**Gender-neutral by design** — the report never uses or infers gender; it surfaces the *shape* of lopsided attention and leaves the meaning to the human.

**Bots excluded on either endpoint.** `get_one_sided_attention_data` reads the recorded-bot set from `known_users` (`is_bot = 1`) and passes it as the report's `exclude_ids`, so a member reacting to or following a bot never surfaces as a lopsided pair, and bot targets don't inflate a member's concentration/distinct-target evidence.

## Permissions

- `/invite` — open to everyone.
- All dashboard report routes — admin tier, **except** the One-Sided Attention report, which is mod-tier (`require_perms({"moderator"})`) to match its investigative purpose.
- Message Review panel + its export — mod tier. Mods who have Discord's Manage Messages permission qualify automatically.
- Bot-side: no Discord permissions are required to read reports. Invite-cache refresh needs **Manage Server**; a missing perm is a soft degrade, not an error.

## User-visible errors

| When | The user sees |
|---|---|
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
- **No verdict from the One-Sided Attention report.** It surfaces evidence for a human to judge — it never emits a single black-box score, ranks pairs behind a hidden number, infers gender, or triggers any automated action.

## Configuration

Reporting reads guild config but owns very little of its own. The dashboard reads:

- The XP-excluded channel list (passed through to activity-by-XP).
- The recorded-bots list (lets activity tiles include or exclude their messages).
- Greeter role, greeter chat channel, welcome / leave / join-leave log channels — for the greeter-response tile.
- The NSFW role — for the oldest-SFW-members tile.

Reporting owns no configuration of its own.

## Stored data

Per-guild and per-user: a directed interaction tally between every (from, to) pair plus an append-only interaction log (with the source message id) so day-windowed graphs can be reconstructed; directed voice-follow capture (an aggregate weight per ordered pair plus a timestamped log, migration 117) feeding the one-sided-attention report; per-join invite-attribution rows (one per invitee, never overwritten); an append-only incident log for velocity spikes and raid attempts; a per-hour-of-day × day-of-week baseline of message velocity, refreshed in the background. No filesystem cache — chart payloads are JSON returned through the per-route memory cache. The velocity tracker and invite cache are per-process in-memory state and rebuild from the database on restart.
