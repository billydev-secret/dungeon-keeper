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
import sqlite3
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


# ── Regressions found in review of 94076e54 ──────────────────────────────


def test_dm_row_appears_without_explicit_role_ids(sync_db_path):
    """The DM dial works off default role *names* when no ids are configured.

    Gating the row on configured ids hid it — and a button that would have
    worked — on every guild that never set explicit ones.
    """
    with open_db(sync_db_path) as conn:
        states = _feature_states(conn, 1, _member(), _bot())
    assert states["dm_mode"].configured
    assert states["dm_mode"].actionable


def test_pen_pals_button_survives_a_deleted_gate_role(sync_db_path):
    """`_handle_join` only refuses when the gate role still resolves.

    A config pointing at a deleted role lets everyone join, so hiding the
    button from everyone would be stricter than the gate it mirrors.
    """
    member = _member()
    member.guild.get_role.return_value = None
    with open_db(sync_db_path) as conn:
        conn.execute(
            "INSERT INTO pen_pals_config (guild_id, enabled, opt_in_role_id) "
            "VALUES (?, 1, 999)",
            (1,),
        )
        conn.commit()
        states = _feature_states(conn, 1, member, _bot())
    assert states["pen_pals"].actionable


def test_one_failing_feature_does_not_cost_the_whole_card(sync_db_path, monkeypatch):
    """Seven features' internals means seven chances to raise.

    This runs after defer(), where the tree's error handler stays silent — so
    an escaping exception is a permanent "thinking…", not an error message.
    """
    import bot_modules.cogs.member_info_cog as mod

    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("no such table: wellness_config")

    monkeypatch.setattr(
        "bot_modules.services.wellness_service.get_wellness_config", _boom
    )
    with open_db(sync_db_path) as conn:
        states = mod._feature_states(conn, 1, _member(), _bot())
    assert "wellness" not in states
    # The features that didn't fail still made it onto the card.
    assert states["birthday"].configured
    assert states["no_contact"].configured


def test_viewable_ids_include_threads():
    """Thread messages are stored under the thread's own id, so the filter
    must know about threads or it drops every one of them."""
    from bot_modules.cogs.member_info_cog import _viewable_channel_ids

    def _chan(cid, visible=True):
        c = MagicMock()
        c.id = cid
        c.permissions_for.return_value.view_channel = visible
        return c

    guild = MagicMock()
    guild.channels = [_chan(1), _chan(2, visible=False)]
    guild.threads = [_chan(10), _chan(11, visible=False)]
    assert _viewable_channel_ids(guild, _member()) == {1, 10}


def test_an_unimportable_feature_module_costs_only_its_own_row(sync_db_path):
    """Lazy imports were still outside the guards, so one bad module took the
    whole card — the outcome the per-feature guarding exists to prevent."""
    import builtins

    import bot_modules.cogs.member_info_cog as mod

    real_import = builtins.__import__

    def _fail_wellness(name, *args, **kwargs):
        if name == "bot_modules.services.wellness_service":
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    with open_db(sync_db_path) as conn:
        builtins.__import__ = _fail_wellness
        try:
            states = mod._feature_states(conn, 1, _member(), _bot())
        finally:
            builtins.__import__ = real_import

    assert "wellness" not in states
    assert states["birthday"].configured
    assert states["no_contact"].configured


def test_pen_pals_info_panel_source_is_labelled_on_the_dashboard():
    """The pool-activity panel renders `REASONS[v] || v`, so an unlabelled
    source shows moderators a raw identifier among readable sentences."""
    from pathlib import Path as _Path

    from bot_modules.member_info.views import PEN_PALS_SOURCE

    panel = _Path("src/web_server/static/js/panels/pen-pals-pool-activity.js")
    if not panel.exists():  # partial checkout on the remote runner
        pytest.skip("web_server assets not present in this checkout")
    assert f"{PEN_PALS_SOURCE}:" in panel.read_text(encoding="utf-8")


def test_every_chart_renderer_is_serialized():
    """All of them share pyplot's global figure registry; a partially covered
    lock protects nothing the moment one of the others gets a caller."""
    import inspect

    from bot_modules.services import activity_graphs

    for name, fn in inspect.getmembers(activity_graphs, inspect.isfunction):
        if name.startswith("render_"):
            assert getattr(fn, "__wrapped__", None) is not None, name


def test_the_render_lock_survives_a_nested_render():
    """`render_nsfw_gender_line_chart` delegates to `render_nsfw_gender_chart`,
    which a non-reentrant lock would deadlock on."""
    import threading

    from bot_modules.services.activity_graphs import _RENDER_LOCK

    with _RENDER_LOCK:
        acquired = _RENDER_LOCK.acquire(timeout=2)
        assert acquired, "render lock is not reentrant — nested render deadlocks"
        _RENDER_LOCK.release()
    assert isinstance(_RENDER_LOCK, type(threading.RLock()))


# ── Streak, and the "More" field's gating ────────────────────────────────


def test_streak_summary_reads_zeros_for_an_unseen_member(sync_db_path):
    from bot_modules.services.economy_service import get_streak_summary

    with open_db(sync_db_path) as conn:
        assert get_streak_summary(conn, 1, 42) == (0, 0)


def test_streak_summary_reads_the_row(sync_db_path):
    from bot_modules.services.economy_service import get_streak_summary

    with open_db(sync_db_path) as conn:
        conn.execute(
            "INSERT INTO econ_streaks (guild_id, user_id, current_streak, "
            "longest_streak) VALUES (?, ?, ?, ?)",
            (1, 42, 12, 31),
        )
        conn.commit()
        assert get_streak_summary(conn, 1, 42) == (12, 31)


def test_help_lines_name_nothing_when_no_cog_is_loaded():
    """Same rule as the opt-in rows: never name a command this server
    doesn't run."""
    import bot_modules.cogs.member_info_cog as mod

    cog = mod.MemberInfoCog.__new__(mod.MemberInfoCog)
    cog.bot = _bot(cogs_loaded=False)
    assert cog._help_lines("") == []


def test_help_lines_use_the_guilds_own_assistant_name():
    """The assistant's name is per-guild branding; the default must never be
    baked into the copy."""
    import bot_modules.cogs.member_info_cog as mod

    cog = mod.MemberInfoCog.__new__(mod.MemberInfoCog)
    cog.bot = _bot(cogs_loaded=True)
    lines = cog._help_lines("Meadow-bot")
    assert any("Meadow-bot" in ln for ln in lines)
    assert not any("Billy-bot" in ln for ln in lines)
