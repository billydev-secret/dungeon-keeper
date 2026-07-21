# Docs Cog (`/docs`) — Feature Spec

This spec describes the **`/docs` Discord command cog** — not the repo's `docs/` folder. A "doc" here is a guild-scoped, single-source markdown document (rules, mod FAQ, staff info, …) authored on the dashboard (Config → Docs) and stored in the database. The cog is the Discord surface for *placing* a doc into channels and re-syncing it: editing the doc's markdown anywhere (dashboard save or `/docs sync`) re-renders every channel it's posted in, in place. Code lives in `src/bot_modules/cogs/docs_cog.py` plus the `src/bot_modules/docs/` package (`render` / `db` / `sync`) and the dashboard route `src/web_server/routes/docs.py`.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/docs post doc_key [channel]` | Slash | Manage Guild + bot-mod | Post a doc into a channel (defaults to the current one) and keep it synced |
| `/docs sync [doc_key]` | Slash | Manage Guild + bot-mod | Re-render a doc everywhere it's posted; with no key, syncs every doc in the guild |
| `/docs unpost doc_key channel` | Slash | Manage Guild + bot-mod | Delete the doc's messages from a channel and drop the placement |
| `/docs list` | Slash | Manage Guild + bot-mod | List the guild's docs and which channels each is posted in |

The group is guild-only with `default_permissions=Manage Guild`, and every command additionally passes through the bot's own `ctx.is_mod` check. All replies are ephemeral. `doc_key` autocompletes from the guild's docs (matches key or title, max 25).

## Behavior

### Rendering (markdown → embeds)

`render.render_doc(title, body_md)` is a pure function (no `discord` import; unit-testable). Discord renders most markdown natively inside an embed *description*, so rendering is mostly pass-through, with structure imposed only where Discord can't:

- A line that is just `---` / `***` / `___` is a **message break** — each section becomes its own message.
- Headings stay inline as `#`/`##`/`###` markdown in the description (Discord renders those larger than the fixed-size embed `title` field). If the first section doesn't open with a heading, the doc's title is prepended as a `#` header. The embed `title` field is never used.
- Image markdown `![alt](url)` is stripped from the text and the section's first image becomes the embed's image. Masked links `[text](url)` pass through.
- A section longer than 4096 chars splits on paragraph boundaries into continuation embeds — never inside a fenced code block; oversized single blocks hard-split on line breaks.
- One embed per message (sidesteps the 6000-char aggregate limit). An empty doc renders one placeholder embed (`*(This document is empty.)*`).

Embed color: the doc's `accent` hex if set, otherwise the guild's branding accent (`resolve_accent_color`).

### Posting and sync reconciliation

`post` upserts a **placement** (doc × channel) and renders into it. Postable channel types: text channels, threads, and voice-channel chats (the slash command only offers text channels; the dashboard can place into any of the three).

`sync` reconciles the rendered embed list against the placement's tracked message ids position-by-position: edit where a tracked message exists, send where it doesn't, delete the surplus (doc shrank). If a tracked message was manually deleted mid-list, the mapping is "torn": since `channel.send` only appends, everything from the tear down is re-sent fresh and the stale tail deleted, keeping the channel visually in doc order. On `Forbidden`/HTTP errors the sync bails with a per-channel status (`ok` / `missing_channel` / `forbidden` / `error`) but never loses track of still-live message ids. Resulting ids are persisted after every sync (except when the channel itself is gone).

`unpost` best-effort deletes each tracked message, then drops the placement row.

### Pinning (dashboard-only)

A placement can be flagged `pinned`; sync then pins the doc's messages and unpins them when cleared. The pin pass is **delta-only** (never re-pins an already-pinned message, so steady-state syncs emit no "pinned a message" notices) and pins bottom-up so the pins list reads in doc order. Pin failures (missing Manage Messages, 50-pin limit) are reported separately (`pin_detail`) and never mark the placement broken. There is no `/docs` subcommand for this — the toggle lives on the dashboard.

### Dashboard surface

The dashboard (moderator-gated) owns authoring: create/edit/delete docs, live embed preview, image upload (raster only — PNG/JPG/GIF/WEBP, 8 MB cap, uuid filenames served from `/static/doc-images/`), placement add/remove, pin toggle, and manual sync. A dashboard save (`PUT /docs/{key}`) immediately re-renders every placement; deleting a doc first pulls its messages down from every channel.

### Limits

Doc keys are slugs (`[a-z0-9-]`, max 49 chars; user input is slugified). Title ≤ 200 chars, body ≤ 40,000 chars.

## Configuration

None in the cog. Per-doc: optional `accent` hex (falls back to the guild branding accent). Access is gated by Discord's Manage Guild default permission plus the bot's moderator check; the bot needs send/edit/delete permission in target channels, and Manage Messages where pinning is enabled.

## Stored data

Three tables (migrations `059_docs.sql`, `061_doc_placement_pinned.sql`):

- **`docs`** — canonical source: `guild_id`, `doc_key` (unique per guild), `title`, `body_md`, `accent`, timestamps, `updated_by`.
- **`doc_placements`** — one row per doc × channel: `doc_id`, `channel_id` (unique pair), `pinned`, timestamps.
- **`doc_placement_messages`** — the ordered Discord message ids rendering a placement (`placement_id`, `message_id`, `position`); replaced wholesale on each sync.

Uploaded doc images live on disk under `src/web_server/static/doc-images/` and are referenced by URL from the markdown.
