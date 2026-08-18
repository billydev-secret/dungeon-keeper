-- Migration 166: a second sticky todo board, scoped to recurring chores, plus
-- the third todo state that a true daily reset needs.
-- (docs/plans/todo-board-and-recurring.md, stage 6.)
--
-- WIDEN todo_board RATHER THAN ADD A SECOND TABLE.
-- 134 made todo_board one row per guild so the channel id and the message id
-- could never be observed apart. That argument is about the *pair* being
-- atomic, not about there being one board, and it survives a composite key
-- untouched: (guild_id, kind) still moves both ids in a single row. A separate
-- todo_chore_board table would instead fork get_board/save_board/clear_board/
-- guilds_with_board and the sticky wiring into two near-identical copies, and
-- the one query this feature actually needs — "is the other board already in
-- this channel?" — would become a cross-table union instead of a WHERE clause.
--
-- SQLite cannot widen a primary key in place, so this is the standard
-- rebuild. It is safe to run under the migration runner's explicit BEGIN: all
-- four statements land atomically or none do. Existing rows become kind 'all'
-- (the original everything-board), which is the only thing they could be.
CREATE TABLE IF NOT EXISTS todo_board_new (
    guild_id   INTEGER NOT NULL,
    -- 'all'    — the original board: every pending todo, oldest first.
    -- 'chores' — recurring instances only, as a daily did-we-do-it scoreboard.
    kind       TEXT    NOT NULL DEFAULT 'all',
    channel_id INTEGER NOT NULL DEFAULT 0,
    message_id INTEGER NOT NULL DEFAULT 0,
    updated_at REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, kind)
);

INSERT INTO todo_board_new (guild_id, kind, channel_id, message_id, updated_at)
    SELECT guild_id, 'all', channel_id, message_id, updated_at FROM todo_board;

DROP TABLE todo_board;

ALTER TABLE todo_board_new RENAME TO todo_board;

-- The refresh loop asks "which guilds have a board of this kind" once a
-- minute, per kind.
CREATE INDEX IF NOT EXISTS idx_todo_board_live
    ON todo_board (kind, channel_id, message_id);

-- THE THIRD STATE. A recurring row used to be either pending or completed, and
-- skip-if-pending meant an undone chore simply aged: Monday's unticked QOTD was
-- still Monday's row on Wednesday, one tick cleared both days, and nothing
-- anywhere recorded that Monday did not happen.
--
-- missed_at closes an instance *without* crediting it, so the next occurrence
-- can spawn a fresh row. That is what makes the chore board a scoreboard rather
-- than a list of arrears, and it is the only reason a streak can be computed at
-- all: without a durable record of the days a chore was skipped, "6 days
-- running" is unknowable.
--
-- A missed row is closed. pending_todos/pending_count exclude it (so it also
-- leaves the all-todos board), and complete_todo refuses it — you cannot tick
-- yesterday's box today.
ALTER TABLE todos ADD COLUMN missed_at REAL;

-- Serves both the "latest instance per definition" board query and the streak
-- walk, which read the same rows newest-first.
CREATE INDEX IF NOT EXISTS idx_todos_recurring_recent
    ON todos (recurring_id, created_at DESC);
