"""Pure logic for the daily feature-channel rotation.

No Discord calls, no DB access, no clock reads beyond what a caller passes in
— everything here is a function of its arguments so the rotation is testable
without a guild or a database.

The central choice: **which rooms are featured is derived, never stored.**
``featured_indices`` walks the pool by the day's ordinal exactly as
``quests.assigned_quest_ids`` walks the quest pool by ``period_index``. Two
things fall out of that. The rotation and the quest board advance in lockstep
by construction rather than by two schedulers agreeing, and a bot that was
offline for three days returns to the correct room instead of three behind.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

# A room whose entry point is a button on an in-channel message can't be used
# while the room is hidden; one reached by a slash command or an ephemeral
# panel can. Only the former belong in a pool row's ``blocked_kinds``.
SECONDS_PER_HOUR = 3600


@dataclass(frozen=True)
class Room:
    """One row of the rotation pool, as the logic layer sees it."""

    channel_id: int
    position: int = 0
    label: str = ""
    blurb: str = ""
    in_rotation: bool = True
    hide_when_off: bool = True
    announce: bool = True
    quest_kinds: tuple[str, ...] = ()
    blocked_kinds: tuple[str, ...] = ()
    launch_game: str = ""
    launch_options: str = ""

    def display(self) -> str:
        """What the announcement calls this room."""
        return self.label.strip() or f"<#{self.channel_id}>"


@dataclass(frozen=True)
class GamePlan:
    """Which rooms should have a game started, and which one ended.

    Separate from :class:`VisibilityPlan` because the two are applied at
    different points of the same flip — the ends go first, while their rooms
    are still visible, and the starts last, once their rooms are open.
    """

    start: tuple[tuple[int, str], ...] = ()
    end: tuple[tuple[int, str], ...] = ()

    def is_empty(self) -> bool:
        return not self.start and not self.end


@dataclass(frozen=True)
class VisibilityPlan:
    """Which channels should end the flip visible, and which hidden."""

    show: tuple[int, ...] = ()
    hide: tuple[int, ...] = ()

    def is_empty(self) -> bool:
        return not self.show and not self.hide


@dataclass(frozen=True)
class RotationDay:
    """Everything derived about one local day."""

    local_day: str
    featured: tuple[int, ...] = ()
    plan: VisibilityPlan = field(default_factory=VisibilityPlan)
    blocked_quest_kinds: frozenset[str] = frozenset()
    featured_quest_kinds: frozenset[str] = frozenset()
    games: GamePlan = field(default_factory=GamePlan)


# ── clock ────────────────────────────────────────────────────────────────────


def local_now(now: float, tz_offset_hours: float) -> datetime:
    """``now`` (epoch seconds) as a naive local datetime at a fixed offset.

    Fixed offset, no DST — the same convention ``announcements_service`` uses,
    so a guild's announcement hour and its rotation hour can't drift apart
    twice a year. The offset itself is the guild's shared ``tz_offset_hours``
    config key (``store.rotation_tz``); the rotation has none of its own, so
    the flip cannot land on a different midnight than the quest board's.
    """
    return datetime.fromtimestamp(now, tz=timezone.utc) + timedelta(
        hours=tz_offset_hours
    )


def local_day(now: float, tz_offset_hours: float) -> str:
    """The local calendar day as ``YYYY-MM-DD``.

    This is the rotation's period key and the exactly-once guard's value, and
    it is deliberately the same string the economy's quest board uses for its
    own day — the two must agree or the featured room and the board diverge.
    """
    return local_now(now, tz_offset_hours).date().isoformat()


def local_hour(now: float, tz_offset_hours: float) -> int:
    """Hour of the local day, 0–23."""
    return local_now(now, tz_offset_hours).hour


def day_ordinal(local_day_str: str) -> int:
    """Proleptic-Gregorian ordinal for a ``YYYY-MM-DD`` day.

    Matches ``quests.period_index('daily', …)``, which is ``date.toordinal()``
    — so the rotation's walk and the quest board's walk index the same integer.
    """
    return date.fromisoformat(local_day_str).toordinal()


# ── the rotation itself ──────────────────────────────────────────────────────


def rotating_rooms(rooms: list[Room]) -> list[Room]:
    """Pool members that actually take part, in a stable order.

    Ordered by ``position`` then ``channel_id`` so two rooms sharing a position
    (easy to do by dragging in the panel) still order deterministically — the
    derived rotation would otherwise depend on row insertion order.
    """
    return sorted(
        (r for r in rooms if r.in_rotation),
        key=lambda r: (r.position, r.channel_id),
    )


def featured_indices(ordinal: int, rooms_per_day: int, pool_len: int) -> list[int]:
    """Indices into the ordered pool that are featured on day ``ordinal``.

    ``start = (ordinal * rooms_per_day) % pool_len`` then a contiguous wrap —
    the same window walk the quest board uses. When ``rooms_per_day`` divides
    the pool the cycle is exact; otherwise it still visits every room, just
    with an uneven period. ``rooms_per_day >= pool_len`` degrades to "all of
    them", which is the honest answer to a pool smaller than the dial.
    """
    if pool_len <= 0 or rooms_per_day <= 0:
        return []
    n = min(rooms_per_day, pool_len)
    start = (ordinal * n) % pool_len
    return [(start + i) % pool_len for i in range(n)]


def featured_channel_ids(
    rooms: list[Room], ordinal: int, rooms_per_day: int
) -> list[int]:
    """Channel ids featured on day ``ordinal``."""
    ordered = rotating_rooms(rooms)
    return [ordered[i].channel_id for i in featured_indices(ordinal, rooms_per_day, len(ordered))]


def plan_visibility(
    rooms: list[Room], featured: list[int], *, protected: set[int] | None = None
) -> VisibilityPlan:
    """Which pool channels to reveal and which to hide for this day.

    A room opts out of hiding with ``hide_when_off`` — it stays visible all the
    time and simply takes its turn being *featured*. ``protected`` is the
    caller's guard list (the announcement channel above all): a channel there is
    never hidden, because hiding the room the announcement posts into would
    hide the announcement.

    A room with ``in_rotation`` off is *shown*, never merely skipped. It takes
    no turn at being featured, but it must still be reopened: un-ticking the
    box on a room that happens to be hidden today would otherwise strand it
    hidden forever, since nothing else would ever plan its return. "Not taking
    part" has to mean "open", because that is the state a pool member is in
    when the feature is doing nothing.
    """
    guarded = protected or set()
    featured_set = set(featured)
    show: list[int] = [r.channel_id for r in rooms if not r.in_rotation]
    hide: list[int] = []
    for room in rotating_rooms(rooms):
        if room.channel_id in featured_set or not room.hide_when_off:
            show.append(room.channel_id)
        elif room.channel_id in guarded:
            show.append(room.channel_id)
        else:
            hide.append(room.channel_id)
    return VisibilityPlan(show=tuple(show), hide=tuple(hide))


def plan_games(rooms: list[Room], featured: list[int]) -> GamePlan:
    """Which rooms start a game today, and which have one ended.

    Only rooms carrying a ``launch_game`` are in either list: the rotation
    manages the lifecycle of the games it was told about and reaches into no
    others, so a room like whisper or confessions keeps the "out of sight,
    still running" behaviour the feature shipped with.

    Both lists carry ``(channel_id, game_key)``: the key is what tells the
    ender which of ``bot.game_channel_closers`` to consult, for a game like
    Risky Rolls whose rounds live in memory rather than in
    ``games_active_games`` and are therefore invisible to a table lookup.

    **Start** is the featured rooms that declare a game. **End** is every other
    room that declares one — keyed on "not featured today" rather than on "this
    flip hid it", for two reasons. A room with ``hide_when_off`` unticked never
    appears in the hide plan, yet it stops being the featured room and its game
    should still end. And the whole module derives the day from an ordinal so a
    bot that was offline for three days returns to the right room; an end that
    required having *observed* yesterday's flip would give that up. Ending a
    room with nothing running is a no-op, so the wider net costs a lookup.

    ``in_rotation`` is deliberately not consulted for the end list, matching
    :func:`plan_visibility`'s reasoning: un-ticking the box on a room whose
    game is running must still bring it to a defined state, or the game is
    stranded with nothing left that would ever close it.
    """
    featured_set = set(featured)
    start = tuple(
        (r.channel_id, r.launch_game)
        for r in rotating_rooms(rooms)
        if r.launch_game and r.channel_id in featured_set
    )
    end = tuple(
        (r.channel_id, r.launch_game)
        for r in rooms
        if r.launch_game and r.channel_id not in featured_set
    )
    return GamePlan(start=start, end=end)


def restore_all(rooms: list[Room]) -> VisibilityPlan:
    """The plan that reopens the whole pool — what "the rotation is off" means.

    Switching the feature off has to be reversible from the dashboard, and the
    derived day is unavailable precisely when it is off, so the "everything
    open" plan is built here rather than by asking for a day that doesn't
    exist.
    """
    return VisibilityPlan(show=tuple(r.channel_id for r in rooms), hide=())


def blocked_quest_kinds(rooms: list[Room], hidden: list[int]) -> frozenset[str]:
    """Quest trigger kinds that cannot be completed today.

    Only the kinds a *hidden* room declares as channel-bound. A room that is
    hidden but whose quests are reachable by slash command or ephemeral panel
    contributes nothing — which is most of them, and is why "out of sight,
    still running" costs so little.
    """
    hidden_set = set(hidden)
    out: set[str] = set()
    for room in rooms:
        if room.channel_id in hidden_set:
            out.update(k for k in room.blocked_kinds if k)
    return frozenset(out)


def featured_quest_kinds(rooms: list[Room], featured: list[int]) -> frozenset[str]:
    """Quest trigger kinds belonging to today's featured room(s).

    The featured pin draws from quests carrying one of these kinds, so the
    board points at the room that is open.
    """
    featured_set = set(featured)
    out: set[str] = set()
    for room in rooms:
        if room.channel_id in featured_set:
            out.update(k for k in room.quest_kinds if k)
    return frozenset(out)


def resolve_day(
    rooms: list[Room],
    *,
    local_day_str: str,
    rooms_per_day: int,
    protected: set[int] | None = None,
) -> RotationDay:
    """Everything derived for one local day, in one pass.

    The service calls this and then does I/O; nothing above this line touches
    Discord or SQLite, so a day's whole behaviour is assertable from a list of
    ``Room``s and a date string.
    """
    featured = featured_channel_ids(rooms, day_ordinal(local_day_str), rooms_per_day)
    plan = plan_visibility(rooms, featured, protected=protected)
    return RotationDay(
        local_day=local_day_str,
        featured=tuple(featured),
        plan=plan,
        blocked_quest_kinds=blocked_quest_kinds(rooms, list(plan.hide)),
        featured_quest_kinds=featured_quest_kinds(rooms, featured),
        games=plan_games(rooms, featured),
    )


# ── kind lists ───────────────────────────────────────────────────────────────


def parse_kinds(raw: str) -> tuple[str, ...]:
    """Split a stored comma-separated trigger-kind list.

    Whitespace-tolerant and order-preserving, dropping blanks and duplicates —
    the same shape ``quests.parse_trigger_words`` gives phrases.
    """
    seen: set[str] = set()
    out: list[str] = []
    for chunk in (raw or "").split(","):
        kind = chunk.strip()
        if not kind or kind in seen:
            continue
        seen.add(kind)
        out.append(kind)
    return tuple(out)


def format_kinds(kinds: object) -> str:
    """Render a kind list back to its stored comma-separated form."""
    if isinstance(kinds, str):
        kinds = parse_kinds(kinds)
    return ",".join(str(k).strip() for k in kinds if str(k).strip())  # type: ignore[union-attr]


# ── launch options ───────────────────────────────────────────────────────────


def parse_launch_options(raw: str) -> dict:
    """The stored launcher options as a dict; ``{}`` for blank or unreadable.

    Stored as a JSON string rather than a dict on :class:`Room` so the frozen
    dataclass stays hashable, and read back leniently: a corrupt value costs
    this room the launcher's *custom* options, never the launch itself, since
    every field in ``SCHEDULE_OPTION_SCHEMA`` carries its own default.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def format_launch_options(options: object) -> str:
    """Render launcher options back to their stored JSON form."""
    if isinstance(options, str):
        options = parse_launch_options(options)
    if not isinstance(options, dict) or not options:
        return ""
    return json.dumps(options, sort_keys=True)


# ── announcement copy ────────────────────────────────────────────────────────


def announcement_rooms(rooms: list[Room], featured: list[int]) -> list[Room]:
    """Featured rooms that opted in to being announced, in pool order."""
    featured_set = set(featured)
    return [
        r
        for r in rotating_rooms(rooms)
        if r.channel_id in featured_set and r.announce
    ]


def build_announcement(rooms: list[Room], featured: list[int]) -> tuple[str, str] | None:
    """``(title, body)`` for today's post, or ``None`` if there's nothing to say.

    Returns ``None`` rather than an empty embed when every featured room has
    ``announce`` off — a silent day is a legitimate configuration, and posting
    a contentless card would be worse than posting nothing.
    """
    picked = announcement_rooms(rooms, featured)
    if not picked:
        return None
    if len(picked) == 1:
        room = picked[0]
        title = "Today's feature"
        body = f"**{room.display()}** is open today."
    else:
        title = "Today's features"
        names = ", ".join(r.display() for r in picked)
        body = f"**{names}** are open today."
    blurbs = [f"> {r.blurb.strip()}" for r in picked if r.blurb.strip()]
    if blurbs:
        body = body + "\n\n" + "\n".join(blurbs)
    return title, body
