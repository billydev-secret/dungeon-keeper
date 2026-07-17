"""The result embed's nickname line must name the loser's OLD name.

Regression for "after the rename this message shows <new name> is <new name>":
``render_result_state`` runs *after* ``loser.edit(nick=...)`` has applied, so a
render that reads the loser's live ``display_name`` prints the new nick on both
sides of "is now known as". Every nickname-stake game shared the bug, so the fix
(thread the pre-edit ``original_name`` through) is checked across all six.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

WINNER_ID = 111
LOSER_ID = 222
OLD_NAME = "OldName"
NEW_NICK = "NewNick"


def _guild_after_rename() -> MagicMock:
    """A guild whose loser already shows the NEW nick — i.e. post-edit state.

    This is the crux: if the loser resolved to the OLD name here, a buggy and a
    fixed render would read identically and the test would pass on the bug.
    """
    winner = SimpleNamespace(display_name="Winner", id=WINNER_ID)
    loser = SimpleNamespace(display_name=NEW_NICK, id=LOSER_ID)
    members = {WINNER_ID: winner, LOSER_ID: loser}
    guild = MagicMock()
    guild.get_member = MagicMock(side_effect=lambda uid: members.get(uid))
    return guild


def _quickdraw_game():
    # fired_at=None takes the "false start" path and skips reaction-time math.
    return SimpleNamespace(
        winner_id=WINNER_ID, loser_id=LOSER_ID, stakes_text=None,
        fired_at=None, resolved_at=None, loser_fired_at=None,
    )


def _pressure_game():
    return SimpleNamespace(
        winner_id=WINNER_ID, loser_id=LOSER_ID, stakes_text=None,
        gauge=50, pumps=[], active_player=None,
    )


def _hot_potato_game():
    # started_at=None skips the style-points block.
    return SimpleNamespace(
        winner_id=WINNER_ID, loser_id=LOSER_ID, stakes_text=None,
        pass_log=[], started_at=None, timer_seconds=0,
    )


def _musical_chairs_game():
    return SimpleNamespace(
        winner_id=WINNER_ID, loser_id=LOSER_ID, stakes_text=None, roster=[],
    )


def _hot_potato_group_game():
    return SimpleNamespace(
        winner_id=WINNER_ID, loser_id=LOSER_ID, stakes_text=None,
        roster=[], pass_log=[], resolved_at=1_700_000_000.0,
    )


def _chicken_game():
    return SimpleNamespace(
        winner_id=WINNER_ID, loser_id=LOSER_ID, stakes_text=None,
        alive=[], bail_log=[],
    )


def _load(module: str, cls: str):
    import importlib

    mod = importlib.import_module(f"bot_modules.cogs.{module}.cog")
    klass = getattr(mod, cls)
    # render_result_state only touches self._name (guild lookups), never
    # self.bot/db — so skip __init__ and its heavy wiring.
    return klass.__new__(klass)


GAMES = [
    ("quickdraw", "QuickdrawDuel", _quickdraw_game),
    ("pressure_cooker", "PressureCookerDuel", _pressure_game),
    ("hot_potato", "HotPotatoDuel", _hot_potato_game),
    ("musical_chairs", "MusicalChairsCog", _musical_chairs_game),
    ("hot_potato_group", "HotPotatoGroupGameCog", _hot_potato_group_game),
    ("chicken", "ChickenCog", _chicken_game),
]


def _nick_field(embed) -> str:
    for field in embed.fields:
        if "Nickname Applied" in (field.name or ""):
            return field.value
    raise AssertionError("no 'Nickname Applied' field in the result embed")


@pytest.mark.parametrize("module, cls, make_game", GAMES, ids=[g[0] for g in GAMES])
def test_result_embed_names_old_name_not_new(module, cls, make_game):
    cog = _load(module, cls)
    embed = cog.render_result_state(
        make_game(), _guild_after_rename(),
        imposed_nick=NEW_NICK, original_name=OLD_NAME,
    )
    value = _nick_field(embed)
    assert value == f"**{OLD_NAME}** is now known as **{NEW_NICK}** for 24 hours."
    # The bug's signature: the new nick on both sides.
    assert f"**{NEW_NICK}** is now known as **{NEW_NICK}**" not in value


@pytest.mark.parametrize("module, cls, make_game", GAMES, ids=[g[0] for g in GAMES])
def test_falls_back_to_live_name_without_original(module, cls, make_game):
    """No original_name (e.g. an older resolved game) → the live name, not a crash."""
    cog = _load(module, cls)
    embed = cog.render_result_state(
        make_game(), _guild_after_rename(), imposed_nick=NEW_NICK,
    )
    assert f"**{NEW_NICK}** is now known as **{NEW_NICK}**" in _nick_field(embed)


class _FlippingLoser:
    """A member whose ``display_name`` becomes the new nick once ``edit`` runs —
    exactly the live-Discord behaviour that made the render read the new name
    on both sides. If the base handler captured the name *after* the edit, the
    spy below would see the new nick instead of the old.
    """

    def __init__(self) -> None:
        self.id = LOSER_ID
        self.nick = OLD_NAME
        self._renamed = False

    @property
    def display_name(self) -> str:
        return NEW_NICK if self._renamed else OLD_NAME

    async def edit(self, *, nick, reason=None):
        self._renamed = True


async def test_base_handler_captures_name_before_the_edit(monkeypatch):
    """The pre-edit display name is what reaches render_result_state."""
    from bot_modules.duels import base_game as bg

    cog = bg.BaseGame.__new__(bg.BaseGame)
    cog.GAME_KEY = "test"
    cog.GAME_DISPLAY_NAME = "Test"
    cog.bot = MagicMock()  # self.db reads self.bot.games_db

    loser = _FlippingLoser()
    winner_id = WINNER_ID
    game = SimpleNamespace(
        id=1, state="RESOLVED", winner_id=winner_id, loser_id=LOSER_ID,
        challenger_id=winner_id,
    )

    guild = MagicMock()
    guild.owner_id = 999  # loser is not the owner
    guild.members = []
    guild.get_member = MagicMock(side_effect=lambda uid: loser if uid == LOSER_ID else MagicMock(id=uid))

    interaction = MagicMock()
    interaction.user.id = winner_id
    interaction.guild = guild
    interaction.response.edit_message = _async_noop()
    interaction.followup.send = _async_noop()

    async def _get_config(db, gid, gtype):
        return {"max_nick_length": 32, "nick_denylist": "[]", "sentence_hours": 24}

    monkeypatch.setattr(bg.duels_db, "get_config", _get_config)
    monkeypatch.setattr(bg.duels_db, "apply_nick", _async_noop())
    monkeypatch.setattr(
        bg, "validate_nickname",
        lambda *a, **k: SimpleNamespace(ok=True, value=NEW_NICK, reason=None),
    )

    cog._db_get_game = _async_return(game)
    cog._check_bot_can_nick = _async_return(None)
    cog._check_no_active_nick = _async_return([])
    cog._db_set_state = _async_noop()
    cog._disabled_result_view = MagicMock(return_value=None)

    spy = MagicMock(return_value=MagicMock())
    cog.render_result_state = spy

    await cog._handle_nick_submit_locked(interaction, game.id, NEW_NICK)

    spy.assert_called_once()
    assert spy.call_args.kwargs["original_name"] == OLD_NAME
    # And the loser really was renamed (the flip fired), proving the capture
    # genuinely preceded the edit rather than the flip just never happening.
    assert loser.display_name == NEW_NICK


def _async_noop():
    async def _fn(*a, **k):
        return None
    return _fn


def _async_return(value):
    async def _fn(*a, **k):
        return value
    return _fn
