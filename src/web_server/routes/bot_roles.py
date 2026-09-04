"""Bot-Managed Roles — the roster of every role Dungeon Keeper makes for itself.

The only surface that can show all sixteen together. Nine of them are made by
features on their own (``ensure_feature_role``) and until round 2 nothing
listed them at all, so a missing one was invisible until it failed. Read-only
except for three narrow writes on the ``config``-KV dials — create, adopt,
stop — each of which is deliberately smaller than it could be:

* **Create** provisions a role that was never made. It writes a key that was
  empty, so it cannot clobber a considered value, and it refuses a
  ``create_on_offer`` dial outright: creating one of those without offering it
  to members is exactly the empty-role failure that kept it out of the registry.
* **Adopt** points a dial at a role that already exists, after checking the bot
  could actually use it — no ``@everyone``, nothing integration-managed, and
  nothing above the bot's own top role where the bot has to hand it out.
* **Stop** stops *pointing* at a role. It never deletes one. Provenance would
  make a delete button safe to offer; that is not the same as wanting one
  (Billy, 2026-09-03), and a role the guild has been using is not ours to
  remove.
"""

from __future__ import annotations

import discord
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from bot_modules.core.db_utils import get_config_value
from bot_modules.core.role_provision import (
    ensure_config_role,
    role_dial_opted_out,
)
from bot_modules.services import feature_roles as fr
from bot_modules.services import role_roster_service as rrs
from bot_modules.services.moderation import write_audit
from bot_modules.services.role_provenance import (
    forget_role_provenance,
    read_role_provenance,
)
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query

router = APIRouter()

_ADMIN = Depends(require_perms({"admin"}))


class RoleKeyBody(BaseModel):
    key: str = ""


class AdoptBody(BaseModel):
    key: str = ""
    #: Snowflake as a string — a bare number above 2^53 is rounded by JS.
    role_id: str = "0"


def _guild_or_503(ctx, guild_id: int) -> discord.Guild:
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The bot isn't connected right now, so it can't read your roles.",
        )
    return guild


def _entry_or_404(key: str) -> fr.FeatureRole:
    entry = fr.BY_KEY.get(key)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not a managed role.")
    return entry


def _live(role: discord.Role) -> rrs.LiveRole:
    return rrs.LiveRole(
        id=role.id,
        name=role.name,
        position=role.position,
        managed=bool(getattr(role, "managed", False)),
        member_count=len(getattr(role, "members", []) or []),
    )


def _bot_top_position(guild: discord.Guild) -> int | None:
    me = getattr(guild, "me", None)
    top = getattr(me, "top_role", None) if me is not None else None
    position = getattr(top, "position", None)
    return position if isinstance(position, int) else None


def _stored_ids(
    ctx, guild_id: int
) -> tuple[dict[str, tuple[int, bool, bool]], dict]:
    """Every dial's stored id, whether it's this guild's own, and opted-out.

    One database pass for all sixteen. Each family is read the way the feature
    that owns it reads it — anything else and the page would report a value no
    feature ever sees.
    """
    from bot_modules.services.dm_perms_service import get_dm_mode_role_ids_with_conn
    from bot_modules.services.survivor_service import get_active_season
    from bot_modules.services.wellness_service import get_wellness_config

    out: dict[str, tuple[int, bool, bool]] = {}
    with ctx.open_db() as conn:
        for entry in fr.MANAGED_ROLES:
            if entry.source != fr.SOURCE_CONFIG:
                continue
            raw = get_config_value(
                conn, entry.key, "0", guild_id,
                allow_legacy_fallback=entry.legacy_fallback,
            )
            try:
                stored = int(raw or "0")
            except ValueError:
                stored = 0
            own = conn.execute(
                "SELECT 1 FROM config WHERE guild_id = ? AND key = ?",
                (guild_id, entry.key),
            ).fetchone() is not None
            # `honours_none`, not `none_means_off`: a create-on-offer dial's
            # panel writes "0" on every unrelated save, and offering the role
            # in onboarding is what makes it — so a 0 there is not a decision.
            # Onboarding reads it the same way; the two must agree or this page
            # says '"(none)" — I won't make one' about a role the Offer button
            # on the very next page will happily create.
            opted_out = entry.honours_none and role_dial_opted_out(
                conn, entry.key, guild_id,
                allow_legacy_fallback=entry.legacy_fallback,
            )
            out[entry.key] = (stored, own, opted_out)

        dm_ids = get_dm_mode_role_ids_with_conn(conn, guild_id)
        for mode, rid in dm_ids.items():
            out[f"dm_mode_{mode}_role_id"] = (int(rid or 0), True, False)

        season = get_active_season(conn, guild_id)
        season_cfg = (season or {}).get("config") or {}
        for entry in fr.MANAGED_ROLES:
            if entry.source == fr.SOURCE_SURVIVOR:
                out[entry.key] = (int(season_cfg.get(entry.key) or 0), True, False)

        cfg = get_wellness_config(conn, guild_id)
        out[fr.WELLNESS_ROLE.key] = (
            int(getattr(cfg, "role_id", 0) or 0), True, False,
        )
        provenance = read_role_provenance(conn, guild_id)
    return out, provenance


def _cards(ctx, guild: discord.Guild) -> list[rrs.RoleCard]:
    stored, provenance = _stored_ids(ctx, guild.id)
    top = _bot_top_position(guild)
    by_name: dict[str, list[rrs.LiveRole]] = {}
    for role in guild.roles:
        by_name.setdefault(role.name, []).append(_live(role))

    cards: list[rrs.RoleCard] = []
    for entry in fr.MANAGED_ROLES:
        stored_id, own, opted_out = stored.get(entry.key, (0, True, False))
        role = guild.get_role(stored_id) if stored_id else None
        cards.append(
            rrs.describe_role(
                entry,
                rrs.DialReading(
                    stored_id=stored_id,
                    stored_is_own=own,
                    opted_out=opted_out,
                    live_role=_live(role) if role is not None else None,
                    named_matches=tuple(by_name.get(entry.spec.name, ())),
                    bot_top_position=top,
                    provenance=provenance.get(entry.key),
                ),
            )
        )
    return cards


def _card_json(card: rrs.RoleCard) -> dict:
    return {
        "key": card.key,
        "name": card.name,
        "emoji": card.emoji,
        "feature": card.feature,
        "state": card.state,
        "headline": card.headline,
        # Snowflakes leave as strings; JS Number can't hold one.
        "role_id": str(card.role_id) if card.role_id else "",
        "current_name": card.current_name,
        "member_count": card.member_count,
        "group": "handed_out" if card.assigns else "pointed_at",
        "panel": card.panel,
        "panel_label": card.panel_label,
        "dial_label": card.dial_label,
        "origin": card.origin,
        "can_create": card.can_create,
        "can_adopt": card.can_adopt,
        "can_stop": card.can_stop,
        "notes": list(card.notes),
    }


@router.get("/bot-roles")
async def get_bot_roles(request: Request, user: AuthenticatedUser = _ADMIN):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    guild = _guild_or_503(ctx, guild_id)
    cards = await run_query(lambda: _cards(ctx, guild))
    me = guild.me
    return {
        "summary": rrs.summary_line(cards),
        "roles": [_card_json(c) for c in cards],
        "can_manage_roles": bool(me and me.guild_permissions.manage_roles),
        "bot_top_role_position": _bot_top_position(guild),
    }


@router.get("/bot-roles/state")
async def get_bot_role_state(
    request: Request, keys: str = "", user: AuthenticatedUser = _ADMIN
):
    """The same cards, filtered — what the per-dial line under a picker reads.

    A panel asks for the one or two keys it owns and gets one request's worth
    of answer. Deliberately no client-side cache: a cached roster would survive
    a guild switch and show one server's roles inside another, which is the
    failure ``resetMetaCaches`` exists to prevent.
    """
    wanted = {k.strip() for k in keys.split(",") if k.strip()}
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    guild = _guild_or_503(ctx, guild_id)
    cards = await run_query(lambda: _cards(ctx, guild))
    return {
        "roles": [
            _card_json(c) for c in cards if not wanted or c.key in wanted
        ],
        "can_manage_roles": bool(guild.me and guild.me.guild_permissions.manage_roles),
    }


def _audit(ctx, guild_id: int, user: AuthenticatedUser, action: str, extra: dict):
    def _w() -> None:
        with ctx.open_db() as conn:
            write_audit(
                conn, guild_id=guild_id, action=action,
                actor_id=int(user.user_id), extra={**extra, "via": "web"},
            )
            conn.commit()

    return _w


@router.post("/bot-roles/create")
async def create_bot_role(
    request: Request, body: RoleKeyBody, user: AuthenticatedUser = _ADMIN
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    guild = _guild_or_503(ctx, guild_id)
    entry = _entry_or_404(body.key)

    if entry.source != fr.SOURCE_CONFIG:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"{entry.panel_label or 'That feature'} makes this role itself — "
            "set it up on its own page.",
        )
    if entry.create_on_offer:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"@{entry.spec.name} is only made when you offer it to members. "
            "Add it on Discord Onboarding and it gets created there — a role "
            "nobody holds would leave this feature switched on and refusing "
            "everybody.",
        )

    role = await ensure_config_role(
        ctx, guild, entry.key, entry.spec,
        feature=entry.feature,
        allow_legacy_fallback=entry.legacy_fallback,
        # The admin is asking for it right now, so an old "(none)" is being
        # overridden deliberately rather than silently.
        respect_opt_out=False,
        assigns=entry.assigns,
    )
    if role is None:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Couldn't make the role — check the bot has Manage Roles.",
        )
    await run_query(
        _audit(ctx, guild_id, user, "bot_role_created",
               {"key": entry.key, "role_id": str(role.id)})
    )
    return {"ok": True, "role_id": str(role.id), "name": role.name}


@router.post("/bot-roles/adopt")
async def adopt_bot_role(
    request: Request, body: AdoptBody, user: AuthenticatedUser = _ADMIN
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    guild = _guild_or_503(ctx, guild_id)
    entry = _entry_or_404(body.key)
    if entry.source != fr.SOURCE_CONFIG:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"{entry.panel_label or 'That feature'} owns this role — point it "
            "somewhere else on its own page.",
        )
    try:
        role_id = int(body.role_id or "0")
    except ValueError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Malformed role id."
        ) from None
    role = guild.get_role(role_id) if role_id else None
    if role is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "That role doesn't exist on this server."
        )
    if role.id == guild.id or getattr(role, "managed", False):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "That role belongs to an integration (or is @everyone), so nobody "
            "can be given it.",
        )
    top = _bot_top_position(guild)
    if entry.assigns and top is not None and role.position >= top:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"@{role.name} sits above my own role, so I could never add or "
            "remove it. Move Dungeon Keeper above it in Server Settings → "
            "Roles first.",
        )

    def _write() -> None:
        with ctx.open_db() as conn:
            from bot_modules.core.db_utils import set_config_value
            from bot_modules.services.role_provenance import (
                record_role_provenance,
            )

            set_config_value(conn, entry.key, str(role.id), guild_id)
            record_role_provenance(conn, guild_id, entry.key, role.id, "adopted")
            write_audit(
                conn, guild_id=guild_id, action="bot_role_adopted",
                actor_id=int(user.user_id),
                extra={"key": entry.key, "role_id": str(role.id), "via": "web"},
            )
            conn.commit()

    await run_query(_write)
    return {"ok": True, "role_id": str(role.id), "name": role.name}


@router.post("/bot-roles/stop")
async def stop_managing_bot_role(
    request: Request, body: RoleKeyBody, user: AuthenticatedUser = _ADMIN
):
    """Stop pointing at the role. **Never** delete it.

    Provenance can tell a role the bot made from one it adopted, which is what
    would make a delete button safe — but safe to offer and wanted are
    different questions, and this one was answered "leave the role in place".
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    entry = _entry_or_404(body.key)
    if entry.source != fr.SOURCE_CONFIG or not entry.none_means_off:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"@{entry.spec.name} can't be switched off — {entry.feature} needs "
            "a role to work at all.",
        )

    def _write() -> None:
        with ctx.open_db() as conn:
            from bot_modules.core.db_utils import set_config_value

            # "0" is the stored form of "(none)": role_dial_opted_out reads it
            # as a decision, so nothing provisions over it afterwards.
            set_config_value(conn, entry.key, "0", guild_id)
            forget_role_provenance(conn, guild_id, entry.key)
            write_audit(
                conn, guild_id=guild_id, action="bot_role_released",
                actor_id=int(user.user_id),
                extra={"key": entry.key, "via": "web"},
            )
            conn.commit()

    await run_query(_write)
    return {"ok": True}
