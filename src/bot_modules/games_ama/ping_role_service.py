"""The AMA hot-seat ping role: reading the dial, and seeding it once.

social-prompt-41 moved the hot-seat announcement off a role looked up by the
name "AMA" and onto a dashboard dial. That was the right shape and it landed
one regression with it: the dial ships unset, so a guild that already had an
``@AMA`` role — the one the old lookup had been pinging for months — went
quiet the moment the change deployed, with nothing to tell an admin why.

So the dial is seeded, **once**, the first time a guild launches an AMA after
this shipped: if it has never been set and the guild has a role literally
named "AMA" (case-insensitively), that role becomes the stored dial and shows
up on the panel like any other saved value.

Once is the whole point. The marker is the dial's own key: whether
``hot_seat_ping_role_id`` is *present* in the stored option bag, which is a
different question from what it holds. A guild that has never seen the dial
has no key; the seed writes one — the role it found, or an explicit empty
value meaning "looked, found nothing" — and an admin who later clears the
dial to (none) writes the key too, so a cleared dial stays cleared instead of
being helpfully re-filled on the next launch.

The bag is read-modify-written rather than replaced: the panel's own save
replaces it wholesale (that is how a retired dial gets cleared), but this
write is not a save and must not drop ``questions_per_turn``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

log = logging.getLogger(__name__)

#: The dial's key in the ``games_game_config`` option bag for game_type 'ama'.
#: The panel (``games-ama.js``) offers the same key.
PING_ROLE_KEY = "hot_seat_ping_role_id"

#: The role name the retired by-name lookup used, and so the only name worth
#: adopting. Compared case-insensitively, whitespace stripped; nothing else
#: ("AMA Pings", "ama-ping") is close enough to ping a whole role on a guess.
SEED_ROLE_NAME = "ama"


def parse_role_id(raw: Any) -> int | None:
    """A stored dial value as a role id, or None for unset/(none)/garbage."""
    try:
        return int(raw) or None
    except (TypeError, ValueError):
        return None


def find_seed_role_id(roles: Iterable[Any]) -> int | None:
    """The id of the guild's role named "AMA", or None if it has none."""
    for role in roles:
        name = str(getattr(role, "name", "") or "").strip()
        if name.casefold() == SEED_ROLE_NAME:
            role_id = parse_role_id(getattr(role, "id", None))
            if role_id is not None:
                return role_id
    return None


def ping_role_seed(options: dict, roles: Iterable[Any]) -> tuple[int | None, bool]:
    """``(role id to use, whether the dial must be written)``.

    Pure, so the three cases that matter are testable without a guild: the
    dial already has an answer (set *or* deliberately cleared) and is left
    alone; the dial has never been answered and a role named AMA exists, so
    it is adopted; and the dial has never been answered and no such role
    exists, so the one-time look is recorded and never repeated.
    """
    if PING_ROLE_KEY in options:
        return parse_role_id(options.get(PING_ROLE_KEY)), False
    return find_seed_role_id(roles), True


async def resolve_ping_role_id(db, guild_id: int, options: dict, guild) -> int | None:
    """The hot-seat ping role for this launch, seeding the dial if it is new.

    ``options`` is the bag ``get_game_options`` already read, so the common
    case (a dial with an answer) costs nothing extra.
    """
    role_id, needs_write = ping_role_seed(options, getattr(guild, "roles", ()) or ())
    if not needs_write or guild is None or guild_id <= 0:
        return role_id
    try:
        await _store_seed(db, guild_id, options, role_id)
    except Exception:
        # A failed write only means the look happens again next launch.
        log.warning("AMA: could not seed the hot-seat ping role for guild %s.", guild_id)
        return role_id
    if role_id is not None:
        log.info(
            "AMA: seeded the hot-seat ping role for guild %s from the @AMA role %s.",
            guild_id, role_id,
        )
    return role_id


async def _store_seed(db, guild_id: int, options: dict, role_id: int | None) -> None:
    """Write the seeded dial into the stored option bag, keeping the rest."""
    merged = dict(options)
    merged[PING_ROLE_KEY] = str(role_id) if role_id is not None else ""
    row = await db.fetchone(
        "SELECT 1 FROM games_game_config WHERE guild_id = ? AND game_type = 'ama'",
        (guild_id,),
    )
    if row:
        await db.execute(
            "UPDATE games_game_config SET options = ?, updated_at = CURRENT_TIMESTAMP"
            " WHERE guild_id = ? AND game_type = 'ama'",
            (json.dumps(merged), guild_id),
        )
    else:
        await db.execute(
            "INSERT INTO games_game_config (guild_id, game_type, enabled, options)"
            " VALUES (?, 'ama', 1, ?)",
            (guild_id, json.dumps(merged)),
        )
