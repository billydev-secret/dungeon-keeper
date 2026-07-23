"""Tests for setup-gap detection — what the server isn't using."""

from __future__ import annotations

import sqlite3

from bot_modules.services import advisor_gaps as ag
from bot_modules.services.settings_registry import Feature, Setting

CH = "111111111111111111"


def _conn(rows, guild_id=1):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE config (guild_id INTEGER NOT NULL DEFAULT 0, key TEXT NOT NULL, "
        "value TEXT NOT NULL, PRIMARY KEY (guild_id, key))"
    )
    conn.executemany(
        "INSERT INTO config VALUES (?, ?, ?)", [(guild_id, k, v) for k, v in rows]
    )
    return conn


# A small synthetic feature so classification tests don't move when the real
# registry gains entries.
_TOGGLE = Setting("t_enabled", "On", "bool", required=True, default="0")
_CHAN = Setting("t_channel_id", "Channel", "channel", required=True, default="0")
_EXTRA = Setting("t_extra", "Extra", "text")
_FEATURE = Feature(
    slug="t", label="Test feature", panel="Config → Test",
    blurb="Does a test thing.", enable_key="t_enabled",
    settings=(_TOGGLE, _CHAN, _EXTRA),
)
_NO_SWITCH = Feature(
    slug="n", label="Switchless", panel="Config → N", blurb="No toggle.",
    settings=(Setting("n_a", "A", "channel", required=True, default="0"),
              Setting("n_b", "B", "channel", required=True, default="0")),
)
# Toggle plus *two* required settings, so "partial" is reachable alongside a switch.
_WIDE = Feature(
    slug="w", label="Wide", panel="Config → W", blurb="Two things to wire.",
    enable_key="w_enabled",
    settings=(Setting("w_enabled", "On", "bool", required=True, default="0"),
              Setting("w_a", "A", "channel", required=True, default="0"),
              Setting("w_b", "B", "channel", required=True, default="0")),
)


# ── classify_feature ────────────────────────────────────────────────────────


def test_nothing_set_is_unconfigured():
    gap = ag.classify_feature(_FEATURE, {})
    assert gap.status == "unconfigured"
    # missing describes wiring only — the toggle is reported via switch_on.
    assert {s.key for s in gap.missing} == {"t_channel_id"}
    assert gap.present == ()
    assert gap.switch_on is False
    assert gap.is_gap is True


def test_everything_set_and_on_is_configured():
    gap = ag.classify_feature(_FEATURE, {"t_enabled": "1", "t_channel_id": CH})
    assert gap.status == "configured"
    assert gap.missing == ()
    assert gap.is_gap is False


def test_wired_up_but_switched_off_is_ready_but_off():
    """The best suggestion there is — the work is done, the switch isn't flipped."""
    gap = ag.classify_feature(_FEATURE, {"t_enabled": "0", "t_channel_id": CH})
    assert gap.status == "ready_but_off"
    assert gap.is_gap is True


def test_switch_state_does_not_make_an_unwired_feature_look_started():
    """The toggle is judged separately from the wiring. A flag flipped on with
    nothing behind it is still 'not set up' — otherwise a default-on toggle
    would make every untouched feature read as half-built."""
    for values in ({"t_enabled": "1"}, {"t_enabled": "1", "t_channel_id": "0"}):
        assert ag.classify_feature(_FEATURE, values).status == "unconfigured"


def test_partial_when_some_wiring_is_done_and_some_is_not():
    gap = ag.classify_feature(_WIDE, {"w_enabled": "1", "w_a": CH})
    assert gap.status == "partial"
    assert [s.key for s in gap.missing] == ["w_b"]
    assert [s.key for s in gap.present] == ["w_a"]
    assert gap.switch_on is True


def test_wide_feature_fully_wired_but_off_is_ready_but_off():
    gap = ag.classify_feature(_WIDE, {"w_enabled": "0", "w_a": CH, "w_b": CH})
    assert gap.status == "ready_but_off"


def test_feature_without_a_switch_partial_and_complete():
    assert ag.classify_feature(_NO_SWITCH, {}).status == "unconfigured"
    assert ag.classify_feature(_NO_SWITCH, {"n_a": CH}).status == "partial"
    assert ag.classify_feature(_NO_SWITCH, {"n_a": CH, "n_b": CH}).status == "configured"


def test_zero_and_blank_do_not_count_as_configured():
    for val in ("0", "", "   "):
        gap = ag.classify_feature(_NO_SWITCH, {"n_a": val, "n_b": val})
        assert gap.status == "unconfigured", val


def test_optional_settings_do_not_affect_status():
    gap = ag.classify_feature(_FEATURE, {"t_enabled": "1", "t_channel_id": CH,
                                         "t_extra": "hello"})
    assert gap.status == "configured"
    # ...and an optional value alone doesn't rescue an unconfigured feature.
    assert ag.classify_feature(_FEATURE, {"t_extra": "hello"}).status == "unconfigured"


def test_effort_counts_remaining_wiring_not_the_switch():
    assert ag.classify_feature(_WIDE, {}).effort == 2
    assert ag.classify_feature(_WIDE, {"w_a": CH}).effort == 1
    # Fully wired but off: zero settings to fill in, just a switch to flip.
    assert ag.classify_feature(_WIDE, {"w_a": CH, "w_b": CH}).effort == 0
    assert ag.classify_feature(_FEATURE, {"t_channel_id": CH}).effort == 0


# ── scan_guild ──────────────────────────────────────────────────────────────


def test_scan_covers_every_registered_feature():
    conn = _conn([])
    gaps = ag.scan_guild(conn, 1)
    assert len(gaps) == len(ag.FEATURES)


def test_scan_orders_cheapest_win_first():
    conn = _conn([
        # welcome fully wired → configured
        ("welcome_channel_id", CH),
        # qa wired but switched off → ready_but_off, should sort above partials
        ("qa_channel_id", CH), ("qa_enabled", "0"),
        # logging half done → partial
        ("log_channel_id", CH),
    ])
    gaps = ag.scan_guild(conn, 1)
    statuses = [g.status for g in gaps if g.is_gap]
    # ready_but_off must precede every partial, which precedes every unconfigured.
    assert statuses == sorted(statuses, key=ag.STATUS_ORDER.index)
    assert gaps[0].status == "ready_but_off"
    assert gaps[0].feature.slug == "qa_rewards"


def test_scan_reads_legacy_guild_zero_values():
    """A server configured before per-guild keys must not look like a gap."""
    conn = _conn([("welcome_channel_id", CH)], guild_id=0)
    gap = next(g for g in ag.scan_guild(conn, 1) if g.feature.slug == "welcome")
    assert gap.status == "configured"


def test_guild_specific_value_overrides_the_legacy_fallback():
    conn = _conn([("welcome_channel_id", CH)], guild_id=0)
    conn.execute("INSERT INTO config VALUES (1, 'welcome_channel_id', '0')")
    gap = next(g for g in ag.scan_guild(conn, 1) if g.feature.slug == "welcome")
    assert gap.status == "unconfigured"


def test_scan_survives_a_missing_config_table():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    gaps = ag.scan_guild(conn, 1)  # must not raise
    # Everything with required settings reads as unset; a feature with none
    # (Billy-bot itself) can't be a gap and is reported as configured.
    assert all(
        g.status == "unconfigured" for g in gaps if g.feature.required_settings()
    )
    assert all(not g.is_gap for g in gaps if not g.feature.required_settings())


def test_suggestions_limits_and_skips_configured():
    conn = _conn([("welcome_channel_id", CH)])
    picks = ag.suggestions(conn, 1, limit=3)
    assert len(picks) == 3
    assert all(g.is_gap for g in picks)
    assert "welcome" not in {g.feature.slug for g in picks}
    assert ag.suggestions(conn, 1, limit=0) == []


# ── format_gap_report ───────────────────────────────────────────────────────


def test_report_names_keys_blurb_and_panel():
    conn = _conn([])
    text = ag.format_gap_report(ag.scan_guild(conn, 1))
    assert "Welcome messages" in text
    assert "welcome_channel_id" in text  # the model needs the key to propose it
    assert "Greets every new member" in text
    assert "Config → Welcome" in text


def test_report_distinguishes_off_from_unbuilt():
    conn = _conn([("qa_channel_id", CH), ("qa_enabled", "0")])
    text = ag.format_gap_report(ag.scan_guild(conn, 1))
    assert "switched OFF" in text
    assert "not set up at all" in text  # other features


def test_report_lists_already_set_settings_for_partials():
    # Tickets needs both a panel channel and a category; give it just the one.
    conn = _conn([("ticket_panel_channel_id", CH)])
    gaps = [g for g in ag.scan_guild(conn, 1) if g.feature.slug == "tickets"]
    assert gaps[0].status == "partial"
    text = ag.format_gap_report(gaps)
    assert "Already set: Ticket panel channel" in text
    assert "ticket_category_id" in text


def test_report_reads_sensibly_when_only_the_switch_is_on():
    conn = _conn([("rules_watch_enabled", "1")])
    gaps = [g for g in ag.scan_guild(conn, 1) if g.feature.slug == "rules_watch"]
    text = ag.format_gap_report(gaps)
    assert "nothing is wired up behind it yet" in text
    assert "not set up at all" not in text


def test_report_when_nothing_is_missing():
    assert "already set up" in ag.format_gap_report([])


def test_report_is_length_capped():
    conn = _conn([])
    text = ag.format_gap_report(ag.scan_guild(conn, 1))
    assert len(text) <= ag._MAX_REPORT_CHARS + 40


def test_report_can_include_configured_features():
    conn = _conn([("welcome_channel_id", CH)])
    text = ag.format_gap_report(ag.scan_guild(conn, 1), include_configured=True)
    assert "Welcome messages: set up and running." in text


# ── fetch_setup_gaps (the tool handler) ─────────────────────────────────────


class FakePerms:
    def __init__(self, **kw):
        for f in ("administrator", "manage_guild"):
            setattr(self, f, kw.get(f, False))


class FakeMember:
    def __init__(self, **kw):
        self.guild_permissions = FakePerms(**kw)


def _db_file(tmp_path, rows):
    path = tmp_path / "g.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE config (guild_id INTEGER NOT NULL DEFAULT 0, key TEXT NOT NULL, "
        "value TEXT NOT NULL, PRIMARY KEY (guild_id, key))"
    )
    conn.executemany("INSERT INTO config VALUES (1, ?, ?)", rows)
    conn.commit()
    conn.close()
    return path


def test_fetch_setup_gaps_requires_admin(tmp_path):
    path = _db_file(tmp_path, [])
    out = ag.fetch_setup_gaps(path, 1, FakeMember())
    assert "only server admins" in out
    assert "welcome_channel_id" not in out  # no reconnaissance leak


def test_fetch_setup_gaps_allows_admin(tmp_path):
    path = _db_file(tmp_path, [])
    out = ag.fetch_setup_gaps(path, 1, FakeMember(administrator=True))
    assert "Welcome messages" in out


def test_fetch_setup_gaps_allows_manage_guild(tmp_path):
    path = _db_file(tmp_path, [])
    assert "Welcome messages" in ag.fetch_setup_gaps(path, 1, FakeMember(manage_guild=True))


def test_fetch_setup_gaps_returns_text_on_failure(tmp_path):
    out = ag.fetch_setup_gaps(tmp_path / "nope" / "missing.db", 1,
                              FakeMember(administrator=True))
    assert "Couldn't check" in out or "Welcome messages" in out
