"""Two independent card sources feed the same QA card renderer.

Per-commit cards come from a commit's own message (a ``Testing:`` section),
parsed fresh each time — no cross-commit diffing, no state ledger. A hook
re-run for the same sha (a retry, a rebase replay) is idempotent purely via
the DB's ``(guild_id, entry_key, commit_sha)`` unique index.

The role checklists are unrelated: static per-feature ``###`` blocks dumped
via ``--only``, unaffected by the per-commit path.
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

GUILD_ID = 424242
SUBJECT = "Gadget: add it"
COMMIT_BODY = f"""{SUBJECT}

Adds the gadget feature.

Testing:
- [ ] check it
"""


@pytest.fixture
def mod(monkeypatch: pytest.MonkeyPatch):
    spec = importlib.util.spec_from_file_location("post_testing_docs", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── testing_checklist(): parsing a commit's message body ─────────────────


def git_returning(mod, body: str | None):
    def fake_git(*args: str) -> str | None:
        return {("log", "-1", "--format=%B", "x"): body}.get(args)

    mod.git = fake_git


def test_testing_checklist_extracts_the_section(mod) -> None:
    git_returning(mod, COMMIT_BODY)
    assert mod.testing_checklist("x") == "- [ ] check it"


def test_testing_checklist_is_case_insensitive(mod) -> None:
    git_returning(mod, f"{SUBJECT}\n\ntesting:\n- [ ] check it\n")
    assert mod.testing_checklist("x") == "- [ ] check it"


def test_testing_checklist_absent_returns_none(mod) -> None:
    git_returning(mod, f"{SUBJECT}\n\nJust prose, no checklist.\n")
    assert mod.testing_checklist("x") is None


def test_testing_checklist_empty_section_returns_none(mod) -> None:
    git_returning(mod, f"{SUBJECT}\n\nTesting:\n")
    assert mod.testing_checklist("x") is None


def test_testing_checklist_missing_commit_returns_none(mod) -> None:
    git_returning(mod, None)
    assert mod.testing_checklist("x") is None


# ── post_commit(): building and posting the card ──────────────────────────


@pytest.fixture
def qa_db(tmp_path) -> Path:
    """A prod-shaped SQLite file with migration 077 applied directly."""
    path = tmp_path / "prod.db"
    conn = sqlite3.connect(path)
    conn.executescript(MIGRATION.read_text())
    conn.commit()
    conn.close()
    return path


def wire(mod, monkeypatch, db: Path | None, body: str = COMMIT_BODY):
    """Point the module at fakes: git, REST, .env-derived paths."""

    def fake_git(*args: str) -> str:
        return {
            ("log", "-1", "--format=%B", "x"): body,
            ("log", "-1", "--format=%s", "x"): SUBJECT,
            ("rev-parse", "--short", "x"): "abc1234",
            ("rev-parse", "--short", "HEAD"): "headsha",
        }.get(args, "ok")

    calls: list[tuple[str, str, dict | None]] = []
    counter = iter(range(1000, 2000))

    def fake_request(method, url, tok, payload=None):
        calls.append((method, url, payload))
        if method == "GET" and "/channels/" in url and "messages" not in url:
            return {"guild_id": str(GUILD_ID)}
        return {"id": str(next(counter))}

    monkeypatch.setattr(mod, "git", fake_git)
    monkeypatch.setattr(mod, "token", lambda: "t")
    monkeypatch.setattr(mod, "request", fake_request)
    monkeypatch.setattr(mod, "db_path", lambda: db)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    return calls


def rows(db: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM qa_tests ORDER BY id").fetchall()
    finally:
        conn.close()


def message_posts(mod, calls) -> list[dict]:
    channel = mod.DEFAULT_QA_CHANNEL
    return [
        p
        for m, u, p in calls
        if m == "POST" and u == f"{mod.API}/channels/{channel}/messages"
    ]


def test_no_testing_section_posts_nothing(mod, monkeypatch, qa_db) -> None:
    calls = wire(mod, monkeypatch, qa_db, body=f"{SUBJECT}\n\nNo checklist here.\n")

    mod.post_commit("x", dry_run=False)

    assert calls == []
    assert rows(qa_db) == []


def test_post_commit_creates_row_and_posts_one_card(mod, monkeypatch, qa_db) -> None:
    calls = wire(mod, monkeypatch, qa_db)

    mod.post_commit("x", dry_run=False)

    (row,) = rows(qa_db)
    assert row["guild_id"] == GUILD_ID
    assert row["entry_key"] == "gadget: add it"
    assert row["title"] == SUBJECT
    assert row["body_md"] == "- [ ] check it"
    assert row["commit_sha"] == "abc1234"
    assert row["commit_subject"] == SUBJECT
    assert row["channel_id"] == int(mod.DEFAULT_QA_CHANNEL)
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
    assert embed["footer"]["text"] == f"abc1234 · {SUBJECT}"
    assert payload["allowed_mentions"] == {"parse": []}
    assert not [u for m, u, _ in calls if m == "PUT"]


def test_rerun_of_the_same_commit_does_not_duplicate(mod, monkeypatch, qa_db) -> None:
    """A hook re-run for the same sha (retry, rebase replay) reuses the row —
    ``ON CONFLICT DO NOTHING`` is the only idempotency layer needed now."""
    calls = wire(mod, monkeypatch, qa_db)

    mod.post_commit("x", dry_run=False)
    mod.post_commit("x", dry_run=False)

    posts = message_posts(mod, calls)
    assert len(posts) == 2  # the card message re-posts...
    (row,) = rows(qa_db)  # ...but still exactly one row
    first, second = (p["components"][0]["components"][0]["custom_id"] for p in posts)
    assert first == second == f"qa:v:{row['id']}:pass"


def test_missing_qa_tests_table_falls_back_to_text(
    mod, monkeypatch, tmp_path, capsys
) -> None:
    """A pre-migration DB degrades to the old text posting and exits cleanly."""
    bare = tmp_path / "old.db"
    sqlite3.connect(bare).close()  # exists, but no qa_tests table
    calls = wire(mod, monkeypatch, bare)

    mod.post_commit("x", dry_run=False)

    (payload,) = message_posts(mod, calls)
    assert "embeds" not in payload
    assert payload["content"].endswith(f"-# `abc1234` · {SUBJECT}")
    assert "077" in capsys.readouterr().out  # printed the migration hint


def test_rest_failure_leaves_a_row_the_next_run_completes(
    mod, monkeypatch, qa_db
) -> None:
    """A dead card-post prints a warning and exits 0. The DB insert already
    committed, so the row is left without a message id, and a retry reuses
    it (via ON CONFLICT) rather than duplicating."""
    wire(mod, monkeypatch, qa_db)

    def dying_request(method, url, tok, payload=None):
        if method == "GET" and "/channels/" in url and "messages" not in url:
            return {"guild_id": str(GUILD_ID)}
        raise SystemExit(f"POST {url} -> 502")

    monkeypatch.setattr(mod, "request", dying_request)
    mod.post_commit("x", dry_run=False)  # must not raise

    (row,) = rows(qa_db)
    assert row["message_id"] is None

    calls = wire(mod, monkeypatch, qa_db)  # REST recovers
    mod.post_commit("x", dry_run=False)

    assert len(rows(qa_db)) == 1  # same row, not a duplicate
    (row,) = rows(qa_db)
    assert row["message_id"] is not None
    assert len(message_posts(mod, calls)) == 1


def test_post_commit_honors_configured_card_channel(mod, monkeypatch, qa_db) -> None:
    """The dashboard's qa_channel_id must be enforced, not a dead setting."""
    set_configured_channel(qa_db, "555000555000555549")
    calls = wire(mod, monkeypatch, qa_db)

    mod.post_commit("x", dry_run=False)

    posts = [
        u for m, u, p in calls if m == "POST" and p is not None and "embeds" in p
    ]
    assert posts == [f"{mod.API}/channels/555000555000555549/messages"]
    (row,) = rows(qa_db)
    assert row["channel_id"] == 555000555000555549  # written back verbatim


def test_unset_channel_setting_falls_back_to_hardcoded(mod, monkeypatch, qa_db) -> None:
    """No config table at all (the tmp DB default) → DEFAULT_QA_CHANNEL."""
    calls = wire(mod, monkeypatch, qa_db)

    mod.post_commit("x", dry_run=False)

    assert message_posts(mod, calls)


# ── role checklists: unaffected by the per-commit path above ─────────────


def run_main(mod, monkeypatch, only: str) -> None:
    monkeypatch.setattr(
        mod.sys, "argv", ["post_testing_docs.py", "--only", only, "--yes"]
    )
    mod.main()


def test_flat_checklist_dump_stays_plain_text(mod, monkeypatch, tmp_path) -> None:
    """A checklist with no ### feature blocks (the old format) posts as text."""
    checklist_dir = tmp_path / "docs" / "testing"
    checklist_dir.mkdir(parents=True)
    (checklist_dir / "admin_testing_checklist.md").write_text(
        "# Admin checklist\n\n## Section\n\n- [ ] poke the thing\n"
    )
    calls = wire(mod, monkeypatch, None)
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
    set_configured_channel(qa_db, "555000555000555549")  # not this doc's concern
    checklist_dir = tmp_path / "docs" / "testing"
    checklist_dir.mkdir(parents=True)
    (checklist_dir / "admin_testing_checklist.md").write_text(
        "# Admin checklist\n\nIntro prose.\n\n## Moderation Config\n\n"
        "### Auto-delete\n\n- [ ] set a rule\n- [ ] remove a rule\n\n"
        "### Hidden Channels\n\n- [ ] hide and restore\n"
    )
    calls = wire(mod, monkeypatch, qa_db)
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
    # Nothing went to the configured override channel -- that only applies
    # to per-commit cards.
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


def test_every_chunk_fits_discords_limit(mod) -> None:
    """The real checklist docs, chunked -- a message over 2000 chars is rejected."""
    for name in mod.DOCS:
        for chunk in mod.plan(name):
            assert len(chunk) <= 2000, f"{name}: {len(chunk)} char chunk"
