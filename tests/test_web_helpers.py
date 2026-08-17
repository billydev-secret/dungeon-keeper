"""Tests for web_server/helpers.py — the shared admin mod-log mirror embed.

The four pins ported from test_voice_master_logic.py when the mirror was
promoted out of voice_master (2026-08-17) and shared with Survivor.
"""

from __future__ import annotations

import discord
import pytest

from web_server.helpers import build_admin_mirror_embed


def _embed(**overrides) -> discord.Embed:
    kwargs = dict(
        domain="🛡️ Voice Control",
        action="force-delete",
        summary="Deleted X.",
        actor_name="mod#1234 (web)",
        actor_id=42,
    )
    kwargs.update(overrides)
    return build_admin_mirror_embed(**kwargs)


@pytest.mark.parametrize(
    "domain", ["🛡️ Voice Control", "🏈 Survivor"], ids=["voice", "survivor"]
)
def test_title_prefixes_domain_and_action(domain):
    embed = _embed(domain=domain, action="force-delete")
    assert embed.title is not None
    assert domain in embed.title
    assert "force-delete" in embed.title


def test_summary_is_description():
    assert _embed(summary="<@1> → <@2>").description == "<@1> → <@2>"


def test_actor_footer_names_and_ids():
    embed = _embed(actor_name="mod#1234 (web)", actor_id=42)
    assert embed.footer.text is not None
    assert "mod#1234 (web)" in embed.footer.text
    assert "42" in embed.footer.text


def test_color_is_semantic_orange():
    # Mod-audit surface: orange stays regardless of guild accent.
    assert _embed().color == discord.Color.orange()
