"""Regression tests for the in-Discord ``/help`` command pages.

``/help`` builds its section pages from a hardcoded list in
``bot_modules.cogs.mod_cog._build_help_pages``. That list has drifted from the
live command set before (dead ``/voice lock``/``hide`` entries after the access
dial collapse; the whole economy missing). These tests lock the everyone-facing
pages against the real command surface so the same drift fails loudly.

The privileged pages (Role Grants / XP Grant / Moderation) are gated behind
``ctx`` permission checks; the stub context below denies them, so only the
member-facing pages render — which is exactly the surface that regressed.
"""

from __future__ import annotations

from typing import Any, cast

from bot_modules.cogs.mod_cog import _build_help_pages


class _DenyCtx:
    """Minimal AppContext stand-in: no privileged pages, no guild config."""

    def can_grant_any_role(self, _interaction) -> bool:
        return False

    def can_use_xp_grant(self, _interaction) -> bool:
        return False

    def is_mod(self, _interaction) -> bool:
        return False


def _pages() -> dict[str, str]:
    pages = _build_help_pages(cast(Any, _DenyCtx()), cast(Any, object()), color=None)
    return {(p.title or ""): (p.description or "") for p in pages}


def _find(pages: dict[str, str], needle: str) -> str:
    for title, body in pages.items():
        if needle in title:
            return body
    raise AssertionError(f"no help page titled ~{needle!r} (have {list(pages)})")


def test_economy_page_present_with_core_commands():
    body = _find(_pages(), "Economy")
    for cmd in ("/bank wallet", "/bank shop", "/bank quests", "/bounty", "/bank pay"):
        assert cmd in body, f"Economy help page missing {cmd}"


def test_bank_mute_described_as_notification_toggle_not_token():
    body = _find(_pages(), "Economy")
    assert "/bank mute" in body
    # It toggles DM notifications — it is NOT a "mute token" perk (that never
    # existed). Guard the old wrong copy from creeping back.
    assert "token" not in body.lower()


def test_voice_page_uses_access_dial_not_dead_commands():
    body = _find(_pages(), "Voice")
    assert "/voice access" in body
    for dead in ("/voice lock", "/voice unlock", "/voice hide", "/voice unhide"):
        assert dead not in body, f"{dead} was removed (collapsed into /voice access)"


def test_games_page_lists_duel_and_lobby_games():
    body = _find(_pages(), "Games Night")
    for cmd in (
        "/games quickdraw challenge",
        "/games hotpotato challenge",
        "/games hotpotatogroup start",
        "/games chicken start",
        "/games musicalchairs start",
        "/games play legitlibs",
        "/games play ffa",
    ):
        assert cmd in body, f"Games Night help page missing {cmd}"


def test_general_page_has_bio_and_recap():
    body = _find(_pages(), "General")
    assert "/bio" in body
    assert "/recap" in body
