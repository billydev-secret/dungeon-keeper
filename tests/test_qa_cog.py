"""Cog-level tests for the QA verdict buttons — gate matrix, pay, modal, thread."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.cogs.qa_cog import (
    QACog,
    _QABlockedButton,
    _QAFailButton,
    _QAPassButton,
    _sweep_stale_card,
)
from bot_modules.core.db_utils import open_db
from bot_modules.qa.cards import STATUS_COLORS
from bot_modules.services.economy_service import get_balance
from bot_modules.services.qa_service import (
    archive_test,
    create_test,
    get_test,
    save_qa_settings,
)
from migrations import apply_migrations_sync
from tests.fakes import FakeGuild, fake_interaction

GUILD_ID = 9001
QA_ROLE_ID = 5005
USER_ID = 500


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    apply_migrations_sync(db_path)
    return db_path


@pytest.fixture
def ctx(db):
    return SimpleNamespace(db_path=db, open_db=lambda: open_db(db))


def _enable(db, **overrides) -> None:
    values: dict[str, object] = {"enabled": True, "role_id": QA_ROLE_ID}
    values.update(overrides)
    with open_db(db) as conn:
        save_qa_settings(conn, GUILD_ID, values)


def _mk_test(db, *, title="My feature (abc1234)", body="- [ ] check the thing") -> int:
    with open_db(db) as conn:
        return create_test(
            conn,
            GUILD_ID,
            "My feature",
            title,
            body,
            commit_sha="abc1234def",
            commit_subject="Feature: do the thing",
        )


def _member(*, admin: bool = False, role_ids: tuple[int, ...] = (QA_ROLE_ID,)) -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.id = USER_ID
    m.mention = f"<@{USER_ID}>"
    m.guild_permissions = MagicMock(administrator=admin)
    m.roles = [SimpleNamespace(id=rid) for rid in role_ids]
    return m


def _card_message() -> tuple[MagicMock, MagicMock]:
    thread = MagicMock(spec=discord.Thread)
    thread.id = 777
    thread.send = AsyncMock()
    msg = MagicMock(spec=discord.Message)
    msg.edit = AsyncMock()
    msg.create_thread = AsyncMock(return_value=thread)
    return msg, thread


def _interaction(ctx, actor: MagicMock) -> MagicMock:
    inter = fake_interaction(guild=FakeGuild(id=GUILD_ID))
    inter.user = actor
    inter.client = SimpleNamespace(ctx=ctx)
    inter.message, inter._thread = _card_message()
    return inter


def _verdict_rows(db, test_id: int) -> list:
    with open_db(db) as conn:
        return conn.execute(
            "SELECT * FROM qa_verdicts WHERE test_id = ?", (test_id,)
        ).fetchall()


def _ephemeral_text(inter) -> str:
    call = inter.response.send_message.await_args
    assert call.kwargs.get("ephemeral") is True
    return call.args[0]


# ── gates ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_crew_member_rejected(ctx, db):
    _enable(db)
    tid = _mk_test(db)
    inter = _interaction(ctx, _member(role_ids=()))  # no QA role, not admin

    await _QAPassButton(tid).callback(inter)

    assert "QA-crew" in _ephemeral_text(inter)
    assert _verdict_rows(db, tid) == []


@pytest.mark.asyncio
async def test_admin_allowed_without_role(ctx, db):
    _enable(db, role_id=0)  # 0 = admins only
    tid = _mk_test(db)
    inter = _interaction(ctx, _member(admin=True, role_ids=()))

    await _QAPassButton(tid).callback(inter)

    rows = _verdict_rows(db, tid)
    assert len(rows) == 1 and rows[0]["verdict"] == "pass"


@pytest.mark.asyncio
async def test_disabled_guild_friendly_ephemeral(ctx, db):
    with open_db(db) as conn:  # enabled defaults on; disable explicitly
        save_qa_settings(conn, GUILD_ID, {"enabled": False})
    tid = _mk_test(db)
    inter = _interaction(ctx, _member())

    await _QAPassButton(tid).callback(inter)

    assert "disabled" in _ephemeral_text(inter).lower()
    assert _verdict_rows(db, tid) == []


@pytest.mark.asyncio
async def test_archived_test_friendly_ephemeral(ctx, db):
    _enable(db)
    tid = _mk_test(db)
    with open_db(db) as conn:
        archive_test(conn, tid)
    inter = _interaction(ctx, _member())

    await _QAPassButton(tid).callback(inter)

    assert "archived" in _ephemeral_text(inter).lower()
    assert _verdict_rows(db, tid) == []
    inter.message.edit.assert_not_awaited()


# ── pass: record + pay + re-render ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pass_records_pays_and_edits_card(ctx, db):
    _enable(db)  # default reward 15
    tid = _mk_test(db)
    inter = _interaction(ctx, _member())

    await _QAPassButton(tid).callback(inter)

    rows = _verdict_rows(db, tid)
    assert len(rows) == 1
    assert rows[0]["verdict"] == "pass"
    assert rows[0]["paid_amount"] == 15
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, USER_ID) == 15
        status = conn.execute(
            "SELECT status, verified_by FROM qa_tests WHERE id = ?", (tid,)
        ).fetchone()
    assert status["status"] == "passed"
    assert status["verified_by"] == USER_ID

    assert "+15" in _ephemeral_text(inter)

    inter.message.edit.assert_awaited_once()
    embed = inter.message.edit.await_args.kwargs["embed"]
    assert embed.color.value == STATUS_COLORS["passed"]
    assert "• check the thing" in embed.description
    # Components untouched — the edit carries only the embed.
    assert "view" not in inter.message.edit.await_args.kwargs


@pytest.mark.asyncio
async def test_daily_cap_reached_records_without_pay(ctx, db):
    _enable(db, daily_cap=0)  # 0 = never pay
    tid = _mk_test(db)
    inter = _interaction(ctx, _member())

    await _QAPassButton(tid).callback(inter)

    rows = _verdict_rows(db, tid)
    assert len(rows) == 1 and rows[0]["paid_amount"] == 0
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, USER_ID) == 0
    assert "daily cap" in _ephemeral_text(inter).lower()


@pytest.mark.asyncio
async def test_reclick_updates_verdict_without_paying_again(ctx, db):
    _enable(db)
    tid = _mk_test(db)

    inter1 = _interaction(ctx, _member())
    await _QAPassButton(tid).callback(inter1)
    inter2 = _interaction(ctx, _member())
    await _QAPassButton(tid).callback(inter2)

    assert "updated" in _ephemeral_text(inter2).lower()
    rows = _verdict_rows(db, tid)
    assert len(rows) == 1
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, USER_ID) == 15  # paid once


# ── fail / blocked: modal + thread notes ──────────────────────────────────────


@pytest.mark.asyncio
async def test_fail_opens_modal_with_required_note(ctx, db):
    _enable(db)
    tid = _mk_test(db)
    inter = _interaction(ctx, _member())

    await _QAFailButton(tid).callback(inter)

    modal = inter.response.send_modal.await_args.args[0]
    assert modal.note.required is True
    assert _verdict_rows(db, tid) == []  # nothing recorded until submit


@pytest.mark.asyncio
async def test_blocked_modal_note_is_optional(ctx, db):
    _enable(db)
    tid = _mk_test(db)
    inter = _interaction(ctx, _member())

    await _QABlockedButton(tid).callback(inter)

    modal = inter.response.send_modal.await_args.args[0]
    assert modal.note.required is False


@pytest.mark.asyncio
async def test_fail_submit_records_and_posts_note_to_thread(ctx, db):
    _enable(db)
    tid = _mk_test(db)
    click = _interaction(ctx, _member())
    card, thread = click.message, click._thread

    await _QAFailButton(tid).callback(click)
    modal = click.response.send_modal.await_args.args[0]
    modal.note._value = "The button did nothing"

    submit = _interaction(ctx, _member())
    await modal.on_submit(submit)

    rows = _verdict_rows(db, tid)
    assert len(rows) == 1
    assert rows[0]["verdict"] == "fail"
    assert rows[0]["note"] == "The button did nothing"

    # Thread created lazily on the card, note posted, id stored on the row.
    card.create_thread.assert_awaited_once()
    assert card.create_thread.await_args.kwargs["name"] == "My feature (abc1234)"
    thread.send.assert_awaited_once()
    note_text = thread.send.await_args.args[0]
    assert "The button did nothing" in note_text
    assert f"<@{USER_ID}>" in note_text
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT thread_id, status FROM qa_tests WHERE id = ?", (tid,)
        ).fetchone()
    assert row["thread_id"] == 777
    assert row["status"] == "failed"

    # Card re-rendered red.
    card.edit.assert_awaited_once()
    assert card.edit.await_args.kwargs["embed"].color.value == STATUS_COLORS["failed"]


@pytest.mark.asyncio
async def test_blocked_submit_without_note_skips_thread(ctx, db):
    _enable(db)
    tid = _mk_test(db)
    click = _interaction(ctx, _member())

    await _QABlockedButton(tid).callback(click)
    modal = click.response.send_modal.await_args.args[0]
    modal.note._value = ""

    submit = _interaction(ctx, _member())
    await modal.on_submit(submit)

    rows = _verdict_rows(db, tid)
    assert len(rows) == 1 and rows[0]["verdict"] == "blocked"
    click.message.create_thread.assert_not_awaited()
    click.message.edit.assert_awaited_once()  # still re-rendered (amber)
    embed = click.message.edit.await_args.kwargs["embed"]
    assert embed.color.value == STATUS_COLORS["blocked"]


@pytest.mark.asyncio
async def test_modal_submit_regates_disabled(ctx, db):
    """Settings can flip between the click and the submit — re-gated."""
    _enable(db)
    tid = _mk_test(db)
    click = _interaction(ctx, _member())
    await _QAFailButton(tid).callback(click)
    modal = click.response.send_modal.await_args.args[0]
    modal.note._value = "broke"

    with open_db(db) as conn:
        save_qa_settings(conn, GUILD_ID, {"enabled": False})
    submit = _interaction(ctx, _member())
    await modal.on_submit(submit)

    assert "disabled" in _ephemeral_text(submit).lower()
    assert _verdict_rows(db, tid) == []


# ── archive sweep ───────────────────────────────────────────────────────────


class _SweepChannel:
    """Just enough TextChannel for the sweep; isinstance-compatible."""

    __class__ = discord.TextChannel  # type: ignore[assignment]

    def __init__(self, *, fetch_exc: Exception | None = None) -> None:
        self.message = AsyncMock(spec=discord.Message)
        if fetch_exc is not None:
            self.fetch_message = AsyncMock(side_effect=fetch_exc)
        else:
            self.fetch_message = AsyncMock(return_value=self.message)


def _passed_test(db, *, channel_id: int | None = 555, message_id: int | None = 999) -> int:
    tid = _mk_test(db)
    with open_db(db) as conn:
        conn.execute(
            "UPDATE qa_tests SET status='passed', verified_at='2000-01-01T00:00:00+00:00', "
            "channel_id=?, message_id=? WHERE id=?",
            (channel_id, message_id, tid),
        )
    return tid


def _test_dict(db, tid: int) -> dict:
    with open_db(db) as conn:
        row = get_test(conn, tid)
        assert row is not None
        return dict(row)


@pytest.mark.asyncio
async def test_sweep_deletes_message_and_archives(db):
    tid = _passed_test(db)
    channel = _SweepChannel()
    bot = SimpleNamespace(get_channel=lambda cid: channel if cid == 555 else None)

    await _sweep_stale_card(bot, db, _test_dict(db, tid))

    channel.fetch_message.assert_awaited_once_with(999)
    channel.message.delete.assert_awaited_once()
    assert _test_dict(db, tid)["status"] == "archived"


@pytest.mark.asyncio
async def test_sweep_archives_when_message_already_gone(db):
    tid = _passed_test(db)
    channel = _SweepChannel(
        fetch_exc=discord.NotFound(SimpleNamespace(status=404, reason="gone"), "gone")
    )
    bot = SimpleNamespace(get_channel=lambda cid: channel)

    await _sweep_stale_card(bot, db, _test_dict(db, tid))

    assert _test_dict(db, tid)["status"] == "archived"


@pytest.mark.asyncio
async def test_sweep_archives_when_channel_unreachable(db):
    tid = _passed_test(db)
    bot = SimpleNamespace(get_channel=lambda cid: None)

    await _sweep_stale_card(bot, db, _test_dict(db, tid))

    assert _test_dict(db, tid)["status"] == "archived"


@pytest.mark.asyncio
async def test_sweep_leaves_passed_on_transient_discord_error(db):
    tid = _passed_test(db)
    channel = _SweepChannel(
        fetch_exc=discord.HTTPException(SimpleNamespace(status=500, reason="err"), "boom")
    )
    bot = SimpleNamespace(get_channel=lambda cid: channel)

    await _sweep_stale_card(bot, db, _test_dict(db, tid))

    # Retried on the next pass rather than archived out from under a hiccup.
    assert _test_dict(db, tid)["status"] == "passed"


# ── wiring ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cog_load_registers_dynamic_items(ctx):
    bot = MagicMock()
    cog = QACog(bot, ctx)
    await cog.cog_load()
    bot.add_dynamic_items.assert_called_once_with(
        _QAPassButton, _QAFailButton, _QABlockedButton
    )


@pytest.mark.asyncio
async def test_cog_load_registers_archive_sweep_task(ctx):
    bot = MagicMock()
    bot.startup_task_factories = []
    cog = QACog(bot, ctx)
    await cog.cog_load()
    assert len(bot.startup_task_factories) == 1
    assert callable(bot.startup_task_factories[0])


def test_extension_registered_in_entry_point():
    from pathlib import Path

    import dungeonkeeper

    entry = Path(dungeonkeeper.__file__).parent / "__main__.py"
    assert '"bot_modules.cogs.qa_cog",' in entry.read_text(encoding="utf-8")
