-- Self-assign role buttons attached to a timed announcement.
-- One row per button (max 5 — a single Discord action row), ordered by position.
-- The posted message carries no state of its own: the button's custom_id embeds
-- the role id, so clicks keep routing after a restart and after this row is gone.
-- Deleting an announcement drops its buttons explicitly in delete_announcement
-- (foreign keys aren't enforced on this connection).
CREATE TABLE IF NOT EXISTS announcement_buttons (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    announcement_id  INTEGER NOT NULL,
    role_id          INTEGER NOT NULL,
    label            TEXT    NOT NULL DEFAULT '',   -- button text; blank = the role's name at post time
    emoji            TEXT    NOT NULL DEFAULT '',   -- optional leading emoji
    style            TEXT    NOT NULL DEFAULT 'primary', -- primary | secondary | success
    position         INTEGER NOT NULL DEFAULT 0     -- left-to-right order
);

CREATE INDEX IF NOT EXISTS idx_announcement_buttons_ann
    ON announcement_buttons(announcement_id, position);
