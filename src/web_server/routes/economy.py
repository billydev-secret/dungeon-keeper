"""Economy endpoints — read and update per-guild ``econ_`` settings."""

from __future__ import annotations

import asyncio
import io
import os
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from bot_modules.economy.metrics import pricing_hints
from bot_modules.economy.quests import POOL_CAP
from bot_modules.services.color_palette import (
    IMAGE_EXTS,
    get_guild_swatch_dir,
    post_or_update_palette_panel,
    resolve_swatch_directory,
    swatch_file_info,
    sync_palette,
)
from bot_modules.services.economy_color_catalog_service import (
    color_in_use,
    delete_catalog_color,
    get_catalog_color,
)
from bot_modules.services.economy_color_catalog_service import (
    list_catalog as list_color_catalog,
)
from bot_modules.services.economy_color_catalog_service import (
    update_catalog_color,
)
from bot_modules.services.economy_icon_catalog_service import (
    add_catalog_icon,
    delete_catalog_icon,
    get_catalog_icon,
    icon_catalog_path,
    icon_in_use,
    list_catalog,
    set_catalog_icon_image,
    update_catalog_icon,
)
from bot_modules.services.economy_metrics_service import (
    get_weekly_metrics,
    latest_median_income,
)
from bot_modules.services.economy_service import (
    load_econ_settings,
    save_econ_settings,
)
from web_server.auth import AuthenticatedUser
from web_server.routes.panel_posting import (
    ChannelIdBody,
    guild_or_503,
    require_post_permissions,
    text_channel_or_400,
)
from web_server.deps import (
    get_active_guild_id,
    get_ctx,
    require_perms,
    run_query,
)

router = APIRouter()


class EconomyConfigUpdate(BaseModel):
    """Partial update — every field optional; unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    bank_channel_id: int | None = Field(default=None, ge=0)
    # The public transaction feed's channel; 0 = feed off (the picker is the
    # toggle). The drain cursor beside it stays bot-managed, so it is not here.
    register_channel_id: int | None = Field(default=None, ge=0)
    # Where an approved Pin of the Day gets pinned; 0 = the feature is off (it
    # also needs price_pin_of_day > 0). The picker is the on switch.
    pin_channel_id: int | None = Field(default=None, ge=0)
    # The bounty board channel; 0 = bounties off (the picker is the on switch).
    bounty_channel_id: int | None = Field(default=None, ge=0)
    manager_role_id: int | None = Field(default=None, ge=0)
    game_role_id: int | None = Field(default=None, ge=0)
    qotd_ping_role_id: int | None = Field(default=None, ge=0)
    currency_name: str | None = Field(default=None, max_length=32)
    currency_plural: str | None = Field(default=None, max_length=32)
    currency_emoji: str | None = Field(default=None, max_length=64)
    currency_icon_url: str | None = Field(default=None, max_length=512)
    wallet_name: str | None = Field(default=None, max_length=32)
    transfers_enabled: bool | None = None
    booster_multiplier: float | None = Field(default=None, ge=1.0)
    xp_per_coin: float | None = Field(default=None, ge=0)
    # 0 = no ceiling on a member's daily XP conversion; see EconSettings.
    conversion_daily_cap: int | None = Field(default=None, ge=0)
    login_text_base: int | None = Field(default=None, ge=0)
    login_voice_base: int | None = Field(default=None, ge=0)
    streak_bonus_cap: int | None = Field(default=None, ge=0)
    milestone_day7: int | None = Field(default=None, ge=0)
    milestone_day30: int | None = Field(default=None, ge=0)
    milestone_day100: int | None = Field(default=None, ge=0)
    milestone_per_100: int | None = Field(default=None, ge=0)
    reward_qotd: int | None = Field(default=None, ge=0)
    reward_game_participation: int | None = Field(default=None, ge=0)
    reward_photo_post: int | None = Field(default=None, ge=0)
    reward_intake_step: int | None = Field(default=None, ge=0)
    # Cat Bot catch payout per rarity tier (games_external cat_catch dials).
    catcatch_coins_common: int | None = Field(default=None, ge=0)
    catcatch_coins_uncommon: int | None = Field(default=None, ge=0)
    catcatch_coins_rare: int | None = Field(default=None, ge=0)
    catcatch_coins_epic: int | None = Field(default=None, ge=0)
    catcatch_coins_mythic: int | None = Field(default=None, ge=0)
    catcatch_coins_divine: int | None = Field(default=None, ge=0)
    cat_catch_daily_cap: int | None = Field(default=None, ge=0)
    reward_game_win: int | None = Field(default=None, ge=0)
    reward_cah_win_max: int | None = Field(default=None, ge=0)
    # Host bounty: per-attendee payout to a party game's host, capped at
    # host_bounty_cap attendees. A 0 in *either* box ships it dark —
    # economy.logic.host_bounty_amount pays nothing on a non-positive rate or
    # cap, so the route matches the service rather than 422ing the one row on
    # Automatic Payments that couldn't be zeroed.
    host_bounty_per_joiner: int | None = Field(default=None, ge=0)
    host_bounty_cap: int | None = Field(default=None, ge=0)
    # Coin Drops. The channel picker is the toggle (0 = off). Cadence is an
    # average — the loop jitters each gap; 48/day (one per ~30 min) is
    # already spammy, so the cap is a guard-rail, not a target.
    drops_channel_id: int | None = Field(default=None, ge=0)
    drops_min_coins: int | None = Field(default=None, ge=0)
    drops_max_coins: int | None = Field(default=None, ge=0)
    drops_per_day: int | None = Field(default=None, ge=0, le=48)
    drops_expire_minutes: int | None = Field(default=None, ge=1)
    # 0 = cadence off for this guild; above POOL_CAP is meaningless (the pool
    # can't exceed it, and a board >= the pool is just "the whole pool").
    quest_board_daily: int | None = Field(default=None, ge=0, le=POOL_CAP)
    quest_board_weekly: int | None = Field(default=None, ge=0, le=POOL_CAP)
    quest_board_monthly: int | None = Field(default=None, ge=0, le=POOL_CAP)
    # Community-weekly beat sheets DM this member (0 = guild owner). Sent as
    # a string from the panel so the snowflake survives JS number precision.
    community_host_user_id: int | None = Field(default=None, ge=0)
    # Clear-the-board set bonuses (0 = off).
    quest_set_bonus_daily: int | None = Field(default=None, ge=0)
    quest_set_bonus_weekly: int | None = Field(default=None, ge=0)
    price_role_color: int | None = Field(default=None, ge=0)
    price_role_name: int | None = Field(default=None, ge=0)
    price_role_icon: int | None = Field(default=None, ge=0)
    price_role_preset: int | None = Field(default=None, ge=0)
    price_role_gradient: int | None = Field(default=None, ge=0)
    price_role_holographic: int | None = Field(default=None, ge=0)
    price_voice_style: int | None = Field(default=None, ge=0)
    mod_perk_comp: bool | None = None
    price_emoji: int | None = Field(default=None, ge=0)
    price_emoji_animated: int | None = Field(default=None, ge=0)
    emoji_sponsor_slots: int | None = Field(default=None, ge=0)
    emoji_sponsor_expire_days: int | None = Field(default=None, ge=0)
    price_text_room: int | None = Field(default=None, ge=0)
    price_voice_room: int | None = Field(default=None, ge=0)
    raffle_enabled: bool | None = None
    price_raffle_ticket: int | None = Field(default=None, ge=0)
    raffle_max_tickets: int | None = Field(default=None, ge=0)
    # Hoard tax: rate% of the excess above the threshold, weekly. 100 caps
    # wealth at the threshold (the floor is protected), so it's a real bound.
    demurrage_rate_pct: int | None = Field(default=None, ge=0, le=100)
    demurrage_threshold: int | None = Field(default=None, ge=0)
    # Wager rake capped well under half: past that a "winner takes the pot"
    # game stops being worth winning.
    wager_rake_pct: int | None = Field(default=None, ge=0, le=50)
    price_quest_reroll: int | None = Field(default=None, ge=0)
    quest_reroll_daily_cap: int | None = Field(default=None, ge=0)
    price_streak_shield: int | None = Field(default=None, ge=0)
    # Sponsored QOTD: charged at submit, refunded on denial/expiry. These were
    # absent from the whitelist, so the price sat at the hardcoded default and
    # couldn't be tuned from the dashboard — hence exposed here + on the Sinks
    # panel.
    price_qotd_sponsor: int | None = Field(default=None, ge=0)
    qotd_sponsor_expire_days: int | None = Field(default=None, ge=0)
    price_pin_of_day: int | None = Field(default=None, ge=0)
    pin_expire_days: int | None = Field(default=None, ge=0)
    bounty_min_stake: int | None = Field(default=None, ge=0)
    bounty_max_open: int | None = Field(default=None, ge=0)
    bounty_expire_days: int | None = Field(default=None, ge=0)
    bounty_rake_pct: int | None = Field(default=None, ge=0, le=100)


def _stringify_snowflakes(cfg: dict) -> dict:
    """Emit every ``*_id`` as a JSON string.

    Discord snowflakes exceed 2**53, so a bare JSON number loses its low digits
    the moment the browser parses it: ``JSON.parse("1526051848518373608")``
    yields ``1526051848518373600``. The panel would then write that rounded
    value straight back on the next save, silently repointing the setting at a
    role or channel that does not exist — which is exactly how this guild's
    game role, manager role and bank channel were lost. Strings survive the
    round trip, and every consumer already reads these via ``String(cfg.x)``.
    """
    return {
        key: (str(val) if key.endswith("_id") and isinstance(val, int) else val)
        for key, val in cfg.items()
    }


@router.get("/economy/config")
async def get_economy_config(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            return _stringify_snowflakes(asdict(load_econ_settings(conn, guild_id)))

    return await run_query(_q)


@router.get("/economy/metrics")
async def get_economy_metrics(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Weekly rollups (newest first) plus pricing hints from the latest median.

    ``weeks`` is a list of rollup dicts; ``faucet_mix`` stays a JSON string
    (``"{}"`` when nothing was minted). ``hints`` is ``{}`` until the first
    rollup exists — the config panel shows no suggestion lines in that case.
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            settings = load_econ_settings(conn, guild_id)
            weeks = [dict(r) for r in get_weekly_metrics(conn, guild_id, limit=12)]
            median = latest_median_income(conn, guild_id)
            hints = pricing_hints(median, settings)
        return {"weeks": weeks, "hints": hints, "median_income": median}

    return await run_query(_q)


@router.put("/economy/config")
async def update_economy_config(
    request: Request,
    body: EconomyConfigUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    # Fields sent as explicit null mean "no change" — without this filter
    # str(None) would be persisted and read back as the string "None".
    values = {
        k: v for k, v in body.model_dump(exclude_unset=True).items()
        if v is not None
    }

    def _q():
        with ctx.open_db() as conn:
            try:
                save_econ_settings(conn, guild_id, values)
            except KeyError as exc:
                # Defensive: extra="forbid" already blocks unknown keys, but a
                # bad key must never surface as a 500.
                raise HTTPException(422, str(exc)) from exc
        return {"ok": True}

    result = await run_query(_q)
    # The casino's hub panel documents "economy off ⇒ panel torn down" —
    # the cog can only honor that if this save pokes it (same dispatch the
    # casino config PUT uses; ensure_panel re-reads everything itself).
    if ctx.bot:
        ctx.bot.dispatch("casino_config_change", guild_id)
    return result


# ── rentable icon catalog ───────────────────────────────────────────────
#
# Admin-curated role icons members rent from the perk shop (a currency sink).
# Each icon carries its own weekly price; the rental engine bills that price via
# ``econ_rentals.catalog_icon_id``. Images are normalized to a small PNG and
# stored under ``<db-parent>/econ_icon_catalog/<guild_id>/<id>.png``.

# Discord caps a role icon at 256KB; mirror that on the stored, re-encoded PNG.
_MAX_ICON_STORE_BYTES = 256 * 1024
# Generous cap on the raw upload before we re-encode (the PNG is what's limited).
_MAX_ICON_UPLOAD_BYTES = 8 * 1024 * 1024
# Role icons render tiny — downscale to keep files well under the 256KB cap.
_ICON_MAX_DIM = 128


class IconCatalogPatch(BaseModel):
    """Partial update of a catalog icon's metadata; unknown keys rejected."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=64)
    price: int | None = Field(default=None, ge=0)
    enabled: bool | None = None
    sort_order: int | None = Field(default=None, ge=0)


def _icon_dict(conn, guild_id: int, row) -> dict:
    """Serialise a catalog row for the dashboard, tagging live-rental usage."""
    icon_id = int(row["id"])
    return {
        "id": icon_id,
        "name": row["name"],
        "price": int(row["price"]),
        "enabled": bool(row["enabled"]),
        "sort_order": int(row["sort_order"]),
        "in_use": icon_in_use(conn, guild_id, icon_id),
    }


def _normalize_icon(content: bytes) -> bytes:
    """Re-encode an upload to a small RGBA PNG, or raise HTTP 400/413.

    Downscales to ``_ICON_MAX_DIM`` and rejects a result over Discord's 256KB
    role-icon limit — so what the dashboard stores is always what Discord will
    accept as a ``display_icon``.

    Blocking (up to ~2 s of PIL CPU for an 8 MB source) and pure bytes-in /
    bytes-out, so callers must run it via ``asyncio.to_thread``: the dashboard
    shares the bot's event loop and this would otherwise stall the gateway.
    """
    if not content:
        raise HTTPException(400, "Empty file.")

    from PIL import Image, UnidentifiedImageError  # noqa: PLC0415

    try:
        with Image.open(io.BytesIO(content)) as im:
            im.load()
            img = im.convert("RGBA")
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(400, "Unsupported or corrupt image.") from exc

    img.thumbnail((_ICON_MAX_DIM, _ICON_MAX_DIM), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    data = buf.getvalue()
    if len(data) > _MAX_ICON_STORE_BYTES:
        raise HTTPException(
            400,
            "That image is too detailed — Discord caps role icons at 256KB. "
            "Try a simpler image.",
        )
    return data


@router.get("/economy/icon-catalog")
async def list_icon_catalog(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Every catalog icon (enabled and disabled), with a live-rental usage flag."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            return [_icon_dict(conn, guild_id, r) for r in list_catalog(conn, guild_id)]

    return await run_query(_q)


@router.get("/economy/icon-catalog/{icon_id}/image")
async def get_icon_catalog_image(
    icon_id: int,
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Serve a catalog icon's PNG for dashboard preview (admin, guild-scoped)."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _load() -> str:
        with ctx.open_db() as conn:
            row = get_catalog_icon(conn, guild_id, icon_id)
            return str(row["image_path"]) if row is not None else ""

    image_path = await run_query(_load)
    if not image_path or not Path(image_path).is_file():
        raise HTTPException(404, "No image for this icon.")
    return FileResponse(image_path, media_type="image/png")


@router.post("/economy/icon-catalog")
async def create_icon_catalog(
    request: Request,
    name: str = Form(...),
    price: int = Form(...),
    image: UploadFile = File(...),
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Add a catalog icon: normalise the image, store it, insert the row."""
    name = name.strip()
    if not name or len(name) > 64:
        raise HTTPException(400, "Name must be 1–64 characters.")
    if price < 0:
        raise HTTPException(400, "Price can't be negative.")
    content = await image.read(_MAX_ICON_UPLOAD_BYTES + 1)
    if len(content) > _MAX_ICON_UPLOAD_BYTES:
        raise HTTPException(413, "Image must be 8 MB or smaller.")
    png = await asyncio.to_thread(_normalize_icon, content)

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            icon_id = add_catalog_icon(conn, guild_id, name=name, price=price)
            path = icon_catalog_path(ctx.db_path, guild_id, icon_id)
            # Written inside the transaction so a disk failure rolls the row back.
            path.write_bytes(png)
            set_catalog_icon_image(conn, guild_id, icon_id, str(path))
            row = get_catalog_icon(conn, guild_id, icon_id)
            return _icon_dict(conn, guild_id, row)

    return await run_query(_q)


@router.patch("/economy/icon-catalog/{icon_id}")
async def patch_icon_catalog(
    icon_id: int,
    request: Request,
    body: IconCatalogPatch,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Rename / re-price / enable-disable / reorder a catalog icon.

    A price change is not charged immediately — existing renters pick it up at
    their next weekly renewal (the billing engine re-reads the current price).
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            if get_catalog_icon(conn, guild_id, icon_id) is None:
                raise HTTPException(404, "Icon not found.")
            row = update_catalog_icon(
                conn, guild_id, icon_id,
                name=body.name, price=body.price,
                enabled=body.enabled, sort_order=body.sort_order,
            )
            return _icon_dict(conn, guild_id, row)

    return await run_query(_q)


@router.delete("/economy/icon-catalog/{icon_id}")
async def remove_icon_catalog(
    icon_id: int,
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Hard-delete a catalog icon — blocked (409) while members are renting it.

    An in-use icon must be disabled, not deleted, so current renters keep the
    icon they paid for.
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            row = get_catalog_icon(conn, guild_id, icon_id)
            if row is None:
                raise HTTPException(404, "Icon not found.")
            if icon_in_use(conn, guild_id, icon_id):
                raise HTTPException(
                    409,
                    "Members are renting this icon — disable it instead of deleting.",
                )
            image_path = str(row["image_path"])
            delete_catalog_icon(conn, guild_id, icon_id)
        if image_path:
            Path(image_path).unlink(missing_ok=True)
        return {"ok": True}

    return await run_query(_q)


# ── curated colour palette ──────────────────────────────────────────────
#
# The admin side of ``econ_color_catalog`` — the palette members rent from via
# the ``role_preset`` perk. It is the icon catalog's sibling and works the same
# way, with two differences that come from its history as the booster
# cosmetic-role picker (migration 159):
#
#  * Colours are authored by *filename*, not by form: swatch images named
#    ``ColorName_HEX1_HEX2.ext`` are uploaded to a managed folder and Sync reads
#    the name, gradient pair and display order out of them. So there is no
#    "create colour" endpoint — upload art and sync.
#  * There is a showroom panel to re-post, because a gradient has to be seen.

_MAX_SWATCH_BYTES = 8 * 1024 * 1024


class ColorCatalogPatch(BaseModel):
    """Partial update of a palette colour's metadata; unknown keys rejected."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=64)
    price: int | None = Field(default=None, ge=0)
    enabled: bool | None = None
    sort_order: int | None = Field(default=None, ge=0)


def _color_dict(conn, guild_id: int, row) -> dict:
    """Serialise a palette row for the dashboard.

    ``price`` 0 means "bill the flat ``price_role_preset``", which the dashboard
    renders as an inherited price rather than free. ``rentable`` is false for a
    row whose swatch filename never parsed — it needs a re-sync, and the panel
    says so.
    """
    color_id = int(row["id"])
    hex1, hex2 = str(row["hex1"]), str(row["hex2"])
    return {
        "id": color_id,
        "key": row["key"],
        "name": row["name"],
        "hex1": hex1,
        "hex2": hex2,
        "price": int(row["price"]),
        "enabled": bool(row["enabled"]),
        "sort_order": int(row["sort_order"]),
        "rentable": bool(hex1 and hex2),
        "in_use": color_in_use(conn, guild_id, color_id),
        # A grandfathered wearer's Discord role, kept for the record. Never
        # granted or revoked by anything — see the migration.
        "legacy_role_id": str(row["legacy_role_id"]),
    }


@router.get("/economy/color-catalog")
async def list_color_palette(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Every palette colour (enabled and disabled), with usage and sync flags."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            return [
                _color_dict(conn, guild_id, r)
                for r in list_color_catalog(conn, guild_id)
            ]

    return await run_query(_q)


@router.get("/economy/color-catalog/{color_id}/image")
async def get_color_catalog_image(
    color_id: int,
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Serve a colour's swatch image for dashboard preview (admin, guild-scoped)."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _load() -> str:
        with ctx.open_db() as conn:
            row = get_catalog_color(conn, guild_id, color_id)
            return str(row["image_path"]) if row is not None else ""

    image_path = await run_query(_load)
    if not image_path or not Path(image_path).is_file():
        raise HTTPException(404, "No image for this color.")
    return FileResponse(image_path)


@router.patch("/economy/color-catalog/{color_id}")
async def patch_color_catalog(
    color_id: int,
    request: Request,
    body: ColorCatalogPatch,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Rename / re-price / enable-disable / reorder a palette colour.

    A price change is not charged immediately — existing renters pick it up at
    their next weekly renewal. The gradient itself is not editable here: it comes
    from the swatch filename, so changing it by hand would desync colour from art.
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            if get_catalog_color(conn, guild_id, color_id) is None:
                raise HTTPException(404, "Color not found.")
            row = update_catalog_color(
                conn, guild_id, color_id,
                name=body.name, price=body.price,
                enabled=body.enabled, sort_order=body.sort_order,
            )
            return _color_dict(conn, guild_id, row)

    return await run_query(_q)


@router.delete("/economy/color-catalog/{color_id}")
async def remove_color_catalog(
    color_id: int,
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Hard-delete a palette colour — blocked (409) while members are renting it.

    Only the catalog row goes: the swatch file stays (delete it from the swatch
    list to retire the colour properly) and so does any legacy Discord role,
    which a grandfathered member may still be wearing.
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            if get_catalog_color(conn, guild_id, color_id) is None:
                raise HTTPException(404, "Color not found.")
            if color_in_use(conn, guild_id, color_id):
                raise HTTPException(
                    409,
                    "Members are renting this color — disable it instead of deleting.",
                )
            delete_catalog_color(conn, guild_id, color_id)
        return {"ok": True}

    return await run_query(_q)


@router.post("/economy/color-catalog/sync")
async def sync_color_catalog(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Reconcile the palette to the swatch files on disk.

    Touches no Discord roles. A colour whose swatch is gone is disabled when
    somebody is renting it and deleted otherwise, so the reply distinguishes the
    two.
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        try:
            added, disabled, removed = sync_palette(ctx.db_path, guild_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return {
            "ok": True,
            "added": added,
            "disabled": disabled,
            "removed": removed,
        }

    return await run_query(_q)


@router.post("/economy/color-catalog/post-panel")
async def post_color_panel(
    request: Request,
    body: ChannelIdBody,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Re-post the palette showroom in the chosen channel."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    guild = guild_or_503(ctx, guild_id)
    channel = text_channel_or_400(guild, body.channel_id)
    # Attachments, not embeds — each colour posts as an image file.
    require_post_permissions(
        guild, channel, "view_channel", "send_messages", "attach_files"
    )

    msgs = await post_or_update_palette_panel(ctx.db_path, guild, channel)
    if not msgs:
        raise HTTPException(400, "No rentable colors in the palette.")
    return {"ok": True, "message_count": len(msgs)}


# ── managed swatch uploads (per-guild folder) ───────────────────────────


def _safe_swatch_name(filename: str | None) -> str:
    """Reject path traversal / unsupported types; return a bare filename."""
    name = os.path.basename(filename or "")
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise HTTPException(400, "Invalid filename")
    ext = os.path.splitext(name)[1].lower()
    if ext not in IMAGE_EXTS:
        raise HTTPException(400, f"Unsupported file type: {ext or '(none)'}")
    return name


def _swatch_listing(db_path, guild_id: int) -> dict:
    managed = get_guild_swatch_dir(db_path, guild_id)
    files = swatch_file_info(managed)
    # The valid-swatch count resolve_swatch_directory needs is already in
    # ``files`` — handing it over saves a second walk of the same directory.
    active = resolve_swatch_directory(
        db_path,
        guild_id,
        managed=managed,
        managed_valid_count=sum(1 for f in files if f["valid"]),
    )
    return {
        "ok": True,
        "files": files,
        "managed_dir": str(managed),
        "active_dir": active,
        "using_managed": active == str(managed),
    }


@router.get("/economy/color-catalog/swatches")
async def list_color_swatches(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """List uploaded swatch files in this guild's managed folder."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    # Directory walk + a config read, so off the event loop (as are the upload
    # and delete handlers below — the dashboard shares the bot's event loop, so
    # an 8 MB write on it stalls the Discord gateway).
    return await run_query(lambda: _swatch_listing(ctx.db_path, guild_id))


@router.post("/economy/color-catalog/swatches")
async def upload_color_swatches(
    request: Request,
    files: list[UploadFile] = File(...),
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Save one or more uploaded swatch images into the managed folder."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    managed = get_guild_swatch_dir(ctx.db_path, guild_id)

    saved: list[str] = []
    for upload in files:
        name = _safe_swatch_name(upload.filename)
        content = await upload.read()
        if not content:
            continue
        if len(content) > _MAX_SWATCH_BYTES:
            raise HTTPException(400, f"{name} exceeds the 8 MB limit")
        target = managed / name
        if target.resolve().parent != managed.resolve():
            raise HTTPException(400, "Invalid filename")
        # Up to 8 MB of disk write per file — off the loop.
        await asyncio.to_thread(target.write_bytes, content)
        saved.append(name)

    listing = await run_query(lambda: _swatch_listing(ctx.db_path, guild_id))
    return {**listing, "saved": saved}


@router.delete("/economy/color-catalog/swatches/{filename}")
async def delete_color_swatch(
    filename: str,
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Delete a single uploaded swatch from the managed folder."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    name = _safe_swatch_name(filename)
    managed = get_guild_swatch_dir(ctx.db_path, guild_id)
    target = managed / name
    if target.resolve().parent != managed.resolve():
        raise HTTPException(400, "Invalid filename")
    if not target.is_file():
        raise HTTPException(404, "File not found")

    def _delete() -> dict:
        target.unlink()
        return _swatch_listing(ctx.db_path, guild_id)

    return await run_query(_delete)
