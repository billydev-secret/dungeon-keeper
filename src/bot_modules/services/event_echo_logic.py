"""Event Echo — the pure half: cooldown arithmetic, link building, copy.

Nothing here touches the database or Discord, so the rate-limiting rules that
decide whether the busiest channel in the server gets another post are unit
tests rather than something you find out about in production.

See ``event_echo_service`` for the I/O half and ``docs/event_echo_spec.md``
for the feature.

**Two cooldowns, both required to pass.** The per-type window stops the same
game being announced over and over; the global floor stops twelve *different*
games doing collectively what one game was forbidden from doing alone. Only
the second is a real defence against the failure mode this feature was
designed around — with ~20 party-game types, per-type alone permits a burst of
twenty posts in a minute, every one of them technically within its own window.

**Echoes are silent by design.** Nothing in this module builds a mention, and
the sender passes ``AllowedMentions.none()``. There is deliberately no ping
setting: an unenforced toggle is worse than no toggle (CLAUDE.md), and a role
ping here has to be opt-in or it is hostile — that is a feature to add on
purpose, not a flag to leave lying around.
"""
from __future__ import annotations

from dataclasses import dataclass

import discord

from bot_modules.services.branding_service import DEFAULT_ACCENT

# Sources. These are the `source` column's domain and also the dedupe
# namespace — a Gamebot message id and a party game's uuid can never collide,
# but keeping them in separate namespaces means a future source can reuse an
# id shape without thinking about it.
SOURCE_PARTY_GAME = "party_game"
SOURCE_GAMEBOT = "gamebot"
SOURCE_DISCORD_EVENT = "discord_event"

# Ben's numbers (2026-07-28): same game type at most hourly, and nothing at
# all within ten minutes of the last echo whatever it was. Ceiling is ~6/hour;
# the realistic rate on the observed game volume is 3–5 a day.
PER_TYPE_COOLDOWN_SECONDS = 3600
GLOBAL_COOLDOWN_SECONDS = 600

# How stale an open lobby may be and still be worth announcing. The poll loop
# picks games up within POLL_SECONDS of them opening, so this only matters
# after downtime: on restart the sweep sees every currently-open game at once,
# and without a freshness bound it would announce a batch of games that opened
# while the bot was down — several of them already finished by then.
FRESHNESS_SECONDS = 600

# Rows older than this are pruned. Only the newest row per key is ever read,
# so the tail is pure history; a day is plenty to explain "why didn't X get
# echoed" while keeping the table trivially small.
RETENTION_SECONDS = 86400


# Per-source copy, keyed by the source the caller already passes. "A game is
# open" reads as a bug on an event called "Movie Night", but threading `lead`
# and `icon` through as separate arguments let them desync from the source
# they describe (`source=SOURCE_GAMEBOT, icon=ICON_EVENT` was a legal call).
# A fourth source is now one row here rather than two more parameters.
@dataclass(frozen=True)
class EchoStyle:
    lead: str
    icon: str


_DEFAULT_STYLE = EchoStyle(lead="A game is open", icon="🎲")
SOURCE_STYLE: dict[str, EchoStyle] = {
    SOURCE_DISCORD_EVENT: EchoStyle(lead="It's happening", icon="📅"),
}


def style_for(source: str) -> EchoStyle:
    """Copy for one source; anything game-shaped gets the default."""
    return SOURCE_STYLE.get(source, _DEFAULT_STYLE)


# Which Gamebot sub-games are worth main chat — policy only. The *names* live
# in games_external.parser, which owns Gamebot's vocabulary; `game_from_start`
# also recognises Connect 4 and Anagrams, which are two-player/quickfire.
GAMEBOT_ECHO_GAMES = frozenset({"cah"})


@dataclass(frozen=True)
class EchoDecision:
    """Whether to post, and — when not — which rule refused.

    ``reason`` is carried rather than dropped so the suppressed row records
    *why*, which is the difference between "the cooldown is working" and "the
    feature is silently broken" when you come back to it in a week.
    """

    allowed: bool
    reason: str = ""


def decide(
    *, now: float, last_same_type: float | None, last_any: float | None
) -> EchoDecision:
    """Apply both cooldowns to a candidate echo.

    ``last_same_type`` / ``last_any`` are epoch seconds of the most recent
    *posted* echo (suppressed rows are excluded by the caller — see the
    migration's note on why a refusal must not push the window out).
    ``None`` means "never", which passes.
    """
    if last_any is not None and now - last_any < GLOBAL_COOLDOWN_SECONDS:
        return EchoDecision(False, "global")
    if last_same_type is not None and now - last_same_type < PER_TYPE_COOLDOWN_SECONDS:
        return EchoDecision(False, "per_type")
    return EchoDecision(True)


def is_fresh(opened_at: float | None, now: float) -> bool:
    """True when a lobby opened recently enough to be worth announcing.

    An unknown open time (``None``) counts as fresh: the row exists in
    ``games_active_games``, so the game is live, and refusing to announce a
    live game over a missing timestamp is the wrong way to be wrong.
    """
    if opened_at is None:
        return True
    return now - opened_at <= FRESHNESS_SECONDS


def build_echo_embed(
    *,
    game_name: str,
    url: str,
    channel_id: int | None = None,
    host_name: str | None = None,
    source: str = SOURCE_PARTY_GAME,
    color: discord.Color | None = None,
) -> discord.Embed:
    """The echo itself: what's starting, where, and a link to go there.

    Deliberately small — this lands in a conversation channel, and a tall
    embed in the middle of a chat is the thing people mute. No thumbnail, no
    fields, one line of body.

    ``channel_id`` is optional because an external Discord event carries a
    location string and no channel; interpolating anything else into ``<#…>``
    renders as a mention Discord can't resolve.
    """
    style = style_for(source)
    where = f" in <#{channel_id}>" if channel_id is not None else ""
    embed = discord.Embed(
        title=f"{style.icon} {game_name} is starting",
        description=f"{style.lead}{where}.\n**[Jump in →]({url})**",
        color=color or discord.Color(DEFAULT_ACCENT),
    )
    if host_name:
        embed.set_footer(text=f"Hosted by {host_name}")
    return embed
