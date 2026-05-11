"""Cog-level tests for the Veil guess flow."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.veil_models import BoundingBox, VeilRound
from tests.fakes import FakeMember, fake_interaction


@pytest.fixture(autouse=True)
def _patch_count_user_guesses():
    """Default per-test guess count to 0; cap tests override via their own patch."""
    with patch("cogs.veil_cog._do_count_user_guesses", return_value=0) as m:
        yield m

VEIL_ROLE_ID = 7001
ROUND_ID = 99


def _make_round(
    *,
    round_id: int = ROUND_ID,
    submitter_id: int = 1001,
    answer_id: int = 2001,
    solved_at: float | None = None,
) -> VeilRound:
    return VeilRound(
        id=round_id,
        guild_id=9001,
        submitter_id=submitter_id,
        answer_id=answer_id,
        channel_id=8001,
        message_id=12345,
        crop_path="/tmp/fake.jpg",
        crop_url="https://cdn.discord.com/fake.jpg",
        original_path="",
        difficulty="medium",
        candidate_count=1,
        reroll_count=0,
        allow_reuse=False,
        is_reuse=False,
        original_round_id=None,
        reuse_blocked=False,
        created_at=1000.0,
        solved_at=solved_at,
        solver_id=None,
        guesses_to_solve=None,
        unique_guessers_to_solve=None,
        answer_optout=False,
        deleted_at=None,
    )


def _make_select_view(
    bot=None,
    veil_members=None,
    game_message=None,
    round_id: int = ROUND_ID,
    cooldown_seconds: int = 0,
):
    from cogs.veil_cog import GuessSelectView

    if bot is None:
        bot = MagicMock()
        bot.ctx.db_path = ":memory:"
    if veil_members is None:
        veil_members = [FakeMember(id=2001, display_name="Alice")]
    if game_message is None:
        game_message = MagicMock()
        game_message.edit = AsyncMock()
        game_message.guild = MagicMock()

    return GuessSelectView(
        bot, round_id, veil_members, game_message,  # type: ignore[arg-type]
        cooldown_seconds=cooldown_seconds,
    )


# ── GuessSelectView tests ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_correct_first_guess_marks_solved_and_edits_message():
    game_msg = MagicMock()
    game_msg.edit = AsyncMock()
    game_msg.guild = MagicMock()
    view = _make_select_view(game_message=game_msg)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    round_row = _make_round()  # not yet solved

    with patch("cogs.veil_cog._do_count_user_guesses", return_value=0), \
         patch("cogs.veil_cog._do_load_round", return_value=round_row), \
         patch("cogs.veil_cog._do_insert_guess"), \
         patch("cogs.veil_cog._do_mark_solved", return_value=(1, 3, 2)), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    game_msg.edit.assert_called_once()
    call_content = interaction.edit_original_response.call_args.kwargs.get("content", "")
    assert "correct" in call_content.lower()


@pytest.mark.asyncio
async def test_correct_guess_attaches_full_image_as_spoiler_and_unlinks(tmp_path):
    """First correct guess swaps the message attachment for a SPOILER_-prefixed
    full original, then deletes the on-disk file and clears original_path."""
    orig_file = tmp_path / "1234.png"
    orig_file.write_bytes(b"fake-original-png-bytes")

    game_msg = MagicMock()
    game_msg.edit = AsyncMock()
    game_msg.guild = MagicMock()
    view = _make_select_view(game_message=game_msg)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    round_row = _make_round()
    round_row.original_path = str(orig_file)

    set_path_calls: list[tuple] = []

    def _capture_set_path(*args, **kwargs):
        set_path_calls.append((args, kwargs))

    with patch("cogs.veil_cog._do_load_round", return_value=round_row), \
         patch("cogs.veil_cog._do_insert_guess"), \
         patch("cogs.veil_cog._do_mark_solved", return_value=(1, 1, 1)), \
         patch("cogs.veil_cog._do_set_original_path", side_effect=_capture_set_path), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    edit_kwargs = game_msg.edit.call_args.kwargs
    attachments = edit_kwargs["attachments"]
    assert len(attachments) == 1
    assert attachments[0].filename == "SPOILER_veil_full.png"
    # Embed body has the answer reveal but no inline image set.
    assert edit_kwargs["embed"].image.url is None
    # File is removed and DB path cleared.
    assert not orig_file.exists()
    assert set_path_calls and set_path_calls[0][0][2] == ""


@pytest.mark.asyncio
async def test_correct_guess_without_original_path_still_solves():
    """Old rounds (pre-migration) have original_path == '' — solve should still
    proceed without trying to attach a missing file."""
    game_msg = MagicMock()
    game_msg.edit = AsyncMock()
    game_msg.guild = MagicMock()
    view = _make_select_view(game_message=game_msg)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    round_row = _make_round()  # original_path defaults to ""

    with patch("cogs.veil_cog._do_load_round", return_value=round_row), \
         patch("cogs.veil_cog._do_insert_guess"), \
         patch("cogs.veil_cog._do_mark_solved", return_value=(1, 1, 1)), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    edit_kwargs = game_msg.edit.call_args.kwargs
    assert edit_kwargs["attachments"] == []


@pytest.mark.asyncio
async def test_correct_guess_already_solved_does_not_edit_game_message():
    game_msg = MagicMock()
    game_msg.edit = AsyncMock()
    game_msg.guild = MagicMock()
    view = _make_select_view(game_message=game_msg)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    round_row = _make_round(solved_at=1234.0)

    with patch("cogs.veil_cog._do_load_round", return_value=round_row), \
         patch("cogs.veil_cog._do_insert_guess"), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    game_msg.edit.assert_not_called()
    call_content = interaction.edit_original_response.call_args.kwargs.get("content", "")
    assert "already" in call_content.lower() or "someone" in call_content.lower()


@pytest.mark.asyncio
async def test_wrong_guess_bumps_counter_and_sends_not_it_message():
    game_msg = MagicMock()
    game_msg.edit = AsyncMock()
    game_msg.guild = MagicMock()
    view = _make_select_view(game_message=game_msg)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    round_row = _make_round()

    with patch("cogs.veil_cog._do_load_round", return_value=round_row), \
         patch("cogs.veil_cog._do_insert_guess"), \
         patch("cogs.veil_cog._do_count_guesses_for_round", return_value=4), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(3333)])):
        await view._on_select(interaction)

    # Counter-bump edit: a fresh GameView with the new count is attached.
    game_msg.edit.assert_called_once()
    edit_kwargs = game_msg.edit.call_args.kwargs
    new_view = edit_kwargs["view"]
    labels = [c.label for c in new_view.children]
    assert "Guesses: 4" in labels

    call_content = interaction.edit_original_response.call_args.kwargs.get("content", "")
    assert "not it" in call_content.lower()


@pytest.mark.asyncio
async def test_wrong_guess_on_solved_round_skips_counter_bump():
    """If the round was already solved when we loaded it, the public message
    already has the solved view — a chip-bump would overwrite it."""
    game_msg = MagicMock()
    game_msg.edit = AsyncMock()
    game_msg.guild = MagicMock()
    view = _make_select_view(game_message=game_msg)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    round_row = _make_round(solved_at=1234.0)

    with patch("cogs.veil_cog._do_load_round", return_value=round_row), \
         patch("cogs.veil_cog._do_insert_guess"), \
         patch("cogs.veil_cog._do_count_guesses_for_round", return_value=8), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(3333)])):
        await view._on_select(interaction)

    game_msg.edit.assert_not_called()


@pytest.mark.asyncio
async def test_guess_cap_blocks_after_five_attempts():
    """Per-(user, round) guess cap: 5th attempt is rejected, no insert/select effect."""
    view = _make_select_view(cooldown_seconds=0)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    insert_mock = MagicMock()
    with patch("cogs.veil_cog._do_count_user_guesses", return_value=5), \
         patch("cogs.veil_cog._do_insert_guess", insert_mock), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    insert_mock.assert_not_called()
    msg = interaction.edit_original_response.call_args.kwargs.get("content", "")
    assert "out of guesses" in msg.lower() or "no guesses" in msg.lower() or "cap" in msg.lower()


@pytest.mark.asyncio
async def test_guess_cap_allows_under_limit():
    """4 prior guesses → 5th still allowed."""
    view = _make_select_view(cooldown_seconds=0)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    round_row = _make_round()
    with patch("cogs.veil_cog._do_count_user_guesses", return_value=4), \
         patch("cogs.veil_cog._do_load_round", return_value=round_row), \
         patch("cogs.veil_cog._do_insert_guess") as insert_mock, \
         patch("cogs.veil_cog._do_count_guesses_for_round", return_value=5), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(3333)])):
        await view._on_select(interaction)

    insert_mock.assert_called_once()


@pytest.mark.asyncio
async def test_correct_guess_loses_race_does_not_edit_message():
    """If _do_mark_solved returns rowcount==0 (someone else won the race),
    the cog must NOT edit the public game message — only ack the guesser."""
    game_msg = MagicMock()
    game_msg.edit = AsyncMock()
    game_msg.guild = MagicMock()
    view = _make_select_view(game_message=game_msg)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    round_row = _make_round()  # solved_at None — but mark_solved will say rowcount=0
    with patch("cogs.veil_cog._do_load_round", return_value=round_row), \
         patch("cogs.veil_cog._do_insert_guess"), \
         patch("cogs.veil_cog._do_mark_solved", return_value=(0, 5, 3)), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    game_msg.edit.assert_not_called()
    msg = interaction.edit_original_response.call_args.kwargs.get("content", "")
    assert "already" in msg.lower() or "someone" in msg.lower()


@pytest.mark.asyncio
async def test_select_is_disabled_after_guess():
    view = _make_select_view()
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    round_row = _make_round()
    with patch("cogs.veil_cog._do_load_round", return_value=round_row), \
         patch("cogs.veil_cog._do_insert_guess"), \
         patch("cogs.veil_cog._do_mark_solved", return_value=(1, 1, 1)), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    assert view._select.disabled is True


# ── Cooldown tests ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_on_select_rejects_guess_within_cooldown():
    import time as _time
    from services.veil_models import VeilGuess

    view = _make_select_view(cooldown_seconds=30)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    recent_guess = VeilGuess(
        id=1, round_id=ROUND_ID, guesser_id=9999,
        guessed_user_id=2001, correct=False,
        created_at=_time.time() - 5,  # 5s ago, within 30s cooldown
    )
    insert_mock = MagicMock()
    with patch("cogs.veil_cog._do_get_last_guess", return_value=recent_guess), \
         patch("cogs.veil_cog._do_insert_guess", insert_mock), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    insert_mock.assert_not_called()
    call_content = interaction.edit_original_response.call_args.kwargs.get("content", "")
    assert "cooldown" in call_content.lower() or "try again" in call_content.lower()


@pytest.mark.asyncio
async def test_on_select_allows_guess_after_cooldown_expires():
    import time as _time
    from services.veil_models import VeilGuess

    view = _make_select_view(cooldown_seconds=30)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    old_guess = VeilGuess(
        id=1, round_id=ROUND_ID, guesser_id=9999,
        guessed_user_id=2001, correct=False,
        created_at=_time.time() - 60,  # 60s ago, past 30s cooldown
    )
    round_row = _make_round()
    with patch("cogs.veil_cog._do_get_last_guess", return_value=old_guess), \
         patch("cogs.veil_cog._do_load_round", return_value=round_row), \
         patch("cogs.veil_cog._do_insert_guess") as insert_mock, \
         patch("cogs.veil_cog._do_mark_solved", return_value=(1, 2, 1)), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    insert_mock.assert_called_once()


@pytest.mark.asyncio
async def test_on_select_cooldown_zero_disables_check():
    """cooldown_seconds=0 means no cooldown enforcement."""
    view = _make_select_view(cooldown_seconds=0)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    round_row = _make_round()
    get_last_mock = MagicMock()
    with patch("cogs.veil_cog._do_get_last_guess", get_last_mock), \
         patch("cogs.veil_cog._do_load_round", return_value=round_row), \
         patch("cogs.veil_cog._do_insert_guess"), \
         patch("cogs.veil_cog._do_mark_solved", return_value=(1, 1, 1)), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    get_last_mock.assert_not_called()


# ── GameView tests ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_guess_callback_message_mentions_timer():
    """The ephemeral 'Who do you think this is?' message must surface the
    countdown so users know the prompt is time-limited."""
    from cogs.veil_cog import GameView
    from services.veil_models import VeilConfig

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    view = GameView(bot, ROUND_ID)

    guesser = FakeMember(id=9999)
    interaction = fake_interaction(user=guesser)
    interaction.response.send_message = AsyncMock()
    veil_role = MagicMock()
    veil_role.members = [FakeMember(id=2001, display_name="Alice")]
    interaction.guild.get_role = MagicMock(return_value=veil_role)

    round_row = _make_round(submitter_id=1001)
    config = VeilConfig(guild_id=9001, veil_role_id=VEIL_ROLE_ID, guess_cooldown_seconds=30)

    with patch("cogs.veil_cog._load_config", return_value=config), \
         patch("cogs.veil_cog._do_load_round", return_value=round_row):
        await view._guess_callback(interaction)

    msg = interaction.response.send_message.call_args.args[0]
    assert "60" in msg or "second" in msg.lower(), f"expected timer hint in: {msg!r}"


@pytest.mark.asyncio
async def test_game_view_rejects_submitter_guessing_own_round():
    from cogs.veil_cog import GameView
    from services.veil_models import VeilConfig

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    view = GameView(bot, ROUND_ID)

    submitter = FakeMember(id=1001)
    interaction = fake_interaction(user=submitter)
    interaction.response.send_message = AsyncMock()

    round_row = _make_round(submitter_id=1001)
    config = VeilConfig(guild_id=9001, veil_role_id=VEIL_ROLE_ID)

    with patch("cogs.veil_cog._load_config", return_value=config), \
         patch("cogs.veil_cog._do_load_round", return_value=round_row):
        await view._guess_callback(interaction)

    msg = interaction.response.send_message.call_args.args[0]
    assert "can't guess" in msg.lower() or "own round" in msg.lower()


# ── CropEditorView post tests ────────────────────────────────────────────────

GUILD_ID = 9001
VEIL_CHANNEL_ID = 8001


@pytest.mark.asyncio
async def test_post_persists_original_bytes_and_stores_path(tmp_path, monkeypatch):
    """On Post, the submitter's original bytes are written to <orig_dir>/<round_id><ext>
    and the path is recorded so /correct guess can attach it as a SPOILER."""
    import cogs.veil_cog as veil_cog
    from cogs.veil_cog import CropEditorView

    monkeypatch.setattr(veil_cog, "_VEIL_ORIG_DIR", tmp_path / "orig")

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    bot.add_view = MagicMock()

    fake_channel = MagicMock()
    fake_channel.mention = "#veil"
    fake_channel.is_nsfw = lambda: True
    fake_msg = MagicMock()
    fake_msg.id = 9999
    attached = MagicMock()
    attached.url = "https://cdn.example/crop.jpg"
    fake_msg.attachments = [attached]
    fake_channel.send = AsyncMock(return_value=fake_msg)

    interaction = fake_interaction()
    interaction.guild.get_channel = lambda cid: fake_channel
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    view = CropEditorView(
        bot,
        image_bytes=b"",
        img_w=500,
        img_h=500,
        crop_box=BoundingBox(0.0, 0.0, 500.0, 500.0),
        guild_id=GUILD_ID,
        veil_channel_id=VEIL_CHANNEL_ID,
        submitter_id=1001,
        answer_id=2001,
        difficulty="medium",
        candidate_count=1,
        original_bytes=b"the-original-png-bytes",
        original_ext=".png",
    )

    set_path_calls: list[tuple] = []

    def _capture(*args, **kwargs):
        set_path_calls.append((args, kwargs))

    with patch("cogs.veil_cog._do_insert_round", return_value=77), \
         patch("cogs.veil_cog._do_update_round_message"), \
         patch("cogs.veil_cog._do_set_original_path", side_effect=_capture), \
         patch("cogs.veil_cog._do_audit"), \
         patch("cogs.veil_cog.render_crop", return_value=b"\xff\xd8fake"), \
         patch("cogs.veil_cog._repost_prompt", new_callable=AsyncMock):
        with patch("cogs.veil_cog.isinstance", lambda obj, types: True):
            await view._on_post(interaction)

    persisted = tmp_path / "orig" / "77.png"
    assert persisted.exists()
    assert persisted.read_bytes() == b"the-original-png-bytes"
    assert set_path_calls and set_path_calls[0][0][2] == str(persisted)
