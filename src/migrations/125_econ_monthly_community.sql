-- Monthly quests become guild-wide, community-measured goals.
--
-- Monthly was a per-user BOARD cadence (personal board draw, per-user
-- econ_quest_progress, self-claim). It is reclassified to a GUILD-WIDE
-- measured cadence: a single shared counter everyone contributes to, paid in
-- 40/70/100% milestone tiers at calendar-month end — the same machinery as
-- the weekly `community` qtype, on a monthly cadence. Auto-tracked only (a
-- monthly quest must carry a trigger_kind), one active per month, rotating a
-- pool with no gap month.
--
-- The community progress/settlement tables (econ_community_progress,
-- econ_community_contrib, econ_community_tier_payouts,
-- econ_community_progress_snapshots) are keyed by quest_id, not qtype/period,
-- so monthly quests reuse them unchanged. The qtype CHECK already allows
-- 'monthly' (migration 070), and rotation memory reuses econ_quests.last_run_week
-- (it now holds a 'YYYY-MM' token for monthly rows; 'YYYY-Www' for community).

-- Month-roll marker, parallel to last_community_week (migration 082): the
-- calendar month the monthly rotation last rolled in this guild. NULL = never
-- seen — the first day roll after upgrade activates the first monthly goal.
ALTER TABLE econ_day_marks ADD COLUMN last_community_month TEXT;

-- ── Reconcile existing monthly quests (preserve carefully) ──────────────
-- Old monthly quests were per-user board quests. Convert the currently-active
-- ones to guild-wide goals without losing this month's in-flight effort or
-- clawing back any per-user claim already paid.

-- 1. Non-auto-tracked active monthly (manual / phrase / no kind) cannot be an
--    auto-tracked guild-wide goal — deactivate (rows + paid claims preserved).
UPDATE econ_quests SET active = 0
WHERE qtype = 'monthly' AND active = 1 AND trigger_kind = '';

-- 2. Single-lane collapse: keep only the lowest-id auto-tracked monthly active
--    per guild (the new model runs one monthly goal at a time). The rest stay
--    in the library (active = 0) for the rotation to pick up later.
UPDATE econ_quests SET active = 0
WHERE qtype = 'monthly' AND active = 1 AND trigger_kind <> ''
  AND id NOT IN (
    SELECT MIN(id) FROM econ_quests
    WHERE qtype = 'monthly' AND active = 1 AND trigger_kind <> ''
    GROUP BY guild_id
  );

-- 3. Seed the guild-wide counter from THIS calendar month's summed per-user
--    progress, so the shared bar already reflects effort logged before upgrade.
--    (Per-user claims already paid are in econ_quest_claims and untouched — the
--    tier settlement pays via disjoint econ_community_tier_payouts rows, so a
--    member is never re-credited for the same reservation.)
INSERT INTO econ_community_progress
    (quest_id, current, completed_at, settled_at, notified_tier, final_notice_sent)
SELECT q.id,
       COALESCE((SELECT SUM(p.current) FROM econ_quest_progress p
                 WHERE p.quest_id = q.id
                   AND p.period = strftime('%Y-%m','now')), 0),
       NULL, NULL, 0, 0
FROM econ_quests q
WHERE q.qtype = 'monthly' AND q.active = 1 AND q.trigger_kind <> ''
ON CONFLICT(quest_id) DO UPDATE SET current = excluded.current;

-- 4. Size the guild-wide target from trailing-28d kind activity ÷ 0.75 (a full
--    month's worth; floor 10), matching auto_size_community_target(cadence=
--    "monthly"). Channel-share scaling is skipped for this one-shot seed — the
--    next month roll re-sizes via the real sizer, so any drift self-corrects.
UPDATE econ_quests
SET community_target = MAX(10, CAST(ROUND(
      COALESCE((SELECT SUM(a.count) FROM econ_kind_activity a
                WHERE a.guild_id = econ_quests.guild_id
                  AND a.kind = econ_quests.trigger_kind
                  AND a.local_day >= date('now','-28 day')
                  AND a.local_day <  date('now')), 0) / 0.75) AS INTEGER))
WHERE qtype = 'monthly' AND active = 1 AND trigger_kind <> ''
  AND community_target IS NULL;

-- 5. Stamp the rotation cursor to the current month + single lane, so next
--    month's roll settles this seeded goal and rotates to the next pool member.
UPDATE econ_quests
SET last_run_week = strftime('%Y-%m','now'), community_slot = 1
WHERE qtype = 'monthly' AND active = 1 AND trigger_kind <> '';
