"""Which stakes a game carries, and what that turns on.

A coin wager with no custom stakes text used to fall through to nickname
mode: creation ran the Manage Nicknames preflight and the result post
carried the rename button. It is now settled by an explicit ``nick_stake``
flag (migration 177) rather than inferred from ``stakes_text``, so the three
stakes — coins, custom text, the rename — are independent and any
combination is legal. The persisted stakes text lists every live one. These
tests drive the real creation entrypoints (`_base_challenge` /
`_base_lobby`) and the group resolution seam.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest_asyncio

from bot_modules.cogs.hot_potato import db as hpdb
from bot_modules.cogs.hot_potato.cog import HotPotatoDuel
from bot_modules.cogs.hot_potato_group import db as hpgdb
from bot_modules.cogs.hot_potato_group.cog import HotPotatoGroupGameCog
from bot_modules.core.db_utils import open_db
from bot_modules.duels.filters import WAGER_STAKES_TEXT
from bot_modules.services.economy_service import (
    apply_credit,
    get_balance,
    save_econ_settings,
)
from bot_modules.services.games_db import GamesDb
from tests.fakes import FakeEconGamesBot, FakeMember, fake_interaction

GUILD = 9001
CH = 100


@pytest_asyncio.fixture
async def db(sync_db_path: Path) -> GamesDb:
    return GamesDb(sync_db_path)


def _seed_economy(sync_db_path: Path, *user_ids: int, amount: int = 500) -> None:
    with open_db(sync_db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
        for uid in user_ids:
            apply_credit(conn, GUILD, uid, amount, "test_seed")


def _creation_interaction(bot: FakeEconGamesBot, user_id: int):
    """Interaction shaped for the creation entrypoints: a real channel_id
    (persisted to sqlite) and an awaitable original_response()."""
    bot.guild.me = None  # accent-color fallback; nick preflight would crash on it
    i = fake_interaction(user=FakeMember(id=user_id), guild=bot.guild, channel_id=CH)
    i.original_response = AsyncMock(return_value=SimpleNamespace(id=555))
    return i


# ── Creation: a wager with no custom stakes is recorded as the stake ───────────
# FakeGuild deliberately has no `.me`: if the nickname preflight ran on these
# wager games (the pre-fix behavior), the calls below would crash on guild.me.

async def test_wager_duel_challenge_records_pot_stakes(db, sync_db_path):
    _seed_economy(sync_db_path, 1)
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]

    await cog._base_challenge(_creation_interaction(bot, 1), FakeMember(id=2), None, wager=50)

    game = await hpdb.get_game(db, 1)
    assert game is not None and game.state == "PENDING"
    assert not game.nick_stake
    assert game.stakes_text is not None and "50" in game.stakes_text


async def test_wager_duel_keeps_explicit_custom_stakes(db, sync_db_path):
    _seed_economy(sync_db_path, 1)
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]

    await cog._base_challenge(
        _creation_interaction(bot, 1), FakeMember(id=2), "loser sings a song", wager=50
    )

    game = await hpdb.get_game(db, 1)
    assert game is not None and game.stakes_text is not None
    assert game.stakes_text.startswith("loser sings a song")
    # The coins ride alongside the typed stakes instead of being invisible
    # until settlement ("Oh there were 2 stakes 👀").
    assert "50" in game.stakes_text
    assert not game.nick_stake


async def test_wager_lobby_records_pot_stakes_and_escrows_host(db, sync_db_path):
    _seed_economy(sync_db_path, 1)
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2, 3])
    cog = HotPotatoGroupGameCog(bot)  # type: ignore[arg-type]

    await cog._base_lobby(_creation_interaction(bot, 1), None, wager=25)

    game = await hpgdb.get_game(db, 1)
    assert game is not None and game.state == "LOBBY"
    assert not game.nick_stake
    assert game.stakes_text is not None and "25" in game.stakes_text
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, 1) == 475  # host ante escrowed


# ── Resolution: wager stakes resolve announce-only, nickname mode unchanged ────

async def _resolve_group_game(db, sync_db_path, stakes_text):
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2], with_channel=True)
    cog = HotPotatoGroupGameCog(bot)  # type: ignore[arg-type]
    gid = await hpgdb.create_lobby(db, GUILD, CH, 1, stakes_text)
    await hpgdb.set_game_state(
        db, gid, "ACTIVE",
        roster=json.dumps([1, 2]), alive=json.dumps([1, 2]),
        elimination_order=json.dumps([]),
    )
    game = await hpgdb.get_game(db, gid)
    await cog._group_eliminate(game, 1, interaction=None)
    assert bot.channel is not None
    return await hpgdb.get_game(db, gid), bot.channel.sent[-1]


async def test_wager_stakes_game_resolves_without_rename_button(db, sync_db_path):
    game, sent = await _resolve_group_game(db, sync_db_path, WAGER_STAKES_TEXT)
    assert game.state == "RESOLVED_NO_NICK"
    assert "view" not in sent


async def test_nickname_game_still_gets_rename_button(db, sync_db_path):
    game, sent = await _resolve_group_game(db, sync_db_path, None)
    assert game.state == "RESOLVED"
    assert sent.get("view") is not None


# ── Duel timer path — Hot Potato's hand-rolled _explode bypasses
# _finalize_result, so its stake-mode gate needs its own pin.

async def _explode_duel(db, sync_db_path, stakes_text):
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2], with_channel=True)
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    gid = await hpdb.create_game(db, GUILD, CH, 1, 2, stakes_text)
    now = time.time()
    await hpdb.set_game_state(
        db, gid, "ACTIVE",
        holder_id=2, started_at=now - 10.0, timer_seconds=10.0,
        pass_log=json.dumps(
            [{"holder_id": 2, "received_at": now - 3.0, "passed_at": None}]
        ),
        last_action_at=now,
    )
    await cog._explode(gid)
    assert bot.channel is not None
    return await hpdb.get_game(db, gid), bot.channel.sent[-1]


async def test_explode_custom_stakes_resolves_without_rename_button(db, sync_db_path):
    game, sent = await _explode_duel(db, sync_db_path, "loser sings a song")
    assert game.state == "RESOLVED_NO_NICK"
    assert "view" not in sent


async def test_explode_wager_stakes_resolves_without_rename_button(db, sync_db_path):
    game, sent = await _explode_duel(db, sync_db_path, WAGER_STAKES_TEXT)
    assert game.state == "RESOLVED_NO_NICK"
    assert "view" not in sent


async def test_explode_nickname_mode_still_gets_rename_button(db, sync_db_path):
    game, sent = await _explode_duel(db, sync_db_path, None)
    assert game.state == "RESOLVED"
    assert sent.get("view") is not None


# ── nickname alongside everything else (game night 2026-08-21) ───────────────


async def test_wager_duel_can_also_stake_nicknames(db, sync_db_path):
    """The reason this flag exists: a Pressure Cooker game staked as "24 hour
    nickname change" plus 500 coins offered nobody a rename button, because
    naming any other stake cancelled the rename."""
    _seed_economy(sync_db_path, 1)
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    interaction = _creation_interaction(bot, 1)
    # A nickname stake runs the Manage Nicknames preflight, so the bot member
    # has to exist here (the wager-only tests rely on it being absent).
    bot.guild.me = SimpleNamespace(
        guild_permissions=SimpleNamespace(manage_nicknames=True),
        top_role=99,
    )
    bot.guild.owner_id = 0

    await cog._base_challenge(
        interaction, FakeMember(id=2),
        "24 hour nickname change", wager=50, nickname=True,
    )

    game = await hpdb.get_game(db, 1)
    assert game is not None and game.nick_stake
    assert game.stakes_text is not None
    assert "24 hour nickname change" in game.stakes_text
    assert "50" in game.stakes_text
    assert "nickname" in game.stakes_text.lower()


async def test_nickname_can_be_turned_off_on_a_plain_duel(db, sync_db_path):
    _seed_economy(sync_db_path, 1)
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]

    await cog._base_challenge(
        _creation_interaction(bot, 1), FakeMember(id=2), "loser sings", nickname=False
    )

    game = await hpdb.get_game(db, 1)
    assert game is not None and not game.nick_stake


async def test_nickname_off_with_nothing_else_staked_is_refused(db, sync_db_path):
    """A duel with no stake at all isn't a duel — refuse rather than persist a
    row every downstream reader would have to guess about."""
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    interaction = _creation_interaction(bot, 1)

    await cog._base_challenge(interaction, FakeMember(id=2), None, nickname=False)

    assert await hpdb.get_game(db, 1) is None
    (text,) = interaction.response.send_message.await_args.args
    assert "stake something else" in text


async def test_wagered_lobby_can_also_stake_nicknames(db, sync_db_path):
    _seed_economy(sync_db_path, 1)
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2, 3])
    bot.guild.me = None
    cog = HotPotatoGroupGameCog(bot)  # type: ignore[arg-type]

    await cog._base_lobby(
        _creation_interaction(bot, 1), None, wager=25, nickname=False
    )
    game = await hpgdb.get_game(db, 1)
    assert game is not None and not game.nick_stake


async def test_flagged_game_resolves_with_the_rename_button(db, sync_db_path):
    """Custom stakes text no longer cancels the rename — the flag decides."""
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2], with_channel=True)
    cog = HotPotatoGroupGameCog(bot)  # type: ignore[arg-type]
    gid = await hpgdb.create_lobby(db, GUILD, CH, 1, "loser sings", True)
    await hpgdb.set_game_state(
        db, gid, "ACTIVE",
        roster=json.dumps([1, 2]), alive=json.dumps([1, 2]),
        elimination_order=json.dumps([]),
    )
    game = await hpgdb.get_game(db, gid)
    await cog._group_eliminate(game, 1, interaction=None)
    game = await hpgdb.get_game(db, gid)
    assert game.state == "RESOLVED"
    assert bot.channel is not None
    assert bot.channel.sent[-1].get("view") is not None


async def test_custom_stakes_without_the_flag_stays_announce_only(db, sync_db_path):
    game, sent = await _resolve_group_game(db, sync_db_path, "loser sings")
    assert game.state == "RESOLVED_NO_NICK"
    assert "view" not in sent


# ── challenge cap is a dashboard dial (game night 2026-08-21) ────────────────


async def test_challenge_cap_reads_the_guild_dial(db, sync_db_path):
    """The cap was a hardcoded 3/hour, which the room's most engaged player hit
    twice in one evening. It comes from duel_config now."""
    from bot_modules.duels import db as duels_db

    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    await duels_db.upsert_config(db, GUILD, "hot_potato", challenge_limit_per_hour=2)
    cfg = await duels_db.get_config(db, GUILD, "hot_potato")

    assert cog._challenge_limit(cfg) == 2
    assert not cog._check_rate_limit(1, 2)
    cog._record_challenge(1)
    cog._record_challenge(1)
    assert cog._check_rate_limit(1, 2)


async def test_challenge_cap_of_zero_means_unlimited(db, sync_db_path):
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    for _ in range(50):
        cog._record_challenge(1)
    assert not cog._check_rate_limit(1, 0)


async def test_challenge_cap_default_is_not_three(db, sync_db_path):
    """A guild with no row saved should not silently inherit the old brake."""
    from bot_modules.duels import db as duels_db

    cfg = await duels_db.get_config(db, GUILD, "hot_potato")
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    assert cog._challenge_limit(cfg) >= 10


async def test_challenge_cap_survives_a_junk_config_value(db, sync_db_path):
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    assert cog._challenge_limit({"challenge_limit_per_hour": "nonsense"}) > 0


async def test_over_the_cap_is_refused_with_the_real_number(db, sync_db_path):
    from bot_modules.duels import db as duels_db

    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    await duels_db.upsert_config(db, GUILD, "hot_potato", challenge_limit_per_hour=1)
    cog._record_challenge(1)
    interaction = _creation_interaction(bot, 1)

    await cog._base_challenge(interaction, FakeMember(id=2), "loser sings")

    assert await hpdb.get_game(db, 1) is None
    (text,) = interaction.response.send_message.await_args.args
    assert "Maximum 1 per hour" in text


# ── stale-accept copy says what happened (game night 2026-08-21) ────────────


async def test_accepting_an_expired_challenge_says_it_timed_out(db, sync_db_path):
    """"LMAO that did not let me accept" — a click a second past the 60-second
    window got "This challenge is no longer active", which reads like a bug. The
    window is five minutes now; the copy still has to say what happened."""
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    gid = await hpdb.create_game(db, GUILD, CH, 1, 2, None, True)
    await hpdb.set_game_state(db, gid, "EXPIRED_PENDING")
    interaction = fake_interaction(user=FakeMember(id=2), guild=bot.guild, channel_id=CH)

    await cog._handle_accept(interaction, gid)

    (text,) = interaction.response.send_message.await_args.args
    assert "timed out" in text and "5 minutes" in text


async def test_accepting_an_already_declined_challenge_says_so(db, sync_db_path):
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    gid = await hpdb.create_game(db, GUILD, CH, 1, 2, None, True)
    await hpdb.set_game_state(db, gid, "DECLINED")
    interaction = fake_interaction(user=FakeMember(id=2), guild=bot.guild, channel_id=CH)

    await cog._handle_accept(interaction, gid)

    (text,) = interaction.response.send_message.await_args.args
    assert "declined" in text


async def test_accepting_a_running_game_says_it_already_started(db, sync_db_path):
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    gid = await hpdb.create_game(db, GUILD, CH, 1, 2, None, True)
    await hpdb.set_game_state(db, gid, "ACTIVE")
    interaction = fake_interaction(user=FakeMember(id=2), guild=bot.guild, channel_id=CH)

    await cog._handle_accept(interaction, gid)

    (text,) = interaction.response.send_message.await_args.args
    assert "already been accepted" in text


async def test_accepting_a_vanished_challenge_does_not_crash(db, sync_db_path):
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    interaction = fake_interaction(user=FakeMember(id=2), guild=bot.guild, channel_id=CH)

    await cog._handle_accept(interaction, 999)

    (text,) = interaction.response.send_message.await_args.args
    assert "gone" in text


# ── nickname:False must not be undone by stakes text that cleans away ───────


async def test_blank_stakes_with_nickname_off_is_refused(db, sync_db_path):
    """Whitespace-only stakes clean to None. Reading the raw string when
    deciding nickname mode answered "something else is staked", so the guard
    was skipped, the Manage Nicknames / active-sentence preflights were
    skipped — and the game then fell through to nickname mode at settlement
    anyway, renaming someone who had explicitly opted out."""
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    interaction = _creation_interaction(bot, 1)

    await cog._base_challenge(interaction, FakeMember(id=2), "   ", nickname=False)

    assert await hpdb.get_game(db, 1) is None
    (text,) = interaction.response.send_message.await_args.args
    assert "stake something else" in text


async def test_blank_stakes_without_a_flag_is_a_plain_nickname_game(db, sync_db_path):
    """The same normalisation the other way: blank stakes and no wager is just
    a plain duel, and it must still run the nickname preflight."""
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]

    interaction = _creation_interaction(bot, 1)  # clears guild.me
    bot.guild.me = SimpleNamespace(
        guild_permissions=SimpleNamespace(manage_nicknames=True), top_role=99
    )
    bot.guild.owner_id = 0

    await cog._base_challenge(interaction, FakeMember(id=2), "   ")

    game = await hpdb.get_game(db, 1)
    assert game is not None
    assert game.nick_stake
    assert game.stakes_text is None


async def test_manage_nicknames_gate_still_fires_for_blank_stakes(db, sync_db_path):
    """The preflight has to see the normalised value: without Manage
    Nicknames this game cannot run, and blank stakes must not smuggle it past."""
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    interaction = _creation_interaction(bot, 1)  # clears guild.me
    bot.guild.me = SimpleNamespace(
        guild_permissions=SimpleNamespace(manage_nicknames=False), top_role=99
    )
    bot.guild.owner_id = 0

    await cog._base_challenge(interaction, FakeMember(id=2), "   ")

    assert await hpdb.get_game(db, 1) is None
    (text,) = interaction.response.send_message.await_args.args
    assert "Manage Nicknames" in text
