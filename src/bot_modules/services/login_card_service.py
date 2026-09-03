"""The live-updating login digest card — store, renderer and hourly sweep.

The daily-login DM ("Daily Streak") goes out at a member's first qualifying
activity of their local day. It used to be a snapshot: a card posted at 8am
still showed 8am's quest bars at 8pm. This module keeps it current, editing
the same message in place as progress lands.

**Edits are silent.** Discord sends no notification for an edit, and that is
the design, not a limitation of it — nobody is pinged a second time, and the
card simply matches reality whenever the member opens the DM. Nothing here
ever posts a second message, so a member who wants one morning DM still gets
exactly one morning DM.

Three rules keep the cost honest, because an unconditional hourly rewrite of
every card would be thousands of requests a day, most of them re-sending an
identical embed:

* **Signature skip** — the rendered card is hashed; an unchanged card costs
  zero API calls (the same trick as :mod:`bot_modules.core.sticky`).
* **Stop when finished** — once every personal quest is done, that render is
  the last one and the row is marked ``final``. Community goals keep moving
  from other people's activity, but they freeze on the card at that point;
  the card is the member's own checklist, not a server dashboard.
* **Stop at the day roll** — a row whose ``local_day`` is no longer today is
  dropped rather than edited, so a card never outlives the day it describes.

Only a real DM is ever recorded. A muted member, one without the opt-in game
role, and one whose DMs are shut all yield no message at all, which is why the
send path reports its surface (:class:`~bot_modules.services.economy_service.DmDelivery`)
instead of a bool — trusting a truthy return here would mean chasing messages
that were never sent. The digest is DM-or-nothing (``public_fallback=False``),
so a stored handle can never point at the public bank-channel copy; editing
that would publish the wellness section.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
from typing import TYPE_CHECKING

import discord

from bot_modules.core.db_utils import get_tz_offset_hours, open_db
from bot_modules.economy import quest_digest
from bot_modules.economy.logic import local_day_for
from bot_modules.economy.quests import previous_local_day
from bot_modules.services.economy_quests_service import (
    community_gains_for_day,
    load_member_quest_board,
)
from bot_modules.services.economy_service import (
    DmDelivery,
    EconSettings,
    LoginOutcome,
    get_notify_muted,
    load_econ_settings,
)
from bot_modules.services.wellness_service import login_digest_value
from bot_modules.core.branding import apply_section_spacing

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger(__name__)

#: Quest cadences that are the member's *own* checklist. Community and monthly
#: goals are guild-wide counters that never reach "done" for one person, and
#: event quests are standing payouts with no period, so neither can decide
#: when a member has finished for the day.
PERSONAL_QTYPES = ("daily", "weekly")


# ── pure helpers ──────────────────────────────────────────────────────────


def all_personal_done(quests: list[dict]) -> bool:
    """Has the member cleared every quest that is theirs to clear today?

    True for a member with no personal quests at all — there is nothing left
    that their own actions can change, so the card is already final. Their
    community bars freeze at that point, which is the same deal every finished
    member gets.
    """
    return all(
        q.get("state") == "done"
        for q in quests
        if str(q.get("qtype") or "") in PERSONAL_QTYPES
    )


def card_handle(delivery: DmDelivery) -> tuple[int, int] | None:
    """The ``(dm_channel_id, message_id)`` worth storing, or None.

    The one decision that must never be got wrong, so it lives here rather
    than as an ``if`` in the cog. Only a message the bot actually sent as a
    **DM** is a card:

    * ``dropped`` — muted, not opted in, or DMs closed: nothing was sent, and
      a stored handle would have the sweep chasing a message that never
      existed.
    * ``bank`` — the public bank-channel fallback. A deliberately different,
      wellness-free embed; editing it later with the private render would
      publish a member's wellness section to a public channel. (The digest
      passes ``public_fallback=False``, so it should never see this — but the
      rule belongs in code, not in a comment about a caller.)
    """
    if delivery.surface != "dm" or delivery.message is None:
        return None
    return delivery.message.channel.id, delivery.message.id


def card_signature(embed: discord.Embed) -> str:
    """Stable hash of everything the member can actually see on the card.

    Compared before every edit so an hour in which nothing moved costs no API
    call. Covers the description and every field, because quest bars are not
    the only thing that drifts: the wellness blurb is recomputed live too, and
    a member who pauses wellness at noon should see that section go.
    """
    parts = [embed.title or "", embed.description or ""]
    for field in embed.fields:
        parts.append(f"{field.name}\x1f{field.value}")
    return hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()[:32]


def build_login_embed(
    settings: EconSettings,
    outcome: LoginOutcome,
    prior_streak: int,
    quests_out: list[dict],
    gains: list[dict],
    accent: discord.Color,
    *,
    wellness_value: str | None = None,
) -> discord.Embed:
    """Daily digest DM: streak update + a fun little quest checklist.

    Shared by the send path (events_cog, at the member's first activity) and
    the hourly refresh below, so a card cannot drift into two renderings of
    the same state.
    """
    embed = discord.Embed(
        title=f"{settings.currency_emoji} Daily Streak",
        color=accent,
    )
    unit = settings.currency_name if outcome.paid == 1 else settings.currency_plural
    streak_line = f"Day **{outcome.streak}** checked in"
    if outcome.paid > 0:
        streak_line += f" — {settings.currency_emoji} **+{outcome.paid:,}** {unit}"
    embed.description = f"{streak_line}."
    if outcome.milestone > 0:
        unit_m = (
            settings.currency_name if outcome.milestone == 1 else settings.currency_plural
        )
        embed.add_field(
            name=f"🏆 Day {outcome.streak} Milestone!",
            value=f"Bonus {settings.currency_emoji} **{outcome.milestone:,}** {unit_m}",
            inline=False,
        )
    if outcome.grace_consumed or outcome.shield_consumed:
        # One combined callout — a 3-day gap consumes grace AND the
        # shield, and two separate "saved" fields would read as a glitch.
        if outcome.shield_consumed and outcome.grace_consumed:
            saved = (
                "Two missed days covered — the free grace day plus your "
                "🛡️ shield (now used up)"
            )
        elif outcome.shield_consumed:
            saved = "Your 🛡️ shield covered a missed day (now used up)"
        else:
            saved = "We covered a missed day"
        value = f"{saved} — your streak lives on at day **{outcome.streak}**."
        if outcome.shield_consumed and settings.price_streak_shield > 0:
            value += " Grab a fresh shield in `/bank shop`."
        embed.add_field(name="🛟 Streak Saved", value=value, inline=False)
    if outcome.reset and prior_streak >= 3:
        embed.add_field(
            name="🔁 Streak Reset",
            value=(
                f"Your **{prior_streak}**-day streak ended. Starting fresh "
                f"at day **{outcome.streak}**."
            ),
            inline=False,
        )
    # Wellness streak (opted-in members only) sits above the quest
    # sections so a long quest list can't bury it.
    if wellness_value:
        embed.add_field(name="🌿 Wellness", value=wellness_value, inline=False)
    # Every quest, grouped by cadence, plus yesterday's movers — the digest
    # formatting (aligned bars, blurbs, channel links, ≤1024-char field
    # packing) lives in quest_digest so it's unit-tested there. Finished
    # quests stay on as ticked-off lines (``include_done``) because this card
    # is edited all day: dropping them would shrink the card of whoever did
    # the most into a bare streak line.
    for name, value in quest_digest.digest_sections(
        quests_out, gains, include_done=True
    ):
        embed.add_field(name=name, value=value, inline=False)
    apply_section_spacing(embed)
    return embed


# ── the card store ────────────────────────────────────────────────────────


def record_card(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    *,
    local_day: str,
    dm_channel_id: int,
    message_id: int,
    signature: str,
    outcome: LoginOutcome,
    prior_streak: int,
    final: bool,
    now_ts: float,
) -> None:
    """Remember where today's card landed, replacing yesterday's row.

    One row per member per guild, so the table is self-pruning: tomorrow's
    digest overwrites today's handle rather than accumulating a row a day.
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO econ_login_digest_cards (
            guild_id, user_id, local_day, dm_channel_id, message_id,
            signature, updated_at, final,
            paid, streak, milestone, grace, reset, shield, prior_streak
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            guild_id, user_id, local_day, dm_channel_id, message_id,
            signature, now_ts, int(final),
            outcome.paid, outcome.streak, outcome.milestone,
            int(outcome.grace_consumed), int(outcome.reset),
            int(outcome.shield_consumed), prior_streak,
        ),
    )


def due_cards(
    conn: sqlite3.Connection, guild_id: int, local_day: str
) -> list[sqlite3.Row]:
    """Today's cards that are still worth refreshing.

    Excludes ``final`` rows (the member finished) and any row left over from a
    previous day — those are cleaned up by :func:`drop_stale_cards` rather than
    edited, so a day-old card is never rewritten with today's numbers.
    """
    return list(
        conn.execute(
            "SELECT * FROM econ_login_digest_cards "
            "WHERE guild_id = ? AND local_day = ? AND final = 0",
            (guild_id, local_day),
        )
    )


def drop_stale_cards(
    conn: sqlite3.Connection, guild_id: int, local_day: str
) -> int:
    """Forget every card that isn't today's. Returns how many were dropped."""
    cur = conn.execute(
        "DELETE FROM econ_login_digest_cards "
        "WHERE guild_id = ? AND local_day <> ?",
        (guild_id, local_day),
    )
    return cur.rowcount or 0


def mark_card(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    *,
    local_day: str,
    signature: str,
    final: bool,
    now_ts: float,
) -> None:
    """Store the signature just rendered, and whether that was the last one.

    Scoped to ``local_day`` because the sweep takes its day once and then does
    Discord I/O per member: if the guild-local day rolls mid-pass and that
    member logs in again, ``record_card`` has already replaced their row with
    a fresh card, and an unscoped UPDATE here would stamp yesterday's
    signature — and possibly ``final`` — onto today's, freezing a brand-new
    card for the whole day.
    """
    conn.execute(
        "UPDATE econ_login_digest_cards "
        "SET signature = ?, final = ?, updated_at = ? "
        "WHERE guild_id = ? AND user_id = ? AND local_day = ?",
        (signature, int(final), now_ts, guild_id, user_id, local_day),
    )


def forget_card(
    conn: sqlite3.Connection, guild_id: int, user_id: int, *, local_day: str
) -> None:
    """Stop editing one card — the message is gone, or the member opted out.

    Day-scoped for the same reason as :func:`mark_card`: a 404 for yesterday's
    message must not delete the row for a card sent minutes ago.
    """
    conn.execute(
        "DELETE FROM econ_login_digest_cards "
        "WHERE guild_id = ? AND user_id = ? AND local_day = ?",
        (guild_id, user_id, local_day),
    )


# ── the hourly sweep ──────────────────────────────────────────────────────


def _outcome_from_row(row: sqlite3.Row) -> LoginOutcome:
    """Rebuild the frozen half of the card from its stored scalars.

    ``econ_logins`` keeps only ``paid``; milestone, grace, reset and shield are
    one-shot results of the day's first login and cannot be recomputed at 3pm,
    which is why they ride on the card row.
    """
    return LoginOutcome(
        paid=int(row["paid"]),
        streak=int(row["streak"]),
        milestone=int(row["milestone"]),
        grace_consumed=bool(row["grace"]),
        reset=bool(row["reset"]),
        shield_consumed=bool(row["shield"]),
    )


def render_card(
    conn: sqlite3.Connection,
    settings: EconSettings,
    row: sqlite3.Row,
    accent: discord.Color,
    local_day: str,
) -> tuple[discord.Embed, bool]:
    """Re-render one member's card from current state.

    Returns the embed and whether this is the final render. Everything but the
    seven stored scalars is read fresh: the quest board, the community movers,
    and the member's wellness blurb — so pausing wellness at noon really does
    take the section off the card at 1pm.
    """
    guild_id = int(row["guild_id"])
    user_id = int(row["user_id"])
    quests_out = load_member_quest_board(
        conn, settings, guild_id, user_id, local_day
    )
    gains = community_gains_for_day(conn, guild_id, previous_local_day(local_day))
    wellness_value = login_digest_value(conn, guild_id, user_id)
    embed = build_login_embed(
        settings,
        _outcome_from_row(row),
        int(row["prior_streak"]),
        quests_out,
        gains,
        accent,
        wellness_value=wellness_value,
    )
    return embed, all_personal_done(quests_out)


async def refresh_guild_cards(
    bot: discord.Client, db_path: Path, guild_id: int, now_ts: float
) -> int:
    """Edit every live card in one guild to match current progress.

    Returns the number of messages actually edited (an unchanged card is not
    one of them). Called once an hour from the economy tick.
    """
    from bot_modules.services.dm_branding import (
        brand_dm_embed,
        guild_display_name,
        guild_icon_url,
        resolve_dm_accent,
    )

    def _load() -> tuple[EconSettings, str, list[sqlite3.Row]] | None:
        with open_db(db_path) as conn:
            settings = load_econ_settings(conn, guild_id)
            if not settings.enabled or not settings.login_card_live_updates:
                return None
            local_day = local_day_for(now_ts, get_tz_offset_hours(conn, guild_id))
            drop_stale_cards(conn, guild_id, local_day)
            return settings, local_day, due_cards(conn, guild_id, local_day)

    loaded = await asyncio.to_thread(_load)
    if loaded is None:
        return 0
    settings, local_day, rows = loaded
    if not rows:
        return 0

    guild = bot.get_guild(guild_id)
    # The same branding the send path applies (economy_service.deliver_econ_dm):
    # the attribution footer and the DM accent, which defaults to DM_PRIMARY
    # rather than the generic embed default. Re-rendering without it would
    # strip the "on behalf of <server>" footer off the card at the first edit
    # and, in a guild with no accent configured, change its colour too.
    accent = await resolve_dm_accent(db_path, guild)
    guild_name = guild_display_name(guild)
    icon_url = guild_icon_url(guild)

    def _still_wants_it(uid: int) -> bool:
        """Do the two member-level gates that sent this card still hold?

        The sweep is a second path writing to the same message, so it has to
        honour the same preferences the send did — a member who mutes economy
        DMs at noon, or who loses the opt-in role, has said what they want,
        and "the edit is silent anyway" is not the member's call to make.
        """
        with open_db(db_path) as conn:
            if get_notify_muted(conn, guild_id, uid):
                return False
        if not settings.game_role_id:
            return False
        member = guild.get_member(uid) if guild else None
        return member is not None and any(
            r.id == settings.game_role_id for r in member.roles
        )

    edited = 0
    for row in rows:
        user_id = int(row["user_id"])

        if not await asyncio.to_thread(_still_wants_it, user_id):
            def _opted_out(uid: int = user_id):
                with open_db(db_path) as conn:
                    forget_card(conn, guild_id, uid, local_day=local_day)
            await asyncio.to_thread(_opted_out)
            continue

        def _render(r: sqlite3.Row = row):
            with open_db(db_path) as conn:
                return render_card(conn, settings, r, accent, local_day)

        try:
            embed, final = await asyncio.to_thread(_render)
        except Exception:
            log.exception(
                "Login card: could not re-render for guild %s user %s.",
                guild_id, user_id,
            )
            continue

        brand_dm_embed(
            embed, guild_name=guild_name, guild_icon_url=icon_url, color=accent
        )
        signature = card_signature(embed)
        if signature == str(row["signature"]) and not final:
            # Nothing the member can see has moved. Skip the API call, and
            # leave the row alone so the next hour compares against the same
            # signature.
            continue

        # A partial message needs no fetch and no cached DM channel: one PATCH
        # and nothing else, which is what makes an hourly sweep over every
        # member cheap enough to run.
        partial = bot.get_partial_messageable(
            int(row["dm_channel_id"]), type=discord.ChannelType.private
        ).get_partial_message(int(row["message_id"]))
        try:
            await partial.edit(embed=embed)
        except discord.NotFound:
            # The member deleted the DM (or the channel is gone). Never repost:
            # this card is silent by design, and a fresh message would notify
            # someone who just threw the old one away.
            def _forget(uid: int = user_id):
                with open_db(db_path) as conn:
                    forget_card(conn, guild_id, uid, local_day=local_day)
            await asyncio.to_thread(_forget)
            continue
        except discord.Forbidden:
            log.warning(
                "Login card: edit forbidden for guild %s user %s; forgetting it.",
                guild_id, user_id,
            )
            def _forget_forbidden(uid: int = user_id):
                with open_db(db_path) as conn:
                    forget_card(conn, guild_id, uid, local_day=local_day)
            await asyncio.to_thread(_forget_forbidden)
            continue
        except discord.HTTPException:
            # Transient — leave the row untouched and try again next hour.
            log.exception(
                "Login card: edit failed for guild %s user %s.", guild_id, user_id
            )
            continue

        edited += 1

        def _mark(uid: int = user_id, sig: str = signature, fin: bool = final):
            with open_db(db_path) as conn:
                mark_card(
                    conn, guild_id, uid, local_day=local_day,
                    signature=sig, final=fin, now_ts=now_ts,
                )
        await asyncio.to_thread(_mark)

    return edited
