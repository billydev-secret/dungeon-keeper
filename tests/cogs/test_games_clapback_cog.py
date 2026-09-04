"""Clapback's recap relaunch goes through the slash entry's gate.

The guard's branches are pinned in ``test_games_price_cog.py``; this proves
both of Clapback's Play Again buttons are wired through it. They call
``_start_new_game`` rather than ``launch`` (the recap carries a fully built
config), so the gate has to sit on the button itself.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot_modules.cogs.games_clapback_cog as cog_module
from bot_modules.cogs.games_clapback_cog import ClapbackCog, ClapbackRecapView
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
    config = {"rounds": 3, "timer": 60, "vote_timer": 30, "anonymous": False}
    view = ClapbackRecapView("old-gid", HOST, config, cog.db, bot, cog)
    interaction = _interaction()

    await getattr(view, button).callback(interaction)  # type: ignore[arg-type]

    if enabled:
        start.assert_awaited_once()
        interaction.response.send_message.assert_not_awaited()
    else:
        start.assert_not_awaited()
        kwargs = interaction.response.send_message.await_args.kwargs
        assert kwargs["ephemeral"] is True
        assert "disabled" in interaction.response.send_message.await_args.args[0]
        # The recap card is left alone so the host can retry once it is back on.
        interaction.response.edit_message.assert_not_awaited()


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

    async def vote_matchup(game_id, channel, payload, mi, matchup, answers, *rest):
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
