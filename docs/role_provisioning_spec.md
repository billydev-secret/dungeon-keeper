# Role provisioning — the roles Dungeon Keeper makes for itself (Reference)

Owed since stage 4 of [plans/role-autocreate.md](plans/role-autocreate.md) and
written in round 2 (2026-09-03). Describes what the code does today. Where this
and `src/` disagree, the code wins — start at
`bot_modules/services/feature_roles.py`, which is the registry every surface
here reads.

---

## 1. The honest figure

> Dungeon Keeper makes up to **sixteen** named roles for itself, across
> **fourteen** dials and three mechanisms, plus one more for every member who
> buys a personal role in the perk shop.

The often-quoted "only 5 of 44 dials are safe to auto-create" was true about
the *registry* and misleading about the *bot*: the other nine roles were built
inline at their call sites, where nothing could enumerate them. Round 2 moved
all of them into `feature_roles.MANAGED_ROLES`, which is what makes the roster
page possible at all.

| # | Role | Dial / store | Bot hands it out? | Made when |
|---|---|---|---|---|
| 1 | `@Welcome Ping` | `config.welcome_ping_role_id` | no — mentioned | a member joins and the welcome post is about to send |
| 2 | `@QOTD` | `config.econ_qotd_ping_role_id` | no — mentioned | a question of the day is posted |
| 3 | `@Risky Rolls` | `config.risky_ping_role_id` | no — mentioned | a round opens with its ping on |
| 4 | `@Promotion Reviewers` | `config.promotion_review_ping_role_id` | no — mentioned | a promotion-review card is posted |
| 5 | `@Economy Notifications` | `config.econ_game_role_id` | **yes** | a member presses 🔔 on the economy guide |
| 6 | `@Guess Who` | `config.guess_role_id` | **yes** | **only** while being offered in onboarding |
| 7 | `@Voice Spectator` | `config.voice_master_spectator_gate_role_id` | no — named in channel overwrites | **only** while being offered in onboarding |
| 8 | `@Jailed` | `config.jailed_role_id` | **yes** | the first jail |
| 9 | `@Inactive` | `config.inactive_role_id` | **yes** | the first inactive marking |
| 10–12 | `@DMs: Open` / `Ask` / `Closed` | `dm_mode_roles` | **yes** | a member sets their DM mode |
| 13–15 | `@🏈 Survivor` / `@👻 Ghost` / `@🏈 Sole Survivor` | the season's config row | **yes** | an admin creates a season |
| 16 | `@Wellness Guardian` | `wellness_config.role_id` | **yes** | **Activate Wellness** with "create one for me" |

Plus the perk shop's per-member colour/name roles
(`economy/perk_actions._create_role`), which are deliberately **not** on the
provisioner: their name comes from a member, and adopt-by-name over a
member-chosen name is privilege escalation.

## 2. What the provisioner does

`core/role_provision.ensure_feature_role`, given the id a feature has stored:

1. It resolves to a live role → **use** it. No API call, no write.
2. It doesn't, but an *adoptable* role named exactly the spec's name exists →
   **adopt** it and store its id. Exact match only.
3. Nothing stored and no name match → **create**, silently.
4. Something *was* stored, it was **this guild's own** row, and it resolves to
   nothing → **recreate**, and say so out loud: a mod-channel line plus a
   durable `audit_log` row, because `log.txt` is wiped every boot.

**Adoptable** (`adoptable_role_ids`, round 2) excludes two kinds of candidate
that used to be adopted and then failed at use:

* anything `managed` — an integration owns it and Discord grants it to nobody;
* for a role the bot *hands out*, anything at or above the bot's own top role.
  A guild with a `@Jailed` above Dungeon Keeper used to have it adopted and
  stored, after which every jail 403'd with a hierarchy error about a role
  nobody had chosen. A working twin lower down is the lesser evil, and the
  roster names the duplicate.

Hierarchy is judged **only** for the roles the bot assigns (the `assigns` flag,
11 of the 16). Mentioning a role needs no hierarchy at all, so a position
warning on a ping dial would be crying wolf.

`ensure_feature_role` never raises into a caller: a missing **Manage Roles** or
a Discord hiccup returns `None` and the caller degrades.

## 3. "(none)" versus never configured

The whole safety of provisioning rests on telling those apart, and only the
`config` KV can: a dial nobody has touched has **no row**; picking "(none)" on
the dashboard writes a row holding `"0"` (`role_dial_opted_out`).

Two complications, both real:

* **Panels save whole forms.** Every config panel sends
  `picker.getValue() || "0"` for *every* field on *every* save, so changing a
  payout writes a `0` into an untouched role dial. A stored 0 is therefore
  weak evidence. The answer is **not** more exemptions — it is that each dial
  now carries a line saying which "(none)" it is looking at (§5), backed by
  `bot_managed_roles`.
* **Two dials have no coherent "off"** — `jailed_role_id` and
  `inactive_role_id`. A jail with no role is not a jail. Those carry
  `none_means_off=False` and a stored 0 means "not set up yet". They are also
  the only two, and a test pins the set.
* **A create-on-offer dial is never "off".** Offering the role in onboarding
  *is* the decision that makes it, and `guess_role_id` /
  `voice_master_spectator_gate_role_id` are written a `0` by their own panels
  on every unrelated save. `FeatureRole.honours_none`
  (`none_means_off and not create_on_offer`) is the single answer to "did the
  admin switch this off", and **every** surface asks it that way — the roster
  page, the per-dial state line and onboarding. Reading it two ways is how the
  Guess Who panel came to print '"(none)" — so I won't make one' directly under
  the hint saying that offering it in onboarding will.

`@Economy Notifications` used to be the exception here (`none_means_off=False`,
2026-08-22) while its panel told admins "(none)" turned notifications off — a
preference the code did not enforce. **Billy reversed that on 2026-09-03**: the
dial is now honoured, and with no role set the 🔔 button tells the member
notifications aren't set up in this server.

### Reading a dial that belongs to another guild

`get_config_value` falls back to the `guild_id = 0` row when a guild has none
of its own. An inherited id names a role in a *different* server, so it never
resolves — and reading that as "an admin deleted it" announces a deletion that
never happened. `ensure_config_role` is the only path that knows whether the id
came from this guild's own row (`stored_is_own`), which is why **every
`config`-KV dial must go through it**. Jail and Inactive read their keys
directly until 2026-09-03 and posted "⚠️ **Jailed** was deleted, so I made a
new one" to guilds that had never had one.

## 4. Provenance — `bot_managed_roles` (migration 203)

One row per `(guild_id, role_key)`: the role id, whether the bot **created** or
**adopted** it, and when. Written by `core/role_provision` and nothing else;
`services/role_provenance.py` is the store.

It exists because every state the dashboard showed was otherwise an inference.
With it:

* the roster's states are facts, not guesses;
* "did the bot ever configure this dial" is answerable without the dial's own
  column being able to express it (which is the standing objection against
  reopening `bump_tracker_config.role_id` and Chat Revive's role dial);
* deleting a bot-made role *could* be offered safely — and deliberately is not
  (§6).

**Degrade, never insist.** Every role provisioned before migration 203 has no
row, and the DM trio never will (`ensure_dm_roles` is reached from a member's
button click and holds no database handle). A missing row means *unknown*, and
the card says so rather than guessing.

**No personal data.** Guild, dial, role, origin, timestamp — no member is
named, so there is no `data_register.md` row. The acting admin is deliberately
not stored: `write_audit` already records who changed a dial, and copying a
member id here would turn server configuration into personal data with an
erasure obligation for no information anybody lacks.

## 5. The surfaces

### Per-dial state lines

Every dial that can make a role carries one `.field-hint` line under its
picker: the state, one sentence of consequence, and a link to the roster.
`static/js/role-dial-state.js` renders it from `GET /api/bot-roles/state?keys=…`
— one small request per panel mount and **no cache**, because a guild-scoped
cache here would survive a guild switch and show one server's roles inside
another.

Wired into: Welcome & Leave, Economy Settings, QOTD, Risky Rolls, XP & Leveling
(promotion review), Guess Who, Voice Control, Moderation & Privacy (jail).

### Bot-Managed Roles (`bot-roles`)

Config → Roles, admin-only. The only surface that can show all sixteen. An
audit page, not a form: an opening sentence rather than stat tiles, two groups
(*Roles I hand out* — where hierarchy matters — and *Roles I only point at*),
and one card per role carrying a state badge, a sentence saying what happens
next, and its actions.

Nine states, computed pure in `services/role_roster_service.py`:
`in_use`, `out_of_reach`, `deleted`, `inherited`, `turned_off`, `not_made`,
`adoptable`, `offer_first` — with *renamed*, *duplicated* and *provenance
unknown* carried as notes rather than states, since none of them stops anything
working.

`adoptable` applies the **same two filters as `adoptable_role_ids`** (§2), not
a bare name match: a same-named role that is integration-managed, or that sits
at or above the bot's own top role for a role the bot hands out, is not a
candidate. Promising "I'll use that one rather than making a second" about a
role the provisioner would skip is how an admin ends up with two `@Jailed`
roles and no idea why theirs is being ignored — so the card falls back to
*not made yet* and carries a note saying a same-named role exists that the bot
can't use.

Three writes, each deliberately narrow:

| Action | Does | Refused when |
|---|---|---|
| **Make it now** | provisions a dial that was never set | the role is create-on-offer, or another feature owns the store |
| **Use a different role** | points the dial at an existing role | `@everyone`, `managed`, or above the bot for a role it hands out |
| **Stop managing** | writes `"0"` (the same "(none)" the provisioner honours) and forgets the provenance row | the dial has no coherent "off" (jail, inactive), or another feature owns it |

Roles stored by another feature (the DM trio, Survivor's three, Wellness) are
**read-only** here and link to the page that owns them — a second writer into
those stores is how a repoint gets silently undone by the owning page's next
whole-form save.

### Discord Onboarding (`onboarding`)

Where members actually pick roles up, and — for the two create-on-offer dials —
the only place they can be created. The panel lists the seven opt-in roles,
plans the change server-side against a freshly read config, and confirms before
writing.

Editing onboarding needs **Manage Server** *and* **Manage Roles**. `/invite`
asks for neither more nor less than it needs day to day and **stays narrow**
(Billy, 2026-09-03), so on a least-privilege install this page is read-only
until an admin grants Manage Server by hand — and the panel now says exactly
that, with the steps, instead of showing a disabled Save. It also says when the
server isn't a Community server, which is a different reason for the same
symptom.

## 6. Two things this deliberately does not do

* **Delete a role.** Provenance makes a delete button *safe* to offer, which is
  not the same as wanting one. "Stop managing" stops the bot pointing at a
  role; the role stays in the server and everybody holding it keeps it. Ask
  before adding it.
* **Provision an authority role.** `mod_role_ids`, `admin_role_ids`,
  `greeter_role_id`, `economy_manager_role_id` and their kin name *who may
  act*. An empty `@Moderator` reads as configured and grants nobody anything —
  the worst failure available, because it looks like success. A future setup
  wizard may *ask* which existing role is the mod role; it must never make one.

## 7. Tests

* `tests/test_role_provision_logic.py` — `choose_role_action`'s table,
  `adoptable_role_ids`' two filters, and that `on_provision` fires on create
  and adopt but never on a plain use.
* `tests/test_feature_roles.py` — the registry guards, now over sixteen roles:
  ping-only, distinct names, the two reopened dials are create-on-offer, which
  dials the bot hands out, and which two ignore a stored "(none)".
* `tests/test_role_roster_service.py` — one `pytest.param` row per state, plus
  the four judgement calls (which "(none)", deleted vs inherited, when
  hierarchy applies, which cards may carry a write button).
* `tests/test_role_provenance.py` — the table's round-trip and that it names no
  member.
* `tests/web/test_bot_roles_routes.py` — the three writes and everything they
  refuse.
* `tests/test_jail_apply.py` / `tests/test_inactive_apply.py` — the inherited-id
  false deletion, written to fail before the fix.
