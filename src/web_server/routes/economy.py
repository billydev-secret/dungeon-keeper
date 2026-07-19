"""Economy endpoints — read and update per-guild ``econ_`` settings."""

from __future__ import annotations

import io
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from bot_modules.economy.metrics import pricing_hints
from bot_modules.economy.quests import POOL_CAP
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
    login_text_base: int | None = Field(default=None, ge=0)
    login_voice_base: int | None = Field(default=None, ge=0)
    streak_bonus_cap: int | None = Field(default=None, ge=0)
    milestone_day7: int | None = Field(default=None, ge=0)
    milestone_day30: int | None = Field(default=None, ge=0)
    milestone_day100: int | None = Field(default=None, ge=0)
    milestone_per_100: int | None = Field(default=None, ge=0)
    reward_qotd: int | None = Field(default=None, ge=0)
    reward_game_participation: int | None = Field(default=None, ge=0)
    reward_game_win: int | None = Field(default=None, ge=0)
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
    price_role_gradient: int | None = Field(default=None, ge=0)
    price_text_room: int | None = Field(default=None, ge=0)
    price_voice_room: int | None = Field(default=None, ge=0)
    price_gift_color: int | None = Field(default=None, ge=0)
    price_quest_reroll: int | None = Field(default=None, ge=0)
    quest_reroll_daily_cap: int | None = Field(default=None, ge=0)


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
    values = body.model_dump(exclude_unset=True)

    def _q():
        with ctx.open_db() as conn:
            try:
                save_econ_settings(conn, guild_id, values)
            except KeyError as exc:
                # Defensive: extra="forbid" already blocks unknown keys, but a
                # bad key must never surface as a 500.
                raise HTTPException(422, str(exc)) from exc
        return {"ok": True}

    return await run_query(_q)


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
    png = _normalize_icon(content)

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
