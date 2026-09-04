"""Tests for survivor/views.py's pure copy helpers.

The pick menus are the one Survivor surface that formats a clock by hand
(select labels can't carry Discord timestamps), so the zone has to be in
the text itself (2026-09-02 review, survivor-186).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bot_modules.survivor.views import kickoff_label, pick_panel_content

# Sunday 2026-09-13 17:00 UTC = 10:00 AM in a UTC-7 guild.
_SUN_17Z = datetime(2026, 9, 13, 17, 0, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        pytest.param(-7.0, "Sun 10:00 AM server time", id="pacific"),
        pytest.param(-4.0, "Sun 1:00 PM server time", id="eastern"),
        pytest.param(0.0, "Sun 5:00 PM server time", id="utc"),
        pytest.param(7.0, "Mon 12:00 AM server time", id="rolls-to-monday"),
    ],
)
def test_kickoff_label_names_the_zone(offset, expected):
    assert kickoff_label(_SUN_17Z, offset) == expected


def test_pick_panel_content_says_server_time_once():
    content = pick_panel_content(3)
    assert content.startswith("Week 3 — pick a team to **win**")
    assert content.count("server time") == 1
