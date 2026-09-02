-- Migration 200: birthdays announce to any number of channels, not a fixed
-- main + second (2026-09-02, Billy: "could we have an arbitrary number of
-- channels? Like all the other stuff, there's a little add button.").
--
-- Two fixed config keys (`birthday_channel_id`/`_message`/`_pin` and their
-- `_2` twins) become rows in `birthday_channels`, one per announced channel,
-- matching the shape `needle_channels` (migration 042) already uses for the
-- same "add any number of channels" idiom.
--
-- Every guild that had a main and/or second channel set keeps every channel
-- it had — the INSERT..SELECT below reads the old keys straight out of
-- `config` before the DELETE at the bottom removes them, so nothing is lost
-- in between; both run inside this migration's own transaction. A guild with
-- neither key set (never configured) gets zero rows, same as before.
--
-- `birthday_channels` names no member — guild_id/channel_id/message/pin only
-- — so it is guild config, not personal data, and needs no
-- docs/data_register.md row (member_birthdays already covers the personal
-- data this feature holds).

CREATE TABLE IF NOT EXISTS birthday_channels (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message    TEXT    NOT NULL DEFAULT '',
    pin        INTEGER NOT NULL DEFAULT 0,
    UNIQUE (guild_id, channel_id)
);

-- Main channel (birthday_channel_id / birthday_message / birthday_pin).
INSERT OR IGNORE INTO birthday_channels (guild_id, channel_id, message, pin)
SELECT
    c.guild_id,
    CAST(c.value AS INTEGER),
    COALESCE(
        (SELECT m.value FROM config m
         WHERE m.guild_id = c.guild_id AND m.key = 'birthday_message'),
        'Happy birthday, {mention}! 🎂' || char(10) || '{request}'
    ),
    CAST(COALESCE(
        (SELECT p.value FROM config p
         WHERE p.guild_id = c.guild_id AND p.key = 'birthday_pin'),
        '0'
    ) AS INTEGER)
FROM config c
WHERE c.key = 'birthday_channel_id'
  AND CAST(c.value AS INTEGER) != 0;

-- Second channel (birthday_channel_id_2 / birthday_message_2 / birthday_pin_2).
INSERT OR IGNORE INTO birthday_channels (guild_id, channel_id, message, pin)
SELECT
    c.guild_id,
    CAST(c.value AS INTEGER),
    COALESCE(
        (SELECT m.value FROM config m
         WHERE m.guild_id = c.guild_id AND m.key = 'birthday_message_2'),
        'Happy birthday, {mention}! 🎂' || char(10) || '{request}'
    ),
    CAST(COALESCE(
        (SELECT p.value FROM config p
         WHERE p.guild_id = c.guild_id AND p.key = 'birthday_pin_2'),
        '0'
    ) AS INTEGER)
FROM config c
WHERE c.key = 'birthday_channel_id_2'
  AND CAST(c.value AS INTEGER) != 0;

-- The old keys are superseded by the rows above; nothing reads them once
-- birthday_cog / routes/config.py ship reading birthday_channels instead
-- (same commit). `birthday_announce_hour` is untouched — it stays a single
-- guild-wide dial, not per-channel.
DELETE FROM config WHERE key IN (
    'birthday_channel_id',
    'birthday_message',
    'birthday_pin',
    'birthday_channel_id_2',
    'birthday_message_2',
    'birthday_pin_2'
);
