# Transcript Markdown Format — Design Spec

**Date:** 2026-05-08  
**Branch:** feat/veil-phase-1 (or separate branch)  
**Scope:** Replace HTML transcript rendering with Markdown

---

## Problem

Transcripts (jail, ticket, policy_ticket) are currently rendered as styled HTML files and delivered as `.html` attachments — both to the transcript channel and via DM to the subject user. HTML requires a browser to read meaningfully and is harder to copy/paste or archive in plain text tools.

## Goal

Replace the HTML transcript format with Markdown (`.md`) everywhere it is sent. The stored JSON representation in the database is unchanged.

---

## Architecture

The change is localised to two files:

### `services/moderation.py`

- **Remove** `import html`
- **Remove** `_TRANSCRIPT_CSS` constant (~60 lines)
- **Remove** `render_transcript_html()` (~70 lines)
- **Add** `render_transcript_markdown(transcript: dict[str, Any]) -> str`

The new function produces a UTF-8 Markdown string with this structure:

```
# {RecordType} #{record_id} — #{channel_name}

**Channel:** #{channel_name}
**Messages:** {count}
**Generated:** {created_at}
[optional metadata: Close Reason, Reason, Closed By, Duration Served, Transcript Stage]

---

**{author_name}** — {timestamp}
{message content (raw, left as-is so Discord markdown renders naturally)}
> **{embed title}**
> {embed description}
📎 [{filename}]({url})

[separator between messages: blank line]
```

- If the transcript has no messages, emit `*No messages in this transcript.*`
- Author names and metadata values are escaped for Markdown special characters (`*`, `_`, `` ` ``, `\`, `[`, `]`, `(`, `)`, `#`, `>`)
- Message content is left unescaped — it originates from Discord and already contains Discord markdown

### `commands/jail_commands.py`

- Change import: `render_transcript_html` → `render_transcript_markdown`
- In `_collect_and_post_transcript()`:
  - Replace `html_bytes = render_transcript_html(transcript).encode("utf-8")` with `md_bytes = render_transcript_markdown(transcript).encode("utf-8")`
  - Replace `filename = f"{record_type}-{record_id}-transcript.html"` with `filename = f"{record_type}-{record_id}-transcript.md"`
  - Update both `discord.File(io.BytesIO(html_bytes), filename)` calls to use `md_bytes`

---

## Data Flow

```
Discord channel messages
        ↓
generate_transcript()        ← unchanged
        ↓
store_transcript() in DB     ← unchanged (stores raw JSON dict)
        ↓
render_transcript_markdown() ← NEW (replaces render_transcript_html)
        ↓
discord.File(.md bytes)
        ↓
  ┌─────────────────────────────────┐
  │ transcript channel (embed + file)│  ← filename: {type}-{id}-transcript.md
  │ DM to subject user (file only)  │
  └─────────────────────────────────┘
```

The web route (`GET /moderation/transcript`) returns raw JSON from the DB and is **unaffected**.

---

## Out of Scope

- No change to how transcripts are stored in the database
- No change to the web dashboard transcript endpoint
- No backward-compatibility shim for old HTML files (already sent ones remain HTML)
- No dual-format delivery (markdown only, always as a file attachment)

---

## Testing

- Unit test `render_transcript_markdown()` in `tests/services/test_moderation.py`:
  - Full transcript with messages, embeds, attachments, and metadata fields
  - Empty transcript (no messages)
  - Author name with markdown special characters
