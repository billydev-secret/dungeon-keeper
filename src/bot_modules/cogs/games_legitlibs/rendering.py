import re
import discord
from bot_modules.games.constants import GAME_ICONS, PHASE_JOINING, PHASE_PLAYING, PHASE_RESULTS, PHASE_RECAP
from .data import HEAT_LABELS, HEAT_ICONS

_MARKER_RE = re.compile(r"\{(\w+)\}")
_GAME_NAME = "LegitLibs"
_ICON = GAME_ICONS["legitlibs"]

# Embeds follow the guild accent (threaded in as ``color`` by the cog). Each
# builder keeps its original phase color only as a fall-back for when accent
# resolution fails, so a branding hiccup never crashes a game.
_ColorT = discord.Color | int


def build_join_embed(
    host_name: str,
    template_title: str,
    tier: int,
    mode: str,
    player_count: int,
    player_min: int,
    color: _ColorT | None = None,
) -> discord.Embed:
    mode_label = {"quiplash": "Quiplash", "classic": "Classic", "hotseat": "Hot Seat"}.get(mode, mode.title())
    embed = discord.Embed(
        title=f"{_ICON} LegitLibs — {mode_label}",
        description=f'**"{template_title}"**',
        color=color if color is not None else PHASE_JOINING,
    )
    embed.add_field(name="Heat", value=HEAT_LABELS[tier], inline=True)
    embed.add_field(name="Mode", value=mode_label, inline=True)
    embed.add_field(name="​", value="​", inline=True)
    embed.add_field(
        name="Players",
        value=f"{player_count} joined" + (f" (need {player_min})" if player_count < player_min else " ✓"),
        inline=False,
    )
    embed.set_footer(text=f"{_ICON} {_GAME_NAME} • Host: {host_name}")
    return embed


def build_fill_embed(
    host_name: str,
    template_title: str,
    tier: int,
    player_count: int,
    submitted_count: int,
    deadline_ts: int | None = None,
    redacted_body: str | None = None,
    color: _ColorT | None = None,
) -> discord.Embed:
    description = f'**"{template_title}"**'
    if redacted_body:
        description += f"\n\n{redacted_body}"
    description += "\n\n*Click **Submit Fills** to fill in the blanks.*"
    embed = discord.Embed(
        title=f"{_ICON} LegitLibs — Fill Phase",
        description=description,
        color=color if color is not None else PHASE_PLAYING,
    )
    embed.add_field(name="Heat", value=HEAT_LABELS[tier], inline=True)
    embed.add_field(name="Submitted", value=f"{submitted_count} / {player_count}", inline=True)
    if deadline_ts:
        embed.add_field(name="Time left", value=f"<t:{deadline_ts}:R>", inline=True)
    embed.set_footer(text=f"{_ICON} {_GAME_NAME} • Host: {host_name}")
    return embed


def build_reveal_embed(
    template_title: str,
    tier: int,
    filled_body: str,
    submission_num: int,
    total_submissions: int,
    color: _ColorT | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"{_ICON} {submission_num} / {total_submissions}",
        description=filled_body,
        color=color if color is not None else PHASE_RESULTS,
    )
    embed.set_footer(text=f"{_ICON} {_GAME_NAME} • {HEAT_ICONS[tier]} {template_title}")
    return embed


def build_no_submissions_embed(
    template_title: str, tier: int, color: _ColorT | None = None
) -> discord.Embed:
    embed = discord.Embed(
        title=f"{_ICON} No Submissions",
        description="Nobody filled in the blanks in time.",
        color=color if color is not None else PHASE_RECAP,
    )
    embed.set_footer(text=f"{_ICON} {_GAME_NAME} • {template_title}")
    return embed


def render_filled_body(body: str, blanks: list[dict], fills: dict[str, str]) -> str:
    """Replace {blank_id} markers with bolded fill text."""
    def replacer(m):
        bid = m.group(1)
        val = fills.get(bid, "___")
        return f"**{discord.utils.escape_markdown(val)}**"
    return _MARKER_RE.sub(replacer, body)


def render_redacted_body(body: str, blanks: list[dict]) -> str:
    """Replace {blank_id} markers with numbered placeholders like **[1]**, matching modal labels."""
    id_to_pos = {b["id"]: b.get("position", i + 1) for i, b in enumerate(blanks)}
    def replacer(m):
        bid = m.group(1)
        pos = id_to_pos.get(bid)
        if pos is None:
            return m.group(0)
        return f"**`[{pos}]`**"
    return _MARKER_RE.sub(replacer, body)


def render_filled_body_attributed(
    body: str,
    blanks: list[dict],
    fills: dict,
    guild,
) -> str:
    """Render template body with inline spoiler-tag attribution.

    Filled blank  -> **value** (-||Display Name||)
    Unfilled blank -> ___
    """
    # Local import avoids a potential circular with utils.game_manager.
    from bot_modules.games.utils.game_manager import resolve_name

    def replacer(m):
        bid = m.group(1)
        fill = fills.get(bid)
        if not fill:
            return "___"
        name = resolve_name(guild, int(fill["by"]))
        safe_val = discord.utils.escape_markdown(str(fill["value"]))
        safe_name = discord.utils.escape_markdown(str(name))
        return f"**{safe_val}** (-||{safe_name}||)"
    return _MARKER_RE.sub(replacer, body)


def build_classic_fill_embed(
    host_name: str,
    template_title: str,
    tier: int,
    player_count: int,
    done_count: int,
    deadline_ts: int,
    color: _ColorT | None = None,
) -> discord.Embed:
    description = (
        f'**"{template_title}"**\n\n'
        "*Each player has been assigned some blanks. "
        "Click **Submit Fills** to fill in yours.*"
    )
    embed = discord.Embed(
        title=f"{_ICON} LegitLibs — Classic — Fill Phase",
        description=description,
        color=color if color is not None else PHASE_PLAYING,
    )
    embed.add_field(name="Heat", value=HEAT_LABELS[tier], inline=True)
    embed.add_field(name="Players done", value=f"{done_count} / {player_count}", inline=True)
    embed.add_field(name="Time left", value=f"<t:{deadline_ts}:R>", inline=True)
    embed.set_footer(text=f"{_ICON} {_GAME_NAME} • Host: {host_name}")
    return embed


def build_classic_rescue_embed(
    template_title: str,
    tier: int,
    unfilled_count: int,
    volunteer_names: list[str],
    deadline_ts: int,
    color: _ColorT | None = None,
) -> discord.Embed:
    s = "s" if unfilled_count != 1 else ""
    description = (
        f'**"{template_title}"**\n\n'
        f"*{unfilled_count} blank{s} still unfilled. "
        "Click **Volunteer** to help rescue them — "
        "unfilled blanks will be split across all volunteers.*"
    )
    embed = discord.Embed(
        title=f"{_ICON} LegitLibs — Classic — Rescue Round",
        description=description,
        color=color if color is not None else PHASE_PLAYING,
    )
    embed.add_field(name="Heat", value=HEAT_LABELS[tier], inline=True)
    embed.add_field(name="Unfilled", value=str(unfilled_count), inline=True)
    embed.add_field(name="Time left", value=f"<t:{deadline_ts}:R>", inline=True)
    vols_text = ", ".join(volunteer_names) if volunteer_names else "—"
    if len(vols_text) > 1020:
        vols_text = vols_text[:1020] + "…"
    embed.add_field(
        name=f"Volunteers ({len(volunteer_names)})",
        value=vols_text,
        inline=False,
    )
    return embed


def build_classic_rescue_fill_embed(
    template_title: str,
    tier: int,
    rescuers_done: int,
    rescuers_total: int,
    deadline_ts: int,
    color: _ColorT | None = None,
) -> discord.Embed:
    description = (
        f'**"{template_title}"**\n\n'
        "*Volunteers: click **Submit Fills** to fill in the rescued blanks.*"
    )
    embed = discord.Embed(
        title=f"{_ICON} LegitLibs — Classic — Rescue Fill",
        description=description,
        color=color if color is not None else PHASE_PLAYING,
    )
    embed.add_field(name="Heat", value=HEAT_LABELS[tier], inline=True)
    embed.add_field(
        name="Rescuers done",
        value=f"{rescuers_done} / {rescuers_total}",
        inline=True,
    )
    embed.add_field(name="Time left", value=f"<t:{deadline_ts}:R>", inline=True)
    return embed


def build_classic_reveal_embed(
    template_title: str,
    tier: int,
    filled_body: str,
    contributor_names: list[str],
    color: _ColorT | None = None,
) -> discord.Embed:
    if len(filled_body) > 4090:
        filled_body = filled_body[:4090] + "…"
    embed = discord.Embed(
        title=f"{_ICON} {template_title}",
        description=filled_body,
        color=color if color is not None else PHASE_RECAP,
    )
    contribs = ", ".join(contributor_names) if contributor_names else "—"
    embed.set_footer(
        text=f"{_ICON} {_GAME_NAME} • Classic • {HEAT_ICONS[tier]} • Contributors: {contribs}"
    )
    return embed
