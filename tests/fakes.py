"""Typed fake objects for Dungeon Keeper tests (spec §9.4).

Import these instead of building ad-hoc mocks in individual test files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord


@dataclass
class FakeRole:
    id: int
    name: str = "Role"
    position: int = 0

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (FakeRole, discord.Role)):
            return self.id == other.id
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.id)

    @property
    def mention(self) -> str:
        return f"<@&{self.id}>"


@dataclass
class FakeUser:
    id: int = 1001
    name: str = "lorem_ipsum"
    display_name: str = "Lorem Ipsum"
    bot: bool = False
    roles: list = field(default_factory=list)
    guild_permissions: MagicMock = field(default_factory=lambda: MagicMock(administrator=False))

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"


@dataclass
class FakeMember(FakeUser):
    """A FakeUser that also satisfies isinstance checks for discord.Member-like usage."""
    joined_at: float | None = None
    guild: object | None = None
    add_roles: AsyncMock = field(default_factory=AsyncMock)
    remove_roles: AsyncMock = field(default_factory=AsyncMock)

    def __post_init__(self) -> None:
        _dm_msg = MagicMock()
        _dm_msg.edit = AsyncMock()
        _dm_msg.content = ""
        _dm_channel = MagicMock()
        _dm_channel.fetch_message = AsyncMock(return_value=_dm_msg)
        self.create_dm = AsyncMock(return_value=_dm_channel)

    def has_role(self, role_id: int) -> bool:
        return any(getattr(r, "id", r) == role_id for r in self.roles)


@dataclass
class FakeChannel:
    id: int
    name: str = "general"
    type: str = "text"
    parent_id: int | None = None
    category: object | None = None

    async def send(self, *args, **kwargs) -> MagicMock:
        return MagicMock()

    async def set_permissions(self, *args, **kwargs) -> None:
        pass


@dataclass
class FakeGuild:
    id: int = 9001
    name: str = "Test Guild"
    members: dict = field(default_factory=dict)
    channels: dict = field(default_factory=dict)
    roles: dict = field(default_factory=dict)

    def get_member(self, uid: int):
        return self.members.get(uid)

    def get_channel(self, cid: int):
        return self.channels.get(cid)

    def get_role(self, rid: int):
        return self.roles.get(rid)


class FakeSendChannel:
    """Channel whose ``send`` returns a message with a real integer id, so
    result posts that persist ``result_message_id`` can run against sqlite."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._next_id = 7000

    async def send(self, *args, **kwargs) -> SimpleNamespace:
        self._next_id += 1
        self.sent.append({"args": args, **kwargs})
        return SimpleNamespace(id=self._next_id)

    async def fetch_message(self, message_id: int):
        raise discord.NotFound(MagicMock(status=404), "no message")


class FakeEconGamesBot:
    """Bot fake for duel/economy payout tests: a real ``games_db``, a
    ``ctx.db_path`` the economy layer can open, and a guild whose members
    resolve (non-bot, non-booster). ``with_channel=True`` adds a
    FakeSendChannel so resolution paths that post a result message work."""

    def __init__(
        self,
        games_db,
        db_path: Path,
        member_ids,
        *,
        guild_id: int = 9001,
        with_channel: bool = False,
    ) -> None:
        self.games_db = games_db
        self.ctx = SimpleNamespace(db_path=db_path)
        members = {
            uid: SimpleNamespace(
                id=uid, bot=False, premium_since=None,
                display_name=f"U{uid}", mention=f"<@{uid}>",
            )
            for uid in member_ids
        }
        self.guild = FakeGuild(id=guild_id, members=members)
        self.channel = FakeSendChannel() if with_channel else None

    def add_view(self, *args, **kwargs) -> None:
        pass

    def get_guild(self, guild_id: int):
        return self.guild if guild_id == self.guild.id else None

    def get_channel(self, channel_id: int):
        return self.channel


def fake_interaction(
    *,
    user: FakeUser | None = None,
    guild: FakeGuild | None = None,
    **overrides,
) -> MagicMock:
    """Build a MagicMock that looks like a discord.Interaction."""
    i = MagicMock(spec=discord.Interaction)
    i.user = user or FakeUser()
    i.guild = guild or FakeGuild()
    i.response = MagicMock()
    i.response.send_message = AsyncMock()
    i.response.send_modal = AsyncMock()
    i.response.defer = AsyncMock()
    i.response.edit_message = AsyncMock()
    i.response.is_done = MagicMock(return_value=False)
    i.followup = MagicMock()
    i.followup.send = AsyncMock()
    i.edit_original_response = AsyncMock()
    for k, v in overrides.items():
        setattr(i, k, v)
    return i
