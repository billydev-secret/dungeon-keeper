-- Casino floor ticker (docs/plans/casino-ephemeral-ux.md): with instant
-- games rendered ephemerally, the hub panel's "Recent action" section is
-- what keeps the floor feeling alive. One bounded row per resolved
-- instant-game play (coinflip/slots/blackjack — communal rounds already
-- recap publicly), written by record_play in the settlement transaction
-- and trimmed on insert so the table never grows past a screenful.

CREATE TABLE IF NOT EXISTS casino_ticker (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    game     TEXT    NOT NULL,
    stake    INTEGER NOT NULL,
    payout   INTEGER NOT NULL,           -- total return; 0 = the house keeps it
    ts       REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_casino_ticker_guild
    ON casino_ticker (guild_id, id);
