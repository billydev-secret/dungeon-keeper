"""The AI advisor's settings registry must agree with the owning panel.

The 2026-08-29 IA audit found six duplicated-control defects with one root:
the registry is a second, hand-written schema for keys the panels own, and it
had drifted — different bounds, a different *unit*, missing side effects, and
one feature description that matched nothing in the codebase. These tests pin
the registry to the routes' own validation so the drift class stays fixed:
where a route model bounds a key, the registry entry must carry the same
bounds; where a panel write has side effects the registry can't reproduce,
the key must not be model-writable.
"""

from __future__ import annotations

import pytest

from bot_modules.services.settings_registry import FEATURES, SETTINGS_BY_KEY
from web_server.routes.bios import BiosConfigBody


def _setting(key: str):
    for feature in FEATURES:
        for s in feature.settings:
            if s.key == key:
                return feature, s
    raise AssertionError(f"registry has no setting {key!r}")


def _field_bounds(model, name: str) -> tuple[int, int]:
    field = model.model_fields[name]
    ge = le = None
    for m in field.metadata:
        ge = getattr(m, "ge", ge)
        le = getattr(m, "le", le)
    assert ge is not None and le is not None, f"{name} carries no ge/le bounds"
    return ge, le


@pytest.mark.parametrize(
    "key,model,field",
    [
        pytest.param("bios_questions_per_bio", BiosConfigBody, "questions_per_bio",
                     id="bios-questions"),
        pytest.param("bios_archive_grace", BiosConfigBody, "archive_grace",
                     id="bios-archive-grace"),
    ],
)
def test_registry_bounds_match_the_owning_route(key, model, field):
    """An advisor Apply must not accept a value the panel's route would
    reject — the panel could then never re-save the form (the QA daily-cap
    5000 case), and the enforcing reader may not clamp at all."""
    _, setting = _setting(key)
    ge, le = _field_bounds(model, field)
    assert (setting.minimum, setting.maximum) == (ge, le), (
        f"{key}: registry allows {setting.minimum}-{setting.maximum}, "
        f"route allows {ge}-{le}"
    )


def test_bios_archive_grace_is_labelled_in_seconds():
    """The 86,400x bug: the panel writes *seconds* the wizard room stays open
    (0-3600); the registry described 'days before an old bio is archived'
    (0-3650) for the same key, so an advisor-applied 'days' value was
    enforced as seconds. The unit belongs in the label."""
    _, setting = _setting("bios_archive_grace")
    assert "second" in setting.label.lower(), setting.label
    assert "day" not in setting.label.lower(), setting.label


def test_side_effect_keys_are_not_model_writable():
    """These keys' panel writes run extra plumbing a bare config write skips:
    the inactive pair goes through POST /config/inactive/channel (creates the
    role, grants and revokes channel access), and the intake reference PUT is
    the only caller of sync_channel. An advisor Apply writing the raw key
    strands the old channel/blocks exactly as the setup flows document."""
    for key in ("inactive_channel_id", "inactive_role_id",
                "intake_reference_channel_id"):
        _, setting = _setting(key)
        assert not setting.writable, f"{key} must not be model-writable"


def test_welcome_trigger_is_a_closed_choice():
    """The Welcome panel is a two-option select and enforcement matches only
    the exact strings 'join' and 'verified' — any other advisor-applied text
    silently disables welcome messages."""
    _, setting = _setting("welcome_trigger")
    assert setting.choices and set(setting.choices) == {"join", "verified"}, (
        setting.choices
    )


def test_qa_keys_are_not_in_the_registry():
    """The registry sold a fictional 'Q&A rewards' feature ('coins for
    answering questions in a help channel', panel 'Config → Q&A rewards')
    that never existed; the qa_* keys belong to Dev → QA Tracker, whose
    panel is the sole writer. The whole feature came out: an entry both
    duplicated the write path (with 10-100x the route's bounds) and made
    gap detection nudge every unconfigured guild to set up dev tooling."""
    for key in ("qa_enabled", "qa_channel_id", "qa_reward",
                "qa_daily_cap", "qa_role_id"):
        assert key not in SETTINGS_BY_KEY, f"{key} is back in the registry"


def test_photo_left_all_game_types():
    """PUT /api/games/config/games/photo was a second live write path to the
    same games_game_config row the standalone Photo Challenge panel owns —
    'photo' stayed in ALL_GAME_TYPES when the feature went standalone."""
    from web_server.routes.games import ALL_GAME_TYPES

    assert "photo" not in ALL_GAME_TYPES
