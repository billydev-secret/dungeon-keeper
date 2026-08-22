"""Wiring assertions for the mahjong cog — glue only (stage 6).

Per CLAUDE.md, rules live in the logic layer and are tested there; this
file pins only what the glue itself owns: the module loads, the command
exists, the extension is registered, custom_ids are stable, and the
private-event → ephemeral-copy mapping."""

from __future__ import annotations

from pathlib import Path

import discord

from bot_modules.cogs.mahjong_cog import MahjongCog
from bot_modules.games.mahjong import views as mj_views
from bot_modules.games.mahjong.game_logic import Phase


class _StubCtx:
    db_path = Path("/nonexistent/mahjong-wiring.db")


class _StubBot:
    ctx = _StubCtx()

    def add_view(self, *a, **k):  # registered views on resume
        pass


def _cog() -> MahjongCog:
    return MahjongCog.__new__(MahjongCog)  # no loop side effects


def test_slash_command_registered():
    cmds = [
        attr for attr in vars(MahjongCog).values()
        if isinstance(attr, discord.app_commands.Command)
    ]
    assert [c.name for c in cmds] == ["mahjong"]


def test_extension_is_in_the_boot_list():
    main = Path(__file__).resolve().parent.parent / "src/dungeonkeeper/__main__.py"
    assert '"bot_modules.cogs.mahjong_cog",' in main.read_text(encoding="utf-8")


def test_table_view_custom_ids_are_stable_per_phase():
    cog = _cog()
    seen: dict[Phase, list[str]] = {}
    for phase in Phase:
        view = mj_views.TableView(cog, 42, phase)
        seen[phase] = [
            item.custom_id for item in view.children
            if isinstance(item, discord.ui.Button)
        ]
        assert all(cid and cid.startswith("mmj:42:") for cid in seen[phase])
    assert "mmj:42:join" in seen[Phase.LOBBY]
    assert "mmj:42:claim_mj" in seen[Phase.CLAIM_WINDOW]
    assert "mmj:42:rematch" in seen[Phase.SETTLE]
    assert seen[Phase.CLOSED] == []
    # every play phase keeps the rack reachable — the §6.4 refresh fallback
    for phase in (Phase.CHARLESTON, Phase.CHARLESTON_VOTE, Phase.COURTESY_PROPOSE,
                  Phase.COURTESY_PICK, Phase.AWAIT_DISCARD, Phase.CLAIM_WINDOW):
        assert "mmj:42:rack" in seen[phase]


def test_register_all_view_answers_every_action():
    """Resume registers a superset view: whatever phase the on-disk sticky
    message carries, its buttons route instead of dying unregistered."""
    cog = _cog()
    view = mj_views.TableView(cog, 7, register_all=True)
    ids = {b.custom_id for b in view.children if isinstance(b, discord.ui.Button)}
    assert ids == {f"mmj:7:{a}" for a in mj_views.TableView.ACTIONS}
    # and every phase-built button is inside that superset
    for phase in Phase:
        for b in mj_views.TableView(cog, 7, phase).children:
            if isinstance(b, discord.ui.Button):
                assert b.custom_id in ids


def test_private_events_map_to_member_facing_copy():
    cog = _cog()
    note = cog._private_note(1, [
        ("claim_recorded", {"seat": 0}),
        ("claim_downgraded", {"seat": 0, "why": "invalid_mahjong", "private": True}),
    ])
    assert note is not None and "Nobody else saw this" in note
    note = cog._private_note(1, [
        ("claim_downgraded", {"seat": 0, "why": "pair_call", "private": True}),
    ])
    assert note is not None and "Pass" in note
    assert cog._private_note(1, [("claim_recorded", {"seat": 0})]) is None
    assert cog._private_note(1, []) is None
