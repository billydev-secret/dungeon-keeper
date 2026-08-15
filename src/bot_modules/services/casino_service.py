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

Blackjack hands, roulette rounds and derby races persist here too, because their
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
from bot_modules.services import casino_logic, pools_logic, pools_metrics
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

GAMES = (
    "coinflip", "slots", "blackjack", "roulette", "derby", "baccarat",
    "dice", "war", "keno",
)


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
    derby_enabled: bool = True
    baccarat_enabled: bool = True
    dice_enabled: bool = True
    war_enabled: bool = True
    keno_enabled: bool = True
    pools_enabled: bool = False
    # Pools runs its own daily market and gets its own channel — a round is
    # a day long, so its panel would otherwise sit pinned above the casino
    # hub all day. 0 = fall back to the casino channel.
    pools_channel_id: int = 0
    # Guild-local hour betting shuts on the day being measured. Late enough
    # that most of the day's mint is visible, early enough that the night's
    # casino play — where the variance lives — is still unwritten.
    pools_close_hour: int = 18
    # Deducted from the whole pool at settle and BURNED. Distinct from
    # jackpot_cut_pct, which is skimmed from each fully-lost stake and fed
    # to a pot that re-mints it. Same number today, different bases.
    pools_takeout_pct: int = 5
    # Which metrics the daily market may draw from, as a comma-separated
    # list of pools_metrics keys. Empty = the whole roster, which is also
    # what every guild gets before an admin ever opens the panel.
    pools_metrics: str = ""
    # An untouched blackjack hand auto-stands after this long.
    blackjack_idle_seconds: int = 180
    # An abandoned private round auto-resolves after this long. NOT a
    # betting deadline — the player paces their own round and resolves it
    # when ready; this is the safety net under a player who bets and walks
    # away, since the stake is already debited and an ephemeral message
    # cannot be repainted once Discord expires its webhook token at 15
    # minutes. Kept comfortably under that so the auto-resolve can still
    # show the player their own result rather than settling invisibly.
    # One knob for all five games, the same way blackjack_idle_seconds
    # covers both blackjack and war.
    round_idle_seconds: int = 600
    # Progressive jackpot: a cut of every fully-lost stake feeds one pot;
    # slots triple-7️⃣ wins max(pot, the flat 120×), then the pot reseeds.
    # The cut is deliberately small (2026-07-25, down from 25): every coin
    # skimmed is a coin the house did NOT destroy — it is escrowed for one
    # future winner. At 25% the first day of real traffic parked 5,211
    # coins (a fifth of the guild's float) in a pot payable to whoever
    # happens to line up three sevens, while the sink the economy actually
    # needed went unfilled. 5% still builds a prize worth chasing at any
    # sane volume; the rest of each lost bet stays burned.
    jackpot_enabled: bool = True
    jackpot_cut_pct: int = 5
    jackpot_seed: int = 100
    # Instant-game wins paying at least this much get a public broadcast
    # in the casino channel (results themselves render ephemerally).
    # 0 = never broadcast.
    broadcast_min_payout: int = 0
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
    "derby_enabled",
    "baccarat_enabled",
    "dice_enabled",
    "war_enabled",
    "keno_enabled",
    "pools_enabled",
    "jackpot_enabled",
]
# Free-text settings — stored and returned verbatim rather than coerced.
_STR_KEYS = ["pools_metrics"]
# Everything else on the dataclass is a plain int.
_INT_KEYS = [
    f.name for f in fields(CasinoSettings)
    if f.name not in _BOOL_KEYS and f.name not in _STR_KEYS
]
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
    for key in _STR_KEYS:
        raw = stored.get(key, "")
        if raw:
            kwargs[key] = raw
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


def pools_channel(settings: CasinoSettings) -> int:
    """Where the daily market lives. Falls back to the casino channel."""
    return settings.pools_channel_id or settings.channel_id


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
    meta: dict[str, object] | None = None,
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

    ``meta`` merges into the ledger row's metadata alongside ``game``.
    Windowed games pass their ``round_id`` so a stake can be tied back to
    the session it belongs to: the economy metric attributes both halves of
    a session to the day it opened, and without the linkage a stake and its
    payout can be booked on opposite sides of midnight (see the plan doc's
    session-day attribution).
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
        # Book the wager against the cap *before* charging for it. The read
        # above runs in autocommit, so two simultaneous bets would otherwise
        # both clear the check and jointly overshoot the only spend limit the
        # casino has. Roulette and double-down already take a claim first;
        # this brings coinflip/slots/blackjack-deal onto the same footing.
        if conn.execute(
            "INSERT INTO casino_daily (guild_id, user_id, local_day, wagered) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id, local_day) "
            "DO UPDATE SET wagered = wagered + excluded.wagered "
            "WHERE wagered + excluded.wagered <= ?",
            (guild_id, user_id, day, amount, settings.daily_wager_cap),
        ).rowcount != 1:
            left = max(0, settings.daily_wager_cap - wagered_today(conn, guild_id, user_id, day))
            return (
                f"That bet would pass your daily casino cap of "
                f"{settings.daily_wager_cap} {unit} — you have {left} left today."
            )
    if not apply_debit(
        conn, guild_id, user_id, amount, STAKE_KIND,
        actor_id=user_id, meta={"game": game, **(meta or {})},
    ):
        if settings.daily_wager_cap:
            # Give the allowance back — an unaffordable bet must not eat into
            # the day's cap.
            conn.execute(
                "UPDATE casino_daily SET wagered = wagered - ? "
                "WHERE guild_id = ? AND user_id = ? AND local_day = ?",
                (amount, guild_id, user_id, day),
            )
        have = get_balance(conn, guild_id, user_id)
        return f"You need {amount} {unit} for that bet — you have {have}."
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
    *, now: float | None = None, settings: CasinoSettings | None = None,
) -> int:
    """Skim the configured cut of a fully-lost stake into the pot.

    Returns the contribution (0 when the jackpot is off, the cut rounds to
    nothing, or the amount is nonpositive). The pot is pure bookkeeping —
    the lost coins were already burned by their ``casino_stake`` debit;
    winning the pot later re-mints this recorded slice of them.
    ``settings`` lets a settlement loop feeding many losses pass one
    preloaded read instead of reloading per bet.
    """
    if lost_amount < 1:
        return 0
    if settings is None:
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


# Every game whose result the channel does not otherwise see lands on the
# hub panel's floor ticker. That used to mean the instant games only,
# because the windowed five recapped publicly — the public recap WAS their
# visibility. Private rounds have no recap, so leaving them off would make
# them genuinely invisible rather than merely quiet, which is the opposite
# of the point: the ticker is where the casino's social texture lives now.
# Pools stays off — its daily market has its own panel and settles there.
TICKER_GAMES = (
    "coinflip", "slots", "blackjack", "war",
    "roulette", "derby", "baccarat", "dice", "keno",
)
# Rows kept per guild — a small multiple of what the hub ever renders, so
# the trim never fights the reader.
TICKER_KEEP = 25


def record_ticker(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    game: str,
    stake: int,
    payout: int,
    *,
    now: float | None = None,
) -> None:
    """Append one floor-ticker row and trim the guild to TICKER_KEEP."""
    conn.execute(
        "INSERT INTO casino_ticker (guild_id, user_id, game, stake, payout, ts) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (guild_id, user_id, game, stake, payout,
         time.time() if now is None else now),
    )
    conn.execute(
        "DELETE FROM casino_ticker WHERE guild_id = ? AND id NOT IN ("
        "SELECT id FROM casino_ticker WHERE guild_id = ? "
        "ORDER BY id DESC LIMIT ?)",
        (guild_id, guild_id, TICKER_KEEP),
    )


def recent_ticker(
    conn: sqlite3.Connection, guild_id: int, limit: int = 6
) -> list[sqlite3.Row]:
    """Newest-first recent instant-game plays for the hub's ticker."""
    return conn.execute(
        "SELECT user_id, game, stake, payout FROM casino_ticker "
        "WHERE guild_id = ? ORDER BY id DESC LIMIT ?",
        (guild_id, limit),
    ).fetchall()


# Broadcast-clearing payouts kept per guild for the top-tier percentile
# (migration 162). Only ANNOUNCED wins are banked — see the gate in
# ``record_play``. Ranking every win instead put the mark below the broadcast
# bar, because the overwhelming majority of casino wins are small pair
# payouts: prod's average stake is 36 coins and its average win returns 71.
WIN_HISTORY_KEEP = 1000
# Below this many banked wins the percentile is refused outright — no ping,
# whatever the payout. A fresh guild must not @here its first win because the
# sample of one made it the top 3%. Sized against the announced-win rate
# rather than the total win rate: prod broadcasts a few dozen times a year, so
# a 100-row floor would have taken years to arm.
PING_MIN_SAMPLE = 40
# "Top 3% of winnings", as a percentile rank.
PING_PERCENTILE = 97


def record_win(
    conn: sqlite3.Connection, guild_id: int, payout: int, *, now: float | None = None
) -> None:
    """Append one announced winning payout and trim to WIN_HISTORY_KEEP.

    Called from the cog's broadcast seam — ONE row per public announcement,
    written after the percentile for that announcement has been read. Both
    halves of that matter:

    * *Per announcement, not per settled bet.* A roulette round where the
      player spread five bets that each cleared the bar is one card in the
      channel, and banking it five times would over-weight multi-bet rounds
      in the percentile. It also keeps jackpot spins out, whose big-win card
      is suppressed in favour of the jackpot celebration — banking those
      would pull the mark up with payouts nobody was ranked against.
    * *After the read.* Banking inside the settle transaction put the current
      win into the population it was about to be ranked against, so a payout
      tying the guild's recent maximum always cleared its own mark — and a
      guild whose announced wins cluster tightly above the floor would then
      ping on every single broadcast.

    Stores no user_id on purpose — see migration 162. This table answers "how
    big is an announced win around here lately" and nothing else.
    """
    conn.execute(
        "INSERT INTO casino_win_history (guild_id, payout, ts) VALUES (?, ?, ?)",
        (guild_id, payout, time.time() if now is None else now),
    )
    conn.execute(
        "DELETE FROM casino_win_history WHERE guild_id = ? AND id NOT IN ("
        "SELECT id FROM casino_win_history WHERE guild_id = ? "
        "ORDER BY id DESC LIMIT ?)",
        (guild_id, guild_id, WIN_HISTORY_KEEP),
    )


def win_percentile(
    conn: sqlite3.Connection, guild_id: int, percentile: int = PING_PERCENTILE
) -> int | None:
    """The payout at ``percentile`` of this guild's recent wins, or None when
    the window is too thin to rank against.

    None is a refusal, not a zero: callers must treat "I can't tell yet" as
    "don't ping", never as "everything qualifies".
    """
    total = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM casino_win_history WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()["n"]
    )
    if total < PING_MIN_SAMPLE:
        return None
    # Size the top band in ROWS and count back from the end, rather than
    # taking an offset forward. `total * 97 // 100` is exact at multiples of
    # 100 but rounds the wrong way below them: at the 40-row sample floor it
    # yields offset 38, leaving two rows above the mark — the top 5%, not 3%,
    # so the smallest guilds (the ones the floor exists to protect) would get
    # the loosest ping bar. At least one row always qualifies.
    band = max(1, total * (100 - percentile) // 100)
    offset = total - band
    row = conn.execute(
        "SELECT payout FROM casino_win_history WHERE guild_id = ? "
        "ORDER BY payout ASC LIMIT 1 OFFSET ?",
        (guild_id, offset),
    ).fetchone()
    return None if row is None else int(row["payout"])


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
    Instant-game plays also land a floor-ticker row here, so the hub's
    Recent action section can never disagree with the stats.
    """
    from bot_modules.core.db_utils import get_tz_offset_hours  # noqa: PLC0415
    from bot_modules.economy.quests import iso_week_for  # noqa: PLC0415

    if game in TICKER_GAMES:
        record_ticker(conn, guild_id, user_id, game, stake, payout, now=now)
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
    day = local_day_for(ts, get_tz_offset_hours(conn, guild_id))
    week = iso_week_for(day)
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
    # Per-guild-local-day net, for the hub's "Today at the tables" standings.
    # Unconditional (casino_daily only exists under a wager cap), so the
    # standings hold even for uncapped guilds; net = returned - wagered.
    conn.execute(
        "INSERT INTO casino_daily_net "
        "(guild_id, user_id, local_day, wagered, returned) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(guild_id, user_id, local_day) DO UPDATE SET "
        "wagered = wagered + excluded.wagered, "
        "returned = returned + excluded.returned",
        (guild_id, user_id, day, stake, payout),
    )
    return streak


def member_casino_stats(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM casino_member_stats WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()


class DailyStanding(NamedTuple):
    """One member's net swing today (net = returned − wagered)."""

    user_id: int
    net: int


def daily_standings(
    conn: sqlite3.Connection, guild_id: int, *, now: float | None = None
) -> tuple[DailyStanding | None, DailyStanding | None]:
    """Today's biggest net winner and biggest net loser for the hub panel.

    Ranks the guild-local day's settled plays by net = returned − wagered
    (refunds/voids never enter record_play, so a handed-back bet never
    sways them). The earner is surfaced only when actually up (net > 0) and
    the loser only when actually down (net < 0): on a day where everyone is
    even or ahead there is simply no loser line, and that same sign gate
    means one member can never fill both slots.
    """
    from bot_modules.core.db_utils import get_tz_offset_hours  # noqa: PLC0415

    ts = time.time() if now is None else now
    day = local_day_for(ts, get_tz_offset_hours(conn, guild_id))
    top = conn.execute(
        "SELECT user_id, returned - wagered AS net FROM casino_daily_net "
        "WHERE guild_id = ? AND local_day = ? "
        "ORDER BY net DESC, returned DESC, user_id LIMIT 1",
        (guild_id, day),
    ).fetchone()
    bottom = conn.execute(
        "SELECT user_id, returned - wagered AS net FROM casino_daily_net "
        "WHERE guild_id = ? AND local_day = ? "
        "ORDER BY net ASC, wagered DESC, user_id LIMIT 1",
        (guild_id, day),
    ).fetchone()
    earner = (
        DailyStanding(int(top["user_id"]), int(top["net"]))
        if top is not None and int(top["net"]) > 0
        else None
    )
    loser = (
        DailyStanding(int(bottom["user_id"]), int(bottom["net"]))
        if bottom is not None and int(bottom["net"]) < 0
        else None
    )
    return earner, loser


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


# The live-hand family (blackjack + war): one settle/idle/boot-sweep
# implementation parameterized by this descriptor, the RoundTables rule
# applied to per-member hands — a hardening fix to the exactly-once
# settle can never land in one game and silently miss the other. Table
# names are trusted module constants, never user input.


class HandTables(NamedTuple):
    game: str   # take_stake / ledger / stats key
    table: str  # live-hand table


BLACKJACK_HANDS = HandTables("blackjack", "casino_blackjack_hands")
WAR_HANDS = HandTables("war", "casino_war_hands")


def _settle_hand(
    conn: sqlite3.Connection,
    t: HandTables,
    hand_id: int,
    payout: int,
    outcome: str,
    *,
    kind: str = PAYOUT_KIND,
    now: float | None = None,
) -> bool:
    """Finalize a live hand and credit its return. False = already settled.

    Exactly-once via the ``settled_at IS NULL`` predicate — an idle
    auto-resolve, a boot sweep and a button resolution can all reach a
    terminal hand, and only the first one pays. The jackpot feeds on the
    LOST PORTION of the stake (war's retreat keeps half; for blackjack,
    whose only sub-stake return is a total loss, this is the same as the
    old payout == 0 rule).
    """
    row = conn.execute(
        f"UPDATE {t.table} SET settled_at = ?, outcome = ? "
        "WHERE id = ? AND settled_at IS NULL RETURNING guild_id, user_id, stake",
        (time.time() if now is None else now, outcome, hand_id),
    ).fetchone()
    if row is None:
        return False
    gid, uid, stake = int(row["guild_id"]), int(row["user_id"]), int(row["stake"])
    if payout >= 1:
        meta: dict[str, object] = {"hand_id": hand_id, "outcome": outcome}
        if kind == PAYOUT_KIND:
            pay_out(conn, gid, uid, payout, t.game, meta=meta)
        else:
            refund(conn, gid, uid, payout, t.game, meta=meta, now=now)
    if kind == PAYOUT_KIND:
        # A real resolution (not a make-whole refund): the lost slice
        # feeds the jackpot, and the play lands in the stats either way.
        if payout < stake:
            feed_jackpot(conn, gid, stake - payout, now=now)
        record_play(conn, gid, uid, t.game, stake, payout, now=now)
    return True


def _idle_live_hands(
    conn: sqlite3.Connection, t: HandTables, older_than: float
) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT * FROM {t.table} "
        "WHERE settled_at IS NULL AND last_action_at < ?",
        (older_than,),
    ).fetchall()


def _refund_live_hands(
    conn: sqlite3.Connection, t: HandTables, *, now: float | None = None
) -> list[sqlite3.Row]:
    """Boot sweep: refund every live hand's full stake (honest reset).

    Returns the swept rows (pre-settlement copies). Exactly-once per hand
    via the settle predicate.
    """
    rows = conn.execute(
        f"SELECT * FROM {t.table} WHERE settled_at IS NULL"
    ).fetchall()
    swept = []
    for row in rows:
        if _settle_hand(
            conn, t, int(row["id"]), int(row["stake"]), "refunded",
            kind=REFUND_KIND, now=now,
        ):
            swept.append(row)
    return swept


def settle_blackjack_hand(
    conn: sqlite3.Connection,
    hand_id: int,
    payout: int,
    outcome: str,
    *,
    kind: str = PAYOUT_KIND,
    now: float | None = None,
) -> bool:
    return _settle_hand(
        conn, BLACKJACK_HANDS, hand_id, payout, outcome, kind=kind, now=now
    )


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
    # Ownership rides in the claim itself. The buttons live on a public message
    # anyone can press, so bumping last_action_at before checking the owner let
    # a stranger reset the idle clock on someone else's hand indefinitely —
    # blocking the auto-stand and stranding the owner's stake (and, under the
    # one-live-hand rule, locking them out of blackjack) until a restart.
    row = conn.execute(
        "UPDATE casino_blackjack_hands SET last_action_at = ? "
        "WHERE id = ? AND settled_at IS NULL AND guild_id = ? AND user_id = ? "
        "RETURNING *",
        (ts, hand_id, guild_id, user_id),
    ).fetchone()
    if row is None:
        # Distinguish "not yours" from "already over" without touching the row.
        other = conn.execute(
            "SELECT settled_at FROM casino_blackjack_hands WHERE id = ?", (hand_id,)
        ).fetchone()
        if other is not None and other["settled_at"] is None:
            return BlackjackStep(err="That's not your hand — deal your own!")
        return BlackjackStep(err="That hand is already finished.")
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

    The blackjack hand (and any pending war decision) settles as refunded;
    the member's bets on any open windowed round are deleted (so the
    resolution can't pay a ghost) and refunded as one credit per game.
    Returns {game: amount} for each game something actually came back from
    (sparse — a game with nothing live has no key).
    """
    out: dict[str, int] = {}
    hand = live_blackjack_hand(conn, guild_id, user_id)
    if hand is not None and settle_blackjack_hand(
        conn, int(hand["id"]), int(hand["stake"]), "refunded",
        kind=REFUND_KIND, now=now,
    ):
        out["blackjack"] = int(hand["stake"])
    war_hand = live_war_hand(conn, guild_id, user_id)
    if war_hand is not None and _settle_war_hand(
        conn, int(war_hand["id"]), int(war_hand["stake"]), "refunded",
        kind=REFUND_KIND, now=now,
    ):
        out["war"] = int(war_hand["stake"])
    for t in ALL_ROUND_TABLES:
        # The DELETE itself is the claim: its status='open' predicate is
        # re-evaluated inside OUR write transaction, so a settle that
        # landed after any earlier read simply leaves nothing to delete —
        # a settled bet can never ALSO be refunded (the double-pay race a
        # separate SELECT would open), and refunds pay only what was
        # actually removed.
        open_pred, extra = "status = 'open'", ()
        if t.leavers_until_close:
            open_pred += " AND closes_at > ?"
            extra = (time.time() if now is None else now,)
        removed = conn.execute(
            f"DELETE FROM {t.bets} WHERE guild_id = ? AND user_id = ? "
            f"AND round_id IN (SELECT id FROM {t.rounds} WHERE {open_pred}) "
            "RETURNING amount",
            (guild_id, user_id, *extra),
        ).fetchall()
        total = sum(int(r["amount"]) for r in removed)
        if total:
            refund(
                conn, guild_id, user_id, total, t.game,
                meta={"left_guild": True}, now=now,
            )
            out[t.game] = total
    return out


def refund_live_blackjack_hands(
    conn: sqlite3.Connection, *, now: float | None = None
) -> list[sqlite3.Row]:
    return _refund_live_hands(conn, BLACKJACK_HANDS, now=now)


def idle_live_blackjack_hands(
    conn: sqlite3.Connection, older_than: float
) -> list[sqlite3.Row]:
    """Live hands untouched since ``older_than`` — the auto-stand sweep."""
    return _idle_live_hands(conn, BLACKJACK_HANDS, older_than)


# ── windowed rounds (roulette + derby: ONE implementation) ─────────────
# Both games are the same machine — a communal betting window per channel,
# bets debited at placement, exactly-once resolution via the status='open'
# claim — differing only in table pair, bet columns and payout math. The
# money-safety logic lives once, parameterized by this descriptor, so a
# hardening fix can never land in one game and silently miss the other.
# Table/column names below are trusted module constants, never user input.


class RoundTables(NamedTuple):
    game: str          # take_stake / ledger / settings key
    rounds: str        # rounds table
    bets: str          # bets table
    result_col: str    # "result" (roulette) / "winner" (derby)
    closed_error: str  # member-facing window-closed message
    # Leaver refunds normally pull a departing member's bets from any open
    # round. That is safe when the window is 45-60s, because "open" and
    # "still accepting bets" mean the same thing for all but an instant.
    # Pools sits open for hours AFTER betting shuts, and its payouts are
    # pro-rata — so pulling a stake out of a closed pool would silently
    # change what every remaining bettor is owed. When True, the sweep
    # stops at the betting close and the stake settles normally.
    leavers_until_close: bool = False
    # Whether a fully-lost stake skims into the progressive jackpot. True for
    # every paytable game; false for Pools, where the losing stakes ARE the
    # winners' payout, so skimming them would pay the pot out of money
    # already owed to somebody — and Pools burns its takeout rather than
    # feeding a pot that re-mints what it holds.
    feeds_jackpot: bool = True


ROULETTE_TABLES = RoundTables(
    "roulette", "casino_roulette_rounds", "casino_roulette_bets",
    "result", "Betting on that round has closed.",
)
DERBY_TABLES = RoundTables(
    "derby", "casino_race_rounds", "casino_race_bets",
    "winner", "Betting on that race has closed.",
)
BACCARAT_TABLES = RoundTables(
    "baccarat", "casino_baccarat_rounds", "casino_baccarat_bets",
    "result", "Betting on that hand has closed.",
)
DICE_TABLES = RoundTables(
    "dice", "casino_dice_rounds", "casino_dice_bets",
    "result", "Betting on that roll has closed.",
)
KENO_TABLES = RoundTables(
    "keno", "casino_keno_rounds", "casino_keno_bets",
    "result", "Tickets for that draw have closed.",
)
POOLS_TABLES = RoundTables(
    "pools", "casino_pools_rounds", "casino_pools_bets",
    "result", "Betting on today's market has closed.",
    leavers_until_close=True, feeds_jackpot=False,
)
# Every windowed game, for cross-game sweeps (leaver refunds). A new game
# added here is automatically covered — never enumerate the tables by hand
# at a call site.
ALL_ROUND_TABLES = (
    ROULETTE_TABLES, DERBY_TABLES, BACCARAT_TABLES, DICE_TABLES, KENO_TABLES,
    POOLS_TABLES,
)


def _live_round(
    conn: sqlite3.Connection, t: RoundTables, channel_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT * FROM {t.rounds} WHERE channel_id = ? AND status = 'open'",
        (channel_id,),
    ).fetchone()


def _live_player_round(
    conn: sqlite3.Connection, t: RoundTables, guild_id: int, user_id: int
) -> sqlite3.Row | None:
    """The player's own live round, if they have one.

    The private-round sibling of ``_live_round``: a round belongs to one
    player now, so "is there already one open" is a question about them,
    not about the channel. Pools keeps the channel-scoped read — its
    market is genuinely communal.
    """
    return conn.execute(
        f"SELECT * FROM {t.rounds} "
        "WHERE guild_id = ? AND user_id = ? AND status = 'open'",
        (guild_id, user_id),
    ).fetchone()


def _get_round(
    conn: sqlite3.Connection, t: RoundTables, round_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT * FROM {t.rounds} WHERE id = ?", (round_id,)
    ).fetchone()


def _open_round(
    conn: sqlite3.Connection,
    t: RoundTables,
    guild_id: int,
    channel_id: int,
    window_seconds: int,
    *,
    user_id: int = 0,
    now: float | None = None,
) -> int | None:
    """Open a round; None if one is already live for the same owner.

    The partial unique index makes the one-open-round rule race-proof; the
    pre-check keeps the common path exception-free.

    ``user_id`` names the player the round belongs to, and with it
    ``window_seconds`` is the abandonment TTL rather than a betting
    deadline (migration 158). The default of 0 is the pre-158 communal
    shape — one anonymous round per guild — which is what the not-yet-
    switched cog still opens, and which the index treats identically
    because the casino is confined to one channel per guild.
    """
    if user_id:
        if _live_player_round(conn, t, guild_id, user_id) is not None:
            return None
    elif _live_round(conn, t, channel_id) is not None:
        return None
    ts = time.time() if now is None else now
    cur = conn.execute(
        f"INSERT INTO {t.rounds} "
        "(guild_id, channel_id, user_id, opened_at, closes_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (guild_id, channel_id, user_id, ts, ts + window_seconds),
    )
    return int(cur.lastrowid or 0)


def _set_round_message(
    conn: sqlite3.Connection, t: RoundTables, round_id: int, message_id: int
) -> None:
    conn.execute(
        f"UPDATE {t.rounds} SET message_id = ? WHERE id = ?",
        (message_id, round_id),
    )


def _place_bet(
    conn: sqlite3.Connection,
    t: RoundTables,
    rnd: sqlite3.Row | None,
    round_id: int,
    user_id: int,
    columns: dict[str, int | str],
    amount: int,
    *,
    now: float | None = None,
) -> str | None:
    """Debit and record one bet. Returns member-facing error or None.

    ``rnd`` is the caller's pre-check read (via its game's public getter,
    so tests can stub it). That read ran in autocommit — a buzzer-beater
    bet can race the settle timer, whose claim + bet-read commit between
    the check and our debit, leaving a stake nothing ever pays or refunds.
    The guarded no-op UPDATE is the first write of OUR transaction: it
    serializes against the settler, and a round it already claimed makes
    us miss here, before any money moves.
    """
    ts = time.time() if now is None else now
    if rnd is None or str(rnd["status"]) != "open" or ts >= float(rnd["closes_at"]):
        return t.closed_error
    claimed = conn.execute(
        f"UPDATE {t.rounds} SET message_id = message_id "
        "WHERE id = ? AND status = 'open' AND closes_at > ? RETURNING id",
        (round_id, ts),
    ).fetchone()
    if claimed is None:
        return t.closed_error
    err = take_stake(
        conn, int(rnd["guild_id"]), user_id, amount, t.game,
        now=now, meta={"round_id": round_id},
    )
    if err is not None:
        return err
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO {t.bets} "
        f"(round_id, guild_id, user_id, {names}, amount, created_at) "
        f"VALUES (?, ?, ?, {marks}, ?, ?)",
        (round_id, int(rnd["guild_id"]), user_id, *columns.values(), amount, ts),
    )
    return None


def _round_bets(
    conn: sqlite3.Connection, t: RoundTables, round_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT * FROM {t.bets} WHERE round_id = ? ORDER BY id", (round_id,)
    ).fetchall()


def _per_bet(payout_fn):
    """Lift a per-bet paytable function to ``_settle_round``'s contract.

    The five paytable games compute each payout from that bet alone, so
    they read better written per-bet; only Pools genuinely needs the whole
    round. This keeps one settle hook with one arity rather than making the
    callable's shape depend on which other argument was passed.
    """
    return lambda bets, result: [payout_fn(bet, result) for bet in bets]


def _settle_round(
    conn: sqlite3.Connection,
    t: RoundTables,
    round_id: int,
    result: int | str,  # a number (roulette/derby) or JSON (baccarat's coup)
    payouts_fn,
    *,
    now: float | None = None,
) -> list[dict] | None:
    """Resolution: claim the round, pay every winning bet.

    None = someone else already settled (or voided) it — exactly-once via
    the status='open' claim, taken BEFORE any credit moves (the raffle-draw
    rule). Returns the bets as dicts with ``payout`` filled in for the
    recap — the rows are read once, settings once (not per losing bet),
    and the winner updates land as one executemany.

    ``payouts_fn(bets, result) -> list[int]`` returns one payout per bet,
    index-aligned with ``bets``. It takes the WHOLE round rather than one
    bet at a time because a parimutuel pool's payouts each depend on every
    other stake in the round — Pools computes the split in one call, while
    the five paytable games just map their per-bet function over the list.
    Whether the round's losing stakes feed the jackpot is a per-game trait
    and lives on ``RoundTables.feeds_jackpot``, beside every other "how does
    this game differ" fact.
    """
    claimed = conn.execute(
        f"UPDATE {t.rounds} "
        f"SET status = 'settled', {t.result_col} = ?, settled_at = ? "
        "WHERE id = ? AND status = 'open' RETURNING guild_id",
        (result, time.time() if now is None else now, round_id),
    ).fetchone()
    if claimed is None:
        return None
    settings = load_casino_settings(conn, int(claimed["guild_id"]))
    bets = [dict(b) for b in _round_bets(conn, t, round_id)]
    payouts = payouts_fn(bets, result)
    winner_updates: list[tuple[int, int]] = []
    for bet, payout in zip(bets, payouts, strict=True):
        amount = int(bet["amount"])
        payout = int(payout)
        bet["payout"] = payout
        if payout:
            winner_updates.append((payout, int(bet["id"])))
            pay_out(
                conn, int(bet["guild_id"]), int(bet["user_id"]), payout,
                t.game, meta={"round_id": round_id, t.result_col: result},
            )
        elif t.feeds_jackpot:
            feed_jackpot(
                conn, int(bet["guild_id"]), amount, now=now, settings=settings
            )
        record_play(
            conn, int(bet["guild_id"]), int(bet["user_id"]), t.game,
            amount, payout, now=now,
        )
    if winner_updates:
        conn.executemany(
            f"UPDATE {t.bets} SET payout = ? WHERE id = ?", winner_updates
        )
    return bets


def _void_round(
    conn: sqlite3.Connection, t: RoundTables, round_id: int,
    *, now: float | None = None,
) -> dict[int, int] | None:
    """Refund every bet on a dead round (channel gone, casino closed).

    Exactly-once via the same status='open' claim. Returns {user_id: total
    refunded}, or **None when the claim was lost** — someone else settled or
    voided it first. An empty dict means we did void it and there was simply
    nothing staked; without that distinction a caller has to read the row
    back to find out which happened.
    """
    ts = time.time() if now is None else now
    claimed = conn.execute(
        f"UPDATE {t.rounds} SET status = 'void', settled_at = ? "
        "WHERE id = ? AND status = 'open' RETURNING guild_id",
        (ts, round_id),
    ).fetchone()
    if claimed is None:
        return None
    guild_id = int(claimed["guild_id"])
    totals: dict[int, int] = {}
    for bet in _round_bets(conn, t, round_id):
        uid = int(bet["user_id"])
        totals[uid] = totals.get(uid, 0) + int(bet["amount"])
    for uid, amount in totals.items():
        refund(
            conn, guild_id, uid, amount, t.game,
            meta={"round_id": round_id}, now=ts,
        )
    return totals


def _open_rounds(conn: sqlite3.Connection, t: RoundTables) -> list[sqlite3.Row]:
    """Every open round — the boot re-arm sweep."""
    return conn.execute(
        f"SELECT * FROM {t.rounds} WHERE status = 'open'"
    ).fetchall()


# The five private-round games. Pools is excluded on purpose: its market is
# a day long and genuinely communal, so a restart must NOT hand every
# bettor their stake back — it settles from the ledger as normal.
PRIVATE_ROUND_TABLES = (
    ROULETTE_TABLES, DERBY_TABLES, BACCARAT_TABLES, DICE_TABLES, KENO_TABLES,
)


def refund_live_rounds(
    conn: sqlite3.Connection, *, now: float | None = None
) -> dict[str, dict[int, int]]:
    """Boot sweep: refund every unresolved private round (honest reset).

    The blackjack/war sibling, and it exists for the same reason. A private
    round renders in an ephemeral message, and a restart kills the webhook
    token that message is editable through — so the round can never be
    shown to its player again. Resolving it anyway would move money against
    a result nobody can see; refunding is the honest reset, and the
    register feed's casino_refund entry is the player-facing notice.

    This also absorbs the migration-158 deploy edge: any round left open by
    the pre-158 communal cog is owned by nobody (user_id 0), and gets
    handed back here rather than stranding a stake behind an index it no
    longer matches.

    Returns {game: {user_id: refunded}}, games with nothing swept omitted.
    Exactly-once per round via ``_void_round``'s status='open' claim, so
    replaying the sweep is free.
    """
    out: dict[str, dict[int, int]] = {}
    for t in PRIVATE_ROUND_TABLES:
        totals: dict[int, int] = {}
        for rnd in _open_rounds(conn, t):
            refunded = _void_round(conn, t, int(rnd["id"]), now=now)
            if not refunded:  # claim lost, or nothing was staked
                continue
            for uid, amount in refunded.items():
                totals[uid] = totals.get(uid, 0) + amount
        if totals:
            out[t.game] = totals
    return out


# ── roulette rounds (thin wrappers over the shared machine) ────────────


def live_roulette_round(
    conn: sqlite3.Connection, channel_id: int
) -> sqlite3.Row | None:
    return _live_round(conn, ROULETTE_TABLES, channel_id)


def live_roulette_player_round(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> sqlite3.Row | None:
    return _live_player_round(conn, ROULETTE_TABLES, guild_id, user_id)


def get_roulette_round(conn: sqlite3.Connection, round_id: int) -> sqlite3.Row | None:
    return _get_round(conn, ROULETTE_TABLES, round_id)


def open_roulette_round(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    window_seconds: int,
    *,
    user_id: int = 0,
    now: float | None = None,
) -> int | None:
    return _open_round(
        conn, ROULETTE_TABLES, guild_id, channel_id, window_seconds,
        user_id=user_id, now=now,
    )


def set_roulette_message(
    conn: sqlite3.Connection, round_id: int, message_id: int
) -> None:
    _set_round_message(conn, ROULETTE_TABLES, round_id, message_id)


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
    if bet_type not in casino_logic.ROULETTE_BET_TYPES:
        raise ValueError(f"unknown roulette bet type: {bet_type}")
    return _place_bet(
        conn, ROULETTE_TABLES, get_roulette_round(conn, round_id), round_id,
        user_id, {"bet_type": bet_type, "selection": selection}, amount, now=now,
    )


def roulette_bets(conn: sqlite3.Connection, round_id: int) -> list[sqlite3.Row]:
    return _round_bets(conn, ROULETTE_TABLES, round_id)


def _roulette_payout_for(bet: dict, result: int) -> int:
    return casino_logic.roulette_payout(
        str(bet["bet_type"]), int(bet["selection"]), result, int(bet["amount"])
    )


def settle_roulette_round(
    conn: sqlite3.Connection,
    round_id: int,
    result: int,
    *,
    now: float | None = None,
) -> list[dict] | None:
    return _settle_round(
        conn, ROULETTE_TABLES, round_id, result,
        _per_bet(_roulette_payout_for), now=now
    )


def void_roulette_round(
    conn: sqlite3.Connection, round_id: int, *, now: float | None = None
) -> dict[int, int]:
    return _void_round(conn, ROULETTE_TABLES, round_id, now=now) or {}


def open_roulette_rounds(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return _open_rounds(conn, ROULETTE_TABLES)


# ── derby races (docs/plans/casino-derby.md — same wrappers) ───────────


def live_race_round(
    conn: sqlite3.Connection, channel_id: int
) -> sqlite3.Row | None:
    return _live_round(conn, DERBY_TABLES, channel_id)


def live_race_player_round(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> sqlite3.Row | None:
    return _live_player_round(conn, DERBY_TABLES, guild_id, user_id)


def get_race_round(conn: sqlite3.Connection, round_id: int) -> sqlite3.Row | None:
    return _get_round(conn, DERBY_TABLES, round_id)


def open_race_round(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    window_seconds: int,
    *,
    user_id: int = 0,
    now: float | None = None,
) -> int | None:
    return _open_round(
        conn, DERBY_TABLES, guild_id, channel_id, window_seconds,
        user_id=user_id, now=now,
    )


def set_race_message(
    conn: sqlite3.Connection, round_id: int, message_id: int
) -> None:
    _set_round_message(conn, DERBY_TABLES, round_id, message_id)


def place_race_bet(
    conn: sqlite3.Connection,
    round_id: int,
    user_id: int,
    runner: int,
    amount: int,
    *,
    now: float | None = None,
) -> str | None:
    if not 0 <= runner < len(casino_logic.DERBY_FIELD):
        raise ValueError(f"unknown derby runner: {runner}")
    return _place_bet(
        conn, DERBY_TABLES, get_race_round(conn, round_id), round_id,
        user_id, {"runner": runner}, amount, now=now,
    )


def race_bets(conn: sqlite3.Connection, round_id: int) -> list[sqlite3.Row]:
    return _round_bets(conn, DERBY_TABLES, round_id)


def _derby_payout_for(bet: dict, winner: int) -> int:
    return casino_logic.derby_payout(
        int(bet["runner"]), winner, int(bet["amount"])
    )


def settle_race_round(
    conn: sqlite3.Connection,
    round_id: int,
    winner: int,
    *,
    now: float | None = None,
) -> list[dict] | None:
    return _settle_round(
        conn, DERBY_TABLES, round_id, winner,
        _per_bet(_derby_payout_for), now=now
    )


def void_race_round(
    conn: sqlite3.Connection, round_id: int, *, now: float | None = None
) -> dict[int, int]:
    return _void_round(conn, DERBY_TABLES, round_id, now=now) or {}


def open_race_rounds(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return _open_rounds(conn, DERBY_TABLES)


# ── baccarat coups (Stage 1a of casino-classics — same wrappers) ───────


def live_baccarat_round(
    conn: sqlite3.Connection, channel_id: int
) -> sqlite3.Row | None:
    return _live_round(conn, BACCARAT_TABLES, channel_id)


def live_baccarat_player_round(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> sqlite3.Row | None:
    return _live_player_round(conn, BACCARAT_TABLES, guild_id, user_id)


def get_baccarat_round(
    conn: sqlite3.Connection, round_id: int
) -> sqlite3.Row | None:
    return _get_round(conn, BACCARAT_TABLES, round_id)


def open_baccarat_round(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    window_seconds: int,
    *,
    user_id: int = 0,
    now: float | None = None,
) -> int | None:
    return _open_round(
        conn, BACCARAT_TABLES, guild_id, channel_id, window_seconds,
        user_id=user_id, now=now,
    )


def set_baccarat_message(
    conn: sqlite3.Connection, round_id: int, message_id: int
) -> None:
    _set_round_message(conn, BACCARAT_TABLES, round_id, message_id)


def place_baccarat_bet(
    conn: sqlite3.Connection,
    round_id: int,
    user_id: int,
    side: str,
    amount: int,
    *,
    now: float | None = None,
) -> str | None:
    if side not in casino_logic.BACCARAT_SIDES:
        raise ValueError(f"unknown baccarat side: {side}")
    return _place_bet(
        conn, BACCARAT_TABLES, get_baccarat_round(conn, round_id), round_id,
        user_id, {"side": side}, amount, now=now,
    )


def baccarat_bets(conn: sqlite3.Connection, round_id: int) -> list[sqlite3.Row]:
    return _round_bets(conn, BACCARAT_TABLES, round_id)


def settle_baccarat_round(
    conn: sqlite3.Connection,
    round_id: int,
    player: list[str],
    banker: list[str],
    *,
    now: float | None = None,
) -> list[dict] | None:
    """Settle the coup against the dealt hands. Unlike roulette's single
    number, the outcome is the cards themselves — they persist as JSON in
    the round's result column so a recap can always re-render the coup."""
    result = json.dumps({"player": player, "banker": banker})

    def payout_for(bet: dict, _result: str) -> int:
        return casino_logic.baccarat_payout(
            str(bet["side"]), player, banker, int(bet["amount"])
        )

    return _settle_round(
        conn, BACCARAT_TABLES, round_id, result, _per_bet(payout_for), now=now
    )


def void_baccarat_round(
    conn: sqlite3.Connection, round_id: int, *, now: float | None = None
) -> dict[int, int]:
    return _void_round(conn, BACCARAT_TABLES, round_id, now=now) or {}


def open_baccarat_rounds(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return _open_rounds(conn, BACCARAT_TABLES)


# ── dice rolls (casino-classics Stage 1b — same wrappers) ──────────────


def live_dice_round(
    conn: sqlite3.Connection, channel_id: int
) -> sqlite3.Row | None:
    return _live_round(conn, DICE_TABLES, channel_id)


def live_dice_player_round(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> sqlite3.Row | None:
    return _live_player_round(conn, DICE_TABLES, guild_id, user_id)


def get_dice_round(conn: sqlite3.Connection, round_id: int) -> sqlite3.Row | None:
    return _get_round(conn, DICE_TABLES, round_id)


def open_dice_round(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    window_seconds: int,
    *,
    user_id: int = 0,
    now: float | None = None,
) -> int | None:
    return _open_round(
        conn, DICE_TABLES, guild_id, channel_id, window_seconds,
        user_id=user_id, now=now,
    )


def set_dice_message(
    conn: sqlite3.Connection, round_id: int, message_id: int
) -> None:
    _set_round_message(conn, DICE_TABLES, round_id, message_id)


def place_dice_bet(
    conn: sqlite3.Connection,
    round_id: int,
    user_id: int,
    bet_type: str,
    amount: int,
    *,
    now: float | None = None,
) -> str | None:
    if bet_type not in casino_logic.SICBO_BET_TYPES:
        raise ValueError(f"unknown dice bet type: {bet_type}")
    return _place_bet(
        conn, DICE_TABLES, get_dice_round(conn, round_id), round_id,
        user_id, {"bet_type": bet_type}, amount, now=now,
    )


def dice_bets(conn: sqlite3.Connection, round_id: int) -> list[sqlite3.Row]:
    return _round_bets(conn, DICE_TABLES, round_id)


def settle_dice_round(
    conn: sqlite3.Connection,
    round_id: int,
    dice: tuple[int, int, int],
    *,
    now: float | None = None,
) -> list[dict] | None:
    """Settle the roll. The three dice persist as JSON in the round's
    result column (the outcome is the dice, not their sum)."""
    result = json.dumps(list(dice))

    def payout_for(bet: dict, _result: str) -> int:
        return casino_logic.sicbo_payout(
            str(bet["bet_type"]), dice, int(bet["amount"])
        )

    return _settle_round(
        conn, DICE_TABLES, round_id, result, _per_bet(payout_for), now=now
    )


def void_dice_round(
    conn: sqlite3.Connection, round_id: int, *, now: float | None = None
) -> dict[int, int]:
    return _void_round(conn, DICE_TABLES, round_id, now=now) or {}


def open_dice_rounds(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return _open_rounds(conn, DICE_TABLES)


# ── keno draws (casino-classics Stage 1d — same wrappers) ──────────────


def live_keno_round(
    conn: sqlite3.Connection, channel_id: int
) -> sqlite3.Row | None:
    return _live_round(conn, KENO_TABLES, channel_id)


def live_keno_player_round(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> sqlite3.Row | None:
    return _live_player_round(conn, KENO_TABLES, guild_id, user_id)


def get_keno_round(conn: sqlite3.Connection, round_id: int) -> sqlite3.Row | None:
    return _get_round(conn, KENO_TABLES, round_id)


def open_keno_round(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    window_seconds: int,
    *,
    user_id: int = 0,
    now: float | None = None,
) -> int | None:
    return _open_round(
        conn, KENO_TABLES, guild_id, channel_id, window_seconds,
        user_id=user_id, now=now,
    )


def set_keno_message(
    conn: sqlite3.Connection, round_id: int, message_id: int
) -> None:
    _set_round_message(conn, KENO_TABLES, round_id, message_id)


def place_keno_ticket(
    conn: sqlite3.Connection,
    round_id: int,
    user_id: int,
    spots: int,
    amount: int,
    *,
    now: float | None = None,
) -> str | list[int]:
    """Quick-pick and place one ticket. A str is the member-facing error;
    a list is the picked numbers (shown back in the confirmation)."""
    if spots not in casino_logic.KENO_TIERS:
        raise ValueError(f"unknown keno tier: {spots} spots")
    picks = casino_logic.keno_quick_pick(spots)
    err = _place_bet(
        conn, KENO_TABLES, get_keno_round(conn, round_id), round_id,
        user_id, {"spots": json.dumps(picks)}, amount, now=now,
    )
    return err if err is not None else picks


def keno_bets(conn: sqlite3.Connection, round_id: int) -> list[sqlite3.Row]:
    return _round_bets(conn, KENO_TABLES, round_id)


def settle_keno_round(
    conn: sqlite3.Connection,
    round_id: int,
    drawn: list[int],
    *,
    now: float | None = None,
) -> list[dict] | None:
    """Settle the draw. The 20 drawn numbers persist as JSON in the
    round's result column."""
    result = json.dumps(drawn)

    def payout_for(bet: dict, _result: str) -> int:
        return casino_logic.keno_payout(
            json.loads(str(bet["spots"])), drawn, int(bet["amount"])
        )

    return _settle_round(
        conn, KENO_TABLES, round_id, result, _per_bet(payout_for), now=now
    )


def void_keno_round(
    conn: sqlite3.Connection, round_id: int, *, now: float | None = None
) -> dict[int, int]:
    return _void_round(conn, KENO_TABLES, round_id, now=now) or {}


def open_keno_rounds(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return _open_rounds(conn, KENO_TABLES)


# ── pools (casino-classics Stage 2 — the parimutuel market) ────────────
#
# Same windowed machine as the five games above, with two differences that
# both fall out of the round being a day long instead of 45 seconds:
# betting shuts at closes_at but the round stays 'open' until the measured
# day is over and can be settled, and payouts are pro-rata over the whole
# pool rather than per-bet off a paytable. The metric and line live in
# pools_service; the maths lives in pools_logic.


def live_pools_round(
    conn: sqlite3.Connection, channel_id: int
) -> sqlite3.Row | None:
    return _live_round(conn, POOLS_TABLES, channel_id)


def get_pools_round(conn: sqlite3.Connection, round_id: int) -> sqlite3.Row | None:
    return _get_round(conn, POOLS_TABLES, round_id)


def open_pools_round(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    day: str,
    line: float,
    closes_at: float,
    *,
    metric: str = pools_metrics.ANCHOR,
    now: float | None = None,
) -> int | None:
    """Open the day's market. None = one already exists for that day.

    The line persists on the row rather than being recomputed at settle.
    The outcome is recoverable from the ledger at any later time, so a
    round whose close was missed by hours must still settle against the
    line members actually bet into — not one recomputed from a history
    that has since grown.

    ``metric`` persists for the same reason and one more: the draw has
    moved on by the time yesterday's round settles, so the outcome can only
    be recomputed against the metric this row named (migration 148).
    """
    ts = time.time() if now is None else now
    try:
        cur = conn.execute(
            "INSERT INTO casino_pools_rounds "
            "(guild_id, channel_id, local_day, line, opened_at, closes_at, "
            " metric) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, channel_id, day, float(line), ts, closes_at, metric),
        )
    except sqlite3.IntegrityError:
        return None
    return int(cur.lastrowid or 0) or None


def set_pools_message(
    conn: sqlite3.Connection, round_id: int, message_id: int
) -> None:
    _set_round_message(conn, POOLS_TABLES, round_id, message_id)


def place_pools_bet(
    conn: sqlite3.Connection,
    round_id: int,
    user_id: int,
    side: str,
    amount: int,
    *,
    now: float | None = None,
) -> str | None:
    """Stake on a side. The member-facing error, or None on success."""
    if side not in pools_logic.SIDES:
        raise ValueError(f"unknown pools side: {side!r}")
    return _place_bet(
        conn, POOLS_TABLES, get_pools_round(conn, round_id), round_id,
        user_id, {"side": side}, amount, now=now,
    )


def pools_bets(conn: sqlite3.Connection, round_id: int) -> list[sqlite3.Row]:
    return _round_bets(conn, POOLS_TABLES, round_id)


class PoolsResult(NamedTuple):
    """What settlement did.

    Exactly one of the three outcomes is live at a time: ``bets`` is set
    when the round settled, ``voided`` is True when a one-sided pool was
    refunded (``refunds`` says to whom), and all three are empty when
    another caller had already claimed the round.
    """

    bets: list[dict] | None = None
    takeout: int = 0
    voided: bool = False
    refunds: dict[int, int] | None = None


# Somebody else claimed the round first — nothing settled, nothing refunded.
_POOLS_ALREADY_CLAIMED = PoolsResult()


def settle_pools_round(
    conn: sqlite3.Connection,
    round_id: int,
    result: int,
    *,
    now: float | None = None,
) -> PoolsResult:
    """Settle against the round's stored line.

    A round with an empty side is voided and refunded in full instead of
    settled: a one-sided pool has no counterparty, so there is nothing to
    pay winners *out of*, and taking a cut would be a pure tax on whoever
    turned up. At this server's size those rounds are routine, not
    exceptional.
    """
    rnd = get_pools_round(conn, round_id)
    if rnd is None:
        return _POOLS_ALREADY_CLAIMED
    line = float(rnd["line"])
    if pools_logic.is_void(
        pools_logic.pool_split([dict(b) for b in pools_bets(conn, round_id)])
    ):
        refunds = _void_round(conn, POOLS_TABLES, round_id, now=now)
        if refunds is None:
            return _POOLS_ALREADY_CLAIMED
        return PoolsResult(voided=True, refunds=refunds)

    settings = load_casino_settings(conn, int(rnd["guild_id"]))
    settlement: list[pools_logic.Settlement] = []

    def payouts_for(bets: list[dict], _result: str) -> list[int]:
        s = pools_logic.settle(
            bets, result, line, settings.pools_takeout_pct
        )
        settlement.append(s)
        return s.payouts

    settled = _settle_round(
        conn, POOLS_TABLES, round_id, str(result), payouts_for, now=now
    )
    if settled is None:
        return _POOLS_ALREADY_CLAIMED
    return PoolsResult(bets=settled, takeout=settlement[0].takeout)


def void_pools_round(
    conn: sqlite3.Connection, round_id: int, *, now: float | None = None
) -> dict[int, int]:
    return _void_round(conn, POOLS_TABLES, round_id, now=now) or {}


# ── casino war (casino-classics Stage 1c — blackjack's live-hand shape) ─
#
# 12 of 13 hands settle inside play_war and never persist. The ~1/13 tie
# opens a live decision row (war or retreat) that follows every blackjack
# hand rule: one live decision per member, exactly-once settlement,
# in-transaction claims before money moves, idle auto-resolve, boot-sweep
# refunds, leaver refunds.


class WarStep(NamedTuple):
    """A war play or decision, ready to render. err set = nothing happened."""

    err: str | None = None
    player: str = ""
    dealer: str = ""
    war_player: str | None = None
    war_dealer: str | None = None
    stake: int = 0  # total staked (doubles when war is declared)
    original: int = 0  # the opening bet — what a Play Again should re-stake
    hand_id: int = 0
    outcome: str | None = None  # None = a tie is waiting on the member
    payout: int = 0
    streak: int = 0
    pot_after: int = 0


def _war_state(
    player: str, dealer: str,
    war_player: str | None = None, war_dealer: str | None = None,
) -> str:
    state: dict[str, str] = {"player": player, "dealer": dealer}
    if war_player is not None and war_dealer is not None:
        state["war_player"] = war_player
        state["war_dealer"] = war_dealer
    return json.dumps(state)


def live_war_hand(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM casino_war_hands "
        "WHERE guild_id = ? AND user_id = ? AND settled_at IS NULL",
        (guild_id, user_id),
    ).fetchone()


def get_war_hand(conn: sqlite3.Connection, hand_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM casino_war_hands WHERE id = ?", (hand_id,)
    ).fetchone()


def set_war_message(
    conn: sqlite3.Connection, hand_id: int, message_id: int
) -> None:
    conn.execute(
        "UPDATE casino_war_hands SET message_id = ? WHERE id = ?",
        (message_id, hand_id),
    )


def _settle_war_hand(
    conn: sqlite3.Connection,
    hand_id: int,
    payout: int,
    outcome: str,
    *,
    kind: str = PAYOUT_KIND,
    now: float | None = None,
) -> bool:
    return _settle_hand(
        conn, WAR_HANDS, hand_id, payout, outcome, kind=kind, now=now
    )


def play_war(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int | None,
    user_id: int,
    amount: int,
    *,
    now: float | None = None,
) -> WarStep:
    """One press of the War button: stake, draw, and either settle on the
    spot (differing ranks — no row ever exists) or open the tie's live
    decision row. The one-live-decision index backstops the pre-check the
    way blackjack's does (a raced second play raises IntegrityError and
    rolls back, stake included)."""
    if live_war_hand(conn, guild_id, user_id) is not None:
        return WarStep(
            err="You already have a war decision pending — finish it first."
        )
    err = take_stake(
        conn, guild_id, user_id, amount, "war", now=now, channel_id=channel_id
    )
    if err is not None:
        return WarStep(err=err)
    player, dealer = casino_logic.draw_war_cards()
    payout = casino_logic.war_payout(player, dealer, amount)
    if payout is None:  # a tie — the member chooses war or retreat
        ts = time.time() if now is None else now
        cur = conn.execute(
            "INSERT INTO casino_war_hands "
            "(guild_id, channel_id, user_id, stake, state_json, created_at, "
            "last_action_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                guild_id, channel_id or 0, user_id, amount,
                _war_state(player, dealer), ts, ts,
            ),
        )
        return WarStep(
            player=player, dealer=dealer, stake=amount, original=amount,
            hand_id=int(cur.lastrowid or 0),
        )
    fed = pot_after = 0
    if payout:
        pay_out(
            conn, guild_id, user_id, payout, "war",
            meta={"player": player, "dealer": dealer},
        )
    else:
        fed = feed_jackpot(conn, guild_id, amount, now=now)
        pot_after = get_jackpot(conn, guild_id) if fed else 0
    streak = record_play(conn, guild_id, user_id, "war", amount, payout, now=now)
    return WarStep(
        player=player, dealer=dealer, stake=amount, original=amount,
        outcome="win" if payout else "lose", payout=payout,
        streak=streak, pot_after=pot_after,
    )


def resolve_war_action(
    conn: sqlite3.Connection,
    guild_id: int,
    hand_id: int,
    user_id: int,
    action: str,
    *,
    now: float | None = None,
) -> WarStep:
    """The tie decision — war or retreat, blackjack's claim rules.

    The opening guarded UPDATE claims the live row inside the write
    transaction (an idle auto-resolve or boot sweep settling from another
    connection makes our claim miss) with ownership riding in the claim
    itself. The war raise equals the row's stake, never caller-supplied.
    """
    if action not in casino_logic.WAR_ACTIONS:
        raise ValueError(f"unknown war action: {action}")
    ts = time.time() if now is None else now
    row = conn.execute(
        "UPDATE casino_war_hands SET last_action_at = ? "
        "WHERE id = ? AND settled_at IS NULL AND guild_id = ? AND user_id = ? "
        "RETURNING *",
        (ts, hand_id, guild_id, user_id),
    ).fetchone()
    if row is None:
        other = conn.execute(
            "SELECT settled_at FROM casino_war_hands WHERE id = ?", (hand_id,)
        ).fetchone()
        if other is not None and other["settled_at"] is None:
            return WarStep(err="That's not your battle — play your own!")
        return WarStep(err="That hand is already finished.")
    return _resolve_war_decision(conn, row, action, now=now)


def _resolve_war_decision(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    action: str,
    *,
    now: float | None = None,
) -> WarStep:
    """Shared tail of the member press and the idle auto-resolve — the
    caller has already claimed the live row."""
    gid, uid = int(row["guild_id"]), int(row["user_id"])
    hand_id, original = int(row["id"]), int(row["stake"])
    state = json.loads(str(row["state_json"]))
    player, dealer = str(state["player"]), str(state["dealer"])

    def _finish(stake: int, payout: int, outcome: str, **cards: str) -> WarStep:
        if not _settle_war_hand(conn, hand_id, payout, outcome, now=now):
            return WarStep(err="That hand is already finished.")
        stats = member_casino_stats(conn, gid, uid)
        pot_after = 0
        if payout < stake:  # the settle fed the pot; read what it left
            settings = load_casino_settings(conn, gid)
            if settings.jackpot_enabled:
                pot_after = get_jackpot(conn, gid)
        return WarStep(
            player=player, dealer=dealer, stake=stake, original=original,
            hand_id=hand_id, outcome=outcome, payout=payout,
            streak=int(stats["streak"]) if stats is not None else 0,
            pot_after=pot_after,
            war_player=cards.get("war_player"),
            war_dealer=cards.get("war_dealer"),
        )

    if action == "retreat":
        return _finish(
            original, casino_logic.war_retreat_payout(original), "retreat"
        )
    # war: the raise equals the original stake, debited before the draw.
    err = take_stake(
        conn, gid, uid, original, "war", now=now, enforce_bet_limits=False
    )
    if err is not None:
        return WarStep(err=err)
    conn.execute(
        "UPDATE casino_war_hands SET stake = stake + ? WHERE id = ?",
        (original, hand_id),
    )
    doubled = original * 2
    war_player, war_dealer = casino_logic.draw_war_cards()
    conn.execute(
        "UPDATE casino_war_hands SET state_json = ? WHERE id = ?",
        (_war_state(player, dealer, war_player, war_dealer), hand_id),
    )
    payout = casino_logic.war_raise_payout(war_player, war_dealer, doubled)
    return _finish(
        doubled, payout, "war_win" if payout else "war_lose",
        war_player=war_player, war_dealer=war_dealer,
    )


def resolve_idle_war_hand(
    conn: sqlite3.Connection, hand_id: int, *, now: float | None = None
) -> WarStep | None:
    """The idle sweep's resolve. None = already settled concurrently.

    Defaults to WAR — the strictly better play for the member (97.25% vs
    96.15% RTP) — but falls back to retreat when the raise can't be
    debited (funds or daily cap), so the sweep never errors out and never
    strands the hand.
    """
    row = conn.execute(
        "UPDATE casino_war_hands SET last_action_at = last_action_at "
        "WHERE id = ? AND settled_at IS NULL RETURNING *",
        (hand_id,),
    ).fetchone()
    if row is None:
        return None
    step = _resolve_war_decision(conn, row, "war", now=now)
    if step.err is not None:
        step = _resolve_war_decision(conn, row, "retreat", now=now)
        if step.err is not None:
            return None
    return step


def idle_live_war_hands(
    conn: sqlite3.Connection, older_than: float
) -> list[sqlite3.Row]:
    """Live war decisions untouched since ``older_than`` — the auto sweep."""
    return _idle_live_hands(conn, WAR_HANDS, older_than)


def refund_live_war_hands(
    conn: sqlite3.Connection, *, now: float | None = None
) -> list[sqlite3.Row]:
    """Boot sweep: refund every live decision's stake (honest reset)."""
    return _refund_live_hands(conn, WAR_HANDS, now=now)
