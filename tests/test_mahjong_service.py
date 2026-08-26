"""Tests for mahjong_service — stage 4 of docs/plans/meadow-mahjong.md.

The money seams: escrow at seating with the balance gate, one table per
channel and one seat per member (schema-enforced), zero-sum settlement at
stake, wall-game refunds, cancel/dissolve refunds, rematch re-escrow with
the unfunded-close path, restart recovery, and card lifecycle. Engine rules
are stage 3's suite — here the engine is driven only far enough to reach
the service's own behavior.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.games.mahjong import game_logic as engine
from bot_modules.games.mahjong.card_logic import FIRST_LIGHT_PATH
from bot_modules.games.mahjong.mahjong_service import (
    GAME_TYPE,
    MahjongService,
    TableError,
    _hand_gid,
    activate_due_cards,
    escrow_amount,
    get_active_card,
    load_settings,
    save_card,
    seed_first_light,
    set_card_status,
)
from bot_modules.games.mahjong.tiles import Tile
from bot_modules.services.economy_service import apply_credit, apply_debit, get_balance
from tests.db_template import migrated_db

GUILD = 900
CHANNEL = 5000
HOST, GUEST, THIRD = 9001, 9002, 9003
#: First Light escrow at stake 1: 75 × 6 (Duel) / 75 × 4 (4-seat)
DUEL_ESCROW = 450


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "mahjong.db"
    migrated_db(path)
    with open_db(path) as conn:
        set_config_value(conn, "mahjong_enabled", "1", GUILD)
        row_id = seed_first_light(conn, GUILD)
        set_card_status(conn, GUILD, row_id, "active")
        for member in (HOST, GUEST, THIRD):
            apply_credit(conn, GUILD, member, 1000, "grant")
    return path


@pytest.fixture
def service(db):
    svc = MahjongService(db)
    yield svc
    for task in svc._timers.values():
        task.cancel()


async def make_duel(service, db) -> int:
    table_id = await service.create_table(GUILD, CHANNEL, HOST, 2, 1)
    await service.join_table(table_id, GUEST)
    return table_id


def table_row(db, table_id):
    with open_db(db) as conn:
        return conn.execute(
            "SELECT * FROM mahjong_tables WHERE id = ?", (table_id,)
        ).fetchone()


def balances(db, *members):
    with open_db(db) as conn:
        return [get_balance(conn, GUILD, m) for m in members]


# ── Creation gates ───────────────────────────────────────────────────────────


async def test_create_debits_escrow_and_join_fills(service, db):
    table_id = await make_duel(service, db)
    assert balances(db, HOST, GUEST) == [1000 - DUEL_ESCROW, 1000 - DUEL_ESCROW]
    row = table_row(db, table_id)
    assert row["status"] == "live" and row["mode"] == 2
    state = engine.state_from_dict(json.loads(row["state"]))
    assert len(state.seats) == 2


async def test_balance_gate_blocks_the_seat(service, db):
    with open_db(db) as conn:
        assert apply_debit(conn, GUILD, THIRD, 900, "adjust")  # 100 left < 450
    table_id = await service.create_table(GUILD, CHANNEL, HOST, 2, 1)
    with pytest.raises(TableError, match="need"):
        await service.join_table(table_id, THIRD)
    # the failed joiner is not seated and pays nothing
    with open_db(db) as conn:
        seats = conn.execute(
            "SELECT user_id FROM mahjong_seats WHERE table_id = ? AND live = 1",
            (table_id,),
        ).fetchall()
    assert [int(r["user_id"]) for r in seats] == [HOST]
    assert balances(db, THIRD) == [100]


async def test_one_table_per_channel(service, db):
    await service.create_table(GUILD, CHANNEL, HOST, 2, 1)
    with pytest.raises(TableError, match="already has a live table"):
        await service.create_table(GUILD, CHANNEL, GUEST, 2, 1)


async def test_one_seat_per_member_across_channels(service, db):
    await service.create_table(GUILD, CHANNEL, HOST, 2, 1)
    with pytest.raises(TableError, match="already seated"):
        await service.create_table(GUILD, CHANNEL + 1, HOST, 2, 1)


async def test_stake_and_enabled_and_card_gates(service, db):
    with pytest.raises(TableError, match="Pick a stake"):
        await service.create_table(GUILD, CHANNEL, HOST, 2, 99)
    with open_db(db) as conn:
        set_config_value(conn, "mahjong_enabled", "0", GUILD)
    with pytest.raises(TableError, match="isn't open"):
        await service.create_table(GUILD, CHANNEL, HOST, 2, 1)
    with open_db(db) as conn:
        set_config_value(conn, "mahjong_enabled", "1", GUILD)
        conn.execute("UPDATE mahjong_cards SET status = 'archived'")
    with pytest.raises(TableError, match="No Meadow Card"):
        await service.create_table(GUILD, CHANNEL, HOST, 2, 1)


async def test_cancel_refunds_everyone(service, db):
    table_id = await make_duel(service, db)
    await service.act(table_id, "cancel", member_id=HOST)
    assert balances(db, HOST, GUEST) == [1000, 1000]
    row = table_row(db, table_id)
    assert row["status"] == "closed" and row["closed_reason"] == "cancelled"
    with open_db(db) as conn:
        live = conn.execute(
            "SELECT COUNT(*) FROM mahjong_seats WHERE table_id = ? AND live = 1",
            (table_id,),
        ).fetchone()[0]
    assert live == 0


# ── Timers: deal countdown, lobby dissolve ───────────────────────────────────


async def test_full_lobby_deals_on_timeout(service, db):
    table_id = await make_duel(service, db)
    await service.timeout(table_id)  # the deal-countdown timer firing
    row = table_row(db, table_id)
    state = engine.state_from_dict(json.loads(row["state"]))
    assert state.phase is engine.Phase.CHARLESTON
    assert row["deadline_at"] is not None


async def test_unfilled_lobby_dissolves_with_refund(service, db):
    table_id = await service.create_table(GUILD, CHANNEL, HOST, 2, 1)
    await service.timeout(table_id)  # lobby lifetime expiring
    row = table_row(db, table_id)
    assert row["status"] == "closed" and row["closed_reason"] == "dissolved"
    assert balances(db, HOST) == [1000]


# ── Settlement ───────────────────────────────────────────────────────────────


async def play_to_settle(service, db, table_id, *, winner_rack, feed_tile):
    """Deal, skip the Charleston via engine surgery, then feed seat 1 its
    winning tile from seat 0's hand and claim Mahjong through the service."""
    await service.timeout(table_id)  # deal
    # surgery: place the racks so the next two service actions settle it
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT * FROM mahjong_tables WHERE id = ?", (table_id,)
        ).fetchone()
        state = engine.state_from_dict(json.loads(row["state"]))
        state.phase = engine.Phase.AWAIT_DISCARD
        state.turn = 0
        state.pending_picks = {}
        state.seats[0].rack = [Tile(feed_tile)] + [Tile("1c")] * 13
        state.seats[1].rack = winner_rack
        conn.execute(
            "UPDATE mahjong_tables SET state = ? WHERE id = ?",
            (json.dumps(engine.state_to_dict(state)), table_id),
        )
    await service.act(table_id, "discard", member_id=HOST, tile=Tile(feed_tile))
    await service.act(table_id, "claim", member_id=GUEST, kind="mahjong")


async def test_mahjong_settlement_moves_coins_and_records(service, db):
    table_id = await make_duel(service, db)
    # gh-1 minus one 8c: jokerless discard win → 25 × 2 × 2 = 100 coins
    winner_rack = ([Tile.FLOWER] * 4 + [Tile("2d")] * 4 + [Tile("6b")] * 4
                   + [Tile("8c")])
    await play_to_settle(service, db, table_id,
                         winner_rack=winner_rack, feed_tile="8c")
    assert balances(db, HOST, GUEST) == [900, 1100]
    with open_db(db) as conn:
        result = conn.execute(
            "SELECT * FROM mahjong_results WHERE table_id = ?", (table_id,)
        ).fetchone()
        assert result["kind"] == "mahjong"
        assert result["winner_id"] == GUEST
        assert result["line_id"] == "gh-1"
        assert result["won_by"] == "discard" and result["jokerless"] == 1
        seats = conn.execute(
            "SELECT user_id, coins_delta, points_delta FROM mahjong_result_seats "
            "WHERE result_id = ? ORDER BY seat_index", (result["id"],),
        ).fetchall()
        assert [(r["user_id"], r["coins_delta"]) for r in seats] == [
            (HOST, -100), (GUEST, 100)]
        stats = conn.execute(
            "SELECT * FROM mahjong_stats WHERE user_id = ?", (GUEST,)
        ).fetchone()
        assert stats["wins"] == 1 and stats["jokerless_wins"] == 1
        assert stats["coins_won"] == 100 and stats["biggest_win"] == 100
        loser = conn.execute(
            "SELECT * FROM mahjong_stats WHERE user_id = ?", (HOST,)
        ).fetchone()
        assert loser["coins_lost"] == 100 and loser["wins"] == 0


async def test_wall_game_refunds_and_records(service, db):
    table_id = await make_duel(service, db)
    await service.timeout(table_id)  # deal
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT * FROM mahjong_tables WHERE id = ?", (table_id,)
        ).fetchone()
        state = engine.state_from_dict(json.loads(row["state"]))
        state.phase = engine.Phase.AWAIT_DISCARD
        state.turn = 0
        state.pending_picks = {}
        state.wall = []  # next draw walls out
        conn.execute("UPDATE mahjong_tables SET state = ? WHERE id = ?",
                     (json.dumps(engine.state_to_dict(state)), table_id))
    tile = state.seats[0].rack[0]
    await service.act(table_id, "discard", member_id=HOST, tile=tile)
    await service.act(table_id, "claim", member_id=GUEST, kind="pass")
    assert balances(db, HOST, GUEST) == [1000, 1000]  # escrow returned
    with open_db(db) as conn:
        result = conn.execute(
            "SELECT * FROM mahjong_results WHERE table_id = ?", (table_id,)
        ).fetchone()
        assert result["kind"] == "wall_game" and result["winner_id"] is None


# ── Claim windows nobody can answer ──────────────────────────────────────────


async def test_a_fully_answered_claim_window_gets_a_short_deadline(service, db):
    """Every seat with a legal route is passed for when the tile lands, so
    there is nobody to wait on — the window becomes a beat to read the
    discard rather than eight seconds of dead time. Measured over 964
    windows, 28.7% have nobody who can act at all."""
    table_id = await make_duel(service, db)
    await service.timeout(table_id)  # deal
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT * FROM mahjong_tables WHERE id = ?", (table_id,)
        ).fetchone()
        state = engine.state_from_dict(json.loads(row["state"]))
        state.phase = engine.Phase.AWAIT_DISCARD
        state.turn = 0
        state.pending_picks = {}
        # the opponent holds nothing that could take a 5c
        state.seats[0].rack = [Tile("5c")] + [Tile("1d")] * 13
        state.seats[1].rack = [Tile("9b")] * 13
        conn.execute("UPDATE mahjong_tables SET state = ? WHERE id = ?",
                     (json.dumps(engine.state_to_dict(state)), table_id))

    before = time.time()
    await service.act(table_id, "discard", member_id=HOST, tile=Tile("5c"))
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT * FROM mahjong_tables WHERE id = ?", (table_id,)
        ).fetchone()
    state = engine.state_from_dict(json.loads(row["state"]))
    assert state.phase is engine.Phase.CLAIM_WINDOW  # still a visible beat
    assert state.claims[1] == (engine.AUTO_PASS, [])
    # ...but a short one, not the full claim window
    assert row["deadline_at"] - before < 6.0


# ── Hand timing (migration 179) ──────────────────────────────────────────────


async def test_the_deal_is_stamped_on_the_table(service, db):
    """The engine takes no clock (D14), so the service owns the timestamp —
    stamped off the `hand_dealt` event rather than at each deal call site."""
    table_id = await make_duel(service, db)
    with open_db(db) as conn:
        assert conn.execute(
            "SELECT hand_started_at FROM mahjong_tables WHERE id = ?", (table_id,)
        ).fetchone()["hand_started_at"] is None
    before = time.time()
    await service.timeout(table_id)  # deals
    with open_db(db) as conn:
        stamped = conn.execute(
            "SELECT hand_started_at FROM mahjong_tables WHERE id = ?", (table_id,)
        ).fetchone()["hand_started_at"]
    assert stamped is not None and stamped >= before


async def test_a_settled_hand_records_its_duration_and_length(service, db):
    """Duration and discards together give seconds-per-discard, which is the
    figure every projected hand length is scaled by and which currently
    rests on a single observed game."""
    table_id = await make_duel(service, db)
    winner_rack = ([Tile.FLOWER] * 4 + [Tile("2d")] * 4 + [Tile("6b")] * 4
                   + [Tile("8c")])
    await play_to_settle(service, db, table_id,
                         winner_rack=winner_rack, feed_tile="8c")
    with open_db(db) as conn:
        result = conn.execute(
            "SELECT * FROM mahjong_results WHERE table_id = ?", (table_id,)
        ).fetchone()
    assert result["started_at"] is not None
    assert result["created_at"] >= result["started_at"]
    assert result["discards"] is not None and result["discards"] >= 1


async def test_a_wall_game_records_its_length_too(service, db):
    """The wall-game constant is the sim's most load-bearing claim; this is
    what lets real play check it."""
    table_id = await make_duel(service, db)
    await service.timeout(table_id)
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT * FROM mahjong_tables WHERE id = ?", (table_id,)
        ).fetchone()
        state = engine.state_from_dict(json.loads(row["state"]))
        state.phase = engine.Phase.AWAIT_DISCARD
        state.turn = 0
        state.pending_picks = {}
        state.wall = []
        conn.execute("UPDATE mahjong_tables SET state = ? WHERE id = ?",
                     (json.dumps(engine.state_to_dict(state)), table_id))
    await service.act(table_id, "discard", member_id=HOST,
                      tile=state.seats[0].rack[0])
    await service.act(table_id, "claim", member_id=GUEST, kind="pass")
    with open_db(db) as conn:
        result = conn.execute(
            "SELECT * FROM mahjong_results WHERE table_id = ?", (table_id,)
        ).fetchone()
    assert result["kind"] == "wall_game"
    assert result["discards"] is not None and result["started_at"] is not None


# ── Rematch escrow ───────────────────────────────────────────────────────────


async def settle_a_hand(service, db):
    table_id = await make_duel(service, db)
    winner_rack = ([Tile.FLOWER] * 4 + [Tile("2d")] * 4 + [Tile("6b")] * 4
                   + [Tile("8c")])
    await play_to_settle(service, db, table_id,
                         winner_rack=winner_rack, feed_tile="8c")
    return table_id


async def test_rematch_re_escrows_and_deals(service, db):
    table_id = await settle_a_hand(service, db)
    await service.act(table_id, "rematch", member_id=HOST)
    await service.act(table_id, "rematch", member_id=GUEST)
    row = table_row(db, table_id)
    state = engine.state_from_dict(json.loads(row["state"]))
    assert state.phase is engine.Phase.CHARLESTON and state.hand_no == 2
    # both re-escrowed for hand 2 on top of hand 1's ±100
    assert balances(db, HOST, GUEST) == [900 - DUEL_ESCROW, 1100 - DUEL_ESCROW]
    with open_db(db) as conn:
        held = conn.execute(
            "SELECT COUNT(*) FROM econ_game_wagers WHERE game_type = ? "
            "AND game_id = ? AND state = 'held'",
            (GAME_TYPE, _hand_gid(table_id, 2)),
        ).fetchone()[0]
    assert held == 2


async def test_rematch_with_a_broke_seat_closes_and_refunds(service, db):
    table_id = await settle_a_hand(service, db)
    with open_db(db) as conn:  # loser can no longer cover 450
        assert apply_debit(conn, GUILD, HOST, 600, "adjust")  # 900 - 600 = 300
    await service.act(table_id, "rematch", member_id=HOST)
    await service.act(table_id, "rematch", member_id=GUEST)
    row = table_row(db, table_id)
    assert row["status"] == "closed" and row["closed_reason"] == "rematch_unfunded"
    # nobody is left holding a fresh hand-2 escrow
    assert balances(db, HOST, GUEST) == [300, 1100]


# ── Restart recovery ─────────────────────────────────────────────────────────


async def test_resume_rearms_live_tables(service, db):
    table_id = await make_duel(service, db)
    fresh = MahjongService(db)
    resumed = await fresh.resume_tables()
    assert resumed == [table_id]
    assert table_id in fresh._timers
    await fresh.shutdown()


async def test_resume_refunds_an_unloadable_table(service, db):
    table_id = await make_duel(service, db)
    with open_db(db) as conn:
        conn.execute("UPDATE mahjong_tables SET state = '{broken' WHERE id = ?",
                     (table_id,))
    fresh = MahjongService(db)
    resumed = await fresh.resume_tables()
    assert resumed == []
    row = table_row(db, table_id)
    assert row["status"] == "closed" and row["closed_reason"] == "unloadable"
    assert balances(db, HOST, GUEST) == [1000, 1000]
    await fresh.shutdown()


# ── Listener + timer arming ──────────────────────────────────────────────────


async def test_listener_hears_transitions(service, db):
    heard: list[tuple[int, str]] = []

    async def listener(table_id, state, events):
        heard.extend((table_id, k) for k, _ in events)

    service.set_listener(listener)
    table_id = await service.create_table(GUILD, CHANNEL, HOST, 2, 1)
    await service.join_table(table_id, GUEST)
    assert (table_id, "table_full") in heard


async def test_deal_countdown_timer_actually_fires(db):
    service = MahjongService(db)
    import bot_modules.games.mahjong.mahjong_service as ms
    original = ms.DEAL_COUNTDOWN
    ms.DEAL_COUNTDOWN = 0.05
    try:
        table_id = await service.create_table(GUILD, CHANNEL, HOST, 2, 1)
        await service.join_table(table_id, GUEST)
        await asyncio.sleep(0.4)
        row = table_row(db, table_id)
        state = engine.state_from_dict(json.loads(row["state"]))
        assert state.phase is engine.Phase.CHARLESTON
    finally:
        ms.DEAL_COUNTDOWN = original
        await service.shutdown()


# ── Cards ────────────────────────────────────────────────────────────────────


def test_card_lifecycle(db):
    with open_db(db) as conn:
        data = json.loads(FIRST_LIGHT_PATH.read_text(encoding="utf-8"))
        data["card_id"] = "second-card"
        row_id = save_card(conn, GUILD, data, uploaded_by=1)
        # activating the new card demotes the old in the same transaction
        set_card_status(conn, GUILD, row_id, "active")
        active = get_active_card(conn, GUILD)
        assert active is not None and active[1].card_id == "second-card"
        # scheduling + promotion
        set_card_status(conn, GUILD, row_id, "archived")
        set_card_status(conn, GUILD, row_id, "scheduled", activate_at=100.0)
        assert activate_due_cards(conn, GUILD, now=50.0) is False
        assert activate_due_cards(conn, GUILD, now=150.0) is True
        active = get_active_card(conn, GUILD)
        assert active is not None and active[1].card_id == "second-card"


def test_bad_card_upload_reports_everything(db):
    from bot_modules.games.mahjong.card_logic import CardError
    with open_db(db) as conn:
        with pytest.raises(CardError):
            save_card(conn, GUILD, {"card_id": "x"}, uploaded_by=1)


def test_settings_roundtrip(db):
    with open_db(db) as conn:
        set_config_value(conn, "mahjong_turn_timer", "30", GUILD)
        set_config_value(conn, "mahjong_stakes_allowed", "2,4", GUILD)
        set_config_value(conn, "mahjong_duel_wall_trim", "60", GUILD)
        s = load_settings(conn, GUILD)
    assert s.turn_timer == 30.0
    assert s.stakes_allowed == (2, 4)
    assert s.duel_wall_trim == 60
    assert s.claim_window(2) == 6.0 and s.claim_window(4) == 8.0


def test_escrow_amounts_match_the_spec(db):
    with open_db(db) as conn:
        active = get_active_card(conn, GUILD)
    assert active is not None
    card = active[1]
    assert escrow_amount(card, 2, 1) == 450   # 75 × 6
    assert escrow_amount(card, 4, 1) == 300   # 75 × 4
    assert escrow_amount(card, 4, 2) == 600


# ── Purge (privacy_service wiring, stage 4 compliance) ───────────────────────


async def test_purge_dissolves_live_seat_and_clears_history(service, db):
    from bot_modules.services.privacy_service import purge_user_data

    table_id = await settle_a_hand(service, db)  # HOST lost 100, GUEST won
    # rematch half-state doesn't matter; purge GUEST mid-settle
    with open_db(db) as conn:
        purge_user_data(conn, GUILD, GUEST)
    row = table_row(db, table_id)
    assert row["status"] == "closed" and row["closed_reason"] == "purged"
    with open_db(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM mahjong_seats WHERE user_id = ?", (GUEST,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM mahjong_result_seats WHERE user_id = ?", (GUEST,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM mahjong_stats WHERE user_id = ?", (GUEST,)
        ).fetchone()[0] == 0
        # the result row survives for the other seats, anonymised
        result = conn.execute(
            "SELECT winner_id FROM mahjong_results WHERE table_id = ?", (table_id,)
        ).fetchone()
        assert result is not None and result["winner_id"] is None
        # HOST's own history is intact
        assert conn.execute(
            "SELECT COUNT(*) FROM mahjong_result_seats WHERE user_id = ?", (HOST,)
        ).fetchone()[0] == 1


async def test_purge_mid_hand_refunds_live_escrow(service, db):
    from bot_modules.services.privacy_service import purge_user_data

    table_id = await make_duel(service, db)
    await service.timeout(table_id)  # deal — escrow is now held for hand 1
    with open_db(db) as conn:
        purge_user_data(conn, GUILD, GUEST)
    # the surviving seat's escrow came back (the table can't continue
    # one-handed); the purged member's whole wallet is erased with them
    assert balances(db, HOST) == [1000]
    row = table_row(db, table_id)
    assert row["status"] == "closed" and row["closed_reason"] == "purged"


async def test_export_flags_mahjong_tables_for_review(service, db):
    from bot_modules.services.privacy_service import export_user_data

    await make_duel(service, db)
    with open_db(db) as conn:
        export = export_user_data(conn, GUILD, HOST)
    assert "mahjong_tables" in export["review_required"]
    assert "mahjong_seats" in export["tables"]


# ── No-contact (D7): joining consults the list, refusal is the stale-race copy


async def test_no_contact_blocks_the_join_indistinguishably(service, db):
    from bot_modules.games.mahjong.mahjong_service import STALE_TABLE
    from bot_modules.services.no_contact_service import add_pair

    add_pair(db, GUILD, HOST, GUEST, reason="test", created_by=1)
    table_id = await service.create_table(GUILD, CHANNEL, HOST, 2, 1)
    with pytest.raises(TableError) as e:
        await service.join_table(table_id, GUEST)
    # the exact copy the cog also uses for genuine stale-table races —
    # indistinguishable by construction (docs/no_contact_spec.md)
    assert str(e.value) == STALE_TABLE
    # not seated, nothing debited
    with open_db(db) as conn:
        seats = conn.execute(
            "SELECT user_id FROM mahjong_seats WHERE table_id = ? AND live = 1",
            (table_id,),
        ).fetchall()
    assert [int(r["user_id"]) for r in seats] == [HOST]
    assert balances(db, GUEST) == [1000]


# ── Guild leavers and zombie tables (stage-6 review) ─────────────────────────


async def test_closed_table_refuses_joins_and_acts(service, db):
    from bot_modules.games.mahjong.mahjong_service import STALE_TABLE

    table_id = await service.create_table(GUILD, CHANNEL, HOST, 2, 1)
    await service.dissolve_table(table_id, "closed")
    with pytest.raises(TableError) as e:
        await service.join_table(table_id, GUEST)
    assert str(e.value) == STALE_TABLE  # a lingering card can't seat anyone
    with pytest.raises(TableError) as e:
        await service.act(table_id, "rematch", member_id=HOST)
    assert str(e.value) == STALE_TABLE
    assert balances(db, GUEST) == [1000]  # and debits nothing


async def test_member_left_mid_hand_folds_fallow_and_settles_duel(service, db):
    table_id = await make_duel(service, db)
    await service.timeout(table_id)  # deal
    await service.member_left(GUILD, GUEST)
    row = table_row(db, table_id)
    state = engine.state_from_dict(json.loads(row["state"]))
    # Duel: the fold ends the hand; escrow settled per the fallow rules —
    # survivor collects, the leaver's escrow paid out rather than refunded
    assert state.phase is engine.Phase.SETTLE
    assert state.outcome is not None and state.outcome.kind == "fallow_end"
    with open_db(db) as conn:
        host_balance = get_balance(conn, GUILD, HOST)
    assert host_balance > 1000 - DUEL_ESCROW  # got escrow back plus the base


async def test_member_left_in_lobby_dissolves_and_refunds(service, db):
    table_id = await service.create_table(GUILD, CHANNEL, HOST, 2, 1)
    await service.member_left(GUILD, HOST)
    row = table_row(db, table_id)
    assert row["status"] == "closed"
    assert balances(db, HOST) == [1000]


async def test_member_left_unseated_is_a_noop(service, db):
    await make_duel(service, db)
    await service.member_left(GUILD, THIRD)  # not seated anywhere
    assert balances(db, HOST, GUEST) == [1000 - DUEL_ESCROW] * 2


# ── Assistance preference (plans/mahjong-assist.md stage 2) ──────────────────


def test_assist_mode_round_trip(db):
    from bot_modules.games.mahjong.mahjong_service import (
        get_assist_mode, load_settings, set_assist_mode,
    )

    with open_db(db) as conn:
        settings = load_settings(conn, GUILD)
        # never chose → the guild default, and the shipped default is 'gap'
        assert get_assist_mode(conn, GUILD, HOST, settings) == "gap"
        set_assist_mode(conn, GUILD, HOST, "coach")
        assert get_assist_mode(conn, GUILD, HOST, settings) == "coach"
        set_assist_mode(conn, GUILD, HOST, "off")
        assert get_assist_mode(conn, GUILD, HOST, settings) == "off"
        # another member is untouched
        assert get_assist_mode(conn, GUILD, GUEST, settings) == "gap"


def test_assist_guild_default_dial_respected_and_overridden(db):
    from bot_modules.core.db_utils import set_config_value
    from bot_modules.games.mahjong.mahjong_service import (
        get_assist_mode, load_settings, set_assist_mode,
    )

    with open_db(db) as conn:
        set_config_value(conn, "mahjong_assist_default", "off", GUILD)
        settings = load_settings(conn, GUILD)
        assert settings.assist_default == "off"
        assert get_assist_mode(conn, GUILD, HOST, settings) == "off"
        # a member's own pick beats the dial
        set_assist_mode(conn, GUILD, HOST, "target")
        assert get_assist_mode(conn, GUILD, HOST, settings) == "target"


def test_assist_corrupt_values_degrade_never_raise(db):
    from bot_modules.core.db_utils import set_config_value
    from bot_modules.games.mahjong.mahjong_service import (
        get_assist_mode, load_settings, set_assist_mode,
    )

    with open_db(db) as conn:
        # corrupt dial → shipped default
        set_config_value(conn, "mahjong_assist_default", "banana", GUILD)
        assert load_settings(conn, GUILD).assist_default == "gap"
        # corrupt stored row → guild default, not an exception
        conn.execute(
            "INSERT INTO mahjong_prefs (guild_id, user_id, mode, updated_at) "
            "VALUES (?, ?, 'banana', 0)",
            (GUILD, HOST),
        )
        settings = load_settings(conn, GUILD)
        assert get_assist_mode(conn, GUILD, HOST, settings) == "gap"
        # and the writer refuses garbage outright
        with pytest.raises(ValueError):
            set_assist_mode(conn, GUILD, HOST, "banana")


def test_purge_clears_the_assist_preference(db):
    from bot_modules.games.mahjong.mahjong_service import set_assist_mode
    from bot_modules.services.privacy_service import purge_user_data

    with open_db(db) as conn:
        set_assist_mode(conn, GUILD, HOST, "coach")
        set_assist_mode(conn, GUILD, GUEST, "target")
        purge_user_data(conn, GUILD, HOST)
        assert conn.execute(
            "SELECT COUNT(*) FROM mahjong_prefs WHERE user_id = ?", (HOST,)
        ).fetchone()[0] == 0
        # the other member's preference survives
        assert conn.execute(
            "SELECT mode FROM mahjong_prefs WHERE user_id = ?", (GUEST,)
        ).fetchone()[0] == "target"


# ── AI seats (plans/mahjong-bots.md stage 2) ─────────────────────────────────


def _ledger_kinds(db, user_id):
    with open_db(db) as conn:
        return [
            str(r[0]) for r in conn.execute(
                "SELECT kind FROM econ_ledger WHERE guild_id = ? AND user_id = ? "
                "ORDER BY id", (GUILD, user_id),
            )
        ]


async def make_practice_duel(service):
    return await service.create_table(GUILD, CHANNEL, HOST, 2, 0, practice=True)


async def test_practice_table_is_born_full_and_stake_free(service, db):
    from bot_modules.games.mahjong.bot_logic import bot_member_id

    table_id = await make_practice_duel(service)
    row = table_row(db, table_id)
    assert row["practice"] == 1 and row["stake"] == 0
    with open_db(db) as conn:
        seats = conn.execute(
            "SELECT user_id, seat_index FROM mahjong_seats WHERE table_id = ? "
            "ORDER BY seat_index", (table_id,),
        ).fetchall()
        assert [int(r["user_id"]) for r in seats] == [
            HOST, bot_member_id(table_id, 1)]
        wagers = conn.execute(
            "SELECT COUNT(*) FROM econ_game_wagers WHERE game_type = 'mahjong'"
        ).fetchone()[0]
    assert wagers == 0                       # nothing held, nobody's coins moved
    assert balances(db, HOST) == [1000]


async def test_practice_needs_its_dial(service, db):
    with open_db(db) as conn:
        set_config_value(conn, "mahjong_practice_bots", "0", GUILD)
    with pytest.raises(TableError):
        await make_practice_duel(service)


async def test_practice_hand_settles_without_money_or_records(service, db):
    from bot_modules.games.mahjong.bot_logic import bot_member_id

    table_id = await make_practice_duel(service)
    bot_id = bot_member_id(table_id, 1)
    await service.timeout(table_id)          # deal
    # surgery: bot feeds the human their winning tile
    winner_rack = ([Tile.FLOWER] * 4 + [Tile("2d")] * 4 + [Tile("6b")] * 4
                   + [Tile("8c")])
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT * FROM mahjong_tables WHERE id = ?", (table_id,)
        ).fetchone()
        state = engine.state_from_dict(json.loads(row["state"]))
        state.phase = engine.Phase.AWAIT_DISCARD
        state.turn = 1
        state.pending_picks = {}
        state.seats[1].rack = [Tile("8c")] + [Tile("1c")] * 13
        state.seats[0].rack = winner_rack
        conn.execute(
            "UPDATE mahjong_tables SET state = ? WHERE id = ?",
            (json.dumps(engine.state_to_dict(state)), table_id),
        )
    await service.act(table_id, "discard", member_id=bot_id, tile=Tile("8c"))
    await service.act(table_id, "claim", member_id=HOST, kind="mahjong")
    row = table_row(db, table_id)
    state = engine.state_from_dict(json.loads(row["state"]))
    assert state.phase is engine.Phase.SETTLE
    assert state.outcome is not None and state.outcome.kind == "mahjong"
    with open_db(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM mahjong_results WHERE table_id = ?", (table_id,)
        ).fetchone()[0] == 0                 # B5: nothing recorded
        assert conn.execute(
            "SELECT COUNT(*) FROM mahjong_stats WHERE guild_id = ?", (GUILD,)
        ).fetchone()[0] == 0
    assert balances(db, HOST) == [1000]      # and no coins moved


async def make_fill_duel(service, db):
    with open_db(db) as conn:
        set_config_value(conn, "mahjong_fill_bots", "1", GUILD)
    table_id = await service.create_table(GUILD, CHANNEL, HOST, 2, 1)
    await service.add_bot(table_id, HOST)
    return table_id


async def test_add_bot_stakes_house_money_visibly(service, db):
    from bot_modules.games.mahjong.bot_logic import bot_member_id

    table_id = await make_fill_duel(service, db)
    bot_id = bot_member_id(table_id, 1)
    with open_db(db) as conn:
        held = conn.execute(
            "SELECT user_id, amount FROM econ_game_wagers WHERE game_type = "
            "'mahjong' AND state = 'held' ORDER BY user_id",
        ).fetchall()
    assert [(int(r["user_id"]), int(r["amount"])) for r in held] == [
        (bot_id, DUEL_ESCROW), (HOST, DUEL_ESCROW)]
    assert _ledger_kinds(db, bot_id) == ["mahjong_house_stake", "wager_stake"]
    assert balances(db, bot_id) == [0]       # topped up exactly, then held


async def test_add_bot_gates(service, db):
    table_id = await service.create_table(GUILD, CHANNEL, HOST, 2, 1)
    with pytest.raises(TableError):          # dial defaults off
        await service.add_bot(table_id, HOST)
    with open_db(db) as conn:
        set_config_value(conn, "mahjong_fill_bots", "1", GUILD)
    with pytest.raises(TableError):          # host only
        await service.add_bot(table_id, GUEST)
    await service.add_bot(table_id, HOST)    # and now it seats


async def test_fill_hand_pays_the_human_and_sweeps_the_bot(service, db):
    from bot_modules.games.mahjong.bot_logic import bot_member_id

    table_id = await make_fill_duel(service, db)
    bot_id = bot_member_id(table_id, 1)
    winner_rack = ([Tile.FLOWER] * 4 + [Tile("2d")] * 4 + [Tile("6b")] * 4
                   + [Tile("8c")])
    await service.timeout(table_id)          # deal
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT * FROM mahjong_tables WHERE id = ?", (table_id,)
        ).fetchone()
        state = engine.state_from_dict(json.loads(row["state"]))
        state.phase = engine.Phase.AWAIT_DISCARD
        state.turn = 1
        state.pending_picks = {}
        state.seats[1].rack = [Tile("8c")] + [Tile("1c")] * 13
        state.seats[0].rack = winner_rack
        conn.execute(
            "UPDATE mahjong_tables SET state = ? WHERE id = ?",
            (json.dumps(engine.state_to_dict(state)), table_id),
        )
    await service.act(table_id, "discard", member_id=bot_id, tile=Tile("8c"))
    await service.act(table_id, "claim", member_id=HOST, kind="mahjong")
    # jokerless discard win: 25 × 2 × 2 = 100 — real coins from the house
    assert balances(db, HOST) == [1100]
    assert balances(db, bot_id) == [0]       # escrow − loss, swept to zero
    kinds = _ledger_kinds(db, bot_id)
    assert kinds[0] == "mahjong_house_stake" and kinds[-1] == "mahjong_house_settle"
    with open_db(db) as conn:
        stats = conn.execute(
            "SELECT user_id FROM mahjong_stats WHERE guild_id = ?", (GUILD,)
        ).fetchall()
        assert [int(r[0]) for r in stats] == [HOST]   # bots keep no aggregates
        seats = conn.execute(
            "SELECT user_id, coins_delta FROM mahjong_result_seats "
            "WHERE guild_id = ? ORDER BY seat_index", (GUILD,),
        ).fetchall()
    assert [(int(r[0]), int(r[1])) for r in seats] == [
        (HOST, 100), (bot_id, -100)]         # history complete, bot included


async def test_bot_pump_plays_the_bot_seat(service, db, monkeypatch):
    import bot_modules.games.mahjong.mahjong_service as svc_mod

    monkeypatch.setattr(svc_mod, "BOT_DELAY", (0.0, 0.0))
    table_id = await make_practice_duel(service)
    await service.timeout(table_id)          # deal → Charleston
    await service._pump_bots(table_id)       # the bot submits its pass
    state = await service.load_state(table_id)
    assert state is not None
    assert 1 in state.pending_picks          # seat 1 (the bot) has picked


async def test_resume_kicks_bot_pumps(service, db):
    table_id = await make_practice_duel(service)
    svc2 = MahjongService(db)
    try:
        resumed = await svc2.resume_tables()
        assert table_id in resumed
        assert table_id in svc2._bot_pumps   # bots pick play back up
    finally:
        await svc2.shutdown()


async def test_purge_dissolving_a_fill_table_burns_the_bot_wallet(service, db):
    from bot_modules.games.mahjong.bot_logic import bot_member_id
    from bot_modules.services.privacy_service import purge_user_data

    table_id = await make_fill_duel(service, db)
    bot_id = bot_member_id(table_id, 1)
    await service.timeout(table_id)          # deal — escrow is live mid-hand
    with open_db(db) as conn:
        purge_user_data(conn, GUILD, HOST)
    row = table_row(db, table_id)
    assert row["status"] == "closed" and row["closed_reason"] == "purged"
    assert balances(db, bot_id) == [0]       # refund landed, then burned
    kinds = _ledger_kinds(db, bot_id)
    assert kinds[-1] == "mahjong_house_settle"


# ── Bots review round (2026-08-22): P1/P2 service fixes ──────────────────────


async def test_bot_pump_chains_through_the_bots_own_actions(service, db, monkeypatch):
    # P1 (three lenses converged): the pump's own act() used to try to
    # schedule its successor while the pump itself was the live registry
    # entry — the guard swallowed it and every bot-after-bot chain stalled
    # to the phase timer. Drive the REAL funnel: after the human discards,
    # the bot must pass the claim window AND then play its own turn with no
    # timer ever firing.
    import bot_modules.games.mahjong.mahjong_service as svc_mod

    monkeypatch.setattr(svc_mod, "BOT_DELAY", (0.0, 0.0))
    table_id = await make_practice_duel(service)
    await service.timeout(table_id)              # deal
    # surgery into a clean AWAIT_DISCARD, human's turn, nothing claimable
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT * FROM mahjong_tables WHERE id = ?", (table_id,)
        ).fetchone()
        state = engine.state_from_dict(json.loads(row["state"]))
        state.phase = engine.Phase.AWAIT_DISCARD
        state.turn = 0
        state.pending_picks = {}
        state.seats[0].rack = [Tile("9c")] + [Tile("1d"), Tile("2d"), Tile("3d"),
                                             Tile("4d"), Tile("5d"), Tile("6d"),
                                             Tile("7d"), Tile("8d"), Tile("9d"),
                                             Tile("1b"), Tile("2b"), Tile("3b"),
                                             Tile("4b")]
        state.seats[1].rack = [Tile("5b"), Tile("6b"), Tile("7b"), Tile("8b"),
                               Tile("9b"), Tile("1c"), Tile("2c"), Tile("3c"),
                               Tile("4c"), Tile("5c"), Tile("6c"), Tile("7c"),
                               Tile("wn")]
        conn.execute(
            "UPDATE mahjong_tables SET state = ? WHERE id = ?",
            (json.dumps(engine.state_to_dict(state)), table_id),
        )
    await service.act(table_id, "discard", member_id=HOST, tile=Tile("9c"))
    # bot must: pass the window (resolving it), draw, and discard — three
    # bot-driven transitions, zero timers. Poll briefly.
    for _ in range(80):
        await asyncio.sleep(0.05)
        state = await service.load_state(table_id)
        assert state is not None
        if len(state.discards) >= 2:
            # the bot passed the window, drew, and discarded — three
            # bot-driven transitions with no timer; the open claim window
            # now correctly waits on the HUMAN's response
            assert state.phase is engine.Phase.CLAIM_WINDOW
            break
    else:
        pytest.fail(
            f"bot chain stalled: phase={state.phase} turn={state.turn} "
            f"discards={len(state.discards)}"
        )


async def test_unloadable_fill_table_still_sweeps_the_bot_wallet(service, db):
    # P2: the orphaned-escrow net refunds every hold — including the house
    # bot's — and used to close the table with the refund stranded on the
    # synthetic wallet forever.
    from bot_modules.games.mahjong.bot_logic import bot_member_id

    table_id = await make_fill_duel(service, db)
    bot_id = bot_member_id(table_id, 1)
    with open_db(db) as conn:
        conn.execute(
            "UPDATE mahjong_tables SET state = 'not json' WHERE id = ?",
            (table_id,),
        )
    svc2 = MahjongService(db)
    try:
        await svc2.resume_tables()
    finally:
        await svc2.shutdown()
    row = table_row(db, table_id)
    assert row["status"] == "closed" and row["closed_reason"] == "unloadable"
    assert balances(db, HOST) == [1000]          # human refunded
    assert balances(db, bot_id) == [0]           # and the house got its coins back
    assert _ledger_kinds(db, bot_id)[-1] == "mahjong_house_settle"


async def test_house_bot_never_ranks_as_an_earner(service, db):
    # P3: the house top-up and settle credits land in econ_ledger under the
    # negative bot id, and the public Top Earners board used to rank the
    # phantom. Negative ids are not members and never rank.
    from bot_modules.economy.leaderboard import collect_leaderboard_data
    from bot_modules.games.mahjong.bot_logic import bot_member_id

    table_id = await make_fill_duel(service, db)
    bot_id = bot_member_id(table_id, 1)
    assert _ledger_kinds(db, bot_id)             # the credits are real...
    with open_db(db) as conn:
        data = collect_leaderboard_data(conn, GUILD, time.time())
    ranked = [uid for uid, _ in data.top_earners]
    assert all(uid > 0 for uid in ranked)        # ...but never ranked


async def test_a_real_timer_firing_still_notifies_and_pumps(service, db, monkeypatch):
    # THE prod-only bug (live QA 2026-08-23): a firing phase timer runs
    # _arm_timer from INSIDE its own fire task, and old.cancel() there
    # cancels the running task itself — CancelledError lands on the next
    # await (the listener), the sticky never updates, the pump is never
    # scheduled, and fire()'s own except swallows it all silently. act()-
    # driven transitions and direct timeout() calls cancel a DIFFERENT
    # task, which is why no test and no out-of-band repro ever caught it:
    # only a real elapsed-time firing with a listener wired reproduces.
    import bot_modules.games.mahjong.mahjong_service as svc_mod

    monkeypatch.setattr(svc_mod, "BOT_DELAY", (0.0, 0.0))
    heard: list[str] = []

    async def listener(table_id, state, events):
        # a real listener suspends (its first DB read); the self-cancel
        # lands exactly at a suspension point, so the stub must have one
        await asyncio.sleep(0)
        heard.append(state.phase.value)

    service.set_listener(listener)
    table_id = await make_practice_duel(service)
    # re-arm the deal countdown to NOW so the real fire task drives it
    await service._arm_timer(table_id, time.time() + 0.05)
    for _ in range(80):
        await asyncio.sleep(0.05)
        state = await service.load_state(table_id)
        assert state is not None
        if 1 in state.pending_picks:
            break
    else:
        pytest.fail(
            f"after a real timer firing: listener heard {heard}, "
            f"bot never picked"
        )
    assert "charleston" in heard      # the listener survived the firing
