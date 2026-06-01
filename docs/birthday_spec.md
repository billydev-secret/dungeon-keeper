# Birthday — Feature Spec

Self-service per-guild birthday tracker. Members set their own birthday (month + day, optional one-line "request"); the bot posts a configurable announcement in a chosen channel once per day at 00:00 UTC, mentioning each birthday-haver. Idempotent across restarts.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/birthday set` | Slash | Everyone (server only) | Open a modal: month (1–12), day (1–31), optional "birthday request" (≤100 chars) |
| `/birthday remove` | Slash | Everyone (server only) | Remove your own birthday |
| Birthday panel | Web (dashboard) | Admin | Configure the announcement channel and message template; preview the next 90 days of upcoming birthdays |

## Behaviour

### Setting / removing a birthday

`/birthday set` opens a modal. The bot validates that month is 1–12 and day is within that month's range (February is treated as 28 days — leap-day birthdays aren't representable). The optional request is stored as-is (trimmed) and shown in the announcement on a second line.

The set command stores the birthday for the current guild only — birthdays don't cross-pollinate between servers. A second call overwrites the existing entry.

`/birthday remove` deletes the stored birthday. Both commands are server-only — running them in a DM returns an ephemeral hint.

### Daily announcement

The bot computes the next 00:00 UTC, sleeps until then, and posts an announcement message in the configured channel for every member whose birthday matches today's UTC date. The announcement uses the configured template (default: `"Happy birthday, {mention}! 🎂"`). If the birthday-haver set a "birthday request", it's appended on a new line as `*Birthday request: …*`.

Each announcement only @-mentions the birthday-haver — `@everyone`, `@here`, and roles are never pinged.

### Startup catch-up

When the bot boots, it runs the daily pass once before sleeping until the next midnight. So a bot that booted at 00:05 UTC still announces today's birthdays. The catch-up is idempotent — a member is announced at most once per (guild, day), even across restarts.

### Timezone

All scheduling is in **UTC**. There is no per-guild timezone override (see Non-goals).

### Dashboard

The admin panel sets two values: the announcement channel and the message template (template's only variable is `{mention}`). It also exposes a calendar projection of upcoming birthdays for the next N days (default 90), sorted by days-until, resolved to member display names.

## Permissions

- **User-side**: `/birthday set` and `/birthday remove` are open to every member.
- **Dashboard**: admin only.
- **Bot-side**: **Send Messages** in the configured channel.

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

- **No per-guild timezone.** Everything is UTC.
- **No leap-day birthdays.** Feb 29 is rejected; members born then choose Feb 28 or Mar 1.
- **No age / year of birth.** Only month and day — keeps the feature low-PII.
- **No multi-channel announcements.** One channel per guild.
- **No retroactive announcements.** Bot offline at 00:00 UTC means today's birthdays only post on next boot; yesterday's are silently missed.
- **No DM notifications.** The message only goes to the announcement channel.
- **No reactions / interactive UI on the announcement.** Plain text + mention.
- **No moderation override.** Admins can't set or remove other members' birthdays through the bot.

## Configuration

| Key | Default | Purpose |
|---|---|---|
| `birthday_channel_id` | unset (no announcements) | Where the daily message posts |
| `birthday_message` | `"Happy birthday, {mention}! 🎂"` | Template. Only `{mention}` is substituted. Empty value rejected on save |

No per-guild timezone, role gating, or rate-limit knob.

## Stored data

- **Member birthdays** — per (guild, user): month, day, optional one-line request, who set it (always the user themselves), when it was last set.
- **Announcement log** — per (guild, user, date): a marker that the daily pass already posted, so restarts and catch-ups don't double-post.

No DMs, no PII beyond birthday + optional request. Announcement-log rows older than ~2 years are pure storage — there's no purge job today.
