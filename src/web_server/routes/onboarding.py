"""Discord onboarding endpoints — read the live config, add opt-in roles to it.

Onboarding ("Channels & Roles" in Discord) is where members actually pick roles
up, so it is where the opt-in ping roles in
:mod:`bot_modules.services.feature_roles` belong. DK already reads this config
hourly for the role-pick quest; these routes are the write half.

Two rules this surface exists to enforce, both from
``docs/plans/role-autocreate.md``:

* **Explicit only.** No loop writes onboarding. Editing it replaces the entire
  prompt list, and a human edits the same config by hand in Server Settings —
  a background sync would clobber those edits wholesale.
* **Plan, show, then write.** ``GET`` returns the live prompts and a preview of
  exactly what would change, so the admin confirms a diff rather than a promise.
  The plan is recomputed server-side on ``POST`` against a freshly read config,
  so a concurrent hand edit is caught instead of overwritten.
"""

from __future__ import annotations

import discord
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from bot_modules.core.role_provision import ensure_config_role
from bot_modules.services import feature_roles as fr
from bot_modules.services import onboarding_service as svc
from bot_modules.services.moderation import write_audit
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query

router = APIRouter()

_ADMIN = Depends(require_perms({"admin"}))


class AddRolesBody(BaseModel):
    #: Registry keys (``feature_roles.CONFIG_ROLES``) to offer.
    keys: list[str] = []
    #: Existing prompt to extend, as a string (snowflake).
    prompt_id: str = ""
    #: ...or a title for a new question. Exactly one of the two.
    new_prompt_title: str = ""


def _guild_or_503(ctx, guild_id: int) -> discord.Guild:
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The bot isn't connected right now, so it can't read onboarding.",
        )
    return guild


async def _read_prompts(guild: discord.Guild) -> tuple[svc.PromptView, ...]:
    try:
        onboarding = await guild.onboarding()
    except discord.Forbidden:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "The bot needs Manage Server to read onboarding.",
        ) from None
    except discord.HTTPException as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Discord refused: {exc}"
        ) from None
    return svc.read_prompts(onboarding)


def _prompt_json(prompt: svc.PromptView) -> dict:
    return {
        # Snowflakes go out as strings — JS Number can't hold one.
        "id": str(prompt.id),
        "title": prompt.title,
        "type": prompt.type,
        "single_select": prompt.single_select,
        "required": prompt.required,
        "in_onboarding": prompt.in_onboarding,
        "options": [
            {
                "title": o.title,
                "description": o.description,
                "emoji": o.emoji,
                "role_ids": [str(r) for r in o.role_ids],
            }
            for o in prompt.options
        ],
    }


def _role_states(ctx, guild: discord.Guild, prompts) -> list[dict]:
    """Each registry role, and whether it can be offered in onboarding.

    Four states an admin needs told apart: already in onboarding; ready to add;
    not created yet (the feature hasn't run, so we'd make it now); and switched
    off deliberately, which we never override.
    """
    from bot_modules.core.db_utils import get_config_value
    from bot_modules.core.role_provision import role_dial_opted_out

    offered = svc.offered_role_ids(prompts)
    out: list[dict] = []
    with ctx.open_db() as conn:
        for entry in fr.CONFIG_ROLES:
            # A create-on-offer dial is never "off": offering it here IS the
            # decision that makes it exist, and its panel writes a "0" on every
            # unrelated save, so reading that 0 as a preference would leave the
            # admin unable to offer a role they are explicitly asking for.
            # `honours_none` carries that rule so the roster page reads the
            # dial the same way this one does.
            opted_out = entry.honours_none and role_dial_opted_out(
                conn, entry.key, guild.id,
                allow_legacy_fallback=entry.legacy_fallback,
            )
            raw = get_config_value(
                conn, entry.key, "0", guild.id,
                allow_legacy_fallback=entry.legacy_fallback,
            )
            try:
                role_id = int(raw or "0")
            except ValueError:
                role_id = 0
            role = guild.get_role(role_id) if role_id else None
            if opted_out:
                state = "off"
            elif role is not None and role.id in offered:
                state = "offered"
            elif role is not None:
                state = "ready"
            else:
                state = "uncreated"
            out.append({
                "key": entry.key,
                "name": entry.spec.name,
                "blurb": entry.blurb,
                "emoji": entry.emoji,
                "feature": entry.feature,
                "role_id": str(role.id) if role is not None else "",
                "state": state,
                # The two dials round 2 reopened: they exist only once offered,
                # so the panel says so rather than implying the feature is
                # already usable.
                "create_on_offer": entry.create_on_offer,
            })
    return out


@router.get("/onboarding")
async def get_onboarding(request: Request, user: AuthenticatedUser = _ADMIN):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    guild = _guild_or_503(ctx, guild_id)
    prompts = await _read_prompts(guild)
    states = await run_query(lambda: _role_states(ctx, guild, prompts))
    me = guild.me
    manage_guild = bool(me and me.guild_permissions.manage_guild)
    manage_roles = bool(me and me.guild_permissions.manage_roles)
    return {
        "prompts": [_prompt_json(p) for p in prompts],
        "roles": states,
        # Surfaced rather than discovered on failure: without Manage Server the
        # write 403s, and the panel should say so before the admin composes one.
        "can_edit": manage_guild and manage_roles,
        # Reported apart so the panel can name the missing bit and the steps to
        # grant it. `/invite` deliberately does NOT ask for Manage Server
        # (Billy, 2026-09-03: keep the invite narrow), so on a fresh
        # least-privilege install this page is read-only until an admin grants
        # it by hand — a visible limitation beats a silently disabled Save.
        "can_manage_guild": manage_guild,
        "can_manage_roles": manage_roles,
        # A server that isn't a Community server has no onboarding at all, and
        # "no questions yet" is a misleading way to say so.
        "is_community": "COMMUNITY" in set(getattr(guild, "features", []) or []),
    }


@router.post("/onboarding/add-roles")
async def add_roles(
    request: Request, body: AddRolesBody, user: AuthenticatedUser = _ADMIN
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    guild = _guild_or_503(ctx, guild_id)

    by_key = {e.key: e for e in fr.CONFIG_ROLES}
    unknown = [k for k in body.keys if k not in by_key]
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Not a managed role: {unknown[0]}"
        )

    try:
        target_id = int(body.prompt_id or "0")
    except ValueError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Malformed question id."
        ) from None

    # Read FIRST, and check the destination still exists, before creating
    # anything. Provisioning up front meant a request doomed by a stale prompt
    # id still left new roles behind in the server.
    prompts = await _read_prompts(guild)
    if target_id and not any(p.id == target_id for p in prompts):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "That question no longer exists — someone changed onboarding in "
            "Server Settings. Reload and try again.",
        )

    # Provision anything not created yet, so an admin can put a role into
    # onboarding before the feature that owns it has ever run. ensure_config_role
    # still refuses a dial an admin switched off, which is what we want.
    additions: list[svc.OptionView] = []
    unavailable: list[str] = []
    for key in body.keys:
        entry = by_key[key]
        role = await ensure_config_role(
            ctx, guild, entry.key, entry.spec,
            feature=entry.feature,
            allow_legacy_fallback=entry.legacy_fallback,
            # This is the create-on-offer action: for the two dials that may
            # only be made while being offered, the admin ticking the box here
            # IS the explicit request, so an old stored "0" does not veto it.
            respect_opt_out=entry.honours_none,
            assigns=entry.assigns,
        )
        if role is None:
            unavailable.append(entry.spec.name)
            continue
        additions.append(
            svc.OptionView(
                title=entry.spec.name,
                description=entry.blurb,
                emoji=entry.emoji,
                role_ids=(role.id,),
            )
        )

    plan = svc.plan_add_options(
        prompts, additions,
        target_prompt_id=target_id or None,
        new_prompt_title=body.new_prompt_title,
    )
    if not plan.ok:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "; ".join(plan.errors))

    if not plan.changes_anything:
        return {
            "written": False,
            "added": [],
            "skipped": [list(s) for s in plan.skipped],
            "unavailable": unavailable,
            "prompts": [_prompt_json(p) for p in plan.prompts],
        }

    try:
        await guild.edit_onboarding(
            prompts=svc.to_discord_prompts(plan.prompts, discord),
            reason=f"Dungeon Keeper: opt-in roles added by {user.user_id}",
        )
    except discord.Forbidden:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "The bot needs Manage Server and Manage Roles to edit onboarding.",
        ) from None
    except discord.HTTPException as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Discord refused the change: {exc}"
        ) from None

    def _audit() -> None:
        with ctx.open_db() as conn:
            write_audit(
                conn, guild_id=guild_id, action="onboarding_roles_added",
                actor_id=int(user.user_id),
                extra={"added": list(plan.added), "via": "web"},
            )
            conn.commit()

    await run_query(_audit)
    fresh = await _read_prompts(guild)
    return {
        "written": True,
        "added": list(plan.added),
        "skipped": [list(s) for s in plan.skipped],
        "unavailable": unavailable,
        "prompts": [_prompt_json(p) for p in fresh],
    }
