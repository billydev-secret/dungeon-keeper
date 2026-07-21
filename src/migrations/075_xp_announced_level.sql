-- XP: track the highest level a member has actually been told about.
--
-- Level-ups were announced by comparing an award's start level to its end
-- level, but both are derived from total_xp at award time. Any path that
-- credits XP without a Discord handle in scope -- quest payouts are the live
-- case (economy_quests_service._credit_reward) -- moved the member up a level
-- with nobody to announce it, and the next ordinary award then computed its
-- start level from the already-credited total, so old == new and the level-up
-- was never announced at all. Silently lost, not deferred.
--
-- announced_level is advanced only by a successful announcement
-- (xp_system.mark_level_announced), never by an award, so a level won on a
-- silent path stays owed and is delivered on the member's next ordinary
-- award. This is what the old code comment claimed already happened.
--
-- Seeding: the goal is that the first award after deploy announces nothing,
-- so nobody's level history replays into the channel. `level` alone is not
-- enough -- it is only as fresh as each member's last award, and a guild that
-- has since lowered xp_coeff_level_curve_factor (the home guild went 15.6 ->
-- 15.0) computes a *higher* level than the stale column records. Those members
-- are currently bumped silently by the old code; announcing the difference
-- would be announcing an admin coefficient change, not an achievement. So seed
-- from whichever is higher: the stored level, or the level total_xp works out
-- to under the guild's *current* factor (mirroring level_for_xp:
-- floor(sqrt(xp / factor)) + 1, factor floored at 0.01, and >= 1).
--
-- Levels won silently before this migration therefore stay silent; delivering
-- those is a deliberate one-shot, not this migration's job.

ALTER TABLE member_xp ADD COLUMN announced_level INTEGER NOT NULL DEFAULT 1;

UPDATE member_xp
SET announced_level = MAX(
    level,
    CASE WHEN total_xp <= 0 THEN 1 ELSE
        CAST(
            sqrt(
                total_xp / MAX(
                    0.01,
                    COALESCE(
                        (SELECT CAST(c.value AS REAL) FROM config c
                         WHERE c.guild_id = member_xp.guild_id
                           AND c.key = 'xp_coeff_level_curve_factor'),
                        15.6
                    )
                )
            ) AS INTEGER
        ) + 1
    END
);
