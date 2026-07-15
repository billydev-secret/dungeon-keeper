# Rename — Feature Spec

Moderator command to change (or reset) another member's server nickname. A thin wrapper around Discord's nickname edit with friendlier validation and error messages; the change is attributed in the audit log to the invoking moderator.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/rename target:<member> new_name:[text]` | Slash (guild-only) | Moderator (see below) | Set `target`'s nickname to `new_name`, or reset it to their username when `new_name` is omitted |

The command is hidden by default from members without **Manage Nicknames** (Discord default-permissions), and additionally runs the bot's own moderator check: the invoker must have **Manage Server** or **Administrator**, or hold a configured mod role (`is_mod`).

## Behaviour

- `new_name` is stripped of surrounding whitespace; a blank or omitted value resets the nickname (sets it to `None`).
- Nicknames longer than 32 characters (Discord's cap) are rejected before any API call.
- The server owner can never be renamed — Discord forbids it, so the command refuses up front.
- Before editing, the bot verifies it has **Manage Nicknames** itself and that the target's top role is strictly below the bot's top role.
- The edit carries an audit-log reason: `Renamed by {user} ({user id})`.
- All responses are ephemeral. Success confirms the old display name and (when set) the new nickname.

## User-visible errors

| When | The user sees |
|---|---|
| Invoker fails the moderator check | "You don't have permission to use this command." |
| Nickname over 32 characters | "Nicknames can be at most 32 characters (that one is {n})." |
| Target is the server owner | "I can't rename the server owner — Discord doesn't allow it." |
| Bot lacks Manage Nicknames | "I need the **Manage Nicknames** permission to do this." |
| Target's top role ≥ bot's top role | "I can't rename {member} — their highest role is above mine." |
| Discord returns Forbidden anyway | "I'm not allowed to rename {member} (role hierarchy or permissions)." |
| Any other Discord API failure | "Something went wrong talking to Discord. Please try again." |

## Configuration

None specific to the feature. Who counts as a moderator comes from the shared guild config (mod roles) plus Discord's **Manage Server** / **Administrator** permissions; the bot needs **Manage Nicknames**.

## Stored data

None. The rename is stateless — the only record is Discord's own audit-log entry.
