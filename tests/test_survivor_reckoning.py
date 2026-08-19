"""Tests for survivor/reckoning.py + tasks.py decisions + ghost streaks.

Stages 5 and 6a of docs/plans/survivor.md: the three-act data assembly,
leaver elimination (§6.14), the weekly-task
due logic over guild-local clocks, and the streak rules as decided
2026-08-17 (miss breaks, void neither, unsettled pending).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.survivor_service import (
    create_season,
    eliminate_player,
    get_season,
    update_config,
)
from bot_modules.survivor import tasks
from bot_modules.survivor.logic import ghost_streaks, join_season, place_pick
from bot_modules.survivor.reckoning import (
    build_reckoning_data,
    eulogy_for,
    eliminate_leavers,
    next_reckoning_week,
)
from bot_modules.survivor.settle import run_settle
from tests.db_template import migrated_db

GID = 100
YEAR = 2026
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc).timestamp()
HOUR = 3600.0
DAY = 24 * HOUR
THU = NOW + DAY
MON = NOW + 5 * DAY
AFTER_W1 = MON + 5 * HOUR

WEEK1 = [
    ("g-thu", "SEA", "NE", THU),
    ("g-mon", "KC", "LV", MON),
]
WEEK2 = [("g2", "SF", "ARI", NOW + 12 * DAY)]
WEEK3 = [("g3", "BUF", "MIA", NOW + 19 * DAY)]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    with open_db(db_path) as conn:
        for week, games in ((1, WEEK1), (2, WEEK2), (3, WEEK3)):
            for gid, home, away, ts in games:
                conn.execute(
                    "INSERT INTO nfl_games (season_year, week, game_id, home, away, kickoff_utc)"
                    " VALUES (?,?,?,?,?,?)",
                    (YEAR, week, gid, home, away, _iso(ts)),
                )
    return db_path


def _season(conn, **overrides):
    return get_season(conn, create_season(conn, GID, "S", YEAR,
                                          overrides=overrides or None))


def _finalize(conn, game_id, winner):
    conn.execute(
        "UPDATE nfl_games SET status = 'final', winner = ? WHERE game_id = ?",
        (winner, game_id),
    )


def _settled_week1(conn, season):
    """Two players: 1 picks SEA (wins), 2 picks NE (dies, sudden death)."""
    join_season(conn, season, 1, NOW)
    join_season(conn, season, 2, NOW)
    place_pick(conn, season, 1, 1, "SEA", NOW)
    place_pick(conn, season, 2, 1, "NE", NOW)
    _finalize(conn, "g-thu", "SEA")
    _finalize(conn, "g-mon", "KC")
    run_settle(conn, season, AFTER_W1)


# ── reckoning week + rotation ──────────────────────────────────────────


def test_next_reckoning_week_gates_on_elapsed(db):
    with open_db(db) as conn:
        season = _season(conn)
        assert next_reckoning_week(conn, season, NOW) is None  # nothing kicked
        assert next_reckoning_week(conn, season, AFTER_W1) == 1
        update_config(conn, season["id"], {"last_reckoned_week": 1})
        season = get_season(conn, season["id"])
        assert next_reckoning_week(conn, season, AFTER_W1) is None  # wk2 open


# ── the three acts ─────────────────────────────────────────────────────


def test_reckoning_data_toll_ledger_deaths(db):
    with open_db(db) as conn:
        season = _season(conn, strikes=0)
        _settled_week1(conn, season)
        data = build_reckoning_data(conn, season, 1, AFTER_W1)
        assert (data["before"], data["after"]) == (2, 1)
        # Deaths sorted first, states honest.
        assert [e["user_id"] for e in data["deaths"]] == [2]
        assert data["deaths"][0]["fatal_team"] == "NE"
        assert [e["user_id"] for e in data["ledger"]] == [1]
        assert data["ledger"][0]["teams"].startswith("SEA")
        assert data["stragglers"] == 0


def test_reckoning_no_deaths_is_just_the_numbers(db):
    # Just-the-facts (2026-08-18): a clean week carries no special line —
    # the survivors delta in the embed says it.
    with open_db(db) as conn:
        season = _season(conn)
        join_season(conn, season, 1, NOW)
        place_pick(conn, season, 1, 1, "SEA", NOW)
        _finalize(conn, "g-thu", "SEA")
        _finalize(conn, "g-mon", "KC")
        run_settle(conn, season, AFTER_W1)
        data = build_reckoning_data(conn, season, 1, AFTER_W1)
        assert data["deaths"] == []
        assert "toll_line" not in data


@pytest.mark.parametrize(
    ("source", "needle"),
    [
        pytest.param("cap", "out of auto-assigns", id="cap-fixed-line"),
        pytest.param("missed", "never picked", id="missed-fixed-line"),
        pytest.param("left", "left the server", id="leaver-fixed-line"),
    ],
)
def test_eulogy_fixed_lines_by_source(source, needle):
    entry = {"source": source, "fatal_team": None, "user_id": 1}
    line = eulogy_for(entry, {"week": 3}, "Loaf", 0)
    assert needle in line and "Loaf" in line


def test_eulogy_football_death_states_the_facts():
    # Just-the-facts (2026-08-18): team, result, week — no corpus.
    entry = {"source": "picks", "fatal_team": "NE", "user_id": 1}
    line = eulogy_for(entry, {"week": 3}, "Loaf", 0)
    assert line == "**Loaf** — NE lost. Eliminated in Week 3."
    assert "{" not in line
    # A brace in a nickname must not crash or leak a slot (stage-1 contract).
    braced = eulogy_for(entry, {"week": 3}, "{Loaf}", 0)
    assert "{Loaf}" in braced


def test_leavers_die_at_the_reckoning(db):
    with open_db(db) as conn:
        season = _season(conn, strikes=0)  # player 2's loss is fatal
        _settled_week1(conn, season)
        # Player 1 (alive) has left the guild; player 2 is already a ghost.
        gone = eliminate_leavers(conn, season, 1, present_ids={2, 999})
        assert gone == [1]
        row = conn.execute(
            "SELECT status, elimination_source FROM survivor_players "
            "WHERE season_id = ? AND user_id = 1",
            (season["id"],),
        ).fetchone()
        assert (row["status"], row["elimination_source"]) == ("ghost", "left")
        data = build_reckoning_data(conn, season, 1, AFTER_W1)
        assert {e["user_id"] for e in data["deaths"]} == {1, 2}


def test_arrivals_gate_counts_gauntlet_walkers_only(db):
    with open_db(db) as conn:
        season = _season(conn)
        # Enrolled before the first kickoff: never walked anything.
        join_season(conn, season, 6, NOW)
        # Mid-week join after Thursday kicked: a real gate arrival.
        join_season(conn, season, 7, THU + HOUR)
        data = build_reckoning_data(conn, season, 1, AFTER_W1)
        assert [a["user_id"] for a in data["arrivals"]] == [7]


# ── ghost streaks (§1.7 as decided) ────────────────────────────────────


def test_ghost_streaks_miss_breaks_void_neither(db):
    with open_db(db) as conn:
        season = _season(conn)
        join_season(conn, season, 1, NOW)
        eliminate_player(conn, season["id"], 1, week=1)
        # wk2: ghost picks SF, wins. wk3: no pick (miss) → streak resets;
        # best stays 1.
        conn.execute(
            "INSERT INTO survivor_picks (season_id, guild_id, user_id, week, slot,"
            " team, game_id, result) VALUES (?, ?, 1, 2, 1, 'SF', 'g2', 'win')",
            (season["id"], GID),
        )
        now = NOW + 21 * DAY  # weeks 1-3 all elapsed
        streaks = ghost_streaks(conn, season, now)
        assert streaks[1] == {"current": 0, "best": 1}
        # A void in wk3 instead of a miss leaves the streak untouched.
        conn.execute(
            "INSERT INTO survivor_picks (season_id, guild_id, user_id, week, slot,"
            " team, game_id, result) VALUES (?, ?, 1, 3, 1, 'BUF', 'g3', 'void')",
            (season["id"], GID),
        )
        streaks = ghost_streaks(conn, season, now)
        assert streaks[1] == {"current": 1, "best": 1}


def test_ghost_streaks_ignore_pre_death_wins(db):
    with open_db(db) as conn:
        season = _season(conn, strikes=0)  # player 2's loss is fatal
        _settled_week1(conn, season)  # player 1 WON week 1, alive
        # Player 2 died wk1; their wk1 loss must not seed a streak, and the
        # living player has no streak entry at all.
        streaks = ghost_streaks(conn, season, AFTER_W1)
        assert streaks[2] == {"current": 0, "best": 0}
        assert 1 not in streaks


# ── weekly-task due logic ──────────────────────────────────────────────


def _cfg_season(conn, **overrides):
    season = _season(conn, **overrides)
    update_config(conn, season["id"], {"channel_id": 555})
    return get_season(conn, season["id"])


@pytest.mark.parametrize(
    ("dow_offset_days", "hour", "due"),
    [
        # NOW is Wednesday 12:00 UTC; slate_hour default 9, offset 0.
        pytest.param(0, 12, True, id="wednesday-after-hour"),
        pytest.param(-1, 12, False, id="tuesday-not-yet"),
        pytest.param(2, 12, True, id="friday-catchup"),
    ],
)
def test_slate_due_frame(db, dow_offset_days, hour, due):
    with open_db(db) as conn:
        season = _cfg_season(conn)
        now = NOW + dow_offset_days * DAY
        got = tasks.slate_due(conn, season, now, 0.0)
        assert (got == 1) is due


def test_slate_due_once_per_week(db):
    with open_db(db) as conn:
        season = _cfg_season(conn)
        assert tasks.slate_due(conn, season, NOW, 0.0) == 1
        update_config(conn, season["id"], {"last_slate_week": 1})
        season = get_season(conn, season["id"])
        assert tasks.slate_due(conn, season, NOW, 0.0) is None
        # Next week's slate becomes due after MNF rolls pick_week forward.
        assert tasks.slate_due(conn, season, NOW + 7 * DAY, 0.0) == 2


def test_reckoning_due_tuesday_after_week_elapsed(db):
    with open_db(db) as conn:
        season = _cfg_season(conn)
        # Week 1 fully kicked Monday night; Tuesday 9am+ is the moment.
        tue_early = MON + 8 * HOUR    # Tue 01:00 UTC
        tue_nine = MON + 21 * HOUR    # Tue 14:00 UTC — past 9 local (UTC)
        assert tasks.reckoning_due(conn, season, tue_early, 0.0) is None
        assert tasks.reckoning_due(conn, season, tue_nine, 0.0) == 1


def test_lastcall_due_saturday(db):
    with open_db(db) as conn:
        season = _cfg_season(conn)
        sat_evening = NOW + 3 * DAY + 7 * HOUR  # Sat 19:00 UTC, hour 18 due
        assert tasks.lastcall_due(conn, season, NOW, 0.0) is None  # Wednesday
        assert tasks.lastcall_due(conn, season, sat_evening, 0.0) == 1


# ── the slate as weekly mini-announcement (2026-08-18) ─────────────────


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        pytest.param(
            {"buyin": 0, "late_entry": "gauntlet", "gauntlet_mode": False},
            "free entry", id="pre-kickoff-free",
        ),
        pytest.param(
            {"buyin": 100, "late_entry": "gauntlet", "gauntlet_mode": True},
            "auto-replay as the Gauntlet", id="gauntlet-era",
        ),
        pytest.param(
            {"buyin": 0, "late_entry": "ghost_only", "gauntlet_mode": True},
            "Ghost Streak side game", id="ghost-only",
        ),
    ],
)
def test_slate_join_line_variants(kwargs, expected):
    from bot_modules.survivor.reckoning import slate_join_line

    line = slate_join_line(**kwargs)
    assert line is not None and expected in line


def test_slate_join_line_closed_is_none():
    from bot_modules.survivor.reckoning import slate_join_line

    assert slate_join_line(
        buyin=0, late_entry="closed", gauntlet_mode=True
    ) is None


def test_panel_embed_carries_the_door_unless_closed():
    from bot_modules.survivor.embeds import build_panel_embed

    games = [{"home": "SEA", "away": "NE", "kickoff_ts": 1_700_000_000.0}]
    kwargs = dict(
        season_name="S", entrants=9, buyin=0, gauntlet_mode=True,
        week=3, games=games, alive=7, eliminated=2, picked=5,
        pot=8150, ghost_pot=2000,
    )
    open_embed = build_panel_embed(**{**kwargs, "late_entry": "gauntlet"})
    assert any(f.name == "New Here?" for f in open_embed.fields)
    assert "Alive **7**" in open_embed.description
    assert "Pot 🪙 **8,150** Coins" in open_embed.description
    games_field = next(
        f for f in open_embed.fields if f.name == "This Week's Games"
    )
    assert "**NE** @ **SEA**" in games_field.value

    closed_embed = build_panel_embed(**{**kwargs, "late_entry": "closed"})
    assert all(f.name != "New Here?" for f in closed_embed.fields)


def test_panel_embed_enrolling_face():
    # No pick week yet (schedule empty/finished): the pre-kickoff pitch,
    # no games section, entrant count front and center.
    from bot_modules.survivor.embeds import build_panel_embed

    embed = build_panel_embed(
        season_name="S", entrants=4, buyin=0, gauntlet_mode=False, pot=8000,
    )
    assert "**4** players in" in embed.description
    assert all(f.name != "This Week's Games" for f in embed.fields)
    assert any(f.name == "New Here?" for f in embed.fields)


# ── history (panel button + public command share one builder) ──────────


def test_history_embed_public_hides_unrevealed_own_shows_tagged():
    from bot_modules.survivor.embeds import build_history_embed

    rows = [
        {"week": 1, "team": "SEA", "result": "win", "auto_assigned": False,
         "opponent": "NE", "is_home": True, "winner": "SEA"},
        {"week": 2, "team": "KC", "result": "loss", "auto_assigned": True,
         "opponent": "LV", "is_home": False, "winner": "LV"},
        {"week": 3, "team": "SF", "result": None, "auto_assigned": False,
         "opponent": "ARI", "is_home": True, "winner": None},
    ]
    public = build_history_embed(
        rows, display_name="Loaf", revealed_week=2, own=False
    )
    assert "SF" not in public.description          # secrecy holds
    assert "Week 1: **SEA** vs NE · ✅ won" in public.description
    assert "Week 2: **KC** at LV · 💀 LV won 📎" in public.description
    assert "Revealed picks only" in public.footer.text

    own = build_history_embed(
        rows, display_name="Loaf", revealed_week=2, own=True
    )
    assert "Week 3: **SF** vs ARI" in own.description  # your own eyes only
    assert "⏳ awaiting result" in own.description
    assert "hidden from others" in own.description
    assert "Only you can see this" in own.footer.text


def test_panel_view_button_roster():
    from bot_modules.survivor.views import panel_view

    ids = [c.custom_id for c in panel_view(7, join_open=True).children]
    assert ids == [
        "survivor_slate:7", "survivor_join:7", "survivor_history:7"
    ]
    ids = [c.custom_id for c in panel_view(7, join_open=False).children]
    assert ids == ["survivor_slate:7", "survivor_history:7"]


# ── the weekly prize (2026-08-18) ──────────────────────────────────────


def test_weekly_prize_pays_all_win_players_only(db):
    from bot_modules.services import economy_service
    from bot_modules.survivor.reckoning import pay_weekly_wins

    with open_db(db) as conn:
        season = _season(conn)  # weekly_win_coins default 25
        _settled_week1(conn, season)  # 1 won (SEA), 2 lost (NE)
        paid = pay_weekly_wins(conn, season, 1)
        assert paid == [(1, 25)]
        assert economy_service.get_balance(conn, GID, 1) == 25
        assert economy_service.get_balance(conn, GID, 2) == 0
        row = conn.execute(
            "SELECT kind, amount, meta FROM econ_ledger WHERE user_id = 1"
        ).fetchone()
        assert row["kind"] == "survivor_weekly_win" and row["amount"] == 25
        import json as _json

        meta = _json.loads(row["meta"])
        assert (meta["season_id"], meta["week"]) == (season["id"], 1)


def test_weekly_prize_ghosts_collect_and_double_pick_split_does_not(db):
    from bot_modules.survivor.reckoning import pay_weekly_wins

    with open_db(db) as conn:
        season = _season(conn)
        join_season(conn, season, 1, NOW)   # ghost, wins
        join_season(conn, season, 2, NOW)   # split week: win + loss
        eliminate_player(conn, season["id"], 1, week=0)
        conn.execute(
            "INSERT INTO survivor_picks (season_id, guild_id, user_id, week,"
            " slot, team, game_id, result) VALUES"
            " (?, ?, 1, 1, 1, 'SEA', 'g-thu', 'win'),"
            " (?, ?, 2, 1, 1, 'SEA', 'g-thu', 'win'),"
            " (?, ?, 2, 1, 2, 'LV', 'g-mon', 'loss')",
            (season["id"], GID, season["id"], GID, season["id"], GID),
        )
        paid = pay_weekly_wins(conn, season, 1)
        assert paid == [(1, 25)]  # the ghost collects; the split week doesn't


def test_weekly_prize_dial_zero_is_off(db):
    from bot_modules.survivor.reckoning import pay_weekly_wins

    with open_db(db) as conn:
        season = _season(conn, weekly_win_coins=0)
        _settled_week1(conn, season)
        assert pay_weekly_wins(conn, season, 1) == []


def test_reckoning_embed_shows_the_prize_line():
    from bot_modules.survivor.reckoning import build_reckoning_embed

    data = {
        "week": 1, "before": 3, "after": 3,
        "pots": {"main": 8000, "ghost": 2000},
        "stragglers": 0, "arrivals": [],
        "deaths": [], "ledger": [],
        "streak_strip": [], "streak_record": 0,
        "weekly_win": {"count": 14, "amount": 25},
    }
    embed = build_reckoning_embed(data, lambda u: f"P{u}", season_name="S")
    assert (
        "💰 14 correct pick(s) collect 🪙 **25** Coins each"
        in embed.description
    )


# ── the join echo (2026-08-18) ─────────────────────────────────────────


def test_survivor_join_echo_source_registered():
    from bot_modules.services.event_echo_logic import (
        SOURCE_SURVIVOR_JOIN,
        spec_for,
    )

    spec = spec_for(SOURCE_SURVIVOR_JOIN)
    assert "joined the Survivor pool" in spec.headline
    assert not spec.exempt  # joins recur; skip-don't-queue is right


def test_join_echo_detail_line():
    from bot_modules.survivor.views import join_echo_detail

    line = join_echo_detail(14, 8150)
    assert "14 players in" in line and "pot 🪙 8,150 Coins" in line

# ── why nothing was due (2026-08-18) ──────────────────────────────────
#
# Every gate routes through pick_week, so a season on a year with no
# ingested schedule looks exactly like a quiet week: all three return None
# and the forced run posts nothing. idle_reason is what the dashboard says
# instead of reporting success — the silence is what cost a debugging round
# when the first live season was created on year 2035.

def test_idle_reason_names_a_missing_schedule(db):
    with open_db(db) as conn:
        season = get_season(
            conn, create_season(conn, GID + 1, "No Schedule", 2035)
        )
        assert tasks.idle_reason(conn, season, NOW) == (
            "no schedule ingested for 2035 — generate or ingest one "
            "before any weekly task can be due"
        )


def test_idle_reason_names_the_once_per_week_state(db):
    with open_db(db) as conn:
        season = _cfg_season(conn)
        update_config(conn, season["id"], {"last_slate_week": 1})
        season = get_season(conn, season["id"])
        assert "already run for this week" in tasks.idle_reason(conn, season, NOW)


def test_idle_reason_names_the_gap_between_weeks(db):
    with open_db(db) as conn:
        season = _cfg_season(conn)
        after_everything = NOW + 60 * DAY
        assert "between weeks" in tasks.idle_reason(
            conn, season, after_everything
        )


# ── last call carries the pick button (2026-08-18) ────────────────────
#
# The Saturday nudge used to point at /survivor pick; now the DM (and the
# closed-DM channel fallback) carries the same persistent SlatePickButton
# as the channel panel — the nudge is the door, not directions to one.
# This is deliberate wiring, so it gets the one wiring assertion.

class _FakeMember:
    def __init__(self, uid, fail=False):
        self.id = uid
        self._fail = fail
        self.sent = []

    async def send(self, content=None, **kwargs):
        import discord as _d

        if self._fail:
            resp = type("R", (), {"status": 403, "reason": "Forbidden"})()
            raise _d.Forbidden(resp, "closed DMs")
        self.sent.append((content, kwargs))


class _FakeChannel:
    def __init__(self, guild):
        self.guild = guild
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


class _FakeGuild:
    def __init__(self, members, channel_id):
        self._members = {m.id: m for m in members}
        self._channel_id = channel_id
        self.channel = _FakeChannel(self)

    def get_member(self, uid):
        return self._members.get(uid)

    def get_channel(self, cid):
        return self.channel if cid == self._channel_id else None


class _FakeBot:
    def __init__(self, guild):
        self._guild = guild

    def get_guild(self, gid):
        return self._guild


def _pick_button_ids(view):
    return [
        c.custom_id for c in view.children
        if getattr(c, "custom_id", "").startswith("survivor_slate:")
    ]


@pytest.mark.asyncio
async def test_last_call_dm_carries_the_pick_button(db, monkeypatch):
    import discord  # noqa: F401 — fakes raise discord errors

    with open_db(db) as conn:
        season = _cfg_season(conn)
        join_season(conn, season, 1, NOW)
        join_season(conn, season, 2, NOW)
        season = get_season(conn, season["id"])
    member_ok = _FakeMember(1)
    member_closed = _FakeMember(2, fail=True)
    guild = _FakeGuild(
        [member_ok, member_closed], int(season["config"]["channel_id"] or 0)
    )
    bot = _FakeBot(guild)
    # _channel() isinstance-checks discord.TextChannel, which a test fake
    # can't be — route it to the fake so the closed-DM fallback is reachable.
    monkeypatch.setattr(tasks, "_channel", lambda b, se: guild.channel)

    handled = await tasks.send_last_call(bot, db, season, 1, NOW)

    assert handled is True
    # The open-DM member got the nudge with the button attached.
    (content, kwargs), = member_ok.sent
    assert "Week 1" in content and "/survivor pick" not in content
    assert _pick_button_ids(kwargs["view"]) == [f"survivor_slate:{season['id']}"]
    # The closed-DM member fell back to the channel — with the button too.
    (fb_content, fb_kwargs), = guild.channel.sent
    assert "<@2>" in fb_content
    assert _pick_button_ids(fb_kwargs["view"]) == [f"survivor_slate:{season['id']}"]


# ── role reconcile (2026-08-18, Billy's #10) ──────────────────────────
#
# Every decision pass repairs life-state roles: alive holds Survivor,
# ghost holds Ghost. The point is drift — the a41e70e2 join crash left a
# member charged but roleless, and nothing would ever have granted it.
# swap_member_roles checks the cached roles first, so the no-drift case
# must make zero API calls.

class _FakeRole:
    def __init__(self, rid):
        self.id = rid


class _RoleMember:
    def __init__(self, uid, roles):
        self.id = uid
        self.roles = list(roles)
        self.calls = []

    async def add_roles(self, role, reason=None):
        self.calls.append(("add", role.id))
        self.roles.append(role)

    async def remove_roles(self, role, reason=None):
        self.calls.append(("remove", role.id))
        self.roles.remove(role)


class _RoleGuild:
    def __init__(self, members, roles):
        self._members = {m.id: m for m in members}
        self._roles = {r.id: r for r in roles}

    def get_member(self, uid):
        return self._members.get(uid)

    def get_role(self, rid):
        return self._roles.get(rid)


@pytest.mark.asyncio
async def test_reconcile_roles_heals_drift_and_skips_the_settled(db):
    with open_db(db) as conn:
        season = _cfg_season(conn)
        update_config(conn, season["id"], {
            "role_survivor_id": 11, "role_ghost_id": 22,
        })
        season = get_season(conn, season["id"])
        join_season(conn, season, 1, NOW)   # alive, roleless (crashed join)
        join_season(conn, season, 2, NOW)   # alive, already correct
        join_season(conn, season, 3, NOW)
        eliminate_player(conn, season["id"], 3, week=1)  # ghost, still 🏈

    survivor, ghost = _FakeRole(11), _FakeRole(22)
    roleless = _RoleMember(1, [])
    settled = _RoleMember(2, [survivor])
    stale_ghost = _RoleMember(3, [survivor])
    guild = _RoleGuild([roleless, settled, stale_ghost], [survivor, ghost])
    bot = _FakeBot(guild)

    await tasks.reconcile_roles(bot, db, season)

    assert roleless.calls == [("add", 11)]          # the crashed-join heal
    assert settled.calls == []                       # no drift → zero API calls
    assert stale_ghost.calls == [("remove", 11), ("add", 22)]


# ── currency vocabulary (2026-08-19, Billy's #12 follow-up) ───────────
#
# The style guide's rule: amounts render with the guild-configured
# emoji/name, singular at 1, never a hard-coded "coins". The second live
# guild runs its own denomination, so this is behavior, not pedantry.

def test_money_renders_in_the_guilds_own_denomination():
    from bot_modules.services.economy_service import EconSettings
    from bot_modules.survivor.embeds import build_panel_embed
    from bot_modules.survivor.reckoning import slate_join_line

    nuts = EconSettings(
        currency_name="Nut", currency_plural="Nuts", currency_emoji="🥜"
    )
    embed = build_panel_embed(
        season_name="S", entrants=3, buyin=100, gauntlet_mode=False,
        pot=10300, settings=nuts,
    )
    assert "Pot 🥜 **10,300** Nuts" in embed.description
    assert "coins" not in embed.description.lower()
    line = slate_join_line(
        buyin=1, late_entry="gauntlet", gauntlet_mode=False, settings=nuts
    )
    assert "🥜 **1** Nut to enter" in line  # singular at 1
