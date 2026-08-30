"""Shared fixtures for cog-level tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bot_modules.services import no_contact_service


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
def no_contact_absent(monkeypatch):
    """Default every cog test to "these two have no no-contact entry".

    Cog tests run against stub db paths (``:memory:``, a mocked
    ``interaction.client``), so the no-contact gate has no table to read and
    would raise.

    Reach, precisely: this patches attributes on the *service module*, so it
    covers the cogs that call attribute-style after
    ``from bot_modules.services import no_contact_service`` — whisper, AMA,
    guess, confessions. It does **not** reach callers that bind the name at
    import (``dm_perms_cog``, ``pen_pals_cog``, ``voice_master_service`` use
    ``from … import is_no_contact`` / ``is_no_contact_conn`` /
    ``no_contact_partners_conn``); those run against a real migrated DB in
    their own tests, which is why they pass without stubbing.

    Tests that want the gate to fire patch it themselves; an inner ``patch``
    wins over this one (see tests/cogs/test_guess_no_contact.py). Because this
    is autouse across tests/cogs/, a new test that means to exercise a gate
    must patch it explicitly rather than relying on the default.

    Applied through ``monkeypatch`` rather than ``mock.patch`` so it shares one
    undo stack with the tests. A test that reaches for
    ``monkeypatch.setattr(no_contact_service, "is_no_contact", ...)`` records
    *whatever is there at the time* as the original — which is this stub — and
    a separate ``mock.patch`` teardown could then run first, put the real
    function back, and let monkeypatch write the stub back over it, for the
    rest of the session. That is not hypothetical: it left the real
    no-contact gate mocked out for every later test in the same worker, and
    tests/test_no_contact_service.py failed with a MagicMock where the gate
    should have been. Sharing the stack makes the restores strictly LIFO.
    """
    monkeypatch.setattr(
        no_contact_service, "check_and_record", MagicMock(return_value=False)
    )
    monkeypatch.setattr(
        no_contact_service, "no_contact_partners", MagicMock(return_value=set())
    )
    monkeypatch.setattr(
        no_contact_service, "is_no_contact", MagicMock(return_value=False)
    )
