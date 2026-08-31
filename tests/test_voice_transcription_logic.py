"""Voice transcription: the transcript line, and the delete-after dial.

The cog's listener is glue over faster-whisper and Discord; what is worth
pinning is the text it posts and the config round trip that decides whether the
audio survives.
"""
from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.cogs.voice_transcription_cog import format_transcript
from bot_modules.services.voice_transcription_service import get_config, set_config

GUILD = 1469491362444480666


@pytest.mark.parametrize(
    ("speaker", "expected"),
    [
        pytest.param("Billy", "📝 **Billy:** walk the site", id="plain"),
        # A display name is member-controlled text landing in a bot message; an
        # unescaped * or _ would reformat the transcript that follows it.
        pytest.param(
            "*Billy*", "📝 **\\*Billy\\*:** walk the site", id="escapes-markdown"
        ),
        pytest.param(
            "under_score", "📝 **under\\_score:** walk the site", id="escapes-underscore"
        ),
    ],
)
def test_the_transcript_names_the_speaker_and_escapes_the_name(speaker, expected):
    assert format_transcript(speaker, "walk the site") == expected


def test_the_transcript_is_not_a_reply_so_it_carries_attribution():
    """Regression guard for why this function exists at all: the transcript used
    to be a reply to the voice message. Deleting the audio leaves a reply
    dangling, so the line has to say whose note it was on its own."""
    line = format_transcript("Billy", "hello")
    assert "Billy" in line and line.startswith("📝")


def test_delete_after_transcribe_defaults_off(sync_db_path):
    """Deleting the audio is irreversible, so a guild opts in."""
    with open_db(str(sync_db_path)) as conn:
        set_config(conn, GUILD, enabled=True, model_name="base.en")
        cfg = get_config(conn, GUILD)
    assert cfg is not None
    assert cfg.delete_after_transcribe is False


@pytest.mark.parametrize("flag", [True, False], ids=["on", "off"])
def test_delete_after_transcribe_round_trips(sync_db_path, flag):
    with open_db(str(sync_db_path)) as conn:
        set_config(
            conn, GUILD, enabled=True, model_name="base.en",
            channel_ids=(5000,), delete_after_transcribe=flag,
        )
        cfg = get_config(conn, GUILD)
    assert cfg is not None
    assert cfg.delete_after_transcribe is flag
    assert cfg.channel_ids == (5000,)


def test_turning_the_dial_off_again_sticks(sync_db_path):
    """The upsert must overwrite the column, not leave the old value behind —
    an admin who changes their mind has to be able to keep the audio."""
    with open_db(str(sync_db_path)) as conn:
        set_config(
            conn, GUILD, enabled=True, model_name="base.en",
            delete_after_transcribe=True,
        )
        set_config(
            conn, GUILD, enabled=True, model_name="base.en",
            delete_after_transcribe=False,
        )
        cfg = get_config(conn, GUILD)
    assert cfg is not None and cfg.delete_after_transcribe is False
