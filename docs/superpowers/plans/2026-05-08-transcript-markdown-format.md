# Transcript Markdown Format Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the HTML transcript file attachment with a Markdown `.md` file delivered to both the transcript channel and the subject user via DM.

**Architecture:** Add `render_transcript_markdown()` to `services/moderation.py`, remove `render_transcript_html()` and `_TRANSCRIPT_CSS`, then update the single call site in `commands/jail_commands.py` to use the new renderer and `.md` filenames. The database schema and JSON storage are untouched.

**Tech Stack:** Python stdlib only (`re`). No new dependencies.

---

## File Map

| File | Change |
|------|--------|
| `services/moderation.py` | Remove `import html`, `_TRANSCRIPT_CSS`, `render_transcript_html()`; add `render_transcript_markdown()` |
| `commands/jail_commands.py` | Swap import and update two call sites in `_collect_and_post_transcript()` |
| `tests/unit/test_transcript_rendering.py` | New — unit tests for `render_transcript_markdown()` |

---

## Task 1: Write failing tests for `render_transcript_markdown`

**Files:**
- Create: `tests/unit/test_transcript_rendering.py`

- [ ] **Step 1: Create the test file**

```python
"""Unit tests for render_transcript_markdown."""
from __future__ import annotations

import pytest

from services.moderation import render_transcript_markdown


def _make_transcript(**kwargs):
    base = {
        "type": "jail",
        "record_id": 42,
        "channel_name": "mod-jail-ben",
        "message_count": 0,
        "created_at": "2026-05-08T14:32:00+00:00",
        "messages": [],
    }
    base.update(kwargs)
    return base


def test_empty_transcript_has_no_messages_note():
    md = render_transcript_markdown(_make_transcript())
    assert "*No messages in this transcript.*" in md


def test_header_contains_type_and_id():
    md = render_transcript_markdown(_make_transcript())
    assert "# Jail #42" in md
    assert "#mod-jail-ben" in md


def test_metadata_fields_rendered():
    md = render_transcript_markdown(
        _make_transcript(close_reason="spamming", duration_served="1h")
    )
    assert "**Close Reason:** spamming" in md
    assert "**Duration Served:** 1h" in md


def test_message_author_and_content():
    t = _make_transcript(
        message_count=1,
        messages=[
            {
                "author_name": "Ben",
                "content": "hello world",
                "timestamp": "2026-05-08T14:10:01+00:00",
            }
        ],
    )
    md = render_transcript_markdown(t)
    assert "**Ben**" in md
    assert "hello world" in md
    assert "2026-05-08" in md


def test_embed_rendered_as_blockquote():
    t = _make_transcript(
        message_count=1,
        messages=[
            {
                "author_name": "Bot",
                "content": "",
                "timestamp": "2026-05-08T14:10:01+00:00",
                "embeds": [{"title": "Role Issue", "description": "Your role was removed."}],
            }
        ],
    )
    md = render_transcript_markdown(t)
    assert "> **Role Issue**" in md
    assert "> Your role was removed." in md


def test_attachment_rendered_as_link():
    t = _make_transcript(
        message_count=1,
        messages=[
            {
                "author_name": "Ben",
                "content": "",
                "timestamp": "2026-05-08T14:10:01+00:00",
                "attachments": [
                    {"filename": "screenshot.png", "url": "https://cdn.discord.com/abc.png"}
                ],
            }
        ],
    )
    md = render_transcript_markdown(t)
    assert "📎 [screenshot.png](https://cdn.discord.com/abc.png)" in md


def test_author_name_with_markdown_special_chars_is_escaped():
    t = _make_transcript(
        message_count=1,
        messages=[
            {
                "author_name": "*bold_user*",
                "content": "hi",
                "timestamp": "2026-05-08T14:10:01+00:00",
            }
        ],
    )
    md = render_transcript_markdown(t)
    # Raw asterisks/underscores in the author line must be escaped
    assert r"\*bold\_user\*" in md


def test_policy_ticket_type_formatted():
    t = _make_transcript(type="policy_ticket")
    md = render_transcript_markdown(t)
    assert "# Policy Ticket #42" in md
```

- [ ] **Step 2: Run the tests and confirm they all fail**

```
pytest tests/unit/test_transcript_rendering.py -v
```

Expected: `ImportError` or `AttributeError` — `render_transcript_markdown` does not exist yet.

---

## Task 2: Implement `render_transcript_markdown` and remove the HTML renderer

**Files:**
- Modify: `services/moderation.py`

- [ ] **Step 1: Remove `import html` from the imports at the top of the file**

The `html` module is only used inside `render_transcript_html`. Delete the import line:

```python
import html  # DELETE THIS LINE
```

- [ ] **Step 2: Delete `_TRANSCRIPT_CSS` and `render_transcript_html`**

Delete from `# Transcript HTML rendering` section header through the end of `render_transcript_html()` (lines ~732–874 in the current file). The section to remove spans:
- The `_TRANSCRIPT_CSS = """..."""` constant (~60 lines of CSS)
- The `render_transcript_html()` function (~70 lines)
- The `_fmt_ts()` helper is used by the new renderer — **keep it**

- [ ] **Step 3: Add a module-level escape pattern and `render_transcript_markdown` after `_fmt_ts`**

Insert after `_fmt_ts()`:

```python
_MD_SPECIAL = re.compile(r'([\\`*_{}\[\]()#+\-.!|])')


def _md_esc(s: str) -> str:
    return _MD_SPECIAL.sub(r'\\\1', s)


def render_transcript_markdown(transcript: dict[str, Any]) -> str:
    """Render a transcript dict as a Markdown document."""
    rtype = str(transcript.get("type", "transcript")).replace("_", " ").title()
    rid = transcript.get("record_id", "")
    channel_name = str(transcript.get("channel_name", ""))
    count = transcript.get("message_count", 0)
    created = _fmt_ts(transcript.get("created_at", ""))

    lines: list[str] = [
        f"# {_md_esc(rtype)} #{rid} — #{_md_esc(channel_name)}",
        "",
        f"**Channel:** #{_md_esc(channel_name)}",
        f"**Messages:** {count}",
        f"**Generated:** {created}",
    ]
    for key in ("close_reason", "reason", "closed_by", "duration_served", "transcript_stage"):
        val = transcript.get(key)
        if val not in (None, ""):
            label = key.replace("_", " ").title()
            lines.append(f"**{label}:** {_md_esc(str(val))}")
    lines += ["", "---", ""]

    messages = transcript.get("messages", [])
    if not messages:
        lines.append("*No messages in this transcript.*")
    else:
        for m in messages:
            author = _md_esc(str(m.get("author_name", "unknown")))
            ts = _fmt_ts(m.get("timestamp", ""))
            content = str(m.get("content", "") or "")
            lines.append(f"**{author}** — {ts}")
            if content:
                lines.append(content)
            for e in m.get("embeds", []) or []:
                etitle = str(e.get("title", "") or "")
                edesc = str(e.get("description", "") or "")
                if etitle:
                    lines.append(f"> **{_md_esc(etitle)}**")
                if edesc:
                    lines.append(f"> {edesc}")
            for a in m.get("attachments", []) or []:
                fn = _md_esc(str(a.get("filename", "file")))
                url = str(a.get("url", ""))
                lines.append(f"\U0001f4ce [{fn}]({url})")
            lines.append("")

    return "\n".join(lines)
```

- [ ] **Step 4: Run the tests and confirm they all pass**

```
pytest tests/unit/test_transcript_rendering.py -v
```

Expected: all 8 tests PASS.

- [ ] **Step 5: Run the full test suite to confirm nothing is broken**

```
pytest --tb=short -q
```

Expected: no regressions (ignore any pre-existing failures unrelated to this change).

- [ ] **Step 6: Commit**

```
git add services/moderation.py tests/unit/test_transcript_rendering.py
git commit -m "feat: replace HTML transcript renderer with Markdown"
```

---

## Task 3: Update the call site in `commands/jail_commands.py`

**Files:**
- Modify: `commands/jail_commands.py`

- [ ] **Step 1: Update the import**

Find the import block near line 45–46:

```python
    generate_transcript,
    render_transcript_html,
```

Change to:

```python
    generate_transcript,
    render_transcript_markdown,
```

- [ ] **Step 2: Update `_collect_and_post_transcript`**

Find the block around lines 194–215:

```python
    # Build HTML file
    html_bytes = render_transcript_html(transcript).encode("utf-8")
    filename = f"{record_type}-{record_id}-transcript.html"
```

Replace with:

```python
    # Build Markdown file
    md_bytes = render_transcript_markdown(transcript).encode("utf-8")
    filename = f"{record_type}-{record_id}-transcript.md"
```

Then update both `discord.File` calls in the same function to use `md_bytes`:

```python
            await ch.send(
                embed=embed, file=discord.File(io.BytesIO(md_bytes), filename)
            )
```

and:

```python
    await _dm_user(user, file=discord.File(io.BytesIO(md_bytes), filename))
```

- [ ] **Step 3: Run the full test suite**

```
pytest --tb=short -q
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```
git add commands/jail_commands.py
git commit -m "chore: wire transcript channel and DM to .md format"
```
