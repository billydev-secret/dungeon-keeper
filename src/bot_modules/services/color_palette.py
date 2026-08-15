"""Curated colour palette — the swatch showroom, its picker button, and the sync.

The Discord surface for ``econ_color_catalog`` (DB layer:
``economy_color_catalog_service``). Three things live here:

* **The showroom panel.** One message per colour — the swatch image plus a
  button — posted to a channel by an admin. It is the only place the gradients
  can actually be *seen*, which is why it survived the move to the shop: a
  select menu can name a colour but not show it.
* **The picker button** (``PaletteColorButton``). Pressing a swatch wears that
  colour, if the member is entitled to the ``role_preset`` perk. It does not
  charge: buying happens in ``/bank shop``, where the price and the member's
  balance are on screen. A public button that silently debits a wallet is a
  worse trade than one extra step.
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
# Showroom panel
# ---------------------------------------------------------------------------


def _safe_filename(name: str, ext: str) -> str:
    """Sanitise a name into a Discord-safe attachment filename."""
    clean = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    return f"{clean}{ext}"


def _make_color_file(row: sqlite3.Row) -> discord.File | None:
    """Return a discord.File for a colour's swatch image, or None."""
    image_path = str(row["image_path"])
    if not image_path:
        return None
    if not os.path.isfile(image_path):
        log.warning("Palette color %r image not found: %s", row["key"], image_path)
        return None
    ext = os.path.splitext(image_path)[1] or ".png"
    filename = _safe_filename(str(row["key"]), ext)
    with open(image_path, "rb") as fp:
        data = fp.read()
    return discord.File(io.BytesIO(data), filename=filename)


async def post_or_update_palette_panel(
    db_path: Path,
    guild: discord.Guild,
    channel: discord.TextChannel,
) -> list[discord.Message]:
    """Post one message per palette colour (swatch + button). Returns the messages.

    Only rentable colours are shown — a disabled colour or one whose swatch
    filename never parsed has nothing to sell.
    """
    with open_db(db_path) as conn:
        colors = list_catalog(conn, guild.id, rentable_only=True)

    if not colors:
        return []

    # Delete old panel messages — bulk-delete per channel (≤100 at a time) so a
    # large panel doesn't burn through the per-channel DELETE bucket and 429.
    with open_db(db_path) as conn:
        old_refs = get_panel_refs(conn, guild.id)
    by_channel: dict[int, list[int]] = {}
    for ch_id, msg_id in old_refs:
        by_channel.setdefault(ch_id, []).append(msg_id)
    for ch_id, msg_ids in by_channel.items():
        old_ch = guild.get_channel(ch_id)
        if not isinstance(old_ch, discord.TextChannel):
            continue
        partials = [old_ch.get_partial_message(mid) for mid in msg_ids]
        for i in range(0, len(partials), 100):
            chunk = partials[i : i + 100]
            try:
                await old_ch.delete_messages(chunk)
            except (discord.NotFound, discord.HTTPException, discord.ClientException):
                # Bulk delete rejects messages >14 days old; fall back per-message.
                for pm in chunk:
                    try:
                        await pm.delete()
                    except (discord.NotFound, discord.HTTPException):
                        pass

    header = await channel.send(
        "**The color palette** — rent **Palette Color** from `/bank shop`, "
        "then press one to wear it. Switch as often as you like."
    )

    messages: list[discord.Message] = [header]
    for i, row in enumerate(colors):
        if i > 0:
            spacer = await channel.send("​")  # zero-width space as visual gap
            messages.append(spacer)

        view = discord.ui.View(timeout=None)
        btn: discord.ui.Button[discord.ui.View] = discord.ui.Button(
            label=str(row["name"]),
            style=discord.ButtonStyle.secondary,
            custom_id=f"booster_role:{row['key']}",
        )
        view.add_item(btn)

        file = _make_color_file(row)
        kwargs: dict = {"view": view}
        if file is not None:
            kwargs["file"] = file
        messages.append(await channel.send(**kwargs))

    with open_db(db_path) as conn:
        replace_panel_refs(
            conn, guild.id, [(channel.id, m.id) for m in messages]
        )
    log.info("Posted %d palette panel messages in #%s", len(messages), channel.name)
    return messages


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
