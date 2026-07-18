# Photo Challenge — react-gated channel payout

**Status:** shipped (single stage). Supersedes the reply-to-card payout model.

## Goal

Reward members for *posting* photos in a dedicated Photo Challenge channel,
gated on the community *liking* them, instead of rewarding a threaded reply to
the prompt card. A post that earns enough distinct reactions pays out, capped
once per guild-local day. The bot can seed a reaction on each photo so members
have a one-tap way to pile on.

## Decisions (from the requester)

- **Trigger:** an image post in the configured photo channel that earns
  **5 distinct human reactions** (the author and bots never count). Threshold is
  configurable; 5 is the default.
- **Cap:** **once per guild-local day** per member (occurrence key is the day,
  like `voice_session` / `boost`).
- **Channel:** the standalone Photo Challenge feature's **dedicated channel**
  (`channel_id`) — payouts and auto-react are dormant until it's set.
- **Auto-react:** an optional emoji the bot seeds on each photo.

> **Landed alongside a parallel refactor.** While this was in flight,
> commit `88c5125` turned Photo Challenge into a standalone dashboard feature
> with its own dedicated channel + schedule (routes `photo_challenge.py`, panel
> `photo-challenge.js`). This work integrates onto that: it reads *that* feature's
> `channel_id` and adds `react_threshold` + `auto_react` to *its* Setup panel,
> rather than the since-removed generic games panel.

## Mechanism

Reuses the event-quest engine. Trigger kind `photo_reply` → **`photo_react`**.

- `EconomyCog._on_photo_react` — `on_raw_reaction_add` listener. Guards, cheapest
  first: TTL-cached channel check → DB eligibility pre-check (economy on,
  `photo_react` source on, ≥1 active `photo_react` quest) → fetch message,
  image + non-bot author → raw-total prune → distinct-reactor count (unions
  reactor sets across every emoji, drops author + bots) → `fire_trigger_quests`
  with `occurrence = local_day`, scoped to the photo channel. A per-process
  `_photo_paid` set stops recounting a post once it has crossed.
- `EconomyCog._on_photo_autoreact` — `on_message` listener; adds the configured
  emoji to image posts in the photo channel. The bot's reaction can't inflate
  the tally (bots are excluded from the distinct count).
- Retired: `_on_photo_reply`, `record_photo_card` / `get_photo_card`, and the
  card-recording call in `PhotoCog.launch`. The `econ_photo_cards` table is left
  in place (unused), not dropped.

## Config surface

Standalone **Photo Challenge → Setup** panel (`photo-challenge.js` +
`photo_challenge.py`, storing into `games_game_config.options`, game_type
`photo`). The dedicated **`channel_id`** already existed there; this work adds
**`react_threshold`** (number, default 5) and **`auto_react`** (emoji text,
blank = off). The reward itself stays a `photo_react` quest in the Economy
Quests studio.

## Migration

`079_photo_react_trigger.sql` rewrites `econ_quests.trigger_kind` and
`econ_income_sources.source` from `photo_reply` to `photo_react`, so a live quest
(main guild id 17, "Picture This") keeps working under the new name. Idempotent.

## Cadence note

The once-per-day cap is the quest *cadence*: a **daily** quest pays once/day, an
**event** quest keyed on the day pays once/day, a **weekly** quest pays once/week.
"Picture This" is weekly, so it pays once per week per qualifying member — flip it
to daily if strict once-per-day is wanted.
