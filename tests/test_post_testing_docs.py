"""Which queue edits the post-commit hook treats as new.

The hook posts into a live channel, so a false positive re-posts a test that was
already signed off. Every case here is an edit the queue actually receives in
normal use: entries land as "(this commit)", get their sha rewritten later, have
their bodies corrected, and finally move to Done with a date.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "post_testing_docs.py"

BEFORE = """# Testing Queue

## Pending

### Widget — does a thing  (this commit)

- [ ] check the widget

## Done

_(none yet)_
"""


@pytest.fixture
def mod(monkeypatch: pytest.MonkeyPatch):
    spec = importlib.util.spec_from_file_location("post_testing_docs", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # An empty ledger isolates the diff logic: anything reported as new here is
    # new on the strength of the diff alone, not because state happened to hide it.
    monkeypatch.setattr(module, "load_state", set)
    return module


def at_commit(mod, after: str) -> list[str]:
    def fake_git(*args: str) -> str:
        return {
            ("show", "x:docs/TESTING_QUEUE.md"): after,
            ("show", "x^:docs/TESTING_QUEUE.md"): BEFORE,
        }.get(args, "ok")

    mod.git = fake_git
    return mod.new_entries("x")


def test_new_entry_is_posted(mod) -> None:
    after = BEFORE.replace(
        "## Pending\n",
        "## Pending\n\n### Gadget — brand new  (this commit)\n\n- [ ] check it\n",
        1,
    )
    entries = at_commit(mod, after)
    assert [mod.entry_key(e) for e in entries] == ["gadget — brand new"]


def test_sha_rewrite_is_not_a_new_entry(mod) -> None:
    """A later commit swaps "(this commit)" for the real sha."""
    assert at_commit(mod, BEFORE.replace("(this commit)", "(a1b2c3d)")) == []


def test_body_edit_is_not_a_new_entry(mod) -> None:
    assert at_commit(mod, BEFORE.replace("- [ ] check the widget", "- [ ] check it well")) == []


@pytest.mark.parametrize(
    "done_heading",
    [
        "### Widget — does a thing — verified 2026-07-20",  # date outside parens
        "### Widget — does a thing  (verified 2026-07-20)",  # date inside parens
    ],
)
def test_moving_to_done_never_reposts(mod, done_heading: str) -> None:
    """Signing an entry off must not push it back into the channel.

    The doc asks for a date when an item moves to Done; a date outside the
    trailing parenthetical changes the heading the entry is keyed on, so this
    only stays silent because Done is never scanned.
    """
    after = f"""# Testing Queue

## Pending

_(nothing pending)_

## Done

{done_heading}

- [ ] check the widget
"""
    assert at_commit(mod, after) == []


def test_every_chunk_fits_discords_limit(mod) -> None:
    """The real docs, chunked -- a message over 2000 chars is rejected outright."""
    for name in mod.DOCS:
        for chunk in mod.plan(name):
            assert len(chunk) <= 2000, f"{name}: {len(chunk)} char chunk"
