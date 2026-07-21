# Embed & panel style guide (Reference)

Conventions for **bot-generated** Discord embeds, panels, and message copy.
These are already followed across the codebase — this doc just writes them down
so new surfaces stay consistent. (For how *members/mods* should format their own
server announcements, see `server_announcement_style.md`.)

## Color

- New embeds take their color from **`resolve_accent_color(db_path, guild)`** —
  the guild's brand accent. Don't hard-code a color.
- Keep **red / green / etc. only where the color *is* the information** — a
  deliberate, commented exception. Examples: the economy register's
  credit-green / debit-red / transfer-neutral, a semantic "danger" confirm.
  If the color doesn't carry meaning, use the accent.

## Section spacing (breathing room)

- End each embed **field value** (and the description) with a zero-width blank
  line — `"…text\n​"` — so the next section heading isn't cramped against
  the previous value. The **last** field skips the trailing blank.
- Give a section heading breathing room *above* it this way rather than padding
  inside the value.

## Tables & column alignment

- Align columns with **fixed-width inline-code cells** (`` `…` ``) padded via
  `_pad` (clip-with-`…` + `ljust`). Discord renders inline code monospace, so
  columns line up.
- Keep **emoji, `**bold**`, and live `<t:…:R>` timestamps *outside* the
  backticks** — a fenced/inline code span freezes bold and swallows live
  timestamps. Pattern: `` `{label}` {emoji} **{value}** `` or
  `` `{padded cell}` {payload} ``.
- Prefer **one monospace cell per row** over two adjacent code spans — a single
  grey box keeps the grid tight (see the quest-board rows: `` `label  desc` pay ``,
  not `` `label` `desc` pay ``).
- Clip overflow inside a cell with a trailing `…`, don't let a long value blow
  the column width.

## User-supplied text

- Escape member text with **`discord.utils.escape_markdown`** before putting it
  in an embed/description, so `*`, `_`, `` ` `` don't reformat the panel.
- Escape mentions (**`escape_mentions`**) in any `content=` that isn't
  mention-allow-listed, so a pasted `@everyone` / `<@id>` can't ping.

## Mentions & pings

- Set **`allowed_mentions`** explicitly. Default to
  `discord.AllowedMentions.none()`; when you *do* want a ping, allow-list
  **exactly** the role/user intended (e.g. the weekly flip pings only the
  economy game role via `AllowedMentions(roles=[Object(id=…)])`), never rely on
  the raw text.

## Buttons & persistent views

- Consistent button **shapes/sizes**; collapse overlapping toggles into one
  multi-state dial rather than several buttons (see Voice Master's access dial).
- A view that must survive a restart uses a **stable static `custom_id` +
  `timeout=None`**, is **re-registered at cog load** (`bot.add_view(...)` /
  `add_dynamic_items(...)`), and its callback looks the cog up by name so it
  **degrades to an ephemeral note, never a dead button**, if the cog is
  mid-reload.

## Sticky panels

- A panel that should stay the channel's last message re-sticks by **delete +
  repost** on member activity — debounced, under a per-guild lock, recording the
  new message id *before* the DB save so the repost's own gateway event is
  skipped (guide + leaderboard panels share this pattern).

## Reach & privacy

- **Member self-service replies are ephemeral** by default; go public only for
  shared state (a leaderboard, an announcement).
- Recurring economy DMs gate on the opt-in game role
  (`notify_member(require_game_role=True)`); don't DM members who didn't opt in.

## Dashboard (JS) specifics

- Snowflake ids cross the dashboard boundary as **JSON strings both ways** — a
  bare number > 2^53 is silently rounded. Never `parseInt` an id.
- No Node on the box: syntax-check panel JS with the **`gjs` `Reflect.parse`**
  one-liner (module mode for ES-module panels). Static-asset cache-busting is
  automatic per boot; JS edits show after the next restart.
