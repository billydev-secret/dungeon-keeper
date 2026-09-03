# Inactive — Feature Spec

Moves inactive members into a single shared "inactive" channel: their roles are snapshotted and stripped, they receive the `@Inactive` role (which can only see that channel), and a persistent panel there invites them to open a ticket to be reactivated. A softer sibling of the jail system — no per-user channels, transcripts, or policy machinery. Members enter manually (`/inactive mark`) or via an inactivity sweep (run from Config → Inactive Sweep on the dashboard, or an opt-in background loop); the only way out is `/inactive release`.

## Commands

All are subcommands of the `/inactive` group.

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/inactive mark user:<member> [reason]` | Slash | Mod (default perm: Moderate Members) | Snapshot + strip roles, apply `@Inactive`, move member to the inactive channel |
| `/inactive release user:<member> [reason]` | Slash | Mod (default perm: Moderate Members) | Restore snapshotted roles and remove `@Inactive` |
| **Config → Inactive Sweep → Inactive Channel** | Web | Admin | Set the inactive channel, create/wire the `@Inactive` role, revoke the old channel's access, post the info + "Open Ticket" panel. Replaced `/inactive panel` 2026-07-28 |
| **Config → Inactive Sweep → Who Would Be Swept** | Web | Admin | Preview the sweep (**Check Now**) or run it (**Move Them Now**). Replaced `/inactive sweep` 2026-07-28; both halves share `compute_candidates`, so the preview can't disagree with the run |

Sweep settings (threshold/auto/cap — previously `/inactive config`) are configured from the
web dashboard's Inactive Sweep panel — see [Configuration](#configuration).

Runtime checks re-verify mod/admin status via the bot's own role config (`_is_mod` / `_is_admin`), independent of Discord default permissions. `mark` and the sweep refuse to run until an inactive channel has been set on Config → Inactive Sweep.

## Behavior

### Marking (`/inactive mark`, sweep)
Both routes go through `apply_inactive`. Preconditions (same policy as jail): never a bot, never yourself, never an admin, only admins may move a moderator, never someone already held inactive, and never a member exempted on the dashboard (see [Exemptions](#exemptions)). On success:

1. `@Inactive` role is fetched or created (`ensure_inactive_role`, which since 2026-08-22 runs on the shared provisioner in `core/role_provision.py`). If the configured id is unset or stale but a role named exactly `Inactive` already exists, that role is **adopted** and its id stored, rather than a second `@Inactive` being created. Only a genuinely new role is denied view+send on every channel and then granted view/send/history on the configured inactive channel — an adopted role is one the guild already set up, and re-denying every channel on it would be destructive. If the id was previously set and now resolves to nothing, an admin deleted the role: it is remade, and that fact is posted to the mod channel and written to the audit log, because the new role is empty and everyone who held the old one has lost it.
2. The member's roles are snapshotted (excluding `@everyone`, the `@Inactive` role, and managed/integration roles) and removed; `@Inactive` is added. A Forbidden here aborts with a role-hierarchy hint.
3. A row is written to `inactive_members` and an `inactive_apply` entry to the moderation audit log.
4. The member is DMed ("your roles are saved", link to the inactive channel, optional reason note); DM failure is ignored.
5. A "Member Moved to Inactive" embed is posted to the guild's `log_channel_id` (if set).

### Sweep candidate selection
A member's last-seen is `max(last message timestamp from processed_messages, joined_at)` — a fresh joiner who hasn't posted isn't treated as ancient; members with no cached join time are skipped. Excluded outright: bots, the guild owner, anyone with Administrator or Manage Guild, configured mods/admins, exempted members, and already-inactive members. Candidates are members idle ≥ the threshold, sorted most-idle-first and truncated to the per-run cap; the overflow count is surfaced so truncation is never silent. Selection itself is a pure function (`select_sweep_candidates`) with unit tests pinning the exclusions and cap.

Building that function's inputs — the last-seen map and the exclusion set — is `compute_candidates` in `inactive/sweep_service.py`. It carries as much policy as the selector, so the cog, the background loop and the dashboard preview all call it rather than keeping copies: a preview built on re-implemented exclusion rules would drift from the sweep it claims to predict. `threshold_days`/`cap` default to the saved config and are overridable (the preview passes an unsaved threshold and `cap=None`, meaning no truncation); the configured cap comes back on the result either way, so a caller that lifted it can still say what one run would reach.

Note the limits of the evidence: `processed_messages` is never pruned by retention, but it only goes back to when tracking started and is wiped per-user by a privacy erasure. A member with no rows at all is aged from `joined_at` alone, so "active before the bot arrived" and "joined long ago and never spoke" are indistinguishable. The sweep treats both as idle; the dashboard preview flags them (`has_tracked_messages`) rather than showing a last-seen date that reads like a last post.

### The sweep run
`POST /api/config/inactive/sweep` — the dashboard's **Move Them Now** button — selects candidates through `compute_candidates` at the saved per-run cap and runs each through the full mark flow (reason "Inactivity sweep", `source="dashboard"`). It 400s until an inactive channel is configured. The response reports how many were moved, how many were considered, and the overflow left for a later run — what `/inactive sweep apply:true` used to report before this route replaced it on 2026-07-28. **Check Now**, the dry-run half, is a separate endpoint covered next.

### Dashboard dry-run preview
`POST /api/config/inactive/preview` (admin) answers "who would lose roles if I switched this on", without writing anything or creating the `@Inactive` role. It selects through `compute_candidates` with `cap=None`, so it lists **every** eligible member rather than one run's worth, and returns `first_run_reach` — how many of the listed members one run actually moves, which is how the overflow contract is honored when nothing is truncated. That count is computed server-side because the cap applies to the whole most-idle-first candidate list *before* anyone is set aside as unstrippable: comparing the sweepable count against the cap would over-promise whenever the top of the list holds members the bot is outranked by. `threshold_days` and `sweep_cap` in the request body override the saved settings so unsaved values can be tried; the threshold changes who is selected, the cap only changes that note. Member/role data is gateway state, so it 503s when the bot isn't connected — including when `guild.me` is uncached, since without the bot's own role position every candidate would be misreported as unstrippable. Every id is serialized as a string.

Per-member role lists come from `build_sweep_preview` (pure, in `inactive/logic.py`), which shares `roles_to_strip` with `apply_inactive` rather than restating the rule — callers filter managed roles out first, then `roles_to_strip` drops `@everyone` and `@Inactive`. A member holding only managed roles still appears — they are still moved and still get `@Inactive` — with an empty removal list.

It also reports two things the sweep never says out loud:

- **`blocked`** — candidates with a role *due to be stripped* at or above the bot's top role. `apply_inactive` would raise `Forbidden` on the strip and the loop counts that as a silent non-move, so they are split out and the headline count excludes them. The yardstick is deliberately the highest **strippable** role, not the member's top role: a managed role above the bot (booster, Twitch sync, another bot's role) is never touched and doesn't stop the roles below it being removed, so judging by top role would file a perfectly sweepable member under "would fail".
- **`inactive_channel_configured`** — false means the background loop no-ops however the toggle is set, so nobody listed would actually move.

### Automatic sweep
A background loop starts with the bot and wakes every **6 hours**. It acts only on the home guild, and only when the web dashboard's Inactive Sweep panel has "Enable automatic sweep" checked **and** an inactive channel is configured. It uses the same candidate selection and cap, marks with actor `guild.me` and `source="auto"`, and logs the moved count.

### Release (`/inactive release`)
Restores whichever snapshotted roles still exist (deleted roles are counted and reported), then removes `@Inactive` — in that order, so a partial failure never strands the member with neither. Marks the DB row `reactivated`, writes an `inactive_reactivate` audit entry, DMs the member, and posts a "Member Reactivated" embed to the log channel. Any ticket the member opened is deliberately left for a moderator to close.

### The info panel
Persists the channel choice, ensures the `@Inactive` role exists and can see the channel, then posts an accent-colored embed with the ticket system's persistent "Open Ticket" button (registered by the jail cog, so it survives restarts).

Re-running the command against a **different** channel re-points the setup: the `@Inactive` role's permission overwrite is cleared from the previously-configured channel (`stale_inactive_channel_id` in `inactive/logic.py` decides whether there is one), so an ex-inactive channel doesn't stay visible to held members. If that cleanup fails (missing permissions or a Discord error) the panel still goes up and the invoker gets a warning naming the channel to fix by hand.

## Configuration

Per-guild keys in the config table. `inactive_channel_id`/`inactive_role_id` are set via
the dashboard's Inactive Channel setup; the sweep-tuning keys are set on the same **Inactive Sweep**
panel (`config-inactive.js` / `PUT /api/config/inactive`) — the `/inactive config` command
that used to set them was removed.

| Key | Default | Meaning |
|---|---|---|
| `inactive_channel_id` | unset (0) | The shared inactive channel; required before mark/sweep work |
| `inactive_role_id` | unset (0) | The `@Inactive` role; auto-created on demand through `ensure_config_role` (2026-09-03) — a stored 0 means "not set up yet", never "off", and an id inherited from the legacy `guild_id = 0` row is not read as a deletion. Listed with its live state on Config → Bot-Managed Roles |
| `inactive_threshold_days` | 30 | Days idle before a member qualifies for a sweep (1–3650) |
| `inactive_auto_sweep` | off | Enables the 6-hourly background sweep |
| `inactive_sweep_cap` | 25 | Max members moved per sweep run (1–200) |

### Exemptions

Per-member exemptions from inactivity holds, managed on the same panel
(`PUT`/`DELETE /api/config/inactive/exemptions/{user_id}`). An exemption is
**absolute**: `check_inactive_preconditions` refuses an exempt target
(`exempt_target`), and since `apply_inactive` runs those preconditions itself,
that one check covers every entry path — `/inactive mark`, the dashboard sweep, and
the background loop. Exempt ids *also* join the sweep's exclusion set upstream,
which is not redundancy for its own sake: it keeps exempt members out of the
selection entirely, so they never appear in the preview and never inflate a
sweep's candidate count only to be refused one at a time. Bots, the owner, admins
and mods are already excluded structurally and need no exemption row.

Also reads the shared `log_channel_id` for audit embeds. Requires the bot to have **Manage Roles** (role creation/assignment) with its top role above both the target's roles and `@Inactive`.

## Stored data

- `inactive_members` table (migration `057_inactive_members.sql`): `guild_id`, `user_id`, `moderator_id`, `reason`, `stored_roles` (JSON list of role IDs), `source` (`command` / `auto`), `created_at`, `status` (`active` / `reactivated`), `reactivated_at`, `reactivate_reason`. One active row per member enforces idempotency.
- `inactive_sweep_exemptions` table (migration `136_inactive_sweep_exemptions.sql`): `guild_id`, `user_id`, `added_by`, `added_at`. One row per exempt member; `added_by`/`added_at` record who spared whom and when; neither is read by the sweep, and the dashboard currently shows only the member.
- Moderation audit log: `inactive_apply` and `inactive_reactivate` actions with actor, target, and reason.
