"""Voice transcription: the transcript line, and the delete-after dial.

The cog's listener is glue over faster-whisper and Discord; what is worth
pinning is the text it posts and the config round trip that decides whether the
audio survives.
"""
from __future__ import annotations

import pytest

from types import SimpleNamespace

from bot_modules.core.db_utils import open_db
from bot_modules.cogs.voice_transcription_cog import (
    _audio_attachment,
    transcript_prefix,
)
from bot_modules.services.voice_transcription_service import (
    MAX_TRANSCRIPT_CHARS,
    fit_transcript,
    split_transcript,
    was_truncated,
    get_config,
    set_config,
)

GUILD = 1469491362444480666


@pytest.mark.parametrize(
    ("speaker", "expected"),
    [
        pytest.param("Billy", "📝 **Billy:** ", id="plain"),
        # A display name is member-controlled text landing in a bot message; an
        # unescaped * or _ would reformat the transcript that follows it.
        pytest.param("*Billy*", "📝 **\\*Billy\\*:** ", id="escapes-markdown"),
        pytest.param(
            "under_score", "📝 **under\\_score:** ", id="escapes-underscore"
        ),
    ],
)
def test_the_transcript_names_the_speaker_and_escapes_the_name(speaker, expected):
    assert transcript_prefix(speaker) == expected


def test_the_transcript_is_not_a_reply_so_it_carries_attribution():
    """Regression guard for why this function exists at all: the transcript used
    to be a reply to the voice message. Deleting the audio leaves a reply
    dangling, so the line has to say whose note it was on its own."""
    line = transcript_prefix("Billy") + "hello"
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


def test_the_speaker_prefix_comes_out_of_the_one_message_budget():
    """The header is part of the message Discord measures, so it must be paid for.

    Sending prefix + a full-budget body was 1900 characters of transcript plus
    a header on top -- fine until a long display name pushed the whole thing
    over the 2000 cap and Discord rejected the post outright.
    """
    prefix = transcript_prefix("A" * 32)
    assert len(fit_transcript("word " * 2000, prefix=prefix)) <= MAX_TRANSCRIPT_CHARS
    assert fit_transcript("hi", prefix=prefix).startswith(prefix)


# ── split_transcript ─────────────────────────────────────────────────────────


def _rejoin(parts, prefix=""):
    """The parts' words back as one string, so nothing lost can hide in them."""
    body = [p[len(prefix):] if i == 0 else p for i, p in enumerate(parts)]
    return " ".join(" ".join(body).split())


def test_a_transcript_that_fits_stays_one_message():
    assert split_transcript("walk the site") == ["walk the site"]


def test_a_long_transcript_spans_messages_instead_of_being_cut():
    text = "word " * 2000
    parts = split_transcript(text)
    assert len(parts) > 1
    assert all(len(p) <= MAX_TRANSCRIPT_CHARS for p in parts)
    assert not any("truncated" in p for p in parts)


def test_splitting_loses_nothing():
    text = "word " * 2000
    assert _rejoin(split_transcript(text)) == text.strip()


def test_there_is_no_cap_on_the_number_of_parts():
    """An explicit press wants the whole note, however long the recording ran."""
    parts = split_transcript("word " * 20_000)
    assert len(parts) > 10
    assert _rejoin(parts) == ("word " * 20_000).strip()


def test_only_the_first_part_carries_the_speaker_header():
    prefix = transcript_prefix("Billy")
    parts = split_transcript("alpha " * 2000, prefix=prefix)
    assert parts[0].startswith(prefix)
    assert not any(p.startswith(prefix) for p in parts[1:])
    assert all("Billy" not in p for p in parts[1:])


def test_the_header_is_paid_for_out_of_the_first_part():
    prefix = transcript_prefix("A" * 32)
    parts = split_transcript("alpha " * 2000, prefix=prefix)
    assert all(len(p) <= MAX_TRANSCRIPT_CHARS for p in parts)
    assert _rejoin(parts, prefix) == ("alpha " * 2000).strip()


def test_the_parts_break_on_word_boundaries():
    parts = split_transcript("alpha " * 2000)
    assert all(p.startswith("alpha") and p.endswith("alpha") for p in parts)


@pytest.mark.parametrize(
    "length,expected_parts",
    [
        pytest.param(MAX_TRANSCRIPT_CHARS - 1, 1, id="just-under"),
        pytest.param(MAX_TRANSCRIPT_CHARS, 1, id="exactly-the-limit"),
        pytest.param(MAX_TRANSCRIPT_CHARS + 1, 2, id="one-over"),
    ],
)
def test_the_split_point_is_the_boundary_not_an_approximation(length, expected_parts):
    assert len(split_transcript("x" * length)) == expected_parts


def test_one_unbroken_word_longer_than_the_budget_is_cut_mid_word():
    """There is no boundary to find, and showing it beats showing nothing."""
    parts = split_transcript("x" * 4000)
    assert len(parts) == 3
    assert all(len(p) <= MAX_TRANSCRIPT_CHARS for p in parts)
    assert "".join(parts) == "x" * 4000


def test_an_empty_transcript_yields_no_messages():
    """Otherwise a caller posts a bare speaker header with nothing after it."""
    assert split_transcript("") == []
    assert split_transcript("   \n  ", prefix=transcript_prefix("Billy")) == []


def test_a_split_inside_markdown_is_not_repaired():
    """Pinned, not fixed: Whisper writes prose, so the emphasis is the speaker's.

    A ``*`` that lands either side of a break renders literally rather than as
    emphasis. Balancing it would mean rewriting what someone said, so the split
    leaves the characters exactly as they were spoken.
    """
    text = "**" + ("word " * 500) + "**"
    parts = split_transcript(text)
    assert len(parts) > 1
    assert parts[0].startswith("**")
    assert "".join(parts).count("*") == 4


def test_the_two_posting_paths_take_the_split_and_the_fit():
    """The one bit of wiring worth pinning: on-demand splits, automatic fits.

    The automatic listener posted raw text for its whole life -- over the cap,
    Discord rejected the message and the note transcribed to nothing.
    """
    import inspect

    from bot_modules.cogs import voice_transcription_cog as cog

    menu = inspect.getsource(cog.VoiceTranscriptionCog._transcribe_context_menu)
    assert "split_transcript(" in menu and "fit_transcript(" not in menu

    listener = inspect.getsource(cog.VoiceTranscriptionCog._on_message)
    assert "fit_transcript(" in listener
    assert "channel.send(text" not in listener


def test_a_truncated_transcript_is_recognisable():
    """The listener has to tell a whole transcript from a cut one."""
    prefix = transcript_prefix("Billy")
    assert not was_truncated(fit_transcript("walk the site", prefix=prefix))
    assert was_truncated(fit_transcript("word " * 2000, prefix=prefix))


def test_a_truncated_transcript_does_not_authorise_deleting_the_audio():
    """Otherwise the cut tail is destroyed with nothing left to recover it from.

    The clip is the only copy of what did not fit, so delete-after-transcribe
    has to stand down when the transcript could not carry the whole note.
    """
    import inspect

    from bot_modules.cogs import voice_transcription_cog as cog

    listener = inspect.getsource(cog.VoiceTranscriptionCog._on_message)
    gate = listener.index("delete_after_transcribe")
    assert "was_truncated(posted)" in listener[gate:]
    assert listener.index("was_truncated(posted)") < listener.index(
        "await message.delete()"
    )


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
