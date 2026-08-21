"""Usage telemetry cog — the glue: which invocations become rows.

Aggregation/reporting behaviour lives in test_usage_telemetry_service.py.
The failure path depends on ``events_cog`` re-dispatching ``tree.error`` as an
``on_app_command_error`` bot event, which is asserted here too — that contract
spans two cogs and would otherwise break silently.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from bot_modules.cogs.usage_telemetry_cog import UsageTelemetryCog, command_name
from bot_modules.core.db_utils import open_db

GUILD = 123
USER = 1001


def _make_cog(db_path: Path):
    bot = MagicMock()
    bot.ctx = SimpleNamespace(db_path=db_path, open_db=lambda: open_db(db_path))
    return UsageTelemetryCog(bot)  # type: ignore[arg-type]


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
    cog = _make_cog(sync_db_path)
    await cog._on_completion(_interaction(), MagicMock())  # type: ignore[arg-type]
    assert _rows(sync_db_path) == [("bank", USER, 555, 1)]


@pytest.mark.asyncio
async def test_error_listener_records_failure(sync_db_path: Path):
    cog = _make_cog(sync_db_path)
    await cog._on_error(_interaction(), MagicMock())  # type: ignore[arg-type]
    assert _rows(sync_db_path) == [("bank", USER, 555, 0)]


@pytest.mark.asyncio
async def test_events_cog_dispatches_the_error_event():
    """The failure path only works because events_cog re-broadcasts its
    single-slot tree.error handler as a bot event. Nothing else asserts that
    cross-cog contract, and breaking it would silently zero the error count."""
    from bot_modules.cogs import events_cog

    client = MagicMock()
    interaction = SimpleNamespace(
        client=client, guild=None, guild_id=GUILD,
        user=SimpleNamespace(id=USER, bot=False),
        response=SimpleNamespace(is_done=lambda: True),
    )
    err = RuntimeError("boom")

    await events_cog._on_tree_error(interaction, err)  # type: ignore[arg-type]

    client.dispatch.assert_any_call("app_command_error", interaction, err)


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
    cog = _make_cog(sync_db_path)
    await cog._on_completion(interaction, MagicMock())  # type: ignore[arg-type]
    assert _rows(sync_db_path) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("listener", ["_on_completion", "_on_error"])
async def test_db_failure_does_not_propagate(sync_db_path: Path, listener):
    """A telemetry write must never break the command it is measuring."""
    cog = _make_cog(sync_db_path)
    cog.bot.ctx.open_db = MagicMock(side_effect=RuntimeError("db is gone"))  # type: ignore[attr-defined]
    await getattr(cog, listener)(_interaction(), MagicMock())
    assert _rows(sync_db_path) == []
