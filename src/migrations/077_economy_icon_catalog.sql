-- Economy — curated, rentable role-icon catalog (a currency sink).
--
-- An admin uploads a set of named icons, each with its own weekly price; a
-- member rents one via the perk shop instead of uploading their own image. It
-- reuses the existing `role_icon` rental perk and the personal-role projector
-- (which reads `econ_personal_roles.icon_path` → Discord `display_icon`), so no
-- new perk kind and no `econ_rentals.perk` CHECK change are needed.
--
-- `econ_icon_catalog` is the per-guild catalog. `price` is the icon's weekly
-- rental cost — the rental engine snapshots it at rent time and re-reads the
-- CURRENT value at each renewal (so a price edit takes effect at the next
-- anniversary, exactly like the flat perk prices — spec §6/§9). `enabled = 0`
-- hides an icon from new renters while leaving current renters untouched;
-- hard-deletion is blocked while any live rental points at the row, so a member
-- always keeps the icon they paid for. `image_path` is a managed on-disk path
-- under `<db-parent>/econ_icon_catalog/<guild_id>/<id>.png`.
CREATE TABLE IF NOT EXISTS econ_icon_catalog (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    name       TEXT    NOT NULL,
    image_path TEXT    NOT NULL DEFAULT '',
    price      INTEGER NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_econ_icon_catalog_guild
    ON econ_icon_catalog (guild_id, enabled, sort_order, id);

-- Ties a `role_icon` rental to the catalog icon it rents. NULL = a legacy /
-- bring-your-own icon rental, priced at the flat `settings.price_role_icon`;
-- a non-NULL id makes billing read that icon's `econ_icon_catalog.price`
-- instead (current value at each renewal, with a defensive fallback to the flat
-- price if the row ever disappears).
ALTER TABLE econ_rentals ADD COLUMN catalog_icon_id INTEGER;

-- The icon spec (path or emoji string) currently PROJECTED onto the member's
-- Discord role. The projector's reconcile diffs the role icon by PRESENCE only
-- (it can't read an uploaded Asset's bytes back), so without this it would
-- never re-upload when a member SWITCHES to a different icon (both old and new
-- states have "an icon"). Storing what was last projected lets the projector
-- detect a change and force the re-upload. '' = no icon projected.
ALTER TABLE econ_personal_roles ADD COLUMN projected_icon_path TEXT NOT NULL DEFAULT '';
