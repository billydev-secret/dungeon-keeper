"""Cog/view tests for DM permissions.

Only the glue that has actually been wrong lives here — what the request
picker and the settings panel keep across a redraw. Everything those panels
*decide* is covered by ``tests/test_dm_perms_logic.py`` and
``tests/test_dm_perms_service.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.cogs.dm_perms_cog import DmRequestLookupView, DmSettingsView
from tests.fakes import fake_interaction

TARGET_ID = 424242


async def _pick_a_user(view: DmRequestLookupView) -> MagicMock:
    """Drive the user select the way a member picking someone does.

    ``_values`` is where discord.py parks the resolved selection before it
    dispatches the callback, and what the ``values`` property reads back.
    """
    user = MagicMock(spec=discord.Member)
    user.id = TARGET_ID
    view.user_select._values = [user]
    await view.user_select.callback(fake_interaction())
    return user


@pytest.mark.asyncio
async def test_picking_a_user_pins_it_as_the_select_default():
    """Without a default value the redraw below has nothing to re-render."""
    view = DmRequestLookupView(MagicMock())
    user = await _pick_a_user(view)
    assert view._selected_user is user
    assert [v.id for v in view.user_select.default_values] == [TARGET_ID]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("button_attr", "expected_type"),
    [("type_dm", "dm"), ("type_friend", "friend")],
)
async def test_switching_request_type_keeps_the_selected_user(
    button_attr, expected_type
):
    """The reported bug: the pick vanished from the panel on every toggle.

    The type buttons redraw the whole view with ``edit_message``, and a
    UserSelect sent without ``default_values`` comes back empty — so the
    member had to re-pick after every switch between Direct Message and
    Friend Request.
    """
    view = DmRequestLookupView(MagicMock())
    user = await _pick_a_user(view)

    interaction = fake_interaction()
    await getattr(view, button_attr).callback(interaction)

    assert view._request_type == expected_type
    assert view._selected_user is user
    # The redrawn view still names the pick, so the client re-renders it.
    assert [v.id for v in view.user_select.default_values] == [TARGET_ID]
    interaction.response.edit_message.assert_awaited_once()
    assert interaction.response.edit_message.await_args.kwargs["view"] is view


@pytest.mark.asyncio
async def test_switching_back_and_forth_keeps_one_default_value():
    """A default per toggle would breach the select's max_values of 1."""
    view = DmRequestLookupView(MagicMock())
    await _pick_a_user(view)
    for attr in ("type_friend", "type_dm", "type_friend"):
        await getattr(view, attr).callback(fake_interaction())
    assert len(view.user_select.default_values) == 1


@pytest.mark.asyncio
async def test_continue_without_a_pick_still_refuses():
    """The guard the default-value change must not paper over."""
    view = DmRequestLookupView(MagicMock())
    interaction = fake_interaction()
    await view.continue_btn.callback(interaction)
    interaction.response.send_message.assert_awaited_once()
    interaction.response.send_modal.assert_not_awaited()


# ── the settings panel's own select ──────────────────────────────────────────


def _settings_view(*, mutual: bool = True) -> DmSettingsView:
    """The My DM Settings panel, with the cog's lookups stubbed out."""
    cog = MagicMock()
    cog._mode_roles_for.return_value = {}
    cog._is_mutual.return_value = mutual
    member = MagicMock(spec=discord.Member)
    member.id = 111
    member.guild = MagicMock()
    member.guild.id = 222
    member.roles = []
    cog.bot.ctx = MagicMock()
    return DmSettingsView(cog, member)


async def _look_up(view: DmSettingsView, user_id: int = TARGET_ID) -> MagicMock:
    target = MagicMock(spec=discord.Member)
    target.id = user_id
    target.display_name = "Someone"
    view.user_select._values = [target]
    await view.user_select.callback(fake_interaction())
    return target


@pytest.mark.asyncio
async def test_settings_panel_keeps_the_looked_up_member_across_a_mode_change():
    """Same defect as the request picker, on the panel next door.

    Every button here redraws the view, so the member's pick disappeared from
    the select while the status line beneath it still named them.
    """
    view = _settings_view()
    target = await _look_up(view)
    assert [v.id for v in view.user_select.default_values] == [TARGET_ID]

    with patch("bot_modules.cogs.dm_perms_cog.set_member_dm_mode", new=AsyncMock()), \
            patch("bot_modules.cogs.dm_perms_cog.write_audit_log"), \
            patch(
                "bot_modules.cogs.dm_perms_cog.build_dm_settings_embed",
                return_value=discord.Embed(title="settings"),
            ), \
            patch(
                "bot_modules.cogs.dm_perms_cog.safe_resolve_accent",
                new=AsyncMock(return_value=None),
            ):
        await view.mode_closed.callback(fake_interaction())

    assert view._selected is target
    assert [v.id for v in view.user_select.default_values] == [TARGET_ID]


@pytest.mark.asyncio
async def test_settings_panel_select_has_no_default_before_anyone_is_picked():
    """An unset select must render its placeholder, not an empty default."""
    view = _settings_view()
    view._sync_select_default()
    assert view.user_select.default_values == []
