"""The one piece of `/ask` glue that is itself a control, not just wiring.

The Apply click is documented as the prompt-injection defence, so the admin has
to be able to read the whole of what they are confirming. The button label
truncates at 80 characters and the embed description is the model's own prose —
so the proposal fields the cog appends are the only complete, non-model-authored
account of a pending write. Everything else in this cog is exercised through
the service layer.
"""

from __future__ import annotations

import discord

from bot_modules.cogs.advisor_cog import _proposal_fields
from bot_modules.services.advisor_actions import ConfigProposal


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
