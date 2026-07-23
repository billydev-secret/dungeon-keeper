"""Tests for the /hidden list body builder — the 2000-char chunk guard.

One "Hidden Channels" category holds up to 50 channels (Discord's own cap),
which easily overruns a single 2000-char message. ``format_hidden_list``
must split into multiple sends so the command never 400s on a full category.
"""

from __future__ import annotations

from bot_modules.cogs.hidden_channels_cog import format_hidden_list


def test_format_hidden_list_single_message_when_short():
    entries = ["• <#1> — hidden by <@2>", "• <#3> — hidden by <@4>"]
    chunks = format_hidden_list(entries)
    assert len(chunks) == 1
    assert chunks[0].startswith("**Hidden channels:**\n")
    assert "<#1>" in chunks[0] and "<#3>" in chunks[0]


def test_format_hidden_list_chunks_under_2000():
    # A full category's worth of long lines overruns a single message.
    entries = [
        f"• (deleted channel {900000000000000000 + i}) — hidden by "
        f"<@{800000000000000000 + i}>"
        for i in range(50)
    ]
    chunks = format_hidden_list(entries)
    assert len(chunks) > 1  # would have been one oversized message before
    assert all(len(chunk) <= 2000 for chunk in chunks)
    # Nothing dropped: every entry survives across the chunks.
    joined = "\n".join(chunks)
    for i in range(50):
        assert str(900000000000000000 + i) in joined
