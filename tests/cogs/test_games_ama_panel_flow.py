"""End-to-end panel-format AMA flow driven through fake Discord objects.

The pure helpers and embed builders are unit-tested elsewhere; this
module exercises the interactive glue that nothing else touches:
Volunteer -> _begin_ask -> AskTargetSelect.callback -> modal on_submit.
It uses lightweight fakes plus stubbed branding/audit so the flow runs
without a live gateway or the branding DB.
"""

import json
from unittest.mock import MagicMock

import discord
import pytest

import bot_modules.cogs.games_ama_cog as ama_mod
from bot_modules.cogs.games_ama_cog import AMACog, AskQuestionModal, AskTargetSelect
from bot_modules.services.games_db import GamesDb


# ── Fakes ────────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self):
        self.messages: list[tuple] = []
        self.modals: list = []
        self.deferred = False

    async def send_message(self, content=None, **kwargs):
        self.messages.append((content, kwargs))

    async def send_modal(self, modal):
        self.modals.append(modal)

    async def defer(self, *args, **kwargs):
        self.deferred = True


class FakeMessage:
    def __init__(self, mid: int, guild):
        self.id = mid
        self.guild = guild
        self.jump_url = "http://discord/x"
        self.edits: list[dict] = []
        self.embeds: list = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if kwargs.get("embed") is not None:
            self.embeds = [kwargs["embed"]]

    async def delete(self):
        pass


def FakeMember(uid: int, name: str):
    # A spec'd mock so production's `isinstance(user, discord.Member)` guards
    # pass, while still letting us pin id/display_name/mention.
    m = MagicMock(spec=discord.Member)
    m.id = uid
    m.display_name = name
    m.mention = f"<@{uid}>"
    return m


class FakeGuild:
    def __init__(self, members):
        self.id = 900
        self._members = {m.id: m for m in members}
        self.roles: list = []
        self.me = None

    def get_member(self, uid):
        return self._members.get(uid)


class FakeChannel:
    def __init__(self, guild):
        self.id = 4242
        self.name = "games"
        self.guild = guild
        self.mention = "#games"
        self.sends: list[tuple] = []
        self._next_id = 9000

    async def send(self, content=None, **kwargs):
        self._next_id += 1
        msg = FakeMessage(self._next_id, self.guild)
        self.sends.append((content, kwargs, msg))
        return msg


class FakeCtx:
    db_path = "unused-branding-db"


class FakeBot:
    def __init__(self, db: GamesDb):
        self.games_db = db
        self.active_views: dict = {}
        self.game_launchers: dict = {}
        self.ctx = FakeCtx()

    def get_cog(self, name):
        return None


class FakeInteraction:
    def __init__(self, user, channel, guild, client):
        self.user = user
        self.channel = channel
        self.guild = guild
        self.client = client
        self.message = None
        self.response = FakeResponse()


@pytest.fixture
def stub_branding(monkeypatch):
    async def _accent(_db_path, _guild):
        return discord.Colour(0x5865F2)

    async def _audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ama_mod, "resolve_accent_color", _accent)
    monkeypatch.setattr(ama_mod, "send_audit_log", _audit)


async def test_panel_flow_volunteer_ask_and_post(sync_db_path, stub_branding):
    db = GamesDb(sync_db_path)
    bot = FakeBot(db)
    cog = AMACog(bot)  # type: ignore[arg-type]

    host = FakeMember(1, "Host")
    panelist = FakeMember(2, "Panelist")
    asker = FakeMember(3, "Asker")
    guild = FakeGuild([host, panelist, asker])
    channel = FakeChannel(guild)

    game_id = await cog.launch(
        channel=channel, host_id=host.id, host_name=host.display_name,
        guild_id=99, options={"mode": "unfiltered", "format": "panel"},
    )
    assert game_id is not None
    view = bot.active_views[game_id]

    # 1. Panelist volunteers -> joins the panel roster.
    vol_inter = FakeInteraction(panelist, channel, guild, bot)
    await view._handle_volunteer(vol_inter)
    assert view.panel == [panelist.id]

    # 2. Asker opens the ask flow -> gets a dropdown of panelists.
    ask_inter = FakeInteraction(asker, channel, guild, bot)
    await view._begin_ask(ask_inter)
    assert ask_inter.response.messages, "ask flow posted nothing"
    ask_view = ask_inter.response.messages[-1][1]["view"]
    select = next(c for c in ask_view.children if isinstance(c, AskTargetSelect))
    assert [o.value for o in select.options] == [str(panelist.id)]

    # 3. Asker picks the panelist from the dropdown -> a question modal opens.
    select._values = [str(panelist.id)]  # discord fills this from the submitted interaction
    pick_inter = FakeInteraction(asker, channel, guild, bot)
    await select.callback(pick_inter)
    assert pick_inter.response.modals, "selecting a panelist opened no modal"
    modal = pick_inter.response.modals[-1]
    assert isinstance(modal, AskQuestionModal)
    assert modal.target_id == panelist.id

    # 4. Asker submits the question -> it posts to the channel, aimed at the panelist.
    modal.question._value = "What's your favorite color?"
    submit_inter = FakeInteraction(asker, channel, guild, bot)
    await modal.on_submit(submit_inter)

    # A question message went to the channel mentioning the panelist.
    posted = [s for s in channel.sends if s[0] == panelist.mention]
    assert posted, "no question message aimed at the panelist"
    _, kwargs, _ = posted[-1]
    assert "embed" in kwargs and "view" in kwargs

    # Payload records the question, directed at the panelist, marked approved.
    row = await db.fetchone(
        "SELECT payload FROM games_active_games WHERE game_id = ?", (game_id,)
    )
    payload = json.loads(row["payload"])
    questions = payload["questions"]
    assert len(questions) == 1
    assert questions[0]["hot_seat_id"] == panelist.id
    assert questions[0]["asker_id"] == asker.id
    assert questions[0]["status"] == "approved"


async def test_panel_ask_rejected_when_target_left(sync_db_path, stub_branding):
    # A panelist who leaves between dropdown-open and submit can't be asked.
    db = GamesDb(sync_db_path)
    bot = FakeBot(db)
    cog = AMACog(bot)  # type: ignore[arg-type]

    host = FakeMember(1, "Host")
    panelist = FakeMember(2, "Panelist")
    asker = FakeMember(3, "Asker")
    guild = FakeGuild([host, panelist, asker])
    channel = FakeChannel(guild)

    game_id = await cog.launch(
        channel=channel, host_id=host.id, host_name=host.display_name,
        guild_id=99, options={"mode": "unfiltered", "format": "panel"},
    )
    view = bot.active_views[game_id]

    await view._handle_volunteer(FakeInteraction(panelist, channel, guild, bot))
    # Panelist leaves the panel (second tap).
    await view._handle_volunteer(FakeInteraction(panelist, channel, guild, bot))
    assert view.panel == []

    # A stale modal captured the panelist as target; submitting must be rejected
    # and post nothing.
    modal = AskQuestionModal(game_id, db, channel, "unfiltered", host.id, panelist.id, view)
    modal.question._value = "late question"
    submit_inter = FakeInteraction(asker, channel, guild, bot)
    await modal.on_submit(submit_inter)

    assert submit_inter.response.messages, "expected a rejection message"
    reject_text = submit_inter.response.messages[-1][0]
    assert "left the panel" in reject_text
    assert not any(s[0] == panelist.mention for s in channel.sends)
