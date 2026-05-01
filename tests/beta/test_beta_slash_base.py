"""Tests for beta_tools.slash._base — role-check decorator."""

from __future__ import annotations

from unittest.mock import MagicMock


from beta_tools.slash._base import has_mod_or_admin


def _fake_member(role_names: list[str]):
    member = MagicMock()
    member.roles = [MagicMock(name=name) for name in role_names]
    for r, name in zip(member.roles, role_names):
        r.name = name
    return member


def test_has_mod_or_admin_accepts_mod():
    member = _fake_member(["Member", "Mod"])
    assert has_mod_or_admin(member) is True


def test_has_mod_or_admin_accepts_admin():
    member = _fake_member(["Admin"])
    assert has_mod_or_admin(member) is True


def test_has_mod_or_admin_rejects_regular():
    member = _fake_member(["Member"])
    assert has_mod_or_admin(member) is False


def test_has_mod_or_admin_rejects_no_roles():
    member = _fake_member([])
    assert has_mod_or_admin(member) is False
