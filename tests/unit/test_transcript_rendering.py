"""Unit tests for render_transcript_markdown."""
from __future__ import annotations


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


def test_embed_multiline_description_all_lines_blockquoted():
    t = _make_transcript(
        message_count=1,
        messages=[
            {
                "author_name": "Bot",
                "content": "",
                "timestamp": "2026-05-08T14:10:01+00:00",
                "embeds": [
                    {
                        "title": "Policy Vote",
                        "description": "Line one.\nLine two.\nLine three.",
                    }
                ],
            }
        ],
    )
    md = render_transcript_markdown(t)
    assert "> Line one." in md
    assert "> Line two." in md
    assert "> Line three." in md
