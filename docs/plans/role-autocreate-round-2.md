# Role autocreate, round 2 — a design drill-down with a front-end lens

**Status:** Design document. **Nothing here is built.** Round 1 (`ac44ec4b`,
2026-08-26) shipped the provisioner and the onboarding panel; stages 3 and 4 of
`docs/plans/role-autocreate.md` were never started, and this document is what
should replace them.

**Billy's ask, verbatim:** he "likes what's there, wants another look at how
roles get added; could drive server setup via Discord's community features / a
duplicable template."

**Method note.** The `frontend-design` skill *was* available and was invoked;
§4 follows its two-pass process (plan, critique against the brief, then build)
and records the critique. Its advice on picking distinctive typefaces and
palettes does **not** apply here and is deliberately not followed: the brief is
an existing design system with those axes already spent
(`docs/dashboard_visual_language.md` — Archivo's width axis is load-bearing,
the palette is Discord's on purpose), and the skill's own rule is that the
brief's words win. The design spend goes entirely on information structure and
interface writing, which is where this feature is actually weak.

Every claim below was verified against `src/` on 2026-09-03. Where round 1's
plan and the code disagree, the code is cited.

---

## 1. Inventory — every role Dungeon Keeper can create today

There are **three** mechanisms, not one, and the "5 of 44" figure describes only
the smallest of them.

### 1a. `ensure_config_role` — five ping dials in the `config` KV

`core/role_provision.py:295`. Reads the dial, decides opted-out-vs-unset, then
delegates. The set is the registry `services/feature_roles.py:146`.

| Role | Dial key | Created when | If it can't be made |
|---|---|---|---|
| `@Welcome Ping` | `welcome_ping_role_id` | a member joins and the welcome post is about to send (`cogs/events_cog.py:1783`) | ping omitted; welcome still posts |
| `@QOTD` | `econ_qotd_ping_role_id` | a question of the day is posted (`cogs/economy_cog.py:4903`) | `content=None`, question posts silent |
| `@Risky Rolls` | `risky_ping_role_id` | a round opens **with ping on** (`cogs/risky_roll_cog.py:250`) | round opens silent |
| `@Promotion Reviewers` | `promotion_review_ping_role_id` | a promotion-review card is posted (`services/promotion_review_views.py:440`) | falls back to the guild's mod roles |
| `@Economy Notifications` | `econ_game_role_id` | **a member presses 🔔** (`economy/guide.py:258`) | member sees `NOTIFY_UNCONFIGURED_MSG` |

Four of the five are *the bot needing a role to mention*. Only the last is a
member asking for one — and it is the only one the bot then **assigns**
(`economy/guide.py:277`), which is why it is the only ping where role hierarchy
matters (§3c).

### 1b. `ensure_feature_role` — nine roles across five features

These never touch the registry; each call site builds its own `RoleSpec` inline.
This is the half that the "5 of 44" figure hides.

| Role(s) | Call site | Storage | Created when | On create |
|---|---|---|---|---|
| `@Jailed` | `jail/apply.py:321` | `config` KV `jailed_role_id` | a mod jails somebody | `_lock_down` denies view+send on **every** channel (`jail/apply.py:306`) |
| `@Inactive` | `inactive/apply.py:203` | `config` KV `inactive_role_id` | a member is marked inactive | deny-view everywhere, allow the inactive channel (`inactive/apply.py:180`) |
| `@DMs Open` / `@DMs Ask First` / `@DMs Closed` | `services/dm_perms_service.py:689` | `dm_mode_roles` table — but **the provisioner stores nothing** (`:700` passes `store=lambda _rid: None`) | a member sets their DM mode | — |
| `@Survivor` / `@Ghost` / `@Sole Survivor` | `web_server/routes/survivor.py:205` | the season's config row | an admin creates a season on the dashboard | `_mark_created` only records it for the panel's report |
| Wellness role (`WELLNESS_ROLE_SPEC`) | `web_server/wellness_routes/admin.py:337` | `wellness_config.role_id` | an admin presses **Activate Wellness** and picks "create one for me" | — |

### 1c. `perk_actions._create_role` — unbounded, per-member, member-named

`economy/perk_actions.py:283`. Personal colour/name roles bought in the shop.
Deliberately **not** on the provisioner, and the module docstring at
`core/role_provision.py:13-20` says why in one line: adopt-by-name plus a
member-chosen name is privilege escalation — a member with the rename perk could
point their personal role at the guild's real `@Moderator`, which
`_reconcile_role` would then rename, recolour and grant them. This one is
correct as it stands and round 2 should not touch it.

### 1d. Correcting "only 5 of 44 dials are safe to auto-create"

The statement is true about **the registry** and misleading about **the bot**.
Precisely:

* **14 fixed-name roles** can be created by the bot, across **12 dials**
  (5 KV ping dials + `jailed_role_id` + `inactive_role_id` + 3 `dm_mode_roles`
  columns + 3 Survivor season keys + `wellness_config.role_id`), plus
* **an unbounded per-member class** from the perk shop.

So the honest sentence is: *"Dungeon Keeper creates up to fourteen named roles
for itself, and one more for every member who buys a personal role. Five of
those fourteen go through the audited registry; nine were grandfathered in when
the provisioner replaced their hand-rolled copies."* The nine are auditable only
by grepping `ensure_feature_role`. **That is the first thing round 2 should
fix**, and it is the reason a roster page has content nothing else can show
(§4).

Round 1's audit total of 44 dials (`docs/plans/role-autocreate.md` §Stage 0)
still holds and is not re-derived here.

---

## 2. The four reasons a dial is excluded

Round 1's classification (A/B/C1–C4) mixed four genuinely different objections
under one heading. Naming them apart is the spine of this document, because
**two of them are permanent and two of them are removable** — and round 1
treated all four as if they were permanent.

### R1 — Ownership. *The role is the guild's, not the bot's.* (permanent)

Creating one makes a twin, and the feature then silently stops matching the real
role. Adopting one by name is worse: it hands the bot a role somebody else's
policy depends on.

Named examples: `pen_pals_config.opt_in_role_id` — this is **Denizen**, the
guild's main membership role (memory `pen-pals-opt-in-role-is-denizen`);
`grant_roles.role_id` / `required_role_id`; `xp_level_5_role_id`;
`promotion_review_grant_role_id`; every `role_menus` option role;
`intake_cards.auto_role_id` and `intake_card_steps.auto_role_id`, which are
*watchers* — a step ticks when a member **gains** a role that already exists, so
a bot-made role is a step that can never tick; and the perk shop's per-member
roles (§1c).

**Permanent.** No amount of UI dissolves it. The only round-2 move is to *say
so* on the dial — the current panels say nothing, so an admin cannot tell an
R1 dial from a provisioned one.

### R2 — Authority. *The dial names who may act, so an empty role is a silent no-op.* (permanent)

This is the safety one. `admin_role_ids` and `mod_role_ids` are the permission
boundary and are hard-blocked from model writes in `settings_registry`;
`greeter_role_id`, `qa_role_id`, `manager_role_id`, `economy_manager_role_id`,
`games_editor_role.role_id`, `whisper_role_id`, `bypass_role_ids`,
`no_contact_settings.alert_role_id` and Pen Pals' `staff_role_ids` are the same
shape. Creating `@Moderator` and storing it would produce a config that *reads*
configured and grants nobody anything — the worst failure available, because it
looks like success.

**Permanent**, and it should stay permanent even if someone later wants a
setup wizard: a wizard may *ask* which existing role is the mod role; it must
never make one.

### R3 — Storage. *The store cannot express "never configured".* (removable)

This is not a safety property at all — it is a schema accident, and it is the
one reason round 1 lost dials to. The whole safety of provisioning a ping role
rests on `role_dial_opted_out` (`core/role_provision.py:263`) distinguishing
*no row* from *a row holding `"0"`*. Two dials cannot:

* `bump_tracker_config.role_id` — `NOT NULL DEFAULT 0`, so both states store 0.
* `revive_guild_config.role_id` — nullable, but the panel's "(none)" is
  `value=""`, saved as NULL. Both states store NULL.

Per-instance dials (a scheduled game's announce role, photo challenge's ping, a
Chat Revive per-channel override) are a third variant: their "unset" was chosen
by an admin filling a form that offered "(none)", so there is *no* unconfigured
state to detect.

**Removable**, two ways. Either migrate the column to nullable-with-a-real-null
meaning "never set" (a migration touching live rows, so it needs the
`config-ia-migrations-pending-prod` care), or — cheaper and more general — record
provenance in a side table rather than inferring it from the dial's value
(direction (d), §5).

### R4 — Emptiness. *An empty role is worse than no role.* (conditionally removable)

Not about ownership or authority: these are roles the bot legitimately owns, but
creating one **flips the feature from honestly-off to configured-and-refusing**.

* `guess_role_id` — unset means "Guess isn't set up" and the game says so.
  Provisioning turns that into a configured game refusing every member with
  "you need the Guess role", because nobody holds the new one.
* `voice_master_spectator_gate_role_id` — ungated spectate makes `@everyone` the
  audience; a gate role *denies* `@everyone` Connect and hands the room to the
  role. An empty gate role is a spectate room nobody can enter. This was the one
  Class A conversion stage 2 was scoped to deliver and it dropped it, which is
  why round 1's Class A shipped empty.

Both are pinned as hazards in `tests/test_feature_roles.py:33-54`.

**Conditionally removable — and this is the interesting one.** R4 is only fatal
because *creation and membership are separate problems*. Pair creation with a
way for members to actually take the role and the objection evaporates. Round 1
half-built exactly that pairing (the onboarding panel) and then never went back
to reconsider the two dials it had excluded on R4 grounds. The condition to
state plainly: **an R4 dial may be provisioned only in the same action that puts
it in front of members** — never lazily on first use.

### Summary

| | Reason | Fatal because | Round 2 |
|---|---|---|---|
| R1 | Ownership | a twin silently breaks matching | permanent — surface it |
| R2 | Authority | an empty role grants nobody anything, but reads configured | permanent — never dissolve |
| R3 | Storage | can't tell "unset" from "(none)" | removable — provenance table or migration |
| R4 | Emptiness | flips off→broken | removable **only** if creation ships with an opt-in surface |

---

## 3. Failure modes that already exist

These are the content of the front end. Every one was verified in code.

### 3a. A stored role-dial `0` is usually a save artifact — confirmed

Confirmed from the panels, not inferred. All three save the **whole form** and
write `"0"` for any untouched picker:

* `static/js/panels/config-welcome.js:282` — `pickers.welcome_ping_role_id.getValue() || "0"`
* `static/js/panels/economy-config.js:521` — `game_role_id: gameRolePicker.getValue() || "0"`
* `static/js/panels/config-risky-rolls.js:112` — `ping_role_id: rolePicker.getValue() || "0"`

So changing a payout on Economy Settings writes `econ_game_role_id = 0`, and one
save of any of those pages locks that dial out of provisioning forever under the
`none_means_off` rule. Round 1 recorded the same finding as an amendment and
handled exactly one dial (`econ_game_role_id`, `feature_roles.py:140`).

**What round 2 owes here is not more exemptions — it is making the difference
visible.** An admin looking at a "(none)" picker cannot tell whether they chose
it. A panel that renders "(none)" identically for *never touched*, *deliberately
off* and *cleared by an unrelated save* is the actual defect.

### 3b. A preference that isn't enforced — `econ_game_role_id`

`economy-config.js:118-127` tells the admin: *"Leave unset to reply in-channel
for everyone and send no recurring DMs."*

That is false. `economy/guide.py:262` passes
`respect_opt_out=ECONOMY_NOTIFY.none_means_off`, which is `False`
(`feature_roles.py:140`), so a member pressing 🔔 provisions
`@Economy Notifications` **regardless of the admin having picked "(none)"**.
The decision behind the code is sound and documented; the panel copy was never
updated to match.

This is a direct violation of CLAUDE.md's *"never ship a preference or toggle
that isn't enforced"*. It is the single clearest thing round 2 must fix, and the
fix is a front-end fix (§4d), not a behaviour change.

### 3c. Hierarchy: adopt has no position check, and jail can strip before it fails

`ensure_feature_role` builds its adoption candidates as
`named = [r.id for r in guild.roles if r.name == spec.name]`
(`core/role_provision.py:201`) — **no `managed` filter, no position check**. A
guild that already has a role called `Jailed` sitting above Dungeon Keeper's own
role gets it adopted, stored, and then every jail fails at `add_roles`.

The codebase already knows how to answer this question in two places and neither
is wired to the provisioner: `core/role_safety.py:66` (`role >= bot_member.top_role`
→ *"is above my highest role — I can't grant it"*) and
`web_server/routes/role_menus.py:142`, whose `/api/role-menus/roles` returns a
per-role `assignable` flag. `GET /api/meta/roles` (`routes/meta.py:227`) already
returns `position` and `managed` but **not** the bot's own top-role position, so
a panel cannot currently compute reach from it.

Worse, jail's recovery is partial. `jail/apply.py:356-357` wraps
`remove_roles` and `add_roles` in **one** try; a Forbidden on the second leaves
the member stripped of every role with no jail row written (the row is written
at step 3, after). The error message is excellent — it prints all three
positions — but the state it leaves behind is not.

**Note the distinction the UI must get right:** mentioning a role needs no
hierarchy at all. Four of the five ping roles are never assigned by the bot, so
a position warning on them would be crying wolf. Reach matters for exactly nine
of the fourteen: the DM trio, Jailed, Inactive, Survivor's three, Wellness — and
`@Economy Notifications`, the one ping the bot hands out. Ten of fourteen.

### 3d. A role deleted out from under a dial — and a false accusation in guild B

The recreate path is right in principle (`role_provision.py:132` splits
`create` from `recreate`; `mod_log_announcer` at `:353` posts to the mod channel
**and** writes a durable audit row, because `log.txt` is wiped every boot).

But `stored_is_own` — the fix that stops an inherited `guild_id=0` id being read
as a deletion — **only exists on the `ensure_config_role` path**
(`role_provision.py:326-340`). `ensure_feature_role` defaults it to `True`
(`:167`), and Jail and Inactive read their dials with the legacy fallback **on**:
`jail/apply.py:298` and `inactive/apply.py:165` both call
`get_config_value(conn, key, "0", guild_id)`, whose `allow_legacy_fallback`
defaults to `True` (`core/db_utils.py:93`).

So in a second guild with no row of its own, `jailed_role_id` resolves to the
home guild's role id, which can never resolve there, no `@Jailed` exists by
name — and the first jail posts
*"⚠️ **Jailed** was deleted, so I made a new one for the jail. Anyone who held
the old role no longer has it."* to that guild's mod channel. Nobody deleted
anything. This is the exact bug round 1 fixed on the other path and left standing
on this one.

(It fires only when no exact-name match exists, since adopt runs first. Cheap to
fix: thread `stored_is_own` through, or pass `allow_legacy_fallback=False` at
both call sites.)

### 3e. The second guild's divergent config

Round 1 recorded three live observations against prod: `econ_game_role_id` is
`0` in two guilds; `welcome_ping_role_id` is `0` in a third; and
`welcome_ping_role_id` is set at `guild_id=0` with three guilds having no row of
their own. Memory `second-guild-economy` records that guild `1476…484` is a live
economy run by somebody else. **This document did not re-query prod** (out of
scope), so treat those three as needing re-verification before any migration.

The design consequence is structural, not numeric: **the roster page must be
guild-scoped and must say when a value is inherited.** A dial showing `@QOTD` in
guild B because guild A's row leaked through the legacy fallback is a lie the
current UI has no way to tell.

### 3f. Onboarding: `enabled` and `in_onboarding` are read and thrown away

`services/onboarding_service.py:230` (`read_prompts`) copies only
`onboarding.prompts`. discord.py's `Onboarding` also carries `enabled` and
`mode` (`discord/onboarding.py:332-335`), and `PromptView` *does* carry
`in_onboarding` per prompt — which `panels/onboarding.js:40` then never renders.

Two ways the panel can truthfully report success while no member ever sees the
result: onboarding is switched off server-wide, or the chosen prompt is
customize-only. Neither is surfaced.

### 3g. Two small ones, both verified

* `panels/onboarding.js:16` uses the CSS class `badge-muted`. It does not exist
  — `app.css:2267-2271` defines `badge-danger/success/info/warning/dim`. The
  "Turned off" pill renders unstyled.
* `/invite` (`cogs/invite_cog.py:28`) does **not** request `manage_guild`.
  `guild.edit_onboarding` requires Manage Server **and** Manage Roles
  (`discord/guild.py:4926-4927`), which is why `routes/onboarding.py:154`
  computes `can_edit` from both. So on a fresh, documented, least-privilege
  install the Discord Onboarding page loads read-only with its save disabled,
  and neither the page nor `DEPLOYMENT.md:86-91` tells the admin how to fix it.
  (The *read* path is fine: `economy_loop.py:1894` calls `guild.onboarding()`
  hourly in prod for the `role_pick` quest.)

---

## 4. The front end

### 4a. Design plan, and the pass that revised it

**What the admin is actually doing here.** Not configuring. Auditing. The job is
*"what has this bot done to my role list, is any of it broken, and what do I do
about it"* — closer to reading a receipt than filling a form. Every other page
under Config is a form; this one should not be.

**Fixed by the brief** (`dashboard_visual_language.md`): palette is Discord's,
type is Archivo (variable width, load-bearing in the nav rail) + Public Sans,
the `--t-*` / `--s-*` scales, existing `.panel` / `.card` / `.section-label` /
`.field-hint` / `.badge` vocabulary. Nothing new is invented.

**Free, and therefore where the design lives:** what the page opens with, how
state is grouped, and every sentence on it.

*First draft, then the critique the skill asks for.* My first plan was: a stat
row across the top (**14 managed** / **12 healthy** / **1 missing**), then one
table with a status column, then a settings card. Reviewed against the brief,
three parts of that are the generic default:

1. **The stat row.** Big-number tiles are the skill's named default treatment
   and they are wrong here on the merits too — the counts are 14 and 1, numbers
   a person reads faster in a sentence than in a tile. **Replaced with one line
   of prose** carrying the same three facts.
2. **One flat table with a Status column.** It reproduces round 1's four-state
   badge table (`panels/onboarding.js:12-17`) at greater width, and a
   three-column table of prose is exactly what the mobile gate punishes.
   **Replaced with a card list in two named groups**, where the group heading
   carries the information the status column can't (§3c: which roles the bot
   *hands out*, and therefore which ones hierarchy applies to).
3. **A badge alone as the state.** A word in a pill tells an admin the state and
   not the consequence. **Every card carries a sentence saying what happens
   next**, and the badge is a scanning aid, not the message.

What survives from the first draft: cards, the existing badge palette, and the
"one card per thing, actions in the card" shape the rest of the dashboard uses.

**The one bold element**, per the skill's spend-it-in-one-place rule: the
opening sentence. It is the only thing on the page set at display size, and it
says something the admin has never been told:

> **Dungeon Keeper has made 6 roles in this server.** Five are in use; `@QOTD`
> was deleted and will be remade the next time a question is posted.

Everything else is quiet.

### 4b. Where it goes

Three candidate homes, and the answer is two of them, not one.

| Option | What it gives | What's wrong with it alone |
|---|---|---|
| **A. New page**, Config → Roles, new id `bot-roles`, label "Bot-Managed Roles" | the only surface that can show all **14** roles together — the nine `ensure_feature_role` ones live on five different pages and one of them (the DM trio) has no page at all | a page an admin visits twice a year; doesn't fix the lie on the dials |
| **B. A section on an existing page** | cheapest | `config-roles` is **Role Grants** (`/grant` allowlists, `panels/config-roles.js`) — a different feature that happens to share a word. Hanging bot-managed roles off it is a naming coincidence, not an IA decision. `onboarding` is a better host but its job is narrower (getting roles to *members*) and it can only see the five |
| **C. Per-dial affordances in place** | fixes the actual lie, where the admin actually looks | can't answer "what has this bot made", and can't reach the DM trio or Survivor |

**Recommendation: C first, then A.** C is mandatory — §3b is a live house-rule
violation and it lives on `economy-config.js`, not on any new page. A is the
addition worth building; B is rejected on IA grounds (ids are frozen, but
*meaning* should be too — `config-roles` means role grants).

`onboarding` then loses its status column and keeps its real job, linking to
`bot-roles` for state. That removes the duplicated four-state vocabulary rather
than growing a second copy of it.

New id `bot-roles` is free — the freeze is on renaming existing ids, not on
minting new ones (`docs/dashboard_ia.md` §Naming). Filed under **Config →
Roles**, third after Role Grants and Reaction Roles.

### 4c. The roster page — states, sentences, actions

Seven states, up from round 1's four. Each is a distinct thing an admin can act
on, and each has one sentence and one obvious next move.

| State | Badge | Sentence on the card | Actions |
|---|---|---|---|
| **In use** | `badge-success` "In use" | "12 members have it." / "Nobody has it yet." | Offer in onboarding · Use a different role · Stop using it |
| **Not made yet** | `badge-dim` "Not made yet" | "I'll make it the first time somebody joins." (per-dial trigger from §1) | Make it now · Use a different role · Never make it |
| **Turned off** | `badge-dim` "Off" | "Set to (none) on Welcome & Leave. I won't make one." | Turn it back on |
| **Deleted** | `badge-danger` "Deleted" | "Someone deleted it. I'll make a replacement the next time a question is posted, and it will start empty." | Make it now · Use a different role |
| **Renamed** | `badge-info` "Renamed" | "It's called @Announcements now. That's fine — I go by id, not name." | — informational |
| **Out of reach** | `badge-warning` "Out of reach" | "@Jailed sits above my own role, so I can't add or remove it. Move Dungeon Keeper above it in Server Settings → Roles." | — the fix is in Discord |
| **Two of them** | `badge-warning` "Two of these" | "There are two roles called @QOTD. I'm using the lower one." | Use a different role |

Notes that make these correct rather than plausible:

* **Out of reach** is computed only for the ten roles the bot assigns (§3c). On
  the four mention-only pings it is not evaluated and not shown.
* **Deleted** is only claimed when the stored id is **this guild's own row**
  — the `stored_is_own` distinction from §3d. An inherited id renders as
  *"Inherited from another server's settings — I'll make one here"*, never as a
  deletion.
* **Turned off** never appears for `econ_game_role_id` while
  `none_means_off=False`, because it would be a state the bot doesn't honour.

### 4d. Layout

Desktop, roughly 900px of content column:

```
┌──────────────────────────────────────────────────────────────────────┐
│  Bot-Managed Roles                                                   │
│  Roles Dungeon Keeper makes for itself, and how each one is doing    │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   Dungeon Keeper has made 6 roles in this server. Five are in        │  ← display size,
│   use; @QOTD was deleted and will be remade the next time a          │    the one bold
│   question is posted.                                                │    element
│                                                                      │
│   ── Roles I only mention ──────────────────────────────────────     │  ← section-label
│   Holding one of these grants nothing. I mention them; I never       │
│   hand them out, so where they sit in your role list doesn't         │
│   matter.                                                            │
│                                                                      │
│   ┌────────────────────────────────────────────────────────────┐     │
│   │ 👋  @Welcome Ping                             [ IN USE ]   │     │
│   │ 12 members have it.                                        │     │
│   │ Set on Welcome & Leave → Welcome Ping Role                 │     │
│   │ ┌ Offer in onboarding ┐ ┌ Use a different role ┐ ┌ Stop ┐  │     │
│   └────────────────────────────────────────────────────────────┘     │
│   ┌────────────────────────────────────────────────────────────┐     │
│   │ 💬  @QOTD                                    [ DELETED ]   │     │
│   │ Someone deleted it. I'll make a replacement the next time   │     │
│   │ a question is posted, and it will start empty.              │     │
│   │ Set on Economy → QOTD                                       │     │
│   │ ┌ Make it now ┐ ┌ Use a different role ┐                    │     │
│   └────────────────────────────────────────────────────────────┘     │
│                                                                      │
│   ── Roles I hand out ──────────────────────────────────────────     │
│   I add and remove these myself, so each one has to sit below        │
│   Dungeon Keeper in Server Settings → Roles.                         │
│                                                                      │
│   ┌────────────────────────────────────────────────────────────┐     │
│   │ 🔒  @Jailed                             [ OUT OF REACH ]   │     │
│   │ @Jailed sits above my own role, so I can't add or remove    │     │
│   │ it. Move Dungeon Keeper above it in Server Settings →       │     │
│   │ Roles.                                                      │     │
│   │ Set on Moderation → Jails                                   │     │
│   └────────────────────────────────────────────────────────────┘     │
│   … @Inactive, @DMs Open/Ask First/Closed, Survivor's three,         │
│     the Wellness role                                                │
└──────────────────────────────────────────────────────────────────────┘
```

Phone (≤420px). The card is already a stacked block; the only thing that has to
give is the action row, which **wraps** rather than scrolling — `display:flex;
flex-wrap:wrap; gap:var(--s-2)` and no fixed widths anywhere, per CLAUDE.md's
"prefer wrapping/scrolling flex rows over fixed-width ones". The badge moves
under the role name rather than sitting right-aligned beside it:

```
┌────────────────────────┐
│ 💬 @QOTD               │
│ [ DELETED ]            │
│ Someone deleted it.    │
│ I'll make a            │
│ replacement the next   │
│ time a question is     │
│ posted, and it will    │
│ start empty.           │
│ Economy → QOTD         │
│ ┌ Make it now ┐        │
│ ┌ Use a different    ┐ │
│ │ role               │ │
└────────────────────────┘
```

No table anywhere on the page, so nothing needs an `overflow-x:auto` wrapper and
the browser layout gate has nothing to catch. `scripts/mobile_layout_scan.py`
should be run against it anyway, with an interaction scenario for the expanded
"Use a different role" picker — that is layout behind a click, which the gate
misses by default (`docs/mobile_layout_testing.md`).

### 4e. Empty / first-run state

On a fresh server every one of the fourteen is *Not made yet*, and a wall of
fourteen identical "not made yet" cards is a to-do list nobody asked for. The
first run is a different screen:

```
┌──────────────────────────────────────────────────────────────────────┐
│  Bot-Managed Roles                                                   │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   Dungeon Keeper hasn't made any roles here yet.                     │
│                                                                      │
│   Most of them appear on their own the first time they're needed —   │
│   @Jailed when you jail somebody, @Inactive when you mark somebody    │
│   inactive. The five notification roles are the exception: they're    │
│   only useful once members can pick them up.                         │
│                                                                      │
│   ┌ Set up the five notification roles ┐                             │
│                                                                      │
│   Makes @Welcome Ping, @QOTD, @Risky Rolls, @Promotion Reviewers      │
│   and @Economy Notifications, then adds them to Discord's Channels    │
│   & Roles screen so members can choose them. You'll see exactly       │
│   what changes before anything is saved.                             │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

That single button is worth more than the rest of the page: it is the only place
in the product where **creation and opt-in happen in one action**, which is the
precondition §2/R4 named for ever provisioning a gate role. It reuses
`POST /api/onboarding/add-roles` unchanged — that endpoint already provisions
missing roles before planning (`routes/onboarding.py:199`) and already refuses
on a stale prompt id (`:185`).

If `can_edit` is false (no Manage Server — the default install, §3g), the button
still creates the roles but says so up front rather than failing at the end:
*"I can make the roles, but I need Manage Server to add them to Channels &
Roles. [How to fix]"*.

### 4f. Per-dial affordances — the part that is not optional

Under each of the five ping pickers, one line in `.field-hint` register showing
the same state and one link:

```
  Welcome Ping Role
  ┌ (none)                                    ▾ ┐
  Mentioned above the welcome embed so its holders get notified
  of every new arrival.
  ⤷ Not made yet — I'll create @Welcome Ping the first time
    somebody joins.  Make it now
```

and, when the admin has deliberately switched it off:

```
  ⤷ Off — you picked (none), so I won't make one.
```

That single line dissolves §3a: *never touched*, *deliberately off* and *cleared
by an unrelated save* stop rendering identically.

**And the Economy Notifications dial gets a different fix.** Its "(none)"
option is a preference the code does not honour (§3b), so the option should not
be offered. Replace it with the **Wellness Activate precedent**, which already
exists in the tree (`panels/wellness-admin.js:71`):

```html
<option value="auto" selected>✨ Create a "…" role for me</option>
```

so the picker reads:

```
  Notifications Role
  ┌ ✨ Make one for me                         ▾ ┐
  The opt-in role members toggle with the 🔔 button. There's no
  way to switch this off — the role IS the opt-in, so the button
  makes one if there isn't one. Point it at a role of your own
  if you'd rather.
```

Code unchanged, behaviour unchanged, the UI stops lying. `mountRolePicker`
already accepts `opts` overriding `emptyLabel`
(`config-helpers.js:699-702`), so this is a copy change plus one option.

### 4g. Actions, and the two-writers hazard

Three of the four actions write a dial that a **whole-form panel elsewhere also
writes** (§3a). If the roster page repoints `welcome_ping_role_id` and the admin
then saves a Welcome & Leave page they loaded ten minutes ago, the stale picker
value wins and the repoint is silently undone. This is the same failure class as
the onboarding replace-the-world problem round 1 handled carefully, at smaller
scale.

Two ways out. **Either** the roster's write actions go through the owning
feature's existing PUT (so there is one writer per key and the roster is a
remote control, not a second author), **or** the roster only ever *links* to the
owning page and its own buttons are limited to **Make it now** — which writes a
key that was empty, and therefore cannot clobber a considered value.

**Recommended: the second, for v1.** "Make it now" and "Offer in onboarding" on
the roster; "Use a different role", "Stop using it" and "Turn it back on" are
links to the owning page's picker, which is where the field hint from §4f now
tells the truth anyway. It is less clever and it cannot lose an admin's edit.

### 4h. API shape

One new read endpoint, `GET /api/bot-roles`, admin-gated, returning per role:

```
{ key, name, emoji, feature, group: "mentioned" | "handed_out",
  state: in_use|not_made|turned_off|deleted|renamed|out_of_reach|twin
         |inherited,
  role_id: "…" (string — snowflake), current_name, member_count,
  assignable: bool|null,      # null for mention-only roles
  owner_panel: { id, label },  # deep link, frozen route id
  can_create: bool }
```

plus a small envelope: `can_manage_roles`, `bot_top_role_position`,
`onboarding_enabled`.

Everything it needs already exists — `guild.get_role`, `role.position`,
`role.managed`, `len(role.members)`, `role_dial_opted_out`
(`role_provision.py:263`) and `choose_role_action` (`:89`) — **except** the nine
`ensure_feature_role` roles, which are not enumerable today because each call
site builds its `RoleSpec` inline (§1b). The endpoint needs a second registry
beside `CONFIG_ROLES` naming them, and each call site then reads its spec from
it. That refactor is small, is the thing that makes the nine auditable at all,
and pays for itself immediately in `tests/test_feature_roles.py`, whose existing
guard tests would then cover fourteen roles instead of five.

Ids go out as **strings** (snowflake-precision sweep, `docs/web_testing.md`).
The panel mounts through `mountAsync` and lets the loader's rejection reach it
(CLAUDE.md), and any per-guild cache it adds must be cleared in
`resetMetaCaches()` or `config-helpers.js`'s test hard-fails.

---

## 5. The directions round 2 could take

Four, deliberately not ranked into a single winner.

### (a) Finish stages 3 + 4 as scoped — visibility over the existing provisioner

**What it does for a fresh server:** nothing on its own. It makes an existing
server's role situation legible, and it fixes §3b.

**Contents:** §4f (per-dial state lines + the Economy picker fix), §4c–§4e (the
`bot-roles` roster), the second registry from §4h, and
`docs/role_provisioning_spec.md` as Reference with an INDEX row. The manual's
half of stage 4 is **already done** — `manual.html:1849-1851` describes all five
roles, the "(none)" rule, the `@Economy Notifications` exception and the
onboarding page, accurately.

**Rough cost:** the per-dial half is a day (five copy changes, one picker
option, one small state fetch). The roster page plus its registry refactor and
endpoint is two to three days including tests. Call it **3–4 days**.

**What could go wrong:** a page nobody opens twice. Mitigated by §4f — the
per-dial lines are where the value lands, and they are the cheap half.

**What it forecloses:** nothing. Every other direction wants this roster to show
its work.

### (b) A guided first-run server-setup flow

**What it does for a fresh server:** a lot, in principle — walks an admin
through the dials a new install needs, creating what it may and asking about
what it may not.

**What it really costs.** Roles are the small half. A working DK also needs a
mod channel, a jail channel, an inactive channel, a welcome channel, a bank
channel, an approvals channel, an economy manager role, mod roles… and R2 says
the bot must never *create* the authority ones. So the flow is mostly a long
form that asks questions, which is what the Config section already is, arranged
by feature instead of by setup order. **Rough cost: 2–3 weeks**, and most of it
is a second IA over the same dials.

**What could go wrong:** a wizard that runs once and then rots while the pages
it duplicates move on — the dashboard has already been through two IA
re-orderings this year (`dashboard_ia.md` IA2/IA3). And it invites exactly the
R2 mistake: a setup flow that "helpfully" makes a `@Moderator` role.

**What it forecloses:** nothing technically, but it commits DK to owning server
setup, which raises the bar on every future feature.

**A cheaper 80%** worth naming: keep the Config section as it is and add a
single **Setup Checklist** panel that *reads* the dials and lists what is unset,
linking to each owning page — no new writer, no second IA, and the roster page
from (a) is its first section. That is closer to a week.

### (c) Lean on Discord's community features / a duplicable template

Assessed in full in §6. Short version: **the duplicable-template half is not
buildable**, the welcome-screen half is adjacent value only, and the one
community surface that genuinely matters for roles — onboarding — is the one
round 1 already built.

**Cost of the buildable residue** (welcome screen editor, `guild.features`
awareness, an onboarding *enabled* check, and `/invite` asking for Manage
Server): **2–3 days**, most of it the permission change and its migration story
for already-installed guilds.

### (d) Provenance — record what the bot made, so it can be undone

Not in the original brief, and it is the direction that makes (a) honest.

**Today the bot cannot tell you which roles it created.** State is inferred from
"the stored id resolves" and "a role of this name exists", which is why §3d can
accuse an admin of a deletion that never happened and why §3a can't tell a
decision from a save artifact.

**What it is:** one small table — guild, dial key, role id, created-or-adopted,
timestamp, actor — written on every create/adopt in
`ensure_feature_role`.

**What it unlocks, concretely:**
* the roster's states become *facts*, not inferences;
* **R3 dissolves** (§2) — provenance answers "was this dial ever configured"
  for `bump_tracker_config.role_id` and `revive_guild_config.role_id`, whose own
  columns cannot, with no migration of live rows;
* "Stop using it" can offer to *delete the role the bot made*, which it must
  never offer for one the guild made;
* an uninstall can clean up after itself.

**Rough cost: 2 days**, plus obligations. **The table holds no per-user data**
(guild id, role id, key, timestamp, and the acting admin's id if we record it) —
if the actor id is recorded it is per-user and needs a `docs/data_register.md`
row with an explicit purge/preserve decision in the same commit. The honest
answer there is *preserve* on Art 17(3)(b)/(e) grounds (it is a record of a
configuration action, the same footing as `write_audit`), and the cheaper answer
is **don't store the actor** — the audit row already does, and then the table
holds no personal data at all. Recommend the latter.

**What could go wrong:** a third source of truth. It must be written by the
provisioner and nothing else, and the roster must degrade to today's inference
when a row is missing (every role created before this ships).

### Sequencing

**Do (a) first**, and inside it do §4f before §4c. Reasons, in order:

1. §3b is a live violation of a house rule — a preference the code does not
   enforce, on a page an admin reads today. It is a one-day fix.
2. Every other direction needs somewhere to show its work, and (a) builds it.
3. (d) is the natural second, because it is what turns (a)'s inferences into
   facts and is the only thing that reopens R3.
4. (b) and (c) are both large and both partly redundant with what exists. Neither
   should start before the roster proves whether anyone actually looks at this.

---

## 6. A hard look at option (c)

Verified against `discord.py 2.7.1` (`requirements.lock:59`) as installed in
this checkout, not against the docs website.

### What the API actually permits

| Surface | discord.py | Permission | Verdict |
|---|---|---|---|
| **Onboarding** read | `Guild.onboarding()` — `discord/guild.py:4897` | none documented | ✅ used hourly in prod (`economy_loop.py:1894`) |
| **Onboarding** write | `Guild.edit_onboarding()` — `guild.py:4912` | Manage Server **+** Manage Roles (`guild.py:4926-4927`) | ✅ built in round 1 — but see §3g, `/invite` never asks for Manage Server |
| **Welcome screen** | `Guild.welcome_screen()` / `edit_welcome_screen()` — `guild.py:3903`, `:3929` | Manage Server, **and `COMMUNITY` in `guild.features`** (`guild.py:3907`, `:3941`) | ⚠️ possible, but it edits channel descriptions — nothing to do with roles |
| **Guild templates** — list/create/sync/edit/delete on *this* guild | `Guild.templates()` `:2816`, `create_template()` `:2941`; `Template.sync/edit/delete` | Manage Server | ⚠️ possible, and nearly useless (below) |
| **Apply a template to an existing guild** | **does not exist** | — | ❌ |
| **Create a guild from a template** | `Template.create_guild()` — `discord/template.py:167` | — | ❌ `@deprecated()` since discord.py 2.6, and *"Bot accounts in more than 10 guilds are not allowed to create guilds"* (`template.py:173`) |
| **Verification level** | `Guild.edit(verification_level=…)` | Manage Server | ⚠️ possible, one dial, easy to lock members out with |

The template route list is exhaustive and settles it —
`discord/http.py:1531-1561` has six routes: `GET /guilds/templates/{code}`,
`GET|POST /guilds/{id}/templates`, `PUT|PATCH|DELETE /guilds/{id}/templates/{code}`,
and `POST /guilds/templates/{code}` (create a **new** guild). **There is no
endpoint that applies a template to a server that already exists.** A template
is a guild constructor, not a configurator.

### Why a DK template wouldn't carry DK

Even granting a bot that could create guilds:

1. A template snapshots **roles, channels and a handful of guild settings**. It
   does not carry members, messages, emoji, webhooks — or **bots**. The new
   server would have the role skeleton and no Dungeon Keeper in it.
2. Every id in the new server is new. DK's configuration is *ids*: ~44 role
   dials and dozens of channel dials in `config`, `wellness_config`,
   `revive_guild_config`, `bump_tracker_config` and the rest. A template
   reproduces the *shapes* and none of the *pointers*, so after applying one,
   every dial still has to be pointed by hand. **The work the template was
   supposed to save is exactly the work that remains.**
3. Discord already lets a human make a server template from the UI in two
   clicks, and it works. The bot adds nothing to that half.

### What is real in the neighbourhood

The valuable thing in the vicinity of "duplicable template" is **not** a Discord
template. It is a **DK config export/import** — take guild A's settings, and on
guild B match them up against B's channels and roles by name, showing the admin
the mapping before writing. That is a real feature, it is genuinely useful for
a second server, and it should be named for what it is rather than shipped as
"server templates", which would promise Discord's thing and deliver something
else. It is also strictly bigger than round 2 (44 role dials × the same problem
for channels), and the roster page from (a) is a prerequisite: you cannot map
what you cannot enumerate.

### The buildable residue of (c)

Small and worth doing, mostly as part of (a):

* **`/invite` should request Manage Server** (`cogs/invite_cog.py:28`) — or the
  onboarding panel should tell an admin how to grant it. Today the bot ships an
  invite link that guarantees its own onboarding page loads read-only (§3g).
  Note the migration cost: an already-installed bot does not gain a permission
  when the invite link changes; the admin must re-authorize or grant the bit by
  hand, so the panel needs the "how to fix" copy either way.
* **Surface `onboarding.enabled`** and per-prompt `in_onboarding` (§3f), so the
  panel stops reporting success into a screen no member sees.
* **Read `guild.features`** for `COMMUNITY` and say plainly when onboarding is
  unavailable because the server isn't a Community server.

### Verdict on (c)

**The template half is not feasible and should not be designed against.** The
community-features half is feasible, small, and already half-done — round 1 built
the only part of it that matters for roles. Recommend folding the three residue
items into (a) and telling Billy the template idea is a config export/import
under another name.

---

## 7. Obligations any of this incurs

* **Tests in the same commit**, logic/service layer. `choose_role_action` is
  already pure and well covered (`tests/test_role_provision_logic.py`, 417
  lines); the new work needs (i) a pure `role_state()` function computing the
  seven states of §4c from `(stored_id, stored_is_own, live_role, bot_top_pos,
  named_matches, opted_out)`, tested table-driven with one `pytest.param` row per
  state, and (ii) extensions to `tests/test_feature_roles.py` once the nine
  `ensure_feature_role` roles join a registry — its existing guard tests
  (ping-only, never mentionable, distinct names, hazards stay out) then cover
  fourteen roles rather than five, which is most of the value of the refactor.
  A new `*_logic.py` / `*_service.py` file with no mapped test is a **hard
  failure** in the scoped gate.
* **`docs/role_provisioning_spec.md`** (Reference) + an `INDEX.md` row — stage 4,
  still owed. It does not exist (checked).
* **`manual.html`** — §4f changes a field hint and §4c adds a page, so both need
  a manual line in the same commit. The existing paragraphs at
  `manual.html:1849-1851` are accurate and should be extended, not rewritten.
* **`docs/data_register.md`** — only if direction (d) stores the acting admin's
  id. Recommended shape stores no personal data, so no row is owed; if that
  changes, the row and the purge/preserve decision land in the same commit.
* **No-contact:** nothing here puts two members in contact, so
  `is_no_contact_conn` is not implicated.
* **NSFW:** not implicated.
* **Browser gate:** `bot-roles` is a new panel — `scripts/mobile_layout_scan.py`
  plus an interaction scenario for the expanded picker (§4d).

---

## 8. Open questions for Billy

1. **Does `@Economy Notifications` keep its "no off switch"?** §3b is a
   preference the code doesn't enforce. Two fixes: change the copy and remove
   "(none)" from that one picker (recommended, one day, no behaviour change), or
   honour "(none)" and let the 🔔 button say notifications aren't set up here.
   The second reverses your 2026-08-22 call, so it's yours to take.
2. **Is `econ_game_role_id = 0` in the two other guilds a decision or a save
   artifact?** Round 1 left this open as its question 2 and it is still open.
   Clearing those rows re-enables the 🔔 button there. Needs a prod read this
   document deliberately didn't take.
3. **A roster page, or only the per-dial lines?** §4f is the fix; §4c is the
   audit surface. §4c is three of the four days. If you'd rather not have a
   fifteenth Config page, say so and (a) shrinks to a day.
4. **Should "Stop using it" be able to delete a role the bot made?** Only
   direction (d) makes that safe to offer, because only provenance can tell a
   bot-made role from an adopted one. Without (d) the action can only mean
   "stop pointing at it".
5. **Do we reopen the two R4 dials** — `guess_role_id` and Voice Control's
   spectate gate — now that onboarding exists? §2/R4 says they become safe if
   and only if creation ships in the same action that offers them to members.
   The spectate gate is the one that makes a real difference (an ungated
   spectate room is `@everyone`).
6. **Should `/invite` ask for Manage Server?** It is the difference between the
   Discord Onboarding page working and not working on a fresh install (§3g). The
   cost is a broader permission on every future install, and it does nothing for
   the servers already running.
7. **Is a DK config export/import worth scoping separately?** §6 concludes that
   is what "duplicable template" actually means once Discord's own template API
   is ruled out. It is a bigger piece of work than all of round 2 and shouldn't
   be smuggled into it.

---

## Decisions — Billy, 2026-09-03

These supersede the "Open questions" section above wherever they overlap.

| Question | Decision |
|---|---|
| §3b `@Economy Notifications` unenforced preference | **Honour "(none)".** The dial becomes real: with no role set the 🔔 button tells the member notifications aren't set up on this server. This **reverses the 2026-08-22 call** that the button should always work — taken knowingly. |
| §4c roster page | **Build it — with provenance** (direction (d)). The full option: per-dial truth, the roster page, and the table recording which roles the bot created versus which were adopted. |
| Deleting a bot-made role | **Not built.** "Stop managing" means *stop pointing at it*; the role is left in the server. Provenance makes a delete button *safe* to offer, which is not the same as wanting one. Ask before adding it. |
| R4 dials (`guess_role_id`, Voice Control spectate gate) | **Reopen both**, create-on-offer — the role is only ever created in the same action that offers it to members, so it never exists empty. This closes the live exposure where an unset spectate gate leaves the room readable by @everyone. |
| §3g `/invite` and Manage Server | **Leave the invite narrow.** The Onboarding panel must detect the missing permission and say so, with the steps to grant it — a visible limitation rather than a silent read-only page. |
| §6 config export/import | **Deferred**, not folded into round 2. Scope it separately if wanted. |

### Corrections to this document from a production read

§8 question 2 asks whether `econ_game_role_id = 0` "in the two other guilds" is a
decision or a save artifact. A read-only query of the production database on
2026-09-03 shows it is **one guild, not two**:

| Guild | `econ_game_role_id` | Messages |
|---|---|---|
| 1469491362444480666 | `1526051848518373608` | 606,233 |
| 1476525656115515484 | `1544611624143552573` | 77,582 |
| 1358148226850492618 | `0` | 96,359 |

The two large guilds both hold real role ids. Only 1358148226850492618 sits at
`0`, and it is not dormant — 96k messages. So its 🔔 button is dead while the
other two work. Clearing that row would re-enable it, but that is a **production
config write** and was not taken here; raise it with Billy as its own decision.

### Consequences that must be carried into the build

- Honouring "(none)" is a **behaviour change in production**, not just copy: any
  guild currently relying on the role being created on demand will stop getting
  it. Check what each guild has set before shipping, and say in the commit which
  guilds are affected.
- The §3d false-deletion bug (a second guild inheriting a `guild_id=0` id gets
  "⚠️ **Jailed** was deleted" posted to its mod channel) and the §3e hierarchy
  hazard in `jail/apply.py` are **not** covered by any decision above. They are
  live defects; fold them in or raise them, don't leave them silently.
- Provenance dissolves R3 (storage), so the roster's states become facts rather
  than inferences. Build the provenance table first — the roster's honesty
  depends on it.
