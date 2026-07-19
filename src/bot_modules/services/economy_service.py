"""Economy service — DB layer for wallets, the ledger, and per-guild settings.

Soft-currency balances, a signed audit ledger, and balance-change DM mute
prefs, plus the per-guild ``econ_`` settings stored in the shared config KV
table. See docs/economy_spec.md for the feature design.
"""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import time
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING

from bot_modules.economy import live_signal, logic

if TYPE_CHECKING:
    from pathlib import Path

    import discord

ECON_PREFIX = "econ_"


@dataclass(frozen=True)
class EconSettings:
    enabled: bool = False
    bank_channel_id: int = 0
    manager_role_id: int = 0
    # Opt-in "economy game" role. When set, auto-claimed quest completions
    # (trigger-word / photo-reply / media-post) DM the claimant their card
    # instead of replying in-channel; members without the role are paid
    # silently. 0 (default) = feature off: everyone gets the legacy
    # in-channel reaction + reply.
    game_role_id: int = 0
    # Role pinged when a mod posts a question of the day. 0 (default) = no
    # ping, preserving the original silent post. The role must be mentionable
    # (or the bot must hold "Mention @everyone, @here, and All Roles"), else
    # Discord renders the mention as inert text.
    qotd_ping_role_id: int = 0
    currency_name: str = "Coin"
    currency_plural: str = "Coins"
    currency_emoji: str = "🪙"
    currency_icon_url: str = ""
    wallet_name: str = "Wallet"
    transfers_enabled: bool = True
    booster_multiplier: float = 1.5
    xp_per_coin: float = 15.0
    login_text_base: int = 5
    login_voice_base: int = 15
    streak_bonus_cap: int = 10
    milestone_day7: int = 25
    milestone_day30: int = 100
    milestone_day100: int = 365
    milestone_per_100: int = 100
    reward_qotd: int = 10
    reward_game_participation: int = 5
    reward_game_win: int = 20
    # How many quests of each cadence a member is shown (and can be paid for)
    # per period — their "personal board", drawn from that cadence's active
    # pool. Tuning these down is how a guild makes the board feel smaller
    # without deactivating library quests; 0 turns the cadence off entirely
    # (nothing shows, nothing pays). Capped at POOL_CAP by the dashboard.
    quest_board_daily: int = 2
    quest_board_weekly: int = 2
    quest_board_monthly: int = 2
    # Community-weekly beat sheets (kickoff / tier crossed / final-24h /
    # resolution) DM this member so they can host the event in their own
    # voice — the bot posts nothing publicly (2026-07-18 decision). 0 =
    # fall back to the guild owner.
    community_host_user_id: int = 0
    # Clear-the-board set bonuses: paid once per period when a member
    # completes EVERY quest on their personal board of that cadence
    # (ledger kind quest_bonus, no booster multiplier). Default OFF — a
    # silent default-on bonus surprises small boards (a 1-quest pool pays
    # it on every claim); guilds opt in on the Settings page (the main
    # guild is seeded 10/25 by scripts/seed_quest_variety.py).
    quest_set_bonus_daily: int = 0
    quest_set_bonus_weekly: int = 0
    price_role_color: int = 50
    price_role_name: int = 35
    price_role_icon: int = 75
    price_role_gradient: int = 120
    price_text_room: int = 200
    price_voice_room: int = 200
    price_gift_color: int = 50
    # Bot-managed bookkeeping for the channel how-to panel (/bank post-guide);
    # readable via GET /economy/config but deliberately absent from the
    # dashboard's editable-field whitelist.
    guide_channel_id: int = 0
    guide_message_id: int = 0
    # Same pattern for the auto-updating leaderboard panel
    # (/bank post-leaderboard, refreshed hourly by the economy loop).
    leaderboard_channel_id: int = 0
    leaderboard_message_id: int = 0
    # Same pattern for the persistent perk-shop panel (/bank post-shop;
    # buttons are DynamicItems so they survive restarts).
    shop_channel_id: int = 0
    shop_message_id: int = 0
    # Public transaction feed (see economy/register.py). Unset (0) = off; the
    # channel picker IS the toggle. Every econ_ledger row for the guild is
    # posted here as it lands, saying what it was for.
    register_channel_id: int = 0
    # Bot-managed drain cursor: the highest econ_ledger.id already posted to
    # the register. Bookkeeping like the *_message_id fields, so it is
    # deliberately absent from the dashboard's editable whitelist. Seeded to
    # the ledger's current MAX(id) on first drain so enabling the feed never
    # backfills the guild's entire history.
    #
    # -1 (not 0) is the "never seeded" sentinel: 0 is a legitimate seeded
    # cursor for a guild whose ledger is still empty, and conflating the two
    # would re-seed past that guild's first-ever transaction and swallow it.
    register_cursor_id: int = -1


DEFAULT_ECON_SETTINGS = EconSettings()

_BOOL_KEYS = ["enabled", "transfers_enabled"]
_FLOAT_KEYS = ["booster_multiplier", "xp_per_coin"]
_STR_KEYS = [
    "currency_name",
    "currency_plural",
    "currency_emoji",
    "currency_icon_url",
    "wallet_name",
]
# Everything else on the dataclass is a plain int.
_INT_KEYS = [
    f.name
    for f in fields(EconSettings)
    if f.name not in _BOOL_KEYS and f.name not in _FLOAT_KEYS and f.name not in _STR_KEYS
]

_ALL_KEYS = frozenset(f.name for f in fields(EconSettings))


def load_econ_settings(conn: sqlite3.Connection, guild_id: int) -> EconSettings:
    """Build an EconSettings from stored ``econ_`` config values.

    Guild-scoped only — ``allow_legacy_fallback=False`` so an unconfigured
    guild gets real defaults instead of inheriting the legacy guild_id=0 rows.
    """
    from bot_modules.core.db_utils import get_config_value, parse_bool

    defaults = DEFAULT_ECON_SETTINGS
    kwargs: dict[str, object] = {}

    for key in _BOOL_KEYS:
        raw = get_config_value(
            conn, f"{ECON_PREFIX}{key}", "", guild_id, allow_legacy_fallback=False
        )
        if raw:
            kwargs[key] = parse_bool(raw, getattr(defaults, key))

    for key in _INT_KEYS:
        raw = get_config_value(
            conn, f"{ECON_PREFIX}{key}", "", guild_id, allow_legacy_fallback=False
        )
        if raw:
            try:
                kwargs[key] = int(raw)
            except ValueError:
                pass

    for key in _FLOAT_KEYS:
        raw = get_config_value(
            conn, f"{ECON_PREFIX}{key}", "", guild_id, allow_legacy_fallback=False
        )
        if raw:
            try:
                kwargs[key] = float(raw)
            except ValueError:
                pass

    for key in _STR_KEYS:
        raw = get_config_value(
            conn, f"{ECON_PREFIX}{key}", "", guild_id, allow_legacy_fallback=False
        )
        if raw:
            kwargs[key] = raw

    if not kwargs:
        return defaults
    for f in defaults.__dataclass_fields__:
        if f not in kwargs:
            kwargs[f] = getattr(defaults, f)
    return EconSettings(**kwargs)  # type: ignore[arg-type]


def save_econ_settings(
    conn: sqlite3.Connection, guild_id: int, values: dict[str, object]
) -> None:
    """Persist a partial dict of settings under the ``econ_`` prefix.

    Every key must name an EconSettings field; an unknown key raises KeyError
    so callers can't silently write dead config. Booleans persist as "1"/"0".
    """
    from bot_modules.core.db_utils import set_config_value

    unknown = set(values) - _ALL_KEYS
    if unknown:
        raise KeyError(f"unknown econ setting(s): {sorted(unknown)}")

    for key, value in values.items():
        if isinstance(value, bool):
            stored = "1" if value else "0"
        else:
            stored = str(value)
        set_config_value(conn, f"{ECON_PREFIX}{key}", stored, guild_id)


def get_balance(conn: sqlite3.Connection, guild_id: int, user_id: int) -> int:
    row = conn.execute(
        "SELECT balance FROM econ_wallets WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    return int(row["balance"]) if row else 0


def apply_credit(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    amount: int,
    kind: str,
    *,
    actor_id: int | None = None,
    meta: dict | None = None,
    booster: bool = False,
    multiplier: float = 1.5,
) -> int:
    """Credit a wallet and record the ledger row as one atomic unit.

    Returns the credited amount: ``ceil(amount * multiplier)`` when ``booster``
    is set, else ``amount``. Raises ValueError for ``amount < 1``. Rides the
    passed connection — the caller's transaction is the commit boundary.
    """
    if amount < 1:
        raise ValueError("credit amount must be >= 1")
    credited = math.ceil(amount * multiplier) if booster else amount
    now = time.time()
    conn.execute(
        """
        INSERT INTO econ_wallets (guild_id, user_id, balance, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            balance    = balance + excluded.balance,
            updated_at = excluded.updated_at
        """,
        (guild_id, user_id, credited, now, now),
    )
    conn.execute(
        """
        INSERT INTO econ_ledger
            (guild_id, user_id, amount, kind, actor_id, meta, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            guild_id,
            user_id,
            credited,
            kind,
            actor_id,
            json.dumps(meta) if meta is not None else None,
            now,
        ),
    )
    live_signal.mark_dirty(guild_id)
    return credited


def apply_debit(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    amount: int,
    kind: str,
    *,
    actor_id: int | None = None,
    meta: dict | None = None,
) -> bool:
    """Debit a wallet and record the ledger row as one atomic unit.

    Returns False with no writes when the balance is below ``amount`` (or the
    wallet doesn't exist); balances never go negative. Raises ValueError for
    ``amount < 1``.
    """
    if amount < 1:
        raise ValueError("debit amount must be >= 1")
    now = time.time()
    cur = conn.execute(
        """
        UPDATE econ_wallets
        SET balance = balance - ?, updated_at = ?
        WHERE guild_id = ? AND user_id = ? AND balance >= ?
        """,
        (amount, now, guild_id, user_id, amount),
    )
    if (cur.rowcount or 0) == 0:
        return False
    conn.execute(
        """
        INSERT INTO econ_ledger
            (guild_id, user_id, amount, kind, actor_id, meta, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            guild_id,
            user_id,
            -amount,
            kind,
            actor_id,
            json.dumps(meta) if meta is not None else None,
            now,
        ),
    )
    return True


def transfer_currency(
    conn: sqlite3.Connection,
    guild_id: int,
    from_id: int,
    to_id: int,
    amount: int,
    *,
    memo: str | None = None,
) -> None:
    """Move ``amount`` between two wallets as one atomic debit + credit.

    Raises ValueError for ``amount < 1``, a self-transfer, or insufficient
    funds — the debit rides ``apply_debit``'s guarded UPDATE, so an
    insufficient balance fails with ZERO writes (no ledger row, no credit).
    Both sides are ledgered: ``transfer_out`` (meta ``{"to": to_id}``) and
    ``transfer_in`` (meta ``{"from": from_id}``). An optional ``memo`` is
    stored verbatim under a ``memo`` key on both rows; callers are responsible
    for trimming/capping it and for escaping at render time. Transfers do NOT
    mint — the booster multiplier is intentionally never applied to the credit,
    so the recipient gets exactly what the sender paid. Rides the caller's
    connection/transaction as the commit boundary.
    """
    if amount < 1:
        raise ValueError("transfer amount must be >= 1")
    if from_id == to_id:
        raise ValueError("cannot transfer to yourself")
    out_meta: dict = {"to": to_id}
    in_meta: dict = {"from": from_id}
    if memo:
        out_meta["memo"] = memo
        in_meta["memo"] = memo
    debited = apply_debit(
        conn, guild_id, from_id, amount, "transfer_out",
        actor_id=from_id, meta=out_meta,
    )
    if not debited:
        raise ValueError("insufficient funds")
    apply_credit(
        conn, guild_id, to_id, amount, "transfer_in",
        actor_id=from_id, meta=in_meta,
    )


def get_ledger(
    conn: sqlite3.Connection, guild_id: int, user_id: int, limit: int = 10
) -> list[sqlite3.Row]:
    """Return the user's most recent ledger rows, newest first."""
    return conn.execute(
        """
        SELECT id, guild_id, user_id, amount, kind, actor_id, meta, created_at
        FROM econ_ledger
        WHERE guild_id = ? AND user_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (guild_id, user_id, limit),
    ).fetchall()


def get_notify_muted(conn: sqlite3.Connection, guild_id: int, user_id: int) -> bool:
    row = conn.execute(
        "SELECT muted FROM econ_notify_prefs WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    return bool(row["muted"]) if row else False


def set_notify_muted(
    conn: sqlite3.Connection, guild_id: int, user_id: int, muted: bool
) -> None:
    conn.execute(
        """
        INSERT INTO econ_notify_prefs (guild_id, user_id, muted)
        VALUES (?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET muted = excluded.muted
        """,
        (guild_id, user_id, 1 if muted else 0),
    )


# ── faucets: login, conversion, QOTD, game rewards ────────────────────


@dataclass(frozen=True)
class LoginOutcome:
    paid: int
    streak: int
    milestone: int
    grace_consumed: bool
    reset: bool


def process_login(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    user_id: int,
    *,
    local_day: str,
    source: str,
    booster: bool,
) -> LoginOutcome | None:
    """Pay the daily login for the first qualifying activity of a local day.

    Returns None when the user already logged in this local day. The
    INSERT OR IGNORE on econ_logins is the race anchor: it rides the same
    connection/transaction as the credits, so concurrent triggers pay at
    most once. Milestone bonuses land as a separate "milestone" ledger row.
    """
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO econ_logins (guild_id, user_id, local_day, source, paid)
        VALUES (?, ?, ?, ?, 0)
        """,
        (guild_id, user_id, local_day, source),
    )
    if (cur.rowcount or 0) == 0:
        return None

    row = conn.execute(
        """
        SELECT current_streak, longest_streak, last_login_day, last_grace_day
        FROM econ_streaks
        WHERE guild_id = ? AND user_id = ?
        """,
        (guild_id, user_id),
    ).fetchone()
    ev = logic.evaluate_login(
        today=local_day,
        last_login_day=row["last_login_day"] if row else None,
        current_streak=int(row["current_streak"]) if row else 0,
        last_grace_day=row["last_grace_day"] if row else None,
    )

    last_grace_day = ev.grace_covers_day if ev.grace_consumed else (
        row["last_grace_day"] if row else None
    )
    longest = max(ev.new_streak, int(row["longest_streak"]) if row else 0)
    conn.execute(
        """
        INSERT INTO econ_streaks
            (guild_id, user_id, current_streak, longest_streak,
             last_login_day, last_grace_day)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            current_streak = excluded.current_streak,
            longest_streak = excluded.longest_streak,
            last_login_day = excluded.last_login_day,
            last_grace_day = excluded.last_grace_day
        """,
        (guild_id, user_id, ev.new_streak, longest, local_day, last_grace_day),
    )

    base = settings.login_voice_base if source == "voice" else settings.login_text_base
    amount = logic.login_amount(ev.new_streak, base, settings.streak_bonus_cap)
    paid = 0
    if amount > 0:
        paid = apply_credit(
            conn,
            guild_id,
            user_id,
            amount,
            "login",
            meta={"local_day": local_day, "source": source, "streak": ev.new_streak},
            booster=booster,
            multiplier=settings.booster_multiplier,
        )

    milestone = logic.milestone_amount(ev.new_streak, settings)
    milestone_paid = 0
    if milestone > 0:
        milestone_paid = apply_credit(
            conn,
            guild_id,
            user_id,
            milestone,
            "milestone",
            meta={"local_day": local_day, "streak": ev.new_streak},
            booster=booster,
            multiplier=settings.booster_multiplier,
        )

    conn.execute(
        """
        UPDATE econ_logins SET paid = ?
        WHERE guild_id = ? AND user_id = ? AND local_day = ?
        """,
        (paid + milestone_paid, guild_id, user_id, local_day),
    )
    return LoginOutcome(
        paid=paid,
        streak=ev.new_streak,
        milestone=milestone_paid,
        grace_consumed=ev.grace_consumed,
        reset=ev.reset,
    )


def process_conversion(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    user_id: int,
    *,
    local_day: str,
    xp: float,
    booster: bool,
) -> int:
    """Convert one local day's XP to currency; returns the credited amount.

    Idempotent per (guild, user, local_day) via INSERT OR IGNORE on
    econ_conversions — a replayed day returns 0 with no writes. The
    fractional remainder from the latest prior conversion carries in.
    """
    prev = conn.execute(
        """
        SELECT remainder FROM econ_conversions
        WHERE guild_id = ? AND user_id = ?
        ORDER BY local_day DESC LIMIT 1
        """,
        (guild_id, user_id),
    ).fetchone()
    carry = float(prev["remainder"]) if prev else 0.0
    coins, remainder = logic.convert_xp(xp, carry, settings.xp_per_coin)

    cur = conn.execute(
        """
        INSERT OR IGNORE INTO econ_conversions
            (guild_id, user_id, local_day, xp, coins, remainder)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (guild_id, user_id, local_day, xp, coins, remainder),
    )
    if (cur.rowcount or 0) == 0:
        return 0
    if coins <= 0:
        return 0
    return apply_credit(
        conn,
        guild_id,
        user_id,
        coins,
        "conversion",
        meta={"local_day": local_day, "xp": round(xp, 2)},
        booster=booster,
        multiplier=settings.booster_multiplier,
    )


def create_qotd(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    message_id: int,
    question: str,
    posted_by: int,
    local_day: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO econ_qotd
            (guild_id, channel_id, message_id, question, posted_by,
             local_day, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (guild_id, channel_id, message_id, question, posted_by, local_day, time.time()),
    )
    return int(cur.lastrowid or 0)


def open_qotd_for(
    conn: sqlite3.Connection, guild_id: int, channel_id: int, local_day: str
) -> sqlite3.Row | None:
    """Return the QOTD open in this channel for this local day (latest wins)."""
    return conn.execute(
        """
        SELECT id, guild_id, channel_id, message_id, question, posted_by,
               local_day, created_at
        FROM econ_qotd
        WHERE guild_id = ? AND channel_id = ? AND local_day = ?
        ORDER BY id DESC LIMIT 1
        """,
        (guild_id, channel_id, local_day),
    ).fetchone()


def try_award_qotd(
    conn: sqlite3.Connection,
    settings: EconSettings,
    qotd_id: int,
    guild_id: int,
    user_id: int,
    *,
    booster: bool,
) -> bool:
    """Pay the QOTD reward once per member; False if already rewarded."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO econ_qotd_rewards (qotd_id, user_id) VALUES (?, ?)",
        (qotd_id, user_id),
    )
    if (cur.rowcount or 0) == 0:
        return False
    if settings.reward_qotd > 0:
        apply_credit(
            conn,
            guild_id,
            user_id,
            settings.reward_qotd,
            "qotd",
            meta={"qotd_id": qotd_id},
            booster=booster,
            multiplier=settings.booster_multiplier,
        )
    return True


def award_game_reward(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    user_id: int,
    *,
    kind: str,
    booster: bool,
) -> int:
    """Credit a game reward; ``kind`` picks the amount. Returns the credit."""
    amounts = {
        "game_participation": settings.reward_game_participation,
        "game_win": settings.reward_game_win,
    }
    if kind not in amounts:
        raise ValueError(f"unknown game reward kind: {kind!r}")
    amount = amounts[kind]
    if amount <= 0:
        return 0
    return apply_credit(
        conn,
        guild_id,
        user_id,
        amount,
        kind,
        booster=booster,
        multiplier=settings.booster_multiplier,
    )


def member_is_booster(bot: discord.Client, guild_id: int, user_id: int) -> bool:
    """True when the member is currently boosting the guild."""
    guild = bot.get_guild(guild_id)
    if guild is None:
        return False
    member = guild.get_member(user_id)
    return member is not None and member.premium_since is not None


async def notify_member(
    bot: discord.Client,
    db_path: Path,
    guild_id: int,
    user_id: int,
    *,
    embed: discord.Embed | None = None,
    content: str | None = None,
    require_game_role: bool = False,
) -> bool:
    """DM an economy notification, falling back to the bank channel.

    A muted member (econ_notify_prefs) is silently dropped and counts as
    delivered. Returns False only when both the DM and the bank-channel
    fallback fail.

    ``require_game_role`` gates the notice on the opt-in economy role: a
    member without it is dropped silently (returns True, like a mute) so
    recurring engagement notices — streaks, milestones — only reach players
    who opted in. With no ``game_role_id`` configured, nobody has opted in
    yet, so the gate defaults to dropping everyone rather than notifying the
    whole guild. Leave it False for transactional notices (e.g. rental
    billing) that target a member by their prior spend, not by opt-in.
    """
    import discord  # local import to keep this module import-light for tests

    from bot_modules.core.db_utils import open_db

    def _read():
        with open_db(db_path) as conn:
            return (
                get_notify_muted(conn, guild_id, user_id),
                load_econ_settings(conn, guild_id),
            )

    muted, settings = await asyncio.to_thread(_read)
    if muted:
        return True

    guild = bot.get_guild(guild_id)
    member = guild.get_member(user_id) if guild else None

    if require_game_role:
        if (
            not settings.game_role_id
            or member is None
            or not any(r.id == settings.game_role_id for r in member.roles)
        ):
            return True

    kwargs: dict = {}
    if content:
        kwargs["content"] = content
    if embed:
        kwargs["embed"] = embed

    if member is not None:
        try:
            await member.send(**kwargs)
            return True
        except (discord.Forbidden, discord.HTTPException):
            pass

    if guild is None or not settings.bank_channel_id:
        return False
    channel = guild.get_channel(settings.bank_channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        return False
    mention = f"<@{user_id}>"
    fallback_kwargs: dict = {"content": f"{mention} {content}" if content else mention}
    if embed:
        fallback_kwargs["embed"] = embed
    try:
        await channel.send(**fallback_kwargs)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False
