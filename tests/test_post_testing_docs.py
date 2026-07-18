"""Which queue edits the post-commit hook treats as new — and how they post.

The hook posts into a live channel, so a false positive re-posts a test that was
already signed off. Every case here is an edit the queue actually receives in
normal use: entries land as "(this commit)", get their sha rewritten later, have
their bodies corrected, and finally move to Done with a date.

Since stage 2, queue entries post as QA cards (embed + verdict buttons backed
by a qa_tests row); the card tests run against a tmp DB with migration 077
applied directly, and the degraded pre-migration path falls back to text.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "post_testing_docs.py"
MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "migrations"
    / "077_qa_tracker.sql"
)

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


# ── stage 2: queue entries post as QA cards ───────────────────────────


GUILD_ID = 424242

AFTER_WITH_GADGET = BEFORE.replace(
    "## Pending\n",
    "## Pending\n\n### Gadget — brand new  (this commit)\n\n- [ ] check it\n",
    1,
)


@pytest.fixture
def qa_db(tmp_path) -> Path:
    """A prod-shaped SQLite file with migration 077 applied directly."""
    path = tmp_path / "prod.db"
    conn = sqlite3.connect(path)
    conn.executescript(MIGRATION.read_text())
    conn.commit()
    conn.close()
    return path


def wire(mod, monkeypatch, db: Path | None, after: str = AFTER_WITH_GADGET):
    """Point the module at fakes: git, REST, .env-derived paths, state."""

    def fake_git(*args: str) -> str:
        return {
            ("show", "x:docs/TESTING_QUEUE.md"): after,
            ("show", "x^:docs/TESTING_QUEUE.md"): BEFORE,
            ("rev-parse", "--verify", "x^"): "parentsha",
            ("rev-parse", "--short", "x"): "abc1234",
            ("log", "-1", "--format=%s", "x"): "Gadget: add it",
            ("rev-parse", "--short", "HEAD"): "headsha",
        }.get(args, "ok")

    calls: list[tuple[str, str, dict | None]] = []
    counter = iter(range(1000, 2000))

    def fake_request(method, url, tok, payload=None):
        calls.append((method, url, payload))
        if method == "GET" and "/channels/" in url and "messages" not in url:
            return {"guild_id": str(GUILD_ID)}
        return {"id": str(next(counter))}

    saved: list[set[str]] = []
    monkeypatch.setattr(mod, "git", fake_git)
    monkeypatch.setattr(mod, "token", lambda: "t")
    monkeypatch.setattr(mod, "request", fake_request)
    monkeypatch.setattr(mod, "db_path", lambda: db)
    monkeypatch.setattr(mod, "save_state", lambda keys: saved.append(set(keys)))
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    return calls, saved


def rows(db: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM qa_tests ORDER BY id").fetchall()
    finally:
        conn.close()


def message_posts(mod, calls) -> list[dict]:
    channel = mod.DOCS["testing-queue"][1]
    return [
        p
        for m, u, p in calls
        if m == "POST" and u == f"{mod.API}/channels/{channel}/messages"
    ]


def test_post_commit_creates_row_and_posts_one_card(mod, monkeypatch, qa_db) -> None:
    calls, saved = wire(mod, monkeypatch, qa_db)

    mod.post_commit("x", dry_run=False)

    (row,) = rows(qa_db)
    assert row["guild_id"] == GUILD_ID
    assert row["entry_key"] == "gadget — brand new"
    assert row["title"] == "Gadget — brand new  (this commit)"
    assert row["body_md"] == "- [ ] check it"
    assert row["commit_sha"] == "abc1234"
    assert row["commit_subject"] == "Gadget: add it"
    assert row["channel_id"] == int(mod.DOCS["testing-queue"][1])
    assert row["message_id"] == 1000  # the posted card's id, written back

    (payload,) = message_posts(mod, calls)
    assert "content" not in payload  # a card, not a text chunk
    buttons = payload["components"][0]["components"]
    assert [b["custom_id"] for b in buttons] == [
        f"qa:v:{row['id']}:pass",
        f"qa:v:{row['id']}:fail",
        f"qa:v:{row['id']}:blocked",
    ]
    embed = payload["embeds"][0]
    assert embed["footer"]["text"] == "abc1234 · Gadget: add it"
    assert payload["allowed_mentions"] == {"parse": []}
    # No reaction plumbing survives: cards superseded the ✅.
    assert not [u for m, u, _ in calls if m == "PUT"]
    assert saved and "gadget — brand new" in saved[-1]


def test_rerun_of_the_same_commit_does_not_duplicate(mod, monkeypatch, qa_db) -> None:
    """Two layers of idempotency: the state ledger, then the DB unique index."""
    calls, saved = wire(mod, monkeypatch, qa_db)

    mod.post_commit("x", dry_run=False)
    assert len(message_posts(mod, calls)) == 1

    # Layer 1 — the ledger (amend/rebase re-presents the same diff): nothing posts.
    monkeypatch.setattr(mod, "load_state", lambda: saved[-1])
    mod.post_commit("x", dry_run=False)
    assert len(message_posts(mod, calls)) == 1

    # Layer 2 — ledger lost: the card re-posts, but ON CONFLICT reuses the row.
    monkeypatch.setattr(mod, "load_state", set)
    mod.post_commit("x", dry_run=False)
    posts = message_posts(mod, calls)
    assert len(posts) == 2
    (row,) = rows(qa_db)  # still exactly one row
    first, second = (p["components"][0]["components"][0]["custom_id"] for p in posts)
    assert first == second == f"qa:v:{row['id']}:pass"


def test_missing_qa_tests_table_falls_back_to_text(
    mod, monkeypatch, tmp_path, capsys
) -> None:
    """A pre-migration DB degrades to the old text posting and exits cleanly."""
    bare = tmp_path / "old.db"
    sqlite3.connect(bare).close()  # exists, but no qa_tests table
    calls, saved = wire(mod, monkeypatch, bare)

    mod.post_commit("x", dry_run=False)

    (payload,) = message_posts(mod, calls)
    assert "embeds" not in payload
    assert payload["content"].endswith("-# `abc1234` · Gadget: add it")
    assert "077" in capsys.readouterr().out  # printed the migration hint
    assert saved and "gadget — brand new" in saved[-1]  # still marked posted


def test_rest_failure_is_contained_and_retried_later(mod, monkeypatch, qa_db) -> None:
    """A dead API prints a warning, exits 0, and leaves the ledger unsaved."""
    calls, saved = wire(mod, monkeypatch, qa_db)

    def dead_request(method, url, tok, payload=None):
        raise SystemExit(f"POST {url} -> 502")

    monkeypatch.setattr(mod, "request", dead_request)
    mod.post_commit("x", dry_run=False)  # must not raise

    assert saved == []  # next commit retries the entry


def full_dump_doc() -> str:
    return """# Testing Queue

Intro prose.

## Pending

### Gadget — brand new  (abc1234)

- [ ] check it

## Done

### Widget — retired — verified 2026-07-01

- [x] checked long ago
"""


def run_main(mod, monkeypatch, only: str) -> None:
    monkeypatch.setattr(
        mod.sys, "argv", ["post_testing_docs.py", "--only", only, "--yes"]
    )
    mod.main()


def test_full_dump_posts_cards_for_pending_and_text_for_done(
    mod, monkeypatch, tmp_path, qa_db
) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "TESTING_QUEUE.md").write_text(full_dump_doc())
    calls, _ = wire(mod, monkeypatch, qa_db)
    monkeypatch.setattr(mod, "REPO", tmp_path)

    run_main(mod, monkeypatch, "testing-queue")

    posts = message_posts(mod, calls)
    cards = [p for p in posts if "embeds" in p]
    texts = [p["content"] for p in posts if "content" in p]
    assert len(cards) == 1
    assert cards[0]["embeds"][0]["title"] == "Gadget — brand new  (abc1234)"
    assert cards[0]["embeds"][0]["footer"]["text"] == "headsha"
    assert any(t.startswith("### Widget — retired") for t in texts)  # Done = text
    assert not [u for m, u, _ in calls if m == "PUT"]  # no reactions anywhere

    (row,) = rows(qa_db)
    assert row["commit_sha"] == "headsha"  # keyed on HEAD for re-run idempotency

    # A second dump reuses the row instead of duplicating it.
    run_main(mod, monkeypatch, "testing-queue")
    assert len(rows(qa_db)) == 1


def test_flat_checklist_dump_stays_plain_text(mod, monkeypatch, tmp_path) -> None:
    """A checklist with no ### feature blocks (the old format) posts as text."""
    checklist_dir = tmp_path / "docs" / "testing"
    checklist_dir.mkdir(parents=True)
    (checklist_dir / "admin_testing_checklist.md").write_text(
        "# Admin checklist\n\n## Section\n\n- [ ] poke the thing\n"
    )
    calls, _ = wire(mod, monkeypatch, None)
    monkeypatch.setattr(mod, "REPO", tmp_path)

    run_main(mod, monkeypatch, "admin-tests")

    channel = mod.DOCS["admin-tests"][1]
    posts = [
        p
        for m, u, p in calls
        if m == "POST" and u == f"{mod.API}/channels/{channel}/messages"
    ]
    assert posts and all("content" in p and "embeds" not in p for p in posts)
    assert not [u for m, u, _ in calls if m == "PUT"]  # reactions removed here too


def test_featured_checklist_posts_cards_to_its_own_channel(
    mod, monkeypatch, tmp_path, qa_db
) -> None:
    """### feature blocks in a checklist post as cards, in that doc's channel,
    with the doc-prefixed entry key — never the queue's configured channel."""
    set_configured_channel(qa_db, "555000555000555549")  # queue override; not ours
    checklist_dir = tmp_path / "docs" / "testing"
    checklist_dir.mkdir(parents=True)
    (checklist_dir / "admin_testing_checklist.md").write_text(
        "# Admin checklist\n\nIntro prose.\n\n## Moderation Config\n\n"
        "### Auto-delete\n\n- [ ] set a rule\n- [ ] remove a rule\n\n"
        "### Hidden Channels\n\n- [ ] hide and restore\n"
    )
    calls, _ = wire(mod, monkeypatch, qa_db)
    monkeypatch.setattr(mod, "REPO", tmp_path)

    run_main(mod, monkeypatch, "admin-tests")

    channel = mod.DOCS["admin-tests"][1]
    posts = [
        p
        for m, u, p in calls
        if m == "POST" and u == f"{mod.API}/channels/{channel}/messages"
    ]
    cards = [p for p in posts if "embeds" in p]
    assert [c["embeds"][0]["title"] for c in cards] == ["Auto-delete", "Hidden Channels"]
    # Nothing went to the queue's configured override channel.
    assert not [
        u for m, u, _ in calls if "555000555000555549" in u and m == "POST"
    ]
    keys = {r["entry_key"] for r in rows(qa_db)}
    assert keys == {"admin-tests: auto-delete", "admin-tests: hidden channels"}


def set_configured_channel(db: Path, channel_id: str) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE config (guild_id INTEGER NOT NULL DEFAULT 0, "
        "key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (guild_id, key))"
    )
    conn.execute(
        "INSERT INTO config (guild_id, key, value) VALUES (1, 'qa_channel_id', ?)",
        (channel_id,),
    )
    conn.commit()
    conn.close()


def test_post_commit_honors_configured_card_channel(mod, monkeypatch, qa_db) -> None:
    """The dashboard's qa_channel_id must be enforced, not a dead setting."""
    set_configured_channel(qa_db, "555000555000555549")
    calls, _ = wire(mod, monkeypatch, qa_db)

    mod.post_commit("x", dry_run=False)

    posts = [
        u for m, u, p in calls if m == "POST" and p is not None and "embeds" in p
    ]
    assert posts == [f"{mod.API}/channels/555000555000555549/messages"]
    (row,) = rows(qa_db)
    assert row["channel_id"] == 555000555000555549  # written back verbatim


def test_unset_channel_setting_falls_back_to_hardcoded(mod, monkeypatch, qa_db) -> None:
    """No config table at all (the tmp DB default) → the DOCS channel."""
    calls, _ = wire(mod, monkeypatch, qa_db)

    mod.post_commit("x", dry_run=False)

    assert message_posts(mod, calls)  # posted to DOCS["testing-queue"]


def test_every_chunk_fits_discords_limit(mod) -> None:
    """The real docs, chunked -- a message over 2000 chars is rejected outright."""
    for name in mod.DOCS:
        for chunk in mod.plan(name):
            assert len(chunk) <= 2000, f"{name}: {len(chunk)} char chunk"
