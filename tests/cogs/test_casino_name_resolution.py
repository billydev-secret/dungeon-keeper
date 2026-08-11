"""The casino cog's name resolver, at the one seam the builders can't cover.

``CasinoCog._names`` is the glue between the embed builders' ``name_fn`` and
``services/name_resolver``. Everything about the fallback chain itself is
pinned in ``tests/test_name_resolver_logic.py``; the only thing worth testing
here is what this cog feeds it — specifically the guild id, because the
``known_users`` fallback is keyed on ``(guild_id, user_id)`` and a wrong id
silently degrades every name back to a raw ``<@id>`` (todo #90's bug).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bot_modules.cogs.casino.cog import CasinoCog
from bot_modules.core.db_utils import open_db
from bot_modules.services.message_store import (
    init_known_users_table,
    upsert_known_user,
)

GUILD_ID = 424242
DEPARTED = 77


@pytest.fixture()
def cog(tmp_path: Path):
    """A stand-in carrying the only attribute ``_names`` actually touches."""
    db_path = tmp_path / "casino.db"
    with open_db(db_path) as conn:
        init_known_users_table(conn)
        upsert_known_user(
            conn, GUILD_ID, DEPARTED, "left_lou", "Departed Lou", ts=1.0,
            current_member=False,
        )
    return SimpleNamespace(ctx=SimpleNamespace(db_path=db_path))


@pytest.mark.asyncio
async def test_names_falls_back_to_known_users_for_a_departed_player(cog):
    name_fn = await CasinoCog._names(cog, None, [DEPARTED], guild_id=GUILD_ID)
    assert name_fn(DEPARTED) == "Departed Lou"


@pytest.mark.asyncio
async def test_names_uses_the_callers_guild_id_when_the_guild_is_uncached(cog):
    """The idle sweep resolves a hand with whatever ``bot.get_guild`` returns,
    and that can be ``None`` — a boot or outage window where the guild isn't
    in the gateway cache yet. That is precisely when ``known_users`` is the
    only source of a name, so the id has to come from the hand's own row
    rather than being defaulted away.

    Before this was threaded through, the lookup ran with ``guild_id=0``,
    matched nothing, and the auto-stood blackjack card rendered the raw
    ``<@id>`` this feature exists to remove — after paying for the query.
    """
    name_fn = await CasinoCog._names(cog, None, [DEPARTED], guild_id=GUILD_ID)
    assert name_fn(DEPARTED) == "Departed Lou"

    # The failure mode itself: the wrong guild id can name nobody.
    wrong = await CasinoCog._names(cog, None, [DEPARTED], guild_id=0)
    assert wrong(DEPARTED) == f"<@{DEPARTED}>"
