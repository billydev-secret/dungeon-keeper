"""Tests for beta_tools.puppet_manager construction + persona diff logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beta_tools.personas import Persona


@pytest.fixture
def three_personas():
    return [
        Persona(
            key="alice", display_name="Alice", avatar_url="https://x/a.png",
            activity_weight=1.0, channel_affinities={"general": 1.0},
            voice_likely=True, message_length_bias="short",
        ),
        Persona(
            key="bob", display_name="Bob", avatar_url="https://x/b.png",
            activity_weight=1.0, channel_affinities={"general": 1.0},
            voice_likely=False, message_length_bias="medium",
        ),
        Persona(
            key="clara", display_name="Clara", avatar_url="https://x/c.png",
            activity_weight=1.0, channel_affinities={"general": 1.0},
            voice_likely=True, message_length_bias="long",
        ),
    ]


def test_puppet_manager_construction(three_personas):
    from beta_tools.puppet_manager import PuppetManager
    tokens = ("t1", "t2", "t3")
    expected_ids = (1, 2, 3)
    pm = PuppetManager(personas=three_personas, tokens=tokens, expected_ids=expected_ids, expected_guild_id=9001)
    assert len(pm.handles) == 3
    assert pm.handles[0].key == "alice"
    assert pm.handles[1].key == "bob"
    assert pm.handles[2].key == "clara"


def test_puppet_manager_rejects_count_mismatch(three_personas):
    from beta_tools.puppet_manager import PuppetManager
    with pytest.raises(ValueError, match="3 personas"):
        PuppetManager(
            personas=three_personas[:2],   # only 2
            tokens=("t1", "t2", "t3"),
            expected_ids=(1, 2, 3),
            expected_guild_id=9001,
        )


def test_puppet_manager_get_handle_by_key(three_personas):
    from beta_tools.puppet_manager import PuppetManager
    pm = PuppetManager(
        personas=three_personas,
        tokens=("t1", "t2", "t3"),
        expected_ids=(1, 2, 3),
        expected_guild_id=9001,
    )
    handle = pm.get_handle("bob")
    assert handle.key == "bob"
    with pytest.raises(KeyError):
        pm.get_handle("nobody")


async def test_apply_persona_skips_when_already_correct(three_personas):
    """If the puppet's display name and avatar already match the persona, don't call edit."""
    from beta_tools.puppet_manager import _apply_persona

    persona = three_personas[0]  # alice, https://x/a.png

    # Build a fake user whose display_name already matches.
    fake_user = MagicMock()
    fake_user.name = "Alice"
    fake_user.display_avatar.url = "https://x/a.png"
    fake_user.edit = AsyncMock()

    fake_client = MagicMock()
    fake_client.user = fake_user

    await _apply_persona(fake_client, persona)
    fake_user.edit.assert_not_called()


async def test_apply_persona_renames_when_name_differs(three_personas):
    from beta_tools.puppet_manager import _apply_persona

    persona = three_personas[0]  # alice
    fake_user = MagicMock()
    fake_user.name = "OldName"
    fake_user.display_avatar.url = "https://x/a.png"
    fake_user.edit = AsyncMock()

    fake_client = MagicMock()
    fake_client.user = fake_user

    await _apply_persona(fake_client, persona)
    # Should call edit with username="Alice"
    fake_user.edit.assert_awaited_once()
    _, kwargs = fake_user.edit.call_args
    assert kwargs["username"] == "Alice"


async def test_apply_persona_does_not_fetch_avatar_when_url_matches(three_personas):
    """Avatar fetch should be skipped when the persona's URL already matches user.display_avatar.url."""
    from beta_tools.puppet_manager import _apply_persona

    persona = three_personas[0]  # alice, https://x/a.png
    fake_user = MagicMock()
    fake_user.name = "OldName"  # name differs, triggers edit
    fake_user.display_avatar.url = "https://x/a.png"  # avatar matches
    fake_user.edit = AsyncMock()

    fake_client = MagicMock()
    fake_client.user = fake_user

    with patch("beta_tools.puppet_manager.aiohttp.ClientSession") as mock_session:
        await _apply_persona(fake_client, persona)
        mock_session.assert_not_called()  # avatar URL matched, so no HTTP fetch

    fake_user.edit.assert_awaited_once()
    _, kwargs = fake_user.edit.call_args
    assert "avatar" not in kwargs
    assert kwargs["username"] == "Alice"


async def test_start_all_raises_when_puppet_task_fails(three_personas):
    """If a puppet's start task crashes (bad token), start_all should raise — not hang."""
    from beta_tools.puppet_manager import PuppetManager

    pm = PuppetManager(
        personas=three_personas,
        tokens=("bad1", "bad2", "bad3"),
        expected_ids=(1, 2, 3),
        expected_guild_id=9001,
    )

    # Patch new_puppet_client to return a fake client whose start() raises immediately.
    fake_client = MagicMock()
    fake_client.start = AsyncMock(side_effect=RuntimeError("simulated bad token"))

    with patch("beta_tools.puppet_manager.new_puppet_client", return_value=fake_client):
        with pytest.raises(RuntimeError, match="failed to start"):
            await pm.start_all()
