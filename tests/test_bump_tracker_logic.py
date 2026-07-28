"""Bump Tracker detection — classifying real listing-bot messages.

The message shapes below are the exact ones the four live listing bots post,
recovered from the bump channel's stored history and from screenshots of the
cases the store never captured.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot_modules.bump_tracker.detector_logic import (
    classify,
    component_texts,
    match_site,
    message_text,
)

# ── Real message fixtures ─────────────────────────────────────────────────────

DISBOARD_BOT = 302050872383242240
DISCADIA_BOT = 1222548162741538938
DISCODUS_BOT = 1159147139960676422
DH_BUMP_BOT = 826100334534328340


def embed(*, title=None, description=None, footer=None, fields=()):
    return SimpleNamespace(
        title=title,
        description=description,
        footer=SimpleNamespace(text=footer) if footer else None,
        author=None,
        fields=[SimpleNamespace(name=n, value=v) for n, v in fields],
    )


def msg(*, content=None, embeds=(), components=()):
    return SimpleNamespace(content=content, embeds=list(embeds), components=list(components))


def text_display(content):
    """A Components V2 leaf — the only node type carrying visible text."""
    return SimpleNamespace(content=content)


def container(*children):
    return SimpleNamespace(children=list(children))


def section(*children, accessory=None):
    return SimpleNamespace(children=list(children), accessory=accessory)


# Discodus posts a classic embed and reuses one title for both outcomes.
DISCODUS_SUCCESS = msg(
    content=(
        "DISCODUS: Discord Server Booster\n"
        "Bump done! :point_right: Check it out on [DISCODUS](https://discodus.com/server/1).\n"
        "You can bump this server again <t:1785219124:R>"
    ),
    embeds=[
        embed(
            title="DISCODUS: Discord Server Booster",
            description=(
                "Bump done! :point_right: Check it out on "
                "[DISCODUS](https://discodus.com/server/1).\n"
                "You can bump this server again <t:1785219124:R>"
            ),
        )
    ],
)

DISCODUS_FAILURE = msg(
    embeds=[
        embed(
            title="DISCODUS: Discord Server Booster",
            description=(
                ":hourglass: Server has been already bumped.\n"
                "You can bump again <t:1785219124:R> !"
            ),
        )
    ],
)

# Discadia's refusal is plain content; its successes arrive as Components V2
# with nothing readable, which is why its detector pattern is empty.
DISCADIA_FAILURE = msg(
    content=":warning: Already bumped recently, please try again <t:1783485678:R>."
)

DISCADIA_SUCCESS = msg(components=[container(text_display("Thanks for bumping Discadia!"))])

# DH Bump is Components V2 for both outcomes — empty content, zero embeds.
DH_SUCCESS = msg(
    components=[
        container(
            section(text_display("## :rocket: Bumped")),
            text_display("Your bump is live and the listing has been refreshed."),
            text_display(":green_circle: **Fresh boost delivered.**"),
            section(text_display("## :bar_chart: Bump Stats")),
            text_display(":stopwatch: Next bump: `120m`"),
        )
    ]
)

DH_FAILURE = msg(
    components=[
        container(
            text_display("**Bump Error!**"),
            text_display(
                "Unfortunately, **1 hours 59 minutes 53 seconds** left until you "
                "can bump again :(\nUpgrade to premium to enable auto-bumps."
            ),
        )
    ]
)


# ── message_text ──────────────────────────────────────────────────────────────


def test_message_text_reads_plain_content():
    assert "already bumped recently" in message_text(DISCADIA_FAILURE)


def test_message_text_reads_embed_title_and_description():
    text = message_text(DISCODUS_SUCCESS)
    assert "discodus: discord server booster" in text
    assert "bump done!" in text


def test_message_text_reads_embed_footer_and_fields():
    text = message_text(
        msg(embeds=[embed(footer="bumped by billy", fields=[("Streak", "10 total")])])
    )
    assert "bumped by billy" in text
    assert "streak" in text
    assert "10 total" in text


def test_message_text_reads_components_v2():
    """DH Bump's text lives only here — content and embeds are both empty."""
    text = message_text(DH_SUCCESS)
    assert "your bump is live and the listing has been refreshed." in text
    assert "next bump" in text


def test_message_text_is_lowercased():
    assert message_text(msg(content="Bump DONE!")) == "bump done!"


def test_message_text_survives_a_message_with_nothing_readable():
    assert message_text(msg()) == ""


# ── component_texts ───────────────────────────────────────────────────────────


def test_component_texts_walks_nested_containers_and_sections():
    tree = [container(section(text_display("inner")), text_display("outer"))]
    assert component_texts(tree) == ["inner", "outer"]


def test_component_texts_reads_a_section_accessory():
    tree = [section(text_display("body"), accessory=text_display("side"))]
    assert component_texts(tree) == ["body", "side"]


def test_component_texts_ignores_components_without_text():
    button = SimpleNamespace(label="Get Premium", url="https://example.invalid")
    assert component_texts([container(button, text_display("kept"))]) == ["kept"]


def test_component_texts_stops_on_a_pathological_tree():
    """A cyclic tree must not hang the message handler."""
    node = SimpleNamespace(content="x", children=[])
    node.children.append(node)
    assert component_texts([node])  # terminates, depth-capped


# ── classify ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("label", "message", "detector_pattern", "failure_pattern", "expected"),
    [
        ("discodus bumped", DISCODUS_SUCCESS, "bump done!", "already bumped", "success"),
        ("discodus refused", DISCODUS_FAILURE, "bump done!", "already bumped", "failure"),
        ("dh bumped", DH_SUCCESS, "your bump is live", "bump error!", "success"),
        ("dh refused", DH_FAILURE, "your bump is live", "bump error!", "failure"),
        ("discadia bumped", DISCADIA_SUCCESS, "", "already bumped recently", "success"),
        ("discadia refused", DISCADIA_FAILURE, "", "already bumped recently", "failure"),
    ],
)
def test_classify_separates_every_live_bots_outcomes(
    label, message, detector_pattern, failure_pattern, expected
):
    outcome = classify(
        message_text(message),
        detector_pattern=detector_pattern,
        failure_pattern=failure_pattern,
    )
    assert outcome == expected, label


def test_empty_detector_pattern_matches_anything():
    """Every live guild was configured this way — it must keep working."""
    assert classify("anything at all", detector_pattern="", failure_pattern="") == "success"


def test_failure_pattern_vetoes_an_otherwise_matching_success():
    """The refusal echoes the success wording, so the veto must be checked first."""
    text = "bump done, but you already bumped recently"
    assert (
        classify(text, detector_pattern="bump done", failure_pattern="already bumped recently")
        == "failure"
    )


def test_unmatched_detector_pattern_is_not_a_bump():
    assert classify("some chatter", detector_pattern="bump done!", failure_pattern="") is None


def test_matching_is_case_insensitive():
    assert (
        classify(message_text(msg(content="BUMP DONE!")), detector_pattern="Bump Done!", failure_pattern="")
        == "success"
    )


# ── match_site ────────────────────────────────────────────────────────────────


def site(name, bot_id, pattern="", failure=""):
    return {
        "site_name": name,
        "detector_bot_id": bot_id,
        "detector_pattern": pattern,
        "failure_pattern": failure,
    }


LIVE_SITES = [
    site("disboard", DISBOARD_BOT),
    site("discadia", DISCADIA_BOT, failure="already bumped recently"),
    site("discodus", DISCODUS_BOT, "bump done!", "server has been already bumped"),
    site("discord home", DH_BUMP_BOT, "your bump is live", "bump error!"),
]


@pytest.mark.parametrize(
    ("bot_id", "message", "expected"),
    [
        (DISCODUS_BOT, DISCODUS_SUCCESS, ("discodus", "success")),
        (DISCODUS_BOT, DISCODUS_FAILURE, ("discodus", "failure")),
        (DH_BUMP_BOT, DH_SUCCESS, ("discord home", "success")),
        (DH_BUMP_BOT, DH_FAILURE, ("discord home", "failure")),
        (DISCADIA_BOT, DISCADIA_SUCCESS, ("discadia", "success")),
        (DISCADIA_BOT, DISCADIA_FAILURE, ("discadia", "failure")),
    ],
)
def test_match_site_resolves_each_bot_to_its_site_and_outcome(bot_id, message, expected):
    assert match_site(LIVE_SITES, bot_id, message_text(message)) == expected


def test_discadia_refusal_no_longer_counts_as_a_bump():
    """Regression: this exact message reset the 24h timer in production.

    Discadia's detector_pattern is empty ("anything from this bot"), so before
    the failure veto existed its public refusal was logged as a bump.
    """
    text = message_text(DISCADIA_FAILURE)

    # The pre-fix configuration, still what the row holds until migration 137
    # seeds it: no failure pattern, so the refusal reads as a bump.
    assert classify(text, detector_pattern="", failure_pattern="") == "success"

    assert match_site(LIVE_SITES, DISCADIA_BOT, text) == ("discadia", "failure")


def test_unknown_bot_matches_nothing():
    assert match_site(LIVE_SITES, 999, message_text(DISCODUS_SUCCESS)) is None


def test_a_members_message_matches_nothing():
    assert match_site(LIVE_SITES, 42, "bump done!") is None


def test_two_sites_sharing_one_bot_are_told_apart_by_pattern():
    sites = [
        site("alpha", 7, "listed on alpha"),
        site("beta", 7, "listed on beta"),
    ]
    assert match_site(sites, 7, "now listed on beta") == ("beta", "success")


def test_site_whose_pattern_misses_falls_through_to_the_next():
    sites = [site("alpha", 7, "never appears"), site("beta", 7, "")]
    assert match_site(sites, 7, "unrelated text") == ("beta", "success")


def test_null_patterns_from_the_database_are_treated_as_empty():
    """Pre-migration rows read back as NULL rather than ''."""
    sites = [{
        "site_name": "legacy",
        "detector_bot_id": 7,
        "detector_pattern": None,
        "failure_pattern": None,
    }]
    assert match_site(sites, 7, "anything") == ("legacy", "success")
