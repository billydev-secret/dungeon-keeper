"""One way to make sure a feature's own Discord role exists.

Five features grew their own copy of "look up the configured role, create it if
it's gone" — Jail, Inactive, DM modes, Survivor and the perk shop — and the five
copies disagreed on every detail that matters: whether an existing role of the
right name is adopted instead of twinned (Survivor and DM modes did, Jail and
Inactive didn't), whether ``Forbidden`` is reported or swallowed, whether
``HTTPException`` is caught at all (``ensure_dm_roles`` didn't, so a rate limit
escaped into a member's button click), and whether the created role starts with
``Permissions.none()`` or inherits ``@everyone``'s. This module is the single
copy; see ``docs/plans/role-autocreate.md`` for the audit behind it.

**Scope — read this before pointing it at a new feature.** It provisions
*singleton, feature-owned roles with a fixed name the bot chooses*: @Jailed,
@Inactive, the DM-mode trio, Survivor's three. It is deliberately **not** used
for the perk shop's personal roles, and must never be: those are per-member and
their name is **member-chosen**, so the adopt-by-name step would let a member
with the rename perk point their personal role at the guild's real @Moderator —
which ``perk_actions._reconcile_role`` would then rename, recolour and grant
them. A role whose name comes from a member is not a feature role.

Nor is it for the ~27 role dials that name a role the *guild* owns — the
membership role Pen Pals opts in against, the roles a role menu hands out, the
roles an intake step watches for. Creating those makes a twin and the feature
silently stops matching the real one.

Resolution order, given the id the feature has stored:

1. It resolves to a live role → **use** it. No API call, no write.
2. It doesn't, but a role named exactly ``spec.name`` exists → **adopt** it and
   store its id. This is what stops a fresh install from twinning a role the
   guild already has. Exact match only: ``@jailed`` is not ``@Jailed``, and
   grabbing the wrong role is worse than making a new one.
3. Nothing stored and no name match → **create**, silently.
4. Something *was* stored and now resolves to nothing → the admin deleted the
   role. Still create, but this is a **recreate**: the new role is empty, every
   member who held the old one is gone, and that deserves to be said out loud
   (``announce``).

Never raises into a caller. A missing **Manage Roles** or a Discord hiccup
returns ``None``; every call site here is reached from a member interaction or a
background loop where an exception would be worse than a degraded feature.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal, Sequence

import discord

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext

log = logging.getLogger("dungeonkeeper.core.role_provision")

#: What :func:`choose_role_action` decided to do.
#:
#: ``create`` and ``recreate`` both end in ``guild.create_role``; they are
#: separate outcomes because only one of them means an admin deleted something.
RoleAction = Literal["use", "adopt", "create", "recreate", "skip"]


@dataclass(frozen=True)
class RoleSpec:
    """What the feature's role should look like when the bot has to make it.

    ``permissions`` defaults to none. A feature role earns its permissions from
    channel overwrites the feature sets explicitly, never from the role itself —
    two of the five copies this module replaces inherited ``@everyone``'s
    permissions by omission.
    """

    name: str
    reason: str
    permissions: discord.Permissions = field(
        default_factory=discord.Permissions.none
    )
    colour: discord.Colour | None = None
    hoist: bool = False
    mentionable: bool = False


# ── the decision, with no Discord in it ──────────────────────────────


def choose_role_action(
    stored_id: int,
    stored_resolves: bool,
    named_role_ids: Sequence[int],
    *,
    opted_out: bool = False,
    stored_is_own: bool = True,
) -> tuple[RoleAction, int | None]:
    """Decide what to do about a feature role, without touching Discord.

    ``stored_resolves`` is whether ``stored_id`` still names a live role — the
    caller answers that with ``guild.get_role``, a cache hit, rather than
    handing over every id in the guild.

    ``stored_is_own`` says the id came from *this guild's* row rather than the
    legacy ``guild_id=0`` fallback. It only changes create vs recreate, and it
    matters: an inherited id names a role in a **different** guild, so it never
    resolves here — reading that as "an admin deleted it" would announce a
    deletion that never happened, in every guild inheriting the row.

    ``opted_out`` says an admin deliberately chose "no role here", which beats
    every other consideration — see :func:`role_dial_opted_out` for how that is
    told apart from a dial nobody has ever touched. Ping dials offer "(none)"
    on the dashboard and features read it as "don't ping"; provisioning a role
    over that would delete a working preference and put a mention nobody holds
    into every post.

    ``named_role_ids`` is the ids of roles whose name matches the spec exactly,
    **in guild order** (``guild.roles`` runs lowest position first) — so when a
    guild somehow holds two roles of the same name, the lowest one wins and
    keeps winning. An arbitrary pick would adopt a different role run to run.

    Returns the action and, for ``use``/``adopt``, the id to use.
    """
    if opted_out:
        return ("skip", None)
    if stored_id and stored_resolves:
        return ("use", stored_id)
    if named_role_ids:
        return ("adopt", named_role_ids[0])
    # Nothing to point at. Whether this is a first run or a deletion is the
    # whole difference between silence and telling the mods — and only this
    # guild's own stored id can have been deleted out from under us.
    return ("recreate", None) if (stored_id and stored_is_own) else ("create", None)


def recreate_notice(spec_name: str, feature: str) -> str:
    """The mod-log line for a role that had to be remade after a deletion."""
    return (
        f"⚠️ **{spec_name}** was deleted, so I made a new one for {feature}. "
        f"Anyone who held the old role no longer has it."
    )


# ── the effectful part ───────────────────────────────────────────────


async def _resolve(value: Any) -> Any:
    """Await ``value`` if the caller handed us a coroutine, else pass it back.

    Lets a call site read its id straight from memory (Survivor collects three
    into a dict) or hop to a thread for a sqlite read (Jail, Inactive) without
    this module deciding which is right.
    """
    if inspect.isawaitable(value):
        return await value
    return value


async def ensure_feature_role(
    guild: discord.Guild,
    spec: RoleSpec,
    *,
    load: Callable[[], int | Awaitable[int]],
    store: Callable[[int], Any],
    announce: Callable[[str], Awaitable[None]] | None = None,
    on_create: Callable[[discord.Role], Awaitable[None]] | None = None,
    opted_out: bool = False,
    stored_is_own: bool = True,
    feature: str = "",
) -> discord.Role | None:
    """Return the feature's role, adopting or creating it if need be.

    ``load``/``store`` read and persist the id wherever this feature keeps it —
    the ``config`` KV, its own table, a dict the caller writes out later. Either
    may be async, and ``store``'s return value is ignored (``set_config_value``
    hands back the stored string). ``store`` is called only when the id changes.

    ``announce`` is awaited with one line of prose when a role had to be
    **recreated** after an admin deleted it; :func:`mod_log_announcer` builds
    the usual one. Its failure is never the caller's problem.

    ``on_create`` is awaited with the new role after a create *or* recreate, and
    never after a use or adopt — it is where first-time setup goes, chiefly the
    channel overwrites Jail and Inactive lay down. That distinction is the point
    of the hook: an adopted role is one the guild already configured, and
    re-running a deny-view-everywhere sweep over it would be a destructive
    surprise. This module never touches channel permissions itself; overwrites
    are the feature's business and are set per channel, explicitly, because
    category grants do not cascade.

    ``opted_out`` short-circuits everything: an admin said "no role here", so
    nothing is created, adopted or written and ``None`` comes straight back.

    ``None`` means the role could not be provisioned (no **Manage Roles**,
    Discord refused, or the dial is opted out) — the caller degrades, it does
    not crash.
    """
    if opted_out:
        return None
    stored_id = int(await _resolve(load()) or 0)
    stored_role = guild.get_role(stored_id) if stored_id else None
    named = [r.id for r in guild.roles if r.name == spec.name]

    action, role_id = choose_role_action(
        stored_id, stored_role is not None, named,
        opted_out=opted_out, stored_is_own=stored_is_own,
    )

    if action == "use":
        return stored_role

    if action == "adopt":
        role = guild.get_role(int(role_id or 0))
        if role is not None:
            await _resolve(store(role.id))
            log.info(
                "role_provision: adopted existing @%s (%s) for %s in guild %s",
                role.name, role.id, feature or spec.name, guild.id,
            )
        return role

    kwargs: dict[str, Any] = {
        "name": spec.name,
        "reason": spec.reason,
        "permissions": spec.permissions,
        "hoist": spec.hoist,
        "mentionable": spec.mentionable,
    }
    if spec.colour is not None:
        kwargs["colour"] = spec.colour
    try:
        role = await guild.create_role(**kwargs)
    except discord.Forbidden:
        log.warning(
            "role_provision: missing Manage Roles creating @%s in guild %s",
            spec.name, guild.id,
        )
        return None
    except discord.HTTPException:
        # Transient (5xx, rate limit). Bail cleanly — the caller reports a
        # failure and the next pass tries again.
        log.warning(
            "role_provision: Discord refused @%s in guild %s",
            spec.name, guild.id, exc_info=True,
        )
        return None

    await _resolve(store(role.id))

    if on_create is not None:
        await on_create(role)

    if action == "recreate" and announce is not None:
        try:
            await announce(recreate_notice(spec.name, feature or "this feature"))
        except Exception:  # noqa: BLE001 — telling the mods is best-effort
            log.warning(
                "role_provision: could not announce @%s recreate in guild %s",
                spec.name, guild.id, exc_info=True,
            )
    return role


def role_dial_opted_out(
    conn: Any,
    key: str,
    guild_id: int,
    *,
    allow_legacy_fallback: bool = True,
) -> bool:
    """Whether an admin explicitly chose "no role" for a ``config`` dial.

    The distinction this draws is the whole reason ping roles can be
    provisioned at all. A dial nobody has touched has **no row**; picking
    "(none)" on the dashboard writes a row holding ``"0"``. Same effective
    value to the feature, opposite meanings to us: one is "set this up for me",
    the other is "I said no pings". Overriding the second would both delete a
    working preference and start mentioning a role nobody holds.

    Mirrors ``get_config_value``'s lookup, legacy fallback included, so a guild
    that inherits a home-guild "(none)" is read as opted out rather than
    unconfigured.
    """
    row = conn.execute(
        "SELECT value FROM config WHERE guild_id = ? AND key = ?", (guild_id, key)
    ).fetchone()
    if row is None and guild_id != 0 and allow_legacy_fallback:
        row = conn.execute(
            "SELECT value FROM config WHERE guild_id = ? AND key = ?", (0, key)
        ).fetchone()
    if row is None:
        return False  # never configured — ours to provision
    return str(row["value"]).strip() in ("", "0", "-1")


async def ensure_config_role(
    ctx: "AppContext",
    guild: discord.Guild,
    key: str,
    spec: RoleSpec,
    *,
    feature: str = "",
    allow_legacy_fallback: bool = True,
    respect_opt_out: bool = True,
) -> discord.Role | None:
    """:func:`ensure_feature_role` for a role dial living in the ``config`` KV.

    Reads the stored id and the opted-out question in one thread hop, then
    provisions. Returns ``None`` when the admin opted out, so a caller can
    write ``role = await ensure_config_role(...)`` / ``if role is not None:``
    and keep exactly the "unset means don't" behaviour it had before.

    ``respect_opt_out=False`` provisions even over a stored ``0``. Only for a
    dial where 0 cannot mean "off" — the dashboard panels save as whole forms
    and write ``"0"`` for any untouched picker, so on those dials a 0 records a
    save, not a decision. ``feature_roles.none_means_off`` carries the per-dial
    answer and the reasoning; don't set it from a call site.
    """
    import asyncio

    from bot_modules.core.db_utils import get_config_value

    def _read() -> tuple[int, bool, bool]:
        with ctx.open_db() as conn:
            opted_out = respect_opt_out and role_dial_opted_out(
                conn, key, guild.id, allow_legacy_fallback=allow_legacy_fallback
            )
            own = conn.execute(
                "SELECT 1 FROM config WHERE guild_id = ? AND key = ?",
                (guild.id, key),
            ).fetchone()
            raw = get_config_value(
                conn, key, "0", guild.id,
                allow_legacy_fallback=allow_legacy_fallback,
            )
        try:
            return int(raw or "0"), opted_out, own is not None
        except ValueError:
            return 0, opted_out, own is not None

    stored_id, opted_out, stored_is_own = await asyncio.to_thread(_read)
    return await ensure_feature_role(
        guild,
        spec,
        load=lambda: stored_id,
        store=lambda rid: ctx.set_config_value(key, str(rid), guild.id),
        announce=mod_log_announcer(ctx, guild),
        opted_out=opted_out,
        stored_is_own=stored_is_own,
        feature=feature or spec.name,
    )


def mod_log_announcer(
    ctx: "AppContext", guild: discord.Guild, *, action: str = "feature_role_recreated"
) -> Callable[[str], Awaitable[None]]:
    """An ``announce`` that posts to the mod channel **and** writes an audit row.

    Both, not either: ``log.txt`` is wiped every boot, so without the durable
    row the mod-log message is the only record a role was ever remade — and a
    guild with no ``mod_channel_id`` would have no record at all.
    """

    async def _announce(message: str) -> None:
        import asyncio

        from bot_modules.services.moderation import write_audit

        def _audit() -> None:
            with ctx.open_db() as conn:
                write_audit(
                    conn,
                    guild_id=guild.id,
                    action=action,
                    # Off-gateway, guild.me is None (and a fake may lack it
                    # entirely); the row still wants writing.
                    actor_id=getattr(getattr(guild, "me", None), "id", 0) or 0,
                    extra={"message": message},
                )
                conn.commit()

        await asyncio.to_thread(_audit)

        channel_id = ctx.guild_config(guild.id).mod_channel_id
        if channel_id <= 0:
            return
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            await channel.send(message)
        except discord.HTTPException:
            pass

    return _announce
