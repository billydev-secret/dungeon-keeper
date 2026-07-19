# Role Grant — Feature Spec

Let trusted members hand out specific community roles with `/grant`, without giving anyone Manage Roles. Each guild configures a set of named "grant roles" (e.g. Denizen, NSFW, Veteran), each with its own per-user/per-role permission allowlist, optional announcement message, and optional audit-log channel. Typical use: greeters grant Denizen to newly vetted members.

> **Not the same feature as Role Menus** (`role_menus_spec.md`): Role Menus is members self-assigning roles via buttons/dropdowns; Role Grant is one member giving a role to *another* member through an allowlist-gated command.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/grant role:<key> member:<@member>` | Slash | Per-grant allowlist, or mod | Give a configured community role to a member |
| `/grant_missing role:<key> min_level:<n>` | Slash | Mod | List members at/above a level who don't have a configured grant role yet |

The `role` argument autocompletes from the guild's configured grant roles, matching against both the internal key and the display label (max 25 choices). Members who can grant at least one role also get a "Role Grants" page in `/help` listing their available grants.

### `/grant_missing`

A mod-only report, not a self-serve command. Defaults to `role:nsfw min_level:5` — surfaces members who hit the level-5 XP threshold (where the "promotion review" post fires, see `xp_spec.md`) but were never actually given the NSFW/adult-access role. Cross-references `member_xp.level` against live Discord role membership, and excludes anyone currently on an active inactive-channel hold (`inactive_members.status = 'active'`) — those members had every role stripped on purpose, so a missing grant there isn't an oversight. Replies ephemerally with an embed listing up to 40 members (mention + level), or a plain "nobody missing" message when the list is empty.

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
