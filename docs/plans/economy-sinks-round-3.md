# Economy sinks, round 3 (perks: gifts, shield, voice lease, emoji, raffle)

**Status:** stage 1 built (2026-07-19), stages 2–5 planned · **Owner:** economy · **Spec:** `docs/economy_spec.md` §6

> **Naming note:** planned in a parallel session as "round 2" before
> `economy-sinks-round-2.md` (rerolls · QOTD sponsorship · burn list · PvP
> wagers) merged first and kept the name. This round is complementary — that
> one builds the consumable tier; this one broadens the rental/perk tier.
> Stage 1's commits (4596d23, 5b5e7a0) reference the old name and migration
> number 090; the migration shipped as **091**⁠_econ_rentals_generic_gifts.

## Goal

The economy has minted ~25,200 coins to date; rentals — the only real sink —
have pulled back ~650 (≈2.6%). Wallets inflate, so prices stop meaning
anything. This round adds five sinks, chosen for infrastructure reuse and
recurring drain, plus one structural cleanup (generic gifting):

1. **Gift expansion** — gift any role perk, not just color (Stage 1)
2. **Streak shield** — prepaid insurance against a streak reset (Stage 2)
3. **Voice style lease** — Voice Master rename + user-limit become a leased
   perk (Stage 3)
4. **Emoji sponsorship** — pay weekly to keep a custom emoji in the server
   (Stage 4)
5. **Weekly raffle** — tickets in, a free-perk-week voucher out (Stage 5)

Driven by user request 2026-07-19: "Let's plan for 1/3/4/5. Let's make voice
channel customization a leasable thing."

## Locked decisions (user Q&A 2026-07-19)

| Decision | Choice |
|---|---|
| Voice lease scope | **Paywall rename + limit** — they become leased controls; the access dial, invite/kick/transfer, and reset stay free |
| Streak insurance shape | **Prepaid shield** — bought in advance, held (max 1), auto-consumed when a reset would land |
| Raffle prize | **Free-perk-week voucher** — no coin jackpot; ticket revenue is a pure burn |
| Emoji approval flow | **Pay first, refund on deny** — escrow debit at submission, compensating credit if a mod denies |
| Gift representation (implementer call, flagged) | Generalize: a gift is the **base perk kind with `beneficiary_id` ≠ `user_id`**; live `gift_color` rows are rewritten to `role_color` in the Stage 1 rebuild, `price_gift_color` retires |

## Grounding (what exists, file:line)

- `econ_rentals` CHECK constraint lists the perk kinds
  (`src/migrations/065_economy_rentals.sql:32`); extending it means a SQLite
  table rebuild — done **once**, in Stage 1, adding every kind this plan needs
  (`voice_style`, `emoji`) so later stages don't rebuild again. The `_PERKS`
  tuple mirror lives at `economy_rentals_service.py:59`.
- `beneficiary_id` is already generic: the live-rental unique index is keyed
  on it, entitlement lookup is beneficiary-based
  (`economy_rentals_service.py:487`), and member-leave cleanup already handles
  both giver and beneficiary sides (`:257`).
- Login streaks: `econ_streaks` (migration 063) +
  `evaluate_login`/`login_amount` in `economy/logic.py` (pure, table-tested).
  Free grace already bridges **one** missed day per rolling 7
  (`GRACE_WINDOW_DAYS`, `logic.py:17`); the shield covers what grace can't.
- Voice Master: panel actions in `voice_master/logic.py` (`_GROUP_ACTIONS`,
  ~line 1190: settings group = rename/limit/reset); per-member persisted
  settings in `voice_master_profiles` (migration 005) re-apply on spawn.
- ISO-week roll: `economy_loop.py` detects `last_iso_week` change and runs
  weekly activation → community settlement → metrics rollup; the raffle draw
  joins that chain.
- The claims queue (`econ_quest_claims`, migration 064) is quest-shaped
  (`quest_id NOT NULL`); the emoji approval queue **copies** its
  pattern/states rather than reusing the table.
- Perk prices are `EconSettings.price_*` fields (`economy_service.py:83-89`),
  owned by the dashboard **Sinks** page. Rental billing snapshots price at
  rent time and re-reads the current price each anniversary.
- Migration numbering: runner keys by filename; next free number is **090**
  (highest today: 089).

## Stage 1 — rental rebuild + generic gifting

**Migration 091** (the one table rebuild; planned as 090, renumbered at merge — the parallel round took `090_qotd_sponsor`):

- Recreate `econ_rentals` with perk CHECK `('role_color','role_name',
  'role_icon','role_gradient','voice_style','emoji')`; copy rows across,
  rewriting `perk='gift_color'` → `'role_color'` (beneficiary already differs
  from user on those rows, so the unique live index still holds).
- Add `shields INTEGER NOT NULL DEFAULT 0` to `econ_streaks` (used Stage 2 —
  riding along so streaks isn't touched twice).

**Gifting:**

- `/bank gift @member perk:<color|name|icon|gradient|voice-style>` — the perk
  becomes a choice; price is the **base perk price** (no separate gift price;
  `price_gift_color` field + Sinks input retire). Confirmation step stays.
- Feature gates apply against the *guild* as usual (icon needs `ROLE_ICONS`,
  gradient needs Enhanced Role Styles); ΔE/blocklist guards apply when the
  **beneficiary** customises, exactly as for self-rentals.
- Recipient customises via `/bank shop` — gifted rows surface with the
  customise button (generalizing today's "Set gifted color" special case).
- Gifting a perk the recipient already self-rents is allowed (unique index
  keys on the giver) but the confirm step warns "they already have this".
- Ledger rendering: historical `gift_color` ledger meta stays as-is;
  `register.py`'s unknown-kind fallback already title-cases old labels.

**Touchpoints:** `economy_rentals_service.py` (`_PERKS`, gift path),
`perk_actions.py`, the shop embed's "For a friend" tier copy, Sinks page
(drop gift price input), spec §6/§7, manual.html, README.

**Tests:** migration round-trip (gift_color rows land as role_color with
intact beneficiary + state), gift-any-perk rent/billing/leave-cleanup paths,
warn-on-duplicate, gate checks per perk. Existing gift_color tests convert.

## Stage 2 — streak shield

- **Purchase:** one-shot (not a rental). New shop row + `ShopRentButton`
  variant; debit ledger kind `streak_shield`; `price_streak_shield`
  (default 30) on the Sinks page. Refused while already holding one
  (`shields` cap = 1). Purchasable any time, even at streak 0.
- **Consumption (pure logic):** extend `evaluate_login` with `shields_held`.
  Rule: a gap of `g` days has `g-1` missed days; covers available =
  free grace (if the rolling-7 window allows) + shields. If missed ≤ covers,
  the streak continues and covers are consumed grace-first; otherwise reset.
  Net effect with max 1 shield: gap 2 survives on grace *or* shield; gap 3
  survives only with both; gap ≥4 always resets. Deterministic, table-tested
  like the existing evaluator.
- **Surfaces:** wallet shows "🛡️ shield held"; the login-day credit line
  notes "shield consumed" when it fires (piggybacks the existing notice —
  gated `notify_member(require_game_role=True)` like all recurring economy
  DMs). Guide panel fine print gains one clause.

**Tests:** evaluator table tests for every gap × grace × shield combination
(the reset condition is the safety-critical branch), purchase dedup, ledger
row, cap enforcement.

## Stage 3 — voice style lease

New perk kind `voice_style` (CHECK already extended in Stage 1),
`price_voice_style` default 30/week (distinct from the Stage-6
`price_voice_room` 200 — that remains private *rooms*).

- **Gate:** the Voice Master panel's **rename** and **limit** actions check an
  active `voice_style` entitlement (beneficiary-based, so it's giftable via
  Stage 1). Without it: branded refusal pointing at `/bank shop`. The access
  dial, invite/kick/transfer, and reset stay free.
- **Spawn:** `voice_master_profiles.saved_name`/`saved_limit` apply on spawn
  **only while leased**; otherwise template name + no cap. Profiles are kept
  stored (dormant), so re-renting restores the member's setup — no data loss.
- **Lapse:** rental-lapse hook best-effort reverts a *live* channel (template
  name, limit 0) as a post-commit effect, mirroring role de-projection; state
  advances before the Discord call (claim-before-side-effect).
- **No suspension path needed** — no guild feature dependency.
- **Rollout note (user-facing nerf):** rename/limit are free today and members
  have saved names. Ship dark, announce via the announcements feature before
  enabling; consider a launch week at price 0 (the Sinks page can do that
  already). Decision on timing stays with the user.

**Touchpoints:** `voice_master/logic.py` (action gate — keep the check in the
logic layer so it's testable), the VM cog's rename/limit handlers, shop
tiers ("Essentials"), spec §6 + voice_master_spec, manual, README.

**Tests:** gate branch (leased/unleased/grace/lapsed), spawn-applies-profile
matrix, lapse revert claim-ordering, gift-of-voice-style entitlement.

## Stage 4 — emoji sponsorship

**Migration 091:** `econ_emoji_submissions` (id, guild_id, user_id, name,
image_path, animated, price, state CHECK `pending/approved/denied/cancelled`,
escrow ledger id, emoji_id, created_at, resolved_at, resolver_id,
deny_reason) — the claims-queue pattern, not the claims table.

- **Submit:** `/bank emoji image: name:` (uploads can't ride modals — same
  reason `/bank role icon` survived). Name: 2–32 chars, `[A-Za-z0-9_]`,
  voice-master blocklist matcher, collision check against existing guild
  emojis and pending submissions. **Escrow debit at submit** (ledger kind
  `emoji_sponsor`, one week's price snapshot). One pending submission per
  member; `/bank emoji` bare shows status + a cancel button (cancel =
  self-deny → refund).
- **Queue:** pending list on the **Sinks** page (it owns sink config) with
  image preview, Approve/Deny(+reason). Deny/cancel → compensating credit
  `emoji_refund` (plain credit — no booster multiplier, mirrors the
  transfers-don't-mint rule).
- **Approve:** upload the emoji, create the rental (`perk='emoji'`, meta
  `{submission_id, emoji_id, name}`, `next_bill_at = approve + 7d` — the
  escrow already paid week one). Renewals bill current `price_emoji` /
  `price_emoji_animated` (defaults 60/90). **Lapse deletes the emoji**
  (post-commit side effect, state first). Guild-loses-slot mid-rental can't
  happen (deletion frees a slot), so no suspension path.
- **Caps:** `emoji_sponsor_slots` setting (default 5) counted against
  pending+active submissions, and a live free-slot check (static/animated
  counted separately) at submit *and* approve time.
- **Follow-on noted, not in scope:** soundboard-sound sponsorship is this
  exact machinery pointed at soundboard slots — build only if emoji lands
  well.

**Tests:** escrow/refund ledger symmetry, name guards (blocklist = safety
gate), slot caps both checkpoints, approve→rental wiring, lapse-deletes
claim-ordering, cancel path, one-pending enforcement.

## Stage 5 — weekly raffle

**Migration 092:** `econ_raffle_tickets` (guild_id, iso_week, user_id, count)
and `econ_vouchers` (id, guild_id, user_id, kind CHECK `'free_week'`, state
CHECK `issued/redeemed/expired`, source, created_at, expires_at, redeemed_at,
rental_id).

- **Settings (Sinks page):** `raffle_enabled` (default **off**),
  `price_raffle_ticket` (default 10), `raffle_max_tickets` per member/week
  (default 10 — caps whale certainty; with N entrants a full book is at most
  10/(10+rest) odds).
- **Buy:** shop-panel row + `/bank shop` row with a quantity modal; debit
  ledger kind `raffle_ticket`. No refunds; tickets are week-scoped.
- **Draw:** at the ISO-week roll in `economy_loop.py`, after community
  settlement: weighted pick over last week's tickets; **record the draw row
  before any side effect** (restart-safe exactly-once, the
  scheduled-games pattern). No winner if zero tickets. Coins are never paid
  out — ticket revenue is a pure burn; the prize costs only forgone revenue.
- **Voucher:** winner gets a `free_week` voucher, 28-day expiry. Redemption is
  automatic: the next rental debit for that member (renewal or first week of
  a new rent) is covered — billing writes a 0-amount ledger row with
  `meta.voucher_id` and marks the voucher redeemed. Grace-state retries may
  also redeem it (it's a payment).
- **Announce:** leaderboard panel gains a raffle section (this week's ticket
  count + entrant count + draw clock; after the draw, the winner **named** —
  buying a ticket is opt-in to that, a deliberate carve-out from the
  anonymous-ticker rule); winner also DMed via
  `notify_member(require_game_role=True)`.

**Tests:** weighted-draw determinism under a seeded RNG, zero-ticket week,
exactly-once draw across a simulated restart, voucher redemption on renewal
vs new rent vs expiry, max-ticket cap, disabled-guild no-op.

## Cross-cutting

- **Price defaults recap** (all per-guild tunable on Sinks):
  voice_style 30 · streak_shield 30 · emoji 60 · emoji_animated 90 ·
  raffle_ticket 10. `price_gift_color` retires.
- Every stage updates `docs/economy_spec.md` §6/§7 (+ §13 parking-lot
  pruning), `manual.html` + help-sections routing, and README's command
  reference **in its own commit**; behavior commits end with a `Testing:`
  checklist for the QA tracker.
- New ledger kinds (`streak_shield`, `emoji_sponsor`, `emoji_refund`,
  `raffle_ticket`) need labels in `register.py`'s kind map and inclusion
  rules checked in stats/leaderboard income queries (refunds must not count
  as income; the existing `transfer_in` exclusion is the precedent).
- Embeds take `resolve_accent_color`; success/failure keep semantic colors.

## Parked / explicitly out of scope this round

Duel coin-wagers with house rake (candidate #2 — not picked), bounties,
QOTD/prompt sponsorship, soundboard sponsorship (noted in Stage 4),
pay-to-pin. Confession-adjacent paid features remain rejected: a ledger row
naming the payer deanonymizes the feature.
