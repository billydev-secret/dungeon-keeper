"""Tests for services/birthday_service.py."""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from tests.db_template import migrated_db
from bot_modules.services.birthday_service import (
    ANNOUNCE_HOUR_KEY,
    DEFAULT_ANNOUNCE_HOUR,
    MAX_DAYS,
    announce_hour,
    announced_birthday_ids,
    delete_birthday,
    delete_channel,
    is_birthday_wish,
    list_all_birthdays,
    list_channels,
    mark_announced,
    month_choices,
    parse_birthday_day,
    todays_unannounced,
    upsert_birthday,
    upsert_channel,
)

GUILD = 123
USER_A = 1001
USER_B = 1002
MOD = 9001


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    return db_path


# ── MAX_DAYS ──────────────────────────────────────────────────────────


def test_max_days_length():
    assert len(MAX_DAYS) == 13  # index 0 unused


def test_max_days_spot_checks():
    assert MAX_DAYS[1] == 31   # January
    assert MAX_DAYS[2] == 28   # February capped
    assert MAX_DAYS[4] == 30   # April
    assert MAX_DAYS[12] == 31  # December


# ── upsert_birthday ───────────────────────────────────────────────────


def test_upsert_inserts_new_birthday(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        rows = list_all_birthdays(conn, GUILD)
    assert rows == [(USER_A, 7, 15, None)]


def test_upsert_overwrites_existing(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        upsert_birthday(conn, GUILD, USER_A, 3, 1, MOD)
        rows = list_all_birthdays(conn, GUILD)
    assert rows == [(USER_A, 3, 1, None)]


def test_upsert_multiple_users(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 6, 10, MOD)
        upsert_birthday(conn, GUILD, USER_B, 6, 5, MOD)
        rows = list_all_birthdays(conn, GUILD)
    # Ordered by month then day
    assert rows == [(USER_B, 6, 5, None), (USER_A, 6, 10, None)]


def test_upsert_stores_preference(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD, preference="Cake and chaos!")
        rows = list_all_birthdays(conn, GUILD)
    assert rows == [(USER_A, 7, 15, "Cake and chaos!")]


def test_upsert_clears_preference_when_omitted(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD, preference="old request")
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        rows = list_all_birthdays(conn, GUILD)
    assert rows == [(USER_A, 7, 15, None)]


# ── delete_birthday ───────────────────────────────────────────────────


def test_delete_existing_birthday(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        removed = delete_birthday(conn, GUILD, USER_A)
        assert removed is True
        assert list_all_birthdays(conn, GUILD) == []  # type: ignore[comparison-overlap]


def test_delete_nonexistent_birthday(db):
    with open_db(db) as conn:
        removed = delete_birthday(conn, GUILD, USER_A)
    assert removed is False


def test_delete_only_affects_target_user(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        upsert_birthday(conn, GUILD, USER_B, 8, 20, MOD)
        delete_birthday(conn, GUILD, USER_A)
        rows = list_all_birthdays(conn, GUILD)
    assert rows == [(USER_B, 8, 20, None)]


# ── list_all_birthdays ────────────────────────────────────────────────


def test_list_empty_guild(db):
    with open_db(db) as conn:
        assert list_all_birthdays(conn, GUILD) == []


def test_list_ordered_by_month_then_day(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, 1001, 12, 25, MOD)
        upsert_birthday(conn, GUILD, 1002, 1, 1, MOD)
        upsert_birthday(conn, GUILD, 1003, 1, 15, MOD)
        rows = list_all_birthdays(conn, GUILD)
    assert [(m, d) for _, m, d, _ in rows] == [(1, 1), (1, 15), (12, 25)]


def test_list_isolated_by_guild(db):
    OTHER_GUILD = 999
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        upsert_birthday(conn, OTHER_GUILD, USER_B, 8, 1, MOD)
        assert len(list_all_birthdays(conn, GUILD)) == 1
        assert len(list_all_birthdays(conn, OTHER_GUILD)) == 1


# ── todays_unannounced ────────────────────────────────────────────────


def test_unannounced_returns_todays_birthdays(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        result = todays_unannounced(conn, GUILD, 7, 15, "2026-07-15")
    assert result == [USER_A]


def test_unannounced_excludes_already_announced(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        mark_announced(conn, GUILD, USER_A, "2026-07-15")
        result = todays_unannounced(conn, GUILD, 7, 15, "2026-07-15")
    assert result == []


def test_unannounced_same_user_different_date_counts_again(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        mark_announced(conn, GUILD, USER_A, "2025-07-15")
        # New year — should appear again
        result = todays_unannounced(conn, GUILD, 7, 15, "2026-07-15")
    assert result == [USER_A]


def test_unannounced_empty_when_no_birthdays_today(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        result = todays_unannounced(conn, GUILD, 7, 16, "2026-07-16")
    assert result == []


def test_unannounced_mixed_announced_and_not(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        upsert_birthday(conn, GUILD, USER_B, 7, 15, MOD)
        mark_announced(conn, GUILD, USER_A, "2026-07-15")
        result = todays_unannounced(conn, GUILD, 7, 15, "2026-07-15")
    assert result == [USER_B]


# ── mark_announced ────────────────────────────────────────────────────


def test_mark_announced_returns_true_on_first_call(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        assert mark_announced(conn, GUILD, USER_A, "2026-07-15") is True


def test_mark_announced_idempotent(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        mark_announced(conn, GUILD, USER_A, "2026-07-15")
        assert mark_announced(conn, GUILD, USER_A, "2026-07-15") is False


def test_mark_announced_allows_same_user_different_dates(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        assert mark_announced(conn, GUILD, USER_A, "2025-07-15") is True
        assert mark_announced(conn, GUILD, USER_A, "2026-07-15") is True


# ── birthday_wish quest detector helpers ──────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "happy birthday!",
        "HAPPY BIRTHDAY 🎂🎂🎂",
        "happppy birthdayyyy",
        "happy bday",
        "happy b-day!!",
        "hbd",
        "hope you have a great one — happy cake day",
        "feliz cumpleaños",
        "joyeux anniversaire",
    ],
)
def test_is_birthday_wish_positive(text):
    assert is_birthday_wish(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "happy monday everyone",
        "my birthday is next week",
        "it's their birthday today",  # stating a fact, not wishing
        "hbdx",  # word boundary
        "so happy! birthday plans later",  # split across punctuation
    ],
)
def test_is_birthday_wish_negative(text):
    assert is_birthday_wish(text) is False


def test_announced_birthday_ids_reads_todays_rows_only(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        upsert_birthday(conn, GUILD, USER_B, 7, 15, MOD)
        mark_announced(conn, GUILD, USER_A, "2026-07-15")
        mark_announced(conn, GUILD, USER_B, "2026-07-14")  # yesterday
        assert announced_birthday_ids(conn, GUILD, "2026-07-15") == {USER_A}
        assert announced_birthday_ids(conn, GUILD, "2026-07-13") == set()
        # A quiet birthday (never announced) is never in the set — the
        # privacy gate for the birthday_wish quest.
        assert announced_birthday_ids(conn, 999, "2026-07-15") == set()


# ── announce_hour ─────────────────────────────────────────────────────


def test_announce_hour_defaults_to_nine(db):
    """Untouched guilds keep the historical 09:00 announce."""
    with open_db(db) as conn:
        assert announce_hour(conn, GUILD) == DEFAULT_ANNOUNCE_HOUR == 9


@pytest.mark.parametrize(
    "stored,expected",
    [
        ("0", 0),      # midnight is a real choice, not "unset"
        ("7", 7),
        ("23", 23),
        (" 18 ", 18),  # tolerated whitespace
        ("24", 9),     # out of range → default
        ("-1", 9),
        ("noon", 9),   # garbage → default, never a stalled loop
        ("", 9),
    ],
)
def test_announce_hour_reads_the_guild_dial(db, stored, expected):
    with open_db(db) as conn:
        set_config_value(conn, ANNOUNCE_HOUR_KEY, stored, GUILD)
        assert announce_hour(conn, GUILD) == expected


def test_announce_hour_is_per_guild(db):
    with open_db(db) as conn:
        set_config_value(conn, ANNOUNCE_HOUR_KEY, "17", GUILD)
        assert announce_hour(conn, GUILD) == 17
        # A guild with no row of its own still gets the default.
        assert announce_hour(conn, 424242) == 9


# ── announcement channels — any number, not a fixed main + second ──────


def test_list_channels_is_empty_for_an_unconfigured_guild(db):
    with open_db(db) as conn:
        assert list_channels(conn, GUILD) == []


def test_upsert_channel_adds_a_channel(db):
    with open_db(db) as conn:
        upsert_channel(conn, guild_id=GUILD, channel_id=5555, message="Hi {mention}", pin=True)
        rows = list_channels(conn, GUILD)
    assert len(rows) == 1
    assert rows[0].channel_id == 5555
    assert rows[0].message == "Hi {mention}"
    assert rows[0].pin is True


def test_upsert_channel_is_idempotent_and_updates_in_place(db):
    """Adding the same channel twice edits the existing row rather than
    creating a second one — the Add form and the per-card Save button post to
    the same endpoint."""
    with open_db(db) as conn:
        upsert_channel(conn, guild_id=GUILD, channel_id=5555, message="First", pin=False)
        upsert_channel(conn, guild_id=GUILD, channel_id=5555, message="Second", pin=True)
        rows = list_channels(conn, GUILD)
    assert len(rows) == 1
    assert rows[0].message == "Second"
    assert rows[0].pin is True


def test_channels_list_in_the_order_they_were_added(db):
    with open_db(db) as conn:
        upsert_channel(conn, guild_id=GUILD, channel_id=3333, message="C", pin=False)
        upsert_channel(conn, guild_id=GUILD, channel_id=1111, message="A", pin=False)
        upsert_channel(conn, guild_id=GUILD, channel_id=2222, message="B", pin=False)
        rows = list_channels(conn, GUILD)
    assert [r.channel_id for r in rows] == [3333, 1111, 2222]


def test_delete_channel_removes_it_and_reports_success(db):
    with open_db(db) as conn:
        upsert_channel(conn, guild_id=GUILD, channel_id=5555, message="Hi", pin=False)
        assert delete_channel(conn, GUILD, 5555) is True
        assert list_channels(conn, GUILD) == []


def test_delete_channel_on_an_unconfigured_channel_reports_no_match(db):
    with open_db(db) as conn:
        assert delete_channel(conn, GUILD, 9999) is False


def test_channels_are_scoped_per_guild(db):
    with open_db(db) as conn:
        upsert_channel(conn, guild_id=GUILD, channel_id=5555, message="Guild A", pin=False)
        upsert_channel(conn, guild_id=999, channel_id=5555, message="Guild B", pin=True)

        a = list_channels(conn, GUILD)
        b = list_channels(conn, 999)
    assert len(a) == 1 and a[0].message == "Guild A" and a[0].pin is False
    assert len(b) == 1 and b[0].message == "Guild B" and b[0].pin is True

    # And removing one guild's channel leaves the other's alone.
    with open_db(db) as conn:
        delete_channel(conn, GUILD, 5555)
        assert list_channels(conn, GUILD) == []
        assert len(list_channels(conn, 999)) == 1


# ── the month is picked, the day is typed (ephemeral-UI audit M4) ──────


def test_month_choices_covers_the_calendar_in_order():
    choices = month_choices()
    assert len(choices) == 12  # comfortably inside Discord's 25-option cap
    assert choices[0] == ("January", 1)
    assert choices[-1] == ("December", 12)
    assert [n for _, n in choices] == list(range(1, 13))


@pytest.mark.parametrize(
    ("raw", "month", "expected"),
    [("15", 7, 15), (" 1 ", 1, 1), ("31", 1, 31), ("28", 2, 28), ("30", 4, 30)],
)
def test_parse_birthday_day_accepts_a_day_that_month_has(raw, month, expected):
    assert parse_birthday_day(raw, month) == (expected, None)


@pytest.mark.parametrize(
    ("raw", "month"),
    [("29", 2), ("31", 4), ("0", 7), ("32", 7), ("-3", 7)],
)
def test_parse_birthday_day_rejects_a_day_outside_that_month(raw, month):
    day, err = parse_birthday_day(raw, month)
    assert day is None
    assert err is not None and str(MAX_DAYS[month]) in err


@pytest.mark.parametrize("raw", ["", "  ", "abc", "1.5", "七"])
def test_parse_birthday_day_rejects_a_non_number(raw):
    day, err = parse_birthday_day(raw, 7)
    assert day is None
    assert err == "❌ Day must be a whole number."


def test_birthday_modal_picks_the_month_and_types_the_day():
    """The month select replaces a typed 1–12 and the error it produced."""
    import discord

    from bot_modules.cogs.birthday_cog import _BirthdayModal

    modal = _BirthdayModal(ctx=None)  # type: ignore[arg-type]
    assert isinstance(modal.month, discord.ui.Select)
    assert [o.label for o in modal.month.options] == [
        name for name, _ in month_choices()
    ]
    assert isinstance(modal.day, discord.ui.TextInput)
    assert [label.text for label in modal.children] == [
        "Month", "Day (1–31)", "Birthday request (optional)",
    ]
