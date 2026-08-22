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


def test_member_panel_carries_my_settings():
    # A10: the settings container rides the play panel; assistance is its
    # first tenant. One wiring assertion — behavior is service/logic-tested.
    cog = _cog()
    view = mj_views.MemberPanelView(cog)
    labels = [c.label for c in view.children if isinstance(c, discord.ui.Button)]
    assert "My Settings" in labels


def test_my_settings_select_offers_every_mode_and_marks_current():
    from bot_modules.games.mahjong.mahjong_service import ASSIST_MODES

    view = mj_views.MySettingsView(_cog(), current="coach")
    select = next(c for c in view.children if isinstance(c, discord.ui.Select))
    assert [o.value for o in select.options] == list(ASSIST_MODES)
    assert [o.value for o in select.options if o.default] == ["coach"]


def test_assist_for_skips_the_service_outside_decision_phases():
    # F6: the pure gates run before the DB round-trip — a rack press on a
    # settled table must not touch the service at all. Counted, not raised:
    # _assist_for's never-raise contract swallows any exception a stub
    # throws, so an exploding stub passes on pre-gate code too (the verify
    # round proved it — the original version of this test was toothless).
    import asyncio

    from tests.test_mahjong_game_logic import play_state

    class _Counting:
        calls = 0

        async def assist_context(self, *a, **k):
            self.calls += 1
            return None

    cog = _cog()
    svc = _Counting()
    cog.service = svc  # type: ignore[assignment]
    state = play_state(2, {0: "9c*13", 1: "9c*13"})
    state.phase = Phase.SETTLE
    assert asyncio.run(cog._assist_for(1, state, 0)) is None
    assert svc.calls == 0  # the gate fired before any service work


def test_create_flow_offers_practice_when_the_dial_is_on():
    cog = _cog()
    view = mj_views.CreateTableView(cog, (1, 2), lambda c, s: 0, practice_open=True)
    labels = [b.label for b in view.children if isinstance(b, discord.ui.Button)]
    assert "Practice Duel" in labels and "Practice Table" in labels
    plain = mj_views.CreateTableView(cog, (1, 2), lambda c, s: 0)
    labels = [b.label for b in plain.children if isinstance(b, discord.ui.Button)]
    assert "Practice Duel" not in labels


def test_lobby_offers_add_bot_only_when_asked():
    cog = _cog()
    with_bot = mj_views.TableView(cog, 42, Phase.LOBBY, add_bot=True)
    ids = [b.custom_id for b in with_bot.children if isinstance(b, discord.ui.Button)]
    assert "mmj:42:add_bot" in ids
    without = mj_views.TableView(cog, 42, Phase.LOBBY)
    ids = [b.custom_id for b in without.children if isinstance(b, discord.ui.Button)]
    assert "mmj:42:add_bot" not in ids
    assert "add_bot" in mj_views.TableView.ACTIONS  # resume registration


def test_names_render_bots_as_flora():
    from bot_modules.games.mahjong.bot_logic import bot_member_id
    from bot_modules.games.mahjong.game_logic import GameState, SeatState, TableConfig

    cog = _cog()

    class _Guild:
        def get_member(self, member_id):  # a bot id must never reach here
            assert member_id > 0
            return None

    state = GameState(
        config=TableConfig(seat_count=2), host=100,
        seats=[SeatState(member_id=100),
               SeatState(member_id=bot_member_id(7, 1))],
    )
    names = cog._names(_Guild(), state)
    assert names[bot_member_id(7, 1)].startswith("🌱 ")
