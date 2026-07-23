# Hidden Channels — Feature Spec

Admin tool to hide a channel from everyone and later restore it exactly as it was. `/hidden hide` snapshots the channel's permission overwrites and placement (parent category + position), denies `@everyone` View Channel, and parks the channel under a "Hidden Channels" category. `/hidden restore` reads the snapshot back to move the channel home and reinstate its exact overwrites.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/hidden hide channel:<channel>` | Slash | Admin | Snapshot perms + placement, then hide the channel from everyone |
| `/hidden restore channel:<channel>` | Slash | Admin | Move the channel back and reinstate its original overwrites |
| `/hidden list` | Slash | Admin | List channels currently hidden and who hid them |

The command group is guild-only with `default_permissions=Administrator`, and every command also passes the bot's own admin check. All require the **bot** to have Manage Channels and Manage Roles. Hideable channel types: text, voice, stage, forum — categories are excluded (hiding one would orphan its children). All responses are ephemeral.

## Behavior

### `/hidden hide`
Rejects a channel that already has an active hold ("already hidden"). Otherwise it records the channel's parent category id (NULL if top-level), position, and full overwrite map, then finds or creates the **Hidden Channels** category (created with `@everyone` denied View Channel, bot allowed) and edits the channel into it with overwrites replaced by `{@everyone: deny view, bot: allow view}`. The snapshot row is persisted **before** the Discord edit: the edit destroys the original overwrites irreversibly, so a DB failure afterwards would lose them for good. A failed write therefore leaves the channel untouched, and a failed edit rolls the row back out. Audit-log reason names the invoking admin.

"Hidden from everyone" means `@everyone` is denied View Channel. Members with Administrator still see it — Discord always exempts Administrator from channel overwrites.

### `/hidden restore`
Rejects a channel with no active hold. Otherwise rebuilds the stored overwrites — targets that no longer exist (deleted role, departed member) are silently skipped so one stale entry can't abort the restore — and edits the channel back to its original category with those overwrites. If the original category was deleted while hidden, the channel is restored to the top level instead. Position restore is best-effort: a second edit sets the original position, and if Discord rejects the stale index the failure is only logged (a misplaced-but-visible channel beats a failed restore). The hold is then marked `restored`.

### `/hidden list`
Lists all active holds in the guild, oldest first, as `channel mention — hidden by @user`. A hidden channel that was deleted shows as `(deleted channel <id>)`.

## User-visible errors

| When | The user sees |
|---|---|
| Invoker isn't an admin | "You need to be an admin to use this command." |
| Bot lacks Manage Channels or Manage Roles | "I need the **Manage Channels** and **Manage Roles** permissions…" |
| `hide` on an already-hidden channel | "{channel} is already hidden. Use `/hidden restore` to bring it back." |
| `restore` on a channel with no hold | "{channel} isn't currently hidden." |
| Discord forbids the move/edit (role hierarchy) | "I'm not allowed to move or edit that channel — check my role's position and permissions." |
| Other Discord API failure | "Something went wrong talking to Discord. Please try again." |
| `hide` can't save the snapshot (DB error) | "I couldn't save this channel's permissions, so I left it alone. Please try again." |
| `list` with nothing hidden | "No channels are currently hidden." |

## Non-goals

- No hiding of categories — only text, voice, stage, and forum channels.
- No scheduled or automatic restore; hides last until an admin runs `/hidden restore`.
- No re-snapshot on repeated hide — a second `/hidden hide` is rejected rather than overwriting the saved state.
- No restoration of overwrites for roles/members that vanished while the channel was hidden; those entries are dropped.
- The "Hidden Channels" category is never deleted automatically, even when it empties out.

## Configuration

None. No dashboard surface — the three slash commands are the entire interface. The parking category name is fixed (`Hidden Channels`, matched by name).

## Stored data

One row per hold in the `hidden_channels` table (migration `src/migrations/058_hidden_channels.sql`): guild id, channel id, original parent id (NULL if top-level), original position, the overwrite snapshot as JSON `[{id, type: role|member, allow, deny}]` bit pairs, who hid it, `created_at` / `restored_at` timestamps, and `status` (`active` / `restored`). A partial unique index enforces at most one active hold per channel. Restored rows are kept, not deleted.
