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
   centred. A gold corner gradient is layered toward the bottom-right.
2. **Text** — the quote is wrapped, emoji-measured, and drawn with a soft
   gaussian drop shadow. Long text is truncated to `QUOTE_MAX_CHARS` (280).
3. **Avatar** (non-`none` modes) — the *unblurred* avatar is drawn on the left
   as a circle or rounded-square with a drop shadow and a double ring
   (cream outer + theme-gold inner). `author_name` is drawn as a small
   attribution below it.
4. **Rounded-corner alpha**, then the **border** is composited last.

**`pfp_shape`:**
- `"circle"` (default) — circular avatar crop with the double ring.
- `"square"` — rounded-square crop (shows the whole avatar uncropped).
- `"none"` — no avatar box. The prompt is centred across the card and
  `author_name` becomes a centred **header** above it. This is the "banner" look
  every non-`quote` caller uses.

### `render_quote(...)` — legacy solid-bg card

Older, simpler path used only by `guess_cog`. Solid dark background, accent bars
top and bottom, centred text, optional footer. Returns **JPEG bytes**. No
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
  bundled.

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
inside that opening. Upload **rejects** a frame whose centre has no usable
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

## Key constraints

- **`QUOTE_MAX_CHARS = 280`** — longer text is ellipsized.
- Fonts load eagerly and raise `FileNotFoundError` loudly if a TTF is missing —
  a broken/absent font is a hard failure, not a silent degrade.
- Rendering must run in a worker thread (`asyncio.to_thread`).
- New embeds/cards should take their accent from `resolve_accent_color`; the
  card themes above are the exception where color is part of the design.

## Related

- Live-test checklist: `docs/TESTING_QUEUE.md`.
- Tests: `tests/test_quote_border.py` (opening detection, mask-fit rendering,
  determinism), `tests/web/test_quote_border_routes.py` (upload/preview/delete).
