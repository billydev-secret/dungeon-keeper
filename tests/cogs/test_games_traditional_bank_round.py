"""Drives the Traditional Truth-or-Dare "Bank Round" button through fakes.

The pure pieces (category selection, bank getter) are unit-tested in
test_games_traditional_logic.py; this exercises the interactive glue that
nothing else covers: pressing Bank Round serves every opted-in player one
bank question (no repeats), records each serve in the shared ``asked``
history plus the served set + counter on the payload, posts one pinged
card per player, reports players it couldn't serve, and on a re-press
only serves players (e.g. late joiners) not already asked.
"""

import json

import discord

from bot_modules.cogs.games_traditional_cog import TraditionalCog
from bot_modules.core.db_utils import open_db
from bot_modules.games.utils.game_manager import get_game_payload, update_game_payload
from bot_modules.services.games_db import GamesDb


# ── Fakes ────────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self):
        self.deferred = False
        self.messages: list[tuple] = []

    async def defer(self, *args, **kwargs):
        self.deferred = True

    async def send_message(self, content=None, **kwargs):
        self.messages.append((content, kwargs))


class FakeFollowup:
    def __init__(self):
        self.messages: list[tuple] = []

    async def send(self, content=None, **kwargs):
        self.messages.append((content, kwargs))


class FakeMessage:
    def __init__(self, mid: int, channel, content=None, embed=None):
        self.id = mid
        self.channel = channel
        self.content = content
        self.embed = embed
        self.edits: list[dict] = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


# Subclass the real Messageable so the cog's `isinstance` narrowing holds.
class FakeChannel(discord.abc.Messageable):
    def __init__(self, guild, nsfw: bool = False):
        self.id = 4242
        self.name = "games"
        self.guild = guild
        self.sends: list[FakeMessage] = []
        self._next_id = 9000
        self._nsfw = nsfw

    def is_nsfw(self) -> bool:
        return self._nsfw

    async def _get_channel(self):
        return self

    async def send(self, content=None, **kwargs):
        self._next_id += 1
        msg = FakeMessage(self._next_id, self, content=content, embed=kwargs.get("embed"))
        self.sends.append(msg)
        return msg


class FakeMember:
    def __init__(self, uid: int, name: str):
        self.id = uid
        self.display_name = name
        self.mention = f"<@{uid}>"


class FakeGuild:
    def __init__(self, members):
        self.id = 77
        self.icon = None
        self.me = None  # branding falls back to the default accent color
        self._members = {m.id: m for m in members}

    def get_member(self, uid):
        return self._members.get(uid)


class FakeCtx:
    def __init__(self, db_path):
        self.db_path = db_path


class FakeBot:
    def __init__(self, db: GamesDb, db_path):
        self.games_db = db
        self.active_views: dict = {}
        self.game_launchers: dict = {}
        self.game_recoverers: dict = {}
        self.added_views: list[tuple] = []
        self.ctx = FakeCtx(db_path)

    def add_view(self, view, *, message_id=None):
        self.added_views.append((view, message_id))


class FakeInteraction:
    def __init__(self, user, channel, guild, client):
        self.user = user
        self.channel = channel
        self.guild = guild
        self.client = client
        self.response = FakeResponse()
        self.followup = FakeFollowup()


def _seed_bank(db_path, rows):
    """rows: list of (question_text, category)."""
    with open_db(db_path) as conn:
        conn.executemany(
            "INSERT INTO games_question_bank (game_type, tags, question_text)"
            " VALUES ('traditional', ?, ?)",
            [(json.dumps([cat]), text) for text, cat in rows],
        )


async def _launch_with_prefs(cog, bot, channel, guild, host, prefs):
    game_id = await cog.launch(
        channel=channel, host_id=host.id, host_name=host.display_name,
        guild_id=guild.id, options={},
    )
    assert game_id is not None
    payload = await get_game_payload(cog.db, game_id)
    payload["participants"] = [int(uid) for uid in prefs]
    payload["prefs"] = prefs
    await update_game_payload(cog.db, game_id, payload)
    return game_id


# ── Tests ────────────────────────────────────────────────────────────


async def test_bank_round_serves_every_player_without_repeats(sync_db_path):
    db = GamesDb(sync_db_path)
    bot = FakeBot(db, str(sync_db_path))
    cog = TraditionalCog(bot)  # type: ignore[arg-type]

    host = FakeMember(1, "Host")
    bee = FakeMember(2, "Bee")
    guild = FakeGuild([host, bee])
    channel = FakeChannel(guild)

    _seed_bank(sync_db_path, [("Truth one", "sfw_truth"), ("Truth two", "sfw_truth")])
    game_id = await _launch_with_prefs(
        cog, bot, channel, guild, host,
        prefs={"1": ["sfw_truth"], "2": ["sfw_truth"]},
    )
    view = bot.active_views[game_id]

    inter = FakeInteraction(host, channel, guild, bot)
    await view.bank_round.callback(inter)

    assert inter.response.deferred
    # Launch posted 1 lobby message; the bank round adds one card per player.
    cards = channel.sends[1:]
    assert len(cards) == 2, "bank round did not post one card per player"
    # Each card pings a distinct player and carries a distinct question.
    mentions = {c.content for c in cards}
    assert mentions == {"<@1>", "<@2>"}
    texts = {c.embed.description for c in cards}
    assert len(texts) == 2, "the same bank question was served twice"

    payload = await get_game_payload(db, game_id)
    assert payload["bank_asked"] == 2
    assert sorted(payload["bank_used"]) == ["Truth one", "Truth two"]
    # Bank questions land in the same asked history as written ones.
    assert set(payload["asked"]) == {"1:sfw_truth", "2:sfw_truth"}

    assert inter.followup.messages, "no host summary was sent"
    assert "Served **2**" in inter.followup.messages[-1][0]


async def test_bank_round_rerun_serves_only_new_players(sync_db_path):
    db = GamesDb(sync_db_path)
    bot = FakeBot(db, str(sync_db_path))
    cog = TraditionalCog(bot)  # type: ignore[arg-type]

    host = FakeMember(1, "Host")
    bee = FakeMember(2, "Bee")
    newbie = FakeMember(3, "Newbie")
    guild = FakeGuild([host, bee, newbie])
    channel = FakeChannel(guild)

    _seed_bank(sync_db_path, [
        ("Truth one", "sfw_truth"),
        ("Truth two", "sfw_truth"),
        ("Truth three", "sfw_truth"),
    ])
    game_id = await _launch_with_prefs(
        cog, bot, channel, guild, host,
        prefs={"1": ["sfw_truth"], "2": ["sfw_truth"]},
    )
    view = bot.active_views[game_id]

    await view.bank_round.callback(FakeInteraction(host, channel, guild, bot))
    assert len(channel.sends) == 3  # lobby + one card each for Host and Bee

    # A new player joins after the first round.
    payload = await get_game_payload(db, game_id)
    payload["participants"].append(3)
    payload["prefs"]["3"] = ["sfw_truth"]
    await update_game_payload(db, game_id, payload)

    inter2 = FakeInteraction(host, channel, guild, bot)
    await view.bank_round.callback(inter2)

    # Only the newcomer got a card; the first group wasn't double-asked.
    cards = channel.sends[3:]
    assert [c.content for c in cards] == ["<@3>"]
    payload = await get_game_payload(db, game_id)
    assert payload["bank_asked"] == 3
    assert set(payload["asked"]) == {"1:sfw_truth", "2:sfw_truth", "3:sfw_truth"}
    msg = inter2.followup.messages[-1][0]
    assert "Served **1**" in msg
    assert "Skipped 2 players" in msg


async def test_bank_round_reports_everyone_already_asked(sync_db_path):
    db = GamesDb(sync_db_path)
    bot = FakeBot(db, str(sync_db_path))
    cog = TraditionalCog(bot)  # type: ignore[arg-type]

    host = FakeMember(1, "Host")
    guild = FakeGuild([host])
    channel = FakeChannel(guild)

    _seed_bank(sync_db_path, [("Truth one", "sfw_truth"), ("Truth two", "sfw_truth")])
    game_id = await _launch_with_prefs(
        cog, bot, channel, guild, host, prefs={"1": ["sfw_truth"]},
    )
    view = bot.active_views[game_id]

    await view.bank_round.callback(FakeInteraction(host, channel, guild, bot))
    inter2 = FakeInteraction(host, channel, guild, bot)
    await view.bank_round.callback(inter2)

    # No second card; the host is told everyone is already covered.
    assert len(channel.sends) == 2  # lobby + the single first-round card
    payload = await get_game_payload(db, game_id)
    assert payload["bank_asked"] == 1
    msg = inter2.followup.messages[-1][0]
    assert "already been asked" in msg


async def test_bank_round_reports_players_with_no_matching_question(sync_db_path):
    db = GamesDb(sync_db_path)
    bot = FakeBot(db, str(sync_db_path))
    cog = TraditionalCog(bot)  # type: ignore[arg-type]

    host = FakeMember(1, "Host")
    guild = FakeGuild([host])
    # Age-restricted, so the NSFW preference is legitimately selectable here
    # and the round fails for the reason under test: an empty bank.
    channel = FakeChannel(guild, nsfw=True)

    # A player who wants nsfw_dare, but the bank only has sfw_truth.
    _seed_bank(sync_db_path, [("A clean truth", "sfw_truth")])
    game_id = await _launch_with_prefs(
        cog, bot, channel, guild, host, prefs={"1": ["nsfw_dare"]},
    )
    view = bot.active_views[game_id]

    inter = FakeInteraction(host, channel, guild, bot)
    await view.bank_round.callback(inter)

    # No card posted (only the launch message remains).
    assert len(channel.sends) == 1
    payload = await get_game_payload(db, game_id)
    assert payload.get("bank_asked", 0) == 0
    msg = inter.followup.messages[-1][0]
    assert "No bank questions were available" in msg
    assert "dashboard" in msg


async def test_bank_round_rejects_non_host(sync_db_path):
    db = GamesDb(sync_db_path)
    bot = FakeBot(db, str(sync_db_path))
    cog = TraditionalCog(bot)  # type: ignore[arg-type]

    host = FakeMember(1, "Host")
    rando = FakeMember(2, "Rando")
    guild = FakeGuild([host, rando])
    channel = FakeChannel(guild)

    _seed_bank(sync_db_path, [("Truth one", "sfw_truth")])
    game_id = await _launch_with_prefs(
        cog, bot, channel, guild, host, prefs={"1": ["sfw_truth"]},
    )
    view = bot.active_views[game_id]

    inter = FakeInteraction(rando, channel, guild, bot)
    await view.bank_round.callback(inter)

    # No card posted; an ephemeral "host or mod only" notice was sent instead.
    assert len(channel.sends) == 1
    assert not inter.response.deferred
    assert inter.response.messages
    assert inter.response.messages[-1][1].get("ephemeral") is True


async def test_bank_round_never_serves_nsfw_in_a_sfw_channel(sync_db_path):
    """The age gate holds even if prefs were set while the channel was NSFW.

    A channel can lose its age-restriction after players opt in, so the round
    filters preferences at serve time rather than trusting the stored payload.
    """
    db = GamesDb(sync_db_path)
    bot = FakeBot(db, str(sync_db_path))
    cog = TraditionalCog(bot)  # type: ignore[arg-type]
    host = FakeMember(1, "Host")
    guild = FakeGuild([host])
    channel = FakeChannel(guild, nsfw=False)

    # The bank has an NSFW question and the player is opted into it.
    _seed_bank(sync_db_path, [("A spicy dare", "nsfw_dare")])
    game_id = await _launch_with_prefs(
        cog, bot, channel, guild, host, prefs={"1": ["nsfw_dare"]},
    )
    view = bot.active_views[game_id]

    inter = FakeInteraction(host, channel, guild, bot)
    await view.bank_round.callback(inter)

    # Nothing served: only the launch message, and no question recorded.
    assert len(channel.sends) == 1
    payload = await get_game_payload(db, game_id)
    assert payload.get("bank_asked", 0) == 0
