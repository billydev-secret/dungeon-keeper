# Embed & panel style guide (Reference)

Conventions for **bot-generated** Discord embeds, panels, and message copy.
These are already followed across the codebase — this doc just writes them down
so new surfaces stay consistent. (For how *members/mods* should format their own
server announcements, see `server_announcement_style.md`.)

## Color

- New embeds take their color from **`resolve_accent_color(db_path, guild)`** —
  the guild's brand accent. Don't hard-code a color. A builder that lives away
  from the guild/db (an `embeds.py` module) takes the resolved color as a
  `color=`/`accent` **param** and lets the cog resolve it; a hard-coded value as
  a `color is None` fallback is fine, an *un-overridable* hard-code is not.
- Keep **red / green / etc. only where the color *is* the information** — a
  deliberate, commented exception. The sanctioned semantic set is
  **green** = success / win / approved / credit, **red** = error / loss /
  denied / debit / danger, **blurple** = neutral / transfer / no-guild fallback,
  **orange-yellow** = warning / expired / caution. Anything outside that set,
  or a semantic color on a surface where the state isn't actually that state, is
  drift — use the accent. Example: the economy register's credit-green /
  debit-red / transfer-blurple.
- **Games follow the accent** (ruling 2026-07-21). A game embed is themed by its
  guild accent like everything else; only true **win = green / loss = red**
  (and warning-orange for expired/abandoned) stay semantic. The old per-phase
  palette (lobby-gold / active-blue / results / recap) is retired — a builder
  that hard-codes a phase color with no `color` param is the pattern to fix.
- **Per-domain identity palettes are a deliberate exception.** A few features
  carry a *fixed brand color* instead of the guild accent, as an intentional
  visual identity — centralized in **`services/embeds.py`** and always used via
  its named constant (never a raw hex literal copy). Sanctioned identities:
  **bios** ember (also dashboard-configurable), **wellness** green
  (`WELLNESS_PRIMARY`), the **moderation** palette (`MOD_JAIL` / `MOD_WARNING` /
  `MOD_SUCCESS` / `MOD_INFO` / `MOD_TICKET` / `MOD_POLICY`), **starboard** gold,
  **dm-perms** gold. A new feature does **not** get an identity color by
  default — use the accent; granting one is a deliberate choice recorded here,
  not something to invent per-embed.

## Currency vocabulary

- Render a coin amount as **`{settings.currency_emoji} **{n:,}** {unit}`** — the
  configured emoji, the number **bolded with a thousands separator**, and a unit
  that goes **singular at 1** (`currency_name` vs `currency_plural`, via the
  `_reward_text` / `_unit` helpers). Never a bare `500 coins`, never an
  always-plural `1 coins`, never an un-separated `1500`.
- The currency emoji is guild-configurable and may be a **custom** emoji, so it
  only renders in the **title / description / fields** — see Footers below.

## Ledger rows

- Any surface that lists `econ_ledger` rows (the register feed, `/bank wallet`)
  renders a row's `kind` through **`register.kind_display(kind)`** — the shared
  (glyph, human-label) map — never the raw snake_case `kind` string. One map
  owns the vocabulary; an unmapped kind degrades to 🪙 + a title-cased name.

## Footers & author names

- Footer and author text render as **plain text**: a custom emoji `<:name:id>`
  shows as its raw tag and markdown doesn't format. Keep custom/guild-settable
  emoji and `**bold**` in the title / description / fields, not the footer.
  **Unicode** emoji (🪙, 🔔) are fine in a footer.

## Titles & field glyphs

- Lead an embed **title** with a relevant emoji when its sibling cards in the
  same feature do — a glyph-rich card with a bare title reads as half-styled.
  Keep the glyph vocabulary consistent within a feature (one concept, one
  glyph). Glyph-lead **field names** on cards whose other fields are glyph-led.

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
