"""Deletion scope modes — every mode clears only Discord messages and keeps
all server-side data (XP/activity/profile and the local message archive)."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from bot_modules.cogs import privacy_cog
from bot_modules.privacy.logic import (
    MODE_ALL,
    MODE_MEDIA,
    MODE_TEXT,
    confirm_button_label,
    message_has_media,
    message_matches_mode,
    render_confirm_prompt,
    render_deletion_summary,
    render_empty_summary,
)


def _msg(*, attachments=(), embeds=(), stickers=()) -> SimpleNamespace:
    return SimpleNamespace(
        attachments=list(attachments),
        embeds=[SimpleNamespace(type=t) for t in embeds],
        stickers=list(stickers),
    )


# ── classifier ──────────────────────────────────────────────────────────


def test_plain_text_is_not_media() -> None:
    assert not message_has_media(_msg())


def test_attachment_is_media() -> None:
    assert message_has_media(_msg(attachments=["photo.png"]))


def test_sticker_is_media() -> None:
    assert message_has_media(_msg(stickers=["blob"]))


def test_image_and_video_embeds_are_media() -> None:
    for kind in ("image", "video", "gifv"):
        assert message_has_media(_msg(embeds=[kind])), kind


def test_link_preview_is_not_media() -> None:
    """Discord auto-embeds any link; a article/rich preview is still text.

    This is the case that would otherwise sweep ordinary chatter into a
    "clear my images" run.
    """
    for kind in ("link", "article", "rich"):
        assert not message_has_media(_msg(embeds=[kind])), kind


def test_text_message_with_a_link_survives_a_media_scrub() -> None:
    linky = _msg(embeds=["article"])
    assert not message_matches_mode(linky, MODE_MEDIA)
    assert message_matches_mode(linky, MODE_TEXT)


# ── the discord.py seam ─────────────────────────────────────────────────
#
# The tests above use hand-made fakes, which prove the logic but not that
# discord.py actually emits these strings. If the real embed types ever drift,
# the classifier silently stops seeing media — and in `text` mode that means
# deleting the very images the member asked to keep. So pin against the real
# library rather than our own fakes.


def test_media_embed_types_exist_in_discord_py() -> None:
    import typing

    from discord.types.embed import EmbedType

    from bot_modules.privacy.logic import _MEDIA_EMBED_TYPES

    known = set(typing.get_args(EmbedType))
    assert _MEDIA_EMBED_TYPES <= known, (
        f"embed types drifted; discord.py knows {sorted(known)}"
    )


def test_classifier_against_real_discord_embeds() -> None:
    import discord

    for kind in ("image", "video", "gifv"):
        embed = discord.Embed.from_dict({"type": kind})
        assert message_has_media(_real_embed_msg(embed)), kind

    for kind in ("rich", "link", "article"):
        embed = discord.Embed.from_dict({"type": kind})
        assert not message_has_media(_real_embed_msg(embed)), kind


def _real_embed_msg(embed) -> SimpleNamespace:
    return SimpleNamespace(attachments=[], embeds=[embed], stickers=[])


def test_a_posted_image_url_is_media_not_text() -> None:
    """No attachment, but Discord embeds it as an image — the member posted a
    picture, so a text scrub must not take it."""
    import discord

    msg = _real_embed_msg(discord.Embed.from_dict({"type": "image"}))
    assert message_matches_mode(msg, MODE_MEDIA)
    assert not message_matches_mode(msg, MODE_TEXT)


# ── mode selection ──────────────────────────────────────────────────────


def test_modes_partition_messages() -> None:
    photo = _msg(attachments=["a.png"])
    chat = _msg()
    # Every message lands in exactly one of media/text, and both land in all.
    for m in (photo, chat):
        assert message_matches_mode(m, MODE_ALL)
        assert message_matches_mode(m, MODE_MEDIA) != message_matches_mode(m, MODE_TEXT)
    assert message_matches_mode(photo, MODE_MEDIA)
    assert message_matches_mode(chat, MODE_TEXT)


# ── copy ────────────────────────────────────────────────────────────────
#
# The load-bearing property of this copy: it must never tell anyone their
# server-side data (XP/activity/profile/message archive) was deleted, because
# it never is. These tests assert that positively.


def test_summary_states_data_is_retained_for_moderation() -> None:
    for mode in (MODE_ALL, MODE_MEDIA, MODE_TEXT):
        out = render_deletion_summary(deleted=3, failed=0, replaced=0, mode=mode)
        assert "kept for moderation" in out
        assert "cleared" not in out.lower()
        assert "erased" not in out.lower()


def test_empty_summary_states_data_is_untouched() -> None:
    for mode in (MODE_ALL, MODE_MEDIA, MODE_TEXT):
        out = render_empty_summary(mode=mode)
        assert "Nothing else was touched" in out
        assert "cleared" not in out.lower()


def test_full_mode_summary_exact_copy() -> None:
    out = render_deletion_summary(deleted=5, failed=1, replaced=2, mode=MODE_ALL)
    assert out == (
        "All done. Here's what was removed:\n"
        "Messages deleted from Discord: **5**\n"
        "XP, activity, profile, and the server's own message records: "
        "**kept for moderation**.\n"
        "Forum posts replaced with tombstone: **2**\n"
        "Messages that couldn't be deleted (no access): **1**"
    )


def test_self_prompt_discloses_the_retained_records() -> None:
    """Say the records are kept before the click, not after."""
    out = render_confirm_prompt(mode=MODE_ALL)
    assert "keeps its own copy" in out
    assert "not from those records" in out
    assert "stay exactly as they are" in out


def test_admin_prompt_also_states_data_is_kept() -> None:
    """The mod-facing prompt must make the same retention promise, not claim erasure."""
    out = render_confirm_prompt(mode=MODE_ALL, subject="@ben")
    assert "@ben's" in out
    assert "keeps its own copy" in out
    assert "erased" not in out.lower()


def test_partial_prompt_names_only_its_slice() -> None:
    out = render_confirm_prompt(mode=MODE_MEDIA)
    assert "images & files" in out
    assert "stay exactly as they are" in out
    # No mode describes itself as deleting everything.
    assert "everything" not in out.lower()


def test_button_label_names_the_real_scope() -> None:
    assert confirm_button_label(MODE_ALL) == "Yes, delete my messages"
    assert (
        confirm_button_label(MODE_ALL, self_service=False)
        == "Yes, delete their messages"
    )
    assert confirm_button_label(MODE_MEDIA) == "Yes, delete my images & files"
    assert (
        confirm_button_label(MODE_TEXT, self_service=False)
        == "Yes, delete their text messages"
    )
    # "everything" overstates the scope and must never be a label.
    for mode in (MODE_ALL, MODE_MEDIA, MODE_TEXT):
        assert "everything" not in confirm_button_label(mode).lower()


def test_button_labels_fit_discord_limit() -> None:
    for mode in (MODE_ALL, MODE_MEDIA, MODE_TEXT):
        for self_service in (True, False):
            assert len(confirm_button_label(mode, self_service=self_service)) <= 80


def test_confirm_view_actually_relabels_the_rendered_button() -> None:
    """The label is set on the decorated button after construction.

    Worth pinning: if that attribute stopped resolving to the bound Button the
    assignment would no-op silently and the button would keep its placeholder
    label instead of the real scope.
    """
    view = privacy_cog._ConfirmDeleteView(
        actor_id=1, confirm_label="Yes, delete my images & files"
    )
    labels = [getattr(c, "label", None) for c in view.children]
    assert "Yes, delete my images & files" in labels
    assert "Cancel" in labels
    assert "Yes, delete my messages" not in labels


def test_confirm_view_default_label_is_messages_scope() -> None:
    view = privacy_cog._ConfirmDeleteView(actor_id=1)
    labels = [getattr(c, "label", None) for c in view.children]
    assert "Yes, delete my messages" in labels
    assert not any("everything" in (label or "").lower() for label in labels)


# ── the safety property, end to end ─────────────────────────────────────


@pytest.fixture
def _wired(monkeypatch):
    """Drive _run_deletion with the network and DB stubbed out.

    The safety property worth guarding: no mode may ever call the DB-purge path.
    ``purge_user_data`` is no longer even imported by the cog, so we assert the
    open_db seam is never entered — if _run_deletion regrew a purge it would have
    to open the DB, and this fixture records every open.
    """
    calls: dict[str, list] = {"db_opened": [], "scanned_with": []}

    async def _fake_find(guild, user_id, *, on_progress=None, predicate=None):
        calls["scanned_with"].append(predicate)
        return [(11, 22)]

    async def _fake_delete(guild, user_id, msg_rows, *, on_progress=None):
        return (len(msg_rows), 0, 0)

    async def _fake_edit(interaction, content):
        return None

    monkeypatch.setattr(privacy_cog, "find_user_messages", _fake_find)
    monkeypatch.setattr(privacy_cog, "_delete_discord_messages", _fake_delete)
    monkeypatch.setattr(privacy_cog, "_edit_or_send", _fake_edit)
    assert not hasattr(privacy_cog, "purge_user_data"), (
        "the cog must not import the hard-erasure purge at all"
    )

    @contextmanager
    def _open_db():
        calls["db_opened"].append(True)
        yield MagicMock()

    ctx = MagicMock()
    ctx.open_db = _open_db
    return calls, ctx


async def _run(ctx, mode: str) -> None:
    guild = MagicMock()
    guild.id = 1
    await privacy_cog._run_deletion(ctx, guild, 42, MagicMock(), mode=mode)


async def test_no_mode_touches_the_database(_wired) -> None:
    calls, ctx = _wired
    for mode in (MODE_ALL, MODE_MEDIA, MODE_TEXT):
        await _run(ctx, mode)
    assert calls["db_opened"] == [], "deletion must not touch server-side data in any mode"


async def test_scrub_passes_a_predicate_and_full_mode_does_not(_wired) -> None:
    """The filter has to reach the scan: without it every message is collected."""
    calls, ctx = _wired
    await _run(ctx, MODE_MEDIA)
    predicate = calls["scanned_with"][0]
    assert predicate is not None
    assert predicate(_msg(attachments=["a.png"]))
    assert not predicate(_msg())

    calls["scanned_with"].clear()
    await _run(ctx, MODE_ALL)
    assert calls["scanned_with"] == [None]
