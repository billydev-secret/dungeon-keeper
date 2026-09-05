"""Tests for the extracted Fantasies & Dealbreakers pure-logic modules.

Covers ``bot_modules/games_fantasies/logic.py`` (add_entry,
apply_vote, tally_entry_votes, build_result_entry,
compute_recap_summary, get_round_entries) and
``bot_modules/games_fantasies/embeds.py`` (lobby, round-submit, vote,
recap embed builders). Mirrors the pressure_cooker / games_hottakes
pattern: the cog file stays thin; this module proves the extracted
pieces work without spinning up Discord.
"""

from __future__ import annotations

import pytest

from bot_modules.games_fantasies.embeds import (
    build_lobby_embed,
    build_recap_embed,
    build_round_submit_embed,
    build_vote_embed,
)
from bot_modules.games_fantasies.logic import (
    CATEGORY_DEALBREAKER,
    CATEGORY_FANTASY,
    add_entry,
    apply_vote,
    build_result_entry,
    compute_recap_summary,
    get_round_entries,
    round_in_progress,
    tally_entry_votes,
)


# ── the two categories ───────────────────────────────────────────────


def test_category_canonical_values():
    # Pin the canonical strings — the cog uses these as button labels, as
    # the modal title, and in audit-log labels.
    assert CATEGORY_FANTASY == "Fantasy"
    assert CATEGORY_DEALBREAKER == "Dealbreaker"


# ── add_entry ────────────────────────────────────────────────────────


def test_add_entry_initializes_rounds_and_entries():
    payload: dict = {}
    add_entry(
        payload,
        round_num=1,
        user_id=42,
        text="Sunsets on the beach",
        category=CATEGORY_FANTASY,
    )
    assert payload["rounds"]["1"]["entries"] == [
        {
            "user_id": 42,
            "text": "Sunsets on the beach",
            "category": CATEGORY_FANTASY,
        }
    ]


def test_add_entry_appends_within_same_round():
    payload: dict = {}
    add_entry(payload, round_num=1, user_id=1, text="a", category="Fantasy")
    add_entry(payload, round_num=1, user_id=2, text="b", category="Dealbreaker")
    entries = payload["rounds"]["1"]["entries"]
    assert len(entries) == 2
    assert [e["text"] for e in entries] == ["a", "b"]
    assert [e["category"] for e in entries] == ["Fantasy", "Dealbreaker"]


def test_add_entry_separate_round_keys_dont_collide():
    payload: dict = {}
    add_entry(payload, round_num=1, user_id=1, text="r1", category="Fantasy")
    add_entry(payload, round_num=2, user_id=1, text="r2", category="Fantasy")
    assert payload["rounds"]["1"]["entries"][0]["text"] == "r1"
    assert payload["rounds"]["2"]["entries"][0]["text"] == "r2"


def test_add_entry_preserves_existing_round_metadata():
    payload = {"rounds": {"1": {"entries": [], "extra": "preserve me"}}}
    add_entry(payload, round_num=1, user_id=1, text="a", category="Fantasy")
    assert payload["rounds"]["1"]["extra"] == "preserve me"
    assert len(payload["rounds"]["1"]["entries"]) == 1


def test_add_entry_round_key_is_stringified():
    """Round keys are str so payload is JSON-friendly."""
    payload: dict = {}
    add_entry(payload, round_num=7, user_id=1, text="t", category="Fantasy")
    assert "7" in payload["rounds"]
    assert 7 not in payload["rounds"]


# ── apply_vote ───────────────────────────────────────────────────────


def test_apply_vote_same_adds_to_same_list():
    same: list[int] = []
    nope: list[int] = []
    changed = apply_vote(same, nope, 1, "same")
    assert changed is False
    assert same == [1]
    assert nope == []


def test_apply_vote_nope_adds_to_nope_list():
    same: list[int] = []
    nope: list[int] = []
    changed = apply_vote(same, nope, 1, "nope")
    assert changed is False
    assert nope == [1]
    assert same == []


def test_apply_vote_idempotent_when_already_voted_same_side():
    same: list[int] = [1]
    nope: list[int] = []
    changed = apply_vote(same, nope, 1, "same")
    assert changed is False
    assert same == [1]  # not duplicated


def test_apply_vote_switching_from_nope_to_same_flags_changed():
    same: list[int] = []
    nope: list[int] = [1]
    changed = apply_vote(same, nope, 1, "same")
    assert changed is True
    assert same == [1]
    assert nope == []


def test_apply_vote_switching_from_same_to_nope_flags_changed():
    same: list[int] = [1]
    nope: list[int] = []
    changed = apply_vote(same, nope, 1, "nope")
    assert changed is True
    assert same == []
    assert nope == [1]


def test_apply_vote_unknown_kind_raises():
    with pytest.raises(ValueError):
        apply_vote([], [], 1, "maybe")


def test_apply_vote_multiple_users_independent():
    same: list[int] = []
    nope: list[int] = []
    apply_vote(same, nope, 1, "same")
    apply_vote(same, nope, 2, "nope")
    apply_vote(same, nope, 3, "same")
    assert same == [1, 3]
    assert nope == [2]


# ── tally_entry_votes ────────────────────────────────────────────────


def test_tally_entry_votes_no_votes():
    same, nope, pct = tally_entry_votes([], [])
    assert same == 0
    assert nope == 0
    assert pct == 0.0


def test_tally_entry_votes_all_same_yields_100_pct():
    same, nope, pct = tally_entry_votes([1, 2, 3], [])
    assert same == 3
    assert nope == 0
    assert pct == 1.0


def test_tally_entry_votes_all_nope_yields_0_pct():
    same, nope, pct = tally_entry_votes([], [1, 2])
    assert same == 0
    assert nope == 2
    assert pct == 0.0


def test_tally_entry_votes_50_50_yields_half():
    same, nope, pct = tally_entry_votes([1, 2], [3, 4])
    assert same == 2
    assert nope == 2
    assert pct == 0.5


def test_tally_entry_votes_uneven_split():
    same, nope, pct = tally_entry_votes([1], [2, 3, 4])
    assert same == 1
    assert nope == 3
    assert pct == 0.25


# ── build_result_entry ───────────────────────────────────────────────


def test_build_result_entry_includes_all_metadata():
    entry = build_result_entry(
        text="My fantasy",
        category="Fantasy",
        author=99,
        same_votes=[1, 2, 3],
        nope_votes=[4],
    )
    assert entry["text"] == "My fantasy"
    assert entry["category"] == "Fantasy"
    assert entry["author"] == 99
    assert entry["same"] == 3
    assert entry["nope"] == 1
    assert entry["same_pct"] == 0.75


def test_build_result_entry_voters_concatenates_both_lists():
    entry = build_result_entry(
        text="t",
        category="Fantasy",
        author=1,
        same_votes=[1, 2],
        nope_votes=[3, 4],
    )
    # Order doesn't matter for correctness, but it should be a list of all 4.
    assert set(entry["voters"]) == {1, 2, 3, 4}
    assert len(entry["voters"]) == 4


def test_build_result_entry_empty_votes_yields_zero_pct():
    entry = build_result_entry(
        text="orphan", category="Fantasy", author=1, same_votes=[], nope_votes=[]
    )
    assert entry["same"] == 0
    assert entry["nope"] == 0
    assert entry["same_pct"] == 0.0
    assert entry["voters"] == []


def test_build_result_entry_voters_is_a_copy_not_a_reference():
    """The cog mutates the vote lists after we build the result; make
    sure the result entry's ``voters`` doesn't alias them."""
    same = [1, 2]
    nope = [3]
    entry = build_result_entry(
        text="t", category="Fantasy", author=1, same_votes=same, nope_votes=nope
    )
    same.append(99)
    nope.append(88)
    # The result entry's voters should still be the original 3 IDs.
    assert set(entry["voters"]) == {1, 2, 3}


# ── compute_recap_summary ────────────────────────────────────────────


def test_compute_recap_summary_returns_none_for_empty_results():
    assert compute_recap_summary([]) is None


def test_compute_recap_summary_single_result_all_three_point_to_same():
    results = [
        {"text": "lone", "same_pct": 0.5, "voters": [1, 2]},
    ]
    summary = compute_recap_summary(results)
    assert summary is not None
    assert summary["most_shared"]["text"] == "lone"
    assert summary["most_polar"]["text"] == "lone"
    assert summary["biggest_outlier"]["text"] == "lone"
    assert summary["total_voters"] == {1, 2}
    assert summary["total_results"] == 1


def test_compute_recap_summary_most_shared_picks_highest_pct():
    results = [
        {"text": "low", "same_pct": 0.2, "voters": [1]},
        {"text": "mid", "same_pct": 0.5, "voters": [2]},
        {"text": "high", "same_pct": 0.9, "voters": [3]},
    ]
    summary = compute_recap_summary(results)
    assert summary is not None
    assert summary["most_shared"]["text"] == "high"


def test_compute_recap_summary_biggest_outlier_picks_lowest_pct():
    results = [
        {"text": "low", "same_pct": 0.2, "voters": [1]},
        {"text": "mid", "same_pct": 0.5, "voters": [2]},
        {"text": "high", "same_pct": 0.9, "voters": [3]},
    ]
    summary = compute_recap_summary(results)
    assert summary is not None
    assert summary["biggest_outlier"]["text"] == "low"


def test_compute_recap_summary_most_polar_picks_closest_to_half():
    results = [
        {"text": "low", "same_pct": 0.1, "voters": [1]},
        {"text": "mid", "same_pct": 0.55, "voters": [2]},
        {"text": "high", "same_pct": 0.95, "voters": [3]},
    ]
    summary = compute_recap_summary(results)
    assert summary is not None
    assert summary["most_polar"]["text"] == "mid"


def test_compute_recap_summary_dedupes_voters_across_entries():
    results = [
        {"text": "a", "same_pct": 0.5, "voters": [1, 2, 3]},
        {"text": "b", "same_pct": 0.5, "voters": [2, 3, 4]},
        {"text": "c", "same_pct": 0.5, "voters": [4, 5]},
    ]
    summary = compute_recap_summary(results)
    assert summary is not None
    assert summary["total_voters"] == {1, 2, 3, 4, 5}
    assert summary["total_results"] == 3


def test_compute_recap_summary_handles_missing_voters_key():
    results = [{"text": "t", "same_pct": 0.5}]
    summary = compute_recap_summary(results)
    assert summary is not None
    assert summary["total_voters"] == set()


# ── get_round_entries ────────────────────────────────────────────────


def test_get_round_entries_returns_empty_when_payload_lacks_rounds():
    assert get_round_entries({}, 1) == []


def test_get_round_entries_returns_empty_when_round_missing():
    payload = {"rounds": {}}
    assert get_round_entries(payload, 1) == []


def test_get_round_entries_returns_entries_when_present():
    payload = {
        "rounds": {
            "1": {
                "entries": [
                    {"user_id": 1, "text": "a", "category": "Fantasy"},
                ]
            }
        }
    }
    assert get_round_entries(payload, 1) == [
        {"user_id": 1, "text": "a", "category": "Fantasy"}
    ]


def test_get_round_entries_uses_stringified_round_num():
    payload = {"rounds": {"3": {"entries": ["x"]}}}
    # The lookup key is str — passing int still finds it.
    assert get_round_entries(payload, 3) == ["x"]


# ── build_lobby_embed ────────────────────────────────────────────────


def test_build_lobby_embed_shows_host_name():
    embed = build_lobby_embed("Alice")
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Host"] == "Alice"


def test_build_lobby_embed_has_title_and_footer():
    embed = build_lobby_embed("Alice")
    assert embed.title is not None
    assert "Fantasies" in embed.title
    assert embed.footer.text is not None
    assert "Fantasies" in embed.footer.text


# ── build_round_submit_embed ────────────────────────────────────────


def test_build_round_submit_embed_includes_round_num_in_title():
    embed = build_round_submit_embed(3)
    assert embed.title is not None
    assert "Round 3" in embed.title


def test_build_round_submit_embed_has_description():
    embed = build_round_submit_embed(1)
    assert embed.description is not None
    assert "anonymously" in embed.description


# ── build_vote_embed ─────────────────────────────────────────────────


def test_build_vote_embed_open_has_no_closed_suffix():
    embed = build_vote_embed(
        entry_text="t",
        entry_num=1,
        category="Fantasy",
        same_votes=[],
        nope_votes=[],
    )
    assert embed.title is not None
    assert "Fantasy #1" in embed.title
    assert "CLOSED" not in embed.title


def test_build_vote_embed_closed_appends_vote_closed_suffix():
    embed = build_vote_embed(
        entry_text="t",
        entry_num=2,
        category="Dealbreaker",
        same_votes=[1],
        nope_votes=[2],
        closed=True,
    )
    assert embed.title is not None
    assert "Vote Closed" in embed.title
    assert "Dealbreaker #2" in embed.title


def test_build_vote_embed_escapes_markdown_in_entry_text():
    embed = build_vote_embed(
        entry_text="**bold** _italic_",
        entry_num=1,
        category="Fantasy",
        same_votes=[],
        nope_votes=[],
    )
    entry_field = next(f for f in embed.fields if f.name == "Entry")
    assert entry_field.value is not None
    assert "\\*\\*bold\\*\\*" in entry_field.value
    assert "\\_italic\\_" in entry_field.value


def test_build_vote_embed_votes_field_shows_both_options():
    embed = build_vote_embed(
        entry_text="t",
        entry_num=1,
        category="Fantasy",
        same_votes=[1, 2],
        nope_votes=[3],
    )
    votes_field = next(f for f in embed.fields if f.name == "Votes")
    assert votes_field.value is not None
    assert "✅ Same" in votes_field.value
    assert "❌ Not for me" in votes_field.value
    # Counts surfaced as raw numbers in parens.
    assert "(2)" in votes_field.value
    assert "(1)" in votes_field.value


def test_build_vote_embed_progress_only_when_total_entries_set():
    embed_no_total = build_vote_embed(
        entry_text="t",
        entry_num=1,
        category="Fantasy",
        same_votes=[],
        nope_votes=[],
        total_entries=0,
    )
    assert all(f.name != "Progress" for f in embed_no_total.fields)

    embed_with_total = build_vote_embed(
        entry_text="t",
        entry_num=2,
        category="Fantasy",
        same_votes=[],
        nope_votes=[],
        total_entries=5,
    )
    progress = next(
        f for f in embed_with_total.fields if f.name == "Progress"
    )
    assert progress.value == "Entry 2/5"


# ── build_recap_embed ────────────────────────────────────────────────


def test_build_recap_embed_returns_none_for_empty_results():
    assert build_recap_embed([]) is None


def test_build_recap_embed_single_result_includes_all_sections():
    results = [
        {"text": "lone", "same_pct": 0.5, "voters": [1, 2], "category": "Fantasy"},
    ]
    embed = build_recap_embed(results)
    assert embed is not None
    by_name = {f.name: f.value for f in embed.fields}
    assert "🌟 Most Universally Shared" in by_name
    assert "⚡ Most Polarizing" in by_name
    assert "🏔️ Biggest Outlier" in by_name
    assert by_name["Total Submissions"] == "1"
    assert by_name["Total Voters"] == "2"


def test_build_recap_embed_formats_pct_as_percent():
    results = [
        {"text": "x", "same_pct": 0.75, "voters": [1]},
    ]
    embed = build_recap_embed(results)
    assert embed is not None
    by_name = {f.name: f.value for f in embed.fields}
    shared = by_name["🌟 Most Universally Shared"]
    assert shared is not None
    assert "75% Same" in shared


def test_build_recap_embed_dedupes_total_voters():
    results = [
        {"text": "a", "same_pct": 0.5, "voters": [1, 2]},
        {"text": "b", "same_pct": 0.5, "voters": [2, 3]},
        {"text": "c", "same_pct": 0.5, "voters": [3, 4]},
    ]
    embed = build_recap_embed(results)
    assert embed is not None
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Total Voters"] == "4"
    assert by_name["Total Submissions"] == "3"


def test_build_recap_embed_picks_highest_pct_for_most_shared():
    results = [
        {"text": "low", "same_pct": 0.1, "voters": [1]},
        {"text": "high", "same_pct": 0.9, "voters": [2]},
    ]
    embed = build_recap_embed(results)
    assert embed is not None
    by_name = {f.name: f.value for f in embed.fields}
    shared = by_name["🌟 Most Universally Shared"]
    assert shared is not None
    assert "high" in shared


def test_build_recap_embed_picks_lowest_pct_for_biggest_outlier():
    results = [
        {"text": "low", "same_pct": 0.1, "voters": [1]},
        {"text": "high", "same_pct": 0.9, "voters": [2]},
    ]
    embed = build_recap_embed(results)
    assert embed is not None
    by_name = {f.name: f.value for f in embed.fields}
    outlier = by_name["🏔️ Biggest Outlier"]
    assert outlier is not None
    assert "low" in outlier


# ── the category is a button, not a typed word (ephemeral-UI audit M1) ──

from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

import bot_modules.cogs.games_fantasies_cog as fan_cog  # noqa: E402


@pytest.mark.parametrize(
    ("press", "expected"),
    [
        ("submit_fantasy", CATEGORY_FANTASY),
        ("submit_dealbreaker", CATEGORY_DEALBREAKER),
    ],
)
async def test_submit_buttons_carry_their_own_category(press, expected):
    """Each button opens the entry modal with the category already decided."""
    view = fan_cog.SubmitRoundView("g1", 1, 2, db=None, bot=None)
    interaction = SimpleNamespace(
        user=SimpleNamespace(display_name="Alice"),
        channel=None,
        response=SimpleNamespace(send_modal=AsyncMock()),
    )

    await getattr(view, press).callback(interaction)  # type: ignore[arg-type]

    modal = interaction.response.send_modal.await_args.args[0]
    assert isinstance(modal, fan_cog.SubmitEntryModal)
    assert modal.category == expected
    assert modal.round_num == 2
    assert getattr(view, press).label == f"Submit a {expected}"
    # The category box is gone: the entry is the only thing left to type.
    assert [c.label for c in modal.children] == ["Your Entry"]


async def test_entry_survives_a_submission_that_used_to_be_rejected(monkeypatch):
    """A 500-character entry can no longer be lost to an unparseable category.

    The old modal asked members to type "Fantasy" or "Dealbreaker" beside
    their entry; a word normalize_category didn't recognise closed the modal
    with an error and discarded everything they had written. There is no
    parse step left to fail.
    """
    captured: dict = {}

    async def _fake_modify(db, game_id, fn):
        payload: dict = {}
        fn(payload)
        captured["payload"] = payload

    monkeypatch.setattr(fan_cog, "modify_payload", _fake_modify)
    monkeypatch.setattr(fan_cog, "audit_anonymous", AsyncMock())

    modal = fan_cog.SubmitEntryModal("g1", None, 1, CATEGORY_DEALBREAKER)
    modal.entry._value = "x" * 500
    interaction = SimpleNamespace(
        user=SimpleNamespace(display_name="Alice", id=7),
        channel=None,
        guild=None,
        client=None,
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    await modal.on_submit(interaction)  # type: ignore[arg-type]

    entry = captured["payload"]["rounds"]["1"]["entries"][0]
    assert entry == {"user_id": 7, "text": "x" * 500, "category": CATEGORY_DEALBREAKER}
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


# ── the game has an ending: End Game posts the recap and pays the room ──

import asyncio  # noqa: E402
import json  # noqa: E402

from bot_modules.games.utils.game_manager import (  # noqa: E402
    ConfirmCloseView,
    create_game,
    get_active_game,
)
from bot_modules.games.utils.launch_guard import busy_message  # noqa: E402
from bot_modules.games_fantasies.logic import roster_from_results  # noqa: E402
from bot_modules.services.games_db import GamesDb  # noqa: E402


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        pytest.param([], [], id="nothing-voted-on"),
        pytest.param(
            [
                {"author": 9, "voters": [1, 2]},
                {"author": 2, "voters": [1, 3, 3]},
                {"voters": [4]},  # a malformed row without an author still counts its voters
            ],
            [1, 2, 3, 4, 9],
            id="authors-and-voters-deduped",
        ),
    ],
)
def test_roster_from_results(results, expected):
    """Entry authors plus everyone who voted either way — the author of the
    most-shared entry may never have voted, so voters alone would drop them.
    Mirrors game_roster._fantasies, which the sweep and /games end use."""
    assert roster_from_results(results) == expected


class _SpyBot:
    def __init__(self, db_path) -> None:
        self.games_db = GamesDb(db_path)
        self.active_views: dict = {}
        self.ctx = SimpleNamespace(db_path=db_path)

    def get_cog(self, name):
        return None


def _interaction(user_id: int, channel):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, display_name="Host"),
        channel=channel,
        channel_id=getattr(channel, "id", None),
        guild=None,
        guild_id=9001,
        message=SimpleNamespace(id=555, edit=AsyncMock(), embeds=[]),
        response=SimpleNamespace(
            send_message=AsyncMock(), edit_message=AsyncMock(), defer=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )


async def test_end_game_button_posts_recap_and_pays_the_roster(monkeypatch, sync_db_path):
    """anon-tail-65: Fantasies had no ending — the recap builder was dead code
    and end_game was reachable only via the 24h sweep or /games end. The host's
    End Game now posts the recap and pays authors + voters, like Hot Takes."""
    spy = AsyncMock()
    monkeypatch.setattr(fan_cog, "end_game", spy)
    bot = _SpyBot(sync_db_path)
    cog = fan_cog.FantasiesCog(bot)  # type: ignore[arg-type]
    results = [
        {"text": "beach", "category": "Fantasy", "same": 2, "nope": 0, "same_pct": 1.0,
         "author": 9, "voters": [1, 2]},
        {"text": "snoring", "category": "Dealbreaker", "same": 1, "nope": 1, "same_pct": 0.5,
         "author": 2, "voters": [1, 3]},
    ]
    payload = {"rounds": {"1": {"entries": []}}, "results": results}
    gid = await create_game(bot.games_db, 100, 1, "fantasies", payload=payload)
    view = fan_cog.FantasiesMainView(gid, 1, bot.games_db, bot, cog)
    bot.active_views[gid] = view
    anchor = SimpleNamespace(id=555, edit=AsyncMock(), embeds=[])
    view._message = anchor  # type: ignore[assignment]
    channel = SimpleNamespace(id=100, guild=None, send=AsyncMock())
    # A round is mid-submission: ending must wake the loop blocked on it.
    sub = fan_cog.SubmitRoundView(gid, 1, 2, bot.games_db, bot)
    sub._message = SimpleNamespace(edit=AsyncMock())  # type: ignore[attr-defined]
    view._active_submit_view = sub

    stranger = _interaction(42, channel)
    await view.end_game_button.callback(stranger)  # type: ignore[arg-type]
    assert stranger.response.send_message.await_args.args[0].startswith("❌")
    spy.assert_not_awaited()

    press = _interaction(1, channel)
    await view.end_game_button.callback(press)  # type: ignore[arg-type]
    confirm = press.response.send_message.await_args.kwargs["view"]
    assert isinstance(confirm, ConfirmCloseView)
    assert press.response.send_message.await_args.kwargs["ephemeral"] is True

    await confirm._callback(_interaction(1, channel))

    recap = channel.send.await_args.kwargs["embed"]
    assert "Results" in recap.title
    call = spy.await_args
    assert call is not None and spy.await_count == 1
    assert call.kwargs["player_ids"] == [1, 2, 3, 9]
    assert call.kwargs["player_count"] == 4
    assert call.kwargs["round_count"] == 2
    assert call.kwargs["bot"] is bot
    assert call.kwargs["payload"]["results"] == results
    assert gid not in bot.active_views
    assert view.is_finished()
    assert sub.is_finished()  # the round loop's View.wait() returns
    anchor.edit.assert_awaited()


async def test_end_game_with_nothing_voted_on_says_so(monkeypatch, sync_db_path):
    """An End with no results posts a line rather than nothing, and pays nobody."""
    spy = AsyncMock()
    monkeypatch.setattr(fan_cog, "end_game", spy)
    bot = _SpyBot(sync_db_path)
    cog = fan_cog.FantasiesCog(bot)  # type: ignore[arg-type]
    gid = await create_game(bot.games_db, 100, 1, "fantasies", payload={"rounds": {}, "results": []})
    view = fan_cog.FantasiesMainView(gid, 1, bot.games_db, bot, cog)
    bot.active_views[gid] = view
    channel = SimpleNamespace(id=100, guild=None, send=AsyncMock())

    press = _interaction(1, channel)
    await view.end_game_button.callback(press)  # type: ignore[arg-type]
    await press.response.send_message.await_args.kwargs["view"]._callback(_interaction(1, channel))

    assert "embed" not in channel.send.await_args.kwargs
    assert "no entries" in channel.send.await_args.args[0].lower()
    call = spy.await_args
    assert call is not None and call.kwargs["player_ids"] == []


async def test_slash_entry_refuses_a_channel_with_a_running_game(monkeypatch, sync_db_path):
    """/games play fantasies goes through the shared launch guard (platform-18)."""
    bot = _SpyBot(sync_db_path)
    cog = fan_cog.FantasiesCog(bot)  # type: ignore[arg-type]
    launch = AsyncMock()
    monkeypatch.setattr(cog, "launch", launch)
    await bot.games_db.execute(
        "INSERT INTO games_allowed_channels (channel_id, guild_id) VALUES (?, ?)", (100, 9001),
    )
    await create_game(bot.games_db, 100, 7, "hottakes", message_id=321, guild_id=9001)
    interaction = _interaction(1, SimpleNamespace(id=100, guild=None, send=AsyncMock()))

    await cog.fantasies.callback(cog, interaction)  # type: ignore[arg-type]

    sent = interaction.response.send_message.await_args
    assert sent.kwargs["ephemeral"] is True
    assert sent.args[0] == busy_message("hottakes", link="https://discord.com/channels/9001/100/321")
    launch.assert_not_awaited()
    interaction.response.defer.assert_not_awaited()
    assert await get_active_game(bot.games_db, 100) is not None


async def test_round_with_no_entries_hands_control_back_to_the_panel(sync_db_path):
    """A zero-entry round is the 'skip': the panel stays live so the host can
    start another round or end the game, and the notice says so."""
    bot = _SpyBot(sync_db_path)
    cog = fan_cog.FantasiesCog(bot)  # type: ignore[arg-type]
    gid = await create_game(bot.games_db, 100, 1, "fantasies", payload={"rounds": {}, "results": []})
    view = fan_cog.FantasiesMainView(gid, 1, bot.games_db, bot, cog)
    bot.active_views[gid] = view
    sent_msg = SimpleNamespace(id=777, edit=AsyncMock())
    channel = SimpleNamespace(id=100, guild=None, send=AsyncMock(return_value=sent_msg))

    task = asyncio.ensure_future(
        cog._run_round(game_id=gid, host_id=1, host_name="Host", round_num=1, channel=channel, main_view=view)
    )
    try:
        for _ in range(300):
            await asyncio.sleep(0.01)
            if view._active_submit_view is not None:
                break
        assert view._active_submit_view is not None
        view._active_submit_view.stop()  # the host closes submissions with nothing in
        await asyncio.wait_for(task, timeout=5)
    finally:
        if not task.done():
            task.cancel()

    notice = channel.send.await_args.args[0]
    assert "No entries" in notice and "End Game" in notice
    assert bot.active_views[gid] is view
    assert view._active_submit_view is None


# ── pacing, the lobby copy, the joining state (anon-tail-71/72/75) ──

from bot_modules.games.utils.round_pacing import TIMER_FIELD_NAME  # noqa: E402
from bot_modules.games_fantasies.embeds import LOBBY_DESCRIPTION  # noqa: E402
from bot_modules.games_fantasies.logic import (  # noqa: E402
    DEFAULT_ENTRY_SECONDS,
    active_voters,
    everyone_has_voted,
)


@pytest.mark.parametrize(
    ("entries", "results", "exclude", "expected"),
    [
        pytest.param([{"user_id": 1}, {"user_id": 2}], [], 1, {2}, id="submitters-minus-the-author"),
        pytest.param([{"user_id": 1}], [{"voters": [5]}], 1, {5}, id="earlier-voters-join"),
        pytest.param([{"user_id": 1}], [], 1, set(), id="a-lone-author-waits-on-nobody"),
    ],
)
def test_active_voters(entries, results, exclude, expected):
    assert active_voters(entries, results, exclude=exclude) == expected


@pytest.mark.parametrize(
    ("expected", "voted", "result"),
    [
        pytest.param({1, 2}, [1, 2], True, id="all-in"),
        pytest.param({1, 2}, [2], False, id="one-missing"),
        pytest.param(set(), [1], False, id="nobody-expected-never-advances"),
    ],
)
def test_everyone_has_voted(expected, voted, result):
    assert everyone_has_voted(expected, voted) is result


def test_lobby_embed_carries_how_to_play_and_the_mod_visibility_line():
    embed = build_lobby_embed("Alice")
    assert embed.description == LOBBY_DESCRIPTION
    assert "How to play" in embed.description
    assert "mods can still see who sent it" in embed.description
    assert all(f.name != "⏰ Starting" for f in embed.fields)
    assert build_lobby_embed("Alice", start_at=1_700_000_000).fields[-1].value == "<t:1700000000:R>"


@pytest.mark.parametrize(
    ("advance_at", "closed", "shown"),
    [
        pytest.param(1_700_000_000, False, True, id="timed-open"),
        pytest.param(1_700_000_000, True, False, id="timed-closed"),
        pytest.param(None, False, False, id="host-paced"),
    ],
)
def test_vote_embed_shows_the_countdown_only_while_timed_and_open(advance_at, closed, shown):
    embed = build_vote_embed(
        entry_text="x", entry_num=1, category="Fantasy", same_votes=[], nope_votes=[],
        closed=closed, advance_at=advance_at,
    )
    assert any(f.name == TIMER_FIELD_NAME for f in embed.fields) is shown


def _voter(user_id: int):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, display_name=f"U{user_id}"),
        channel=None,
        message=SimpleNamespace(id=1, edit=AsyncMock()),
        response=SimpleNamespace(send_message=AsyncMock()),
    )


async def test_the_vote_closes_itself_once_everyone_expected_has_voted(sync_db_path):
    bot = _SpyBot(sync_db_path)
    advance = AsyncMock()
    view = fan_cog.FantasiesVoteView(
        "g", 1, "beach", 1, "Fantasy", bot.games_db, bot, "Host", advance,
        entry_author_id=9, expected_voters={2, 3},
    )
    await view.vote_same.callback(_voter(2))  # type: ignore[arg-type]
    advance.assert_not_awaited()
    await view.vote_nope.callback(_voter(3))  # type: ignore[arg-type]
    assert advance.await_count == 1
    assert (view.same_votes, view.nope_votes) == ([2], [3])


@pytest.mark.parametrize(
    ("options", "stored_dial", "expected"),
    [
        pytest.param({}, None, DEFAULT_ENTRY_SECONDS, id="built-in-default"),
        pytest.param({}, 15, 15, id="dashboard-dial"),
        pytest.param({"round_seconds": 0}, 15, 0, id="slash-zero-beats-the-dial"),
    ],
)
async def test_launch_opens_a_joining_lobby_with_the_entry_timer(sync_db_path, options, stored_dial, expected):
    bot = _SpyBot(sync_db_path)
    cog = fan_cog.FantasiesCog(bot)  # type: ignore[arg-type]
    if stored_dial is not None:
        await bot.games_db.execute(
            "INSERT INTO games_game_config (guild_id, game_type, enabled, options) VALUES (?, ?, 1, ?)",
            (9001, "fantasies", json.dumps({"round_seconds": stored_dial})),
        )
    channel = SimpleNamespace(
        id=100, guild=None, name="games", send=AsyncMock(return_value=SimpleNamespace(id=555)),
    )
    gid = await cog.launch(channel=channel, host_id=1, host_name="Host", guild_id=9001, options={**options, "start_in": 3})
    assert gid is not None
    row = await get_active_game(bot.games_db, 100)
    assert row is not None and row["state"] == "joining"
    payload = json.loads(row["payload"])
    assert payload["round_seconds"] == expected
    assert payload["start_epoch"] > 0


async def test_start_round_leaves_the_lobby_state(monkeypatch, sync_db_path):
    """The start-ping sweep polls state='joining'; a game with a round running
    must drop out of it or the idle close could take a live game."""
    bot = _SpyBot(sync_db_path)
    cog = fan_cog.FantasiesCog(bot)  # type: ignore[arg-type]
    monkeypatch.setattr(cog, "_run_round", AsyncMock())
    gid = await create_game(bot.games_db, 100, 1, "fantasies", state="joining", payload={"rounds": {}, "results": []})
    view = fan_cog.FantasiesMainView(gid, 1, bot.games_db, bot, cog)

    await view.start_round.callback(_interaction(1, SimpleNamespace(id=100, guild=None)))  # type: ignore[arg-type]

    row = await get_active_game(bot.games_db, 100)
    assert row is not None and row["state"] == "playing"


def test_fantasies_is_a_lobby_game():
    from bot_modules.games.constants import LOBBY_GAME_TYPES, LOBBY_MIN_PLAYERS, LOBBY_START_BUTTON

    assert "fantasies" in LOBBY_GAME_TYPES
    assert LOBBY_START_BUTTON["fantasies"] == "Start Round"
    assert LOBBY_MIN_PLAYERS["fantasies"] == 1


# ── Start Round guard ────────────────────────────────────────────────────────
#
# A second press while a round runs used to start a concurrent round and
# overwrite the main view's live submit view.
@pytest.mark.parametrize(
    ("active_submit", "active_vote", "running", "expected"),
    [
        pytest.param(None, None, False, False, id="idle"),
        pytest.param(object(), None, False, True, id="submit-phase"),
        pytest.param(None, object(), False, True, id="vote-phase"),
        pytest.param(None, None, True, True, id="between-phases"),
    ],
)
def test_round_in_progress(active_submit, active_vote, running, expected):
    assert round_in_progress(active_submit, active_vote, running=running) is expected
