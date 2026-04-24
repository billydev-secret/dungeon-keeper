from __future__ import annotations

from types import SimpleNamespace


from utils import (
    format_guild_for_log,
    format_user_for_log,
    resolve_guild_for_log,
    resolve_user_for_log,
)


# ── format_user_for_log ───────────────────────────────────────────────

def test_no_args_returns_unknown():
    assert format_user_for_log() == "unknown user"


def test_user_id_only():
    assert format_user_for_log(user_id=42) == "user 42"


def test_display_name_matches_username():
    user = SimpleNamespace(id=1, display_name="Alice", name="Alice")
    assert format_user_for_log(user) == "Alice (1)"  # type: ignore[arg-type]


def test_display_name_differs_from_username():
    user = SimpleNamespace(id=1, display_name="Wonderland Alice", name="alice99")
    assert format_user_for_log(user) == "Wonderland Alice [alice99] (1)"  # type: ignore[arg-type]


def test_display_name_none_falls_back_to_username():
    user = SimpleNamespace(id=5, display_name=None, name="bob")
    assert format_user_for_log(user) == "bob (5)"  # type: ignore[arg-type]


def test_user_overrides_user_id():
    user = SimpleNamespace(id=10, display_name="Carol", name="Carol")
    assert format_user_for_log(user, user_id=99) == "Carol (10)"  # type: ignore[arg-type]


def test_user_with_no_id_uses_fallback_id():
    user = SimpleNamespace(display_name="Dave", name="dave")
    assert format_user_for_log(user, user_id=7) == "Dave [dave] (7)"  # type: ignore[arg-type]


# ── resolve_user_for_log ──────────────────────────────────────────────

def test_known_member_uses_format():
    member = SimpleNamespace(id=10, display_name="Eve", name="Eve")
    guild = SimpleNamespace(get_member=lambda uid: member if uid == 10 else None)
    assert resolve_user_for_log(guild, 10) == "Eve (10)"  # type: ignore[arg-type]


def test_unknown_member_falls_back_to_id():
    guild = SimpleNamespace(get_member=lambda uid: None)
    assert resolve_user_for_log(guild, 99) == "user 99"  # type: ignore[arg-type]


def test_none_guild_falls_back_to_id():
    assert resolve_user_for_log(None, 42) == "user 42"


# ── format_guild_for_log ──────────────────────────────────────────────

def test_format_guild_no_args_returns_unknown():
    assert format_guild_for_log() == "unknown guild"


def test_format_guild_id_only():
    assert format_guild_for_log(guild_id=42) == "guild 42"


def test_format_guild_with_name():
    guild = SimpleNamespace(id=7, name="My Server")
    assert format_guild_for_log(guild) == "My Server (7)"  # type: ignore[arg-type]


def test_format_guild_without_name_uses_id():
    guild = SimpleNamespace(id=7, name=None)
    assert format_guild_for_log(guild) == "guild 7"  # type: ignore[arg-type]


# ── resolve_guild_for_log ─────────────────────────────────────────────

def test_resolve_known_guild_uses_format():
    guild = SimpleNamespace(id=10, name="Zone")
    bot = SimpleNamespace(get_guild=lambda gid: guild if gid == 10 else None)
    assert resolve_guild_for_log(bot, 10) == "Zone (10)"  # type: ignore[arg-type]


def test_resolve_unknown_guild_falls_back_to_id():
    bot = SimpleNamespace(get_guild=lambda gid: None)
    assert resolve_guild_for_log(bot, 99) == "guild 99"  # type: ignore[arg-type]


def test_resolve_none_bot_falls_back_to_id():
    assert resolve_guild_for_log(None, 42) == "guild 42"
