"""Drives the FFA embed-mode Next button + concurrent anonymous replies.

The prompt picker is unit-tested in test_games_ffa_logic.py; this exercises
the interactive glue that nothing else covers:

* Next posts the next prompt as a NEW channel message and leaves every earlier
  prompt fully interactive (buttons kept, no in-place edit).
* Replies to different prompts keep independent reply-count footers and
  reference the correct message.
* Recovery rebuilds one view per posted prompt after a restart.
"""

import json

import bot_modules.cogs.games_ffa_cog as ffa_mod
from bot_modules.cogs.games_ffa_cog import FFACog, FFAEmbedReplyModal
from bot_modules.services.games_db import GamesDb


# ── Fakes ────────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self):
        self.deferred = False
        self.messages: list[tuple] = []

    async def defer(self, *args, **kwargs):
        self.deferred = True

    async def send_message(self, content=None, **kwargs):
        self.messages.append((content, kwargs))


class FakeFollowup:
    def __init__(self):
        self.messages: list[tuple] = []

    async def send(self, content=None, **kwargs):
        self.messages.append((content, kwargs))


class FakeMessage:
    def __init__(self, mid: int, channel):
        self.id = mid
        self.channel = channel
        self.guild = channel.guild
        self.edits: list[dict] = []
        self.embeds: list = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if "embed" in kwargs:
            self.embeds = [kwargs["embed"]] if kwargs["embed"] is not None else []


class FakeChannel:
    def __init__(self, guild):
        self.id = 4242
        self.name = "games"
        self.guild = guild
        self.calls: list[dict] = []          # every send(): {content, kwargs, msg}
        self._by_id: dict[int, FakeMessage] = {}
        self._next_id = 9000

    async def send(self, content=None, **kwargs):
        self._next_id += 1
        msg = FakeMessage(self._next_id, self)
        if kwargs.get("embed") is not None:
            msg.embeds = [kwargs["embed"]]
        self.calls.append({"content": content, "kwargs": kwargs, "msg": msg})
        self._by_id[msg.id] = msg
        return msg

    async def fetch_message(self, mid):
        msg = self._by_id.get(int(mid))
        if msg is None:
            raise Exception("message deleted")
        return msg

    # Convenience: the prompt (embed) messages posted so far, in order.
    def prompt_messages(self):
        return [c["msg"] for c in self.calls if c["kwargs"].get("embed") is not None]

    # Convenience: reply sends (posted with a MessageReference), in order.
    def reply_calls(self):
        return [c for c in self.calls if c["kwargs"].get("reference") is not None]


class FakeCtx:
    db_path = "unused-branding-db"


class FakeBot:
    def __init__(self, db: GamesDb):
        self.games_db = db
        self.active_views: dict = {}
        self.game_launchers: dict = {}
        self.added_views: list[tuple] = []
        self.ctx = FakeCtx()

    def add_view(self, view, *, message_id=None):
        self.added_views.append((view, message_id))


class FakeUser:
    def __init__(self, uid: int, name: str):
        self.id = uid
        self.display_name = name


class FakeGuild:
    def __init__(self):
        self.id = 77
        self.icon = None


class FakeInteraction:
    def __init__(self, user, channel, guild, client):
        self.user = user
        self.channel = channel
        self.channel_id = channel.id
        self.guild = guild
        self.client = client
        self.response = FakeResponse()
        self.followup = FakeFollowup()


# ── Helpers ──────────────────────────────────────────────────────────


def _stub_next_prompt(monkeypatch, label="DARE", text="Second prompt."):
    async def _fake_pick(_db, **_kwargs):
        return (label, text)

    monkeypatch.setattr(ffa_mod, "get_ffa_prompt", _fake_pick)


def _stub_replies(monkeypatch):
    """Keep the anonymous-reply flow off the confessions DB and the audit log."""
    monkeypatch.setattr(ffa_mod, "get_or_assign_anon_identity", lambda *a, **k: (0, 0))
    monkeypatch.setattr(ffa_mod, "get_ephemeral_anon_identity", lambda *a, **k: (1, 1))

    async def _noop_audit(*_a, **_k):
        return None

    monkeypatch.setattr(ffa_mod, "send_audit_log", _noop_audit)


async def _launch(cog, bot, channel, guild, host):
    return await cog.launch(
        channel=channel, host_id=host.id, host_name=host.display_name,
        guild_id=guild.id, options={"kind": "truth", "tags": [], "prompt": "First prompt."},
    )


async def _reply(view, user, channel, guild, bot):
    modal = FFAEmbedReplyModal(view, ephemeral_identity=False)
    modal.answer._value = "an anonymous reply"
    inter = FakeInteraction(user, channel, guild, bot)
    await modal.on_submit(inter)  # type: ignore[arg-type]
    return inter


async def _row(db, game_id):
    row = await db.fetchone(
        "SELECT * FROM games_active_games WHERE game_id = ?", (game_id,)
    )
    assert row is not None
    return row


async def _payload(db, game_id):
    return json.loads((await _row(db, game_id))["payload"])


def _footer_text(msg: FakeMessage) -> str:
    return msg.embeds[-1].footer.text if msg.embeds else ""


# ── Tests ────────────────────────────────────────────────────────────


async def test_next_posts_new_message_keeps_old_interactive(sync_db_path, monkeypatch):
    db = GamesDb(sync_db_path)
    bot = FakeBot(db)
    cog = FFACog(bot)  # type: ignore[arg-type]

    guild = FakeGuild()
    channel = FakeChannel(guild)
    host = FakeUser(1, "Host")
    _stub_next_prompt(monkeypatch)

    game_id = await _launch(cog, bot, channel, guild, host)
    assert game_id is not None
    view_a = bot.active_views[game_id]
    msg_a = view_a._game_msg
    assert len(channel.prompt_messages()) == 1

    inter = FakeInteraction(host, channel, guild, bot)
    await view_a.next_prompt.callback(inter)

    # A brand-new prompt message was posted; the first was NOT edited/stripped.
    prompts = channel.prompt_messages()
    assert len(prompts) == 2
    msg_b = prompts[-1]
    assert msg_b.id != msg_a.id
    assert msg_a.edits == [], "the earlier prompt's buttons were disturbed"
    assert not view_a.is_finished(), "the earlier prompt's view was stopped"

    # New view is registered against the new message; active view tracks latest.
    view_b = bot.active_views[game_id]
    assert view_b is not view_a
    assert view_b._game_msg is msg_b
    assert (view_b, msg_b.id) in bot.added_views

    # Payload records both prompts; the DB anchor stays on the launch message.
    row = await _row(db, game_id)
    assert row["message_id"] == msg_a.id
    payload = json.loads(row["payload"])
    assert [e["message_id"] for e in payload["prompts"]] == [msg_a.id, msg_b.id]
    assert [e["prompt"] for e in payload["prompts"]] == ["First prompt.", "Second prompt."]
    assert payload["seen"] == ["First prompt.", "Second prompt."]


async def test_concurrent_replies_have_independent_counts(sync_db_path, monkeypatch):
    db = GamesDb(sync_db_path)
    bot = FakeBot(db)
    cog = FFACog(bot)  # type: ignore[arg-type]

    guild = FakeGuild()
    channel = FakeChannel(guild)
    host = FakeUser(1, "Host")
    _stub_next_prompt(monkeypatch)
    _stub_replies(monkeypatch)

    game_id = await _launch(cog, bot, channel, guild, host)
    view_a = bot.active_views[game_id]
    msg_a = view_a._game_msg

    await view_a.next_prompt.callback(FakeInteraction(host, channel, guild, bot))
    view_b = bot.active_views[game_id]
    msg_b = view_b._game_msg

    # Two replies to the OLD prompt, one to the NEW prompt — interleaved.
    await _reply(view_a, FakeUser(2, "R1"), channel, guild, bot)
    await _reply(view_b, FakeUser(3, "R2"), channel, guild, bot)
    await _reply(view_a, FakeUser(4, "R3"), channel, guild, bot)

    # Each reply was posted referencing its own prompt message.
    refs = [c["kwargs"]["reference"].message_id for c in channel.reply_calls()]
    assert refs.count(msg_a.id) == 2
    assert refs.count(msg_b.id) == 1

    # Per-message counts are independent in the payload...
    payload = await _payload(db, game_id)
    by_id = {e["message_id"]: e["reply_count"] for e in payload["prompts"]}
    assert by_id[msg_a.id] == 2
    assert by_id[msg_b.id] == 1

    # ...and each message's own footer reflects only its own replies.
    assert "2 anonymous replies" in _footer_text(msg_a)
    assert "1 anonymous reply" in _footer_text(msg_b)


async def test_next_rejects_non_host(sync_db_path, monkeypatch):
    db = GamesDb(sync_db_path)
    bot = FakeBot(db)
    cog = FFACog(bot)  # type: ignore[arg-type]

    guild = FakeGuild()
    channel = FakeChannel(guild)
    host = FakeUser(1, "Host")
    rando = FakeUser(2, "Rando")
    _stub_next_prompt(monkeypatch)

    game_id = await _launch(cog, bot, channel, guild, host)
    view = bot.active_views[game_id]

    inter = FakeInteraction(rando, channel, guild, bot)
    await view.next_prompt.callback(inter)

    assert len(channel.prompt_messages()) == 1  # no new prompt
    assert inter.response.messages, "non-host got no rejection message"
    assert inter.response.messages[-1][1].get("ephemeral") is True


async def test_recover_rebuilds_view_per_prompt(sync_db_path, monkeypatch):
    db = GamesDb(sync_db_path)
    bot = FakeBot(db)
    cog = FFACog(bot)  # type: ignore[arg-type]

    guild = FakeGuild()
    channel = FakeChannel(guild)
    host = FakeUser(1, "Host")
    _stub_next_prompt(monkeypatch)

    game_id = await _launch(cog, bot, channel, guild, host)
    await bot.active_views[game_id].next_prompt.callback(
        FakeInteraction(host, channel, guild, bot)
    )
    row = await _row(db, game_id)
    payload = json.loads(row["payload"])
    assert len(payload["prompts"]) == 2

    # Simulate a restart: fresh bot/cog, same Discord-side channel + messages.
    bot2 = FakeBot(db)
    cog2 = FFACog(bot2)  # type: ignore[arg-type]
    anchor = channel._by_id[row["message_id"]]
    ok = await cog2.recover_game(row, payload, channel, anchor)

    assert ok is True
    recovered_ids = {mid for (_v, mid) in bot2.added_views}
    assert recovered_ids == {e["message_id"] for e in payload["prompts"]}
    assert game_id in bot2.active_views


async def test_recover_migrates_legacy_single_prompt(sync_db_path, monkeypatch):
    """A game from before per-message prompts existed recovers its one message
    and gets a synthesized ``prompts`` entry persisted forward."""
    from bot_modules.games.utils.game_manager import create_game, update_game_message

    db = GamesDb(sync_db_path)
    bot = FakeBot(db)
    cog = FFACog(bot)  # type: ignore[arg-type]

    guild = FakeGuild()
    channel = FakeChannel(guild)
    msg = await channel.send(content="legacy prompt")  # stand-in anchor message

    game_id = await create_game(
        db, channel.id, 1, "ffa", state="open",
        payload={  # legacy shape: no "prompts", a global reply_count
            "prompt": "Legacy prompt.", "label": "TRUTH", "kind": "truth",
            "tags": [], "mode": "embed", "reply_count": 3, "seen": ["Legacy prompt."],
        },
    )
    await update_game_message(db, game_id, msg.id)

    row = await _row(db, game_id)
    payload = json.loads(row["payload"])
    ok = await cog.recover_game(row, payload, channel, msg)

    assert ok is True
    assert bot.added_views[-1][1] == msg.id
    migrated = await _payload(db, game_id)
    assert migrated["prompts"] == [
        {"message_id": msg.id, "prompt": "Legacy prompt.", "label": "TRUTH", "reply_count": 3}
    ]
