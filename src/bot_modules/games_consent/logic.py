"""Pure decision logic for the games-consent cog.

The cog itself is mostly Discord glue — a button view, two slash
commands, an ``on_message`` listener that delegates to
``scan_mentions_for_consent``. The only piece worth extracting is
"row → (label, color, last-updated)" for ``/consent-status`` and the
matching toggle copy for the opt-in/opt-out buttons, so the cog stops
having three near-identical embed bodies open-coded.

Coverage target here is modest by design: the cog is intentionally
thin. The task brief allows reporting "pure Discord glue" instead of
extracting; we extract a small kernel because it removes real string
duplication and gives the success/error copy a single source of truth.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from bot_modules.games.constants import (
    ERROR_COLOR,
    BRAND_COLOR,
    SUCCESS_COLOR,
)

#: Title shown on the ``/consent`` landing embed.
CONSENT_PROMPT_TITLE = "🌸 Consent Settings"

#: Body shown on the ``/consent`` landing embed — purely informational.
CONSENT_PROMPT_BODY = (
    "Record your participation preference for game nights.\n\n"
    "**Opt in** — happy to be included, mentioned, and participate fully.\n"
    "**Opt out** — prefer to observe or participate on your own terms.\n\n"
    "You can change this at any time."
)

#: Footer for the ``/consent`` landing embed.
CONSENT_PROMPT_FOOTER = "Community Games"

#: Color for the neutral landing embed.
CONSENT_PROMPT_COLOR = BRAND_COLOR

#: Footer for the ``/consent-status`` reply.
STATUS_FOOTER = "Use /consent to change your preference."

#: Title for the ``/consent-status`` reply.
STATUS_TITLE = "Consent Status"


def opt_in_summary() -> tuple[str, str, int]:
    """Return ``(title, body, color)`` for the opt-in confirmation."""
    return (
        "✅ Preference Updated",
        "You've **opted in** — recorded as happy to participate fully.",
        SUCCESS_COLOR,
    )


def opt_out_summary() -> tuple[str, str, int]:
    """Return ``(title, body, color)`` for the opt-out confirmation."""
    return (
        "❌ Preference Updated",
        "You've **opted out** — recorded as preferring not to be included.",
        ERROR_COLOR,
    )


def interpret_consent_status(
    row: Mapping[str, Any] | tuple[Any, ...] | None,
) -> tuple[str, int, str]:
    """Return ``(status_label, color, updated_text)`` for ``/consent-status``.

    Accepts either a tuple-shaped row (``(tod_consent, updated_at)``,
    which is what aiosqlite ``Row`` returns for an indexed SELECT) or
    a mapping with the same keys. ``None`` means no record exists yet
    — we report opted-out so the user sees their effective state.

    The 2-tuple shape returned by the cog (label + color) gets a third
    field for the ``last updated`` string so the cog's f-string stays
    a one-liner.
    """
    if row is None:
        return ("❌ **Opted Out** (or no record found)", ERROR_COLOR, "Never")

    consent_value: Any
    updated_value: Any
    if isinstance(row, Mapping):
        consent_value = row.get("tod_consent")
        updated_value = row.get("updated_at")
    else:
        consent_value = row[0] if len(row) > 0 else None
        updated_value = row[1] if len(row) > 1 else None

    if consent_value:
        label = "✅ **Opted In**"
        color = SUCCESS_COLOR
    else:
        label = "❌ **Opted Out** (or no record found)"
        color = ERROR_COLOR
    updated = str(updated_value) if updated_value is not None else "Never"
    return (label, color, updated)


def format_status_description(label: str, updated: str) -> str:
    """Build the description body for the ``/consent-status`` embed."""
    return f"Your current status: {label}\nLast updated: `{updated}`"
