"""Tests for the extracted games-consent logic module.

Covers ``bot_modules/games_consent/logic.py`` — the row → status
interpretation, the opt-in/opt-out copy constants, and the prompt
copy constants used by the cog. The cog itself is intentionally thin:
a button view and two slash commands with one DB call each, so most
of its surface is Discord glue that doesn't benefit from extraction.
"""

from __future__ import annotations

import pytest

from bot_modules.games.constants import (
    ERROR_COLOR,
    BRAND_COLOR,
    SUCCESS_COLOR,
)
from bot_modules.games_consent.logic import (
    CONSENT_PROMPT_BODY,
    CONSENT_PROMPT_COLOR,
    CONSENT_PROMPT_FOOTER,
    CONSENT_PROMPT_TITLE,
    STATUS_FOOTER,
    STATUS_TITLE,
    format_status_description,
    interpret_consent_status,
    opt_in_summary,
    opt_out_summary,
)


# ── opt_in_summary / opt_out_summary ─────────────────────────────────


def test_opt_in_summary_returns_success_tuple():
    title, body, color = opt_in_summary()
    assert "Updated" in title
    assert "opted in" in body.lower()
    assert color == SUCCESS_COLOR


def test_opt_out_summary_returns_error_tuple():
    title, body, color = opt_out_summary()
    assert "Updated" in title
    assert "opted out" in body.lower()
    assert color == ERROR_COLOR


def test_opt_in_and_opt_out_use_distinct_colors():
    """The two paths must remain visually distinguishable to the user."""
    _, _, in_color = opt_in_summary()
    _, _, out_color = opt_out_summary()
    assert in_color != out_color


# ── interpret_consent_status ─────────────────────────────────────────


def test_interpret_consent_status_none_returns_opted_out():
    """No record == effectively opted out, with 'Never' as the timestamp."""
    label, color, updated = interpret_consent_status(None)
    assert "Opted Out" in label
    assert color == ERROR_COLOR
    assert updated == "Never"


def test_interpret_consent_status_truthy_tuple_returns_opted_in():
    label, color, updated = interpret_consent_status((1, "2026-05-15"))
    assert "Opted In" in label
    assert color == SUCCESS_COLOR
    assert updated == "2026-05-15"


def test_interpret_consent_status_falsy_tuple_returns_opted_out():
    label, color, updated = interpret_consent_status((0, "2026-05-15"))
    assert "Opted Out" in label
    assert color == ERROR_COLOR
    assert updated == "2026-05-15"


def test_interpret_consent_status_accepts_mapping_input():
    """aiosqlite Row supports both __getitem__-by-int and mapping access."""
    row = {"tod_consent": True, "updated_at": "2026-01-01"}
    label, color, updated = interpret_consent_status(row)
    assert "Opted In" in label
    assert color == SUCCESS_COLOR
    assert updated == "2026-01-01"


def test_interpret_consent_status_mapping_with_falsy_consent():
    row = {"tod_consent": False, "updated_at": "2026-01-01"}
    label, color, updated = interpret_consent_status(row)
    assert "Opted Out" in label
    assert color == ERROR_COLOR
    assert updated == "2026-01-01"


def test_interpret_consent_status_mapping_missing_updated_at():
    """A row that somehow lacks updated_at degrades to 'Never' instead of crashing."""
    row = {"tod_consent": True}
    _, _, updated = interpret_consent_status(row)
    assert updated == "Never"


def test_interpret_consent_status_tuple_only_consent_no_updated():
    """A one-element tuple still produces a sensible response."""
    label, color, updated = interpret_consent_status((True,))
    assert "Opted In" in label
    assert color == SUCCESS_COLOR
    assert updated == "Never"


@pytest.mark.parametrize("truthy_value", [1, True, "yes", 7])
def test_interpret_consent_status_treats_truthy_values_as_opted_in(truthy_value):
    label, _, _ = interpret_consent_status((truthy_value, "x"))
    assert "Opted In" in label


@pytest.mark.parametrize("falsy_value", [0, False, "", None])
def test_interpret_consent_status_treats_falsy_values_as_opted_out(falsy_value):
    label, _, _ = interpret_consent_status((falsy_value, "x"))
    assert "Opted Out" in label


# ── format_status_description ────────────────────────────────────────


def test_format_status_description_includes_label_and_updated():
    text = format_status_description("✅ **Opted In**", "2026-05-15")
    assert "Opted In" in text
    assert "2026-05-15" in text


def test_format_status_description_formats_updated_as_code():
    """The timestamp is rendered in backticks for monospace clarity."""
    text = format_status_description("✅", "2026-05-15")
    assert "`2026-05-15`" in text


def test_format_status_description_uses_newline_between_lines():
    text = format_status_description("X", "Y")
    assert "\n" in text


# ── module-level constants ───────────────────────────────────────────


def test_prompt_constants_are_non_empty_strings():
    """The cog reads these directly; empty strings would surface to users."""
    assert CONSENT_PROMPT_TITLE
    assert CONSENT_PROMPT_BODY
    assert CONSENT_PROMPT_FOOTER
    assert isinstance(CONSENT_PROMPT_TITLE, str)
    assert isinstance(CONSENT_PROMPT_BODY, str)
    assert isinstance(CONSENT_PROMPT_FOOTER, str)


def test_prompt_body_explains_both_options():
    """User-facing copy must spell out what 'opt in' and 'opt out' mean."""
    assert "Opt in" in CONSENT_PROMPT_BODY
    assert "Opt out" in CONSENT_PROMPT_BODY


def test_prompt_color_is_golden_meadow():
    """Neutral landing embed uses the cluster's palette."""
    assert CONSENT_PROMPT_COLOR == BRAND_COLOR


def test_status_constants_are_non_empty_strings():
    assert STATUS_TITLE
    assert STATUS_FOOTER
    assert isinstance(STATUS_TITLE, str)
    assert isinstance(STATUS_FOOTER, str)
