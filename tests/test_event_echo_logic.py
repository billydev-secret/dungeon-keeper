"""Event Echo's rate limiting — the rules that keep main chat a chat channel.

These are the tests that matter for this feature. Everything else is plumbing;
this is the part that decides whether the busiest channel in the server gets
another post, and the failure mode it guards against (a bot flooding a live
channel) has happened here before.
"""
from __future__ import annotations

import discord
import pytest

from bot_modules.services.event_echo_logic import (
    GLOBAL_COOLDOWN_SECONDS,
    PER_TYPE_COOLDOWN_SECONDS,
    build_echo_embed,
    decide,
    is_fresh,
    jump_url,
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
    verdict = decide(now=NOW, last_same_type=last_same_type, last_any=last_any)
    assert verdict.allowed is allowed
    assert verdict.reason == reason


def test_decide_honours_custom_windows():
    """The windows are parameters, so a caller can tighten them without a fork."""
    assert decide(
        now=NOW, last_same_type=NOW - 120, last_any=NOW - 120,
        per_type_seconds=60, global_seconds=30,
    ).allowed


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


def test_freshness_suppresses_a_restart_backlog():
    """After downtime the sweep sees every open game at once; old ones stay quiet."""
    opened = [NOW - 30, NOW - 4000, NOW - 9000]
    assert [is_fresh(t, NOW) for t in opened] == [True, False, False]


def test_jump_url_includes_the_message_id():
    """A channel-only link loses the message — the point is landing *on* the lobby."""
    url = jump_url(111, 222, 333)
    assert url == "https://discord.com/channels/111/222/333"


class TestEchoEmbed:
    def test_links_and_names_the_origin_channel(self):
        embed = build_echo_embed(
            game_name="Truth or Dare", channel_id=999, url="https://x/1"
        )
        assert "Truth or Dare" in (embed.title or "")
        assert "<#999>" in (embed.description or "")
        assert "https://x/1" in (embed.description or "")

    def test_host_is_optional(self):
        assert build_echo_embed(
            game_name="G", channel_id=1, url="u"
        ).footer.text is None
        assert build_echo_embed(
            game_name="G", channel_id=1, url="u", host_name="Ada"
        ).footer.text == "Hosted by Ada"

    def test_builds_no_mentions(self):
        """The echo is silent: nothing here may render a ping.

        The sender also passes AllowedMentions.none(), but a mention in the
        copy would still show as a highlighted @ — belt and braces, since the
        whole design rests on this post not notifying anyone.
        """
        embed = build_echo_embed(
            game_name="Truth or Dare", channel_id=999, url="u", host_name="Ada"
        )
        blob = f"{embed.title}{embed.description}{embed.footer.text}"
        assert "@everyone" not in blob
        assert "@here" not in blob
        assert "<@" not in blob  # user or role mention
        assert "<@&" not in blob

    def test_accent_passthrough_and_fallback(self):
        accent = discord.Color(0x5A32A8)
        assert build_echo_embed(
            game_name="G", channel_id=1, url="u", color=accent
        ).color == accent
        assert build_echo_embed(game_name="G", channel_id=1, url="u").color is not None
