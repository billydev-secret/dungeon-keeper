"""Announcement role buttons: the view builder and the click path.

The click path is the interesting half — a posted announcement stays clickable
forever, so the grant branch re-checks role safety on every press rather than
trusting whatever the dashboard validated when the post was written.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
import pytest

from bot_modules.announcements.buttons import (
    MAX_BUTTONS,
    AnnouncementRoleButton,
    build_announcement_view,
    button_label,
    resolve_style,
)

ROLE_ID = 555


@dataclass(order=True)
class _Role:
    position: int
    id: int = field(compare=False)
    name: str = field(default="Movie Night", compare=False)
    managed: bool = field(default=False, compare=False)
    perms: dict = field(default_factory=dict, compare=False)

    def __post_init__(self):
        self.permissions = SimpleNamespace(**self.perms)

    def is_default(self):
        return False


class _Guild:
    def __init__(self, roles=(), bot_top=100):
        self.id = 9001
        self._by_id = {r.id: r for r in roles}
        self.me = SimpleNamespace(top_role=_Role(position=bot_top, id=1, name="DK"))

    def get_role(self, rid):
        return self._by_id.get(rid)


class _Response:
    def __init__(self):
        self.messages = []

    async def send_message(self, content, **kwargs):
        self.messages.append((content, kwargs))


def _Member(roles=(), raise_on_change=None):
    """A member fake that satisfies the callback's ``isinstance`` guard.

    ``spec=discord.Member`` is what makes the isinstance check pass; the role
    mutations are hand-rolled so each test can assert on what was applied.
    """
    member = MagicMock(spec=discord.Member)
    member.roles = list(roles)
    member.added = []
    member.removed = []

    async def _add(role, reason=None):
        if raise_on_change is not None:
            raise raise_on_change
        member.added.append(role)
        member.roles.append(role)

    async def _remove(role, reason=None):
        if raise_on_change is not None:
            raise raise_on_change
        member.removed.append(role)
        member.roles = [r for r in member.roles if r.id != role.id]

    member.add_roles = _add
    member.remove_roles = _remove
    return member


class _Interaction:
    def __init__(self, guild, member):
        self.guild = guild
        self.user = member
        self.response = _Response()


def _btn_row(**over):
    row = {"role_id": ROLE_ID, "label": "", "emoji": "", "style": "primary"}
    row.update(over)
    return row


def _http_error(status=403):
    return discord.HTTPException(SimpleNamespace(status=status, reason="nope"), "boom")


def _click(member, roles=(), bot_top=100):
    guild = _Guild(roles, bot_top=bot_top)
    return _Interaction(guild, member), AnnouncementRoleButton(ROLE_ID)


# ── view builder ─────────────────────────────────────────────────────────────

def test_no_buttons_builds_no_view():
    # None is what discord.py reads as "no components" — see _process_due.
    assert build_announcement_view([], None) is None


def test_view_carries_one_item_per_button():
    guild = _Guild([_Role(position=5, id=ROLE_ID), _Role(position=5, id=556)])
    view = build_announcement_view(
        [_btn_row(), _btn_row(role_id=556, label="Bookworm")], guild
    )
    assert view is not None
    assert len(view.children) == 2
    assert view.timeout is None  # persistent


def test_view_is_capped_at_one_action_row():
    rows = [_btn_row(role_id=600 + i, label=f"R{i}") for i in range(MAX_BUTTONS + 3)]
    view = build_announcement_view(rows, None)
    assert view is not None
    assert len(view.children) == MAX_BUTTONS


def test_custom_id_embeds_the_role_id():
    view = build_announcement_view([_btn_row()], None)
    assert view.children[0].custom_id == f"ann_role:{ROLE_ID}"


def test_blank_label_falls_back_to_the_live_role_name():
    guild = _Guild([_Role(position=5, id=ROLE_ID, name="Movie Night")])
    assert button_label(_btn_row(), guild) == "Movie Night"


def test_blank_label_without_a_resolvable_role_has_a_generic_fallback():
    assert button_label(_btn_row(), None) == "Get role"


def test_configured_label_wins_over_the_role_name():
    guild = _Guild([_Role(position=5, id=ROLE_ID, name="Movie Night")])
    assert button_label(_btn_row(label="Join us"), guild) == "Join us"


def test_labels_are_truncated_to_discords_limit():
    assert len(button_label(_btn_row(label="x" * 200), None)) == 80


@pytest.mark.parametrize("name,expected", [
    ("primary", discord.ButtonStyle.primary),
    ("secondary", discord.ButtonStyle.secondary),
    ("success", discord.ButtonStyle.success),
    ("SUCCESS", discord.ButtonStyle.success),
    ("nonsense", discord.ButtonStyle.primary),
    (None, discord.ButtonStyle.primary),
])
def test_style_resolution(name, expected):
    assert resolve_style(name) == expected


# ── click path: grants ───────────────────────────────────────────────────────

async def test_click_grants_a_safe_role():
    role = _Role(position=5, id=ROLE_ID)
    member = _Member()
    interaction, button = _click(member, [role])

    await button.callback(interaction)

    assert member.added == [role]
    assert "You now have" in interaction.response.messages[0][0]
    assert interaction.response.messages[0][1]["ephemeral"] is True


async def test_second_click_removes_the_role():
    role = _Role(position=5, id=ROLE_ID)
    member = _Member([role])
    interaction, button = _click(member, [role])

    await button.callback(interaction)

    assert member.removed == [role]
    assert member.added == []
    assert "Removed" in interaction.response.messages[0][0]


async def test_deleted_role_is_reported_not_crashed():
    member = _Member()
    interaction, button = _click(member, [])  # role no longer in the guild

    await button.callback(interaction)

    assert member.added == []
    assert "isn't available" in interaction.response.messages[0][0]


async def test_click_outside_a_guild_is_refused():
    button = AnnouncementRoleButton(ROLE_ID)
    interaction = _Interaction(None, _Member())

    await button.callback(interaction)

    assert "only works in a server" in interaction.response.messages[0][0]


async def test_http_failure_is_reported_not_raised():
    role = _Role(position=5, id=ROLE_ID)
    member = _Member(raise_on_change=_http_error())
    interaction, button = _click(member, [role])

    await button.callback(interaction)

    assert member.added == []
    assert "couldn't change your roles" in interaction.response.messages[0][0]


# ── click path: the safety re-check ──────────────────────────────────────────

async def test_role_that_became_dangerous_is_no_longer_granted():
    # The whole point of re-checking: this role was safe when the announcement
    # was written and picked up ban_members afterwards.
    role = _Role(position=5, id=ROLE_ID, name="Mod", perms={"ban_members": True})
    member = _Member()
    interaction, button = _click(member, [role])

    await button.callback(interaction)

    assert member.added == []
    assert "isn't available" in interaction.response.messages[0][0]


async def test_role_that_rose_above_the_bot_is_no_longer_granted():
    role = _Role(position=200, id=ROLE_ID, name="Owner")
    member = _Member()
    interaction, button = _click(member, [role], bot_top=100)

    await button.callback(interaction)

    assert member.added == []
    assert "isn't available" in interaction.response.messages[0][0]


async def test_managed_role_is_no_longer_granted():
    role = _Role(position=5, id=ROLE_ID, name="Booster", managed=True)
    member = _Member()
    interaction, button = _click(member, [role])

    await button.callback(interaction)

    assert member.added == []


async def test_a_dangerous_role_can_still_be_shed():
    # Blocking removal would trap members in a role they didn't ask to keep;
    # only handing one out is gated.
    role = _Role(position=5, id=ROLE_ID, name="Mod", perms={"administrator": True})
    member = _Member([role])
    interaction, button = _click(member, [role])

    await button.callback(interaction)

    assert member.removed == [role]
    assert "Removed" in interaction.response.messages[0][0]


async def test_the_refusal_message_leaks_no_server_configuration():
    role = _Role(position=5, id=ROLE_ID, name="Secret Mod Role",
                 perms={"administrator": True})
    member = _Member()
    interaction, button = _click(member, [role])

    await button.callback(interaction)

    said = interaction.response.messages[0][0]
    assert "Secret Mod Role" not in said
    assert "administrator" not in said.lower()
