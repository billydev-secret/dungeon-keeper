# Role Grant — Feature Spec

Let trusted members hand out specific community roles with `/grant`, without giving anyone Manage Roles. Each guild configures a set of named "grant roles" (e.g. Denizen, NSFW, Veteran), each with its own per-user/per-role permission allowlist, optional announcement message, and optional audit-log channel. Typical use: greeters grant Denizen to newly vetted members.

> **Not the same feature as Role Menus** (`role_menus_spec.md`): Role Menus is members self-assigning roles via buttons/dropdowns; Role Grant is one member giving a role to *another* member through an allowlist-gated command.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/grant role:<key> member:<@member>` | Slash | Per-grant allowlist, or mod | Give a configured community role to a member |
| `/grant_audit role:<key> min_level:<n> channel:<#ch>` | Slash | Mod | Post (or refresh/move) the auto-updating grant-audit card |

The `role` argument autocompletes from the guild's configured grant roles, matching against both the internal key and the display label (max 25 choices). Members who can grant at least one role also get a "Role Grants" page in `/help` listing their available grants.

(The old `/grant_missing` audit command was replaced by the dashboard's **Grant Audit** panel and the `/grant_audit` card, below.)

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

### The auto-updating card (`/grant_audit`)

The same three buckets also render as a channel embed a mod can pin anywhere: `/grant_audit role:<key> min_level:<n> [channel:<#ch>]` posts the card (defaults: `nsfw`, level 5, current channel). Behavior mirrors the economy leaderboard panel:

- The card's channel/message ids and grant/min-level parameters are stored per guild in config (`grant_audit_card_*` keys); one card per guild.
- Re-running the command in the same channel refreshes in place; a different channel moves the card (the stale message is deleted when reachable).
- An hourly loop (`grant_audit_card_loop`, registered at startup) re-renders every stored card. A 404 on the message (mod deleted it) retires the card by clearing the stored ids; a missing grant config or role does the same.
- The waiting bucket caps at 15 lines with an "…and N more on the dashboard" overflow; the two stripped buckets show `stripped <t:…:R>` relative timestamps that tick client-side between edits.

## Behavior

Permission first: mods always pass; anyone else must appear in the grant's allowlist (`grant_role_permissions`) either directly by user ID or via any role they hold. The checks then run in order — guild-only, target isn't a bot, no granting to yourself (mods may), the grant has a `role_id` configured and the role still exists, the target doesn't already have it, the bot has Manage Roles, and the role sits below the bot's top role.

On success the bot adds the role (audit-log reason "Granted by {user} via slash command"), records a `role_events` row, and confirms to the invoker ephemerally.

**Announcement** — if the grant has an `announce_channel_id` and a non-empty `grant_message`, the template is posted to that channel. Placeholders: `{member}`, `{member_name}`, `{role}`, `{role_name}`, `{actor}`.

**Audit log** — if the grant has a `log_channel_id`, a green embed ("{member} was granted {role} by {granter}.", mentions suppressed) is posted there.

**Prerequisite role** — the shared grant executor supports a `required_role_id` (target must already hold it; mods bypass), and the dashboard can set it, but `/grant` currently does not pass it through, so it is **not enforced**.

## User-visible errors

All ephemeral.

| When | The user sees |
|---|---|
| Not on the grant's allowlist (and not mod) | "You don't have permission to use this command." |
| Grant key isn't configured | "This grant role is not configured." |
| Used outside a guild | "This command only works in a server." |
| Target is a bot | "Bots can't receive this role." |
| Non-mod targets themselves | "You can't grant this role to yourself." |
| Grant has no `role_id` set | "This role is not configured yet." |
| Configured role was deleted | "The configured role no longer exists." |
| Target already has the role | "{member} already has {role}." |
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
| `required_role_id` | Prerequisite role (settable, currently unenforced — see Behavior) |

Plus an allowlist of `(entity_type, entity_id)` entries — individual users and/or roles — per grant.

**Legacy migration** — on startup, a guild with no `grant_roles` rows gets a one-time migration from old flat config keys (`{name}_role_id`, `{name}_log_channel_id`, `{name}_announce_channel_id`, `{name}_grant_message`) for the five historical grants: `denizen`, `nsfw`, `veteran`, `kink`, `goldengirl`. A legacy `greeter_role_id` becomes a role-allowlist entry on all five. (The README's description of `greeter_role_id`/`denizen_role_id` as live config keys is this legacy shape.)

## Stored data

| Table | Contents |
|---|---|
| `grant_roles` | One row per guild + grant key with the fields above |
| `grant_role_permissions` | Allowlist entries: `(guild_id, grant_name, entity_type ∈ user/role, entity_id)` |
| `role_events` | One row per successful grant: guild, user, role name, action `grant`, timestamp |
| `role_prune_events` | Durable prune ledger: guild, user, role id, source, `pruned_at`, `restored_at` (NULL while open) — written by the inactivity-prune loop, closed by `/grant` |
