"""A voice-note quest scoped to a channel must only pay in that channel.

The listener fired the ``voice_message`` trigger with no channel context at
all, so a quest could only ever be server-wide: scoping one to a channel made
it unreachable, and leaving it unscoped paid for notes posted anywhere. Guild
1476525656115515484's "VN in Stop it and Drop it!" was earning on notes in
every channel because of it.
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock

import pytest

import bot_modules.economy.game_rewards as gr
from bot_modules.cogs.voice_transcription_cog import (
    VoiceTranscriptionCog,
    _quest_channel_ids,
)

_VOICE_FLAG = 1 << 13


def _message(channel):
    return types.SimpleNamespace(
        guild=types.SimpleNamespace(id=7),
        author=types.SimpleNamespace(id=42, bot=False),
        attachments=[types.SimpleNamespace(content_type="audio/ogg")],
        flags=types.SimpleNamespace(value=_VOICE_FLAG),
        channel=channel,
        id=555,
    )


@pytest.mark.parametrize(
    ("channel", "expected"),
    [
        pytest.param(
            types.SimpleNamespace(id=222, parent_id=None), (222,), id="plain-channel"
        ),
        # A note dropped in a thread still counts for a quest scoped to the
        # channel the thread hangs off.
        pytest.param(
            types.SimpleNamespace(id=333, parent_id=222), (333, 222), id="thread"
        ),
    ],
)
def test_the_note_reports_its_channel_and_thread_parent(channel, expected):
    assert _quest_channel_ids(_message(channel)) == expected


async def test_the_listener_hands_the_channel_to_the_quest_trigger(
    monkeypatch, sync_db_path
):
    spy = AsyncMock()
    monkeypatch.setattr(gr, "fire_member_trigger", spy)
    bot = types.SimpleNamespace(ctx=types.SimpleNamespace(db_path=str(sync_db_path)))
    cog = VoiceTranscriptionCog.__new__(VoiceTranscriptionCog)
    cog.bot = bot

    await cog._on_message(_message(types.SimpleNamespace(id=222, parent_id=None)))

    spy.assert_awaited_once()
    _args, kwargs = spy.await_args
    assert kwargs["channel_ids"] == (222,)
