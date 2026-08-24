"""Shared helpers behind the dashboard route handlers."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from types import SimpleNamespace

from web_server.helpers import (
    channel_in_guild,
    parse_time_of_day,
    require_channel_in_guild,
)


@pytest.mark.parametrize(
    ("raw", "minutes"),
    [
        pytest.param("00:00", 0, id="midnight"),
        pytest.param("09:30", 570, id="morning"),
        pytest.param("23:59", 1439, id="last-minute"),
        pytest.param("7:05", 425, id="unpadded-hour"),
    ],
)
def test_parses_a_time_of_day_into_minutes(raw, minutes):
    assert parse_time_of_day(raw) == minutes


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("", id="empty"),
        pytest.param("noon", id="words"),
        pytest.param("12", id="no-colon"),
        pytest.param("12:00:00", id="with-seconds"),
        pytest.param("aa:bb", id="non-numeric"),
        pytest.param(None, id="missing"),
    ],
)
def test_unparseable_input_is_a_400(raw):
    with pytest.raises(HTTPException) as exc:
        parse_time_of_day(raw)  # type: ignore[arg-type]
    assert exc.value.status_code == 400
    assert "must be 'HH:MM'" in exc.value.detail


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("25:00", id="hour-too-big"),
        pytest.param("-1:00", id="negative"),
    ],
)
def test_a_time_outside_the_day_is_a_400(raw):
    with pytest.raises(HTTPException) as exc:
        parse_time_of_day(raw)
    assert exc.value.status_code == 400
    assert "out of range" in exc.value.detail


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("10:75", id="minute-over-59"),
        pytest.param("0:90", id="minute-way-over"),
        pytest.param("1:-30", id="negative-minute"),
        pytest.param("24:00", id="hour-past-midnight"),
    ],
)
def test_an_out_of_range_component_is_a_400(raw):
    """Not just the total. The three copies this replaces checked only the
    sum, so "10:75" was accepted and stored as 11:15 — a typoed post time got
    a success toast and a job that fired 75 minutes off. The dashboard sends
    an <input type="time"> so nothing reaches this in practice; a hand-rolled
    API call now gets a 400 instead of a silent reinterpretation."""
    with pytest.raises(HTTPException) as exc:
        parse_time_of_day(raw)
    assert exc.value.status_code == 400
    assert "out of range" in exc.value.detail


def test_the_field_name_travels_into_the_message():
    """Three panels share this; the 400 has to point at the right input box.
    Announcements calls its field post_time, the other two call it time."""
    with pytest.raises(HTTPException) as exc:
        parse_time_of_day("nope", field="post_time")
    assert exc.value.detail == "post_time must be 'HH:MM'"

    with pytest.raises(HTTPException) as exc:
        parse_time_of_day("99:99", field="post_time")
    assert exc.value.detail == "post_time out of range"


# ── channel_in_guild ──────────────────────────────────────────────────
#
# The guard on the write routes that store a channel to post into later.
# Its interesting property is what it does when it *can't* tell: it allows,
# rather than blocking a save the admin did nothing wrong to make.


def _ctx(bot=None):
    return SimpleNamespace(bot=bot) if bot is not None else SimpleNamespace()


def _bot_with(guild_id=None, channel_id=None):
    guild = None
    if guild_id is not None:
        guild = SimpleNamespace(
            get_channel=lambda cid: object() if cid == channel_id else None
        )
    return SimpleNamespace(get_guild=lambda gid: guild if gid == guild_id else None)


def test_a_channel_the_bot_can_see_passes():
    assert channel_in_guild(_ctx(_bot_with(7, 55)), 7, 55) is True


def test_a_channel_in_another_server_is_refused():
    """The mistake this exists to catch: pasting an id from elsewhere."""
    assert channel_in_guild(_ctx(_bot_with(7, 55)), 7, 999) is False


def test_an_uncached_guild_allows():
    """Unanswerable, not wrong — the post path re-checks when it actually sends."""
    assert channel_in_guild(_ctx(_bot_with(7, 55)), 12345, 55) is True


def test_no_bot_attached_allows():
    """The dashboard can run without the bot; a save must not depend on it."""
    assert channel_in_guild(_ctx(), 7, 55) is True
    assert channel_in_guild(SimpleNamespace(bot=None), 7, 55) is True


def test_require_raises_a_400_with_the_message_both_routes_used():
    with pytest.raises(HTTPException) as exc:
        require_channel_in_guild(_ctx(_bot_with(7, 55)), 7, 999)
    assert exc.value.status_code == 400
    assert exc.value.detail == "Channel is not in this server"


def test_require_is_silent_when_the_channel_is_fine():
    require_channel_in_guild(_ctx(_bot_with(7, 55)), 7, 55)
