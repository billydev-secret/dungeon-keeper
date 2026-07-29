-- Reaction tips — per-emoji price ladder + one-charge-per-reactor awards
-- (docs/plans/nsfw-classifier-and-reaction-tips.md, Stage 5).
--
-- A tip is a TRANSFER with a rake, not a mint. The reactor pays from their own
-- wallet, the poster is credited the remainder, and the difference is burned —
-- never credited to anyone. That makes reactions a net SINK, pointing the same
-- direction as the economy retune of 2026-07-26 (casino RTP trim 3488750d,
-- jackpot skim 25%->5% bede17e9, XP-mint daily ceiling 5a3e2945) rather than
-- against it. A house-minted version was considered and rejected: at ~1,050
-- reactions/day against a 74,083-coin supply it would have inflated the money
-- supply ~1.4% per day.
--
-- Rungs are per-emoji so the channel's auto-react emoji set doubles as a price
-- ladder — which emoji you tap is how much you give. That is also the closest
-- thing this design has to a confirmation dialog: reacting is a one-tap,
-- low-deliberation action and Discord offers no confirm step, so the emoji
-- itself has to carry the price.
--
-- reaction_tip_awards copies the shape of xp_reaction_awards (27,266 rows in
-- prod): one row per (guild, message, reactor), which makes a charge
-- idempotent across react/unreact/re-react. Removing a reaction refunds
-- nothing and re-adding costs nothing, so the toggle can't be farmed.

CREATE TABLE IF NOT EXISTS reaction_tip_rungs (
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    emoji      TEXT    NOT NULL,
    amount     INTEGER NOT NULL,   -- what the poster is quoted, before rake
    PRIMARY KEY (guild_id, channel_id, emoji)
);

CREATE TABLE IF NOT EXISTS reaction_tip_awards (
    guild_id    INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,   -- the reactor who paid
    author_id   INTEGER NOT NULL,   -- who received it
    emoji       TEXT    NOT NULL,
    amount_paid INTEGER NOT NULL,   -- actually debited (may be < the rung)
    rake        INTEGER NOT NULL,   -- burned, credited to nobody
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, message_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_reaction_tip_awards_author
    ON reaction_tip_awards (guild_id, author_id, created_at);
