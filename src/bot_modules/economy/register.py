"""Economy register — the public transaction feed for a channel.

A bank register: every currency movement, posted as it happens, each entry
saying what it was *for*. ``econ_ledger`` is the single source of truth
(nothing mutates a wallet without going through ``apply_credit`` /
``apply_debit``), so draining it by ``id`` catches every payout, purchase,
transfer, and staff grant — including dashboard grants — with no per-call-site
hooks to forget.

The feed is **completions only**: a ledger row exists once a transaction has
happened, so there is no per-tick progress spam. A counted quest's entry shows
its final tally ("5/5") rather than the ticks that got there.

Pure collector + builder — all Discord I/O stays in the loop. The builder takes
a ``resolve_name`` callable so it never touches the gateway itself.

The channel lives in the ``econ_`` config (``register_channel_id``); unset (0)
means the feature is off. ``register_cursor_id`` is the bot-managed drain
cursor (same bookkeeping pattern as ``leaderboard_message_id``).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord

from bot_modules.economy import quests as quest_logic

if TYPE_CHECKING:
    from collections.abc import Callable

    from bot_modules.services.economy_service import EconSettings

# Credit/debit colours are semantic here (money in vs money out), so they stay
# fixed rather than following the guild accent. A transfer is neither — the
# currency only moves sideways between members — so it gets its own neutral.
CREDIT_COLOUR = discord.Colour(0x2ECC71)
DEBIT_COLOUR = discord.Colour(0xE74C3C)
TRANSFER_COLOUR = discord.Colour(0x5865F2)

# Ledger kinds the register never posts.
#
# ``login`` and ``conversion`` are the automated per-member faucets: they fire
# once per active member and all land together at the guild-local day roll, so
# posting them would bury every quest, purchase and transfer under a nightly
# burst of routine noise. They still hit the ledger, the wallet, and the
# metrics rollup — they are simply not news.
#
# ``transfer_in`` is skipped because a transfer writes two rows (out + in) for
# one event; the register posts the ``transfer_out`` leg as a single
# consolidated "A → B" entry instead of reporting the same movement twice.
SKIP_KINDS: tuple[str, ...] = ("login", "conversion", "transfer_in")

# Per-kind glyph + human label. The label is the fallback memo for kinds whose
# meta carries nothing extra to say.
_KIND_DISPLAY: dict[str, tuple[str, str]] = {
    "quest": ("💰", "Quest reward"),
    "quest_community": ("🤝", "Community quest"),
    "rental": ("🛒", "Perk rental"),
    "transfer_out": ("↔️", "Transfer sent"),
    "transfer_in": ("↔️", "Transfer received"),
    "login": ("📅", "Daily login"),
    "milestone": ("🏆", "Streak milestone"),
    "conversion": ("✨", "XP conversion"),
    "qotd": ("💬", "Question of the day"),
    "game_participation": ("🎲", "Game participation"),
    "game_win": ("🥇", "Game win"),
    "grant": ("🎁", "Staff grant"),
    "quest_reroll": ("🎲", "Quest reroll"),
}

_FALLBACK_DISPLAY = ("🪙", "Adjustment")

# Human labels for the rentable perks (rentals_service._PERKS). gift_color
# stays although the kind retired in migration 091: ledger meta was not
# rewritten, so pre-090 rental rows still carry it.
_PERK_LABELS: dict[str, str] = {
    "role_color": "Custom role colour",
    "role_name": "Custom role name",
    "role_icon": "Role icon",
    "role_gradient": "Gradient role colour",
    "gift_color": "Gifted role colour",
}

_LOGIN_SOURCE_LABELS = {"text": "text", "voice": "voice"}


@dataclass(frozen=True)
class RegisterEntry:
    """One drained ledger row, with everything the embed needs pre-resolved."""

    ledger_id: int
    user_id: int
    amount: int  # signed: + credit, − debit
    kind: str
    actor_id: int | None
    meta: dict
    created_at: float
    # The wallet balance immediately AFTER this row was applied.
    balance_after: int
    # Resolved from meta at collect time (quest rows only).
    quest_title: str = ""
    quest_target: int = 1


def _parse_meta(raw: object) -> dict:
    """Best-effort decode of a ledger row's JSON meta blob."""
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def collect_register_entries(
    conn: sqlite3.Connection,
    guild_id: int,
    after_id: int,
    limit: int,
) -> list[RegisterEntry]:
    """Drain up to ``limit`` postable ledger rows newer than ``after_id``.

    Oldest first. :data:`SKIP_KINDS` is filtered out in SQL, not afterwards, so
    a midnight burst of login/conversion rows can't consume the whole batch and
    starve the entries anyone wants to read.

    Balances are reconstructed rather than read live — the live wallet balance
    is only right for the newest row. The rewind walks **every** row for these
    members (skipped kinds included): a login row sitting between two posted
    entries still moved the balance, so ignoring it would print arithmetic that
    doesn't add up. All reads share one connection snapshot, so a concurrent
    credit can't skew it.
    """
    skips = ",".join("?" * len(SKIP_KINDS))
    rows = conn.execute(
        f"""
        SELECT id, user_id, amount, kind, actor_id, meta, created_at
        FROM econ_ledger
        WHERE guild_id = ? AND id > ? AND kind NOT IN ({skips})
        ORDER BY id ASC
        LIMIT ?
        """,
        (guild_id, after_id, *SKIP_KINDS, limit),
    ).fetchall()
    if not rows:
        return []

    batch_ids = {int(r["id"]) for r in rows}
    min_id = min(batch_ids)
    user_ids = {int(r["user_id"]) for r in rows}
    placeholders = ",".join("?" * len(user_ids))

    balances = {
        int(r["user_id"]): int(r["balance"])
        for r in conn.execute(
            f"SELECT user_id, balance FROM econ_wallets "
            f"WHERE guild_id = ? AND user_id IN ({placeholders})",
            [guild_id, *user_ids],
        ).fetchall()
    }
    # Every row from the batch's start onward, unfiltered — the rewind needs
    # the movements we are NOT posting just as much as the ones we are.
    movements = conn.execute(
        f"SELECT id, user_id, amount FROM econ_ledger "
        f"WHERE guild_id = ? AND user_id IN ({placeholders}) AND id >= ? "
        f"ORDER BY id ASC",
        [guild_id, *user_ids, min_id],
    ).fetchall()

    # Walk backwards from each member's live balance, recording the balance
    # each batch row produced as we pass it.
    running = dict(balances)
    balance_after: dict[int, int] = {}
    for mv in reversed(movements):
        uid = int(mv["user_id"])
        mid = int(mv["id"])
        if mid in batch_ids:
            balance_after[mid] = running.get(uid, 0)
        running[uid] = running.get(uid, 0) - int(mv["amount"])

    quests = _resolve_quests(conn, guild_id, rows)
    periods = _resolve_claim_periods(conn, guild_id, rows)

    entries: list[RegisterEntry] = []
    for row in rows:
        uid = int(row["user_id"])
        meta = _parse_meta(row["meta"])
        title, target = _quest_display(quests, periods, meta, uid)
        entries.append(
            RegisterEntry(
                ledger_id=int(row["id"]),
                user_id=uid,
                amount=int(row["amount"]),
                kind=str(row["kind"]),
                actor_id=int(row["actor_id"]) if row["actor_id"] is not None else None,
                meta=meta,
                created_at=float(row["created_at"]),
                balance_after=balance_after.get(int(row["id"]), 0),
                quest_title=title,
                quest_target=target,
            )
        )
    return entries


def _quest_id_of(meta: dict) -> int:
    """The meta blob's quest id, or 0 when it carries none."""
    try:
        return int(meta.get("quest_id") or 0)
    except (TypeError, ValueError):
        return 0


def _claim_id_of(meta: dict) -> int:
    """The meta blob's claim id, or 0 when it carries none."""
    try:
        return int(meta.get("claim_id") or 0)
    except (TypeError, ValueError):
        return 0


def _resolve_quests(
    conn: sqlite3.Connection, guild_id: int, rows: list[sqlite3.Row]
) -> dict[int, sqlite3.Row]:
    """Bulk-load the quest rows referenced by the batch's meta blobs.

    This is the Venmo memo: the ledger says "+50", the quest says what earned it.
    """
    quest_ids = {
        qid for qid in (_quest_id_of(_parse_meta(r["meta"])) for r in rows) if qid
    }
    if not quest_ids:
        return {}
    placeholders = ",".join("?" * len(quest_ids))
    return {
        int(r["id"]): r
        for r in conn.execute(
            f"SELECT id, title, target_count, target_min, target_max "
            f"FROM econ_quests WHERE guild_id = ? AND id IN ({placeholders})",
            [guild_id, *quest_ids],
        ).fetchall()
    }


def _resolve_claim_periods(
    conn: sqlite3.Connection, guild_id: int, rows: list[sqlite3.Row]
) -> dict[int, str]:
    """Bulk-resolve claim_id → period for the batch's quest rows.

    The period is what makes a banded quest's per-member target reproducible
    (:func:`quests.effective_target` is seeded on user+quest+period).
    """
    claim_ids = {
        cid for cid in (_claim_id_of(_parse_meta(r["meta"])) for r in rows) if cid
    }
    if not claim_ids:
        return {}
    placeholders = ",".join("?" * len(claim_ids))
    return {
        int(r["id"]): str(r["period"])
        for r in conn.execute(
            f"SELECT id, period FROM econ_quest_claims "
            f"WHERE guild_id = ? AND id IN ({placeholders})",
            [guild_id, *claim_ids],
        ).fetchall()
    }


def _quest_display(
    quests: dict[int, sqlite3.Row],
    periods: dict[int, str],
    meta: dict,
    user_id: int,
) -> tuple[str, int]:
    """A quest row's (title, target) for one ledger entry.

    The target is the *member's own* target, not the library's ``target_count``:
    a banded quest draws each member a different one, so showing the raw column
    would print a tally the member never actually worked to.
    """
    quest = quests.get(_quest_id_of(meta))
    if quest is None:
        return "", 1
    period = periods.get(_claim_id_of(meta))
    if period is None:
        # No claim to pin the period on (a hand-written or legacy row): the
        # fixed count is right unless the quest is banded, where no honest
        # tally exists — fall back to "no tally".
        target_count = int(quest["target_count"] or 1)
        banded = 0 < int(quest["target_min"] or 0) < int(quest["target_max"] or 0)
        return str(quest["title"]), 1 if banded else target_count
    return str(quest["title"]), quest_logic.effective_target(
        int(quest["target_count"] or 1),
        int(quest["target_min"] or 0),
        int(quest["target_max"] or 0),
        user_id=user_id,
        quest_id=int(quest["id"]),
        period=period,
    )


def render_memo(entry: RegisterEntry, resolve_name: Callable[[int], str]) -> str:
    """The "what it was for" line — a human memo built from kind + meta.

    Every ledger kind gets a specific memo where its meta supports one; unknown
    kinds (a future payout source that predates this map) degrade to a
    title-cased kind rather than going blank.
    """
    kind = entry.kind
    meta = entry.meta

    if kind == "quest":
        title = entry.quest_title or "a quest"
        if entry.quest_target > 1:
            return f"Quest: **{title}** ({entry.quest_target}/{entry.quest_target})"
        return f"Quest: **{title}**"

    if kind == "quest_community":
        return "Community quest payout"

    if kind == "rental":
        perk = _PERK_LABELS.get(str(meta.get("perk", "")), str(meta.get("perk") or "a perk"))
        if meta.get("renewal"):
            return f"Perk renewal: **{perk}**"
        return f"Perk rental: **{perk}**"

    if kind == "transfer_out":
        # Consolidated: the counterparty is named in the header, not here.
        return "Transfer"

    if kind == "transfer_in":
        # Not posted (see SKIP_KINDS) — rendered only if something asks.
        return f"Transfer from {resolve_name(int(meta.get('from') or 0))}"

    if kind == "login":
        source = _LOGIN_SOURCE_LABELS.get(str(meta.get("source", "")), "")
        streak = int(meta.get("streak") or 0)
        bits = "Daily login"
        if source:
            bits += f" ({source})"
        if streak > 1:
            bits += f" — {streak}-day streak"
        return bits

    if kind == "milestone":
        streak = int(meta.get("streak") or 0)
        return f"Streak milestone — {streak} days" if streak else "Streak milestone"

    if kind == "conversion":
        xp = float(meta.get("xp") or 0)
        return f"XP conversion — {xp:,.0f} XP earned" if xp else "XP conversion"

    if kind == "qotd":
        return "Answered the question of the day"

    if kind in ("game_participation", "game_win"):
        return _KIND_DISPLAY[kind][1]

    if kind == "grant":
        reason = str(meta.get("reason") or "").strip()
        actor = resolve_name(entry.actor_id) if entry.actor_id else "staff"
        return f"Staff grant by {actor} — {reason}" if reason else f"Staff grant by {actor}"

    label = _KIND_DISPLAY.get(kind, _FALLBACK_DISPLAY)[1]
    return label if kind in _KIND_DISPLAY else kind.replace("_", " ").capitalize()


def build_register_embed(
    entry: RegisterEntry,
    settings: EconSettings,
    resolve_name: Callable[[int], str],
    *,
    avatar_url: str | None = None,
) -> discord.Embed:
    """One register entry as a compact Venmo-style embed.

    Green for money in, red for money out — the colour IS the information here,
    so this is a deliberate exception to the guild-accent convention.
    """
    glyph = _KIND_DISPLAY.get(entry.kind, _FALLBACK_DISPLAY)[0]
    emoji = settings.currency_emoji
    credit = entry.amount >= 0
    magnitude = abs(entry.amount)

    if entry.kind == "transfer_out":
        # One entry for the whole movement: "Billy → Sam", unsigned (the
        # currency didn't enter or leave the economy, it moved sideways), and
        # the footer is explicitly the sender's wallet.
        recipient = resolve_name(int(entry.meta.get("to") or 0))
        header = f"{resolve_name(entry.user_id)} → {recipient}"
        colour = TRANSFER_COLOUR
        amount_text = f"**{magnitude:,} {emoji}**"
        footer = f"{resolve_name(entry.user_id)}'s {settings.wallet_name.lower()}: "
    else:
        header = resolve_name(entry.user_id)
        colour = CREDIT_COLOUR if credit else DEBIT_COLOUR
        amount_text = f"**{'+' if credit else '−'}{magnitude:,} {emoji}**"
        footer = f"{settings.wallet_name}: "

    embed = discord.Embed(
        colour=colour,
        description=f"{glyph} {amount_text} · {render_memo(entry, resolve_name)}",
        timestamp=datetime.fromtimestamp(entry.created_at, tz=timezone.utc),
    )
    embed.set_author(name=header, icon_url=avatar_url)
    embed.set_footer(text=f"{footer}{entry.balance_after:,} {emoji}")
    return embed
