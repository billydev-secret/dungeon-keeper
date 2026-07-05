"""Round-trip tests for hidden-channel permission serialization.

The ``.edit()`` calls in the cog can't be unit-tested, but the pure
``serialize_overwrites`` ↔ ``rebuild_overwrites`` conversion is the crux: if it
loses or swaps a bit, a restored channel comes back with the wrong permissions.
These pin the round-trip, including the allow-and-deny-on-the-same-overwrite
case where ``pair()``/``from_pair()`` would break under a swap.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import discord

from bot_modules.hidden_channels.overwrites import (
    rebuild_overwrites,
    serialize_overwrites,
)


def _role(role_id: int) -> MagicMock:
    r = MagicMock(spec=discord.Role)
    r.id = role_id
    return r


def _member(member_id: int) -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.id = member_id
    return m


def _guild(roles: dict[int, object], members: dict[int, object]) -> MagicMock:
    g = MagicMock(spec=discord.Guild)
    g.get_role = lambda i: roles.get(i)
    g.get_member = lambda i: members.get(i)
    return g


def test_roundtrip_everyone_and_member_and_mixed_bits():
    everyone = _role(1)  # stands in for guild.default_role
    role = _role(2)
    member = _member(3)

    overwrites = {
        everyone: discord.PermissionOverwrite(view_channel=False),
        # Both allow and deny set on one overwrite — the swap-sensitive case.
        role: discord.PermissionOverwrite(view_channel=True, send_messages=False),
        member: discord.PermissionOverwrite(manage_messages=True),
    }

    records = serialize_overwrites(overwrites)
    assert {r["type"] for r in records} == {"role", "member"}

    guild = _guild({1: everyone, 2: role}, {3: member})
    rebuilt = rebuild_overwrites(records, guild)

    assert rebuilt[everyone] == overwrites[everyone]
    assert rebuilt[role] == overwrites[role]
    assert rebuilt[member] == overwrites[member]


def test_allow_and_deny_are_not_swapped():
    role = _role(7)
    ow = discord.PermissionOverwrite(view_channel=True, send_messages=False)
    [record] = serialize_overwrites({role: ow})

    allow, deny = ow.pair()
    assert record["allow"] == allow.value
    assert record["deny"] == deny.value

    guild = _guild({7: role}, {})
    rebuilt = rebuild_overwrites([record], guild)
    # view_channel stays allowed, send_messages stays denied after the round-trip.
    assert rebuilt[role].view_channel is True
    assert rebuilt[role].send_messages is False


def test_vanished_targets_are_skipped_not_aborted():
    present = _role(10)
    gone_role = _role(11)
    gone_member = _member(12)

    records = serialize_overwrites(
        {
            present: discord.PermissionOverwrite(view_channel=True),
            gone_role: discord.PermissionOverwrite(view_channel=False),
            gone_member: discord.PermissionOverwrite(view_channel=False),
        }
    )

    # Guild only knows about `present`; the others were deleted / left.
    guild = _guild({10: present}, {})
    rebuilt = rebuild_overwrites(records, guild)

    assert list(rebuilt) == [present]
    assert rebuilt[present].view_channel is True
