-- Deferred level-5 "promotion review" posts.
--
-- The level-5 announcement doubles as a promotion-review signal for staff,
-- so it shouldn't fire for a member who hit level 5 within their first two
-- days (fast XP from an early burst, not a track record). Since the
-- crossing that triggers the post only happens once (role_grant_due is keyed
-- off announced_level, which advances regardless), skipping outright would
-- drop the post forever. Instead handle_level_progress parks it here and a
-- background sweep (promotion_review_recheck_loop) fires it once the member
-- clears the tenure bar, or drops the row if they've since left.

CREATE TABLE IF NOT EXISTS pending_promotion_posts (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    total_xp    REAL    NOT NULL,
    eligible_at REAL    NOT NULL,
    created_at  REAL    NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

-- The recheck sweep scans for rows past their eligible_at across all guilds.
CREATE INDEX IF NOT EXISTS idx_pending_promotion_posts_eligible
    ON pending_promotion_posts (eligible_at);
