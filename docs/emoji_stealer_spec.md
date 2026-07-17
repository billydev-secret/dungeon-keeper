# Emoji Stealer — Feature Spec

Add a custom emoji to one of DungeonKeeper's servers, either by right-clicking a message that carries a custom emoji — in its text **or** as a reaction on it — or by giving a direct image URL. When the bot is in multiple servers, prompts the user to pick which server.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `Steal Emoji` | Message context menu | None | Pull emojis from a message; upload to a chosen server |
| `/steal_emoji url:<url> name:<name>` | Slash | None | Upload from any HTTPS image URL |

Both require the **bot** to have Manage Expressions in the destination server.

## Behaviour

### Right-click → "Steal Emoji"
Parses every custom emoji from the clicked message — both those written in its text and those added to it as reactions — deduplicating repeats (an emoji that appears in both is offered once, at its in-text position). Unicode reactions are skipped; only custom emoji are stealable. With exactly one emoji and one eligible server, uploads immediately. Otherwise opens a picker (emoji selector + server selector + **Steal** / **Steal All** / **Cancel**) that times out after two minutes and only accepts input from the invoker.

**Steal All** uploads every emoji in the message to one server. A single emoji failing doesn't abort the batch — failures are collected and reported alongside successes.

### `/steal_emoji`
URL must be HTTPS (Discord's CDN is HTTPS-only). Name must be ≥2 characters of letters / numbers / underscores; auto-sanitized then rejected if sanitization produces a too-short name. With one eligible server, uploads immediately; otherwise opens a server picker.

### GIF compression
Animated GIFs over Discord's 256 KB emoji ceiling are downscaled (96 → 64 → 48 → 32 pixel squares) until they fit. If 32 px still exceeds the limit, Discord rejects the upload and the user sees the rejection. Static images upload unchanged.

## User-visible errors

| When | The user sees |
|---|---|
| Bot lacks Manage Expressions in the chosen server | "I don't have **Manage Expressions** in **{server}**." |
| Discord rejects the upload (size, slot-full, content policy) | "Discord rejected it: {reason}" |
| URL or emoji download fails | "Couldn't download the {emoji \| image}: {reason}" |
| Message has no custom emojis (in text or reactions) | "No custom emojis found in that message or its reactions." |
| URL doesn't start with `https://` | "URL must start with `https://`." |
| Emoji name fails validation | "Emoji name must be at least 2 characters (letters, numbers, underscores)." |
| Non-invoker clicks a picker button | "This menu isn't for you." |

## Non-goals

- No resizing of static formats (PNG, WEBP, JPG, APNG); they upload as-is.
- No check of destination server's remaining emoji slots before upload — a slot-full failure surfaces as a Discord rejection.
- No bulk export of a server's emojis. **Steal All** operates on a single clicked message.

## Configuration

None. Behaviour is gated by Discord's **Manage Expressions** permission on the destination server.

## Stored data

None. Emoji stealer is stateless — nothing in the database, no filesystem cache.
