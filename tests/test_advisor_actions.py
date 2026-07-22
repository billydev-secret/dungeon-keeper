"""Tests for Billy-bot's admin-confirmed config changes (validate + apply)."""

from __future__ import annotations

import sqlite3

import pytest

from bot_modules.services import advisor_actions as aa


class FakeChannel:
    def __init__(self, cid, name):
        self.id = cid
        self.name = name


class FakeRole:
    def __init__(self, rid, name):
        self.id = rid
        self.name = name


class FakeGuild:
    def __init__(self, gid, channels=(), roles=()):
        self.id = gid
        self.text_channels = list(channels)
        self.roles = list(roles)
        self._ch = {c.id: c for c in channels}
        self._ro = {r.id: r for r in roles}

    def get_channel(self, cid):
        return self._ch.get(cid)

    def get_role(self, rid):
        return self._ro.get(rid)


CH_ID = 111111111111111111
ROLE_ID = 222222222222222222


def _guild():
    return FakeGuild(
        1,
        channels=[FakeChannel(CH_ID, "welcome")],
        roles=[FakeRole(ROLE_ID, "Greeter")],
    )


def _conn(rows, guild_id=1):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE config (guild_id INTEGER NOT NULL DEFAULT 0, key TEXT NOT NULL, "
        "value TEXT NOT NULL, PRIMARY KEY (guild_id, key))"
    )
    conn.executemany(
        "INSERT INTO config VALUES (?, ?, ?)", [(guild_id, k, v) for k, v in rows]
    )
    return conn


# ── validate: key gates ─────────────────────────────────────────────────────


def test_unknown_key_rejected():
    conn = _conn([("welcome_channel_id", "0")])
    with pytest.raises(ValueError, match="isn't a saved setting"):
        aa.validate_config_change(conn, _guild(), "made_up_key", "1")


def test_secret_key_rejected_even_if_present():
    conn = _conn([("spotify_bot_refresh_token", "s3cret")])
    with pytest.raises(ValueError, match="can't be changed"):
        aa.validate_config_change(conn, _guild(), "spotify_bot_refresh_token", "x")


def test_empty_key_or_value_rejected():
    conn = _conn([("welcome_enabled", "1")])
    with pytest.raises(ValueError):
        aa.validate_config_change(conn, _guild(), "", "1")
    with pytest.raises(ValueError):
        aa.validate_config_change(conn, _guild(), "welcome_enabled", "  ")


def test_overlong_value_rejected():
    conn = _conn([("welcome_message", "hi")])
    with pytest.raises(ValueError, match="too long"):
        aa.validate_config_change(conn, _guild(), "welcome_message", "x" * 500)


def test_legacy_guild0_key_is_changeable():
    # Reads fall back to guild_id=0, so those keys must be proposable too.
    conn = _conn([("welcome_enabled", "1")], guild_id=0)
    prop = aa.validate_config_change(conn, _guild(), "welcome_enabled", "off")
    assert prop.value == "0"


# ── validate: value shapes ──────────────────────────────────────────────────


def test_channel_by_name_mention_and_id():
    conn = _conn([("welcome_channel_id", "0")])
    for raw in ("#welcome", "welcome", f"<#{CH_ID}>", str(CH_ID)):
        prop = aa.validate_config_change(conn, _guild(), "welcome_channel_id", raw)
        assert prop.value == str(CH_ID)
        assert prop.display == "welcome_channel_id → #welcome"


def test_channel_unknown_rejected():
    conn = _conn([("welcome_channel_id", "0")])
    with pytest.raises(ValueError, match="no channel named"):
        aa.validate_config_change(conn, _guild(), "welcome_channel_id", "#nope")
    with pytest.raises(ValueError, match="no channel with id"):
        aa.validate_config_change(conn, _guild(), "welcome_channel_id", "999999999999999999")


def test_channel_clear_words():
    conn = _conn([("welcome_channel_id", str(CH_ID))])
    prop = aa.validate_config_change(conn, _guild(), "welcome_channel_id", "none")
    assert prop.value == "0"
    assert "cleared" in prop.display


def test_role_by_name_mention_and_id():
    conn = _conn([("greeter_role_id", "0")])
    for raw in ("@Greeter", "greeter", f"<@&{ROLE_ID}>", str(ROLE_ID)):
        prop = aa.validate_config_change(conn, _guild(), "greeter_role_id", raw)
        assert prop.value == str(ROLE_ID)
        assert prop.display == "greeter_role_id → @Greeter"


def test_boolean_normalization():
    conn = _conn([("welcome_enabled", "0")])
    assert aa.validate_config_change(conn, _guild(), "welcome_enabled", "on").value == "1"
    assert aa.validate_config_change(conn, _guild(), "welcome_enabled", "Disabled").value == "0"
    with pytest.raises(ValueError, match="on/off"):
        aa.validate_config_change(conn, _guild(), "welcome_enabled", "maybe")


def test_numeric_and_free_text():
    conn = _conn([("xp_per_message", "5"), ("welcome_message", "hello")])
    assert aa.validate_config_change(conn, _guild(), "xp_per_message", "1,000").value == "1000"
    with pytest.raises(ValueError, match="whole number"):
        aa.validate_config_change(conn, _guild(), "xp_per_message", "lots")
    assert (
        aa.validate_config_change(conn, _guild(), "welcome_message", "Hi there!").value
        == "Hi there!"
    )


# ── apply ───────────────────────────────────────────────────────────────────


def _db_file(tmp_path, rows):
    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE config (guild_id INTEGER NOT NULL DEFAULT 0, key TEXT NOT NULL, "
        "value TEXT NOT NULL, PRIMARY KEY (guild_id, key))"
    )
    conn.executemany("INSERT INTO config VALUES (1, ?, ?)", rows)
    conn.commit()
    conn.close()
    return path


def test_apply_writes_confirmed_change(tmp_path):
    path = _db_file(tmp_path, [("welcome_channel_id", "0")])
    prop = aa.ConfigProposal("welcome_channel_id", str(CH_ID), "x")
    aa.apply_config_change(path, _guild(), prop)
    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT value FROM config WHERE guild_id = 1 AND key = 'welcome_channel_id'"
    ).fetchone()
    assert row[0] == str(CH_ID)


def test_apply_revalidates_stale_proposal(tmp_path):
    # Channel was deleted between propose and click → apply must refuse.
    path = _db_file(tmp_path, [("welcome_channel_id", "0")])
    prop = aa.ConfigProposal("welcome_channel_id", "999999999999999999", "x")
    with pytest.raises(ValueError):
        aa.apply_config_change(path, _guild(), prop)
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT value FROM config WHERE key = 'welcome_channel_id'").fetchone()
    assert row[0] == "0"  # unchanged
