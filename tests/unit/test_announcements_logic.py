"""Pure announcement builders: time math + message/mention composition (no I/O)."""

from datetime import datetime, timedelta, timezone

import discord

from bot_modules.services.announcements_service import (
    build_announcement_message,
    compute_post_at,
)

ACCENT = discord.Color(0x00FF00)


def _epoch(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()


def _row(**over):
    row = dict(
        title="Big news", body="Something happened", image_url=None,
        accent_hex=None, plain_text=None, mention_kind="none", mention_role_id=None,
    )
    row.update(over)
    return row


# ── compute_post_at ──────────────────────────────────────────────────────────

def test_utc_offset_zero_is_exact():
    assert compute_post_at("2030-01-01", 18 * 60, 0.0) == _epoch(2030, 1, 1, 18)


def test_negative_offset_shifts_forward():
    # 18:00 local at UTC-7 is 01:00 UTC the next day.
    assert compute_post_at("2030-01-01", 18 * 60, -7.0) == _epoch(2030, 1, 2, 1)


def test_fractional_offset():
    # 09:00 local at UTC+5.5 is 03:30 UTC.
    assert compute_post_at("2030-06-15", 9 * 60, 5.5) == _epoch(2030, 6, 15, 3, 30)


def test_midnight_and_end_of_day_edges():
    assert compute_post_at("2030-01-01", 0, 0.0) == _epoch(2030, 1, 1)
    assert compute_post_at("2030-01-01", 23 * 60 + 59, 0.0) == _epoch(2030, 1, 1, 23, 59)


def test_local_wall_time_preserved_roundtrip():
    epoch = compute_post_at("2030-03-10", 6 * 60, -7.0)
    local = datetime.fromtimestamp(epoch, tz=timezone.utc) + timedelta(hours=-7)
    assert (local.hour, local.minute) == (6, 0)


# ── build_announcement_message: mentions (safety-critical) ──────────────────

def test_kind_none_pings_nothing():
    content, _, allowed = build_announcement_message(_row(), ACCENT)
    assert content is None
    assert allowed.everyone is False
    assert allowed.roles is False
    assert allowed.users is False


def test_kind_role_pings_exactly_that_role():
    content, _, allowed = build_announcement_message(
        _row(mention_kind="role", mention_role_id=555), ACCENT
    )
    assert content == "<@&555>"
    assert allowed.everyone is False
    assert [r.id for r in allowed.roles] == [555]


def test_kind_everyone_pings_everyone_only():
    content, _, allowed = build_announcement_message(
        _row(mention_kind="everyone"), ACCENT
    )
    assert content == "@everyone"
    assert allowed.everyone is True
    assert allowed.roles is False


def test_kind_role_without_id_degrades_to_no_ping():
    content, _, allowed = build_announcement_message(
        _row(mention_kind="role", mention_role_id=None), ACCENT
    )
    assert content is None
    assert allowed.everyone is False and allowed.roles is False


def test_plain_text_without_mention():
    content, _, allowed = build_announcement_message(
        _row(plain_text="Heads up!"), ACCENT
    )
    assert content == "Heads up!"
    assert allowed.everyone is False and allowed.roles is False


def test_mention_prefixes_plain_text():
    content, _, _ = build_announcement_message(
        _row(mention_kind="everyone", plain_text="Event tonight"), ACCENT
    )
    assert content == "@everyone Event tonight"


# ── build_announcement_message: embed ────────────────────────────────────────

def test_embed_carries_title_body_image():
    _, embed, _ = build_announcement_message(
        _row(image_url="https://example.com/x.png"), ACCENT
    )
    assert embed.title == "Big news"
    assert embed.description == "Something happened"
    assert embed.image.url == "https://example.com/x.png"


def test_accent_hex_override_wins():
    _, embed, _ = build_announcement_message(_row(accent_hex="FF0000"), ACCENT)
    assert embed.color.value == 0xFF0000


def test_accent_hex_with_hash_parses():
    _, embed, _ = build_announcement_message(_row(accent_hex="#0000FF"), ACCENT)
    assert embed.color.value == 0x0000FF


def test_blank_accent_falls_back_to_server_color():
    _, embed, _ = build_announcement_message(_row(accent_hex=None), ACCENT)
    assert embed.color.value == ACCENT.value


def test_garbage_accent_falls_back_to_server_color():
    _, embed, _ = build_announcement_message(_row(accent_hex="not-hex"), ACCENT)
    assert embed.color.value == ACCENT.value
