# Voice Master — Feature Spec

Members create personal voice channels on demand by joining a designated **Hub** channel. The bot creates a new channel in the configured category, applies the member's saved profile (or defaults), moves them in, and marks them as owner. Channels are deleted automatically when empty.

## Commands

All member commands live under the `/voice` group. Owner commands act on **the channel the caller owns** (found via the DB), not necessarily the channel they're sitting in — if they own none, the reply is "You don't own a voice channel right now — join the Hub to create one."

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/voice access <state>` | Slash | Channel owner | Single 4-state access dial: 🔓 open / 🔞 nsfw / 🔒 locked / 🎭 spectate (see below) |
| `/voice rename <name>` | Slash | Channel owner | Rename your channel (1–100 chars; server name-blocklist checked; rate-gated) |
| `/voice limit <limit>` | Slash | Channel owner | Set the user cap (`0` = no cap, or 1–99) |
| `/voice invite <member> [remember]` | Slash | Channel owner | Grant View + Connect; `remember` also adds to trust list. Invitee gets a DM with a jump link |
| `/voice kick <member> [remember]` | Slash | Channel owner | Deny Connect and disconnect them if present; `remember` also adds to blocklist |
| `/voice transfer <member>` | Slash | Channel owner | Hand ownership to a non-bot member currently in the channel |
| `/voice claim` | Slash | Member in channel | Take ownership when the owner has been gone past the grace period (or left the server) |
| `/voice reset [also_profile]` | Slash | Channel owner | Wipe all per-member overwrites (owner keeps access, `@everyone` back to neutral, status back to open); `also_profile` also deletes the saved profile. Does **not** clear Discord's channel-level NSFW flag — a channel reset out of `nsfw`/`locked`/`spectate` keeps its age-restricted badge even though permissions and the status line go back to open |
| `/voice owner` | Slash | Everyone | See who owns the voice channel you're in |
| `/voice sleepkick <hours>` | Slash | Everyone | Self-disconnect timer: 0–24 hours; `0` cancels a pending timer |
| `/voice knock <channel>` | Slash | Everyone | Ask a managed channel's owner to let you in (Accept/Deny posted in the control channel) |
| `/voice trusted list \| add \| remove` | Slash | Member (manages own list) | Manage your trust list |
| `/voice blocked list \| add \| remove` | Slash | Member (manages own list) | Manage your blocklist |
| `/voice profile show` | Slash | Member (own profile) | See saved settings (name, limit, access state, trusted/blocked counts) |
| `/voice profile reset [field]` | Slash | Member (own profile) | Reset all saved settings or one field: `all`, `name`, `limit`, `access`, `trusted`, `blocked` |
| `/voice-admin post-panel` | Slash | Administrator | Post (or repost) the persistent control panel into the configured control channel |
| Panel dropdowns | Persistent | Channel owner | Same actions surfaced as grouped select menus (control channel + each channel's side chat) |
| Claim button | Persistent | Member in channel | Posted into a channel's side chat once the owner is gone past grace |

`/voice-admin` is a separate top-level group with `default_permissions=administrator`; `post-panel` additionally re-checks admin at runtime (Discord Administrator permission or the bot's configured admin roles). It is the group's only command — all other admin configuration lives in the web dashboard (see Configuration).

## Behaviour

### The Hub

A configured voice channel acts as the **Hub** (e.g. "+ Create Voice"). When a member joins it, the bot creates a new channel in the configured target category, applies the member's saved profile, and moves them in. They are now the owner. The Hub channel itself stays in place.

If the member already owns a live channel, joining the Hub moves them back into it instead of creating another. A Hub join inside the create cooldown, or past the max-channels-per-member cap, silently disconnects them from the Hub (no message — mid-event DMs are unreliable).

On create the bot also: marks the channel age-gated (`nsfw=True`) when the profile's access state is anything but open, sets the access-state status line, posts the inline control panel into the channel's side chat (if enabled), DMs the owner about anything skipped, and arms the empty-grace timer (so an orphaned channel gets cleaned up even if the move-in failed).

### The access dial (`/voice access`)

One 4-state dial replaces the old separate lock/hide toggles. **Every state except open is age-gated** (the channel's Discord NSFW flag is set; open clears it). Each state also writes a matching voice-channel *status* line (a separate endpoint, immune to the rename rate limit):

| State | Who can see / join | Status line |
|---|---|---|
| 🔓 `open` | Anyone can see and join; no age gate | "👋 All welcome" |
| 🔞 `nsfw` | Age-gated, but anyone can see and join | "🔞 Age-gated · all welcome" |
| 🔒 `locked` | Age-gated, **hidden from the channel list and invite-only**: `@everyone` is denied both View Channel and Connect. Invited/trusted members can still see and join; others can `/voice knock` | "🔒 Age-gated · ask to join" |
| 🎭 `spectate` | Age-gated muted audience: joiners can listen but `speak`, `stream` (camera/Go Live) and both `send_messages` perms are denied. With a **spectator gate role** configured, `@everyone` is denied Connect and only role-holders may join (as the muted audience) | "🎭 Age-gated · spectators welcome" |

Details:

- Any state can be set from any state; re-picking the current state just re-applies it (no error).
- **Locked** ties into Discord's rule that a voice channel's side chat requires View + Connect: on entering locked, everyone currently in the channel gets transient `view_channel`/`connect` grants so their chat keeps working. On **leaving** locked those transient grants are cleaned up — and by design this also drops one-off invite grants (the unhide pass clears the guest's view grant, which makes their overwrite match the transient-lock shape the unlock pass removes). **Invited guests are not preserved across leaving the locked state** — harmless while the room is open, but they must be re-invited if it's locked again. Trusted members and the owner are never touched.
- **Spectate** and locked are mutually exclusive: entering spectate tears down any lock/hide first; leaving spectate restores the open baseline before applying the new state. The owner and anyone already inside when spectate turns on keep full participation (explicit member grants outrank the audience deny). Anyone let in afterwards via invite, accepted knock, transfer or claim also comes in as a full speaker.
- A plain open↔nsfw switch never touches member overwrites — only the NSFW flag and status line change.
- The state is saved to the member's profile (under the `access` saveable field) and audit-logged as `vm_channel_access_<state>`.

### Profiles (saved across sessions)

Every member has a per-guild saved profile that persists across channel lifecycles. When they create a new channel, it's pre-configured with:

- Their last channel name (or the default template if never renamed)
- Their access state at last use (stored as `locked`/`hidden`/`spectator`/`age_gated` flags; legacy hidden-only profiles fall through to open)
- Their user-limit preference
- A bitrate (saved value, else the guild default, else the guild's boost-tier maximum — always clamped to the tier maximum)
- Their trust list applied (each trusted member auto-granted View + Connect)
- Their blocklist applied (each blocked member auto-denied Connect)

Any change to their active channel writes through to the profile automatically (rename, access state, limit, trusted invite, permanent block). One-off invites and one-off kicks do **not** persist. If any apply-on-create step fails (saved name is now blocklisted, trusted member left the server), the bot skips that piece and continues, DMing the owner one message listing what was skipped.

### Trust list and blocklist

Inviting and kicking each come in two flavours:

- **One-off invite / one-off kick** — applies only to the current channel. Default.
- **Trusted invite / permanent block** — applies to the current channel **and** writes to the member's saved trust list / blocklist, so every future channel they own auto-applies it.

Both lists are capped (default 25 each); adding past the cap evicts the oldest entry, which the confirmation reply mentions. Adds are idempotent ("X is already on your trust list."). You can't trust/block bots, can't trust yourself ("You're always trusted by yourself."), can't block yourself. Trust and block lists are private to the owner; neither trusted nor blocked members are notified.

### Knock (`/voice knock <channel>`)

Anyone can knock on a managed channel to ask its owner in. The bot posts an embed with owner-only **Accept** / **Deny** buttons into the configured control channel (mentioning the owner; buttons live for one hour). Accept grants the requester View + Connect (plus speaker perms if the room is spectating), DMs them a jump link, and is audit-logged as `vm_invite` with `via: knock`. Rejections the knocker sees: channel not managed by Voice Master; "You already own that channel."; owner no longer in the server (pointed at `/voice claim`); control channel unconfigured or unavailable.

### Sleep-kick (`/voice sleepkick <hours>`)

A personal self-disconnect timer: after `hours` (any value >0 up to 24, fractions allowed) the bot disconnects you from whatever voice channel you're in — a no-op if you've already left voice. `0` cancels a pending timer ("Sleep-kick cancelled." / "No active sleep-kick to cancel."). One timer per member per guild; setting a new one replaces the old. Timers are in-memory only and do not survive a bot restart. Not tied to Voice Master channels or ownership — it works in any voice channel.

### Persistent panel

The control panel is an embed plus **three grouped dropdown menus** (not buttons):

- **Access** — the four access states.
- **Settings** — Rename (modal), Limit (modal), Reset (two-button confirm: "Reset just this channel" vs "Reset channel + my saved profile").
- **Permissions** — Invite and Kick (ephemeral user-picker with two buttons: one-off vs remember), Transfer (select limited to non-bot members currently in the channel).

Menus act on the channel the clicker currently owns; if they don't own one, the bot replies ephemerally "You don't own a voice channel right now — join the Hub to create one." After each pick, the menu resets to its placeholder. The dropdowns are persistent dynamic items and survive restarts.

The panel appears in two places:

1. **Control channel** — one persistent copy, posted via `/voice-admin post-panel` (the message id is saved to config so it can be reposted). Requires the control channel to be configured first ("No control channel set. Configure it in the web dashboard first.").
2. **Inline** — a copy is auto-posted into each new channel's side chat on create (config `voice_master_post_inline_panel`, default on), so owners have the controls where they are.

### How-to guide

Admins can post a member-facing how-it-works embed into any text channel (e.g. a lobby) from the web dashboard's Voice Master panel — pick a channel and click **Post guide**. The embed explains the Hub-to-create flow, the four access states, invite/kick/knock, and the side-chat panel; it mentions the configured Hub channel when one is set, and falls back to plain text otherwise. It's a one-shot post (re-run anytime), separate from the persistent control panel, and posting is audit-logged (`vm_post_howto`).

### Ownership and the grace period

When the owner leaves the channel, ownership stays with them — they can return at any time. If they've been gone past the configured **grace period** (default 5 min) or have left the server entirely, any remaining member can claim ownership via `/voice claim` or the **claim button**: once the grace window elapses with members still inside, the bot posts a "👑 Channel up for grabs" prompt with a Claim button into the channel's side chat (cancelled if the owner returns in time; re-armed across restarts by reconciliation). The button requires the clicker to be in the channel and re-validates eligibility on every click, so a stale prompt refuses cleanly. Claiming grants the claimer View + Connect (plus speaker perms if spectating) and swaps the prompt for a "claimed" embed.

If the owner leaves or is banned from the server, ownership auto-transfers to the first remaining non-bot member (if any), otherwise the channel is deleted. The departed member is purged from every other member's trust **and** block lists.

### Cleanup

Channels are ephemeral — deleted automatically when empty. A configurable **empty-channel grace period** (default 15 s) prevents deletion when someone disconnects and reconnects briefly. The channel-to-owner mapping is persisted, so on bot restart the bot reconciles state: empty tracked channels are cleaned up, occupied tracked channels resume normally with ownership preserved, claim prompts are re-armed for channels whose owner left during the downtime, and untracked channels in the category are left alone with a warning logged.

### Rename cooldown

Discord limits each channel to 2 name edits per 10 minutes. The bot tracks a two-slot edit timestamp pair per channel and rejects renames that would exceed it: "Discord limits voice channel edits to 2 per 10 minutes — try again in Xs." **Only renames consume this budget** — access changes, limit, invite and kick ride separate endpoints (permission overwrites, user_limit, status) that aren't subject to the name rate limit. A rename rejected by validation (empty name, blocklisted) never burns a budget slot.

### Saveable-fields whitelist

The admin-controlled `voice_master_saveable_fields` config (see Configuration) is a comma-separated list of field names. A field's auto-save behaviour only fires if its name is present in the list. Removing a token turns off auto-save for that one field while leaving the others on. There's also a global kill switch (`voice_master_disable_saves`) that disables every auto-save (and makes every member's hub-join apply pure defaults). With saves disabled or the relevant field removed, `trusted add` / `blocked add` refuse with "Saving the trust list/blocklist is disabled by an admin on this server."

### Category fallback

If the target category has hit Discord's 50-channel cap, the bot creates the channel outside the category and logs a warning. If channel creation fails outright (e.g. server-wide channel cap), the failure is logged and the member simply stays in the Hub — there is no user-facing error message.

## Permissions

- **Bot:** Manage Channels (create, delete, rename, edit perms), Move Members (move the creator in, kicks, sleep-kick), Connect (verify and clean up), View Channels (in the category), Send Messages (panels, claim prompts, knock requests).
- **User:** any member can use the Hub and `/voice` commands. `/voice-admin` requires Discord's Administrator permission by default (`default_permissions`), and `post-panel` re-checks admin at runtime (Administrator or a configured admin role).

## User-visible errors

| When | The user sees |
|---|---|
| Owner command / panel pick without owning a channel | "You don't own a voice channel right now — join the Hub to create one." |
| Rename would exceed Discord's 2-per-10-min edit cap | "Discord limits voice channel edits to 2 per 10 minutes — try again in Xs." |
| Rename to an empty / filtered name | "Channel name can't be empty." / "That name matches a server-wide content filter — pick another." |
| `/voice claim` before the owner's grace period has elapsed | "The owner left Xs ago — claim available in Ys." |
| `/voice claim` while the owner is still around | "The owner is still active in or watching the channel." |
| `/voice claim` on your own channel | "You already own this channel." |
| Trying to trust/block yourself or a bot | Friendly rejection ("Can't trust bots.", "You're always trusted by yourself.", "Can't block yourself." …) |
| Trust/block add while saves are disabled | "Saving the trust list/blocklist is disabled by an admin on this server." |
| Transfer to a bot / yourself / someone outside the channel | Friendly rejection ("The new owner must currently be in the voice channel." …) |
| Kick yourself | "You can't kick yourself — transfer ownership first." |
| `/voice reset` when the owner has left the server (Discord can't resolve them as a member) | "Couldn't resolve channel owner." |
| `/voice knock` on an unmanaged channel / your own channel / unconfigured control channel | Friendly rejection naming the reason |
| `/voice sleepkick` outside 0–24 | "Hours must be between 0 and 24." |
| Saved profile applies a name that's now blocklisted | Channel falls back to the default name template; owner gets a DM notice |
| Trusted/blocked member left the server | Skipped on apply; owner's post-create DM counts them; profile view shows counts only |
| Category hit Discord's 50-channel cap | Channel silently created outside the category (warning logged; no user message) |
| Channel creation fails outright | No message — member stays in the Hub; failure logged |
| Hub or category was deleted by an admin | Feature disables; admins alerted in the mod-log channel; nothing auto-recreates (the alert text names `/voice-admin set-hub` / `set-category`, but reconfiguration actually happens in the web dashboard) |
| `/voice-admin post-panel` without a control channel | "No control channel set. Configure it in the web dashboard first." |

## Non-goals

- Soundboards or per-channel audio settings.
- Channel templates / presets ("Game Night", "Co-working").
- Multiple named profiles per member.
- Sharing or import/export of profiles between members.
- Stage channel support.
- Voice activity / talk-time tracking.
- Auto-region selection.

## Configuration

All admin configuration is done through the web dashboard (`/voice-master/config` routes); the only configuration slash command is `/voice-admin post-panel`. Keys live in the shared `config` table under a `voice_master_` prefix.

Setup:

- Hub voice channel (`voice_master_hub_channel_id`)
- Target category for created channels (`voice_master_category_id`)
- Control text channel (`voice_master_control_channel_id`) — hosts the persistent panel and knock requests
- Panel message id (`voice_master_panel_message_id`) — written automatically by `post-panel`
- Inline panel toggle (`voice_master_post_inline_panel`, default on) — post the panel into each new channel's side chat
- Spectator gate role (`voice_master_spectator_gate_role_id`, default unset) — when set, spectate mode admits only role-holders as the audience

Defaults applied to fresh channels:

- Name template (supports `{display_name}`, `{username}`; default `{display_name}'s Room`)
- User limit (`voice_master_default_user_limit`, default 0 = no limit)
- Bitrate (`voice_master_default_bitrate`, default 0 = the guild's boost-tier maximum; always clamped to the tier maximum)

Limits and cooldowns:

- Create cooldown (`voice_master_create_cooldown_s`, default 30 s between Hub-created channels)
- Max channels per member (`voice_master_max_per_member`, default 1)
- Trust list cap (`voice_master_trust_cap`, default 25), blocklist cap (`voice_master_block_cap`, default 25) — FIFO eviction past the cap
- Owner-disconnect grace period before claim is allowed (`voice_master_owner_grace_s`, default 300 s)
- Empty-channel grace period before delete (`voice_master_empty_grace_s`, default 15 s)
- Trusted-member auto-prune threshold in days inactive (`voice_master_trusted_prune_days`, default 0 = never; checked daily against XP-system activity)

Saveable-fields toggles:

- `voice_master_disable_saves` — global kill switch for all auto-save (default off).
- `voice_master_saveable_fields` — comma-separated whitelist of fields that auto-save. Default `"name,limit,access,trusted,blocked"`. Removing a token turns off auto-save for that one field.

Moderation overrides (all via the web dashboard, all audit-logged and mirrored to the mod-log channel):

- Channel-name blocklist (case-insensitive substring patterns; applied to renames and saved names)
- Force-delete a channel (`vm_admin_force_delete`)
- Force-transfer ownership (`vm_admin_force_transfer`)
- Force-clear a member's profile (`vm_admin_clear_profile`)
- View a member's profile (`vm_admin_view_profile`)

## Stored data

Per-guild and per-member, all in the database:

- **Active channels** (`voice_master_channels`) — channel id, guild id, owner id, creation timestamp, the two most recent edit timestamps (rename cooldown tracking), and `owner_left_at` (when the owner walked out; cleared on return/claim/transfer).
- **Member profiles** (`voice_master_profiles`) — saved name, `locked`/`hidden`/`spectator`/`age_gated` flags (together encoding the access state), user limit, bitrate. Per-guild — settings in one server don't carry across to another.
- **Trust list entries** (`voice_master_trusted`) — one row per (guild, owner, trusted member), with added-at ordering for cap eviction.
- **Block list entries** (`voice_master_blocked`) — one row per (guild, owner, blocked member).
- **Name blocklist** (`voice_master_name_blocklist`) — per-guild lowercase patterns with who added them.
- **Guild config** — all `voice_master_*` keys in the shared `config` table.

Admin profile views are logged. Sleep-kick timers, empty-grace timers and claim-prompt timers are in-memory only (the latter two are re-derived on restart by reconciliation; sleep-kick timers are simply lost).
