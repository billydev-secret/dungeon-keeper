"""Tests for Veil role-removal -> answer_optout flagging + dropdown filtering."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.veil_models import VeilConfig, VeilRound
from bot_modules.services.guess_repo import (
    flag_user_open_rounds_optout,
    get_round,
    insert_round,
    mark_round_solved,
)
from tests.fakes import FakeMember, FakeRole, fake_interaction

GUILD_ID = 9001
VEIL_ROLE_ID = 7001
ROUND_ID = 50
ANSWER_ID = 1001


# ── repo: flag_user_open_rounds_optout ───────────────────────────────────────

def test_flag_user_open_rounds_only_flags_unsolved_undeleted(sync_db_path: Path) -> None:
    with open_db(sync_db_path) as conn:
        rid_open = insert_round(conn, guild_id=GUILD_ID, submitter_id=ANSWER_ID, answer_id=ANSWER_ID)
        rid_solved = insert_round(conn, guild_id=GUILD_ID, submitter_id=ANSWER_ID, answer_id=ANSWER_ID)
        rid_other = insert_round(conn, guild_id=GUILD_ID, submitter_id=2002, answer_id=2002)
        mark_round_solved(conn, rid_solved, solver_id=99, guesses_to_solve=1, unique_guessers_to_solve=1)

    with open_db(sync_db_path) as conn:
        flagged = flag_user_open_rounds_optout(conn, guild_id=GUILD_ID, user_id=ANSWER_ID)

    assert flagged == 1
    with open_db(sync_db_path) as conn:
        r_open = get_round(conn, rid_open)
        r_solved = get_round(conn, rid_solved)
        r_other = get_round(conn, rid_other)
    assert r_open is not None and r_solved is not None and r_other is not None
    assert r_open.answer_optout is True
    assert r_solved.answer_optout is False
    assert r_other.answer_optout is False


def test_flag_user_open_rounds_scoped_per_guild(sync_db_path: Path) -> None:
    with open_db(sync_db_path) as conn:
        rid_g1 = insert_round(conn, guild_id=GUILD_ID, submitter_id=ANSWER_ID, answer_id=ANSWER_ID)
        rid_g2 = insert_round(conn, guild_id=999, submitter_id=ANSWER_ID, answer_id=ANSWER_ID)

    with open_db(sync_db_path) as conn:
        flag_user_open_rounds_optout(conn, guild_id=GUILD_ID, user_id=ANSWER_ID)

    with open_db(sync_db_path) as conn:
        r_g1 = get_round(conn, rid_g1)
        r_g2 = get_round(conn, rid_g2)
    assert r_g1 is not None and r_g2 is not None
    assert r_g1.answer_optout is True
    assert r_g2.answer_optout is False


# ── on_member_update listener ────────────────────────────────────────────────

def _make_cog():
    from bot_modules.cogs.veil_cog import VeilCog
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    return VeilCog(bot)


@pytest.mark.asyncio
async def test_member_update_flags_orphaned_rounds_when_role_removed():
    """Removing the Veil role should flag the user's open rounds as answer_optout."""
    from bot_modules.cogs.veil_cog import VeilCog

    cog = _make_cog()
    veil_role = FakeRole(id=VEIL_ROLE_ID)
    other_role = FakeRole(id=8888)

    # Before: had veil role + other.  After: only other (veil removed).
    before = FakeMember(id=ANSWER_ID, roles=[veil_role, other_role])
    after = FakeMember(id=ANSWER_ID, roles=[other_role])
    after.guild = MagicMock(id=GUILD_ID)

    cfg = VeilConfig(guild_id=GUILD_ID, veil_role_id=VEIL_ROLE_ID)
    with patch("bot_modules.cogs.veil_cog._load_config", return_value=cfg), \
         patch("bot_modules.cogs.veil_cog._do_flag_user_open_rounds_optout", return_value=2) as flag_mock:
        await VeilCog.on_member_update(cog, before, after)  # type: ignore[arg-type]

    flag_mock.assert_called_once()
    kwargs = flag_mock.call_args.kwargs
    assert kwargs.get("guild_id") == GUILD_ID or flag_mock.call_args.args[1:] == (GUILD_ID, ANSWER_ID)


@pytest.mark.asyncio
async def test_member_update_noop_when_role_still_held():
    """No-op when the user still has the Veil role after the update."""
    from bot_modules.cogs.veil_cog import VeilCog

    cog = _make_cog()
    veil_role = FakeRole(id=VEIL_ROLE_ID)

    before = FakeMember(id=ANSWER_ID, roles=[veil_role])
    after = FakeMember(id=ANSWER_ID, roles=[veil_role])
    after.guild = MagicMock(id=GUILD_ID)

    cfg = VeilConfig(guild_id=GUILD_ID, veil_role_id=VEIL_ROLE_ID)
    with patch("bot_modules.cogs.veil_cog._load_config", return_value=cfg), \
         patch("bot_modules.cogs.veil_cog._do_flag_user_open_rounds_optout") as flag_mock:
        await VeilCog.on_member_update(cog, before, after)  # type: ignore[arg-type]

    flag_mock.assert_not_called()


# ── _guess_callback honours answer_optout ────────────────────────────────────

def _make_round(*, answer_optout: bool = False) -> VeilRound:
    return VeilRound(
        id=ROUND_ID, guild_id=GUILD_ID, submitter_id=2222,
        answer_id=ANSWER_ID, channel_id=8001, message_id=12345,
        crop_path="", crop_url="", original_path="",
        difficulty="medium", candidate_count=1, reroll_count=0,
        allow_reuse=False, is_reuse=False, original_round_id=None,
        reuse_blocked=False, created_at=1000.0, solved_at=None, solver_id=None,
        guesses_to_solve=None, unique_guessers_to_solve=None,
        answer_optout=answer_optout, deleted_at=None,
    )


@pytest.mark.asyncio
async def test_guess_callback_blocks_when_answer_opted_out():
    """If round.answer_optout is True, the guess flow must surface that fact
    and not open the dropdown — the round is unsolvable."""
    from bot_modules.cogs.veil_cog import GameView

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    view = GameView(bot, ROUND_ID)

    guesser = FakeMember(id=9999)
    interaction = fake_interaction(user=guesser)
    interaction.response.send_message = AsyncMock()

    cfg = VeilConfig(guild_id=GUILD_ID, veil_role_id=VEIL_ROLE_ID)
    with patch("bot_modules.cogs.veil_cog._load_config", return_value=cfg), \
         patch("bot_modules.cogs.veil_cog._do_load_round", return_value=_make_round(answer_optout=True)):
        await view._guess_callback(interaction)

    args = interaction.response.send_message.call_args
    msg = args.args[0] if args.args else args.kwargs.get("content", "")
    assert "no longer" in msg.lower() or "opted out" in msg.lower() or "unsolvable" in msg.lower()
    assert "view" not in args.kwargs or args.kwargs["view"] is None
