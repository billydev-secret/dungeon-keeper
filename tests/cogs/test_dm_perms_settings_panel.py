"""The ephemeral DM settings panel — the only member-facing DM surface.

``/dm_help``, ``/dm_set_mode``, ``/dm_status`` and ``/dm_revoke`` were removed
on 2026-07-28 and folded into ``DmSettingsView``, reached from the request
panel's "My DM Settings" button. With no slash commands left, every guard that
used to sit on a command signature (guild-only, "not yourself") now lives in the
view, so it is tested here rather than assumed.

Deliberately thin: the revoke *decision* is covered by
``test_dm_perms_logic.py::discard_consent_pair`` and the DB writes by
``test_dm_perms_service.py``. What's asserted here is the wiring those tests
can't see — which control appears when.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.cogs.dm_perms_cog import (
    DM_SETTINGS_PANEL_CUSTOM_ID,
    DmRequestPanelView,
    DmSettingsView,
)


def _member(user_id: int = 1, *, roles=None) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.display_name = f"User{user_id}"
    member.roles = roles or []
    guild = MagicMock()
    guild.id = 99
    guild.icon = None
    member.guild = guild
    return member


def _cog(*, mutual: bool = False) -> MagicMock:
    cog = MagicMock()
    cog.ctx.db_path = ":memory:"
    cog._mode_roles_for.return_value = {}
    cog._is_mutual.return_value = mutual
    cog.revoke_connection = AsyncMock(return_value=True)
    return cog


def _interaction() -> MagicMock:
    interaction = MagicMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    # Fresh interaction: _rerender picks edit_message over edit_original_response.
    interaction.response.is_done.return_value = False
    interaction.edit_original_response = AsyncMock()
    return interaction


def _revoke_button(view: DmSettingsView):
    return view._revoke_button


@pytest.fixture(autouse=True)
def audit_rows(monkeypatch):
    """Capture ``write_audit_log`` instead of writing it.

    A mode change records itself since 2026-08-18, and ``_cog`` hands the view a
    ``:memory:`` path with no schema behind it — so without this every mode test
    dies on "no such table: dm_audit_log" rather than on anything it asserts.
    Returning the list lets one test check the row is actually written.
    """
    import bot_modules.cogs.dm_perms_cog as mod

    rows: list[tuple] = []
    monkeypatch.setattr(
        mod, "write_audit_log",
        lambda *args, **kwargs: rows.append((args, kwargs)),
    )
    return rows


@pytest.fixture(autouse=True)
def _stub_accent(monkeypatch):
    """The panel resolves the guild accent; colour isn't what's under test."""
    import bot_modules.cogs.dm_perms_cog as mod

    monkeypatch.setattr(
        mod, "resolve_accent_color", AsyncMock(return_value=discord.Color.blurple())
    )


@pytest.mark.asyncio
async def test_settings_button_refuses_outside_a_guild():
    """interaction.user is a User, not a Member, in DMs — the mode roles the
    panel reads don't exist there, so it must not open."""
    view = DmRequestPanelView(_cog())
    interaction = _interaction()
    interaction.user = MagicMock(spec=discord.User)

    await DmRequestPanelView.open_settings(view, interaction, MagicMock())

    interaction.response.send_message.assert_awaited_once()
    assert "server" in interaction.response.send_message.await_args.args[0].lower()


def test_settings_button_is_registered_with_a_stable_custom_id():
    """The panel view is persistent, so the button needs a stable custom_id to
    survive a restart."""
    view = DmRequestPanelView(_cog())
    ids = {getattr(c, "custom_id", None) for c in view.children}
    assert DM_SETTINGS_PANEL_CUSTOM_ID in ids


@pytest.mark.asyncio
async def test_selecting_yourself_is_refused_and_offers_no_revoke():
    view = DmSettingsView(_cog(mutual=True), _member(1))
    interaction = _interaction()
    select = MagicMock()
    select.values = [_member(1)]

    await DmSettingsView.user_select(view, interaction, select)

    assert _revoke_button(view) is None
    note = interaction.response.edit_message.await_args.kwargs["content"]
    assert "yourself" in note.lower()


@pytest.mark.asyncio
async def test_selecting_a_connected_member_offers_revoke():
    view = DmSettingsView(_cog(mutual=True), _member(1))
    interaction = _interaction()
    select = MagicMock()
    select.values = [_member(2)]

    await DmSettingsView.user_select(view, interaction, select)

    assert _revoke_button(view) is not None
    assert _revoke_button(view).style is discord.ButtonStyle.danger


@pytest.mark.asyncio
async def test_selecting_an_unconnected_member_offers_no_revoke():
    """No connection means nothing to revoke — a dead button would just invite
    a pointless click."""
    view = DmSettingsView(_cog(mutual=False), _member(1))
    interaction = _interaction()
    select = MagicMock()
    select.values = [_member(2)]

    await DmSettingsView.user_select(view, interaction, select)

    assert _revoke_button(view) is None


@pytest.mark.asyncio
async def test_switching_between_connected_members_relabels_the_revoke_button():
    """Regression: the button was only created when absent, so a second pick
    kept the first member's name while _selected had moved on — the label named
    Alice on a button that revoked Bob."""
    view = DmSettingsView(_cog(mutual=True), _member(1))

    first = MagicMock()
    first.values = [_member(2)]
    await DmSettingsView.user_select(view, _interaction(), first)
    assert "User2" in _revoke_button(view).label

    second = MagicMock()
    second.values = [_member(3)]
    await DmSettingsView.user_select(view, _interaction(), second)

    assert "User3" in _revoke_button(view).label
    assert "User2" not in _revoke_button(view).label
    assert view._selected.id == 3


@pytest.mark.asyncio
async def test_revoking_after_switching_targets_the_labelled_member():
    """The end-to-end version of the above: whoever the button names is who
    gets torn down."""
    cog = _cog(mutual=True)
    view = DmSettingsView(cog, _member(1))

    for uid in (2, 3):
        select = MagicMock()
        select.values = [_member(uid)]
        await DmSettingsView.user_select(view, _interaction(), select)

    await view._on_revoke(_interaction_capturing(view))

    assert cog.revoke_connection.await_args.args[2].id == 3


@pytest.mark.asyncio
async def test_mode_change_rerenders_with_the_new_mode(monkeypatch):
    """Regression: add_roles/remove_roles go out over REST and don't update the
    cached member, so re-reading roles right after made the panel show the old
    mode while the confirmation line claimed the new one."""
    import bot_modules.cogs.dm_perms_cog as mod

    monkeypatch.setattr(mod, "set_member_dm_mode", AsyncMock())
    # roles stay empty ⇒ resolve_mode() keeps saying "ask", as on a live gateway lag
    view = DmSettingsView(_cog(), _member(1))
    assert view._current_mode() == "ask"

    await view._set_mode(_interaction_capturing(view), "closed")

    assert view._current_mode() == "closed"
    active = [
        c for c in view.children
        if getattr(c, "custom_id", "") == "dm_settings_mode:closed"
    ]
    assert active[0].style is discord.ButtonStyle.primary


@pytest.mark.asyncio
async def test_failed_mode_change_does_not_move_the_marker(monkeypatch):
    """A Forbidden must not leave the panel claiming a mode that was never set."""
    import bot_modules.cogs.dm_perms_cog as mod

    monkeypatch.setattr(
        mod,
        "set_member_dm_mode",
        AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "nope")),
    )
    view = DmSettingsView(_cog(), _member(1))

    await view._set_mode(_interaction(), "open")

    assert view._current_mode() == "ask"


@pytest.mark.asyncio
async def test_revoke_defers_before_its_rest_calls():
    """revoke_connection can take several REST round-trips; without an upfront
    defer the 3s interaction window closes and the edit raises NotFound."""
    view = DmSettingsView(_cog(mutual=True), _member(1))
    view._selected = _member(2)
    interaction = _interaction()
    interaction.response.defer = AsyncMock()
    interaction.response.is_done.return_value = True
    interaction.edit_original_response = AsyncMock()

    await view._on_revoke(interaction)

    interaction.response.defer.assert_awaited_once()
    interaction.edit_original_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_revoke_reports_when_there_was_nothing_to_remove():
    """revoke_connection returning False must read as "no connection", not as
    a successful removal."""
    cog = _cog(mutual=True)
    cog.revoke_connection = AsyncMock(return_value=False)
    view = DmSettingsView(cog, _member(1))
    view._selected = _member(2)

    await view._on_revoke(_interaction_capturing(view))

    note = view._last_note
    assert "don't have a connection" in note.lower()


@pytest.mark.asyncio
async def test_revoke_clears_the_button_after_success():
    cog = _cog(mutual=True)
    view = DmSettingsView(cog, _member(1))
    view._selected = _member(2)
    view._set_revoke_visible(True, "User2")

    await view._on_revoke(_interaction_capturing(view))

    cog.revoke_connection.assert_awaited_once()
    assert _revoke_button(view) is None
    assert "has been removed" in view._last_note


@pytest.mark.asyncio
async def test_revoke_without_a_selection_asks_for_one():
    view = DmSettingsView(_cog(), _member(1))
    interaction = _interaction()

    await view._on_revoke(interaction)

    interaction.response.send_message.assert_awaited_once()
    assert "pick someone" in interaction.response.send_message.await_args.args[0].lower()


@pytest.mark.parametrize("mode", ["open", "ask", "closed"])
@pytest.mark.asyncio
async def test_setting_a_mode_writes_it_and_reports_it(monkeypatch, mode):
    import bot_modules.cogs.dm_perms_cog as mod

    set_mode = AsyncMock()
    monkeypatch.setattr(mod, "set_member_dm_mode", set_mode)
    view = DmSettingsView(_cog(), _member(1))

    await view._set_mode(_interaction_capturing(view), mode)

    assert set_mode.await_args.args[1] == mode
    assert mode.upper() in view._last_note


@pytest.mark.asyncio
async def test_mode_change_records_itself_in_the_audit_log(monkeypatch, audit_rows):
    """Without this row, only Discord's own audit log knew a mode had changed."""
    import bot_modules.cogs.dm_perms_cog as mod

    monkeypatch.setattr(mod, "set_member_dm_mode", AsyncMock())
    view = DmSettingsView(_cog(), _member(1))

    await view._set_mode(_interaction_capturing(view), "closed")

    assert len(audit_rows) == 1
    args, kwargs = audit_rows[0]
    assert args[2] == "mode_set"
    assert kwargs["notes"] == "mode=closed"
    assert kwargs["actor_id"] == 1


@pytest.mark.asyncio
async def test_second_mode_change_uses_live_roles_not_the_panel_snapshot(monkeypatch):
    """Regression (2026-08-18): the view cached its member at panel-open time.

    A member who changed mode twice without closing the panel had the second
    change computed against the *first* change's stale role set, so the previous
    mode's role was never removed — leaving two DM roles on and handing the dedup
    listener a choice it then got wrong, silently reverting the pick.
    """
    import bot_modules.cogs.dm_perms_cog as mod

    set_mode = AsyncMock()
    monkeypatch.setattr(mod, "set_member_dm_mode", set_mode)

    ask_role = MagicMock(spec=discord.Role)
    ask_role.id, ask_role.name, ask_role.position = 20, "ASK TO DM", 9
    stale = _member(1)                       # what the panel opened with: no roles
    live = _member(1, roles=[ask_role])      # what the member holds by the 2nd click

    view = DmSettingsView(_cog(), stale)
    interaction = _interaction_capturing(view)
    interaction.user = live

    await view._set_mode(interaction, "closed")

    assert set_mode.await_args.args[0] is live


@pytest.mark.asyncio
async def test_mode_change_surfaces_a_missing_permission(monkeypatch):
    """A Forbidden from role assignment must say so rather than claim success."""
    import bot_modules.cogs.dm_perms_cog as mod

    monkeypatch.setattr(
        mod,
        "set_member_dm_mode",
        AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "nope")),
    )
    view = DmSettingsView(_cog(), _member(1))
    interaction = _interaction()

    await view._set_mode(interaction, "open")

    interaction.response.send_message.assert_awaited_once()
    assert "permission" in interaction.response.send_message.await_args.args[0].lower()


def _interaction_capturing(view: DmSettingsView) -> MagicMock:
    """An interaction whose edit_message stashes the note on the view, so tests
    can assert on what the member was told."""
    interaction = _interaction()

    async def _capture(*_a, **kw):
        view._last_note = kw.get("content") or ""

    interaction.response.edit_message = AsyncMock(side_effect=_capture)
    return interaction
