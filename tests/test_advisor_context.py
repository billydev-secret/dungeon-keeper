"""Tests for Billy-bot's per-asker server context — especially the privacy gate."""

from __future__ import annotations

import contextlib
import dataclasses
import sqlite3

from unittest.mock import AsyncMock

from bot_modules.services import advisor_context as ac


# ── fakes ───────────────────────────────────────────────────────────────────


class FakePerms:
    def __init__(self, view: bool) -> None:
        self.view_channel = view


class FakeRole:
    def __init__(self, name: str = "@everyone", is_default: bool = True, rid: int = 0) -> None:
        self.name = name
        self.is_default = is_default
        self.id = rid


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
    def __init__(self, gid, channels, default_role=None, roles=None):
        self.id = gid
        self.text_channels = channels
        self.default_role = default_role or FakeRole()
        self._by_id = {c.id: c for c in channels}
        self._roles = {r.id: r for r in (roles or [])}

    def get_channel(self, cid):
        return self._by_id.get(cid)

    def get_role(self, rid):
        return self._roles.get(rid)


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


# ── is_staff (which model tier answers) ─────────────────────────────────────


def test_is_staff_none_and_plain_member():
    assert ac.is_staff(None) is False
    assert ac.is_staff(FakeMember(1, "Reg", FakeGuildPerms())) is False
    # A named role alone doesn't make someone staff — permissions do.
    assert ac.is_staff(FakeMember(1, "Reg", FakeGuildPerms(), role_names=("Mod",))) is False


def test_is_staff_true_for_each_staff_permission():
    for flag in (
        "administrator",
        "manage_guild",
        "manage_messages",
        "moderate_members",
        "kick_members",
        "ban_members",
    ):
        m = FakeMember(1, "Staff", FakeGuildPerms(**{flag: True}))
        assert ac.is_staff(m) is True, flag


def test_is_server_admin_is_stricter_than_can_see_config():
    """Manage Server sees settings; only administrator gets the admin_only tier."""
    manage = FakeMember(1, "Manager", FakeGuildPerms(manage_guild=True))
    assert ac.can_see_config(manage) is True
    assert ac.is_server_admin(manage) is False

    admin = FakeMember(2, "Boss", FakeGuildPerms(administrator=True))
    assert ac.can_see_config(admin) is True
    assert ac.is_server_admin(admin) is True

    assert ac.is_server_admin(None) is False
    assert ac.is_server_admin(FakeMember(3, "Mod", FakeGuildPerms(manage_messages=True))) is False


def test_is_staff_is_wider_than_can_see_config():
    """A message-moderating mod gets the better model but not settings access."""
    mod = FakeMember(1, "Mod", FakeGuildPerms(manage_messages=True))
    assert ac.is_staff(mod) is True
    assert ac.can_see_config(mod) is False


def test_is_staff_ignores_non_staff_manage_permissions():
    """manage_roles/manage_channels aren't in the staff set — keep it deliberate."""
    m = FakeMember(1, "Decorator", FakeGuildPerms(manage_roles=True, manage_channels=True))
    assert ac.is_staff(m) is False


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

    # Visible public content is present, with a clickable <#id> mention…
    assert "Chat here" in ctx
    assert "<#10>" in ctx
    assert "Read the rules" in ctx
    assert "Party Friday" in ctx
    assert "Be nice." in ctx  # docs always included
    # …mod-only + NSFW + unsent content is NOT (not even the channel id).
    assert "Mods only" not in ctx
    assert "<#20>" not in ctx
    assert "Mod secret" not in ctx
    assert "Mods meet" not in ctx
    assert "after-dark" not in ctx
    assert "<#30>" not in ctx
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
    assert "<#20>" in ctx
    assert "Mod secret" in ctx


def test_visible_text_channels_filters_by_viewer():
    guild, public, staff, adult = _guild_with_mixed_channels()
    outsider = FakeMember(7)
    insider = FakeMember(99)
    assert [c.id for c in ac.visible_text_channels(guild, outsider)] == [10]
    assert [c.id for c in ac.visible_text_channels(guild, insider)] == [10, 20]
    assert [c.id for c in ac.visible_text_channels(guild, None)] == [10]  # public


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


# ── build_config_summary (admin-only, secret-filtered) ──────────────────────


def _conn_with_config(rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE config (guild_id INTEGER, key TEXT, value TEXT, "
        "PRIMARY KEY(guild_id, key))"
    )
    conn.executemany("INSERT INTO config VALUES (1, ?, ?)", rows)
    return conn


def test_config_summary_admin_only(monkeypatch):
    monkeypatch.setattr(ac, "_FEATURE_LOADERS", [])  # isolate the KV section
    conn = _conn_with_config([("welcome_enabled", "1")])
    guild = FakeGuild(1, [])
    admin = FakeMember(1, perms=FakeGuildPerms(administrator=True))
    member = FakeMember(2, perms=FakeGuildPerms())
    assert "welcome_enabled = on" in ac.build_config_summary(conn, guild, admin)
    assert ac.build_config_summary(conn, guild, member) == ""  # not an admin
    assert ac.build_config_summary(conn, guild, None) == ""


def test_config_summary_manage_guild_also_allowed(monkeypatch):
    monkeypatch.setattr(ac, "_FEATURE_LOADERS", [])
    conn = _conn_with_config([("xp_enabled", "0")])
    guild = FakeGuild(1, [])
    manager = FakeMember(1, perms=FakeGuildPerms(manage_guild=True))
    assert "xp_enabled = off" in ac.build_config_summary(conn, guild, manager)


def test_config_summary_filters_secrets_and_resolves_ids(monkeypatch):
    monkeypatch.setattr(ac, "_FEATURE_LOADERS", [])
    conn = _conn_with_config([
        ("spotify_bot_refresh_token", "supersecret"),
        ("welcome_channel_id", "10"),
        ("greeter_role_id", "55"),
        ("xp_enabled", "1"),
        ("some_prompt", "x" * 500),
    ])
    guild = FakeGuild(
        1,
        [FakeChannel(10, "welcome")],
        roles=[FakeRole(name="Greeter", is_default=False, rid=55)],
    )
    admin = FakeMember(1, perms=FakeGuildPerms(administrator=True))
    s = ac.build_config_summary(conn, guild, admin)
    assert "supersecret" not in s  # secret value never surfaced
    assert "refresh_token" not in s  # secret key filtered by name
    assert "welcome_channel_id = #welcome" in s  # channel id → name
    assert "greeter_role_id = @Greeter" in s  # role id → name
    assert "xp_enabled = on" in s
    assert "some_prompt" not in s  # over-long value skipped


def test_context_inserts_config_summary_for_admin(monkeypatch):
    guild, *_ = _guild_with_mixed_channels()
    ac._pins.clear()
    _patch_db(monkeypatch)
    monkeypatch.setattr(
        ac, "build_config_summary", lambda conn, g, m, db=None: "CFG: welcome=on"
    )
    admin = FakeMember(99, perms=FakeGuildPerms(administrator=True))
    ctx = ac.build_asker_context(guild, admin, "db")
    assert "CFG: welcome=on" in ctx


# ── feature-loader serialization ────────────────────────────────────────────


@dataclasses.dataclass
class FakeEconSettings:
    manager_role_id: int
    log_channel_id: int
    daily_reward: int
    currency_name: str
    enabled: bool
    api_secret: str  # must be filtered by name


def test_fmt_value_resolves_and_flags():
    guild = FakeGuild(
        1,
        [FakeChannel(10, "logs")],
        roles=[FakeRole(name="Mods", is_default=False, rid=55)],
    )
    assert ac._fmt_value(guild, "log_channel_id", 10) == "#logs"
    assert ac._fmt_value(guild, "manager_role_id", 55) == "@Mods"
    assert ac._fmt_value(guild, "enabled", True) == "on"
    assert ac._fmt_value(guild, "enabled", False) == "off"
    assert ac._fmt_value(guild, "count", 7) == "7"
    assert ac._fmt_value(guild, "fields", frozenset({"a", "b"})) == "2 configured"
    assert ac._fmt_value(guild, "empty", []) is None


def test_to_flat_dict_handles_shapes():
    assert ac._to_flat_dict(None) is None
    assert ac._to_flat_dict({"a": 1}) == {"a": 1}
    assert ac._to_flat_dict([1, 2, 3]) == {"entries": 3}
    assert ac._to_flat_dict([]) is None
    dc = FakeEconSettings(55, 10, 100, "coins", True, "x")
    assert ac._to_flat_dict(dc)["daily_reward"] == 100


def test_feature_section_serializes_dataclass_and_filters_secret():
    guild = FakeGuild(
        1,
        [FakeChannel(10, "logs")],
        roles=[FakeRole(name="Mods", is_default=False, rid=55)],
    )
    cfg = FakeEconSettings(55, 10, 100, "coins", True, "supersecret")
    section = ac._feature_section(guild, "Economy", cfg)
    assert section.startswith("[Economy]")
    assert "manager_role_id = @Mods" in section
    assert "log_channel_id = #logs" in section
    assert "daily_reward = 100" in section
    assert "enabled = on" in section
    assert "supersecret" not in section  # api_secret filtered by key name
    assert "api_secret" not in section


def test_build_config_summary_includes_feature_sections_and_isolates_failures(monkeypatch):
    conn = _conn_with_config([("welcome_enabled", "1")])
    guild = FakeGuild(1, [])
    admin = FakeMember(1, perms=FakeGuildPerms(administrator=True))

    def _boom(conn, gid, db):
        raise RuntimeError("loader exploded")

    monkeypatch.setattr(ac, "_FEATURE_LOADERS", [
        ("Economy", lambda conn, gid, db: FakeEconSettings(0, 0, 250, "gold", True, "x")),
        ("Broken", _boom),  # must be skipped, not crash the whole summary
    ])
    s = ac.build_config_summary(conn, guild, admin, "db")
    assert "[General]" in s and "welcome_enabled = on" in s  # KV still there
    assert "[Economy]" in s and "daily_reward = 250" in s  # feature section added
    assert "Broken" not in s  # failing loader dropped silently


# ── fetch_feature_settings (the get_server_settings tool handler) ───────────


def _patch_settings_db(monkeypatch, conn):
    @contextlib.contextmanager
    def fake_open_db(_db_path):
        yield conn

    monkeypatch.setattr(ac, "open_db", fake_open_db)


def test_feature_keys_cover_general_and_loaders():
    assert "general" in ac.FEATURE_KEYS
    assert "economy" in ac.FEATURE_KEYS
    assert "voice_master" in ac.FEATURE_KEYS
    assert len(ac.FEATURE_KEYS) == len(set(ac.FEATURE_KEYS))


def test_fetch_settings_requires_admin(monkeypatch):
    member = FakeMember(1, perms=FakeGuildPerms())
    out = ac.fetch_feature_settings(FakeGuild(1, []), member, "db", "general")
    assert "only server admins" in out
    out = ac.fetch_feature_settings(FakeGuild(1, []), None, "db", "general")
    assert "only server admins" in out


def test_fetch_settings_general_and_unknown(monkeypatch):
    conn = _conn_with_config([("welcome_enabled", "1")])
    _patch_settings_db(monkeypatch, conn)
    guild = FakeGuild(1, [])
    admin = FakeMember(1, perms=FakeGuildPerms(administrator=True))
    out = ac.fetch_feature_settings(guild, admin, "db", "general")
    assert "welcome_enabled = on" in out
    out = ac.fetch_feature_settings(guild, admin, "db", "flux_capacitor")
    assert "Unknown feature" in out
    assert "general" in out  # lists what IS available


def test_fetch_settings_feature_loader_and_empty(monkeypatch):
    conn = _conn_with_config([])
    _patch_settings_db(monkeypatch, conn)
    monkeypatch.setattr(ac, "_FEATURES_BY_SLUG", {
        "economy": ("Economy", lambda conn, gid, db: FakeEconSettings(0, 0, 250, "gold", True, "x")),
        "starboard": ("Starboard", lambda conn, gid, db: None),
    })
    guild = FakeGuild(1, [])
    admin = FakeMember(1, perms=FakeGuildPerms(administrator=True))
    out = ac.fetch_feature_settings(guild, admin, "db", "economy")
    assert "[Economy]" in out and "daily_reward = 250" in out
    # Unconfigured feature → readable pointer, not an empty string.
    out = ac.fetch_feature_settings(guild, admin, "db", "starboard")
    assert "No saved settings" in out


def test_fetch_settings_loader_error_is_readable(monkeypatch):
    conn = _conn_with_config([])
    _patch_settings_db(monkeypatch, conn)

    def _boom(conn, gid, db):
        raise RuntimeError("loader exploded")

    monkeypatch.setattr(ac, "_FEATURES_BY_SLUG", {"economy": ("Economy", _boom)})
    admin = FakeMember(1, perms=FakeGuildPerms(administrator=True))
    out = ac.fetch_feature_settings(FakeGuild(1, []), admin, "db", "economy")
    assert "Couldn't read" in out


def test_context_include_config_false_skips_settings(monkeypatch):
    guild, *_ = _guild_with_mixed_channels()
    ac._pins.clear()
    _patch_db(monkeypatch)
    monkeypatch.setattr(
        ac, "build_config_summary", lambda conn, g, m, db=None: "CFG: welcome=on"
    )
    admin = FakeMember(99, perms=FakeGuildPerms(administrator=True))
    ctx = ac.build_asker_context(guild, admin, "db", include_config=False)
    assert "CFG" not in ctx  # tools replace the inline dump
    assert "administrator" in ctx  # rest of the context still there


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
