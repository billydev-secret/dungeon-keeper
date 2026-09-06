"""Embed builders for the Spin-the-Compliment cog.

These functions accept plain dicts/primitives and return
``discord.Embed`` objects. They never call out to Discord — testable
with no network and no mocks of the Bot/Guild API.

Two embed shapes are exposed:

* :func:`build_lobby_embed` — the join-the-pool embed shown alongside
  the host/player buttons. Same shape on first render and after every
  Add-Me toggle (only the participant list changes).
* :func:`build_pairings_embed` — the post-close embed listing each
  ``giver → receiver`` mapping with the closing "deliver your
  compliment" call-to-action; reposted at the wrap's halfway nudge with a
  ✅ on every giver seen delivering. Members are *named* through a ``name_fn``
  (``services/name_resolver``), never ``<@id>``-mentioned: an embed
  mention is resolved by the reading client from its own cache and shows
  as a bare number to anyone who hasn't seen that member, and once the
  15-second ping is gone this card is the only record of who compliments
  whom. The ping itself lives in message ``content``, where a mention
  belongs.

* :func:`build_wrap_recap_embed` — the closing card: how many of the
  compliments were seen delivered, with the payout footer the cog appends.

The line-formatter (:func:`format_pairing_line`) is split out so the
pairings text can be assembled deterministically in tests without
constructing a Discord embed.
"""

from __future__ import annotations

import discord

from bot_modules.games.constants import GAME_ICONS, BRAND_COLOR
from bot_modules.core.branding import apply_section_spacing
from bot_modules.games_compliment.logic import delivered_line
from bot_modules.services.name_resolver import NameFn, mention


def build_lobby_embed(
    host_name: str,
    participants: list[str],
    color: "discord.Color | None" = None,
    start_at: int | None = None,
) -> discord.Embed:
    """Build the lobby embed shown while players are joining.

    ``participants`` is a list of pre-resolved display names (the cog
    runs the resolution against the guild before calling). Empty list
    renders as ``"—"`` so the field always has a value.

    ``start_at`` is an optional UTC epoch shown as a live Discord relative
    timestamp — the host's advertised start time. The host still presses the
    button; the countdown is advertising, not automation.
    """
    if color is None:
        color = discord.Color(BRAND_COLOR)
    embed = discord.Embed(
        title=f"{GAME_ICONS['compliment']} Spin the Compliment",
        color=color,
    )
    embed.add_field(name="Host", value=host_name, inline=True)
    if start_at:
        embed.add_field(name="⏰ Starting", value=f"<t:{start_at}:R>", inline=True)
    pool_str = ", ".join(participants) if participants else "—"
    embed.add_field(
        name=f"Pool ({len(participants)})", value=pool_str, inline=False
    )
    embed.set_footer(text=f"{GAME_ICONS['compliment']} Spin the Compliment")
    apply_section_spacing(embed)
    return embed


def format_pairing_line(giver_name: str, receiver_name: str, *, delivered: bool = False) -> str:
    """Format a single ``giver → receiver`` line for the pairings embed.

    Centralised so the arrow symbol/spacing can be tweaked in one place
    and so tests can assert on the line shape without rebuilding an
    embed. ``delivered`` prefixes a ✅ on the wrap's repost.
    """
    line = f"{giver_name} → {receiver_name}"
    return f"✅ {line}" if delivered else line


def build_pairings_embed(
    pairings: dict[int, int],
    color: "discord.Color | None" = None,
    *,
    name_fn: NameFn = mention,
    delivered: "set[int] | None" = None,
    ends_at: int | None = None,
) -> discord.Embed:
    """Build the post-close embed announcing the pairings.

    ``pairings`` is the ``{giver_id: receiver_id}`` map from
    ``generate_pairings``; every id is rendered through ``name_fn`` (the
    resolver from ``build_name_fn``). The default keeps an un-wired caller
    rendering a mention rather than crashing — the cog always passes one.

    ``delivered`` marks the givers already seen complimenting (the wrap's
    halfway repost); ``ends_at`` is the wrap's closing epoch, shown as a
    live countdown so the room knows when the recap lands. The trailing
    call-to-action is appended unconditionally so even a 2-player game
    has the prompt — and it says *how* to deliver (reply or @mention),
    because that is what the wrap counts.
    """
    if color is None:
        color = discord.Color(BRAND_COLOR)
    done = delivered or set()
    embed = discord.Embed(
        title=f"{GAME_ICONS['compliment']} Compliment Pairings",
        color=color,
    )
    body = "\n".join(
        format_pairing_line(name_fn(giver), name_fn(receiver), delivered=giver in done)
        for giver, receiver in pairings.items()
    )
    cta = "💛 Reply to (or @mention) your partner with your compliment!"
    if ends_at:
        cta += f"\n🏁 Wrap-up <t:{int(ends_at)}:R>."
    embed.description = f"{body}\n\n{cta}"
    embed.set_footer(text=f"{GAME_ICONS['compliment']} Spin the Compliment")
    return embed


def build_wrap_recap_embed(
    delivered_count: int,
    total: int,
    color: "discord.Color | None" = None,
) -> discord.Embed:
    """The closing card after the wrap: how the round went, in one line.

    The cog appends the economy's payout footer; the payout itself fires
    at this point, not when the pairings post (social-prompt-39).
    """
    if color is None:
        color = discord.Color(BRAND_COLOR)
    embed = discord.Embed(
        title=f"{GAME_ICONS['compliment']} Spin the Compliment — Wrap-Up",
        description=delivered_line(delivered_count, total),
        color=color,
    )
    embed.set_footer(text=f"{GAME_ICONS['compliment']} Thanks for playing Spin the Compliment!")
    return embed
