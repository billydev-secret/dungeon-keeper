"""Shared fixtures for cog-level tests."""

from __future__ import annotations

from unittest.mock import patch

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


@pytest.fixture(autouse=True)
def no_contact_absent():
    """Default every cog test to "these two have no no-contact entry".

    Cog tests run against stub db paths (``:memory:``, a mocked
    ``interaction.client``), so the no-contact gate has no table to read and
    would raise. Patching the *service* module rather than each cog's import
    of it covers every gated cog at once — they all do
    ``from bot_modules.services import no_contact_service`` and call
    attribute-style, so one patch reaches all of them, and a newly gated cog
    is covered without adding a sixth copy of this fixture.

    Tests that want the gate to fire patch it themselves; an inner ``patch``
    wins over this one (see tests/cogs/test_guess_no_contact.py). The gate's
    real behaviour is covered in tests/test_no_contact_service.py, and its
    wiring in tests/cogs/test_guess_no_contact.py.
    """
    with (
        patch(
            "bot_modules.services.no_contact_service.check_and_record",
            return_value=False,
        ),
        patch(
            "bot_modules.services.no_contact_service.no_contact_partners",
            return_value=set(),
        ),
        patch(
            "bot_modules.services.no_contact_service.is_no_contact",
            return_value=False,
        ),
    ):
        yield
