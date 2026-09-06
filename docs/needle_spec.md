# Needle (Auto-Thread) — Feature Spec

Automatically spawns a thread from each new message in designated text channels (inspired by [discord-needle](https://github.com/MarcusOtter/discord-needle)). Each thread gets a configurable name and an optional pinned welcome message with **Archive thread** / **Edit title** buttons, and the starter message can be given a fixed set of decorative reactions. Keeps Q&A and discussion channels tidy at a glance.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/close` | Slash | Thread owner or Manage Threads | Archive the current thread (also unlocks it) |
| `/title name:<name>` | Slash | Thread owner or Manage Threads | Rename the current thread (max 100 chars) |

Both work only inside a thread. The welcome-message buttons duplicate these commands with the same permission check (Edit title opens a modal). Channel and global configuration has **no slash commands** — it lives entirely in the web dashboard.

## Behavior

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

### Auto-reactions

Channels can list **default reactions** (comma-separated emoji) that the bot
adds to every new message it threads. They are a **cue and nothing more** — a
nudge to vote, or just something to react to. The bot adds them once and never
looks at them again: it does not read them back, swap them, or take them off,
and no bot behaviour anywhere depends on which of them are present.

That is the whole of Needle's reaction surface. Until 2026-09-06 there was also
a three-marker **status** machine (🔵 open / ✅ archived / 🔒 locked, swapped on
`on_thread_update`, with an option to clear the open marker on the first reply),
which made a reaction something the bot asserted and maintained. It was removed
deliberately — see migration 215. A reaction on a Needle-threaded post now
carries no meaning the bot put there.

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
| Non-owner without Manage Threads tries `/close` or the **Archive Thread** button | "Only the thread owner or a moderator can close this thread." / "...archive this thread." |
| Non-owner without Manage Threads tries `/title` or the **Edit Title** button | "Only the thread owner or a moderator can rename this thread." |
| Empty title submitted | "Title can't be empty." |

Thread-creation and welcome-message failures are logged as warnings; most reaction failures are silently ignored (only a failed default reaction is logged, at debug level). Either way, members see nothing.

## Non-goals

- No forum, voice, or announcement channel support — only regular text channels.
- No thread-status markers. Reactions Needle adds are decoration; nothing reads them.
- No slash-command configuration; setup is dashboard-only.
- No retroactive threading of messages sent before a channel was configured.

## Configuration

All configuration is per-guild via the web dashboard (admin permission required):

- **Per channel** (`PUT /config/needle/{channel_id}`, `DELETE` to remove): title style + custom title, include bots, slowmode (0–21600 s), delete behavior, reply type + custom reply, default reactions.
- **Guild-wide** (`PUT /config/needle/settings`): the default reply template (default "Thread created by $USER in $CHANNEL"), used by any channel whose reply type is `default`.

## Stored data

- `needle_channels` table — one row per configured channel: `(guild_id, channel_id)` primary key plus the per-channel settings above.
- Guild config key `needle_default_reply` in the shared config store.

No per-thread state is stored, and nothing in Discord is treated as state either
— since the status markers were removed there is no reaction the bot reads back.
