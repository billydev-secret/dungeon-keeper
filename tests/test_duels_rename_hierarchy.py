"""A player outranking the bot must not stop a nickname-stake game.

The role-hierarchy check used to be a fatal preflight — one staff member whose
top role sat above the bot's aborted the whole game with "My highest role must
be above all players' roles…". Now it's non-fatal: the game runs, a warning
names who can't be renamed, and if one of them loses the win stands with no
nickname applied. Only the **Manage Nicknames** permission remains a hard gate
(without it the bot can rename no one, so the game is pointless).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from bot_modules.duels import base_game as bg

LOSER_ID = 222
WINNER_ID = 111
OWNER_ID = 999
NEW_NICK = "NewNick"
OLD_NAME = "OldName"


class _Role:
    """A stand-in role that orders by position like ``discord.Role`` does."""

    def __init__(self, position: int) -> None:
        self.position = position

    def __le__(self, other: "_Role") -> bool:
        return self.position <= other.position


def _member(uid: int, role_pos: int, name: str = "Someone"):
    return SimpleNamespace(id=uid, top_role=_Role(role_pos), display_name=name)


def _guild(bot_role_pos: int, *, owner_id: int = OWNER_ID, manage_nicks: bool = True):
    me = SimpleNamespace(
        top_role=_Role(bot_role_pos),
        guild_permissions=SimpleNamespace(manage_nicknames=manage_nicks),
    )
    return SimpleNamespace(me=me, owner_id=owner_id)


def _cog():
    return bg.BaseGame.__new__(bg.BaseGame)


# ── _unrenameable_members ─────────────────────────────────────────────────────


def test_unrenameable_members_flags_only_those_at_or_above_the_bot():
    cog = _cog()
    guild = _guild(bot_role_pos=5)
    below = _member(1, role_pos=3, name="Regular")
    equal = _member(2, role_pos=5, name="Peer")
    above = _member(3, role_pos=9, name="Stef")  # staff, outranks the bot
    flagged = cog._unrenameable_members(guild, [below, equal, above])
    # `me.top_role <= member.top_role` → equal and above both blocked.
    assert flagged == [equal, above]


def test_unrenameable_members_excludes_the_guild_owner():
    """The owner can't be renamed either, but has a self-apply path — so they
    never count as a blocker here."""
    cog = _cog()
    guild = _guild(bot_role_pos=5, owner_id=OWNER_ID)
    owner = _member(OWNER_ID, role_pos=50, name="Owner")
    assert cog._unrenameable_members(guild, [owner]) == []


# ── _unrenameable_notice ──────────────────────────────────────────────────────


def test_unrenameable_notice_is_none_when_everyone_is_renameable():
    assert _cog()._unrenameable_notice([]) is None


def test_unrenameable_notice_names_the_blocked_players():
    cog = _cog()
    one = cog._unrenameable_notice([_member(3, 9, "Stef")])
    assert one is not None
    assert "**Stef**" in one and "win stands" in one and "this player" in one
    two = cog._unrenameable_notice([_member(3, 9, "Stef"), _member(4, 8, "Mod")])
    assert two is not None
    assert "**Stef**" in two and "**Mod**" in two and "these players" in two


# ── _check_bot_can_nick (now Manage-Nicknames only) ───────────────────────────


@pytest.mark.asyncio
async def test_check_bot_can_nick_blocks_only_on_missing_permission():
    cog = _cog()
    # Missing the permission → hard error string.
    err = await cog._check_bot_can_nick(_guild(bot_role_pos=5, manage_nicks=False))
    assert err is not None and "Manage Nicknames" in err
    # Has the permission → no error, regardless of any player's role height.
    assert await cog._check_bot_can_nick(_guild(bot_role_pos=1)) is None


# ── Resolution: a loser who outranks the bot → win stands, no rename ───────────


def _async_noop():
    async def _fn(*a, **k):
        return None

    return _fn


def _async_return(value):
    async def _fn(*a, **k):
        return value

    return _fn


@pytest.mark.asyncio
async def test_nick_submit_skips_rename_when_loser_outranks_bot(monkeypatch):
    """The winner submits a nick but the loser is staff: the game concludes at
    NO_NICK_SET, the loser is never edited, and the message says the win stands."""
    cog = _cog()
    cog.GAME_KEY = "test"
    cog.GAME_DISPLAY_NAME = "Test"
    cog.bot = MagicMock()

    loser = MagicMock()
    loser.id = LOSER_ID
    loser.display_name = OLD_NAME
    loser.nick = OLD_NAME
    loser.edit = _async_noop()

    game = SimpleNamespace(
        id=1, state="RESOLVED", winner_id=WINNER_ID, loser_id=LOSER_ID,
        challenger_id=WINNER_ID,
    )
    guild = MagicMock()
    guild.owner_id = OWNER_ID
    guild.get_member = MagicMock(
        side_effect=lambda uid: loser if uid == LOSER_ID else MagicMock(id=uid)
    )

    interaction = MagicMock()
    interaction.user.id = WINNER_ID
    interaction.guild = guild
    interaction.response.send_message = _async_noop()

    async def _get_config(db, gid, gtype):
        return {"max_nick_length": 32, "nick_denylist": "[]", "sentence_hours": 24}

    monkeypatch.setattr(bg.duels_db, "get_config", _get_config)
    monkeypatch.setattr(
        bg, "validate_nickname",
        lambda *a, **k: SimpleNamespace(ok=True, value=NEW_NICK, reason=None),
    )

    apply_spy = MagicMock()
    monkeypatch.setattr(bg.duels_db, "apply_nick", _wrap_async(apply_spy))
    edit_spy = MagicMock()
    loser.edit = _wrap_async(edit_spy)

    cog._db_get_game = _async_return(game)
    cog._check_bot_can_nick = _async_return(None)  # has Manage Nicknames
    cog._check_no_active_nick = _async_return([])
    # The crux: the loser outranks the bot.
    cog._unrenameable_members = MagicMock(return_value=[loser])
    set_state = MagicMock()
    cog._db_set_state = _wrap_async(set_state)

    sent = MagicMock()
    interaction.response.send_message = _wrap_async(sent)

    await cog._handle_nick_submit_locked(interaction, game.id, NEW_NICK)

    edit_spy.assert_not_called()  # loser never renamed
    apply_spy.assert_not_called()  # no sentence recorded
    set_state.assert_any_call(game.id, "NO_NICK_SET")
    msg = sent.call_args[0][0]
    assert OLD_NAME in msg and "win stands" in msg


def _wrap_async(spy):
    async def _fn(*a, **k):
        return spy(*a, **k)

    return _fn


# ── owner heads-up before the game, not a surprise at the end ────────────────
# Discord blocks renaming the guild owner outright, so _unrenameable_members
# deliberately excludes them (the rename flow has its own branch). Nothing
# warned about it up front, though — the owner lost their name on 2026-08-21
# and had to apply it by hand with no sign anywhere that it would work that way.


def test_owner_notice_names_the_owner():
    cog = _cog()
    guild = _guild(bot_role_pos=99)
    owner = _member(OWNER_ID, role_pos=1, name="Billy")
    notice = cog._owner_notice(guild, [owner, _member(1, role_pos=1)])
    assert notice is not None
    assert "Billy" in notice
    assert "apply it themselves" in notice


def test_owner_notice_is_silent_when_the_owner_is_not_playing():
    cog = _cog()
    assert cog._owner_notice(_guild(bot_role_pos=99), [_member(1, role_pos=1)]) is None


def test_rename_warning_combines_hierarchy_and_owner_cases():
    """Both can be true at once — a staff member above the bot *and* the owner
    in the same lobby — and the challenger needs to hear about both."""
    cog = _cog()
    guild = _guild(bot_role_pos=5)
    staff = _member(1, role_pos=9, name="Staff")
    owner = _member(OWNER_ID, role_pos=1, name="Billy")
    warning = cog._rename_warning(guild, [staff, owner])
    assert warning is not None
    assert "Staff" in warning       # role above mine
    assert "Billy" in warning       # server owner


def test_rename_warning_is_silent_when_everyone_is_renameable():
    cog = _cog()
    guild = _guild(bot_role_pos=9)
    assert cog._rename_warning(guild, [_member(1, role_pos=1)]) is None


def test_owner_is_still_excluded_from_the_unrenameable_list():
    """The two paths must stay separate: the owner's sentence is recorded and
    self-applied, which the hierarchy branch would short-circuit."""
    cog = _cog()
    guild = _guild(bot_role_pos=1)
    owner = _member(OWNER_ID, role_pos=9, name="Billy")
    assert cog._unrenameable_members(guild, [owner]) == []
