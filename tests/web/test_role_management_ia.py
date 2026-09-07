"""Moderation → Role Management, and the split that came with it (2026-09-07).

Role work used to be spread over three places: Role Grants / Reaction Roles /
Bot-Managed Roles under Config → Roles, Grant Audit at the top of Moderation →
Audit Logs, and the promotion-review dials buried in the XP & Leveling form.
They are one moderator job, so they now sit under one heading, in the order
that job runs.

The promotion-review dials moving is not only tidying. XP & Leveling saves as
one payload, so changing *any* XP dial rewrote the promotion keys too — which
is how ``promotion_review_ping_role_id`` reached production as 0 and review
cards fell back to pinging @moderator. Splitting the page splits the payload,
and the assertions below are what keeps those three keys off the XP form.

Source-level, deliberately: the nav lives in ``SECTIONS`` in app.js and the
payloads are literal object keys, so a string sweep catches a regression on
every commit rather than only in the browser tier.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_JS = Path(__file__).resolve().parents[2] / "src" / "web_server" / "static" / "js"
_PANELS = _JS / "panels"

# The heading's membership, in nav order: decide what can be handed out, let
# members self-serve, provision the roles the bot needs itself, review the
# members who are up for one, then read back who granted what.
_ROLE_MANAGEMENT_IDS = [
    "config-roles",
    "role-menus",
    "bot-roles",
    "promotion-reviews",
    "grant-audit",
]

# The three keys that moved off the XP form. Config key names are unchanged —
# only the page they are edited on moved, so no stored value needs migrating.
_MOVED_KEYS = [
    "level_5_log_channel_id",
    "promotion_review_ping_role_id",
    "promotion_review_grant_role_id",
]


def _app_src() -> str:
    return (_JS / "app.js").read_text(encoding="utf-8")


def _section_src(section_id: str, next_section_id: str) -> str:
    src = _app_src()
    start = src.index(f'id: "{section_id}",')
    return src[start : src.index(f'id: "{next_section_id}",', start)]


def _group_src(section_src: str, heading: str) -> str:
    start = section_src.index(f'{{ heading: "{heading}", items: [')
    return section_src[start : section_src.index("]},", start)]


def test_role_management_group_holds_the_five_role_pages_in_order():
    group = _group_src(_section_src("moderation", "config"), "Role Management")
    ids = re.findall(r'id:\s*"([A-Za-z0-9_-]+)"', group)
    assert ids == _ROLE_MANAGEMENT_IDS


def test_no_role_page_was_renamed_on_the_way():
    """The freeze is on ids: deep links, `help:` mappings and usage telemetry
    all key off them, so a regroup may move an entry but never rename it."""
    ids = set(re.findall(r'\bid:\s*"([A-Za-z0-9_-]+)"', _app_src()))
    survivors = [pid for pid in _ROLE_MANAGEMENT_IDS if pid != "promotion-reviews"]
    missing = [pid for pid in survivors if pid not in ids]
    assert not missing, f"regrouping was supposed to keep these ids: {missing}"


def test_grant_audit_left_the_audit_logs_group():
    """It was the one entry a moderator could open there, sitting below nine
    locked rows; it reads back the grants Role Management hands out."""
    group = _group_src(_section_src("moderation", "config"), "Audit Logs")
    assert "grant-audit" not in group


def test_config_no_longer_carries_a_roles_heading():
    """The whole heading moved. Auto-Role and Discord Onboarding stayed behind
    on purpose — they are steps in the New Members narrative, not role admin."""
    config = _section_src("config", "economy")
    assert '{ heading: "Roles", items: [' not in config
    assert "config-auto-role" in config and "onboarding" in config


def test_promotion_reviews_names_a_panel_that_exists():
    assert (_PANELS / "promotion-reviews.js").is_file()


@pytest.mark.parametrize("key", _MOVED_KEYS)
def test_xp_settings_no_longer_touches_a_promotion_key(key):
    """The regression guard for the ping role reaching prod as 0: saving an XP
    dial must not write these three at all."""
    src = (_PANELS / "xp-settings.js").read_text(encoding="utf-8")
    assert key not in src


@pytest.mark.parametrize("key", _MOVED_KEYS)
def test_promotion_reviews_edits_each_moved_key(key):
    src = (_PANELS / "promotion-reviews.js").read_text(encoding="utf-8")
    assert f'data-picker="{key}"' in src
    assert f"{key}:" in src


def test_promotion_reviews_sends_only_its_own_three_fields():
    """The whole point of the split: a partial PUT to /api/config/xp leaves
    every field it isn't sent alone (_apply_config_fields skips None), so this
    page cannot disturb an XP dial the way the shared form disturbed these."""
    src = (_PANELS / "promotion-reviews.js").read_text(encoding="utf-8")
    body = src[src.index('apiPut("/api/config/xp"') : src.index("});", src.index('apiPut("/api/config/xp"'))]
    sent = re.findall(r"^\s*([a-z0-9_]+):", body, re.M)
    assert sorted(sent) == sorted(_MOVED_KEYS)


def test_xp_settings_keeps_the_dials_that_are_still_its_own():
    """Level 5 Role and the Level-Up log stay: they are level plumbing, not the
    review card. The level-up hint points at the new page because pointing both
    channels at one place is what suppresses the duplicate level-5 notice."""
    src = (_PANELS / "xp-settings.js").read_text(encoding="utf-8")
    assert 'data-picker="level_5_role_id"' in src
    assert 'data-picker="level_up_log_channel_id"' in src
    assert "#/promotion-reviews" in src


def test_promotion_reviews_and_grant_audit_point_at_each_other():
    """``dashboard_ia.md`` requires audit↔config ``related:`` links to be
    bidirectional across sibling pairs. Grant Audit is where you read back what
    a Promotion Reviews Grant press did, so the cross-link has to survive in
    both directions or the reader only ever finds one of them."""
    group = _group_src(_section_src("moderation", "config"), "Role Management")
    for a, b in (("promotion-reviews", "grant-audit"), ("grant-audit", "promotion-reviews")):
        entry = group[group.index(f'id: "{a}",') :]
        entry = entry[: entry.index("},")]
        assert f'"{b}"' in entry, f"{a} does not link back to {b}"
