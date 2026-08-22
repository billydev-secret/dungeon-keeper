"""Post a channel panel from the dashboard.

One route replacing six near-identical slash commands whose whole job was "put
this panel in that channel" (``/bank post-guide``, ``/bank post-leaderboard``,
``/bank post-shop``, ``/voice-admin post-panel``, ``/guess prompt``,
``/ticket panel``, all removed 2026-07-28). ``bot_modules.services.panel_registry``
holds the table; this module owns only the Discord plumbing each of those
commands used to repeat: resolve the guild, resolve the channel, check the bot
can actually post there, call the cog, report what happened.

Still one route after the Channel Panels page was split up and its controls
moved onto each feature's own config page — see the registry's module docstring
for why the posting path didn't split with the UI.

Follows the bridge already used by ``/config/dms/post-panel`` — look the cog up
on the live bot and call a public method — rather than reaching into cog
internals from a route.
"""

from __future__ import annotations

import discord
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from bot_modules.services.panel_registry import (
    PanelSpec,
    get_panel_spec,
    list_panel_specs,
)
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms
from web_server.routes.panel_posting import own_channel_id, sticky_conflict

router = APIRouter()

_ADMIN = Depends(require_perms({"admin"}))


class PostPanelRequest(BaseModel):
    #: Ignored by panels that own their destination (Voice Control, Guess Who),
    #: which post into the channel configured on their own settings page.
    channel_id: str | None = None
    #: Values for the options a spec declares. Anything not declared is dropped
    #: rather than forwarded, so the request body can't reach a keyword the
    #: panel method never meant to expose.
    options: dict[str, str] | None = None


@router.get("/panels")
async def list_panels(request: Request, _: AuthenticatedUser = _ADMIN):
    """The postable panels, for the dashboard to render.

    Static data — no guild or bot needed, so this still answers while the bot
    is offline and the page can explain itself rather than showing an error.
    """
    return {
        "panels": [
            {
                "key": spec.key,
                "label": spec.label,
                "description": spec.description,
                "targets_own_channel": spec.method in _OWN_CHANNEL_METHODS,
                "options": _describe_options(request, spec),
            }
            for spec in list_panel_specs()
        ]
    }


def _describe_options(request: Request, spec: PanelSpec) -> list[dict]:
    """Render a spec's options for the dashboard, resolving any live choices.

    ``grant_role`` choices come from per-guild config rather than the registry,
    since which roles a server hands out is that server's business. Read from
    the database, so the list is right even with the bot offline.
    """
    out: list[dict] = []
    for opt in spec.options:
        row = {
            "name": opt.name,
            "label": opt.label,
            "kind": opt.kind,
            "default": opt.default,
            "hint": opt.hint,
            "minimum": opt.minimum,
        }
        if opt.kind == "grant_role":
            ctx = get_ctx(request)
            grants = ctx.guild_config(get_active_guild_id(request)).grant_roles
            row["choices"] = [
                {"value": key, "label": cfg.get("label") or key}
                for key, cfg in sorted(grants.items())
                if cfg.get("role_id", 0) > 0
            ]
        out.append(row)
    return out


def _coerce_options(spec: PanelSpec, supplied: dict[str, str] | None) -> dict:
    """Build the keyword arguments for a panel method from a request body.

    Only declared options are read — an undeclared key is ignored rather than
    forwarded, so a crafted body can't reach a keyword the method didn't intend
    to expose. Missing values fall back to the declared default.
    """
    values = supplied or {}
    kwargs: dict = {}
    for opt in spec.options:
        raw = values.get(opt.name)
        if raw is None or raw == "":
            kwargs[opt.name] = opt.default
            continue
        if opt.kind == "int":
            try:
                number = int(raw)
            except (TypeError, ValueError):
                raise HTTPException(400, f"{opt.label} must be a whole number")
            if opt.minimum is not None and number < opt.minimum:
                raise HTTPException(
                    400, f"{opt.label} must be at least {opt.minimum}"
                )
            kwargs[opt.name] = number
        else:
            kwargs[opt.name] = str(raw)
    return kwargs


# Panels that post into a channel from their own config rather than one the
# caller picks. Passing a channel to these is accepted and ignored, so the UI
# can hide the picker without the route depending on it having done so.
_OWN_CHANNEL_METHODS = {"post_control_panel", "post_prompt_panel"}


@router.post("/panels/{key}/post")
async def post_panel(
    key: str,
    body: PostPanelRequest,
    request: Request,
    _: AuthenticatedUser = _ADMIN,
):
    spec = get_panel_spec(key)
    if spec is None:
        raise HTTPException(404, f"Unknown panel: {key}")

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    bot = getattr(ctx, "bot", None)
    if bot is None:
        raise HTTPException(503, "Bot not available")
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise HTTPException(503, "Discord guild not available")
    cog = bot.get_cog(spec.cog)
    if cog is None:
        raise HTTPException(503, f"{spec.cog} not loaded")
    post = getattr(cog, spec.method, None)
    if post is None:
        raise HTTPException(503, f"{spec.cog}.{spec.method} is unavailable")

    channel = None
    if spec.method not in _OWN_CHANNEL_METHODS:
        # "0" is the picker's *unset* sentinel (mountChannelPicker's
        # emptyValue), and it arrives as a truthy string — so an admin who
        # typed into the filter without tapping a row used to fall through to
        # get_channel(0) and be told "Channel must be a text channel in this
        # guild", which sends them hunting for a channel problem that isn't
        # there. Name the real one.
        if not body.channel_id or str(body.channel_id).strip() in ("", "0"):
            raise HTTPException(400, "Pick a channel for this panel")
        try:
            channel_id = int(body.channel_id)
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid channel_id")
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise HTTPException(400, "Channel must be a text channel in this guild")
        # Fail here with something actionable rather than letting the cog return
        # a bare None the admin has to guess at.
        perms = channel.permissions_for(guild.me)
        missing = [
            label
            for flag, label in (
                (perms.view_channel, "View Channel"),
                (perms.send_messages, "Send Messages"),
                (perms.embed_links, "Embed Links"),
            )
            if not flag
        ]
        if missing:
            raise HTTPException(
                400,
                f"The bot can't post in #{channel.name} — missing permissions: "
                f"{', '.join(missing)}",
            )

    # One bottom slot per channel. Refuse the collision that cannot be lived
    # with, warn about the one that can — for every panel in the registry
    # rather than only the two features that grew their own check.
    target_id = (
        channel.id if channel else await own_channel_id(ctx, guild_id, spec.key)
    )
    warning = (
        await sticky_conflict(ctx, guild_id, target_id, excluding=spec.key)
        if target_id
        else None
    )

    try:
        message = await post(guild, channel, **_coerce_options(spec, body.options))
    except ValueError as e:
        # A panel refusing for a reason worth naming (unconfigured role, a
        # channel it can't post in) rather than a generic failure.
        raise HTTPException(400, str(e))
    except discord.HTTPException as e:
        raise HTTPException(502, f"Discord rejected the post: {e}")

    if message is None:
        # Own-channel panels return None when their own config is unset, which
        # is the common case and deserves its own wording.
        if spec.method in _OWN_CHANNEL_METHODS:
            raise HTTPException(
                400,
                f"{spec.label} has no channel configured yet — set one on its "
                "settings page first.",
            )
        raise HTTPException(502, "Discord rejected the post — panel was not posted")

    return {
        "ok": True,
        "message_url": getattr(message, "jump_url", None),
        "warning": warning,
    }
