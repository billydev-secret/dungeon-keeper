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
