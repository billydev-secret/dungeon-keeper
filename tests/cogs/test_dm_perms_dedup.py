"""The DM-role dedup listener.

The dedup *decision* is ``test_dm_perms_logic``'s job; what is asserted here is
the wiring it can't see — that the listener reads the member-update diff at all,
which it did not before 2026-08-18. Its sibling defect (the settings panel's
stale member cache) is covered in ``test_dm_perms_settings_panel``.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot_modules.cogs.dm_perms_cog import DmPermsCog

# The guild in the incident used custom role names with configured ids, which
# is the path that only ``is_dm_mode_role``'s id branch can match.
ROLE_IDS = {"open": 10, "ask": 20, "closed": 30}


def _role(role_id: int, name: str, position: int):
    return SimpleNamespace(id=role_id, name=name, position=position)


OPEN = _role(10, "DM OPEN", 5)
ASK = _role(20, "ASK TO DM", 9)      # sits *above* closed, as it did in prod
CLOSED = _role(30, "DM CLOSED", 1)


@pytest.mark.asyncio
async def test_dedup_strips_the_previous_role_not_the_one_just_added():
    """Holding Ask and pressing Closed must leave Closed, not Ask.

    Before the fix the listener ignored ``before`` entirely and kept whichever
    role sat highest, so the just-granted role was removed 0.3s after the
    member chose it — 21 times across one guild.
    """
    before = SimpleNamespace(roles=[ASK])
    after = SimpleNamespace(
        id=1, roles=[ASK, CLOSED], guild=SimpleNamespace(id=99), remove_roles=AsyncMock()
    )
    cog = SimpleNamespace(_mode_roles_for=lambda _guild_id: ROLE_IDS)

    await DmPermsCog._on_member_update_dm_roles(cog, before, after)

    after.remove_roles.assert_awaited_once()
    assert after.remove_roles.await_args.args == (ASK,)
