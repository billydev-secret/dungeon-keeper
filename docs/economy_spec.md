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
`manager_role_id`, `transfers_enabled`, `enabled`.

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
- **Event host 30 (mod grant):** `/bank grant @member amount reason` + Bank Manager
  panel button; manager-role or admin gated; audit-tagged in the ledger.

## 4. Quest System

### 4.1 Authoring (Bank Manager panel, gated on `economy_manager_role` or admin —
mirrors the `games_editor_role` / `require_game_host` dashboard pattern)
Fields: title, description, type (daily/weekly/community/event), reward, sign-off
tickbox, criteria (freeform v1), date range, repeat/auto-rotate tag, trigger words +
optional trigger channel (§4.4, daily/weekly only — hidden for community), event
trigger kind (§4.5, event only). Rewards free-entry with an amber out-of-band
warning; out-of-band saves fine, audit-tagged. Library model: **1 active daily + up
to 5 weeklies + 1 active event quest** per guild; dailies can auto-rotate from a
tagged pool (rotation happens on the guild-local day roll in the economy loop).

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
  Bank Manager panel's pending queue resolve the same claim; a dashboard resolution also
  best-effort edits the card and DMs the claimant over the shared event loop.

### 4.3 Community Quests
Guild-wide objective with a progress bar. Community quests are **not member-claimable** —
a manager drives `current` toward the target from the Bank Manager panel and `completed_at`
stamps once on the crossing. **Payout: flat, to every member active in the last 30 days**
(`member_activity` via `active_member_ids`). Settlement is exactly-once: a per-(quest, user)
row in `econ_community_payouts` is reserved before crediting (wellness-scheduler pattern),
so a replay pays only the members it missed and `settled_at` stamps last. **Sign-off gates
the sweep:** a sign-off community quest settles ONLY via the dashboard's manual Settle
(`settle_community_quest`); a plain one auto-settles on the weekly ISO-week roll in the
economy loop (`list_settleable_community_quests` filters out sign-off quests, so the
auto-sweep never pays one awaiting approval).

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

Trigger quests are **excluded from the `/bank quests` claim select** (state
`trigger` on the wallet page) — self-claiming without saying the phrase would
bypass the verification. Repeats inside a period fall out silently via the
per-period claim collision, so a busy "good morning" channel never gets error
spam. Per-guild trigger quests are cached in the cog for **60 s**
(`_TRIGGER_CACHE_TTL`), so a dashboard edit takes effect within a minute without
a restart; the cache also stores empty lists, keeping the per-message cost of
guilds without trigger quests to a dict lookup. Community quests never trigger
(not member-claimable).

### 4.5 Event Quests (trigger-paid, no calendar period)
An **event quest** (`qtype='event'`, `trigger_kind` names the trigger) is never
member-claimed: a bot listener pays it through the same `claim_quest` state machine
when its trigger fires, passing a **per-occurrence period key** instead of a
calendar one — so payouts dedupe per member per occurrence with **no time gate**.
`quest_period('event', …)` deliberately raises; only listeners build event periods.
Slot rule: **1 active event quest** per guild (two active would double-pay one
trigger); it occupies no daily/weekly/community slot. Not offered by the AI idea
generator. On the wallet page the quest shows a standing how-to (state =
`trigger_kind`), never a claim button.

**`photo_reply`** (the only v1 kind): when a Photo Challenge card posts (manual
`/games play photo` or the games scheduler — both go through `PhotoCog.launch`),
the cog records it in `econ_photo_cards` (message → game mapping; recorded even
with no quest active, so a quest activated later still pays for old cards). A
member's **Discord reply to a card carrying an image attachment** (content-type,
filename-extension fallback) claims with period `photo:<game_id>` — once per member
per card, forever; each new card is a fresh payout. Instant quests pay on the spot
(✅ + embed); sign-off files the pending claim and posts the bank-channel card, which
doubles as photo review. The Photo Challenge Games-Studio panel gained a
**ping-role option** (`ping_role_id` in `games_game_config.options`) mentioned with
every posted card — distinct from the per-schedule announce ping; don't set both.

`/bank pay @member amount` — min 1, whole numbers, no fee. **Confirmation step over
100** (an ephemeral confirm button before the debit lands). Both sides ledgered
(payer `transfer_out`, recipient `transfer_in`). Per-guild `transfers_enabled` toggle
(default **on**) is the kill switch for alt-funneling; `/bank pay` refuses with a
branded notice when it is off. **Transfers do not mint** — the recipient's
`transfer_in` credit takes **no** booster multiplier (the ×1.5 is a faucet-only patron
bonus); a transfer only moves existing currency between wallets.

## 6. Sinks (The Perk Shop)

**Shipped (Stage 3):** the role-customization perks (solid colour, name, icon,
gradient) and **gift-a-color** are live, browsable and rentable via `/bank shop`
and `/bank role` (§7). Private rooms stay **Stage 6** and the spotlight slot stays
**v2** — both still design-only below.

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

**Personal roles:** one per member, auto-created **positioned above the booster
cosmetic swatch band** (the "#### Cosmetics" anchor) so a rented colour wins the
display-colour contest — the position is set **on create only** (a reconcile never
re-hoists a manually moved role). The projector is idempotent: it reconciles the role
to the member's current entitlements (name / colour / gradient / icon) and downgrades
cleanly when a component lapses. Guards: a **ΔE ≥ 25 collision check against staff role
colours** (a too-close colour is refused, the message naming the staff role it clashes
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
  - **`/bank pay @member amount`** — transfer (§5); **`/bank shop`** — browse the perk
    catalogue with branded prices, icon/gradient rows reflecting the server's role
    features; **`/bank gift @member <perk>`** — pay to rent a friend a solid colour
    (eager role creation on the recipient).
  - **`/bank role`** subgroup drives the shipped personal-role perks (Stage 3):
    **`name`**, **`color`**, **`gradient`**, **`icon`** — each applies the matching
    rented component to the member's personal role (§6), subject to the blocklist / ΔE
    / feature gates.
- **Channel guide panel (shipped):** **`/bank post-guide [channel]`** [mod] posts a
  single branded "how it works" embed (earning streams with live rates, shop prices,
  command crib sheet — all templated from `EconSettings`) into a channel. Panel ids
  persist as `econ_guide_channel_id` / `econ_guide_message_id` (Voice Master
  panel pattern): re-running in the same channel edits the panel in place (use after
  re-pricing/re-branding); pointing at another channel deletes the old panel and
  reposts. Builder in `economy/guide.py`; the two ids are bot-managed and not
  dashboard-editable.
- **Manager surface (dashboard):** the **Bank Manager** panel — a top-level nav section
  gated on `economy_manager_role_id` or admin (mirrors `games_editor_role` /
  `require_game_host`) — is where managers author quests, work the pending sign-off queue
  (Approve/Deny), set community progress + run manual Settle, grant, and read the ledger
  audit. It is the dashboard counterpart to the `[mod]` `/bank grant` and `/qotd post`
  commands.
- **Role customization in v1** happens via `/bank role` subcommands + modals
  (name / color hex / gradient / icon emoji-or-upload), proxied through the bot.
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

**Statistics page (Bank Manager).** A live, on-demand tuning surface under Bank
Manager (`GET /api/economy/stats`, gated on `require_economy_manager` — manager
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
