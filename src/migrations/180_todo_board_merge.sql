-- Migration 180: one sticky todo board again, showing everything.
--
-- 166 split the board in two — `all` (every pending task) and `chores`
-- (recurring instances as a daily scoreboard) — so a daily "post the QOTD"
-- wasn't buried among "fix the quote bot". The split solved that by making the
-- two lists live in two channels, which turned out to cost more than it saved:
--
--   * the boards may never share a channel (only one can sit at the bottom),
--     so a server with one mod channel has to choose;
--   * choosing is what actually happened. In prod the chore board is posted
--     and the all-todos board never was, leaving 25 open tasks — the oldest
--     from June — with no Discord surface at all;
--   * every path forked: two placements, two refreshes, two views, two
--     sticky listeners, a collision check and its 409.
--
-- One board with headed sections answers both questions in one place, which is
-- what having two boards was trying to do. The `kind` column goes with the
-- split — a single-valued discriminator is a worse lie than no column.
--
-- MERGE RULE: keep the posted row. If a guild somehow has BOTH posted, keep
-- the CHORES channel: chores are mod-facing, and a combined board carries them,
-- so landing the merged board in a public channel would disclose more than
-- landing tasks in a mod channel. The losing board's Discord message is left
-- where it is — a migration can't call Discord — and goes stale until someone
-- deletes it. In prod this never arises: only one board is posted there.
CREATE TABLE todo_board_new (
    guild_id   INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL DEFAULT 0,
    message_id INTEGER NOT NULL DEFAULT 0,
    updated_at REAL    NOT NULL DEFAULT 0
);

INSERT INTO todo_board_new (guild_id, channel_id, message_id, updated_at)
SELECT guild_id, channel_id, message_id, updated_at
FROM (
    SELECT guild_id, channel_id, message_id, updated_at,
           ROW_NUMBER() OVER (
               PARTITION BY guild_id
               ORDER BY (channel_id != 0 AND message_id != 0) DESC,
                        (kind = 'chores') DESC
           ) AS rn
    FROM todo_board
)
WHERE rn = 1;

DROP TABLE todo_board;

ALTER TABLE todo_board_new RENAME TO todo_board;

-- The refresh loop asks "which guilds have a board" once a minute.
CREATE INDEX IF NOT EXISTS idx_todo_board_live
    ON todo_board (channel_id, message_id);
