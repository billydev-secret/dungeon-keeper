"""Curated colour palette — the swatch showroom, its picker button, and the sync.

The Discord surface for ``econ_color_catalog`` (DB layer:
``economy_color_catalog_service``). Three things live here:

* **The showroom gallery** (``showroom_page``). A select menu can name a colour
  but not show it, so the shop's palette picker sends the swatches themselves:
  one embed per colour carrying its art as an attachment, ten to a page. Until
  2026-08-26 this was a channel of permanent image posts an admin had to
  maintain; it is now built on demand inside ``/bank shop`` and nothing but the
  take-down is left of the channel version.
* **The picker button** (``PaletteColorButton``). Pressing a swatch on one of
  those old channel panels wears that colour, if the member is entitled to the
  ``role_preset`` perk. It does not charge: buying happens in ``/bank shop``,
  where the price and the member's balance are on screen. Kept registered so a
  showroom nobody has taken down yet still works.
* **The swatch sync.** Filenames of the form ``ColorName_HEX1_HEX2.ext`` are the
  authoring flow — drop art in, get a palette — and stay the source of truth for
  each colour's name, gradient pair and display order.

Until migration 159 this was the booster cosmetic-role picker: boosters claimed
one of these as a real Discord role, free and permanently. Two consequences
still shape the code:

* **The sync no longer creates or deletes Discord roles.** 15 members wear a
  legacy role and keep it permanently (they are grandfathered — see the
  migration). The old sync deleted a colour's role when its swatch file
  disappeared, which would now strip those members, so a vanished swatch
  *disables* an in-use colour instead of deleting anything.
* **Button custom-ids keep the ``booster_role:`` prefix** so panels posted
  before the migration keep working without a repost.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import discord

from bot_modules.core.db_utils import get_config_value, open_db
from bot_modules.services.economy_color_catalog_service import (
    color_ints,
    list_catalog,
    upsert_catalog_color,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

#: Swatch upload types the sync and the dashboard both accept.
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

log = logging.getLogger("dungeonkeeper.color_palette")


# ---------------------------------------------------------------------------
# Panel message bookkeeping
# ---------------------------------------------------------------------------


def get_panel_refs(conn: sqlite3.Connection, guild_id: int) -> list[tuple[int, int]]:
    rows = conn.execute(
        "SELECT channel_id, message_id FROM econ_color_panel_messages WHERE guild_id = ?",
        (guild_id,),
    ).fetchall()
    return [(int(r["channel_id"]), int(r["message_id"])) for r in rows]


def replace_panel_refs(
    conn: sqlite3.Connection, guild_id: int, refs: list[tuple[int, int]]
) -> None:
    conn.execute(
        "DELETE FROM econ_color_panel_messages WHERE guild_id = ?", (guild_id,)
    )
    conn.executemany(
        "INSERT INTO econ_color_panel_messages (guild_id, channel_id, message_id) "
        "VALUES (?, ?, ?)",
        [(guild_id, ch, msg) for ch, msg in refs],
    )


# ---------------------------------------------------------------------------
# Persistent picker button via DynamicItem
# ---------------------------------------------------------------------------


class PaletteColorButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"booster_role:(?P<key>.+)",
):
    """Handles every palette swatch press, surviving bot restarts.

    The ``booster_role:`` template is deliberate legacy: panels posted before
    migration 159 carry those custom-ids, and re-templating would have silently
    broken every button already sitting in the channel.
    """

    def __init__(self, key: str) -> None:
        super().__init__(
            discord.ui.Button(
                label=key,
                style=discord.ButtonStyle.secondary,
                custom_id=f"booster_role:{key}",
            )
        )
        self.key = key

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: discord.utils.MISSING,  # type: ignore[assignment]
        /,
    ) -> PaletteColorButton:
        key = (item.custom_id or "").removeprefix("booster_role:")
        return cls(key)

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "This only works in a server.", ephemeral=True
            )
            return
        bot = cast("Bot", interaction.client)
        await wear_palette_color(bot, interaction, guild, member, key=self.key)


async def wear_palette_color(
    bot: Bot,
    interaction: discord.Interaction,
    guild: discord.Guild,
    member: discord.Member,
    *,
    key: str,
) -> None:
    """Put a palette colour on an entitled member's personal role.

    Shared by the showroom button and the shop's picker, so both routes charge
    (or don't) identically. The member must already be entitled to
    ``role_preset`` — by rental, by gift, or by the staff comp; renting happens
    in the shop. An entitled member with a real rental also has it re-tagged to
    the chosen colour, so the next renewal bills that colour's price.
    """
    # Deferred imports: this module is imported at startup to register the
    # dynamic item, and the economy package pulls in the whole billing stack.
    from bot_modules.economy.perk_actions import apply_role_perks, feature_gate_ok
    from bot_modules.services.economy_color_catalog_service import (
        get_catalog_color_by_key,
    )
    from bot_modules.services.economy_rentals_service import (
        effective_entitlements,
        get_live_preset_rental,
        set_rental_catalog_color,
        upsert_personal_role,
    )
    from bot_modules.services.economy_service import load_econ_settings

    ctx = bot.ctx
    guild_id = guild.id
    is_staff = ctx.member_is_mod(member)

    def _load():
        with ctx.open_db() as conn:
            row = get_catalog_color_by_key(conn, guild_id, key)
            perks = effective_entitlements(
                conn, guild_id, member.id, is_staff=is_staff
            )
            rental = get_live_preset_rental(conn, guild_id, member.id)
            settings = load_econ_settings(conn, guild_id)
            colour = (
                {
                    "id": int(row["id"]),
                    "name": str(row["name"]),
                    "pair": color_ints(row),
                    "enabled": bool(row["enabled"]),
                    # A colour priced 0 bills the flat perk price — resolve it
                    # here so the refusal below quotes what this colour would
                    # actually cost, not a flat price it may not charge.
                    "price": int(row["price"]) or int(settings.price_role_preset),
                }
                if row is not None
                else None
            )
            rental_id = int(rental["id"]) if rental is not None else None
        return colour, perks, rental_id, settings

    colour, perks, rental_id, settings = await asyncio.to_thread(_load)

    if not settings.enabled:
        # Every sibling entry point refuses when the economy is switched off
        # (the shop's `_refuse_disabled`); a showroom button that kept re-tagging
        # rentals and re-projecting roles would be the one way in.
        await interaction.response.send_message(
            "The economy is currently switched off here.", ephemeral=True
        )
        return
    if colour is None or colour["pair"] is None or not colour["enabled"]:
        await interaction.response.send_message(
            "That color isn't available anymore.", ephemeral=True
        )
        return
    if "role_preset" not in perks:
        await interaction.response.send_message(
            f"These colors are a shop perk now — **{colour['name']}** rents for "
            f"{settings.currency_emoji} **{colour['price']:,}/week**. Grab it from "
            "`/bank shop` and this button is yours.",
            ephemeral=True,
        )
        return
    if not await feature_gate_ok(bot, guild_id, "role_preset"):
        await interaction.response.send_message(
            "❌ This server doesn't support gradient roles right now.", ephemeral=True
        )
        return

    color, color2 = colour["pair"]

    def _wear() -> None:
        with ctx.open_db() as conn:
            if rental_id is not None:
                set_rental_catalog_color(conn, guild_id, rental_id, colour["id"])
            upsert_personal_role(
                conn, guild_id, member.id, {"color": color, "color2": color2}
            )

    await asyncio.to_thread(_wear)
    # Defer before the projector: its role edits are rate-limited and can run
    # past the 3s interaction budget, and the pick is already committed.
    await interaction.response.defer(ephemeral=True)
    ok = await apply_role_perks(bot, ctx.db_path, guild_id, member.id)
    tail = "" if ok else " (I couldn't update your role right now — try again shortly.)"
    await interaction.followup.send(
        f"You're wearing **{colour['name']}**.{tail}", ephemeral=True
    )


# ---------------------------------------------------------------------------
# Showroom gallery — the swatches, shown inside the shop
# ---------------------------------------------------------------------------

#: Discord caps a message at ten embeds *and* ten attachments, and the showroom
#: spends one of each per colour, so a page is exactly ten swatches.
PALETTE_PAGE_SIZE = 10

#: How many bytes of swatch art one page may carry. A single upload may be 8 MB
#: (the dashboard's cap) and ten of those would be rejected by Discord as one
#: message, so art stops being attached once the page is this full and the
#: remaining colours fall back to their rendered fade, which costs a kilobyte.
_PAGE_ATTACH_BUDGET = 7 * 1024 * 1024

#: Size of the gradient rendered when a colour's art can't be attached.
_FADE_SIZE = (512, 128)


def _safe_filename(name: str, ext: str) -> str:
    """Sanitise a name into a Discord-safe attachment filename.

    The name is half of an ``attachment://`` reference, so anything Discord
    would reject there breaks the embed that points at it.
    """
    clean = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    return f"{clean}{ext}"


@dataclass(frozen=True)
class ShowroomPage:
    """One page of the shop's swatch gallery, ready to send.

    ``embeds`` and ``attachments`` are parallel: each embed's image points at
    ``attachment://<filename>`` of the same index. The caller wraps the bytes in
    ``discord.File`` — a File consumes its buffer when sent, so the page holds
    raw bytes and every send (including a page flip) builds its own.
    """

    page: int
    page_count: int
    embeds: list[discord.Embed]
    attachments: list[tuple[str, bytes]]


def palette_page_count(total: int, page_size: int = PALETTE_PAGE_SIZE) -> int:
    """How many gallery pages ``total`` colours fill (always at least one)."""
    if total <= 0:
        return 1
    return -(-total // page_size)


def render_gradient_png(
    hex1: str, hex2: str, size: tuple[int, int] = _FADE_SIZE
) -> bytes:
    """Render a colour's two hex codes as a horizontal fade, as PNG bytes.

    The stand-in for a swatch whose art is missing. Production's palette rows
    still point at an assets folder that moved, so without this the gallery
    would show eleven colours and no colour — and the fade is the thing being
    sold, so drawing it from the hexes is a truthful picture rather than a
    placeholder.
    """
    from PIL import Image  # noqa: PLC0415

    width, height = size
    width = max(2, width)
    r1, g1, b1 = (int(hex1[i : i + 2], 16) for i in (0, 2, 4))
    r2, g2, b2 = (int(hex2[i : i + 2], 16) for i in (0, 2, 4))
    span = width - 1
    row = [
        (
            round(r1 + (r2 - r1) * x / span),
            round(g1 + (g2 - g1) * x / span),
            round(b1 + (b2 - b1) * x / span),
        )
        for x in range(width)
    ]
    strip = Image.new("RGB", (width, 1))
    strip.putdata(row)
    buf = io.BytesIO()
    strip.resize((width, max(1, height))).save(buf, format="PNG")
    return buf.getvalue()


def _read_swatch(image_path: str) -> bytes | None:
    """The colour's art from disk, or None when it isn't there to read."""
    if not image_path or not os.path.isfile(image_path):
        return None
    try:
        with open(image_path, "rb") as fp:
            return fp.read()
    except OSError:
        log.warning("Palette swatch unreadable: %s", image_path)
        return None


def _accent(hex1: str) -> discord.Color:
    """A swatch embed's stripe is the colour it is selling — semantic, not brand."""
    try:
        return discord.Color(int(hex1, 16))
    except (TypeError, ValueError):
        return discord.Color.default()


def showroom_page(
    colors: Sequence[Mapping],
    page: int,
    *,
    currency_emoji: str,
    page_size: int = PALETTE_PAGE_SIZE,
) -> ShowroomPage:
    """Build one page of the swatch gallery: an embed + an image per colour.

    Reads files and renders fallbacks, so callers run it off the event loop.
    ``page`` is clamped into range rather than rejected — a member paging a
    gallery that shrank under them gets the nearest page, not an error.
    """
    page_count = palette_page_count(len(colors), page_size)
    page = max(0, min(page, page_count - 1))
    rows = list(colors[page * page_size : (page + 1) * page_size])

    embeds: list[discord.Embed] = []
    attachments: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    spent = 0
    for index, row in enumerate(rows):
        art = _read_swatch(str(row["image_path"] or ""))
        if art is not None and spent + len(art) > _PAGE_ATTACH_BUDGET:
            art = None
        if art is None:
            data = render_gradient_png(str(row["hex1"]), str(row["hex2"]))
            ext = ".png"
        else:
            data = art
            ext = os.path.splitext(str(row["image_path"]))[1].lower() or ".png"
        filename = _safe_filename(str(row["key"]), ext)
        if filename in seen:
            # Two keys can sanitise to the same name ("a b" and "a-b"), and two
            # attachments sharing one filename would leave both embeds pointing
            # at whichever Discord kept.
            filename = _safe_filename(f"{row['key']}_{index}", ext)
        seen.add(filename)
        spent += len(data)

        embed = discord.Embed(
            title=str(row["name"]),
            description=f"{currency_emoji} **{int(row['price']):,}** / week",
            color=_accent(str(row["hex1"])),
        )
        embed.set_image(url=f"attachment://{filename}")
        embeds.append(embed)
        attachments.append((filename, data))

    return ShowroomPage(page, page_count, embeds, attachments)


async def take_down_palette_panel(db_path: Path, guild: discord.Guild) -> int:
    """Delete the showroom messages this guild still has sitting in a channel.

    The showroom moved inside `/bank shop`, so a channel of permanent swatch
    posts is no longer how anyone browses. This clears the ones already posted
    and forgets them; buttons on any panel that isn't taken down keep working,
    because ``PaletteColorButton`` is still registered. Returns how many
    messages went.
    """
    with open_db(db_path) as conn:
        refs = get_panel_refs(conn, guild.id)
    by_channel: dict[int, list[int]] = {}
    for ch_id, msg_id in refs:
        by_channel.setdefault(ch_id, []).append(msg_id)

    deleted = 0
    for ch_id, msg_ids in by_channel.items():
        channel = guild.get_channel(ch_id)
        if not isinstance(channel, discord.TextChannel):
            continue
        partials = [channel.get_partial_message(mid) for mid in msg_ids]
        # Bulk-delete in ≤100 chunks so a large showroom doesn't burn through
        # the per-channel DELETE bucket and 429.
        for i in range(0, len(partials), 100):
            chunk = partials[i : i + 100]
            try:
                await channel.delete_messages(chunk)
            except (discord.NotFound, discord.HTTPException, discord.ClientException):
                # Bulk delete rejects messages over 14 days old — and every
                # showroom worth taking down is older than that.
                for pm in chunk:
                    try:
                        await pm.delete()
                    except discord.NotFound:
                        deleted += 1  # already gone is the outcome we wanted
                    except discord.HTTPException:
                        pass
                    else:
                        deleted += 1
            else:
                deleted += len(chunk)

    # Forget the refs whatever happened: a message we can no longer reach (its
    # channel deleted, say) is not coming back, and keeping the row would make
    # every future take-down report work it isn't doing.
    with open_db(db_path) as conn:
        replace_panel_refs(conn, guild.id, [])
    if refs:
        log.info(
            "Took down %d of %d palette showroom messages in %s",
            deleted, len(refs), guild.id,
        )
    return deleted


# ---------------------------------------------------------------------------
# Swatch sync — scan directory, reconcile the catalog to the files
# ---------------------------------------------------------------------------


def _parse_swatch_filename(filename: str) -> tuple[str, str, str] | None:
    """Parse ``ColorName_HEX1_HEX2.ext`` → (label, hex1, hex2) or None."""
    stem = os.path.splitext(filename)[0]
    parts = stem.split("_")
    if len(parts) < 3:
        return None
    hex2 = parts[-1]
    hex1 = parts[-2]
    if not (
        re.fullmatch(r"[0-9A-Fa-f]{6}", hex1) and re.fullmatch(r"[0-9A-Fa-f]{6}", hex2)
    ):
        return None
    label = " ".join(parts[:-2]).strip()
    if not label:
        # `_FF0000_8B0000.png` parses as three parts with an empty name. An
        # unnamed colour is not merely ugly: Discord rejects a SelectOption with
        # an empty label, which would break the picker for *every* member and
        # blow up panel posting after it had already deleted the old showroom.
        return None
    return label, hex1, hex2


def _hex_sort_key(hex1: str, hex2: str) -> int:
    """Return an integer sort key: hue of hex1 primary, hue of hex2 secondary.

    Uses HSV hue (0-359) so colors order by visual gradient.
    """
    import colorsys

    def _hue(h: str) -> int:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        hue, _s, _v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        return int(hue * 3600)  # 0-3600 for extra precision

    return _hue(hex1) * 10000 + _hue(hex2)


def get_swatch_directory(db_path: Path) -> str:
    with open_db(db_path) as conn:
        return get_config_value(conn, "booster_swatch_dir", "")


def get_guild_swatch_dir(db_path: Path, guild_id: int) -> Path:
    """Return (and create) the managed per-guild swatch upload folder.

    Lives next to the database under ``swatches/<guild_id>/`` so each server
    keeps its own isolated set of uploaded swatch images.
    """
    directory = db_path.parent / "swatches" / str(guild_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def count_valid_swatches(directory: str) -> int:
    """Count files in ``directory`` whose names parse as valid swatches."""
    if not directory or not os.path.isdir(directory):
        return 0
    total = 0
    for entry in os.listdir(directory):
        if os.path.splitext(entry)[1].lower() not in IMAGE_EXTS:
            continue
        if _parse_swatch_filename(entry) is not None:
            total += 1
    return total


def swatch_file_info(directory: Path) -> list[dict]:
    """List image files in ``directory`` with parsed swatch metadata.

    Each entry is ``{name, valid, label, hex1, hex2}``. ``valid`` is False for
    image files whose names don't match ``ColorName_HEX1_HEX2.ext`` — those are
    skipped by Sync, so the UI surfaces them as needing a rename.
    """
    if not directory.is_dir():
        return []
    out: list[dict] = []
    for entry in sorted(os.listdir(directory)):
        if os.path.splitext(entry)[1].lower() not in IMAGE_EXTS:
            continue
        parsed = _parse_swatch_filename(entry)
        if parsed is not None:
            label, hex1, hex2 = parsed
            out.append(
                {"name": entry, "valid": True, "label": label, "hex1": hex1, "hex2": hex2}
            )
        else:
            out.append(
                {"name": entry, "valid": False, "label": None, "hex1": None, "hex2": None}
            )
    return out


def resolve_swatch_directory(
    db_path: Path,
    guild_id: int,
    *,
    managed: Path | None = None,
    managed_valid_count: int | None = None,
) -> str:
    """Pick the directory that Sync will scan for this guild.

    The managed per-guild upload folder wins as soon as it holds at least one
    validly named swatch; otherwise fall back to the configured
    ``booster_swatch_dir`` override (legacy host-path deployments). When neither
    has content, the (empty) managed folder is returned so the caller's
    zero-swatch guard can abort safely.

    A caller that has already resolved the managed path or listed its contents
    passes them in, so the dashboard's swatch panel does not walk the same
    directory (or re-run its mkdir) a second time.
    """
    if managed is None:
        managed = get_guild_swatch_dir(db_path, guild_id)
    if managed_valid_count is None:
        managed_valid_count = count_valid_swatches(str(managed))
    if managed_valid_count > 0:
        return str(managed)
    configured = get_swatch_directory(db_path)
    if configured and os.path.isdir(configured):
        return configured
    return str(managed)


def sync_palette(
    db_path: Path,
    guild_id: int,
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Reconcile a guild's palette to the swatch files on disk.

    Returns ``(added, disabled, removed, still_disabled)`` labels.
    ``still_disabled`` names colours whose swatch is present but which are not
    being offered. Sync deliberately never re-enables a colour — it cannot tell
    an admin's retirement from its own auto-disable — so re-uploading a swatch
    deleted by mistake would otherwise leave that colour quietly out of the shop
    for good. Naming them makes the one-click fix obvious instead.

    Unlike the booster-era sync
    this touches **no Discord roles at all**: colours project onto members'
    personal roles now, and the legacy roles are worn by grandfathered members
    who must keep them. A colour whose swatch file disappears is therefore
    *disabled* when a live rental still points at it (the renter keeps what they
    paid for, and it stops being offered to anyone new) and only deleted when
    nobody holds it.
    """
    from bot_modules.services.economy_color_catalog_service import (
        color_in_use,
        delete_catalog_color,
        update_catalog_color,
    )

    swatch_dir = resolve_swatch_directory(db_path, guild_id)
    if not swatch_dir or not os.path.isdir(swatch_dir):
        raise ValueError(f"Swatch directory not configured or missing: `{swatch_dir}`")

    # key → (label, hex1, hex2, file_path, sort_order)
    found: dict[str, tuple[str, str, str, str, int]] = {}
    for entry in os.listdir(swatch_dir):
        if os.path.splitext(entry)[1].lower() not in IMAGE_EXTS:
            continue
        parsed = _parse_swatch_filename(entry)
        if parsed is None:
            log.warning("Skipping swatch file with unexpected name format: %s", entry)
            continue
        label, hex1, hex2 = parsed
        found[label.lower().replace(" ", "_")] = (
            label, hex1, hex2, os.path.join(swatch_dir, entry),
            _hex_sort_key(hex1, hex2),
        )

    # Guard: an empty or all-invalid folder must never reach the removal loop,
    # which would retire the entire palette.
    if not found:
        raise ValueError(
            f"No valid swatch files found in `{swatch_dir}`. Upload images named "
            "`ColorName_HEX1_HEX2.png` before syncing."
        )

    added: list[str] = []
    disabled: list[str] = []
    removed: list[str] = []
    still_disabled: list[str] = []

    with open_db(db_path) as conn:
        existing = {str(r["key"]): r for r in list_catalog(conn, guild_id)}
        for key, (label, hex1, hex2, file_path, skey) in sorted(found.items()):
            if key not in existing:
                added.append(label)
            elif not int(existing[key]["enabled"]):
                still_disabled.append(str(existing[key]["name"]))
            upsert_catalog_color(
                conn, guild_id, key,
                name=label, hex1=hex1, hex2=hex2,
                image_path=file_path, sort_order=skey,
            )
        for key, row in existing.items():
            if key in found:
                continue
            if color_in_use(conn, guild_id, int(row["id"])):
                update_catalog_color(conn, guild_id, int(row["id"]), enabled=False)
                disabled.append(str(row["name"]))
            else:
                delete_catalog_color(conn, guild_id, int(row["id"]))
                removed.append(str(row["name"]))

    return added, disabled, removed, still_disabled
