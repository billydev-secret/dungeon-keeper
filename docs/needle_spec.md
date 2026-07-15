# Needle (Auto-Thread) — Feature Spec

Automatically spawns a thread from each new message in designated text channels (inspired by [discord-needle](https://github.com/MarcusOtter/discord-needle)). Each thread gets a configurable name, an optional pinned welcome message with **Archive thread** / **Edit title** buttons, and optional status reactions on the starter message showing whether the thread is unanswered, archived, or locked. Keeps Q&A and discussion channels tidy at a glance.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/close` | Slash | Thread owner or Manage Threads | Archive the current thread (also unlocks it) |
| `/title name:<name>` | Slash | Thread owner or Manage Threads | Rename the current thread (max 100 chars) |

Both work only inside a thread. The welcome-message buttons duplicate these commands with the same permission check (Edit title opens a modal). Channel and global configuration has **no slash commands** — it lives entirely in the web dashboard.

## Behaviour

### Thread creation
On every new message in a configured text channel (system messages, the bot's own messages, and — unless *include bots* is on — other bots are skipped), Needle creates a thread on the message with a 24-hour auto-archive duration and the configured slowmode. The thread name comes from the channel's title style:

| `title_type` | Thread name |
|---|---|
| `first_fifty` (default) | First 50 characters of the message, newlines flattened |
| `first_line` | First line of the message |
| `user_date` | `{display name} ({YYYY-MM-DD})` |
| `custom` | Custom template; supports `$USER` and `$DATE` |

Names are clamped to 100 characters; an empty result becomes "New Thread". If the bot lacks permission or Discord rejects the thread, the failure is logged and nothing else happens.

### Welcome message
Unless the channel's reply type is `none`, the bot posts the reply template (`custom` → per-channel text, `default` → the guild-wide template) into the new thread with the persistent Archive/Edit-title buttons. Templates support `$USER`, `$CHANNEL`, and `$THREAD`. An empty template posts nothing. With Manage Messages the bot pins the welcome message and deletes its own "pinned a message" system notice.

### Status reactions
When *status reactions* is on for the channel:

- The starter message gets the **unanswered** emoji when the thread is created.
- When the thread is archived or locked, all bot status reactions are cleared and the **archived** or **locked** emoji is added (locked wins if both changed). Unarchiving just clears them.
- If *archive immediately* is also on, the unanswered emoji is removed as soon as someone other than the message author replies in the thread. Despite the name, nothing is archived — this flag only gates the reaction removal.

Channels can also list **default reactions** (comma-separated emojis) added to every new message regardless of status reactions.

### Deleted starter messages
When a message that owns a thread is deleted, the channel's `delete_behavior` decides the thread's fate:

| `delete_behavior` | Effect |
|---|---|
| `archive_if_empty` (default) | Delete the thread if its recent history contains only the OP and the bot; otherwise archive it |
| `archive` | Archive the thread |
| `delete` | Delete the thread (falls back to archiving if the bot lacks Manage Threads) |
| `nothing` | Leave the thread alone |

## User-visible errors

| When | The user sees |
|---|---|
| `/close`, `/title`, or a button used outside a thread | "This command can only be used inside a thread." / "Not in a thread." |
| Non-owner without Manage Threads tries to close/rename | "Only the thread owner or a moderator can close/rename this thread." |
| Empty title submitted | "Title can't be empty." |

Thread-creation, reaction, and welcome-message failures are logged silently — members see nothing.

## Non-goals

- No forum, voice, or announcement channel support — only regular text channels.
- No automatic archiving on reply; *archive immediately* only removes the unanswered reaction.
- No slash-command configuration; setup is dashboard-only.
- No retroactive threading of messages sent before a channel was configured.

## Configuration

All configuration is per-guild via the web dashboard (admin permission required):

- **Per channel** (`PUT /config/needle/{channel_id}`, `DELETE` to remove): title style + custom title, include bots, slowmode (0–21600 s), delete behavior, reply type + custom reply, status reactions, archive immediately, default reactions.
- **Guild-wide** (`PUT /config/needle/settings`): the three status emojis — unanswered (default 🔵), archived (default ✅), locked (default 🔒) — and the default reply template (default "Thread created by $USER in $CHANNEL").

## Stored data

- `needle_channels` table — one row per configured channel: `(guild_id, channel_id)` primary key plus the per-channel settings above.
- Guild config keys `needle_emoji_unanswered`, `needle_emoji_archived`, `needle_emoji_locked`, `needle_default_reply` in the shared config store.

No per-thread state is stored; thread status lives entirely in Discord (reactions and thread flags).
