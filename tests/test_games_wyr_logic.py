"""Tests for the extracted Would-You-Rather pure-logic modules.

Covers ``bot_modules/games_wyr/logic.py`` (question parser, vote
toggle, next-button label) and ``bot_modules/games_wyr/embeds.py``
(round embed open/closed/revealed states + closed-game variant).
Mirrors the pressure_cooker pattern: the cog file stays thin; this
module proves the extracted pieces work without spinning up Discord.
"""

from __future__ import annotations

import pytest

from bot_modules.games.constants import (
    GAME_ICONS,
    PHASE_PLAYING,
)
from bot_modules.games_wyr.embeds import build_closed_embed, build_wyr_embed
from bot_modules.games_wyr.logic import (
    next_button_label,
    parse_question_input,
    record_show_vote,
    shown_on_side,
    toggle_vote,
)
from bot_modules.core.branding import SECTION_SPACER


def _unspaced(value: str | None) -> str:
    """A field value without the trailing spacer ``apply_section_spacing`` adds.

    Stacked fields carry ``SECTION_SPACER`` for breathing room
    (docs/embed_style_guide.md § Section spacing). These tests assert content,
    not spacing, so they compare against the value with it removed.
    """
    text = value or ""
    return text[: -len(SECTION_SPACER)] if text.endswith(SECTION_SPACER) else text


# ── parse_question_input ─────────────────────────────────────────────


def test_parse_question_input_splits_simple_pair():
    assert parse_question_input("fly | be invisible") == ("fly", "be invisible")


def test_parse_question_input_strips_whitespace_around_options():
    assert parse_question_input("  swim   |   fly  ") == ("swim", "fly")


def test_parse_question_input_returns_none_for_empty_string():
    assert parse_question_input("") is None


def test_parse_question_input_returns_none_for_whitespace_only():
    assert parse_question_input("   \n\t  ") is None


def test_parse_question_input_returns_none_when_no_pipe():
    assert parse_question_input("just one option") is None


def test_parse_question_input_returns_none_when_left_half_empty():
    assert parse_question_input("  | something") is None


def test_parse_question_input_returns_none_when_right_half_empty():
    assert parse_question_input("something |  ") is None


def test_parse_question_input_returns_none_when_both_halves_empty():
    assert parse_question_input(" | ") is None


def test_parse_question_input_uses_only_first_pipe():
    """Extra pipes in option B are preserved as-is — split(|, 1)."""
    assert parse_question_input("a | b | c") == ("a", "b | c")


# ── toggle_vote ──────────────────────────────────────────────────────


def test_toggle_vote_a_records_fresh_vote_returns_false():
    votes_a: list[int] = []
    votes_b: list[int] = []
    changed = toggle_vote(votes_a, votes_b, user_id=42, choice="a")
    assert changed is False
    assert votes_a == [42]
    assert votes_b == []


def test_toggle_vote_b_records_fresh_vote_returns_false():
    votes_a: list[int] = []
    votes_b: list[int] = []
    changed = toggle_vote(votes_a, votes_b, user_id=42, choice="b")
    assert changed is False
    assert votes_a == []
    assert votes_b == [42]


def test_toggle_vote_switching_from_b_to_a_returns_true():
    votes_a: list[int] = []
    votes_b: list[int] = [42]
    changed = toggle_vote(votes_a, votes_b, 42, "a")
    assert changed is True
    assert votes_a == [42]
    assert votes_b == []


def test_toggle_vote_switching_from_a_to_b_returns_true():
    votes_a: list[int] = [42]
    votes_b: list[int] = []
    changed = toggle_vote(votes_a, votes_b, 42, "b")
    assert changed is True
    assert votes_a == []
    assert votes_b == [42]


def test_toggle_vote_re_pressing_same_side_is_idempotent():
    """A user already on side A pressing A again is a no-op (no duplicate
    in the list and changed=False)."""
    votes_a: list[int] = [42]
    votes_b: list[int] = []
    changed = toggle_vote(votes_a, votes_b, 42, "a")
    assert changed is False
    assert votes_a == [42]
    assert votes_b == []


def test_toggle_vote_preserves_other_voters():
    """Switching one user must not disturb anyone else's vote."""
    votes_a: list[int] = [1, 2]
    votes_b: list[int] = [3, 4]
    toggle_vote(votes_a, votes_b, 3, "a")
    assert votes_a == [1, 2, 3]
    assert votes_b == [4]


def test_toggle_vote_raises_on_invalid_choice():
    with pytest.raises(ValueError):
        toggle_vote([], [], 1, "c")


# ── next_button_label ────────────────────────────────────────────────


# ── record_show_vote / shown_on_side ─────────────────────────────────


@pytest.mark.parametrize(
    "votes_a, votes_b, shown, uid, expected, shown_after",
    [
        pytest.param([1], [], [], 1, "shown", [1], id="voter-a-shows"),
        pytest.param([], [2], [], 2, "shown", [2], id="voter-b-shows"),
        pytest.param([1], [], [], 9, "not_voted", [], id="no-vote-nothing-to-show"),
        pytest.param([1], [], [1], 1, "already", [1], id="re-press-is-idempotent"),
    ],
)
def test_record_show_vote(votes_a, votes_b, shown, uid, expected, shown_after):
    """vote-games-61: showing is the voter's own act, and needs a vote."""
    assert record_show_vote(votes_a, votes_b, shown, uid) == expected
    assert shown == shown_after


def test_shown_on_side_follows_the_voter_across_a_switch():
    votes_a, votes_b, shown = [1, 2], [3], []
    record_show_vote(votes_a, votes_b, shown, 1)
    assert shown_on_side(votes_a, shown) == [1] and shown_on_side(votes_b, shown) == []
    toggle_vote(votes_a, votes_b, 1, "b")
    assert shown_on_side(votes_a, shown) == [] and shown_on_side(votes_b, shown) == [1]


def test_next_button_label_zero():
    assert next_button_label(0) == "⏭️ Next (0 queued)"


def test_next_button_label_one():
    assert next_button_label(1) == "⏭️ Next (1 queued)"


def test_next_button_label_many():
    assert next_button_label(17) == "⏭️ Next (17 queued)"


# ── build_wyr_embed ──────────────────────────────────────────────────


def _field_by_name(embed) -> dict[str, str]:
    return {f.name: _unspaced(f.value) for f in embed.fields}


def test_build_wyr_embed_title_when_open():
    embed = build_wyr_embed("Alice", "fly", "swim", [], [], False, 1)
    assert embed.title is not None
    assert "Would You Rather" in embed.title
    assert "Round Over" not in embed.title
    assert embed.color is not None
    assert embed.color.value == PHASE_PLAYING


def test_build_wyr_embed_title_when_closed():
    embed = build_wyr_embed("Alice", "fly", "swim", [], [], False, 1, closed=True)
    assert embed.title is not None
    assert "Round Over" in embed.title
    # Per the 2026-07-21 ruling WYR is a voting game with no winner, so the
    # closed state no longer flips to a semantic results color — with no
    # accent passed it falls back to the PHASE_PLAYING default.
    assert embed.color is not None
    assert embed.color.value == PHASE_PLAYING


def test_build_wyr_embed_shows_round_and_options():
    embed = build_wyr_embed("Alice", "fly", "swim", [], [], False, 3)
    by_name = _field_by_name(embed)
    assert by_name["Round"] == "3"
    assert by_name["🅰️"] == "fly"
    assert by_name["🅱️"] == "swim"


def test_build_wyr_embed_escapes_markdown_in_options():
    """Discord markdown in option text must be escaped so the embed
    can't be tricked into rendering bold or links from user input."""
    embed = build_wyr_embed("Alice", "**bold**", "_italic_", [], [], False, 1)
    by_name = _field_by_name(embed)
    # Asterisks/underscores get escaped (backslash-prefixed)
    assert "\\*" in by_name["🅰️"]
    assert "\\_" in by_name["🅱️"]


def test_build_wyr_embed_counts_votes_in_labels():
    embed = build_wyr_embed("Alice", "fly", "swim", [1, 2, 3], [4], False, 1)
    by_name = _field_by_name(embed)
    votes_field = by_name["Votes"]
    assert "(3)" in votes_field  # A count
    assert "(1)" in votes_field  # B count


def _named(uid: int) -> str:
    return f"Member{uid}"


def test_build_wyr_embed_revealed_lists_voter_names():
    """A fully revealed round names each voter through ``name_fn`` — a ``<@id>`` in
    an embed shows as a bare number to anyone whose client hasn't cached
    that member, so the reveal must carry resolved names."""
    embed = build_wyr_embed(
        "Alice", "fly", "swim", [1, 2], [3], False, 1, revealed=True, name_fn=_named
    )
    votes_field = _field_by_name(embed)["Votes"]
    assert "Member1" in votes_field
    assert "Member2" in votes_field
    assert "Member3" in votes_field
    assert "<@" not in votes_field


def test_build_wyr_embed_names_only_the_shown_voters():
    """A shown voter is named on their side; the rest stay a count."""
    embed = build_wyr_embed(
        "Alice", "fly", "swim", [1, 2], [3], True, 1, name_fn=_named, shown=[1],
    )
    votes = _unspaced(embed.fields[-1].value)
    assert "Member1 +1 anonymous" in votes
    assert "Member2" not in votes and "Member3" not in votes


def test_build_wyr_embed_shown_side_with_nobody_hidden_has_no_count():
    embed = build_wyr_embed(
        "Alice", "fly", "swim", [1], [2], True, 1, name_fn=_named, shown=[1, 2],
    )
    votes = _unspaced(embed.fields[-1].value)
    assert "Member1" in votes and "Member2" in votes and "anonymous" not in votes


def test_build_closed_embed_keeps_the_shown_names():
    embed = build_closed_embed(
        "Alice", "fly", "swim", [1], [], True, 1, name_fn=_named, shown=[1],
    )
    assert "Member1" in _unspaced(embed.fields[-1].value)


def test_build_wyr_embed_hides_voters_until_revealed():
    embed = build_wyr_embed("Alice", "fly", "swim", [1, 2], [3], False, 1, name_fn=_named)
    assert "Member" not in _field_by_name(embed)["Votes"]


def test_build_wyr_embed_revealed_uses_dash_when_a_side_empty():
    """No voters on a side renders as an em-dash placeholder, not blank."""
    embed = build_wyr_embed(
        "Alice", "fly", "swim", [], [5], False, 1, revealed=True, name_fn=_named
    )
    votes_field = _field_by_name(embed)["Votes"]
    # The A side has no voters -> dash placeholder
    assert "—" in votes_field
    assert "Member5" in votes_field


def test_build_closed_embed_threads_name_fn_through():
    embed = build_closed_embed(
        "Alice", "fly", "swim", [1], [], False, 1, revealed=True, name_fn=_named
    )
    assert "Member1" in _field_by_name(embed)["Votes"]


def test_every_wyr_render_site_passes_a_resolver():
    """``name_fn`` defaults to ``mention`` so an un-wired caller still renders;
    the wiring therefore needs its own guard, or a render site that forgets
    the resolver silently brings the bare-number bug back."""
    import ast
    import inspect
    import pathlib

    import bot_modules.cogs.games_wyr_cog as cog_module
    import bot_modules.games_wyr.embeds as embeds_module

    needs = {
        name
        for name, fn in inspect.getmembers(embeds_module, inspect.isfunction)
        if "name_fn" in inspect.signature(fn).parameters
    }
    source = pathlib.Path(inspect.getfile(cog_module)).read_text(encoding="utf-8")
    missed = [
        f"games_wyr_cog.py:{node.lineno} {node.func.id}()"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in needs
        and not any(kw.arg == "name_fn" for kw in node.keywords)
    ]
    assert not missed, "render sites with no name_fn: " + ", ".join(missed)


def test_build_wyr_embed_anonymous_badge_in_footer():
    """The badge names the way out of anonymity, not just the default
    (vote-games-61)."""
    embed = build_wyr_embed("Alice", "fly", "swim", [], [], True, 1)
    assert embed.footer.text is not None
    assert "Anonymous" in embed.footer.text and "Show My Vote" in embed.footer.text


def test_build_wyr_embed_no_anonymous_badge_when_off():
    embed = build_wyr_embed("Alice", "fly", "swim", [], [], False, 1)
    assert embed.footer.text is not None
    assert "Anonymous" not in embed.footer.text


def test_build_wyr_embed_footer_includes_round_number():
    embed = build_wyr_embed("Alice", "fly", "swim", [], [], False, 7)
    assert embed.footer.text is not None
    assert "Round 7" in embed.footer.text


def test_build_wyr_embed_renders_game_icon_in_title():
    embed = build_wyr_embed("Alice", "fly", "swim", [], [], False, 1)
    assert embed.title is not None
    assert GAME_ICONS["wyr"] in embed.title


# ── build_closed_embed ───────────────────────────────────────────────


def test_build_closed_embed_title_says_closed():
    embed = build_closed_embed("Alice", "fly", "swim", [1], [2], True, 1)
    assert embed.title is not None
    assert "Closed" in embed.title
    assert "Round Over" not in embed.title  # CLOSED overrides the ROUND-OVER suffix


def test_build_closed_embed_defaults_to_phase_playing():
    """No accent passed → the closed embed no longer overrides to a recap
    color; it falls back to the PHASE_PLAYING default like the round embed."""
    embed = build_closed_embed("Alice", "fly", "swim", [1], [2], True, 1)
    assert embed.color is not None
    assert embed.color.value == PHASE_PLAYING


def test_build_closed_embed_preserves_vote_counts():
    """The CLOSED embed still shows the final vote tallies."""
    embed = build_closed_embed("Alice", "fly", "swim", [1, 2], [3], True, 4)
    by_name = _field_by_name(embed)
    assert by_name["Round"] == "4"
    assert "(2)" in by_name["Votes"]
    assert "(1)" in by_name["Votes"]


def test_build_closed_embed_can_reveal_voters():
    embed = build_closed_embed("Alice", "fly", "swim", [1], [2], True, 1, revealed=True)
    votes_field = _field_by_name(embed)["Votes"]
    assert "<@1>" in votes_field
    assert "<@2>" in votes_field


# ── economy roster enrichment (Stage 2 faucet) ──────────────────────

from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

import bot_modules.cogs.games_wyr_cog as wyr_cog  # noqa: E402
from bot_modules.games.utils.game_manager import (  # noqa: E402
    create_game,
    end_game,
    get_active_game_by_id,
    get_game_payload,
)
from bot_modules.games.utils.round_pacing import ADVANCE_DENIED, RoundPacing  # noqa: E402
from bot_modules.services.games_db import GamesDb  # noqa: E402


class _SpyBot:
    def __init__(self, db_path) -> None:
        self.games_db = GamesDb(db_path)
        self.active_views: dict = {}
        self.ctx = SimpleNamespace(db_path=db_path)

    def get_cog(self, name):
        return None


def _channel(guild=None):
    return SimpleNamespace(
        id=100, name="games", guild=guild,
        send=AsyncMock(return_value=SimpleNamespace(id=555, edit=AsyncMock())),
    )


def _message():
    return SimpleNamespace(id=555, edit=AsyncMock())


def _interaction(uid: int, *, message=None):
    return SimpleNamespace(
        user=SimpleNamespace(id=uid, display_name=f"U{uid}", roles=[]),
        guild=None, guild_id=None,
        channel=SimpleNamespace(id=100, name="games", guild=None),
        message=message or _message(),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
    )


async def test_run_round_empty_bank_opens_a_waiting_round(monkeypatch, sync_db_path):
    """vote-games-50: the bank had nothing, so the round waits for a posed
    question with only Pose / End / Help live — the game does not end."""
    monkeypatch.setattr(wyr_cog, "get_wyr_question", AsyncMock(return_value=None))
    bot = _SpyBot(sync_db_path)
    gid = await create_game(bot.games_db, 100, 1, "wyr", payload={"rounds": {}})
    cog = wyr_cog.WYRCog(bot)  # type: ignore[arg-type]
    channel = _channel()
    await cog._run_round(None, gid, 1, "Host", 1, channel)

    view = bot.active_views[gid]
    assert view.waiting is True
    assert view.vote_a.disabled and view.vote_b.disabled and view.next_btn.disabled
    assert not view.pose_question.disabled and not view.end_game_btn.disabled
    assert await get_active_game_by_id(bot.games_db, gid) is not None
    payload = await get_game_payload(bot.games_db, gid)
    assert payload["rounds"]["1"]["q"] == ""
    embed = channel.send.await_args.kwargs["embed"]
    assert "Pose Question" in (embed.description or "")

    # A posed question opens the round in place.
    await view.begin_round("fly", "swim", _message())
    assert view.waiting is False and not view.vote_a.disabled and not view.next_btn.disabled
    payload = await get_game_payload(bot.games_db, gid)
    assert payload["rounds"]["1"]["q"] == "fly OR swim"
    assert payload["rounds"]["1"]["opened_at"]


async def test_votes_are_persisted_on_every_press(sync_db_path):
    """vote-games-58: a restart mid-round used to rebuild an empty tally under
    a full bar, because votes reached the payload only on Next."""
    bot = _SpyBot(sync_db_path)
    gid = await create_game(
        bot.games_db, 100, 1, "wyr", payload={"rounds": {"1": {"a": [], "b": [], "q": "x OR y"}}},
    )
    cog = wyr_cog.WYRCog(bot)  # type: ignore[arg-type]
    view = cog._build_round_view(
        game_id=gid, host_id=1, host_name="Host", round_num=1, channel=_channel(),
        option_a="x", option_b="y",
    )
    await view.vote_a.callback(_interaction(7))  # type: ignore[arg-type]
    await view.vote_b.callback(_interaction(8))  # type: ignore[arg-type]
    payload = await get_game_payload(bot.games_db, gid)
    assert payload["rounds"]["1"] == {"a": [7], "b": [8], "shown": [], "q": "x OR y"}


async def test_show_my_vote_is_the_voters_own_and_survives_a_restart(sync_db_path):
    """vote-games-61: any voter can name themselves — no host gate — and
    the choice rides with the votes, so a rebuilt view still shows it."""
    bot = _SpyBot(sync_db_path)
    gid = await create_game(
        bot.games_db, 100, 1, "wyr", payload={"rounds": {"1": {"a": [], "b": [], "q": "x OR y"}}},
    )
    cog = wyr_cog.WYRCog(bot)  # type: ignore[arg-type]
    view = cog._build_round_view(
        game_id=gid, host_id=1, host_name="Host", round_num=1, channel=_channel(),
        option_a="x", option_b="y",
    )
    # No vote yet: refused, nothing shown.
    stranger = _interaction(9)
    await view.show_my_vote.callback(stranger)  # type: ignore[arg-type]
    assert "Vote" in stranger.response.send_message.await_args.args[0]
    assert view.shown == []

    await view.vote_a.callback(_interaction(7))  # type: ignore[arg-type]
    shower = _interaction(7)
    shower.response.edit_message = AsyncMock()
    await view.show_my_vote.callback(shower)  # type: ignore[arg-type]
    assert shower.response.edit_message.await_args is not None
    embed = shower.response.edit_message.await_args.kwargs["embed"]
    assert "User 7" in embed.fields[-1].value or "7" in embed.fields[-1].value
    payload = await get_game_payload(bot.games_db, gid)
    assert payload["rounds"]["1"]["shown"] == [7]

    row = await get_active_game_by_id(bot.games_db, gid)
    assert row is not None
    rebuilt_bot = _SpyBot(sync_db_path)
    rebuilt_bot.add_view = lambda *a, **k: None  # type: ignore[attr-defined]
    rebuilt = wyr_cog.WYRCog(rebuilt_bot)  # type: ignore[arg-type]
    assert await rebuilt.recover_game(row, payload, _channel(), _message())
    assert rebuilt_bot.active_views[gid].shown == [7]


async def test_end_game_posts_the_recap_and_pays_every_voter(monkeypatch, sync_db_path):
    """vote-games-52: the host's 🏁 End Game ends through the paying path with
    the recap (most divisive question + total votes)."""
    spy = AsyncMock(return_value=None)
    monkeypatch.setattr(wyr_cog, "end_game", spy)
    bot = _SpyBot(sync_db_path)
    payload = {"rounds": {
        "1": {"a": [1, 2], "b": [3], "q": "A OR B"},
        "2": {"a": [], "b": [], "q": "C OR D"},
    }}
    gid = await create_game(bot.games_db, 100, 1, "wyr", payload=payload)
    cog = wyr_cog.WYRCog(bot)  # type: ignore[arg-type]
    channel = _channel()
    view = cog._build_round_view(
        game_id=gid, host_id=1, host_name="Host", round_num=2, channel=channel,
        option_a="C", option_b="D",
    )
    bot.active_views[gid] = view
    view.votes_a, view.votes_b = [4], [1]
    message = _message()

    assert view.finish_callback is not None
    await view.finish_callback(message, "ended")

    assert view._closed is True
    message.edit.assert_awaited()  # the board is greyed out
    recap = channel.send.await_args.kwargs["embed"]
    assert "Game Over" in recap.title
    fields = {f.name: f.value for f in recap.fields}
    assert fields["Total Votes"].startswith("5")
    assert "C OR D" in fields["Most Divisive"]  # 1–1 beats 2–1
    call = spy.await_args
    assert call is not None and spy.await_count == 1
    assert call.kwargs["player_ids"] == [1, 2, 3, 4]
    assert call.kwargs["bot"] is bot
    assert call.kwargs["reason"] == "ended"
    assert call.kwargs["round_count"] == 2
    assert gid not in bot.active_views


async def test_expired_at_next_ends_with_the_recap_and_pays(monkeypatch, sync_db_path):
    """vote-games-59: pressing Next on a >24h game used to archive a bare,
    guild-0 row and pay nobody."""
    spy = AsyncMock(return_value=None)
    monkeypatch.setattr(wyr_cog, "end_game", spy)
    monkeypatch.setattr(wyr_cog, "is_game_expired", AsyncMock(return_value=True))
    bot = _SpyBot(sync_db_path)
    gid = await create_game(
        bot.games_db, 100, 1, "wyr", payload={"rounds": {"1": {"a": [], "b": [], "q": "x OR y"}}},
    )
    cog = wyr_cog.WYRCog(bot)  # type: ignore[arg-type]
    channel = _channel()
    view = cog._build_round_view(
        game_id=gid, host_id=1, host_name="Host", round_num=1, channel=channel,
        option_a="x", option_b="y",
    )
    bot.active_views[gid] = view
    view.votes_a = [5, 6]

    await view.advance_callback(_message())

    call = spy.await_args
    assert call is not None and spy.await_count == 1
    assert call.kwargs["player_ids"] == [5, 6]
    assert call.kwargs["bot"] is bot
    assert call.kwargs["reason"] == "expired"
    # The last round's votes were written before the archive.
    assert call.kwargs["payload"]["rounds"]["1"]["a"] == [5, 6]
    assert channel.send.await_count == 1  # the recap, and no new round


async def test_round_cap_ends_with_the_recap(monkeypatch, sync_db_path):
    """discovery-3: the configured last round posts the recap instead of
    opening another round."""
    spy = AsyncMock(return_value=None)
    monkeypatch.setattr(wyr_cog, "end_game", spy)
    bot = _SpyBot(sync_db_path)
    gid = await create_game(
        bot.games_db, 100, 1, "wyr",
        payload={"rounds": {"1": {"a": [], "b": [], "q": "a OR b"}, "2": {"a": [], "b": [], "q": "c OR d"}}, "max_rounds": 2},
    )
    cog = wyr_cog.WYRCog(bot)  # type: ignore[arg-type]
    channel = _channel()
    view = cog._build_round_view(
        game_id=gid, host_id=1, host_name="Host", round_num=2, channel=channel,
        option_a="c", option_b="d", pacing=RoundPacing(max_rounds=2),
    )
    bot.active_views[gid] = view
    view.votes_a = [9]

    await view.advance_callback(_message())

    call = spy.await_args
    assert call is not None and call.kwargs["reason"] == "round_cap"
    recap = channel.send.await_args.kwargs["embed"]
    assert "last round" in (recap.description or "")


async def test_launch_stores_pacing_and_the_scheduled_flag(monkeypatch, sync_db_path):
    """The slash/schedule option wins over the dashboard default; a launch with
    no host at the keyboard is marked scheduled (platform-23)."""
    monkeypatch.setattr(wyr_cog, "get_wyr_question", AsyncMock(return_value=("x", "y")))
    bot = _SpyBot(sync_db_path)
    await bot.games_db.execute(
        "INSERT INTO games_game_config (guild_id, game_type, enabled, options) VALUES (?, ?, 1, ?)",
        (9001, "wyr", '{"round_seconds": 45, "max_rounds": 4}'),
    )
    cog = wyr_cog.WYRCog(bot)  # type: ignore[arg-type]

    gid = await cog.launch(channel=_channel(), host_id=5, host_name="H", guild_id=9001, options={})
    assert gid is not None
    payload = await get_game_payload(bot.games_db, gid)
    assert (payload["round_seconds"], payload["max_rounds"], payload["scheduled"]) == (45, 4, False)
    bot.active_views[gid].pacing.cancel_timer()
    await end_game(bot.games_db, gid)

    gid = await cog.launch(
        channel=_channel(), host_id=0, host_name="Today's feature", guild_id=9001,
        options={"round_seconds": 0, "max_rounds": 0, "scheduled": True},
    )
    assert gid is not None
    payload = await get_game_payload(bot.games_db, gid)
    assert (payload["round_seconds"], payload["max_rounds"], payload["scheduled"]) == (0, 0, True)
    assert bot.active_views[gid].pacing.timer_task is None  # host-paced: no timer


async def test_scheduled_round_lets_a_voter_press_next_after_the_window(sync_db_path):
    """platform-23: a scheduled game whose creator isn't there cannot stall."""
    bot = _SpyBot(sync_db_path)
    advance = AsyncMock()
    view = wyr_cog.WYRRoundView(
        "g", 1, "x", "y", 1, True, bot.games_db, bot, "Host", advance,
        pacing=RoundPacing(round_seconds=0, scheduled=True, opened_at=1.0),
    )
    view.votes_a = [7]
    # A non-voter is told to vote first.
    interaction = _interaction(8)
    await view.next_btn.callback(interaction)  # type: ignore[arg-type]
    assert "Vote first" in interaction.response.send_message.await_args.args[0]
    advance.assert_not_awaited()
    # A voter, once the window has elapsed, advances the round.
    interaction = _interaction(7)
    await view.next_btn.callback(interaction)  # type: ignore[arg-type]
    interaction.response.defer.assert_awaited_once()
    advance.assert_awaited_once()

    # A hosted game keeps Next to the host / mods / Game Host role.
    hosted = wyr_cog.WYRRoundView(
        "g", 1, "x", "y", 1, True, bot.games_db, bot, "Host", AsyncMock(),
        pacing=RoundPacing(round_seconds=0, scheduled=False, opened_at=1.0),
    )
    hosted.votes_a = [7]
    interaction = _interaction(7)
    await hosted.next_btn.callback(interaction)  # type: ignore[arg-type]
    assert interaction.response.send_message.await_args.args[0] == ADVANCE_DENIED


async def test_slash_entry_refuses_a_busy_channel_through_the_shared_guard(sync_db_path):
    """platform-18: the slash entry runs the one launch guard (busy channel,
    empty bank, ...) instead of its own two checks."""
    bot = _SpyBot(sync_db_path)
    cog = wyr_cog.WYRCog(bot)  # type: ignore[arg-type]
    launch = AsyncMock(return_value="new")
    cog.launch = launch  # type: ignore[method-assign]
    await bot.games_db.execute(
        "INSERT INTO games_allowed_channels (channel_id, guild_id) VALUES (?, ?)", (100, 9001),
    )
    await create_game(bot.games_db, 100, 5, "nhie", state="playing", payload={}, guild_id=9001)
    interaction = _interaction(1)
    interaction.guild_id = 9001
    interaction.channel_id = 100

    await wyr_cog.WYRCog.wyr.callback(cog, interaction)  # type: ignore[attr-defined]

    launch.assert_not_awaited()
    text = interaction.response.send_message.await_args.args[0]
    assert "already a game running" in text and "Never Have I Ever" in text
