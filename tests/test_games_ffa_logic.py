"""Tests for the Truth-or-Dare card (FFA) prompt bank.

Covers ``bot_modules/games_ffa/prompts.py`` (prompt bank + picker). The
cog itself reuses the confession bot's anonymous-identity machinery for
replies, which is covered by the confessions tests; FFA's only standalone
pure logic is the prompt picker.
"""

from __future__ import annotations

import asyncio
import json

from bot_modules.games.utils.question_source import get_ffa_prompt
from bot_modules.games_ffa.prompts import (
    DARE,
    TRUTH,
    TRUTH_NSFW,
    TRUTH_SFW,
    label_for_kind,
    pick_prompt,
)


def test_pick_prompt_truth_returns_truth_label():
    label, text = pick_prompt("truth", nsfw=False)
    assert label == TRUTH
    assert isinstance(text, str) and text


def test_pick_prompt_dare_returns_dare_label():
    label, text = pick_prompt("dare", nsfw=True)
    assert label == DARE
    assert isinstance(text, str) and text


def test_pick_prompt_random_always_returns_a_valid_label():
    for _ in range(50):
        label, text = pick_prompt("random", nsfw=False)
        assert label in (TRUTH, DARE)
        assert text


def test_pick_prompt_sfw_pulls_from_sfw_bank():
    nsfw_only = set(TRUTH_NSFW) - set(TRUTH_SFW)
    for _ in range(50):
        _, text = pick_prompt("truth", nsfw=False)
        assert text not in nsfw_only


def test_pick_prompt_nsfw_pulls_from_nsfw_bank():
    sfw_only = set(TRUTH_SFW) - set(TRUTH_NSFW)
    for _ in range(50):
        _, text = pick_prompt("truth", nsfw=True)
        assert text not in sfw_only


def test_label_for_kind_defaults_to_truth():
    assert label_for_kind("dare") == DARE
    assert label_for_kind("truth") == TRUTH
    assert label_for_kind("random") == TRUTH


# ── get_ffa_prompt (bank-backed, with code fallback) ──────────────────────────


class _FakeDB:
    def __init__(self, rows):
        # rows: (game_type, tags_list, question_text[, last_served_at])
        self._rows = rows
        self.served: list[int] = []

    async def fetchall(self, sql, params):
        (game_type,) = params
        return [
            (qid, r[2], json.dumps(r[1]), r[3] if len(r) > 3 else None)
            for qid, r in enumerate(self._rows)
            if r[0] == game_type
        ]

    async def execute(self, sql, params):
        (qid,) = params
        self.served.append(qid)


def _run(coro):
    return asyncio.run(coro)


def test_get_ffa_prompt_kind_drives_label():
    db = _FakeDB([
        ("ffa", ["truth"], "A truth."),
        ("ffa", ["dare"], "A dare."),
    ])
    assert _run(get_ffa_prompt(db, kind="truth")) == (TRUTH, "A truth.")
    assert _run(get_ffa_prompt(db, kind="dare")) == (DARE, "A dare.")


def test_get_ffa_prompt_excludes_nsfw_unless_allow_nsfw():
    """NSFW is gated on the channel's age-restriction flag (``allow_nsfw``);
    requesting the 'nsfw' tag cannot re-enable it."""
    db = _FakeDB([
        ("ffa", ["truth"], "Tame truth."),
        ("ffa", ["truth", "nsfw"], "Spicy truth."),
    ])
    # Default (no channel opt-in) → nsfw rows are excluded.
    seen = {_run(get_ffa_prompt(db, kind="truth"))[1] for _ in range(40)}
    assert seen == {"Tame truth."}
    # Requesting the 'nsfw' tag without allow_nsfw doesn't re-enable NSFW —
    # only tame content comes back.
    seen = {_run(get_ffa_prompt(db, kind="truth", tags=["nsfw"]))[1] for _ in range(40)}
    assert seen == {"Tame truth."}
    # An nsfw-only pool with the tag requested but no channel opt-in is a
    # filtered miss (no code-bank fallback when a tag filter was supplied).
    nsfw_only = _FakeDB([("ffa", ["truth", "nsfw"], "Spicy truth.")])
    assert _run(get_ffa_prompt(nsfw_only, kind="truth", tags=["nsfw"])) is None
    # Channel opt-in (allow_nsfw=True) → both pools are candidates.
    seen = {_run(get_ffa_prompt(db, kind="truth", allow_nsfw=True))[1] for _ in range(40)}
    assert seen == {"Tame truth.", "Spicy truth."}


def test_get_ffa_prompt_filtered_miss_returns_none():
    db = _FakeDB([("ffa", ["truth"], "Only truth.")])
    assert _run(get_ffa_prompt(db, kind="random", tags=["nope"])) is None


def test_get_ffa_prompt_empty_bank_falls_back_to_code():
    db = _FakeDB([])
    label, text = _run(get_ffa_prompt(db, kind="truth"))
    assert label == TRUTH and isinstance(text, str) and text


# ── exclude (the Next button's seen-set) ──────────────────────────────────────

def test_get_ffa_prompt_exclude_skips_seen():
    db = _FakeDB([
        ("ffa", ["truth"], "First truth."),
        ("ffa", ["truth"], "Second truth."),
    ])
    # With one shown, only the other can come back.
    seen = {_run(get_ffa_prompt(db, kind="truth", exclude=["First truth."]))[1] for _ in range(30)}
    assert seen == {"Second truth."}


def test_get_ffa_prompt_exclude_exhausted_returns_none():
    """When every match is excluded the set is exhausted → None (no code fallback,
    even unfiltered), signalling the caller to reset the seen-set."""
    db = _FakeDB([("ffa", ["truth"], "Only truth.")])
    assert _run(get_ffa_prompt(db, kind="truth", exclude=["Only truth."])) is None


# ── round-robin: least-recently-served wins over pure randomness ─────


def test_get_ffa_prompt_prefers_never_served_row():
    db = _FakeDB([
        ("ffa", ["truth"], "Served already.", "2026-07-01 00:00:00"),
        ("ffa", ["truth"], "Never served.", None),
    ])
    for _ in range(25):
        assert _run(get_ffa_prompt(db, kind="truth"))[1] == "Never served."


def test_get_ffa_prompt_marks_the_served_row():
    db = _FakeDB([("ffa", ["truth"], "Only truth.")])
    _run(get_ffa_prompt(db, kind="truth"))
    assert db.served == [0]


# ── the ending FFA never had: repliers, recap, Close, payout (anon-tail-70) ──

from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

import discord  # noqa: E402
import pytest  # noqa: E402

import bot_modules.cogs.games_ffa_cog as ffa_cog  # noqa: E402
from bot_modules.games.utils.game_manager import create_game, get_active_game  # noqa: E402
from bot_modules.games.utils.game_roster import NO_ROSTER_TYPES, roster_from_payload  # noqa: E402
from bot_modules.games.utils.question_source import has_matching_questions  # noqa: E402
from bot_modules.games_ffa.logic import note_reply, recap_summary, roster_from_prompts  # noqa: E402
from bot_modules.services.games_db import GamesDb  # noqa: E402


def test_note_reply_counts_every_reply_but_lists_a_replier_once():
    entry = {"message_id": 1, "prompt": "p", "label": "TRUTH", "reply_count": 0}
    assert note_reply(entry, 9) == 1
    assert note_reply(entry, 9) == 2
    assert note_reply(entry, 4) == 3
    assert entry["repliers"] == [9, 4]


def test_note_reply_tolerates_a_legacy_entry_without_a_repliers_list():
    entry = {"message_id": 1, "prompt": "p", "label": "TRUTH", "reply_count": 2}
    note_reply(entry, 5)
    assert entry == {"message_id": 1, "prompt": "p", "label": "TRUTH", "reply_count": 3, "repliers": [5]}


@pytest.mark.parametrize(
    ("prompts", "expected"),
    [
        pytest.param(None, [], id="no-prompts"),
        pytest.param([{"repliers": [3, 1]}, {"repliers": [1, "2", "junk"]}, "not-a-dict"], [1, 2, 3], id="deduped-sorted-coerced"),
    ],
)
def test_roster_from_prompts(prompts, expected):
    assert roster_from_prompts(prompts) == expected


def test_recap_summary_names_the_busiest_prompt_and_counts_nobody_by_name():
    prompts = [
        {"label": "TRUTH", "prompt": "a", "reply_count": 1, "repliers": [1]},
        {"label": "DARE", "prompt": "b", "reply_count": 3, "repliers": [1, 2, 3]},
        {"label": "TRUTH", "prompt": "c", "reply_count": 3, "repliers": [2]},
    ]
    summary = recap_summary(prompts)
    assert summary is not None
    assert summary["busiest"] == ("DARE", "b", 3)  # first wins the tie
    assert summary["total_replies"] == 7
    assert summary["prompt_count"] == 3
    assert summary["repliers"] == [1, 2, 3]
    assert summary["per_prompt"][0] == ("TRUTH", "a", 1)


def test_recap_summary_with_no_replies_has_no_busiest_prompt():
    summary = recap_summary([{"label": "TRUTH", "prompt": "a", "reply_count": 0}])
    assert summary is not None and summary["busiest"] is None and summary["total_replies"] == 0
    assert recap_summary([]) is None


def test_ffa_left_the_no_roster_set_and_pays_its_repliers_from_the_payload():
    """The sweep and /games end rebuild the same roster the host's Close pays."""
    assert "ffa" not in NO_ROSTER_TYPES
    payload = {"mode": "embed", "prompts": [{"repliers": [4, 9]}, {"repliers": [9, 2]}]}
    assert roster_from_payload("ffa", payload) == ([4, 9, 2], 2)
    assert roster_from_payload("ffa", {"mode": "banner"}) == ([], 0)


def test_recap_embed_quotes_prompts_and_names_nobody():
    summary = recap_summary([
        {"label": "TRUTH", "prompt": "x" * 200, "reply_count": 2, "repliers": [123456789012345678, 5]},
    ])
    assert summary is not None
    embed = ffa_cog.build_recap_embed(summary, color=discord.Color.blue())
    fields = {f.name: f.value for f in embed.fields}
    assert "🔥 Busiest Prompt" in fields and fields["Total Replies"] == "2" and fields["Repliers"] == "2"
    assert "123456789012345678" not in str(embed.to_dict())
    assert "…" in fields["Prompts"]


class _SpyBot:
    def __init__(self, db_path) -> None:
        self.games_db = GamesDb(db_path)
        self.active_views: dict = {}
        self.ctx = SimpleNamespace(db_path=db_path)

    def get_cog(self, name):
        return None

    def add_view(self, *args, **kwargs) -> None:
        pass


class _Channel:
    """Records sends; prompt messages are fetchable so Close can retire them."""

    def __init__(self, guild=None) -> None:
        self.id = 100
        self.guild = guild
        self.sent: list[dict] = []
        self.retired: list[int] = []

    async def send(self, *args, **kwargs):
        self.sent.append({"args": args, **kwargs})
        return SimpleNamespace(id=999)

    async def fetch_message(self, message_id: int):
        channel = self

        class _Msg:
            async def edit(self, **kwargs):
                channel.retired.append(message_id)

        return _Msg()


async def test_close_retires_the_prompts_posts_the_recap_and_pays_the_repliers(monkeypatch, sync_db_path):
    spy = AsyncMock()
    monkeypatch.setattr(ffa_cog, "end_game", spy)
    cleared = []
    monkeypatch.setattr(ffa_cog, "clear_anon_identities", lambda db_path, gid, roots: cleared.append((gid, list(roots))))
    monkeypatch.setattr(ffa_cog, "safe_resolve_accent", AsyncMock(return_value=discord.Color.blue()))
    from bot_modules.economy import game_rewards
    monkeypatch.setattr(game_rewards, "append_payout_footer", AsyncMock())
    bot = _SpyBot(sync_db_path)
    payload = {
        "mode": "embed",
        "prompts": [
            {"message_id": 11, "prompt": "a", "label": "TRUTH", "reply_count": 2, "repliers": [4, 9]},
            {"message_id": 12, "prompt": "b", "label": "TRUTH", "reply_count": 1, "repliers": [9]},
        ],
    }
    gid = await create_game(bot.games_db, 100, 1, "ffa", payload=payload)
    bot.active_views[gid] = object()
    channel = _Channel(guild=SimpleNamespace(id=9001))

    await ffa_cog.finish_ffa_game(bot, bot.games_db, gid, channel)

    assert channel.retired == [11, 12]
    assert isinstance(channel.sent[-1]["embed"], discord.Embed)
    assert cleared == [(9001, [11, 12])]
    assert gid not in bot.active_views
    call = spy.await_args
    assert call is not None and call.kwargs["player_ids"] == [4, 9]
    assert call.kwargs["reason"] == "ended" and call.kwargs["round_count"] == 2
    assert call.kwargs["bot"] is bot


async def test_close_with_no_replies_says_so_and_pays_nobody(monkeypatch, sync_db_path):
    spy = AsyncMock()
    monkeypatch.setattr(ffa_cog, "end_game", spy)
    bot = _SpyBot(sync_db_path)
    payload = {"mode": "embed", "prompts": [{"message_id": 11, "prompt": "a", "label": "TRUTH", "reply_count": 0}]}
    gid = await create_game(bot.games_db, 100, 1, "ffa", payload=payload)
    channel = _Channel()

    await ffa_cog.finish_ffa_game(bot, bot.games_db, gid, channel)

    assert "nothing to recap" in channel.sent[-1]["args"][0]
    assert spy.await_args.kwargs["player_ids"] == []


def _press(user_id: int, channel):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, display_name=f"U{user_id}"),
        channel=channel, channel_id=100, guild=None, guild_id=9001,
        message=SimpleNamespace(id=11, edit=AsyncMock(), embeds=[]),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


async def test_close_button_is_host_only_and_confirms_first(sync_db_path):
    bot = _SpyBot(sync_db_path)
    gid = await create_game(bot.games_db, 100, 1, "ffa", payload={"mode": "embed", "prompts": []})
    view = ffa_cog.FFAEmbedView(gid, 1, "a", "TRUTH", discord.Color.blue(), bot.games_db, bot)
    channel = _Channel()

    stranger = _press(42, channel)
    await view.close_game.callback(stranger)  # type: ignore[arg-type]
    assert stranger.response.send_message.await_args.args[0].startswith("❌")
    assert await get_active_game(bot.games_db, 100) is not None

    host = _press(1, channel)
    await view.close_game.callback(host)  # type: ignore[arg-type]
    confirm = host.response.send_message.await_args.kwargs["view"]
    assert isinstance(confirm, ffa_cog.ConfirmCloseView)
    await confirm._callback(_press(1, channel))
    assert await get_active_game(bot.games_db, 100) is None


def test_the_reply_button_command_defaults_to_truth_and_the_banner_to_random():
    bot = _SpyBot(":memory:")
    cog = ffa_cog.FFACog(bot)  # type: ignore[arg-type]
    kind = next(p for p in cog.ffa.parameters if p.name == "kind")
    assert kind.default == "truth"
    banner_kind = next(p for p in cog.ffa_banner.parameters if p.name == "kind")
    assert banner_kind.default == "random"


async def test_a_launch_that_names_no_kind_draws_a_truth(monkeypatch):
    seen: list[dict] = []

    async def fake_prompt(db, **kwargs):
        seen.append(kwargs)
        return None  # a miss is enough: only the kind asked for matters here

    monkeypatch.setattr(ffa_cog, "get_ffa_prompt", fake_prompt)
    bot = _SpyBot(":memory:")
    cog = ffa_cog.FFACog(bot)  # type: ignore[arg-type]
    channel = SimpleNamespace(id=100, guild=None)
    assert await cog.launch(channel=channel, host_id=1, host_name="H", guild_id=9001, options={"tags": []}) is None
    assert seen[-1]["kind"] == "truth"
    assert await cog.launch_banner(channel=channel, host_id=1, host_name="H", guild_id=9001, options={}) is None
    assert seen[-1]["kind"] == "random"


# ── kind + tags miss is refused at the command (anon-tail-76) ──

@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        pytest.param("dare", False, id="no-dare-carries-the-tag"),
        pytest.param("truth", True, id="a-truth-does"),
        pytest.param(None, True, id="random-places-no-kind-requirement"),
    ],
)
def test_has_matching_questions_honours_the_ffa_kind(kind, expected):
    db = _FakeDB([
        ("ffa", ["truth", "lily"], "A lily truth."),
        ("ffa", ["dare"], "An untagged dare."),
    ])
    assert _run(has_matching_questions(db, "ffa", ["lily"], kind=kind)) is expected


async def test_slash_entry_refuses_a_kind_and_tag_combination_the_bank_cannot_serve(monkeypatch, sync_db_path):
    bot = _SpyBot(sync_db_path)
    cog = ffa_cog.FFACog(bot)  # type: ignore[arg-type]
    launch = AsyncMock()
    monkeypatch.setattr(cog, "launch", launch)
    await bot.games_db.execute(
        "INSERT INTO games_allowed_channels (channel_id, guild_id) VALUES (?, ?)", (100, 9001),
    )
    await bot.games_db.execute(
        "INSERT INTO games_question_bank (game_type, question_text, tags) VALUES (?, ?, ?)",
        ("ffa", "A lily truth.", json.dumps(["truth", "lily"])),
    )
    channel = SimpleNamespace(id=100, guild=None, is_nsfw=lambda: False)
    interaction = _press(1, channel)

    await cog.ffa.callback(cog, interaction, kind="dare", tags="lily")  # type: ignore[arg-type]

    sent = interaction.response.send_message.await_args
    assert sent.kwargs["ephemeral"] is True and "lily" in sent.args[0]
    launch.assert_not_awaited()
    interaction.response.defer.assert_not_awaited()
