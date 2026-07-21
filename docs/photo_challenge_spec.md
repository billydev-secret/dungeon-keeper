# Photo Challenge

**Flavor: Reference** — matches current behavior.

## Purpose

A scheduled photo prompt for one dedicated channel: on its schedule the bot
posts a rendered "PHOTO CHALLENGE" card with a prompt from the bank, members
reply by posting their photos in the channel, and the economy pays them for
posting. **There is no slash command** — Photo Challenge left the `/games`
party-games suite and is scheduled-only, configured entirely from its own
dashboard section (see History).

## How a round runs

1. **Schedule fires.** Photo schedules are ordinary `games_scheduled` rows with
   `game_type='photo'`, polled by the shared `scheduled_games_loop`
   (`scheduled_games_service.py`, 60s tick). The loop's generic guards apply:
   channel-busy skip (`skipped_active`, with give-up grace for `once` rows),
   an enabled re-check via `check_game_enabled` — the panel's **Enabled**
   toggle, `games_game_config.enabled` — producing `skipped_disabled`, and
   claim-before-launch so a crash mid-launch can't double-fire.
2. **Launch.** The loop calls `PhotoCog.launch` (`games_photo_cog.py`,
   registered in `bot.game_launchers["photo"]`; the cog registers no
   commands). It pulls a random prompt via `get_photo_prompt`
   (`question_source.py`) — **bank-only, no AI fallback**; an empty bank logs
   a warning and skips the run, posting nothing.
3. **Card.** The prompt is rendered by `render_quote_card`
   (`quote_renderer.py`, `golden_meadow` theme, header text
   `PHOTO CHALLENGE`) over the **guild icon**, falling back to the schedule
   creator's avatar. The card posts as a bare image (`photo.png`); if a ping
   role is configured, the message content is that role mention
   (`AllowedMentions(roles=True)`).
4. **No live game state.** The play is recorded to history fire-and-forget:
   `create_game` → `update_session` → `end_game` immediately. Members just
   post photos in the channel afterwards; nothing tracks "the round" beyond
   the history row, and payouts (below) are per-post, not per-card.

**NSFW:** prompt selection passes `allow_nsfw=channel_allows_nsfw(channel)` —
gated on Discord's own `channel.is_nsfw()` (threads inherit the parent;
unresolvable channels are treated as SFW).

## Economy integration

Two stacking payouts fire from `EconomyCog._on_photo_post` (an `on_message`
listener in `economy_cog.py`) when a member posts an **image attachment**
(`_has_image_attachment`: content-type, filename-extension fallback) in the
configured photo channel. Both are capped once per member per **guild-local
day**; posting several photos pays each side once. Reactions/replies are
irrelevant — the post itself earns.

1. **Flat participation award** — `EconSettings.reward_photo_post` (default 5,
   `0` = off), credited via `apply_credit(kind="photo_post")` with the booster
   multiplier. Dedup rides an `INSERT OR IGNORE INTO econ_photo_rewards
   (guild_id, user_id, local_day)` anchor inside the credit's transaction
   (mirrors the login faucet).
2. **`photo_post` quest bonus** on top, if a quest with that trigger kind is
   active — `fire_trigger_quests(..., "photo_post", occurrence=<local_day>)`,
   scoped to the photo channel, deduped on `econ_quest_claims`. Quest cadence
   applies: a weekly `photo_post` quest pays once/week, not once/day.

Guards run cheapest-first: guild/bot → image → TTL-cached channel id
(`_photo_channel`, 60s TTL over `games_game_config`) → `_photo_eligible`
pre-check (economy enabled, `photo_post` income source on, and something to
pay: positive flat award or ≥1 active `photo_post` quest). The **`photo_post`
income-source toggle gates both payouts**; the mechanic is **dormant until a
channel is configured** (`channel_id` unset ⇒ listener no-ops).

Feedback is a reaction on the member's photo: the quest outcome carries ✅
(paid) / 📝 (sign-off card filed, `_announce_quest_claim`); the flat award
adds a ✅ only when no quest fired. No channel reply, no DM.

The separate `media_post` trigger (`_on_media_post`) is a different, unscoped
mechanic — it fires for images in *any* channel per its own quest scoping and
is not part of Photo Challenge.

## Configuration

Dashboard nav **Photo Challenge** (`app.js`; admin perms, and visible to the
game-host role — every `/api/photo-challenge` route requires
`require_game_host`):

- **Setup & Schedule** (`panels/photo-challenge.js` + `routes/photo_challenge.py`,
  mounted at `/api/photo-challenge`):
  - *Setup*: dedicated **channel**, optional **ping role**, **Enabled** toggle.
    Stored in `games_game_config` under `game_type='photo'` (`enabled` column;
    `channel_id`/`ping_role_id` in the `options` JSON) — the same row the
    scheduler's enable-gate, the cog's `launch()`, and the economy listener
    read, so no dedicated table exists. Saving a channel repoints all existing
    photo schedules at it.
  - *Schedules*: once / daily / weekly (weekday set), time in guild-local
    `HH:MM` (`compute_next_run` with `get_tz_offset_hours`). Rows are created
    with the channel forced from config (400 until one is set), `announce=0`.
    Row actions: pause, resume (recomputes `next_run_at`), **run now** (sets
    `next_run_at=now`; fires on the next poll, reusing the busy/disabled
    guards), edit, delete. Last-run status is shown per row (`launched`,
    `skipped_active`, `skipped_disabled`, `skipped_giveup`, `error`).
  - An inline **Prompt Bank** section (`mountGamePanel`, `game_type='photo'`).
- **Prompts & AI** (`panels/games-studio.js` with `gt: "photo"`) — the full
  bank studio for curating prompts.

Economy knobs live on the economy pages, not here: the flat
`reward_photo_post` rate on **Economy → Income Sources**
(`economy-income-sources.js`, "Photo Challenge post (flat award)") alongside
the `photo_post` source on/off toggle; the stacking bonus is a `photo_post`
quest in **Economy → Quests**.

### API summary (`/api/photo-challenge`, all `require_game_host`)

| Route | Purpose |
|---|---|
| `GET/PUT /config` | channel_id, ping_role_id, enabled |
| `GET/POST /schedule`, `PUT/DELETE /schedule/{id}` | CRUD (photo rows only; 404 on other game types) |
| `POST /schedule/{id}/pause` · `/resume` · `/run-now` | row actions |

## Stored data

No feature-specific tables beyond the payout anchor — everything else reuses
games/economy infrastructure:

- `games_game_config` (`game_type='photo'`) — enabled + options JSON
  (`channel_id`, `ping_role_id`).
- `games_scheduled` (`game_type='photo'`) — schedule rows; hidden from the
  shared scheduler UI (`photo` is deliberately absent from
  `SCHEDULABLE_GAME_TYPES` in `games/constants.py`, and
  `routes/scheduled_games.py` filters listings to that set).
- `games_question_bank` (`game_type='photo'`) — the prompt bank.
- `games_game_history` — one row per posted card (via `create_game`).
- `econ_photo_rewards(guild_id, user_id, local_day)` — migration
  `101_econ_photo_rewards.sql`, the flat award's once-per-day dedup anchor.
- Trigger-kind renames: `079_photo_react_trigger.sql`
  (`photo_reply` → `photo_react`) and `099_photo_post_trigger.sql`
  (`photo_react` → `photo_post`) rewrite `econ_quests.trigger_kind` and
  `econ_income_sources.source` in place. The reply-era `econ_photo_cards`
  table still exists but is **unused**.

## Non-goals / History

Photo Challenge moved out of the `/games` menu, help list, and shared
scheduler (its icon is deliberately absent from `GAME_ICONS`; `GAME_NAMES`
keeps the display name for logs) — see the departure note in
`games_system_spec.md`. There is no winner, voting, or judging: payout is
participation-based, per post. The payout model evolved reply-gated
(`photo_reply`, mig 068) → reaction-gated (`photo_react`, mig 079) →
post-gated (`photo_post`, migs 099/101); the retired listeners
(reaction counting, auto-react seeding, `react_threshold`/`auto_react`
config) are gone from code. Full history and decisions:
[plans/photo-challenge-post-payout.md](plans/photo-challenge-post-payout.md).
