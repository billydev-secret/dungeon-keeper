"""Tests for bot_modules/economy/quest_digest.py — the login-digest layout.

Pure string formatting: aligned monospace meters, per-quest blurbs + channel
links, cadence grouping with no cap, the "biggest movers" section, and the
≤1024-char field packing that keeps every embed field legal.
"""

from __future__ import annotations

from bot_modules.economy import quest_digest as qd
from bot_modules.economy.quest_digest import bar_meter, digest_sections, quest_block


# ── bar_meter ─────────────────────────────────────────────────────────


def test_bar_meter_is_monospace_with_spaced_counts():
    meter = bar_meter(2196, 16635)
    assert meter.startswith("`") and meter.endswith("`")
    assert "2,196 / 16,635" in meter
    # A fixed 10-cell bar so meters line up down the column.
    assert meter.count("▰") + meter.count("▱") == 10


def test_bar_meter_zero_target_shows_bare_count():
    assert bar_meter(5, 0) == "`5`"


# ── quest_block ───────────────────────────────────────────────────────


def test_community_block_has_title_bar_and_blurb():
    block = quest_block(
        {
            "title": "Server Buzz",
            "qtype": "community",
            "state": "community",
            "current": 2196,
            "target": 16635,
            "description": "Keep the whole server chatting.",
        }
    )
    lines = block.split("\n")
    assert lines[0] == "🔹 **Server Buzz**"
    assert lines[1].startswith("`") and "2,196 / 16,635" in lines[1]
    assert lines[2] == "_Keep the whole server chatting._"


def test_block_renders_channel_link_when_scoped():
    block = quest_block(
        {
            "title": "Photo of the Day",
            "qtype": "event",
            "state": "photo_post",
            "description": "Post a photo to earn.",
            "trigger_channel_id": 42,
        }
    )
    assert "_Post a photo to earn._" in block
    assert "→ <#42>" in block


def test_counted_block_uses_progress_meter():
    block = quest_block(
        {
            "title": "Talk It Out",
            "qtype": "daily",
            "state": "message_count",
            "progress_current": 3,
            "progress_target": 10,
        }
    )
    assert "3 / 10" in block


def test_block_falls_back_to_cadence_blurb_without_description():
    block = quest_block({"title": "Mystery", "qtype": "daily", "state": "claimable"})
    assert "🎁 Ready to claim!" in block
    assert "resets tomorrow" in block  # daily fallback blurb


def test_long_description_is_clipped():
    block = quest_block(
        {"title": "Wordy", "qtype": "daily", "state": "claimable", "description": "x" * 400}
    )
    assert "…" in block
    assert len(block) < 400


def test_done_block_ticks_off_with_a_full_bar():
    """A finished quest reads as an achievement, not as a job still to do.

    The login card is edited in place all day, so a completed quest stays on
    it — with the bar filled, the tick as its bullet, and none of the "go do
    this in #channel" framing that only makes sense while it is open.
    """
    block = quest_block(
        {
            "title": "Talk It Out",
            "qtype": "daily",
            "state": "done",
            "progress_current": 10,
            "progress_target": 10,
            "description": "Chat after midnight.",
            "trigger_channel_id": 42,
        }
    )
    lines = block.split("\n")
    assert lines[0] == "✅ **Talk It Out**"
    assert "10 / 10" in lines[1]
    assert lines[2] == "_Done — nice work._"
    # No channel link: inviting someone to go earn a quest they already earned
    # is worse than saying nothing.
    assert "<#42>" not in block


def test_done_block_clamps_an_overshot_bar():
    """Progress keeps counting past the target; a 12 / 10 bar looks like a bug."""
    block = quest_block(
        {
            "title": "Chatterbox",
            "qtype": "daily",
            "state": "done",
            "progress_current": 12,
            "progress_target": 10,
        }
    )
    assert "10 / 10" in block
    assert "12" not in block


def test_done_block_without_a_target_is_just_a_ticked_line():
    block = quest_block({"title": "Early Bird", "qtype": "daily", "state": "done"})
    assert block == "✅ **Early Bird**\n_Done — nice work._"


def test_done_tick_is_not_the_claimable_glyph():
    """One glyph for "you did it" and "you still have to press a button" would
    make a member skip a payout they earned."""
    done = quest_block({"title": "A", "qtype": "daily", "state": "done"})
    claimable = quest_block({"title": "A", "qtype": "daily", "state": "claimable"})
    assert done.splitlines()[0].startswith("✅")
    assert "✅" not in claimable


# ── digest_sections ───────────────────────────────────────────────────


def test_sections_show_every_open_quest_grouped_no_cap():
    quests = [
        {"title": f"D{i}", "qtype": "daily", "state": "claimable"} for i in range(8)
    ] + [
        {"title": "Weekly One", "qtype": "weekly", "state": "claimable"},
        {
            "title": "Goal",
            "qtype": "community",
            "state": "community",
            "current": 5,
            "target": 10,
        },
        {"title": "Finished", "qtype": "daily", "state": "done"},
    ]
    sections = digest_sections(quests, gains=[])
    headings = [name for name, _ in sections]
    assert "🎯 Daily Quests" in headings
    assert "📅 Weekly Quests" in headings
    assert "🌍 Community Goals" in headings
    joined = "\n".join(v for _, v in sections)
    for i in range(8):  # nothing dropped, no "…and N more"
        assert f"D{i}" in joined
    assert "more" not in joined.lower()
    assert "Finished" not in joined  # done quests excluded


def test_movers_section_leads_and_ranks():
    gains = [{"title": "Server Buzz", "gain": 800}, {"title": "Talk It Out", "gain": 50}]
    sections = digest_sections([], gains=gains)
    assert sections[0][0] == qd.MOVERS_HEADING
    value = sections[0][1]
    assert "🥇 **Server Buzz** +800" in value
    assert "🥈 **Talk It Out** +50" in value


def test_no_quests_no_gains_is_empty():
    assert digest_sections([], gains=[]) == []


def test_oversized_group_splits_into_legal_fields():
    quests = [
        {
            "title": f"Quest {i}",
            "qtype": "daily",
            "state": "claimable",
            "description": "x" * 150,
        }
        for i in range(12)
    ]
    sections = digest_sections(quests, gains=[])
    daily = [(n, v) for n, v in sections if n.startswith("🎯 Daily Quests")]
    assert len(daily) >= 2  # one field would overrun 1024 chars
    assert any(n.endswith("(cont.)") for n, _ in daily)
    for _, value in daily:
        assert len(value) <= qd.FIELD_LIMIT


def test_weekly_community_goals_sort_before_the_monthly_goal():
    # Near-term first: the weekly community goals lead, the month-long goal
    # anchors the foot — mirroring the /bank quests board's section order.
    quests = [
        {"title": "Month Goal", "qtype": "monthly", "state": "community",
         "current": 1, "target": 10},
        {"title": "Week Goal", "qtype": "community", "state": "community",
         "current": 1, "target": 10},
    ]
    headings = [name for name, _ in digest_sections(quests, gains=[])]
    assert headings.index("🌍 Community Goals") < headings.index(
        "🗓️ Monthly Quests"
    )


def test_sections_drop_done_quests_by_default():
    """The default rendering is unchanged — only the login card opts in."""
    quests = [
        {"title": "Open", "qtype": "daily", "state": "claimable"},
        {"title": "Finished", "qtype": "daily", "state": "done"},
    ]
    body = "".join(v for _, v in digest_sections(quests, gains=[]))
    assert "Open" in body
    assert "Finished" not in body


def test_include_done_keeps_finished_quests_in_board_order():
    """Finishing a quest ticks it in place; it must not jump up or down the
    list, because the member is reading the same card all day."""
    quests = [
        {"title": "First", "qtype": "daily", "state": "done"},
        {"title": "Second", "qtype": "daily", "state": "claimable"},
        {"title": "Third", "qtype": "daily", "state": "done"},
    ]
    body = "".join(v for _, v in digest_sections(quests, gains=[], include_done=True))
    assert body.index("First") < body.index("Second") < body.index("Third")


def test_include_done_keeps_a_fully_cleared_card_from_emptying():
    """The bug this option exists to stop: the members who did the most would
    otherwise watch their card shrink to nothing."""
    quests = [
        {"title": "All", "qtype": "daily", "state": "done"},
        {"title": "Gone", "qtype": "weekly", "state": "done"},
    ]
    assert digest_sections(quests, gains=[]) == []
    kept = digest_sections(quests, gains=[], include_done=True)
    body = "".join(v for _, v in kept)
    assert "All" in body and "Gone" in body
