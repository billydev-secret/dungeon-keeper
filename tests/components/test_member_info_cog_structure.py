"""Wiring assertions for the ``/info`` panel that the logic tests can't reach.

Deliberately thin (CLAUDE.md: cogs are glue, exercised through the logic
layer). What's here is only what lives *in* the glue: that the panel is
ephemeral, that the view builds one button per actionable row and none for
the rest, and that every button routes to a real handler — a typo in the
runner table would otherwise surface as a dead button in production.
"""

from __future__ import annotations

import asyncio
import importlib
from unittest.mock import MagicMock

import pytest

from bot_modules.cogs.member_info_cog import _feature_states
from bot_modules.core.db_utils import open_db

from bot_modules.member_info.logic import FeatureState, build_optin_rows
from bot_modules.member_info.views import _RUNNERS, MemberInfoView

ALL_KEYS = ("pen_pals", "whispers", "guess", "dm_mode", "wellness", "birthday", "no_contact")


def test_module_imports_and_registers_setup():
    mod = importlib.import_module("bot_modules.cogs.member_info_cog")
    assert asyncio.iscoroutinefunction(mod.setup)
    assert hasattr(mod, "MemberInfoCog")


def test_info_command_is_registered():
    mod = importlib.import_module("bot_modules.cogs.member_info_cog")
    names = [c.name for c in mod.MemberInfoCog.__cog_app_commands__]
    assert "info" in names


def test_info_command_is_always_ephemeral():
    """The card is the caller's own data; it must never post to a channel."""
    import inspect

    mod = importlib.import_module("bot_modules.cogs.member_info_cog")
    source = inspect.getsource(mod.MemberInfoCog)
    assert "ephemeral=True" in source
    assert "ephemeral=False" not in source


def test_cog_is_registered_for_loading():
    """A cog that isn't in the extension list ships as dead code."""
    from pathlib import Path

    main = Path("src/dungeonkeeper/__main__.py")
    if not main.exists():  # partial checkout on the remote runner
        pytest.skip("src/dungeonkeeper not present in this checkout")
    assert "bot_modules.cogs.member_info_cog" in main.read_text(encoding="utf-8")


@pytest.mark.parametrize("key", ALL_KEYS)
def test_every_feature_key_has_a_runner(key):
    """The logic table and the view's dispatch table must not drift apart."""
    assert key in _RUNNERS


def test_view_builds_one_button_per_actionable_row():
    rows = build_optin_rows({key: FeatureState(configured=True) for key in ALL_KEYS})
    view = MemberInfoView(object(), rows)
    assert len(view.children) == len(rows)


def test_view_builds_no_buttons_for_unreachable_flows():
    rows = build_optin_rows(
        {key: FeatureState(configured=True, actionable=False) for key in ALL_KEYS}
    )
    view = MemberInfoView(object(), rows)
    assert view.children == []


def test_view_button_custom_ids_are_unique():
    rows = build_optin_rows({key: FeatureState(configured=True) for key in ALL_KEYS})
    view = MemberInfoView(object(), rows)
    ids = [child.custom_id for child in view.children]
    assert len(set(ids)) == len(ids)


# ── The gathering queries, against the real schema ───────────────────────
# The logic tests take state as an argument, so nothing there executes a
# query. These two run `_feature_states` against a migrated database, which
# is what catches a renamed column or a moved helper — the failure mode that
# would otherwise reach production as an empty panel.


def _member(user_id: int = 42, role_ids: tuple[int, ...] = ()):
    member = MagicMock()
    member.id = user_id
    member.roles = [MagicMock(id=rid) for rid in role_ids]
    return member


def _bot(cogs_loaded: bool = True):
    bot = MagicMock()
    bot.get_cog.return_value = object() if cogs_loaded else None
    return bot


def test_feature_states_on_an_unconfigured_guild_offers_nothing(sync_db_path):
    """A guild with nothing set up gets a card with no opt-in rows at all."""
    with open_db(sync_db_path) as conn:
        states = _feature_states(conn, 1, _member(), _bot(cogs_loaded=False))
    assert states == {}


def test_feature_states_reads_pen_pals_config(sync_db_path):
    with open_db(sync_db_path) as conn:
        conn.execute(
            "INSERT INTO pen_pals_config (guild_id, enabled, opt_in_role_id) "
            "VALUES (?, 1, 0)",
            (1,),
        )
        conn.commit()
        states = _feature_states(conn, 1, _member(), _bot())
    assert states["pen_pals"].configured
    assert states["pen_pals"].state == "unset"


def test_pen_pals_button_withheld_when_the_member_lacks_the_gate_role(sync_db_path):
    """Configured, but Join would be refused — status yes, button no."""
    with open_db(sync_db_path) as conn:
        conn.execute(
            "INSERT INTO pen_pals_config (guild_id, enabled, opt_in_role_id) "
            "VALUES (?, 1, 999)",
            (1,),
        )
        conn.commit()
        without = _feature_states(conn, 1, _member(), _bot())
        holding = _feature_states(conn, 1, _member(role_ids=(999,)), _bot())
    assert without["pen_pals"].configured and not without["pen_pals"].actionable
    assert holding["pen_pals"].actionable
