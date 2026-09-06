"""Wiring assertions for the mahjong cog — glue only (stage 6).

Per CLAUDE.md, rules live in the logic layer and are tested there; this
file pins only what the glue itself owns: the module loads, the command
exists, the extension is registered, custom_ids are stable, and the
private-event → ephemeral-copy mapping."""

from __future__ import annotations

from pathlib import Path

import discord
import pytest

from bot_modules.cogs.mahjong_cog import MahjongCog
from bot_modules.games.mahjong import views as mj_views
from bot_modules.games.mahjong.game_logic import AUTO_PASS, Phase
from bot_modules.games.mahjong.mahjong_service import NudgeRecord


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


def test_member_panel_carries_how_to_play():
    view = mj_views.MemberPanelView(_cog())
    labels = [c.label for c in view.children if isinstance(c, discord.ui.Button)]
    assert "How to Play" in labels
    # Discord allows five buttons per action row — the panel is now full,
    # so a sixth entry must start a row rather than silently fail to render
    assert len(labels) <= 5


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


def test_rack_watch_edits_fresh_and_drops_dead_and_expired():
    # Live rack refresh (glue): a stored ephemeral panel is edited on a
    # transition; an expired token and a dead (dismissed) panel drop out.
    import asyncio
    import time as _time

    from tests.test_mahjong_game_logic import play_state

    class _Msg:
        def __init__(self, dead=False):
            self.edits = 0
            self.dead = dead

        async def edit(self, **kw):
            if self.dead:
                raise discord.HTTPException(
                    type("R", (), {"status": 404, "reason": "gone"})(), "gone")
            self.edits += 1

    class _Svc:
        async def assist_context(self, *a, **k):
            return None

    cog = _cog()
    cog.rack_watch = {}
    cog.service = _Svc()  # type: ignore[assignment]
    state = play_state(2, {0: "9c*13", 1: "8b*13"})
    human = state.seats[0].member_id
    fresh, dead = _Msg(), _Msg(dead=True)
    cog.rack_watch[(7, human)] = (fresh, _time.time())
    cog.rack_watch[(7, 424242)] = (dead, _time.time())        # not seated → drop
    cog.rack_watch[(7, state.seats[1].member_id)] = (
        _Msg(), _time.time() - 20 * 60)                        # expired → drop
    cog.rack_watch[(8, human)] = (_Msg(), _time.time())        # other table → kept
    meta = {"status": "live", "deadline_at": None}
    names = {human: "You", state.seats[1].member_id: "Them"}
    asyncio.run(cog._refresh_rack_watches(7, state, meta, names))
    assert fresh.edits == 1
    assert (7, human) in cog.rack_watch
    assert (7, 424242) not in cog.rack_watch
    assert (7, state.seats[1].member_id) not in cog.rack_watch
    assert (8, human) in cog.rack_watch


def test_rack_watch_dead_panel_dropped_on_edit_failure():
    import asyncio
    import time as _time

    from tests.test_mahjong_game_logic import play_state

    class _DeadMsg:
        async def edit(self, **kw):
            raise discord.HTTPException(
                type("R", (), {"status": 401, "reason": "expired"})(), "expired")

    class _Svc:
        async def assist_context(self, *a, **k):
            return None

    cog = _cog()
    cog.rack_watch = {}
    cog.service = _Svc()  # type: ignore[assignment]
    state = play_state(2, {0: "9c*13", 1: "8b*13"})
    human = state.seats[0].member_id
    cog.rack_watch[(7, human)] = (_DeadMsg(), _time.time())
    meta = {"status": "live", "deadline_at": None}
    asyncio.run(cog._refresh_rack_watches(7, state, meta, {human: "You"}))
    assert cog.rack_watch == {}


def test_rack_context_reports_the_seats_own_status_every_phase():
    # The live panel IS the confirmation, so every phase must say what this
    # seat has already done — otherwise suppressing the per-tap ephemeral
    # would lose information.
    from bot_modules.games.mahjong.game_logic import Phase as P
    from bot_modules.games.mahjong.tiles import Tile
    from tests.test_mahjong_game_logic import play_state

    cog = _cog()
    names = {100: "You", 101: "Them"}
    state = play_state(2, {0: "9c*13", 1: "8b*13"})

    state.phase = P.CHARLESTON_VOTE
    assert "Vote on the table card" in cog._rack_context(state, 0, names)
    state.votes = {0: True}
    assert "Your vote (yes) is in" in cog._rack_context(state, 0, names)

    state.phase = P.COURTESY_PROPOSE
    assert "Offer 0–3" in cog._rack_context(state, 0, names)
    state.proposals = {0: 2}
    assert "You offered 2" in cog._rack_context(state, 0, names)

    state.phase = P.CLAIM_WINDOW
    state.live_discarder = 1
    assert "claim from the table card" in cog._rack_context(state, 0, names)
    state.claims = {0: ("pass", [])}
    assert "You passed" in cog._rack_context(state, 0, names)
    state.claims = {0: ("call", [Tile("9c")])}
    assert "You called" in cog._rack_context(state, 0, names)
    state.live_discarder = 0
    state.claims = {}
    assert "Your discard is live" in cog._rack_context(state, 0, names)

    state.phase = P.SETTLE
    assert "Hand over" in cog._rack_context(state, 0, names)
    state.rematch_votes = {0}
    assert "Rematch vote in" in cog._rack_context(state, 0, names)


def test_confirm_suppresses_routine_notes_only_behind_a_live_panel():
    import asyncio
    import time as _time

    sent: list[str] = []

    class _Followup:
        async def send(self, text, **kw):
            sent.append(text)

    class _User:
        id = 100

    class _Interaction:
        user = _User()
        followup = _Followup()

    cog = _cog()
    cog.rack_watch = {}
    inter = _Interaction()

    # no live panel → the routine confirmation is the only feedback
    asyncio.run(cog._confirm(inter, 7, [], "Pass is in."))
    assert sent == ["Pass is in."]

    # live panel → suppressed; the panel's Now line says it
    sent.clear()
    cog.rack_watch[(7, 100)] = (object(), _time.time())
    asyncio.run(cog._confirm(inter, 7, [], "Pass is in."))
    assert sent == []

    # an EXPIRED panel is not live → confirmation returns
    sent.clear()
    cog.rack_watch[(7, 100)] = (object(), _time.time() - 20 * 60)
    asyncio.run(cog._confirm(inter, 7, [], "Pass is in."))
    assert sent == ["Pass is in."]

    # a private note always lands, live panel or not
    sent.clear()
    cog.rack_watch[(7, 100)] = (object(), _time.time())
    events = [("claim_downgraded",
               {"seat": 0, "why": "invalid_mahjong", "private": True})]
    asyncio.run(cog._confirm(inter, 7, events, "Pass is in."))
    assert len(sent) == 1 and "Pass is in." not in sent[0]


def test_tile_menus_use_the_registered_faces_and_fall_back_to_chips():
    # The pass/discard/courtesy menus carry the tile art once registration
    # has run; before it, the chip stays in the label so an option is never
    # unidentifiable (§7.2's launch state).
    from bot_modules.games.mahjong import tile_render, views as v
    from bot_modules.games.mahjong.tiles import Tile

    rack = [Tile("5b"), Tile("5b"), Tile.JOKER, Tile.FLOWER]

    tile_render._map_cache = {}                    # unregistered
    try:
        plain = v._tile_options(rack)
        assert all(o.emoji is None for o in plain)
        assert plain[0].label.startswith("5B — ")
        # duplicate kinds stay separately selectable
        assert len({o.value for o in plain}) == len(rack)

        tile_render._map_cache = {t.code: 1400000000000000001 for t in Tile}
        rich = v._tile_options(rack)
        assert all(o.emoji is not None for o in rich)
        assert rich[0].emoji.name == "mm_5b"       # type: ignore[union-attr]
        assert rich[0].label == "5 Bam"            # face carries the picture
        assert [o.value for o in rich] == [o.value for o in plain]
    finally:
        tile_render._map_cache = None              # back to the real map


def test_redeem_menu_carries_the_face_too():
    from bot_modules.games.mahjong import tile_render, views as v
    from bot_modules.games.mahjong.game_logic import ExposureState
    from bot_modules.games.mahjong.tiles import Tile
    from tests.test_mahjong_game_logic import play_state

    state = play_state(
        2, {0: "9d 9c*12", 1: "8b*13"},
        exposures={1: [ExposureState(exposure_id=3, natural=Tile("9d"),
                                     count=4, jokers=1)]},
    )
    tile_render._map_cache = {t.code: 1400000000000000001 for t in Tile}
    try:
        view = v.RedeemView(_cog(), 7, state, 0)
        select = next(c for c in view.children
                      if isinstance(c, discord.ui.Select))
        assert select.options[0].emoji is not None
        assert "exposure #3" in select.options[0].label
    finally:
        tile_render._map_cache = None


@pytest.mark.parametrize("kind, copy", [
    pytest.param("pass", "You passed", id="pass"),
    pytest.param("call", "You called", id="call"),
    pytest.param("mahjong", "You declared Mahjong", id="mahjong"),
    pytest.param(AUTO_PASS, "Nothing to claim here", id="auto_pass"),
])
def test_rack_context_claim_window_copy_per_response(kind, copy):
    # mahjong-146: the auto-pass row used to print the raw enum
    # ("You auto_pass — waiting on the window.") — the Now line most seats
    # see on most windows.
    from tests.test_mahjong_game_logic import play_state

    cog = _cog()
    state = play_state(2, {0: "9c*13", 1: "8b*13"})
    state.phase = Phase.CLAIM_WINDOW
    state.live_discarder = 1
    state.claims = {0: (kind, [])}
    line = cog._rack_context(state, 0, {100: "You", 101: "Them"})
    assert line is not None and copy in line
    assert "auto_pass" not in line


def test_turn_nudges_post_delete_and_never_reping_the_same_turn():
    # mahjong-144: a plain content ping (never an embed) on each human turn
    # start, allowed_mentions restricted to that member, deleted on the next
    # transition; the second strike gets its own warning line. Every id is
    # mirrored onto the table row so a restart can still sweep them.
    import asyncio

    from tests.test_mahjong_game_logic import play_state

    class _Msg:
        _next = iter(range(9000, 9999))

        def __init__(self):
            self.id = next(_Msg._next)
            self.deleted = False

        async def delete(self):
            self.deleted = True

    class _Channel:
        def __init__(self):
            self.sent: list[tuple[str, discord.AllowedMentions, _Msg]] = []

        async def send(self, content, *, allowed_mentions, **kw):
            assert "embed" not in kw
            msg = _Msg()
            self.sent.append((content, allowed_mentions, msg))
            return msg

        def get_partial_message(self, message_id):
            for _, _, msg in self.sent:
                if msg.id == message_id:
                    return msg
            raise AssertionError(f"deleted an unknown message {message_id}")

    class _Service:
        """Stands in for the row: what the cog last persisted."""

        def __init__(self):
            self.saved: NudgeRecord | None = None

        async def set_nudges(self, table_id, record):
            self.saved = NudgeRecord.from_json(
                None if record.is_empty() else record.to_json())

        async def get_nudges(self, table_id):
            return self.saved or NudgeRecord()

    cog = _cog()
    cog.nudges = {}
    cog.service = _Service()  # type: ignore[assignment]
    channel = _Channel()
    state = play_state(2, {0: "9c*13", 1: "8b*13"}, turn=0)

    asyncio.run(cog._post_nudges(channel, 7, state, [("tile_drawn", {"seat": 0})]))
    content, mentions, first = channel.sent[-1]
    assert content == "<@100> — your draw."
    assert [u.id for u in mentions.users] == [100]  # type: ignore[union-attr]
    assert mentions.everyone is False and mentions.roles is False
    # and it reached the row, not just the cog
    assert cog.service.saved is not None  # type: ignore[attr-defined]
    assert cog.service.saved.draws == [first.id]  # type: ignore[attr-defined]

    # same turn again (a redeem, an assist refresh) → nothing new, nothing gone
    asyncio.run(cog._post_nudges(channel, 7, state, []))
    assert len(channel.sent) == 1 and not first.deleted

    # seat 0 times out on its second strike: the auto-discard opens a claim
    # window (no turn), and the warning is posted there
    struck = play_state(2, {0: "9c*13", 1: "8b*13"}, turn=0)
    struck.phase = Phase.CLAIM_WINDOW
    struck.seats[0].strikes = 2
    asyncio.run(cog._post_nudges(
        channel, 7, struck, [("strike", {"seat": 0, "strikes": 2})]))
    _, _, warning = channel.sent[-1]
    assert channel.sent[-1][0] == "<@100> — one more missed turn and your seat folds."
    assert cog.service.saved.warnings == {100: warning.id}  # type: ignore[attr-defined]

    # next turn → the old draw line goes, the next seat gets its own — and
    # the warning STAYS: the member it names is the one not looking, and a
    # line that lived only through the claim window warned nobody
    nxt = play_state(2, {0: "9c*13", 1: "8b*13"}, turn=1)
    nxt.discard_count = 1
    nxt.seats[0].strikes = 2
    asyncio.run(cog._post_nudges(channel, 7, nxt, [("tile_drawn", {"seat": 1})]))
    assert first.deleted and not warning.deleted
    assert channel.sent[-1][0] == "<@101> — your draw."

    # a timely act resets the strikes → the warning is no longer true → gone
    acted = play_state(2, {0: "9c*13", 1: "8b*13"}, turn=1)
    acted.discard_count = 2
    asyncio.run(cog._post_nudges(channel, 7, acted, [("tile_drawn", {"seat": 1})]))
    assert warning.deleted

    # the table closes → every nudge is swept, and the row goes back to empty
    asyncio.run(cog._post_nudges(
        channel, 7, struck, [("strike", {"seat": 0, "strikes": 2})]))
    asyncio.run(cog._clear_nudges(channel, 7))
    assert all(m.deleted for _, _, m in channel.sent)
    assert 7 not in cog.nudges
    assert cog.service.saved.is_empty()  # type: ignore[attr-defined]


def test_a_restart_mid_hand_still_sweeps_the_stale_turn_ping():
    # ship review: the ids lived only on the cog, so a restart orphaned the
    # "your draw" line — the resumed process re-armed the table but could no
    # longer delete a ping for a turn that had already passed.
    import asyncio

    deleted: list[int] = []

    class _Channel:
        async def send(self, content, *, allowed_mentions, **kw):
            raise AssertionError("this half of the test posts nothing")

        def get_partial_message(self, message_id):
            class _P:
                async def delete(_self):
                    deleted.append(message_id)
            return _P()

    class _Service:
        def __init__(self, record):
            self.record = record

        async def get_nudges(self, table_id):
            return self.record

        async def set_nudges(self, table_id, record):
            self.record = record

    # what the pre-restart process left on the row: turn 0's ping
    stored = NudgeRecord(turn_key=[1, 0, 0, "wall"], draws=[555])
    cog = _cog()
    cog.nudges = {}
    cog.service = _Service(stored)  # type: ignore[assignment]
    channel = _Channel()

    # the resumed cog reads the row back …
    cog.nudges[7] = asyncio.run(cog.service.get_nudges(7))  # type: ignore[attr-defined]
    # … and the table closing sweeps the stale ping it never posted itself
    asyncio.run(cog._clear_nudges(channel, 7))
    assert deleted == [555]
    assert cog.service.record.is_empty()  # type: ignore[attr-defined]


def test_table_sticky_holds_its_restick_through_a_claim_window():
    # mahjong-154: chat in the first seconds of a claim window used to
    # delete-and-repost the card — with the only Mahjong/Call/Pass buttons —
    # mid-window. The panel now waits for the window to resolve.
    import asyncio

    cog = _cog()
    cog.bot = _StubBot()  # type: ignore[assignment]
    cog.panels = {}
    cog.channel_tables = {}
    cog.table_phase = {}
    cog._track_table(7, 5000)
    panel = cog.panels[7]
    assert panel._hold is not None
    assert asyncio.run(panel._hold(900)) is False
    cog.table_phase[7] = Phase.CLAIM_WINDOW
    assert asyncio.run(panel._hold(900)) is True
    cog.table_phase[7] = Phase.AWAIT_DISCARD
    assert asyncio.run(panel._hold(900)) is False
    # the window is seconds long; a 15 s re-check would outlast it
    assert panel._hold_poll <= 3.0


def test_create_flow_offers_quick_practice_when_the_house_opened_it():
    # mahjong-147: the quick deck leads the practice row, on its own row
    cog = _cog()
    view = mj_views.CreateTableView(
        cog, (1, 2), lambda c, s: 0, practice_open=True, short_rank=5)
    buttons = [b for b in view.children if isinstance(b, discord.ui.Button)]
    labels = [b.label for b in buttons]
    assert labels == [
        "Duel (2)", "Full Table (4)", "Quick Duel (1–5)", "Quick Table (1–5)",
        "Quick Practice Duel (1–5)", "Quick Practice Table (1–5)",
        "Practice Duel", "Practice Table",
    ]
    assert {b.row for b in buttons if "Practice" in (b.label or "")} == {1}
    assert {b.row for b in buttons if "Practice" not in (b.label or "")} == {0}
    plain = mj_views.CreateTableView(cog, (1, 2), lambda c, s: 0, practice_open=True)
    labels = [b.label for b in plain.children if isinstance(b, discord.ui.Button)]
    assert not any("Quick Practice" in (lbl or "") for lbl in labels)


def test_member_panel_states_the_escrow_per_size_before_any_click():
    # mahjong-150: the hold used to appear three clicks in
    from bot_modules.games.mahjong.embeds import build_member_panel
    from bot_modules.games.mahjong.mahjong_service import escrow_amount, load_card
    from bot_modules.games.mahjong.card_logic import FIRST_LIGHT_PATH
    import json

    card = load_card(json.loads(FIRST_LIGHT_PATH.read_text(encoding="utf-8")))
    embed = build_member_panel(
        card, (1, 2, 5), 120, escrow_for=lambda seats, st: escrow_amount(card, seats, st))
    field = next(f for f in embed.fields if f.name == "Escrow per Seat")
    value = field.value or ""
    assert "**450** coins for a Duel" in value and "**300** for a Full Table" in value
    # no card: nothing to price, no field
    assert build_member_panel(None, (1,), 0).fields == []


@pytest.mark.parametrize(
    "reason, copy",
    [
        pytest.param("dissolved", "the lobby never filled", id="dissolved"),
        pytest.param("expired", "nobody rematched in time", id="expired"),
        pytest.param("rematch_unfunded", "a seat couldn't cover the next hand's escrow", id="unfunded"),
        pytest.param("cancelled", "cancelled before the deal", id="cancelled"),
        pytest.param("closed", "closed from the settle screen", id="closed"),
        pytest.param("purged", "closed by the house", id="purged"),
        pytest.param(None, "Table closed.", id="unknown"),
    ],
)
def test_closed_card_says_why(reason, copy):
    # mahjong-151: the final card used to be whatever the last live render
    # was — a settle card still asking for a Rematch
    import asyncio
    from tests.test_mahjong_game_logic import play_state

    class _Guild:
        id = 1

        def get_member(self, member_id):
            return None

    cog = _cog()
    cog.bot = _StubBot()  # type: ignore[assignment]
    state = play_state(2, {0: "9c*13", 1: "8b*13"}, turn=0)
    state.phase = Phase.CLOSED
    embed = asyncio.run(cog._closed_card(
        _Guild(), state, {"stake": 1, "practice": False}, reason))  # type: ignore[arg-type]
    field = next(f for f in embed.fields if f.name == "Closed")
    assert copy in field.value
    assert not any(f.name == "Rematch?" for f in embed.fields)
    assert embed.footer.text and "Closed" in embed.footer.text


def test_table_open_ping_is_plain_content_naming_the_role():
    # mahjong-149: content, never an embed; the role is the only mention
    from bot_modules.games.mahjong.embeds import build_table_open_ping

    line = build_table_open_ping(77, 4, 2, 3, "https://discord.com/x", quick=False)
    assert line.startswith("<@&77> ")
    assert "Full Table" in line and "2/point" in line and "3 seats open" in line
    assert line.endswith("https://discord.com/x")
    duel = build_table_open_ping(77, 2, 1, 1, "j", quick=True)
    assert "Quick Duel" in duel and "1 seat open" in duel


def test_table_open_ping_reads_the_game_night_dial_and_skips_when_unset(monkeypatch):
    # the same reader as the lobby-games sweep; "(none)" posts nothing
    import asyncio
    from bot_modules.cogs import mahjong_cog as cog_mod

    class _Channel:
        id = 5000
        sent: list = []

        async def send(self, content, *, allowed_mentions):
            self.sent.append((content, allowed_mentions))

    class _Guild:
        id = 900

    class _Svc:
        async def table_meta(self, table_id):
            return {"sticky_message_id": 4242}

    cog = _cog()
    cog.bot = _StubBot()  # type: ignore[assignment]
    cog.service = _Svc()  # type: ignore[assignment]
    channel = _Channel()
    calls: list[tuple] = []

    async def unset(bot, guild_id):
        calls.append((guild_id,))
        return True, None

    monkeypatch.setattr(cog_mod, "resolve_game_night_role", unset)
    asyncio.run(cog._announce_table(_Guild(), channel, 7, 4, 1, 9))  # type: ignore[arg-type]
    assert calls == [(900,)] and channel.sent == []

    async def role(bot, guild_id):
        return True, 77

    monkeypatch.setattr(cog_mod, "resolve_game_night_role", role)
    asyncio.run(cog._announce_table(_Guild(), channel, 7, 4, 1, 9))  # type: ignore[arg-type]
    (content, mentions), = channel.sent
    assert content.startswith("<@&77> ") and "/channels/900/5000/4242" in content
    assert [r.id for r in mentions.roles] == [77]  # type: ignore[union-attr]
    assert mentions.users is False and mentions.everyone is False


def test_lobby_hides_add_bot_until_two_members_sit():
    # mahjong-143: the button and the service refusal share one predicate
    from bot_modules.games.mahjong.mahjong_service import fill_bot_allowed
    from bot_modules.games.mahjong.game_logic import TableConfig, create_table, join_table

    lone = create_table(TableConfig(seat_count=4), 100)
    assert fill_bot_allowed(lone) is False
    pair, _ = join_table(lone, 101)
    assert fill_bot_allowed(pair) is True
    src = Path(__file__).resolve().parent.parent / "src/bot_modules/cogs/mahjong_cog.py"
    text = src.read_text(encoding="utf-8")
    assert "fill_bot_allowed(state)" in text
