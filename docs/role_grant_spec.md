# Role Grant — Feature Spec

Let trusted members hand out specific community roles with `/grant`, without giving anyone Manage Roles. Each guild configures a set of named "grant roles" (e.g. Denizen, NSFW, Veteran), each with its own per-user/per-role permission allowlist, optional announcement message, and optional audit-log channel. Typical use: greeters grant Denizen to newly vetted members.

> **Not the same feature as Role Menus** (`role_menus_spec.md`): Role Menus is members self-assigning roles via buttons/dropdowns; Role Grant is one member giving a role to *another* member through an allowlist-gated command.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/grant role:<key> member:<@member>` | Slash | Per-grant allowlist, or admin | Give a configured community role to a member |
| **Grant Audit → Post Card** | Web | Admin | Post (or refresh/move) the auto-updating grant-audit card. Uses the page's own grant-role and minimum-level controls, so the card matches the tables above it |

The `role` argument autocompletes from the guild's configured grant roles, matching against both the internal key and the display label (max 25 choices). Members who can grant at least one role also get a "Role Grants" page in `/help` listing their available grants.

(The old `/grant_missing` audit command was replaced by the dashboard's **Grant Audit** report panel; the Discord card it also posts moved onto that same report panel on 2026-07-28, retiring `/grant_audit`.)

## Grant Audit panel

A moderator dashboard panel (Reports → Member Lists → Grant Audit, `GET /api/reports/grant-audit?grant_name=<key>&min_level=<n>`, default `nsfw` / level 5) that splits members missing a grant role into three buckets:

| Bucket | Meaning |
|---|---|
| **Waiting for first grant** | At/above the level bar with **no evidence of ever holding the role** — no prune ledger row *and* no historical `role_events` grant row. The role was simply never given. Sorted highest level first. |
| **Stripped but came back** | Stripped (open `role_prune_events` row, or an *implicit strip* — see below), active again since (last activity at/after the prune window's cutoff), but never re-granted — pruned fairly, returned, and nobody closed the loop. |
| **Last 10 inactive stripped** | Most recent open strips whose members are still inactive — the prune working as intended, shown for visibility. Newest first (implicit strips sort last), capped at 10. |

All buckets exclude bots, members who left, and anyone on an active inactive-channel hold or jail — checked against both the DB hold rows **and** a hold role held live in Discord (a mod may have stripped roles by hand without a DB row).

**Implicit strips.** `role_events` isn't gapless — a removal that happens while the bot is down is never logged, so it can't be backfilled into the ledger. A member with a historical grant row for the role, no ledger row, and no role today is therefore treated as an *implicit* open strip with an unknown date: they're bucketed by activity like any other open event and shown with "date unrecorded" (dashboard: "unrecorded") instead of a fabricated timestamp, and they never appear in "waiting for first grant". This keys off `role_events.role_name`, so a role rename orphans older history — an accepted `role_events` limitation.

### The `role_prune_events` ledger

The inactivity-prune loop (`inactivity_prune_service`, see the prune rule config) removes the configured role from long-inactive members. Each removal now writes a durable row to `role_prune_events` (`guild_id`, `user_id`, `role_id`, `source = 'inactivity_prune'`, `pruned_at`, `restored_at`). The lifecycle:

- **pruned** — the prune loop inserts an open row (`restored_at IS NULL`) per member it strips.
- **restored** — a successful `/grant` of the same role closes any open rows for that member (`restored_at` set to the grant time). "Active again" is *not* stored — it stays a live computation against last-activity, since it's a moving target.

A one-off backfill helper (`role_grant_audit_service.backfill_prune_events_from_role_events`) seeds the ledger from historical `role_events` removals, skipping removals the activity history disproves as prunes and inserting already-restored rows for members who hold the role again. It's idempotent and is run once per guild/role from a REPL after deploy.

### The auto-updating card

The same three buckets also render as a channel embed a mod can pin anywhere. The **Post Card** control at the bottom of that report panel posts it — pick a channel and press Post Card; the card audits whatever grant role and level bar the page is currently showing. Behavior mirrors the economy leaderboard panel:

- The card's channel/message ids and grant/min-level parameters are stored per guild in config (`grant_audit_card_*` keys); one card per guild.
- Re-running the command in the same channel refreshes in place; a different channel moves the card (the stale message is deleted when reachable).
- An hourly loop (`grant_audit_card_loop`, registered at startup) re-renders every stored card. To keep the card at the bottom of the channel it edits in place only while the card is still the channel's newest message (`channel.last_message_id`); once anyone has posted since, it reposts a fresh copy at the bottom, deletes the old one, and repoints the stored message id. A 404 on the message (mod deleted it) retires the card by clearing the stored ids; a missing grant config or role does the same.
- The waiting and "stripped but came back" buckets each cap at 15 lines with an "…and N more on the dashboard" overflow (keeping the card under Discord's 1024-char field limit); the recently-stripped-still-inactive bucket is already capped at 10 upstream. The stripped buckets show `stripped <t:…:R>` relative timestamps that tick client-side between edits.

## Behavior

Permission first: **administrators** always pass; everyone else — moderators included — must appear in the grant's allowlist (`grant_role_permissions`) either directly by user ID or via any role they hold. The checks then run in order — guild-only, target isn't a bot, no granting to yourself (mods may), the grant has a `role_id` configured and the role still exists, the target doesn't already have it, the bot has Manage Roles, and the role sits below the bot's top role.

### Why admin and not mod

Until 2026-07-30 the gate short-circuited on `is_mod`, which made the allowlist
**additive only**: every moderator could hand out every configured grant, and no
list could take that away. "Golden Girl is one keeper's to give" was
unexpressable. `can_use_grant_role` / `can_grant_any_role` now short-circuit on
`is_admin` instead, so the per-grant list actually decides. Administrators keep
the bypass so a guild can't lock itself out of its own grants.

Note `is_admin` is stricter than `is_mod` in a second way: it honours the
`administrator` permission and configured admin roles, but **not** bare
`manage_guild`.

Migration `144_grant_permissions_seed_mods.sql` copies each guild's configured
`mod_role_ids` into every grant it has, so the flip is behavior-neutral on the
day it ships and narrowing a grant is a dashboard edit rather than an outage.

The self-grant block inside `_execute_grant` is deliberately still `is_mod` —
it governs how a grant executes, not who may invoke it. A non-mod keeper can
therefore use their grant but still can't grant it to themselves. (The
`required_role_id` gate was the other `is_mod` check; it moved to `is_admin`
on 2026-08-11 — see **Prerequisite role**.)

On success the bot adds the role (audit-log reason "Granted by {user} via slash command"), records a `role_events` row, and confirms to the invoker ephemerally.

**Announcement** — if the grant has an `announce_channel_id` and a non-empty `grant_message`, the template is posted to that channel. Placeholders: `{member}`, `{member_name}`, `{role}`, `{role_name}`, `{actor}`.

**Audit log** — if the grant has a `log_channel_id`, a green embed ("{member} was granted {role} by {granter}.", mentions suppressed) is posted there. **Grants only**: nothing posts there when the role is later removed (removals reach the `role_events` table via `on_member_update`, and the dashboard's Grant Audit report). The panel hint used to promise "given or taken away" and was corrected 2026-08-29.

**Prerequisite role** — a grant may name a `required_role_id`: the target must
already hold that role to receive this one. The decision is
`role_grant_logic.prerequisite_gate`, a pure function so the matrix is a test
table rather than Discord mocks.

*Only administrators bypass it* — not moderators. A moderator is the person
most likely to run `/grant` on a fresh arrival, so exempting them would leave
the gate barely load-bearing; admins keep the override so a guild can't wedge
itself behind a prerequisite it can no longer satisfy.

A configured prerequisite whose role has been **deleted** fails *closed* (the
grant is refused as misconfigured), because the alternative silently disables
a safety gate the moment someone tidies up a role.

**Self-service menus honour it too** (2026-08-29). A Role Menu button is a
second door to a role, and until now the menu path never consulted the grant
config: publishing a button for a gated role handed it out to anyone who
clicked, silently, with the publish-time validation checking only
managed/hierarchy/dangerous-permission roles. The click path now asks
`role_grant_logic.prerequisites_for_role(grant_roles, role_id)` — keyed by role
id, since that's what a menu option holds, and returning *every* prerequisite
when two grants point at the same role — and runs each through the same
`prerequisite_gate`. Refusals are ephemeral ("You need the **@X** role before
you can pick that"); a deleted prerequisite fails closed and alerts the mods
once, like the menus' other config-drift failures. Checking on the click rather
than at publish time is deliberate: it also covers menus published *before* the
grant was configured. The per-grant *Who Can Hand This Out* allow-list is
**not** consulted — it answers who may run `/grant` for someone else, which a
self-service button isn't.

History: settable since migration `021` and exposed on the dashboard as *Role
Required First*, but `/grant` never passed it to the executor, so from `021`
until **2026-08-11** the gate could not run at all. In production this meant
the Member grant's prerequisite — the verification role
(`intake_verified_role_id`) — was advisory, and members were granted Member
without ever verifying. See `scripts/backfill_verified_role.py` for the
companion remediation, which reads live Discord role state rather than
`role_events` (that table only records role changes the bot observed, so a
role gained during a downtime is invisible to it).

## User-visible errors

All ephemeral.

| When | The user sees |
|---|---|
| Not on the grant's allowlist (and not admin) | "You don't have permission to use this command." |
| Grant key isn't configured | "This grant role is not configured." |
| Used outside a guild | "This command only works in a server." |
| Target is a bot | "Bots can't receive this role." |
| Non-mod targets themselves | "You can't grant this role to yourself." |
| Grant has no `role_id` set | "This role is not configured yet." |
| Configured role was deleted | "The configured role no longer exists." |
| Target already has the role | "{member} already has {role}." |
| Target lacks the prerequisite role (non-admin) | "{member} needs {required} before they can receive {role}." |
| Prerequisite role was deleted (non-admin) | "This grant is misconfigured — the required role no longer exists. Contact an admin." |
| Bot lacks Manage Roles | "I need the Manage Roles permission to do that." |
| Role is above the bot's top role | "I can't grant {role} because it is above my highest role." |
| Discord rejects the role add | "I couldn't grant {role}. Check my role hierarchy and permissions." |

## Configuration

Everything lives in the database, per guild, managed from the web dashboard (admin): `GET /config` returns the grant-role snapshot, `PUT /config/roles/{grant_name}` creates/updates one, `DELETE /config/roles/{grant_name}` removes it (and its permissions). No Discord-side setup commands.

Each grant role has:

| Field | Meaning |
|---|---|
| `grant_name` | Internal key used as the `/grant role:` value |
| `label` | Display name shown in autocomplete and `/help` |
| `role_id` | Discord role to grant |
| `log_channel_id` | Optional audit-log channel (0 = off) |
| `announce_channel_id` | Optional announcement channel (0 = off) |
| `grant_message` | Announcement template (empty = no announcement) |
| `required_role_id` | Prerequisite role — target must hold it first; admins bypass (see Behavior) |

Plus an allowlist of `(entity_type, entity_id)` entries — individual users and/or roles — per grant.

**Legacy migration** — on startup, a guild with no `grant_roles` rows gets a one-time migration from old flat config keys (`{name}_role_id`, `{name}_log_channel_id`, `{name}_announce_channel_id`, `{name}_grant_message`) for the five historical grants: `denizen`, `nsfw`, `veteran`, `kink`, `goldengirl`. A legacy `greeter_role_id` becomes a role-allowlist entry on all five.

## Stored data

| Table | Contents |
|---|---|
| `grant_roles` | One row per guild + grant key with the fields above |
| `grant_role_permissions` | Allowlist entries: `(guild_id, grant_name, entity_type ∈ user/role, entity_id)` |
| `role_events` | One row per successful grant: guild, user, role name, action `grant`, timestamp |
| `role_prune_events` | Durable prune ledger: guild, user, role id, source, `pruned_at`, `restored_at` (NULL while open) — written by the inactivity-prune loop, closed by `/grant` |
