"""Tests for web_server/routes/todo.py — tasks, board placement, recurring."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock

import discord

import pytest
from fastapi.testclient import TestClient

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.services.todo_service import (
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
    """Adding and completing each repaint it; the panel is signature-guarded,
    so a call that changed nothing costs a DB read and no API call."""
    cog = _attach_bot(fake_ctx)
    todo_id = authed_client.post("/api/todos", json={"task": "Do it"}).json()["id"]
    authed_client.post(f"/api/todos/{todo_id}/complete")
    assert cog.refresh_board.await_count == 2


def test_board_refresh_failure_does_not_fail_the_request(authed_client, fake_ctx):
    """The 60s loop repaints anyway — a Discord hiccup must not lose the task."""
    cog = _attach_bot(fake_ctx)
    cog.refresh_board = AsyncMock(side_effect=RuntimeError("gateway down"))
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


# ── board placement vs the other sticky panels ────────────────────────
#
# The same-channel refusal above only ever knew about the *other* todo board.
# Every other sticky panel in the guild was invisible to it until the registry
# landed (2026-08-22).


def _occupy(fake_ctx, channel_id: int, *, key: str) -> None:
    from bot_modules.core.db_utils import open_db

    with open_db(fake_ctx.db_path) as conn:
        if key == "survivor":
            from bot_modules.services.survivor_service import (
                create_season,
                set_panel_ids,
            )

            create_season(conn, GUILD, "S", 2035)
            set_panel_ids(conn, GUILD, channel_id, 1)
        elif key == "pen-pals":
            conn.execute(
                "INSERT INTO pen_pals_config (guild_id, panel_channel_id)"
                " VALUES (?, ?)",
                (GUILD, channel_id),
            )
        else:  # pragma: no cover - guards a typo in a test
            raise AssertionError(key)


def test_board_refuses_a_channel_the_survivor_panel_holds(authed_client, fake_ctx):
    """The Survivor panel re-sticks under the bot's own posts, so a board
    sharing its channel is buried after every Reckoning and never comes back."""
    cog = _attach_bot(fake_ctx)
    _occupy(fake_ctx, 555, key="survivor")
    resp = authed_client.put("/api/todos/board", json={"channel_id": "555"})
    assert resp.status_code == 400
    assert "Survivor panel" in resp.json()["detail"]
    cog.place_board.assert_not_awaited()


def test_board_warns_beside_a_human_only_panel(authed_client, fake_ctx):
    cog = _attach_bot(fake_ctx)
    _occupy(fake_ctx, 555, key="pen-pals")
    resp = authed_client.put("/api/todos/board", json={"channel_id": "555"})
    assert resp.status_code == 200
    assert "pen pals panel" in resp.json()["warning"]
    cog.place_board.assert_awaited_once()


def _seed_board(fake_ctx, channel_id: int) -> None:
    """Record a board as posted. ``place_board`` is mocked in these tests, so
    the row it would normally write has to be put there directly."""
    from bot_modules.core.db_utils import open_db
    from bot_modules.services.todo_service import save_board

    with open_db(fake_ctx.db_path) as conn:
        save_board(conn, GUILD, channel_id, 1)


def test_reposting_a_board_where_it_already_is_is_not_a_conflict(
    authed_client, fake_ctx
):
    """Re-posting is how a board that has drifted out of view is rescued, so
    a board must never be refused on account of itself."""
    cog = _attach_bot(fake_ctx)
    _seed_board(fake_ctx, 555)
    resp = authed_client.put("/api/todos/board", json={"channel_id": "555"})
    assert resp.status_code == 200
    assert resp.json()["warning"] is None
    cog.place_board.assert_awaited_once()


# ── recurring tasks ───────────────────────────────────────────────────


def _make(client, **over):
    body = {"task": "Post QOTD", "recurrence": "daily", "time_of_day": 540}
    body.update(over)
    return client.post("/api/todos/recurring", json=body)


def _make_unspawned(fake_ctx, **over) -> int:
    """A recurring definition with nothing outstanding behind it.

    The create route spawns today's instance when the chore's time of day has
    already gone by, which for a 09:00 daily depends on what time the suite is
    run — so the run-now tests, which are about what *that* button reaches,
    insert the definition directly with create-time spawning off.
    """
    import time as _time

    from bot_modules.services.todo_recurring_service import create_recurring

    kwargs = {"task": "Post QOTD", "recurrence": "daily", "time_of_day": 540}
    kwargs.update(over)
    with open_db(fake_ctx.db_path) as conn:
        return create_recurring(
            conn, GUILD, now_ts=_time.time(), spawn_if_slot_passed=False, **kwargs
        )


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


def test_auto_complete_round_trips(authed_client):
    """The trigger the picker sends comes back on the row it configures."""
    assert _make(authed_client, auto_complete="qotd").status_code == 200
    item = authed_client.get("/api/todos/recurring").json()["items"][0]
    assert item["auto_complete"] == "qotd"

    body = {
        "task": "Post QOTD", "recurrence": "daily", "time_of_day": 540,
        "auto_complete": "",  # the picker's own "Nothing" option
    }
    resp = authed_client.put(f"/api/todos/recurring/{item['id']}", json=body)
    assert resp.status_code == 200
    updated = authed_client.get("/api/todos/recurring").json()["items"][0]
    assert updated["auto_complete"] is None


def test_a_put_without_the_field_keeps_the_trigger(authed_client):
    """A stale client editing the schedule must not un-wire the automation."""
    _make(authed_client, auto_complete="game")
    item = authed_client.get("/api/todos/recurring").json()["items"][0]

    resp = authed_client.put(
        f"/api/todos/recurring/{item['id']}",
        json={"task": "Run any game", "recurrence": "daily", "time_of_day": 600},
    )
    assert resp.status_code == 200
    updated = authed_client.get("/api/todos/recurring").json()["items"][0]
    assert updated["time_of_day"] == 600
    assert updated["auto_complete"] == "game"


def test_a_chore_is_hand_ticked_unless_asked_otherwise(authed_client):
    """Every definition that predates the picker sends no field at all."""
    _make(authed_client)
    assert authed_client.get("/api/todos/recurring").json()["items"][0]["auto_complete"] is None


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"task": "  "}, "empty"),
        ({"recurrence": "hourly"}, "daily or weekly"),
        ({"time_of_day": 5000}, "00:00"),
        ({"recurrence": "weekly", "recur_days": []}, "day of the week"),
        # A trigger nothing fires would be a chore that silently never signs
        # itself off, so it is rejected rather than stored.
        ({"auto_complete": "photo"}, "QOTD"),
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
    rid = _make_unspawned(fake_ctx)
    resp = authed_client.post(f"/api/todos/recurring/{rid}/run-now")
    assert resp.status_code == 200
    assert resp.json()["spawned"] is True
    todos = authed_client.get("/api/todos").json()["todos"]
    assert [t["task"] for t in todos] == ["Post QOTD"]
    cog.refresh_board.assert_awaited()


def test_run_now_twice_does_not_duplicate(authed_client, fake_ctx):
    """Skip-if-pending: the same chore must not stack two identical rows."""
    rid = _make_unspawned(fake_ctx)
    authed_client.post(f"/api/todos/recurring/{rid}/run-now")
    resp = authed_client.post(f"/api/todos/recurring/{rid}/run-now")
    assert resp.json()["spawned"] is False
    assert "already on the list" in resp.json()["detail"]
    assert authed_client.get("/api/todos").json()["pending_count"] == 1


def test_chore_added_after_its_time_is_tickable_immediately(authed_client):
    """A chore whose slot has gone by arrives with today's instance already on
    the list, instead of sitting ⬜ open on the board with nothing to tick."""
    # 00:00 — always behind whatever time the suite runs at.
    _make(authed_client, task="Overnight sweep", time_of_day=0)
    todos = authed_client.get("/api/todos").json()["todos"]
    assert [t["task"] for t in todos] == ["Overnight sweep"]


def test_recurring_is_scoped_to_the_active_guild(authed_client, fake_ctx):
    _make(authed_client)
    with open_db(fake_ctx.db_path) as conn:
        rows = conn.execute("SELECT guild_id FROM todo_recurring").fetchall()
    assert [r["guild_id"] for r in rows] == [GUILD]


def test_moderator_can_manage_recurring(mod_client):
    """Recurring entries are worklist curation — mods, not just admins."""
    assert _make(mod_client).status_code == 200
    assert mod_client.get("/api/todos/recurring").status_code == 200


def test_run_now_does_not_resume_a_paused_entry(authed_client, fake_ctx):
    """Adding one instance by hand must not silently restart the schedule."""
    rid = _make_unspawned(fake_ctx)
    authed_client.post(f"/api/todos/recurring/{rid}/pause")
    resp = authed_client.post(f"/api/todos/recurring/{rid}/run-now")
    assert resp.status_code == 200
    assert resp.json()["spawned"] is True
    item = authed_client.get("/api/todos/recurring").json()["items"][0]
    assert item["status"] == "paused"
    assert authed_client.get("/api/todos").json()["pending_count"] == 1


def test_run_now_leaves_the_schedule_alone(authed_client, fake_ctx):
    rid = _make_unspawned(fake_ctx)
    before = authed_client.get("/api/todos/recurring").json()["items"][0]["next_run_at"]
    authed_client.post(f"/api/todos/recurring/{rid}/run-now")
    after = authed_client.get("/api/todos/recurring").json()["items"][0]["next_run_at"]
    assert after == before


# ── the one board (migration 180) ─────────────────────────────────────


def test_the_list_no_longer_carries_a_second_board(authed_client, fake_ctx):
    """Migration 180 left one board; a stale second key would mislead a client."""
    with open_db(fake_ctx.db_path) as conn:
        save_board(conn, GUILD, 111, 222)
    data = authed_client.get("/api/todos").json()
    assert data["board"]["channel_id"] == "111"
    assert data["board"]["posted"] is True
    assert "chore_board" not in data
    assert "kind" not in data["board"]


def test_a_stray_kind_field_is_refused(authed_client, fake_ctx):
    """The body forbids extras, so a client still sending `kind` is told so
    rather than having it silently ignored and posting the wrong board."""
    _attach_bot(fake_ctx)
    resp = authed_client.put(
        "/api/todos/board", json={"channel_id": "555", "kind": "chores"}
    )
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/api/todos/recurring", {"task": "Post QOTD"}),
        ("post", "/api/todos/recurring/{id}/pause", None),
        ("post", "/api/todos/recurring/{id}/resume", None),
        ("delete", "/api/todos/recurring/{id}", None),
    ],
)
def test_changing_a_definition_repaints_the_board(
    authed_client, fake_ctx, method, path, body
):
    """The board lists one row per chore *definition*, so a CRUD change alters it
    even though no todo row moved — and the 60s loop is no backstop, since it
    only repaints guilds where a spawn or a write-off happened. Without this an
    added chore stays invisible and a deleted one leaves a ghost row until the
    next scheduled fire: a day away for a daily, a week for a weekly."""
    cog = _attach_bot(fake_ctx)
    existing = authed_client.post(
        "/api/todos/recurring", json={"task": "Seed", "recurrence": "daily"}
    ).json()["id"]
    cog.refresh_board.reset_mock()

    url = path.format(id=existing)
    resp = getattr(authed_client, method)(url, **({"json": body} if body else {}))

    assert resp.status_code == 200
    cog.refresh_board.assert_awaited()


def test_editing_a_definition_repaints_the_board(authed_client, fake_ctx):
    cog = _attach_bot(fake_ctx)
    rid = authed_client.post(
        "/api/todos/recurring", json={"task": "Seed", "recurrence": "daily"}
    ).json()["id"]
    cog.refresh_board.reset_mock()

    resp = authed_client.put(
        f"/api/todos/recurring/{rid}",
        json={"task": "Renamed", "recurrence": "daily"},
    )
    assert resp.status_code == 200
    cog.refresh_board.assert_awaited()


def test_a_failed_definition_change_does_not_repaint(authed_client, fake_ctx):
    """A 404 changed nothing, so it should cost no Discord edit."""
    cog = _attach_bot(fake_ctx)
    assert authed_client.delete("/api/todos/recurring/9999").status_code == 404
    cog.refresh_board.assert_not_awaited()
