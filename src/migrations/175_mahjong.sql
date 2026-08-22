-- 175_mahjong.sql
-- Meadow Mahjong (docs/meadow_mahjong_spec.md; plan docs/plans/meadow-mahjong.md
-- stage 4). Five tables, laid down together so no later stage needs a schema
-- change:
--
--   mahjong_cards         versioned card data per guild: the uploaded JSON,
--                         lifecycle status, optional scheduled activation.
--                         One active card per guild, enforced at the schema
--                         (the survivor one-live-season pattern) so a
--                         double-click on Set Active can't leave two.
--   mahjong_tables        one row per table; `state` is the serialized engine
--                         (game_logic.state_to_dict) re-loaded on restart with
--                         timers re-armed. One LIVE table per channel is a
--                         schema rule (spec §6.3) — it is also what keeps the
--                         sticky table message from fighting another panel.
--   mahjong_seats         who sits where, normalized out of the state JSON so
--                         the one-seat-per-member-per-guild rule (§6.3) is a
--                         schema rule and so purge/export can SEE live seats —
--                         a member id buried in `state` JSON is the documented
--                         list-column blind spot (privacy_service). `live` is
--                         denormalized from the parent table's status and
--                         maintained in the same transaction.
--   mahjong_results       one row per settled hand (winner, line, how won).
--   mahjong_result_seats  per-seat coin/point deltas of a result — a real
--                         table, not a JSON blob, for the same purge/export
--                         reason as mahjong_seats (plan D5).
--   mahjong_stats         per-member aggregates for the dashboard report.
--
-- Escrow deliberately has NO table here: stakes ride the existing
-- econ_game_wagers rows (game_type 'mahjong', economy_wager_service) — the
-- exactly-once settle/refund machinery already lives there (plan D4).
--
-- Per-user data: mahjong_seats, mahjong_results (winner_id),
-- mahjong_result_seats, mahjong_stats. All PURGED on erasure — game history
-- has no Art 17(3) ground; the money that moved lives on in the preserved
-- econ_ledger. A live seat is dissolved (escrow refunded) before its row
-- goes. Rows in docs/data_register.md land in the same commit.
-- House-rules dials (claim windows, timers, wall trim, stakes) are `config`
-- KV keys (mahjong_* — see mahjong_service.MahjongSettings), not a table.

CREATE TABLE IF NOT EXISTS mahjong_cards (
    id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    card_id TEXT NOT NULL,              -- from the JSON ("meadow-first-light")
    display_name TEXT NOT NULL,
    season TEXT NOT NULL,
    card_json TEXT NOT NULL,            -- full card, linted at upload
    status TEXT NOT NULL DEFAULT 'archived',  -- active|scheduled|archived
    activate_at REAL,                   -- when status = 'scheduled'
    uploaded_by INTEGER,                -- admin member id (audit only)
    created_at REAL NOT NULL,
    UNIQUE (guild_id, card_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_mahjong_one_active_card
    ON mahjong_cards (guild_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS mahjong_tables (
    id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    mode INTEGER NOT NULL,              -- seat count: 2 (Duel) | 4
    stake INTEGER NOT NULL,             -- coins per point
    card_row_id INTEGER NOT NULL REFERENCES mahjong_cards(id),
    host_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'live',  -- live|closed
    state TEXT NOT NULL,                -- serialized engine state (JSON)
    sticky_message_id INTEGER,          -- the persistent table message
    deadline_at REAL,                   -- when the phase timer fires; re-armed
                                        -- with remaining time on restart (§6.2)
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    closed_reason TEXT                  -- cancelled|dissolved|finished|purged
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_mahjong_one_table_per_channel
    ON mahjong_tables (guild_id, channel_id) WHERE status = 'live';

CREATE TABLE IF NOT EXISTS mahjong_seats (
    table_id INTEGER NOT NULL REFERENCES mahjong_tables(id),
    guild_id INTEGER NOT NULL,          -- denormalized: export guild-scopes
    user_id INTEGER NOT NULL,
    seat_index INTEGER NOT NULL,
    live INTEGER NOT NULL DEFAULT 1,    -- mirrors the parent status
    PRIMARY KEY (table_id, seat_index)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_mahjong_one_seat_per_member
    ON mahjong_seats (guild_id, user_id) WHERE live = 1;

CREATE TABLE IF NOT EXISTS mahjong_results (
    id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    table_id INTEGER NOT NULL REFERENCES mahjong_tables(id),
    hand_no INTEGER NOT NULL,
    mode INTEGER NOT NULL,
    stake INTEGER NOT NULL,
    card_id TEXT NOT NULL,
    kind TEXT NOT NULL,                 -- mahjong|wall_game|fallow_end
    winner_id INTEGER,                  -- member id; NULL on wall game
    line_id TEXT,
    line_name TEXT,
    base_value INTEGER NOT NULL,
    won_by TEXT,                        -- discard|self_pick; NULL otherwise
    jokerless INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mahjong_results_guild
    ON mahjong_results (guild_id, created_at);

CREATE TABLE IF NOT EXISTS mahjong_result_seats (
    result_id INTEGER NOT NULL REFERENCES mahjong_results(id),
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    seat_index INTEGER NOT NULL,
    points_delta INTEGER NOT NULL,
    coins_delta INTEGER NOT NULL,
    fallow INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (result_id, seat_index)
);

CREATE INDEX IF NOT EXISTS idx_mahjong_result_seats_user
    ON mahjong_result_seats (guild_id, user_id);

CREATE TABLE IF NOT EXISTS mahjong_stats (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    mode INTEGER NOT NULL,
    hands_played INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    jokerless_wins INTEGER NOT NULL DEFAULT 0,
    coins_won INTEGER NOT NULL DEFAULT 0,     -- gross winnings
    coins_lost INTEGER NOT NULL DEFAULT 0,    -- gross losses (positive number)
    biggest_win INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, mode)
);
