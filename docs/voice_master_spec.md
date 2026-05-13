# Dungeon Keeper — Voice Master Feature Spec

**Status:** Feature spec for handoff to Claude Code
**Scope:** Feature behavior only. No implementation details.
**Module:** New cog within Dungeon Keeper

---

## 1. Overview

Voice Master gives members on-demand control over their own voice channels in The Golden Meadow. Members create personal channels by joining a designated "click to create" lobby (the Hub), and from there manage who can join, what the channel is called, and whether it's open to the meadow at large.

Channels persist as long as anyone is in them. When the last person leaves, the channel is deleted automatically. Each member has a saved profile that remembers their preferences across sessions, so their next channel picks up where they left off.

**Goals:**
- Low-friction, member-owned voice spaces — no mod intervention required
- Rooms that feel like *the owner's* space — named, sized, and gated however they want
- Empty channels never linger; the category stays clean
- Surface is discoverable without a tutorial

---

## 2. Core Concept: The Hub

A designated voice channel acts as the **Hub** (e.g. "➕ Create Voice"). When a member joins the Hub, the bot:

1. Creates a new voice channel in the configured Voice Master category
2. Applies the member's saved profile (or defaults, if no profile exists)
3. Moves the member into the new channel
4. Marks them as the **owner** of that channel

The owner gets a control panel (slash commands and a persistent button panel in a designated text channel) for managing their channel.

---

## 3. Owner Capabilities

The owner of a voice channel can perform all of the following actions on their channel:

### Channel Settings
- **Rename** — change the display name. Subject to a configurable cooldown (Discord enforces 2 channel edits per 10 minutes per channel — design must respect this). Profanity/blocklist filtering applies.
- **Set user limit** — cap the number of users (1–99). Or remove the cap.
- **Lock** — denies `Connect` to `@everyone`. Members already in the channel are not kicked. Trusted members (see §6) retain access.
- **Unlock** — restores `Connect` for `@everyone`.
- **Hide** — denies `View Channel` to `@everyone`. Channel becomes invisible to non-invited members. Distinct from lock: locked-but-visible means people see the room exists; hidden means they don't.
- **Unhide** — restores `View Channel` for `@everyone`.

### Member Management
- **Invite (one-off)** — grants `Connect` to a specific member for this channel only. Sends them a DM with a clickable join link. Disappears when the channel is deleted.
- **Trusted invite** — grants `Connect` *and* adds the member to the owner's persistent trust list (see §6). Every future channel the owner creates auto-grants them access.
- **Kick** — removes a member's `Connect` permission and disconnects them if they're currently in the channel. One-off by default.
- **Permanent block** — kick + add to the owner's persistent blocklist. Auto-denied on every future channel.

### Ownership
- **Transfer ownership** — hand control to another member currently in the channel.
- **Claim ownership** — if the current owner is no longer in the channel and has been disconnected from it for at least the configured threshold (default 5 minutes) or has left the server, any remaining member can claim ownership.
- **Reset channel** — wipe all custom permissions and settings on the *current channel*, returning it to default state. Owner is preserved.

### Profile Management
- **Reset profile** — see §6.

---

## 4. Member Capabilities (Non-Owners)

- **Request to join a locked channel** — sends a notification to the owner (in the designated control text channel, pinging the owner) with Accept / Deny buttons. The owner can grant access without needing to type a command.
- **See who owns a channel** — a simple lookup so members know who to ask. Available via `/voice owner` or by clicking the channel info on the panel.

---

## 5. Default Channel Behavior

When a new channel is created from the Hub *and the member has no saved profile*:

| Field | Default |
|---|---|
| Name | Configurable template, default `{owner display name}'s Room` |
| Visibility | Public (visible and joinable by `@everyone`) |
| User limit | None |
| Bitrate | Server default |
| Category | Configured Voice Master category |

All defaults are configurable per-server by admins (see §9).

---

## 6. Saved Preferences & Channel State

Every member has a **saved channel profile** that persists across sessions. When they join the Hub, their new channel is created with their saved settings already applied — name, lock state, user limit, hidden state, trust list, and blocklist.

The first time someone creates a channel, defaults apply. From then on, any change they make to their active channel writes through to their profile automatically. Next time they spin one up, it picks up where they left off.

### What Gets Saved (per member)

- **Channel name** — last custom name, or default template if never renamed
- **Lock state** — locked (private) or unlocked (public) at last use
- **Hidden state** — hidden from `@everyone` view at last use
- **User limit** — preferred cap, or no limit
- **Bitrate** — saved for forward compatibility (not in v1 owner controls)
- **Trust list** — members the owner has explicitly chosen to remember as trusted
- **Blocklist** — members the owner has explicitly chosen to remember as blocked

### Trust List vs. One-Off Invites

Inviting someone is the common case and shouldn't pollute saved state. So invites have two flavors:

**One-off invite** (default) — grants `Connect` for this channel only. Disappears when the channel is deleted. This is what the 👋 Invite button does by default.

**Trusted invite** — grants `Connect` *and* adds the member to the owner's trust list. Every future channel the owner creates will auto-grant them access, even when locked or hidden. Exposed as a "Remember this person" checkbox in the invite modal, or via `/voice trust @user`.

Same two-flavor pattern for kicks: one-off kick vs. permanent block.

### Trust & Block List Management

- `/voice trusted list` — ephemeral list of all trusted members
- `/voice trusted add @user` — add without needing them in a channel
- `/voice trusted remove @user` — pull them off
- `/voice blocked list` / `add` / `remove` — same pattern for the blocklist

### Apply-on-Create Flow

When a member joins the Hub:

1. Bot creates the channel in the configured category
2. Loads the member's saved profile (or defaults if none)
3. Applies name, user limit, lock state, hidden state in a single batch
4. Applies trust list (grants `Connect` to each trusted member)
5. Applies blocklist (denies `Connect` to each blocked member)
6. Moves the member in

If any step fails silently (saved name is now blocklisted, trusted member left the server, etc.), the bot skips that piece and continues. The owner receives one ephemeral notification listing what was skipped, if anything.

### Auto-Save Behavior

Changes to the active channel write through to the profile in real time:

- Rename → saves new name
- Lock / unlock → saves lock state
- Hide / unhide → saves hidden state
- Set limit → saves limit
- Trusted invite → adds to trust list
- Permanent block → adds to blocklist

One-off invites and one-off kicks do **not** write to the profile.

### Profile Inspection & Reset

- `/voice profile show` — ephemeral display of saved settings: name, lock state, limit, trust list, blocklist
- `/voice profile reset` — clears everything to server defaults. Confirmation prompt required.
- `/voice profile reset name` / `limit` / `trusted` / `blocked` — granular resets

The 🧹 Reset button on the panel must distinguish between two actions:
- "Reset just this channel" (current state only)
- "Reset my saved profile too" (current state + persisted profile)

These are easy to confuse, so the button opens a confirmation with both options clearly labeled.

### Privacy

The trust list and blocklist are private to the owner. No one else — including the members on them — can see who's on them. A trusted member knows they have access (they can join locked channels) but receives no "you are trusted by X" notification. A blocked member is not notified they're blocked; they simply find they can't connect.

Admins with override permissions can view any member's profile for moderation, and any such view is logged.

---

## 7. Persistence & Cleanup

- Channels are **ephemeral** — deleted automatically when empty
- A grace period (configurable, default 15 seconds) prevents deletion if someone briefly disconnects and reconnects
- Channel-to-owner mapping is persisted in the database, not held in memory only
- On bot restart, the bot reconciles state on boot:
  - Tracked channels that are now empty → cleaned up
  - Tracked channels with people still in them → resume normally, ownership preserved
  - Untracked channels in the Voice Master category → leave alone, log a warning

---

## 8. Interface Design

Two complementary control surfaces. Both should always be available; members pick what they prefer.

### Slash Commands

```
/voice rename <name>
/voice lock
/voice unlock
/voice hide
/voice unhide
/voice limit <number>
/voice invite <member> [remember: true/false]
/voice kick <member> [remember: true/false]
/voice transfer <member>
/voice claim
/voice reset
/voice owner
/voice trusted list | add <member> | remove <member>
/voice blocked list | add <member> | remove <member>
/voice profile show
/voice profile reset [field]
```

### Persistent Panel

A single message in a designated text channel, with buttons:

| Button | Action |
|---|---|
| 🔒 Lock | Lock channel |
| 🔓 Unlock | Unlock channel |
| 👁️ Hide | Hide channel |
| 👀 Unhide | Unhide channel |
| ✏️ Rename | Opens modal |
| 🔢 Limit | Opens modal |
| 👋 Invite | Opens user-select + "remember" checkbox |
| 🚫 Kick | Opens user-select + "remember" checkbox |
| 👑 Transfer | Opens user-select (only members in channel) |
| 🧹 Reset | Opens confirm dialog with two options |
| ⚙️ Profile | Opens profile inspect/reset menu |

Buttons only act on the channel the clicker currently owns. If they don't own a channel, the bot responds ephemerally: "You don't own a voice channel right now — join the Hub to create one."

The panel uses modals for text inputs (rename, limit) and user-select dropdowns for member targets (invite, kick, transfer). The "remember this person" toggle on invite/kick is a checkbox inside the modal.

---

## 9. Admin Configuration

Server admins (or members with a configured "Voice Master Admin" role) can configure:

### Setup
- **Hub channel** — the click-to-create voice channel
- **Target category** — where created channels live
- **Control text channel** — where the persistent panel and join request notifications appear

### Defaults
- **Default name template** — supports tokens like `{display_name}`, `{username}`
- **Default user limit** — number or "no limit"
- **Default bitrate**

### Limits & Cooldowns
- **Rename cooldown** — must respect Discord's 2-edits-per-10-min hard limit
- **Create cooldown** — prevents spam (default e.g. 30 seconds between Hub joins)
- **Max channels per member** — default 1
- **Trust list size cap** — default 25
- **Blocklist size cap** — default 25
- **Owner-disconnect grace period** — before claim becomes available, default 5 minutes
- **Empty-channel grace period** — before auto-delete, default 15 seconds
- **Trusted member auto-prune threshold** — days absent before auto-removal from trust lists, default never

### Moderation Controls
- **Name blocklist** — words/patterns that can't appear in channel names
- **Disable saved preferences server-wide** — force every channel to use server defaults
- **Saveable fields whitelist** — e.g. allow saving limit and trust list but force the name to always be the default template
- **Force-delete a channel** — admin override
- **Force-transfer ownership** — admin override
- **Force-clear a member's profile** — admin override (logged)
- **View a member's profile** — admin override (logged)

---

## 10. Permissions Required (Bot)

- `Manage Channels` — create, delete, rename, edit permissions
- `Move Members` — to move the creator into their new channel
- `Connect` — to verify channels and clean up
- `View Channels` — in the Voice Master category

---

## 11. Edge Cases & Behavior Notes

### Ownership
- **Owner leaves but channel still active:** ownership stays with them. They can return and resume control. Other members can claim only after the configured grace period (default 5 min) or if the owner has left the server entirely.
- **Owner disconnects briefly:** does not transfer or void ownership.
- **Owner is timed out:** ownership stays; they cannot use commands while timed out. Channel persists.
- **Owner is banned or kicked from server:** channel is auto-transferred to a remaining member if any, otherwise auto-deleted. Their entry is removed from all other members' trust lists (cleanup job).

### Channel & Server Limits
- **Discord's 50-channel-per-category limit hit:** bot falls back to creating outside the category, notifies the owner, alerts admins.
- **Discord's 500-channel server limit hit:** creation fails gracefully with a clear message to the user.
- **Hub channel deleted by an admin:** bot logs an error and disables the feature until reconfigured. Does not auto-recreate the Hub.
- **Target category deleted:** same — disable, log, alert.

### Member Management
- **Kicked member tries to rejoin:** can't, until re-invited or channel is unlocked (or, if blocked, never on this owner's channels).
- **Trusted member left the server:** silently skipped on apply. Stays in the list in case they return. Auto-pruned per admin config (default never).
- **Trusted member is now server-banned:** skipped on apply, flagged for removal on next profile view.
- **Member tries to trust themselves:** rejected with a friendly error.
- **Member tries to trust a bot:** rejected.

### Profile
- **Saved name is now blocklisted by admins:** falls back to default template, owner notified.
- **Saved profile references a member who no longer exists:** silently skipped, member is pruned from list per admin config.
- **Profile data exceeds list caps:** oldest entries are pruned (FIFO) when caps are exceeded; user is notified.

### Rename Cooldown
- Discord allows only 2 channel edits per 10 minutes per channel. The bot must:
  - Track edits per channel in memory
  - Reject rename attempts that would exceed the limit, with a clear message ("You can rename again in X seconds")
  - Note: this counts *all* edits, not just renames, so internal permission changes from invite/kick also consume the budget. The bot must be conservative.

---

## 12. Out of Scope (v1)

Deliberately excluded to keep surface area tight:

- Soundboards / per-channel audio settings
- Channel templates ("Game Night," "Co-working," "Hangout" presets)
- Multiple named profiles per member ("Game Night profile" vs "Co-working profile")
- Sharing profiles between members
- Profile import/export
- Stage channel support
- Voice activity / talk-time tracking
- Integration with the member quality scoring algorithm (worth revisiting in v2 — voice engagement is currently invisible to it)
- Auto-region selection

---

## 13. Success Criteria

The feature is working well when:

- Members create voice channels without asking for help
- Mods are not pinged to "kick someone from voice" or "make this private"
- Channels feel like *their* space — named, sized, gated however the owner wants
- Empty channels never linger; the category stays clean
- The control surface is discoverable enough that new members find it without a tutorial
- Saved preferences make repeat use feel effortless — members don't reconfigure every time

---

## 14. Data Model Notes (for implementer)

The implementer should design schemas for at least the following persistent entities:

- **Active channel** — channel ID, owner ID, guild ID, created-at timestamp, last-edit timestamps (for cooldown tracking)
- **Member profile** — user ID, guild ID, saved name, saved limit, saved lock state, saved hidden state, saved bitrate
- **Trust list entry** — owner user ID, trusted user ID, guild ID, added-at timestamp
- **Block list entry** — owner user ID, blocked user ID, guild ID, added-at timestamp
- **Guild config** — guild ID, hub channel ID, category ID, control channel ID, all configurable defaults and caps

Profiles are per-guild, not global. A member's saved settings in The Golden Meadow are independent of any other server.

---

*End of spec.*
