# Birthday — Feature Spec

Self-service per-guild birthday tracker. Members set their own birthday (month + day, optional one-line "request"); the bot posts a configurable announcement in up to two chosen channels once per local day at 09:00 guild-local time, mentioning each birthday-haver. Announcements can optionally be pinned per channel, with an automatic next-day unpin. Idempotent across restarts.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/birthday set` | Slash | Everyone (server only) | Open a modal: month (1–12), day (1–31), optional "birthday request" (≤100 chars) |
| `/birthday remove` | Slash | Everyone (server only) | Remove your own birthday |
| Birthday panel | Web (dashboard) | Admin | Configure up to two announcement channels (each with its own message template and pin toggle); preview the next 90 days of upcoming birthdays |

## Behavior

### Setting / removing a birthday

`/birthday set` opens a modal with three fields:

```
/birthday set
  Month:            7
  Day:              15
  Birthday request: Ping me with cake reactions! 🍰
```

The bot validates that month is 1–12 and day is within that month's range (February is treated as 28 days — leap-day birthdays aren't representable). The optional request is stored as-is (trimmed) and substituted into the announcement via the `{request}` template placeholder.

The set command stores the birthday for the current guild only — birthdays don't cross-pollinate between servers. A second call overwrites the existing entry.

`/birthday remove` deletes the stored birthday. Both commands are server-only — running them in a DM returns an ephemeral hint.

### Daily announcement

The announcement loop ticks **hourly**. On each pass it computes every guild's local date and hour from its `tz_offset_hours` config (via `get_tz_offset_hours` — the same offset reports, games, and jail honor) and, once the local clock has reached **09:00**, posts an announcement in each configured channel for every member whose birthday matches today's local date. Each (guild, member, day) is announced at most once, so later ticks in the same day are no-ops.

Templates are per-channel. The default is:

```
Happy birthday, {mention}! 🎂
{request}
```

Placeholders: `{mention}` pings the member, `{name}` is their display name, and `{request}` is their optional birthday request (blank when unset — empty lines left behind by a blank `{request}` are stripped, so the default template degrades cleanly to a one-liner). A template that renders to nothing (e.g. just `{request}` with no request set) skips the send for that channel.

Each announcement only @-mentions the birthday-haver — `@everyone`, `@here`, and roles are never pinged.

### Second channel

A guild can announce in up to **two** channels: `birthday_channel_id` / `birthday_message` and `birthday_channel_id_2` / `birthday_message_2`. Each channel has its own template; the same member is announced in both channels on their day. Leaving the second channel unset keeps the classic single-channel behavior.

### Pinning

Each channel has an independent pin toggle (`birthday_pin` / `birthday_pin_2`). When on, the bot pins the announcement it just posted (requires **Manage Messages** in that channel) and records the pin. On the next local day's pass, pins recorded on a previous day are unpinned automatically — the cleanup runs whether or not anyone has a birthday today, so a pin from a quiet stretch still comes down. If the unpin fails (message deleted, permission lost), the failure is logged and the pin record is dropped anyway rather than retried forever.

### Startup catch-up

The loop runs a pass on boot before settling into its hourly cadence. So a bot that was offline at 09:00 local still announces today's birthdays on the first pass after it comes back — any tick later in the local day catches up. The catch-up is idempotent — a member is announced at most once per (guild, day), even across restarts.

### Timezone

Scheduling is guild-local: the 09:00 announce hour and the "one announcement per day" boundary both follow the guild's `tz_offset_hours` config. A guild with no offset row inherits the global default.

### Dashboard

The admin panel configures both announcement channels — for each, a channel dropdown, a message template with a live preview, and the pin toggle. It also exposes a calendar projection of upcoming birthdays for the next N days (default 90), sorted by days-until, resolved to member display names.

## Permissions

- **User-side**: `/birthday set` and `/birthday remove` are open to every member.
- **Dashboard**: admin only.
- **Bot-side**: **Send Messages** in each configured channel; **Manage Messages** there too when the pin toggle is on (pinning is silently skipped without it).

## User-visible errors

| When | The user sees |
|---|---|
| Run `/birthday set` or `/birthday remove` in a DM | "Set your birthday from inside a server, not a DM." / "Run this from inside a server, not a DM." |
| Month or day field isn't a number | "Month and day must be whole numbers." |
| Month outside 1–12 | "Month must be between 1 and 12." |
| Day too high for that month | "{Month} has at most N days." |
| `/birthday remove` with nothing stored | "You didn't have a birthday on file." |
| `/birthday set` succeeds | "Your birthday has been set to **{Month} {Day}**." |
| Admin saves an empty template | HTTP 400 "Message cannot be empty" |

The daily announcement loop is silent on failure — if the configured channel was deleted or the bot lost send perms, the loop just skips that guild and logs operator-side.

## Non-goals

- **No leap-day birthdays.** Feb 29 is rejected; members born then choose Feb 28 or Mar 1.
- **No age / year of birth.** Only month and day — keeps the feature low-PII.
- **No retroactive announcements.** A bot offline for the rest of the local day after 09:00 catches up on the next boot that same day; once the local day rolls over, yesterday's birthdays are silently missed.
- **No DM notifications.** The message only goes to the announcement channel.
- **No reactions / interactive UI on the announcement.** Plain text + mention.
- **No moderation override.** Admins can't set or remove other members' birthdays through the bot.

## Configuration

| Key | Default | Purpose |
|---|---|---|
| `birthday_channel_id` | unset (no announcements) | First announcement channel |
| `birthday_message` | `"Happy birthday, {mention}! 🎂\n{request}"` | First channel's template. `{mention}`, `{name}`, and `{request}` are substituted. Empty value rejected on save |
| `birthday_channel_id_2` | unset | Optional second announcement channel |
| `birthday_message_2` | same default | Second channel's template |
| `birthday_pin` / `birthday_pin_2` | off | Pin the announcement in that channel; auto-unpinned on the next local day |

The announce hour follows the guild's shared `tz_offset_hours` config (owned elsewhere — not a birthday-specific knob). No role gating or rate-limit knob.

## Stored data

- **Member birthdays** — per (guild, user): month, day, optional one-line request, who set it (always the user themselves), when it was last set.
- **Announcement log** — per (guild, user, date): a marker that the daily pass already posted, so restarts and catch-ups don't double-post.
- **Pin records** — per (guild, channel, message): the local date the announcement was pinned, so the next day's pass can unpin it. Rows are dropped once processed, unpinned or not.

No DMs, no PII beyond birthday + optional request. Announcement-log rows older than ~2 years are pure storage — there's no purge job today.
