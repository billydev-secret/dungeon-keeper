"""Personal-role projector + colour helpers (Stage 3, Agent C).

The frozen async signatures the billing loop (Agent B) and cog (Agent C) code
against, now with the real Discord projection behind them:

``apply_role_perks``   — PROJECTOR: reconcile the member's Discord personal role
                         to their entitlements + desired ``econ_personal_roles``
                         state. Full reconcile, not additive: an attribute the
                         member is no longer entitled to is *cleared*, not left
                         stale (a lapsed gradient drops ``secondary_colour``, a
                         lapsed icon drops ``display_icon``). Idempotent — only
                         edits when the projection actually differs, so a steady
                         state costs zero role edits (the 2-edits/10-min budget).
``revoke_role_perks``  — re-project after a lapse/cancel; when NO entitlement
                         remains, delete the Discord role and the
                         ``econ_personal_roles`` row. Otherwise re-projects so a
                         downgrade (gradient→solid) still takes effect.
``feature_gate_ok``    — whether the guild currently supports a feature-gated
                         perk. discord.py 2.7.1 exposes both gate strings as real
                         ``GuildFeature`` literals (``ROLE_ICONS`` for icons,
                         ``ENHANCED_ROLE_COLORS`` for gradient/holographic
                         roles), so this is a plain ``in guild.features`` check —
                         no attempt-and-catch needed.

Also home to the pure colour maths the cog's ΔE staff-collision guard uses
(``delta_e_cie76`` / ``find_color_clash``) and ``parse_hex_colour`` — kept here
so the projector and its guard live together and stay unit-testable without
Discord.

Discord effects are best-effort and self-healing: a failed edit/create is logged
and the projector returns False; the next apply/revoke re-projects from the same
desired state, so a transient Discord outage never corrupts billing (which is
committed before any of this runs).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

import discord

from bot_modules.economy.rentals import effective_color_mode
from bot_modules.services.economy_rentals_service import (
    delete_personal_role,
    entitlements,
    get_personal_role,
    upsert_personal_role,
)
from bot_modules.services.economy_service import load_econ_settings

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger("dungeonkeeper.economy.perk_actions")

# The named hierarchy anchor booster_roles.sync_swatches also positions against;
# personal roles sit just above it so a rented colour wins the display contest.
_COSMETICS_ANCHOR = "#### Cosmetics"

# Perk → the guild feature it needs (perks not listed are always available).
_FEATURE_FOR_PERK = {
    "role_icon": "ROLE_ICONS",
    "role_gradient": "ENHANCED_ROLE_COLORS",
}

# ΔE (CIE76) below this ⇒ "too close" to a staff colour. Empirically ~25 is the
# threshold where two role colours read as the same hue in the member list.
STAFF_CLASH_THRESHOLD = 25.0


# ── feature gate ───────────────────────────────────────────────────────


async def feature_gate_ok(bot: discord.Client, guild_id: int, perk: str) -> bool:
    """Whether the guild currently supports a feature-gated perk.

    ``role_icon`` needs ``ROLE_ICONS``; ``role_gradient`` needs
    ``ENHANCED_ROLE_COLORS`` (both real ``GuildFeature`` literals in discord.py
    2.7.1). Un-gated perks return True. A missing guild returns False (can't
    confirm support).
    """
    feature = _FEATURE_FOR_PERK.get(perk)
    if feature is None:
        return True
    guild = bot.get_guild(guild_id)
    if guild is None:
        return False
    return feature in getattr(guild, "features", ())


# ── projector ──────────────────────────────────────────────────────────


def _resolve_icon_payload(icon_path: str) -> bytes | str | None:
    """Turn a stored icon spec into a ``display_icon`` payload.

    A stored filesystem path (uploaded image) reads back as bytes; anything else
    is treated as a unicode emoji string. Empty ⇒ None (no icon).
    """
    if not icon_path:
        return None
    if os.path.isfile(icon_path):
        try:
            with open(icon_path, "rb") as fh:
                return fh.read()
        except OSError:
            log.warning("perk_actions: could not read icon file %s", icon_path)
            return None
    return icon_path


async def apply_role_perks(
    bot: discord.Client, db_path: Path, guild_id: int, user_id: int
) -> bool:
    """Reconcile the member's personal role to their entitlements + desired state.

    Returns True when the projection is in place (including the no-op steady
    state), False when the guild/member is unreachable or a Discord call failed.
    """
    guild = bot.get_guild(guild_id)
    if guild is None:
        return False
    member = guild.get_member(user_id)
    if member is None:
        # Can't hold a role for someone not in the guild; leave state for the
        # member-remove path / a later apply once they're resolvable.
        return False

    def _read() -> tuple[set[str], dict | None, object, bytes | str | None]:
        from bot_modules.core.db_utils import open_db

        with open_db(db_path) as conn:
            ent = entitlements(conn, guild_id, user_id)
            row = get_personal_role(conn, guild_id, user_id)
            settings = load_econ_settings(conn, guild_id)
        desired = dict(row) if row is not None else None
        icon_payload = (
            _resolve_icon_payload(str(desired["icon_path"])) if desired else None
        )
        return ent, desired, settings, icon_payload

    raw_ent, desired, settings, icon_payload = await asyncio.to_thread(_read)

    if not raw_ent:
        # Nothing entitles a role — apply is a no-op (revoke owns deletion).
        return True

    # Feature-gate the *visuals* (not role existence): a suspended/unsupported
    # gradient or icon just isn't rendered, but the role stays while any rental
    # is live, so it snaps back the moment the feature returns.
    features = set(getattr(guild, "features", ()) or ())
    applied = set(raw_ent)
    if "ENHANCED_ROLE_COLORS" not in features:
        applied.discard("role_gradient")
    if "ROLE_ICONS" not in features:
        applied.discard("role_icon")

    mode = effective_color_mode(applied)

    d_name = str(desired["name"]) if desired else ""
    d_color = int(desired["color"]) if desired else -1
    d_color2 = int(desired["color2"]) if desired else -1
    d_role_id = (
        int(desired["role_id"]) if desired and desired["role_id"] else None
    )

    # Target projection.
    if "role_name" in raw_ent and d_name:
        target_name = d_name[:100]
    else:
        target_name = member.display_name
    if mode in ("solid", "gradient") and d_color != -1:
        target_color = discord.Colour(d_color)
    else:
        target_color = discord.Colour.default()
    target_secondary = (
        discord.Colour(d_color2) if mode == "gradient" and d_color2 != -1 else None
    )
    want_icon = "role_icon" in applied and icon_payload is not None

    role = guild.get_role(d_role_id) if d_role_id else None

    if role is None:
        role = await _create_role(
            guild, target_name, target_color, target_secondary,
            icon_payload if want_icon else None,
        )
        if role is None:
            return False
        await _alert_if_role_ceiling(bot, guild, settings)
        await _position_personal_role(guild, role)

        def _persist() -> None:
            from bot_modules.core.db_utils import open_db

            with open_db(db_path) as conn:
                upsert_personal_role(conn, guild_id, user_id, {"role_id": role.id})

        await asyncio.to_thread(_persist)
    else:
        if not await _reconcile_role(
            role, target_name, target_color, target_secondary, want_icon, icon_payload
        ):
            return False

    # Make sure the member actually wears it.
    if role not in getattr(member, "roles", ()):
        try:
            await member.add_roles(role, reason="Economy personal role")
        except discord.HTTPException:
            log.warning("perk_actions: could not add role to member %s", user_id)
    return True


async def _create_role(
    guild: discord.Guild,
    name: str,
    color: discord.Colour,
    secondary: discord.Colour | None,
    icon_payload: bytes | str | None,
) -> discord.Role | None:
    kwargs: dict = {"name": name, "colour": color, "reason": "Economy personal role"}
    if secondary is not None:
        kwargs["secondary_color"] = secondary
    if icon_payload is not None:
        kwargs["display_icon"] = icon_payload
    try:
        return await guild.create_role(**kwargs)
    except discord.HTTPException:
        log.exception("perk_actions: failed to create personal role in %s", guild.id)
        return None


async def _reconcile_role(
    role: discord.Role,
    target_name: str,
    target_color: discord.Colour,
    target_secondary: discord.Colour | None,
    want_icon: bool,
    icon_payload: bytes | str | None,
) -> bool:
    """Edit only the attributes that differ — steady state costs zero edits."""
    edits: dict = {}
    if role.name != target_name:
        edits["name"] = target_name
    if role.colour != target_color:
        edits["colour"] = target_color
    cur_secondary = getattr(role, "secondary_colour", None)
    if cur_secondary != target_secondary:
        edits["secondary_color"] = target_secondary
    # Icon: presence-diff only (can't cheaply read an Asset's bytes back), so we
    # set when the role has none, clear when it shouldn't, and assume unchanged
    # when both present — never re-upload on a steady state.
    role_has_icon = getattr(role, "display_icon", None) is not None
    if want_icon and not role_has_icon:
        edits["display_icon"] = icon_payload
    elif not want_icon and role_has_icon:
        edits["display_icon"] = None
    if not edits:
        return True
    try:
        await role.edit(reason="Economy personal role sync", **edits)
        return True
    except discord.HTTPException:
        log.exception("perk_actions: failed to edit personal role %s", role.id)
        return False


async def _position_personal_role(guild: discord.Guild, role: discord.Role) -> None:
    """Position the role just above the cosmetics band, under the bot's top role.

    Above ``#### Cosmetics`` (so a rented colour outranks a booster swatch) but
    never above ``guild.me``'s top role, which Discord would reject.
    """
    me = guild.me
    ceiling = (
        me.top_role.position - 1 if me is not None and me.top_role is not None else 1
    )
    anchor = discord.utils.get(guild.roles, name=_COSMETICS_ANCHOR)
    target = anchor.position + 1 if anchor is not None else ceiling
    target = max(1, min(target, ceiling))
    if role.position == target:
        return
    try:
        await guild.edit_role_positions(positions={role: target})
    except discord.HTTPException:
        log.warning("perk_actions: could not position personal role %s", role.id)


async def _alert_if_role_ceiling(
    bot: discord.Client, guild: discord.Guild, settings: object
) -> None:
    """Warn the bank channel when the guild is near Discord's 250-role cap."""
    if len(guild.roles) < 200:
        return
    channel_id = int(getattr(settings, "bank_channel_id", 0) or 0)
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        return
    try:
        await channel.send(
            f"⚠️ This server now has {len(guild.roles)} roles — Discord caps at 250. "
            "Personal-role perks will stop working once the cap is hit."
        )
    except discord.HTTPException:
        log.warning("perk_actions: could not post role-ceiling alert in %s", guild.id)


async def revoke_role_perks(
    bot: discord.Client, db_path: Path, guild_id: int, user_id: int
) -> None:
    """Re-project after a lapse/cancel; delete the role when nothing remains."""
    guild = bot.get_guild(guild_id)
    if guild is None:
        return

    def _read() -> tuple[set[str], dict | None]:
        from bot_modules.core.db_utils import open_db

        with open_db(db_path) as conn:
            ent = entitlements(conn, guild_id, user_id)
            row = get_personal_role(conn, guild_id, user_id)
        return ent, (dict(row) if row is not None else None)

    raw_ent, desired = await asyncio.to_thread(_read)

    if raw_ent:
        # Still entitled to something (e.g. gradient lapsed but colour remains) —
        # re-project so the downgrade actually lands.
        await apply_role_perks(bot, db_path, guild_id, user_id)
        return

    role_id = int(desired["role_id"]) if desired and desired["role_id"] else None
    if role_id:
        role = guild.get_role(role_id)
        if role is not None:
            try:
                await role.delete(reason="Economy perks lapsed")
            except discord.HTTPException:
                log.warning("perk_actions: could not delete role %s", role_id)

    def _drop() -> None:
        from bot_modules.core.db_utils import open_db

        with open_db(db_path) as conn:
            delete_personal_role(conn, guild_id, user_id)

    await asyncio.to_thread(_drop)


# ── colour maths — ΔE staff-collision guard ────────────────────────────


def parse_hex_colour(text: str) -> int | None:
    """Parse ``#RRGGBB`` / ``RRGGBB`` → 0xRRGGBB int, or None if malformed."""
    s = text.strip().lstrip("#")
    if len(s) != 6:
        return None
    try:
        return int(s, 16)
    except ValueError:
        return None


def _int_to_rgb(value: int) -> tuple[int, int, int]:
    return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)


def _srgb_channel_to_linear(c: float) -> float:
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _rgb_to_lab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    """sRGB (0-255) → CIE L*a*b* under the D65 illuminant."""
    r, g, b = (_srgb_channel_to_linear(c) for c in rgb)
    # Linear RGB → XYZ (D65).
    x = r * 0.4124 + g * 0.3576 + b * 0.1805
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = r * 0.0193 + g * 0.1192 + b * 0.9505
    # Normalise by the D65 white point.
    x, y, z = x / 0.95047, y / 1.0, z / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else (7.787 * t) + (16 / 116)

    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e_cie76(rgb1: tuple[int, int, int], rgb2: tuple[int, int, int]) -> float:
    """CIE76 colour difference (Euclidean distance in L*a*b*)."""
    l1, a1, b1 = _rgb_to_lab(rgb1)
    l2, a2, b2 = _rgb_to_lab(rgb2)
    return ((l1 - l2) ** 2 + (a1 - a2) ** 2 + (b1 - b2) ** 2) ** 0.5


def _is_staff_role(role: discord.Role) -> bool:
    """A role carrying a moderation-grade permission (admin/manage/moderate)."""
    perms = role.permissions
    return bool(
        perms.administrator or perms.manage_guild or perms.moderate_members
    )


def find_color_clash(
    guild: discord.Guild,
    color_value: int,
    *,
    threshold: float = STAFF_CLASH_THRESHOLD,
) -> discord.Role | None:
    """The first staff role whose colour is within ΔE ``threshold``, or None.

    Staff = roles with a moderation permission AND a non-default colour; a member
    picking a near-identical hue could impersonate them in the member list, so
    the colour is rejected and this names the clashing role.
    """
    target = _int_to_rgb(color_value)
    for role in getattr(guild, "roles", ()):
        value = role.colour.value
        if value == 0:  # default/no colour — nothing to clash with
            continue
        if not _is_staff_role(role):
            continue
        if delta_e_cie76(_int_to_rgb(value), target) < threshold:
            return role
    return None
