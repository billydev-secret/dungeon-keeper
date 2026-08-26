-- Migration 182: per-perk shop switches replace "price 0 means off".
--
-- Every shop line an admin can switch off now has its own `econ_shop_<perk>_
-- enabled` config row, set from a checkbox on the Shop & Perks page. Before
-- this, a price of 0 was the de-facto off switch, and it meant three different
-- things depending on which dial you set it on:
--
--   * `price_streak_shield` 0 -> the shop row disappeared;
--   * `price_emoji` 0 -> no NEW sponsorships, existing ones billed on;
--   * `price_voice_style` 0 -> the paywall disarmed and every member got the
--     voice rename/limit controls FREE — the opposite of "off".
--
-- Nothing here changes any price. This is purely a backfill so the new
-- checkboxes start out saying what each guild's prices already said, and it is
-- deliberately written to be a no-op for the guilds that were never using
-- 0-as-off. From here on, 0 is a price of zero and nothing else.
--
-- Defaults (in EconSettings, not written here — an absent row IS the default):
-- every perk ON except `voice_style`, which is OFF because `price_voice_style`
-- defaults to 0. That keeps a guild which has never configured the economy on
-- free voice controls instead of waking it up behind a paywall.
--
-- Two backfills, both conditioned on an EXPLICIT stored price, so a guild with
-- no econ config at all is left entirely alone and keeps the defaults above.

-- 1. A guild that priced the voice lease above 0 was selling it. Switch it on,
--    or the checkbox default (off) would silently disarm a live paywall and
--    hand out the controls for free. This is the one row that matters in prod:
--    both live economies price the lease (40 and 900) and members hold rentals
--    against it.
INSERT OR IGNORE INTO config (guild_id, key, value)
SELECT guild_id, 'econ_shop_voice_style_enabled', '1'
FROM config
WHERE key = 'econ_price_voice_style'
  AND CAST(value AS INTEGER) > 0;

-- 2. A guild that set the shield price to exactly 0 was using it as the off
--    switch (that dial hid the row). Carry that intent onto the checkbox.
--    Matches no guild in prod today; it exists so the meaning of an existing
--    setting survives the migration wherever it was actually used.
INSERT OR IGNORE INTO config (guild_id, key, value)
SELECT guild_id, 'econ_shop_streak_shield_enabled', '0'
FROM config
WHERE key = 'econ_price_streak_shield'
  AND CAST(value AS INTEGER) = 0;
