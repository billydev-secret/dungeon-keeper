"""Pure decision logic for the Name Your Price cog.

All functions here take and return plain Python values so they're unit-
testable without spinning up Discord. The cog calls these from inside
its modal handlers, vote select callbacks, and round/recap builders;
the Discord glue (sending the message, persisting via ``modify_payload``)
stays in the cog.

High-leverage pieces:

* :func:`parse_price` — normalises the modal's free-text price input
  ("500"/"$1,000"/"5k"/"1.5M"/"2B") into an integer clamped to
  ``[0, 999_999_999]``.
* :func:`format_price` / :func:`price_label` — human-readable rendering
  with optional flavor for extremes (``"$0 (free?!)"`` /
  ``"$999.0M (absolutely not)"``).
* :func:`build_ladder` — sorts ``user_id -> amount`` into the reveal
  ladder; pure so the reveal embed builder can call it.
* :func:`ladder_stats` — spread / median / mean of a ladder's amounts.
* :func:`tally_winners` — collapses ``voter_id -> target_id`` into
  ``(winners, max_votes)``, handling ties by returning every uid that
  shares the top count. Used for both Most Reasonable and Most
  Unhinged.
* :func:`compute_recap_awards` — derives the overall awards
  (Reasonable, Unhinged, Spender, Cheapest, Consistent, Wildest) from
  ``rounds_data`` + ``scores``, returning a dict keyed by award slug
  whose values are ``(label, [uids], detail)`` triples — the cog
  resolves uids to display names.
* :func:`compute_highlight` — picks the round with the widest min/max
  spread, returning ``(round_num, min_amt, max_amt)`` or ``None`` when
  no round has 2+ submissions.
"""

from __future__ import annotations

import statistics
from typing import Any

# Clamp limits for parsed prices.
MIN_PRICE: int = 0
MAX_PRICE: int = 999_999_999

# Suffix multipliers recognized by :func:`parse_price`. Order matters
# only when one suffix is a prefix of another (none currently are), but
# we keep "million"/"billion" before "m"/"b" so a literal "billion"
# input doesn't get stripped to "billio" and misread.
_SUFFIX_MULTIPLIERS: list[tuple[str, int]] = [
    ("million", 1_000_000),
    ("billion", 1_000_000_000),
    ("k", 1_000),
    ("m", 1_000_000),
    ("b", 1_000_000_000),
]


def parse_price(raw: str) -> int | None:
    """Parse user input into an integer dollar amount in ``[0, 999_999_999]``.

    Strips ``$`` and ``,`` separators, then recognizes ``k``/``m``/``b``
    (or ``million``/``billion``) suffixes case-insensitively, then falls
    back to a plain ``float()``-then-truncate parse. Returns ``None`` on
    empty or unparseable input. Out-of-range values are clamped, not
    rejected, so a player typing ``-50`` lands at ``0`` and ``1T`` at
    ``999_999_999`` rather than getting an error popup.
    """
    if raw is None:
        return None
    cleaned = raw.strip().replace("$", "").replace(",", "").strip()
    if not cleaned:
        return None

    lower = cleaned.lower()
    for suffix, mult in _SUFFIX_MULTIPLIERS:
        if lower.endswith(suffix):
            num_part = cleaned[: len(cleaned) - len(suffix)].strip()
            if not num_part:
                return None
            try:
                return max(MIN_PRICE, min(int(float(num_part) * mult), MAX_PRICE))
            except ValueError:
                return None

    try:
        value = int(float(cleaned))
        return max(MIN_PRICE, min(value, MAX_PRICE))
    except ValueError:
        return None


def format_price(amount: int) -> str:
    """Render an integer dollar amount compactly.

    * ``>= 1B`` → ``$1.2B`` (one decimal)
    * ``>= 1M`` → ``$3.4M`` (one decimal)
    * ``>= 1000`` → ``$12,345`` (grouped, no suffix)
    * otherwise → ``$42``
    """
    if amount >= 1_000_000_000:
        return f"${amount / 1_000_000_000:.1f}B"
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.1f}M"
    if amount >= 1_000:
        return f"${amount:,}"
    return f"${amount}"


def price_label(amount: int) -> str:
    """Decorated price string: appends flavor for extremes.

    ``0`` → ``"$0 (free?!)"``; anything ``>= 999_000_000`` →
    ``"$999.0M (absolutely not)"``; everything else falls through to
    plain :func:`format_price`.
    """
    base = format_price(amount)
    if amount == 0:
        return f"{base} (free?!)"
    if amount >= 999_000_000:
        return f"{base} (absolutely not)"
    return base


def build_ladder(prices: dict[int, int]) -> list[tuple[int, int]]:
    """Sort ``user_id -> amount`` lowest to highest, breaking ties by uid.

    Returns a list of ``(user_id, amount)`` pairs. Stable on amount so
    the reveal embed shows submitters in a deterministic order even
    when several players guess the same number.
    """
    return sorted(prices.items(), key=lambda x: (x[1], x[0]))


def ladder_stats(amounts: list[int]) -> dict[str, int] | None:
    """Compute spread / median / mean over a list of amounts.

    Returns ``None`` when ``amounts`` is empty so the embed builder can
    skip the Stats field entirely. ``median`` and ``mean`` are coerced
    to ``int`` to match the existing inline formatting.
    """
    if not amounts:
        return None
    return {
        "low": min(amounts),
        "high": max(amounts),
        "median": int(statistics.median(amounts)),
        "mean": int(statistics.mean(amounts)),
    }


def tally_winners(
    votes_by_user: dict[int, int],
) -> tuple[list[int], int]:
    """Collapse ``voter_id -> target_id`` into ``(winners, max_votes)``.

    ``winners`` lists every uid tied for the highest count; empty when
    no votes were cast. ``max_votes`` is ``0`` in that case. Used for
    both Most Reasonable and Most Unhinged — they share this shape.
    """
    if not votes_by_user:
        return [], 0

    tally: dict[int, int] = {}
    for target in votes_by_user.values():
        tally[target] = tally.get(target, 0) + 1

    max_votes = max(tally.values())
    winners = [uid for uid, v in tally.items() if v == max_votes]
    return winners, max_votes


def _player_prices_from_rounds(
    rounds_data: dict[str, dict[str, Any]],
) -> dict[int, list[int]]:
    """Gather each player's prices across every round.

    Round payloads store ``prices`` as ``{str(uid): amount}`` — this
    helper unflattens that into ``{uid: [amount, ...]}`` keyed by int.
    """
    player_prices: dict[int, list[int]] = {}
    for rnd in rounds_data.values():
        for uid_str, amt in rnd.get("prices", {}).items():
            try:
                uid = int(uid_str)
            except (TypeError, ValueError):
                continue
            player_prices.setdefault(uid, []).append(amt)
    return player_prices


def compute_recap_awards(
    rounds_data: dict[str, dict[str, Any]],
    scores: dict[str, dict[str, int]],
) -> dict[str, tuple[str, list[int], str]]:
    """Derive the overall awards block from accumulated round data.

    Returns a dict keyed by award slug; each value is
    ``(label, [winner_uids], detail)``. The cog resolves the uids to
    display names so this function stays Discord-free.

    Award slugs:

    * ``reasonable`` — players with the most Most-Reasonable round wins
    * ``unhinged`` — players with the most Most-Unhinged round wins
    * ``spender`` — highest mean price across rounds
    * ``cheapest`` — lowest mean price across rounds
    * ``consistent`` — lowest sample stddev (requires ≥2 rounds played)
    * ``wildest`` — highest sample stddev (requires ≥2 rounds played)
    """
    awards: dict[str, tuple[str, list[int], str]] = {}

    reasonable_wins = scores.get("reasonable_wins", {})
    if reasonable_wins:
        max_r = max(reasonable_wins.values())
        winners_uids = [int(uid) for uid, v in reasonable_wins.items() if v == max_r]
        awards["reasonable"] = (
            "🎯 Most Reasonable (overall):",
            winners_uids,
            f"won {max_r} round{'s' if max_r != 1 else ''}",
        )

    unhinged_wins = scores.get("unhinged_wins", {})
    if unhinged_wins:
        max_u = max(unhinged_wins.values())
        winners_uids = [int(uid) for uid, v in unhinged_wins.items() if v == max_u]
        awards["unhinged"] = (
            "🤯 Most Unhinged (overall):",
            winners_uids,
            f"won {max_u} round{'s' if max_u != 1 else ''}",
        )

    player_prices = _player_prices_from_rounds(rounds_data)
    if player_prices:
        avg_prices = {uid: statistics.mean(p) for uid, p in player_prices.items()}
        max_avg_uid = max(avg_prices, key=lambda u: avg_prices[u])
        min_avg_uid = min(avg_prices, key=lambda u: avg_prices[u])
        awards["spender"] = (
            "💸 Biggest Spender:",
            [max_avg_uid],
            f"avg {format_price(int(avg_prices[max_avg_uid]))}",
        )
        awards["cheapest"] = (
            "🆓 Cheapest Date:",
            [min_avg_uid],
            f"avg {format_price(int(avg_prices[min_avg_uid]))}",
        )

    multi_round_players = {uid: p for uid, p in player_prices.items() if len(p) >= 2}
    if multi_round_players:
        std_devs = {uid: statistics.stdev(p) for uid, p in multi_round_players.items()}
        most_consistent = min(std_devs, key=lambda u: std_devs[u])
        wildest = max(std_devs, key=lambda u: std_devs[u])
        awards["consistent"] = (
            "📏 Most Consistent:",
            [most_consistent],
            f"std dev {format_price(int(std_devs[most_consistent]))}",
        )
        awards["wildest"] = (
            "🎢 Wildest Swings:",
            [wildest],
            f"std dev {format_price(int(std_devs[wildest]))}",
        )

    return awards


def compute_highlight(
    rounds_data: dict[str, dict[str, Any]],
) -> tuple[str, int, int] | None:
    """Pick the round with the widest min→max spread.

    Returns ``(round_num_str, min_amount, max_amount)`` or ``None`` when
    no round had 2+ submissions. The cog formats this into the recap's
    Highlight field; keeping the picker pure lets tests assert tie-
    breaking behavior without spinning up Discord.

    Tie-break: when multiple rounds share the same spread, the first one
    encountered while iterating ``rounds_data.values()`` wins — matching
    the existing ``>`` comparison.
    """
    widest_round: str | None = None
    widest_spread = -1
    widest_low = 0
    widest_high = 0
    for rnum, rnd in rounds_data.items():
        p = rnd.get("prices", {})
        if len(p) < 2:
            continue
        amounts = list(p.values())
        spread = max(amounts) - min(amounts)
        if spread > widest_spread:
            widest_spread = spread
            widest_round = rnum
            widest_low = min(amounts)
            widest_high = max(amounts)

    if widest_round is None:
        return None
    return widest_round, widest_low, widest_high


def collect_all_players(
    rounds_data: dict[str, dict[str, Any]],
) -> set[int]:
    """Return the set of every uid that submitted in any round.

    Mirrors the cog's ``set(player_prices.keys())`` line so the recap's
    "Players" field can be computed without re-iterating rounds in the
    caller.
    """
    return set(_player_prices_from_rounds(rounds_data).keys())
