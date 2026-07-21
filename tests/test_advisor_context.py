"""Tests for Billy-bot's per-asker server context — especially the privacy gate."""

from __future__ import annotations

import contextlib

from unittest.mock import AsyncMock

from bot_modules.services import advisor_context as ac


# ── fakes ───────────────────────────────────────────────────────────────────


class FakePerms:
    def __init__(self, view: bool) -> None:
        self.view_channel = view


class FakeRole:
    def __init__(self, name: str = "@everyone", is_default: bool = True) -> None:
        self.name = name
        self.is_default = is_default


class FakeChannel:
    """`public` = visible to @everyone; `allowed` = extra member ids that can view."""

    def __init__(self, cid, name, topic="", nsfw=False, public=True, allowed=()):
        self.id = cid
        self.name = name
        self.topic = topic
        self._nsfw = nsfw
        self._public = public
        self._allowed = set(allowed)

    def is_nsfw(self) -> bool:
        return self._nsfw

    def permissions_for(self, viewer):
        if getattr(viewer, "is_default", False):
            return FakePerms(self._public)
        return FakePerms(self._public or getattr(viewer, "id", None) in self._allowed)


class FakeGuild:
    def __init__(self, gid, channels, default_role=None):
        self.id = gid
        self.text_channels = channels
        self.default_role = default_role or FakeRole()


class FakeGuildPerms:
    _FLAGS = (
        "administrator", "manage_guild", "manage_roles", "manage_channels",
        "manage_messages", "moderate_members", "kick_members", "ban_members",
    )

    def __init__(self, **kw):
        for f in self._FLAGS:
            setattr(self, f, kw.get(f, False))


class FakeMember:
    def __init__(self, mid, display_name="Alice", perms=None, role_names=()):
        self.id = mid
        self.display_name = display_name
        self.guild_permissions = perms or FakeGuildPerms()
        self.roles = [FakeRole(name=n, is_default=False) for n in ("@everyone", *role_names)]


def _patch_db(monkeypatch, docs=None, anns=None):
    @contextlib.contextmanager
    def fake_open_db(_db_path):
        yield object()

    monkeypatch.setattr(ac, "open_db", fake_open_db)
    monkeypatch.setattr(ac, "list_docs", lambda conn, gid: list(docs or []))
    monkeypatch.setattr(ac, "list_announcements", lambda conn, gid: list(anns or []))


# ── can_view (the gate) ─────────────────────────────────────────────────────


def test_can_view_excludes_nsfw_even_if_public():
    ch = FakeChannel(1, "adult", public=True, nsfw=True)
    assert ac.can_view(ch, FakeRole()) is False


def test_can_view_public_channel_visible_to_everyone():
    ch = FakeChannel(1, "general", public=True)
    assert ac.can_view(ch, FakeRole()) is True


def test_can_view_private_channel_hidden_then_granted():
    ch = FakeChannel(1, "staff", public=False, allowed={99})
    outsider = FakeMember(7)
    insider = FakeMember(99)
    assert ac.can_view(ch, outsider) is False
    assert ac.can_view(ch, insider) is True
    # @everyone can never see a non-public channel.
    assert ac.can_view(ch, FakeRole()) is False


def test_can_view_fails_closed_on_error():
    class Boom:
        def is_nsfw(self):
            raise RuntimeError("boom")

    assert ac.can_view(Boom(), FakeRole()) is False


# ── capability_summary (what they can do) ───────────────────────────────────


def test_capability_summary_admin():
    m = FakeMember(1, "Boss", FakeGuildPerms(administrator=True))
    assert "administrator" in ac.capability_summary(m)


def test_capability_summary_moderator_lists_powers():
    m = FakeMember(1, "Mod", FakeGuildPerms(manage_messages=True, moderate_members=True))
    s = ac.capability_summary(m)
    assert "moderate messages" in s
    assert "moderator actions" in s


def test_capability_summary_plain_member():
    m = FakeMember(1, "Reg", FakeGuildPerms(), role_names=("Verified",))
    s = ac.capability_summary(m)
    assert "regular member" in s
    assert "Verified" in s  # roles surfaced
    assert "@everyone" not in s  # base role filtered out


def test_capability_summary_none_is_generic_member():
    assert "regular member" in ac.capability_summary(None)


# ── build_asker_context ─────────────────────────────────────────────────────


def _guild_with_mixed_channels():
    public = FakeChannel(10, "general", topic="Chat here", public=True)
    staff = FakeChannel(20, "staff", topic="Mods only", public=False, allowed={99})
    adult = FakeChannel(30, "after-dark", topic="18+ lounge", public=True, nsfw=True)
    return FakeGuild(1, [public, staff, adult]), public, staff, adult


def test_context_scopes_topics_pins_and_announcements_to_visibility(monkeypatch):
    guild, public, staff, adult = _guild_with_mixed_channels()
    ac._pins.clear()
    ac._pins[1] = {10: ["Read the rules"], 20: ["Mod secret"]}
    _patch_db(
        monkeypatch,
        docs=[{"title": "Rules", "doc_key": "rules", "body_md": "Be nice."}],
        anns=[
            {"status": "sent", "sent_channel_id": 10, "channel_id": 10, "title": "Event", "body": "Party Friday"},
            {"status": "sent", "sent_channel_id": 20, "channel_id": 20, "title": "Staff note", "body": "Mods meet"},
            {"status": "draft", "sent_channel_id": 10, "channel_id": 10, "title": "WIP", "body": "unsent"},
        ],
    )
    member = FakeMember(7, "Reg")  # cannot see staff (id 7 not in {99})

    ctx = ac.build_asker_context(guild, member, "db")

    # Visible public content is present…
    assert "#general — Chat here" in ctx
    assert "Read the rules" in ctx
    assert "Party Friday" in ctx
    assert "Be nice." in ctx  # docs always included
    # …mod-only + NSFW + unsent content is NOT.
    assert "Mods only" not in ctx
    assert "Mod secret" not in ctx
    assert "Mods meet" not in ctx
    assert "after-dark" not in ctx
    assert "18+ lounge" not in ctx
    assert "unsent" not in ctx


def test_context_insider_sees_staff_channel(monkeypatch):
    guild, *_ = _guild_with_mixed_channels()
    ac._pins.clear()
    ac._pins[1] = {20: ["Mod secret"]}
    _patch_db(monkeypatch)
    insider = FakeMember(99, "Mod", FakeGuildPerms(manage_messages=True))

    ctx = ac.build_asker_context(guild, insider, "db")
    assert "Mods only" in ctx
    assert "Mod secret" in ctx


def test_context_none_viewer_falls_back_to_public(monkeypatch):
    guild, *_ = _guild_with_mixed_channels()
    ac._pins.clear()
    _patch_db(monkeypatch)
    ctx = ac.build_asker_context(guild, None, "db")
    assert "#general" in ctx
    assert "staff" not in ctx  # @everyone can't see it


def test_context_respects_hard_cap(monkeypatch):
    guild = FakeGuild(1, [FakeChannel(10, "general", topic="x" * 500)])
    ac._pins.clear()
    _patch_db(monkeypatch)
    monkeypatch.setattr(ac, "MAX_CONTEXT_CHARS", 100)
    ctx = ac.build_asker_context(guild, None, "db")
    assert len(ctx) <= 100


# ── refresh_guild_pins ──────────────────────────────────────────────────────


class FakeMessage:
    def __init__(self, content="", embeds=None):
        self.content = content
        self.embeds = embeds or []


async def test_refresh_guild_pins_skips_nsfw_and_caps(monkeypatch):
    good = FakeChannel(10, "general", public=True)
    good.pins = AsyncMock(return_value=[FakeMessage(f"pin {i}") for i in range(10)])
    nsfw = FakeChannel(30, "after-dark", public=True, nsfw=True)
    nsfw.pins = AsyncMock(return_value=[FakeMessage("adult pin")])
    guild = FakeGuild(1, [good, nsfw])
    ac._pins.clear()

    result = await ac.refresh_guild_pins(guild)
    assert 30 not in result  # nsfw never snapshotted
    nsfw.pins.assert_not_called()
    assert len(result[10]) == ac.MAX_PINS_PER_CHANNEL  # capped
    assert ac._pins[1] == result
