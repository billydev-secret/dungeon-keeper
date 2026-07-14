# Implementation plan — Chat Revive ("Ember")

**Status:** v1 built (stages 0–4, 2026-07-14). Stages 2–4 landed together in
one commit at Billy's request ("let the whole thing rip") instead of the
per-stage live-testing cadence below; the shared sync helpers live in
`chat_revive_loop.py` and the cog imports them, so the loop commit precedes
the cog in spirit. Live verification is queued in `docs/TESTING_QUEUE.md`.

**Spec:** `docs/chat_revive_spec.md` (added at Stage 0; source: product spec 2026-07-14).
Commits are tagged `Chat Revive (stage N): …`. Each stage is built in a worktree,
`scripts/gate.py` green, spec + `docs/INDEX.md` + `docs/TESTING_QUEUE.md` updated in the
same commit, then merged to main for live testing before the next stage starts.

## What we're building on (recon summary)

- **No new message ingest.** `processed_messages` (`xp_system.py`) already records
  `(guild_id, channel_id, user_id, created_at)` for every human message regardless of
  storage level, with full history and no pruning. It is the rhythm-learning source and
  the "did conversation follow?" source. It has **no channel-leading index** — our
  migration adds `(guild_id, channel_id, created_at)`.
- **Loop pattern:** startup task factory (`bot.startup_task_factories.append(...)` in
  `__main__.py`, `_resilient_task` wrapper), plain `while not bot.is_closed()` poller like
  `scheduled_games_loop`. All SQLite via `asyncio.to_thread` (deep-review S1-loop).
- **Busy gate:** `get_active_game(bot.games_db, channel_id)` + every entry in
  `bot.game_busy_checks` (covers in-memory games like Risky Roll).
- **Guild-local time:** `get_tz_offset_hours` + `economy/logic.py` `local_day_for` /
  `local_day_bounds`. (Note: main guild has no tz row and inherits global −7 — setup
  command must surface the computed local time so a wrong offset is caught immediately.)
- **Bank shape:** hybrid of `econ_quests` (guild-owned, `active` flag, `created_by`) and
  `legitlibs_templates` (`use_count`, `last_used_at`); JSON `tags` with reserved `nsfw`
  + `channel_allows_nsfw()` gating (`games/utils/question_source.py`).
- **No self-role system exists** despite the spec's assumption — see Stage 4.
- **Cog registration is manual:** add to `extension_names` in `__main__.py` (S3-extdrift).

## Layout

```
src/migrations/073_chat_revive.sql        # tables + processed_messages channel index
src/bot_modules/chat_revive/
    __init__.py
    logic.py            # pure: gap stats, band math, fire decision, question weighting
    starter_pack.py     # ~60 seed questions, tagged by category
src/bot_modules/services/
    chat_revive_service.py   # CRUD: config, bank, events; rhythm profiler; selection
    chat_revive_loop.py      # silence monitor + gates + post + 30-min follow-up measure
src/bot_modules/cogs/chat_revive_cog.py   # /revive command group (thin)
tests/test_chat_revive_logic.py
tests/test_chat_revive_service.py
tests/test_chat_revive_loop.py
tests/test_chat_revive_cog.py
```

## Data model (migration 073)

- `revive_guild_config` — `guild_id PK, enabled, role_id, quiet_start (0), quiet_end (8),
  daily_budget (3), guild_gap_minutes (90), flourish_enabled (1)`.
- `revive_channel_config` — `(guild_id, channel_id) PK, enabled, categories TEXT '[]',
  ping_enabled, role_id_override, rest_hours (8), fire_multiplier (4.0)`.
- `revive_questions` — `id AUTOINCREMENT, guild_id, text, category, nsfw INTEGER,
  active INTEGER 1, created_by, created_at, use_count 0, last_used_at REAL`.
- `revive_events` — `id, guild_id, channel_id, question_id, message_id, trigger
  ('auto'|'manual'), pinged INTEGER, local_day TEXT, created_at REAL,
  measured_at REAL NULL, follow_msgs INTEGER, follow_authors INTEGER,
  success INTEGER NULL`. This table alone answers every frequency gate (daily budget =
  COUNT per local_day; guild gap = MAX(created_at); channel rest = MAX per channel;
  ping scarcity = MAX(created_at) WHERE pinged per channel per local_day) — no separate
  state table.
- `revive_channel_rhythm` — `(guild_id, channel_id, band) PK, median_gap, p90_gap,
  msgs_per_day, sample_gaps, computed_at`. Cache refreshed by the loop every ~6 h per
  channel; also read by `/revive check` for the plain-language explanation.
- `CREATE INDEX IF NOT EXISTS idx_pm_channel_ts ON processed_messages
  (guild_id, channel_id, created_at)` — required before any per-channel gap queries;
  one-time build on the ~516 MB live DB will slow the first restart (testing-queue note).

## The fire decision (logic.py, pure functions)

Bands = 2-hour local buckets (12/day). Profile per band over a trailing 60 days:
median gap, p90 gap, msgs/day, gap count. A band with < 30 sampled gaps falls back to
the whole-day profile; a channel with < 14 days of history runs **fallback mode**
(fixed 6 h silence threshold, fires only 10:00–22:00 local).

Auto-fire requires ALL of:
1. silence ≥ max(`fire_multiplier` × median_gap(band), p90_gap(band)) — "several times a
   normal gap, and longer than almost any lull in this band";
2. band msgs/day ≥ liveness floor (default ≥ 20% of the channel's best band, min 5/day)
   — "the channel is normally alive right now";
3. a human message exists after the channel's last revive (`processed_messages` only
   records humans, so this is one indexed MAX() comparison) — "never talks to itself";
4. every protection clear: channel rest, guild daily budget, guild gap, quiet hours
   (guild-local), slowmode (`channel.slowmode_delay > 0`), busy checks, channel enabled.

`decide(...)` returns a verdict object — either "fire with question X" or the first
blocking reason as a human-readable string — so the loop and `/revive check` share one
code path and the preview can never disagree with reality.

Question selection: filter to channel's categories, `nsfw` only when
`channel_allows_nsfw(channel)`, exclude `last_used_at` within 30 days (guild-wide);
weight by Beta-smoothed success `(successes + 1) / (uses + 2)` from `revive_events`, so
proven sparkers surface and duds fade without hard deletion.

## Stage 0 — Foundation: schema + pure logic

- Add `docs/chat_revive_spec.md` (the product spec) and register it in `docs/INDEX.md`
  as a Design spec (`Notes: Stage 0 built`).
- Migration `073_chat_revive.sql` (tables + index above). Re-check the next free number
  at commit time.
- `chat_revive/logic.py`: gap-stat computation from raw timestamp lists, band bucketing
  with tz offset, fallback-mode rule, full `decide()` gate chain, selection weighting.
  No Discord or DB imports — everything takes plain values (`now_ts` injected, the
  economy-loop test convention).
- `tests/test_chat_revive_logic.py`: band math across tz offsets, sparse-band fallback,
  cold-start mode, each gate blocking individually, weighting distribution sanity.

## Stage 1 — Service layer: bank, config, rhythm profiler

- `chat_revive_service.py`: config CRUD; bank CRUD (add / bulk-add / retire / list) with
  attribution; `starter_pack.py` seeding (only into an empty guild bank);
  `refresh_rhythm(conn, guild_id, channel_id, now_ts)` computing band profiles from
  `processed_messages` into `revive_channel_rhythm`; `pick_question(...)`;
  `record_event(...)`. All sync-SQLite functions (callers wrap in `to_thread`).
- `tests/test_chat_revive_service.py` over `sync_db_path` + real migrations: seeding
  idempotence, NSFW filtering, 30-day anti-repeat, rhythm rows from synthetic message
  histories, event-derived budget/gap/ping queries.

## Stage 2 — Cog: /revive commands

- `chat_revive_cog.py`, registered in `__main__.py` `extension_names`.
  Group `/revive` with `@app_commands.default_permissions(manage_guild=True)` +
  `ctx.is_mod` inline (repo norm):
  - `setup` — pick/create role, set quiet hours + budget, seed starter pack, echo the
    guild's computed local time as the tz sanity check.
  - `channel` — enable/disable a channel, set its category mix, ping on/off, per-channel
    rest/role overrides.
  - `check [channel]` — "would it fire right now?": renders the `decide()` verdict —
    current lull vs. the band's normal rhythm, the blocking protection if any, and the
    question it would pick. Status embed uses `resolve_accent_color`.
  - `fire [channel]` — manual revive: runs selection + posting, skips lull detection,
    still respects ping scarcity; recorded as `trigger='manual'`.
  - `question add / bulk / list / retire` — subgroup; bulk accepts a text attachment
    (one question per line, `category:` prefix optional).
- `tests/test_chat_revive_cog.py` with `fake_interaction` / `FakeChannel`.

## Stage 3 — The loop: monitor, post, measure

- `chat_revive_loop.py`, registered via `bot.startup_task_factories`; ~120 s cadence.
  Each tick, per enabled channel: refresh rhythm if stale (> 6 h), evaluate `decide()`,
  post on fire. Every DB call through `asyncio.to_thread`.
- Posting (the zero-embarrassment section):
  - Plain text: optional rotating flourish + optional role mention + question. Ping only
    if channel ping-enabled AND no ping there this local day.
  - `allowed_mentions=discord.AllowedMentions.none()` plus exactly the revive role when
    pinging — nothing else can ever be mentioned.
  - Immediately before send: `channel.history(limit=1)` re-check that the newest message
    is neither ours nor newer than the evaluated silence (defeats ingest-lag and
    tick-race double-posts); a per-channel asyncio lock guards the evaluate→send window.
  - Insert into `revive_events` before send-confirmation is *not* enough — write the row
    with the sent `message_id` after a successful send; a send failure records nothing
    and the gates naturally retry later.
- Follow-up measurement: each tick, events with `measured_at IS NULL` and
  `created_at + 1800 < now` get `follow_msgs` / `follow_authors` counted from
  `processed_messages` in `(created_at, created_at+1800]`; `success = follow_msgs ≥ 3
  AND follow_authors ≥ 2`; bump question stats.
- `tests/test_chat_revive_loop.py`: economy-loop style (`now_ts` injection, hand-rolled
  bot/channel stubs) — fires on a genuine lull, refuses on each protection, never fires
  twice across overlapping ticks, measurement marks success/dud correctly, crash-replay
  between send and record doesn't double-post (history re-check catches it).

## Stage 4 — Opt-in role surface, scoreboard, polish

- **Opt-in role:** the spec assumes "the existing self-role flow" — none exists in this
  bot. v1 ships `/revive optin-post`: posts a persistent join/leave button message for
  the configured role (booster-roles `add_dynamic_items` pattern). If the guild already
  manages the role via another bot's role menu, admins simply skip this command.
- `stats` subcommand (mirrors `economy_stats_service.py` shape): revives/week per
  channel, success rate (30-min window), top/bottom questions by smoothed success, role
  member count over time if cheap.
- Flourish rotation list + per-guild off switch; question attribution surfaced in
  `question list`.
- Flip `docs/INDEX.md` entry toward Reference as behavior stabilizes.

## Cross-cutting rails

- **No sync SQLite on the event loop** (deep-review S1-loop): every loop/cog DB touch is
  `await asyncio.to_thread(...)`. No new `on_message` listener anywhere in the feature.
- **Zero-embarrassment invariants get their own tests** (spec: one violation costs more
  than fifty good revives): never two bot revives without an intervening human message;
  never a ping twice per channel-day; never a post inside quiet hours; each encoded as a
  direct test, not incidental coverage.
- Rewards/economy integration is deliberately **not** wired in (deep-review progression
  stance: no farmable rewards on engagement surfaces until safety gaps close).
- Coverage floor stays put — the pure `logic.py` split is what keeps this cheap.
- Open-findings check: this plan touches `__main__.py` and migrations only among
  reviewed areas; S3-migrate (idempotent migrations) and S3-extdrift (extension list)
  are both addressed above.

## Deliberately deferred (per spec §out-of-scope)

- Web dashboard panel for bank/settings (command-managed at launch).
- AI-generated or context-aware questions.
- Per-member ping preferences beyond the single opt-in role.
- Any reply/reaction/follow-up behavior on the bot's own revive.
- "Your question revived chat" contributor moment (attribution is stored, surface later).
