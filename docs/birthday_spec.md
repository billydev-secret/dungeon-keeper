# Birthday — Feature Spec

Self-service per-guild birthday tracker. Members set their own birthday (month + day, optional one-line "request"); the bot posts a configurable announcement in any number of chosen channels once per local day at the guild's chosen local hour (09:00 by default), mentioning each birthday-haver. Announcements can optionally be pinned per channel, with an automatic next-day unpin. Idempotent across restarts.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/birthday set` | Slash | Everyone (server only) | Open a modal: month (1–12), day (1–31), optional "birthday request" (≤100 chars) |
| `/birthday remove` | Slash | Everyone (server only) | Remove your own birthday |
| Birthday panel | Web (dashboard) | Admin | Configure the announcement hour (its own Timing card) and any number of announcement channels, each its own card with its own message template and pin toggle |
| Birthday Calendar panel (Reports → Member Lists) | Web (dashboard) | Moderator | Browse upcoming birthdays over a selectable window (30/60/90/365 days, default 90), sorted by days-until |

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

The announcement loop ticks **hourly**. On each pass it computes every guild's local date and hour from its `tz_offset_hours` config (via `get_tz_offset_hours` — the same offset reports, games, and jail honor) and, once the local clock has reached the guild's **`birthday_announce_hour`** (0–23, default 9), posts an announcement in each configured channel for every member whose birthday matches today's local date. Each (guild, member, day) is announced at most once, so later ticks in the same day are no-ops.

Templates are per-channel. The default is:

```
Happy birthday, {mention}! 🎂
{request}
```

Placeholders: `{mention}` pings the member, `{name}` is their display name, and `{request}` is their optional birthday request (blank when unset — empty lines left behind by a blank `{request}` are stripped, so the default template degrades cleanly to a one-liner). A template that renders to nothing (e.g. just `{request}` with no request set) skips the send for that channel.

Each announcement only @-mentions the birthday-haver — `@everyone`, `@here`, and roles are never pinged.

### Announcement channels

A guild can announce in **any number** of channels — there's no fixed cap. Each configured channel is a row in the `birthday_channels` table (guild_id, channel_id, message, pin), added and removed one at a time from the dashboard. Each channel has its own template; a birthday-haver is announced in every configured channel on their day (an empty channel list means the guild posts no announcements at all). A guild can't configure the same channel twice — adding a channel that's already on the list edits that channel's existing row (message + pin) rather than creating a duplicate.

### Pinning

Each channel has its own pin toggle (the `pin` column on its `birthday_channels` row). When on, the bot pins the announcement it just posted (requires **Manage Messages** in that channel) and records the pin. On the next local day's pass, pins recorded on a previous day are unpinned automatically — the cleanup runs whether or not anyone has a birthday today, so a pin from a quiet stretch still comes down. If the unpin fails (message deleted, permission lost), the failure is logged and the pin record is dropped anyway rather than retried forever.

### Startup catch-up

The loop runs a pass on boot before settling into its hourly cadence. So a bot that was offline at the announce hour still announces today's birthdays on the first pass after it comes back — any tick later in the local day catches up. The catch-up is idempotent — a member is announced at most once per (guild, day), even across restarts.

### Timezone

Scheduling is guild-local: the announce hour (`birthday_announce_hour`, default 9) and the "one announcement per day" boundary both follow the guild's `tz_offset_hours` config. A guild with no offset row inherits the global default. A stored hour that isn't an integer in 0–23 falls back to 9 rather than stalling the loop.

### Dashboard

The Birthdays panel has three parts, each its own card:

- **Timing** — a 00:00–23:00 dropdown for the guild-local announcement hour, saved independently of the channel cards.
- **Announcement Channels** — one card per already-configured channel: a heading naming the channel, a message textarea with a live preview (substituting the viewing admin's own name and a sample request), the pin checkbox, and its own **Save** / **Remove Channel** buttons. Removing a channel asks for confirmation first. A guild with none configured sees "No channels are set up yet. Add your first one below." instead of an empty list.
- **Add a Channel** — a channel picker plus the same message/preview/pin fields, with an **Add Channel** button that appends a new card.

Every card is its own form: saving or removing one channel never touches unsaved edits sitting in another. A calendar preview of upcoming birthdays lives on a separate report page, **Birthday Calendar** (Reports → Member Lists), cross-linked from this panel — it isn't part of the Birthdays panel itself.

## Permissions

- **User-side**: `/birthday set` and `/birthday remove` are open to every member.
- **Dashboard**: the Birthdays settings panel (hour, channels, templates, pin toggles) is admin only to save; the Birthday Calendar report is moderator-level.
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
- **No retroactive announcements.** A bot offline for the rest of the local day after the announce hour catches up on the next boot that same day; once the local day rolls over, yesterday's birthdays are silently missed.
- **No DM notifications.** The message only goes to the announcement channel.
- **No reactions / interactive UI on the announcement.** Plain text + mention.
- **No moderation override.** Admins can't set or remove other members' birthdays through the bot.

## Configuration

### Announcement channels (`birthday_channels` table)

Since migration 200, announcement channels are rows in a `birthday_channels` table rather than flat config keys — the same "add any number of channels" shape `needle_channels` (Auto-Thread) uses.

| Column | Purpose |
|---|---|
| `id` | Autoincrement primary key |
| `guild_id` | Which guild this channel belongs to |
| `channel_id` | The channel to announce in |
| `message` | This channel's template. `{mention}`, `{name}`, and `{request}` are substituted; the save endpoint rejects an empty value. The dashboard's "Add a Channel" form pre-fills `"Happy birthday, {mention}! 🎂\n{request}"` |
| `pin` | Pin the announcement in this channel; auto-unpinned on the next local day |

`UNIQUE (guild_id, channel_id)` — a guild can list a given channel only once; adding it again from the dashboard updates that row instead of inserting a second one. A guild with no rows posts no announcements at all.

Migration 200 (2026-09-02) carried every guild's old channels across automatically, so no admin had to re-enter anything: the old `birthday_channel_id` / `birthday_message` / `birthday_pin` trio became one row, and `birthday_channel_id_2` / `birthday_message_2` / `birthday_pin_2` became a second, in that order. A guild that had only set the first slot got one row; a guild that had set neither got none. If both slots had pointed at the **same** channel, the table's `UNIQUE (guild_id, channel_id)` constraint meant only one row could exist for it — the first slot's message and pin won, and the second slot's were dropped as a duplicate insert. The six old config keys no longer exist; nothing reads them.

### Config key

| Key | Default | Purpose |
|---|---|---|
| `birthday_announce_hour` | `9` | Guild-local hour (0–23) announcements go out; out-of-range or non-numeric falls back to 9 |

The announce hour stayed a single guild-wide dial through migration 200 — it isn't per-channel. It's interpreted against the guild's shared `tz_offset_hours` config (owned elsewhere). No role gating or rate-limit knob.

## Stored data

- **Member birthdays** (`member_birthdays`) — per (guild, user): month, day, optional one-line request, who set it (always the user themselves), when it was last set. Unchanged by migration 200.
- **Announcement channels** (`birthday_channels`) — per (guild, channel): message template, pin toggle. This table names no member — it's guild configuration, not personal data, so it isn't a `docs/data_register.md` row; the birthdays themselves stay covered by that register's `member_birthdays` entry.
- **Announcement log** — per (guild, user, date): a marker that the daily pass already posted, so restarts and catch-ups don't double-post.
- **Pin records** — per (guild, channel, message): the local date the announcement was pinned, so the next day's pass can unpin it. Rows are dropped once processed, unpinned or not.

No DMs, no PII beyond birthday + optional request. Announcement-log rows older than ~2 years are pure storage — there's no purge job today.
