"""Currency payouts for completed games (participation + win rewards).

Stage 1 faucet wiring. Party games pay out at ``game_manager.end_game``; the
six duel cogs pay at their resolution points. Every credit goes through
``economy_service.award_game_reward``; the whole payout is wrapped so a failure
is logged and never propagates — economy must never block game flow.

``resolve_winners`` turns a completed game's payload into the list of winning
user ids, modelled on the "best moment" extraction in
``games_session/logic.py``. Only game types with a genuine per-user winner
return ids; everything else (including wyr, whose "most divisive" is a question,
not a player) returns ``[]``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

import discord

from bot_modules.core.db_utils import (
    get_tz_offset_hours,
    open_db,
    open_db_immediate,
)
from bot_modules.economy.logic import cat_catch_payout, local_day_bounds, local_day_for
from bot_modules.economy.quest_views import refresh_signoff_board
from bot_modules.services.economy_quests_service import (
    fire_trigger_quests,
    source_enabled,
)
from bot_modules.services.embeds import footer_emoji
from bot_modules.services.economy_service import (
    EconSettings,
    apply_credit,
    award_game_reward,
    award_host_bounty,
    cat_coins_earned_since,
    load_econ_settings,
    member_is_booster,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger(__name__)

# Game types that count as the "duel" quest trigger; every other game_type
# reaching pay_game_rewards is a party game. Mirrors the six PvP cogs that
# call the faucet at their own resolution points.
_DUEL_GAME_TYPES = frozenset(
    {"chicken", "hot_potato", "hot_potato_group", "musical_chairs", "pressure", "quickdraw"}
)


async def pay_game_rewards(
    bot: "Bot",
    guild_id: int,
    participant_ids: Sequence[int | str],
    winner_ids: Sequence[int | str],
    game_type: str,
    *,
    occurrence: str | None = None,
    host_id: int | None = None,
) -> None:
    """Credit participation to every participant and a win bonus to winners.

    No-op unless the guild's economy is enabled. Duplicate ids, bots, and
    non-positive/unresolvable ids are dropped; winners also receive
    participation and are restricted to resolved participants. Per-member
    failures are logged and never propagate.

    Also fires the matching quest trigger ("duel" for the PvP cogs,
    "party_game" otherwise) for every participant. ``occurrence`` is the
    stable per-game id event quests dedupe on; without one, only
    daily/weekly trigger quests fire.

    ``host_id`` marks the member who ran the game (party games only — duels are
    peer challenges and external games have no local host). When set and at
    least one *other* member joined, the host earns the scaled host bounty and
    fires the ``game_host`` trigger, which drives both the personal "host a
    game" daily and the guild-wide community counted quest. A host with no
    joiners earns nothing and fires nothing — that is the anti-farm gate, so an
    empty game started to game the quest pays out zero.
    """
    try:
        guild = bot.get_guild(guild_id)
        if guild is None:
            return

        def _coerce(raw: object) -> int | None:
            try:
                return int(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None

        def _valid(uid: int) -> bool:
            if uid <= 0:
                return False
            member = guild.get_member(uid)
            return member is not None and not member.bot

        coerced = [c for c in (_coerce(u) for u in participant_ids) if c is not None]
        participants = [uid for uid in dict.fromkeys(coerced) if _valid(uid)]
        if not participants:
            return
        allowed = set(participants)
        winners = {c for c in (_coerce(u) for u in winner_ids) if c in allowed}

        db_path = bot.ctx.db_path

        def _load():
            with open_db(db_path) as conn:
                return load_econ_settings(conn, guild_id)

        settings = await asyncio.to_thread(_load)
        if not settings.enabled:
            return

        boosters = {uid: member_is_booster(bot, guild_id, uid) for uid in participants}

        def _credit() -> None:
            with open_db(db_path) as conn:
                for uid in participants:
                    booster = boosters[uid]
                    try:
                        award_game_reward(
                            conn, settings, guild_id, uid,
                            kind="game_participation", booster=booster,
                        )
                        if uid in winners:
                            award_game_reward(
                                conn, settings, guild_id, uid,
                                kind="game_win", booster=booster,
                            )
                    except Exception:
                        log.exception(
                            "game reward failed for user %s (%s)", uid, game_type
                        )

        await asyncio.to_thread(_credit)

        await pay_host_bounty(
            bot, guild_id, settings, host_id,
            participants=participants, boosters=boosters,
            game_type=game_type, occurrence=occurrence,
        )

        is_duel = game_type in _DUEL_GAME_TYPES
        kind = "duel" if is_duel else "party_game"
        # Namespace with the game type: duel ids are per-type table PKs, so
        # chicken #5 and quickdraw #5 must not share one occurrence key.
        scoped = f"{game_type}:{occurrence}" if occurrence is not None else None
        await _fire_triggers(
            bot, guild, settings, kind, participants, boosters, scoped
        )
        if winners:
            win_kind = "duel_win" if is_duel else "game_win"
            await _fire_triggers(
                bot, guild, settings, win_kind, sorted(winners), boosters, scoped
            )
        if is_duel:
            # Everyone in a resolved duel who wasn't the winner "lost" — fire
            # duel_lose for them (same per-match occurrence key namespace).
            losers = sorted(set(participants) - winners)
            if losers:
                await _fire_triggers(
                    bot, guild, settings, "duel_lose", losers, boosters, scoped
                )
    except Exception:
        log.exception("pay_game_rewards failed for guild %s (%s)", guild_id, game_type)


async def fire_member_trigger(
    bot: "Bot",
    guild_id: int,
    user_id: int | None,
    trigger_kind: str,
    *,
    occurrence: str | None = None,
    daily_occurrence: bool = False,
) -> None:
    """Fire a quest trigger for one member outside the game-faucet path.

    For modules with per-member actions but no participation payout (Risky
    Roll dares, Guess Who rounds). Same guarantees as ``pay_game_rewards``:
    no-op when the economy is off or the member is a bot/unresolvable, and
    a failure is logged, never raised into the calling game flow.

    ``daily_occurrence=True`` keys the occurrence to the guild-local day
    (computed where the fire already derives it), making an event quest on
    this kind pay at most once per day by construction — the
    voice_session/photo_post pattern for call sites without tz plumbing.
    """
    try:
        guild = bot.get_guild(guild_id)
        if guild is None or user_id is None:
            return
        member = guild.get_member(int(user_id))
        if member is None or member.bot:
            return

        db_path = bot.ctx.db_path

        def _load():
            with open_db(db_path) as conn:
                return load_econ_settings(conn, guild_id)

        settings = await asyncio.to_thread(_load)
        if not settings.enabled:
            return
        boosters = {member.id: member.premium_since is not None}
        await _fire_triggers(
            bot, guild, settings, trigger_kind, [member.id], boosters, occurrence,
            daily_occurrence=daily_occurrence,
        )
    except Exception:
        log.exception(
            "fire_member_trigger failed for guild %s (%s)", guild_id, trigger_kind
        )


async def pay_cat_catch(
    bot: "Bot",
    guild_id: int,
    user_id: int,
    *,
    coins: int,
    rarity: str,
    doubled: bool,
    occurrence: str,
) -> int:
    """Credit a Cat Bot catch: ``coins`` (rarity-tiered, blessed already folded)
    plus the ``cat_catch`` quest trigger. Returns the coins **actually
    credited**, which is what the caller should log — with a cap in play that
    is not ``coins``.

    ``coins`` is the value the guild's ``catcatch_coins_*`` dials produced;
    ``cat_catch_daily_cap`` then clips it to the member's remaining allowance
    for the guild-local day (see ``logic.cat_catch_payout``). A catch clipped
    to nothing still fires the quest trigger — the cap limits the coin faucet,
    not participation, and a member who has hit it should still see their
    "catch N cats" quest tick.

    Same guarantees as the other faucets: no-op when the economy is off or the
    member is a bot/unresolvable, booster multiplier applied, failures logged
    not raised. Caller dedupes per catch (the payout ledger) — ``apply_credit``
    is not occurrence-guarded, so this must fire at most once per catch.
    """
    try:
        guild = bot.get_guild(guild_id)
        if guild is None:
            return 0
        member = guild.get_member(int(user_id))
        if member is None or member.bot or coins < 1:
            return 0

        db_path = bot.ctx.db_path

        def _load() -> EconSettings:
            with open_db(db_path) as conn:
                return load_econ_settings(conn, guild_id)

        settings = await asyncio.to_thread(_load)
        if not settings.enabled:
            return 0

        booster = member_is_booster(bot, guild_id, user_id)

        def _credit() -> int:
            # BEGIN IMMEDIATE: this reads the day's running total and then
            # writes against it, and every catch is its own to_thread — on a
            # plain DEFERRED transaction two concurrent catches could both
            # read the same earned_today and both pay, overshooting the cap.
            with open_db_immediate(db_path) as conn:
                # Only read the day's running total when a cap is actually set,
                # so the uncapped default costs no extra query on a path that
                # fires ~150 times a day.
                earned_today = 0
                if settings.cat_catch_daily_cap > 0:
                    offset = get_tz_offset_hours(conn, guild_id)
                    day_start, _ = local_day_bounds(
                        local_day_for(time.time(), offset), offset
                    )
                    earned_today = cat_coins_earned_since(
                        conn, guild_id, user_id, day_start
                    )
                payout = cat_catch_payout(
                    coins,
                    earned_today=earned_today,
                    daily_cap=settings.cat_catch_daily_cap,
                )
                if payout < 1:
                    return 0
                return apply_credit(
                    conn, guild_id, user_id, payout, "cat_catch",
                    meta={"rarity": rarity, "doubled": doubled},
                    booster=booster, multiplier=settings.booster_multiplier,
                )

        credited = await asyncio.to_thread(_credit)
        # Outside _credit, and outside its `payout < 1` return, so a catch
        # clipped to nothing by the cap still advances a cat quest.
        await _fire_triggers(
            bot, guild, settings, "cat_catch", [user_id],
            {user_id: booster}, f"catbot:{occurrence}",
        )
        return credited
    except Exception:
        log.exception("pay_cat_catch failed for guild %s", guild_id)
        return 0


async def pay_mention_award(
    bot: "Bot",
    guild_id: int,
    user_id: int,
    *,
    coins: int,
    rule_id: int,
    occurrence: str,
) -> bool:
    """Credit a Mention Award: ``coins`` plus the ``mention_award`` quest
    trigger. The ``pay_cat_catch`` shape — a game the bot does not host,
    detected by a watcher, paid directly with a quest hook on top.

    Returns **whether the credit actually landed**. The caller burns the
    announcement's once-ever payout claim before calling, so it must be able
    to tell a paid announcement from a silent no-op (economy off, member
    unresolvable) and release the claim for the latter — otherwise the member
    is unpaid forever. True is decided by the credit alone: a failure firing
    the quest trigger afterwards is logged, not surfaced, because releasing
    the claim after a landed credit would double-pay on retry.

    ``occurrence`` is the ready-made quest occurrence key
    (``mention_awards.logic.quest_occurrence``) — built by the caller so the
    live path and the backfill can never drift on its format.

    Same guarantees as the other faucets: no-op when the economy is off or
    the member is a bot/unresolvable, booster multiplier applied, failures
    logged not raised. Caller dedupes per announcement (the payout ledger) —
    ``apply_credit`` is not occurrence-guarded, so this must fire at most
    once per announcement.
    """
    try:
        guild = bot.get_guild(guild_id)
        if guild is None:
            return False
        member = guild.get_member(int(user_id))
        if member is None or member.bot or coins < 1:
            return False

        db_path = bot.ctx.db_path

        def _load() -> EconSettings:
            with open_db(db_path) as conn:
                return load_econ_settings(conn, guild_id)

        settings = await asyncio.to_thread(_load)
        if not settings.enabled:
            return False

        booster = member_is_booster(bot, guild_id, user_id)

        def _credit() -> None:
            with open_db(db_path) as conn:
                apply_credit(
                    conn, guild_id, user_id, coins, "mention_award",
                    meta={"rule_id": rule_id},
                    booster=booster, multiplier=settings.booster_multiplier,
                )

        await asyncio.to_thread(_credit)
    except Exception:
        log.exception("pay_mention_award failed for guild %s", guild_id)
        return False
    try:
        await _fire_triggers(
            bot, guild, settings, "mention_award", [user_id],
            {user_id: booster}, occurrence,
        )
    except Exception:
        log.exception("mention_award trigger fire failed for guild %s", guild_id)
    return True


async def pay_cah_game_by_score(
    bot: "Bot",
    guild_id: int,
    scores: Mapping[Any, Any],
    winner_id: Any,
    *,
    occurrence: str | None = None,
    game_key: str = "cah",
    host_id: int | None = None,
) -> None:
    """Credit a finished external game proportional to each player's score.

    Replaces the flat participation/win payout for score-carrying external
    games: the top scorer (the *Game over!* winner) earns
    ``reward_cah_win_max`` coins, and everyone else earns that amount scaled by
    their score's ratio to the winner's, rounded to the nearest coin — a share
    that rounds to 0 pays nothing (``apply_credit`` rejects amounts under 1).
    Still fires the same ``party_game``/``game_win`` quest triggers as
    ``pay_game_rewards`` so external games keep feeding quests identically;
    only the coin math changes. Same guarantees as the other faucets: no-op
    when the economy is off, bots/unresolvable members are dropped, failures
    logged not raised.

    ``winner_id`` accepts a single id **or a collection of them** — Wordle ties
    are common (``👑 3/6: <@a> <@b>``), and everyone on the winning line should
    take the cap and fire ``game_win``. Ids not present in ``scores`` are
    dropped, so a winner who can't be resolved never creates a phantom payout.

    ``game_key`` scopes the quest ``occurrence`` and defaults to CAH, which
    this started life as. Gamebot Anagrams passes ``"anagrams"``, Wordle
    ``"wordle"``, Co-ordle ``"coordle"`` — all sharing the same
    ``reward_cah_win_max`` cap, one dial for "an external game win", since they
    all pay by score *ratio* and the absolute point scales (5 vs 900 vs 6)
    cancel out.
    """
    try:
        guild = bot.get_guild(guild_id)
        if guild is None:
            return

        def _coerce(raw: object) -> int | None:
            try:
                return int(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None

        def _valid(uid: int) -> bool:
            if uid <= 0:
                return False
            member = guild.get_member(uid)
            return member is not None and not member.bot

        numeric_scores: dict[int, int] = {}
        for raw_uid, raw_score in scores.items():
            uid, score = _coerce(raw_uid), _coerce(raw_score)
            if uid is not None and score is not None and _valid(uid):
                numeric_scores[uid] = score
        if not numeric_scores:
            return

        top_score = max(numeric_scores.values())
        if top_score <= 0:
            return

        participants = sorted(numeric_scores)
        if winner_id is None or isinstance(winner_id, (str, bytes, int)):
            raw_winners: list[object] = [winner_id]
        else:
            try:
                raw_winners = list(winner_id)  # type: ignore[arg-type]
            except TypeError:
                raw_winners = [winner_id]
        winners = {
            w for w in (_coerce(r) for r in raw_winners) if w in numeric_scores
        }

        db_path = bot.ctx.db_path

        def _load() -> EconSettings:
            with open_db(db_path) as conn:
                return load_econ_settings(conn, guild_id)

        settings = await asyncio.to_thread(_load)
        if not settings.enabled:
            return
        cap = settings.reward_cah_win_max
        if cap <= 0:
            return

        boosters = {uid: member_is_booster(bot, guild_id, uid) for uid in participants}

        def _credit() -> None:
            with open_db(db_path) as conn:
                for uid in participants:
                    share = round(cap * numeric_scores[uid] / top_score)
                    if share < 1:
                        continue
                    kind = "game_win" if uid in winners else "game_participation"
                    try:
                        apply_credit(
                            conn, guild_id, uid, share, kind,
                            meta={"score": numeric_scores[uid], "top_score": top_score},
                            booster=boosters[uid], multiplier=settings.booster_multiplier,
                        )
                    except Exception:
                        log.exception(
                            "%s score payout failed for user %s (guild %s)",
                            game_key, uid, guild_id,
                        )

        await asyncio.to_thread(_credit)

        scoped = f"{game_key}:{occurrence}" if occurrence is not None else None
        await _fire_triggers(
            bot, guild, settings, "party_game", participants, boosters, scoped
        )
        if winners:
            await _fire_triggers(
                bot, guild, settings, "game_win", sorted(winners), boosters, scoped
            )
        await pay_host_bounty(
            bot, guild_id, settings, host_id,
            participants=participants, boosters=boosters,
            game_type=game_key, occurrence=occurrence,
        )
    except Exception:
        log.exception("pay_cah_game_by_score failed for guild %s", guild_id)


def resolve_named_scores(
    guild: discord.Guild, scores: Mapping[str, int]
) -> tuple[dict[int, int], list[str]]:
    """Map a ``{username: points}`` scoreboard onto ``{member_id: points}``.

    Gamebot's Anagrams scoreboard names players by Discord username rather than
    mention, so — exactly like the Cat Bot catch path — they have to be looked
    up by name. Returns the resolved scores plus the names that matched nobody
    (left the server, renamed, or a bot), which the caller logs rather than
    guesses at. On a duplicate display name the higher score wins, so a
    collision can't silently zero someone out.
    """
    resolved: dict[int, int] = {}
    unresolved: list[str] = []
    for name, points in scores.items():
        member = guild.get_member_named(name)
        if member is None or member.bot:
            unresolved.append(name)
            continue
        resolved[member.id] = max(resolved.get(member.id, 0), int(points))
    return resolved, unresolved


async def pay_host_bounty(
    bot: "Bot",
    guild_id: int,
    settings: EconSettings,
    host_id: int | None,
    *,
    participants: Sequence[int],
    boosters: dict[int, bool],
    game_type: str,
    occurrence: str | None,
) -> None:
    """Credit a game's host and fire the ``game_host`` trigger.

    Shared by the flat faucet (``pay_game_rewards``) and the score-proportional
    one (``pay_cah_game_by_score``) so external games pay hosting on exactly the
    same terms native ones do — they paid none at all until 2026-07-26, since
    neither caller passed a host.

    ``joiners`` counts participants *other than* the host, so a solo game pays
    nothing: that's the anti-farm gate, and it's what stops an abandoned lobby
    earning its would-be host a bounty. The host need not be a participant.
    """
    host = int(host_id) if host_id else 0
    if host <= 0:
        return
    guild = bot.get_guild(guild_id)
    if guild is None:
        return
    member = guild.get_member(host)
    if member is None or member.bot:
        return
    joiners = len([uid for uid in participants if uid != host])
    if joiners <= 0:
        return

    host_booster = (
        boosters[host] if host in boosters else member_is_booster(bot, guild_id, host)
    )
    db_path = bot.ctx.db_path

    def _bounty() -> None:
        with open_db(db_path) as conn:
            # Gate the coin faucet on the game_host income source, the same way
            # photo_post gates its flat award — flipping the source off must
            # stop the payout, not just the quest.
            if not source_enabled(conn, guild_id, "game_host"):
                return
            try:
                award_host_bounty(
                    conn, settings, guild_id, host,
                    joiners=joiners, booster=host_booster,
                )
            except Exception:
                log.exception("host bounty failed for %s (%s)", host, game_type)

    await asyncio.to_thread(_bounty)
    # The game_host trigger drives the personal daily and the guild-wide
    # community counted quest. Keyed to the game id so it fires once.
    await fire_member_trigger(
        bot, guild_id, host, "game_host",
        occurrence=str(occurrence) if occurrence is not None else None,
    )


async def _fire_triggers(
    bot: "Bot",
    guild: discord.Guild,
    settings: EconSettings,
    trigger_kind: str,
    user_ids: Sequence[int],
    boosters: dict[int, bool],
    occurrence: str | None,
    *,
    daily_occurrence: bool = False,
) -> None:
    """Auto-claim active trigger-kind quests for members; silent by design.

    Like the participation faucet, paid claims make no channel noise (a game
    ending with a dozen "quest complete" embeds would drown the recap) — the
    wallet ledger and /quests state carry the news. Sign-off claims do repaint
    the mods' todo board, since a manager has to be able to act on them.
    """
    db_path = bot.ctx.db_path

    def _fire() -> list[tuple[int, Any]]:
        results: list[tuple[int, Any]] = []
        with open_db(db_path) as conn:
            offset = get_tz_offset_hours(conn, guild.id)
            day = local_day_for(time.time(), offset)
            occ = day if daily_occurrence else occurrence
            for uid in user_ids:
                fired = fire_trigger_quests(
                    conn,
                    settings,
                    guild.id,
                    trigger_kind,
                    uid,
                    local_day=day,
                    occurrence=occ,
                    booster=boosters.get(uid, False),
                )
                results.extend((uid, outcome) for _quest, outcome in fired)
        return results

    results = await asyncio.to_thread(_fire)

    # One repaint for the batch, not one per claim: a game can fire the same
    # sign-off quest for every player at once, and the board shows all of them
    # after a single edit.
    if any(outcome.state == "pending" for _uid, outcome in results):
        await refresh_signoff_board(bot, guild.id)


# ── Winner extraction ─────────────────────────────────────────────────────────


def _winners_nhie(payload: dict[str, Any]) -> list[int]:
    """Guiltiest player — highest guilt score."""
    scores = payload.get("guilt_scores") or {}
    if not scores:
        return []
    guiltiest = max(scores, key=lambda k: scores[k])
    return [int(guiltiest)]


def _winners_ttl(payload: dict[str, Any]) -> list[int]:
    """Best Liar + Best Guesser — both skill awards pay, ties included.

    Delegates to the game's own recap logic so the paid winners always
    match the names on the FINAL RESULTS embed. (Open Book — fooled the
    fewest — is a booby prize and deliberately unpaid.)
    """
    from bot_modules.games_ttl.logic import (
        compute_recap_winners,
        played_ids_from_payload,
    )

    raw_scores = payload.get("scores") or {}
    if not raw_scores:
        return []
    # Normalize entries so a partial dict (hand-edited or legacy) can't
    # KeyError inside the recap logic and void the whole payout.
    scores = {
        uid: {
            "fooled": (s or {}).get("fooled", 0),
            "correct_guesses": (s or {}).get("correct_guesses", 0),
            "total_guessers": (s or {}).get("total_guessers", 0),
        }
        for uid, s in raw_scores.items()
    }
    stats = compute_recap_winners(scores, played_ids_from_payload(payload))
    # A zero extremum means nobody actually earned the award (nobody was
    # fooled / nobody guessed right) — an all-tie at 0 must not pay everyone.
    liars = stats["best_liar"] if stats["most_fooled_count"] > 0 else []
    guessers = stats["best_guesser"] if stats["max_correct"] > 0 else []
    winners: list[int] = []
    for uid in liars + guessers:
        try:
            iuid = int(uid)
        except (TypeError, ValueError):
            continue
        if iuid not in winners:
            winners.append(iuid)
    return winners


def _winners_hottakes(payload: dict[str, Any]) -> list[int]:
    """Author of the highest-rated take."""
    results = payload.get("results") or []
    if not results:
        return []
    hottest = max(results, key=lambda r: r.get("avg", 0))
    author = hottest.get("author")
    return [int(author)] if author is not None else []


def _top_scorers(scores: dict[str, Any]) -> list[int]:
    """Uids tied for the highest positive numeric score.

    Shared shape for clapback points, MLT crowns, and any future
    ``{uid: count}`` scoreboard. An all-zero board returns ``[]`` —
    "nobody scored" must not pay everyone.
    """
    numeric: dict[int, float] = {}
    for uid, val in (scores or {}).items():
        try:
            numeric[int(uid)] = float(val)
        except (TypeError, ValueError):
            continue
    if not numeric:
        return []
    top = max(numeric.values())
    if top <= 0:
        return []
    return [uid for uid, v in numeric.items() if v == top]


def _winners_rushmore(payload: dict[str, Any]) -> list[int]:
    """Vote winner(s) — most votes for best Mt. Rushmore board."""
    votes = payload.get("votes") or {}
    tally: dict[str, int] = {}
    for target in votes.values():
        key = str(target)
        tally[key] = tally.get(key, 0) + 1
    return _top_scorers(tally)


def _winners_clapback(payload: dict[str, Any]) -> list[int]:
    """Highest score after the final round."""
    return _top_scorers(payload.get("scores") or {})


def _winners_mlt(payload: dict[str, Any]) -> list[int]:
    """Most round crowns."""
    return _top_scorers(payload.get("crowns") or {})


def _winners_price(payload: dict[str, Any]) -> list[int]:
    """Most Reasonable (overall) — most Most-Reasonable round wins."""
    scores = payload.get("scores") or {}
    return _top_scorers(scores.get("reasonable_wins") or {})


_WINNER_RESOLVERS: dict[str, Callable[[dict[str, Any]], list[int]]] = {
    "nhie": _winners_nhie,
    "ttl": _winners_ttl,
    "hottakes": _winners_hottakes,
    "rushmore": _winners_rushmore,
    "clapback": _winners_clapback,
    "mlt": _winners_mlt,
    "price": _winners_price,
}


async def append_payout_footer(bot: "Bot", embed: discord.Embed, guild_id: int, game_type: str) -> None:
    """Stamp a recap embed with what the game just paid out.

    Adds a line like ``🪙 +20 to winners · +5 to everyone who played`` under
    any existing footer text, using the guild's configured amounts. Silently
    a no-op when the economy is disabled, amounts are zero, or settings can't
    load — a recap must never fail over its footer. Game types with no winner
    resolver only advertise the participation payout.
    """
    try:
        db_path = bot.ctx.db_path

        def _load() -> EconSettings:
            with open_db(db_path) as conn:
                return load_econ_settings(conn, guild_id)

        settings = await asyncio.to_thread(_load)
        if not settings.enabled:
            return
        parts: list[str] = []
        if game_type in _WINNER_RESOLVERS and settings.reward_game_win:
            parts.append(f"+{settings.reward_game_win} to winners")
        if settings.reward_game_participation:
            parts.append(f"+{settings.reward_game_participation} to everyone who played")
        if not parts:
            return
        # Custom currency emoji render as raw text in a footer — drop it there.
        prefix = footer_emoji(settings.currency_emoji)
        line = f"{prefix} {' · '.join(parts)}".lstrip()
        existing = embed.footer.text if embed.footer else None
        embed.set_footer(text=f"{existing}\n{line}" if existing else line)
    except Exception:
        log.exception("payout footer failed for guild %s (%s)", guild_id, game_type)


def resolve_winners(game_type: str, payload: dict) -> list[int]:
    """Winning user ids for a completed game; ``[]`` for types with no winner.

    Table-driven and defensive: an unknown game type or a malformed payload
    yields ``[]`` rather than raising into the game-end path.
    """
    resolver = _WINNER_RESOLVERS.get(game_type)
    if resolver is None:
        return []
    try:
        return resolver(payload or {})
    except Exception:
        log.debug("resolve_winners failed for %s", game_type, exc_info=True)
        return []
