# Implementation plan — features auto-create the roles they need

**Status:** Stages 1–2 built 2026-08-22. Stages 3–4 not started.
**Source:** todo #105 (Billy, 2026-08-18, during the Survivor first-look review).
**Reference implementation:** Survivor (`web_server/routes/survivor.py` create_season
+ `survivor/tasks.py::reconcile_roles`).

**Goal, in Billy's words:** no feature ever needs manual role setup.

**Decisions already taken** (asked and answered 2026-08-22, before planning):

1. **Creation is lazy — at first use**, not at panel-save and not behind a
   "Create it for me" button. This matches what Jail, Inactive and DM modes
   already do, and it means a feature nobody uses never litters the role list.
2. **Ping-only roles get created, and that's all.** How a member comes to
   *hold* a ping role stays exactly whatever each feature does today. Some will
   sit empty until someone wires an opt-in surface; that is accepted and
   explicitly out of scope here.
3. **The verification roles stay manual.** `unverified_role_id` and
   `intake_verified_role_id` are not about *enabling a feature* — they name
   membership state a human already curates. Class C, not converted.
4. **Stage 1 lands as one commit**, not five.
7. **Ping roles are provisioned only where the dial was never configured** — a
   stored "(none)" is a preference and is left alone. (Taken at Stage 2; see
   that section for what it cost.)
5. **Adopt-by-name is exact-match only** — what Survivor and DM modes already
   do. `"Jailed"` adopts `@Jailed` and nothing else; `@jailed` gets a twin.
   Never grabbing the wrong role beats adopting a few more.
6. **A re-create is announced; a first create is silent.** Stored id existed and
   no longer resolves ⇒ the admin deleted the role ⇒ post to the mod log.

---

## Stage 0 — the audit

### What "role-shaped config knob" turned up

67 distinct role identifiers appear in `src/`. Stripping the ones that are
*derived at runtime* (`member_role_ids`, `held_role_ids`, `gained_role_ids`,
`before/after_role_ids`, `all_role_ids`, …) leaves **44 knobs an admin can
actually set**, across three storage shapes:

| Storage | Knobs | Examples |
| --- | --- | --- |
| `config` KV (`get/set_config_value`) | 22 | `jailed_role_id`, `guess_role_id`, `xp_level_5_role_id` |
| A feature's own table | 19 | `revive_guild_config.role_id`, `bump_tracker_config.role_id`, `grant_roles.role_id` |
| JSON blob inside a config row | 3 | photo challenge's `ping_role_id` in `games_game_config.options` |

Tables carrying a role column: `announcement_buttons`, `announcements`,
`booster_roles`, `bump_tracker_config`, `econ_color_catalog`,
`econ_personal_roles`, `games_editor_role`, `games_scheduled`, `grant_roles`,
`inactivity_prune_rules`, `intake_cards`, `intake_card_steps`,
`mention_award_rules`, `no_contact_settings`, `pen_pals_config`,
`revive_channel_config`, `revive_guild_config`, `role_menu_*`, `role_menus`,
`role_prune_events`, `wellness_config`.

### Classification

The point of the audit. Three classes; only A and B get touched.

#### Class A — bot-owned (the role exists *because* the feature exists)

The bot creates it, the bot hands it out, and deleting the feature would leave
the role meaningless. These are unambiguously auto-creatable.

| Knob | Feature | Today |
| --- | --- | --- |
| `jailed_role_id` | Jail | ✅ auto-creates (`jail/apply.py:304`) |
| `inactive_role_id` | Inactive | ✅ auto-creates (`inactive/apply.py:177`) |
| `open/ask/closed_role_id` | DM modes | ✅ auto-creates (`dm_perms_service.py:591`) |
| `role_survivor_id`, `role_ghost_id`, `role_sole_survivor_id` | Survivor | ✅ auto-creates (`routes/survivor.py:203`) — the reference |
| `econ_personal_roles.role_id`, `econ_color_catalog.role_id` | Perk shop personal/colour roles | ✅ auto-creates (`perk_actions.py:299`) |
| `voice_master_spectator_gate_role_id` | Voice Control spectate dial | ❌ **convert — the only one** |

**Five features already do this, five different ways.** That is the first real
finding: there is no shared helper, and the five copies disagree on every
interesting detail — whether they adopt an existing role by name (Survivor and
DM modes do; Jail and Inactive don't), whether `Forbidden` is reported or
swallowed, whether `HTTPException` is caught at all (`ensure_dm_roles` doesn't —
a rate limit there escapes into a member's button click), and whether the
created role gets `Permissions.none()` (Jail and Inactive pass it; the other
three inherit `@everyone`'s).

#### Class B — ping / opt-in roles (safe to create, opt-in unchanged)

Naming one of these grants nothing. Per decision 2, we create and store; we do
not build opt-in surfaces.

`welcome_ping_role_id` · `qotd_ping_role_id` · `guess_role_id` ·
`game_role_id` (economy game opt-in) · `risky_ping_role_id` ·
`revive_guild_config.role_id` + `revive_channel_config.role_id_override` ·
`bump_tracker_config.role_id` · `games_scheduled.announce_role_id` ·
photo challenge `ping_role_id` · `promotion_review_ping_role_id`

#### Class C — member-supplied by nature; **never** auto-create

The trap Billy flagged, and it is much bigger than Pen Pals alone. Three
distinct reasons a knob lands here:

**C1 — it points at an existing membership role.** Auto-creating makes a twin
and the feature silently stops matching the real one.
`pen_pals_config.opt_in_role_id` (**this is Denizen**, the guild's main
membership role — see memory `pen-pals-opt-in-role-is-denizen`) ·
`grant_roles.role_id` and `grant_roles.required_role_id` (NSFW / Denizen /
Veteran) · `xp_level_5_role_id` (the promotion target, an existing role) ·
`promotion_review_grant_role_id` (what the Grant button hands out).

**C2 — it names authority, and creating an empty one is a silent no-op.**
`admin_role_ids` · `mod_role_ids` (both are the permission boundary and are
hard-blocked from model writes in `settings_registry`) · `greeter_role_id` ·
`qa_role_id` · `manager_role_id` · `economy_manager_role_id` ·
`games_editor_role.role_id` · `whisper_role_id` · `bypass_role_ids` ·
`no_contact_settings.alert_role_id` · Pen Pals `staff_role_ids`.

**C3 — it *filters, watches or targets* roles that exist for other reasons.**
`auto_role_ids` (granted on join) · `role_menus` option roles,
`required_role_id`, and binding roles · `inactivity_prune_rules.role_id` ·
`mention_award_rules` author/mentioned filters ·
`announcement_buttons.role_id` (a self-serve button pointing at an existing
role; `core/role_safety.py` already gates which roles may ride one) ·
**all of Intake** — `intake_cards.auto_role_id` / `intake_card_steps.auto_role_id`
are *watchers*: a checklist step auto-ticks when the member **gains** a role
that already exists (the member role, the NSFW role). Creating one would give
the bot a role nobody ever gains, i.e. a step that can never tick.

**C4 — membership state a human curates.** `unverified_role_id` and
`intake_verified_role_id` (Billy, 2026-08-22: *"leave them manual, this is
about enabling features"*). Both are Class A in a fresh guild and Class C in
TGM, where the ☑️ role has real history — a dead prerequisite gate that ran for
months, and a `role_events` table that saw 5 of 77 grants (memory
`verified-role-backfill-2026-08-11`).

**Count: 6 already done, 1 to convert in A, 10 in B, 27 in C.** Roughly
two-thirds of the role dials in this bot are legitimately member-supplied. The
blanket reading of "make every feature create its roles" would have broken most
of them — and the audit's real output is that **the work is almost entirely
consolidation, not conversion**: five features already auto-create, badly and
five different ways, and exactly one feature plus ten ping roles are missing.

---

## Stage 1 — one helper, four call sites retired *(built)*

> **Built with two deviations from the plan above, both found while doing it.**
>
> **The perk shop is NOT retrofitted, and must never be.** It looked like a
> sixth copy of the same pattern; it isn't. Its roles are **per-member** and
> the name is **member-chosen** (`perk_actions.py:198` — the `role_name` perk
> puts the member's own string on the role). Adopt-by-name there would let a
> member with that perk point their personal role at the guild's real
> @Moderator: `_reconcile_role` renames and recolours whatever id is stored,
> and `apply_role_perks` then `add_roles` it to them. Privilege escalation
> through a cosmetic perk. The module docstring says so, in those terms, so the
> next person doesn't "finish the job". Its own `_create_role` already handles
> `HTTPException`, so it loses nothing.
>
> **DM modes can persist nothing and cannot reach the mod log.**
> `ensure_dm_roles` is reached only from `set_member_dm_mode(member, mode,
> role_ids)`, which holds neither an `AppContext` nor a `db_path`. It gets the
> three real fixes (`HTTPException` handling, `Permissions.none()`,
> exact-match adopt) with a no-op `store`; that is what it did before, and
> adopt-by-name re-finds the role each pass. A recreate there is therefore
> silent — the one place decision 6 doesn't reach. Threading a ctx through is a
> Stage 2 candidate, not a Stage 1 smuggle-in.

New `src/bot_modules/core/role_provision.py`. Nothing in this stage changes
observable behaviour except where the five existing copies disagreed.

```python
@dataclass(frozen=True)
class RoleSpec:
    name: str                 # target/adoption name, e.g. "Jailed"
    reason: str               # Discord audit-log reason
    permissions: discord.Permissions = discord.Permissions.none()
    colour: discord.Colour | None = None
    hoist: bool = False
    mentionable: bool = False

async def ensure_feature_role(guild, spec, *, load, store) -> discord.Role | None
```

`load`/`store` are a getter/setter pair so a knob in the `config` KV, a knob in
a feature table, and a knob inside a JSON blob all use the same helper; a
`config_key=` shortcut covers the 22 KV cases.

Resolution order — **the adopt step is the important one**:

1. Stored id resolves via `guild.get_role` → use it. (No API call, no writes.)
2. Stored id is 0/stale, but a role named **exactly** `spec.name` exists →
   **adopt it**, store the id. This is what stops a fresh install from twinning
   TGM's existing @Jailed / @Inactive. Exact match per decision 5; if two roles
   share the name, the lowest-positioned wins, deterministically.
3. Neither → `create_role(**spec)`, store the id.
4. `Forbidden` → log once, return `None`. **Never raises into an interaction.**
5. `HTTPException` → log, return `None` (fixes the `ensure_dm_roles` gap).

**Re-create is a distinct outcome from create** (decision 6). The pure decision
function separates them — a stored id that was non-zero and no longer resolves
means an admin deleted the role — and the helper then, best-effort:

* posts one line to `guild_config(guild_id).mod_channel_id` naming the role and
  the feature ("@QOTD Ping was deleted; Dungeon Keeper made a new one — its
  members are gone. Repoint it on Config → …"). There is no shared mod-log
  poster in the codebase; this follows `role_menus/views.py:479`'s pattern.
* writes a durable audit row via `moderation.write_audit`, because **log.txt is
  wiped every boot** and the mod-log message is the only other record.

A missing `mod_channel_id`, or a send that fails, must not block the role.

Everything created gets `Permissions.none()` unless the spec says otherwise, and
**no channel overwrites are set by the helper**. Jail's and Inactive's
deny-view-everywhere sweeps stay in their own modules, per-channel and explicit
— category grants do not cascade (2026-08-05 Use Activities incident).

The decision itself is pure and testable without Discord mocks:

```python
def choose_role_action(stored_id, live_role_ids, names_to_ids, target_name)
    -> ("use", id) | ("adopt", id) | ("create", None)
```

A third hook fell out of the retrofit: **`on_create`**, awaited only after a
create or recreate, never after a use or adopt. Jail and Inactive lay down
deny-view-everywhere channel overwrites *on first creation only*, and firing
that sweep over an **adopted** role — one the guild already configured — would
be a destructive surprise. It also keeps the promise that the helper itself
never touches channel permissions: overwrites stay in the feature, per channel
and explicit, because category grants do not cascade.

`tests/test_role_provision_logic.py` covers: stored hit; stored stale + name
hit; **stored stale + no name hit ⇒ `("recreate", None)`, the mod-log case**;
stored 0 + no name hit ⇒ `("create", None)`, silent; stored 0 + name hit;
case differing by one letter does **not** adopt (decision 5); duplicate names
resolve deterministically; empty guild. Then Jail, Inactive, DM modes, Survivor
and the perk shop are retrofitted onto it, one commit each, existing tests green.

**Deliberate behaviour changes in this stage, worth a Testing: line each:** Jail
and Inactive gain adopt-by-name (previously they'd make a second @Jailed if the
dial was cleared); DM modes stop leaking `HTTPException` into a member's click,
and a mode whose role can't be provisioned now leaves the member's existing
modes alone instead of raising `KeyError` on `roles[mode]`; Survivor's created
roles drop to `Permissions.none()`; a role remade after a deletion posts to the
mod log and writes an audit row (Jail, Inactive, and Survivor's — not DM modes,
see above).

## Stage 2 — the conversions *(built)*

Decision 7, taken before building: **provision only where the dial was never
configured.** A ping dial's "(none)" is a working preference — features read
`if role_id:` and stay silent — so provisioning over a stored 0 would delete the
only way to say "don't ping" *and* put a mention nobody holds into every post.
Prod already records the difference: a deliberate "(none)" writes a row holding
`"0"`, a dial nobody touched has no row at all. `role_dial_opted_out` reads
exactly that, legacy `guild_id=0` fallback included.

**That test also decides which dials are eligible, and it cost five of the
eleven this stage was scoped to.** Three cannot express "never configured":

| Dropped | Why |
| --- | --- |
| `bump_tracker_config.role_id` | `NOT NULL DEFAULT 0` — both cases store 0 |
| `revive_guild_config.role_id` | nullable, but the panel's "(none)" is `value=""`, saved as NULL — same as never set |
| scheduled games / photo challenge / revive per-channel | per-instance: "unset" was chosen by an admin in a form offering "(none)", so there is no unconfigured state at all |

Two more were dropped for a harder reason — **an empty role is worse than no
role**, which makes them gates rather than pings, and Class C after all:

* **Voice Control's spectate gate** — the one Class A conversion this stage was
  supposed to deliver. Ungated spectate makes `@everyone` the audience; a gate
  role *denies* `@everyone` Connect and hands the room to the role instead. An
  empty gate role is a spectate room nobody can enter. **Class A is now empty.**
* **`guess_role_id`** — unset means "Guess isn't set up" and the game says so
  plainly. Provisioning turns that into a configured game refusing every member
  with "you need the Guess role".

What shipped — all in the `config` KV, which is not a coincidence: it is the
only store where the two states are distinguishable.

| Dial | Role | Trigger |
| --- | --- | --- |
| `welcome_ping_role_id` | @Welcome Ping | a welcome post |
| `econ_qotd_ping_role_id` | @QOTD | a QOTD post |
| `risky_ping_role_id` | @Risky Rolls | a Risky Rolls round |
| `promotion_review_ping_role_id` | @Promotion Reviewers | a review card |
| `econ_game_role_id` | @Economy Notifications | a member pressing 🔔 |

The last is the only one that makes something work that didn't: pressing 🔔 on
a guild with no opt-in role used to dead-end at "ask an admin". It is also the
only trigger that is a *member asking for the role* rather than the bot needing
one — so it is the only one of the five that will actually gain members.

`services/feature_roles.py` is the registry: one auditable list, with the
exclusions above recorded in its docstring so the next person doesn't "finish
the job". `tests/test_feature_roles.py` guards the rules (ping-only, never
mentionable, no known hazard creeping back in).

Three traps worth remembering, all found against the live config rather than by
reading code.

The econ keys carry the **`econ_` prefix** and are guild-scoped with **no legacy
fallback** — reading them as bare `qotd_ping_role_id` finds nothing and would
provision over a guild that is already configured.

The welcome path reads the **cached snapshot** first and only provisions when
that is 0, so the provisioner is not touched on every join.

And the one that would have shipped a visible mistake: **an id inherited from
the legacy `guild_id=0` row names a role in a different guild.** Prod has
exactly this — `welcome_ping_role_id` is set at `guild_id=0`, and three guilds
have no row of their own. That id can never resolve in those guilds, and the
first draft read "stored id that doesn't resolve" as "an admin deleted the
role", which would have posted a *false* deletion warning to three mod channels
on the next join. `choose_role_action` now takes `stored_is_own`, and only a
guild's **own** stored id can count as a deletion; an inherited one is a first
run, and silent.

Fixed along the way: the QOTD ping's allow-list was widened to
`AllowedMentions(roles=[...])`, whose *unset fields default to allow* — a
question containing `@everyone` would have pinged the server. `everyone`,
`users` and `replied_user` are now pinned False, with a regression test.

## Stage 3 — the dashboard side *(not started)*

The dial stops being the *only* way a role gets set, so the panel has to say so.
A role dial whose feature auto-creates renders "created automatically — pick a
different role to repoint" instead of a bare "(none)", and a small
Config → Roles status card lists each managed role with its live state
(present / missing / no Manage Roles). Existing route ids are frozen; this rides
`config-roles`.

## Stage 4 — docs *(not started)*

New `docs/role_provisioning_spec.md` (Reference) + `docs/INDEX.md` row;
`manual.html` gets a line under each converted feature saying the role appears
by itself. **No new tables, so no `data_register.md` row** — every knob reuses
storage that already exists.

---

## Open questions

1. **Class B ping roles will sit empty** — accepted per decision 2. Worth a
   follow-up todo for opt-in surfaces, or leave it? Stage 2 sharpened this:
   only @Economy Notifications has a way for members to take it, so the other
   four will stay empty until something hands them out. *(Non-blocking.)*
2. **`econ_game_role_id` is stored as 0 in two prod guilds**, so the 🔔 dead end
   persists there by the opt-out rule. Setting the dial (or clearing the row)
   is a one-line dashboard fix if that 0 was never a deliberate "no".

*Resolved:* verification roles stay manual (3); Stage 1 lands as one commit (4);
adopt-by-name is exact (5); a re-create hits the mod log (6); provision only
where the dial was never configured (7).
