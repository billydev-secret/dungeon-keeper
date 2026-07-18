"""Bank Manager endpoints — the quest library, claim sign-off, community
goals, ledger audit, and manual grants.

Every route is gated by ``require_economy_manager`` (admins OR holders of the
configured ``manager_role_id``). Paths mount under ``/api/economy`` alongside
the admin-only config router.

Service errors map to HTTP status the dashboard can act on:
``SlotLimitError`` (activation over-fills a slot) and the ``ValueError`` from
``delete_quest`` (paid claims exist) / ``resolve_claim`` (claim not pending) →
409; an unknown quest field → 422. Resolving a claim also best-effort edits the
Discord sign-off card and DMs the claimant; a card/DM failure never fails the
API call (it returns 200 with ``card_updated: false``).
"""

from __future__ import annotations

import logging
import sqlite3
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from bot_modules.economy import quests as quest_rules
from bot_modules.economy.perk_actions import revoke_role_perks
from bot_modules.services import economy_quests_service as quests_svc
from bot_modules.services import economy_rentals_service as rentals_svc
from bot_modules.services.economy_service import (
    apply_credit,
    load_econ_settings,
    member_is_booster,
    notify_member,
)
from bot_modules.services.economy_stats_service import compute_stats
from web_server.auth import AuthenticatedUser
from web_server.deps import (
    get_active_guild_id,
    get_ctx,
    require_economy_manager,
    run_query,
)

router = APIRouter()
_log = logging.getLogger("dungeonkeeper.web.economy_manager")

_QTYPES = ("daily", "weekly", "monthly", "community", "event")
# The AI idea generator has no prompt shape for trigger-paid event quests.
_AI_QTYPES = ("daily", "weekly", "community")


# ── request bodies ────────────────────────────────────────────────────


class QuestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=2000)
    qtype: str
    reward: int = Field(ge=0)
    signoff: bool = False
    criteria: str = Field(default="", max_length=2000)
    starts_at: float | None = None
    ends_at: float | None = None
    rotate_tag: str = Field(default="", max_length=64)
    community_target: int | None = Field(default=None, ge=0)
    trigger_words: str = Field(default="", max_length=1000)
    trigger_channel_id: int | None = None
    trigger_kind: str = Field(default="", max_length=32)
    target_count: int = Field(default=1, ge=1, le=10000)
    reward_xp: int = Field(default=0, ge=0, le=100000)


class QuestUpdate(BaseModel):
    """Partial patch — only the columns ``update_quest`` accepts. ``active`` is
    deliberately absent (activation flows through the ``/active`` endpoint), so
    ``extra='forbid'`` turns an ``active`` key into a 422 at parse time."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=2000)
    qtype: str | None = None
    reward: int | None = Field(default=None, ge=0)
    signoff: bool | None = None
    criteria: str | None = Field(default=None, max_length=2000)
    starts_at: float | None = None
    ends_at: float | None = None
    rotate_tag: str | None = Field(default=None, max_length=64)
    community_target: int | None = Field(default=None, ge=0)
    trigger_words: str | None = Field(default=None, max_length=1000)
    trigger_channel_id: int | None = None
    trigger_kind: str | None = Field(default=None, max_length=32)
    target_count: int | None = Field(default=None, ge=1, le=10000)
    reward_xp: int | None = Field(default=None, ge=0, le=100000)


class QuestGenerateBody(BaseModel):
    """Ask the AI for a batch of quest ideas of one type. ``theme`` is an
    optional steer; ``count`` is clamped server-side to the module's cap."""

    model_config = ConfigDict(extra="forbid")

    qtype: str
    count: int = Field(default=0, ge=0)
    theme: str = Field(default="", max_length=200)


class ActiveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool


class SourceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class DenyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=300)


class ProgressBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current: int = Field(ge=0)


class GrantBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    member_id: int
    amount: int = Field(ge=1)
    reason: str = Field(default="", max_length=300)


# ── serialization ─────────────────────────────────────────────────────


def _quest_dict(row: sqlite3.Row | None) -> dict:
    assert row is not None
    return {
        "id": int(row["id"]),
        "title": row["title"],
        "description": row["description"],
        "qtype": row["qtype"],
        "reward": int(row["reward"]),
        "signoff": bool(row["signoff"]),
        "criteria": row["criteria"],
        "starts_at": row["starts_at"],
        "ends_at": row["ends_at"],
        "active": bool(row["active"]),
        "rotate_tag": row["rotate_tag"],
        "community_target": row["community_target"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "trigger_words": row["trigger_words"],
        "trigger_kind": row["trigger_kind"],
        "target_count": int(row["target_count"]),
        "reward_xp": int(row["reward_xp"]),
        # Stringified: channel snowflakes overflow JS number precision.
        "trigger_channel_id": (
            str(row["trigger_channel_id"])
            if row["trigger_channel_id"] is not None
            else None
        ),
    }


def _claim_dict(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "quest_id": int(row["quest_id"]),
        "user_id": str(row["user_id"]),
        "period": row["period"],
        "state": row["state"],
        "created_at": row["created_at"],
        "resolved_at": row["resolved_at"],
        "resolver_id": str(row["resolver_id"]) if row["resolver_id"] else None,
        "deny_reason": row["deny_reason"],
    }


def _rental_dict(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "user_id": str(row["user_id"]),
        "perk": row["perk"],
        "state": row["state"],
        # ``price`` is the current guild price once past the first week (rent-time
        # snapshot only for week 1) — the panel labels it "price/wk (current)".
        "price": int(row["price"]),
        "next_bill_at": row["next_bill_at"],
        "suspended": bool(row["suspended"]),
        "cancel_at_period_end": bool(row["cancel_at_period_end"]),
        # Equal to user_id except for gift_color (the befriended recipient).
        "beneficiary_id": str(row["beneficiary_id"]),
    }


# ── quest library CRUD ────────────────────────────────────────────────


@router.get("/economy/quests")
async def list_quests(
    request: Request,
    _: AuthenticatedUser = Depends(require_economy_manager),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            rows = quests_svc.list_quests(conn, guild_id)
            out = []
            for row in rows:
                quest = _quest_dict(row)
                if row["qtype"] == "community":
                    prog = conn.execute(
                        "SELECT current, completed_at, settled_at "
                        "FROM econ_community_progress WHERE quest_id = ?",
                        (int(row["id"]),),
                    ).fetchone()
                    quest["community_current"] = int(prog["current"]) if prog else 0
                    quest["community_completed_at"] = (
                        prog["completed_at"] if prog else None
                    )
                    quest["community_settled_at"] = prog["settled_at"] if prog else None
                out.append(quest)
            return out

    return {"quests": await run_query(_q)}


@router.post("/economy/quests")
async def create_quest(
    request: Request,
    body: QuestCreate,
    user: AuthenticatedUser = Depends(require_economy_manager),
):
    if body.qtype not in _QTYPES:
        raise HTTPException(422, f"unknown quest type: {body.qtype!r}")
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            try:
                quest_id = quests_svc.create_quest(
                    conn,
                    guild_id,
                    title=body.title,
                    description=body.description,
                    qtype=body.qtype,
                    reward=body.reward,
                    signoff=1 if body.signoff else 0,
                    criteria=body.criteria,
                    starts_at=body.starts_at,
                    ends_at=body.ends_at,
                    rotate_tag=body.rotate_tag,
                    community_target=body.community_target,
                    created_by=user.user_id,
                    trigger_words=body.trigger_words,
                    trigger_channel_id=body.trigger_channel_id,
                    trigger_kind=body.trigger_kind,
                    target_count=body.target_count,
                    reward_xp=body.reward_xp,
                )
            except ValueError as exc:
                # Bad qtype/trigger_kind pairing from the service validator.
                raise HTTPException(422, str(exc)) from exc
            return _quest_dict(quests_svc.get_quest(conn, guild_id, quest_id))

    return await run_query(_q)


@router.post("/economy/quests/generate")
async def generate_quest_ideas(
    request: Request,
    body: QuestGenerateBody,
    _: AuthenticatedUser = Depends(require_economy_manager),
):
    """Generate a batch of quest ideas for the New-quest form to load.

    Uses the same Anthropic cloud path as the party-game studio
    (:func:`bot_modules.games.utils.ai_client.generate_text`) — not the local
    LLM. Nothing is persisted: the manager reviews the returned ideas and
    one-click loads one into the form, editing before they create it.
    """
    from bot_modules.economy import quest_ai
    from bot_modules.games.utils.ai_client import generate_text

    if body.qtype not in _AI_QTYPES:
        raise HTTPException(422, f"unknown quest type: {body.qtype!r}")

    count = body.count or quest_ai.DEFAULT_COUNT
    count = max(1, min(quest_ai.MAX_COUNT, count))

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _settings():
        with ctx.open_db() as conn:
            return load_econ_settings(conn, guild_id)

    settings = await run_query(_settings)

    system = quest_ai.build_system_prompt(settings.currency_name)
    user = quest_ai.build_user_prompt(body.qtype, count, body.theme)
    text = await generate_text(
        system,
        user,
        max_tokens=quest_ai.MAX_TOKENS,
        temperature=quest_ai.TEMPERATURE,
    )
    if not text:
        raise HTTPException(503, "Idea generation failed — check ANTHROPIC_API_KEY.")

    ideas = quest_ai.parse_quest_ideas(text, body.qtype, limit=count)
    return {"ideas": [idea.as_dict() for idea in ideas]}


@router.put("/economy/quests/{quest_id}")
async def update_quest(
    request: Request,
    quest_id: int,
    body: QuestUpdate,
    _: AuthenticatedUser = Depends(require_economy_manager),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    values = body.model_dump(exclude_unset=True)
    if "qtype" in values and values["qtype"] not in _QTYPES:
        raise HTTPException(422, f"unknown quest type: {values['qtype']!r}")

    def _q():
        with ctx.open_db() as conn:
            if quests_svc.get_quest(conn, guild_id, quest_id) is None:
                raise HTTPException(404, "quest not found")
            try:
                quests_svc.update_quest(conn, guild_id, quest_id, values)
            except KeyError as exc:
                # extra="forbid" already blocks unknown keys; this is defence.
                raise HTTPException(422, str(exc)) from exc
            except ValueError as exc:
                # Bad qtype/trigger_kind pairing from the service validator.
                raise HTTPException(422, str(exc)) from exc
            return _quest_dict(quests_svc.get_quest(conn, guild_id, quest_id))

    return await run_query(_q)


@router.delete("/economy/quests/{quest_id}")
async def delete_quest(
    request: Request,
    quest_id: int,
    _: AuthenticatedUser = Depends(require_economy_manager),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            if quests_svc.get_quest(conn, guild_id, quest_id) is None:
                raise HTTPException(404, "quest not found")
            try:
                quests_svc.delete_quest(conn, guild_id, quest_id)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    return await run_query(_q)


@router.post("/economy/quests/{quest_id}/active")
async def set_quest_active(
    request: Request,
    quest_id: int,
    body: ActiveBody,
    _: AuthenticatedUser = Depends(require_economy_manager),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            try:
                quests_svc.set_quest_active(conn, guild_id, quest_id, body.active)
            except quests_svc.SlotLimitError as exc:
                raise HTTPException(409, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from exc
            return _quest_dict(quests_svc.get_quest(conn, guild_id, quest_id))

    return await run_query(_q)


# ── income sources (custom-coded trigger hooks) ───────────────────────


@router.get("/economy/income-sources")
async def list_income_sources(
    request: Request,
    _: AuthenticatedUser = Depends(require_economy_manager),
):
    """Every custom trigger source with its enable state and quest usage."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            states = quests_svc.list_income_sources(conn, guild_id)
            usage: dict[str, list[dict]] = {}
            for row in conn.execute(
                """
                SELECT trigger_kind, title, qtype, active FROM econ_quests
                WHERE guild_id = ? AND trigger_kind != ''
                ORDER BY active DESC, id
                """,
                (guild_id,),
            ).fetchall():
                usage.setdefault(str(row["trigger_kind"]), []).append({
                    "title": row["title"],
                    "qtype": row["qtype"],
                    "active": bool(row["active"]),
                })
            settings = load_econ_settings(conn, guild_id)
            return {
                "sources": [
                    {
                        "source": kind,
                        "label": quest_rules.TRIGGER_KINDS[kind],
                        "info": quest_rules.TRIGGER_KIND_INFO.get(kind, ""),
                        "enabled": states[kind],
                        "quests": usage.get(kind, []),
                    }
                    for kind in quest_rules.TRIGGER_KINDS
                ],
                # Built-in faucet rates. Managers see them read-only here;
                # admins edit them in place on this page via the admin-gated
                # PUT /economy/config (partial update).
                "faucets": {
                    "login_text_base": settings.login_text_base,
                    "login_voice_base": settings.login_voice_base,
                    "streak_bonus_cap": settings.streak_bonus_cap,
                    "milestone_day7": settings.milestone_day7,
                    "milestone_day30": settings.milestone_day30,
                    "milestone_day100": settings.milestone_day100,
                    "milestone_per_100": settings.milestone_per_100,
                    "reward_qotd": settings.reward_qotd,
                    "reward_game_participation": settings.reward_game_participation,
                    "reward_game_win": settings.reward_game_win,
                    "xp_per_coin": settings.xp_per_coin,
                },
            }

    return await run_query(_q)


@router.put("/economy/income-sources/{source}")
async def set_income_source(
    request: Request,
    source: str,
    body: SourceBody,
    _: AuthenticatedUser = Depends(require_economy_manager),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            try:
                quests_svc.set_income_source(conn, guild_id, source, body.enabled)
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from exc
        return {"source": source, "enabled": body.enabled}

    return await run_query(_q)


# ── claims ────────────────────────────────────────────────────────────


@router.get("/economy/claims")
async def list_claims(
    request: Request,
    state: str | None = None,
    _: AuthenticatedUser = Depends(require_economy_manager),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            rows = quests_svc.list_claims(conn, guild_id, state=state)
            titles = {
                int(r["id"]): r["title"]
                for r in quests_svc.list_quests(conn, guild_id)
            }
            out = []
            for row in rows:
                claim = _claim_dict(row)
                claim["quest_title"] = titles.get(int(row["quest_id"]), "")
                claim["deny_count"] = len(
                    quests_svc.deny_history(
                        conn, int(row["quest_id"]), int(row["user_id"])
                    )
                )
                out.append(claim)
            return out

    return {"claims": await run_query(_q)}


async def _resolve_and_notify(
    request: Request,
    claim_id: int,
    *,
    approve: bool,
    resolver_id: int,
    deny_reason: str | None,
) -> dict:
    """Resolve a pending claim, then best-effort edit its card + DM.

    Booster status is the CLAIMANT's (read before resolving). The card edit and
    DM only run when the bot is ready; either failing leaves the API 200 with
    ``card_updated: false``.
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)

    # Phase 1 (thread): read the claim + settings so booster can be computed off
    # the worker thread, before the resolving write.
    def _read():
        with ctx.open_db() as conn:
            claim = conn.execute(
                "SELECT * FROM econ_quest_claims WHERE id = ? AND guild_id = ?",
                (claim_id, guild_id),
            ).fetchone()
            settings = load_econ_settings(conn, guild_id)
            return claim, settings

    claim, settings = await run_query(_read)
    if claim is None:
        raise HTTPException(404, "claim not found")
    claimant_id = int(claim["user_id"])
    booster = bool(bot) and member_is_booster(bot, guild_id, claimant_id)

    # Phase 2 (thread): the resolving write + credit.
    def _resolve():
        with ctx.open_db() as conn:
            try:
                return quests_svc.resolve_claim(
                    conn,
                    settings,
                    claim_id,
                    approve=approve,
                    resolver_id=resolver_id,
                    deny_reason=deny_reason,
                    booster=booster,
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc

    resolution = await run_query(_resolve)

    # Phase 3 (async): best-effort Discord card edit + DM. Never fatal.
    card_updated = False
    if bot is not None and bot.is_ready():
        card_updated = await _update_card_and_dm(
            bot, ctx, guild_id, claim, settings, approve, resolution
        )

    return {
        "ok": True,
        "paid": resolution.paid,
        "card_updated": card_updated,
    }


async def _update_card_and_dm(
    bot, ctx, guild_id, claim, settings, approve, resolution
) -> bool:
    """Edit the sign-off card to reflect the resolution and DM the claimant.

    Returns True only if the card message was edited; DM failure alone does not
    flip it back. All Discord errors are swallowed (logged) so the API stays
    200 regardless.
    """
    import discord

    unit = settings.currency_plural if resolution.paid != 1 else settings.currency_name
    card_updated = False
    channel_id = claim["card_channel_id"]
    message_id = claim["card_message_id"]
    if channel_id and message_id:
        try:
            channel = bot.get_channel(int(channel_id))
            if isinstance(channel, discord.abc.Messageable):
                message = await channel.fetch_message(int(message_id))
                embed = message.embeds[0] if message.embeds else discord.Embed()
                if approve:
                    embed.colour = discord.Colour.green()
                    embed.add_field(
                        name="Approved",
                        value=f"Paid {resolution.paid:,} {unit}.",
                        inline=False,
                    )
                else:
                    embed.colour = discord.Colour.red()
                    embed.add_field(
                        name="Denied",
                        value=resolution.deny_reason or "—",
                        inline=False,
                    )
                await message.edit(embed=embed, view=None)
                card_updated = True
        except (discord.HTTPException, discord.NotFound, discord.Forbidden, IndexError):
            _log.warning("quest claim card edit failed", exc_info=True)

    try:
        if approve:
            content = (
                f"Your quest claim was approved — {resolution.paid:,} "
                f"{unit} {settings.currency_emoji}."
            )
        else:
            reason = resolution.deny_reason or ""
            content = f"Your quest claim was denied. {reason}".strip()
        await notify_member(
            bot, ctx.db_path, guild_id, int(claim["user_id"]), content=content
        )
    except Exception:  # noqa: BLE001 — notification must never fail the request
        _log.warning("quest claim DM failed", exc_info=True)

    return card_updated


@router.post("/economy/claims/{claim_id}/approve")
async def approve_claim(
    request: Request,
    claim_id: int,
    user: AuthenticatedUser = Depends(require_economy_manager),
):
    return await _resolve_and_notify(
        request, claim_id, approve=True, resolver_id=user.user_id, deny_reason=None
    )


@router.post("/economy/claims/{claim_id}/deny")
async def deny_claim(
    request: Request,
    claim_id: int,
    body: DenyBody,
    user: AuthenticatedUser = Depends(require_economy_manager),
):
    return await _resolve_and_notify(
        request,
        claim_id,
        approve=False,
        resolver_id=user.user_id,
        deny_reason=body.reason,
    )


# ── community quests ──────────────────────────────────────────────────


@router.post("/economy/quests/{quest_id}/progress")
async def set_progress(
    request: Request,
    quest_id: int,
    body: ProgressBody,
    _: AuthenticatedUser = Depends(require_economy_manager),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            quest = quests_svc.get_quest(conn, guild_id, quest_id)
            if quest is None:
                raise HTTPException(404, "quest not found")
            if quest["qtype"] != "community":
                raise HTTPException(422, "progress applies only to community quests")
            target = quest["community_target"] or 0
            crossed = quests_svc.set_community_progress(
                conn, quest_id, body.current, target=target
            )
            return {"ok": True, "current": body.current, "completed": crossed}

    return await run_query(_q)


@router.post("/economy/quests/{quest_id}/settle")
async def settle_quest(
    request: Request,
    quest_id: int,
    _: AuthenticatedUser = Depends(require_economy_manager),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)

    # Phase 1 (thread): active roster + settings.
    def _read():
        with ctx.open_db() as conn:
            quest = quests_svc.get_quest(conn, guild_id, quest_id)
            if quest is None:
                raise HTTPException(404, "quest not found")
            if quest["qtype"] != "community":
                raise HTTPException(422, "settle applies only to community quests")
            settings = load_econ_settings(conn, guild_id)
            member_ids = quests_svc.active_member_ids(conn, guild_id)
            return settings, member_ids

    settings, member_ids = await run_query(_read)

    # Booster map computed off the worker thread (Discord cache access).
    boosters = {
        uid: (bool(bot) and member_is_booster(bot, guild_id, uid))
        for uid in member_ids
    }

    def _settle():
        with ctx.open_db() as conn:
            paid = quests_svc.settle_community_quest(
                conn, settings, guild_id, quest_id, boosters
            )
            return {"ok": True, "paid_count": paid}

    return await run_query(_settle)


# ── ledger audit + manual grant ───────────────────────────────────────


@router.get("/economy/ledger")
async def get_ledger(
    request: Request,
    user_id: int | None = None,
    kind: str | None = None,
    limit: int = 50,
    _: AuthenticatedUser = Depends(require_economy_manager),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    limit = min(max(limit, 1), 500)

    def _q():
        where = ["guild_id = ?"]
        params: list[object] = [guild_id]
        if user_id is not None:
            where.append("user_id = ?")
            params.append(user_id)
        if kind:
            where.append("kind = ?")
            params.append(kind)
        params.append(limit)
        with ctx.open_db() as conn:
            rows = conn.execute(
                f"""
                SELECT id, user_id, amount, kind, actor_id, meta, created_at
                FROM econ_ledger
                WHERE {" AND ".join(where)}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "user_id": str(r["user_id"]),
                "amount": int(r["amount"]),
                "kind": r["kind"],
                "actor_id": str(r["actor_id"]) if r["actor_id"] else None,
                "meta": r["meta"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    return {"entries": await run_query(_q)}


@router.post("/economy/grant")
async def grant(
    request: Request,
    body: GrantBody,
    user: AuthenticatedUser = Depends(require_economy_manager),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    # Same booster rule as /bank grant: the TARGET member's boost multiplies.
    booster = bool(bot) and member_is_booster(bot, guild_id, body.member_id)

    def _q():
        with ctx.open_db() as conn:
            settings = load_econ_settings(conn, guild_id)
            if not settings.enabled:
                # Mirror /bank grant: no crediting while the economy is off.
                raise HTTPException(409, "economy disabled")
            credited = apply_credit(
                conn,
                guild_id,
                body.member_id,
                body.amount,
                "grant",
                actor_id=user.user_id,
                meta={"reason": body.reason, "granted_by": str(user.user_id)},
                booster=booster,
                multiplier=settings.booster_multiplier,
            )
            return {"ok": True, "credited": credited}

    return await run_query(_q)


# ── statistics ────────────────────────────────────────────────────────


@router.get("/economy/stats")
async def economy_stats(
    request: Request,
    limit: int = 100,
    _: AuthenticatedUser = Depends(require_economy_manager),
):
    """Tuning-grade snapshot: supply/inequality, distribution, 7d flow, a
    per-member earning table, engagement, top transfers, and affordability.
    ``limit`` caps the member table (default 100, hard cap 500)."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    member_limit = min(max(limit, 1), 500)
    now = time.time()

    def _q():
        with ctx.open_db() as conn:
            settings = load_econ_settings(conn, guild_id)
            return compute_stats(
                conn, settings, guild_id, now=now, member_limit=member_limit
            )

    return await run_query(_q)


# ── perk rentals ──────────────────────────────────────────────────────


@router.get("/economy/rentals")
async def list_rentals(
    request: Request,
    _: AuthenticatedUser = Depends(require_economy_manager),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            # Default states = the live ones (active + grace).
            rows = rentals_svc.list_rentals(conn, guild_id)
            return [_rental_dict(r) for r in rows]

    return {"rentals": await run_query(_q)}


@router.post("/economy/rentals/{rental_id}/cancel")
async def cancel_rental(
    request: Request,
    rental_id: int,
    user: AuthenticatedUser = Depends(require_economy_manager),
):
    """Manager force-cancel: an active rental runs to the paid week's end
    (``cancel_at_period_end``), a grace rental is cancelled immediately.
    ``cancel_rental`` raises ValueError for a missing or already-terminal
    rental → 409 (per the rentals-service contract).

    A grace cancel lands the rental in ``cancelled`` at once. The billing loop
    only walks live (active/grace) rentals, so nothing else would ever
    de-project the beneficiary's personal role — we reconcile here, best-effort
    and post-commit (mirrors the claim approve/deny bot work): guarded on a
    ready bot, all Discord failures swallowed+logged, ``role_updated`` reports
    whether the de-projection ran."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)

    def _q():
        with ctx.open_db() as conn:
            try:
                row = rentals_svc.cancel_rental(
                    conn,
                    guild_id,
                    rental_id,
                    requester_id=user.user_id,
                    force=True,
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            return _rental_dict(row)

    result = await run_query(_q)

    role_updated = False
    if result["state"] == "cancelled" and bot is not None and bot.is_ready():
        try:
            await revoke_role_perks(
                bot, ctx.db_path, guild_id, int(result["beneficiary_id"])
            )
            role_updated = True
        except Exception:  # noqa: BLE001 — Discord cleanup must never fail the API
            _log.warning(
                "rental cancel role de-projection failed", exc_info=True
            )
    result["role_updated"] = role_updated
    return result
