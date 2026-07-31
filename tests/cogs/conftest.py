"""Shared fixtures for cog-level tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _stub_whisper_name_fn(monkeypatch):
    """Neutralise the whisper cog's display-name prefetch for mocked guilds.

    ``build_name_fn`` reads ``guild.get_member`` and the ``known_users`` table.
    These tests build guilds with bare ``MagicMock()``s, which hand back mock
    members whose ``display_name`` is itself a mock rather than a string — and
    ``escape_markdown`` raises on that. Stubbing at the use-site namespace (the
    same trick these files already use for ``resolve_accent_color``) makes
    embeds fall back to plain ``<@id>`` text.

    The resolver's real fallback chain is covered directly in
    ``tests/test_name_resolver_logic.py``; re-proving it through Discord mocks
    here would be exactly the cog-test bloat CLAUDE.md warns against.
    """
    async def _fake(**kwargs):
        return lambda uid: f"<@{uid}>"

    monkeypatch.setattr("bot_modules.cogs.whisper_cog.build_name_fn", _fake)
