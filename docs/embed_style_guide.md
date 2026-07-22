# Embed, panel & copy style guide (Reference)

Conventions for **bot-generated** Discord embeds, panels, message copy, and
dashboard UI text. Most of these are already followed across the codebase —
this doc writes them down so new surfaces stay consistent; a few are explicit
rulings (dated) that retire older drift. (For how *members/mods* should format
their own server announcements, see `server_announcement_style.md`.)

Two register notes up front:

- **Titles/labels vs prose.** Casing rules for *titles and labels* (embed
  titles, field names, button labels, modal titles, dashboard headings/buttons)
  are different from *prose* (descriptions, error strings, DMs, toasts). Don't
  apply one register's rule to the other.
- **Games are playful, utilities are calm.** Games copy may use exclamation
  marks and an excited voice (`"Start a Would You Rather game!"`); moderation,
  economy, and admin surfaces stay measured. This split applies to *voice*,
  not to structure — structure rules (casing, colors, ❌ prefixes) are global.

## Color

- New embeds take their color from **`resolve_accent_color(db_path, guild)`** —
  the guild's brand accent. Don't hard-code a color. A builder that lives away
  from the guild/db (an `embeds.py` module) takes the resolved color as a
  `color=`/`accent` **param** and lets the cog resolve it; a hard-coded value as
  a `color is None` fallback is fine, an *un-overridable* hard-code is not.
  (Also: the kwarg is `color=`, not `colour=` — 551 vs 1 in the codebase.)
- Keep **red / green / etc. only where the color *is* the information** — a
  deliberate, commented exception. The sanctioned semantic set is
  **green** = success / win / approved / credit, **red** = error / loss /
  denied / debit / danger, **blurple** = neutral / transfer / no-guild fallback,
  **orange-yellow** = warning / expired / caution. Anything outside that set,
  or a semantic color on a surface where the state isn't actually that state, is
  drift — use the accent. Example: the economy register's credit-green /
  debit-red / transfer-blurple.
- **One canonical semantic pair** (ruling 2026-07-21): semantic green/red are
  **`COLOR_GREEN` (0x23A55A) / `COLOR_RED` (0xF23F43)** in
  `bot_modules/services/embeds.py` — Discord's own success/danger shades.
  `MOD_SUCCESS`/`MOD_JAIL` and the games `SUCCESS_COLOR`/`ERROR_COLOR` should
  become aliases of these, not independent shades; never introduce a **new**
  green/red hex literal. (Today three greens and three reds coexist — collapse
  them when touching the module.)
- **Games follow the accent** (ruling 2026-07-21). A game embed is themed by its
  guild accent like everything else; only true **win = green / loss = red**
  (and warning-orange for expired/abandoned) stay semantic. The old per-phase
  palette (lobby-gold / active-blue / results / recap in `games/constants.py`)
  is retired — a builder that hard-codes a phase color with no `color` param is
  the pattern to fix. One narrow extra semantic is allowed: a **content-type
  affordance** where the color itself tells the player what kind of card they're
  looking at — Truth vs Dare card coding (Truth or Dare, FFA) — since there the
  color *is* information, the same test win/loss passes. Everything else in a
  game follows the accent.
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

## Card anatomy

Which embed slot does which job:

- **`title` = the event or thing** ("Member Jailed", "Perk Shop", "Final
  Results"). **`set_author` = the person the card is about** — bios, wallet
  header, starboard's original author, a DM-request's requester, music's
  requester. Never both fighting over the same job; a card about a member puts
  the member in `author` (name + avatar icon) and keeps the title for what
  happened.
- **Thumbnail semantics**: currency icon on money cards, member avatar on
  person cards, guild icon on guild-level panels. **`set_image`** is reserved
  for real content renders (`attachment://` images — quote cards, guess
  puzzles), not decoration.
- **Separators**: **em-dash `" — "` in titles** ("Grant Audit — {label}"),
  **middot `" • "` (single-spaced) in footers**. Don't mix in `·` vs `•` or
  double-spaced variants; recase strays when touching the module.
- Lead an embed **title** with a relevant emoji when its sibling cards in the
  same feature do — a glyph-rich card with a bare title reads as half-styled.
  Keep the glyph vocabulary consistent within a feature (one concept, one
  glyph). Glyph-lead **field names** on cards whose other fields are glyph-led
  (`"📋 Queue"`, `"✅ Yes" / "❌ No" / "➖ Abstain"`).

## Titles, labels & casing (ruling 2026-07-21: Title Case)

- **Titles and labels use Title Case** everywhere: embed titles ("Perk Shop",
  "Daily Streak", "Hot Takes — Final Results"), button labels ("Submit Fills",
  "Keep Playing", "Yes, End Game"), modal titles ("Add Your Sentence", "Send
  Anonymous Whisper"), field names, dashboard headings and buttons ("Save
  Settings", "Run Test").
- The games **ALL-CAPS register is retired** (`"HOT TAKES — FINAL RESULTS"`,
  `"C L A P B A C K"`), as is economy's sentence-case register ("Perk shop").
  Don't mass-rename; recase when touching a builder.
- **Prose stays sentence-style**: descriptions, error strings, DMs, dashboard
  toasts ("Quest deleted"), command descriptions. Title Case is for *labels*,
  not sentences.

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

## Footers

- Footer and author text render as **plain text**: a custom emoji `<:name:id>`
  shows as its raw tag and markdown doesn't format. Keep custom/guild-settable
  emoji and `**bold**` in the title / description / fields, not the footer.
  **Unicode** emoji (🪙, 🔔) are fine in a footer — route guild-settable ones
  through the **`footer_emoji()`** helper in `services/embeds.py` (it drops
  custom emoji rather than showing a raw tag); adopt it rather than re-checking
  inline.
- A footer does **one** of these jobs — pick one, don't stack:
  - **Next-step hint**: "Use /policy vote to start the formal vote when ready."
  - **Attribution**: "Granted by {actor}", "Sponsored by {sponsor}",
    "Host: {host} • Need {n}+ players to start."
  - **Freshness / live status**: "⚡ Live — updates within ~2 min of activity"
  - **Game signature** (games only): `{GAME_ICON} Game Name • extra` — every
    game card signs itself so screenshots stay attributable.
  - **Pagination** (see Empty states & pagination).

## Timestamps

- Inline times are f-string Discord timestamps, **relative by default**:
  `<t:{ts}:R>` ("in 3 hours"). Use absolute styles (`:f`/`:D`/`:t`) only when
  the wall-clock time is the point (a scheduled event's start). This is
  hand-rolled everywhere; `discord.utils.format_dt` is unused — either is fine,
  but don't invent a third form.
- **`embed.timestamp`**: set it on **record cards** — things someone scrolls
  back to as an audit trail (jail actions, starboard entries, grant audit,
  whisper logs, leaderboard refresh stamps). Skip it on transient/ephemeral
  panels and live game cards, where "now" is implied. (Convention inferred
  2026-07-21 from the modules that do set it; new record-ish cards should.)

## Fields & layout

- **Small facts go in inline triples** — three `inline=True` fields render as
  one row (Host / Hot Seat / Mode; Yes / No / Abstain tallies). Anything
  list-like, long, or sentence-shaped is `inline=False`.
- Keep cards to a handful of fields; a card that wants ten fields is usually
  two cards or a table.

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

## Progress bars

- New progress bars use the **`▰▱` vocabulary** with the economy format:
  `{bar} {current:,}/{target:,}` — it renders cleanly without code spans.
- Existing `█░` bars (games `live_bar.py`, chicken, pressure cooker) and the
  bracket/pipe wrappers around them are legacy; converge when touching, don't
  add a fourth vocabulary.
- A bar with named milestone regions (community goals' 40/70/100% tiers,
  `leaderboard.community_progress_bar`) divides the same `▰▱` bar into
  segments with a `┃` divider at each threshold — no new fill characters, no
  color; the tier lines underneath still carry the numbers.

## Empty states & pagination

- Empty states are a **short plain sentence, no emoji**: "No passed policies
  yet." / "No active game in this channel." Add a nudge when there's an obvious
  next step ("No role menus yet. Create one to get started."). Same pattern on
  the dashboard ("No verdicts recorded yet.", "No tickets match this filter.").
- Pagination lives in the **footer** as **`Page {n}/{total}`** (1-based) —
  optionally `• {context}` after it. Not "Pool page 1 of 3", not
  pagination-in-title.
- Truncated lists say what's hidden and where the rest lives:
  "Showing 10 of 34. See dashboard for full list."

## Errors, denials & confirmations (ruling 2026-07-21: ❌ everywhere)

- Member-facing **error and denial replies open with `❌ `** — all features,
  not just games. `✅ ` prefixes success acks ("✅ You joined!");
  `⚠️ ` is for non-blocking warnings. These are **ephemeral plain
  `content=` strings**, not embeds.
- **One shared no-permission string** — don't paste a new variant (the
  codebase has one canonical sentence copied ~19× plus four mutations; converge
  on a shared constant when touching those cogs). Role-specific denials name
  the role: "❌ Only the host or a mod can start."
- **Say how to fix it** when there's a fix: "You need the Whisper role —
  use `/whisper optin` to join." / "…ask a mod to pick the notification role on
  the dashboard." A denial that just says no is a dead end.
- The bot speaks **first person about its own failures**: "I don't have
  permission to post in {channel}.", "I couldn't start the game here — please
  grant me {perms}."
- "Please" appears **only in error-recovery** sentences ("Something went wrong.
  Please try again."), never in happy-path copy.

## Voice & terminology

- **Second person** ("you") to the member; third person only in broadcast
  cards ("{display} just hit a **7-day streak**").
- **Contractions** ("don't", "can't") — the uncontracted forms read robotic.
  Terse validation strings may keep "cannot" ("This cannot be undone.").
- **"server", never "guild"** in member-facing copy ("guild" is Discord API
  jargon; two known leaks: `xp_cog.py`, `guess_cog.py`). Dashboard/admin
  surfaces should also prefer "server" in new copy.
- Currency, quest, and perk vocabulary route through settings/shared maps (see
  Currency vocabulary, Ledger rows) — never hard-code "coins" user-facing.
- **DMs open with the point** — no greeting, no sign-off ("Payment for your
  **{perk}** perk failed — you have {h}h of grace…"). Wellness's 💚 motif is a
  sanctioned per-feature voice, not a template. Recurring DMs mention the
  opt-out ("Toggle it off any time — it only changes your DMs"); rental/billing
  DMs are exempt by design.
- Unicode **`…`**, not `...`, everywhere user-facing (placeholders, progress
  states, clipped cells).

## Slash commands

- Names are lowercase; **prefer a single word** (`/bank`, `/quote`); when two
  words are unavoidable, **snake_case** (`steal_emoji`, `xp_give`) — ruling
  2026-07-21 for *future* commands; existing kebab/concatenated names keep
  their muscle memory.
- **Descriptions are one sentence, verb-first, with a terminal period**:
  "Create or update your bio." Games may use their register ("Start a Clapback
  game — comedy head-to-head!"). Command **groups get a real description too**
  (several ship empty today — fix when touching).
- Parameter `describe()` strings: same rule, terminal period.
- State gating **in the sentence**, not a prefix: "End the active game in this
  channel (host or mod)." — not "(Mod) End…".

## Buttons, modals & selects

- Button labels: **Title Case, 1–3 words, optional leading emoji**
  ("📝 Submit Fills"). Confirm flows keep the "Yes, …" comma form, recased
  ("Yes, End Game"). Cancel is plain **"Cancel"** — no ✕/✗ glyph.
- Consistent button **shapes/sizes**; collapse overlapping toggles into one
  multi-state dial rather than several buttons (see Voice Master's access dial).
- Modal titles: Title Case. Modal field labels are **terse noun phrases with a
  parenthetical hint**: "Reason (optional)", "User limit (0–99, 0 = no cap)".
- Select placeholders: imperative **"Pick …" + unicode ellipsis**
  ("Pick the sender…", "Pick a member to invite…"). "Pick" over "Select".

## Persistent views

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

## Mentions, pings & user-supplied text

- Escape member text with **`discord.utils.escape_markdown`** before putting it
  in an embed/description, so `*`, `_`, `` ` `` don't reformat the panel.
- Escape mentions (**`escape_mentions`**) in any `content=` that isn't
  mention-allow-listed, so a pasted `@everyone` / `<@id>` can't ping.
- Set **`allowed_mentions`** explicitly. Default to
  `discord.AllowedMentions.none()`; when you *do* want a ping, allow-list
  **exactly** the role/user intended (e.g. the weekly flip pings only the
  economy game role via `AllowedMentions(roles=[Object(id=…)])`), never rely on
  the raw text.

## Builder conventions

- Embed construction lives in a **per-feature `embeds.py`** with **pure
  `build_*` functions**: plain dicts/primitives in, `discord.Embed` out, no
  Discord/network calls — testable offline. Name-lookup needs come in as a
  resolver callable. Cogs stay thin; a cog building eight embeds inline
  (today's `economy_cog.py`) is the anti-pattern.
- Colors come in as a param (see Color); a builder never resolves the accent
  itself.

## Dashboard (JS) specifics

- **Headings and buttons: Title Case** ("Weekly Reports", "Save Settings",
  "Run Test") — matches the bot-side ruling; sentence-case strays recase when
  touched.
- **Toasts: terse past-tense, no punctuation** — "Saved", "Quest deleted";
  failures as "Save failed: {detail}". Progress states use unicode ellipsis
  ("Loading…", "Saving…").
- Destructive confirms go through **`confirmDialog`** (question + one
  consequence sentence, `danger` styling), never native `confirm()`:
  "Retire this field? Old bios keep their stored values…"
- Snowflake ids cross the dashboard boundary as **JSON strings both ways** — a
  bare number > 2^53 is silently rounded. Never `parseInt` an id.
- No Node on the box: syntax-check panel JS with the **`gjs` `Reflect.parse`**
  one-liner (module mode for ES-module panels). Static-asset cache-busting is
  automatic per boot; JS edits show after the next restart.

## Known drift (converge when touching, don't mass-fix)

- Games ALL-CAPS titles and economy sentence-case titles → Title Case.
- Three green/red constant families → alias to `COLOR_GREEN`/`COLOR_RED`.
- Non-game error strings missing the `❌ ` prefix; ~19 pasted no-permission
  variants → shared constant.
- "guild" in member-facing errors (`xp_cog.py:209`, `guess_cog.py:1805`).
- `█░`/bracket/pipe progress bars → `▰▱`.
- Separator strays: `·` in titles (voice master), double-spaced `•` footers.
- `footer_emoji()` adoption outside economy/starboard.
- Pagination wording variants; ASCII `...` placeholders; "Select" placeholders.
- One `colour=` kwarg; one "You do not have permission…" uncontracted string.
