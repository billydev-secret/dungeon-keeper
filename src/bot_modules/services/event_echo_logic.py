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
SOURCE_BOUNTY = "bounty"
SOURCE_AUCTION_CLOSING = "auction_closing"
SOURCE_POOLS_CLOSING = "pools_closing"
SOURCE_RAFFLE_CLOSING = "raffle_closing"

# How long before a deadline the "last chance" echo fires.
CLOSING_LEAD_SECONDS = 3600

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


# Everything that varies per source, keyed by the source the caller already
# passes. "A game is open" reads as a bug on an event called "Movie Night",
# but threading `lead` and `icon` through as separate arguments let them
# desync from the source they describe. A new source is one row here.
@dataclass(frozen=True)
class SourceSpec:
    headline: str  # format string over {name}
    lead: str
    icon: str
    #: A *deadline* echo — "last chance", not "this just started".
    #:
    #: These bypass the cooldowns entirely (the per-ref claim still fires them
    #: exactly once). Skip-don't-queue is right for a game start: miss one and
    #: another comes along in an hour. It is wrong for a deadline, because
    #: there is no useful later moment — an "auction ends in an hour" dropped
    #: because a party game echoed 8 minutes ago is simply lost. The floor
    #: exists to stop ~20 game types bursting; auctions and pools fire a
    #: handful of times a year (2 auctions in the server's whole history), so
    #: exempting them costs nothing and only ever saves the valuable ones.
    deadline: bool = False


_DEFAULT_SPEC = SourceSpec(
    headline="{name} is starting", lead="A game is open", icon="🎲"
)
SOURCE_SPECS: dict[str, SourceSpec] = {
    SOURCE_DISCORD_EVENT: SourceSpec(
        headline="{name} is starting", lead="It's happening", icon="📅"
    ),
    SOURCE_BOUNTY: SourceSpec(
        headline="New bounty: {name}", lead="Up for grabs", icon="🎯"
    ),
    SOURCE_AUCTION_CLOSING: SourceSpec(
        headline="Last call: {name}",
        lead="Bidding closes soon",
        icon="🔨",
        deadline=True,
    ),
    SOURCE_POOLS_CLOSING: SourceSpec(
        headline="Last call: {name}",
        lead="Betting closes soon",
        icon="📈",
        deadline=True,
    ),
    SOURCE_RAFFLE_CLOSING: SourceSpec(
        headline="Last call: {name}",
        lead="Ticket sales close",
        icon="🎟️",
        deadline=True,
    ),
}


def spec_for(source: str) -> SourceSpec:
    """Copy and policy for one source; anything unknown gets game-shaped copy."""
    return SOURCE_SPECS.get(source, _DEFAULT_SPEC)


def closing_due(deadline_epoch: float | None, now: float) -> bool:
    """True once a deadline is near enough to be worth a last-chance echo.

    Deliberately has no lower bound: a sweep that was down through the ideal
    moment should still fire on its next tick while there is any time left to
    act. Past the deadline there is nothing to link to, so it stops.
    """
    if deadline_epoch is None:
        return False
    return 0 < deadline_epoch - now <= CLOSING_LEAD_SECONDS


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
    *,
    now: float,
    last_same_type: float | None,
    last_any: float | None,
    deadline: bool,
) -> EchoDecision:
    """Apply both cooldowns to a candidate echo.

    ``last_same_type`` / ``last_any`` are epoch seconds of the most recent
    *posted* echo (suppressed rows are excluded by the caller — see the
    migration's note on why a refusal must not push the window out).
    ``None`` means "never", which passes.

    ``deadline`` skips both windows — see ``SourceSpec.deadline``, which the
    caller resolves. Passed rather than looked up here so a function about
    clocks has no opinion about headlines, and so a caller can't silently get
    game policy by forgetting an argument. The per-ref claim still holds a
    deadline echo to one apiece, so "exempt" means "can't be crowded out",
    not "can repeat".
    """
    if deadline:
        return EchoDecision(True)
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
    name: str,
    url: str,
    channel_id: int | None = None,
    host_name: str | None = None,
    source: str = SOURCE_PARTY_GAME,
    deadline_epoch: float | None = None,
    color: discord.Color | None = None,
) -> discord.Embed:
    """The echo itself: what's happening, where, and a link to go there.

    Deliberately small — this lands in a conversation channel, and a tall
    embed in the middle of a chat is the thing people mute. No thumbnail, no
    fields, one line of body.

    ``channel_id`` is optional because an external Discord event carries a
    location string and no channel; interpolating anything else into ``<#…>``
    renders as a mention Discord can't resolve.

    ``deadline_epoch`` renders as Discord's own relative timestamp rather than
    a baked-in "in 1 hour". An auction's soft close *extends* ``ends_at`` when
    a late bid lands — which this echo is trying to cause — so any fixed
    phrasing would be wrong by the time someone read it. ``<t:…:R>`` at least
    reflects the deadline as it stood when the message was sent, and Discord
    renders it in the reader's own timezone.
    """
    spec = spec_for(source)
    where = f" in <#{channel_id}>" if channel_id is not None else ""
    when = f" <t:{int(deadline_epoch)}:R>" if deadline_epoch is not None else ""
    embed = discord.Embed(
        title=f"{spec.icon} {spec.headline.format(name=name)}",
        description=f"{spec.lead}{where}{when}.\n**[Jump in →]({url})**",
        color=color or discord.Color(DEFAULT_ACCENT),
    )
    if host_name:
        embed.set_footer(text=f"Hosted by {host_name}")
    return embed
