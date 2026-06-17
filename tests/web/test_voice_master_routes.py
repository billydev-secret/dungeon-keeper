"""Tests for /api/voice-master/* admin endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.services.voice_master_service import (
    VoiceProfile,
    add_trusted,
    add_blocked,
    insert_active_channel,
    save_profile,
    set_voice_master_config_value,
)


def _config_payload(**overrides):
    """Build a complete config payload with reasonable defaults."""
    base = {
        "hub_channel_id": "100",
        "category_id": "200",
        "control_channel_id": "300",
        "default_name_template": "{member}'s room",
        "default_user_limit": 5,
        "default_bitrate": 64000,
        "create_cooldown_s": 60,
        "max_per_member": 2,
        "trust_cap": 25,
        "block_cap": 25,
        "owner_grace_s": 30,
        "empty_grace_s": 120,
        "trusted_prune_days": 30,
        "disable_saves": False,
        "saveable_fields": ["name", "limit"],
        "post_inline_panel": True,
    }
    base.update(overrides)
    return base


def _auth_member():
    m = MagicMock()
    m.id = 1
    m.bot = False
    m.guild_permissions = MagicMock(value=0x8)
    m.display_name = "tester"
    default_role = MagicMock(id=0, name="@everyone")
    default_role.is_default = MagicMock(return_value=True)
    m.roles = [default_role]
    return m


def _attach_bot(fake_ctx, *, channels=None, members=None):
    members = (members or []) + [_auth_member()]
    channels = channels or []
    by_member = {m.id: m for m in members}
    by_channel = {c.id: c for c in channels}

    guild = MagicMock()
    guild.id = fake_ctx.guild_id
    guild.get_member = MagicMock(side_effect=lambda uid: by_member.get(int(uid)))
    guild.get_channel = MagicMock(side_effect=lambda cid: by_channel.get(int(cid)))
    guild.members = members

    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    fake_ctx.bot = bot
    return guild


# ── GET /voice-master/config ─────────────────────────────────────────


def test_get_config_returns_defaults_when_nothing_persisted(authed_client):
    resp = authed_client.get("/api/voice-master/config")
    assert resp.status_code == 200
    body = resp.json()
    assert "hub_channel_id" in body
    assert body["name_blocklist"] == []
    assert isinstance(body["saveable_fields"], list)


def test_get_config_returns_persisted_values(authed_client, fake_ctx):
    with open_db(fake_ctx.db_path) as conn:
        set_voice_master_config_value(
            conn, fake_ctx.guild_id, "voice_master_hub_channel_id", "9999"
        )
        set_voice_master_config_value(
            conn, fake_ctx.guild_id, "voice_master_default_name_template", "{nick}'s room"
        )

    body = authed_client.get("/api/voice-master/config").json()
    assert body["hub_channel_id"] == "9999"
    assert body["default_name_template"] == "{nick}'s room"


# ── POST /voice-master/config ────────────────────────────────────────


def test_post_config_rejects_unknown_saveable_fields(authed_client):
    payload = _config_payload(saveable_fields=["name", "bogus"])
    resp = authed_client.post("/api/voice-master/config", json=payload)
    assert resp.status_code == 400
    assert "bogus" in resp.json()["detail"]


def test_post_config_rejects_non_numeric_channel_id(authed_client):
    payload = _config_payload(hub_channel_id="abc")
    resp = authed_client.post("/api/voice-master/config", json=payload)
    assert resp.status_code == 400


def test_post_config_persists_values(authed_client, fake_ctx):
    payload = _config_payload(hub_channel_id="123456", default_user_limit=7)
    resp = authed_client.post("/api/voice-master/config", json=payload)
    assert resp.status_code == 200

    body = authed_client.get("/api/voice-master/config").json()
    assert body["hub_channel_id"] == "123456"
    assert body["default_user_limit"] == 7


def test_post_config_treats_empty_channel_ids_as_zero(authed_client):
    payload = _config_payload(hub_channel_id="", category_id="  ", control_channel_id="")
    resp = authed_client.post("/api/voice-master/config", json=payload)
    assert resp.status_code == 200
    body = authed_client.get("/api/voice-master/config").json()
    assert body["hub_channel_id"] == "0"
    assert body["category_id"] == "0"


# ── POST /voice-master/post-howto ────────────────────────────────────


def test_post_howto_returns_503_when_bot_offline(authed_client, fake_ctx):
    # fake_ctx.bot is None by default → no live bot to send the message.
    resp = authed_client.post(
        "/api/voice-master/post-howto", json={"channel_id": "300"}
    )
    assert resp.status_code == 503


def test_post_howto_rejects_non_text_channel(authed_client, fake_ctx):
    voice = MagicMock(spec=discord.VoiceChannel)
    voice.id = 300
    _attach_bot(fake_ctx, channels=[voice])
    resp = authed_client.post(
        "/api/voice-master/post-howto", json={"channel_id": "300"}
    )
    assert resp.status_code == 400


def test_post_howto_posts_embed_with_hub_mention(authed_client, fake_ctx):
    lobby = MagicMock(spec=discord.TextChannel)
    lobby.id = 300
    sent = MagicMock()
    sent.jump_url = "https://discord.com/channels/1/300/999"
    lobby.send = AsyncMock(return_value=sent)

    with open_db(fake_ctx.db_path) as conn:
        set_voice_master_config_value(
            conn, fake_ctx.guild_id, "voice_master_hub_channel_id", "555"
        )
    _attach_bot(fake_ctx, channels=[lobby])

    resp = authed_client.post(
        "/api/voice-master/post-howto", json={"channel_id": "300"}
    )
    assert resp.status_code == 200
    assert resp.json()["message_url"] == sent.jump_url
    lobby.send.assert_awaited_once()
    embed = lobby.send.await_args.kwargs["embed"]
    assert "Voice Channel" in embed.title
    assert "<#555>" in embed.description  # configured Hub is mentioned


# ── name blocklist ───────────────────────────────────────────────────


def test_add_blocklist_rejects_empty_pattern(authed_client):
    resp = authed_client.post("/api/voice-master/name-blocklist", json={"pattern": "  "})
    assert resp.status_code == 400


def test_add_blocklist_persists_pattern(authed_client):
    resp = authed_client.post(
        "/api/voice-master/name-blocklist", json={"pattern": "BadName"}
    )
    assert resp.status_code == 200
    assert resp.json()["added"] is True
    assert resp.json()["pattern"] == "badname"  # lowercased

    listed = authed_client.get("/api/voice-master/config").json()
    assert "badname" in listed["name_blocklist"]


def test_add_blocklist_dedupes(authed_client):
    authed_client.post("/api/voice-master/name-blocklist", json={"pattern": "badname"})
    resp = authed_client.post(
        "/api/voice-master/name-blocklist", json={"pattern": "BADNAME"}
    )
    # already present → added=False
    assert resp.json()["added"] is False


def test_remove_blocklist_returns_removed_flag(authed_client):
    authed_client.post("/api/voice-master/name-blocklist", json={"pattern": "foo"})
    resp = authed_client.delete("/api/voice-master/name-blocklist/foo")
    assert resp.json()["removed"] is True

    # Second delete is a no-op.
    resp2 = authed_client.delete("/api/voice-master/name-blocklist/foo")
    assert resp2.json()["removed"] is False


# ── GET /voice-master/channels ───────────────────────────────────────


def test_list_channels_returns_empty(authed_client, fake_ctx):
    _attach_bot(fake_ctx)
    body = authed_client.get("/api/voice-master/channels").json()
    assert body == {"channels": []}


def test_list_channels_marks_deleted_channels(authed_client, fake_ctx):
    """An active-channel row with no matching guild channel renders as '(deleted)'."""
    _attach_bot(fake_ctx, channels=[], members=[])  # no live channel for id=5001

    with open_db(fake_ctx.db_path) as conn:
        insert_active_channel(
            conn,
            channel_id=5001,
            guild_id=fake_ctx.guild_id,
            owner_id=42,
            now=100.0,
        )

    body = authed_client.get("/api/voice-master/channels").json()
    assert len(body["channels"]) == 1
    assert body["channels"][0]["channel_name"] == "(deleted)"
    assert body["channels"][0]["owner_id"] == 42


def test_list_channels_counts_non_bot_members(authed_client, fake_ctx):
    voice = MagicMock(spec=discord.VoiceChannel)
    voice.id = 5001
    voice.name = "Chat Room"

    human = MagicMock()
    human.bot = False
    bot_member = MagicMock()
    bot_member.bot = True
    voice.members = [human, human, bot_member]

    owner = _auth_member()
    owner.id = 42
    # owner is currently in the channel
    owner.voice = MagicMock()
    owner.voice.channel = voice

    _attach_bot(fake_ctx, channels=[voice], members=[owner])

    with open_db(fake_ctx.db_path) as conn:
        insert_active_channel(
            conn,
            channel_id=5001,
            guild_id=fake_ctx.guild_id,
            owner_id=42,
            now=100.0,
        )

    body = authed_client.get("/api/voice-master/channels").json()
    row = body["channels"][0]
    assert row["channel_name"] == "Chat Room"
    assert row["members_count"] == 2  # bot excluded
    assert row["owner_in_channel"] is True


# ── POST /voice-master/channels/{id}/force-delete ────────────────────


def test_force_delete_returns_404_for_unknown_channel(authed_client, fake_ctx):
    _attach_bot(fake_ctx)
    resp = authed_client.post("/api/voice-master/channels/99999/force-delete")
    assert resp.status_code == 404


def test_force_delete_cleans_db_when_channel_already_gone(authed_client, fake_ctx):
    """If the channel row exists but Discord has no matching voice channel,
    the row is just deleted."""
    _attach_bot(fake_ctx)
    with open_db(fake_ctx.db_path) as conn:
        insert_active_channel(
            conn,
            channel_id=5001,
            guild_id=fake_ctx.guild_id,
            owner_id=42,
            now=100.0,
        )

    resp = authed_client.post("/api/voice-master/channels/5001/force-delete")
    assert resp.status_code == 200
    assert "already deleted" in resp.json()["note"]

    body = authed_client.get("/api/voice-master/channels").json()
    assert body["channels"] == []


def test_force_delete_invokes_channel_delete_and_clears_db(authed_client, fake_ctx):
    voice = MagicMock(spec=discord.VoiceChannel)
    voice.id = 5001
    voice.name = "Room"
    voice.members = []
    voice.delete = AsyncMock()

    _attach_bot(fake_ctx, channels=[voice])
    with open_db(fake_ctx.db_path) as conn:
        insert_active_channel(
            conn,
            channel_id=5001,
            guild_id=fake_ctx.guild_id,
            owner_id=42,
            now=100.0,
        )

    resp = authed_client.post("/api/voice-master/channels/5001/force-delete")
    assert resp.status_code == 200
    voice.delete.assert_awaited_once()
    # DB row is gone
    body = authed_client.get("/api/voice-master/channels").json()
    assert body["channels"] == []


def test_force_delete_mirrors_to_mod_log(authed_client, fake_ctx):
    """Web force-delete posts the same mod-log mirror embed the retired
    /voice-admin command did."""
    voice = MagicMock(spec=discord.VoiceChannel)
    voice.id = 5001
    voice.name = "Room"
    voice.members = []
    voice.delete = AsyncMock()

    mod_log = MagicMock(spec=discord.TextChannel)
    mod_log.id = 7777
    mod_log.send = AsyncMock()

    with open_db(fake_ctx.db_path) as conn:
        set_config_value(conn, "mod_channel_id", "7777", fake_ctx.guild_id)
        insert_active_channel(
            conn,
            channel_id=5001,
            guild_id=fake_ctx.guild_id,
            owner_id=42,
            now=100.0,
        )

    _attach_bot(fake_ctx, channels=[voice, mod_log])

    resp = authed_client.post("/api/voice-master/channels/5001/force-delete")
    assert resp.status_code == 200
    mod_log.send.assert_awaited_once()
    embed = mod_log.send.await_args.kwargs["embed"]
    assert "force-delete" in embed.title
    assert "Room" in embed.description


# ── POST /voice-master/channels/{id}/force-transfer ──────────────────


def test_force_transfer_rejects_unknown_new_owner(authed_client, fake_ctx):
    _attach_bot(fake_ctx)
    resp = authed_client.post(
        "/api/voice-master/channels/5001/force-transfer",
        json={"new_owner_id": 99999},
    )
    assert resp.status_code == 404


def test_force_transfer_rejects_bot_new_owner(authed_client, fake_ctx):
    bot_member = MagicMock()
    bot_member.id = 7000
    bot_member.bot = True
    _attach_bot(fake_ctx, members=[bot_member])

    resp = authed_client.post(
        "/api/voice-master/channels/5001/force-transfer",
        json={"new_owner_id": 7000},
    )
    assert resp.status_code == 400


def test_force_transfer_rejects_when_channel_missing(authed_client, fake_ctx):
    new_owner = _auth_member()
    new_owner.id = 200
    _attach_bot(fake_ctx, members=[new_owner])

    resp = authed_client.post(
        "/api/voice-master/channels/5001/force-transfer",
        json={"new_owner_id": 200},
    )
    assert resp.status_code == 404


def test_force_transfer_updates_owner(authed_client, fake_ctx):
    new_owner = _auth_member()
    new_owner.id = 200

    voice = MagicMock(spec=discord.VoiceChannel)
    voice.id = 5001
    voice.name = "Room"
    voice.overwrites_for = MagicMock(return_value=MagicMock(connect=False, view_channel=False))
    voice.set_permissions = AsyncMock()

    _attach_bot(fake_ctx, channels=[voice], members=[new_owner])
    with open_db(fake_ctx.db_path) as conn:
        insert_active_channel(
            conn,
            channel_id=5001,
            guild_id=fake_ctx.guild_id,
            owner_id=42,
            now=100.0,
        )

    resp = authed_client.post(
        "/api/voice-master/channels/5001/force-transfer",
        json={"new_owner_id": 200},
    )
    assert resp.status_code == 200
    voice.set_permissions.assert_awaited_once()


# ── GET /voice-master/profiles/{user_id} ─────────────────────────────


def test_get_profile_returns_none_when_unknown(authed_client):
    body = authed_client.get("/api/voice-master/profiles/999").json()
    assert body["user_id"] == 999
    assert body["profile"] is None
    assert body["trusted"] == []
    assert body["blocked"] == []


def test_get_profile_returns_saved_data(authed_client, fake_ctx):
    with open_db(fake_ctx.db_path) as conn:
        save_profile(
            conn,
            fake_ctx.guild_id,
            user_id=42,
            profile=VoiceProfile(
                saved_name="Cozy Corner",
                saved_limit=4,
                locked=True,
                hidden=False,
                bitrate=128000,
            ),
        )
        add_trusted(conn, fake_ctx.guild_id, owner_id=42, target_id=100)
        add_blocked(conn, fake_ctx.guild_id, owner_id=42, target_id=200)

    body = authed_client.get("/api/voice-master/profiles/42").json()
    assert body["profile"]["saved_name"] == "Cozy Corner"
    assert body["profile"]["saved_limit"] == 4
    assert body["profile"]["locked"] is True
    assert 100 in body["trusted"]
    assert 200 in body["blocked"]


# ── POST /voice-master/profiles/{user_id}/clear ──────────────────────


def test_clear_profile_removes_data(authed_client, fake_ctx):
    with open_db(fake_ctx.db_path) as conn:
        save_profile(
            conn,
            fake_ctx.guild_id,
            user_id=42,
            profile=VoiceProfile(
                saved_name="x", saved_limit=0, locked=False, hidden=False, bitrate=None
            ),
        )
        add_trusted(conn, fake_ctx.guild_id, owner_id=42, target_id=100)
        add_blocked(conn, fake_ctx.guild_id, owner_id=42, target_id=200)

    resp = authed_client.post("/api/voice-master/profiles/42/clear")
    assert resp.status_code == 200

    body = authed_client.get("/api/voice-master/profiles/42").json()
    assert body["profile"] is None
    assert body["trusted"] == []
    assert body["blocked"] == []


# ── Auth gate ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/voice-master/config"),
        ("GET", "/api/voice-master/channels"),
        ("GET", "/api/voice-master/profiles/1"),
    ],
)
def test_voice_master_requires_auth(fake_ctx, method, path):
    from fastapi.testclient import TestClient

    from web_server.auth import DiscordOAuthAuth
    from web_server.server import create_app

    app = create_app(fake_ctx, auth=DiscordOAuthAuth("test-secret", fake_ctx.guild_id))
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(path) if method == "GET" else client.post(path)
    assert resp.status_code in (401, 403)
    client.close()
