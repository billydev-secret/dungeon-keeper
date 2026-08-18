from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from bot_modules.core.db_utils import (
    get_config_id_set,
    get_config_value,
    init_config_db,
    open_db,
    open_db_immediate,
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


# ── legacy-fallback behavior ─────────────────────────────────────────
# guild_id=0 stores legacy (single-guild) config. Non-zero guild reads
# normally fall back to it, but per-guild callers can opt out so an
# unconfigured non-home guild gets real defaults instead of inheriting
# the home guild's settings.

def test_get_config_value_falls_back_to_legacy_for_non_home_guild(config_db):
    with open_db(config_db) as conn:
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (0, 'mod_channel_id', '999')"
        )
        # No guild-100 row → falls back to legacy
        assert get_config_value(conn, "mod_channel_id", "0", guild_id=100) == "999"


def test_get_config_value_strict_mode_skips_legacy_fallback(config_db):
    with open_db(config_db) as conn:
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (0, 'mod_channel_id', '999')"
        )
        result = get_config_value(
            conn, "mod_channel_id", "0", guild_id=100, allow_legacy_fallback=False
        )
        assert result == "0"


def test_get_config_value_strict_mode_still_returns_guild_specific(config_db):
    with open_db(config_db) as conn:
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (0, 'mod_channel_id', '111')"
        )
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (100, 'mod_channel_id', '222')"
        )
        # Strict mode does NOT block the guild-specific read, only the legacy fallback
        result = get_config_value(
            conn, "mod_channel_id", "0", guild_id=100, allow_legacy_fallback=False
        )
        assert result == "222"


def test_get_config_value_legacy_lookup_ignores_strict_flag(config_db):
    """guild_id=0 is the legacy bucket itself; the flag has no effect there."""
    with open_db(config_db) as conn:
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (0, 'mod_channel_id', '999')"
        )
        assert (
            get_config_value(
                conn, "mod_channel_id", "0", guild_id=0, allow_legacy_fallback=False
            )
            == "999"
        )


def test_get_config_id_set_falls_back_to_legacy_for_non_home_guild(config_db):
    with open_db(config_db) as conn:
        conn.execute(
            "INSERT INTO config_ids (guild_id, bucket, value) VALUES (0, 'roles', 10)"
        )
        conn.execute(
            "INSERT INTO config_ids (guild_id, bucket, value) VALUES (0, 'roles', 20)"
        )
        assert get_config_id_set(conn, "roles", guild_id=100) == {10, 20}


def test_get_config_id_set_strict_mode_skips_legacy_fallback(config_db):
    with open_db(config_db) as conn:
        conn.execute(
            "INSERT INTO config_ids (guild_id, bucket, value) VALUES (0, 'roles', 10)"
        )
        result = get_config_id_set(
            conn, "roles", guild_id=100, allow_legacy_fallback=False
        )
        assert result == set()


def test_get_config_id_set_strict_mode_still_returns_guild_specific(config_db):
    with open_db(config_db) as conn:
        conn.execute(
            "INSERT INTO config_ids (guild_id, bucket, value) VALUES (0, 'roles', 10)"
        )
        conn.execute(
            "INSERT INTO config_ids (guild_id, bucket, value) VALUES (100, 'roles', 77)"
        )
        result = get_config_id_set(
            conn, "roles", guild_id=100, allow_legacy_fallback=False
        )
        assert result == {77}


# ── open_db_immediate's hand-driven transaction ───────────────────────
#
# open_db_immediate opens with isolation_level=None and issues BEGIN
# IMMEDIATE / COMMIT itself. A conn.commit() inside the block therefore
# ends the transaction early and leaves the exit-time COMMIT with nothing
# to commit — sqlite3 raises "cannot commit - no transaction is active",
# and the handler's ROLLBACK raises on top, masking it. The write has
# already landed by then, so the caller sees a half-finished operation:
# survivor's first live join charged and enrolled the member, then threw
# before refreshing the panel, echoing, or granting the role (2026-08-18).

def test_open_db_immediate_commits_on_clean_exit(tmp_path):
    db = tmp_path / "t.db"
    with open_db_immediate(db) as conn:
        conn.execute("CREATE TABLE t (v INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
    with open_db(db) as conn:
        assert [r["v"] for r in conn.execute("SELECT v FROM t")] == [1]


def test_open_db_immediate_rolls_back_on_error(tmp_path):
    db = tmp_path / "t.db"
    with open_db_immediate(db) as conn:
        conn.execute("CREATE TABLE t (v INTEGER)")
    with pytest.raises(ValueError):
        with open_db_immediate(db) as conn:
            conn.execute("INSERT INTO t VALUES (1)")
            raise ValueError("boom")
    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_inner_commit_breaks_open_db_immediate(tmp_path):
    """The failure mode the contract test below exists to prevent."""
    db = tmp_path / "t.db"
    with pytest.raises(sqlite3.OperationalError):
        with open_db_immediate(db) as conn:
            conn.execute("CREATE TABLE t (v INTEGER)")
            conn.commit()


def test_no_source_commits_inside_open_db_immediate():
    """No `with open_db_immediate(...)` block in src/ may commit itself.

    Static because the breakage only surfaces at runtime, deep inside a
    view callback, where the write has already succeeded and only the
    follow-up work is lost.
    """
    src = Path(__file__).resolve().parents[1] / "src"
    offenders = []
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "open_db_immediate" not in text:
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, ast.With):
                continue
            names = {
                item.context_expr.func.id
                for item in node.items
                if isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Name)
            }
            if "open_db_immediate" not in names:
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "commit"
                ):
                    offenders.append(
                        f"{path.relative_to(src)}:{inner.lineno}"
                    )
    assert not offenders, (
        "commit() inside an open_db_immediate block (the context manager "
        f"already commits): {', '.join(offenders)}"
    )
