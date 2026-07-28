# Quote Renderer — spec

**Status: Reference** (matches current behavior as of commit `0c85b52`).

`src/bot_modules/services/quote_renderer.py` is a shared, stateless
text-to-image service. It turns a string + a background image into a branded
PNG (or JPEG) "card". It is **not** a cog — it has no commands and no DB access;
callers pass everything in and get bytes back. Several cogs depend on it, so
treat changes here as cross-cutting.

## Where it's used

| Caller | Function | Mode | Notes |
|---|---|---|---|
| `quote_cog` `/quote` (message context menu) | `render_quote_card` | avatar (`circle`/`square`) | Quotes a message; avatar on the left, quote on the right. Theme/font/border picked in an ephemeral style view. |
| `quote_cog` `/banner` | `render_quote_card` | `none` (banner) | Free-text banner; guild icon (or invoker avatar) as background. Uses the guild's uploaded border by default. |
| `economy_cog` QOTD | `render_quote_card` | `none` | `author_name="Question of the Day"`, `theme=midnight`. Falls back to a plain embed if no image / render raises. |
| `games_photo_cog` launch | `render_quote_card` | `none` | Photo-challenge prompt card, `theme=golden_meadow`. |
| `games_ffa_cog` launch | `render_quote_card` | `none` | FFA round banner; theme chosen per game label. |
| `guess_cog` confession rounds | `render_quote` (legacy) | — | Solid-background JPEG spoiler card, `footer="Guess #N"`. |
| `web_server/routes/config.py` | `analyze_border_opening`, `guild_border_path`, `BorderStyle` | — | Dashboard upload/preview/delete of a guild's custom border. |

Every cog call runs under `asyncio.to_thread(...)` — rendering is CPU-bound
(PIL + numpy + a Python-loop vignette) and must stay off the event loop.

## Two render paths

### `render_quote_card(...)` — pfp-as-background card

The primary path. Returns **PNG bytes** with transparent rounded corners.
Default canvas **900×500**.

**Pipeline (`_build_background` → foreground → border):**
1. **Background** — the passed `avatar_bytes` is fit-covered to the canvas,
   Gaussian-blurred (radius 18), desaturated, blended with the theme's overlay
   color, and given a radial vignette. In avatar modes the face is pushed left
   (`offset_x`) so it doesn't sit under the text column; in `none` mode it stays
   centered. A gold corner gradient is layered toward the bottom-right.
2. **Text** — the quote is wrapped, emoji-measured, and drawn with a soft
   gaussian drop shadow. Long text is truncated to `QUOTE_MAX_CHARS` (280).
3. **Avatar** (non-`none` modes) — the *unblurred* avatar is drawn on the left
   as a circle or rounded-square with a drop shadow and a double ring
   (cream outer + theme-gold inner). `author_name` is drawn as a small
   attribution below the quote (see **Attribution placement**).
4. **Rounded-corner alpha**, then the **border** is composited last.

**`pfp_shape`:**
- `"circle"` (default) — circular avatar crop with the double ring.
- `"square"` — rounded-square crop (shows the whole avatar uncropped).
- `"none"` — no avatar box. The prompt is **center-aligned** across the card and
  `author_name` becomes a centered **header** above it. This is the "banner" /
  announcement look every non-`quote` caller uses. (Avatar modes keep the body
  **left-aligned** in the right-hand column — only banner/announcement bodies are
  centered. Body lines are clamped left of the floral corner so centering never
  runs text under the flowers.)

### Attribution placement

In avatar modes the attribution (`— Name`) **left-aligns to the quote column** and
hangs one `gap` (~0.85 × attribution font size) below the quote's last line. The
quote and its attribution are then **centred as a single group**, so neither the
top nor the bottom of the card carries a dead band.

It is *not* centred under the avatar. That was the earlier behaviour, and because
it centred on the disc (`pfp_cx − attr_w / 2`), any name wider than the disc drove
the anchor negative, collapsed it onto the left-margin floor, and ran the first
characters across the avatar's lower-left arc — while leaving the attribution
aligned to nothing. Anchoring to the text column fixes both at once: the column
already begins clear of the avatar's drawn footprint (double ring + drop shadow,
not the bare radius), so **no name length can reach back into the avatar**.

A name too wide for the column is **shrunk** to the largest size that fits (floor
≈ half the base attribution size) and only then **truncated with an ellipsis**, so
it never runs past the column's right edge.

Placement is factored into pure helpers so the geometry is unit-testable without
pixel-peeping a blurred card: `attribution_y()`, `attribution_block_h()`, and
`fit_attribution_text()`. Both layout paths share them — the bundled-border path
and the custom-frame (`mask_fit`) path, where a frame opening's bottom edge also
clamps the line upward. **y resolves before x**, because the column's horizontal
bounds are read at whatever row the line finally lands on; reading them at the
pre-clamp row would pick the wrong bound whenever the clamp moved the line, and in
an opening that narrows toward the bottom that means drawing over the frame.

Attribution height comes from the font's **line box** (`ascent + descent`), never a
string's ink bbox — an ink bbox varies with which letters the name contains ("Bob"
20px, "gg" 21px, a parenthesised name 30px), which would make the reserve and the
centring jitter per user. The line box is also identical whether or not pilmoji is
installed, so geometry doesn't fork on an optional dependency. (Width *does* go
through pilmoji when available, so an emoji contributes its drawn width rather than
the tofu box it replaces.) The custom-frame path reserves this same measured block
instead of a `1.7 ×` font-size estimate; the two are close at the default size
(49 vs 45px), so that change is about having one source of truth, not about size.

### Vertical bounding and the floral corner

Both paths bound the text to a vertical band and **cap the line count** to what
fits, ellipsizing the last line. The bundled-border path previously had no such
bound: past ~9 lines (~150 chars, well inside `QUOTE_MAX_CHARS`) the quote ran off
both card edges, and the attribution — which follows the quote rather than sitting
at a fixed y — went off the bottom with it and vanished from the PNG.

On the slim border, the text column's nominal right edge (738 at 900w) runs into
the flower cluster (pasted from x≈586, y≈253), so a low, long line was drawn under
the petals. The usable right edge is therefore bounded **per row** by
`slim_flower_left_edge()` — the cluster's leftmost solidly-drawn pixel per row,
read from its alpha and cached per (frame, mtime, size). Reading real alpha rather
than the cluster's bounding box matters for typography: the cluster's upper rows
are a few sparse buds, and reserving its full width for them cost a wrapped line at
*every* quote length. `flower_limit()` takes the tightest bound across the rows a
line actually covers, since a line is a band and petals dipping into its lower rows
must bound it too. `slim_flower_rect()` is shared with `_composite_slim_border()`
so the layout's idea of where the flowers sit can't drift from where they're drawn.

The **banner** (`pfp_shape="none"`) layout uses the same per-row edge. It used to
carve the corner with a hand-tuned linear ramp (0.95w tapering to 0.58w) that
over-reserved by up to 233px on a 900×500 card, so each line wrapped shorter than
the last and short words were orphaned onto their own line. Frames *without* a
separable cluster (anything not `slim_frame`) keep that ramp: it's a crude but safe
stand-in for border art this can't measure, and dropping it could put text over
their artwork.

The **default frame is resolved at the top of `render_quote_card()`**, not just
before compositing. Filling it in late meant a caller passing no `border_style` —
which is every caller except `/quote` — laid its text out as if the card were bare
and then had the poppy frame drawn over it, putting the last lines back under the
petals.

**Heights that feed layout always come from a font's line box, never a string's ink
bbox.** This applies to the attribution (above) *and* the banner header: the
header's height sets where the body starts, and the body's usable width narrows
toward the floral corner, so an ink-bbox header made the **quote itself re-wrap
based on which glyphs the author's name contained** — a name with parentheses
pushed the body down a line.

### `render_quote(...)` — legacy solid-bg card

Older, simpler path used only by `guess_cog`. Solid dark background, accent bars
top and bottom, centered text, optional footer. Returns **JPEG bytes**. No
themes, avatars, or borders. Kept for the confession-guess spoiler cards.

## Themes

`THEMES: dict[str, QuoteTheme]` — controls overlay color/strength, desaturation,
text/attribution colors, and vignette darkness.

| Key | Look |
|---|---|
| `golden_meadow` | Warm amber-gold overlay, cream text. The brand default. |
| `midnight` | Deep blue overlay, near-white text. |
| `rose` | Pink-magenta overlay, blush text. |

## Fonts

`FONT_STYLES: dict[str, Path]` maps a style key to a bundled TTF in
`assets/fonts/`. Body and header fonts are **independent**:

- **Body** — `font_style` param (default `"inter"` in the signature; callers
  default it to `"times"`).
- **Header** (`none` mode only) — `header_font_style` param, default
  `"helvetica"`, so the editorial **sans-header / serif-body** pairing holds
  regardless of the body font a mod picks. The header is faux-bolded with a
  light stroke (`header_size // 40`) plus its drop shadow — no bold TTF is
  bundled. Its **color is the border's dominant accent**
  (`dominant_border_color`), so the title echoes the frame (Golden Poppy → gold)
  rather than a fixed theme color.

| Key | Face | Notes |
|---|---|---|
| `times` | Liberation Serif | OFL, metric-compatible Times New Roman stand-in. **Default body font.** |
| `helvetica` | Arimo | OFL, metric-compatible Helvetica/Arial stand-in. **Default header font.** |
| `inter` | Inter | |
| `playfair` | Playfair Display | High-contrast display serif. |
| `oswald` | Oswald | Condensed sans. |
| `caveat` | Caveat | Handwriting. |
| `bebas` | Bebas Neue | Tall condensed caps. |

> The originals (Helvetica, Times New Roman) are proprietary and cannot be
> bundled; the metric-compatible OFL clones are used and exposed under the
> familiar names. A previously-listed `lora` style was removed — its shipped
> file was corrupt (HTML, not a font) and crashed any card that selected it;
> a stored `lora` value now falls back to the default.

## Borders

`BORDERS: dict[str, BorderStyle]` holds the bundled frames; a guild may also
upload its own. A `BorderStyle` carries `path`, `flip` (mirror so a corner
motif lands away from the left avatar), `luma_key` (key out an opaque black
background vs. use a real alpha channel), `mask_fit`, and `slim_frame`.

### Golden Poppy (`golden_poppy`, `slim_frame=True`)

The default bundled floral frame. Its PNG bakes a thick gold frame **and** a
large flower cluster into one raster. To keep the frame full-bleed (so the
hand-tuned avatar/text layout is untouched) while making the decoration subtler,
`slim_frame` triggers `_composite_slim_border`, which:
- draws a **thin gold rounded-rect** frame itself (replacing the thick baked one), and
- crops **only** the flower cluster (a fixed interior box clear of the frame
  lines), shrinks it to **~72%** (`_SLIM_FLOWER_SCALE`), and tucks it into the
  bottom-right corner.

Tuning knobs live at module scope: `_SLIM_FRAME_GOLD`, `_SLIM_FLOWER_SCALE`,
`_SLIM_FLOWER_CROP`.

### Midnight (`midnight_frame`)

A real-alpha PNG composited as-is (no slim treatment, no luma key).

### Custom uploaded frames (`mask_fit=True`)

A guild can upload its own frame via the dashboard
(`/config/quote-border`). It's stored beside the DB at
`db_path.parent/quote_borders/<guild_id>/border.png` (`guild_border_path`) and
re-encoded to a real-alpha RGBA PNG, so `flip`/`luma_key` are both False.

Unlike bundled frames, a custom frame **drives its own layout**: rather than a
fixed text column, `analyze_border_opening` reads the frame's transparency and
returns the see-through opening as per-row `[left, right]` spans plus a fitted
avatar disc. `render_quote_card` then flows the avatar + auto-shrunk quote text
inside that opening. Upload **rejects** a frame whose center has no usable
opening (probed at 900×500), so rendering always has a valid hole to fit into.
Openings are cached by `(path, mtime, size)`.

> Because custom-frame layout derives from the alpha opening, the `slim_frame`
> shrink treatment is intentionally **not** applied to them — only Golden Poppy
> opts in. Midnight and custom frames render through the original composite.

## Emoji

Both Unicode emoji (via `pilmoji` + Twemoji, when installed) and Discord custom
emoji (`<:name:id>` / `<a:name:id>`, passed in as `custom_emojis: {id: bytes}`)
are measured and composited inline at their token positions. `pilmoji` is an
optional dependency; without it, text still renders (custom emoji still
composite; Unicode emoji fall back to the font).

The quote body, the attribution line, and the no-pfp header all draw through
pilmoji, so a Unicode emoji renders in color wherever it appears — including
inside a member's display name.

Twemoji is fetched over HTTP the first time each glyph is drawn. Two safeguards
keep that off the critical path:

- **Bounded fetch.** pilmoji sets no timeout, so a stalled CDN would hang the
  render worker thread indefinitely. A custom `TwemojiEmojiSource` subclass
  threads a `_EMOJI_FETCH_TIMEOUT` (5 s) into whichever HTTP backend pilmoji
  chose (`timeout` can't sit in pilmoji's `REQUEST_KWARGS` — `urllib.Request`
  rejects it, only `urlopen` takes it).
- **Fail-soft everywhere.** A fetch failure never fails the card. The
  attribution/header draw is wrapped and degrades that line to plain PIL text
  (emoji become tofu). The body render snapshots the canvas first, and on any
  failure rolls back to that snapshot and re-renders the lines without pilmoji —
  so a partial draw can't double up, and an outage yields a tofu-emoji card
  rather than a failed one.

## Display names

`author_name` is NFKC-normalized once on entry to `render_quote_card`, before
either the attribution or the header uses it. Discord names commonly use
Mathematical Alphanumeric Symbols (`𝓟𝓻𝓲𝓷𝓬𝓮𝓼𝓼 𝓡𝓪𝓬𝓱𝓮𝓵` → `Princess Rachel`) or
fullwidth forms; **no bundled TTF carries those codepoints**, so without folding
they draw as a row of tofu boxes. NFKC maps them to their compatibility
equivalents, is a no-op for ordinary names, and leaves emoji intact (they have
no decomposition) for pilmoji to draw.

## Key constraints

- **`QUOTE_MAX_CHARS = 280`** — longer text is ellipsized.
- Fonts load eagerly and raise `FileNotFoundError` loudly if a TTF is missing —
  a broken/absent font is a hard failure, not a silent degrade.
- Rendering must run in a worker thread (`asyncio.to_thread`).
- New embeds/cards should take their accent from `resolve_accent_color`; the
  card themes above are the exception where color is part of the design.

## Related

- Live-test checklist: the commit's own `Testing:` section (see CLAUDE.md),
  posted as a QA Tracker card.
- Tests: `tests/test_quote_border.py` (opening detection, mask-fit rendering,
  determinism), `tests/web/test_quote_border_routes.py` (upload/preview/delete).
