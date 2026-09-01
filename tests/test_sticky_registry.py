"""Tests for services/sticky_registry.py — who already holds a channel's bottom slot.

Discord has one bottom slot per channel, so a second sticky panel posted into
an occupied one either trades places with the resident forever (visible,
survivable) or is buried after every render (not survivable). The registry is
what makes that legible *before* anything is posted, so what matters here is
that every panel is actually in the table and that ``restick_on_bot`` is right
per panel — the flag is what splits "warn" from "refuse".

The registry moved out of ``economy_auction_service`` when it grew past the
economy's own panels; the first eight tests came with it.
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_service import save_econ_settings
from bot_modules.services.sticky_registry import (
    bot_chasing_resident,
    is_sticky_panel,
    occupies,
    panel_channels,
    resident_in,
    sticky_panel_channels,
)
from tests.db_template import migrated_db

GUILD = 800


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "auction.db"
    migrated_db(db_path)
    return db_path


# ── the panel-collision warning ─────────────────────────────────────────────


def test_sticky_panel_channels_lists_configured_panels(db):
    """The two panels that only re-stick under human messages, so an auction
    sharing their channel is warned about rather than refused.

    The panel that once had its own pair merged into the economy panel on
    2026-08-18 and its retired ids were deleted on 2026-08-29, so only the
    surviving two resolve to residents.
    """
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {
            "guide_channel_id": 11,
            "shop_channel_id": 33,
        })
        found = sticky_panel_channels(conn, GUILD)
    assert {cid: r.name for cid, r in found.items()} == {
        11: "the economy panel",
        33: "the shop panel",
    }
    assert not any(r.restick_on_bot for r in found.values())


def test_sticky_panel_channels_includes_the_casino_hub(db):
    """The verified real collision: auction #1 in prod ran in the casino hub's
    channel, and the hub re-sticks under bot messages where the card does not."""
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            (GUILD, "casino_panel_channel_id", "1530328883449040967"),
        )
        found = sticky_panel_channels(conn, GUILD)
    assert found[1530328883449040967].name == "the casino hub panel"
    assert found[1530328883449040967].restick_on_bot is True


def test_sticky_panel_channels_includes_the_bounty_board(db):
    """The bounty hub is the fifth sticky panel reachable from a config read.
    It keys off bounty_channel_id, not where the panel was last posted — the
    board channel is where the hub lives and where its cards land."""
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"bounty_channel_id": 44})
        found = sticky_panel_channels(conn, GUILD)
    assert found[44].name == "the bounty board panel"


@pytest.mark.parametrize(
    ("setting", "channel_id", "blocks"),
    [
        # These two chase the bot's own posts, so an auction card here is
        # buried after every render and never resurfaces → refuse.
        pytest.param("bounty_channel_id", 44, True, id="bounty-board-blocks"),
        pytest.param("shop_channel_id", 33, False, id="shop-only-warns"),
    ],
)
def test_restick_on_bot_marks_the_residents_that_block_an_auction(
    db, setting, channel_id, blocks
):
    """The flag is what splits refuse-outright from merely-warn, so it is worth
    pinning per resident rather than trusting the tuple table by eye."""
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {setting: channel_id})
        found = sticky_panel_channels(conn, GUILD)
    assert found[channel_id].restick_on_bot is blocks


def test_sticky_panel_channels_is_empty_when_nothing_is_configured(db):
    """An unconfigured guild must not warn about channel 0."""
    with open_db(db) as conn:
        assert sticky_panel_channels(conn, GUILD) == {}


def test_sticky_panel_channels_merges_two_residents_in_one_channel(db):
    """This was built by comprehension, so a shared channel reported only
    whichever panel came last in the table — a mod warned about a shared channel
    was told about one of the two things they were sharing it with
    (2026-08-06 review, F1)."""
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"shop_channel_id": 77})
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            (GUILD, "casino_panel_channel_id", "77"),
        )
        found = sticky_panel_channels(conn, GUILD)
    assert "the shop panel" in found[77].name
    assert "the casino hub panel" in found[77].name
    # restick_on_bot is the union: one bot-chaser in the channel is enough to
    # bury an auction card reliably, so the block must still fire.
    assert found[77].restick_on_bot is True


# ── the two-bot-chasers collision ───────────────────────────────────────────


def test_bot_chasing_resident_finds_the_other_opted_in_panel(db):
    """Two panels that both chase bot posts in one channel take the bottom slot
    from each other on every trigger, and before core.sticky learned to ignore
    another panel's placement they re-posted forever with nobody typing. A live
    guild had bounty_channel_id == casino_panel_channel_id (2026-08-06, F1)."""
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"bounty_channel_id": 99})
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            (GUILD, "casino_panel_channel_id", "99"),
        )
        assert bot_chasing_resident(
            conn, GUILD, 99, excluding="economy-bounty"
        ) == "the casino hub panel"


def test_bot_chasing_resident_excludes_the_asking_panel(db):
    """The bounty hub lives in the bounty channel by definition, so without the
    exclusion it would always find itself and never be postable at all."""
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"bounty_channel_id": 99})
        assert bot_chasing_resident(conn, GUILD, 99, excluding="economy-bounty") is None


def test_bot_chasing_resident_ignores_human_only_panels(db):
    """The guide/leaderboard/shop panels only move under human messages, so they
    trade places visibly rather than looping — that warns, it does not block."""
    with open_db(db) as conn:
        save_econ_settings(
            conn, GUILD, {"bounty_channel_id": 99, "shop_channel_id": 99}
        )
        assert bot_chasing_resident(conn, GUILD, 99, excluding="economy-bounty") is None


# ── the panels the registry used to be blind to ──────────────────────────────
#
# Until 2026-08-22 this table held only the economy and casino panels, and its
# docstring conceded the rest were "not worth four cross-cog imports". That
# meant an auction — or any dashboard panel post — could land in a channel
# already held by pen pals, DM perms, Voice Control, a todo board, the Guess
# Who prompt or the Survivor panel with no warning at all.


def _seed_pen_pals(conn, channel_id: int) -> None:
    conn.execute(
        "INSERT INTO pen_pals_config (guild_id, panel_channel_id) VALUES (?, ?)",
        (GUILD, channel_id),
    )


def _seed_dm_perms(conn, channel_id: int) -> None:
    conn.execute(
        "INSERT INTO dm_panel_settings (guild_id, panel_channel_id, panel_message_id)"
        " VALUES (?, ?, ?)",
        (GUILD, channel_id, 1),
    )


def _seed_config_key(key: str):
    def seed(conn, channel_id: int) -> None:
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            (GUILD, key, str(channel_id)),
        )

    return seed


def _seed_todo_board(conn, channel_id: int) -> None:
    from bot_modules.services.todo_service import save_board

    save_board(conn, GUILD, channel_id, 1)


def _seed_survivor(conn, channel_id: int) -> None:
    from bot_modules.services.survivor_service import create_season, set_panel_ids

    create_season(conn, GUILD, "S", 2035)
    set_panel_ids(conn, GUILD, channel_id, 1)


def _seed_survivor_unposted(conn, channel_id: int) -> None:
    """A live season with a channel set and no panel in it yet."""
    from bot_modules.services.survivor_service import create_season, update_config

    season_id = create_season(conn, GUILD, "S", 2035)
    update_config(conn, season_id, {"channel_id": channel_id})


@pytest.mark.parametrize(
    ("seed", "key", "name", "blocks"),
    [
        pytest.param(_seed_pen_pals, "pen-pals", "the pen pals panel", False,
                     id="pen-pals"),
        pytest.param(_seed_dm_perms, "dm-perms", "the DM request panel", False,
                     id="dm-perms"),
        pytest.param(
            _seed_config_key("voice_master_panel_channel_id"),
            "voice-control", "the Voice Control owner panel", False,
            id="voice-control",
        ),
        pytest.param(
            _seed_config_key("guess_prompt_channel_id"),
            "guess-prompt", "the Guess Who prompt", False, id="guess-prompt",
        ),
        pytest.param(_seed_todo_board, "todo-board", "the todo board", False,
                     id="todo-board"),
        pytest.param(
            _seed_config_key("mod_stats_panel_channel_id"),
            "mod-stats", "the moderator stats panel", False, id="mod-stats",
        ),
        # The sharp one: the Survivor panel follows the bot's own Reckoning and
        # last-call posts down, so sharing its channel is the refuse case — and
        # it was the least visible of the six.
        pytest.param(_seed_survivor, "survivor", "the Survivor panel", True,
                     id="survivor"),
    ],
)
def test_every_sticky_panel_is_in_the_registry(db, seed, key, name, blocks):
    channel_id = 6001
    with open_db(db) as conn:
        seed(conn, channel_id)
        found = sticky_panel_channels(conn, GUILD)
        assert channel_id in panel_channels(conn, GUILD)[key][0]
    assert found[channel_id].name == name
    assert found[channel_id].restick_on_bot is blocks


def test_a_guess_prompt_from_before_its_own_channel_key_falls_back(db):
    """Prompts predating ``guess_prompt_channel_id`` have only a message id.

    The cog's own id reader carries this fallback, so the registry has to as
    well or those guilds look unoccupied and the check waves an auction in.
    """
    with open_db(db) as conn:
        for key, value in (
            ("guess_channel_id", "6002"),
            ("guess_prompt_message_id", "77"),
        ):
            conn.execute(
                "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
                (GUILD, key, value),
            )
        found = sticky_panel_channels(conn, GUILD)
    assert found[6002].name == "the Guess Who prompt"


def test_a_guess_channel_with_no_prompt_posted_is_not_a_resident(db):
    """Configuring the Guess channel does not put a prompt in it."""
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            (GUILD, "guess_channel_id", "6003"),
        )
        assert sticky_panel_channels(conn, GUILD) == {}


def test_a_survivor_panel_and_the_shop_in_one_channel_both_get_named(db):
    """The merge has to survive a resident from outside the economy."""
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"shop_channel_id": 6004})
        _seed_survivor(conn, 6004)
        found = sticky_panel_channels(conn, GUILD)
    assert "the shop panel" in found[6004].name
    assert "the Survivor panel" in found[6004].name
    assert found[6004].restick_on_bot is True


# ── resident_in (what the dashboard's posting guard asks) ────────────────────


def test_resident_in_names_who_is_already_there(db):
    with open_db(db) as conn:
        _seed_pen_pals(conn, 6005)
        resident = resident_in(conn, GUILD, 6005)
    assert resident is not None
    assert resident.name == "the pen pals panel"
    assert resident.restick_on_bot is False


def test_resident_in_ignores_the_panel_doing_the_asking(db):
    """Re-posting a panel into the channel it already occupies is the normal
    case — refusing it on account of itself would make it unpostable."""
    with open_db(db) as conn:
        _seed_pen_pals(conn, 6006)
        assert resident_in(conn, GUILD, 6006, excluding="pen-pals") is None


def test_resident_in_still_reports_the_others_when_excluding_itself(db):
    with open_db(db) as conn:
        _seed_pen_pals(conn, 6007)
        _seed_dm_perms(conn, 6007)
        resident = resident_in(conn, GUILD, 6007, excluding="pen-pals")
    assert resident is not None
    assert resident.name == "the DM request panel"


def test_resident_in_is_none_for_an_empty_channel(db):
    with open_db(db) as conn:
        _seed_pen_pals(conn, 6008)
        assert resident_in(conn, GUILD, 6009) is None


def test_survivor_is_a_resident_before_its_panel_has_been_posted(db):
    """Keying off the last posted location made Survivor invisible in exactly
    the window that matters: nothing else is warned off the channel, and the
    Wednesday repost then lands on top of whatever was placed there."""
    with open_db(db) as conn:
        _seed_survivor_unposted(conn, 6010)
        found = sticky_panel_channels(conn, GUILD)
    assert "Survivor" in found[6010].name
    # But it only *warns*. get_active_season matches any season that is not
    # complete, so an `enrolling` season pointed at a channel and then
    # abandoned would otherwise hard-refuse every panel and every auction
    # there forever, naming a message nobody can find or delete.
    assert found[6010].restick_on_bot is False


def test_survivor_with_no_live_season_is_not_a_resident(db):
    """An archived season's channel belongs to nobody."""
    with open_db(db) as conn:
        assert sticky_panel_channels(conn, GUILD) == {}


def test_survivor_covers_where_the_panel_actually_is(db):
    """A configured channel isn't the only place the panel can be."""
    from bot_modules.services.survivor_service import create_season, set_panel_ids

    with open_db(db) as conn:
        create_season(conn, GUILD, "S", 2035)
        set_panel_ids(conn, GUILD, 6011, 1)
        found = sticky_panel_channels(conn, GUILD)
    assert found[6011].name == "the Survivor panel"


def test_survivor_holds_both_channels_while_a_move_settles(db):
    """An `or` chain here reported only the configured channel, and the live
    bot-chasing panel sitting in the old one went unmentioned — so an auction
    started there was waved through and buried by the next repost."""
    from bot_modules.services.survivor_service import (
        create_season,
        set_panel_ids,
        update_config,
    )

    with open_db(db) as conn:
        season_id = create_season(conn, GUILD, "S", 2035)
        set_panel_ids(conn, GUILD, 6012, 1)      # panel is here
        update_config(conn, season_id, {"channel_id": 6013})  # repointed here
        found = sticky_panel_channels(conn, GUILD)
        assert bot_chasing_resident(conn, GUILD, 6012, excluding="casino") == (
            "the Survivor panel"
        )
    assert found[6012].name == "the Survivor panel", "the old channel was dropped"
    assert found[6012].restick_on_bot is True, "the live panel must still block"
    assert "Survivor" in found[6013].name, "the new channel was dropped"
    assert found[6013].restick_on_bot is False, "nothing is posted there yet"


def test_survivor_in_one_channel_is_named_once(db):
    """Configured and recorded are normally the same channel; a panel holding
    it must not read as "the Survivor panel and the Survivor panel"."""
    from bot_modules.services.survivor_service import (
        create_season,
        set_panel_ids,
        update_config,
    )

    with open_db(db) as conn:
        season_id = create_season(conn, GUILD, "S", 2035)
        update_config(conn, season_id, {"channel_id": 6014})
        set_panel_ids(conn, GUILD, 6014, 1)
        found = sticky_panel_channels(conn, GUILD)
    assert found[6014].name == "the Survivor panel"


# ── which panels the collision rules apply to at all ─────────────────────────


@pytest.mark.parametrize(
    ("key", "sticky"),
    [
        pytest.param("economy-panel", True, id="economy-panel"),
        pytest.param("survivor", True, id="survivor"),
        pytest.param("voice-control", True, id="voice-control"),
        # Posted once and then scrolls like any other message — no bottom-slot
        # contest to lose, so it must never be refused a channel.
        pytest.param("ticket-panel", False, id="ticket-panel"),
        pytest.param("grant-audit", False, id="grant-audit"),
        pytest.param("no-such-panel", False, id="unknown"),
    ],
)
def test_is_sticky_panel(key, sticky):
    assert is_sticky_panel(key) is sticky


def test_an_abandoned_enrolling_season_only_warns(db):
    """`get_active_season` matches anything that isn't complete, so a season
    created, pointed at a channel and forgotten would otherwise hard-refuse
    every sticky panel and every auction in that channel forever — naming a
    panel that was never posted and that nobody can find to delete."""
    with open_db(db) as conn:
        _seed_survivor_unposted(conn, 6020)
        assert bot_chasing_resident(conn, GUILD, 6020, excluding="casino") is None
        resident = resident_in(conn, GUILD, 6020)
    assert resident is not None
    assert resident.restick_on_bot is False


def test_excluding_takes_more_than_one_key(db):
    """Survivor holds two — where the panel is, and where it is going — and
    its own repost has to ignore both."""
    from bot_modules.services.survivor_service import (
        create_season,
        set_panel_ids,
        update_config,
    )

    with open_db(db) as conn:
        season_id = create_season(conn, GUILD, "S", 2035)
        set_panel_ids(conn, GUILD, 6021, 1)
        update_config(conn, season_id, {"channel_id": 6022})
        both = ("survivor", "survivor-pending")
        assert resident_in(conn, GUILD, 6021, excluding=both) is None
        assert resident_in(conn, GUILD, 6022, excluding=both) is None
        # Excluding only one still finds the other.
        assert resident_in(conn, GUILD, 6022, excluding="survivor") is not None


def test_occupies_is_true_only_where_the_panel_actually_is(db):
    with open_db(db) as conn:
        _seed_pen_pals(conn, 6023)
        assert occupies(conn, GUILD, 6023, "pen-pals") is True
        assert occupies(conn, GUILD, 6024, "pen-pals") is False
        assert occupies(conn, GUILD, 6023, "dm-perms") is False
        assert occupies(conn, GUILD, 6023, ("dm-perms", "pen-pals")) is True
