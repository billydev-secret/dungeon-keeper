"""Mention-injection guard for the Needle auto-thread welcome message.

``_apply_variables`` substitutes a member-controlled display name into the
welcome template that the bot then *sends and pins*. A nickname like
``<@&roleId>`` or ``@everyone`` must not survive as a live ping. These tests
exercise the substitution helper directly (logic layer) — no Discord needed.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Any, cast

import pytest

from bot_modules.cogs import needle_cog
from bot_modules.cogs.needle_cog import (
    NeedleChannelConfig,
    NeedleGlobalConfig,
    _apply_variables,
    _ensure_tables,
    _get_channel_config,
    _upsert_channel,
)


def _msg(display: str, channel_id: int = 555) -> Any:
    author = SimpleNamespace(display_name=display, name=display)
    return cast(Any, SimpleNamespace(author=author, channel=SimpleNamespace(id=channel_id)))


def _thread(mention: str = "<#999>") -> Any:
    return cast(Any, SimpleNamespace(mention=mention))


def test_apply_variables_neutralizes_role_mention_in_nickname():
    out = _apply_variables(
        "Thread created by $USER in $CHANNEL",
        message=_msg("<@&123456789012345678>"),
        thread=_thread(),
    )
    assert "<@&123456789012345678>" not in out
    assert "​" in out  # zero-width break inserted by escape_mentions


def test_apply_variables_neutralizes_everyone_in_nickname():
    out = _apply_variables(
        "Welcome $USER",
        message=_msg("@everyone"),
        thread=_thread(),
    )
    assert "@everyone" not in out


def test_apply_variables_keeps_channel_and_thread_refs():
    out = _apply_variables(
        "$USER in $CHANNEL — $THREAD",
        message=_msg("Alice", channel_id=42),
        thread=_thread("<#777>"),
    )
    assert "Alice" in out
    assert "<#42>" in out
    assert "<#777>" in out


# ── Auto-reactions survive; the status machine does not ──────────────────────
#
# Needle's reactions used to be two different things wearing the same clothes:
# a decorative `default_reactions` list, and a three-marker status machine the
# bot wrote, read back and swapped (🔵 open / ✅ archived / 🔒 locked). The
# machine was removed 2026-09-06 (migration 215) so that a reaction Needle adds
# never means anything — these tests pin both halves of that.


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_tables(c)
    yield c
    c.close()


def _upsert(conn, **over):
    kwargs = dict(
        guild_id=1, channel_id=2, title_type="first_fifty", custom_title="",
        include_bots=False, slowmode=0, delete_behavior="archive_if_empty",
        reply_type="default", custom_reply="", default_reactions="",
    )
    kwargs.update(over)
    _upsert_channel(conn, **kwargs)  # type: ignore[arg-type]
    return _get_channel_config(conn, kwargs["guild_id"], kwargs["channel_id"])


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("👍,👎", "👍,👎"),
        ("  👍 , 👎  ", "👍,👎"),   # padding trimmed
        ("👍,,👎", "👍,👎"),        # empty slots dropped
        ("", ""),                    # blank means no reactions
        (" , , ", ""),               # only-separators collapses to blank
    ],
)
def test_default_reactions_round_trip(conn, stored, expected):
    cfg = _upsert(conn, default_reactions=stored)
    assert cfg is not None
    assert cfg.default_reactions == expected


def test_channel_config_carries_no_status_fields(conn):
    """A stored channel has no status-reaction state left to read."""
    cfg = _upsert(conn, default_reactions="🔵")
    assert cfg is not None
    assert not hasattr(cfg, "status_reactions")
    assert not hasattr(cfg, "archive_immediately")
    # 🔵 as a *decorative* reaction is fine — it is only the machine that went.
    assert cfg.default_reactions == "🔵"


def test_needle_channels_table_has_no_status_columns(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(needle_channels)")}
    assert "default_reactions" in cols
    assert "status_reactions" not in cols
    assert "archive_immediately" not in cols


def test_dataclasses_expose_only_the_surviving_dials():
    assert not {"status_reactions", "archive_immediately"} & set(
        NeedleChannelConfig.__dataclass_fields__
    )
    # The guild-wide config is now the reply template and nothing else — the
    # three emoji keys went with the machine.
    assert set(NeedleGlobalConfig.__dataclass_fields__) == {"default_reply"}


def test_status_reaction_handlers_are_gone():
    """Regression guard: the listeners that made a reaction load-bearing."""
    for gone in ("_on_thread_update", "_handle_thread_reply"):
        assert not hasattr(needle_cog.NeedleCog, gone), f"{gone} came back"
