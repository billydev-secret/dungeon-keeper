-- Migration 103: bring monthly quest rewards back down to earth.
--
-- Review of 8 days of live ledger data: quests were 45% of all minted currency
-- against a ~5% burn rate, and monthly quests were the richest per claim — 43
-- claims produced 5,690 coins (avg ~132, up to 150), on an advisory band that
-- topped out at 200. Two clicks a month banked a member ~260 coins, several
-- role-perk rentals.
--
-- Roughly halve every monthly quest reward, with a floor of 50 so nothing drops
-- below the new band minimum. Only reduces (never raises), guild-agnostic, and a
-- no-op for any monthly already at/below 50. The advisory band is lowered to
-- 50–90 in code (quests.py _REWARD_BANDS) in the same change.

UPDATE econ_quests
SET reward = MAX(50, reward / 2)
WHERE qtype = 'monthly' AND reward > 50;
