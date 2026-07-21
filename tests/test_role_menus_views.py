"""Interaction-path tests for role menus (views.py) with fake Discord objects.

Covers the seams the pure engine can't: guard ordering (enabled/required/
cooldown), role application + grant history, and the once-per-menu
degradation alert.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.role_menus import db as menus_db
from bot_modules.role_menus.views import handle_menu_interaction
from migrations import apply_migrations_sync
from tests.fakes import fake_interaction

GUILD_ID = 123
MEMBER_ID = 555


@dataclass(order=True)
class Role:
    position: int
    id: int = field(compare=False)
    name: str = field(default="Role", compare=False)

    @property
    def mention(self) -> str:
        return f"<@&{self.id}>"


class Ctx:
    """Just enough AppContext for the interaction path."""

    def __init__(self, db_path, mod_channel_id=0):
        self.db_path = db_path
        self._mod_channel_id = mod_channel_id

    def open_db(self):
        return open_db(self.db_path)

    def guild_config(self, _guild_id):
        return SimpleNamespace(mod_channel_id=self._mod_channel_id)


class Guild:
    def __init__(self, roles, mod_channel=None):
        self.id = GUILD_ID
        self._roles = {r.id: r for r in roles}
        self._mod_channel = mod_channel
        top = Role(position=100, id=1, name="DK")
        self.me = SimpleNamespace(
            guild_permissions=SimpleNamespace(manage_roles=True), top_role=top
        )

    def get_role(self, rid):
        return self._roles.get(rid)

    def get_channel(self, _cid):
        return self._mod_channel


def make_member(*roles):
    member = MagicMock(spec=discord.Member)
    member.id = MEMBER_ID
    member.mention = f"<@{MEMBER_ID}>"
    member.roles = list(roles)
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    return member


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "views.db"
    apply_migrations_sync(path)
    return path


def seed_menu(db_path, *, mode="toggle", enabled=True, required_role_id=0,
              role_ids=(11, 22)) -> int:
    with open_db(db_path) as conn:
        mid = menus_db.create_menu(conn, GUILD_ID, "Colors", 1, time.time())
        menus_db.update_menu(
            conn, mid, title="Colors", description="", accent="",
            thumbnail_url="", style="buttons", mode=mode, max_roles=0,
            required_role_id=required_role_id, cooldown_seconds=0,
            placeholder="", user_id=1, now=time.time(),
        )
        menus_db.replace_options(
            conn, mid,
            [{"role_id": rid, "label": f"r{rid}"} for rid in role_ids],
            time.time(),
        )
        if not enabled:
            menus_db.set_menu_enabled(conn, mid, False, time.time())
        return mid


def make_interaction(ctx, guild, member):
    # message=None: the post-selection view reset is Discord-surface behavior,
    # skipped when there's no live message to edit.
    return fake_interaction(
        user=member, guild=guild, client=SimpleNamespace(ctx=ctx), message=None
    )


def _sent_text(interaction) -> str:
    assert interaction.followup.send.await_count == 1
    return interaction.followup.send.await_args.args[0]


async def test_toggle_click_grants_and_records(db_path):
    ctx = Ctx(db_path)
    red = Role(position=5, id=11, name="Red")
    guild = Guild([red, Role(position=6, id=22, name="Blue")])
    member = make_member()
    mid = seed_menu(db_path)
    interaction = make_interaction(ctx, guild, member)

    await handle_menu_interaction(interaction, mid, clicked_role_id=11)

    member.add_roles.assert_awaited_once()
    assert member.add_roles.await_args.args == (red,)
    assert "You now have" in _sent_text(interaction)
    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT role_id, action FROM role_menu_grants WHERE menu_id = ?", (mid,)
        ).fetchall()
    assert [(r["role_id"], r["action"]) for r in rows] == [(11, "grant")]


async def test_disabled_menu_rejects_politely(db_path):
    ctx = Ctx(db_path)
    guild = Guild([Role(position=5, id=11, name="Red")])
    member = make_member()
    mid = seed_menu(db_path, enabled=False)
    interaction = make_interaction(ctx, guild, member)

    await handle_menu_interaction(interaction, mid, clicked_role_id=11)

    member.add_roles.assert_not_awaited()
    assert "turned off" in _sent_text(interaction)


async def test_required_role_gates_the_menu(db_path):
    ctx = Ctx(db_path)
    verified = Role(position=9, id=99, name="Verified")
    guild = Guild([Role(position=5, id=11, name="Red"), verified])
    member = make_member()  # doesn't hold Verified
    mid = seed_menu(db_path, required_role_id=99)
    interaction = make_interaction(ctx, guild, member)

    await handle_menu_interaction(interaction, mid, clicked_role_id=11)

    member.add_roles.assert_not_awaited()
    assert "requires the **@Verified** role" in _sent_text(interaction)


async def test_binding_second_pick_is_permanent(db_path):
    ctx = Ctx(db_path)
    red = Role(position=5, id=11, name="Red")
    blue = Role(position=6, id=22, name="Blue")
    guild = Guild([red, blue])
    mid = seed_menu(db_path, mode="binding")

    member = make_member()
    first = make_interaction(ctx, guild, member)
    await handle_menu_interaction(first, mid, clicked_role_id=11)
    assert "locked in" in _sent_text(first)

    second = make_interaction(ctx, guild, make_member(red))
    await handle_menu_interaction(second, mid, clicked_role_id=22)
    assert "permanent" in _sent_text(second)


async def test_missing_role_alerts_mods_once(db_path):
    mod_channel = MagicMock(spec=discord.TextChannel)
    mod_channel.send = AsyncMock()
    ctx = Ctx(db_path, mod_channel_id=800)
    # Role 11 is configured in the menu but deleted from the guild.
    guild = Guild([Role(position=6, id=22, name="Blue")], mod_channel=mod_channel)
    mid = seed_menu(db_path)

    first = make_interaction(ctx, guild, make_member())
    await handle_menu_interaction(first, mid, clicked_role_id=11)
    assert "isn't available anymore" in _sent_text(first)
    assert mod_channel.send.await_count == 1
    assert "misconfigured" in mod_channel.send.await_args.args[0]

    # Second member hits the same wall — no second alert (spec §4).
    second = make_interaction(ctx, guild, make_member())
    await handle_menu_interaction(second, mid, clicked_role_id=11)
    assert "isn't available anymore" in _sent_text(second)
    assert mod_channel.send.await_count == 1


async def test_selection_updates_roles_to_match(db_path):
    ctx = Ctx(db_path)
    red = Role(position=5, id=11, name="Red")
    blue = Role(position=6, id=22, name="Blue")
    guild = Guild([red, blue])
    member = make_member(red)  # holds Red, submits {Blue}
    mid = seed_menu(db_path)
    interaction = make_interaction(ctx, guild, member)

    await handle_menu_interaction(interaction, mid, selected=[22])

    assert member.add_roles.await_args.args == (blue,)
    assert member.remove_roles.await_args.args == (red,)
    text = _sent_text(interaction)
    assert "+Blue" in text and "−Red" in text
