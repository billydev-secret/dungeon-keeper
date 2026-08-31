"""Voice transcription: the transcript line, and the delete-after dial.

The cog's listener is glue over faster-whisper and Discord; what is worth
pinning is the text it posts and the config round trip that decides whether the
audio survives.
"""
from __future__ import annotations

import pytest

from types import SimpleNamespace

from bot_modules.core.db_utils import open_db
from bot_modules.cogs.voice_transcription_cog import _audio_attachment, format_transcript
from bot_modules.services.voice_transcription_service import (
    MAX_TRANSCRIPT_CHARS,
    fit_transcript,
    get_config,
    set_config,
)

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


# ── the on-demand context menu ───────────────────────────────────────────
#
# "Apps → Transcribe Voice Note" (a long press on mobile) is installed to users
# as well as guilds, which is what lets it run in a personal DM. Its two pure
# pieces are which attachment it picks and how it fits a long transcript into
# one Discord message; the interaction plumbing itself is glue.

_VOICE_FLAG = 1 << 13


def _msg(*attachments, voice: bool = False):
    return SimpleNamespace(
        attachments=list(attachments),
        flags=SimpleNamespace(value=_VOICE_FLAG if voice else 0),
    )


def _att(content_type: str | None, filename: str = "clip.ogg"):
    return SimpleNamespace(content_type=content_type, filename=filename)


def test_a_voice_message_is_picked():
    att = _att("audio/ogg")
    assert _audio_attachment(_msg(att, voice=True)) is att


def test_a_plain_audio_upload_is_picked_too():
    """Wider than the listener on purpose: someone who reached for the menu
    picked that message and means it, flag or no flag."""
    att = _att("audio/mpeg", "note.mp3")
    assert _audio_attachment(_msg(att, voice=False)) is att


def test_a_voice_message_without_a_content_type_still_works():
    """Discord does not always report content_type; the flag is the fallback."""
    att = _att(None)
    assert _audio_attachment(_msg(att, voice=True)) is att


@pytest.mark.parametrize(
    "message",
    [
        pytest.param(_msg(), id="no-attachments"),
        pytest.param(_msg(_att("image/png", "cat.png")), id="an-image"),
        pytest.param(_msg(_att(None, "notes.txt")), id="untyped-non-voice"),
    ],
)
def test_nothing_to_transcribe_returns_none(message):
    assert _audio_attachment(message) is None


def test_a_short_transcript_is_left_alone():
    assert fit_transcript("walk the site") == "walk the site"


def test_a_long_transcript_is_cut_and_says_so():
    text = "word " * 2000
    out = fit_transcript(text)
    assert len(out) < 2000  # fits one Discord message
    assert "truncated" in out


def test_the_cut_lands_on_a_word_boundary():
    out = fit_transcript("alpha " * 1000)
    body = out.split("\n\n")[0]
    assert not body.endswith("alph")
    assert body.endswith("alpha")


def test_the_limit_is_the_boundary_not_an_approximation():
    assert fit_transcript("x" * MAX_TRANSCRIPT_CHARS) == "x" * MAX_TRANSCRIPT_CHARS
    assert "truncated" in fit_transcript("x" * (MAX_TRANSCRIPT_CHARS + 1))


def test_the_menu_is_user_installable_and_allowed_in_dms():
    """The one piece of wiring worth pinning.

    Without `allowed_installs(users=True)` the command only exists inside
    guilds the bot is in, and the whole point of it — long-pressing a voice
    note in a personal DM — silently does not appear. Nothing else in the suite
    would notice, because every other test runs against a guild.
    """
    from unittest.mock import MagicMock

    from bot_modules.cogs.voice_transcription_cog import VoiceTranscriptionCog

    cog = VoiceTranscriptionCog(MagicMock())
    installs, contexts = cog.ctx_menu.allowed_installs, cog.ctx_menu.allowed_contexts
    assert installs is not None and installs.user is True
    assert contexts is not None
    assert contexts.dm_channel is True and contexts.private_channel is True
