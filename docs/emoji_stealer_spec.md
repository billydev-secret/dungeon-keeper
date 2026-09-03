# Emoji Stealer — Feature Spec

Add a custom emoji to one of DungeonKeeper's servers, either by right-clicking a message that carries a custom emoji — in its text **or** as a reaction on it — or by giving a direct image URL. When the bot is in multiple servers, prompts the user to pick which server.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `Steal Emoji` | Message context menu | Manage Expressions, Manage Server, or Administrator in the destination server | Pull emojis from a message; upload to a chosen server |
| `/steal_emoji url:<url> name:<name>` | Slash | Manage Expressions, Manage Server, or Administrator in the destination server | Upload from any HTTPS image URL |

Neither command carries a Discord-level permission restriction of its own — anyone can run them. The gate is enforced by which servers get offered: a server only counts as eligible when the **bot** has Manage Expressions there *and* the invoking member has Manage Expressions, Manage Server, or Administrator there. A member with none of those roles anywhere sees "I don't have **Manage Expressions** in any server," even if the bot has the permission in servers the member just isn't privileged in.

## Behavior

### Right-click → "Steal Emoji"
Parses every custom emoji from the clicked message — both those written in its text and those added to it as reactions — deduplicating repeats (an emoji that appears in both is offered once, at its in-text position). Unicode reactions are skipped; only custom emoji are stealable. With exactly one emoji and one eligible server, uploads immediately. Otherwise opens a picker (emoji selector + server selector + **Steal** / **Steal All** / **Cancel**) that times out after two minutes and only accepts input from the invoker.

**Steal All** uploads every emoji in the message to one server. A single emoji failing doesn't abort the batch — failures are collected and reported alongside successes.

### `/steal_emoji`
URL must be HTTPS (Discord's CDN is HTTPS-only). Name must be ≥2 characters of letters / numbers / underscores; auto-sanitized then rejected if sanitization produces a too-short name. With one eligible server, uploads immediately; otherwise opens a server picker.

### Duplicate detection
Every single-emoji steal (right-click with one emoji/one server, the picker's **Steal** button, or `/steal_emoji`) checks the destination server's existing emojis before uploading, in priority order:

1. **Exact** — SHA-256 byte match against a downloaded existing emoji.
2. **Similar** — a perceptual (color-aware) hash match tolerant of Discord's re-encoding and this bot's own GIF resizing, so a re-steal of the same emoji is still caught after compression.
3. **Name** — an existing emoji with the same (sanitized) name but a different image.

A hit doesn't block the upload — it shows a warning ("⚠️ **{server}** already has this exact emoji as {emoji} `:{name}:`. Add it anyway?", worded per match kind) with **Add Anyway** / **Cancel** buttons, same two-minute/invoker-only rules as the other pickers.

**Steal All** skips this confirmation — a duplicate is silently left out of the batch, and the count/names of skipped emojis are reported alongside successes and failures.

Guild emoji hashes are cached in memory per server (self-healing: entries for emoji that have since been removed are dropped, new ones are fetched lazily on the next steal). The cache is not persisted — it's rebuilt from scratch after a bot restart.

### GIF compression
Animated GIFs over Discord's 256 KB emoji ceiling are downscaled (96 → 64 → 48 → 32 pixel squares) until a pass fits. If even the 32 px pass still exceeds the limit, compression gives up and the original, unresized bytes are uploaded instead — Discord rejects them and the user sees the rejection. Static images upload unchanged.

## User-visible errors

| When | The user sees |
|---|---|
| No server is both bot-eligible and invoker-eligible (see Permission above) | "I don't have **Manage Expressions** in any server." |
| Bot lacks Manage Expressions in the chosen server at upload time | "I don't have **Manage Expressions** in **{server}**." |
| Discord rejects the upload (size, slot-full, content policy) | "Discord rejected it: {reason}" |
| URL or emoji download fails | "Couldn't download the emoji: {reason}" (always says "emoji", even for `/steal_emoji`) |
| Downloaded content isn't a recognized image (PNG/JPEG/GIF/WEBP magic bytes) | "That doesn't look like an image — give me a direct image URL (ending in .png, .gif, or .webp), not a webpage or message link." |
| A duplicate is detected on a single-emoji steal | "⚠️ **{server}** already has this exact emoji as {emoji} `:{name}:`. Add it anyway?" (wording varies by exact/similar/name match) — offers **Add Anyway** / **Cancel** |
| Message has no custom emojis (in text or reactions) | "No custom emojis found in that message or its reactions." |
| URL doesn't start with `https://` | "URL must start with `https://`." |
| Emoji name fails validation | "Emoji name must be at least 2 characters (letters, numbers, underscores)." |
| Non-invoker clicks a picker button | "This menu isn't for you." |

## Non-goals

- No resizing of static formats (PNG, WEBP, JPG, APNG); they upload as-is.
- No check of destination server's remaining emoji slots before upload — a slot-full failure surfaces as a Discord rejection.
- No bulk export of a server's emojis. **Steal All** operates on a single clicked message.

## Configuration

None. Behavior is gated by Discord's **Manage Expressions** permission on the destination server.

## Stored data

None. Emoji stealer is stateless — nothing in the database, no filesystem cache.
