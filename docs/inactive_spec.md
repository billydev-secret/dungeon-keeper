# Inactive — Feature Spec

Moves inactive members into a single shared "inactive" channel: their roles are snapshotted and stripped, they receive the `@Inactive` role (which can only see that channel), and a persistent panel there invites them to open a ticket to be reactivated. A softer sibling of the jail system — no per-user channels, transcripts, or policy machinery. Members enter manually (`/inactive mark`) or via an inactivity sweep (manual `/inactive sweep` or an opt-in background loop); the only way out is `/inactive release`.

## Commands

All are subcommands of the `/inactive` group.

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/inactive mark user:<member> [reason]` | Slash | Mod (default perm: Moderate Members) | Snapshot + strip roles, apply `@Inactive`, move member to the inactive channel |
| `/inactive release user:<member> [reason]` | Slash | Mod (default perm: Moderate Members) | Restore snapshotted roles and remove `@Inactive` |
| `/inactive panel channel:<text channel>` | Slash | Admin (default perm: Manage Guild) | Set the inactive channel, create/wire the `@Inactive` role, post the info + "Open Ticket" panel |
| `/inactive sweep [apply:<bool>]` | Slash | Admin (default perm: Manage Guild) | Preview (default, dry run) or execute an inactivity sweep |
| `/inactive config [threshold_days] [auto] [cap]` | Slash | Admin (default perm: Manage Guild) | View or change sweep settings; always echoes current values |

Runtime checks re-verify mod/admin status via the bot's own role config (`_is_mod` / `_is_admin`), independent of Discord default permissions. `mark` and `sweep` refuse to run until `/inactive panel` has set an inactive channel.

## Behavior

### Marking (`/inactive mark`, sweep)
Both routes go through `apply_inactive`. Preconditions (same policy as jail): never a bot, never yourself, never an admin, only admins may move a moderator, and never someone already held inactive. On success:

1. `@Inactive` role is fetched or created (`ensure_inactive_role`). On first creation it is denied view+send on every channel, then granted view/send/history on the configured inactive channel.
2. The member's roles are snapshotted (excluding `@everyone`, the `@Inactive` role, and managed/integration roles) and removed; `@Inactive` is added. A Forbidden here aborts with a role-hierarchy hint.
3. A row is written to `inactive_members` and an `inactive_apply` entry to the moderation audit log.
4. The member is DMed ("your roles are saved", link to the inactive channel, optional reason note); DM failure is ignored.
5. A "Member Moved to Inactive" embed is posted to the guild's `log_channel_id` (if set).

### Sweep candidate selection
A member's last-seen is `max(last message timestamp from processed_messages, joined_at)` — a fresh joiner who hasn't posted isn't treated as ancient; members with no cached join time are skipped. Excluded outright: bots, the guild owner, anyone with Administrator or Manage Guild, configured mods/admins, and already-inactive members. Candidates are members idle ≥ the threshold, sorted most-idle-first and truncated to the per-run cap; the overflow count is surfaced so truncation is never silent. Selection itself is a pure function (`select_sweep_candidates`) with unit tests pinning the exclusions and cap.

### `/inactive sweep`
Default is a **dry run**: lists up to 20 candidates with idle days plus an overflow note, and instructs re-running with `apply: true`. With `apply: true` each candidate goes through the full mark flow (reason "Inactivity sweep") and the moved count is reported.

### Automatic sweep
A background loop starts with the bot and wakes every **6 hours**. It acts only on the home guild, and only when `/inactive config auto:true` has been set **and** an inactive channel is configured. It uses the same candidate selection and cap, marks with actor `guild.me` and `source="auto"`, and logs the moved count.

### Release (`/inactive release`)
Restores whichever snapshotted roles still exist (deleted roles are counted and reported), then removes `@Inactive` — in that order, so a partial failure never strands the member with neither. Marks the DB row `reactivated`, writes an `inactive_reactivate` audit entry, DMs the member, and posts a "Member Reactivated" embed to the log channel. Any ticket the member opened is deliberately left for a moderator to close.

### Panel (`/inactive panel`)
Persists the channel choice, ensures the `@Inactive` role exists and can see the channel, then posts an accent-colored embed with the ticket system's persistent "Open Ticket" button (registered by the jail cog, so it survives restarts).

## Configuration

Per-guild keys in the config table, set via `/inactive panel` and `/inactive config`:

| Key | Default | Meaning |
|---|---|---|
| `inactive_channel_id` | unset (0) | The shared inactive channel; required before mark/sweep work |
| `inactive_role_id` | unset (0) | The `@Inactive` role; auto-created on demand |
| `inactive_threshold_days` | 30 | Days idle before a member qualifies for a sweep (1–3650) |
| `inactive_auto_sweep` | off | Enables the 6-hourly background sweep |
| `inactive_sweep_cap` | 25 | Max members moved per sweep run (1–200) |

Also reads the shared `log_channel_id` for audit embeds. Requires the bot to have **Manage Roles** (role creation/assignment) with its top role above both the target's roles and `@Inactive`.

## Stored data

- `inactive_members` table (migration `057_inactive_members.sql`): `guild_id`, `user_id`, `moderator_id`, `reason`, `stored_roles` (JSON list of role IDs), `source` (`command` / `auto`), `created_at`, `status` (`active` / `reactivated`), `reactivated_at`, `reactivate_reason`. One active row per member enforces idempotency.
- Moderation audit log: `inactive_apply` and `inactive_reactivate` actions with actor, target, and reason.
