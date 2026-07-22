"""The Golden Meadow casino — settings + the single money choke point.

Every coin that enters or leaves a casino game moves through this module:
:func:`take_stake` (the only debit path, guarding economy-enabled → casino
open → table open → bet limits → daily cap → funds), :func:`pay_out` and
:func:`refund` (unboosted credits — house payouts must never mint through
the booster multiplier, the wager-service rule). Game math lives in
``casino_logic``; Discord glue in ``cogs/casino``.

Settings persist as ``casino_*`` keys in the shared config KV table, the
EconSettings pattern: a frozen dataclass, load with per-key fallback to
defaults, partial-dict save that raises ``KeyError`` on unknown fields.
``channel_id`` is the master switch — 0 (the default) means the whole
casino is off, so the feature ships dark like every sink.

Blackjack hands and roulette rounds persist here too, because their
settlement IS money movement: both predicate on ``settled_at IS NULL``
(exactly-once under replayed timers, boot sweeps and double-clicks), and
every terminal path settles or refunds — a stake can never evaporate.
"""

from __future__ import annotations

import json
import sqlite3
import time

from dataclasses import dataclass, fields
from typing import NamedTuple

from bot_modules.economy.logic import local_day_for
from bot_modules.services import casino_logic
from bot_modules.services.economy_service import (
    apply_credit,
    apply_debit,
    get_balance,
    load_econ_settings,
)

CASINO_PREFIX = "casino_"

STAKE_KIND = "casino_stake"
PAYOUT_KIND = "casino_payout"
REFUND_KIND = "casino_refund"

GAMES = ("coinflip", "slots", "blackjack", "roulette")


@dataclass(frozen=True)
class CasinoSettings:
    # Master switch: the channel the casino lives in. 0 = casino closed.
    channel_id: int = 0
    min_bet: int = 5
    max_bet: int = 100
    # Per-member total staked per guild-local day; 0 = uncapped.
    daily_wager_cap: int = 500
    coinflip_enabled: bool = True
    slots_enabled: bool = True
    blackjack_enabled: bool = True
    roulette_enabled: bool = True
    roulette_window_seconds: int = 45
    # An untouched blackjack hand auto-stands after this long.
    blackjack_idle_seconds: int = 180
    # Bot bookkeeping (the hub panel message + where it lives, so a channel
    # move can clean up the old panel) — not dashboard-editable.
    panel_message_id: int = 0
    panel_channel_id: int = 0


DEFAULT_CASINO_SETTINGS = CasinoSettings()

_BOOL_KEYS = [
    "coinflip_enabled",
    "slots_enabled",
    "blackjack_enabled",
    "roulette_enabled",
]
# Everything else on the dataclass is a plain int.
_INT_KEYS = [f.name for f in fields(CasinoSettings) if f.name not in _BOOL_KEYS]
_ALL_KEYS = frozenset(f.name for f in fields(CasinoSettings))


def load_casino_settings(conn: sqlite3.Connection, guild_id: int) -> CasinoSettings:
    """Build CasinoSettings from stored ``casino_*`` config values.

    Guild-scoped only (no legacy guild_id=0 fallback), missing or
    unparseable values fall back to the dataclass defaults — the econ
    loader's contract. One query for all keys (GLOB treats the underscore
    literally, unlike LIKE) — this loader runs on every bet, so per-field
    SELECTs would be ~12 round-trips per click.
    """
    from bot_modules.core.db_utils import parse_bool  # noqa: PLC0415

    stored = {
        str(r["key"])[len(CASINO_PREFIX):]: str(r["value"])
        for r in conn.execute(
            "SELECT key, value FROM config WHERE guild_id = ? "
            "AND key GLOB 'casino_*'",
            (guild_id,),
        )
    }
    defaults = DEFAULT_CASINO_SETTINGS
    kwargs: dict[str, object] = {}
    for key in _BOOL_KEYS:
        raw = stored.get(key, "")
        if raw:
            kwargs[key] = parse_bool(raw, getattr(defaults, key))
    for key in _INT_KEYS:
        raw = stored.get(key, "")
        if raw:
            try:
                kwargs[key] = int(raw)
            except ValueError:
                pass
    if not kwargs:
        return defaults
    for f in defaults.__dataclass_fields__:
        if f not in kwargs:
            kwargs[f] = getattr(defaults, f)
    return CasinoSettings(**kwargs)  # type: ignore[arg-type]


def save_casino_settings(
    conn: sqlite3.Connection, guild_id: int, values: dict[str, object]
) -> None:
    """Persist a partial dict of settings; unknown keys raise KeyError."""
    from bot_modules.core.db_utils import set_config_value  # noqa: PLC0415

    unknown = set(values) - _ALL_KEYS
    if unknown:
        raise KeyError(f"unknown casino setting(s): {sorted(unknown)}")
    for key, value in values.items():
        stored = ("1" if value else "0") if isinstance(value, bool) else str(value)
        set_config_value(conn, f"{CASINO_PREFIX}{key}", stored, guild_id)


def game_enabled(settings: CasinoSettings, game: str) -> bool:
    return bool(getattr(settings, f"{game}_enabled"))


# ── the money choke point ──────────────────────────────────────────────


def wagered_today(
    conn: sqlite3.Connection, guild_id: int, user_id: int, local_day: str
) -> int:
    row = conn.execute(
        "SELECT wagered FROM casino_daily "
        "WHERE guild_id = ? AND user_id = ? AND local_day = ?",
        (guild_id, user_id, local_day),
    ).fetchone()
    return int(row["wagered"]) if row else 0


def take_stake(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    amount: int,
    game: str,
    *,
    now: float | None = None,
    enforce_bet_limits: bool = True,
    channel_id: int | None = None,
) -> str | None:
    """Debit a stake, or return the member-facing reason it can't happen.

    ``None`` = the money moved (kind ``casino_stake``). The guard order is
    deliberate: feature gates before member-specific limits, funds last so
    the error a member sees is the most actionable one. A blackjack
    double-down passes ``enforce_bet_limits=False`` — its amount was
    already validated at the deal, and doubling a table-max bet is part of
    the game — but the daily cap and the balance still apply.

    ``channel_id`` (when the caller knows it) must match the configured
    casino channel: an orphaned hub panel — one a channel move failed to
    delete — keeps working buttons forever, and this is the guard that
    stops it taking real money outside the casino.
    """
    if amount < 1:
        raise ValueError("A casino stake has to be at least 1.")
    econ = load_econ_settings(conn, guild_id)
    if not econ.enabled:
        return "The economy isn't enabled here, so the casino can't run."
    settings = load_casino_settings(conn, guild_id)
    if not settings.channel_id:
        return "The casino is closed."
    if channel_id is not None and channel_id != settings.channel_id:
        return f"The casino has moved — find it in <#{settings.channel_id}>."
    if not game_enabled(settings, game):
        return "That table is closed right now."
    unit = econ.currency_plural
    if enforce_bet_limits:
        if amount < settings.min_bet:
            return f"Minimum bet is {settings.min_bet} {unit}."
        if settings.max_bet and amount > settings.max_bet:
            return f"Maximum bet is {settings.max_bet} {unit}."
    ts = time.time() if now is None else now
    day = ""
    if settings.daily_wager_cap:
        from bot_modules.core.db_utils import get_tz_offset_hours  # noqa: PLC0415

        day = local_day_for(ts, get_tz_offset_hours(conn, guild_id))
        already = wagered_today(conn, guild_id, user_id, day)
        if already + amount > settings.daily_wager_cap:
            left = max(0, settings.daily_wager_cap - already)
            return (
                f"That bet would pass your daily casino cap of "
                f"{settings.daily_wager_cap} {unit} — you have {left} left today."
            )
    if not apply_debit(
        conn, guild_id, user_id, amount, STAKE_KIND,
        actor_id=user_id, meta={"game": game},
    ):
        have = get_balance(conn, guild_id, user_id)
        return f"You need {amount} {unit} for that bet — you have {have}."
    if settings.daily_wager_cap:
        conn.execute(
            "INSERT INTO casino_daily (guild_id, user_id, local_day, wagered) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id, local_day) "
            "DO UPDATE SET wagered = wagered + excluded.wagered",
            (guild_id, user_id, day, amount),
        )
    return None


def pay_out(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    amount: int,
    game: str,
    meta: dict[str, object] | None = None,
) -> None:
    """Credit a win (kind ``casino_payout``). Amount 0 credits nothing."""
    if amount < 1:
        return
    full_meta: dict[str, object] = {"game": game, **(meta or {})}
    apply_credit(
        conn, guild_id, user_id, amount, PAYOUT_KIND,
        meta=full_meta,
        booster=False,  # a house payout must never mint through the booster
    )


def refund(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    amount: int,
    game: str,
    meta: dict[str, object] | None = None,
    *,
    now: float | None = None,
) -> None:
    """Return a stake (kind ``casino_refund``) — void rounds, boot sweeps.

    Also hands back daily-cap headroom: a house-initiated refund (restart,
    voided round) must not leave the member's cap consumed by a bet that
    never resolved. The decrement targets the CURRENT guild-local day,
    clamped at 0 — a refund landing after the day rolled simply finds no
    counter to give back, which is fine because that day's cap is moot.
    """
    if amount < 1:
        return
    full_meta: dict[str, object] = {"game": game, **(meta or {})}
    apply_credit(
        conn, guild_id, user_id, amount, REFUND_KIND,
        meta=full_meta,
        booster=False,
    )
    from bot_modules.core.db_utils import get_tz_offset_hours  # noqa: PLC0415

    day = local_day_for(
        time.time() if now is None else now, get_tz_offset_hours(conn, guild_id)
    )
    conn.execute(
        "UPDATE casino_daily SET wagered = MAX(0, wagered - ?) "
        "WHERE guild_id = ? AND user_id = ? AND local_day = ?",
        (amount, guild_id, user_id, day),
    )


# ── blackjack hands ────────────────────────────────────────────────────


def serialize_blackjack(
    deck: list[str], player: list[str], dealer: list[str]
) -> str:
    return json.dumps({"deck": deck, "player": player, "dealer": dealer})


def deserialize_blackjack(state_json: str) -> tuple[list[str], list[str], list[str]]:
    state = json.loads(state_json)
    return state["deck"], state["player"], state["dealer"]


def live_blackjack_hand(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM casino_blackjack_hands "
        "WHERE guild_id = ? AND user_id = ? AND settled_at IS NULL",
        (guild_id, user_id),
    ).fetchone()


def get_blackjack_hand(conn: sqlite3.Connection, hand_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM casino_blackjack_hands WHERE id = ?", (hand_id,)
    ).fetchone()


def create_blackjack_hand(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    user_id: int,
    stake: int,
    state_json: str,
    *,
    now: float | None = None,
) -> int:
    """Open a hand row (caller has already debited via take_stake).

    The one-live-hand-per-member partial unique index backstops the caller's
    live_blackjack_hand check — a raced second deal raises IntegrityError
    and rolls back with the whole transaction, stake included.
    """
    ts = time.time() if now is None else now
    cur = conn.execute(
        "INSERT INTO casino_blackjack_hands "
        "(guild_id, channel_id, user_id, stake, state_json, created_at, "
        "last_action_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (guild_id, channel_id, user_id, stake, state_json, ts, ts),
    )
    return int(cur.lastrowid or 0)


def set_blackjack_message(
    conn: sqlite3.Connection, hand_id: int, message_id: int
) -> None:
    conn.execute(
        "UPDATE casino_blackjack_hands SET message_id = ? WHERE id = ?",
        (message_id, hand_id),
    )


def update_blackjack_state(
    conn: sqlite3.Connection, hand_id: int, state_json: str,
    *, now: float | None = None,
) -> None:
    conn.execute(
        "UPDATE casino_blackjack_hands SET state_json = ?, last_action_at = ? "
        "WHERE id = ? AND settled_at IS NULL",
        (state_json, time.time() if now is None else now, hand_id),
    )


def double_blackjack_stake(
    conn: sqlite3.Connection,
    guild_id: int,
    hand_id: int,
    user_id: int,
    amount: int,
    *,
    now: float | None = None,
) -> str | None:
    """Debit the double-down's second stake and fold it into the hand.

    Returns the member-facing error (daily cap / funds) or None. Bet limits
    don't re-apply — the original amount was validated at the deal.

    The guarded no-op UPDATE claims the live hand INSIDE the write
    transaction before any money moves: a boot sweep or auto-stand that
    settled the hand from another connection makes the claim miss, so the
    second stake is never debited against a finished hand.
    """
    claimed = conn.execute(
        "UPDATE casino_blackjack_hands SET doubled = doubled "
        "WHERE id = ? AND settled_at IS NULL RETURNING id",
        (hand_id,),
    ).fetchone()
    if claimed is None:
        return "That hand is already finished."
    err = take_stake(
        conn, guild_id, user_id, amount, "blackjack",
        now=now, enforce_bet_limits=False,
    )
    if err is not None:
        return err
    conn.execute(
        "UPDATE casino_blackjack_hands SET stake = stake + ?, doubled = 1 "
        "WHERE id = ?",
        (amount, hand_id),
    )
    return None


def settle_blackjack_hand(
    conn: sqlite3.Connection,
    hand_id: int,
    payout: int,
    outcome: str,
    *,
    kind: str = PAYOUT_KIND,
    now: float | None = None,
) -> bool:
    """Finalize a hand and credit its return. False = already settled.

    Exactly-once via the ``settled_at IS NULL`` predicate — the idle
    auto-stand timer, a boot sweep and a button resolution can all reach a
    terminal hand, and only the first one pays.
    """
    row = conn.execute(
        "UPDATE casino_blackjack_hands SET settled_at = ?, outcome = ? "
        "WHERE id = ? AND settled_at IS NULL RETURNING guild_id, user_id, stake",
        (time.time() if now is None else now, outcome, hand_id),
    ).fetchone()
    if row is None:
        return False
    if payout >= 1:
        meta: dict[str, object] = {"hand_id": hand_id, "outcome": outcome}
        if kind == PAYOUT_KIND:
            pay_out(
                conn, int(row["guild_id"]), int(row["user_id"]), payout,
                "blackjack", meta=meta,
            )
        else:
            refund(
                conn, int(row["guild_id"]), int(row["user_id"]), payout,
                "blackjack", meta=meta, now=now,
            )
    return True


class BlackjackStep(NamedTuple):
    """One resolved hit/stand/double press. err set = nothing happened."""

    err: str | None = None
    player: list[str] | None = None
    dealer: list[str] | None = None
    stake: int = 0
    doubled: bool = False
    outcome: str | None = None  # None = the hand is still live
    payout: int = 0


def resolve_blackjack_action(
    conn: sqlite3.Connection,
    guild_id: int,
    hand_id: int,
    user_id: int,
    action: str,
    *,
    now: float | None = None,
) -> BlackjackStep:
    """One button press — every rule and coin movement in one tested place.

    The opening guarded UPDATE both claims the live hand inside the write
    transaction (so a boot sweep / auto-stand settling from another
    connection can't interleave — their commit makes our claim miss) and
    bumps ``last_action_at``, resetting the idle clock on every press.
    The double-down's second stake is derived from the hand row, never
    caller-supplied, and only a two-card hand may double.
    """
    ts = time.time() if now is None else now
    row = conn.execute(
        "UPDATE casino_blackjack_hands SET last_action_at = ? "
        "WHERE id = ? AND settled_at IS NULL RETURNING *",
        (ts, hand_id),
    ).fetchone()
    if row is None:
        return BlackjackStep(err="That hand is already finished.")
    if int(row["guild_id"]) != guild_id or int(row["user_id"]) != user_id:
        return BlackjackStep(err="That's not your hand — deal your own!")
    deck, player, dealer = deserialize_blackjack(str(row["state_json"]))
    stake = int(row["stake"])
    doubled = bool(row["doubled"])

    def _finish(payout: int, outcome: str) -> BlackjackStep:
        if not settle_blackjack_hand(conn, hand_id, payout, outcome, now=now):
            return BlackjackStep(err="That hand is already finished.")
        return BlackjackStep(
            player=player, dealer=dealer, stake=stake, doubled=doubled,
            outcome=outcome, payout=payout,
        )

    if action == "double":
        if len(player) != 2:
            return BlackjackStep(
                err="You can only double on your first two cards."
            )
        err = double_blackjack_stake(conn, guild_id, hand_id, user_id, stake, now=now)
        if err is not None:
            return BlackjackStep(err=err)
        stake *= 2
        doubled = True
        player.append(deck.pop())
        if casino_logic.hand_value(player) > 21:
            return _finish(0, "bust")
        casino_logic.dealer_play(deck, dealer)
        return _finish(*casino_logic.blackjack_settle(player, dealer, stake))

    if action == "hit":
        player.append(deck.pop())
        value = casino_logic.hand_value(player)
        if value > 21:
            return _finish(0, "bust")
        if value == 21:
            casino_logic.dealer_play(deck, dealer)
            return _finish(*casino_logic.blackjack_settle(player, dealer, stake))
        update_blackjack_state(
            conn, hand_id, serialize_blackjack(deck, player, dealer), now=now
        )
        return BlackjackStep(
            player=player, dealer=dealer, stake=stake, doubled=doubled
        )

    if action == "stand":
        casino_logic.dealer_play(deck, dealer)
        return _finish(*casino_logic.blackjack_settle(player, dealer, stake))

    raise ValueError(f"unknown blackjack action: {action}")


def stand_idle_blackjack_hand(
    conn: sqlite3.Connection, hand_id: int, *, now: float | None = None
) -> BlackjackStep | None:
    """The idle sweep's auto-stand. None = the hand was already settled.

    Same in-transaction claim as :func:`resolve_blackjack_action`, minus
    the owner check (the system stands on the member's behalf).
    """
    row = conn.execute(
        "UPDATE casino_blackjack_hands SET last_action_at = last_action_at "
        "WHERE id = ? AND settled_at IS NULL RETURNING *",
        (hand_id,),
    ).fetchone()
    if row is None:
        return None
    deck, player, dealer = deserialize_blackjack(str(row["state_json"]))
    stake = int(row["stake"])
    casino_logic.dealer_play(deck, dealer)
    payout, outcome = casino_logic.blackjack_settle(player, dealer, stake)
    if not settle_blackjack_hand(conn, hand_id, payout, outcome, now=now):
        return None
    return BlackjackStep(
        player=player, dealer=dealer, stake=stake, doubled=bool(row["doubled"]),
        outcome=outcome, payout=payout,
    )


def refund_member_live_stakes(
    conn: sqlite3.Connection, guild_id: int, user_id: int, *, now: float | None = None
) -> dict[str, int]:
    """Refund a departing member's live casino money — the on_member_remove
    seam the PvP wager escrow already has, extended to the casino.

    The blackjack hand settles as refunded; the member's bets on any open
    roulette round are deleted (so the spin can't pay a ghost) and refunded
    as one credit. Returns {"blackjack": amount, "roulette": amount}.
    """
    out = {"blackjack": 0, "roulette": 0}
    hand = live_blackjack_hand(conn, guild_id, user_id)
    if hand is not None and settle_blackjack_hand(
        conn, int(hand["id"]), int(hand["stake"]), "refunded",
        kind=REFUND_KIND, now=now,
    ):
        out["blackjack"] = int(hand["stake"])
    bets = conn.execute(
        "SELECT b.id, b.amount FROM casino_roulette_bets b "
        "JOIN casino_roulette_rounds r ON r.id = b.round_id "
        "WHERE b.guild_id = ? AND b.user_id = ? AND r.status = 'open'",
        (guild_id, user_id),
    ).fetchall()
    total = sum(int(b["amount"]) for b in bets)
    if total:
        conn.executemany(
            "DELETE FROM casino_roulette_bets WHERE id = ?",
            [(int(b["id"]),) for b in bets],
        )
        refund(
            conn, guild_id, user_id, total, "roulette",
            meta={"left_guild": True}, now=now,
        )
        out["roulette"] = total
    return out


def refund_live_blackjack_hands(
    conn: sqlite3.Connection, *, now: float | None = None
) -> list[sqlite3.Row]:
    """Boot sweep: refund every live hand's full stake (honest reset).

    Returns the swept rows (pre-settlement copies) so the cog can best-effort
    edit their messages. Exactly-once per hand via settle's predicate.
    """
    rows = conn.execute(
        "SELECT * FROM casino_blackjack_hands WHERE settled_at IS NULL"
    ).fetchall()
    swept = []
    for row in rows:
        if settle_blackjack_hand(
            conn, int(row["id"]), int(row["stake"]), "refunded",
            kind=REFUND_KIND, now=now,
        ):
            swept.append(row)
    return swept


def idle_live_blackjack_hands(
    conn: sqlite3.Connection, older_than: float
) -> list[sqlite3.Row]:
    """Live hands untouched since ``older_than`` — the auto-stand sweep."""
    return conn.execute(
        "SELECT * FROM casino_blackjack_hands "
        "WHERE settled_at IS NULL AND last_action_at < ?",
        (older_than,),
    ).fetchall()


# ── roulette rounds ────────────────────────────────────────────────────


def live_roulette_round(
    conn: sqlite3.Connection, channel_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM casino_roulette_rounds "
        "WHERE channel_id = ? AND status = 'open'",
        (channel_id,),
    ).fetchone()


def get_roulette_round(conn: sqlite3.Connection, round_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM casino_roulette_rounds WHERE id = ?", (round_id,)
    ).fetchone()


def open_roulette_round(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    window_seconds: int,
    *,
    now: float | None = None,
) -> int | None:
    """Open a betting round; None if the channel already has one live.

    The partial unique index makes the one-open-round rule race-proof; the
    pre-check keeps the common path exception-free.
    """
    if live_roulette_round(conn, channel_id) is not None:
        return None
    ts = time.time() if now is None else now
    cur = conn.execute(
        "INSERT INTO casino_roulette_rounds "
        "(guild_id, channel_id, opened_at, closes_at) VALUES (?, ?, ?, ?)",
        (guild_id, channel_id, ts, ts + window_seconds),
    )
    return int(cur.lastrowid or 0)


def set_roulette_message(
    conn: sqlite3.Connection, round_id: int, message_id: int
) -> None:
    conn.execute(
        "UPDATE casino_roulette_rounds SET message_id = ? WHERE id = ?",
        (message_id, round_id),
    )


def place_roulette_bet(
    conn: sqlite3.Connection,
    round_id: int,
    user_id: int,
    bet_type: str,
    selection: int,
    amount: int,
    *,
    now: float | None = None,
) -> str | None:
    """Debit and record one bet. Returns member-facing error or None."""
    if bet_type not in casino_logic.ROULETTE_BET_TYPES:
        raise ValueError(f"unknown roulette bet type: {bet_type}")
    rnd = get_roulette_round(conn, round_id)
    ts = time.time() if now is None else now
    if rnd is None or str(rnd["status"]) != "open" or ts >= float(rnd["closes_at"]):
        return "Betting on that round has closed."
    # That pre-check ran in autocommit — a buzzer-beater bet can race the
    # settle timer, whose claim + bet-read commit between our check and our
    # debit, leaving a stake nothing ever pays or refunds. This guarded
    # no-op UPDATE is the first write of OUR transaction: it serializes
    # against the settler, and a round it already claimed makes us miss
    # here, before any money moves.
    claimed = conn.execute(
        "UPDATE casino_roulette_rounds SET message_id = message_id "
        "WHERE id = ? AND status = 'open' AND closes_at > ? RETURNING id",
        (round_id, ts),
    ).fetchone()
    if claimed is None:
        return "Betting on that round has closed."
    err = take_stake(conn, int(rnd["guild_id"]), user_id, amount, "roulette", now=now)
    if err is not None:
        return err
    conn.execute(
        "INSERT INTO casino_roulette_bets "
        "(round_id, guild_id, user_id, bet_type, selection, amount, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (round_id, int(rnd["guild_id"]), user_id, bet_type, selection, amount, ts),
    )
    return None


def roulette_bets(conn: sqlite3.Connection, round_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM casino_roulette_bets WHERE round_id = ? ORDER BY id",
        (round_id,),
    ).fetchall()


def settle_roulette_round(
    conn: sqlite3.Connection,
    round_id: int,
    result: int,
    *,
    now: float | None = None,
) -> list[sqlite3.Row] | None:
    """Spin resolution: claim the round, pay every winning bet.

    None = someone else already settled (or voided) it — exactly-once via
    the status='open' claim, taken BEFORE any credit moves (the raffle-draw
    rule). Returns the bet rows with ``payout`` filled in for the recap.
    """
    claimed = conn.execute(
        "UPDATE casino_roulette_rounds "
        "SET status = 'settled', result = ?, settled_at = ? "
        "WHERE id = ? AND status = 'open' RETURNING id",
        (result, time.time() if now is None else now, round_id),
    ).fetchone()
    if claimed is None:
        return None
    for bet in roulette_bets(conn, round_id):
        payout = casino_logic.roulette_payout(
            str(bet["bet_type"]), int(bet["selection"]), result, int(bet["amount"])
        )
        if payout:
            conn.execute(
                "UPDATE casino_roulette_bets SET payout = ? WHERE id = ?",
                (payout, int(bet["id"])),
            )
            pay_out(
                conn, int(bet["guild_id"]), int(bet["user_id"]), payout,
                "roulette", meta={"round_id": round_id, "result": result},
            )
    return roulette_bets(conn, round_id)


def void_roulette_round(
    conn: sqlite3.Connection, round_id: int, *, now: float | None = None
) -> dict[int, int]:
    """Refund every bet on a dead round (channel gone, casino closed).

    Exactly-once via the same status='open' claim. Returns {user_id: total
    refunded}.
    """
    ts = time.time() if now is None else now
    claimed = conn.execute(
        "UPDATE casino_roulette_rounds SET status = 'void', settled_at = ? "
        "WHERE id = ? AND status = 'open' RETURNING guild_id",
        (ts, round_id),
    ).fetchone()
    if claimed is None:
        return {}
    guild_id = int(claimed["guild_id"])
    totals: dict[int, int] = {}
    for bet in roulette_bets(conn, round_id):
        uid = int(bet["user_id"])
        totals[uid] = totals.get(uid, 0) + int(bet["amount"])
    for uid, amount in totals.items():
        refund(
            conn, guild_id, uid, amount, "roulette",
            meta={"round_id": round_id}, now=ts,
        )
    return totals


def open_roulette_rounds(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every open round — the boot re-arm sweep."""
    return conn.execute(
        "SELECT * FROM casino_roulette_rounds WHERE status = 'open'"
    ).fetchall()
