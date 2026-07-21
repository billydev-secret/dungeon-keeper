# Auto-Role on Join

**Flavor: Reference** — matches current behavior.

## Purpose

Automatically applies a configured set of roles to every new human member the
moment they join. No commands — dashboard-configured (**Config → Roles →
Auto-Role**, panel id `config-auto-role`, admin-only), per the "config lives
on the web" rule.

## Behavior

`EventsCog.on_member_join` (`src/bot_modules/cogs/events_cog.py`) runs the
auto-role step after the jail-rejoin check, join bookkeeping, invite tracking,
and the welcome/greeter messages. It fires only when all of:

- `cfg.auto_role_ids` is non-empty (from the cached `GuildConfig` snapshot),
- the joiner is **not a bot** (`member.bot`), and
- the joiner is **not a jailed rejoiner** (`check_jail_rejoin` returned True —
  someone who left while jailed gets their jail state back instead of the
  welcome roles).

Configured ids are filtered **at apply time** to roles that still exist, are
not `managed` (booster/integration roles Discord won't let a bot assign), and
sit **below the bot's own top role**. Skipped ids are logged
(`"auto_role: skipping unassignable role ids …"`) but never removed from
config — fix the role hierarchy and the next join gets them. The surviving
roles are applied in one `member.add_roles(...)` call with audit-log reason
`"auto-role on join"`; `Forbidden`/`HTTPException` are logged, not retried.

Timing is immediate and one-shot: on the join event only. There is no delay,
no retroactive application to existing members, and no re-application on
rejoin beyond the join event itself firing again.

## Configuration

| Panel field | Storage | Meaning |
|---|---|---|
| Roles to assign on join | `config_ids` bucket `auto_role_ids` (per guild) | The role set. Empty = feature off. |

The panel (`src/web_server/static/js/panels/config-auto-role.js`) shows a
checkbox list of the guild's non-managed roles and saves via
`PUT /api/config/auto-role` (`update_auto_role` in
`src/web_server/routes/config.py`, admin-gated with
`require_perms({"admin"})`; save clears and rewrites the bucket). Current
state is read from `GET /api/config` (`auto_role.auto_role_ids`, ids as
strings for snowflake safety). The route calls
`ctx.invalidate_guild_config(guild_id)` on save, so changes apply to the next
join without a restart.

## Stored data

Rows in the shared `config_ids` table (bucket `auto_role_ids`, one row per
role id per guild) — no dedicated table or migration. Loaded into
`GuildConfig.auto_role_ids` as a `frozenset[int]`
(`src/bot_modules/core/app_context.py`).

## Non-goals

- No bot-specific role assignment — bots are skipped entirely, and there is
  no separate "bot role" setting.
- No delayed/verified-gate assignment (no "after N minutes" or
  screening-complete trigger).
- No backfill of existing members when the config changes.
- No per-role conditions; it's one flat set applied to everyone who joins.
