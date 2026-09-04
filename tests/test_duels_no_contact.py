"""The duel and lobby games consult the no-contact list.

A challenge publicly pings its target and makes them answer in-channel, a
lobby seats whoever presses Join next to whoever is already in, and a win
lets one member rename the other for a day — the strongest contact surfaces
in the bot after DMs. All three go through ``BaseGame`` / ``BaseDuel``, so the
gate lives there and every game inherits it (review finding duels-party-112).

Per docs/no_contact_spec.md the refusal is always an ordinary outcome the
surface already produces, never a new "blocked" line: the challenge gets the
"game in progress" refusal, a lobby join gets the cooldown refusal, and the
Name-the-Loser press gets the "already serving a sentence" refusal. These
tests drive the real entrypoints against a real sqlite ``no_contact_pairs``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest
import pytest_asyncio

from bot_modules.cogs.hot_potato import db as hpdb
from bot_modules.cogs.hot_potato.cog import HotPotatoDuel
from bot_modules.cogs.hot_potato_group import db as hpgdb
from bot_modules.cogs.hot_potato_group.cog import HotPotatoGroupGameCog
from bot_modules.core.db_utils import open_db
from bot_modules.services import no_contact_service as ncs
from bot_modules.services.economy_service import apply_credit, save_econ_settings
from bot_modules.services.games_db import GamesDb
from bot_modules.services.no_contact_logic import KIND_ATTEMPT, SURFACE_DUEL_CHALLENGE
from tests.fakes import FakeEconGamesBot, FakeMember, fake_interaction

GUILD = 9001
CH = 100

IN_PROGRESS = "You two already have a game in progress."
COOLDOWN = "You're on cooldown for this game."


@pytest_asyncio.fixture
async def db(sync_db_path: Path) -> GamesDb:
    return GamesDb(sync_db_path)


@pytest.fixture(autouse=True)
def _stub_accent():
    with patch(
        "bot_modules.core.branding.resolve_accent_color",
        new=AsyncMock(return_value=discord.Color.blurple()),
    ):
        yield


def _pair(db_path: Path, protected: int, other: int) -> None:
    ncs.add_pair(
        db_path, GUILD, protected, other,
        created_by=protected, protected_user_id=protected,
    )


def _seed_economy(db_path: Path, *user_ids: int, amount: int = 500) -> None:
    with open_db(db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
        for uid in user_ids:
            apply_credit(conn, GUILD, uid, amount, "test_seed")


def _interaction(bot, user_id: int):
    bot.guild.me = None  # accent fallback; the nick preflight would crash on it
    i = fake_interaction(user=FakeMember(id=user_id), guild=bot.guild, channel_id=CH)
    i.original_response = AsyncMock(return_value=SimpleNamespace(id=555))
    return i


def _sent(interaction) -> list[str]:
    return [c.args[0] for c in interaction.response.send_message.call_args_list if c.args]


# ── Challenge ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "pair",
    [
        pytest.param(None, id="no-pair"),
        pytest.param((2, 1), id="target-protected"),
        pytest.param((1, 2), id="challenger-protected"),
    ],
)
async def test_blocked_challenge_gets_the_ordinary_in_progress_line(db, sync_db_path, pair):
    """Either direction of the pair refuses with the existing 'game in progress'
    copy — no new wording the challenger could read as 'blocked' — creates no
    row, and records an attempt for staff. Without a pair the challenge posts."""
    _seed_economy(sync_db_path, 1, 2)
    if pair:
        _pair(sync_db_path, *pair)
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    interaction = _interaction(bot, 1)

    await cog._base_challenge(interaction, FakeMember(id=2), None, wager=50)  # type: ignore[arg-type]

    game = await hpdb.get_game(db, 1)
    events = ncs.list_events(sync_db_path, GUILD)
    if pair is None:
        assert game is not None and game.state == "PENDING"
        assert events == []
        return
    assert game is None
    assert _sent(interaction) == [IN_PROGRESS]
    assert [(e["kind"], e["surface"], e["actor_id"], e["target_id"]) for e in events] == [
        (KIND_ATTEMPT, SURFACE_DUEL_CHALLENGE, 1, 2)
    ]


async def test_blocked_challenge_is_not_a_rate_limit_strike(db, sync_db_path):
    """The ordinary 'game in progress' refusal doesn't count against the
    hourly challenge limit, so neither does its look-alike."""
    _seed_economy(sync_db_path, 1, 2)
    _pair(sync_db_path, 2, 1)
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]

    await cog._base_challenge(_interaction(bot, 1), FakeMember(id=2), None, wager=50)  # type: ignore[arg-type]

    assert cog._challenge_rate[1] == cog._challenge_rate.default_factory()  # type: ignore[misc]


# ── Lobby join ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("stakes", "pair", "joined"),
    [
        pytest.param("loser sings", None, True, id="custom-stakes-no-pair"),
        pytest.param("loser sings", (3, 2), False, id="custom-stakes-pair-with-a-player"),
        pytest.param("loser sings", (2, 1), False, id="custom-stakes-pair-with-the-host"),
        pytest.param(None, (3, 2), False, id="nick-mode-pair-with-a-player"),
    ],
)
async def test_blocked_joiner_gets_the_ordinary_cooldown_line(
    db, sync_db_path, stakes, pair, joined
):
    """A joiner who holds a pair with anyone already seated — host or not —
    is refused with the lobby's own cooldown copy, in every stake mode, and
    the roster is untouched. The gate runs before the nickname preflight, so
    a refused joiner never reaches it."""
    if pair:
        _pair(sync_db_path, *pair)
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2, 3])
    cog = HotPotatoGroupGameCog(bot)  # type: ignore[arg-type]
    gid = await hpgdb.create_lobby(db, GUILD, CH, 1, stakes, nick_stake=stakes is None)
    await hpgdb.set_game_state(db, gid, "LOBBY", message_id=555, roster="[1, 3]")
    interaction = fake_interaction(user=FakeMember(id=2), guild=bot.guild)

    await cog._handle_lobby_join(interaction, gid)

    refreshed = await hpgdb.get_game(db, gid)
    assert refreshed is not None
    roster = refreshed.roster
    if joined:
        assert roster == [1, 3, 2]
        interaction.response.edit_message.assert_awaited()
    else:
        assert roster == [1, 3]
        assert _sent(interaction) == [COOLDOWN]
        interaction.response.edit_message.assert_not_awaited()


# ── Name the Loser ────────────────────────────────────────────────────────────


async def _resolved_duel(db, winner: int, loser: int) -> int:
    gid = await hpdb.create_game(db, GUILD, CH, winner, loser, None, nick_stake=True)
    await hpdb.set_game_state(db, gid, "RESOLVED", winner_id=winner, loser_id=loser)
    return gid


@pytest.mark.parametrize("paired", [False, True], ids=["no-pair", "pair"])
async def test_name_the_loser_press_is_refused_across_a_pair(db, sync_db_path, paired):
    """The winner's press never opens the nickname modal when the two hold a
    pair: they get the ordinary 'already serving a sentence' line and the game
    concludes at NO_NICK_SET, exactly as a real overlapping sentence would."""
    if paired:
        _pair(sync_db_path, 2, 1)
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    gid = await _resolved_duel(db, winner=1, loser=2)
    interaction = fake_interaction(user=FakeMember(id=1), guild=bot.guild)

    await cog._handle_set_nick(interaction, gid)

    refreshed = await hpdb.get_game(db, gid)
    assert refreshed is not None
    state = refreshed.state
    if not paired:
        interaction.response.send_modal.assert_awaited_once()
        assert state == "RESOLVED"
        return
    interaction.response.send_modal.assert_not_awaited()
    assert _sent(interaction) == [cog._sentence_in_progress_copy("U2")]
    assert "already serving a nickname sentence" in _sent(interaction)[0]
    assert state == "NO_NICK_SET"


async def test_name_the_loser_submit_is_refused_across_a_pair(db, sync_db_path):
    """The locked submit path is gated too, so a modal already open when the
    pair was added still applies no rename."""
    _pair(sync_db_path, 1, 2)  # winner protected: the direction doesn't matter
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    gid = await _resolved_duel(db, winner=1, loser=2)
    interaction = fake_interaction(user=FakeMember(id=1), guild=bot.guild)
    edit = AsyncMock()
    bot.guild.members[2].edit = edit

    await cog._handle_nick_submit_locked(interaction, gid, "Loser McLoserface")

    edit.assert_not_awaited()
    assert "already serving a nickname sentence" in _sent(interaction)[0]
    refreshed = await hpdb.get_game(db, gid)
    assert refreshed is not None and refreshed.state == "NO_NICK_SET"
