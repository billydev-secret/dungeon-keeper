"""Economy endpoints — read and update per-guild ``econ_`` settings."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from bot_modules.economy.metrics import pricing_hints
from bot_modules.economy.quests import POOL_CAP
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
    price_role_color: int | None = Field(default=None, ge=0)
    price_role_name: int | None = Field(default=None, ge=0)
    price_role_icon: int | None = Field(default=None, ge=0)
    price_role_gradient: int | None = Field(default=None, ge=0)
    price_text_room: int | None = Field(default=None, ge=0)
    price_voice_room: int | None = Field(default=None, ge=0)
    price_gift_color: int | None = Field(default=None, ge=0)


@router.get("/economy/config")
async def get_economy_config(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            return asdict(load_econ_settings(conn, guild_id))

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
