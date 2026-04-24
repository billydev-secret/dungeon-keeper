-- Migration 002: birthday tracker
CREATE TABLE IF NOT EXISTS member_birthdays (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    birth_month INTEGER NOT NULL,
    birth_day   INTEGER NOT NULL,
    set_by      INTEGER NOT NULL,
    set_at      REAL    NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS birthday_announcements (
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    announced_date  TEXT    NOT NULL,
    PRIMARY KEY (guild_id, user_id, announced_date)
);
