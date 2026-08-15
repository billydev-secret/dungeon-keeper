# Booster colours → shop purchase (todo #76)

**Status: implemented 2026-08-15** (migration 159). Kept as the record of why
this was done and what was decided; the built behaviour is specified in
[color_palette_spec.md](../color_palette_spec.md), which wins on any detail.

Deltas from the plan as written:

- **Grandfathering needed no new state at all.** The role-hierarchy trick below
  turned out to be sufficient on its own, so there is no grandfather table and —
  as predicted — no `data_register.md` change (both new tables are guild config
  with no user column, and `econ_rentals` is already inside the registered
  `econ_*` bundle).
- **Per-colour pricing shipped**, via `econ_rentals.catalog_color_id` mirroring
  `catalog_icon_id`, with `price = 0` meaning "use the flat price" so a palette
  can still be priced once for the whole set.
- **The showroom panel survived** as a browsable storefront, but it never
  charges: it applies a colour to members who are already entitled and points
  everyone else at `/bank shop`. Buying happens where the price is on screen.
- **`routes/panel_posting.py` is new** — the panel-posting guards were private to
  `config.py` and two routers needed them, so they moved to a shared module
  rather than being copied.
- **Code price defaults** are `price_role_preset` 80 and `price_role_gradient`
  240 (up from 120). The **live dials — palette 100, gradient 300 — are still
  Billy's to set** on Economy → Sinks; defaults only affect guilds with no
  explicit config row, and the main guild has one.

## What this changes

The cosmetic gradient palette stops being a boost entitlement and becomes a
rented shop perk anyone can buy. Boosters get no colour-specific advantage
(they already earn 1.5× currency). Members holding a palette colour today keep
it, free, forever — but switching becomes a paid privilege.

## What's actually there today (verified against prod, read-only)

`booster_roles` holds **11 rows, one guild** (1469…666) — all colour, no
non-colour rows. Each is a *two-hex gradient* whose swatch filename carries the
pair (`dusk_ember_F0A830_8842C8.png`); `sync_swatches` creates a Discord role
per swatch with `color`/`secondary_color` from that pair. No role icons are set
— `image_path` is only the panel artwork. `sunbeam` has `role_id 0`: its
Discord role was never created, so it is unclaimable today.

Live census (live Discord member state, not `role_events`):

| | |
|---|---|
| Members scanned | 205 |
| Current boosters | 27 |
| Palette holders | **15** (12 still boosting, 3 lapsed) |
| Holders wearing more than one | 0 (mutual exclusion holds) |
| Boosters with no palette colour | 15 |
| Colours with zero holders | 6 of 11 |
| Holders who also rent a personal role | 2 |

The claim gate is `member.premium_since is None` and nothing revokes the role
when boosting lapses — hence the 3 lapsed holders.

**The shop already sells gradients.** `role_gradient` ("two-color fade",
150 coins) has **8 active rentals**; `role_color` (solid, 65) has 10. So the
palette is not filling an empty slot — it's a curated, cheaper sibling of a
perk members already buy.

Perks are **recurring rentals**, not one-time buys: `econ_rentals` bills at
`next_bill_at` with `grace`/`lapsed`/`cancelled` states, and `perk` carries a
`CHECK` constraint listing every valid kind.

## Decisions taken (Billy, 2026-08-13)

1. **Grandfather** all 15 current holders — keep the colour permanently, free.
2. **No booster advantage** — everyone pays; boosters already get 1.5× currency.
3. **The curated palette stays its own product**, cheaper than `role_gradient`,
   which keeps its "any two colours you like" premium slot.
4. **Recurring rental**, same machinery as every other perk.
5. **Grandfathered holders keep what they wear; switching requires the rental.**
6. **Palette 100/week; `role_gradient` rises 150 → 300.** The curated option
   becomes the value pick and custom becomes a deliberate splurge.
7. **Option B** — the palette moves into `econ_color_catalog`, mirroring the icon
   catalogue.

## Design

### Grandfathering costs nothing to build

Personal rented roles are positioned *above* the `#### Cosmetics` anchor
(`perk_actions._COSMETICS_ANCHOR`) precisely so a rented colour outranks a
palette swatch. That gives grandfathering for free:

- Leave the 11 shared Discord roles alone and strip nobody. The 15 keep wearing
  theirs.
- They can't switch, because the claim button will require a live rental.
- If a grandfathered member *does* rent, their personal role overlays the shared
  one; when that rental lapses and the personal role is deleted, **their
  original colour reappears underneath**.

So there is no grandfather table, no per-user state, and therefore no
`docs/data_register.md` row needed. The one hard requirement: `sync_swatches`
currently **deletes** a Discord role whose swatch file disappears — that path
must refuse to delete a role that still has holders, or it will silently strip
grandfathered members.

### The perk projects colours, it does not grant the shared role

New perk kind `role_preset`. Picking a preset writes its hex pair into
`econ_personal_roles.color`/`color2` and calls `apply_role_perks`. It reuses the
entire projector, lapse, and revoke pipeline unchanged, and needs no new colour
mode — `effective_color_mode` just treats `role_preset` as `gradient` (a preset
*is* a two-colour fade). A member renting both `role_gradient` and `role_preset`
shares one personal role and last-write-wins; that's inherent to the one-role
model and fine.

### Where the catalogue lives — decided: Option B

`econ_icon_catalog` is already the pattern this wants to be: admin-curated,
per-item priced, browsable in Discord (`open_icon_catalog` / `pick_catalog_icon`),
chosen id recorded on the rental as `catalog_icon_id`, surcharge resolved by
`_catalog_icon_price`, administered from `economy-sinks.js` next to the perk
price dials. Its schema is nearly `booster_roles` already.

So the palette migrates into `econ_color_catalog` `(id, guild_id, name, hex1,
hex2, image_path, price, enabled, sort_order)` — the 11 rows carried over,
`catalog_color_id` recorded on the rental, admin folded into `economy-sinks.js`
beside the perk dials, and the `config-booster-roles` panel plus its five routes
retired. Per-colour pricing comes along free. This lands the feature where the
shop's other curated catalogue already lives and retires the "booster" misnomer
rather than entrenching it.

The swatch-sync pipeline survives (filename hex pairs are a genuinely good
authoring flow), retargeted to write the catalogue and to **stop creating Discord
roles** for new presets, since colours now project onto personal roles.

`booster_roles` and `booster_panel_messages` are dropped once the rows are
migrated; the 11 Discord roles themselves stay put, unmanaged, worn by the
grandfathered 15.

### Storefront

The in-channel swatch panel stays — the artwork is the selling point. Buttons
change meaning: no live rental → "Rent **Palette Color** in `/bank shop`";
rental held → pick/switch freely, like `role_color` lets you restyle anytime.

### Shop copy and price

`PERK_LABELS["role_preset"] = "Palette Color"`, short `Palette`, blurb
`curated two-tone`, emoji 🖌️ (🎨 is taken), tier **Signature** beside
`role_gradient`. Price **100/week**, with `role_gradient` rising **150 → 300** so
the curated palette is plainly the value pick. New ladder: name 45, color 65,
palette 100, gradient 300, holographic 500, icon 1200.

**The gradient increase is self-announcing.** The billing loop re-reads the price
each cycle, and a renewal at a changed price DMs the member the old and new
figure (the "economy A2" behaviour added after the 07-30 reprice). So the 8 live
gradient renters are charged 300 at their next weekly bill, each with a DM saying
"was 150" — no comms to build, and nobody is charged more mid-period.

Both numbers are live dashboard dials on prod, so **Billy sets them**; this work
changes the `EconSettings` defaults (which only affect guilds with no explicit
config row) and the docs. Guild 1476…484 ("nut") is someone else's economy on its
own ~8× denomination — its dials are not touched.

Feature-gated on `ENHANCED_ROLE_COLORS` like `role_gradient`, since it projects
a secondary colour.

"nut" also has **no palette rows**, so the shop row must hide (or politely
refuse) when a guild's palette is empty rather than selling nothing.

### Float

This is a **new recurring sink** plus a **doubled existing one**, both pointing
the right way given the +5,221/day float the round-2 retune proposal is trying to
flatten. Nothing is given away: the 15 grandfathered roles were never paid for,
and the 12 boosters who never claimed a colour lose an entitlement they weren't
using. The gradient increase is the one real risk — 8 live rentals at 2× may
produce cancellations rather than revenue, so the sink is worth measuring rather
than assuming (`economy-stats.js` already tracks per-perk price weights). Read
`docs/reviews/2026-08-06-economy-retune-round2-proposal.md` for context; this
plan does not act on it.

## Work items

1. Migration: extend the `econ_rentals.perk` CHECK to include `role_preset`
   (SQLite table rebuild), add `catalog_color_id`, create `econ_color_catalog`,
   migrate the 11 rows, and drop `booster_roles` / `booster_panel_messages`.
   Check the migration number against `main` for collisions and snapshot-test
   against the prod schema via the sqlite3 backup API. Note `booster_roles` is
   created by `init_booster_role_tables` at web-server startup, not a numbered
   migration — that function has to go too, or it will recreate the table.
2. `economy/perks.py`: label, short, blurb, emoji, tier, `SELF_PERKS`,
   `PERK_REFUSAL`, `CUSTOMISE_LABELS`, `FEATURE_GATED`.
3. `perk_actions._FEATURE_FOR_PERK`; `rentals.effective_color_mode`;
   `economy_rentals_service` perk tuple.
4. Price plumbing: `EconSettings.price_role_preset`, `routes/economy.py` field,
   `economy-sinks.js` dial, `economy-stats.js` + `metrics.py` + `stats.py`
   weights, `register.py` ledger label.
5. `economy_cog.py`: preset picker (select menu over the palette) wired into the
   customise dispatch, mirroring `pick_catalog_icon`.
6. Replace `services/booster_roles.py` with a colour-catalogue service: claim
   gate → live-rental check; pick → project hex onto the personal role; keep the
   swatch-sync authoring flow but retarget it at the catalogue and **stop it
   creating or deleting Discord roles** (the delete path is what would strip the
   grandfathered 15).
7. Dashboard: retire the `config-booster-roles` panel, its nav entry, and its five
   routes; fold palette admin into `economy-sinks.js` beside the icon catalogue.
8. Docs in the same commits: replace `docs/booster_roles_spec.md` with a colour-
   palette spec (and fix its `INDEX.md` entry), update `docs/economy_spec.md`, and
   update **`manual.html`** — the shop table, the new price ladder, and removing
   the booster-claim instructions.
   **No `data_register.md` change needed**: `econ_color_catalog` is guild config
   with no user column, the dropped `booster_roles` likewise, and `econ_rentals`
   is already inside the registered `econ_*` bundle covered by `econ_purge_user`
   — so `catalog_color_id` adds no new personal data. Worth a re-check at
   implementation time that `econ_purge_user` still clears rentals cleanly.
9. Tests: `effective_color_mode` with `role_preset`; the rental gate replacing
   the `premium_since` gate; the sync-delete holder guard (fails before the
   fix); empty-palette guild hides the row; price plumbing; one cog wiring
   assertion for the picker.

## Open questions

Non-blocking — each has a working default, noted first:

1. **`sunbeam`** (no Discord role, unclaimable today): *default is keep it* —
   under this design colours project from hex, so it simply starts working.
2. **The 6 zero-holder colours**: *default is keep all 11* — migrate the catalogue
   as-is and prune later from the dashboard, since pruning is now a per-row
   `enabled` toggle rather than a code change.
3. **Telling the 15 grandfathered members**: *default is no DM* — nothing is taken
   from them, and the change is only visible if they try to switch. A line in the
   shop/manual copy covers it. Say the word if you'd rather they hear it directly.
