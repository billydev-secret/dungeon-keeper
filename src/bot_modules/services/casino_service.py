"""The casino — settings + the single money choke point.

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

from bot_modules.economy.logic import local_day_bounds, local_day_for
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
    # Progressive jackpot: a cut of every fully-lost stake feeds one pot;
    # slots triple-7️⃣ wins max(pot, the flat 120×), then the pot reseeds.
    jackpot_enabled: bool = True
    jackpot_cut_pct: int = 25
    jackpot_seed: int = 100
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
    "jackpot_enabled",
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


def daily_cap_status(
    conn: sqlite3.Connection, guild_id: int, user_id: int,
    *, now: float | None = None,
) -> tuple[int, int, float]:
    """(wagered today, cap [0 = uncapped], reset timestamp) — the numbers
    the bet modal's label and the My Stats card show, so members never
    learn about the cap from an error."""
    from bot_modules.core.db_utils import get_tz_offset_hours  # noqa: PLC0415

    ts = time.time() if now is None else now
    offset = get_tz_offset_hours(conn, guild_id)
    day = local_day_for(ts, offset)
    _, day_end = local_day_bounds(day, offset)
    cap = load_casino_settings(conn, guild_id).daily_wager_cap
    return wagered_today(conn, guild_id, user_id, day), cap, day_end


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
            _, day_end = local_day_bounds(day, get_tz_offset_hours(conn, guild_id))
            return (
                f"That bet would pass your daily casino cap of "
                f"{settings.daily_wager_cap} {unit} — you have {left} left "
                f"today (resets <t:{int(day_end)}:R>)."
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


# ── progressive jackpot + play stats ───────────────────────────────────


def get_jackpot(conn: sqlite3.Connection, guild_id: int, *, seed: int = 0) -> int:
    """The current pot — ``seed`` when nobody has fed it yet."""
    row = conn.execute(
        "SELECT pot FROM casino_jackpot WHERE guild_id = ?", (guild_id,)
    ).fetchone()
    return int(row["pot"]) if row is not None else seed


def feed_jackpot(
    conn: sqlite3.Connection, guild_id: int, lost_amount: int,
    *, now: float | None = None,
) -> int:
    """Skim the configured cut of a fully-lost stake into the pot.

    Returns the contribution (0 when the jackpot is off, the cut rounds to
    nothing, or the amount is nonpositive). The pot is pure bookkeeping —
    the lost coins were already burned by their ``casino_stake`` debit;
    winning the pot later re-mints this recorded slice of them.
    """
    if lost_amount < 1:
        return 0
    settings = load_casino_settings(conn, guild_id)
    if not settings.jackpot_enabled:
        return 0
    cut = lost_amount * max(0, min(100, settings.jackpot_cut_pct)) // 100
    if cut < 1:
        return 0
    ts = time.time() if now is None else now
    conn.execute(
        # A fresh row starts at seed + cut; an existing row grows by the
        # cut alone (excluded.pot carries the seed, so it must not be the
        # conflict increment).
        "INSERT INTO casino_jackpot (guild_id, pot, updated_at) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET "
        "pot = pot + ?, updated_at = excluded.updated_at",
        (guild_id, settings.jackpot_seed + cut, ts, cut),
    )
    return cut


def claim_jackpot(
    conn: sqlite3.Connection, guild_id: int, winner_id: int,
    *, now: float | None = None,
) -> int:
    """Take the whole pot and reseed it — exactly-once by construction.

    Runs inside the caller's write transaction (the slots spin), so two
    simultaneous triple-7️⃣s serialize: the second finds the reseeded pot.
    Returns the claimed amount (the seed itself if the pot was never fed).
    """
    settings = load_casino_settings(conn, guild_id)
    ts = time.time() if now is None else now
    conn.execute(
        "INSERT OR IGNORE INTO casino_jackpot (guild_id, pot, updated_at) "
        "VALUES (?, ?, ?)",
        (guild_id, settings.jackpot_seed, ts),
    )
    row = conn.execute(
        "UPDATE casino_jackpot SET last_amount = pot, pot = ?, "
        "last_winner_id = ?, last_won_at = ?, updated_at = ? "
        "WHERE guild_id = ? RETURNING last_amount",
        (settings.jackpot_seed, winner_id, ts, ts, guild_id),
    ).fetchone()
    return int(row["last_amount"]) if row is not None else settings.jackpot_seed


def record_play(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    game: str,
    stake: int,
    payout: int,
    *,
    now: float | None = None,
) -> int:
    """Fold one resolved play into lifetime + weekly stats; returns the new
    signed streak (+n win run, −n loss run, 0 after a push).

    Called in the same transaction as the play's settlement. Refunds and
    voids never reach here — a bet the house handed back is not a play.
    """
    from bot_modules.core.db_utils import get_tz_offset_hours  # noqa: PLC0415
    from bot_modules.economy.quests import iso_week_for  # noqa: PLC0415

    streak = casino_logic.next_streak(
        int(
            (
                conn.execute(
                    "SELECT streak FROM casino_member_stats "
                    "WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                ).fetchone()
                or {"streak": 0}
            )["streak"]
        ),
        stake,
        payout,
    )
    won = 1 if payout > stake else 0
    conn.execute(
        "INSERT INTO casino_member_stats "
        "(guild_id, user_id, wagered, returned, plays, wins, biggest_win, "
        "biggest_win_game, streak, best_streak) "
        "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
        "wagered = wagered + excluded.wagered, "
        "returned = returned + excluded.returned, "
        "plays = plays + 1, "
        "wins = wins + excluded.wins, "
        "biggest_win_game = CASE WHEN excluded.biggest_win > biggest_win "
        "THEN excluded.biggest_win_game ELSE biggest_win_game END, "
        "biggest_win = MAX(biggest_win, excluded.biggest_win), "
        "streak = excluded.streak, "
        "best_streak = MAX(best_streak, excluded.streak)",
        (
            guild_id, user_id, stake, payout, won,
            payout if won else 0, game if won else "",
            streak, max(streak, 0),
        ),
    )
    ts = time.time() if now is None else now
    week = iso_week_for(local_day_for(ts, get_tz_offset_hours(conn, guild_id)))
    mult_x100 = payout * 100 // stake if won else 0
    conn.execute(
        "INSERT INTO casino_weekly "
        "(guild_id, iso_week, user_id, wagered, won, biggest_win, "
        "biggest_mult_x100) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(guild_id, iso_week, user_id) DO UPDATE SET "
        "wagered = wagered + excluded.wagered, "
        "won = won + excluded.won, "
        "biggest_win = MAX(biggest_win, excluded.biggest_win), "
        "biggest_mult_x100 = MAX(biggest_mult_x100, excluded.biggest_mult_x100)",
        (
            guild_id, week, user_id, stake, payout,
            payout if won else 0, mult_x100,
        ),
    )
    return streak


def member_casino_stats(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM casino_member_stats WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()


def weekly_table_highlights(
    conn: sqlite3.Connection, guild_id: int, iso_week: str
) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
    """(biggest single win, best multiplier) rows for the week — the
    leaderboard's Night at the Tables block."""
    biggest = conn.execute(
        "SELECT user_id, biggest_win FROM casino_weekly "
        "WHERE guild_id = ? AND iso_week = ? AND biggest_win > 0 "
        "ORDER BY biggest_win DESC, user_id ASC LIMIT 1",
        (guild_id, iso_week),
    ).fetchone()
    luckiest = conn.execute(
        "SELECT user_id, biggest_mult_x100 FROM casino_weekly "
        "WHERE guild_id = ? AND iso_week = ? AND biggest_mult_x100 > 0 "
        "ORDER BY biggest_mult_x100 DESC, user_id ASC LIMIT 1",
        (guild_id, iso_week),
    ).fetchone()
    return biggest, luckiest


class InstantResult(NamedTuple):
    """A settled coinflip/slots play, ready to render."""

    payout: int
    label: str | None = None
    jackpot_won: int = 0
    streak: int = 0
    # On a loss that fed the jackpot: the cut and the pot it left behind,
    # so the result embed can show the loss watering the honeypot.
    fed: int = 0
    pot_after: int = 0


def settle_coinflip(
    conn: sqlite3.Connection, guild_id: int, user_id: int, stake: int,
    call: str, landed: str, *, now: float | None = None,
) -> InstantResult:
    """Pay/feed/record one flip (stake already debited by take_stake)."""
    payout = casino_logic.coinflip_payout(stake) if landed == call else 0
    fed = pot_after = 0
    if payout:
        pay_out(
            conn, guild_id, user_id, payout, "coinflip",
            meta={"call": call, "landed": landed},
        )
    else:
        fed = feed_jackpot(conn, guild_id, stake, now=now)
        pot_after = get_jackpot(conn, guild_id) if fed else 0
    streak = record_play(
        conn, guild_id, user_id, "coinflip", stake, payout, now=now
    )
    return InstantResult(
        payout=payout, streak=streak, fed=fed, pot_after=pot_after
    )


def settle_slots(
    conn: sqlite3.Connection, guild_id: int, user_id: int, stake: int,
    reels: tuple[str, str, str], *, now: float | None = None,
) -> InstantResult:
    """Pay/feed/record one spin; triple-7️⃣ takes max(pot, the flat 120×).

    The claim resets the pot either way — the flat multiplier is a floor
    under an early, barely-fed pot, not a separate prize.
    """
    payout, label = casino_logic.slots_payout(reels, stake)
    jackpot_won = 0
    if reels == (casino_logic.SEVEN,) * 3:
        settings = load_casino_settings(conn, guild_id)
        if settings.jackpot_enabled:
            pot = claim_jackpot(conn, guild_id, user_id, now=now)
            payout = max(pot, payout)
            jackpot_won = payout
    fed = pot_after = 0
    if payout:
        meta: dict[str, object] = {"reels": "".join(reels)}
        if jackpot_won:
            meta["jackpot"] = jackpot_won
        pay_out(conn, guild_id, user_id, payout, "slots", meta=meta)
    else:
        fed = feed_jackpot(conn, guild_id, stake, now=now)
        pot_after = get_jackpot(conn, guild_id) if fed else 0
    streak = record_play(conn, guild_id, user_id, "slots", stake, payout, now=now)
    return InstantResult(
        payout=payout, label=label, jackpot_won=jackpot_won, streak=streak,
        fed=fed, pot_after=pot_after,
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
    gid, uid, stake = int(row["guild_id"]), int(row["user_id"]), int(row["stake"])
    if payout >= 1:
        meta: dict[str, object] = {"hand_id": hand_id, "outcome": outcome}
        if kind == PAYOUT_KIND:
            pay_out(conn, gid, uid, payout, "blackjack", meta=meta)
        else:
            refund(conn, gid, uid, payout, "blackjack", meta=meta, now=now)
    if kind == PAYOUT_KIND:
        # A real resolution (not a make-whole refund): a total loss feeds
        # the jackpot, and the play lands in the stats either way.
        if payout == 0:
            feed_jackpot(conn, gid, stake, now=now)
        record_play(conn, gid, uid, "blackjack", stake, payout, now=now)
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
    streak: int = 0  # post-settle signed run, for the 🔥/🧊 callout
    pot_after: int = 0  # jackpot after a losing hand fed it (0 otherwise)


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
        stats = member_casino_stats(conn, guild_id, user_id)
        pot_after = 0
        if payout == 0:  # the settle fed the pot; read what it left
            settings = load_casino_settings(conn, guild_id)
            if settings.jackpot_enabled:
                pot_after = get_jackpot(conn, guild_id)
        return BlackjackStep(
            player=player, dealer=dealer, stake=stake, doubled=doubled,
            outcome=outcome, payout=payout,
            streak=int(stats["streak"]) if stats is not None else 0,
            pot_after=pot_after,
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
    stats = member_casino_stats(conn, int(row["guild_id"]), int(row["user_id"]))
    return BlackjackStep(
        player=player, dealer=dealer, stake=stake, doubled=bool(row["doubled"]),
        outcome=outcome, payout=payout,
        streak=int(stats["streak"]) if stats is not None else 0,
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
        amount = int(bet["amount"])
        payout = casino_logic.roulette_payout(
            str(bet["bet_type"]), int(bet["selection"]), result, amount
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
        else:
            feed_jackpot(conn, int(bet["guild_id"]), amount, now=now)
        record_play(
            conn, int(bet["guild_id"]), int(bet["user_id"]), "roulette",
            amount, payout, now=now,
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
