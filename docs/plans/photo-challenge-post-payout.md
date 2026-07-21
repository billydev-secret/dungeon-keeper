# Photo Challenge — post-gated channel payout

**Status:** shipped (single stage). Supersedes the reaction-gated payout model.

## Goal

Reward members for *posting* photos in a dedicated Photo Challenge channel. The
post itself pays — no replies to the card, no reactions required. Two stacking
payouts, each capped once per guild-local day per member: a **flat participation
award** for showing up, plus the **`photo_post` quest** bonus on top.

## Decisions (from the requester)

- **Trigger:** an image post (an attachment Discord recognises as an image) in the
  configured photo channel. Reactions and replies are irrelevant.
- **Two payouts:** a flat participation award (default **5**, no quest required)
  **plus** the `photo_post` quest reward if a quest is active — they stack.
- **Cap:** each side pays **once per guild-local day** per member; posting several
  photos in a day pays each side once.
- **Channel:** the standalone Photo Challenge feature's **dedicated channel**
  (`channel_id`) — the payout is dormant until it's set.

## Mechanism

Two independent payouts in one `on_message` listener, `EconomyCog._on_photo_post`.
Guards, cheapest first: guild/bot check → image check (content-type with a
filename-extension fallback) → TTL-cached channel check → DB eligibility pre-check
(economy on, `photo_post` source on, and something to pay). Then:

1. **Flat participation award** — `EconSettings.reward_photo_post` (default 5,
   0 = off) via `apply_credit(kind="photo_post")`, deduped once per local day by
   an `INSERT OR IGNORE INTO econ_photo_rewards (guild_id, user_id, local_day)`
   anchor riding the credit's transaction (mirrors the login faucet).
2. **Quest bonus** — `fire_trigger_quests(..., "photo_post", occurrence=day)`,
   scoped to the photo channel, deduped on `econ_quest_claims`.

The `photo_post` income-source toggle gates both. Announces ✅ (paid) or 📝
(sign-off filed) on the member's photo — the quest outcome carries the react, or
the flat award adds a ✅ when no quest fired.
- Retired from the previous model: the `on_raw_reaction_add` listener
  (`_on_photo_react`), the distinct-reactor count (`_distinct_reactors`), the
  `_photo_paid` recount-guard, the auto-react seeder (`_on_photo_autoreact`), and
  the `react_threshold` / `auto_react` Setup-panel fields. The `econ_photo_cards`
  table (already unused from the reply era) is still left in place, not dropped.

## Config surface

- **Photo Challenge → Setup** panel (`photo-challenge.js` + `photo_challenge.py`,
  `games_game_config.options`, game_type `photo`) — owns the dedicated
  **`channel_id`**, all this mechanic reads for *where*.
- **Economy → Income Sources** page — the flat **`reward_photo_post`** rate is
  edited here alongside the other faucets (admin-gated `PUT /economy/config`),
  and the `photo_post` on/off source toggle lives on the same page.
- **Economy → Quests** studio — the optional stacking bonus stays a `photo_post`
  quest with its own reward.

## Migrations

- `099_photo_post_trigger.sql` rewrites `econ_quests.trigger_kind` and
  `econ_income_sources.source` from `photo_react` to `photo_post`, so the live
  quest (main guild id 17, "Picture This") keeps working under the new name.
  Idempotent.
- `101_econ_photo_rewards.sql` adds the `econ_photo_rewards` once-per-day dedup
  anchor for the flat participation award.

## History

- **Reply model** (original): `photo_reply` paid a member who replied to the
  prompt card with a photo. Migration `068` introduced it; `079` renamed it.
- **Reaction model** (`079_photo_react_trigger.sql`): `photo_react` paid a member
  whose image post drew N distinct human reactions (default 5). Superseded here.

## Cadence note

The once-per-day cap is the quest *cadence*: a **daily** quest pays once/day, an
**event** quest keyed on the day pays once/day, a **weekly** quest pays once/week.
"Picture This" is weekly, so it pays once per week per qualifying member — flip it
to daily if strict once-per-day is wanted.
