# Economy & Perk Shop — Spec V3.1 (repo-grounded)

**Brandable per-guild currency · Logins & streaks · XP conversion · Quests · Rentable perks**
*Status: largely shipped — Stages 0–4 built (wallets/ledger/config, faucets,
quests, transfers, the rental engine + role perks + gifts, and the metrics
dashboard), plus sinks rounds 1–3 (§6: gifting, streak shield, voice-style lease,
emoji sponsorship, raffle, wagers, hoard tax — the last few shipped dark).
Soak/tuning (Stage 5) is ongoing; private rooms (Stage 6) plus the v2 member
dashboard and spotlight slots remain design-only. Implementation plan in
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
- **Streak shield (sinks round 3, stage 2):** a prepaid one-shot bought in `/bank shop`
  (`price_streak_shield`, default 30; 0 hides the row), held at most ONE, auto-burned
  when a reset would land — it covers what grace can't. Covers consume grace-first:
  a 2-day gap survives on grace *or* shield, a 3-day gap only with both, 4+ always
  resets, and a hopeless gap leaves the shield held. Purchase is a guarded
  claim-before-debit upsert (`shields = 1 WHERE shields = 0`, then the debit — ledger
  kind `streak_shield`), so a race can't double-charge; state is
  `econ_streaks.shields` (migration 091). Purchasable at streak 0.
- **Milestones:** day 7 → +25 · day 30 → +100 · day 100 → +365 · +100 each 100 after.
- Idempotency: `INSERT OR IGNORE` on `(guild_id, user_id, local_day)` — one login/day
  no matter how many events race (birthday-announcement pattern).
- **Daily digest DM:** members with the opt-in `game_role_id` role get one DM per
  qualifying login — a streak/payout line, any milestone/grace/shield/reset callout
  (grace and shield burning together collapse into one "streak saved" field), and a
  "quests to play with today" checklist (open quests with `progress_bar` meters, capped
  at `_LOGIN_QUEST_RECAP_LIMIT`) so deciding what to do next is one glance, not a dig
  through `/bank quests`. Members without the role earn the same rewards with no DM.

### 3.2 XP → Daily Conversion — dormant (off by default)
**This faucet ships OFF.** `econ_xp_per_coin` defaults to **0**, and the day-roll
driver skips the conversion entirely while the rate is 0 — earning XP no longer
mints currency. The mechanism (`convert_xp` / `process_conversion` /
`econ_conversions`) is retained intact so a guild can re-enable it by setting a
positive rate on the Income Sources panel; it is not a code change. XP itself is
unaffected (it still drives levels/leaderboards); it simply has no currency faucet.

When re-enabled, the behaviour below applies. Members earn XP as today
(`xp_events` ledger: text per-word, replies, image-reacts, voice 1.67/min with ≥2
humans); at guild-local midnight, each member's XP earned that local day converts
to currency.

- **Conversion: `econ_xp_per_coin` XP → 1 currency** (0 = off; a former default
  was 15), rounded down; fractional remainder carries to the next day (stored on
  the conversion row).
- Because the driver skips conversion while the rate is 0, turning the faucet off
  does **not** accumulate a remainder backlog — re-enabling resumes from that day,
  consistent with the no-retroactive-backlog rule (§ outage behaviour), rather than
  paying out every skipped day at once.
- V3's flat XP table (3/post etc.) is **dropped**; the real rates rule. Reference at
  a rate of 15: a 2-hour qualifying voice hangout ≈ 200 XP ≈ 13 coins. The conversion
  rate and all XP coefficients are the scaling parameters in the config menu (§9);
  income anchors are tuned there against the metrics card, not hardcoded.
- **XP source `reaction_given`** (live, Stage 1): reacting to someone else's message
  pays the *reactor* XP — default 0.34 (double the existing image-react rate), tunable via
  `xp_coeff_reaction_given_xp` on the dashboard XP panel. Feeds regular XP/leaderboards
  (and currency too, but only when the conversion faucet is on). Guards: no self-reactions,
  no bots, one award per (message, reactor) ever (dedup table), so react/unreact can't farm.
  See [[xp-spec]].
- When on, conversion is silent (ledger entry: "Daily activity"). Runs from the economy
  hourly loop when a guild's local day rolls; idempotent via a per-(guild, user,
  local_day) conversion row.

### 3.3 Quests — per §4. Daily 10–20 · weekly 25–75 · community flat payout.

### 3.4 Interaction Rewards
- **QOTD (built, manual):** the reward is paid for **replying** to a registered
  question — a direct Discord reply (`message.reference`) to the QOTD message earns
  **10**, once per QOTD (dedup row per member). Two ways to register one:
  - **Tag the role (the usual path).** Any message from an economy manager
    (admin or `manager_role_id`) that mentions `qotd_ping_role_id` becomes that
    day's question — no command, no card, the mod asks in their own words.
    `logic.qotd_marker_question` strips the mentions for the stored audit text
    (capped at 300 chars; empty is valid, since an image can be the question) and
    `events_cog._econ_work` writes the `econ_qotd` row in the same transaction as
    the login faucet. The manager gate is the security boundary — Discord lets
    anyone *type* `<@&id>` regardless of ping permission, so without it any member
    could mint a faucet and farm replies to it. Re-entrant: a row already
    registered for that message id is never duplicated.
  - **`/qotd post [question]`**, which renders the banner card
    (`render_quote_card`, the same renderer `ffa_banner` uses), pings the role, and
    is still how the paid **sponsored** queue reaches the server.
  Replies pay only while the question's `local_day` is still the current
  guild-local day — the row lives forever, so without that check a member could
  reply down a month of old questions for 10 each. No scheduler; mods ask when
  they want. `qotd_ping_role_id` unset (the default) means silent posts and no
  tag-to-ask. The mention only notifies if the role is mentionable or the bot holds
  "Mention @everyone, @here, and All Roles" — Discord renders it as inert text
  otherwise; that permission governs *notification*, not registration.
- **Sponsor a QOTD (built, sink — migration 090):** a member runs
  `/bank sponsor <question>`, paying `price_qotd_sponsor` (default 40) to put a
  question in front of the server. **Charged at submit** — a free queue invites
  spam — which makes decline and expiry *refund* paths (ledger kind
  `qotd_sponsor` out, `qotd_sponsor_refund` back). A mod reviews it on a
  persistent Approve/Decline card in the bank channel (DynamicItems
  `econ_qotd_sub:{approve,deny}:<id>`, so clicks survive a restart) or on the
  dashboard queue (Economy → Sponsored QOTD, `require_economy_manager`, which
  also **withdraws** an already-approved question back out of the post queue —
  the service only *resolves* pending rows, so withdrawal is its own path);
  declining opens a reason modal and the reason reaches the member by DM.
  Resolving from the dashboard re-renders the bank-channel card and DMs the
  sponsor with the same copy the card buttons use, best-effort: a Discord
  failure leaves the API 200 with `card_updated: false`. Approved questions join a FIFO queue that `/qotd post` draws
  from when the mod supplies **no** question text; the QOTD card is bylined
  "sponsored by <name>" and `econ_qotd.sponsor_user_id` records them
  (`posted_by` stays the mod who ran the command — different people, both
  audit-relevant). One open submission per member (partial unique index), so
  nobody can buy the whole queue.
  - **Refunds are exactly-once**, guarded by a `refunded_at IS NULL` predicate
    inside the same UPDATE that moves the state — not a caller-set flag — so a
    double-click or a replay cannot pay twice.
  - **The queue claim is atomic and happens before the send**
    (`claim_next_approved`, `UPDATE … RETURNING`), so two mods racing
    `/qotd post` take different questions rather than double-posting one; if
    the send then fails, `release_claim` puts it back rather than eating a
    member's paid slot.
  - Pending submissions expire after `qotd_sponsor_expire_days` (default 14)
    and refund, swept per-guild by the hourly loop. **Approved ones never
    expire** — they're waiting on staff, and timing them out would punish the
    member for staff latency. `price_qotd_sponsor = 0` disables the feature.
- **Pin of the Day (built, sink — migration 108, plan
  `docs/plans/pin-of-the-day.md`):** the sponsor pattern applied to a *public*
  artifact. `/bank pin` opens a modal; the text is charged `price_pin_of_day`
  at submit (ledger `pin_sponsor` out / `pin_sponsor_refund` back) and queued
  `pending`; a mod Approves/Declines on a bank-channel card
  (`PinApproveButton`/`PinDenyButton`, persistent). Approve posts + pins a
  "Pinned by @X" card in `pin_channel_id` and flips the row to `live` with a
  24h `expires_at` — the Discord post happens *before* the DB move, so a failed
  post refunds (the member is never charged for a pin nobody saw). One live pin
  per guild (partial unique index); a new approval **supersedes** the prior one
  (unpinned early — mod-paced). The hourly loop's `run_pin_expiry` unpins live
  pins past 24h (**no refund** — the day ran) and refunds `pending` ones no mod
  reached within `pin_expire_days` (default 3). Enabled only when
  `price_pin_of_day > 0` **and** `pin_channel_id` is set — a public sink, dark
  by default. One submission in flight per member.
- **Community Bounty (built, sink — migration 109, plan
  `docs/plans/community-bounty.md`):** the economy's first *many-payer* mechanic.
  `/bounty` opens a modal (title, details, opening stake); anyone chips into a
  bounty's pot from its board card in `bounty_channel_id` (💰 Chip in), and a
  mod Awards it (a `UserSelect` picks the winner) or Cancels it — persistent
  `BountyChipInButton`/`BountyAwardButton`/`BountyCancelButton`. Every stake is
  an `apply_debit` (`bounty_stake`) recorded as its own `econ_bounty_contributions`
  row; the pot is `SUM(non-refunded contributions)` (never stored). Award credits
  one `bounty_payout` of `pot − floor(pot × bounty_rake_pct / 100)` to the winner;
  the rake is escrow never credited back — the **burn** (a real sink, next to
  `wager_rake_pct`/`demurrage`). Cancel/expire refund every contribution
  (`bounty_refund`, exactly-once, **never raked**). `run_bounty_expiry` on the
  hourly loop expires + refunds open bounties past `bounty_expire_days`
  (default 14) and re-renders the card. Guards: `bounty_min_stake` floor,
  `bounty_max_open` per member. Enabled only when `bounty_channel_id` is set —
  dark by default.
- **Game participation 5:** paid at the party-games `end_game` choke point
  (`games/utils/game_manager.py`) from the session's player set, and — since the
  stage-4a funnel (sinks round 2) — at the duel games' **single terminal-state
  seam**: `BaseGame._db_set_state` is a concrete template method (cogs implement
  `_db_write_state`) that fires `_on_terminal_state` on every game-ending
  transition (`RESOLVED`/`RESOLVED_NO_NICK`/`ABANDONED`/`VOID`/`EXPIRED_*`).
  `RESOLVED`/`RESOLVED_NO_NICK` pay participation + win from the re-read row
  (roster for group games, challenger/target for duels; `winner_id=None`
  wipeouts pay participation only); the other terminal states pay nothing but
  still reach the hook, which is the guarantee the stage-4b wager escrow will
  settle and refund on. No duel cog calls `pay_game_rewards` directly anymore.
  Participation now covers **20 of 23 games**: the six duel games,
  ttl/traditional/legitlibs, and — enriched in Stage 2 — 11 party cogs that now pass
  their real player rosters into `end_game` (ama, clapback, compliment, hottakes, mfk,
  mlt, nhie, price, rushmore, story, wyr). ffa and fantasies are excluded by design
  (anonymous submissions); photo has no per-player completion hook either, but pays
  through the **`photo_post` faucet** (§4.5 — paid on the post itself) instead of
  `end_game`.
- **Game win +20:** paid for **both** game architectures in v1 (decided). Duel games
  read their explicit `winner_id` (chicken, hot potato, musical chairs, pressure
  cooker, quickdraw, …). Party games get a per-game-type winner resolver over the
  `end_game` payload (`economy/game_rewards.py::_WINNER_RESOLVERS`). Since the
  2026-07-20 game-UX round, **every party game with a genuine winner resolves one**:
  NHIE guiltiest, TTL Best Liar **and** Best Guesser (ties included; Open Book is a
  booby prize and unpaid), Hot Takes hottest author, **Rushmore vote winner(s),
  Clapback top score, MLT most crowns, Price "Most Reasonable (overall)"**. All-zero
  scoreboards pay nobody (an everyone-ties-at-0 board must not pay the room). Game
  types with no meaningful winner (story, AMA, MFK, compliment, traditional, WYR by
  design) pay participation only.
- **Payout visibility (2026-07-20):** every paying party game's recap embed gets a
  footer via `append_payout_footer` — `🪙 +20 to winners · +5 to everyone who
  played`, using the guild's configured amounts and currency emoji; winner line only
  for game types with a resolver; suppressed entirely when the economy is disabled.
  WYR has no recap embed, so it stays footer-less.
- **Event host 30 (mod grant):** `/bank grant @member amount reason` + Operations
  page button; manager-role or admin gated; audit-tagged in the ledger.

### 3.5 Coin Drops (built — migration 105)
- The bot drops a pouch of coins into `drops_channel_id` at random moments;
  the **first member to press the drop message's Claim button** collects
  it. The channel picker is the toggle (0 = off); everything is on the
  dashboard's Economy Settings page (`drops_min_coins` 5 /
  `drops_max_coins` 25, uniform roll; `drops_per_day` 4 as an *average*
  cadence; `drops_expire_minutes` 60).
- **Scheduling** (`economy_drops_loop`, 60 s tick, startup task factory):
  each guild's next drop lands a jittered 0.5–1.5× of the average period
  after the last, so members can't clock it. A due drop additionally waits
  until (a) the channel isn't mid-game (`channel_is_busy`, the chat-revive
  helper over `games_active_games` + `bot.game_busy_checks`), (b) the
  guild holds no other open pouch, and (c) someone has spoken since the
  bot's own newest message — a drop should land in conversation, not echo
  into the void, so dead hours simply drop nothing (the due time stands).
- **Claim** (`econ_drops`, `economy_drops_service`): `DropClaimButton` is a
  stateless `DynamicItem` whose `custom_id` carries the drop id
  (`econ_drop:claim:<id>`), so pouches survive restarts with no cache or
  re-seed. The `econ_drops` row is created *before* the send (the button
  needs the id; `message_id` starts 0 and is backfilled, or the open row
  deleted if the send fails). The race is settled by a conditional UPDATE
  (`status = 'open' AND expires_at > now`, rowcount 0 = lost — the
  Guess-Who pattern, never check-then-write), then `apply_credit` kind
  **`drop`** with the booster multiplier. Winner: the click edits the
  embed to "claimed by X" and removes the button; losers get an ephemeral
  "too slow". Claims only check `settings.enabled`, so a drop already
  posted stays claimable even if the channel is re-pointed meanwhile.
- **Expiry**: the tick sweeps overdue open drops (`status → 'expired'`,
  select-then-conditional-update per row so a racing claim wins), edits the
  message to "vanished" (button removed), and pays nobody.

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
- `/bank quests` + wallet page: active quests, progress, claim state. The
  embed is one line per quest — title cell | status glyph (✅ done, ⏳
  sign-off, 🔶 claim below, ▸ n/target, ☐ to do) | payment — grouped by
  cadence (Daily/Weekly/Monthly/Anytime/Community goals). Descriptions and
  the how-it-completes explainers (`quest_views.QUEST_STATE_LABEL`) moved
  behind an ℹ️ details select (`QuestDetailSelect`, always attached when
  quests exist) that answers with a one-quest ephemeral embed.
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
  push clears it. No manual override by design (2026-07-18 decision). A
  **channel-scoped** community quest on a message-shaped kind
  (`quests.CHANNEL_SHARE_KINDS`) scales the guild total by the channel's
  message share first (`channel_message_share` over `processed_messages`) —
  without it a 43%-of-traffic channel would be sized against 100% of the
  activity and its top tiers would be mathematically unreachable.
- **Tiers at 40/70/100%** (`quests.COMMUNITY_TIERS`): settlement at the
  closing week roll pays the quest's flat `reward` once per crossed tier to
  every 30d-active member — exactly-once per run via
  `econ_community_tier_payouts` (tier 0 reserves the **top-contributor
  bonus**: `reward // 2` to the top 3 by contribution). Contribution and
  tier-payout rows reset at the next activation, so a re-run pays afresh;
  idempotency only has to hold within a run. **Anonymous kinds**
  (`quests.ANON_COMMUNITY_KINDS`: confession, confession_reply, whisper)
  pay flat tiers only — no bonus, an empty top list in the settle summary,
  and a name-free resolution beat sheet — because naming the most active
  confessors/repliers/whisperers would deanonymize the feed.
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

- **Instant quest:** pays on the spot — ✅ reaction only, no reply or DM.
  Wallet/quest log carries the news, same as every other trigger kind.
- **Sign-off quest:** files the `pending` claim, posts the bank-channel card, and
  reacts 📝 — a manager still approves the payout.

`game_role_id` no longer affects trigger/photo/media quest completions (it
used to gate an in-channel reply vs. a DM); it still gates other recurring
engagement DMs — the daily digest (§3.1) and the weekly raffle-winner notice.

Members toggle the role themselves with the **🔔 Notifications** button on the
guide panel (§ channel guide panel) — a persistent static-`custom_id` view
(`econ_guide_notify`) re-registered via `bot.add_view` at cog load. The
button answers ephemerally with whichever way it flipped; the toggle decision
is the pure `economy/logic.py::resolve_notify_toggle`.

The same opt-in gate covers the **daily digest DM** (§3.1: streak + payout +
milestone/grace/reset callouts + quest checklist): it only reaches members who
took the role (`notify_member(..., require_game_role=True)`); everyone else
keeps earning with no DMs about it. With no role configured, nobody has opted
in yet, so the gate defaults to dropping the notice for everyone rather than
notifying the whole guild. Transactional notices (rental billing) are *not*
gated — they target a member by their prior spend, not by opt-in.

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
| `photo_post` | a member posts an image in the configured Photo Challenge channel (the post itself pays — no reactions needed) | `EconomyCog._on_photo_post` (on_message listener; announces ✅/📝 — in-channel, or DM under `game_role_id`) | `photo_post:<local_day>` (once/day by construction) |
| `party_game` | party game completes with the member in the roster — **including external games** (a Gamebot Cards Against Humanity game parsed from `/games track`, `game_type="cah"`) | `pay_game_rewards` via `game_manager.end_game`, or `games_external_cog._pay_cah_game` for CAH | `party_game:<game_type>:<game_id>` (`party_game:cah:<game-over-msg-id>`) |
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
| `game_win` | winning a party game (NHIE, TTL liar+guesser, Hot Takes, Rushmore, Clapback, MLT, Price resolve winners as of 2026-07-20) — **including external CAH** (the *Game over!* winner) | `pay_game_rewards` winners pass | `game_win:<game_type>:<game_id>` |
| `duel_win` | winning a duel/PvP match | `pay_game_rewards` winners pass | `duel_win:<game_type>:<id>` |
| `duel_lose` | resolving a duel/PvP match without winning it (every participant minus the winner set) | `pay_game_rewards` losers pass | `duel_lose:<game_type>:<id>` |
| `cat_catch` | catching a cat with the external **Cat Bot** in a `/games track … kind:Cat Bot` channel — parsed from the catch message (catcher resolved by username→member, rarity from the emoji). Pays **rarity-tiered coins** (common 1 → divine 300, blessed catches ×2) *and* this trigger | `games_external_cog._pay_cat_catch` → `pay_cat_catch` (`apply_credit` kind `cat_catch` + trigger); once per catch via the `games_external_payouts` ledger | `catbot:<catch-msg-id>` |
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
| `guess_post` | member submits a Guess Who round for others to solve (confession rounds included — producer half of the guess-who pair, see §4.6 pairing) | `guess_cog` both `_do_insert_round` call sites | `guess_post:<round_id>` |
| `session_join` | member appears in a game-night session's roster (end_game now merges the real roster into `games_session_tracker`, which start-time calls only seeded with the host) | `game_manager._fire_session_join` | `session_join:<session_id>` — later games in the same session collide silently |
| `voice_message` | member posts a voice message (fires before the transcription config gate — the quest is the post, not the transcript) | `voice_transcription_cog._on_message` | `voice_message:<message_id>` — use daily/weekly with a target count |
| `music_request` | member's `/play` adds ≥1 track | `music_cog.play` via `daily_occurrence=True` | `music_request:<local_day>` (once/day by construction — a 30-track playlist and 30 requests look the same) |
| `birthday_set` | member saves their birthday | `birthday_cog` modal submit | `birthday_set:set` (event = once ever, the `bio_set` pattern) |
| `level_up` | member's level-up is announced (announce-time, not award-time, so quest-XP payouts can't recurse into another claim; a silently-won level fires when its announcement lands) | `xp_service.handle_level_progress` via `fire_trigger_inline`, one fire per delivered level | `level_up:<level>` |
| `ama_answer` | hot-seat answers a question in their own AMA | `games_ama_cog` reply-modal submit | `ama_answer:<game_id>:<q_idx>` — use daily/weekly with a target count |
| `conversed` | member replies to another member (replies only, never bare mentions — mention spam is free, a reply is directed) | `events_cog._econ_work` beside `reply_sent` | `conversed:<partner_id>` — **occurrence = the partner**, so counted quests read "talk with N different people"; repeat partners collide in the marks table |
| `replied_to` | someone else replies to the member's message (passive twin; needs the reference resolved to a real non-bot author) | same site, fired for the target author | `replied_to:<replier_id>` — counted = "have N different people reply to you" |
| `reacted_to_member` | member's reaction lands on a new person's message (inherits the reaction XP farm guard) | `events_cog.on_raw_reaction_add` beside `reaction_given` | `reacted_to_member:<author_id>` — counted = "react to N different members" |
| `channel_hop` | member posts in a channel (threads count toward their parent, so thread-hopping can't farm it) | `events_cog._econ_work` | `channel_hop:<channel_id>` — counted = "talk in N different channels" |
| `active_day` | member's message on a guild-local day | `events_cog._econ_work` | `active_day:<local_day>` — counted weekly = "show up any N days this week"; the gentle streak (research: hard reset-to-zero streaks burn small communities) |
| `voice_partner` | member earns voice XP with another undeafened human in the channel (anti-idle rules gate the member's side) | `voice_xp_service` tick beside `voice_session` | `voice_partner:<partner_id>` — counted = "share voice with N different people" |
| `thread_deep` | member posts in a thread at ≥ `THREAD_DEEP_MIN` (20) messages (`Thread.message_count` at ingest — no storage) | `events_cog._econ_work` | `thread_deep:<thread_id>` — once per thread; everyone posting after the crossing gets credit |
| `welcome` | member replies to someone who joined within `WELCOME_WINDOW_SECONDS` (7 days) | `events_cog._econ_work` | `welcome:<newcomer_id>` — counted = "welcome N new faces"; the retention quest |
| `conversation_starter` | member's message draws replies from `CONVERSATION_STARTER_REPLIERS` (3) distinct humans — distinct-replier rows accrue in `econ_msg_replies` (migration 085, ingest-derived since content is never stored; pruned to 14 days on the day roll) and the fire happens exactly on the crossing | `events_cog._econ_work` reply path, fired for the target author | `conversation_starter:<message_id>` |
| `greeting_answered` | member replies to / @mentions someone whose greeting is still pending in Greeting Watch, same channel (pending ≈ inside the window — the loop resolves rows right after it closes). Self-gates on the feature: no watched channels, no fires | `events_cog._econ_work` via `greeting_watch_service.pending_greetings_for` | `greeting_answered:<greeting_message_id>` — one hello credits an answerer once |
| `birthday_wish` | member wishes a happy birthday on a day a birthday was **announced** (`birthday_announcements` row — quiet/unset birthdays never become quest bait; pre-09:00 wishes miss, documented soft edge): a reply/mention of the birthday member, or a wish phrase (`birthday_service.is_birthday_wish`) anywhere when no target resolved. One fire per message; the wisher can't be the birthday member | `events_cog._econ_work` | `birthday_wish:<target_id>:<local_day>` (phrase fallback: `birthday_wish:day:<local_day>`) |
| `drop_claim` | member wins a coin-drop Claim race — pays beside the drop's own credit (the `cat_catch` double-pay pattern); drop cadence is the natural rate limit | `economy_drops_service.try_claim_drop` after the credit | `drop_claim:<drop_id>` |
| `role_pick` | member self-assigns a role via a role menu **grant** (removals never fire) or an announcement role button grant. Setup kind (see below) | `role_menus/views._apply_outcome` + `announcements/buttons._apply` | `role_pick:set` (once ever) |
| `confession_reply` | member posts an anonymous reply to someone ELSE's confession (OP self-replies never fire; both thread and channel reply paths). Same privacy contract as `confession` — silent claim, no channel noise | `confessions_cog.ReplyModal.on_submit` → `_fire_confession_trigger(kind="confession_reply")` | `confession_reply:<reply_message_id>` — use daily/weekly with a target count |
| `shop_purchase` | member makes a voluntary shop purchase: perk rent (voucher-covered counts — the quest rewards shop engagement, not the spend), streak shield, emoji sponsorship, QOTD sponsorship, raffle tickets. Renewal billing (`bill_rental`) deliberately never fires. Setup kind (see below) | each purchase service beside its `apply_debit` | `shop_purchase:set` (once ever) |

**One-time setup kinds** (`SETUP_QUEST_KINDS`): `bio_set`, `birthday_set`,
`role_pick`, `shop_purchase`. Board-cadence quests on these kinds claim once
ever on a constant period (occurrence `set`), pay on completion even when not
drawn on the member's board, and drop off the board once the underlying thing
is done (`_setup_underlying_done`: bio row / birthday row / any
`role_menu_grants` grant row / any `econ_ledger` row with a purchase kind —
`PURCHASE_LEDGER_KINDS`). Known soft edge: announcement-button grants aren't
recorded in `role_menu_grants`, so those pickers stay board-visible until the
paid-claim backstop catches them.

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
the notification role, contradicting the "role set = opt-in to DMs" model
(unlike a member who joins the server, a member who takes the role has opted
in). No replacement fires on join; members discover the
library through `/quests`. The `onboarding` column and `econ_onboarding_dms`
table remain as inert dead schema (migration 071), no longer read or written,
and the quest editor's onboarding toggle is gone. Don't reintroduce a join-time
economy DM without a real opt-in signal.

**One-time setup quests (the welcome guide, 2026-07-18):** the sanctioned,
pull-not-push successor to the onboarding path. The setup trigger kinds
(`quests.SETUP_QUEST_KINDS` = `bio_set`, `birthday_set`) can be run as ordinary
**daily** quests, so they're drawn into a member's random daily board like any
other — a subtle "fill out your bio / set your birthday" nudge that a newcomer
happens on while browsing `/quests`, never a DM. Two service-layer
special-cases make a once-in-a-lifetime action fit a daily cadence:

- **Claim once ever, not per day.** `fire_trigger_quests` claims a
  board-cadence setup quest on the constant occurrence period
  `"<kind>:set"` (not the calendar day) and *independently of the board draw*.
  So the completing member always gets paid the moment they do it — a lifetime
  action can't wait for a lucky daily roll — and re-saving a bio tomorrow
  collides on the same claim key and pays nothing (a plain daily would re-earn
  each period; this is the whole reason `bio_set`/`birthday_set` were
  event-only before). The setup claim's `:`-keyed period also skips
  `maybe_pay_set_bonus` (it isn't part of any day's board set).
- **Hide once done.** `assigned_board_ids` drops a setup quest from a member's
  board once they've done the underlying thing (a `bios` / `member_birthdays`
  row exists) or already claimed it — **drop, no refill**, so a completed setup
  slot just leaves the board one shorter rather than reshuffling the window and
  stranding a counted quest's in-progress work. Net: only members who *haven't*
  done it ever see it, and it silently vanishes the moment they do. Rerolls
  won't swap a member into a setup quest they've completed, and setup quests
  are excluded from the clear-the-board set-bonus requirement (a member
  shouldn't have to do their once-ever bio to earn today's daily set bonus).

Enabling it is pure data: create a daily quest on each kind (normal daily
reward). The fire hooks (`bios/wizard`, `birthday_cog`) already exist.

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

**Paired quests** (migration 106, `pair_tag`): two active quests of the same
cadence sharing a non-empty `pair_tag` are drawn as a **bundle** — when the
pure draw picks either, `quests.apply_pair_bundles` swaps its partner in for
the last unpaired slot, so producer/consumer prompts (submit a Guess Who
round + play one; send a whisper + unmask one) land on a member's board the
same period. Pairs the draw already completed are never split; a tag carried
by one active quest or by three-plus is inert (strict exactly-two rule,
`quests.pair_map`); a board of one can't hold a pair and is left alone.
Reroll overrides apply **after** pairing — a member can still opt out of half
a pair, and that's their call. Note the frequency effect: a paired quest
appears whenever *either* member of the pair is drawn, roughly doubling how
often each shows up relative to an untagged pool-mate. Tag editing is on the
dashboard Quests page (Pair tag field); like any pool change, edit at the
period boundary or boards reshuffle mid-period.

Both surfaces filter to the member's board: `fire_trigger_quests` and the
trigger-word `on_message` path skip any daily/weekly/monthly quest not on the
board this period (so a member only *earns* a kind when its quest is on their
board), and `_load_quests_state` shows only the board on `/quests` + the wallet.
Because assignment cadence equals the claim period, counted progress never
fragments mid-period.

**Board add-ons (stage 5 of the quest-variety plan, migration 084):**

- **Board reroll** — one **free** per member per guild-local day
  (`econ_rerolls`), then up to `EconSettings.quest_reroll_daily_cap`
  (default 3) more at `price_quest_reroll` (default 10) each, ledger kind
  `quest_reroll`, counted by `econ_rerolls.paid_count` (migration 089).
  The cap is the point: unlimited paid rerolls turn a "this quest doesn't
  fit how I use the server" escape hatch into a shopping trip for the
  cheapest quests. Either setting at 0 disables the paid tier and leaves
  the free reroll intact — the free one is never taken away. Offered via a
  🎲 select on `/bank quests` that names the price. Swaps one *untouched*
  board quest (no claim, no counted progress this period) for the first
  pool quest in the member's own shuffle order that isn't on their board,
  **preferring a different trigger kind**. Persisted as an
  `econ_board_overrides` row keyed by the draw's `period_idx` and applied
  on top of the pure draw in `assigned_board_ids` (a same-period re-reroll
  would update `to_quest_id` in place, so application never chains; the
  override dies with the period). The reroll spends *after* validation —
  a refused reroll costs neither the free allowance nor a coin, and a
  failed debit leaves the board untouched.
- **Clear-the-board set bonus** — completing every quest on the personal
  daily (or weekly) board in one period pays
  `EconSettings.quest_set_bonus_daily` / `_weekly` (**default 0 = off** —
  a silent default-on bonus surprises 1-quest boards; the main guild opts
  in at 10/25 via the seed script, editable on Settings) as ledger kind
  `quest_bonus`, no booster multiplier. Checked after every paid claim — instant and sign-off
  approval both, against the CLAIM's period — with an `econ_set_bonus`
  reservation row as the exactly-once guard.
- **⚡ Weekly spotlight** — one featured trigger kind per ISO week pays
  **double** on quest claims (`spotlight_kind`: deterministic sha256 over
  (guild, week) across the distinct kinds with an active non-community
  quest; `None` under 2 kinds — rotation needs something to rotate).
  Applied in `_credit_reward` (meta `spotlight: true` on the ledger row);
  surfaced on `/quests` (⚡ tags + banner), the leaderboard embed, the flip
  announcement, and the live tracker. A sign-off approved after the week
  flips pays at the approval week's rate — accepted drift.
- **Flip announcement** — at the ISO-week roll the loop posts "this week's
  quests are up" (+ the spotlight reveal) to the leaderboard panel's
  channel, bank channel fallback (`_post_flip_announcement`; skipped when
  neither is configured). It **pings the economy game role** (`game_role_id`,
  the notifications opt-in) when set — the one recurring economy post that
  reaches opted-in members without a DM — allow-listing exactly that role
  (`flip_announcement_content`).

**Dynamic target band:** a counted quest may carry a target *band*
(`0 < target_min < target_max`) instead of a fixed `target_count`. Each
member's target for a period then resolves **from their own pace**
(`resolve_member_target`, migration 083): the median of their trailing
completed periods of that kind in `econ_kind_activity` (4 days / 4 ISO
weeks / 2 months by cadence) × `DYNAMIC_STRETCH` (1.15), clamped to the
author's band — a chatty member gets "send 45", a quiet one "send 10", for
the same flat reward (effort-equity; scaling the payout would re-reward the
already-active). The result is **stored on the progress row at first
touch** (fire path or wallet view, whichever sees the period first), so it
never moves mid-period and both surfaces agree. Members with fewer than 2
active trailing periods of the kind fall back to the deterministic
**Gaussian draw** over the band (`quests.effective_target`, stable on
`(user, quest, period)`) — the cold-start behavior, and the entire behavior
before migration 080's ledger accrued history. Sandbagging by going quiet
floors out at `target_min` and is self-defeating (less activity is less
<<<<<<< HEAD
income anyway). **Channel-scoped band quests** on message-shaped kinds
(`quests.CHANNEL_SHARE_KINDS`: message_sent, reply_sent, media_post) scale
the member's median by *their own* share of traffic in the scoped channel
(`channel_message_share` over `processed_messages`, trailing 28 days) —
kind activity has no channel dimension, so "send N in #the-meadow" sizes
to their meadow pace, not their whole-server pace. The Gaussian fallback
is never scaled (the author wrote the band for the channel already).
Thread messages archive under the thread's id while scoped fires credit
the parent, so shares read slightly low in thready channels — targets err
forgiving.
=======
income anyway).

Kinds in `quests.PERSONAL_P25_KINDS` (currently `reaction_given`) resolve
at the member's own trailing-period **p25 instead of the stretched median**
(`quests.p25_target`, no `DYNAMIC_STRETCH`): reactions are passive
one-click acts with a heavy-tailed distribution, so the target means "at
least your own quiet-week level" — stretching past typical pace would turn
the anti-freebie fix into a grind on a heavy reactor's off week. Zeros
still count in the quantile, same as the median path.
>>>>>>> main
`0/0` (the default) means no band — the fixed `target_count` applies, so existing
quests are unchanged. Both the counted-claim path and the `/quests` progress
meter read the same `effective_target`.

Game-fired claims are **silent in-channel** (matching the participation faucet —
a game recap followed by a dozen quest embeds would be noise); the wallet ledger
and `/quests` carry the news, and sign-off claims still post the bank-channel
card. The photo-post and media-post listeners announce (✅/📝 on the member's
own message — the payout lands on the post itself). Hooks that fire inside another module's open
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

**Photo plumbing:** payout fires on the post itself, not on replies to the card
and not on reactions. `EconomyCog._on_photo_post` (an `on_message` listener) fires
when a member posts an image in the configured photo channel, and pays **two
independent, stacking amounts**, each capped once per guild-local day:

1. a **flat participation award** (`EconSettings.reward_photo_post`, default 5,
   ledger kind `photo_post`) on the post itself — no quest required. Dedup rides
   an `INSERT OR IGNORE INTO econ_photo_rewards (guild_id, user_id, local_day)`
   anchor in the credit's transaction (mirrors the login faucet). 0 turns the
   flat award off; and
2. the **`photo_post` quest** bonus on top, if one is active (occurrence
   `photo_post:<local_day>`, dedup on `econ_quest_claims`).

Guards are cheapest-first: a guild/bot check, an image check (content-type with a
filename-extension fallback), a TTL-cached channel check, then a DB eligibility
pre-check (economy on, `photo_post` source on, and something to pay — a positive
participation award *or* ≥1 active `photo_post` quest). The `photo_post`
income-source toggle gates **both** payouts. The channel is the standalone Photo
Challenge feature's dedicated channel — `channel_id` in `games_game_config.options`
(game type `photo`), owned by the **Photo Challenge → Setup** panel
(`/api/photo-challenge/config`). When that config carries no channel but an
**active photo schedule** does (a schedule created without the Setup panel ever
being saved leaves the config row empty), the listener recovers the channel from
`games_scheduled` so schedule-only setups still pay; the payout is dormant only
when neither knows a channel. The flat
rate is edited on the **Income Sources** page alongside the other faucets. *(The
old reaction-gated model and its `react_threshold`/`auto_react` knobs are retired;
migration 099 renames existing `photo_react` quests and income-source rows to
`photo_post`; migration 101 adds `econ_photo_rewards`.)*

## 5. Transfers

`/bank pay @member amount` — min 1, whole numbers, no fee. **Confirmation step over
100** (an ephemeral confirm button before the debit lands). Both sides ledgered
(payer `transfer_out`, recipient `transfer_in`). Per-guild `transfers_enabled` toggle
(default **on**) is the kill switch for alt-funneling; `/bank pay` refuses with a
branded notice when it is off. **Transfers do not mint** — the recipient's
`transfer_in` credit takes **no** booster multiplier (the ×1.5 is a faucet-only patron
bonus); a transfer only moves existing currency between wallets. An optional
**memo** rides `/bank pay` — collapsed to a single trimmed line and length-capped,
stored verbatim under a `memo` key on both ledger rows and surfaced (escaped at
render time) in the wallet ledger, the dashboard bank-manager ledger, and the
public register feed's consolidated `A → B` transfer entry.

## 6. Sinks (The Perk Shop)

**Shipped (Stage 3):** the role-customization perks (solid color, name, icon,
gradient) are live — browsed, rented **and customised** in `/bank shop`'s
ephemeral panel (§7), and **every one is giftable** (sinks round 2, stage 1:
a gift is the base perk rented with `beneficiary_id` = the friend; the old
`gift_color` kind and its separate `price_gift_color` retired in migration
091, which rewrote live rows to `role_color`-with-beneficiary and widened the
perk CHECK once for the round's later kinds, `voice_style` and `emoji` —
see `docs/plans/economy-sinks-round-3.md`). Private rooms stay **Stage 6**
and the spotlight slot stays **v2** — both still design-only below.

Weekly rentals bill on personal anniversary tick. Defaults below; every price per-guild
tunable (§9). **Renewal bills the CURRENT guild price at each anniversary** — the
rent-time price is snapshotted only for week one; a price tuned in the config panel
takes effect on the next cycle, never retroactively.

| Perk | Per week | Repo grounding |
|---|---|---|
| Custom role color (solid) | 50 | `guild.create_role(color=…)` |
| Custom role name | 35 | 32-char, filtered via the voice-master name-blocklist matcher (shared table). Setting it renames the member's personal role **and** sets their server nickname to match (`member.edit(nick=…)`, best-effort — a Forbidden/HTTP failure still keeps the role rename and tells them why via `_custom_name_confirmation`). When the perk lapses, `revoke_role_perks` reverts the nick too (`should_revert_nick` — only if the nick still equals the perk's name, so a game name-penalty stake set since is never clobbered) |
| Role icon | 75 | Requires `ROLE_ICONS` in `guild.features`; upload utils exist in `booster_roles.py` |
| Gradient (member-picked two-color fade) | 120 | **Capability confirmed**: `booster_roles.py` already sets `secondary_color` on create/edit; requires Enhanced Role Styles guild feature; supersedes solid |
| Holographic (Discord's fixed shimmer preset) | 300 | `role_holographic` perk (migration 107): the projector sets the fixed `(primary, secondary, tertiary)` triple Discord accepts for `tertiary_color`; requires the same Enhanced Role Styles feature; supersedes gradient; member picks nothing (no customise modal) |
| Private text room | 200 | §8 (Stage 6) |
| Private voice room | 200 | §8 (Stage 6) |
| Gift (any perk above) | base perk price | Payer funds a friend's perk — same kind, `beneficiary_id` = friend; billed to the payer at the perk's current price |
| Streak shield | 30 once | One-shot consumable, not a rental — §3.1; shop "One-shot" row + panel button, wallet shows "held" |
| Sponsored emoji | 60/wk (animated 90) | **Sinks round 3, stage 4.** `/bank emoji image: name:` escrows week one (`emoji_sponsor` kind); mod approves on the Sinks page queue → two-phase claim-then-upload opens a real `econ_rentals` row (perk `emoji`, meta carries `animated` so renewals bill the right rate); deny/cancel/expiry refund exactly-once (`emoji_sponsor_refund`, `refunded_at` predicate); lapse deletes the emoji and frees the slot + name. Caps: `emoji_sponsor_slots` (default 5) + never the guild's last free emoji slot of that kind. One in flight per member and one claim per name via partial unique indexes (migration 092). Names: 2–32 `[A-Za-z0-9_]` + the shared blocklist. `price_emoji` 0 disables new sponsorships; pending reviews auto-refund after `emoji_sponsor_expire_days` (default 14, QOTD-sponsor sweep pattern) |
| Voice style | **0 (dark)**, suggested 30 | Leases Voice Master **rename + user limit** (sinks round 3, stage 3). Price 0 = paywall off (the shipped default AND the per-guild opt-out); pricing it on the Sinks page is the launch switch — announce first. Armed only while the economy is enabled. Entitlement is beneficiary-based (giftable); saved VM profiles stay stored but only re-apply while leased; lapse best-effort walks a live temp channel back to the template name + default limit (no role involved). Access dial / invite / kick / transfer / reset stay free. Verdict is pure (`voice_master/logic.style_lease_blocks`), enforced in `_apply_rename`/`_apply_limit` (one choke point for slash + panel) and the spawn profile loader |
| PvP game wager | player-chosen, uncapped | **Sinks round 2, stage 4b** (built 2026-07-20). Optional `wager:` on all six duel/group games: equal ante, winner takes the pot minus the optional house rake — `wager_rake_pct`, default **0 (dark)**, capped 50, added 2026-07-20 revising the round's original no-rake stance (at 0 a wager is still the pure transfer that made the games matter; a priced rake evaporates its cut of every settled pot, read at settlement time like rental renewals, never snapshotted). Refunds are never raked, nor is a single-stake pot (a winner reclaiming their own ante isn't a contest); the payout announcement and register memo both name the cut (`meta.rake`) so the arithmetic visibly adds up. Escrow in `econ_game_wagers` (migration 094) keyed to (game_type, game_id, user_id); duels declare at challenge and debit both sides at accept (decline/timeout costs nothing), lobbies debit on join and refund on leave. Settlement/refund rides the stage-4a terminal seam, exactly-once via `settled_at`. Every non-settling terminal state (ABANDONED / VOID / EXPIRED_LOBBY / DECLINED) and a `winner_id` of None refunds; a guild-leaver's stake is refunded by the economy cog's member-remove listener. Ledger kinds `wager_stake` / `wager_payout` / `wager_refund`, payout and refund unboosted so a wager can never mint |
| Raffle ticket | 10 each, ≤10/member/week | **Sinks round 3, stage 5.** Week-scoped tickets (`raffle_ticket` burn, no refunds); weighted draw at the ISO-week roll, exactly-once via the `econ_raffle_draws` PK (claim-before-side-effect). Prize is NEVER coins: a `free_week` voucher (28-day expiry) auto-covers the winner's next rental debit — renewal or first week of a new rent — as a 0-amount `rental` ledger row (`meta.voucher_id`). Winner DMed (opt-in-role gated) and **named** on the leaderboard panel's raffle section (the deliberate anonymous-ticker carve-out — buying in is opting in). `raffle_enabled` default **off**; enabling is a comms decision, announce first. Shop: ticket row + quantity modal (ephemeral + persistent panel). Migration 093 |
| Hoard tax (demurrage) | **0% (dark)**, suggested 2%/wk over a 500 floor | **Built 2026-07-20 (migration 100).** The only sink that needs no buyer: at the ISO-week roll, every wallet above `demurrage_threshold` loses `demurrage_rate_pct`% of the **excess** only — the floor is protected, so nobody is taxed below it and 100% is a hard wealth cap, not a wipe. Floor-division grace: a tax that rounds to 0 goes uncollected. Exactly-once via the `econ_demurrage_sweeps` (guild, week) PK — claim-before-debit, the raffle-draw pattern — with per-sweep totals recorded for metrics. Ledger kind `demurrage` (meta: closed week + pre-tax balance) narrated by the register feed (🐉 "Hoard tax") — no separate announcement. Rate 0 default = off; both knobs on the Sinks page; enabling is a comms decision, announce first (`economy_demurrage_service.py`, swept from the week roll beside the raffle draw) |
| Spotlight slot | 150 flat | **v2 (decided).** Featured embed in `spotlight_channel_id`, buyer text through the name blocklist, 7-day expiry, 3/ISO-week inventory |

**Curated role-icon catalog (currency sink).** Alongside bring-your-own icon
uploads, an admin can stock a per-guild catalog of named role icons, each with its
own weekly price, from the **Sinks** dashboard page — which also now **owns the flat
perk prices** (moved off the Settings panel). When a catalog exists, `/bank shop`'s
role-icon row shows the catalog's price span and how many icons back it
(`catalog_price_range` returns `(min, max, count)`), and its button becomes a
picker of curated icons (Discord caps the select at 25) instead
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
  `pay`, `quests`, `shop`, `gift`, `sponsor`, `emoji`, `role`, `mute`, `grant` [mod],
  `post-guide` [mod], `post-leaderboard` [mod], `post-shop` [mod])
  plus `/qotd post` [mod]
  and rooms-stage `/room …` — keeps the bot's top-level command budget flat. Command
  names are global; all *strings* inside are currency-branded.
  - **`/bank pay @member amount`** — transfer (§5); **`/bank shop`** — one ephemeral
    panel that both browses and configures. The listing is an aligned code-cell
    table in the quest-board's house style (one `label  blurb` cell, then the
    price — blurbs kept short enough that a row fits a phone-width line), grouped
    into price tiers — **Essentials** (name, color), **Signature** (gradient,
    icon, holographic), **For a friend** (a prose row — gifting has no single
    price to tabulate) — sorted by the guild's configured price
    inside each tier, with the viewer's balance in the description and the
    renewal fine print in the footer. Unrented rows carry an emoji-led **Rent**
    button (no price in the label), rented rows a green **customise** button
    opening the matching modal (name / color hex / gradient hexes /
    server-emoji icon) — except holographic, a fixed preset with nothing to
    pick, whose rented row shows an inert **Active** chip instead — with
    icon/gradient/holographic rows
    reflecting the server's role features and rented rows marked ✅. A fresh rental's
    confirmation carries the same customise button. Entitlements are
    beneficiary-based, so a *gifted* perk surfaces exactly like a self-rented
    one (customise button, ✅ mark). **`/bank gift @member perk:<choice>`** —
    pay to rent a friend any self-perk at its base price (eager role creation
    on the recipient); feature-gated perks check the guild gate, and gifting
    a perk the friend already has stops at an explicit "Gift anyway?" confirm
    (the rental would stack silently).
  - Each modal setter applies the matching rented component to the member's personal
    role (§6), re-checking entitlements on submit, subject to the blocklist / ΔE /
    feature gates. Emoji icons accept **this server's custom emojis only** (typed
    `:name:` or pasted; the bot stores the emoji's image; animated refused).
  - **`/bank role icon image:`** is the one surviving subcommand — modals can't take
    file uploads, so image icons (256KB max) still arrive via slash command. The
    former `name`/`color`/`gradient` subcommands are removed in favour of the shop's
    modals.
- **Channel guide panel (shipped):** **`/bank post-guide [channel]`** [mod] posts a
  single branded "how it works" embed (a **Notifications** field explaining the
  panel's own 🔔 toggle for the opt-in DM role — it replaced a **Joining** field
  that pointed at `<id:customize>` back when that role also gated the channels —
  then an **Earning** table — aligned what-pays-what rows in
  the leaderboard's fixed-width-cell style — and a **Spending** command table,
  with streak/booster/rental fine print collapsed into the footer — all
  templated from `EconSettings`) into a channel. Panel ids
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
  place — embed **and** view — another channel deletes + reposts). Button
  labels carry no price (the embed's table does), so re-pricing only needs
  the embed refreshed. Gifting stays command-only (`/bank gift` needs a
  target member, which a button can't carry).
- **Leaderboard panel (shipped, live):** **`/bank post-leaderboard
  [channel]`** [mod] posts a single live status embed — the economy's
  centerpiece surface. Content, top to bottom: **today's pulse** (guild-local
  coins paid / quests completed / distinct earners, plus dailies-reset and
  new-weeklies clocks as Discord relative timestamps, which tick client-side
  between edits); top 5 earners over a rolling 7 days (income = positive
  ledger sums excluding `transfer_in`, matching the Statistics page) each
  annotated **(+N today)**; community-goal progress bars — auto weeklies add
  tier state with the next tier's threshold, a daily-bucket pace verdict
  ("on pace"/"needs a push", same 90%-of-linear rule as `compute_live`),
  contributor count, today's contribution delta (from `econ_kind_activity`;
  omitted for channel-scoped goals the scope-blind activity ledger can't
  measure), and a week-end deadline clock; a **quest-board summary** — one
  line per board cadence ("**N** on your board, drawn from M" + reward
  range; a cadence sized 0 or with an empty pool is omitted) rather than the
  full pool, since members only ever face their personal draw; board-less
  "Anytime" (event) quests stay individually listed (capped at 12 lines),
  and the ⚡ spotlight line keeps its "until" clock; an **anonymous live
  feed** — today's paid completions
  aggregated per quest (title × count + latest relative timestamp, max 5
  lines, plus a full-board-bonus count; titles and counts only, never member
  names, per the 2026-07-18 ticker decision); and a blurb pointing members
  at `/quests` + `/bank wallet` for their own numbers. The panel carries a
  persistent **Show my quests** button (`econ:show_my_quests`, a static-id
  `QuestBoardView` re-registered at cog load and re-attached on every
  repaint) that opens the same ephemeral panel as `/bank quests` — the
  members' door from the anonymous board into their own personal draw.
  Sections stack
  full-width, each heading given breathing room by a zero-width blank
  line ending the previous section's value (and the description); each body is a small table — fixed-width inline-code cells
  align the columns (pulse label | value, earner name | amount, quest
  cadence | description | payment, feed title | count | when) while
  emoji, bold, and live `<t:…:R>` timestamps stay outside the backticks,
  where Discord still renders them (code blocks would freeze both). Panel ids persist as
  `econ_leaderboard_channel_id` / `econ_leaderboard_message_id` (guide-panel
  pattern: same-channel repost edits in place, another channel deletes +
  reposts). **Refresh is event-driven:** every economy credit
  (`apply_credit`), community-counter bump, and dashboard progress edit
  marks the guild dirty in `economy/live_signal.py` (process-local,
  import-free), and `leaderboard_live_loop` (20 s poll) repaints a dirty
  panel at most once per 120 s — so the panel moves within ~2 minutes of
  the action while a busy hour stays ≤30 edits. The hourly economy-loop
  pass (`run_guild_leaderboard`) remains the backstop for restarts and
  quiet drift. **Bottom-sticky:** like the guide panel, a member message in
  the panel's channel arms a debounced repost (`_restick_leaderboard_panel` →
  `_place_leaderboard_panel`, delete + repost fresh at the bottom, 6 s
  debounce, per-guild lock), so the stats panel stays the channel's last
  message. Because a re-stick changes the message id, the loop's 404 handler
  re-reads the id and only clears when it's unchanged — a moved panel is not
  a deleted one, so deleting the message (id still stored) is still how staff
  retire it. Collector + builder in
  `economy/leaderboard.py` (pure — Discord I/O stays in the cog/loops);
  ids are bot-managed and not dashboard-editable.
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

## 8. Private Rooms (Stage 6)

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

**"Happening now" (Statistics page, top card).** The live quest pulse
(`GET /api/economy/quests/live` → `compute_live`, manager-gated,
45 s panel auto-refresh): the running community weekly as a hero card
(progress bar, %, 40/70/100 tier chips, contributor count, daily-bucket
pace flag "on track"/"needs a push", time to the week roll — or a gap-week
note), per-quest **anonymous** completion counts for the current period of
every active daily/weekly/monthly quest (+ counted-quest in-flight counts),
event-quest totals (7d / ever), quests-done-today / this-week tickers, and
day/week reset countdowns. Aggregates only, never member names (2026-07-18
decision). The leaderboard embed mirrors this live view in-channel — see
the leaderboard panel bullet in §7 for its content and its event-driven
refresh (a tier-crossing bump repaints the panel within ~2 minutes, not on
the next hourly tick).

**Statistics page (Economy section).** A live, on-demand tuning surface
(`GET /api/economy/stats`, gated on `require_economy_manager` — manager
role or admin; member table capped at 500), complementing the weekly rollup with a
same-instant read of the ledger. It shows: **supply concentration** — total supply,
holder count, median balance, top-10% share, and Gini, all computed over **positive
balances only** (inequality of who-holds-what, not the zero-balance long tail); a
fixed-bucket **balance histogram**; an **income-sources stacked bar** — minted coins
per faucet group (logins / activity / quests / games / grants, the same `FAUCET_GROUPS`
split the rollup's `faucet_mix` uses) across the last **8 trailing 7-day buckets**, so the
*composition* of income and how it shifts week to week is visible at a glance (`transfer_in`
excluded, same as every other mint figure); **7-day flow** — minted vs burned with a burn
rate, plus transfer volume and grants (money definitions match the rollup: mint /
income exclude `transfer_in`, burn excludes `transfer_out`); a **per-member income
velocity table** (top holders by balance) with 7/30-day income, coins/day, 7d spend
(**every** sink kind, not just `rental` — it read rentals-only until consumables
shipped), top faucet group, live rentals, streak, and last-earned; **engagement** — earner
ratio (7d earners ÷ 30d active), spenders, quest claims, **quest approval rate**
(resolved paid ÷ paid+denied over 30d, resolved-only), and **hoard-weeks** (median
balance ÷ latest-rollup median weekly income); **perk affordability** in days of
median daily income per price field; the **biggest spenders board** (top 15 by
**lifetime** currency burned, with each member's share of the guild's whole burn
and the sink they spend most on); and the **top 5 transfer pairs** (30d, by
`transfer_out` magnitude) as the alt-funnel audit surface for transfer abuse (§12).
All ratios/divides are guarded (0 or `null` when there is no denominator).

The spenders board is deliberately **all-time, not a trailing window** — its job
is to make spending a standing status worth chasing, and a 7-day window would
erase that standing every week. Burn excludes `transfer_out` (sideways: the coins
land in another wallet, so nothing leaves the economy) and `qa_void` (a staff
clawback is a real removal but not a purchase, and crediting it would rank
someone top for having had a reward revoked). Shares are computed against the
guild's **total** burn, not the sum of the visible rows, so the top-15 cut can't
inflate its own percentages; ties break on user id so the table doesn't reshuffle
between refreshes.

## 10. Notifications

DM-first via a shared `try_dm`-style helper; on failure, fall back to the bank channel.
Per-member mute toggle via `/bank mute` (`econ_notify_prefs`; also surfaces on the v2
wallet page). The dm_perms system is
member-to-member consent and does **not** gate bot DMs — no interaction there.

| Event | Notify |
|---|---|
| Daily login digest (streak + quest recap, §3.1) | DM (opt-in role) |
| Quest approved / denied (with reason) | DM |
| Rental grace entered / lapsed | DM |
| Daily XP→currency conversion | Silent (ledger) |
| Sign-off claims, community settlements | Bank channel |

### 10.1 Register channel (public transaction feed)

`econ_register_channel_id` (dashboard: **Economy → Config → Register channel**)
turns on a running public feed of the guild's currency movements — a bank
register. Unset (`0`) is off; the picker **is** the toggle. It is deliberately
a *separate* channel from `bank_channel_id`: the bank channel is the
interactive approval surface (sign-off cards, ceiling alerts, DM fallback), and
posting register entries there would double up every signed-off quest.

**Source: the ledger, not the call sites.** The feed drains `econ_ledger` by
`id`. Since `apply_credit` / `apply_debit` are the only paths that mutate a
wallet, draining the ledger catches every movement — quest payouts, community
settlements, rentals and renewals, transfers, milestones, QOTD, game rewards,
and staff grants from both `/bank grant` and the dashboard — with no
per-call-site hook to forget.

**What it does NOT post** (`register.SKIP_KINDS`): `login` and `conversion` are
the automated per-member faucets — they fire once per active member and all
land together at the day roll, so posting them would bury every quest, purchase
and transfer under a nightly burst of routine noise. They still hit the ledger,
the wallet, and the metrics rollup; they are simply not news. `transfer_in` is
skipped because a transfer writes two rows for one event: the register posts the
`transfer_out` leg as a single consolidated "A → B" entry (unsigned, in its own
neutral colour — the currency moved sideways rather than entering or leaving the
economy) with the sender's resulting balance.

The skip list is applied **in SQL, before the LIMIT** — filtering after it would
let a midnight flood of login rows fill the batch and starve the entries anyone
wants to read. Balance reconstruction still walks the *unfiltered* rows: a
skipped login between two posted entries moved the wallet, and ignoring it would
print arithmetic that doesn't add up.

**Completions only.** A ledger row exists only once a transaction has happened,
so there is no per-tick progress spam. A counted quest's entry shows the final
tally instead ("Daily Chatterbox (5/5)"); for a *banded* quest the tally is the
member's own `effective_target`, resolved via the claim's period — never the
library's raw `target_count`, which no member necessarily worked to.

**Each entry says what it was for** (`register.render_memo`): `kind` + the
`meta` blob become a human memo, and a `quest` row resolves `meta.quest_id` to
the quest's title — the Venmo memo line. Every kind has a memo; an unknown
future kind degrades to its title-cased name rather than rendering blank.
Credits are green, debits red — here the colour is semantic, so this is a
deliberate exception to the `resolve_accent_color` convention. The footer
carries the balance that row produced, reconstructed per row (the live wallet
balance would be wrong for every entry but the last).

**Cursor.** `econ_register_cursor_id` is bot-managed bookkeeping (like the
`*_message_id` fields — readable via `GET /economy/config`, absent from the
editable whitelist). `-1` means "never seeded": the first drain seeds it to the
ledger's current `MAX(id)` and posts nothing, so switching the feed on never
replays history. `-1` rather than `0` because `0` is a legitimate seeded cursor
for a guild whose ledger is still empty — conflating them would re-seed past
that guild's first-ever transaction and swallow it. The cursor advances only
over rows actually posted (or deliberately skipped), and only after the sends
land, so a crash mid-drain replays the un-posted tail rather than losing it: at
worst a duplicate entry, never a silent gap. A `Forbidden` (missing perms)
leaves the cursor for a later retry.

## 11. Scheduled Work

Two loops, both registered via `bot.startup_task_factories` (each gets
`_resilient_task` crash-restart). The main economy loop ticks hourly; the
**register loop** (§10.1) ticks every `REGISTER_INTERVAL_SECONDS` (30s) because
a transaction entry is only useful while it is still news — the hourly tick
would batch a day's activity into lumps an hour apart. The register drain is
capped at `REGISTER_MAX_PER_TICK` (8) entries per guild per tick so a burst (a
community settlement paying dozens of members at once) spills into later ticks
instead of hammering the channel's rate limit, and skips rows older than
`REGISTER_STALE_SECONDS` (1h) — a backlog after downtime is noise, not news.

The hourly economy loop:

| On tick | Action |
|---|---|
| Guild-local day rolled | XP→currency conversion (only the most recent marked local day — no retroactive backlog after an outage, §12); streak evaluation (grace/reset); daily quest rotation; QOTD reward window closes |
| Every tick (hourly) | Rental billing + grace retries; pending-claim expiry; QOTD-sponsor and emoji-sponsor pending-review expiry (auto-refund); (v2) spotlight expiry; (rooms stage) room archive/purge |
| Guild-local ISO week rolled | Weekly quest activation; community settlement; raffle draw (§6); demurrage sweep (§6); metrics rollup; (v2) spotlight inventory reset |

The **coin-drops loop** (§3.5) is its own startup task on a 60 s tick:
expiry sweep first, then the per-guild jittered drop scheduler. Its
next-due times are deliberately in-memory — a restart just re-jitters each
guild's next drop, and open pouches survive because their Claim button is
a stateless `DynamicItem`.

A second, lightweight startup task — `leaderboard_live_loop` — gives the
leaderboard panel its near-real-time cadence: a 20 s poll over the
process-local dirty-guild set (`economy/live_signal.py`, marked by
`apply_credit` / community bumps / dashboard progress edits), repainting each
dirty panel at most once per 120 s. Deliberately in-memory: a mark lost to a
restart costs at most one hour, because the hourly tick refreshes every
panel anyway (§7).

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
  photo pays via the `photo_post` faucet (§4.5, on the post itself), not `end_game`.
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
(delivered: §4.4 trigger words plus the full §4.5 trigger-kind table —
`photo_post`, `game_win`, `duel_win`, `active_day`, and the rest — nothing
here remains parked) ·
fines/tickets (Jail exists — integration stays parked by design call) ·
room upgrades · streak-rental discount ·
auctions/seasonal drops · per-quest contributors-only payout · scheduled/auto
QOTD · soundboard-sound sponsorship (the emoji-sponsorship machinery pointed
at soundboard slots — build only if emoji lands well). *Delivered by sinks
round 3 (2026-07-19): giveaway entries (as the weekly raffle) and emoji
sponsorship — see §6.*
