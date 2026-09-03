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

- New embeds take their color from **`safe_resolve_accent(source, guild)`**
  (`bot_modules.core.branding`) — the guild's brand accent. `source` is
  whatever the caller has in hand: a bot, an AppContext, or a `db_path`.
  Don't hard-code a color.

  **Never call `resolve_accent_color` directly.** It reads the branding table
  and, in avatar mode, fetches the bot avatar over HTTP, so it *raises* — into
  a live game, a background loop or an HTTP handler, depending on where you
  called it. `safe_resolve_accent` wraps it and returns `None` (or `default`)
  on any failure instead. It returns `discord.Color | None`, so pass
  `default=DEFAULT_ACCENT_COLOR` where a non-optional `Color` is required.
  `tests/test_branding.py` fails the suite on any direct call outside
  `core/branding.py` itself. A builder that lives away
  from the guild/db (an `embeds.py` module) takes the resolved color as a
  `color=`/`accent` **param** and lets the cog resolve it; a hard-coded value as
  a `color is None` fallback is fine, an *un-overridable* hard-code is not.
  (Also: the kwarg is `color=`, not `colour=` — the codebase has fully
  converged on `color=`.)
  This contract is enforced by `tests/test_embed_accent_contract.py` — a new
  builder adds one `case()` row there (passthrough + fallback), never
  per-file accent tests.
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
  `MOD_SUCCESS` and the games `SUCCESS_COLOR`/`ERROR_COLOR` are now aliases of
  these; `MOD_JAIL` still carries its own independent red shade — collapse it
  (and any other literal copy) when touching the module. Never introduce a
  **new** green/red hex literal.
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
- **`/help` follows the accent** (ruling 2026-07-27, #76). Its section pages
  carried a per-section palette (`mod_cog._SECTION_EMOJI`, née `_SECTION_META`:
  Economy gold, Moderation red, Voice green…). That palette is retired — the
  section **emoji** is the wayfinding cue, the color is the guild's branding.
  Sections without a resolvable guild (a DM) share one `_NO_ACCENT_FALLBACK`,
  never a spread of colors. Same for the privacy deletion progress cards,
  which were a module-level blurple constant with no accent path
  (`_PROGRESS_FALLBACK` is now only the no-guild default); the "Deletion
  complete" summary stays semantic green.
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
- **A masked link never renders in a `title` or a field `name`** (ruling
  2026-09-03). `[text](url)` resolves in a **description** or a **field value**
  and nowhere else, so a title written that way shows the reader the literal
  `[Song Name](https://…)` — which is exactly how the music now-playing card
  shipped. The slot that makes a title clickable is **`embed.url`**; set the
  title as plain text and hand the link to that. The Footers rule below is the
  sibling of this one: footer and author text render as plain text *entirely*,
  markdown included. (Text formatting like `**bold**` behaves differently again
  per slot — it works in a field name, not in a footer. If a card's meaning
  depends on formatting rendering in a title, don't: put it in the description.)
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
- A footer does **one** of these jobs — pick one, don't stack. "One job" is
  about not cramming two unrelated purposes into a footer; a single thought
  carried in two `" • "`-joined clauses is still one job (ruling 2026-09-03),
  which is why "Host: {host} • Need {n}+ players to start." and the game
  signature's `• extra` form are both fine. Three separate reviews declined to
  file footer findings because the rule read as banning its own examples:
  - **Next-step hint**: "Use /policy vote to start the formal vote when ready."
  - **Attribution**: "Granted by {actor}", "Sponsored by {sponsor}",
    "Host: {host} • Need {n}+ players to start."
  - **Freshness / live status**: "⚡ Live — updates within ~2 min of activity"
  - **Game signature** (games only): `{GAME_ICON} Game Name • extra`. This is a
    **requirement, not an option** (ruling 2026-09-03): a public game card
    carries its signature unless its footer is genuinely doing one of the other
    jobs, so screenshots stay attributable. It previously read as a description
    of one of five things a footer *may* do, which meant an unsigned card broke
    no rule — and most casino, quickdraw, chicken, musical-chairs, hot-potato
    and duels cards carry no footer at all. Those are a known backlog, not a
    licence: sign a card when you touch it.
  - **Pagination** (see Empty states & pagination).
  - **Privacy / retention notice** (ruling 2026-09-03) — telling a member what
    happens to what they just wrote ("When this ticket is closed, the
    conversation is archived to the moderator transcript channel."). It is the
    one job that may sit under a footer already doing another, because a
    data-handling notice outranks tidiness. Don't let a mechanical sweep
    "converge" one of these away.

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
- Builders that assemble a `discord.Embed` directly should call
  `apply_section_spacing(embed)` (`bot_modules.core.branding`, exposing
  `SECTION_SPACER`) once after adding fields — it appends the spacer to every
  stacked field but the last, idempotently. String-layer builders that return
  `(name, value)` pairs (login digest, weekly leaderboard) stay
  Discord-object-free and append the same `"\n​"` spacer themselves.
- **`inline=True` fields are skipped** (ruling 2026-09-03). The spacer stops a
  section heading hugging the value above it, and an inline field has no
  heading below it — it sits *beside* its neighbours, and Discord starts a
  fresh row for whatever follows the group. Spacing one only makes its box
  taller, which on a three-across row is dead height on every card. Because the
  helper handles that itself, **every** multi-field builder calls it; there is
  no "this card has triples so leave it alone" exemption to reason about.
- A field already at Discord's **1024-char value cap is left alone**. Plenty of
  builders fill a field right to that line (`fit_lines`, a raw `value[:1024]`
  slice), and the two extra characters would make Discord reject the whole
  embed — losing the card entirely to buy two pixels of air.

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
  `{bar} {current:,}/{target:,}`, drawn from `core/meters.py` — never
  hand-rolled. `meters.fill()` returns the raw glyphs; `meters.mono()` wraps.
- **Always render a bar inside a code span.** `▰` and `▱` do *not* share an
  advance width in Discord's proportional font stack — the outlined glyph is
  wider — so a bare bar gets visibly **shorter as it fills** even though its
  character count never changes. Side by side (game vote options, goal lists)
  that reads as a bug. A code span forces monospace and fixes it. The one
  exception: a meter being composed into a code span the caller already
  builds (the `/bank quests` table cell, the login digest) stays raw —
  backticks don't nest. `progress_bar(..., code=False)` is that path.
- Markdown does not render inside a code span, so anything that needs bold
  (the casino's `**62%** over`) stays *outside* the backticks.
- Existing `█░` bars and the bracket/pipe wrappers around them are legacy;
  converge when touching, don't add a fourth vocabulary.
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
- **One shared no-permission string** — `NO_PERMISSION` in
  `services/replies.py`, imported at ~28 call sites; don't paste a new literal
  copy of the sentence. Role-specific denials still write their own sentence
  naming the role: "❌ Only the host or a mod can start."
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
  jargon; one known leak: `guess_cog.py`'s "manage_guild permission" denial).
  Dashboard/admin surfaces should also prefer "server" in new copy.
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
  multi-state dial rather than several buttons (see Voice Control's access dial).
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

## DM branding

A DM has no guild of its own. The bot's **username and avatar are global** —
Discord exposes no per-guild identity in a DM channel, so a member who shares
two servers with the bot sees the same sender either way. Nothing in the
codebase can change that; don't accept a request that assumes otherwise.
What *is* per-guild is the message body.

- Send member DMs through **`services/dm_branding.py`**, not a local
  `user.send` wrapper. Four near-identical `_try_dm` helpers had accumulated
  before it existed; there should not be a fifth.
  - **`send_branded_dm(user, db_path=…, guild=…, embed=…)`** — the common
    case. Resolves the accent, attributes the guild, sends, and returns
    `None` on a closed DM (callers that roll back DB state test for `None`).
  - **`brand_dm_embed(embed, …)`** — pure and synchronous, for callers that
    already own a delivery policy. `economy_service.notify_member` has mute
    prefs, an opt-in role gate, and a bank-channel fallback; it brands with
    this and keeps its own send path.
- **Attribution defaults to the footer**, appended after any footer the
  builder already set (` • ` separator). Author placement
  (`placement=ATTRIBUTION_AUTHOR`) is opt-in, because several DM embeds use
  the author slot for something better — `dm_perms` puts the *requesting
  member* there.
- **`keep_color=True`** attributes without recoloring. Use it where the color
  is semantic rather than decorative (jail's release notice is green because
  it's good news) — see Color.
- Branding **never costs a delivery**: a failed accent lookup degrades to the
  DM default, and a guild object missing `name`/`icon` just skips
  attribution. A content-only DM is passed through unbranded — an accent
  needs an embed to live in, so convert to an embed if it should carry one.

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

## Naming members in embeds (never `<@id>`)

**Inside an embed, a `<@id>` is not a name — it is a number.** Discord's
servers do nothing to a mention; the *reading* client resolves it from its own
member cache, so any viewer who has not seen that user sees a bare id. This has
been diagnosed and fixed three times now (Whisper `aa7ec8cb`, Casino
`0ae70448`, Guess) and it always looks the same in the wild: a public card
showing digits to everyone but the person it names. Mentions in **message
`content=`** are fine — those *are* resolved server-side, and that's where a
deliberate ping belongs.

- **Resolve through `services/name_resolver.py`.** `build_name_fn(guild=…,
  db_path=…, guild_id=…, user_ids=…)` returns a sync `NameFn`; the chain is
  live member cache → `known_users.display_name` → `known_users.username` →
  `<@id>`. The cache leads (with `intents.members` on it is complete and
  nickname-fresh); the table covers the one case it structurally cannot,
  members who have **left**. Names come back markdown-escaped.
- **Build once, reuse.** `build_name_fn` only queries for ids the member cache
  misses, so a roster of present members costs no I/O — but it is async, so a
  long-lived view prefetches once at mount and the sync builders close over the
  result, rather than re-resolving per render.
- **Builders take a `name_fn`, defaulting to `mention`.** The default keeps an
  un-wired caller rendering instead of crashing — which is exactly why the
  wiring needs its own guard: pair the builder table with an **AST test that
  every render site passes a resolver** (`tests/test_casino_embeds.py`,
  `tests/test_guess_embeds.py` are the two models; a new builder adds a
  `pytest.param` row, not a new file).
- **Mod-facing embeds keep the id *alongside* the name** — `Name (`id`)` — so a
  moderator retains something copyable. Member-facing cards get the name alone.
- **The one exception: a no-contact pair.** Where a surface would name two
  people the no-contact list keeps apart, degrade to a plain `User <id>` for
  both. The bot naming them together in its own voice manufactures exactly the
  association the list exists to prevent, and a mention there is worse than a
  number, not better. Pin that branch with a test, or the next
  resolve-the-ids sweep will quietly undo it. → `no_contact_spec.md`

## Pointing at things (ruling 2026-08-28: link the message)

**If you hold a message id, link the message.** A channel-only link drops the
reader at the *bottom* of the channel, where they still have to find the thing
you were telling them about — and the busier the channel, the worse it lands.
`core/utils.jump_url(guild_id, channel_id, message_id)` builds the permalink
from stored ids; `message.jump_url` is there when you hold the object. Never
hand-roll `f"https://discord.com/channels/…"` — the point of the helper is that
the next person can't accidentally omit the message id.

- **A notification that reports something that happened to a specific post
  carries a link to that post.** "Your question got a reply", "your pin is
  live", "you won the bounty" — the news is about one message, so the reader
  should be one tap from it, not from the room it lives in. **A DM with no way
  back at all is the real failure**; a channel mention beats nothing.
- **Link the channel when the channel really is the subject.** "Head to
  <#x> and open a ticket", "the casino has moved", "missing permission to post
  in <#x>", a voice room you join, a quest that counts activity *anywhere* in a
  channel — these name a room, and repointing them at some message inside it
  would be a downgrade. Use `channel_url` / a plain `<#id>` mention and move on.
- **When the id is missing *or won't keep*, degrade to the channel** rather
  than dropping the pointer. `bios/trigger.py` is the model for the first:
  message link when a stored ref exists, channel link when it doesn't. The
  auction's outbid DM is the model for the second — a **sticky panel is
  deleted and reposted** as chat moves, so a permalink to a live one is dead
  within minutes and the room is the only stable pointer. Link a sticky only
  once it has stopped moving (`_frozen_card_link`, after the freeze).
- **Both is allowed and often best.** A mention names *where* in words the
  reader recognises; the link gets them *there*. The no-contact alert
  (Channel field + Jump field) and the confession-reply DM (which links the
  reply *and* the original confession) do this.

## Builder conventions

- Embed construction lives in a **per-feature `embeds.py`** with **pure
  `build_*` functions**: plain dicts/primitives in, `discord.Embed` out, no
  Discord/network calls — testable offline. Name-lookup needs come in as a
  resolver callable. Cogs stay thin; a cog building several embeds inline
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

Re-measured 2026-09-02 against all 364 embed call sites. What the sweep closed
has been removed from this list rather than left to mislead the next reader;
what is below is what was still true afterwards.

- Economy sentence-case titles → Title Case.
- `·` in two casino pool titles (`cogs/casino/embeds.py`). They need a Title
  Case rewrite of the same two strings, so do both edits together.
- Separator strays in **body text**, which no rule covers yet: double-spaced
  `•` inside field values (`games_ama`), and `·` in voice control's room-type
  legend (`voice_master/embeds.py:114`). Titles and footers are clean —
  decide whether the rule extends to values before anyone sweeps these.
- "guild" in **dashboard route** errors (`web_server/routes/*`), and one
  member-facing leak the sweep argued itself out of: `guess_cog.py:2065`
  names the Discord permission inline — "Only mods (manage_guild permission)
  can inspect rounds." Naming the API permission to a member is still jargon;
  say "Manage Server". (Caught by the parallel filler-text review, not by this
  one — a reminder that "member-facing copy is clean" was too strong.)
- Question-form and bare-noun select placeholders ("Which server?", "Theme",
  "Assistance level") → imperative "Pick …".
- Empty-state replies that *read* like denials — "No one is in the hot seat.",
  "There's no active game in this channel.", "Nobody submitted a price this
  round." These deliberately do **not** take the `❌ ` prefix: they are empty
  states, not refusals. Listed here so the next sweep doesn't "fix" them.

### Now gated, not honour-system

`tests/test_embed_style_contract.py` fails the suite on: a denial reply missing
`❌ `, a footer separating with `·`, a select placeholder saying "Select", the
`colour=` spelling, and a pure-stacked multi-section card that never calls
`apply_section_spacing`. Each sweep carries a meta-test proving it can still
see a violation.

Know what the sweeps can and cannot see — the first version of this section
overclaimed, and review caught it:

- The denial sweep follows **one** level of indirection: a literal at a
  `send`-shaped call, or one passed to a local wrapper named in
  `_SEND_WRAPPERS` (`_reply`, `_ephemeral`, `safe_ephemeral`). Six denials in
  `role_menus/views.py` hid behind a local `_reply` until it did. **Write a new
  send wrapper, add its name to that set**, or its denials go unseen.
- The footer sweep reads `set_footer` literals **and** any function whose name
  contains "footer", so a string-layer builder that assembles the text and
  hands it over later is covered (`todo/board_logic.render_board_footer` and
  `economy/game_rewards.append_payout_footer` were both invisible before).
  Footer text assembled under some other name still slips through.
- The spacing sweep covers **every** builder with two or more fields. It used
  to exempt cards with `inline=True` triples; the helper skips those fields
  itself now, so the exemption is gone.

### Closed by the 2026-09-02 sweep (verified zero — don't go looking)

- The `colour=` kwarg; `█░`/bracket/pipe progress bars.
- Games ALL-CAPS titles — now zero, but **look for `.upper()`, not just for
  literal shouting**. This sweep's first pass reported zero because it grepped
  `title="[A-Z]…`; three had in fact survived it — the casino's two jackpot
  cards (one of them `f"…{casino_name.upper()}…"`, shouting a guild's own
  configured name back at it) and `games_traditional`, which built its card
  title as `label.upper()` from an already-Title-Case table. All three are
  recased.
- The "~19 pasted no-permission variants". There is one shared `NO_PERMISSION`
  in `services/replies.py` (28 references, matching § Errors above) plus six
  feature-specific
  `MANAGE_DENIED_MSG` constants that each name their own action ("…to review
  quest claims", "…to award or cancel bounties"). That is the *preferred*
  form under "say how to fix it", not drift to converge.
- Independent green/red constant families: `SUCCESS_COLOR` / `ERROR_COLOR` /
  `MOD_SUCCESS` are aliases of `COLOR_GREEN` / `COLOR_RED` now. `MOD_JAIL`
  keeps its own red as part of the sanctioned moderation identity palette.
