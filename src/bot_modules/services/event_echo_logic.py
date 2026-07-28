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

_DEFAULT_ACCENT = discord.Color(0x5865F2)

# Gamebot's sub-game keys → the name a member would recognise. Only the games
# worth echoing appear; `game_from_start` also recognises Connect 4 and
# Anagrams, which are two-player/quickfire and don't warrant main chat.
GAMEBOT_ECHO_NAMES = {
    "cah": "Cards Against Humanity",
}


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
    per_type_seconds: float = PER_TYPE_COOLDOWN_SECONDS,
    global_seconds: float = GLOBAL_COOLDOWN_SECONDS,
) -> EchoDecision:
    """Apply both cooldowns to a candidate echo.

    ``last_same_type`` / ``last_any`` are epoch seconds of the most recent
    *posted* echo (suppressed rows are excluded by the caller — see the
    migration's note on why a refusal must not push the window out).
    ``None`` means "never", which passes.
    """
    if last_any is not None and now - last_any < global_seconds:
        return EchoDecision(False, "global")
    if last_same_type is not None and now - last_same_type < per_type_seconds:
        return EchoDecision(False, "per_type")
    return EchoDecision(True)


def is_fresh(opened_at: float | None, now: float, window: float = FRESHNESS_SECONDS) -> bool:
    """True when a lobby opened recently enough to be worth announcing.

    An unknown open time (``None``) counts as fresh: the row exists in
    ``games_active_games``, so the game is live, and refusing to announce a
    live game over a missing timestamp is the wrong way to be wrong.
    """
    if opened_at is None:
        return True
    return now - opened_at <= window


def jump_url(guild_id: int, channel_id: int, message_id: int) -> str:
    """A permalink to one message.

    Hand-built rather than ``msg.jump_url`` because the party-game source
    works from database rows, not message objects — it never fetches the
    message, so there is nothing to read the property off. Same format.
    """
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def build_echo_embed(
    *,
    game_name: str,
    channel_id: int,
    url: str,
    host_name: str | None = None,
    color: discord.Color | None = None,
) -> discord.Embed:
    """The echo itself: what's starting, where, and a link to go there.

    Deliberately small — this lands in a conversation channel, and a tall
    embed in the middle of a chat is the thing people mute. No thumbnail, no
    fields, one line of body.
    """
    embed = discord.Embed(
        title=f"🎲 {game_name} is starting",
        description=(
            f"A game is open in <#{channel_id}>.\n"
            f"**[Jump in →]({url})**"
        ),
        color=color or _DEFAULT_ACCENT,
    )
    if host_name:
        embed.set_footer(text=f"Hosted by {host_name}")
    return embed
