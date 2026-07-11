# Economy & Perk Shop — Spec V3.1 (repo-grounded)

**Brandable per-guild currency · Logins & streaks · XP conversion · Quests · Rentable perks**
*Status: Design spec (unbuilt — implementation plan in `docs/plans/economy-and-perk-shop.md`).
Supersedes the uploaded V3 draft. All numbers are per-guild-tunable defaults (§9).*

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
- **New XP source `reaction_given`** (decided): reacting to someone else's message pays
  the *reactor* XP — default 0.34 (double the existing image-react rate), tunable via
  `xp_coeff_reaction_given_xp`. Feeds regular XP/leaderboards *and* (via conversion)
  currency. Guards: no self-reactions, no bots, one award per (message, reactor) ever
  (dedup table), so react/unreact can't farm.
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
  resolution point.
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
Fields: title, description, type (daily/weekly/community), reward, sign-off tickbox,
criteria (freeform v1), date range, repeat/auto-rotate tag. Rewards free-entry with an
amber out-of-band warning; out-of-band saves fine, audit-tagged. Library model:
**1 active daily + up to 5 weeklies** per guild; dailies can auto-rotate from a tagged
pool (rotation happens on the guild-local day roll in the economy loop).

### 4.2 Member Flow
- `/bank quests` + wallet page: active quests, progress, claim state.
- **Instant quests:** claim → immediate payout.
- **Sign-off quests:** claim → card in the bank channel with Approve/Deny buttons
  (persistent views, re-registered on restart like other recoverable views). Approve
  pays; Deny DMs the reason. Denied claims are re-claimable within the quest period;
  one pending claim per quest per member; pending claims auto-expire (→ re-claimable)
  after 7 days via the hourly loop.

### 4.3 Community Quests
Guild-wide objective with a progress bar. **Payout: flat, to every member active in the
last 30 days** — sourced from the existing `member_activity` table. Settlement is
exactly-once: a per-(quest, user) payout row is reserved before crediting
(wellness-scheduler pattern). Sign-off tickbox gates settlement if enabled.

## 5. Transfers

`/bank pay @member amount` — min 1, whole numbers, no fee. Confirmation step over 100.
Both sides ledgered. Per-guild `transfers_enabled` toggle (default **on**) is the kill
switch for alt-funneling.

## 6. Sinks (The Perk Shop)

Weekly rentals bill on personal anniversary tick. Defaults below; every price per-guild
tunable (§9).

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

**Personal roles:** one per member, auto-created in a fixed hierarchy band anchored by a
named anchor role (`guild.edit_role_positions` under an anchor — the exact mechanism
`booster_roles.sync_swatches` uses), positioned **above** the booster cosmetic swatch
band so a rented color wins the display-color contest. ΔE collision check against staff
colors. Deleted when all role-perks lapse; role-count alert at 200.

**Rental engine:** hourly billing loop → debit on `next_bill_at` → on failed debit,
36h grace with hourly retries and one DM → revoke on expiry (`lapsed`), re-purchasable.
Restart-safe exactly-once via claim-before-side-effect on the rental row
(`scheduled_games_service` pattern: state is advanced before the Discord side-effect).
Dashboard cancel runs to end of paid week, no refunds. Leave/ban = immediate cancel +
cleanup (member-remove listener).

## 7. Member Surface

- **Discord is the entire v1 member surface** (decided — the member-facing dashboard
  wallet page and role studio are v2). One top-level group **`/bank`** (`wallet`,
  `pay`, `quests`, `shop`, `role`, `mute`, `grant` [mod]) plus `/qotd post` [mod] and
  rooms-stage `/room …` — keeps the bot's top-level command budget flat. Command names
  are global; all *strings* inside are currency-branded.
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
module): median & p90 weekly income, minted vs burned week-over-week (flag >20% MoM
supply growth with flat rental uptake), faucet mix, rental uptake & churn, streak
health, and **pricing hints** ("solid color ≈ 50% of median weekly income") computed
from the ledger and shown beside each price field in the config panel.

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
| Guild-local day rolled | XP→currency conversion; streak evaluation (grace/reset); daily quest rotation; QOTD reward window closes |
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
- Guild loses Level 2 / Enhanced Role Styles mid-rental: perk enters grace as if unpaid?
  No — billing pauses the affected perk (icon/gradient) and DMs the owner; auto-resumes
  when the feature returns (no charge while suspended).

## 13. V2 (committed) & Parking Lot

**V2 — committed, not parked (decided):** member wallet dashboard page · role studio
panel with live preview · spotlight slots.

**Parking lot** (unchanged from V3 unless noted): auto-tracked quest criteria ·
fines/tickets (Jail exists — integration stays parked by design call) · giveaway
entries · emoji sponsorship · room upgrades · streak-rental discount ·
auctions/seasonal drops · per-quest contributors-only payout · scheduled/auto QOTD.
