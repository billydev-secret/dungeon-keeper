"""Typed fake objects for Dungeon Keeper tests (spec §9.4).

Import these instead of building ad-hoc mocks in individual test files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass
class FakeUser:
    id: int = 1001
    name: str = "alt_jailbird"
    display_name: str = "alt_jailbird"
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
