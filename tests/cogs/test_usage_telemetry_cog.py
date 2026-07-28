"""Usage telemetry cog — the glue: which invocations become rows, and the
tree.on_error chaining that must not swallow the previous handler.

Aggregation/reporting behaviour lives in test_usage_telemetry_service.py.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot_modules.cogs.usage_telemetry_cog import UsageTelemetryCog, command_name
from bot_modules.core.db_utils import open_db

GUILD = 123
USER = 1001


def _make_cog(db_path: Path):
    bot = MagicMock()
    prev_error = AsyncMock()
    bot.tree.on_error = prev_error
    ctx = SimpleNamespace(db_path=db_path, open_db=lambda: open_db(db_path))
    return UsageTelemetryCog(bot, ctx), prev_error  # type: ignore[arg-type]


def _interaction(*, guild_id=GUILD, user_id=USER, is_bot=False, name="bank"):
    return SimpleNamespace(
        guild_id=guild_id,
        channel_id=555,
        command=SimpleNamespace(qualified_name=name, name=name),
        user=SimpleNamespace(id=user_id, bot=is_bot),
    )


def _rows(db_path):
    with open_db(db_path) as conn:
        # open_db sets row_factory=sqlite3.Row; tuples compare readably.
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT name, user_id, channel_id, ok FROM usage_events"
            ).fetchall()
        ]


# ── command_name ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (SimpleNamespace(qualified_name="quest board", name="board"), "quest board"),
        (SimpleNamespace(qualified_name="", name="bank"), "bank"),
        (None, ""),
    ],
)
def test_command_name_prefers_qualified_name(command, expected):
    assert command_name(command) == expected


# ── recording ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_completion_records_success(sync_db_path: Path):
    cog, _ = _make_cog(sync_db_path)
    await cog._on_completion(_interaction(), MagicMock())  # type: ignore[arg-type]
    assert _rows(sync_db_path) == [("bank", USER, 555, 1)]


@pytest.mark.asyncio
async def test_tree_error_records_failure_and_chains(sync_db_path: Path):
    cog, prev_error = _make_cog(sync_db_path)
    interaction = _interaction()
    err = MagicMock()

    await cog._on_tree_error(interaction, err)  # type: ignore[arg-type]

    assert _rows(sync_db_path) == [("bank", USER, 555, 0)]
    # Chained, not replaced — telemetry must not disable error reporting.
    prev_error.assert_awaited_once_with(interaction, err)


# ── guards ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "interaction",
    [
        pytest.param(_interaction(guild_id=None), id="dm_has_no_guild"),
        pytest.param(_interaction(is_bot=True), id="bot_actor"),
        pytest.param(_interaction(name=""), id="unnamed_command"),
    ],
)
async def test_no_row_for_guarded_invocations(sync_db_path: Path, interaction):
    cog, _ = _make_cog(sync_db_path)
    await cog._on_completion(interaction, MagicMock())  # type: ignore[arg-type]
    assert _rows(sync_db_path) == []


@pytest.mark.asyncio
async def test_db_failure_does_not_propagate(sync_db_path: Path):
    """A telemetry write must never break the command it is measuring."""
    cog, _ = _make_cog(sync_db_path)
    cog.ctx.open_db = MagicMock(side_effect=RuntimeError("db is gone"))  # type: ignore[attr-defined]
    await cog._on_completion(_interaction(), MagicMock())  # type: ignore[arg-type]
    assert _rows(sync_db_path) == []


@pytest.mark.asyncio
async def test_tree_error_still_chains_when_write_fails(sync_db_path: Path):
    cog, prev_error = _make_cog(sync_db_path)
    cog.ctx.open_db = MagicMock(side_effect=RuntimeError("db is gone"))  # type: ignore[attr-defined]
    interaction, err = _interaction(), MagicMock()
    await cog._on_tree_error(interaction, err)  # type: ignore[arg-type]
    prev_error.assert_awaited_once_with(interaction, err)


@pytest.mark.asyncio
async def test_cog_unload_restores_previous_error_handler(sync_db_path: Path):
    cog, prev_error = _make_cog(sync_db_path)
    assert cog.bot.tree.on_error is not prev_error
    await cog.cog_unload()
    assert cog.bot.tree.on_error is prev_error
