"""The winner has thirty minutes to name the loser, is reminded once, and a
game that ends with no rename says why (duels-party-118).

``NO_NICK_SET`` covered four different endings — the winner never pressed the
button, the loser outranks the bot, the loser left the server, the loser was
already serving — and the five-minute window had no reminder. The window is
``NAMING_WINDOW_SECONDS`` now (or until the next game between the pair), one
in-channel ping lands at ``NAMING_REMINDER_SECONDS``, and every path through
``_conclude_unnamed`` writes ``nick_reason``.
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest
import pytest_asyncio

from bot_modules.cogs.chicken import db as chdb
from bot_modules.cogs.hot_potato import db as hpdb
from bot_modules.cogs.hot_potato.cog import HotPotatoDuel
from bot_modules.cogs.hot_potato_group import db as hpgdb
from bot_modules.cogs.musical_chairs import db as mcdb
from bot_modules.cogs.pressure_cooker import db as pdb
from bot_modules.cogs.quickdraw import db as qdb
from bot_modules.core.db_utils import open_db
from bot_modules.duels import db as duels_db
from bot_modules.duels.db import NAMING_REMINDER_SECONDS, NAMING_WINDOW_SECONDS
from bot_modules.services import no_contact_service as ncs
from bot_modules.services.economy_service import apply_credit, save_econ_settings
from bot_modules.services.games_db import GamesDb
from tests.fakes import FakeEconGamesBot, FakeMember, FakeMessageableChannel, fake_interaction

GUILD = 9001
CH = 100


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


class _Members(dict):
    """``FakeGuild.members`` is keyed by id for ``get_member``; the rename
    path also iterates ``guild.members`` the way discord.py's list allows."""

    def __iter__(self):
        return iter(self.values())


def _real_members(bot, *user_ids: int) -> None:
    bot.guild.members = _Members(
        {uid: FakeMember(id=uid, display_name=f"U{uid}") for uid in user_ids}
    )
    bot.guild.me = SimpleNamespace(
        guild_permissions=SimpleNamespace(manage_nicknames=True), top_role=99,
    )
    bot.guild.owner_id = 0


async def _resolved(db, *, age: float, winner: int = 1, loser: int = 2) -> int:
    gid = await hpdb.create_game(db, GUILD, CH, winner, loser, None, nick_stake=True)
    await hpdb.set_game_state(
        db, gid, "RESOLVED", winner_id=winner, loser_id=loser,
        resolved_at=time.time() - age, result_message_id=700,
    )
    return gid


# ── the sweep window ──────────────────────────────────────────────────────────

_DUELS = [
    pytest.param(pdb, "duel", id="pressure"),
    pytest.param(qdb, "duel", id="quickdraw"),
    pytest.param(hpdb, "duel", id="hot-potato"),
    pytest.param(hpgdb, "group", id="hot-potato-group"),
    pytest.param(chdb, "group", id="chicken"),
    pytest.param(mcdb, "group", id="musical-chairs"),
]


@pytest.mark.parametrize(("mod", "shape"), _DUELS)
@pytest.mark.parametrize(
    ("age", "swept"),
    [
        pytest.param(NAMING_WINDOW_SECONDS - 60, False, id="inside-the-window"),
        pytest.param(NAMING_WINDOW_SECONDS + 60, True, id="past-the-window"),
        pytest.param(320, False, id="the-old-5-minute-window-is-gone"),
    ],
)
async def test_naming_sweep_uses_the_shared_window(db, mod, shape, age, swept):
    if shape == "duel":
        gid = await mod.create_game(db, GUILD, CH, 1, 2, None)
    else:
        gid = await mod.create_lobby(db, GUILD, CH, 1, None)
    await mod.set_game_state(
        db, gid, "RESOLVED", winner_id=1, loser_id=2, resolved_at=time.time() - age,
    )
    ids = {g.id for g in await mod.fetch_sweepable_games(db, time.time())}
    assert (gid in ids) is swept


def test_the_window_is_thirty_minutes_with_a_two_minute_reminder():
    assert NAMING_WINDOW_SECONDS == 1800
    assert NAMING_REMINDER_SECONDS == 120


# ── the reason column ─────────────────────────────────────────────────────────


async def test_the_timeout_writes_its_reason(db, sync_db_path):
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    gid = await _resolved(db, age=NAMING_WINDOW_SECONDS + 1)

    await cog._expire_resolved(await hpdb.get_game(db, gid))

    assert (await hpdb.get_game(db, gid)).state == "NO_NICK_SET"
    assert await duels_db.get_nick_reason(db, "hot_potato", gid) == "winner_timeout"


async def test_a_loser_who_left_writes_loser_left(db, sync_db_path):
    bot = FakeEconGamesBot(db, sync_db_path, [1])
    _real_members(bot, 1)  # the loser (2) is gone
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    gid = await _resolved(db, age=10)
    interaction = fake_interaction(user=FakeMember(id=1), guild=bot.guild)

    await cog._handle_nick_submit_locked(interaction, gid, "Loser McLoserface")

    assert (await hpdb.get_game(db, gid)).state == "NO_NICK_SET"
    assert await duels_db.get_nick_reason(db, "hot_potato", gid) == "loser_left"
    (text,) = [c.args[0] for c in interaction.response.send_message.call_args_list]
    assert text.startswith("❌ ") and "left the server" in text


async def test_a_no_contact_pair_writes_already_serving(db, sync_db_path):
    """The no-contact gate borrows the sentence-in-progress refusal, and the
    reason it records is the same one — the pair's existence never leaks
    into the game row."""
    ncs.add_pair(sync_db_path, GUILD, 2, 1, created_by=2, protected_user_id=2)
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    gid = await _resolved(db, age=10)

    await cog._handle_set_nick(fake_interaction(user=FakeMember(id=1), guild=bot.guild), gid)

    assert await duels_db.get_nick_reason(db, "hot_potato", gid) == "already_serving"


async def test_an_actual_sentence_in_progress_writes_already_serving(db, sync_db_path):
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    gid = await _resolved(db, age=10)
    await duels_db.apply_nick(
        db, game_id=99, game_type="quickdraw", guild_id=GUILD, loser_id=2, winner_id=3,
        original_nick=None, imposed_nick="Already", sentence_hours=24,
    )
    _real_members(bot, 1, 2)
    interaction = fake_interaction(user=FakeMember(id=1), guild=bot.guild)

    await cog._handle_nick_submit_locked(interaction, gid, "Second Sentence")

    assert (await hpdb.get_game(db, gid)).state == "NO_NICK_SET"
    assert await duels_db.get_nick_reason(db, "hot_potato", gid) == "already_serving"


def test_every_reason_is_a_known_one():
    assert duels_db.NICK_REASONS == {
        "winner_timeout", "loser_outranks", "loser_left", "winner_left",
        "already_serving", "superseded",
    }


# ── the next game between the pair ends the window ────────────────────────────


async def test_the_winner_starting_a_new_game_supersedes_the_unnamed_result(db, sync_db_path):
    with open_db(sync_db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
        apply_credit(conn, GUILD, 1, 500, "test_seed")
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    old = await _resolved(db, age=60)  # 1 won, 2 lost
    interaction = fake_interaction(user=FakeMember(id=1), guild=bot.guild, channel_id=CH)
    interaction.original_response = AsyncMock(return_value=SimpleNamespace(id=555))

    await cog._base_challenge(interaction, FakeMember(id=2), None, wager=50)  # type: ignore[arg-type]

    assert (await hpdb.get_game(db, old)).state == "NO_NICK_SET"
    assert await duels_db.get_nick_reason(db, "hot_potato", old) == "superseded"
    new = await hpdb.get_game(db, old + 1)
    assert new is not None and new.state == "PENDING"


async def test_the_loser_cannot_end_the_winners_window(db, sync_db_path):
    """A lost nickname duel followed by a quick challenge (or Run It Back)
    from the loser must not wipe the rename the winner is about to apply."""
    with open_db(sync_db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
        apply_credit(conn, GUILD, 2, 500, "test_seed")
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    old = await _resolved(db, age=60)  # 1 won, 2 lost
    interaction = fake_interaction(user=FakeMember(id=2), guild=bot.guild, channel_id=CH)
    interaction.original_response = AsyncMock(return_value=SimpleNamespace(id=555))

    await cog._base_challenge(interaction, FakeMember(id=1), None, wager=50)  # type: ignore[arg-type]

    assert (await hpdb.get_game(db, old)).state == "RESOLVED"
    assert await duels_db.get_nick_reason(db, "hot_potato", old) is None
    assert await hpdb.get_game(db, old + 1) is None
    (text,) = [c.args[0] for c in interaction.response.send_message.call_args_list]
    assert text.startswith("❌ ") and "hasn't named you yet" in text


async def test_a_refused_challenge_leaves_the_window_open(db, sync_db_path):
    """The window closes when the new game is actually made, not on a
    challenge that bounced off a later gate (here: the wager precheck)."""
    with open_db(sync_db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    old = await _resolved(db, age=60)
    interaction = fake_interaction(user=FakeMember(id=1), guild=bot.guild, channel_id=CH)

    await cog._base_challenge(interaction, FakeMember(id=2), None, wager=50)  # type: ignore[arg-type]

    assert (await hpdb.get_game(db, old)).state == "RESOLVED"
    assert await hpdb.get_game(db, old + 1) is None


# ── one reminder to the winner ────────────────────────────────────────────────


async def test_the_winner_is_reminded_once(db, sync_db_path):
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    bot.channel = FakeMessageableChannel(CH)  # type: ignore[assignment]
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    gid = await _resolved(db, age=NAMING_REMINDER_SECONDS + 5)
    resolved_at = (await hpdb.get_game(db, gid)).resolved_at

    await cog._remind_unnamed(time.time())
    await cog._remind_unnamed(time.time() + 60)

    texts = bot.channel.texts
    assert len(texts) == 1
    assert "<@1>" in texts[0] and "**U2**" in texts[0] and "Name the Loser" in texts[0]
    assert f"<t:{int(resolved_at + NAMING_WINDOW_SECONDS)}:R>" in texts[0]


async def test_a_fresh_result_is_not_reminded_yet(db, sync_db_path):
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    bot.channel = FakeMessageableChannel(CH)  # type: ignore[assignment]
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    await _resolved(db, age=NAMING_REMINDER_SECONDS - 30)
    await cog._remind_unnamed(time.time())
    assert bot.channel.texts == []


async def test_a_renamed_game_is_never_reminded(db, sync_db_path):
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    bot.channel = FakeMessageableChannel(CH)  # type: ignore[assignment]
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    gid = await _resolved(db, age=NAMING_REMINDER_SECONDS + 5)
    await hpdb.set_game_state(db, gid, "NICKED")
    await cog._remind_unnamed(time.time())
    assert bot.channel.texts == []
