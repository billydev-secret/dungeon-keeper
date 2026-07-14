-- Role Menus — self-service role assignment via component menus.
--
-- A `role_menu` is an embed + a set of button/dropdown options built on the
-- dashboard. Publishing posts one message (channel_id/message_id; both 0 while
-- the menu is a draft) whose components carry the menu id in their custom_id,
-- so clicks survive restarts (DynamicItem). `role_menu_options` are the
-- choices; they're replaced wholesale on save, ordered by `position`.
-- `role_menu_grants` is an append-only history of every grant/removal made
-- through a menu — it references role ids (not option rows) and outlives both
-- options and menus, so "who picked what, when" always has an answer.
-- `role_menu_bindings` records a member's permanent pick in a Binding-mode
-- menu, independent of whether they still hold the role.

CREATE TABLE IF NOT EXISTS role_menus (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    title            TEXT    NOT NULL DEFAULT '',
    description      TEXT    NOT NULL DEFAULT '',  -- markdown, rendered in the embed
    accent           TEXT    NOT NULL DEFAULT '',  -- optional #hex; '' = branding accent
    thumbnail_url    TEXT    NOT NULL DEFAULT '',
    style            TEXT    NOT NULL DEFAULT 'buttons',   -- buttons | dropdown
    mode             TEXT    NOT NULL DEFAULT 'toggle',    -- toggle|unique|verify|drop|binding
    max_roles        INTEGER NOT NULL DEFAULT 0,   -- 0 = no cap
    required_role_id INTEGER NOT NULL DEFAULT 0,   -- 0 = open to everyone
    cooldown_seconds INTEGER NOT NULL DEFAULT 0,   -- 0 = no cooldown
    placeholder      TEXT    NOT NULL DEFAULT '',  -- dropdown hint text
    enabled          INTEGER NOT NULL DEFAULT 1,   -- 0 = interactions rejected politely
    channel_id       INTEGER NOT NULL DEFAULT 0,   -- 0 = draft (never published)
    message_id       INTEGER NOT NULL DEFAULT 0,
    alerted_at       REAL    NOT NULL DEFAULT 0,   -- last degradation mod-alert (dedupe)
    created_at       REAL    NOT NULL,
    updated_at       REAL    NOT NULL,
    updated_by       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS role_menu_options (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    menu_id      INTEGER NOT NULL,
    role_id      INTEGER NOT NULL,
    label        TEXT    NOT NULL,
    emoji        TEXT    NOT NULL DEFAULT '',      -- unicode or <:name:id> form
    description  TEXT    NOT NULL DEFAULT '',      -- dropdown style only
    button_color TEXT    NOT NULL DEFAULT 'secondary',  -- secondary|primary|success|danger
    position     INTEGER NOT NULL DEFAULT 0,
    elevated     INTEGER NOT NULL DEFAULT 0        -- explicit dangerous-role override
);

CREATE TABLE IF NOT EXISTS role_menu_grants (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    menu_id    INTEGER NOT NULL,                   -- not FK-enforced; history outlives menus
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    role_id    INTEGER NOT NULL,
    action     TEXT    NOT NULL,                   -- grant | remove
    created_at REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS role_menu_bindings (
    menu_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    role_id    INTEGER NOT NULL,
    created_at REAL    NOT NULL,
    PRIMARY KEY (menu_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_role_menus_guild
    ON role_menus (guild_id);
CREATE INDEX IF NOT EXISTS idx_role_menu_options_menu
    ON role_menu_options (menu_id, position);
CREATE INDEX IF NOT EXISTS idx_role_menu_grants_guild_user
    ON role_menu_grants (guild_id, user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_role_menu_grants_menu
    ON role_menu_grants (menu_id, created_at);
