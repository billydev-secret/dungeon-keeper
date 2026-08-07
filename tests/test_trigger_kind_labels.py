"""The JS trigger-kind label mirror must not drift from Python.

``economy-sources-shared.js`` KIND_LABELS exists as "the JS mirror of
TRIGGER_KINDS ... so the two never drift apart" — but nothing enforced that,
and the 2026-08 Mention Awards review caught it drifted: the kind existed in
Python and was absent from the dropdown, so the quest could not be authored.
This is the enforcement.
"""

from __future__ import annotations

import re
from pathlib import Path

from bot_modules.economy.quests import TRIGGER_KINDS

_JS = Path(__file__).resolve().parent.parent / (
    "src/web_server/static/js/panels/economy-sources-shared.js"
)


def _js_kind_label_keys() -> set[str]:
    text = _JS.read_text(encoding="utf-8")
    start = text.index("export const KIND_LABELS = {")
    block = text[start : text.index("};", start)]
    return set(re.findall(r"^\s{2}(\w+):", block, re.M))


def test_js_kind_labels_mirror_trigger_kinds():
    js = _js_kind_label_keys()
    py = set(TRIGGER_KINDS)
    assert js == py, (
        f"KIND_LABELS drifted from TRIGGER_KINDS — "
        f"missing in JS: {sorted(py - js)}; extra in JS: {sorted(js - py)}"
    )
