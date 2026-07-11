"""Economy service — DB layer for wallets, the ledger, and per-guild settings.

Soft-currency balances, a signed audit ledger, and balance-change DM mute
prefs, plus the per-guild ``econ_`` settings stored in the shared config KV
table. See docs/economy_spec.md for the feature design.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from dataclasses import dataclass, fields

ECON_PREFIX = "econ_"


@dataclass(frozen=True)
class EconSettings:
    enabled: bool = False
    bank_channel_id: int = 0
    manager_role_id: int = 0
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
    price_role_color: int = 50
    price_role_name: int = 35
    price_role_icon: int = 75
    price_role_gradient: int = 120
    price_text_room: int = 200
    price_voice_room: int = 200
    price_gift_color: int = 50


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
