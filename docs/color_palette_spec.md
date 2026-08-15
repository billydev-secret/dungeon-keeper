# Color Palette

**Flavor: Reference** — matches current behavior.

## Purpose

A curated set of named gradient colors members **rent from the perk shop** via
the `role_preset` perk ("Palette Color"). It is the cheaper, opinionated
sibling of `role_gradient`: the same two-color fade, chosen from art the admin
curates rather than from two hex codes the member types.

Until migration 159 (todo #76) this was **Booster Roles** — the same 11 colors,
claimable free and permanently by any server booster from an image panel, and
never revoked when boosting lapsed. Boosters already earn 1.5× currency, so the
colors became a shop good instead. What survives from that era is documented
under [Grandfathering](#grandfathering); it is load-bearing, not vestigial.

Two modules:

- `src/bot_modules/services/economy_color_catalog_service.py` — the DB layer for
  `econ_color_catalog`, modelled on `economy_icon_catalog_service` (migration
  077).
- `src/bot_modules/services/color_palette.py` — the Discord surface: the
  showroom panel, its picker button, and the swatch sync.

## Buying and wearing

Renting and restyling are the same act — the color you pick *is* what you rent,
exactly as with catalog icons.

**In the shop** (`/bank shop` → **🖌️ Browse colors** / **Change color**):
`EconomyCog.open_color_palette` lists rentable colors in a select, each showing
its gradient and weekly price. Choosing one runs `pick_catalog_color`:

- **No live rental** → `rent_perk(..., "role_preset", catalog_color_id=…)`
  charges the first week upfront and writes the pair to
  `econ_personal_roles.color`/`color2` in **one transaction**, so a failed debit
  rolls the whole thing back.
- **A live rental** → the rental is re-tagged (`set_rental_catalog_color`) and
  the colors rewritten. No charge: the week is already paid for, and the new
  color's price applies from the next renewal.
- **A comped moderator** (`mod_perk_comp` on) → colors are written with no
  rental row at all. A comp is never a purchase.

Then `apply_role_perks` projects it. The shop row and its button are **hidden
entirely** when a guild has no rentable colors (`color_catalog=None`).

**In the showroom** (the in-channel panel): pressing a swatch calls
`color_palette.wear_palette_color`, which is also what the shop picker's
switch path amounts to. It **never charges** — a public button that debits a
wallet on a press would be a trap — so it requires an existing entitlement
(rental, gift or staff comp) and otherwise replies with the shop's price and a
pointer to `/bank shop`. An entitled member's rental is re-tagged so billing
follows the color they are actually wearing.

## Projection

A palette color is a two-color fade, so `effective_color_mode` returns
`gradient` for `role_preset` exactly as for `role_gradient` — the two differ
only in where the pair came from, which is a shop concern, not a projection one.
It follows that:

- `role_preset` is feature-gated on **`ENHANCED_ROLE_COLORS`**
  (`perk_actions._FEATURE_FOR_PERK`), and the projector drops it from `applied`
  when the guild loses that feature, like the other gradient perks.
- A member holding both `role_preset` and `role_gradient` shares one pair of
  `econ_personal_roles` columns; last write wins. That is inherent to the
  one-personal-role model, not a palette quirk.
- `role_holographic` still outranks everything.

## Pricing

Flat `price_role_preset` (default 80; **Economy → Sinks → Palette Color, Per
Week**), which a color may override with its own `price`. **`price = 0` means
"use the flat price"**, not "free" — most palettes are priced once for the whole
set. `economy_rentals_service._price_for` resolves this at rent time and re-reads
it at every renewal, so an admin's price edit lands at the next anniversary, never
mid-week. A vanished catalog row falls back to the flat price rather than wedging
billing.

The shop row shows the span across rentable colors
(`economy_color_catalog_service.catalog_price_range`, which substitutes the flat
price for 0-priced rows). Unlike the icon row, the flat price is **not** folded
into that span: there is no bring-your-own palette color, so the only prices on
offer are the palette's own.

`price_role_gradient` rose from 120 to 240 with the palette's arrival, so the
curated set reads as the value pick and picking your own two colors reads as the
splurge. Existing renters move at their next renewal and are DM'd the old and new
figure (the "economy A2" reprice notice).

## Grandfathering

15 members wore a booster cosmetic role when this shipped (12 still boosting, 3
lapsed). **They keep it, free, permanently.** Nothing grants or revokes those
Discord roles any more; `econ_color_catalog.legacy_role_id` records which role
each color used to hand out, purely as a record.

This works for free because of role hierarchy: personal rented roles are
positioned **above** the `#### Cosmetics` anchor the legacy roles sit under
(`perk_actions._COSMETICS_ANCHOR`). So a grandfathered member who rents a color
overlays their old one, and if that rental lapses the personal role is deleted
and the original color shows through again rather than leaving them bare.

They cannot *switch* for free: the showroom button requires an entitlement, so
changing color means renting like everyone else.

Two guards keep the promise:

- **The sync never touches Discord roles.** It used to delete a color's role
  when the swatch file vanished, which would now strip grandfathered members.
- **A vanished swatch disables an in-use color instead of deleting it**, so a
  live renter keeps what they paid for.

## Swatch sync

Colors are authored by **file name**, which is why there is no "add a color"
form. `sync_palette(db_path, guild_id)` — Economy → Sinks → **Sync Palette** —
reconciles the catalog to the images on disk:

- **Source folder:** per-guild managed uploads at `swatches/<guild_id>/`; it wins
  as soon as it holds one validly named file, else the legacy global
  `booster_swatch_dir` config key (a host path, still settable under Config →
  Global → Server File Paths) is used (`resolve_swatch_directory`).
- **Filename contract:** `ColorName_HEX1_HEX2.ext` (png/jpg/jpeg/gif/webp) →
  name + gradient pair; `key` is the lowercased, underscored label. Invalid names
  are skipped and flagged in the UI. Sort order is the HSV hue of the gradient,
  so the showroom reads as a color wheel.
- **What sync owns:** name, hexes, image path, sort order. It deliberately does
  **not** touch `price` or `enabled` — those are the admin's edits, and a re-sync
  must not silently re-enable a retired color or reset its price.
- **Retiring:** a color whose file is gone is *disabled* if a live rental points
  at it, and deleted outright only if nobody holds it.
- An empty or all-invalid folder **aborts** rather than retiring everything.

A color is **rentable** only with both hexes present and `enabled = 1`. A row
whose filename never parsed still exists (dropping it would make the palette
silently short) but is never offered, and the dashboard flags it for a re-sync.

## Showroom panel

`post_or_update_palette_panel` (Economy → Sinks → **Post Panel**) deletes the
previously posted messages (bulk-delete in ≤100 chunks per channel, per-message
fallback for >14-day-old messages), then posts a header plus one message per
**rentable** color — the swatch image with a single button — with zero-width-space
spacers between. Message ids are saved to `econ_color_panel_messages` for the
next repost's cleanup. Posting with nothing rentable is a 400.

The showroom exists because a select menu can name a color but not show it; the
art is the only place the gradients can actually be seen.

Buttons are `PaletteColorButton`, a `discord.ui.DynamicItem` whose custom-id
template is **still `booster_role:(?P<key>.+)`**, registered at startup via
`bot.add_dynamic_items(PaletteColorButton)` in `src/dungeonkeeper/__main__.py`.
The legacy prefix is deliberate: panels posted before migration 159 carry those
ids, and re-templating would have silently broken every button already sitting in
a channel. `key` is likewise carried over from `booster_roles.role_key` so those
buttons still resolve.

## Configuration (Economy → Sinks → Color Palette)

Admin lives beside the icon catalog in `static/js/panels/economy-sinks.js`, with
routes in `src/web_server/routes/economy.py`, all `require_perms({"admin"})`:

| Route | Purpose |
|---|---|
| `GET /api/economy/color-catalog` | Every color, with `rentable` / `in_use` flags. |
| `GET /api/economy/color-catalog/{id}/image` | Swatch preview. |
| `PATCH /api/economy/color-catalog/{id}` | Rename / re-price / enable-disable / reorder. |
| `DELETE /api/economy/color-catalog/{id}` | Delete — **409** while a live rental points at it. |
| `POST /api/economy/color-catalog/sync` | Run the swatch sync. |
| `POST /api/economy/color-catalog/post-panel` | Repost the showroom. |
| `GET/POST/DELETE /api/economy/color-catalog/swatches[/{filename}]` | Managed uploads: list / upload (8 MB cap, sanitized filenames, image extensions only) / delete. |

The panel-posting guards (`routes/panel_posting.py`) refuse up front when the bot
lacks View Channel / Send Messages / **Attach Files** — the showroom sends image
attachments and no embeds, and the poster deletes the old panel before its
unguarded sends, so a missing permission would otherwise leave the guild with no
showroom and a repost that fails the same way.

There is no `config-booster-roles` panel any more; it and its five
`/api/config/booster-roles/*` routes were removed in the same change.

## Stored data

```
econ_color_catalog(id, guild_id, key, name, hex1, hex2, image_path,
                   price, enabled, sort_order, legacy_role_id, created_at)
    UNIQUE (guild_id, key)
econ_color_panel_messages(guild_id, channel_id, message_id)
    PK (guild_id, message_id)
econ_rentals.catalog_color_id   -- the palette entry a role_preset rental wears
```

Both tables are guild configuration and hold **no per-user data**, so neither
needs a `data_register.md` row; `econ_rentals` is already inside the registered
`econ_*` bundle covered by `econ_purge_user`.

Plus `booster_swatch_dir` in config (global, guild 0) and uploaded swatch images
on disk under `swatches/<guild_id>/`.

## Non-goals

- **No self-service color creation** — members pick from the admin-curated set;
  bringing your own two colors is `role_gradient`, a separate product.
- **No editing a gradient by hand** — the hexes come from the swatch filename, so
  editing them in the dashboard would desync the color from its art.
- **No buying from the showroom** — the panel applies, the shop sells.
- **No automatic panel refresh** after a sync or a color edit; reposting is an
  explicit admin action.
- **No slash-command surface at all.**
