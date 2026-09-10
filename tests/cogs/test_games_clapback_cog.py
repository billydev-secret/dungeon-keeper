"""Clapback's recap relaunch goes through the slash entry's gate.

The guard's branches are pinned in ``test_games_price_cog.py``; this proves
both of Clapback's Play Again buttons are wired through it. They call
``_start_new_game`` rather than ``launch`` (the recap carries a fully built
config), so the gate has to sit on the button itself.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import bot_modules.cogs.games_clapback_cog as cog_module
from bot_modules.cogs.games_clapback_cog import ClapbackCog, ClapbackJoinView, ClapbackRecapView
from bot_modules.games.utils.game_manager import create_game
from bot_modules.services.games_db import GamesDb

GUILD = 4242
CHAN = 779
HOST = 1


def _interaction():
    return SimpleNamespace(
        user=SimpleNamespace(id=HOST, display_name="Host"),
        guild=None,
        guild_id=GUILD,
        channel_id=CHAN,
        channel=SimpleNamespace(id=CHAN, name="games", guild=None, send=AsyncMock()),
        message=SimpleNamespace(edit=AsyncMock()),
        response=SimpleNamespace(
            send_message=AsyncMock(), defer=AsyncMock(), edit_message=AsyncMock()
        ),
    )


@pytest.mark.parametrize("button", ["play_again", "play_again_shuffled"])
@pytest.mark.parametrize("enabled", [True, False], ids=["on", "off"])
async def test_play_again_honours_the_enabled_dial(
    sync_db_path, enabled, button, monkeypatch
):
    monkeypatch.setattr(cog_module, "sign_off_game_chore", AsyncMock())
    bot = SimpleNamespace(
        games_db=GamesDb(sync_db_path), active_views={},
        ctx=SimpleNamespace(db_path=sync_db_path),
    )
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    start = AsyncMock(return_value="new-gid")
    cog._start_new_game = start  # type: ignore[method-assign]
    await cog.db.execute(
        "INSERT INTO games_allowed_channels (channel_id, guild_id) VALUES (?, ?)",
        (CHAN, GUILD),
    )
    await cog.db.execute(
        "INSERT INTO games_game_config (guild_id, game_type, enabled) VALUES (?, ?, ?)",
        (GUILD, "clapback", int(enabled)),
    )
    # The shared guard also refuses an empty bank; this test is about the dial.
    await cog.db.execute(
        "INSERT INTO games_question_bank (game_type, category, question_text)"
        " VALUES ('clapback', 'sfw', 'p?')",
    )
    config = {
        "rounds": 3, "timer": 60, "vote_timer": 30, "anonymous": False,
        # The finished game's countdown, long spent.
        "start_epoch": 1_700_000_000,
    }
    view = ClapbackRecapView("old-gid", HOST, config, cog.db, bot, cog, players=[HOST, 2, 3])
    interaction = _interaction()

    await getattr(view, button).callback(interaction)  # type: ignore[arg-type]

    if enabled:
        start.assert_awaited_once()
        # The rematch lobby opens with the finished roster seated (clapback-10)
        # and no countdown: a seeded roster plus a stale start_epoch would have
        # the sweep start it on its next tick without the host's press.
        assert start.await_args is not None
        assert start.await_args.kwargs["players"] == [HOST, 2, 3]
        assert start.await_args.kwargs["config"].get("start_epoch") is None
        interaction.response.send_message.assert_not_awaited()
    else:
        start.assert_not_awaited()
        kwargs = interaction.response.send_message.await_args.kwargs
        assert kwargs["ephemeral"] is True
        assert "disabled" in interaction.response.send_message.await_args.args[0]
        # The recap card is left alone so the host can retry once it is back on.
        interaction.response.edit_message.assert_not_awaited()


@pytest.mark.parametrize("button", ["play_again", "play_again_shuffled"])
async def test_play_again_bank_check_honours_the_channels_age_gate(
    sync_db_path, button, monkeypatch
):
    """The shared guard's empty-bank check reads the bank through the
    channel's age-gate, as the slash entry does — an NSFW-only bank must not
    refuse a rematch in the age-restricted room it was just played in."""
    monkeypatch.setattr(cog_module, "sign_off_game_chore", AsyncMock())
    bot = SimpleNamespace(
        games_db=GamesDb(sync_db_path), active_views={},
        ctx=SimpleNamespace(db_path=sync_db_path),
    )
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    start = AsyncMock(return_value="new-gid")
    cog._start_new_game = start  # type: ignore[method-assign]
    await cog.db.execute(
        "INSERT INTO games_allowed_channels (channel_id, guild_id) VALUES (?, ?)",
        (CHAN, GUILD),
    )
    await cog.db.execute(
        "INSERT INTO games_question_bank (game_type, category, question_text, tags)"
        " VALUES ('clapback', 'nsfw', 'p?', '[\"nsfw\"]')",
    )
    config = {"rounds": 3, "timer": 60, "vote_timer": 30, "anonymous": False}
    view = ClapbackRecapView("old-gid", HOST, config, cog.db, bot, cog)
    interaction = _interaction()
    interaction.channel.is_nsfw = lambda: True

    await getattr(view, button).callback(interaction)  # type: ignore[arg-type]

    start.assert_awaited_once()
    interaction.response.send_message.assert_not_awaited()


# ── The no-contact gate is wired through Start and the bracket ───────────────
#
# The gate's decisions live in games_clapback/logic.py and are pinned in
# tests/test_games_clapback_logic.py; these prove the cog actually fetches
# the pairs and hands them to the two bracket functions.

A, B, C = 11, 12, 13


def _bot(sync_db_path):
    return SimpleNamespace(
        games_db=GamesDb(sync_db_path), active_views={},
        ctx=SimpleNamespace(db_path=sync_db_path),
    )


def _start_interaction():
    interaction = _interaction()
    interaction.guild = SimpleNamespace(id=GUILD)
    return interaction


@pytest.mark.parametrize(
    "pairs, starts",
    [
        pytest.param([(A, B)], True, id="one-pair-still-three-playable"),
        pytest.param([(A, B), (A, C)], False, id="one-member-blocked-from-both"),
    ],
)
async def test_start_counts_only_players_the_list_lets_play(sync_db_path, pairs, starts):
    """A three-player lobby where one member is kept apart from both others
    has no game in it, and the host gets the ordinary short-lobby line —
    roster count and all, so it reads exactly like any other refusal."""
    from bot_modules.games.utils.game_manager import create_game
    from bot_modules.services.no_contact_service import add_pair

    for x, y in pairs:
        add_pair(sync_db_path, GUILD, x, y, created_by=x)
    bot = _bot(sync_db_path)
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    run = AsyncMock()
    cog._run_game = run  # type: ignore[method-assign]
    config = {"rounds": 3, "timer": 60, "vote_timer": 30, "anonymous": False}
    game_id = await create_game(
        cog.db, CHAN, HOST, "clapback", state="joining",
        payload={"config": config, "players": [A, B, C], "host_id": HOST},
    )
    view = cog_module.ClapbackJoinView(game_id, HOST, cog.db, bot, cog, config)
    interaction = _start_interaction()

    await view.start_game.callback(interaction)  # type: ignore[arg-type]

    if starts:
        run.assert_awaited_once()
        interaction.response.send_message.assert_not_awaited()
    else:
        run.assert_not_awaited()
        args, kwargs = interaction.response.send_message.await_args
        assert args[0] == "Need at least 3 players to start Clapback. Currently: 3."
        assert kwargs["ephemeral"] is True


async def test_bracket_never_seats_the_pair(sync_db_path, monkeypatch):
    """One round of a three-player game with A and B on the list: the
    pre-picked bye is one of them, the round's only matchup is the other
    against C, and the bye is paid and recorded like any bye."""
    from bot_modules.games.utils.game_manager import create_game, get_game_payload
    from bot_modules.services.no_contact_service import add_pair

    add_pair(sync_db_path, GUILD, A, B, created_by=A)
    bot = _bot(sync_db_path)
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    config = {"rounds": 1, "timer": 60, "vote_timer": 30, "anonymous": False}
    game_id = await create_game(
        cog.db, CHAN, HOST, "clapback", state="playing",
        payload={
            "config": config, "players": [A, B, C], "host_id": HOST,
            "scores": {str(p): 0 for p in (A, B, C)},
            "scores_checkpoint": {str(p): 0 for p in (A, B, C)},
            "clapbacks": {str(p): 0 for p in (A, B, C)},
            "round_history": [], "bye_history": [], "used_prompts": [],
        },
    )
    # The loop reads "no live view" as a cancelled game.
    bot.active_views[game_id] = object()
    monkeypatch.setattr(cog_module, "fetch_prompt", AsyncMock(return_value="A prompt"))

    submit_byes: list = []

    async def submit_phase(game_id, channel, payload, prompt, round_num, config, host_id, bye_player=None):
        submit_byes.append(bye_player)
        return {
            str(p): f"answer{p}" for p in (A, B, C)
            if bye_player is None or str(p) != str(bye_player)
        }

    seated: list[set[int]] = []

    async def vote_matchup(game_id, channel, payload, mi, matchup, answers, *rest, **kw):
        a, b = int(matchup["pair"][0]), int(matchup["pair"][1])
        seated.append({a, b})
        return {
            "player_a": a, "answer_a": answers[str(a)], "votes_a": 2,
            "player_b": b, "answer_b": answers[str(b)], "votes_b": 0,
            "clapback": True, "_scores": {a: 125, b: 0},
        }

    monkeypatch.setattr(cog, "_submit_phase", submit_phase)
    monkeypatch.setattr(cog, "_vote_matchup", vote_matchup)
    monkeypatch.setattr(cog, "_post_scoreboard", AsyncMock())
    monkeypatch.setattr(cog, "_post_recap", AsyncMock())
    channel = SimpleNamespace(id=CHAN, name="games", guild=SimpleNamespace(id=GUILD), send=AsyncMock())

    await cog._run_game(game_id, channel, await get_game_payload(cog.db, game_id))

    assert len(submit_byes) == 1 and submit_byes[0] in {str(A), str(B)}
    bye = int(submit_byes[0])
    assert seated == [{C, A + B - bye}]
    payload = await get_game_payload(cog.db, game_id)
    record = payload["round_history"][0]
    assert record["bye_players"] == [str(bye)]
    # The bye is paid the round's average like any other bye.
    assert payload["scores"][str(bye)] == record["bye_award"] == round((125 + 0) / 2)


# ── clapback-9: a lobby that dies says who was in it and why ─────────────────
#
# ``end_game``'s own recording (stored payload, derived roster, ``reason``) is
# pinned in tests/test_game_manager_end_game.py; these prove the two lobby
# cancel paths hand it the roster and a reason instead of a bare call.


async def _history(db, game_id):
    row = await db.fetchone(
        "SELECT guild_id, player_count, payload FROM games_game_history WHERE game_id = ?",
        (game_id,),
    )
    assert row is not None
    return row["guild_id"], row["player_count"], json.loads(row["payload"])


async def test_a_timed_out_lobby_archives_its_roster_and_the_reason(sync_db_path):
    bot = _bot(sync_db_path)
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    config = {"rounds": 3, "timer": 60, "vote_timer": 30, "anonymous": False}
    gid = await create_game(
        cog.db, CHAN, HOST, "clapback", state="joining",
        payload={"config": config, "players": [HOST, 2, 3], "host_id": HOST}, guild_id=GUILD,
    )

    await cog._cancel_game(gid, reason="lobby_timeout")

    guild_id, player_count, payload = await _history(cog.db, gid)
    assert guild_id == GUILD
    assert player_count == 3
    assert payload["players"] == [HOST, 2, 3]
    assert payload["reason"] == "lobby_timeout"
    assert gid not in bot.active_views


async def test_a_crashed_start_archives_its_roster_and_the_reason(sync_db_path, monkeypatch):
    bot = _bot(sync_db_path)
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    monkeypatch.setattr(cog, "_forbidden_pairs", AsyncMock(return_value=set()))

    async def boom(game_id, channel, payload):
        raise RuntimeError("bracket exploded")

    monkeypatch.setattr(cog, "_run_game", boom)
    config = {"rounds": 3, "timer": 60, "vote_timer": 30, "anonymous": False}
    gid = await create_game(
        cog.db, CHAN, HOST, "clapback", state="joining",
        payload={"config": config, "players": [HOST, 2, 3], "host_id": HOST}, guild_id=GUILD,
    )
    view = ClapbackJoinView(gid, HOST, cog.db, bot, cog, config)
    bot.active_views[gid] = view

    await view.start_game.callback(_start_interaction())  # type: ignore[arg-type]

    guild_id, player_count, payload = await _history(cog.db, gid)
    assert guild_id == GUILD
    assert player_count == 3
    assert payload["players"] == [HOST, 2, 3]
    assert payload["reason"] == "crash"
    assert gid not in bot.active_views


# ── P4: pacing, edges and the lobby's dead ends ──────────────────────────────


def _msg():
    return SimpleNamespace(id=5150, edit=AsyncMock())


def _channel(guild=None):
    return SimpleNamespace(
        id=CHAN, name="games", guild=guild, send=AsyncMock(return_value=_msg()),
    )


async def test_start_new_game_seeds_the_roster_and_rereads_the_age_gate(sync_db_path):
    """The recap carries the finished game's config, whose allow_nsfw was
    read when *that* game launched; the new lobby reads the channel again
    (safety-sweep-10) and seats the seeded roster (clapback-10)."""
    from bot_modules.games.utils.game_manager import get_game_payload

    bot = _bot(sync_db_path)
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    channel = _channel()
    channel.is_nsfw = lambda: False
    config = {"rounds": 3, "timer": 60, "vote_timer": 30, "anonymous": False, "allow_nsfw": True}

    gid = await cog._start_new_game(
        channel=channel, host_id=HOST, host_name="Host", guild=None, config=config,
        players=[2, 3, 3, HOST],
    )

    assert gid
    payload = await get_game_payload(cog.db, gid)
    assert payload["players"] == [2, 3, HOST]
    assert payload["config"]["allow_nsfw"] is False
    assert config["allow_nsfw"] is True  # the recap's copy is left alone
    embed = channel.send.await_args.kwargs["embed"]
    assert embed.fields[0].name == "Players (3)"


async def test_late_answer_modal_is_refused_once_the_window_closes(sync_db_path):
    """A modal opened in round 1 and sent during the vote (clapback-4)."""
    from bot_modules.cogs.games_clapback_cog import ClapbackAnswerModal
    from bot_modules.games.utils.game_manager import get_game_payload

    bot = _bot(sync_db_path)
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    gid = await create_game(
        cog.db, CHAN, HOST, "clapback", state="playing",
        payload={"config": {}, "players": [HOST, 2], "phase": "voting",
                 "current_round": 1, "answers": {"2": "kept"}},
    )
    modal = ClapbackAnswerModal(gid, 1, cog.db, cog)
    modal.answer_input._value = "too late"  # type: ignore[attr-defined]
    interaction = _interaction()

    await modal.on_submit(interaction)  # type: ignore[arg-type]

    args, kwargs = interaction.response.send_message.await_args
    assert args[0] == "❌ Answers for round 1 are closed."
    assert kwargs["ephemeral"] is True
    assert (await get_game_payload(cog.db, gid))["answers"] == {"2": "kept"}


async def test_lobby_cancel_archives_the_lobby_and_retires_the_message(sync_db_path):
    """Host presses Cancel → confirm → the row is archived as cancelled and
    the lobby message says so with its buttons off (clapback-12)."""
    bot = _bot(sync_db_path)
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    config = {"rounds": 3, "timer": 60, "vote_timer": 30, "anonymous": False}
    gid = await create_game(
        cog.db, CHAN, HOST, "clapback", state="joining",
        payload={"config": config, "players": [HOST, 2], "host_id": HOST}, guild_id=GUILD,
    )
    view = ClapbackJoinView(gid, HOST, cog.db, bot, cog, config)
    lobby_msg = _msg()
    view.message = lobby_msg  # type: ignore[assignment]
    bot.active_views[gid] = view
    interaction = _start_interaction()

    await view.cancel.callback(interaction)  # type: ignore[arg-type]

    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    confirm = kwargs["view"]
    await confirm._callback(SimpleNamespace())

    _, player_count, payload = await _history(cog.db, gid)
    assert player_count == 2 and payload["reason"] == "cancelled"
    assert gid not in bot.active_views
    edit = lobby_msg.edit.await_args.kwargs
    assert "Lobby cancelled" in edit["content"]
    assert all(getattr(item, "disabled") for item in view.children)


async def test_lobby_cancel_is_host_or_mod_only(sync_db_path):
    bot = _bot(sync_db_path)
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    config = {"rounds": 3, "timer": 60, "vote_timer": 30}
    gid = await create_game(
        cog.db, CHAN, HOST, "clapback", state="joining",
        payload={"config": config, "players": [HOST, 2], "host_id": HOST},
    )
    view = ClapbackJoinView(gid, HOST, cog.db, bot, cog, config)
    interaction = _start_interaction()
    interaction.user = SimpleNamespace(id=2, display_name="Two", guild_permissions=None)

    await view.cancel.callback(interaction)  # type: ignore[arg-type]

    args, kwargs = interaction.response.send_message.await_args
    assert args[0].startswith("❌") and kwargs["ephemeral"] is True
    assert "view" not in kwargs


async def test_recap_view_timeout_retires_its_buttons(sync_db_path):
    """Play Again looked live two minutes after the recap and failed on the
    click (clapback-13)."""
    bot = _bot(sync_db_path)
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    view = ClapbackRecapView("gid", HOST, {}, cog.db, bot, cog, players=[1])
    recap_msg = _msg()
    view.message = recap_msg  # type: ignore[assignment]

    await view.on_timeout()

    assert all(getattr(item, "disabled") for item in view.children)
    recap_msg.edit.assert_awaited_once()
    assert view.timeout == 600


@pytest.mark.parametrize(
    "who, answers, closes, reply",
    [
        pytest.param(HOST, {"1": "a", "2": "b"}, True, "🔒 Closing answers with 2 in.", id="host"),
        pytest.param(HOST, {"1": "a"}, False, "❌ Only 1 answer in — at least 2 are needed to run the round.", id="one-answer"),
        pytest.param(2, {"1": "a", "2": "b"}, False, "❌ Only the host or a mod can close answers.", id="not-host"),
    ],
)
async def test_close_answers_button(sync_db_path, who, answers, closes, reply):
    from bot_modules.cogs.games_clapback_cog import ClapbackSubmitView

    bot = _bot(sync_db_path)
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    gid = await create_game(
        cog.db, CHAN, HOST, "clapback", state="playing",
        payload={"config": {}, "players": [HOST, 2, 3], "phase": "submitting", "answers": answers},
    )
    view = ClapbackSubmitView(gid, HOST, 1, cog.db, bot, cog)
    import asyncio
    cog._submit_events[gid] = asyncio.Event()
    interaction = _start_interaction()
    interaction.user = SimpleNamespace(id=who, display_name="P", guild_permissions=None)

    await view.close_answers.callback(interaction)  # type: ignore[arg-type]

    assert interaction.response.send_message.await_args.args[0] == reply
    assert view.close_requested is closes
    assert cog._submit_events[gid].is_set() is closes


async def test_submit_window_closes_one_short_once_quiet(sync_db_path, monkeypatch):
    """Three writers, two answers in, nothing for the idle window: the round
    closes instead of waiting the whole timer for the absent one (clapback-2),
    and the phase is shut before the answers are read (clapback-4)."""
    from bot_modules.games.utils.game_manager import get_game_payload
    from bot_modules.games_clapback import logic as clapback_logic

    monkeypatch.setattr(clapback_logic, "SUBMIT_IDLE_CLOSE_SECONDS", 1)
    bot = _bot(sync_db_path)
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    config = {"rounds": 1, "timer": 30, "vote_timer": 30}
    gid = await create_game(
        cog.db, CHAN, HOST, "clapback", state="playing",
        payload={"config": config, "players": [HOST, 2, 3], "phase": "submitting",
                 "current_round": 1, "answers": {"1": "a", "2": "b"}},
    )
    bot.active_views[gid] = object()
    channel = _channel()

    import time
    started = time.monotonic()
    answers = await cog._submit_phase(
        gid, channel, await get_game_payload(cog.db, gid), "prompt?", 1, config, HOST,
    )

    assert answers == {"1": "a", "2": "b"}
    assert time.monotonic() - started < 10
    assert (await get_game_payload(cog.db, gid))["phase"] == "bracketing"


@pytest.mark.parametrize(
    "voters, early",
    [
        pytest.param(["3", "4"], True, id="eligible-all-voted"),
        pytest.param(["3", "4", "5"], True, id="bye-voted-too"),
        pytest.param(["3", "4", "99"], False, id="spectator-keeps-the-timer"),
    ],
)
async def test_vote_matchup_closes_once_every_eligible_player_has_voted(
    sync_db_path, monkeypatch, voters, early,
):
    """Five on the roster, 5 benched: once 3 and 4 have voted the matchup
    reveals after the grace (decision D1); a spectator's vote keeps the
    full timer, the case the June decision protected."""
    from bot_modules.games.utils.game_manager import get_game_payload
    from bot_modules.games_clapback import logic as clapback_logic

    monkeypatch.setattr(clapback_logic, "VOTE_CLOSE_GRACE_SECONDS", 0)
    bot = _bot(sync_db_path)
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    cog.REVEAL_SECONDS = 0  # type: ignore[misc]
    vote_timer = 4
    config = {"rounds": 1, "timer": 30, "vote_timer": vote_timer, "anonymous": False}
    matchup = {"pair": ["1", "2"], "votes": {v: "1" for v in voters}, "winner": None}
    gid = await create_game(
        cog.db, CHAN, HOST, "clapback", state="playing",
        payload={"config": config, "players": [1, 2, 3, 4, 5], "phase": "voting",
                 "scores": {str(p): 0 for p in range(1, 6)}, "clapbacks": {},
                 "matchups": [matchup]},
    )
    bot.active_views[gid] = object()

    import time
    started = time.monotonic()
    result = await cog._vote_matchup(
        gid, _channel(), await get_game_payload(cog.db, gid), 0, matchup,
        {"1": "a", "2": "b"}, config, HOST, 1, 1, "prompt?", byes=["5"],
    )
    took = time.monotonic() - started

    assert result is not None and result["votes_a"] == len(voters)
    if early:
        assert took < vote_timer - 1
    else:
        assert took >= vote_timer - 0.5


async def test_mid_game_leave_withdraws_the_score(sync_db_path):
    from bot_modules.games.utils.game_manager import get_game_payload

    bot = _bot(sync_db_path)
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    gid = await create_game(
        cog.db, CHAN, HOST, "clapback", state="playing",
        payload={"config": {}, "players": [HOST, 2, 3], "scores": {"1": 5, "2": 90, "3": 1}},
    )

    ok, text = await cog.mid_game_leave(_channel(), gid, SimpleNamespace(id=2, display_name="Two"))

    assert ok and "withdrawn" in text
    payload = await get_game_payload(cog.db, gid)
    assert payload["players"] == [HOST, 3]
    assert payload["left"] == ["2"]
    assert payload["scores"]["2"] == 90


# ── countdown auto-start (clapback-8) ───────────────────────────────────────


async def _lobby_row(db, gid):
    return await db.fetchone("SELECT * FROM games_active_games WHERE game_id = ?", (gid,))


async def _archived(db, gid):
    """The ``games_game_history`` row ``end_game`` writes, or None. ``reason``
    lives inside its archived payload."""
    row = await db.fetchone(
        "SELECT * FROM games_game_history WHERE game_id = ?", (gid,)
    )
    return json.loads(row["payload"]) if row else None


async def test_auto_start_takes_the_lobby_into_play_without_a_press(sync_db_path, monkeypatch):
    bot = _bot(sync_db_path)
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    monkeypatch.setattr(cog, "_forbidden_pairs", AsyncMock(return_value=set()))
    ran = asyncio.Event()

    async def _run(game_id, channel, payload):
        ran.set()

    monkeypatch.setattr(cog, "_run_game", _run)
    config = {"rounds": 3, "timer": 60, "vote_timer": 30, "anonymous": False, "start_epoch": 1}
    gid = await create_game(
        cog.db, CHAN, HOST, "clapback", state="joining",
        payload={"config": config, "players": [HOST, 2, 3], "host_id": HOST}, guild_id=GUILD,
    )
    view = ClapbackJoinView(gid, HOST, cog.db, bot, cog, config)
    lobby_msg = _msg()
    view.message = lobby_msg  # type: ignore[assignment]
    bot.active_views[gid] = view
    channel = _channel(guild=SimpleNamespace(id=GUILD))

    started = await cog.auto_start(await _lobby_row(cog.db, gid), {"players": [HOST, 2, 3]}, channel)

    assert started is True
    await asyncio.wait_for(ran.wait(), 2)
    row = await _lobby_row(cog.db, gid)
    assert row["state"] == "playing"
    assert view.is_finished()
    lobby_msg.edit.assert_awaited_once()
    assert all(getattr(item, "disabled", False) for item in view.children)
    payload = json.loads(row["payload"])
    assert payload["scores"] == {str(HOST): 0, "2": 0, "3": 0}
    # Three at the floor: the thin-game note goes out as it does on a press.
    channel.send.assert_awaited_once_with(cog_module.THREE_PLAYER_NOTE)


async def test_auto_start_reads_the_roster_fresh(sync_db_path, monkeypatch):
    # A join that landed after the sweep read the row is seated.
    bot = _bot(sync_db_path)
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    monkeypatch.setattr(cog, "_forbidden_pairs", AsyncMock(return_value=set()))
    monkeypatch.setattr(cog, "_run_game", AsyncMock())
    config = {"rounds": 3, "timer": 60, "vote_timer": 30}
    gid = await create_game(
        cog.db, CHAN, HOST, "clapback", state="joining",
        payload={"config": config, "players": [HOST, 2, 3, 4], "host_id": HOST}, guild_id=GUILD,
    )
    bot.active_views[gid] = ClapbackJoinView(gid, HOST, cog.db, bot, cog, config)

    assert await cog.auto_start(await _lobby_row(cog.db, gid), {"players": [HOST, 2, 3]}, _channel()) is True
    payload = json.loads((await _lobby_row(cog.db, gid))["payload"])
    assert set(payload["scores"]) == {str(HOST), "2", "3", "4"}


async def test_auto_start_applies_the_no_contact_floor(sync_db_path):
    # Three joined, one kept apart from both others: Start would refuse, so
    # the sweep must not start it either — it nudges instead.
    from bot_modules.services.no_contact_service import add_pair

    add_pair(sync_db_path, GUILD, A, B, created_by=A)
    add_pair(sync_db_path, GUILD, A, C, created_by=A)
    bot = _bot(sync_db_path)
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    config = {"rounds": 3, "timer": 60, "vote_timer": 30}
    gid = await create_game(
        cog.db, CHAN, HOST, "clapback", state="joining",
        payload={"config": config, "players": [A, B, C], "host_id": A}, guild_id=GUILD,
    )
    view = ClapbackJoinView(gid, A, cog.db, bot, cog, config)
    bot.active_views[gid] = view

    started = await cog.auto_start(
        await _lobby_row(cog.db, gid), {"players": [A, B, C]}, _channel(guild=SimpleNamespace(id=GUILD)),
    )

    assert started is False
    assert (await _lobby_row(cog.db, gid))["state"] == "joining"
    assert not view.is_finished()


async def test_auto_start_refuses_a_lobby_with_no_live_view(sync_db_path):
    bot = _bot(sync_db_path)
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    gid = await create_game(
        cog.db, CHAN, HOST, "clapback", state="joining",
        payload={"config": {}, "players": [HOST, 2, 3], "host_id": HOST}, guild_id=GUILD,
    )
    assert await cog.auto_start(await _lobby_row(cog.db, gid), {"players": [HOST, 2, 3]}, _channel()) is False
    assert (await _lobby_row(cog.db, gid))["state"] == "joining"


def test_setup_registers_the_auto_starter():
    src = open(cog_module.__file__, encoding="utf-8").read()
    assert 'bot.lobby_auto_starters["clapback"] = cog.auto_start' in src


# ── Crash classification (2026-09-10) ────────────────────────────────────
#
# Game 959cd749 was four full rounds into a five-round game with four players
# when ``channel.send`` raised a 503 on the next vote card. ``_play``'s blanket
# ``except Exception`` archived it as a crash — which calls ``end_game`` and so
# *deletes the row ``recover_game`` would have resumed from*, and does it
# without the ``bot=``/``player_ids=`` that pay a roster. Four played rounds,
# nothing paid, nothing resumable.
#
# The payload here carries the checkpoint keys ``_run_game`` resumes on, so
# these rows are the real shape a mid-game crash leaves behind.

IN_PLAY_PAYLOAD = {
    "config": {"rounds": 5, "timer": 60, "vote_timer": 30},
    "players": [HOST, A, B, C],
    "host_id": HOST,
    "scores": {str(HOST): 300, str(A): 210, str(B): 175, str(C): 90},
    "scores_checkpoint": {str(HOST): 300, str(A): 210, str(B): 175, str(C): 90},
    "clapbacks": {str(HOST): 1},
    "clapbacks_checkpoint": {str(HOST): 1},
    "round_history": [{"round": n, "prompt": "p", "matchups": []} for n in range(1, 5)],
}


def _server_error() -> discord.DiscordServerError:
    """The 503 prod raised, built the way discord.py builds it."""
    return discord.DiscordServerError(
        SimpleNamespace(status=503, reason="Service Unavailable"),
        {"code": 0, "message": "upstream connect error"},
    )


async def _in_play_game(cog):
    return await create_game(
        cog.db, CHAN, HOST, "clapback", state="playing",
        payload=IN_PLAY_PAYLOAD, guild_id=GUILD,
    )


def _raiser(exc, calls, *, stop_after=None):
    """A ``_run_game`` stand-in that raises *exc* — or stops raising once it
    has been entered *stop_after* times, standing in for an edge that comes
    back."""

    async def _run(game_id, channel, payload):
        calls.append(game_id)
        if stop_after is not None and len(calls) > stop_after:
            return
        raise exc

    return _run


async def test_a_transient_server_error_leaves_the_game_row_alive(
    sync_db_path, monkeypatch
):
    """The whole point: the row survives, so ``recover_game`` can pick the
    game up at round 5 — and the 24h sweep pays the roster if it doesn't."""
    monkeypatch.setattr(cog_module, "REDRIVE_PAUSE_S", 0)
    bot = _bot(sync_db_path)
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    gid = await _in_play_game(cog)
    channel = _channel()
    calls: list[str] = []
    monkeypatch.setattr(cog, "_run_game", _raiser(_server_error(), calls))

    await cog._play(gid, channel, dict(IN_PLAY_PAYLOAD))

    row = await _lobby_row(cog.db, gid)
    assert row is not None, "a transient 503 must not archive the game"
    # Its four completed rounds are still there for the resume to stand on.
    assert len(json.loads(row["payload"])["round_history"]) == 4
    assert await _archived(cog.db, gid) is None, "nothing should be archived"
    # And the members are not told the game ended, because it hasn't.
    said = " ".join(str(c.args[0]) for c in channel.send.await_args_list if c.args)
    assert "Game ended" not in said


async def test_a_transient_server_error_re_drives_the_game_once(
    sync_db_path, monkeypatch
):
    """One in-process re-drive: ``_run_game`` re-reads the payload and resumes
    at ``len(round_history) + 1``, so the game just carries on."""
    monkeypatch.setattr(cog_module, "REDRIVE_PAUSE_S", 0)
    bot = _bot(sync_db_path)
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    gid = await _in_play_game(cog)
    calls: list[str] = []
    # Fails once, then the edge comes back.
    monkeypatch.setattr(cog, "_run_game", _raiser(_server_error(), calls, stop_after=1))

    await cog._play(gid, _channel(), dict(IN_PLAY_PAYLOAD))

    assert len(calls) == 2, "the game should have been re-driven exactly once"
    assert await _lobby_row(cog.db, gid) is not None
    assert await _archived(cog.db, gid) is None


async def test_the_re_drive_is_not_an_endless_loop(sync_db_path, monkeypatch):
    """A Discord outage that outlasts the pause must stop at one re-drive and
    leave the game frozen — not re-post phase cards forever."""
    monkeypatch.setattr(cog_module, "REDRIVE_PAUSE_S", 0)
    bot = _bot(sync_db_path)
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    gid = await _in_play_game(cog)
    calls: list[str] = []
    monkeypatch.setattr(cog, "_run_game", _raiser(_server_error(), calls))

    await cog._play(gid, _channel(), dict(IN_PLAY_PAYLOAD))

    assert len(calls) == 2
    assert await _lobby_row(cog.db, gid) is not None


async def test_a_real_bug_still_cancels_the_game(sync_db_path, monkeypatch):
    """Don't make every exception sticky: a genuinely broken game must still
    archive, or it becomes an immortal row nothing can clear."""
    monkeypatch.setattr(cog_module, "REDRIVE_PAUSE_S", 0)
    bot = _bot(sync_db_path)
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    gid = await _in_play_game(cog)
    channel = _channel()
    calls: list[str] = []
    monkeypatch.setattr(cog, "_run_game", _raiser(KeyError("scores"), calls))

    await cog._play(gid, channel, dict(IN_PLAY_PAYLOAD))

    assert len(calls) == 1, "a logic bug must not be retried"
    assert await _lobby_row(cog.db, gid) is None
    assert (await _archived(cog.db, gid))["reason"] == "crash"
    said = " ".join(str(c.args[0]) for c in channel.send.await_args_list if c.args)
    assert "Something went wrong" in said


async def test_a_permission_failure_still_cancels_the_game(
    sync_db_path, monkeypatch
):
    """403 is not a hiccup — the bot cannot post in that channel, and no
    number of restarts will change that."""
    monkeypatch.setattr(cog_module, "REDRIVE_PAUSE_S", 0)
    bot = _bot(sync_db_path)
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    gid = await _in_play_game(cog)
    forbidden = discord.Forbidden(
        SimpleNamespace(status=403, reason="Forbidden"), {"code": 50013, "message": "no"}
    )
    monkeypatch.setattr(cog, "_run_game", _raiser(forbidden, []))

    await cog._play(gid, _channel(), dict(IN_PLAY_PAYLOAD))

    assert await _lobby_row(cog.db, gid) is None


async def test_a_game_ended_during_the_pause_is_not_resumed(sync_db_path, monkeypatch):
    """A mod running `/games end` while the game waits out the wobble deletes
    the row. Re-driving then would have ``_run_game`` read an empty payload
    and replay the whole game from round 1, into a channel that has moved on."""
    monkeypatch.setattr(cog_module, "REDRIVE_PAUSE_S", 0)
    bot = _bot(sync_db_path)
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    gid = await _in_play_game(cog)
    calls: list[str] = []
    error = _server_error()

    async def _run(game_id, channel, payload):
        calls.append(game_id)
        # Stand in for `/games end` landing between the failure and the resume.
        await cog.db.execute(
            "DELETE FROM games_active_games WHERE game_id = ?", (game_id,)
        )
        raise error

    monkeypatch.setattr(cog, "_run_game", _run)

    await cog._play(gid, _channel(), dict(IN_PLAY_PAYLOAD))

    assert len(calls) == 1, "a game that has been ended must not be re-driven"


async def test_the_redrive_counter_is_cleared_with_the_rest_of_the_game(sync_db_path):
    """Otherwise a channel that hiccuped once would never re-drive again —
    _redrives is keyed by game_id, and game ids are not reused, but the dict
    would grow for the life of the process."""
    bot = _bot(sync_db_path)
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    cog._redrives["gid"] = 1
    cog._cleanup("gid")
    assert "gid" not in cog._redrives


def test_every_phase_card_goes_out_through_the_retry():
    """The 503 landed on a phase card's send. A new phase card added without
    the retry would be the same bug again, so no bare ``channel.send`` may
    hold a message the game loop goes on to use."""
    src = open(cog_module.__file__, encoding="utf-8").read()
    for bare in ("msg = await channel.send(", "view.message = await channel.send("):
        assert bare not in src, f"a phase card still sends without retry_transient: {bare}"
    # And the card that actually died is wrapped, by name.
    assert "clapback round {round_num} matchup {matchup_index + 1}" in src
