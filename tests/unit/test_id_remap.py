"""Tier 1 unit tests: ID remap matching logic."""

import logging


from id_remap import match_channel, match_role


# ── match_channel ─────────────────────────────────────────────────────

def _dev_channels(*entries):
    return [
        {"id": e[0], "name": e[1], "type": e[2], "parent_name": e[3] if len(e) > 3 else None}
        for e in entries
    ]


def test_exact_match_returns_dev_id():
    prod = {"name": "general", "type": "text", "parent_name": "— GENERAL —"}
    dev = _dev_channels((301, "general", "text", "— GENERAL —"))
    assert match_channel(prod, dev) == 301


def test_no_match_returns_none():
    prod = {"name": "secret-channel", "type": "text", "parent_name": "— MOD —"}
    dev = _dev_channels((301, "general", "text", "— GENERAL —"))
    assert match_channel(prod, dev) is None


def test_ambiguous_match_returns_none(caplog):
    prod = {"name": "general", "type": "text", "parent_name": "— GENERAL —"}
    dev = _dev_channels(
        (301, "general", "text", "— GENERAL —"),
        (302, "general", "text", "— GENERAL —"),
    )
    result = match_channel(prod, dev)
    assert result is None
    assert "ambiguous" in caplog.text.lower()


def test_loose_match_when_parent_differs(caplog):
    """Falls back to (name, type) match when parent differs."""
    prod = {"name": "audit-log", "type": "text", "parent_name": "— MOD —"}
    dev = _dev_channels((401, "audit-log", "text", "— LOGS —"))
    with caplog.at_level(logging.INFO, logger="dungeonkeeper.id_remap"):
        result = match_channel(prod, dev)
    assert result == 401
    assert "loose" in caplog.text.lower()


def test_type_mismatch_no_match():
    prod = {"name": "— GENERAL —", "type": "category", "parent_name": None}
    dev = _dev_channels((301, "— GENERAL —", "text", None))
    assert match_channel(prod, dev) is None


# ── match_role ────────────────────────────────────────────────────────

def _dev_roles(*entries):
    return [{"id": e[0], "name": e[1]} for e in entries]


def test_role_exact_match():
    prod = {"name": "Mod", "id": 4001}
    dev = _dev_roles((5001, "Mod"))
    assert match_role(prod, dev) == 5001


def test_role_no_match():
    prod = {"name": "LegacyRole", "id": 4002}
    dev = _dev_roles((5001, "Mod"))
    assert match_role(prod, dev) is None


def test_role_ambiguous_returns_none(caplog):
    prod = {"name": "Mod", "id": 4001}
    dev = _dev_roles((5001, "Mod"), (5002, "Mod"))
    result = match_role(prod, dev)
    assert result is None
    assert "ambiguous" in caplog.text.lower()
