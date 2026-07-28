ALTER TABLE bump_tracker_sites ADD COLUMN failure_pattern TEXT NOT NULL DEFAULT '';

-- Discadia announces a refused bump publicly, as plain message content:
--   ":warning: Already bumped recently, please try again <t:...:R>."
-- Its *successful* bumps arrive as Components V2 with no content and no
-- embeds, so every live detector for it was configured with an empty
-- detector_pattern — "match anything this bot posts". That made the refusal
-- indistinguishable from a bump and reset the 24h timer. Seed the veto for
-- every guild that already tracks Discadia so the fix lands without hand
-- editing each row.
UPDATE bump_tracker_sites
SET failure_pattern = 'already bumped recently'
WHERE detector_bot_id = 1222548162741538938 AND failure_pattern = '';

-- Discodus and DH Bump announce refusals publicly too, and guilds already
-- tracking them carry the same empty-pattern exposure. Seed only the veto,
-- never detector_pattern: a wrong success pattern would stop detection
-- outright, whereas a wrong veto only fails back to today's behavior.
UPDATE bump_tracker_sites
SET failure_pattern = 'server has been already bumped'
WHERE detector_bot_id = 1159147139960676422 AND failure_pattern = '';

UPDATE bump_tracker_sites
SET failure_pattern = 'bump error!'
WHERE detector_bot_id = 826100334534328340 AND failure_pattern = '';
