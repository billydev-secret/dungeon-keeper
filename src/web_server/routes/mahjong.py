"""Meadow Mahjong dashboard API — stage 7 of docs/plans/meadow-mahjong.md.

The feature's entire admin surface (spec §8): card management with the
server-side linter reporting every problem inline, house-rule dials, stakes,
and the tables report. There is no Discord-side admin surface at all.

Auth tiers: admin for everything except ``GET /mahjong/card`` — the card
viewer is readable by any logged-in member (plan D6: the "public, read-only
study page" at the same tier as manual.html, not an anonymous route).
Snowflakes go out as strings — JS ``Number`` can't hold a Discord id.
"""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from bot_modules.core.db_utils import set_config_value
from bot_modules.games.mahjong.card_logic import lint_card_data, load_card
from bot_modules.games.mahjong.mahjong_service import (
    ASSIST_MODES,
    PAYOUT_CAP,
    TableError,
    load_settings,
    save_card,
    set_card_status,
)
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query

log = logging.getLogger("web.mahjong")

router = APIRouter()
_ADMIN = Depends(require_perms({"admin"}))
_MEMBER = Depends(require_perms(set()))


class ConfigBody(BaseModel):
    enabled: bool
    claim_window_4: float = Field(ge=3, le=60)
    claim_window_2: float = Field(ge=3, le=60)
    turn_timer: float = Field(ge=10, le=300)
    phase_timer: float = Field(ge=15, le=600)
    duel_wall_trim: int = Field(ge=0, le=100)
    second_charleston: bool
    stakes_allowed: list[int] = Field(min_length=1, max_length=8)
    assist_default: str = Field(pattern=f"^({'|'.join(ASSIST_MODES)})$")


class CardUploadBody(BaseModel):
    card: dict


class CardStatusBody(BaseModel):
    status: str = Field(pattern="^(active|scheduled|archived)$")
    activate_at: float | None = None


def _settings_payload(conn, guild_id: int) -> dict:
    s = load_settings(conn, guild_id)
    return {
        "enabled": s.enabled,
        "claim_window_4": s.claim_window_4,
        "claim_window_2": s.claim_window_2,
        "turn_timer": s.turn_timer,
        "phase_timer": s.phase_timer,
        "duel_wall_trim": s.duel_wall_trim,
        "second_charleston": s.second_charleston,
        "stakes_allowed": list(s.stakes_allowed),
        "assist_default": s.assist_default,
    }


def _cards_payload(conn, guild_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, card_id, display_name, season, status, activate_at, "
        "created_at FROM mahjong_cards WHERE guild_id = ? ORDER BY created_at",
        (guild_id,),
    ).fetchall()
    out = []
    for r in rows:
        card = load_card(json.loads(conn.execute(
            "SELECT card_json FROM mahjong_cards WHERE id = ?", (r["id"],)
        ).fetchone()["card_json"]))
        out.append({
            "row_id": r["id"],
            "card_id": r["card_id"],
            "display_name": r["display_name"],
            "season": r["season"],
            "status": r["status"],
            "activate_at": r["activate_at"],
            "created_at": r["created_at"],
            "hands": len(card.hands),
            "max_value": card.max_value,
        })
    return out


@router.get("/mahjong/config")
async def get_config(request: Request, user=_ADMIN):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            settings = _settings_payload(conn, guild_id)
            cards = _cards_payload(conn, guild_id)
            card = next((c for c in cards if c["status"] == "active"), None)
            escrow = None
            if card is not None:
                escrow = {
                    str(mode): {
                        str(stake): card["max_value"] * cap * stake
                        for stake in settings["stakes_allowed"]
                    }
                    for mode, cap in PAYOUT_CAP.items()
                }
            return {"settings": settings, "cards": cards, "escrow_preview": escrow}

    return await run_query(_q)


@router.put("/mahjong/config")
async def put_config(request: Request, body: ConfigBody, user=_ADMIN):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    stakes = sorted({s for s in body.stakes_allowed if 1 <= s <= 100})
    if not stakes:
        raise HTTPException(status_code=400, detail="At least one stake of 1–100.")

    def _q():
        with ctx.open_db() as conn:
            set_config_value(conn, "mahjong_enabled", "1" if body.enabled else "0", guild_id)
            set_config_value(conn, "mahjong_claim_window_4", str(body.claim_window_4), guild_id)
            set_config_value(conn, "mahjong_claim_window_2", str(body.claim_window_2), guild_id)
            set_config_value(conn, "mahjong_turn_timer", str(body.turn_timer), guild_id)
            set_config_value(conn, "mahjong_phase_timer", str(body.phase_timer), guild_id)
            set_config_value(conn, "mahjong_duel_wall_trim", str(body.duel_wall_trim), guild_id)
            set_config_value(
                conn, "mahjong_second_charleston",
                "1" if body.second_charleston else "0", guild_id)
            set_config_value(
                conn, "mahjong_stakes_allowed", ",".join(map(str, stakes)), guild_id)
            set_config_value(conn, "mahjong_assist_default", body.assist_default, guild_id)
            return _settings_payload(conn, guild_id)

    return await run_query(_q)


@router.post("/mahjong/cards")
async def upload_card(request: Request, body: CardUploadBody, user=_ADMIN):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    report = lint_card_data(body.card)
    if not report.ok:
        # every problem inline, not just the first (§8)
        return {"ok": False, "errors": report.errors, "warnings": report.warnings}

    def _q():
        with ctx.open_db() as conn:
            row_id = save_card(conn, guild_id, body.card, uploaded_by=int(user.user_id))
            return {"ok": True, "row_id": row_id, "warnings": report.warnings}

    return await run_query(_q)


@router.post("/mahjong/cards/{row_id}/status")
async def card_status(request: Request, row_id: int, body: CardStatusBody, user=_ADMIN):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    if body.status == "scheduled":
        if body.activate_at is None or body.activate_at <= time.time():
            raise HTTPException(status_code=400, detail="Scheduling needs a future time.")

    def _q():
        with ctx.open_db() as conn:
            try:
                set_card_status(conn, guild_id, row_id, body.status, body.activate_at)
            except TableError as e:
                raise HTTPException(status_code=404, detail=str(e)) from e
            return {"ok": True}

    return await run_query(_q)


@router.get("/mahjong/card")
async def active_card(request: Request, user=_MEMBER):
    """The card viewer: the active card by section, for out-of-Discord study
    (member tier, plan D6)."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            row = conn.execute(
                "SELECT card_json FROM mahjong_cards "
                "WHERE guild_id = ? AND status = 'active'",
                (guild_id,),
            ).fetchone()
            if row is None:
                return {"card": None}
            card = load_card(json.loads(row["card_json"]))
            sections = []
            for section in card.sections():
                sections.append({
                    "name": section,
                    "hands": [
                        {
                            "id": h.id, "name": h.name, "display": h.display,
                            "value": h.value, "concealed": h.concealed,
                            "notes": h.notes,
                        }
                        for h in card.hands if h.section == section
                    ],
                })
            return {"card": {
                "card_id": card.card_id,
                "display_name": card.display_name,
                "season": card.season,
                "sections": sections,
            }}

    return await run_query(_q)


@router.get("/mahjong/report")
async def report(request: Request, user=_ADMIN):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            tables = [
                {
                    "table_id": r["id"],
                    "channel_id": str(r["channel_id"]),
                    "mode": r["mode"],
                    "stake": r["stake"],
                    "host_id": str(r["host_id"]),
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                }
                for r in conn.execute(
                    "SELECT * FROM mahjong_tables "
                    "WHERE guild_id = ? AND status = 'live' ORDER BY created_at",
                    (guild_id,),
                )
            ]
            results = [
                {
                    "result_id": r["id"],
                    "hand_no": r["hand_no"],
                    "mode": r["mode"],
                    "stake": r["stake"],
                    "kind": r["kind"],
                    "winner_id": str(r["winner_id"]) if r["winner_id"] else None,
                    "line_id": r["line_id"],
                    "line_name": r["line_name"],
                    "base_value": r["base_value"],
                    "won_by": r["won_by"],
                    "jokerless": bool(r["jokerless"]),
                    "created_at": r["created_at"],
                }
                for r in conn.execute(
                    "SELECT * FROM mahjong_results WHERE guild_id = ? "
                    "ORDER BY created_at DESC LIMIT 50",
                    (guild_id,),
                )
            ]
            aggregates = [
                {
                    "user_id": str(r["user_id"]),
                    "mode": r["mode"],
                    "hands_played": r["hands_played"],
                    "wins": r["wins"],
                    "jokerless_wins": r["jokerless_wins"],
                    "coins_won": r["coins_won"],
                    "coins_lost": r["coins_lost"],
                    "biggest_win": r["biggest_win"],
                }
                for r in conn.execute(
                    "SELECT * FROM mahjong_stats WHERE guild_id = ? "
                    "ORDER BY coins_won DESC LIMIT 100",
                    (guild_id,),
                )
            ]
            return {"tables": tables, "results": results, "aggregates": aggregates}

    return await run_query(_q)
