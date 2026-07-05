"""Pure serialization of channel permission overwrites.

Discord's ``channel.overwrites`` is a live ``dict[Role | Member, PermissionOverwrite]``
that can't be persisted directly. These helpers flatten it to/from a JSON-able
list of ``{id, type, allow, deny}`` records so a hidden channel's exact perms
survive a bot restart and can be reinstated verbatim on restore.

Kept free of any I/O or ``discord`` client state so the round-trip is unit-testable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord

if TYPE_CHECKING:
    from collections.abc import Mapping

# Target-kind tags stored in the JSON so restore knows whether to resolve the
# id as a role or a member.
ROLE = "role"
MEMBER = "member"

OverwriteRecord = dict[str, Any]


def serialize_overwrites(
    overwrites: Mapping[Any, discord.PermissionOverwrite],
) -> list[OverwriteRecord]:
    """Flatten a channel's overwrites into JSON-able records.

    Each ``PermissionOverwrite`` becomes an ``(allow, deny)`` bit pair keyed by
    the target's id and kind. Roles and members are the only overwrite targets
    Discord supports.
    """
    records: list[OverwriteRecord] = []
    for target, overwrite in overwrites.items():
        allow, deny = overwrite.pair()
        kind = ROLE if isinstance(target, discord.Role) else MEMBER
        records.append(
            {
                "id": target.id,
                "type": kind,
                "allow": allow.value,
                "deny": deny.value,
            }
        )
    return records


def rebuild_overwrites(
    records: list[OverwriteRecord],
    guild: discord.Guild,
) -> dict[discord.Role | discord.Member, discord.PermissionOverwrite]:
    """Resolve stored records back into an overwrites dict for ``channel.edit``.

    Targets that no longer exist (deleted role, departed member) are skipped so
    one stale entry can't abort the whole restore.
    """
    rebuilt: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {}
    for rec in records:
        target: discord.Role | discord.Member | None
        if rec["type"] == ROLE:
            target = guild.get_role(rec["id"])
        else:
            target = guild.get_member(rec["id"])
        if target is None:
            continue
        overwrite = discord.PermissionOverwrite.from_pair(
            discord.Permissions(rec["allow"]),
            discord.Permissions(rec["deny"]),
        )
        rebuilt[target] = overwrite
    return rebuilt
