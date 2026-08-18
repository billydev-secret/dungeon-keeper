"""Tests for web_server/routes/todo.py — tasks, board placement, recurring."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock

import discord

import pytest
from fastapi.testclient import TestClient

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.services.todo_service import (
    BOARD_ALL,
    BOARD_CHORES,
    clear_board,
    create_todo,
    save_board,
)
from web_server.auth import SESSION_COOKIE, DiscordOAuthAuth
from web_server.deps import invalidate_report_cache
from web_server.server import create_app

GUILD = 123

# 0x20 = Manage Guild → moderator, but NOT admin.
_MOD_ONLY_BITS = 0x20


@pytest.fixture
def mod_client(fake_ctx) -> Generator[TestClient, None, None]:
    """A logged-in moderator who is not an administrator."""
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    app = create_app(fake_ctx, auth=auth)
    client = TestClient(app)
    cookie = auth.create_session_cookie(
        user_id=7,
        username="mod",
        access_token="token",
        permission_bits=_MOD_ONLY_BITS,
        guild_id=fake_ctx.guild_id,
        guilds=[{"id": fake_ctx.guild_id, "name": "Test Guild", "icon": None}],
    )
    client.cookies.set(SESSION_COOKIE, cookie)
    invalidate_report_cache()
    yield client
    client.close()
    invalidate_report_cache()


def _attach_bot(fake_ctx, *, channel=True, perm_bits=0x8):
    """Give the fake ctx a bot whose TodoCog records what it was asked to do.

    ``perm_bits`` sets the *live member's* Discord permissions. Once a bot is
    attached, auth resolves perms from the member rather than the session
    cookie, so this has to match the client the test is driving.
    """
    cog = MagicMock()
    message = MagicMock()
    message.id = 666
    cog.place_board = AsyncMock(return_value=message)
    cog.unpost_board = AsyncMock(return_value=True)
    cog.refresh_board = AsyncMock(return_value=True)
    cog.place_chore_board = AsyncMock(return_value=message)
    cog.unpost_chore_board = AsyncMock(return_value=True)
    cog.refresh_chore_board = AsyncMock(return_value=True)
    cog.refresh_boards = AsyncMock(return_value=None)

    member = MagicMock()
    member.guild_permissions.value = perm_bits
    member.display_name = "tester"
    member.roles = []

    guild = MagicMock()
    guild.id = GUILD
    guild.get_member.return_value = member
    # spec'd as TextChannel: the route isinstance-checks it, matching what
    # place_board is annotated to accept.
    target = MagicMock(spec=discord.TextChannel) if channel else None
    if target is not None:
        target.id = 555
        target.send = AsyncMock()
    guild.get_channel.return_value = target

    bot = MagicMock()
    bot.get_cog.return_value = cog
    bot.get_guild.return_value = guild
    fake_ctx.bot = bot
    return cog


# ── task list ─────────────────────────────────────────────────────────


def test_list_includes_board_block(authed_client, fake_ctx):
    with open_db(fake_ctx.db_path) as conn:
        create_todo(conn, GUILD, 1, "Post QOTD")
    data = authed_client.get("/api/todos").json()
    assert data["pending_count"] == 1
    assert data["board"]["posted"] is False
    assert data["board"]["channel_id"] == "0"


def test_list_exposes_admin_capability(authed_client, mod_client):
    assert authed_client.get("/api/todos").json()["can_manage_board"] is True
    assert mod_client.get("/api/todos").json()["can_manage_board"] is False


def test_board_ids_are_strings(authed_client, fake_ctx):
    """Snowflakes must never cross the wire as bare numbers (>2^53 loses bits)."""
    with open_db(fake_ctx.db_path) as conn:
        save_board(conn, GUILD, 9007199254740995, 9007199254740997)
    board = authed_client.get("/api/todos").json()["board"]
    assert board["channel_id"] == "9007199254740995"
    assert board["message_id"] == "9007199254740997"


def test_list_reports_recurring_provenance(authed_client, fake_ctx):
    with open_db(fake_ctx.db_path) as conn:
        create_todo(conn, GUILD, 1, "Post QOTD", recurring_id=4)
    assert authed_client.get("/api/todos").json()["todos"][0]["recurring_id"] == 4


@pytest.mark.parametrize(
    ("task", "message"),
    [("   ", "empty"), ("x" * 501, "500 characters")],
)
def test_create_rejects_bad_input(authed_client, task, message):
    resp = authed_client.post("/api/todos", json={"task": task})
    assert resp.status_code == 400
    assert message in resp.json()["detail"]


def test_create_then_complete(authed_client, fake_ctx):
    todo_id = authed_client.post("/api/todos", json={"task": "Do it"}).json()["id"]
    assert authed_client.post(f"/api/todos/{todo_id}/complete").status_code == 200
    # Second completion is a 404, not a silent success.
    assert authed_client.post(f"/api/todos/{todo_id}/complete").status_code == 404


def test_mutations_refresh_the_board(authed_client, fake_ctx):
    """Both boards: the dashboard cannot cheaply tell whether the row it just
    touched was a recurring instance, and each panel is signature-guarded."""
    cog = _attach_bot(fake_ctx)
    todo_id = authed_client.post("/api/todos", json={"task": "Do it"}).json()["id"]
    authed_client.post(f"/api/todos/{todo_id}/complete")
    assert cog.refresh_boards.await_count == 2


def test_board_refresh_failure_does_not_fail_the_request(authed_client, fake_ctx):
    """The 60s loop repaints anyway — a Discord hiccup must not lose the task."""
    cog = _attach_bot(fake_ctx)
    cog.refresh_boards = AsyncMock(side_effect=RuntimeError("gateway down"))
    assert authed_client.post("/api/todos", json={"task": "Do it"}).status_code == 200


# ── board placement (admin-gated) ─────────────────────────────────────


def test_board_placement_requires_admin(mod_client, fake_ctx):
    """Posting the bot into an arbitrary channel is server config, not curation."""
    cog = _attach_bot(fake_ctx, perm_bits=_MOD_ONLY_BITS)
    resp = mod_client.put("/api/todos/board", json={"channel_id": "555"})
    assert resp.status_code == 403
    cog.place_board.assert_not_awaited()


def test_admin_can_post_the_board(authed_client, fake_ctx):
    cog = _attach_bot(fake_ctx)
    resp = authed_client.put("/api/todos/board", json={"channel_id": "555"})
    assert resp.status_code == 200
    assert resp.json()["posted"] is True
    cog.place_board.assert_awaited_once()


def test_zero_channel_unposts(authed_client, fake_ctx):
    cog = _attach_bot(fake_ctx)
    resp = authed_client.put("/api/todos/board", json={"channel_id": "0"})
    assert resp.status_code == 200
    assert resp.json()["posted"] is False
    cog.unpost_board.assert_awaited_once()


def test_unknown_channel_is_a_400(authed_client, fake_ctx):
    _attach_bot(fake_ctx, channel=False)
    resp = authed_client.put("/api/todos/board", json={"channel_id": "555"})
    assert resp.status_code == 400
    assert "doesn't exist" in resp.json()["detail"]


def test_non_numeric_channel_is_a_400(authed_client, fake_ctx):
    _attach_bot(fake_ctx)
    resp = authed_client.put("/api/todos/board", json={"channel_id": "abc"})
    assert resp.status_code == 400


def test_forbidden_channel_explains_the_permission(authed_client, fake_ctx):
    cog = _attach_bot(fake_ctx)
    cog.place_board = AsyncMock(return_value=None)
    resp = authed_client.put("/api/todos/board", json={"channel_id": "555"})
    assert resp.status_code == 400
    assert "Send Messages" in resp.json()["detail"]


def test_board_route_without_a_bot_is_503(authed_client, fake_ctx):
    fake_ctx.bot = None
    resp = authed_client.put("/api/todos/board", json={"channel_id": "555"})
    assert resp.status_code == 503


# ── recurring tasks ───────────────────────────────────────────────────


def _make(client, **over):
    body = {"task": "Post QOTD", "recurrence": "daily", "time_of_day": 540}
    body.update(over)
    return client.post("/api/todos/recurring", json=body)


def test_create_and_list_recurring(authed_client):
    assert _make(authed_client).status_code == 200
    data = authed_client.get("/api/todos/recurring").json()
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["task"] == "Post QOTD"
    assert item["cadence"] == "Daily at 09:00"
    assert item["status"] == "active"
    assert item["next_run_at"] is not None


def test_weekly_reports_its_days(authed_client):
    _make(authed_client, recurrence="weekly", recur_days=[0, 3], time_of_day=600)
    item = authed_client.get("/api/todos/recurring").json()["items"][0]
    assert item["recur_days"] == [0, 3]
    assert item["cadence"] == "Weekly on Mon, Thu at 10:00"


def test_recurring_honours_guild_timezone(authed_client, fake_ctx):
    """time_of_day is guild-local, so the offset must reach the scheduler."""
    with open_db(fake_ctx.db_path) as conn:
        set_config_value(conn, "tz_offset_hours", "-5", guild_id=GUILD)
    _make(authed_client)
    assert authed_client.get("/api/todos/recurring").json()["tz_offset_hours"] == -5.0


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"task": "  "}, "empty"),
        ({"recurrence": "hourly"}, "daily or weekly"),
        ({"time_of_day": 5000}, "00:00"),
        ({"recurrence": "weekly", "recur_days": []}, "day of the week"),
    ],
)
def test_create_recurring_validation(authed_client, over, message):
    resp = _make(authed_client, **over)
    assert resp.status_code == 400
    assert message in resp.json()["detail"]


def test_unknown_field_is_rejected(authed_client):
    resp = _make(authed_client, sneaky="value")
    assert resp.status_code == 422


def test_update_recurring(authed_client):
    rid = _make(authed_client).json()["id"]
    resp = authed_client.put(
        f"/api/todos/recurring/{rid}",
        json={"task": "Post QOTD later", "recurrence": "daily", "time_of_day": 600},
    )
    assert resp.status_code == 200
    item = authed_client.get("/api/todos/recurring").json()["items"][0]
    assert item["task"] == "Post QOTD later"
    assert item["cadence"] == "Daily at 10:00"


def test_delete_recurring(authed_client):
    rid = _make(authed_client).json()["id"]
    assert authed_client.delete(f"/api/todos/recurring/{rid}").status_code == 200
    assert authed_client.get("/api/todos/recurring").json()["items"] == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("put", "/api/todos/recurring/4242"),
        ("delete", "/api/todos/recurring/4242"),
        ("post", "/api/todos/recurring/4242/pause"),
        ("post", "/api/todos/recurring/4242/run-now"),
    ],
)
def test_missing_recurring_is_a_404(authed_client, method, path):
    kwargs = {}
    if method == "put":
        kwargs["json"] = {"task": "x", "recurrence": "daily", "time_of_day": 0}
    resp = getattr(authed_client, method)(path, **kwargs)
    assert resp.status_code == 404


def test_pause_and_resume(authed_client):
    rid = _make(authed_client).json()["id"]
    assert authed_client.post(f"/api/todos/recurring/{rid}/pause").status_code == 200
    assert authed_client.get("/api/todos/recurring").json()["items"][0]["status"] == "paused"
    assert authed_client.post(f"/api/todos/recurring/{rid}/resume").status_code == 200
    assert authed_client.get("/api/todos/recurring").json()["items"][0]["status"] == "active"


def test_unknown_action_is_a_404(authed_client):
    rid = _make(authed_client).json()["id"]
    assert authed_client.post(f"/api/todos/recurring/{rid}/explode").status_code == 404


def test_run_now_spawns_a_task(authed_client, fake_ctx):
    cog = _attach_bot(fake_ctx)
    rid = _make(authed_client).json()["id"]
    resp = authed_client.post(f"/api/todos/recurring/{rid}/run-now")
    assert resp.status_code == 200
    assert resp.json()["spawned"] is True
    todos = authed_client.get("/api/todos").json()["todos"]
    assert [t["task"] for t in todos] == ["Post QOTD"]
    cog.refresh_boards.assert_awaited()


def test_run_now_twice_does_not_duplicate(authed_client):
    """Skip-if-pending: the same chore must not stack two identical rows."""
    rid = _make(authed_client).json()["id"]
    authed_client.post(f"/api/todos/recurring/{rid}/run-now")
    resp = authed_client.post(f"/api/todos/recurring/{rid}/run-now")
    assert resp.json()["spawned"] is False
    assert "already on the list" in resp.json()["detail"]
    assert authed_client.get("/api/todos").json()["pending_count"] == 1


def test_recurring_is_scoped_to_the_active_guild(authed_client, fake_ctx):
    _make(authed_client)
    with open_db(fake_ctx.db_path) as conn:
        rows = conn.execute("SELECT guild_id FROM todo_recurring").fetchall()
    assert [r["guild_id"] for r in rows] == [GUILD]


def test_moderator_can_manage_recurring(mod_client):
    """Recurring entries are worklist curation — mods, not just admins."""
    assert _make(mod_client).status_code == 200
    assert mod_client.get("/api/todos/recurring").status_code == 200


def test_run_now_does_not_resume_a_paused_entry(authed_client):
    """Adding one instance by hand must not silently restart the schedule."""
    rid = _make(authed_client).json()["id"]
    authed_client.post(f"/api/todos/recurring/{rid}/pause")
    resp = authed_client.post(f"/api/todos/recurring/{rid}/run-now")
    assert resp.status_code == 200
    assert resp.json()["spawned"] is True
    item = authed_client.get("/api/todos/recurring").json()["items"][0]
    assert item["status"] == "paused"
    assert authed_client.get("/api/todos").json()["pending_count"] == 1


def test_run_now_leaves_the_schedule_alone(authed_client):
    rid = _make(authed_client).json()["id"]
    before = authed_client.get("/api/todos/recurring").json()["items"][0]["next_run_at"]
    authed_client.post(f"/api/todos/recurring/{rid}/run-now")
    after = authed_client.get("/api/todos/recurring").json()["items"][0]["next_run_at"]
    assert after == before


# ── the chore board, and the collision guard ──────────────────────────


def test_list_includes_both_boards(authed_client, fake_ctx):
    with open_db(fake_ctx.db_path) as conn:
        save_board(conn, GUILD, 111, 222, kind=BOARD_ALL)
        save_board(conn, GUILD, 333, 444, kind=BOARD_CHORES)
    data = authed_client.get("/api/todos").json()
    assert data["board"]["channel_id"] == "111"
    assert data["chore_board"]["channel_id"] == "333"
    assert data["chore_board"]["posted"] is True


def test_admin_can_post_the_chore_board(authed_client, fake_ctx):
    cog = _attach_bot(fake_ctx)
    resp = authed_client.put(
        "/api/todos/board", json={"channel_id": "555", "kind": "chores"}
    )
    assert resp.status_code == 200
    assert resp.json()["kind"] == "chores"
    cog.place_chore_board.assert_awaited_once()
    cog.place_board.assert_not_awaited()


def test_chore_board_placement_requires_admin(mod_client, fake_ctx):
    cog = _attach_bot(fake_ctx, perm_bits=_MOD_ONLY_BITS)
    resp = mod_client.put(
        "/api/todos/board", json={"channel_id": "555", "kind": "chores"}
    )
    assert resp.status_code == 403
    cog.place_chore_board.assert_not_awaited()


def test_zero_channel_unposts_the_chore_board(authed_client, fake_ctx):
    cog = _attach_bot(fake_ctx)
    resp = authed_client.put(
        "/api/todos/board", json={"channel_id": "0", "kind": "chores"}
    )
    assert resp.status_code == 200
    cog.unpost_chore_board.assert_awaited_once()
    cog.unpost_board.assert_not_awaited()


def test_an_unknown_board_kind_is_a_400(authed_client, fake_ctx):
    _attach_bot(fake_ctx)
    resp = authed_client.put(
        "/api/todos/board", json={"channel_id": "555", "kind": "nonsense"}
    )
    assert resp.status_code == 400


def test_omitting_the_kind_still_means_the_all_todos_board(authed_client, fake_ctx):
    """An older client that never learned about `kind` keeps working."""
    cog = _attach_bot(fake_ctx)
    resp = authed_client.put("/api/todos/board", json={"channel_id": "555"})
    assert resp.status_code == 200
    cog.place_board.assert_awaited_once()
    cog.place_chore_board.assert_not_awaited()


def test_the_two_boards_are_refused_the_same_channel(authed_client, fake_ctx):
    """The guard this feature exists behind.

    A channel has one bottom slot. Two sticky boards in it wake on the same
    message, race, and one ends up buried above the other with nothing anyone
    does in the channel able to raise it (see
    ``test_two_default_panels_cannot_both_hold_the_channel_bottom`` in
    tests/test_core_sticky.py). The sticky layer cannot arbitrate that —
    someone has to lose — so configuration refuses it, and nothing is posted.
    """
    cog = _attach_bot(fake_ctx)
    with open_db(fake_ctx.db_path) as conn:
        save_board(conn, GUILD, 555, 222, kind=BOARD_ALL)

    resp = authed_client.put(
        "/api/todos/board", json={"channel_id": "555", "kind": "chores"}
    )
    assert resp.status_code == 409
    assert "server todo board" in resp.json()["detail"]
    cog.place_chore_board.assert_not_awaited()


def test_the_collision_is_refused_from_either_direction(authed_client, fake_ctx):
    """Adding the second board must not open the hole the other way round."""
    cog = _attach_bot(fake_ctx)
    with open_db(fake_ctx.db_path) as conn:
        save_board(conn, GUILD, 555, 222, kind=BOARD_CHORES)

    resp = authed_client.put(
        "/api/todos/board", json={"channel_id": "555", "kind": "all"}
    )
    assert resp.status_code == 409
    assert "chore board" in resp.json()["detail"]
    cog.place_board.assert_not_awaited()


def test_a_board_can_be_reposted_into_its_own_channel(authed_client, fake_ctx):
    """Re-posting where it already lives is a move, not a collision."""
    cog = _attach_bot(fake_ctx)
    with open_db(fake_ctx.db_path) as conn:
        save_board(conn, GUILD, 555, 222, kind=BOARD_ALL)

    resp = authed_client.put("/api/todos/board", json={"channel_id": "555"})
    assert resp.status_code == 200
    cog.place_board.assert_awaited_once()


def test_removing_one_board_frees_its_channel_for_the_other(authed_client, fake_ctx):
    """Unposting must actually release the channel, not reserve it forever."""
    cog = _attach_bot(fake_ctx)
    with open_db(fake_ctx.db_path) as conn:
        save_board(conn, GUILD, 555, 222, kind=BOARD_ALL)
        # What the real cog's _write_ids does on unpost. The cog here is a
        # mock, so driving it through the route would not touch the DB.
        clear_board(conn, GUILD, kind=BOARD_ALL)

    resp = authed_client.put(
        "/api/todos/board", json={"channel_id": "555", "kind": "chores"}
    )
    assert resp.status_code == 200
    cog.place_chore_board.assert_awaited_once()
