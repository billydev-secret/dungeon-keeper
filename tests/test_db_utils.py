from __future__ import annotations

import pytest

from db_utils import (
    get_config_id_set,
    get_config_value,
    init_config_db,
    open_db,
    parse_bool,
)


# ── parse_bool ────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ("1", "true", "True", "TRUE", "yes", "YES", "on", "ON"))
def test_truthy_strings(value):
    assert parse_bool(value) is True


@pytest.mark.parametrize("value", ("0", "false", "False", "no", "off", "random", ""))
def test_falsy_strings(value):
    assert parse_bool(value) is False


def test_none_returns_default_false():
    assert parse_bool(None) is False


def test_none_returns_explicit_default():
    assert parse_bool(None, default=True) is True
    assert parse_bool(None, default=False) is False


def test_strips_whitespace():
    assert parse_bool("  true  ") is True
    assert parse_bool("  false  ") is False


# ── config DB ─────────────────────────────────────────────────────────

@pytest.fixture
def config_db(tmp_path):
    db_path = tmp_path / "test.db"
    init_config_db(db_path)
    return db_path


def test_get_config_value_missing_key_returns_default(config_db):
    with open_db(config_db) as conn:
        assert get_config_value(conn, "missing", "fallback") == "fallback"


def test_get_config_value_stored_key(config_db):
    with open_db(config_db) as conn:
        conn.execute("INSERT INTO config (key, value) VALUES ('mykey', 'myval')")
        assert get_config_value(conn, "mykey", "fallback") == "myval"


def test_get_config_value_overrides_default(config_db):
    with open_db(config_db) as conn:
        conn.execute("INSERT INTO config (key, value) VALUES ('guild_id', '12345')")
        assert get_config_value(conn, "guild_id", "0") == "12345"


def test_get_config_id_set_empty_bucket(config_db):
    with open_db(config_db) as conn:
        assert get_config_id_set(conn, "no_such_bucket") == set()


def test_get_config_id_set_returns_correct_ids(config_db):
    with open_db(config_db) as conn:
        conn.execute("INSERT INTO config_ids (bucket, value) VALUES ('roles', 10)")
        conn.execute("INSERT INTO config_ids (bucket, value) VALUES ('roles', 20)")
        conn.execute("INSERT INTO config_ids (bucket, value) VALUES ('other', 99)")
        assert get_config_id_set(conn, "roles") == {10, 20}


def test_get_config_id_set_scoped_to_bucket(config_db):
    with open_db(config_db) as conn:
        conn.execute("INSERT INTO config_ids (bucket, value) VALUES ('a', 1)")
        conn.execute("INSERT INTO config_ids (bucket, value) VALUES ('b', 2)")
        assert get_config_id_set(conn, "a") == {1}
        assert get_config_id_set(conn, "b") == {2}


def test_init_config_db_is_idempotent(config_db):
    with open_db(config_db) as conn:
        conn.execute("INSERT INTO config (key, value) VALUES ('test', 'val')")
    init_config_db(config_db)
    with open_db(config_db) as conn:
        assert get_config_value(conn, "test", "missing") == "val"
