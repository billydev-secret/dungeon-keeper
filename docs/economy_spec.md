# Economy & Perk Shop — Spec V3.1 (repo-grounded)

**Brandable per-guild currency · Logins & streaks · XP conversion · Quests · Rentable perks**
*Status: partially shipped — Stages 0–3 built (wallets/ledger/config, faucets,
quests, and transfers + the rental engine + role perks + gifts). Metrics (Stage 4),
soak/tuning (Stage 5), and private rooms (Stage 6) plus the v2 member dashboard and
spotlight slots remain design-only. Implementation plan in
`docs/plans/economy-and-perk-shop.md`. Supersedes the uploaded V3 draft. All numbers
are per-guild-tunable defaults (§9).*

This revision replaces V3's assumptions with the systems Dungeon Keeper actually has.
The load-bearing change: **currency does not get its own activity tracker** — it converts
from the existing XP ledger (`xp_events`), so one system counts activity and the economy
inherits its anti-grind damping and voice rules.

---

## 1. Overview

A per-guild economy module. Each guild brands its currency and runs an
interaction-driven economy with four income streams — daily logins with streaks,
daily XP→currency conversion, quests, and game/QOTD rewards — spent on
weekly-renewing perks (role customization, private rooms) and social sinks
(gifting, spotlights, transfers).

**Design pillars** (unchanged from V3, with one amendment)
1. Interaction is the currency; voice presence counts.
2. Per-guild identity and tuning, informed by live metrics (§9).
3. Rentals, not purchases — weekly auto-renew keeps demand recurring.
4. Wide income spread: baseline regular ≈ 100–120/wk; hyper-active ceiling ≈ 1,000–1,200/wk
   (anchors are now *targets to tune toward* with real `xp_events` data, not derived from
   fixed rates — see §3.2).
5. Boosters are patrons: 1.5× on all faucet credits; same shop as everyone.
6. ~~No earn throttling~~ → **Inherited damping.** The XP ledger already applies
   cooldown/duplicate/pair-streak multipliers to text XP. The economy inherits them by
   design; no *additional* economy-specific throttles are added.

## 2. Branding Configuration (per guild)

Dashboard → Economy → Settings: `currency_name`/plural, `currency_emoji`,
`currency_icon_url`, `wallet_name`, `bank_channel_id`, `spotlight_channel_id`,
`manager_role_id`, `game_role_id`, `transfers_enabled`, `enabled`.
Dashboard → Economy → QOTD: `qotd_ping_role_id` (§3.4).

- Stored as `econ_*` keys in the shared per-guild `config` table, loaded into a frozen
  `EconSettings` dataclass (same pattern as `XpSettings` / `load_xp_settings`,
  `src/bot_modules/core/xp_system.py`). Per-guild with **no** `guild_id=0` legacy fallback
  (`allow_legacy_fallback=False`) so non-primary guilds get true defaults.
- Neutral defaults ("Coins", 🪙) pre-branding. All member-facing strings template these.
- Embeds use `resolve_accent_color(db_path, guild)` per repo convention; currency emoji
  is decorative, not semantic color.
- **Feature gate:** economy is off until `enabled` is set AND `bank_channel_id` is
  configured (Pen Pals pattern). Prod DK is multi-guild; each guild opts in separately.

## 3. Faucets (Earning)

All credits flow through a single `apply_credit()` in the economy service: it applies
the **booster ×1.5** (checked via `member.premium_since`, as `booster_roles.py:196` does),
rounds up, writes the wallet balance and an append-only ledger row atomically.

### 3.1 Daily Login
- **Trigger:** first qualifying activity of the guild-local day —
  a counted message (hooks the existing `EventsCog.on_message` pipeline) **or** a
  qualifying voice session reaching 5 minutes (piggybacks the 30s voice-XP poll tick and
  the existing `voice_sessions.qualified_since`; same qualification as voice XP:
  ≥2 humans, non-AFK; deafened still counts — decided).
- **Day boundary: guild-local midnight** via existing `tz_offset_hours`
  (`get_tz_offset_hours`), not UTC. V3's §12 "midnight flip" edge case is thereby removed.
  The main guild needs an explicit tz row set at rollout (it currently inherits global −7).
- **Reward:** text login 5 base; **voice login 15 base** (voice deliberately richer —
  decided). +1 per consecutive day, streak bonus capped at +10.
- **Grace:** one free missed day per rolling 7, automatic and silent; second miss resets.
- **Milestones:** day 7 → +25 · day 30 → +100 · day 100 → +365 · +100 each 100 after.
- Idempotency: `INSERT OR IGNORE` on `(guild_id, user_id, local_day)` — one login/day
  no matter how many events race (birthday-announcement pattern).

### 3.2 XP → Daily Conversion
Members earn XP exactly as today (`xp_events` ledger: text per-word, replies,
image-reacts, voice 1.67/min with ≥2 humans). At guild-local midnight, each member's
XP earned that local day converts to currency.

- **Conversion: `econ_xp_per_coin` XP → 1 currency** (default 15), rounded down;
  fractional remainder carries to the next day (stored on the conversion row).
- V3's flat XP table (3/post etc.) is **dropped**; the real rates rule. Reference with
  real rates: a 2-hour qualifying voice hangout ≈ 200 XP ≈ 13 coins at the default rate.
  The conversion rate and all XP coefficients are the scaling parameters in the config
  menu (§9); income anchors are tuned there against the metrics card, not hardcoded.
- **New XP source `reaction_given`** (live, Stage 1): reacting to someone else's message
  pays the *reactor* XP — default 0.34 (double the existing image-react rate), tunable via
  `xp_coeff_reaction_given_xp` on the dashboard XP panel. Feeds regular XP/leaderboards
  *and* (via conversion) currency. Guards: no self-reactions, no bots, one award per
  (message, reactor) ever (dedup table), so react/unreact can't farm. See [[xp-spec]].
- Conversion is silent (ledger entry: "Daily activity"). Runs from the economy hourly
  loop when a guild's local day rolls; idempotent via a per-(guild, user, local_day)
  conversion row.

### 3.3 Quests — per §4. Daily 10–20 · weekly 25–75 · community flat payout.

### 3.4 Interaction Rewards
- **QOTD (built, manual):** a moderator runs `/qotd post <question>` in the target
  channel. The bot renders the question as a banner card (`render_quote_card`, the same
  renderer `ffa_banner` uses) and posts it. Every member who posts a non-bot message in
  that channel from post time until end of the guild-local day earns **10**, once per
  QOTD (dedup row per member). No scheduler — mods run it when they want.
  When `qotd_ping_role_id` is set (dashboard → Economy → QOTD), the post mentions that
  role; unset (the default) posts silently. The mention only notifies if the role is
  mentionable or the bot holds "Mention @everyone, @here, and All Roles" — Discord
  renders it as inert text otherwise.
- **Game participation 5:** paid at the party-games `end_game` choke point
  (`games/utils/game_manager.py`) from the session's player set, and at each duel cog's
  resolution point. Participation now covers **20 of 23 games**: the six duel games,
  ttl/traditional/legitlibs, and — enriched in Stage 2 — 11 party cogs that now pass
  their real player rosters into `end_game` (ama, clapback, compliment, hottakes, mfk,
  mlt, nhie, price, rushmore, story, wyr). ffa and fantasies are excluded by design
  (anonymous submissions); photo has no per-player completion hook either, but pays
  through the **photo-reply event quest** (§4.5) instead of `end_game`.
- **Game win +20:** paid for **both** game architectures in v1 (decided). Duel games
  read their explicit `winner_id` (chicken, hot potato, musical chairs, pressure
  cooker, quickdraw, …). Party games get a per-game-type winner resolver over the
  `end_game` payload — the "best moment" extraction in `games_session/logic.py:85-118`
  (NHIE guiltiest, TTL best-liar, hottakes hottest, …) is the template. Game types
  with no meaningful winner pay participation only.
- **Event host 30 (mod grant):** `/bank grant @member amount reason` + Operations
  page button; manager-role or admin gated; audit-tagged in the ledger.

## 4. Quest System

### 4.1 Authoring (Quests page, gated on `economy_manager_role` or admin —
mirrors the `games_editor_role` / `require_game_host` dashboard pattern)
Fields: title, description, type (daily/weekly/community/event), reward, sign-off
tickbox, criteria (freeform v1), date range, repeat/auto-rotate tag, and a
**completion mode** — member claims it / trigger phrase (§4.4) / game trigger
(§4.5) — phrase and game trigger being mutually exclusive. Rewards free-entry with
an amber out-of-band warning; out-of-band saves fine, audit-tagged. Counted
trigger quests take a **target count** ("how many times", §4.5). Library model:
each of daily/weekly/monthly is a **pool** of active quests (capped at
`POOL_CAP = 25` each) that the **per-user board** (§4.6) draws from, **plus 1
active event quest per trigger kind** per guild. (The former "1 active daily +
rotate-tag pool" rule is retired — `rotate_tag`/`rotate_pool` still exist but
are inert once every pool quest is active; the per-user board supplies the
variety rotation used to.) Authoring
lives on the **Quests** page (library w/ pool summary + inline edit → **Board
size** dials (§4.6, admin-only — the section is read-only prose for
manager-role holders, since `GET/PUT /economy/config` is admin-gated) → quest
editor → AI ideas) with in-place editing (PUT); the sign-off inbox is the
**Claims** page (pending queue with Approve/Deny plus a state filter over the
paid/denied/expired history); the remaining operational cards — community
goals → grant → rentals → ledger, with member pickers for grant/ledger and a
ledger kind datalist — live on the **Operations** page.

**AI idea generator.** The New-quest form has a "Generate ideas" button
(`POST /api/economy/quests/generate`, manager-gated) that batches suggestions for
the selected quest type. It uses the **Anthropic cloud path** — the same
`bot_modules.games.utils.ai_client.generate_text` the party-game studio uses, *not*
the local moderation LLM — and prompts for in-band rewards (daily 10–20, weekly 25–75)
plus a `community_target` for community goals. Ideas render as clickable cards; picking
one loads title/description/criteria/reward into the form. **Nothing is persisted** —
a suggestion is inert until the manager reviews and submits it. Prompt building and the
tolerant JSON parser (fenced-array / leading-prose / title-only fallbacks) live in
`bot_modules/economy/quest_ai.py`; the prompt text is hardcoded for v1 (editable-prompt
parity with the Games Studio is a parking-lot item).

### 4.2 Member Flow
- `/bank quests` + wallet page: active quests, progress, claim state.
- **Claims are period-keyed.** A daily's period is the guild-local day (`YYYY-MM-DD`),
  a weekly's is the ISO week (`YYYY-Www`), community is `once`. Partial-unique indexes on
  `(quest_id, user_id, period)` permit at most one `pending` and one `paid` row per
  period, so re-claimability falls out of the key — a member is claimable again the next
  local day / ISO week with no reset sweep. Instant quests insert `paid` and credit in
  one transaction; sign-off quests insert `pending`.
- **Instant quests:** claim → immediate payout (ledger kind `quest`).
- **Sign-off quests:** claim → a card in the bank channel carrying Approve/Deny as
  persistent `DynamicItem` buttons (`custom_id` `econ_claim:approve|deny:<claim_id>`,
  re-registered in `cog_load` so they survive a restart — no per-message view store).
  Approve pays and DMs; Deny opens a reason modal, DMs the reason, and leaves the period
  re-claimable. One pending claim per quest per member; a claim left pending **>7 days**
  expires via the hourly loop (`expire_stale_claims`), DMs the claimant, and frees a
  re-claim. **Approve/Deny works from both surfaces** — the bank-channel card and the
  Claims page's pending queue resolve the same claim; a dashboard resolution also
  best-effort edits the card and DMs the claimant over the shared event loop.

### 4.3 Community Quests
Guild-wide objective with a progress bar; never member-claimable. Two flavors
since stage 3 of the quest-variety plan
(`docs/plans/quest-variety-and-community-weeklies.md`):

**Manual (no trigger kind — the original).** A manager drives `current`
toward the target from the Operations page and `completed_at` stamps once on
the crossing. **Payout: flat, to every member active in the last 30 days**
(`member_activity` via `active_member_ids`). Settlement is exactly-once: a
per-(quest, user) row in `econ_community_payouts` is reserved before
crediting (wellness-scheduler pattern), so a replay pays only the members it
missed and `settled_at` stamps last. **Sign-off gates the sweep:** a
sign-off community quest settles ONLY via the dashboard's manual Settle
(`settle_community_quest`); a plain one auto-settles on the weekly ISO-week
roll (`list_settleable_community_quests` filters out sign-off AND
kind-carrying quests).

**Auto-tracking community weeklies (`trigger_kind` set).** The kind's module
events advance the counter guild-wide — `fire_trigger_quests` bumps every
active same-kind community quest (`_bump_community_kind`), deliberately NOT
filtered by personal boards, with per-member rows in `econ_community_contrib`
(migration 082). Lifecycle is scheduler-owned, **one week on / one week off**
(`_roll_community_weekly` at the ISO-week roll, alternation state in
`econ_day_marks.last_community_week`):

- **Activation** (first roll after a full gap week, or ever): the library's
  least-recently-run inactive kind community quest (`next_community_weekly`,
  `last_run_week` ordering) activates with a **fully automatic target** —
  trailing 28 days of that kind's `econ_kind_activity` ÷ 4 ÷ 0.75
  (`community_auto_target`, floor 10), so a typical week lands ~75% and a
  push clears it. No manual override by design (2026-07-18 decision).
- **Tiers at 40/70/100%** (`quests.COMMUNITY_TIERS`): settlement at the
  closing week roll pays the quest's flat `reward` once per crossed tier to
  every 30d-active member — exactly-once per run via
  `econ_community_tier_payouts` (tier 0 reserves the **top-contributor
  bonus**: `reward // 2` to the top 3 by contribution). Contribution and
  tier-payout rows reset at the next activation, so a re-run pays afresh;
  idempotency only has to hold within a run.
- **Beat sheets, not bot posts:** kickoff / tier-crossed / final-24h /
  resolution are **DMed to the community host**
  (`EconSettings.community_host_user_id`, 0 → guild owner) as numbers +
  suggested copy — the host narrates publicly in their own voice
  (2026-07-18 decision). Tier crossings and the final-24h nudge are detected
  hourly (`community_hourly_beats`; `notified_tier` / `final_notice_sent`
  advance in the same transaction, so each beat sends once).
- Kind community quests reject `signoff=1`, and the dashboard's manual
  progress/settle endpoints 422 on them (the Operations card renders them
  read-only with an "auto-tracking" note); the manual Settle path and the
  legacy completed-quest sweep are for manual quests only.

### 4.4 Trigger-Word Quests
A daily/weekly quest may carry **trigger words** (comma/newline-separated phrases,
`econ_quests.trigger_words`) and an optional **trigger channel**
(`trigger_channel_id`, NULL = anywhere; threads count toward their parent channel).
The message is the verification: when a member's message contains a phrase
(whole-phrase, case-insensitive, word-boundary-anchored — "gm" never matches
"dogma"; matching lives in `quests.parse_trigger_words` /
`compile_trigger_pattern`), the `EconomyCog` `on_message` listener claims the quest
on their behalf through the ordinary `claim_quest` state machine:

- **Instant quest:** pays on the spot — ✅ reaction + a reply embed naming the
  quest and payout.
- **Sign-off quest:** files the `pending` claim, posts the bank-channel card, and
  reacts 📝 — a manager still approves the payout.

**Game-role delivery.** When `game_role_id` is set, the completion card
(both the instant ✅ card and the sign-off 📝 card) is **DMed** to the
claimant instead of replied in-channel — the reaction still lands on their
message, but the embed goes via `notify_member` (DM, bank-channel fallback,
honors the notify mute). Members **without** the role are paid/filed
**silently** (no reaction, no reply, no DM). With `game_role_id` unset (0,
the default) the feature is off and every claimant gets the legacy in-channel
reaction + reply. The bank-channel sign-off card (manager approval) is posted
regardless of the claimant's role.

The same opt-in gate covers the **streak / milestone / grace / reset DMs**
(§3.1): when `game_role_id` is set, those notices only reach members who took
the role (`notify_member(..., require_game_role=True)`); everyone else keeps
earning silently. With no role configured the gate is inert (every earner is
notified). Transactional notices (rental billing) are *not* gated — they target
a member by their prior spend, not by opt-in.

Trigger quests are **excluded from the `/bank quests` claim select** (state
`trigger` on the wallet page) — self-claiming without saying the phrase would
bypass the verification. Repeats inside a period fall out silently via the
per-period claim collision, so a busy "good morning" channel never gets error
spam. Per-guild trigger quests are cached in the cog for **60 s**
(`_TRIGGER_CACHE_TTL`), so a dashboard edit takes effect within a minute without
a restart; the cache also stores empty lists, keeping the per-message cost of
guilds without trigger quests to a dict lookup. Community quests never trigger
(not member-claimable).

### 4.5 Game-Trigger Quests (`trigger_kind`)
A quest may carry a **trigger kind** — a custom-coded module hook that completes
it automatically (`quests.TRIGGER_KINDS`; mutually exclusive with trigger words).
The kind decides *what* completes the quest; the qtype decides *how often it can
pay*:

- **daily/weekly + kind:** "do it once today / this week" — the trigger
  auto-claims the ordinary calendar period. Never in the manual claim select
  (wallet state = the kind's how-to line).
- **event + kind** (`qtype='event'`, kind required): pays **every occurrence**,
  period `"<kind>:<occurrence>"` — once per member per game/card/round, forever,
  no time gate. `quest_period('event', …)` deliberately raises; only listeners
  build occurrence keys. Slot rule: **1 active event quest per trigger kind**
  (`can_activate_event`); different kinds coexist, and event quests occupy no
  daily/weekly/community slot. Not offered by the AI idea generator.

All firing funnels through `fire_trigger_quests` (service) — one member, one
kind, every matching active quest — riding the normal `claim_quest` machine, so
sign-off, booster multiplier, ledger kind `quest`, and per-period dedup come for
free. Repeats fall out silently on the claim collision. Kinds:

| kind | fires when | fired from | occurrence key |
|---|---|---|---|
| `photo_react` | a member's image post in the configured Photo Challenge channel draws `react_threshold` distinct human reactions (default 5; the author and bots never count) | `EconomyCog._on_photo_react` (raw-reaction listener; announces ✅/📝 — in-channel, or DM under `game_role_id`) | `photo_react:<local_day>` (once/day by construction) |
| `party_game` | party game completes with the member in the roster | `pay_game_rewards` via `game_manager.end_game` | `party_game:<game_type>:<game_id>` |
| `duel` | duel/PvP game resolves (chicken, hot potato ×2, musical chairs, pressure, quickdraw) | `pay_game_rewards` at each duel cog's resolution | `duel:<game_type>:<id>` |
| `risky_roll` | member presses Roll in a Risky Rolls round | `RiskyRollView.roll_button` → `fire_member_trigger` | `risky_roll:<game_id>` |
| `guess` | member submits a scored guess in a Guess Who round | `GuessSelectView._on_select` → `fire_member_trigger` | `guess:<round_id>` |
| `voice_session` | member earns voice-activity XP (anti-idle rules apply) | `voice_xp_service.process_voice_xp_tick` (inline conn) | `voice_session:<local_day>` (once/day by construction) |
| `qotd_reply` | member newly earns the QOTD flat award | `events_cog._econ_work` after `try_award_qotd` returns True | `qotd_reply:<qotd_id>` |
| `starboard` | a member's message first crosses the star threshold | `starboard_cog` first-crossing insert (bot authors excluded) | `starboard:<message_id>` |
| `invite` | a member the inviter recruited joins | `events_cog.on_member_join` beside `record_invite` | `invite:<invitee_id>` (rejoin never re-pays) |
| `boost` | member starts a server boost (`premium_since` None→set) | `EconomyCog._on_boost_started` (new listener) | `boost:<boost_start_ts>` |
| `bio_set` | member saves/updates their bio via the wizard | `bios/wizard._persist_sync` | `bio_set:set` (event = once ever) |
| `media_post` | member posts a message with an image; per-quest `trigger_channel_id` scopes it (threads count via parent) | `EconomyCog._on_media_post` (announces like photo; DM under `game_role_id`) | `media_post:<message_id>` — use daily/weekly cadence |
| `pen_pal` | two members are paired into a Pen Pals session (both fire) | `pen_pals_cog._do_pair` save | `pen_pal:<session_id>` |
| `message_sent` | any member message (channel-scopable) | `events_cog._econ_work` (same txn as login/QOTD) | `message_sent:<message_id>` |
| `reply_sent` | a Discord reply to someone ELSE's message (self-replies skipped; unresolvable references count) | `events_cog._econ_work` | `reply_sent:<message_id>` |
| `reaction_given` | reaction-given XP newly awarded (inherits the farm guard: one per message+reactor ever, no self-reacts) | `events_cog.on_raw_reaction_add` | `reaction_given:<message_id>` |
| `game_win` | winning a party game (only NHIE/TTL/Hot Takes resolve a winner) | `pay_game_rewards` winners pass | `game_win:<game_type>:<game_id>` |
| `duel_win` | winning a duel/PvP match | `pay_game_rewards` winners pass | `duel_win:<game_type>:<id>` |
| `duel_lose` | resolving a duel/PvP match without winning it (every participant minus the winner set) | `pay_game_rewards` losers pass | `duel_lose:<game_type>:<id>` |
| `confession` | member submits an anonymous confession (posts to the feed) | `confessions_cog.ConfessModal.on_submit` → `_fire_confession_trigger` (both forum + text paths) | `confession:<message_id>` — silent claim keeps the feed anonymous; only trace is the ledger |
| `ama_ask` | member's AMA question becomes visible: on submit (unfiltered) or on host approval (screened; rejected never pays) | `games_ama_cog` `AskQuestionModal.on_submit` + `ScreenedQuestionView.approve` → `_fire_ama_ask_trigger` | `ama_ask:<game_id>:<q_idx>` |
| `whisper` | member sends an anonymous whisper that is delivered | `whisper_cog.WhisperCog._send_impl` after the DM+feed post succeed | `whisper:<whisper_id>` |
| `quote` | member turns a message into a quote card via the make-it-a-quote role (the invoker is credited, not the quoted author; self-quotes never fire) | `quote_cog._on_quote_trigger` after the card posts | `quote:<quoted_message_id>` — mildly farmable, pair with daily/weekly + target count |
| `quoted` | someone ELSE quote-cards the member's message (the quoted author's passive twin of `quote`; self-quotes never fire) | `quote_cog._on_quote_trigger` beside the `quote` fire | `quoted:<quoted_message_id>` |
| `chat_revive` | member talks in the 30-min follow window after a Chat Revive prompt (every distinct human author fires; individual credit doesn't hinge on the collective success bool) | `chat_revive_service.measure_due_events` via `fire_trigger_inline` (measured-at NULL check keeps replays out) | `chat_revive:<event_id>` |
| `bump` | member bumps the server — `/bump log` (mod-gated) credits its invoker; an auto-detected listing-bot response credits `message.interaction_metadata.user` (migration 081 also stores the bumper on `bump_tracker_log`) | `bump_tracker_cog` both paths | `bump:<interaction_or_message_id>` — site cooldowns are the natural rate limit |
| `voice_room_host` | member's Voice Master room reaches 2+ non-bot guests with the owner present (once per room lifetime, in-memory set; restarts forgive but the occurrence key still blocks a same-room re-pay) | `voice_master_cog._handle_joined_tracked` | `voice_room_host:<channel_id>` |
| `pen_pal_complete` | a Pen Pals session reaches its natural 24 h expiry (both members fire; `early`/`channel_missing` closes never fire) | `pen_pals_cog` expiry sweep | `pen_pal_complete:<session_id>` |
| `whisper_guess` | member correctly guesses a whisper's sender (fires after the race-consumed check, so a two-tab solve pays once) | `whisper_cog._handle_guess_outcome` | `whisper_guess:<whisper_id>` |
| `guess_win` | member wins a Guess Who round (stretch twin of `guess`; fires only for the solve-race winner) | `guess_cog` solved path | `guess_win:<round_id>` |
| `session_join` | member appears in a game-night session's roster (end_game now merges the real roster into `games_session_tracker`, which start-time calls only seeded with the host) | `game_manager._fire_session_join` | `session_join:<session_id>` — later games in the same session collide silently |
| `voice_message` | member posts a voice message (fires before the transcription config gate — the quest is the post, not the transcript) | `voice_transcription_cog._on_message` | `voice_message:<message_id>` — use daily/weekly with a target count |
| `music_request` | member's `/play` adds ≥1 track | `music_cog.play` via `daily_occurrence=True` | `music_request:<local_day>` (once/day by construction — a 30-track playlist and 30 requests look the same) |
| `birthday_set` | member saves their birthday | `birthday_cog` modal submit | `birthday_set:set` (event = once ever, the `bio_set` pattern) |
| `level_up` | member's level-up is announced (announce-time, not award-time, so quest-XP payouts can't recurse into another claim; a silently-won level fires when its announcement lands) | `xp_service.handle_level_progress` via `fire_trigger_inline`, one fire per delivered level | `level_up:<level>` |
| `ama_answer` | hot-seat answers a question in their own AMA | `games_ama_cog` reply-modal submit | `ama_answer:<game_id>:<q_idx>` — use daily/weekly with a target count |

**Kind activity ledger.** Every `fire_trigger_quests` call — before the
income-source switch and the personal-board filter — bumps
`econ_kind_activity` (migration 080: one row per guild/member/kind/local-day,
pruned to a trailing 70 days on the economy loop's day roll). It measures
what members actually *do*, not what happened to pay, and is the data source
for dynamic target sizing on both surfaces (personal trailing-period medians
and community guild-wide sums — plan
`docs/plans/quest-variety-and-community-weeklies.md`). Historical warm-up for
the xp_events-mirrored kinds is a stage-2 script job, not a migration
(local-day bucketing needs per-guild tz offsets).

`confession` quests reject `signoff=1` at creation/update: a sign-off claim
posts a bank-channel card naming the claimant, timing-correlatable against the
anonymous feed. (Community quests already forbid any trigger kind, so the only
paths left for confession are the silent daily/weekly/monthly/event auto-claims.)

**Monthly cadence:** `qtype='monthly'` claims once per guild-local calendar
month (period `"YYYY-MM"`, so the window opens on the 1st); up to 5 active, own
slot pool, suggested band 75–200, no rotation, and — like all cadences — needs
no loop work: the period key itself resets claimability.

**XP rewards:** `reward_xp` pays levelling XP alongside the coins on every
quest payout — instant and approved sign-off both flow through
`_credit_reward`, so an approved claim pays XP at approval, not filing. XP is
flat (no booster multiplier — that's a currency patron bonus; minting XP would
distort the level curve), ledgered as `xp_events` source `quest`. Level
progression lands in the DB; any level-up announces on the member's next
ordinary XP award.

**Onboarding path (removed 2026-07-18):** an earlier build DMed each new member
a "starter path" embed of the guild's `onboarding`-flagged quests on join. It
was deleted — a join-time DM pushes the economy at members who never opted into
the game role, contradicting the "role set = opt-in, members without it are
paid silently" model (unlike a member who joins the server, a member who takes
the role has opted in). No replacement fires on join; members discover the
library through `/quests`. The `onboarding` column and `econ_onboarding_dms`
table remain as inert dead schema (migration 071), no longer read or written,
and the quest editor's onboarding toggle is gone. Don't reintroduce a join-time
economy DM without a real opt-in signal.

**Counted quests:** a trigger-kind quest on a daily/weekly/monthly cadence may
set `target_count` > 1 ("send 20 messages this week"). Each distinct occurrence
inserts an `econ_quest_progress_marks` row (the dedup — replays never
double-count) and bumps `econ_quest_progress`; the ordinary claim fires when
the count reaches the target (see §4.6 for the per-member Gaussian target
band), and `/quests` shows a progress meter. Targets are
invalid on manual quests (nothing counts) and event quests (every occurrence
already pays). Progress is per (quest, member, period) — a new period starts a
fresh count with no reset sweep.

### 4.6 Per-user quest board (`quests.assigned_quest_ids`)
Daily/weekly/monthly quests are **personal**: each cadence's active quests form
a **pool**, and every member is shown/paid their own subset drawn from it per
period. How many is **per-guild configurable** — `EconSettings.quest_board_daily`
/ `_weekly` / `_monthly` (default **2** each, matching `PERSONAL_BOARD_SIZE`),
edited on the dashboard Quests page under *Board size* and capped at `POOL_CAP`.
Lowering a dial is how a guild makes the board feel less busy without
deactivating library quests; **0 turns that cadence off entirely** — nothing
shows and nothing pays.

Because 0 is a real setting, "has a board" and "board size" are deliberately
separate: `quests.has_board(qtype)` (true for daily/weekly/monthly, always) is
what gates the board filter, never `board_size(...) > 0`. Gating on the size
would make a cadence sized to 0 read as *unfiltered* — community/event's
"no board concept, every active quest counts" — and pay out the whole pool,
the exact inverse of the intent.

The draw is a pure function of `(pool_ids, user_id, period_index, board_size)` — a per-member
`sha256` shuffle of the pool walked N-at-a-time by `period_index(qtype, day)`
(day ordinal / ISO-week / month) — so it needs **no assignment table**: it is
stable within a period, differs between members, and spaces a member's repeats
roughly **⌊poolsize/N⌋ periods** apart (a full cycle when N divides the pool,
approximate otherwise — e.g. a 5-daily pool at N=2 recurs some dailies every 2
days; small pools where `N ≥ poolsize` degrade to "everyone sees everything"). Community and event quests are **not** personalized (community is a
guild-wide objective; event pays per occurrence).

Both surfaces filter to the member's board: `fire_trigger_quests` and the
trigger-word `on_message` path skip any daily/weekly/monthly quest not on the
board this period (so a member only *earns* a kind when its quest is on their
board), and `_load_quests_state` shows only the board on `/quests` + the wallet.
Because assignment cadence equals the claim period, counted progress never
fragments mid-period.

**Gaussian target band:** a counted quest may carry a target *band*
(`0 < target_min < target_max`) instead of a fixed `target_count`. Then each
member's target for a period is drawn from a Gaussian over `[min, max]`
(`quests.effective_target`), deterministic on `(user, quest, period)` — so it is
stable all period and varies member to member (the band is set from the p35–p85
historical percentile of that action, keeping targets in a "reasonable" range).
`0/0` (the default) means no band — the fixed `target_count` applies, so existing
quests are unchanged. Both the counted-claim path and the `/quests` progress
meter read the same `effective_target`.

Game-fired claims are **silent in-channel** (matching the participation faucet —
a game recap followed by a dozen quest embeds would be noise); the wallet ledger
and `/quests` carry the news, and sign-off claims still post the bank-channel
card. The photo-reply and media-post listeners announce (✅/📝 on the member's
own message — a 1:1 exchange). Hooks that fire inside another module's open
transaction use `fire_trigger_inline` (savepoint-wrapped, never raises, no
bank-card posting — pending sign-off claims still appear on the claims panel).

**Income Sources page** (Economy section of the dashboard): a per-guild
enable switch for every trigger kind, stored in `econ_income_sources`
(absent = enabled; the gate is checked once inside `fire_trigger_quests`, so a
disabled source stops firing everywhere immediately while its quests wait in
the library). The page also shows which quests use each source, the built-in
faucet rates (read-only for manager-role holders; admins edit them in place —
saves go through the admin-gated partial `PUT /api/economy/config`, and the
panel detects admin-ness by probing the admin-gated config GET), and the
suggested-sources roadmap (bump attribution, survey — no survey feature exists
in code yet —, invite retention, counted quests, monthly cadence; confessions
rejected for anonymity). JS labels shared with the quest form via
`economy-sources-shared.js`.

**Photo plumbing:** payout is reaction-gated on member posts, not replies to the
card. `EconomyCog._on_photo_react` (a `on_raw_reaction_add` listener) fires when
an image post in the configured photo channel has drawn `react_threshold`
distinct non-bot reactors other than the author. The expensive reactor fetch is
guarded: a TTL-cached channel check, a DB eligibility pre-check (economy on,
`photo_react` source on, ≥1 active `photo_react` quest), a raw-total prune, and a
per-process `_photo_paid` set that stops recounting a post once it has crossed.
The occurrence key is the guild-local day, so a member earns at most once per day
regardless of how many photos cross. The image check is content-type with a
filename-extension fallback. The channel is the standalone Photo Challenge
feature's dedicated channel — `channel_id` in `games_game_config.options` (game
type `photo`), owned by the **Photo Challenge → Setup** panel (`/api/photo-
challenge/config`); payouts and auto-react are dormant until it's set. This
feature adds two options to that same panel/blob: **`react_threshold`** (distinct
reactors, default 5) and **`auto_react`** (an emoji the bot seeds on each photo so
members can one-tap pile on — the bot's own reaction never counts). *(The old
reply model and its `econ_photo_cards` registry are retired; migration 079
renames existing `photo_reply` quests and income-source rows to `photo_react`.)*

`/bank pay @member amount` — min 1, whole numbers, no fee. **Confirmation step over
100** (an ephemeral confirm button before the debit lands). Both sides ledgered
(payer `transfer_out`, recipient `transfer_in`). Per-guild `transfers_enabled` toggle
(default **on**) is the kill switch for alt-funneling; `/bank pay` refuses with a
branded notice when it is off. **Transfers do not mint** — the recipient's
`transfer_in` credit takes **no** booster multiplier (the ×1.5 is a faucet-only patron
bonus); a transfer only moves existing currency between wallets. An optional
**memo** rides `/bank pay` — collapsed to a single trimmed line and length-capped,
stored verbatim under a `memo` key on both ledger rows and surfaced (escaped at
render time) in the wallet ledger and the dashboard bank-manager ledger.

## 6. Sinks (The Perk Shop)

**Shipped (Stage 3):** the role-customization perks (solid color, name, icon,
gradient) and **gift-a-color** are live — browsed, rented **and customised** in
`/bank shop`'s ephemeral panel (§7). Private rooms stay **Stage 6** and the
spotlight slot stays **v2** — both still design-only below.

Weekly rentals bill on personal anniversary tick. Defaults below; every price per-guild
tunable (§9). **Renewal bills the CURRENT guild price at each anniversary** — the
rent-time price is snapshotted only for week one; a price tuned in the config panel
takes effect on the next cycle, never retroactively.

| Perk | Per week | Repo grounding |
|---|---|---|
| Custom role color (solid) | 50 | `guild.create_role(color=…)` |
| Custom role name | 35 | 32-char, filtered via the voice-master name-blocklist matcher (shared table) |
| Role icon | 75 | Requires `ROLE_ICONS` in `guild.features`; upload utils exist in `booster_roles.py` |
| Gradient/holographic | 120 | **Capability confirmed**: `booster_roles.py` already sets `secondary_color` on create/edit; requires Enhanced Role Styles guild feature; supersedes solid |
| Private text room | 200 | §8 (Stage 2) |
| Private voice room | 200 | §8 (Stage 2) |
| Gift-a-color | 50 | Payer funds a friend's solid color |
| Spotlight slot | 150 flat | **v2 (decided).** Featured embed in `spotlight_channel_id`, buyer text through the name blocklist, 7-day expiry, 3/ISO-week inventory |

**Curated role-icon catalog (currency sink).** Alongside bring-your-own icon
uploads, an admin can stock a per-guild catalog of named role icons, each with its
own weekly price, from the **Sinks** dashboard page — which also now **owns the flat
perk prices** (moved off the Settings panel). When a catalog exists, `/bank shop`'s
role-icon row becomes a picker of curated icons (Discord caps the select at 25) instead
of a single flat-priced Rent button; choosing one rents or switches to it. It reuses the
existing `role_icon` rental perk and the personal-role projector — **no new perk kind
and no `econ_rentals.perk` CHECK change**: the rented catalog icon id is recorded on the
rental (`catalog_icon_id`; NULL = a legacy/bring-your-own rental at the flat
`price_role_icon`) and its image is projected as the role's `display_icon`. Billing
snapshots the icon's price at rent time and re-reads the **current** catalog price at each
renewal (like the flat perks), with a defensive fallback to the flat price if the row ever
disappears. Disabling an icon (`enabled = 0`) hides it from new renters without touching
current renters; a hard delete is blocked while any live rental points at the row, so a
member always keeps the icon they paid for. Because the projector diffs the role icon by
**presence only** (it can't read an uploaded asset's bytes back),
`econ_personal_roles.projected_icon_path` records what was last projected, so a member
*switching* from one icon to another forces the re-upload.

**Personal roles:** one per member, auto-created **positioned above the booster
cosmetic swatch band** (the "#### Cosmetics" anchor) so a rented color wins the
display-color contest — the position is set **on create only** (a reconcile never
re-hoists a manually moved role). The projector is idempotent: it reconciles the role
to the member's current entitlements (name / color / gradient / icon) and downgrades
cleanly when a component lapses. Guards: a **ΔE ≥ 25 collision check against staff role
colors** (a too-close color is refused, the message naming the staff role it clashes
with), and role **names run through the Voice Master name blocklist** (the shared
matcher/table). Icon perks gate on `ROLE_ICONS` and gradient on Enhanced Role Styles
(`ENHANCED_ROLE_COLORS`) in `guild.features`. The role is deleted when the member's last
role-perk lapses; a role-count alert fires near the 250 ceiling.

**Rental engine:** hourly billing loop → debit on `next_bill_at` → on failed debit,
**36h grace with hourly retries and one DM at entry** → revoke on expiry (`lapsed`),
re-purchasable. Anniversaries are **no-drift** (each renewal advances `next_bill_at` by
exactly one week off the scheduled time, so downtime never shifts the billing day) and a
multi-week catch-up after downtime charges **once**, not once per missed week.
**Suspension** (a guild losing `ROLE_ICONS`/Enhanced Role Styles mid-rental) **pauses
both the billing clock and the visual** — no charge accrues while suspended and the
perk auto-resumes cleanly (re-projected, clock restarted) when the feature returns.
Restart-safe exactly-once via claim-before-side-effect on the rental row
(`scheduled_games_service` pattern: state is advanced before the Discord side-effect),
with feature-loss re-projection and transition-only DMs handled as post-commit effects.
Dashboard cancel: an **active** rental runs to the end of the paid week (no refund); a
**grace** rental is cancelled immediately and best-effort de-projects the role (§12).
Leave/ban = immediate cancel + cleanup (member-remove listener), covering both rentals
the member owns and gift rentals where they are the beneficiary.

## 7. Member Surface

- **Discord is the entire v1 member surface** (decided — the member-facing dashboard
  wallet page and role studio are v2). One top-level group **`/bank`** (`wallet`,
  `pay`, `quests`, `shop`, `gift`, `role`, `mute`, `grant` [mod], `post-guide` [mod])
  plus `/qotd post` [mod]
  and rooms-stage `/room …` — keeps the bot's top-level command budget flat. Command
  names are global; all *strings* inside are currency-branded.
  - **`/bank pay @member amount`** — transfer (§5); **`/bank shop`** — one ephemeral
    panel that both browses and configures: unrented rows carry a **Rent** button,
    rented rows a green **customise** button opening the matching modal (name /
    color hex / gradient hexes / server-emoji icon), with icon/gradient rows
    reflecting the server's role features and rented rows marked ✅. A fresh rental's
    confirmation carries the same customise button, and a member holding only a
    *gifted* color gets a "Set gifted color" button. **`/bank gift @member
    <perk>`** — pay to rent a friend a solid color (eager role creation on the
    recipient).
  - Each modal setter applies the matching rented component to the member's personal
    role (§6), re-checking entitlements on submit, subject to the blocklist / ΔE /
    feature gates. Emoji icons accept **this server's custom emojis only** (typed
    `:name:` or pasted; the bot stores the emoji's image; animated refused).
  - **`/bank role icon image:`** is the one surviving subcommand — modals can't take
    file uploads, so image icons (256KB max) still arrive via slash command. The
    former `name`/`color`/`gradient` subcommands are removed in favour of the shop's
    modals.
- **Channel guide panel (shipped):** **`/bank post-guide [channel]`** [mod] posts a
  single branded "how it works" embed (a **Joining** field pointing members at the
  onboarding Channels & Roles screen via the `<id:customize>` mention to grab the
  economy-game role, then earning streams with live rates, shop prices, and a
  command crib sheet — all templated from `EconSettings`) into a channel. Panel ids
  persist as `econ_guide_channel_id` / `econ_guide_message_id` (Voice Master
  panel pattern): re-running in the same channel edits the panel in place (use after
  re-pricing/re-branding); pointing at another channel deletes the old panel and
  reposts. Builder in `economy/guide.py`; the two ids are bot-managed and not
  dashboard-editable. **Sticky:** an `on_message` listener keeps the panel as the
  last message in its channel — any message there (member chatter *or* the bot's own
  economy notices) arms a debounced delete-and-repost (`_GUIDE_STICKY_DELAY`s of
  quiet), so a busy channel re-sticks once activity pauses. The panel skips its own
  repost by id (`should_restick_guide`), and the repost shares `_place_guide_panel`
  with the command under a per-guild lock. Only the guide panel is sticky — the shop
  and leaderboard panels are not.
- **Shop panel (shipped):** **`/bank post-shop [channel]`** [mod] posts the
  perk-shop listing as a persistent panel: the same embed `/bank shop` shows
  minus the per-member bits (no ✅ rented marks — the panel is member-agnostic;
  prices templated from `EconSettings`, feature-gated rows annotated and
  their buttons disabled) with one **`ShopRentButton` per self-perk — a
  `DynamicItem` (`econ_shop_panel:<perk>`) re-registered in `cog_load`, so
  the buttons survive restarts with no per-message view store.** Any member
  can click; settings and the feature gate are re-read on every click (the
  panel can outlive a re-pricing), and every reply is ephemeral to the
  clicker. The rent flow itself (`_rent_perk_flow`) is shared with the
  ephemeral `/bank shop` view. Panel ids persist as `econ_shop_channel_id` /
  `econ_shop_message_id` (guide-panel pattern: same-channel repost edits in
  place — embed **and** view, so re-priced button labels refresh — another
  channel deletes + reposts). Button labels bake prices at post time; re-run
  the command after re-pricing. Gifting stays command-only (`/bank gift`
  needs a target member, which a button can't carry).
- **Leaderboard panel (shipped):** **`/bank post-leaderboard [channel]`** [mod]
  posts a single auto-updating embed: top 5 earners over a rolling 7 days
  (income = positive ledger sums excluding `transfer_in`, matching the
  Statistics page), community-goal progress bars with completed/paid states,
  the active quest board (daily/weekly/monthly/event, capped at 12 lines),
  and a blurb pointing members at `/quests` + `/bank wallet` for their own
  numbers. Panel ids persist as `econ_leaderboard_channel_id` /
  `econ_leaderboard_message_id` (guide-panel pattern: same-channel repost
  edits in place, another channel deletes + reposts). The **economy loop
  refreshes it in place every hourly tick** (`run_guild_leaderboard`); a 404
  on the stored message clears the ids, so deleting the panel message is how
  staff retire it. Collector + builder in `economy/leaderboard.py` (pure —
  Discord I/O stays in the cog/loop); ids are bot-managed and not
  dashboard-editable.
- **Manager surface (dashboard):** the **Economy** nav section, gated on
  `economy_manager_role_id` or admin (mirrors `games_editor_role` /
  `require_game_host`). Its pages: **Operations** (community progress +
  manual Settle, grant, rentals, ledger audit), **Claims** (the pending
  sign-off queue with Approve/Deny + a state filter over paid/denied/expired
  history), **Quests** (library + authoring + AI ideas), **Income Sources**
  (trigger switches + faucet rates), **Statistics**, and admin-only
  **Settings** (wiring, branding, perk prices — hidden from non-admin
  managers since its endpoints require admin). It is the dashboard
  counterpart to the `[mod]` `/bank grant` and `/qotd post` commands. The
  home dashboard's **Moderation tile** also surfaces the pending-claims
  count (+ latest claimant/quest) via the `/api/home` moderation group, so
  waiting sign-offs are visible without opening the Economy section.
- **Role customization in v1** happens inside `/bank shop`'s ephemeral panel —
  customise buttons opening modals (name / color hex / gradient / server-emoji
  icon), plus `/bank role icon` for image uploads — proxied through the bot.
- **v2 member dashboard:** wallet page (`require_perms(set())` like the home page —
  balance, XP-today, streak + grace, quests, rentals with next-bill countdown, 30-day
  history, mute toggle) and a role studio panel with live preview.

## 8. Private Rooms (Stage 2)

As V3 §8 (owner as landlord: invite cap 25, block list persisting across re-rentals,
rename/topic/NSFW/slowmode/user-limit/bitrate/lock, Manage Messages+Threads inside,
mods retain disclosed view access), built on existing machinery:

- **Voice rooms:** Voice Master's owner registry, capped-FIFO trust/block lists
  (`_add_target_with_cap`), pure overwrite planner (`plan_initial_overwrites`), and
  startup reconciliation planner are reused/generalized.
- **Text rooms:** Pen Pals' category-scoped private channel creation.
- **Lapse:** text rooms archived 14 days then deleted using the hidden-channels
  overwrite snapshot/restore (`serialize_overwrites`/`rebuild_overwrites`); voice rooms
  deleted immediately.
- NSFW flag inherits guild verification gates (never bypasses ID verification —
  consistent with the Voice Master age-gated dial).

## 9. Metrics & Per-Guild Tuning

Every rate, price, and scaling parameter is editable in the Economy config panel
(admin) — conversion rate, login bases, streak cap, milestone amounts, all perk prices,
quest reward bands, booster multiplier. XP coefficients remain on the existing XP config
surface.

Home dashboard gains an **Economy Metrics tile** (widget-registry entry + `tiles/`
module): median & p90 weekly income, minted vs burned, faucet mix, rental uptake &
churn, streak health, and **pricing hints** ("solid color ≈ 50% of median weekly
income") computed from the ledger and shown beside each price field in the config
panel.

**Shipped (Stage 4).** A **weekly rollup** runs at the guild-local ISO-week roll
(inside the same loop transaction as the weekly rotation / community settlement) and
writes one immutable `econ_metrics_weekly` row per (guild, closed week), idempotent on
its `(guild_id, iso_week)` primary key. Each row records, over the closed week:
median / p90 income across **earners only** (positive credits, `transfer_in`
excluded — transfers move currency, they neither mint nor burn), **minted** (positive
credits ex-`transfer_in`) vs **burned** (`|negative|` ex-`transfer_out`), the
**faucet mix** (minted share per group: logins / activity / quests / games / grants,
`{}` when nothing minted), rental **holders / live / ended** (churn via the new
`econ_rentals.ended_at`, stamped on every termination path), and streak health
(`streaks_7plus`, `grace_used`). The admin-only home tile (`source: "economy"`,
Health category) shows the latest week with a **week-over-week net-mint arrow**
(minted − burned, direction only, needs ≥ 2 weeks); before the first rollup it shows a
"rollup pending" empty state. **Pricing hints** are `round(median weekly income ×
fixed per-perk factor)` served from `GET /api/economy/metrics` and rendered under each
price field in the config panel; advisory only (no enforcement), and `{}` until the
first rollup lands.

**Statistics page (Economy section).** A live, on-demand tuning surface
(`GET /api/economy/stats`, gated on `require_economy_manager` — manager
role or admin; member table capped at 500), complementing the weekly rollup with a
same-instant read of the ledger. It shows: **supply concentration** — total supply,
holder count, median balance, top-10% share, and Gini, all computed over **positive
balances only** (inequality of who-holds-what, not the zero-balance long tail); a
fixed-bucket **balance histogram**; **7-day flow** — minted vs burned with a burn
rate, plus transfer volume and grants (money definitions match the rollup: mint /
income exclude `transfer_in`, burn excludes `transfer_out`); a **per-member income
velocity table** (top holders by balance) with 7/30-day income, coins/day, 7d spend,
top faucet group, live rentals, streak, and last-earned; **engagement** — earner
ratio (7d earners ÷ 30d active), spenders, quest claims, **quest approval rate**
(resolved paid ÷ paid+denied over 30d, resolved-only), and **hoard-weeks** (median
balance ÷ latest-rollup median weekly income); **perk affordability** in days of
median daily income per price field; and the **top 5 transfer pairs** (30d, by
`transfer_out` magnitude) as the alt-funnel audit surface for transfer abuse (§12).
All ratios/divides are guarded (0 or `null` when there is no denominator).

## 10. Notifications

DM-first via a shared `try_dm`-style helper; on failure, fall back to the bank channel.
Per-member mute toggle via `/bank mute` (`econ_notify_prefs`; also surfaces on the v2
wallet page). The dm_perms system is
member-to-member consent and does **not** gate bot DMs — no interaction there.

| Event | Notify |
|---|---|
| Streak milestone / grace consumed / reset | DM |
| Quest approved / denied (with reason) | DM |
| Rental grace entered / lapsed | DM |
| Login payout & daily conversion | Silent (ledger) |
| Sign-off claims, community settlements | Bank channel |

## 11. Scheduled Work

One economy loop registered via `bot.startup_task_factories` (gets `_resilient_task`
crash-restart), ticking hourly:

| On tick | Action |
|---|---|
| Guild-local day rolled | XP→currency conversion (only the most recent marked local day — no retroactive backlog after an outage, §12); streak evaluation (grace/reset); daily quest rotation; QOTD reward window closes |
| Every tick (hourly) | Rental billing + grace retries; pending-claim expiry; (v2) spotlight expiry; (rooms stage) room archive/purge |
| Guild-local ISO week rolled | Weekly quest activation; community settlement; metrics rollup; (v2) spotlight inventory reset |

Event-driven (no polling): login + reaction XP on existing `events_cog` listeners;
voice login on the existing voice-XP tick; game payouts at `end_game`/duel resolution;
boost multiplier read live from `premium_since` at credit time (no flag to maintain);
member leave/ban cleanup on the existing member-remove listener.

All periodic actions are idempotent via per-period dedup rows — a restart replays
nothing and misses nothing (catch-up on next tick).

## 12. Edge Cases

- Guild-local midnight (not UTC) removes V3's mid-hangout flip. `tz_offset_hours` has
  no DST; acceptable, documented.
- Voice login across midnight: a qualifying member in VC at 00:05 local gets the new
  day's voice login — intended.
- Grace gaming: rolling 7-day window; miss/login/miss still resets.
- Transfer abuse: audit stream + per-guild toggle.
- Sign-off re-claim loops: one pending at a time; deny history on the claim card.
- Community payout scope: active-in-30-days (`member_activity`) — "contributors only"
  tickbox is v2.
- Reaction farming: one `reaction_given` award per (message, reactor), ever.
- Wallets are integer; XP remainders carry as fractions on conversion rows only.
- Multi-day outage: the conversion loop converts only the most recent marked local day
  and jumps forward — intervening days' XP is not converted retroactively (prevents
  backlog minting when a guild re-enables the economy; same trade the games scheduler
  makes).
- Game participation payouts cover 20 of 23 games — the six duel games,
  ttl/traditional/legitlibs, and the 11 party cogs whose rosters were enriched in
  Stage 2. ffa and fantasies stay excluded by design (anonymous submissions);
  photo pays via the photo-reply event quest (§4.5), not `end_game`.
- Guild loses Level 2 / Enhanced Role Styles mid-rental: perk enters grace as if unpaid?
  No — billing pauses the affected perk (icon/gradient) and DMs the owner; auto-resumes
  when the feature returns (no charge while suspended).
- **Dashboard grace-cancel de-projection (Stage 3):** the billing loop only walks *live*
  (active/grace) rentals, so a manager force-cancelling a **grace** rental — which lands
  it in `cancelled` at once — would otherwise leave the Discord role projected. The
  cancel endpoint reconciles best-effort post-commit (`revoke_role_perks` on the
  *beneficiary*, guarded on a ready bot, failures swallowed+logged, `role_updated`
  reported); an active-rental cancel just sets `cancel_at_period_end` and de-projects at
  the anniversary. A DB write always lands even if the Discord cleanup can't run.
- **Suspend at anniversary (Stage 3):** if a perk is suspended (feature lost) exactly as
  its `next_bill_at` comes due, the billing clock is frozen, so on resume the perk can be
  billed immediately (the anniversary is already in the past). Benign — the member owed
  that week's rent regardless and was not charged while the visual was suspended;
  documented rather than special-cased.

## 13. V2 (committed) & Parking Lot

**V2 — committed, not parked (decided):** member wallet dashboard page · role studio
panel with live preview · spotlight slots.

**Parking lot** (unchanged from V3 unless noted): auto-tracked quest criteria
(partially delivered: trigger words §4.4 + the photo-reply event trigger §4.5;
further trigger kinds — game wins, streaks — remain parked) ·
fines/tickets (Jail exists — integration stays parked by design call) · giveaway
entries · emoji sponsorship · room upgrades · streak-rental discount ·
auctions/seasonal drops · per-quest contributors-only payout · scheduled/auto QOTD.
