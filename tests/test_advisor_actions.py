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


# Real registry keys, so the schema the tests exercise is the shipped one.
BOOL_KEY = "welcome_ping_member"
INT_KEY = "qa_reward"
TEXT_KEY = "welcome_message"
CHANNEL_KEY = "welcome_channel_id"
ROLE_KEY = "welcome_ping_role_id"  # ping-only, so it's writable


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
    conn = _conn([(CHANNEL_KEY, "0")])
    with pytest.raises(ValueError, match="isn't a setting I can change"):
        aa.validate_config_change(conn, _guild(), "made_up_key", "1")


def test_key_present_in_db_but_not_in_registry_is_rejected():
    """The old rule was 'any key with a row'. That let the model reach keys
    nobody vetted — privilege keys included. Presence alone is no longer enough."""
    conn = _conn([("admin_role_ids", "123"), ("mod_role_ids", "456")])
    for key in ("admin_role_ids", "mod_role_ids"):
        with pytest.raises(ValueError, match="isn't a setting I can change"):
            aa.validate_config_change(conn, _guild(), key, str(ROLE_ID))


def test_registry_key_marked_panel_only_is_rejected_with_a_pointer():
    conn = _conn([("ticket_category_id", "0")])
    with pytest.raises(ValueError, match="dashboard"):
        aa.validate_config_change(conn, _guild(), "ticket_category_id", "123")


# ── admin_only settings ─────────────────────────────────────────────────────


def test_admin_only_setting_rejected_for_manage_guild_asker():
    """Manage Server can change ordinary settings but not access-granting ones."""
    conn = _conn([("jailed_role_id", "0")])
    with pytest.raises(ValueError, match="full server administrator"):
        aa.validate_config_change(conn, _guild(), "jailed_role_id", str(ROLE_ID))


def test_admin_only_setting_allowed_for_full_admin():
    conn = _conn([("jailed_role_id", "0")])
    prop = aa.validate_config_change(
        conn, _guild(), "jailed_role_id", "@Greeter", is_admin=True
    )
    assert prop.value == str(ROLE_ID)
    assert prop.display == "Jailed role → @Greeter"


def test_is_admin_defaults_to_false_so_callers_fail_closed():
    conn = _conn([("qa_role_id", "0")])
    with pytest.raises(ValueError, match="full server administrator"):
        aa.validate_config_change(conn, _guild(), "qa_role_id", str(ROLE_ID))


def test_ordinary_settings_need_no_admin_flag():
    conn = _conn([(ROLE_KEY, "0")])
    assert aa.validate_config_change(conn, _guild(), ROLE_KEY, "@Greeter").value == str(
        ROLE_ID
    )


def test_privilege_keys_stay_blocked_even_for_a_full_admin():
    """admin_role_ids / mod_role_ids / message_storage_level are not a tier —
    they're off the table at any permission level."""
    conn = _conn([("admin_role_ids", "1"), ("mod_role_ids", "1"),
                  ("message_storage_level", "1")])
    for key in ("admin_role_ids", "mod_role_ids", "message_storage_level"):
        with pytest.raises(ValueError, match="isn't a setting I can change"):
            aa.validate_config_change(conn, _guild(), key, "2", is_admin=True)


def test_secret_key_rejected_even_if_present():
    conn = _conn([("spotify_bot_refresh_token", "s3cret")])
    with pytest.raises(ValueError, match="can't be changed"):
        aa.validate_config_change(conn, _guild(), "spotify_bot_refresh_token", "x")


def test_empty_key_or_value_rejected():
    conn = _conn([(BOOL_KEY, "1")])
    with pytest.raises(ValueError):
        aa.validate_config_change(conn, _guild(), "", "1")
    with pytest.raises(ValueError):
        aa.validate_config_change(conn, _guild(), BOOL_KEY, "  ")


def test_overlong_value_rejected():
    conn = _conn([(TEXT_KEY, "hi")])
    with pytest.raises(ValueError, match="too long"):
        aa.validate_config_change(conn, _guild(), TEXT_KEY, "x" * 500)


def test_unset_key_is_proposable():
    """The adoption case: nothing stored for this guild yet, and it still works.
    Under the old value-shape inference this raised 'isn't a saved setting'."""
    conn = _conn([])  # empty config table
    prop = aa.validate_config_change(conn, _guild(), CHANNEL_KEY, "#welcome")
    assert prop.value == str(CH_ID)


def test_noop_change_rejected_but_allowed_at_apply_time():
    conn = _conn([(BOOL_KEY, "1")])
    with pytest.raises(ValueError, match="already set"):
        aa.validate_config_change(conn, _guild(), BOOL_KEY, "on")
    # Re-validation on the Apply click must not trip over it.
    prop = aa.validate_config_change(conn, _guild(), BOOL_KEY, "on", allow_noop=True)
    assert prop.value == "1"


def test_legacy_guild0_value_counts_for_the_noop_check():
    # Reads fall back to guild_id=0, so a guild-0 value is the effective current.
    conn = _conn([(BOOL_KEY, "1")], guild_id=0)
    with pytest.raises(ValueError, match="already set"):
        aa.validate_config_change(conn, _guild(), BOOL_KEY, "on")
    assert aa.validate_config_change(conn, _guild(), BOOL_KEY, "off").value == "0"


# ── validate: value shapes ──────────────────────────────────────────────────


def test_channel_by_name_mention_and_id():
    conn = _conn([(CHANNEL_KEY, "0")])
    for raw in ("#welcome", "welcome", f"<#{CH_ID}>", str(CH_ID)):
        prop = aa.validate_config_change(conn, _guild(), CHANNEL_KEY, raw)
        assert prop.value == str(CH_ID)
        # Display now uses the registry's human label, not the raw key.
        assert prop.display == "Welcome channel → #welcome"


def test_channel_unknown_rejected():
    conn = _conn([(CHANNEL_KEY, "0")])
    with pytest.raises(ValueError, match="no channel named"):
        aa.validate_config_change(conn, _guild(), CHANNEL_KEY, "#nope")
    with pytest.raises(ValueError, match="no channel with id"):
        aa.validate_config_change(conn, _guild(), CHANNEL_KEY, "999999999999999999")


def test_channel_clear_words():
    conn = _conn([(CHANNEL_KEY, str(CH_ID))])
    prop = aa.validate_config_change(conn, _guild(), CHANNEL_KEY, "none")
    assert prop.value == "0"
    assert "cleared" in prop.display


def test_role_by_name_mention_and_id():
    conn = _conn([(ROLE_KEY, "0")])
    for raw in ("@Greeter", "greeter", f"<@&{ROLE_ID}>", str(ROLE_ID)):
        prop = aa.validate_config_change(conn, _guild(), ROLE_KEY, raw)
        assert prop.value == str(ROLE_ID)
        assert prop.display == "Role to ping on join → @Greeter"


def test_boolean_normalization():
    conn = _conn([(BOOL_KEY, "0")])
    assert aa.validate_config_change(conn, _guild(), BOOL_KEY, "on").value == "1"
    conn2 = _conn([(BOOL_KEY, "1")])
    assert aa.validate_config_change(conn2, _guild(), BOOL_KEY, "Disabled").value == "0"
    with pytest.raises(ValueError, match="on/off"):
        aa.validate_config_change(conn, _guild(), BOOL_KEY, "maybe")


def test_numeric_and_free_text():
    conn = _conn([(INT_KEY, "5"), (TEXT_KEY, "hello")])
    assert aa.validate_config_change(conn, _guild(), INT_KEY, "1,000").value == "1000"
    with pytest.raises(ValueError, match="whole number"):
        aa.validate_config_change(conn, _guild(), INT_KEY, "lots")
    assert (
        aa.validate_config_change(conn, _guild(), TEXT_KEY, "Hi there!").value
        == "Hi there!"
    )


def test_numeric_bounds_enforced_from_the_schema():
    """Bounds come from the registry — the stored value can't imply a range."""
    conn = _conn([(INT_KEY, "5")])
    with pytest.raises(ValueError, match="below"):
        aa.validate_config_change(conn, _guild(), INT_KEY, "-1")
    with pytest.raises(ValueError, match="above"):
        aa.validate_config_change(conn, _guild(), INT_KEY, "999999999")


def test_shape_comes_from_schema_not_stored_value():
    """A bool whose stored value looks like text is still validated as a bool."""
    conn = _conn([(BOOL_KEY, "banana")])
    with pytest.raises(ValueError, match="on/off"):
        aa.validate_config_change(conn, _guild(), BOOL_KEY, "sometimes")


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
    path = _db_file(tmp_path, [(CHANNEL_KEY, "0")])
    prop = aa.ConfigProposal(CHANNEL_KEY, str(CH_ID), "x")
    aa.apply_config_change(path, _guild(), prop)
    conn = sqlite3.connect(path)
    row = conn.execute(
        f"SELECT value FROM config WHERE guild_id = 1 AND key = '{CHANNEL_KEY}'"
    ).fetchone()
    assert row[0] == str(CH_ID)


def test_apply_creates_a_row_for_a_never_set_key(tmp_path):
    path = _db_file(tmp_path, [])
    prop = aa.ConfigProposal(CHANNEL_KEY, str(CH_ID), "x")
    aa.apply_config_change(path, _guild(), prop)
    conn = sqlite3.connect(path)
    row = conn.execute(
        f"SELECT value FROM config WHERE guild_id = 1 AND key = '{CHANNEL_KEY}'"
    ).fetchone()
    assert row[0] == str(CH_ID)


def test_apply_revalidates_stale_proposal(tmp_path):
    # Channel was deleted between propose and click → apply must refuse.
    path = _db_file(tmp_path, [(CHANNEL_KEY, "0")])
    prop = aa.ConfigProposal(CHANNEL_KEY, "999999999999999999", "x")
    with pytest.raises(ValueError):
        aa.apply_config_change(path, _guild(), prop)
    conn = sqlite3.connect(path)
    row = conn.execute(f"SELECT value FROM config WHERE key = '{CHANNEL_KEY}'").fetchone()
    assert row[0] == "0"  # unchanged


def test_apply_refuses_a_proposal_for_a_panel_only_key(tmp_path):
    """A forged/stale proposal naming a non-writable key must not write."""
    path = _db_file(tmp_path, [("ticket_category_id", "0")])
    prop = aa.ConfigProposal("ticket_category_id", "123", "x")
    with pytest.raises(ValueError):
        aa.apply_config_change(path, _guild(), prop, is_admin=True)
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT value FROM config WHERE key = 'ticket_category_id'").fetchone()
    assert row[0] == "0"


def test_apply_rechecks_admin_only_against_the_clicker(tmp_path):
    """The asker may have been a full admin; whoever clicks must be one too."""
    path = _db_file(tmp_path, [("jailed_role_id", "0")])
    prop = aa.ConfigProposal("jailed_role_id", str(ROLE_ID), "x")

    with pytest.raises(ValueError, match="full server administrator"):
        aa.apply_config_change(path, _guild(), prop, is_admin=False)
    conn = sqlite3.connect(path)
    assert conn.execute(
        "SELECT value FROM config WHERE key = 'jailed_role_id'"
    ).fetchone()[0] == "0"
    conn.close()

    aa.apply_config_change(path, _guild(), prop, is_admin=True)
    conn = sqlite3.connect(path)
    assert conn.execute(
        "SELECT value FROM config WHERE key = 'jailed_role_id'"
    ).fetchone()[0] == str(ROLE_ID)
