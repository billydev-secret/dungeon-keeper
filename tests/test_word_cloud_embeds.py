"""Tests for the word cloud card.

A pure builder, so the copy is asserted directly. The escaping test is the
point of the file: a display name is member-supplied text going into an embed
description, and `docs/embed_style_guide.md` requires it be escaped.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from bot_modules.word_cloud.embeds import FILENAME, build_cloud_embed


def _embed(**kw):
    params = dict(
        message_count=1234,
        member_name=None,
        scope_label="#general",
        span=timedelta(days=7),
        source_label="stored history",
        by_sentiment=False,
        notes=[],
        color=None,
    )
    params.update(kw)
    return build_cloud_embed(**params)


def test_card_states_the_count_scope_window_and_source():
    desc = _embed().description
    assert "1,234" in desc
    assert "#general" in desc
    assert "7 days" in desc
    assert "stored history" in desc


def test_card_labels_the_window_actually_covered():
    """A 7-day ask clamped to 10 minutes must not still read "7 days"."""
    desc = _embed(
        span=timedelta(minutes=10), source_label="the last few minutes of live chat"
    ).description
    assert "10 minutes" in desc
    assert "7 days" not in desc


def test_card_names_a_member_when_one_was_asked_for():
    assert "Robin" in _embed(member_name="Robin").description


@pytest.mark.parametrize("name", ["__Robin__", "**Robin**", "`Robin`", "*Robin*"])
def test_card_escapes_a_members_display_name(name):
    """An unescaped name reformats the description around it."""
    desc = _embed(member_name=name).description
    assert name not in desc
    assert "Robin" in desc


def test_card_never_carries_a_raw_mention():
    """An embed mention renders as a bare id to anyone who hasn't cached the
    user — see docs/embed_style_guide.md."""
    assert "<@" not in _embed(member_name="Robin").description


def test_card_explains_the_colours_only_when_they_mean_something():
    assert any(f.name == "Colour" for f in _embed(by_sentiment=True).fields)
    assert not any(f.name == "Colour" for f in _embed(by_sentiment=False).fields)


def test_card_carries_the_notes_it_was_given():
    field = next(f for f in _embed(notes=["one", "two"]).fields if f.name == "Worth knowing")
    assert "one" in field.value
    assert "two" in field.value


def test_card_omits_the_notes_field_when_there_are_none():
    assert not any(f.name == "Worth knowing" for f in _embed().fields)


def test_card_points_its_image_at_the_attachment():
    assert _embed().image.url == f"attachment://{FILENAME}"
