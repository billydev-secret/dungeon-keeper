"""Photo Challenge payout service — channel resolution, gating, and the award.

These cover what used to be reachable only by constructing a fake Discord
message: the two-payout split (flat participation + quest bonus), the
once-per-guild-local-day dedup on ``econ_photo_rewards``, and the channel
fallback from an active schedule when the setup panel never wrote a config.
The listener that decides *when* to call this stays in test_economy_cog.py.
"""
from __future__ import annotations

import json
import time

import pytest

from bot_modules.core.db_utils import (
    get_tz_offset_hours,
    open_db,
    set_config_value,
)
from bot_modules.economy.logic import local_day_for
from bot_modules.services.economy_photo_service import (
    award_photo_post,
    payout_possible,
    read_photo_channel,
)
from bot_modules.services.economy_quests_service import (
    create_quest,
    set_income_source,
    set_quest_active,
)
from bot_modules.services.economy_service import (
    get_balance,
    save_econ_settings,
)
from tests.db_template import migrated_db

GUILD_ID = 9001
USER_ID = 501
PHOTO_CHANNEL_ID = 4242


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    return db_path


def _enable(db, **overrides) -> None:
    values: dict[str, object] = {"enabled": True, "reward_photo_post": 5}
    values.update(overrides)
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD_ID, values)


def _set_config_channel(db, channel_id=PHOTO_CHANNEL_ID) -> None:
    opts = {"channel_id": str(channel_id) if channel_id else ""}
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO games_game_config (guild_id, game_type, enabled, options)"
            " VALUES (?, 'photo', 1, ?)"
            " ON CONFLICT(guild_id, game_type) DO UPDATE SET options = excluded.options",
            (GUILD_ID, json.dumps(opts)),
        )
        conn.commit()


def _set_schedule_channel(db, channel_id=PHOTO_CHANNEL_ID, status="active") -> None:
    """A photo schedule with no config row — the live desync this falls back for."""
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO games_scheduled"
            " (guild_id, channel_id, game_type, created_by, created_at,"
            "  time_of_day, recurrence, status)"
            " VALUES (?, ?, 'photo', 1, 0, 540, 'daily', ?)",
            (GUILD_ID, channel_id, status),
        )
        conn.commit()


def _mk_photo_quest(db, *, reward=10, signoff=0) -> int:
    with open_db(db) as conn:
        qid = create_quest(
            conn, GUILD_ID, title="Snap it", description="", qtype="event",
            reward=reward, signoff=signoff, criteria="Post a photo",
            starts_at=None, ends_at=None, rotate_tag="", community_target=None,
            created_by=1, trigger_words="", trigger_channel_id=None,
            trigger_kind="photo_post",
        )
        set_quest_active(conn, GUILD_ID, qid, True)
    return qid


def _award(db, *, booster=False, now=None):
    with open_db(db) as conn:
        return award_photo_post(
            conn, GUILD_ID, USER_ID,
            channel_id=PHOTO_CHANNEL_ID,
            booster=booster,
            now=time.time() if now is None else now,
        )


def _balance(db) -> int:
    with open_db(db) as conn:
        return get_balance(conn, GUILD_ID, USER_ID)


# ── channel resolution ────────────────────────────────────────────────────


def test_channel_is_zero_when_nothing_is_configured(db):
    with open_db(db) as conn:
        assert read_photo_channel(conn, GUILD_ID) == 0


def test_channel_comes_from_the_game_config(db):
    _set_config_channel(db)
    with open_db(db) as conn:
        assert read_photo_channel(conn, GUILD_ID) == PHOTO_CHANNEL_ID


def test_active_schedule_supplies_the_channel_when_config_has_none(db):
    """A schedule created without saving the Setup panel must still pay."""
    _set_config_channel(db, channel_id="")
    _set_schedule_channel(db)
    with open_db(db) as conn:
        assert read_photo_channel(conn, GUILD_ID) == PHOTO_CHANNEL_ID


def test_config_channel_wins_over_the_schedule(db):
    _set_config_channel(db, channel_id=PHOTO_CHANNEL_ID)
    _set_schedule_channel(db, channel_id=9999)
    with open_db(db) as conn:
        assert read_photo_channel(conn, GUILD_ID) == PHOTO_CHANNEL_ID


def test_an_inactive_schedule_does_not_supply_a_channel(db):
    _set_config_channel(db, channel_id="")
    _set_schedule_channel(db, status="ended")
    with open_db(db) as conn:
        assert read_photo_channel(conn, GUILD_ID) == 0


def test_malformed_config_options_do_not_raise(db):
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO games_game_config (guild_id, game_type, enabled, options)"
            " VALUES (?, 'photo', 1, 'not json')",
            (GUILD_ID,),
        )
        conn.commit()
    with open_db(db) as conn:
        assert read_photo_channel(conn, GUILD_ID) == 0


# ── the payout-possible gate ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("overrides", "with_quest", "expected"),
    [
        pytest.param({"enabled": False}, True, False, id="economy-off"),
        pytest.param({}, False, True, id="flat-award-only"),
        pytest.param({"reward_photo_post": 0}, True, True, id="quest-only"),
        pytest.param({"reward_photo_post": 0}, False, False, id="nothing-to-pay"),
    ],
)
def test_payout_possible_gate(db, overrides, with_quest, expected):
    _enable(db, **overrides)
    if with_quest:
        _mk_photo_quest(db)
    with open_db(db) as conn:
        assert payout_possible(conn, GUILD_ID) is expected


def test_payout_impossible_when_the_income_source_is_disabled(db):
    """Its own test: the toggle lives in a different table from settings."""
    _enable(db)
    with open_db(db) as conn:
        set_income_source(conn, GUILD_ID, "photo_post", False)
    with open_db(db) as conn:
        assert payout_possible(conn, GUILD_ID) is False


# ── the award itself ──────────────────────────────────────────────────────


def test_award_returns_none_when_the_economy_is_off(db):
    assert _award(db) is None
    assert _balance(db) == 0


def test_award_returns_none_when_the_source_is_disabled(db):
    """The source toggle gates the participation award, not just quests."""
    _enable(db, reward_photo_post=5)
    with open_db(db) as conn:
        set_income_source(conn, GUILD_ID, "photo_post", False)
    assert _award(db) is None
    assert _balance(db) == 0


def test_flat_award_pays_once_per_local_day(db):
    _enable(db, reward_photo_post=5)

    _settings, participation, fired = _award(db)
    assert participation == 5
    assert fired == []
    assert _balance(db) == 5

    # A second photo the same day earns nothing more.
    _settings, participation, fired = _award(db)
    assert participation == 0
    assert _balance(db) == 5


def test_flat_award_pays_again_on_the_next_local_day(db):
    _enable(db, reward_photo_post=5)
    _award(db)
    assert _balance(db) == 5
    _award(db, now=time.time() + 86_400)
    assert _balance(db) == 10


def test_booster_multiplier_applies_to_the_flat_award(db):
    _enable(db, reward_photo_post=10, booster_multiplier=2.0)
    _settings, participation, _fired = _award(db, booster=True)
    assert participation == 20
    assert _balance(db) == 20


def test_quest_bonus_stacks_on_top_of_the_flat_award(db):
    _enable(db, reward_photo_post=5)
    _mk_photo_quest(db, reward=10)

    _settings, participation, fired = _award(db)
    assert participation == 5
    assert len(fired) == 1
    assert _balance(db) == 15


def test_quest_alone_pays_when_participation_is_unpriced(db):
    _enable(db, reward_photo_post=0)
    _mk_photo_quest(db, reward=10)

    _settings, participation, fired = _award(db)
    assert participation == 0
    assert len(fired) == 1
    assert _balance(db) == 10


def test_signoff_quest_files_a_claim_instead_of_paying(db):
    _enable(db, reward_photo_post=0)
    qid = _mk_photo_quest(db, reward=10, signoff=1)

    _settings, _participation, fired = _award(db)
    assert len(fired) == 1
    assert _balance(db) == 0  # sign-off gates the payout

    with open_db(db) as conn:
        claim = conn.execute(
            "SELECT state FROM econ_quest_claims WHERE quest_id = ? AND user_id = ?",
            (qid, USER_ID),
        ).fetchone()
    assert claim is not None and claim["state"] == "pending"


def test_the_dedup_row_records_the_guild_local_day(db):
    """The key is the *guild's* day, not UTC — pinned with a real offset."""
    _enable(db, reward_photo_post=5)
    with open_db(db) as conn:
        set_config_value(conn, "tz_offset_hours", "-8", GUILD_ID)

    # 2027-01-15 04:00 UTC is still 2027-01-14 at UTC-8.
    now = 1_799_985_600.0
    _award(db, now=now)
    with open_db(db) as conn:
        offset = get_tz_offset_hours(conn, GUILD_ID)
        row = conn.execute(
            "SELECT local_day FROM econ_photo_rewards"
            " WHERE guild_id = ? AND user_id = ?",
            (GUILD_ID, USER_ID),
        ).fetchone()
    assert offset == -8
    assert row["local_day"] == local_day_for(now, offset)
    assert row["local_day"] != local_day_for(now, 0)
