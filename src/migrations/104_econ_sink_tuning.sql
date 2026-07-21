-- Migration 104: sink tuning for the main guild (thegoldenmeadow, 1469…666).
--
-- Sink review found only ~4.4% of minted currency is ever spent (9 of 104
-- members). Two changes here, both taking effect on the next restart so they
-- land alongside an announcement:
--
-- 1. Voice lease ON at 30. `price_voice_style` ships at 0 (dark), which keeps
--    Voice Master rename + user-limit free for everyone. Setting a price is the
--    launch switch, so it is scoped to the main guild only; the secondary guild
--    can opt in from the Sinks dashboard. INSERT OR IGNORE so a value already
--    set by hand is respected.
--
-- 2. Premium-perk nudge: role gradient 120 -> 150. Entry perks (role color 50,
--    role name 35) are deliberately left cheap so newcomers can still buy in;
--    only the premium tier moves. Guarded on the old value so a hand-tuned
--    price is not clobbered.
--
-- The raffle (the broadest sink) is intentionally NOT enabled here — it names
-- the winner publicly, so flipping `raffle_enabled` stays a dashboard action
-- taken after the raffle is announced.

INSERT OR IGNORE INTO config (guild_id, key, value)
VALUES (1469491362444480666, 'econ_price_voice_style', '30');

UPDATE config
SET value = '150'
WHERE guild_id = 1469491362444480666
  AND key = 'econ_price_role_gradient'
  AND value = '120';
