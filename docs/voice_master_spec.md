# Voice Master — Feature Spec

Members create personal voice channels on demand by joining a designated **Hub** channel. The bot creates a new channel in the configured category, applies the member's saved profile (or defaults), moves them in, and marks them as owner. Channels are deleted automatically when empty.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/voice rename <name>` | Slash | Channel owner | Rename your channel |
| `/voice lock` / `/voice unlock` | Slash | Channel owner | Deny / restore `Connect` for `@everyone` |
| `/voice hide` / `/voice unhide` | Slash | Channel owner | Deny / restore `View Channel` for `@everyone` |
| `/voice limit <number>` | Slash | Channel owner | Set or clear the user cap (1–99, or none) |
| `/voice invite <member> [remember]` | Slash | Channel owner | Grant Connect; optionally add to trust list |
| `/voice kick <member> [remember]` | Slash | Channel owner | Remove Connect; optionally add to blocklist |
| `/voice transfer <member>` | Slash | Channel owner | Hand ownership to a member currently in the channel |
| `/voice claim` | Slash | Member in channel | Take ownership when the owner has been gone past the grace period |
| `/voice reset` | Slash | Channel owner | Wipe custom perms / settings on the current channel |
| `/voice owner` | Slash | Everyone | See who owns a channel |
| `/voice trusted list \| add \| remove` | Slash | Member (manages own list) | Manage your trust list |
| `/voice blocked list \| add \| remove` | Slash | Member (manages own list) | Manage your blocklist |
| `/voice profile show` | Slash | Member (own profile) | See saved settings |
| `/voice profile reset [field]` | Slash | Member (own profile) | Reset all saved settings or just one field |
| Persistent panel buttons | Persistent | Channel owner | Same actions surfaced as buttons in a designated text channel |

## Behaviour

### The Hub

A configured voice channel acts as the **Hub** (e.g. "+ Create Voice"). When a member joins it, the bot creates a new channel in the configured target category, applies the member's saved profile, and moves them in. They are now the owner. The Hub channel itself stays in place.

### Profiles (saved across sessions)

Every member has a per-guild saved profile that persists across channel lifecycles. When they create a new channel, it's pre-configured with:

- Their last channel name (or the default template if never renamed)
- Their lock / hide states at last use
- Their user-limit preference
- Their trust list applied (each trusted member auto-granted Connect)
- Their blocklist applied (each blocked member auto-denied Connect)

Any change to their active channel writes through to the profile automatically (rename, lock, hide, limit, trusted invite, permanent block). One-off invites and one-off kicks do **not** persist. If any apply-on-create step fails (saved name is now blocklisted, trusted member left the server, etc.) the bot skips that piece and continues, sending the owner one ephemeral notification listing what was skipped.

### Trust list and blocklist

Inviting and kicking each come in two flavours:

- **One-off invite / one-off kick** — applies only to the current channel. Default.
- **Trusted invite / permanent block** — applies to the current channel **and** writes to the member's saved trust list / blocklist, so every future channel they own auto-applies it.

Trust and block lists are private to the owner. Trusted members get no "you are trusted by X" notification; blocked members aren't notified either.

### Persistent panel

A single message in a designated text channel holds buttons for every owner action: Lock, Unlock, Hide, Unhide, Rename (modal), Limit (modal), Invite (user-select + remember checkbox), Kick (user-select + remember checkbox), Transfer (user-select limited to channel members), Reset (confirmation), Profile (inspect / reset). Buttons act on the channel the clicker currently owns; if they don't own one, the bot replies ephemerally "You don't own a voice channel right now — join the Hub to create one."

The Reset button asks the owner to choose between resetting just this channel (current state) or also resetting their saved profile.

### Ownership and the grace period

When the owner leaves the channel, ownership stays with them — they can return at any time. If they've been gone for past the configured **grace period** (default 5 min) or have left the server entirely, any remaining member can claim ownership via `/voice claim` or the panel.

If the owner is banned or kicked from the server, ownership auto-transfers to a remaining member (if any), otherwise the channel is auto-deleted. The departed member is purged from other members' trust lists.

### Cleanup

Channels are ephemeral — deleted automatically when empty. A configurable **empty-channel grace period** (default 15 s) prevents deletion when someone disconnects and reconnects briefly. The channel-to-owner mapping is persisted, so on bot restart the bot reconciles state: empty tracked channels are cleaned up, occupied tracked channels resume normally with ownership preserved, and untracked channels in the category are left alone with a warning logged.

### Rename cooldown

Discord limits each channel to 2 edits per 10 minutes. The bot tracks edits per channel and rejects rename attempts that would exceed this with "You can rename again in X seconds." Invite and kick operations also consume this budget (they change channel permissions), so the bot is conservative.

### Saveable-fields whitelist

The admin-controlled `voice_master_saveable_fields` config (see Configuration) is a comma-separated list of field names. A field's auto-save behaviour only fires if its name is present in the list. Removing a token turns off auto-save for that one field while leaving the others on. There's also a global kill switch (`voice_master_disable_saves`) that disables every auto-save.

### Category fallback

If the target category has hit Discord's 50-channel cap, the bot creates the channel outside the category, notifies the owner, and alerts admins. If the server itself has hit the 500-channel cap, creation fails with a clear message.

## Permissions

- **Bot:** Manage Channels (create, delete, rename, edit perms), Move Members (move the creator in), Connect (verify and clean up), View Channels (in the category).
- **User:** A "Voice Master Admin" role (configurable) is needed to change setup or use admin overrides; otherwise any member can use the Hub.

## User-visible errors

| When | The user sees |
|---|---|
| Panel button click without owning a channel | "You don't own a voice channel right now — join the Hub to create one." |
| Rename would exceed Discord's 2-per-10-min edit cap | "You can rename again in X seconds." |
| `/voice claim` before the owner's grace period has elapsed | Ephemeral rejection naming the time remaining |
| Trying to trust yourself or a bot | Friendly rejection |
| Saved profile applies a name that's now blocklisted | Channel falls back to the default name template; owner gets an ephemeral notice |
| Trusted member left the server | Silently skipped on apply; surfaces on profile view |
| Category hit Discord's 50-channel cap | Channel created outside the category; owner notified; admins alerted |
| Server hit Discord's 500-channel cap | "Couldn't create your channel — server is full." |
| Hub or category was deleted by an admin | Feature disables; admins alerted; nothing auto-recreates |

## Non-goals

- Soundboards or per-channel audio settings.
- Channel templates / presets ("Game Night", "Co-working").
- Multiple named profiles per member.
- Sharing or import/export of profiles between members.
- Stage channel support.
- Voice activity / talk-time tracking.
- Auto-region selection.

## Configuration

Setup:

- Hub voice channel
- Target category for created channels
- Control text channel (where the persistent panel and join-request notifications live)

Defaults applied to fresh channels:

- Name template (supports `{display_name}`, `{username}`; default `{display_name}'s Room`)
- User limit (default: no limit)
- Bitrate (default: server default)

Limits and cooldowns:

- Rename cooldown (must respect Discord's 2-per-10-min)
- Create cooldown (default 30 s between Hub joins)
- Max channels per member (default 1)
- Trust list size cap (default 25), blocklist size cap (default 25)
- Owner-disconnect grace period before claim is allowed (default 5 min)
- Empty-channel grace period before delete (default 15 s)
- Trusted-member auto-prune threshold in days absent (default: never)

Saveable-fields toggles:

- `voice_master_disable_saves` — global kill switch for all auto-save (default off).
- `voice_master_saveable_fields` — comma-separated whitelist of fields that auto-save. Default `"name,limit,locked,hidden,trusted,blocked"`. Removing a token turns off auto-save for that one field.

Moderation overrides (logged):

- Channel-name blocklist (words / patterns)
- Force-delete a channel
- Force-transfer ownership
- Force-clear a member's profile
- View a member's profile

## Stored data

Per-guild and per-member, all in the database:

- **Active channels** — channel id, owner id, creation timestamp, recent edit timestamps (for cooldown tracking).
- **Member profiles** — saved name, lock state, hide state, user limit, bitrate. Per-guild — settings in one server don't carry across to another.
- **Trust list entries** — one row per (owner, trusted member, guild).
- **Block list entries** — one row per (owner, blocked member, guild).
- **Guild config** — Hub channel, category, control channel, all admin defaults and caps.

Admin profile views are logged.
