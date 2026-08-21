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
