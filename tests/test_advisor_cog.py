"""The one piece of `/ask` glue that is itself a control, not just wiring.

The Apply click is documented as the prompt-injection defence, so the admin has
to be able to read the whole of what they are confirming. The button label
truncates at 80 characters and the embed description is the model's own prose —
so the proposal fields the cog appends are the only complete, non-model-authored
account of a pending write. Everything else in this cog is exercised through
the service layer.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from bot_modules.cogs import advisor_cog
from bot_modules.cogs.advisor_cog import _proposal_fields
from bot_modules.services.advisor_actions import ConfigProposal
from bot_modules.services.advisor_service import AdvisorResult


def _embed():
    return discord.Embed(title="🤖 Billy-bot", description="Sure, here you go.")


def test_full_value_is_disclosed_even_when_the_button_label_truncates():
    long_value = "Welcome to the NSFW side {member}! " + "please read the rules " * 7
    prop = ConfigProposal(
        key="grant_message",
        value=long_value,
        display=f"NSFW grant message → {long_value}",
        target="grant_role",
        grant_name="nsfw",
    )
    embed = _embed()
    _proposal_fields(embed, [prop])

    button_label = f"Apply: {prop.display}"[:80]
    assert long_value not in button_label  # the gap this closes

    field = embed.fields[0]
    assert long_value in field.value
    assert "nsfw role grant" in field.value
    assert "grant_message" in field.value


def test_every_queued_proposal_gets_its_own_field():
    props = [
        ConfigProposal("welcome_channel_id", "1", "Welcome channel → #welcome"),
        ConfigProposal("welcome_ping_member", "1", "Ping the new member → on"),
    ]
    embed = _embed()
    _proposal_fields(embed, props)

    assert len(embed.fields) == 2
    assert "Welcome channel → #welcome" in embed.fields[0].value
    assert "Ping the new member → on" in embed.fields[1].value
    # A config-table change says so, so "grant" in a field means a grant.
    assert "server setting" in embed.fields[0].value


def test_disclosure_never_exceeds_discord_field_limits():
    props = [
        ConfigProposal(f"k{i}", "v", "L → " + "x" * 400) for i in range(6)
    ]
    embed = _embed()
    _proposal_fields(embed, props)

    assert len(embed.fields) == 4  # _MAX_PROPOSALS — same slice the view takes
    assert all(len(f.value) <= 1024 for f in embed.fields)


# ── public posting: the gate on what reaches the channel ────────────────────
#
# The public path is the second control in this cog. Everything a mod can see
# that the room can't — staff channel names and topics, live settings, Apply
# buttons — has to be stripped *before* the answer is generated, so these
# assert on what the cog hands the service, not on the prose that comes back.


def _member(**perms):
    m = MagicMock(spec=discord.Member)
    m.id = 7
    m.display_name = "Mod Molly"
    m.guild_permissions = discord.Permissions(**perms)
    return m


def _interaction(user):
    i = MagicMock(spec=discord.Interaction)
    i.user = user
    i.guild = MagicMock(spec=discord.Guild, id=123)
    i.channel = MagicMock(spec=discord.TextChannel, id=456, name="general")
    i.response = MagicMock()
    i.response.defer = AsyncMock()
    i.followup = MagicMock()
    i.followup.send = AsyncMock()
    return i


def _cog():
    ctx = MagicMock()
    ctx.db_path = ":memory:"
    return SimpleNamespace(ctx=ctx)


@contextlib.contextmanager
def _patched(monkeypatch, *, answer="1. Run /daily.\n2. Spend it.", ok=True):
    """Everything around the ask, with both guild toggles ON.

    Context and tools enabled is the worst case for a public post: it's the
    configuration where a private ask *would* get the admin path.
    """
    mod = advisor_cog
    monkeypatch.setattr(mod, "open_db", lambda p: contextlib.nullcontext(object()))
    monkeypatch.setattr(mod, "resolve_advisor_model", lambda *a, **k: "model-x")
    monkeypatch.setattr(mod, "resolve_assistant_name_conn", lambda *a, **k: "Billy-bot")
    monkeypatch.setattr(mod, "get_advisor_context_enabled", lambda *a, **k: True)
    monkeypatch.setattr(mod, "get_advisor_tools_enabled", lambda *a, **k: True)
    monkeypatch.setattr(mod, "resolve_accent_color", AsyncMock(return_value=None))
    monkeypatch.setattr(mod, "build_asker_context", MagicMock(return_value="CTX"))
    monkeypatch.setattr(mod, "_make_tools", MagicMock(name="_make_tools"))
    monkeypatch.setattr(
        mod, "answer_advisor", AsyncMock(return_value=AdvisorResult(ok, answer))
    )
    yield mod


async def test_public_ask_is_refused_for_a_plain_member(monkeypatch):
    """The refusal lands before the model call — nothing is generated, and the
    member is told how to get their answer privately instead."""
    with _patched(monkeypatch) as mod:
        interaction = _interaction(_member())
        await mod.AdvisorCog.ask.callback(
            _cog(), interaction, "how do I earn coins?", public=True
        )
    mod.answer_advisor.assert_not_awaited()
    msg = interaction.followup.send.await_args.args[0]
    assert "Only mods" in msg
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True


async def test_public_ask_drops_tools_and_the_askers_own_context(monkeypatch):
    """Even for an administrator with both toggles on: an answer the room will
    read is built at @everyone visibility, with no config tools behind it."""
    with _patched(monkeypatch) as mod:
        interaction = _interaction(_member(administrator=True))
        await mod.AdvisorCog.ask.callback(
            _cog(), interaction, "how do I earn coins?", public=True
        )

    mod._make_tools.assert_not_called()  # so no Apply button can exist
    kwargs = mod.answer_advisor.await_args.kwargs
    assert kwargs["tools"] is None
    assert kwargs["public_tutorial"] is True

    ctx_call = mod.build_asker_context.call_args
    assert ctx_call.args[1] is None  # viewer=None → @everyone visibility
    assert ctx_call.kwargs["include_config"] is False


async def test_private_ask_still_gets_the_admin_path(monkeypatch):
    """The public branch must not have taken the config tools away from a
    normal admin ask."""
    with _patched(monkeypatch) as mod:
        interaction = _interaction(_member(administrator=True))
        await mod.AdvisorCog.ask.callback(_cog(), interaction, "is welcome on?")

    mod._make_tools.assert_called_once()
    kwargs = mod.answer_advisor.await_args.kwargs
    assert kwargs["public_tutorial"] is False
    assert mod.build_asker_context.call_args.args[1] is not None  # the asker


async def test_public_ask_previews_instead_of_posting(monkeypatch):
    """Nothing reaches the channel from the command itself — the mod gets an
    ephemeral preview carrying the Post button."""
    with _patched(monkeypatch) as mod:
        interaction = _interaction(_member(manage_messages=True))
        await mod.AdvisorCog.ask.callback(
            _cog(), interaction, "how do I earn coins?", public=True
        )

    interaction.channel.send.assert_not_called()
    kwargs = interaction.followup.send.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert isinstance(kwargs["view"], advisor_cog._PublicPostView)
    assert kwargs["embed"].title == "🤖 how do I earn coins?"


async def test_a_failed_answer_is_never_offered_for_posting(monkeypatch):
    """An 'I couldn't reach the model' notice must not come with a Post button."""
    with _patched(monkeypatch, answer="I couldn't reach Billy-bot", ok=False) as mod:
        interaction = _interaction(_member(manage_messages=True))
        await mod.AdvisorCog.ask.callback(
            _cog(), interaction, "how do I earn coins?", public=True
        )
    assert "view" not in interaction.followup.send.await_args.kwargs


async def test_post_button_publishes_the_previewed_embed(monkeypatch):
    """What posts is the object that was previewed — not a re-render, and not
    a re-run of the model."""
    channel = MagicMock(spec=discord.TextChannel, name="general")
    channel.send = AsyncMock()
    embed = advisor_cog._answer_embed(
        question="how do I earn coins?",
        answer="1. Run /daily.",
        assistant_name="Billy-bot",
        color=None,
        asker=_member(),
    )
    view = advisor_cog._PublicPostView(channel=channel, embed=embed, asker_id=7)
    interaction = _interaction(_member(manage_messages=True))
    interaction.response.edit_message = AsyncMock()

    await view.post.callback(interaction)

    assert channel.send.await_args.kwargs["embed"] is embed
    assert channel.send.await_args.kwargs["allowed_mentions"].everyone is False
    assert all(c.disabled for c in view.children)
    assert "Posted" in interaction.response.edit_message.await_args.kwargs["content"]


async def test_post_button_survives_a_missing_permission(monkeypatch):
    """A channel the bot can't write in says so, instead of raising into the
    generic error handler and looking like the answer was lost."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "nope")
    )
    view = advisor_cog._PublicPostView(
        channel=channel, embed=discord.Embed(title="x"), asker_id=7
    )
    interaction = _interaction(_member(manage_messages=True))
    interaction.response.edit_message = AsyncMock()

    await view.post.callback(interaction)

    content = interaction.response.edit_message.await_args.kwargs["content"]
    assert "can't post" in content
    assert all(c.disabled for c in view.children)


async def test_only_the_asker_can_press_post():
    view = advisor_cog._PublicPostView(
        channel=MagicMock(), embed=discord.Embed(title="x"), asker_id=7
    )
    someone_else = _member(administrator=True)
    someone_else.id = 99
    interaction = _interaction(someone_else)
    interaction.response.send_message = AsyncMock()

    assert await view.interaction_check(interaction) is False
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


def test_the_question_titles_every_answer():
    """Public or private, the card says what it is answering."""
    private = advisor_cog._answer_embed(
        question="how do I earn coins?",
        answer="1. Run /daily.",
        assistant_name="Billy-bot",
        color=None,
    )
    assert private.title == "🤖 how do I earn coins?"
    assert private.footer.text.startswith("Billy-bot")
    assert "/ask" not in private.description  # no nudge on a private reply

    public = advisor_cog._answer_embed(
        question="how do I earn coins?",
        answer="1. Run /daily.",
        assistant_name="Billy-bot",
        color=None,
        asker=_member(),
    )
    assert public.footer.text.startswith("Asked by Mod Molly • Billy-bot")
    assert "Ask your own question with `/ask`." in public.description


def test_a_long_question_fits_discords_title_cap():
    embed = advisor_cog._answer_embed(
        question="how do I " + "really " * 60 + "earn coins?",
        answer="1. Run /daily.",
        assistant_name="Billy-bot",
        color=None,
    )
    assert len(embed.title) <= 256
    assert embed.title.endswith("…")
