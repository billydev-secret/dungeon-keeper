# Photo Challenge — post-gated channel payout

**Status:** shipped (single stage). Supersedes the reaction-gated payout model.

## Goal

Reward members for *posting* photos in a dedicated Photo Challenge channel. The
post itself pays — no replies to the card, no reactions required. Capped once per
guild-local day per member.

## Decisions (from the requester)

- **Trigger:** an image post (an attachment Discord recognises as an image) in the
  configured photo channel. Reactions and replies are irrelevant.
- **Cap:** **once per guild-local day** per member (occurrence key is the day,
  like `voice_session` / `boost`); the claim collision dedups, so several photos
  in a day still pay once.
- **Channel:** the standalone Photo Challenge feature's **dedicated channel**
  (`channel_id`) — the payout is dormant until it's set.

## Mechanism

Reuses the event-quest engine. Trigger kind `photo_post`.

- `EconomyCog._on_photo_post` — `on_message` listener. Guards, cheapest first:
  guild/bot check → image check (content-type with a filename-extension fallback)
  → TTL-cached channel check → DB eligibility pre-check (economy on, `photo_post`
  source on, ≥1 active `photo_post` quest) → `fire_trigger_quests` with
  `occurrence = local_day`, scoped to the photo channel. Announces ✅ (paid) or 📝
  (sign-off filed) on the member's photo.
- Retired from the previous model: the `on_raw_reaction_add` listener
  (`_on_photo_react`), the distinct-reactor count (`_distinct_reactors`), the
  `_photo_paid` recount-guard, the auto-react seeder (`_on_photo_autoreact`), and
  the `react_threshold` / `auto_react` Setup-panel fields. The `econ_photo_cards`
  table (already unused from the reply era) is still left in place, not dropped.

## Config surface

Standalone **Photo Challenge → Setup** panel (`photo-challenge.js` +
`photo_challenge.py`, storing into `games_game_config.options`, game_type
`photo`). The dedicated **`channel_id`** is all this mechanic reads; the reward
itself stays a `photo_post` quest in the Economy Quests studio.

## Migration

`099_photo_post_trigger.sql` rewrites `econ_quests.trigger_kind` and
`econ_income_sources.source` from `photo_react` to `photo_post`, so the live quest
(main guild id 17, "Picture This") keeps working under the new name. Idempotent.

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
