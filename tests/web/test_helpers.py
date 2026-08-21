"""Shared helpers behind the dashboard route handlers."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from web_server.helpers import parse_time_of_day


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
        pytest.param("24:00", id="hour-past-midnight"),
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
    ("raw", "minutes"),
    [
        pytest.param("12:60", 780, id="12:60-becomes-13:00"),
        pytest.param("0:90", 90, id="0:90-becomes-01:30"),
    ],
)
def test_an_out_of_range_minute_carries_into_the_hour(raw, minutes):
    """Documented, not endorsed — this is what the three copies already did.

    Only the total is range-checked, so a minute over 59 rolls forward
    instead of being rejected: "12:60" schedules at 13:00. Harmless in
    practice because the dashboard sends an <input type="time">, but a
    hand-rolled API call gets a silent reinterpretation rather than a 400.
    Left as-is here: the sweep that moved this function was not the place to
    start rejecting input three panels currently accept.
    """
    assert parse_time_of_day(raw) == minutes


def test_the_field_name_travels_into_the_message():
    """Three panels share this; the 400 has to point at the right input box.
    Announcements calls its field post_time, the other two call it time."""
    with pytest.raises(HTTPException) as exc:
        parse_time_of_day("nope", field="post_time")
    assert exc.value.detail == "post_time must be 'HH:MM'"

    with pytest.raises(HTTPException) as exc:
        parse_time_of_day("99:99", field="post_time")
    assert exc.value.detail == "post_time out of range"
