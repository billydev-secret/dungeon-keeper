"""AMA's endings, its dials, and what a restart must find again.

social-prompt-33: the recap + payout footer posts on *every* end path — the
host's 🏁 End AMA, ``/games end`` and the 24h sweep all go through the view's
``close_now``. social-prompt-34: answered questions, not asked ones, use up a
hot seat's turn, and the turn length is a dashboard dial. social-prompt-35:
screened approval buttons never expire and survive a restart. social-prompt-41:
the hot-seat ping role is a dial, not a role looked up by name.
social-prompt-42: the seat's hour is re-armed after a restart. social-prompt-43:
open question cards are retired when the game closes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import bot_modules.cogs.games_ama_cog as ama_mod
from bot_modules.cogs.games_ama_cog import (
    ENDED_CARD_FOOTER,
    END_DENIED_TEXT,
    GAME_ENDED_TEXT,
    REASON_HOST_ENDED,
    AMACog,
    AMAView,
    ScreenedQuestionView,
)
from bot_modules.cogs.games_config_cog import RECAP_ENDING_COGS
from bot_modules.core import branding
from bot_modules.games.utils.expiry_service import EXPIRE_REASON, sweep_expired_games
from bot_modules.games.utils.game_manager import (
    get_active_game_by_id,
    get_game_payload,
    update_game_payload,
)
from bot_modules.games_ama.logic import utcnow_iso
from bot_modules.services.games_db import GamesDb

GUILD = 99
CH = 4242


class _Response:
    def __init__(self):
        self.messages: list[tuple] = []
        self.modals: list = []
        self.edits: list[dict] = []
        self.deferred = False

    async def send_message(self, content=None, **kwargs):
        self.messages.append((content, kwargs))

    async def send_modal(self, modal):
        self.modals.append(modal)

    async def defer(self, *a, **kw):
        self.deferred = True

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)


class _Message:
    def __init__(self, mid: int, guild, *, with_embed: bool = True):
        self.id = mid
        self.guild = guild
        self.jump_url = f"http://discord/{mid}"
        self.edits: list[dict] = []
        self.embeds = [discord.Embed(title="Q")] if with_embed else []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if kwargs.get("embed") is not None:
            self.embeds = [kwargs["embed"]]

    async def delete(self):
        pass


def _member(uid: int, name: str, *, mod: bool = False):
    m = MagicMock(spec=discord.Member)
    m.id = uid
    m.display_name = name
    m.mention = f"<@{uid}>"
    m.guild_permissions = SimpleNamespace(manage_guild=mod, administrator=False, manage_messages=mod)
    return m


class _Guild:
    def __init__(self, members):
        self.id = GUILD
        self._members = {m.id: m for m in members}
        self.roles: list = []
        self.me = None

    def get_member(self, uid):
        return self._members.get(uid)


class _Channel:
    def __init__(self, guild):
        self.id = CH
        self.name = "games"
        self.guild = guild
        self.mention = "#games"
        self.sends: list[tuple] = []
        self.messages: dict[int, _Message] = {}
        self._next_id = 9000

    async def send(self, content=None, **kwargs):
        self._next_id += 1
        msg = _Message(self._next_id, self.guild, with_embed="embed" in kwargs)
        self.messages[msg.id] = msg
        self.sends.append((content, kwargs, msg))
        return msg

    async def fetch_message(self, mid):
        try:
            return self.messages[int(mid)]
        except KeyError:
            raise discord.NotFound(MagicMock(status=404), "gone")


class _Bot:
    def __init__(self, db_path, channel=None):
        self.games_db = GamesDb(db_path)
        self.active_views: dict[str, Any] = {}
        self.ctx = SimpleNamespace(db_path=db_path)
        self.added_views: list[tuple[Any, int | None]] = []
        self._channel = channel
        self._cog: AMACog | None = None

    def get_cog(self, name):
        return self._cog if name == "AMACog" else None

    def get_guild(self, _gid):
        return None

    def get_channel(self, cid):
        return self._channel if self._channel is not None and cid == CH else None

    def add_view(self, view, *, message_id=None):
        self.added_views.append((view, message_id))


def _interaction(user, channel, guild, bot):
    return SimpleNamespace(
        user=user, channel=channel, guild=guild, client=bot, message=None,
        channel_id=channel.id, guild_id=guild.id, response=_Response(),
    )


@pytest.fixture
def stubs(monkeypatch):
    async def _accent(_db_path, _guild):
        return discord.Color(0x5865F2)

    async def _audit(*_a, **_kw):
        return None

    async def _footer(_bot, _embed, _guild_id, _game_type):
        return None

    from bot_modules.economy import game_rewards

    monkeypatch.setattr(branding, "resolve_accent_color", _accent)
    monkeypatch.setattr(ama_mod, "audit_anonymous", _audit)
    monkeypatch.setattr(game_rewards, "append_payout_footer", _footer)


async def _live_game(db_path, *, mode="unfiltered", game_format="hot_seat", options=None):
    host = _member(1, "Host")
    seat = _member(2, "Seat")
    asker = _member(3, "Asker")
    stranger = _member(4, "Stranger")
    guild = _Guild([host, seat, asker, stranger])
    channel = _Channel(guild)
    bot = _Bot(db_path, channel)
    cog = AMACog(bot)  # type: ignore[arg-type]
    bot._cog = cog
    if options:
        await bot.games_db.execute(
            "INSERT INTO games_game_config (guild_id, game_type, enabled, options) VALUES (?, ?, 1, ?)",
            (GUILD, "ama", __import__("json").dumps(options)),
        )
    game_id = await cog.launch(
        channel=channel, host_id=host.id, host_name="Host", guild_id=GUILD,
        options={"mode": mode, "format": game_format},
    )
    assert game_id
    view = bot.active_views[game_id]
    return SimpleNamespace(
        bot=bot, cog=cog, guild=guild, channel=channel, view=view, game_id=game_id,
        host=host, seat=seat, asker=asker, stranger=stranger,
    )


def _recap_posts(channel):
    return [s for s in channel.sends if (e := s[1].get("embed")) is not None and "Game Over" in (e.title or "")]


# ── 🏁 End AMA (social-prompt-33) ────────────────────────────────────


async def test_end_ama_is_host_or_mod_only(sync_db_path, stubs):
    g = await _live_game(sync_db_path)
    inter = _interaction(g.stranger, g.channel, g.guild, g.bot)
    await g.view.end_ama.callback(inter)  # type: ignore[arg-type]
    assert inter.response.messages == [(END_DENIED_TEXT, {"ephemeral": True})]
    assert not g.view._closed


async def test_end_ama_confirms_then_posts_the_recap_and_pays(sync_db_path, stubs, monkeypatch):
    g = await _live_game(sync_db_path)
    spy = AsyncMock()
    monkeypatch.setattr(ama_mod, "end_game", spy)
    inter = _interaction(g.host, g.channel, g.guild, g.bot)
    await g.view.end_ama.callback(inter)  # type: ignore[arg-type]

    (text, kwargs) = inter.response.messages[-1]
    assert "Are you sure" in text and kwargs["ephemeral"] is True
    assert not g.view._closed, "nothing closes before the confirm"

    confirm = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))
    await kwargs["view"].confirm.callback(confirm)

    assert g.view._closed
    assert _recap_posts(g.channel), "the recap card must post"
    assert spy.await_args is not None
    assert spy.await_args.kwargs["reason"] == REASON_HOST_ENDED
    assert spy.await_args.kwargs["bot"] is g.bot
    assert g.game_id not in g.bot.active_views


async def test_end_ama_on_a_closed_game_says_so(sync_db_path, stubs):
    g = await _live_game(sync_db_path)
    g.view._closed = True
    inter = _interaction(g.host, g.channel, g.guild, g.bot)
    await g.view.end_ama.callback(inter)  # type: ignore[arg-type]
    assert inter.response.messages[-1][0] == "This game already ended."


# ── /games end and the 24h sweep prefer the recap ending ─────────────


def test_games_end_hands_ama_to_its_recap_ending():
    assert RECAP_ENDING_COGS["ama"] == "AMACog"


async def test_end_with_recap_closes_the_live_view(sync_db_path, stubs, monkeypatch):
    g = await _live_game(sync_db_path)
    spy = AsyncMock()
    monkeypatch.setattr(ama_mod, "end_game", spy)
    assert await g.cog.end_with_recap(g.channel, g.game_id) is True
    assert _recap_posts(g.channel)
    assert spy.await_args is not None and spy.await_args.kwargs["reason"] == REASON_HOST_ENDED
    # No live view (a failed recovery) -> the caller falls back to force-close.
    assert await g.cog.end_with_recap(g.channel, g.game_id) is False
    assert await g.cog.end_with_recap(g.channel, "no-such-game") is False


async def test_sweep_closes_an_ama_through_its_own_recap(sync_db_path, stubs):
    g = await _live_game(sync_db_path)
    payload = await get_game_payload(g.bot.games_db, g.game_id)
    payload["questions"] = [{"asker_id": 3, "hot_seat_id": 2, "status": "answered"}]
    await update_game_payload(g.bot.games_db, g.game_id, payload)
    await g.bot.games_db.execute(
        "UPDATE games_active_games SET created_at = datetime('now', '-30 hours') WHERE game_id = ?",
        (g.game_id,),
    )

    assert await sweep_expired_games(g.bot, g.bot.games_db) == 1

    assert _recap_posts(g.channel), "the sweep used to end an AMA silently"
    assert await get_active_game_by_id(g.bot.games_db, g.game_id) is None
    assert g.game_id not in g.bot.active_views
    row = await g.bot.games_db.fetchone(
        "SELECT payload FROM games_game_history WHERE game_id = ?", (g.game_id,)
    )
    assert row is not None and f'"reason": "{EXPIRE_REASON}"' in row["payload"]
    # One recap, no separate "archived after 24h" line on top of it.
    assert not any("archived after" in (s[0] or "") for s in g.channel.sends)


async def test_sweep_falls_back_to_archiving_when_the_close_raises(sync_db_path, stubs):
    g = await _live_game(sync_db_path)
    await g.bot.games_db.execute(
        "UPDATE games_active_games SET created_at = datetime('now', '-30 hours') WHERE game_id = ?",
        (g.game_id,),
    )

    async def _boom(_channel, *, reason=None):
        raise RuntimeError("discord hiccup")

    g.view.close_now = _boom  # type: ignore[method-assign]

    assert await sweep_expired_games(g.bot, g.bot.games_db) == 1
    assert await get_active_game_by_id(g.bot.games_db, g.game_id) is None


# ── closing retires the open cards (social-prompt-43) ────────────────


async def test_close_retires_open_question_cards_with_a_footer(sync_db_path, stubs, monkeypatch):
    g = await _live_game(sync_db_path)
    monkeypatch.setattr(ama_mod, "end_game", AsyncMock())
    open_card = await g.channel.send(embed=discord.Embed(title="Q1"), view=object())
    done_card = await g.channel.send(embed=discord.Embed(title="Q2"), view=object())
    payload = await get_game_payload(g.bot.games_db, g.game_id)
    payload["questions"] = [
        {"asker_id": 3, "hot_seat_id": 2, "status": "approved", "question_message_id": open_card.id},
        {"asker_id": 3, "hot_seat_id": 2, "status": "answered", "question_message_id": done_card.id},
        {"asker_id": 3, "hot_seat_id": 2, "status": "approved", "question_message_id": 424242},  # deleted
    ]
    await update_game_payload(g.bot.games_db, g.game_id, payload)

    await g.view.close_now(g.channel)

    assert open_card.edits and open_card.edits[-1]["view"] is None
    assert open_card.embeds[0].footer.text == ENDED_CARD_FOOTER
    assert not done_card.edits, "an answered card is left as it is"


async def test_reply_and_pass_refuse_after_the_game_closed(sync_db_path, stubs):
    g = await _live_game(sync_db_path)
    g.view.hot_seat_id = g.seat.id
    card = ama_mod.QuestionView(g.game_id, g.seat.id, g.bot.games_db, 0, g.asker.id, g.view, "Q?")
    g.view._closed = True
    for button in (card.reply_question, card.pass_question):
        inter = _interaction(g.seat, g.channel, g.guild, g.bot)
        await button.callback(inter)  # type: ignore[arg-type]
        assert inter.response.messages[-1] == (GAME_ENDED_TEXT, {"ephemeral": True})
    modal = ama_mod.ReplyModal(g.game_id, g.bot.games_db, 0, g.asker.id, g.view, "Q?")
    modal.reply._value = "late"
    inter = _interaction(g.seat, g.channel, g.guild, g.bot)
    await modal.on_submit(inter)
    assert inter.response.messages[-1] == (GAME_ENDED_TEXT, {"ephemeral": True})


# ── answered questions use up the turn (social-prompt-34) ────────────


async def test_the_seat_rotates_on_answers_not_asks(sync_db_path, stubs):
    g = await _live_game(sync_db_path, options={"questions_per_turn": 2})
    view: AMAView = g.view
    assert view.per_turn == 2
    await view._set_hot_seat(g.seat, g.channel, announce=False)

    for _ in range(3):
        await view.after_question_posted(g.channel)
    assert view.questions_this_turn == 0 and view.hot_seat_id == g.seat.id

    await view.after_question_resolved(g.channel, g.seat.id)
    assert view.questions_this_turn == 1 and view.hot_seat_id == g.seat.id
    # A card from an earlier seat, answered late, is not this seat's turn.
    await view.after_question_resolved(g.channel, g.host.id)
    assert view.questions_this_turn == 1
    await view.after_question_resolved(g.channel, g.seat.id)
    assert view.hot_seat_id is None, "two answers on a two-question dial rotate the seat"
    assert any("turn complete" in (s[0] or "") for s in g.channel.sends)


async def test_questions_per_turn_dial_is_clamped_and_persisted(sync_db_path, stubs):
    g = await _live_game(sync_db_path, options={"questions_per_turn": 0})
    assert g.view.per_turn == 1
    payload = await get_game_payload(g.bot.games_db, g.game_id)
    assert payload["questions_per_turn"] == 1


# ── the hot-seat ping role dial (social-prompt-41) ───────────────────


async def test_hot_seat_announcement_pings_the_dialled_role(sync_db_path, stubs):
    role_id = 123456789012345678
    g = await _live_game(sync_db_path, options={"hot_seat_ping_role_id": str(role_id)})
    assert g.view.ping_role_id == role_id
    await g.view._set_hot_seat(g.seat, g.channel, announce=True)
    content, kwargs, _ = g.channel.sends[-1]
    assert content.startswith(f"<@&{role_id}> ")
    allowed = kwargs["allowed_mentions"]
    assert [r.id for r in allowed.roles] == [role_id]
    assert allowed.everyone is False and allowed.users is True


async def test_no_dial_means_no_role_ping_even_if_a_role_is_named_ama(sync_db_path, stubs):
    g = await _live_game(sync_db_path)
    g.guild.roles = [SimpleNamespace(name="AMA", mention="<@&1>")]
    await g.view._set_hot_seat(g.seat, g.channel, announce=True)
    content, kwargs, _ = g.channel.sends[-1]
    assert "<@&" not in content
    assert kwargs["allowed_mentions"] is None


# ── screened approval survives the host's coffee and a restart (35) ──


async def test_screened_view_is_persistent_and_remembers_its_dm(sync_db_path, stubs, monkeypatch):
    g = await _live_game(sync_db_path, mode="screened")
    g.view.hot_seat_id = g.seat.id
    dm_msg = _Message(777, g.guild)
    captured: dict = {}

    async def _dm(member, **kwargs):
        captured.update(kwargs)
        return dm_msg

    monkeypatch.setattr(ama_mod, "send_branded_dm", _dm)
    modal = ama_mod.AskQuestionModal(
        g.game_id, g.bot.games_db, g.channel, "screened", g.host.id, g.seat.id, g.view,
    )
    modal.question._value = "the actual question"
    await modal.on_submit(_interaction(g.asker, g.channel, g.guild, g.bot))

    view = captured["view"]
    assert isinstance(view, ScreenedQuestionView)
    assert view.timeout is None
    assert all(getattr(c, "custom_id", None) for c in view.children)
    assert "the actual question" in (captured["embed"].description or ""), (
        "the host must be able to read what they are approving"
    )
    payload = await get_game_payload(g.bot.games_db, g.game_id)
    assert payload["questions"][0]["host_dm_message_id"] == 777


async def test_screened_approve_after_close_retires_the_dm(sync_db_path, stubs):
    g = await _live_game(sync_db_path, mode="screened")
    g.view._closed = True
    view = ScreenedQuestionView(g.game_id, "q?", 0, g.bot.games_db, g.channel, g.seat.id, g.asker.id, g.view)
    inter = _interaction(g.host, g.channel, g.guild, g.bot)
    await view.approve.callback(inter)  # type: ignore[arg-type]
    assert GAME_ENDED_TEXT in inter.response.edits[-1]["content"]
    assert not any("embed" in s[1] and s[1].get("view") for s in g.channel.sends[2:])


# ── recover_game: the timer, the DMs, the dials (35 / 42) ────────────


async def test_recover_rearms_the_seat_timer_and_the_pending_dms(sync_db_path, stubs, monkeypatch):
    g = await _live_game(sync_db_path, mode="screened", options={
        "hot_seat_ping_role_id": "555", "questions_per_turn": 6,
    })
    started = datetime.now(timezone.utc) - timedelta(minutes=20)
    payload = await get_game_payload(g.bot.games_db, g.game_id)
    payload["hot_seat_id"] = g.seat.id
    payload["hot_seat_started_at"] = utcnow_iso(started)
    payload["questions"] = [
        {"asker_id": 3, "hot_seat_id": 2, "status": "pending", "host_dm_message_id": 777, "text": "q1"},
        {"asker_id": 3, "hot_seat_id": 2, "status": "pending"},           # DM never landed
        {"asker_id": 3, "hot_seat_id": 2, "status": "rejected", "host_dm_message_id": 778},
    ]
    await update_game_payload(g.bot.games_db, g.game_id, payload)
    row = await get_active_game_by_id(g.bot.games_db, g.game_id)
    assert row is not None
    payload = await get_game_payload(g.bot.games_db, g.game_id)

    armed: list[int] = []
    monkeypatch.setattr(
        AMAView, "_start_hot_seat_timer",
        lambda self, channel, seconds=3600: armed.append(seconds),
    )
    fresh_bot = _Bot(sync_db_path, g.channel)
    cog = AMACog(fresh_bot)  # type: ignore[arg-type]
    anchor = _Message(int(row["message_id"]), g.guild)

    assert await cog.recover_game(row, payload, g.channel, anchor) is True

    view = fresh_bot.active_views[g.game_id]
    assert view.hot_seat_id == g.seat.id
    assert view.ping_role_id == 555 and view.per_turn == 6
    assert len(armed) == 1 and 2350 <= armed[0] <= 2400, "twenty minutes in: forty left"
    screened = [(v, mid) for v, mid in fresh_bot.added_views if isinstance(v, ScreenedQuestionView)]
    assert [mid for _, mid in screened] == [777]
    assert screened[0][0].question_idx == 0 and screened[0][0].ama_view is view


async def test_recover_without_a_seat_arms_no_timer(sync_db_path, stubs, monkeypatch):
    g = await _live_game(sync_db_path)
    row = await get_active_game_by_id(g.bot.games_db, g.game_id)
    assert row is not None
    payload = await get_game_payload(g.bot.games_db, g.game_id)
    armed: list[int] = []
    monkeypatch.setattr(
        AMAView, "_start_hot_seat_timer",
        lambda self, channel, seconds=3600: armed.append(seconds),
    )
    fresh_bot = _Bot(sync_db_path, g.channel)
    cog = AMACog(fresh_bot)  # type: ignore[arg-type]
    assert await cog.recover_game(row, payload, g.channel, _Message(int(row["message_id"]), g.guild))
    assert armed == []


async def test_set_hot_seat_records_when_the_seat_started(sync_db_path, stubs):
    g = await _live_game(sync_db_path)
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    await g.view._set_hot_seat(g.seat, g.channel, announce=False)
    payload = await get_game_payload(g.bot.games_db, g.game_id)
    started = datetime.fromisoformat(payload["hot_seat_started_at"])
    assert started >= before
    # Never leave the timer running into the next test.
    if g.view._hot_seat_timer_task:
        g.view._hot_seat_timer_task.cancel()


async def test_a_game_without_a_channel_is_archived_not_closed(sync_db_path, stubs):
    """The sweep can only post a recap where it can post at all."""
    g = await _live_game(sync_db_path)
    g.bot._channel = None
    await g.bot.games_db.execute(
        "UPDATE games_active_games SET created_at = datetime('now', '-30 hours') WHERE game_id = ?",
        (g.game_id,),
    )
    assert await sweep_expired_games(g.bot, g.bot.games_db) == 1
    assert not _recap_posts(g.channel)
    assert await get_active_game_by_id(g.bot.games_db, g.game_id) is None
