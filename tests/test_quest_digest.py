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
    assert "✅ Ready to claim!" in block
    assert "resets tomorrow" in block  # daily fallback blurb


def test_long_description_is_clipped():
    block = quest_block(
        {"title": "Wordy", "qtype": "daily", "state": "claimable", "description": "x" * 400}
    )
    assert "…" in block
    assert len(block) < 400


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
