-- Migration 190: remember where this morning's login digest DM landed, so it
-- can be edited in place as quest progress comes in.
--
-- The digest ("Daily Streak") is DM'd at a member's first qualifying activity
-- of their local day. Until now the `discord.Message` was discarded the
-- instant it was sent, so there was no handle to update — a card posted at
-- 8am still showed 8am's bars at 8pm. One row per member per guild holds that
-- handle plus everything needed to re-render, and the hourly economy tick
-- refreshes the card in place.
--
-- Edits are SILENT: Discord sends no notification for an edit, which is the
-- point. Nobody is pinged twice, and the card quietly matches reality for
-- anyone who opens the DM. Nothing here ever posts a second message.
--
-- Only a real DM is recorded. A muted member, one without the opt-in game
-- role, and one whose DMs are closed all produce no message at all (the send
-- path reports its surface explicitly rather than a bool), so no row is
-- written and the sweep never chases a card that does not exist. The digest
-- is DM-or-nothing (`public_fallback=False`), so a row can never point at the
-- public bank channel — editing that copy would leak the wellness section.
--
-- The seven outcome scalars are the parts of a `LoginOutcome` that cannot be
-- recomputed later: `econ_logins` stores only `paid`, and milestone/grace/
-- reset/shield are one-shot results of the day's first login. Everything else
-- the card shows is re-derived live at sweep time — quests from the member's
-- board, the wellness blurb from that member's current wellness state. That is
-- deliberate: no rendered prose is stored here, and in particular the wellness
-- section's text never lands in this table.
--
-- `signature` is a hash of the rendered card. An hourly pass that would
-- rewrite an identical embed skips the API call entirely, so a quiet member
-- costs zero requests all day (same trick as core/sticky.py).
--
-- `final` is set once every personal quest is done: that render is the last
-- one, and the row stops being swept. A stale `local_day` stops it too, so a
-- card never outlives the day it describes.
--
-- Personal data: registered in docs/data_register.md, cleared by
-- `purge_user_data` via econ_purge_user's table list.
CREATE TABLE IF NOT EXISTS econ_login_digest_cards (
    guild_id      INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    local_day     TEXT    NOT NULL,
    dm_channel_id INTEGER NOT NULL,
    message_id    INTEGER NOT NULL,
    signature     TEXT    NOT NULL DEFAULT '',
    updated_at    REAL    NOT NULL DEFAULT 0,
    final         INTEGER NOT NULL DEFAULT 0,
    -- LoginOutcome, frozen at send time (see above).
    paid          INTEGER NOT NULL DEFAULT 0,
    streak        INTEGER NOT NULL DEFAULT 0,
    milestone     INTEGER NOT NULL DEFAULT 0,
    grace         INTEGER NOT NULL DEFAULT 0,
    reset         INTEGER NOT NULL DEFAULT 0,
    shield        INTEGER NOT NULL DEFAULT 0,
    prior_streak  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
