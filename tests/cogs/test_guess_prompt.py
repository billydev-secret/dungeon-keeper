"""Cog-level tests for the sticky channel prompt.

The prompt moved onto ``core.sticky.StickyPanel`` on 2026-08-06 — it was the
last hand-rolled copy of the placer, and it still deleted before posting and
left placements unshielded. So the debounce, the lock and the placement
semantics are covered once in ``tests/test_core_sticky.py``; what is left here
is this cog's own glue: the three sticky callbacks, the dashboard entry point,
and one assertion that the listener actually forwards.
"""
from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.services.guess_models import GuessConfig
from tests.fakes import FakeGuild, fake_interaction

GUESS_CHANNEL_ID = 8001
GUESS_ROLE_ID = 7001
GUILD_ID = 9001


@pytest.fixture(autouse=True)
def _stub_accent_color(monkeypatch):
    """resolve_accent_color awaits guild.me.display_avatar.read(), which the
    mocked guilds here can't satisfy — stub it at the use-site namespace."""
    monkeypatch.setattr(
        "bot_modules.core.branding.resolve_accent_color",
        AsyncMock(return_value=discord.Color.default()),
    )


def _make_cog(db_path: str = ":memory:"):
    from bot_modules.cogs.guess_cog import GuessCog
    bot = MagicMock()
    bot.ctx.db_path = db_path
    bot.add_view = MagicMock()
    return GuessCog(bot)


def _config(
    *,
    channel_id: int = GUESS_CHANNEL_ID,
    prompt_id: int = 0,
    prompt_channel_id: int | None = None,
) -> GuessConfig:
    return GuessConfig(
        guild_id=GUILD_ID,
        guess_role_id=GUESS_ROLE_ID,
        guess_channel_id=channel_id,
        prompt_message_id=prompt_id,
        prompt_channel_id=(
            channel_id if prompt_channel_id is None else prompt_channel_id
        ),
    )


def _make_text_channel(channel_id: int = GUESS_CHANNEL_ID, *, send_returns_id: int = 99999):
    """A MagicMock that satisfies isinstance(..., discord.TextChannel)."""
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = channel_id
    ch.mention = f"<#{channel_id}>"
    sent_msg = MagicMock()
    sent_msg.id = send_returns_id
    ch.send = AsyncMock(return_value=sent_msg)
    ch.fetch_message = AsyncMock()
    return ch


# ── GuessPromptView ───────────────────────────────────────────────────────────

def test_prompt_view_has_two_buttons_with_stable_custom_ids():
    from bot_modules.cogs.guess_cog import GuessPromptView

    view = GuessPromptView(MagicMock())
    children = cast(list[discord.ui.Button], view.children)
    custom_ids = {c.custom_id for c in children if c.custom_id}
    assert "guess_prompt_submit" in custom_ids
    assert "guess_prompt_help" in custom_ids
    assert len(children) == 2


def test_prompt_view_is_persistent():
    from bot_modules.cogs.guess_cog import GuessPromptView

    view = GuessPromptView(MagicMock())
    assert view.timeout is None


@pytest.mark.asyncio
async def test_prompt_submit_button_sends_ephemeral_instructions():
    from bot_modules.cogs.guess_cog import GuessPromptView

    view = GuessPromptView(MagicMock())
    children = cast(list[discord.ui.Button], view.children)
    submit_btn = next(c for c in children if c.custom_id == "guess_prompt_submit")
    interaction = fake_interaction()

    await submit_btn.callback(interaction)

    interaction.response.send_modal.assert_awaited_once()


@pytest.mark.asyncio
async def test_prompt_help_button_sends_ephemeral_rules():
    from bot_modules.cogs.guess_cog import GuessPromptView

    view = GuessPromptView(MagicMock())
    children = cast(list[discord.ui.Button], view.children)
    help_btn = next(c for c in children if c.custom_id == "guess_prompt_help")
    interaction = fake_interaction()

    await help_btn.callback(interaction)

    interaction.response.send_message.assert_awaited_once()
    call_kwargs = interaction.response.send_message.call_args.kwargs
    assert call_kwargs.get("ephemeral") is True
    msg = interaction.response.send_message.call_args.args[0]
    assert "guess" in msg.lower() and ("guess" in msg.lower() or "submit" in msg.lower())


# ── the sticky callbacks ─────────────────────────────────────────────────────


def _panel_cog(config: GuessConfig):
    """A cog whose ``open_db`` hands the callbacks a stubbed config."""
    cog = _make_cog()
    conn = MagicMock()
    cog.bot.ctx.open_db.return_value.__enter__.return_value = conn
    return cog, conn


@pytest.mark.asyncio
async def test_panel_ids_report_where_the_prompt_actually_is():
    """Not where it ought to be. ``place`` deletes the old prompt through the
    stored channel, so pairing a stale message id with a repointed
    ``guess_channel_id`` would aim the delete at the wrong channel and strand
    the old prompt with its buttons live."""
    cog, _conn = _panel_cog(_config())
    with patch(
        "bot_modules.cogs.guess_cog.get_guess_config",
        return_value=_config(prompt_id=555, prompt_channel_id=4242),
    ):
        assert cog._panel_ids(GUILD_ID) == (4242, 555)


@pytest.mark.asyncio
async def test_panel_ids_are_zero_before_the_prompt_is_posted():
    """(0, 0) is what makes the restick a no-op — it only ever maintains a
    prompt that already exists, never creates one."""
    cog, _conn = _panel_cog(_config())
    with patch(
        "bot_modules.cogs.guess_cog.get_guess_config",
        return_value=_config(prompt_id=0, prompt_channel_id=0),
    ):
        assert cog._panel_ids(GUILD_ID) == (0, 0)


def test_save_panel_ids_records_the_channel_as_well_as_the_message():
    """Storing only the message id was what left the delete aiming at whatever
    channel the caller happened to pass."""
    cog, _conn = _panel_cog(_config())
    with patch("bot_modules.cogs.guess_cog.set_guess_config_value") as setter:
        cog._save_panel_ids(GUILD_ID, 4242, 555)
    saved = {call.args[2]: call.args[3] for call in setter.call_args_list}
    assert saved == {
        "guess_prompt_channel_id": "4242",
        "guess_prompt_message_id": "555",
    }


@pytest.mark.asyncio
async def test_build_prompt_panel_carries_the_persistent_view():
    """The prompt's buttons must survive a restart, so the view the placer sends
    has to be the registered persistent one."""
    from bot_modules.cogs.guess_cog import GuessPromptView

    cog = _make_cog()
    content = await cog._build_prompt_panel(FakeGuild(id=GUILD_ID))
    assert isinstance(content.view, GuessPromptView)
    assert content.view.timeout is None


@pytest.mark.asyncio
async def test_repost_prompt_places_through_the_shared_panel():
    """The four call sites still speak ``_repost_prompt``; it must not grow its
    own placer again."""
    from bot_modules.cogs.guess_cog import _repost_prompt

    cog = _make_cog()
    cog.prompt_panel = MagicMock()
    cog.prompt_panel.place = AsyncMock()
    bot = MagicMock()
    bot.get_cog.return_value = cog
    channel = _make_text_channel()
    channel.guild = FakeGuild(id=GUILD_ID)

    await _repost_prompt(bot, channel, GUILD_ID)

    cog.prompt_panel.place.assert_awaited_once_with(channel.guild, channel)


@pytest.mark.asyncio
async def test_repost_prompt_is_a_noop_without_the_cog():
    from bot_modules.cogs.guess_cog import _repost_prompt

    bot = MagicMock()
    bot.get_cog.return_value = None
    channel = _make_text_channel()
    await _repost_prompt(bot, channel, GUILD_ID)  # must not raise


# ── on_message listener ──────────────────────────────────────────────────────


def _make_message(*, channel_id: int, author_bot: bool = False, guild_id: int = GUILD_ID):
    msg = MagicMock()
    msg.author.bot = author_bot
    msg.author.id = 555
    guild = FakeGuild(id=guild_id)
    msg.guild = guild
    channel = _make_text_channel(channel_id=channel_id)
    msg.channel = channel
    return msg


@pytest.mark.asyncio
async def test_on_message_forwards_to_the_sticky_panel():
    """The only wiring worth asserting here — everything the panel then does
    (bot filter, known-guilds gate, TTL cache, debounce) is covered in
    tests/test_core_sticky.py rather than re-proved through Discord mocks."""
    cog = _make_cog()
    cog.prompt_panel = MagicMock()
    cog.prompt_panel.on_message = AsyncMock()
    msg = _make_message(channel_id=GUESS_CHANNEL_ID)

    await cog.on_message(msg)

    cog.prompt_panel.on_message.assert_awaited_once_with(msg)


@pytest.mark.asyncio
async def test_on_message_no_longer_reads_the_db_per_message():
    """Regression for F3 (2026-08-06): this listener used to open a fresh
    connection for every message in every guild, before it had even looked at
    the channel."""
    cog = _make_cog()
    cog.prompt_panel = MagicMock()
    cog.prompt_panel.on_message = AsyncMock()
    msg = _make_message(channel_id=GUESS_CHANNEL_ID)

    with patch("bot_modules.cogs.guess_cog._load_config") as load_cfg:
        await cog.on_message(msg)

    load_cfg.assert_not_called()


@pytest.mark.asyncio
async def test_cog_unload_cancels_the_panel_debounce():
    cog = _make_cog()
    cog.prompt_panel = MagicMock()
    cog._age_out_loop = MagicMock()

    await cog.cog_unload()  # type: ignore[attr-defined]

    cog.prompt_panel.cancel_all.assert_called_once()


@pytest.mark.asyncio
async def test_channel_delete_clears_the_prompt_ids():
    cog = _make_cog()
    cog.prompt_panel = MagicMock()
    cog.prompt_panel.on_channel_delete = AsyncMock()
    channel = _make_text_channel()

    await cog._forget_deleted_prompt_channel(channel)

    cog.prompt_panel.on_channel_delete.assert_awaited_once_with(channel)


# ── post_prompt_panel (dashboard entry point) ────────────────────────────────
#
# /guess prompt was replaced by a dashboard post control on 2026-07-28. The
# method ignores any channel handed to it — the prompt belongs in the configured
# Guess channel, since that is the only place the cog's sticky listener looks.


@pytest.mark.asyncio
async def test_post_prompt_panel_returns_none_when_channel_unset():
    """None is how the route knows to say "set a Guess channel first" rather
    than reporting a Discord failure."""
    cog = _make_cog()
    guild = FakeGuild(id=GUILD_ID)

    with patch("bot_modules.cogs.guess_cog._load_config", return_value=_config(channel_id=0)):
        result = await cog.post_prompt_panel(guild, None)

    assert result is None


@pytest.mark.asyncio
async def test_post_prompt_panel_posts_to_the_configured_channel():
    channel = _make_text_channel()
    guild = FakeGuild(id=GUILD_ID, channels={GUESS_CHANNEL_ID: channel})

    cog = _make_cog()
    cog.prompt_panel = MagicMock()
    cog.prompt_panel.place_or_refresh = AsyncMock()
    with patch("bot_modules.cogs.guess_cog._load_config", return_value=_config()):
        result = await cog.post_prompt_panel(guild, None)

    # place_or_refresh, not place: re-running the dashboard control after a
    # re-brand should repaint the prompt, not hop it to the bottom.
    cog.prompt_panel.place_or_refresh.assert_awaited_once_with(guild, channel)
    assert result is channel


@pytest.mark.asyncio
async def test_post_prompt_panel_ignores_a_supplied_channel():
    """Honouring a picked channel would strand the Submit button outside the
    flow the sticky listener drives."""
    configured = _make_text_channel()
    other = _make_text_channel(channel_id=4242)
    guild = FakeGuild(id=GUILD_ID, channels={GUESS_CHANNEL_ID: configured})

    cog = _make_cog()
    cog.prompt_panel = MagicMock()
    cog.prompt_panel.place_or_refresh = AsyncMock()
    with patch("bot_modules.cogs.guess_cog._load_config", return_value=_config()):
        await cog.post_prompt_panel(guild, other)

    cog.prompt_panel.place_or_refresh.assert_awaited_once_with(guild, configured)


# ── the 2026-08-06 code-review follow-ups ────────────────────────────────────


@pytest.mark.asyncio
async def test_panel_ids_fall_back_to_the_guess_channel_for_a_legacy_prompt():
    """``guess_prompt_channel_id`` arrived with the core.sticky migration, so
    every guild that already had a prompt has a message id and no channel id —
    verified against prod: three guilds with a live prompt, zero rows for the new
    key. Without this fallback those prompts read (0, live_id): should_restick
    bails on a falsy channel so the prompt stops re-sticking altogether, and the
    first placement cannot resolve a channel to delete the old one through,
    leaving two prompts with the stale one's buttons live.
    """
    cog, _conn = _panel_cog(_config())
    with patch(
        "bot_modules.cogs.guess_cog.get_guess_config",
        return_value=_config(prompt_id=555, prompt_channel_id=0),
    ):
        assert cog._panel_ids(GUILD_ID) == (GUESS_CHANNEL_ID, 555)


@pytest.mark.asyncio
async def test_panel_ids_do_not_invent_a_channel_when_nothing_is_posted():
    """The fallback is for legacy rows only — a guild that never posted a prompt
    must still read (0, 0) so the restick never creates one."""
    cog, _conn = _panel_cog(_config())
    with patch(
        "bot_modules.cogs.guess_cog.get_guess_config",
        return_value=_config(prompt_id=0, prompt_channel_id=0),
    ):
        assert cog._panel_ids(GUILD_ID) == (0, 0)


@pytest.mark.asyncio
async def test_a_deleted_thread_also_clears_the_prompt_ids():
    """The Guess channel may be a thread — that is what target_types was widened
    for — and Discord dispatches on_thread_delete rather than
    on_guild_channel_delete for those, so the panel needs both."""
    cog = _make_cog()
    cog.prompt_panel = MagicMock()
    cog.prompt_panel.on_channel_delete = AsyncMock()
    thread = MagicMock(spec=discord.Thread)

    await cog._forget_deleted_prompt_thread(thread)

    cog.prompt_panel.on_channel_delete.assert_awaited_once_with(thread)
