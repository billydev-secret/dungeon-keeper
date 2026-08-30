-- Migration 191: clear three economy config keys nothing reads any more
-- (2026-08-29 economy channel-settings audit).
--
-- 1./2. `econ_leaderboard_channel_id` / `econ_leaderboard_message_id`. Retired
--    on 2026-08-18 when the how-to guide and the leaderboard merged into one
--    panel: the surviving message kept the `guide_*` pair, and these two were
--    zeroed on the first boot after the merge by a one-shot startup task.
--    They were kept as dataclass fields only so a guild whose bot had not yet
--    restarted still loaded, and so that one-shot could see what to clean up.
--
--    Every guild has now restarted past it, which is what makes this safe:
--    guilds 1469…666 and 1476…484 both hold an explicit 0, and 1358…618 has
--    no row at all. Nothing is left for the one-shot to find, so it and
--    `economy.logic.plan_panel_merge` go in this commit too. The rows are
--    deleted rather than left at 0 because a stale key is a key someone later
--    mistakes for a setting.
--
-- 3. `econ_price_gift_color`. The `gift_color` perk kind was retired in
--    migration 091, when gifting was widened to cover every perk. The price
--    row outlived it: two guilds still carry `50` for a perk that cannot be
--    bought, and no `EconSettings` field has read it since. Exactly the
--    silent no-op a config key with no reader always is — harmless here only
--    because nothing acts on it.
--
-- Config rows only; no schema change, and nothing to roll back beyond
-- re-inserting a value no code path consults.

DELETE FROM config WHERE key IN (
    'econ_leaderboard_channel_id',
    'econ_leaderboard_message_id',
    'econ_price_gift_color'
);
