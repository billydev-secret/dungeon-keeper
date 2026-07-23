"""Tests for intake_views: pure formatting + post_intake_card return contract.

The interactive button flow is Discord glue tested via the service layer
(test_intake_logic.py); here we pin the pure formatting branches plus the
one contract the join hook depends on — post_intake_card's return value,
which decides whether the legacy arrival ping falls back.
"""

from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.services import intake_service as svc
from bot_modules.services.intake_views import (
    build_intake_embed,
    format_step_lines,
    post_intake_card,
)
from migrations import apply_migrations_sync

GUILD = 42
NEWCOMER = 7
CHANNEL = 555


def _step(key, label, auto_kind="", done_at=None, done_by=None, skipped=0):
    return {
        "step_key": key,
        "label": label,
        "auto_kind": auto_kind,
        "done_at": done_at,
        "done_by": done_by,
        "skipped": skipped,
    }


STEPS = [
    _step("greeted", "Greeted", "greeted", done_at=1_700_000_000.0, done_by=99),
    _step("verified", "Verified", "verified", done_at=1_700_000_100.0, done_by=0),
    _step("sfw_questions", "SFW questions asked"),
    _step("nsfw_questions", "NSFW questions asked", skipped=1),
]


def test_format_step_lines_states():
    lines = format_step_lines(STEPS)
    assert lines[0] == "✅ Greeted — <@99> <t:1700000000:R>"
    # done_by 0 renders as the bot's auto-tick, not a broken mention.
    assert lines[1] == "✅ Verified — auto <t:1700000100:R>"
    assert lines[2] == "⬜ SFW questions asked"
    assert lines[3] == "⏭️ ~~NSFW questions asked~~ — skipped"


def test_build_embed_progress_and_fields():
    embed = build_intake_embed(
        discord.Color.blurple(),
        member_mention="<@7>",
        member_display="newbie#1",
        account_created_ts=1_600_000_000.0,
        inviter_mention="<@55>",
        steps=STEPS,
    )
    # Skipped steps never count toward the bar: 2 of 4 here.
    assert "2/4" in (embed.description or "")
    assert "▰" in (embed.description or "")
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Invited by"] == "<@55>"
    assert "<t:1600000000:R>" in fields["Account created"]
    assert "⬜ SFW questions asked" in fields["Checklist"]
    assert "Resolved" not in fields


def test_build_embed_unknown_inviter_and_missing_account_ts():
    embed = build_intake_embed(
        discord.Color.blurple(),
        member_mention="<@7>",
        member_display="7",
        account_created_ts=None,
        inviter_mention=None,
        steps=STEPS,
    )
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Invited by"] == "unknown"
    assert "Account created" not in fields


# ── post_intake_card return contract (drives the legacy-ping fallback) ─


class _Ctx:
    def __init__(self, db_path):
        self.db_path = db_path

    def open_db(self):
        return open_db(self.db_path)


@pytest.fixture
def ctx(tmp_path):
    path = tmp_path / "views.db"
    apply_migrations_sync(path)
    return _Ctx(path)


def _member(guild):
    return SimpleNamespace(guild=guild, id=NEWCOMER, mention=f"<@{NEWCOMER}>")


def _guild(channel=None):
    return SimpleNamespace(id=GUILD, get_channel=lambda cid: channel)


@pytest.fixture(autouse=True)
def _clean_watch():
    svc._reset_watch_for_tests()
    yield
    svc._reset_watch_for_tests()


async def test_post_card_dark_returns_false(ctx):
    # Intake disabled → the caller must send the legacy arrival ping.
    assert await post_intake_card(ctx, _member(_guild())) is False


async def test_post_card_missing_channel_falls_back(ctx):
    # Regression: a broken/deleted intake channel used to return True and
    # silence arrivals entirely. It must return False (legacy ping fires)
    # and roll the card back so no ghost row sits in the queue.
    with ctx.open_db() as conn:
        set_config_value(conn, svc.ENABLED_KEY, "1", GUILD)
        set_config_value(conn, svc.CHANNEL_KEY, str(CHANNEL), GUILD)
    assert await post_intake_card(ctx, _member(_guild(channel=None))) is False
    with ctx.open_db() as conn:
        assert svc.get_open_card(conn, GUILD, NEWCOMER) is None
    assert svc.is_watched(GUILD, NEWCOMER) is False


async def test_post_card_already_carded_returns_true(ctx):
    # A still-open card from a previous join IS the arrival surface —
    # no legacy ping, and the member goes (back) on the watch set.
    with ctx.open_db() as conn:
        set_config_value(conn, svc.ENABLED_KEY, "1", GUILD)
        set_config_value(conn, svc.CHANNEL_KEY, str(CHANNEL), GUILD)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
    assert await post_intake_card(ctx, _member(_guild())) is True
    assert svc.is_watched(GUILD, NEWCOMER) is True


def test_build_embed_resolved_headlines():
    for resolution, needle in [
        (svc.RESOLUTION_COMPLETED, "welcomed by <@99>"),
        (svc.RESOLUTION_DISMISSED, "Dismissed by <@99>"),
        (svc.RESOLUTION_LEFT, "left the server"),
        (svc.RESOLUTION_BANNED, "banned"),
    ]:
        embed = build_intake_embed(
            discord.Color.blurple(),
            member_mention="<@7>",
            member_display="newbie#1",
            account_created_ts=None,
            inviter_mention=None,
            steps=STEPS,
            resolved=(resolution, "<@99>"),
        )
        resolved = next(f for f in embed.fields if f.name == "Resolved")
        assert needle in resolved.value
