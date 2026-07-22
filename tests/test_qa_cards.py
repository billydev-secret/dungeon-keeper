"""Pure tests for the QA card renderer (embed + component dicts)."""
from __future__ import annotations

from datetime import datetime

import pytest

from bot_modules.qa.cards import (
    DESCRIPTION_LIMIT,
    STATUS_COLORS,
    build_card_components,
    build_card_embed,
)


def _test_row(**overrides) -> dict:
    row = {
        "id": 7,
        "guild_id": 9001,
        "entry_key": "My feature",
        "title": "My feature (abc1234)",
        "body_md": "- [ ] first step\n- [x] second step",
        "commit_sha": "abc1234def5678",
        "commit_subject": "Feature: do the thing",
        "status": "pending",
        "verified_by": None,
        "verified_at": None,
        "thread_id": None,
    }
    row.update(overrides)
    return row


def _verdict(verdict: str, *, user_id: int = 1, voided: bool = False) -> dict:
    return {
        "verdict": verdict,
        "user_id": user_id,
        "voided_at": "2026-07-16T00:00:00+00:00" if voided else None,
    }


# ── colors ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "color"),
    [
        ("pending", 0x95A5A6),
        ("passed", 0x23A55A),
        ("failed", 0xF23F43),
        ("blocked", 0xE67E22),
        ("archived", 0x7F8C8D),
    ],
)
def test_color_per_status(status, color):
    verified = {"verified_by": 42, "verified_at": None} if status == "passed" else {}
    embed = build_card_embed(_test_row(status=status, **verified), [])
    assert embed["color"] == color
    assert STATUS_COLORS[status] == color


def test_unknown_status_falls_back_to_pending_gray():
    embed = build_card_embed(_test_row(status="???"), [])
    assert embed["color"] == STATUS_COLORS["pending"]


# ── body normalisation + truncation ──────────────────────────────────────────


def test_checkbox_markers_become_bullets():
    body = "- [ ] first step\n- [x] second step\n  - [X] nested\n- plain dash"
    embed = build_card_embed(_test_row(body_md=body), [])
    assert embed["description"] == (
        "• first step\n• second step\n  • nested\n- plain dash"
    )


def test_description_truncated_with_ellipsis():
    body = "x" * (DESCRIPTION_LIMIT + 500)
    embed = build_card_embed(_test_row(body_md=body), [])
    assert len(embed["description"]) <= DESCRIPTION_LIMIT
    assert embed["description"].endswith("…")


def test_short_description_not_truncated():
    embed = build_card_embed(_test_row(body_md="- [ ] just one line"), [])
    assert embed["description"] == "• just one line"


# ── title + footer ────────────────────────────────────────────────────────────


def test_title_and_footer_from_commit():
    embed = build_card_embed(_test_row(), [])
    assert embed["title"] == "My feature (abc1234)"
    assert embed["footer"]["text"] == "abc1234 · Feature: do the thing"


def test_footer_omitted_without_commit_info():
    embed = build_card_embed(_test_row(commit_sha=None, commit_subject=None), [])
    assert "footer" not in embed


# ── verdict tally ─────────────────────────────────────────────────────────────


def test_tally_counts_unvoided_verdicts_only():
    verdicts = [
        _verdict("pass", user_id=1),
        _verdict("pass", user_id=2),
        _verdict("fail", user_id=3),
        _verdict("blocked", user_id=4, voided=True),  # voided → excluded
    ]
    embed = build_card_embed(_test_row(), verdicts)
    tally = embed["fields"][0]
    assert tally["name"] == "Verdicts"
    assert tally["value"] == "✅ 2 · ❌ 1 · 🚧 0"


def test_tally_zero_with_no_verdicts():
    embed = build_card_embed(_test_row(), [])
    assert embed["fields"][0]["value"] == "✅ 0 · ❌ 0 · 🚧 0"


# ── verified-by field ─────────────────────────────────────────────────────────


def test_verified_field_on_passed_status():
    verified_at = "2026-07-16T12:00:00+00:00"
    unix = int(datetime.fromisoformat(verified_at).timestamp())
    embed = build_card_embed(
        _test_row(status="passed", verified_by=42, verified_at=verified_at),
        [_verdict("pass", user_id=42)],
    )
    field = embed["fields"][1]
    assert field["name"] == "Verified by"
    assert field["value"] == f"<@42> · <t:{unix}:R>"


def test_verified_field_survives_unparseable_timestamp():
    embed = build_card_embed(
        _test_row(status="passed", verified_by=42, verified_at="not-a-date"), []
    )
    assert embed["fields"][1]["value"] == "<@42>"


def test_no_verified_field_when_not_passed():
    embed = build_card_embed(_test_row(status="failed"), [_verdict("fail")])
    assert len(embed["fields"]) == 1


# ── components ────────────────────────────────────────────────────────────────


def test_components_shape_and_custom_ids():
    rows = build_card_components(7)
    assert len(rows) == 1
    row = rows[0]
    assert row["type"] == 1
    buttons = row["components"]
    assert [b["custom_id"] for b in buttons] == [
        "qa:v:7:pass",
        "qa:v:7:fail",
        "qa:v:7:blocked",
    ]
    assert [b["style"] for b in buttons] == [3, 4, 2]  # success, danger, secondary
    assert all(b["type"] == 2 for b in buttons)
    assert [b["label"] for b in buttons] == ["Passed", "Failed", "Blocked"]


def test_cards_module_imports_stdlib_only():
    """The stage-2 hook imports this from a dependency-free script."""
    import ast
    from pathlib import Path

    import bot_modules.qa.cards as cards

    tree = ast.parse(Path(cards.__file__).read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert roots <= {"__future__", "re", "collections", "datetime", "typing"}
