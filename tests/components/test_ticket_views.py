"""Tier 2 component tests: ticket View/Button/Modal structural assertions.

These tests verify Discord API constraints without making any network calls:
- Buttons have stable custom_id patterns (required for persistent components)
- Button labels are within Discord's 80-char limit
- Modals have required field constraints
- custom_id is deterministic for a given ticket_id
"""

from __future__ import annotations

import pytest

from commands.jail_commands import (
    TicketCloseButton,
    TicketDeleteButton,
    TicketPanelButton,
    TicketReopenButton,
    _TicketCloseModal,
    _TicketOpenModal,
)


# ── Button custom_id stability ─────────────────────────────────────────

def test_ticket_panel_button_has_static_custom_id():
    btn = TicketPanelButton()
    assert btn.item.custom_id == "ticket_panel:open"


def test_ticket_close_button_custom_id_contains_ticket_id():
    btn = TicketCloseButton(42)
    assert btn.item.custom_id is not None
    assert "42" in btn.item.custom_id
    assert btn.item.custom_id == "ticket_action:close:42"


def test_ticket_reopen_button_custom_id_contains_ticket_id():
    btn = TicketReopenButton(99)
    assert btn.item.custom_id is not None
    assert "99" in btn.item.custom_id
    assert btn.item.custom_id == "ticket_action:reopen:99"


def test_ticket_delete_button_custom_id_contains_ticket_id():
    btn = TicketDeleteButton(7)
    assert btn.item.custom_id is not None
    assert "7" in btn.item.custom_id
    assert btn.item.custom_id == "ticket_action:delete:7"


def test_button_custom_ids_are_deterministic():
    """Same ticket_id always produces same custom_id."""
    assert TicketCloseButton(1).item.custom_id == TicketCloseButton(1).item.custom_id
    assert TicketReopenButton(2).item.custom_id == TicketReopenButton(2).item.custom_id


# ── Button label length ────────────────────────────────────────────────

_ALL_BUTTONS = [
    TicketPanelButton(),
    TicketCloseButton(1),
    TicketReopenButton(1),
    TicketDeleteButton(1),
]


@pytest.mark.parametrize("btn", _ALL_BUTTONS, ids=lambda b: b.item.custom_id or "unknown")
def test_button_label_within_discord_limit(btn):
    """Discord enforces max 80 chars on button labels."""
    assert len(btn.item.label or "") <= 80


# ── Modal TextInput constraints ────────────────────────────────────────

def test_ticket_open_modal_description_max_length():
    modal = _TicketOpenModal()
    assert modal.description.max_length is not None
    assert modal.description.max_length <= 4000


def test_ticket_close_modal_has_reason_field():
    modal = _TicketCloseModal(ticket_id=1)
    assert hasattr(modal, "reason") or hasattr(modal, "note") or len(modal.children) >= 1


def test_ticket_open_modal_title_not_empty():
    assert _TicketOpenModal.title


# ── DynamicItem template patterns ─────────────────────────────────────

def test_close_button_template_matches_custom_id():
    btn = TicketCloseButton(123)
    pattern = TicketCloseButton.__discord_ui_compiled_template__
    assert pattern.fullmatch(btn.item.custom_id or "")


def test_reopen_button_template_matches_custom_id():
    btn = TicketReopenButton(456)
    pattern = TicketReopenButton.__discord_ui_compiled_template__
    assert pattern.fullmatch(btn.item.custom_id or "")


def test_delete_button_template_matches_custom_id():
    btn = TicketDeleteButton(789)
    pattern = TicketDeleteButton.__discord_ui_compiled_template__
    assert pattern.fullmatch(btn.item.custom_id or "")
