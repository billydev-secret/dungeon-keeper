-- Booster cosmetic colours become a rentable shop perk (todo #76).
--
-- WHY: the 11 curated gradient roles in `booster_roles` were claimable by any
-- server booster from an image panel, free and forever — nothing revoked them
-- when boosting lapsed. Boosters already earn 1.5x currency, so the colours
-- stop being a boost entitlement and become a shop good anyone can rent. The
-- curated palette stays its own product, cheaper than the free-form
-- `role_gradient` it undercuts.
--
-- The palette moves to `econ_color_catalog`, modelled on `econ_icon_catalog`
-- (migration 077): admin-curated, optionally priced per row, `enabled = 0`
-- hides a colour from new renters without disturbing current ones, and a live
-- rental blocks hard-deletion. Renting projects the colour pair onto the
-- member's personal role via `econ_personal_roles.color`/`color2` — the same
-- projector every other colour perk goes through — so the palette needs no
-- Discord roles of its own.
--
-- GRANDFATHERING: the 11 Discord roles are deliberately NOT touched, here or
-- anywhere else. 15 members wear one today (12 still boosting, 3 lapsed) and
-- they keep it, free, permanently. Personal rented roles sit ABOVE the
-- `#### Cosmetics` anchor those roles live under, so a member who later rents
-- a palette colour overlays their old one, and if that rental lapses the
-- original shows through again. `legacy_role_id` keeps the link on record;
-- nothing reads it to grant or revoke.

-- The per-guild colour palette. `hex1`/`hex2` are the gradient pair as 6-char
-- hex (converted to ints by the projector, matching how the swatch sync has
-- always read them). `price` = 0 means "bill the flat settings.price_role_preset"
-- so a guild gets one price for the whole palette until an admin prices a
-- colour individually. `key` is the old `booster_roles.role_key`, kept so
-- buttons on already-posted panels (custom_id `booster_role:<key>`) keep
-- resolving after this migration without a repost.
CREATE TABLE IF NOT EXISTS econ_color_catalog (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id       INTEGER NOT NULL,
    key            TEXT    NOT NULL,
    name           TEXT    NOT NULL,
    hex1           TEXT    NOT NULL DEFAULT '',
    hex2           TEXT    NOT NULL DEFAULT '',
    image_path     TEXT    NOT NULL DEFAULT '',
    price          INTEGER NOT NULL DEFAULT 0,
    enabled        INTEGER NOT NULL DEFAULT 1,
    sort_order     INTEGER NOT NULL DEFAULT 0,
    legacy_role_id INTEGER NOT NULL DEFAULT 0,
    created_at     REAL    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_econ_color_catalog_key
    ON econ_color_catalog (guild_id, key);

CREATE INDEX IF NOT EXISTS idx_econ_color_catalog_guild
    ON econ_color_catalog (guild_id, enabled, sort_order, id);

-- Carry the palette across, parsing the gradient pair out of each swatch
-- filename (`ColorName_HEX1_HEX2.ext`) — the hexes were never stored in
-- `booster_roles`, only in the filename the sync generated the role from.
--
-- The parse walks in from the end with the standard rtrim/replace
-- last-delimiter idiom (SQLite has no reverse() or regexp): `rtrim(s,
-- replace(s, D, ''))` strips every trailing character that is not the
-- delimiter, leaving the prefix through the last D. Applied to '/' it yields
-- the basename, to '.' the stem, then twice to '_' the two hex tokens.
-- A row whose filename does not parse still lands (so the palette is never
-- silently short) but with empty hexes; the service treats a hex-less row as
-- unrentable and the dashboard flags it for a re-sync.
INSERT INTO econ_color_catalog
    (guild_id, key, name, hex1, hex2, image_path, price, enabled, sort_order,
     legacy_role_id, created_at)
SELECT
    guild_id,
    role_key,
    label,
    CASE WHEN upper(hex1) GLOB '[0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F]'
         THEN upper(hex1) ELSE '' END,
    CASE WHEN upper(hex2) GLOB '[0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F]'
         THEN upper(hex2) ELSE '' END,
    image_path,
    0,
    1,
    sort_order,
    role_id,
    strftime('%s', 'now') + 0.0
FROM (
    SELECT
        guild_id, role_key, label, image_path, sort_order, role_id,
        -- hex1: the token before the last underscore of the stem
        substr(
            stem1,
            length(rtrim(stem1, replace(stem1, '_', ''))) + 1
        ) AS hex1,
        -- hex2: the token after the last underscore of the stem
        substr(
            stem,
            length(rtrim(stem, replace(stem, '_', ''))) + 1
        ) AS hex2
    FROM (
        SELECT
            guild_id, role_key, label, image_path, sort_order, role_id,
            stem,
            -- the stem with its final `_HEX2` chopped off
            substr(
                rtrim(stem, replace(stem, '_', '')),
                1,
                length(rtrim(stem, replace(stem, '_', ''))) - 1
            ) AS stem1
        FROM (
            SELECT
                guild_id, role_key, label, image_path, sort_order, role_id,
                -- basename with its extension removed
                substr(
                    rtrim(base, replace(base, '.', '')),
                    1,
                    length(rtrim(base, replace(base, '.', ''))) - 1
                ) AS stem
            FROM (
                SELECT
                    guild_id, role_key, label, image_path, sort_order, role_id,
                    replace(
                        image_path,
                        rtrim(image_path, replace(image_path, '/', '')),
                        ''
                    ) AS base
                FROM booster_roles
            )
        )
    )
);

-- Panel bookkeeping follows the palette out of the "booster" namespace. The
-- posted panel messages themselves are left in place and keep working; only
-- the table holding their ids is renamed.
CREATE TABLE IF NOT EXISTS econ_color_panel_messages (
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, message_id)
);

INSERT OR IGNORE INTO econ_color_panel_messages (guild_id, channel_id, message_id)
SELECT guild_id, channel_id, message_id FROM booster_panel_messages;

DROP TABLE IF EXISTS booster_panel_messages;
DROP TABLE IF EXISTS booster_roles;

-- New rentable perk `role_preset` (the curated palette) and the column tying a
-- rental to the colour it rents. SQLite can't ALTER a CHECK in place, so the
-- `econ_rentals.perk` constraint is widened with a table rebuild — same shape
-- as migrations 091 and 107. Every column and index carries across unchanged.
--
-- `catalog_color_id` mirrors `catalog_icon_id`: NULL for every other perk, and
-- for a `role_preset` rental it names the palette entry, so billing re-reads
-- that colour's current price at each renewal (falling back to the flat
-- `settings.price_role_preset` when the row prices at 0 or has vanished).
CREATE TABLE econ_rentals_new (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id             INTEGER NOT NULL,
    user_id              INTEGER NOT NULL,
    perk                 TEXT    NOT NULL CHECK (perk IN
                             ('role_color', 'role_name', 'role_icon',
                              'role_gradient', 'role_holographic',
                              'role_preset', 'voice_style', 'emoji')),
    state                TEXT    NOT NULL CHECK (state IN
                             ('active', 'grace', 'lapsed', 'cancelled')),
    price                INTEGER NOT NULL,
    started_at           REAL    NOT NULL,
    next_bill_at         REAL    NOT NULL,
    grace_since          REAL,
    cancel_at_period_end INTEGER NOT NULL DEFAULT 0,
    suspended            INTEGER NOT NULL DEFAULT 0,
    suspended_since      REAL,
    beneficiary_id       INTEGER NOT NULL,
    meta                 TEXT,
    created_at           REAL    NOT NULL,
    ended_at             REAL,
    catalog_icon_id      INTEGER,
    catalog_color_id     INTEGER
);

INSERT INTO econ_rentals_new
    (id, guild_id, user_id, perk, state, price, started_at, next_bill_at,
     grace_since, cancel_at_period_end, suspended, suspended_since,
     beneficiary_id, meta, created_at, ended_at, catalog_icon_id)
SELECT id, guild_id, user_id, perk, state, price, started_at, next_bill_at,
       grace_since, cancel_at_period_end, suspended, suspended_since,
       beneficiary_id, meta, created_at, ended_at, catalog_icon_id
FROM econ_rentals;

DROP TABLE econ_rentals;
ALTER TABLE econ_rentals_new RENAME TO econ_rentals;

CREATE INDEX IF NOT EXISTS idx_econ_rentals_billing
    ON econ_rentals (guild_id, state, next_bill_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_econ_rentals_live
    ON econ_rentals (guild_id, user_id, perk, beneficiary_id)
    WHERE state IN ('active', 'grace');
