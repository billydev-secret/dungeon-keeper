"""Pure quest math — no discord, no database (spec §4).

The claim ``period`` model, the library slot rule, the rotate-pool cursor,
the reward bands, and the trigger-phrase matcher. Everything is deterministic
on its inputs so the ISO week boundaries, slot matrix, rotation cycling, and
phrase-boundary rules stay table-testable.
"""

from __future__ import annotations

import hashlib
import random
import re
from datetime import date

# Library slot limits per guild. Daily/weekly/monthly active quests form a
# per-cadence *pool*: each member is shown/paid a personal subset of N drawn
# from that pool per period (see assigned_quest_ids), so the caps are a
# sanity ceiling on pool size, not a hard "one active" rule. Community goals
# are uncapped. Event quests are capped at 1 active PER TRIGGER KIND — the
# listener pays every active quest matching its trigger, so two same-kind
# actives would double-pay one occurrence.
POOL_CAP = 25
MAX_ACTIVE_DAILY = POOL_CAP
MAX_ACTIVE_WEEKLY = POOL_CAP
MAX_ACTIVE_MONTHLY = POOL_CAP
MAX_ACTIVE_EVENT_PER_KIND = 1

# Cadences that draw a *personal board* — a per-member subset of the pool.
# Membership here is the has-a-board predicate, kept separate from the board
# size: a size of 0 means "this guild shows none of this cadence", which is
# the opposite of community/event's "no board concept, every active quest
# counts". Conflating the two would make a disabled cadence pay everything.
BOARD_CADENCES = frozenset({"daily", "weekly", "monthly"})

# One-time member-setup trigger kinds. These fire once in a member's lifetime
# (setting a bio, saving a birthday) but we still want them to *appear* in the
# random daily board as a subtle welcome guide — a quest you're nudged to do
# once and that then quietly drops off. So a board-cadence quest on one of
# these kinds gets two special-cases in the service layer:
#   • it is claimed on a constant once-ever period (occurrence "set"), not the
#     calendar day, so re-saving a bio tomorrow can't re-earn it, and the
#     completing action always pays even if the quest wasn't drawn that day;
#   • it drops off a member's board once they've done the underlying thing
#     (bio row / birthday row exists) or already claimed it — so only members
#     who *haven't* done it ever see it.
# Kept here (pure) as the single source of truth; the DB-facing completion
# checks live in economy_quests_service.
SETUP_QUEST_KINDS = frozenset({"bio_set", "birthday_set"})

# Default quests each member draws from each cadence's pool per period, when
# a guild hasn't tuned its own (EconSettings.quest_board_*). The repeat gap
# for a member is ~floor(poolsize / N) periods, so a bigger pool (or a
# smaller N) spaces repeats further apart.
PERSONAL_BOARD_SIZE: dict[str, int] = {"daily": 2, "weekly": 2, "monthly": 2}

# Game/module triggers a quest can be auto-completed by (label = how the
# dashboard describes it). On an *event* quest the trigger pays per
# occurrence (period "<kind>:<occurrence>", no time gate); on a daily/weekly
# quest it auto-claims the ordinary calendar period — "do it once today/this
# week". The firing side lives with each module: the photo-post listener in
# EconomyCog, the game-completion hooks in economy/game_rewards.py.
TRIGGER_KINDS: dict[str, str] = {
    "photo_post": "Post a photo in the Photo Challenge channel",
    "party_game": "Finish a party game",
    "duel": "Finish a duel / PvP challenge",
    "risky_roll": "Take a Risky Roll dare",
    "guess": "Play a Guess Who round",
    "voice_session": "Be active in voice chat",
    "qotd_reply": "Answer the Question of the Day",
    "starboard": "Get a message on the starboard",
    "invite": "Invite a new member",
    "boost": "Boost the server",
    "bio_set": "Set or update your bio",
    "media_post": "Post an image (optionally scoped to the trigger channel)",
    "pen_pal": "Get matched with a Pen Pal",
    "message_sent": "Send a message",
    "reply_sent": "Reply to someone's message",
    "reaction_given": "React to someone's message",
    "game_win": "Win a party game",
    "duel_win": "Win a duel / PvP challenge",
    "duel_lose": "Lose a duel / PvP challenge",
    "confession": "Post an anonymous confession",
    "ama_ask": "Ask a question in an AMA",
    "whisper": "Send an anonymous whisper",
    "quote": "Turn a message into a quote card",
    "chat_revive": "Answer a Chat Revive prompt",
    "bump": "Bump the server",
    "voice_room_host": "Host a voice room that draws guests",
    "pen_pal_complete": "See a Pen Pals session through to the end",
    "whisper_guess": "Correctly guess who sent a whisper",
    "guess_win": "Win a Guess Who round",
    "quoted": "Have your message turned into a quote card",
    "session_join": "Join a scheduled game session",
    "voice_message": "Post a voice message",
    "music_request": "Request a song",
    "birthday_set": "Set your birthday",
    "level_up": "Reach a new level",
    "ama_answer": "Answer a question in your AMA",
    "conversed": "Reply to different members",
    "replied_to": "Have different members reply to you",
    "reacted_to_member": "React to different members' messages",
    "channel_hop": "Talk in different channels",
    "active_day": "Be active on different days",
    "voice_partner": "Share voice with different members",
    "thread_deep": "Be part of a deep thread",
    "welcome": "Welcome a new member",
    "conversation_starter": "Start a conversation that takes off",
    "cat_catch": "Catch a cat with Cat Bot",
}

# Longer per-kind copy for the Income Sources page: what fires it and what
# the event-quest occurrence key means for repeat payouts.
TRIGGER_KIND_INFO: dict[str, str] = {
    "photo_post": "Posting an image in the configured Photo Challenge channel — the post itself pays, no reactions needed. Event cadence: once per guild-local day.",
    "party_game": "Any party game completing with the member in the roster. Event cadence: once per game.",
    "duel": "A duel/PvP game resolving (chicken, hot potato, musical chairs, pressure cooker, quickdraw). Event cadence: once per match.",
    "risky_roll": "Pressing Roll in a Risky Rolls round. Event cadence: once per round.",
    "guess": "Submitting a scored guess in a Guess Who round. Event cadence: once per round.",
    "voice_session": "Earning voice-activity XP (being in VC, not idle-muted). Event cadence: once per guild-local day.",
    "qotd_reply": "Earning the QOTD reward (first message in the QOTD channel that day). Event cadence: once per question.",
    "starboard": "Having a message cross the starboard threshold. Event cadence: once per starred message.",
    "invite": "A member you invited joining the server. Event cadence: once per distinct invitee — alt-farmable, enable with care.",
    "boost": "Starting a server boost. Event cadence: once per day it is detected.",
    "bio_set": "Saving or updating your member bio. Event cadence: once ever.",
    "media_post": "Posting a message with an image attached; set a trigger channel to scope it (e.g. #art). Event cadence: once per message — use daily/weekly for this one.",
    "pen_pal": "Being paired into a Pen Pals session (both members fire). Event cadence: once per session.",
    "message_sent": "Any message in the server. Pair with a target count ('send 20 messages this week') — a target of 1 completes on the first message, and rewarding raw volume invites spam.",
    "reply_sent": "Using Discord's reply on someone ELSE's message (self-replies never count). Best with a target count.",
    "reaction_given": "Reacting to someone else's message — inherits the XP farm guard (one per message per reactor, ever; no self-reacts, no bots). Best with a target count.",
    "game_win": "Winning a party game (only types with a real winner resolve one: NHIE guiltiest, TTL best liar, Hot Takes hottest). Event cadence: once per game.",
    "duel_win": "Winning a duel/PvP match. Event cadence: once per match.",
    "duel_lose": "Not winning a duel/PvP match (every participant who wasn't the winner). Event cadence: once per match.",
    "confession": "Submitting an anonymous confession. The confessor is credited privately — there is no public 'quest complete' message, only their own quest log (the only trace is the staff-side ledger row). Event cadence: once per confession — use daily/weekly with a target count.",
    "ama_ask": "Asking a question in an AMA. Unfiltered questions fire on submit; screened questions fire only once the host approves (rejected ones never pay). Event cadence: once per question — use daily/weekly with a target count.",
    "whisper": "Sending an anonymous whisper to another member. Event cadence: once per whisper — use daily/weekly with a target count.",
    "quote": "Turning someone's message into a quote card with the make-it-a-quote role (the quoter who invokes it is credited). Event cadence: once per quoted message — mildly farmable, so use daily/weekly with a target count.",
    "chat_revive": "Responding to a Chat Revive prompt while the lull window is open (the reply the revive service counts as an answer). Event cadence: once per prompt.",
    "bump": "Bumping the server on a listing site (the member who ran the bump command is credited). Event cadence: once per bump — bump cooldowns are the natural rate limit.",
    "voice_room_host": "Your Voice Master room reaching 2+ other members at once (bots and you excluded). Fires once per room lifetime, on the crossing. Event cadence: once per room.",
    "pen_pal_complete": "A Pen Pals session you were in reaching its natural end — both members fire; sessions that end early don't. Event cadence: once per session.",
    "whisper_guess": "Correctly guessing who sent you an anonymous whisper. Event cadence: once per whisper.",
    "guess_win": "Winning a Guess Who round. Event cadence: once per round.",
    "quoted": "Someone ELSE turning your message into a quote card (the quoted author is credited; self-quotes never fire). Event cadence: once per quoted message.",
    "session_join": "Joining a scheduled game session. Event cadence: once per session.",
    "voice_message": "Posting a voice message (the transcription listener is the detector). Event cadence: once per message — use daily/weekly with a target count.",
    "music_request": "Requesting a song in the music player. Capped at once per guild-local day by construction, so raw queue spam never multi-pays.",
    "birthday_set": "Saving your birthday. Event cadence: once ever — the bio_set pattern.",
    "level_up": "Reaching a new XP level. Event cadence: once per level reached.",
    "ama_answer": "Answering a question as the hot seat in your own AMA. Event cadence: once per question answered — use daily/weekly with a target count.",
    "conversed": "Replying to another member's message — each occurrence is that MEMBER, so a counted quest reads 'talk with N different people' (repeat replies to the same person never re-count in a period). Replies only, never bare mentions (mention spam is free; a reply is a real directed interaction).",
    "replied_to": "Someone else replying to YOUR message — the passive twin of conversed; occurrences are the repliers, so counted = 'have N different people reply to you'.",
    "reacted_to_member": "Reacting to a message by someone you haven't reacted to yet this period — occurrences are the message AUTHORS, so counted = 'spread reactions across N different members'. Inherits the reaction XP farm guard.",
    "channel_hop": "Posting in a channel (threads count toward their parent) — occurrences are the CHANNELS, so counted = 'talk in N different channels'. Gets members out of their one home channel.",
    "active_day": "Your first message of a guild-local day — occurrences are the DAYS, so a weekly counted quest reads 'show up any N days this week'. The gentle streak: skipping a day costs nothing but the day.",
    "voice_partner": "Sharing a voice channel with another member while you both earn voice XP (anti-idle rules apply) — occurrences are the PARTNERS, so counted = 'hang out in voice with N different people'.",
    "thread_deep": "Posting in a thread that has reached 20+ messages — once per thread. Rewards sustaining a deep conversation; everyone who posts after the crossing gets their credit.",
    "welcome": "Replying to a member who joined within the last 7 days — occurrences are the newcomers, so counted = 'welcome N new faces'. The retention quest.",
    "conversation_starter": "Your message drawing replies from 3+ distinct members (self-replies and bots never count) — once per message, detected at reply ingest. Event cadence: once per qualifying message — use daily/weekly with a target count.",
    "cat_catch": "Catching a cat with the external Cat Bot in a channel tracked via `/games track watch … kind:Cat Bot`. The catch also pays rarity-tiered coins directly (common→divine); this trigger is the quest hook on top. Event cadence: once per catch (keyed on the catch message).",
}


# Suggested reward bands per quest type (community is judged by the author).
# Monthly was lowered from (75, 200) to (50, 90) after live data showed monthly
# quests were the richest per-claim faucet (see migration 103); the old ceiling
# let a single monthly pay ~150, several role-perk rentals from one click.
_REWARD_BANDS: dict[str, tuple[int, int]] = {
    "daily": (10, 20),
    "weekly": (25, 75),
    "monthly": (50, 90),
}


# Community weekly milestone tiers, as fractions of the auto-sized target.
# Tier 1 is sized to be near-certain, tier 3 a genuine stretch; each tier
# crossed pays the quest's flat reward once (research: binary pass/fail
# community goals at small scale just produce attributable disappointment).
COMMUNITY_TIERS: tuple[float, ...] = (0.4, 0.7, 1.0)


def community_tiers_crossed(current: int, target: int) -> int:
    """How many milestone tiers a community counter has crossed (0-3)."""
    if target <= 0 or current <= 0:
        return 0
    frac = current / target
    return sum(1 for t in COMMUNITY_TIERS if frac >= t)


def community_auto_target(four_week_total: int) -> int:
    """Size a community weekly from the guild's trailing 4-week kind total.

    target ≈ typical week ÷ 0.75, so an average week lands at ~75% (tier 2)
    and a visible push closes tier 3. Floor of 10 keeps a cold kind from
    producing a degenerate one-action goal.
    """
    weekly_typical = four_week_total / 4
    return max(10, round(weekly_typical / 0.75))


# thread_deep fires for posts in threads at or past this message count —
# deep enough to feel earned, common enough to happen weekly (2026-07-18
# choice: 20).
THREAD_DEEP_MIN = 20

# welcome fires for replies to members who joined within this window.
WELCOME_WINDOW_SECONDS = 7 * 86400

# conversation_starter fires when a message has drawn replies from this many
# distinct humans.
CONVERSATION_STARTER_REPLIERS = 3

# Personal dynamic-target stretch factor: a member's counted target is
# their own trailing-period median × this, clamped to the author's band —
# ~15% over their normal pace, so effort is comparable across members while
# reward stays flat (paying more for higher output would just re-reward the
# already-active).
DYNAMIC_STRETCH = 1.15


def dynamic_target(median_count: float, target_min: int, target_max: int) -> int:
    """Clamp a member's stretched trailing median into the author's band."""
    return max(target_min, min(target_max, round(median_count * DYNAMIC_STRETCH)))


# ── Community-weekly beat sheets ──────────────────────────────────────
# DMed to the host (not posted publicly): the numbers plus suggested copy
# they can paste or rewrite in their own voice. Pure string builders so the
# copy stays table-testable.


def beat_kickoff(title: str, kind_label: str, target: int, week: str) -> str:
    return (
        f"🎬 **Community weekly kicked off** ({week})\n"
        f"**{title}** — {kind_label}\n"
        f"Target: **{target}** · tiers at 40% / 70% / 100%, each tier pays "
        f"everyone.\n\n"
        f"Suggested post:\n"
        f"> 📣 New community goal this week: **{title}**! Every one of us "
        f"counts toward it — {kind_label.lower()}. Hit {target} together "
        f"and everyone gets paid three times over. Progress lives on the "
        f"leaderboard. Go!"
    )


def beat_tier(
    title: str, tier: int, current: int, target: int, contributors: int
) -> str:
    pct = round(100 * current / target) if target else 0
    return (
        f"🏁 **Tier {tier} crossed** — {title}\n"
        f"{current}/{target} ({pct}%) · {contributors} members contributed\n\n"
        f"Suggested post:\n"
        f"> 🎉 Tier {tier} down on **{title}** — {pct}% and climbing, "
        f"{contributors} of you have chipped in. Payout secured for "
        f"everyone; next tier's on the board!"
    )


def beat_final24(title: str, current: int, target: int) -> str:
    pct = round(100 * current / target) if target else 0
    need = max(0, target - current)
    return (
        f"⏳ **Final 24h** — {title}\n"
        f"{current}/{target} ({pct}%) · {need} to go for the full clear\n\n"
        f"Suggested post:\n"
        f"> ⏰ Last day on **{title}** — we're at {pct}%. {need} more and "
        f"it's a full clear. One push!"
    )


def beat_resolution(summary: dict) -> str:
    title = summary["title"]
    crossed = summary["tiers_crossed"]
    current, target = summary["current"], summary["target"]
    contributors = summary["contributors"]
    top = summary["top_contributors"]
    bonus_paid = summary["bonus_paid"]
    pct = round(100 * int(current) / int(target)) if target else 0
    top_lines = "\n".join(
        f"  {i + 1}. <@{uid}> — {n}" for i, (uid, n) in enumerate(top)
    ) or "  (nobody)"
    tier_word = f"{crossed}/3 tiers" if crossed else "no tiers"
    return (
        f"🏆 **Community weekly resolved** — {title}\n"
        f"Final: {current}/{target} ({pct}%) → **{tier_word}** paid to every "
        f"active member ({summary['reward_per_tier']} per tier).\n"
        f"Contributors: {contributors}\n"
        f"Top contributors{' (bonus paid)' if bonus_paid else ''}:\n"
        f"{top_lines}\n\n"
        f"Suggested post:\n"
        f"> 🏆 **{title}** is in the books: {pct}% and {tier_word} cleared — "
        f"payouts are in your wallets. Shout-out to our top contributors "
        f"{' '.join(f'<@{uid}>' for uid, _ in top) or '…nobody?!'} and all "
        f"{contributors} of you who moved the bar. Next goal after a "
        f"breather week. 💰"
    )


def iso_week_for(local_day: str) -> str:
    """Return the ISO week ("YYYY-Www") a guild-local calendar day falls in.

    Uses the ISO year from ``date.isocalendar()``, not the calendar year, so
    the year-rollover boundary is correct — 2026-12-31 is 2027-W01 and
    2027-01-01 can be 2026-W53.
    """
    iso = date.fromisoformat(local_day).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def month_for(local_day: str) -> str:
    """The calendar month ("YYYY-MM") a guild-local day falls in.

    Plain calendar months — a monthly quest's window opens on the 1st at
    guild-local midnight, no ISO-style shifting.
    """
    return local_day[:7]


def quest_period(qtype: str, local_day: str) -> str:
    """The claim period key for a quest type on a given guild-local day.

    Daily → the local day; weekly → its ISO week; community → the constant
    ``'once'`` (a community quest is claimed/settled once, not per period).
    Re-claimability falls straight out of this key — no reset sweeps.
    """
    if qtype == "daily":
        return local_day
    if qtype == "weekly":
        return iso_week_for(local_day)
    if qtype == "monthly":
        return month_for(local_day)
    if qtype == "community":
        return "once"
    # Event quests have no calendar period — the trigger listener supplies a
    # per-occurrence key (see occurrence_period), so a calendar lookup is a bug.
    raise ValueError(f"unknown quest type: {qtype!r}")


def occurrence_period(kind: str, occurrence: str) -> str:
    """The claim period key for one trigger occurrence on an *event* quest.

    Keyed to the occurrence (a photo card, one game, one duel …), not the
    calendar: each occurrence pays each member at most once, forever.
    """
    return f"{kind}:{occurrence}"


def can_activate(existing_active: list[str], qtype: str) -> bool:
    """True if activating one more ``qtype`` quest respects the slot rule.

    ``existing_active`` is the list of qtypes of the guild's currently-active
    quests (excluding the one under consideration). Community is uncapped.
    """
    if qtype == "daily":
        return existing_active.count("daily") < MAX_ACTIVE_DAILY
    if qtype == "weekly":
        return existing_active.count("weekly") < MAX_ACTIVE_WEEKLY
    if qtype == "monthly":
        return existing_active.count("monthly") < MAX_ACTIVE_MONTHLY
    if qtype == "community":
        return True
    if qtype == "event":
        # Callers gate event quests per trigger kind via can_activate_event;
        # type-level there is no cap (one photo + one duel event is fine).
        return True
    raise ValueError(f"unknown quest type: {qtype!r}")


def can_activate_event(existing_event_kinds: list[str], trigger_kind: str) -> bool:
    """True if activating one more event quest of this kind respects the cap.

    ``existing_event_kinds`` is the trigger kinds of the guild's currently
    active event quests (excluding the one under consideration). One active
    per kind — the listener pays every matching quest, so two same-kind
    actives would double-pay one occurrence.
    """
    return existing_event_kinds.count(trigger_kind) < MAX_ACTIVE_EVENT_PER_KIND


def pick_rotation(pool_ids: list[int], current_id: int | None) -> int | None:
    """The next quest id to activate when cycling a rotate-tag pool.

    Cycles by ascending id: the id after ``current_id`` wrapping around. A
    pool of one (or empty) has nowhere to rotate → None. When ``current_id``
    is not in the pool, start at the first id.
    """
    ordered = sorted(set(pool_ids))
    if len(ordered) <= 1:
        return None
    if current_id is None or current_id not in ordered:
        return ordered[0]
    idx = ordered.index(current_id)
    return ordered[(idx + 1) % len(ordered)]


def reward_band(qtype: str) -> tuple[int, int] | None:
    """The suggested (low, high) reward range for a quest type, or None.

    Advisory only — the dashboard warns out-of-band but saves anyway.
    Community has no band (author's call).
    """
    return _REWARD_BANDS.get(qtype)


# ── per-user quest board (spec §4.6) ──────────────────────────────────


def _seed(*parts: object) -> int:
    """A stable 64-bit seed from the parts — same across processes/versions.

    ``hash()`` is salted per-process and ``random.seed(str)`` isn't guaranteed
    stable, so we hash explicitly. Determinism is what makes the board a pure
    function of ``(user, period)`` with no stored assignment table.
    """
    digest = hashlib.sha256(":".join(str(p) for p in parts).encode()).hexdigest()
    return int(digest[:16], 16)


def period_index(qtype: str, local_day: str) -> int:
    """A monotonic integer index for the period ``local_day`` falls in.

    One integer per daily/weekly/monthly period, increasing over time — the
    board walks the per-user pool by this index, so a member's set advances
    exactly once per period and never mid-period (counted progress can't
    fragment). Community/event have no calendar period and raise.
    """
    d = date.fromisoformat(local_day)
    if qtype == "daily":
        return d.toordinal()
    if qtype == "weekly":
        iso = d.isocalendar()
        return iso.year * 53 + iso.week
    if qtype == "monthly":
        return d.year * 12 + (d.month - 1)
    raise ValueError(f"quest type has no board period: {qtype!r}")


def assigned_quest_ids(
    pool_ids: list[int], user_id: int, index: int, n: int
) -> list[int]:
    """The ``n`` quest ids a member draws from a cadence pool for a period.

    The pool is shuffled deterministically per member (so two members get
    different sets), then walked ``n``-at-a-time by ``index``, spacing a
    member's repeats roughly ``floor(len/n)`` periods apart — exactly a full
    cycle when ``n`` divides the pool size, approximate otherwise (e.g. len 5,
    n 2 recurs some ids every 2 periods). ``n >= len`` (or a tiny pool)
    degrades gracefully to "the whole pool". Returns sorted ids.
    """
    ordered = sorted(set(pool_ids))
    m = len(ordered)
    if m == 0 or n <= 0:
        return []
    if n >= m:
        return ordered
    # Per-member shuffle: order the pool by a per-(user, quest) hash.
    shuffled = sorted(ordered, key=lambda q: _seed(user_id, q))
    start = (index * n) % m
    picked = [shuffled[(start + i) % m] for i in range(n)]
    return sorted(picked)


def has_board(qtype: str) -> bool:
    """Whether this cadence draws a personal board at all.

    True for daily/weekly/monthly regardless of the configured size — a
    guild that set the size to 0 still *has* a board, it's just empty. Gate
    board filtering on this, never on ``board_size(...) > 0``.
    """
    return qtype in BOARD_CADENCES


def board_size(qtype: str, sizes: dict[str, int] | None = None) -> int:
    """How many quests a member draws from this cadence's pool per period.

    ``sizes`` overrides the defaults per cadence (the guild's configured
    board sizes); a cadence absent from it falls back to the default. 0 is a
    meaningful value — the cadence is off for this guild.
    """
    if sizes is not None and qtype in sizes:
        return sizes[qtype]
    return PERSONAL_BOARD_SIZE.get(qtype, 0)


def effective_target(
    target_count: int,
    target_min: int,
    target_max: int,
    *,
    user_id: int,
    quest_id: int,
    period: str,
) -> int:
    """A counted quest's target for one member+period.

    With a band (``0 < target_min < target_max``) the target is drawn from a
    Gaussian centered on the band, clamped to ``[min, max]`` — deterministic on
    ``(user, quest, period)`` so it's stable all period and varies run to run.
    Without a band it's the fixed ``target_count``. Never below 1.
    """
    if not (0 < target_min < target_max):
        return max(1, int(target_count))
    rng = random.Random(_seed(user_id, quest_id, period))
    mu = (target_min + target_max) / 2
    # ~95% of the mass lands inside the band before clamping; the tails clamp.
    sigma = (target_max - target_min) / 4 or 1
    draw = round(rng.gauss(mu, sigma))
    return max(target_min, min(target_max, draw))


# ── trigger-phrase verification (spec §4.4) ───────────────────────────


def parse_trigger_words(raw: str) -> list[str]:
    """Split a stored ``trigger_words`` value into clean phrases.

    Phrases are separated by commas or newlines; surrounding whitespace is
    stripped, internal runs of whitespace collapse to one space, and
    duplicates (case-insensitive) keep their first occurrence.
    """
    seen: set[str] = set()
    out: list[str] = []
    for chunk in re.split(r"[,\n]", raw or ""):
        phrase = " ".join(chunk.split())
        if not phrase:
            continue
        key = phrase.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(phrase)
    return out


def compile_trigger_pattern(words: list[str]) -> re.Pattern[str] | None:
    """One case-insensitive pattern matching any phrase as a whole word.

    ``(?<!\\w)…(?!\\w)`` instead of ``\\b`` so phrases that start or end with
    non-word characters (e.g. ``:wave:``) still anchor correctly, and "gm"
    never matches inside "dogma". Whitespace inside a phrase matches any
    whitespace run. None when there are no phrases.
    """
    if not words:
        return None
    alternatives = [
        r"\s+".join(re.escape(token) for token in phrase.split())
        for phrase in words
    ]
    return re.compile(
        r"(?<!\w)(?:" + "|".join(alternatives) + r")(?!\w)",
        re.IGNORECASE,
    )


def message_matches_trigger(content: str, pattern: re.Pattern[str] | None) -> bool:
    """True when a message body contains one of the quest's trigger phrases."""
    return bool(pattern is not None and content and pattern.search(content))
