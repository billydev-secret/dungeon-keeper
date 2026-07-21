"""QA card rendering — embed + component dicts for a test entry.

Pure and stdlib-only on purpose: the cog wraps the embed dict with
``discord.Embed.from_dict`` while the stage-2 post-commit hook posts the
same dicts verbatim over raw REST, so this module must import neither
discord nor sqlite3. Callers hand in plain dicts (``dict(row)`` a
sqlite3.Row first).

Status colors are semantic (pass/fail/blocked) — explicitly exempt from
the ``resolve_accent_color`` branding convention.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

# Discord's hard cap on an embed description.
DESCRIPTION_LIMIT = 4096

STATUS_COLORS = {
    "pending": 0x95A5A6,   # gray
    "passed": 0x23A55A,    # green — services/embeds.py COLOR_GREEN (kept literal:
    "failed": 0xF23F43,    # red   — this module stays import-free for the REST hook)
    "blocked": 0xE67E22,   # amber
    "archived": 0x7F8C8D,  # dark gray
}

VERDICT_EMOJI = {"pass": "✅", "fail": "❌", "blocked": "🚧"}

# Leading markdown checkbox list markers (`- [ ]` / `- [x]`): the boxes are
# the buttons' job now, so the body renders them as plain bullets.
_CHECKBOX_RE = re.compile(r"^(\s*)- \[[ xX]\][ \t]*", re.MULTILINE)


def _normalise_body(body_md: str) -> str:
    text = _CHECKBOX_RE.sub(r"\1• ", body_md).strip()
    if len(text) > DESCRIPTION_LIMIT:
        text = text[: DESCRIPTION_LIMIT - 1].rstrip() + "…"
    return text


def _footer_text(test: Mapping[str, Any]) -> str:
    parts: list[str] = []
    sha = test.get("commit_sha")
    if sha:
        parts.append(str(sha)[:7])
    subject = test.get("commit_subject")
    if subject:
        parts.append(str(subject))
    return " · ".join(parts)


def _unix(iso: str) -> int | None:
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except ValueError:
        return None


def build_card_embed(
    test: Mapping[str, Any], verdicts: Sequence[Mapping[str, Any]]
) -> dict:
    """Render a test row + its verdict rows into an embed dict.

    ``verdicts`` may include voided rows (``list_verdicts`` returns them for
    audit); only un-voided ones count toward the tally.
    """
    status = str(test.get("status") or "pending")
    live = [v for v in verdicts if not v.get("voided_at")]
    tally = {name: 0 for name in VERDICT_EMOJI}
    for v in live:
        verdict = str(v.get("verdict"))
        if verdict in tally:
            tally[verdict] += 1

    embed: dict = {
        "title": str(test.get("title") or ""),
        "description": _normalise_body(str(test.get("body_md") or "")),
        "color": STATUS_COLORS.get(status, STATUS_COLORS["pending"]),
        "fields": [
            {
                "name": "Verdicts",
                "value": " · ".join(
                    f"{VERDICT_EMOJI[name]} {tally[name]}" for name in VERDICT_EMOJI
                ),
                "inline": False,
            }
        ],
    }

    footer = _footer_text(test)
    if footer:
        embed["footer"] = {"text": footer}

    if status == "passed" and test.get("verified_by"):
        value = f"<@{int(test['verified_by'])}>"
        ts = _unix(str(test["verified_at"])) if test.get("verified_at") else None
        if ts is not None:
            value += f" · <t:{ts}:R>"
        embed["fields"].append(
            {"name": "Verified by", "value": value, "inline": False}
        )

    return embed


def build_card_components(test_id: int) -> list[dict]:
    """One action row with the three verdict buttons.

    custom_ids follow ``qa:v:<test_id>:<verdict>`` — the cog's DynamicItem
    templates dispatch on exactly these, so hook-posted cards work too.
    """
    return [
        {
            "type": 1,  # action row
            "components": [
                {
                    "type": 2,  # button
                    "style": 3,  # success
                    "label": "Passed",
                    "emoji": {"name": "✅"},
                    "custom_id": f"qa:v:{test_id}:pass",
                },
                {
                    "type": 2,
                    "style": 4,  # danger
                    "label": "Failed",
                    "emoji": {"name": "❌"},
                    "custom_id": f"qa:v:{test_id}:fail",
                },
                {
                    "type": 2,
                    "style": 2,  # secondary
                    "label": "Blocked",
                    "emoji": {"name": "🚧"},
                    "custom_id": f"qa:v:{test_id}:blocked",
                },
            ],
        }
    ]
