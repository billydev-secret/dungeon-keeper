from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_ZERO_WIDTH = re.compile(r"[​-‏‪-‮⁠-⁤﻿]")
_DANGEROUS_PREFIX = re.compile(r"^[@#/]")
_EVERYONE_HERE = re.compile(r"\b(everyone|here)\b", re.IGNORECASE)

DEFAULT_NICK_DENYLIST: list[str] = [
    r"\bn[i1]gg[ae3]r\b",
    r"\bf[a@]gg[o0]t\b",
    r"\br[e3]t[a@]rd\b",
]


@dataclass
class FilterResult:
    ok: bool
    value: str
    reason: str | None


def _clean(text: str) -> str:
    text = _ZERO_WIDTH.sub("", text)
    return unicodedata.normalize("NFC", text).strip()


def validate_nickname(
    raw: str,
    *,
    max_length: int = 32,
    denylist: list[str] | None = None,
    admin_display_names: list[str] | None = None,
    all_member_display_names: list[str] | None = None,
) -> FilterResult:
    """Full nickname filter pipeline. Returns FilterResult(ok, cleaned_value, reason)."""
    cleaned = _clean(raw)
    if not cleaned:
        return FilterResult(ok=False, value=raw, reason="Nickname cannot be blank after cleaning.")
    if len(cleaned) > max_length:
        return FilterResult(
            ok=False, value=raw, reason=f"Nickname must be {max_length} characters or fewer."
        )
    effective_denylist = list(DEFAULT_NICK_DENYLIST) + (denylist or [])
    for pattern in effective_denylist:
        if re.search(pattern, cleaned, re.IGNORECASE):
            return FilterResult(ok=False, value=raw, reason="Nickname contains disallowed content.")
    if _DANGEROUS_PREFIX.search(cleaned):
        return FilterResult(
            ok=False, value=raw, reason="Nickname cannot start with @, #, or /."
        )
    if _EVERYONE_HERE.search(cleaned):
        return FilterResult(
            ok=False, value=raw, reason="Nickname cannot contain 'everyone' or 'here'."
        )
    for name in admin_display_names or []:
        if cleaned.lower() == name.lower():
            return FilterResult(
                ok=False, value=raw, reason="Nickname impersonates a server admin or mod."
            )
    for name in all_member_display_names or []:
        if cleaned.lower() == name.lower():
            return FilterResult(
                ok=False,
                value=raw,
                reason="That display name is already taken by a server member.",
            )
    return FilterResult(ok=True, value=cleaned, reason=None)


def validate_stakes(
    raw: str,
    *,
    max_length: int = 200,
    denylist: list[str] | None = None,
) -> FilterResult:
    """Lighter filter for stakes text — strip, length, and denylist only."""
    cleaned = _clean(raw)
    if len(cleaned) > max_length:
        return FilterResult(
            ok=False, value=raw, reason=f"Stakes must be {max_length} characters or fewer."
        )
    effective_denylist = list(DEFAULT_NICK_DENYLIST) + (denylist or [])
    for pattern in effective_denylist:
        if re.search(pattern, cleaned, re.IGNORECASE):
            return FilterResult(
                ok=False, value=raw, reason="Stakes text contains disallowed content."
            )
    return FilterResult(ok=True, value=cleaned, reason=None)
