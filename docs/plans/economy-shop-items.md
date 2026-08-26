# Custom shop items (economy spending)

**Status:** ALL FIVE STAGES BUILT (2026-08-26) · **Owner:** economy · **Spec:** `docs/economy_spec.md` §6

## Goal

Let staff sell things the code doesn't hardcode. Today the shop's catalogue is
a fixed list of perks compiled into `economy/perks.py`; adding a product means
a release. An admin should be able to define an item on the dashboard — a name,
a price, and what happens when someone buys it — and have it appear in
`/bank shop` the same day.

Driven by user request 2026-08-25. Private rooms (spec §8, Stage 6) were scoped
into the same session and then explicitly deferred; see "Deferred" below so the
next session doesn't re-derive the decisions already made about them.

## Locked decisions (user Q&A 2026-08-25)

| Decision | Choice |
|---|---|
| Fulfilment | **Per item**: `role` (grant one role automatically) or `manual` (a staff to-do) |
| Duration | **Per item**: `once` (pay once) or `weekly` (renewing rental) |
| Member surface | The existing `/bank shop`, **own section** below the perk table; absent entirely in a guild with no items |
| Limits | **Stock**, **per-member limit**, **availability window**. Deliberately *no* role gate |
| Dashboard home | **A tab on Shop & Perks** (`economy-sinks`), not a new nav entry — Spending stays at two entries |
| Money timing | **Escrow on purchase**, per the sponsored-emoji flow; exactly-once refund on deny/cancel/expiry |
| Automatic effects | **Roles only for v1** — no coin payouts (a faucet inside a sink), no announcement posts |
| Mod checklist | **The existing Todo List** — a manual purchase auto-spawns a todo; ticking it off delivers the order |
| Refusing an order | **The orders queue** — refund + reason; the todo closes as *missed*, never as done |

Defaults taken without a specific question, stated so they're contestable:
members may cancel their own pending order for a refund; an item can prompt for
an optional buyer note; custom items fire the `shop_purchase` quest trigger;
they are **not** giftable and **not** covered by the staff comp in v1.

## Why the todo board rather than a bespoke queue

The ask was "an auto-populated to-do list to help the mods keep up". That
already exists: `todos` + two sticky in-channel boards (`all`, `chores`) +
the Moderation → Todo List panel, and mods already work through it. A second
queue on the shop page would be a parallel list competing with the one they
have the habit of reading.

The provenance shape also already exists. Migration 134 added
`todos.recurring_id` so a spawned row knows what produced it; `purchase_id` is
the same column for the same reason.

**Delivery rides an existing exactly-once guard.** `complete_todo` is a guarded
`UPDATE ... WHERE completed_at IS NULL AND missed_at IS NULL` that returns True
exactly once, precisely so the board button and the dashboard can race. The
escrow release hangs off that boolean instead of inventing its own idempotence.

**Refusing uses `mark_missed`, not `complete_todo`.** That function exists to
close outstanding work without crediting it — exactly a refunded order. A
refunded order must never render as delivered.

## The privacy constraint that shapes the task text

`data_register.md` records `todos` as **anonymised, not deleted** on erasure
(`added_by` → 0, `completed_by` → NULL), and the stated ground is that the row
is two things at once: ids naming a person, and *task text that is server work
product, not member disclosure*. Deleting the row to reach the ids would take
real work off other people's list.

A task reading `Deliver Custom Emoji to @Billy` would quietly falsify that
ground — the member would survive the erasure inside the work-product half.

**So the task text names the item only** (`Deliver Custom Emoji`), and the buyer
is rendered live by joining `purchase_id → econ_shop_purchases.user_id`. Since
`econ_shop_purchases` *is* purged, an erased buyer's order line renders the same
"unknown member" every other surface already renders. Costs one join on the
board render; keeps a documented property true.

## Data model — migration 179

### `econ_shop_items` — guild config

```
id, guild_id, name, blurb, description, price,
kind      CHECK (kind IN ('role','manual')),
billing   CHECK (billing IN ('once','weekly')),
role_id, stock (NULL = unlimited), sold, per_member_limit (NULL = unlimited),
available_from, available_until, ask_note, enabled, sort_order,
created_by, created_at
```

`created_by` is an admin id — the mention_award_rules precedent: preserved under
Art 17(3)(e) as the record of who opened a spend surface.

### `econ_shop_purchases` — per member

```
id, guild_id, user_id, item_id,
state CHECK (state IN ('pending','fulfilled','live','denied','cancelled',
                       'expired','lapsed')),
price (snapshot), note, todo_id, rental_id, resolver_id, deny_reason,
refunded_at, created_at, resolved_at
```

Purged (added to `economy_service._PURGE_USER_ID_TABLES`); the money history
survives in the preserved `econ_ledger`, so erasing the order corrupts nothing.

### `todos` — one column

`purchase_id INTEGER` + a partial index on open rows, mirroring `recurring_id`.

### `econ_rentals` — the 4th table rebuild

`catalog_item_id` joins `catalog_icon_id` / `catalog_color_id`, and the `perk`
CHECK gains `custom_item`.

> **The live-rental unique index must become
> `(guild_id, user_id, perk, beneficiary_id, COALESCE(catalog_item_id, 0))`.**
> SQLite treats every NULL in a unique index as distinct, so adding the bare
> nullable column would let two live `role_color` rentals coexist — silently
> dissolving the one-live-rental-per-perk guarantee for every existing perk.
> The COALESCE collapses all the NULLs to 0, preserving today's behaviour
> exactly while letting a member hold several *different* custom items at once.

## Flows

| Item | Buy | Resolve |
|---|---|---|
| `once` + `role` | debit, grant role | complete on the spot |
| `once` + `manual` | escrow debit, spawn todo | tick → `fulfilled` · refund → `denied` + missed · 14d → `expired` + refund |
| `weekly` + `role` | `rent_perk('custom_item', item)`, grant role | lapse strips the role (new `revoke_perk_effect` branch) |
| `weekly` + `manual` | escrow week one, spawn todo | tick → opens the rental, weekly clock starts |

Stock is a **guarded UPDATE** —
`UPDATE econ_shop_items SET sold = sold + 1 WHERE id = ? AND (stock IS NULL OR sold < stock)`
— so two simultaneous buyers cannot both take the last one; zero rows affected
is the sold-out signal. Same shape as `apply_debit`'s guarded balance write.

Ledger kinds: `shop_item` (escrow debit), `shop_item_refund` (plain credit,
never boosted). Weekly renewals bill the ordinary `rental` kind.
Unfulfilled orders expire on `shop_item_expire_days` (default 14), the emoji
sponsor sweep pattern.

## Stages

All five landed. Stage 1 shipped 2026-08-25 (`eefd885f`), with a round of
`/code-review` fixes in `5e178c58` — the sharp one being that a refused
`purchase()` was committing its writes, because `open_db` commits on normal
exit and a returned refusal is not a rollback. Stages 2–5 shipped 2026-08-26.

1. **Migration + logic + service.** `economy_shop_items_logic.py` (pure
   purchasability verdict: enabled, window, stock, per-member limit, funds) and
   `economy_shop_items_service.py` (buy / resolve / refund / expire). Tests.
2. **Discord.** The shop's items section, the buy flow inside Open Shop, the
   role grant, the lapse branch.
3. **Todo integration.** Spawn on purchase, the completion hook, the board and
   panel render join.
4. **Dashboard.** Item editor + orders list with refund. Shipped as stacked
   sections rather than a tab (the dashboard has no tab machinery); the perk-shop
   page split of 2026-08-26 then moved the orders queue onto **Approvals**, beside
   the sponsored-emoji queue, which is a better home than the one planned here.
5. **Docs.** `economy_spec.md` §6, `data_register.md` (new row +
   `todos` amendment), `manual.html`, the help section.

## Deferred — private rooms (spec §8)

Scoped in the same Q&A, then deferred by the user before any code. Decisions
already taken, so the next session starts here rather than from zero:

- Text rooms and voice rooms are **two separate products**, priced separately —
  not one bundled clubhouse. Matches the spec's two price rows.
- **No-contact reaches every pair in the room**, not just owner↔invitee: an
  invite is refused if the invitee is no-contact with the owner *or* anyone
  already inside, and a room is swept when a no-contact pair forms later. The
  refusal must be indistinguishable from an ordinary failure.
- Lapse is **§8 as written**: a text room hides for 14 days with its overwrites
  snapshotted (so re-renting restores the exact guest list) then deletes; a
  voice room deletes at once.
- The dormant `price_voice_room` key is still unadopted. Decide adopt-or-
  supersede explicitly when the feature is picked up — a config key whose reader
  never shipped is a silent no-op.
