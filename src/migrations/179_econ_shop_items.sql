-- Custom shop items (docs/plans/economy-shop-items.md, spec §6).
--
-- Staff define items on the dashboard — a name, a price, and what buying one
-- does — instead of the catalogue being the fixed list compiled into
-- economy/perks.py. Two axes, both per item:
--
--   kind     'role'   grant one role, automatically, on purchase
--            'manual' a staff to-do: escrow the coins and spawn a todo
--   billing  'once'   pay once, done
--            'weekly' an ordinary econ_rentals row (perk = 'custom_item')
--
-- The manual queue is NOT a new queue. It spawns rows on the todo board mods
-- already work through (todos.purchase_id below), which is why this migration
-- touches three otherwise unrelated tables.

CREATE TABLE IF NOT EXISTS econ_shop_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    name             TEXT    NOT NULL,
    -- The one-line cell in the shop table (kept short — the shop row is one
    -- code cell of a phone-width monospace table).
    blurb            TEXT    NOT NULL DEFAULT '',
    -- The longer text on the buy confirmation, where there is room.
    description      TEXT    NOT NULL DEFAULT '',
    price            INTEGER NOT NULL,
    kind             TEXT    NOT NULL DEFAULT 'manual'
                             CHECK (kind IN ('role', 'manual')),
    billing          TEXT    NOT NULL DEFAULT 'once'
                             CHECK (billing IN ('once', 'weekly')),
    -- kind='role' only: the role granted on purchase and stripped on lapse.
    role_id          INTEGER,
    -- NULL = unlimited. `sold` counts purchases that consumed stock; a
    -- refunded order gives its unit back, so this is not a monotonic total.
    stock            INTEGER,
    sold             INTEGER NOT NULL DEFAULT 0,
    -- NULL = unlimited. Counted over the member's non-refunded purchases.
    per_member_limit INTEGER,
    -- NULL either side = open-ended. Epoch seconds, compared against now.
    available_from   REAL,
    available_until  REAL,
    -- Prompt the buyer for a free-text note (the name to engrave, the colour
    -- they want) and show it on the todo.
    ask_note         INTEGER NOT NULL DEFAULT 0,
    enabled          INTEGER NOT NULL DEFAULT 1,
    sort_order       INTEGER NOT NULL DEFAULT 0,
    -- The admin who defined the item. Preserved on erasure under Art 17(3)(e)
    -- — the record of who opened a spend surface, the mention_award_rules
    -- precedent. See docs/data_register.md.
    created_by       INTEGER NOT NULL DEFAULT 0,
    created_at       REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_econ_shop_items_guild
    ON econ_shop_items (guild_id, enabled, sort_order);

-- One order. `state` is the whole lifecycle of both kinds:
--
--   pending    manual only — escrowed, on the todo board, nobody has acted
--   fulfilled  delivered (a once item; the terminal happy state)
--   live       a weekly item whose rental is running
--   denied     a mod refused it — refunded
--   cancelled  the buyer withdrew it — refunded
--   expired    nobody resolved it inside shop_item_expire_days — refunded
--   lapsed     a weekly item whose rental ended
--
-- A role+once purchase is born 'fulfilled': there is nothing to wait for.
CREATE TABLE IF NOT EXISTS econ_shop_purchases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    item_id     INTEGER NOT NULL,
    state       TEXT    NOT NULL DEFAULT 'pending'
                        CHECK (state IN ('pending', 'fulfilled', 'live',
                                         'denied', 'cancelled', 'expired',
                                         'lapsed')),
    -- Snapshotted at purchase: the item's price can move underneath an open
    -- order, and the refund must return what was actually taken.
    price       INTEGER NOT NULL,
    note        TEXT    NOT NULL DEFAULT '',
    todo_id     INTEGER,
    rental_id   INTEGER,
    resolver_id INTEGER,
    deny_reason TEXT    NOT NULL DEFAULT '',
    -- The exactly-once refund predicate (the emoji-sponsor pattern): a refund
    -- is guarded on this being NULL, so no path can pay one twice.
    refunded_at REAL,
    created_at  REAL    NOT NULL,
    resolved_at REAL
);

CREATE INDEX IF NOT EXISTS idx_econ_shop_purchases_open
    ON econ_shop_purchases (guild_id, state, created_at);

CREATE INDEX IF NOT EXISTS idx_econ_shop_purchases_member
    ON econ_shop_purchases (guild_id, user_id, item_id);

-- Provenance for a todo spawned by a purchase — the same column, for the same
-- reason, as `recurring_id` in migration 134: the board marks the row, and the
-- completion path can find the order it delivers.
--
-- The task text names the ITEM ONLY ("Deliver Custom Emoji"), never the buyer.
-- data_register.md justifies anonymising rather than deleting a todos row on
-- the ground that its text is server work product and not member disclosure;
-- baking a name in would leave the member inside the half that survives an
-- erasure. The buyer is rendered live by joining through this column.
ALTER TABLE todos ADD COLUMN purchase_id INTEGER;

CREATE INDEX IF NOT EXISTS idx_todos_purchase
    ON todos (purchase_id) WHERE purchase_id IS NOT NULL;

-- econ_rentals gains `custom_item` and the column tying a rental to the item
-- it rents. SQLite can't ALTER a CHECK in place, so this is the standard
-- rebuild — the fourth, after 091, 107 and 159. Every column and index carries
-- across unchanged EXCEPT the live-rental unique index, below.
CREATE TABLE econ_rentals_new (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id             INTEGER NOT NULL,
    user_id              INTEGER NOT NULL,
    perk                 TEXT    NOT NULL CHECK (perk IN
                             ('role_color', 'role_name', 'role_icon',
                              'role_gradient', 'role_holographic',
                              'role_preset', 'voice_style', 'emoji',
                              'custom_item')),
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
    catalog_color_id     INTEGER,
    catalog_item_id      INTEGER
);

INSERT INTO econ_rentals_new
    (id, guild_id, user_id, perk, state, price, started_at, next_bill_at,
     grace_since, cancel_at_period_end, suspended, suspended_since,
     beneficiary_id, meta, created_at, ended_at, catalog_icon_id,
     catalog_color_id)
SELECT id, guild_id, user_id, perk, state, price, started_at, next_bill_at,
       grace_since, cancel_at_period_end, suspended, suspended_since,
       beneficiary_id, meta, created_at, ended_at, catalog_icon_id,
       catalog_color_id
FROM econ_rentals;

DROP TABLE econ_rentals;
ALTER TABLE econ_rentals_new RENAME TO econ_rentals;

CREATE INDEX IF NOT EXISTS idx_econ_rentals_billing
    ON econ_rentals (guild_id, state, next_bill_at);

-- COALESCE IS LOAD-BEARING. A member may hold several DIFFERENT custom items
-- at once, so the item id has to join the race anchor — but SQLite treats
-- every NULL in a unique index as distinct, and `catalog_item_id` is NULL for
-- all eight existing perks. Adding the bare column would therefore let two
-- live `role_color` rentals coexist, silently dissolving the
-- one-live-rental-per-perk guarantee this index exists to enforce. Collapsing
-- the NULLs to 0 keeps every existing perk behaving exactly as it does today.
CREATE UNIQUE INDEX IF NOT EXISTS idx_econ_rentals_live
    ON econ_rentals (guild_id, user_id, perk, beneficiary_id,
                     COALESCE(catalog_item_id, 0))
    WHERE state IN ('active', 'grace');
