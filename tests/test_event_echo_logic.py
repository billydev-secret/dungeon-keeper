"""Event Echo's rate limiting — the rules that keep main chat a chat channel.

These are the tests that matter for this feature. Everything else is plumbing;
this is the part that decides whether the busiest channel in the server gets
another post, and the failure mode it guards against (a bot flooding a live
channel) has happened here before.
"""
from __future__ import annotations


import discord
import pytest

from bot_modules.core.utils import jump_url
from bot_modules.services.event_echo_logic import (
    CLOSING_LEAD_SECONDS,
    GLOBAL_COOLDOWN_SECONDS,
    PER_TYPE_COOLDOWN_SECONDS,
    SOURCE_AUCTION_CLOSING,
    SOURCE_BOUNTY,
    SOURCE_DISCORD_EVENT,
    SOURCE_PARTY_GAME,
    SOURCE_POOLS_CLOSING,
    SOURCE_RAFFLE_CLOSING,
    build_echo_embed,
    closing_due,
    decide,
    is_fresh,
    spec_for,
)

NOW = 1_800_000_000.0


@pytest.mark.parametrize(
    "last_same_type, last_any, allowed, reason",
    [
        # Nothing has ever been echoed.
        pytest.param(None, None, True, "", id="first-ever"),
        # Both windows long expired.
        pytest.param(NOW - 7200, NOW - 7200, True, "", id="both-windows-clear"),
        # The same game type, just inside its hour.
        pytest.param(
            NOW - (PER_TYPE_COOLDOWN_SECONDS - 60),
            NOW - (PER_TYPE_COOLDOWN_SECONDS - 60),
            False,
            "per_type",
            id="same-type-within-hour",
        ),
        # Same type exactly at the boundary — the window has elapsed, so post.
        pytest.param(
            NOW - PER_TYPE_COOLDOWN_SECONDS,
            NOW - PER_TYPE_COOLDOWN_SECONDS,
            True,
            "",
            id="same-type-at-boundary",
        ),
        # The case per-type alone cannot catch: a *different* game type, well
        # outside its own window, but seconds after some other echo. Without
        # the global floor ~20 game types could post ~20 times in a minute and
        # every one would be within its own limit.
        pytest.param(None, NOW - 60, False, "global", id="different-type-inside-global"),
        # Global floor elapsed, this type never echoed.
        pytest.param(
            None, NOW - GLOBAL_COOLDOWN_SECONDS, True, "", id="different-type-past-global"
        ),
        # Global is checked first: when both would refuse, the reason recorded
        # is the one that actually stopped it.
        pytest.param(NOW - 30, NOW - 30, False, "global", id="both-refuse-global-wins"),
    ],
)
def test_decide_applies_both_cooldowns(last_same_type, last_any, allowed, reason):
    verdict = decide(
        now=NOW, last_same_type=last_same_type, last_any=last_any, deadline=False
    )
    assert verdict.allowed is allowed
    assert verdict.reason == reason



@pytest.mark.parametrize(
    "opened_at, fresh",
    [
        pytest.param(NOW - 10, True, id="just-opened"),
        pytest.param(NOW - 599, True, id="inside-window"),
        pytest.param(NOW - 601, False, id="stale"),
        # A missing timestamp must not silence a game that is demonstrably
        # live — it is in games_active_games, so it exists.
        pytest.param(None, True, id="unknown-open-time-counts-as-fresh"),
    ],
)
def test_is_fresh(opened_at, fresh):
    assert is_fresh(opened_at, NOW) is fresh



def test_jump_url_includes_the_message_id():
    """A channel-only link loses the message — the point is landing *on* the lobby."""
    url = jump_url(111, 222, 333)
    assert url == "https://discord.com/channels/111/222/333"


class TestEchoEmbed:
    def test_links_and_names_the_origin_channel(self):
        embed = build_echo_embed(
            name="Truth or Dare", channel_id=999, url="https://x/1"
        )
        assert "Truth or Dare" in (embed.title or "")
        assert "<#999>" in (embed.description or "")
        assert "https://x/1" in (embed.description or "")

    def test_a_channel_less_echo_renders_no_mention(self):
        """External Discord events have a location string and no channel."""
        embed = build_echo_embed(name="Movie Night", url="https://x/1")
        assert "<#" not in (embed.description or "")
        assert "https://x/1" in (embed.description or "")

    def test_copy_is_derived_from_the_source(self):
        """"A game is open" reads as a bug on an event called Movie Night.

        Keyed off `source` rather than passed alongside it, so the copy can't
        desync from the thing it describes.
        """
        game = build_echo_embed(
            name="Truth or Dare", channel_id=9, url="u", source=SOURCE_PARTY_GAME
        )
        event = build_echo_embed(
            name="Movie Night", channel_id=9, url="u", source=SOURCE_DISCORD_EVENT
        )
        game_spec = spec_for(SOURCE_PARTY_GAME)
        event_spec = spec_for(SOURCE_DISCORD_EVENT)
        assert game_spec != event_spec
        assert game_spec.lead in (game.description or "")
        assert event_spec.lead in (event.description or "")
        assert (game.title or "").startswith(game_spec.icon)
        assert (event.title or "").startswith(event_spec.icon)

    def test_an_unknown_source_falls_back_to_game_copy(self):
        """A new source is one table row; forgetting it must not crash."""
        assert spec_for("something_new") == spec_for(SOURCE_PARTY_GAME)

    def test_deadline_renders_a_relative_timestamp(self):
        """Not "in 1 hour" — an auction's soft close moves the deadline.

        A late bid (which this echo exists to cause) extends ends_at, so any
        fixed phrasing is wrong by the time it's read. Discord's own <t:…:R>
        at least reflects the deadline as it stood at send time.
        """
        embed = build_echo_embed(
            name="A rare hat", channel_id=9, url="u",
            source=SOURCE_AUCTION_CLOSING, deadline_epoch=NOW,
        )
        assert f"<t:{int(NOW)}:R>" in (embed.description or "")

    def test_no_deadline_renders_no_timestamp(self):
        embed = build_echo_embed(name="G", channel_id=9, url="u")
        assert "<t:" not in (embed.description or "")

    @pytest.mark.parametrize(
        "source, expected_in_title",
        [
            pytest.param(SOURCE_PARTY_GAME, "is starting", id="game-starts"),
            pytest.param(SOURCE_BOUNTY, "New bounty", id="bounty-posted"),
            # "Auction X is starting" would be actively wrong on a last-call.
            pytest.param(SOURCE_AUCTION_CLOSING, "Last call", id="auction-closing"),
            pytest.param(SOURCE_POOLS_CLOSING, "Last call", id="pools-closing"),
        ],
    )
    def test_headline_matches_what_actually_happened(self, source, expected_in_title):
        embed = build_echo_embed(name="Thing", url="u", source=source)
        assert expected_in_title in (embed.title or "")
        assert "Thing" in (embed.title or "")


class TestDeadlineSources:
    @pytest.mark.parametrize(
        "source, deadline",
        [
            pytest.param(SOURCE_PARTY_GAME, False, id="game-start"),
            pytest.param(SOURCE_BOUNTY, False, id="new-bounty"),
            pytest.param(SOURCE_AUCTION_CLOSING, True, id="auction-closing"),
            pytest.param(SOURCE_POOLS_CLOSING, True, id="pools-closing"),
            pytest.param(SOURCE_RAFFLE_CLOSING, True, id="raffle-closing"),
        ],
    )
    def test_which_sources_are_deadlines(self, source, deadline):
        assert spec_for(source).deadline is deadline

    def test_a_deadline_echo_cannot_be_crowded_out(self):
        """The rare valuable echo must not lose to a routine one.

        A game echoed 30s ago would refuse anything else under the global
        floor — but "auction ends in an hour" has no useful later moment, so
        dropping it loses it outright.
        """
        blocked = decide(
            now=NOW, last_same_type=NOW - 30, last_any=NOW - 30,
            deadline=spec_for(SOURCE_PARTY_GAME).deadline,
        )
        allowed = decide(
            now=NOW, last_same_type=NOW - 30, last_any=NOW - 30,
            deadline=spec_for(SOURCE_AUCTION_CLOSING).deadline,
        )
        assert blocked.allowed is False
        assert allowed.allowed is True



    @pytest.mark.parametrize(
        "deadline_epoch, due",
        [
            pytest.param(NOW + 30 * 60, True, id="half-an-hour-out"),
            pytest.param(NOW + CLOSING_LEAD_SECONDS, True, id="exactly-at-the-lead"),
            pytest.param(NOW + CLOSING_LEAD_SECONDS + 1, False, id="too-early"),
            # No lower bound: a sweep that was down through the ideal moment
            # should still fire while there is time left to act.
            pytest.param(NOW + 5, True, id="only-seconds-left-still-fires"),
            pytest.param(NOW, False, id="deadline-passed"),
            pytest.param(NOW - 60, False, id="long-gone"),
            pytest.param(None, False, id="no-deadline"),
        ],
    )
    def test_closing_due(self, deadline_epoch, due):
        assert closing_due(deadline_epoch, NOW) is due

    def test_host_is_optional(self):
        assert build_echo_embed(
            name="G", channel_id=1, url="u"
        ).footer.text is None
        assert build_echo_embed(
            name="G", channel_id=1, url="u", host_name="Ada"
        ).footer.text == "Hosted by Ada"

    def test_builds_no_mentions(self):
        """The echo is silent: nothing here may render a ping.

        The sender also passes AllowedMentions.none(), but a mention in the
        copy would still show as a highlighted @ — belt and braces, since the
        whole design rests on this post not notifying anyone.
        """
        embed = build_echo_embed(
            name="Truth or Dare", channel_id=999, url="u", host_name="Ada"
        )
        blob = f"{embed.title}{embed.description}{embed.footer.text}"
        assert "@everyone" not in blob
        assert "@here" not in blob
        assert "<@" not in blob  # user or role mention
        assert "<@&" not in blob

    def test_accent_passthrough_and_fallback(self):
        accent = discord.Color(0x5A32A8)
        assert build_echo_embed(
            name="G", channel_id=1, url="u", color=accent
        ).color == accent
        assert build_echo_embed(name="G", channel_id=1, url="u").color is not None
