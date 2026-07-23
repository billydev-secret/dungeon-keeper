"""Tests for the settings registry — the schema behind Billy-bot's config help.

The registry's own invariants are asserted at import (``_check_registry``), so
several of these are belt-and-braces: they'd fail at collection anyway, but a
named test says *which* rule broke.
"""

from __future__ import annotations

import pytest

from bot_modules.services import settings_registry as sr


# ── registry invariants ─────────────────────────────────────────────────────


def test_registry_is_non_empty_and_indexed_consistently():
    assert sr.FEATURES
    assert len(sr.SETTINGS_BY_KEY) == sum(len(f.settings) for f in sr.FEATURES)
    assert set(sr.FEATURES_BY_SLUG) == {f.slug for f in sr.FEATURES}


def test_no_privilege_key_is_writable():
    """A confirmed-by-click privilege escalation is still an escalation."""
    for key in sr.PRIVILEGE_KEYS:
        setting = sr.get_setting(key)
        if setting is not None:
            assert not setting.writable, key
    assert not (sr.writable_keys() & sr.PRIVILEGE_KEYS)


def test_no_secret_shaped_key_in_the_registry():
    for key in sr.SETTINGS_BY_KEY:
        assert not sr._SECRET_KEY_RE.search(key), key


def test_every_setting_has_a_known_kind_and_a_label():
    for key, s in sr.SETTINGS_BY_KEY.items():
        assert s.kind in sr.KINDS, key
        assert s.label.strip(), key


def test_every_feature_has_a_panel_a_blurb_and_a_required_setting():
    """A feature with no required setting can never be reported as unconfigured."""
    for f in sr.FEATURES:
        assert f.panel.strip(), f.slug
        assert f.blurb.strip(), f.slug
        if f.slug != "billy_bot":  # Billy-bot works with nothing set
            assert f.required_settings(), f.slug


def test_enable_key_when_present_is_one_of_the_features_settings():
    for f in sr.FEATURES:
        if f.enable_key:
            assert f.enable_key in {s.key for s in f.settings}, f.slug


def test_check_registry_rejects_a_writable_privilege_key(monkeypatch):
    bad = sr.Feature(
        slug="bad", label="Bad", panel="p", blurb="b",
        settings=(sr.Setting("admin_role_ids", "Admins", "role", writable=True),),
    )
    monkeypatch.setattr(sr, "FEATURES", (bad,))
    with pytest.raises(ValueError, match="privilege key"):
        sr._check_registry()


def test_check_registry_rejects_a_duplicate_key(monkeypatch):
    dup = sr.Setting("welcome_channel_id", "W", "channel")
    monkeypatch.setattr(sr, "FEATURES", (
        sr.Feature(slug="a", label="A", panel="p", blurb="b", settings=(dup,)),
        sr.Feature(slug="b", label="B", panel="p", blurb="b", settings=(dup,)),
    ))
    with pytest.raises(ValueError, match="two features"):
        sr._check_registry()


def test_check_registry_rejects_an_unknown_kind(monkeypatch):
    monkeypatch.setattr(sr, "FEATURES", (
        sr.Feature(slug="a", label="A", panel="p", blurb="b",
                   settings=(sr.Setting("welcome_channel_id", "W", "colour"),)),
    ))
    with pytest.raises(ValueError, match="unknown kind"):
        sr._check_registry()


# ── lookups ─────────────────────────────────────────────────────────────────


def test_get_setting_trims_and_misses_cleanly():
    assert sr.get_setting("  welcome_channel_id  ") is not None
    assert sr.get_setting("nope") is None
    assert sr.get_setting("") is None


def test_feature_for_key_round_trips():
    f = sr.feature_for_key("welcome_channel_id")
    assert f is not None and f.slug == "welcome"
    assert sr.feature_for_key("not_a_key") is None


def test_writable_keys_is_a_strict_subset():
    w = sr.writable_keys()
    assert w
    assert w < set(sr.SETTINGS_BY_KEY)  # some settings are panel-only


# ── admin_only tier ─────────────────────────────────────────────────────────


def test_writable_keys_narrows_for_a_non_admin():
    admin = sr.writable_keys(is_admin=True)
    managed = sr.writable_keys(is_admin=False)
    assert managed < admin  # strictly fewer
    assert "jailed_role_id" in admin
    assert "jailed_role_id" not in managed
    # Ordinary settings are in both tiers.
    assert "welcome_channel_id" in managed


def test_access_granting_roles_are_writable_but_admin_only():
    for key in (
        "jailed_role_id",
        "qa_role_id",
        "whisper_role_id",
        "greeter_role_id",
        "inactive_role_id",
    ):
        s = sr.get_setting(key)
        assert s is not None, key
        assert s.writable is True, key
        assert s.admin_only is True, key


def test_ping_only_roles_are_writable_without_admin():
    for key in ("welcome_ping_role_id", "guess_role_id"):
        s = sr.get_setting(key)
        assert s is not None and s.writable and not s.admin_only, key


def test_admin_only_never_covers_a_privilege_key():
    """The two are different mechanisms — a privilege key isn't a higher tier,
    it's off the table. No amount of permission should reach one."""
    for key in sr.PRIVILEGE_KEYS:
        assert key not in sr.writable_keys(is_admin=True), key


def test_no_dead_key_is_in_the_registry():
    """These have rows on live servers but nothing reads them — a change would
    silently do nothing, which is worse than refusing."""
    assert not (set(sr.SETTINGS_BY_KEY) & sr.DEAD_KEYS)


def test_check_registry_rejects_a_dead_key(monkeypatch):
    monkeypatch.setattr(sr, "FEATURES", (
        sr.Feature(slug="a", label="A", panel="p", blurb="b",
                   settings=(sr.Setting("nsfw_role_id", "NSFW", "role", writable=True),)),
    ))
    with pytest.raises(ValueError, match="nothing reads this key"):
        sr._check_registry()


def test_check_registry_rejects_admin_only_without_writable(monkeypatch):
    monkeypatch.setattr(sr, "FEATURES", (
        sr.Feature(slug="a", label="A", panel="p", blurb="b",
                   settings=(sr.Setting("welcome_channel_id", "W", "channel",
                                        writable=False, admin_only=True),)),
    ))
    with pytest.raises(ValueError, match="meaningless without writable"):
        sr._check_registry()


# ── is_set (what "configured" means) ────────────────────────────────────────


def test_is_set_channel_and_role_treat_zero_as_unset():
    ch = sr.get_setting("welcome_channel_id")
    assert ch is not None
    assert ch.is_set("123456789012345678") is True
    assert ch.is_set("0") is False
    assert ch.is_set("") is False
    assert ch.is_set(None) is False
    assert ch.is_set("  ") is False


def test_is_set_bool_reads_falsey_words_as_unset():
    flag = sr.get_setting("qa_enabled")
    assert flag is not None
    assert flag.is_set("1") is True
    assert flag.is_set("on") is True
    assert flag.is_set("0") is False
    assert flag.is_set("false") is False


def test_is_set_respects_an_explicit_default():
    s = sr.Setting("k", "K", "int", default="10")
    assert s.is_set("10") is False  # still at default → not deliberately configured
    assert s.is_set("11") is True


def test_is_set_text_any_nonblank_counts():
    s = sr.get_setting("welcome_message")
    assert s is not None
    assert s.is_set("hello") is True
    assert s.is_set("   ") is False


# ── coerce_value ────────────────────────────────────────────────────────────


def test_coerce_bool_accepts_synonyms_both_ways():
    s = sr.get_setting("welcome_ping_member")
    assert s is not None
    for raw in ("1", "on", "TRUE", "yes", "Enable", "enabled"):
        assert sr.coerce_value(s, raw) == "1"
    for raw in ("0", "off", "False", "no", "disable", "DISABLED"):
        assert sr.coerce_value(s, raw) == "0"
    with pytest.raises(ValueError, match="on/off"):
        sr.coerce_value(s, "perhaps")


def test_coerce_int_strips_commas_and_enforces_bounds():
    s = sr.get_setting("qa_reward")
    assert s is not None
    assert sr.coerce_value(s, "1,000") == "1000"
    with pytest.raises(ValueError, match="whole number"):
        sr.coerce_value(s, "many")
    with pytest.raises(ValueError, match="below"):
        sr.coerce_value(s, "-5")
    with pytest.raises(ValueError, match="above"):
        sr.coerce_value(s, "10000000")


def test_coerce_int_allows_the_exact_bounds():
    s = sr.Setting("k", "K", "int", minimum=1, maximum=10)
    assert sr.coerce_value(s, "1") == "1"
    assert sr.coerce_value(s, "10") == "10"


def test_coerce_text_enforces_choices_when_present():
    s = sr.Setting("k", "K", "text", choices=("a", "b"))
    assert sr.coerce_value(s, "a") == "a"
    with pytest.raises(ValueError, match="must be one of"):
        sr.coerce_value(s, "c")


def test_coerce_rejects_blank():
    s = sr.get_setting("welcome_message")
    assert s is not None
    with pytest.raises(ValueError, match="required"):
        sr.coerce_value(s, "   ")


def test_coerce_text_passes_through_trimmed():
    s = sr.get_setting("welcome_message")
    assert s is not None
    assert sr.coerce_value(s, "  Welcome!  ") == "Welcome!"
