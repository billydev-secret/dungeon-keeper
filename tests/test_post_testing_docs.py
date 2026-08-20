"""Two independent card sources feed the same QA card renderer.

Per-commit cards come from a commit's own message (a ``Testing:`` section),
parsed fresh each time — no cross-commit diffing, no state ledger. A hook
re-run for the same sha (a retry, a rebase replay) is idempotent purely via
the DB's ``(guild_id, entry_key, commit_sha)`` unique index. Only commits
landing straight on main take that path: a merge posts nothing, because the
branch it lands is a feature and a feature gets one card, assembled from
everything it ever shipped when the session is torn down.

The role checklists are unrelated: static per-feature ``###`` blocks dumped
via ``--only``, unaffected by the per-commit path.
"""

from __future__ import annotations

import importlib.util
import json
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
    conn.executescript(MIGRATION.read_text(encoding="utf-8"))
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


def test_configured_channel_is_read_from_this_guild_only(
    mod, monkeypatch, qa_db
) -> None:
    """config is per-guild: another server's qa_channel_id must be ignored,
    or a multi-guild install cross-posts the checklist to the wrong server."""
    set_configured_channel(qa_db, "999000999000999549", guild_id=GUILD_ID + 1)
    set_configured_channel(qa_db, "555000555000555549")  # ours, inserted second
    calls = wire(mod, monkeypatch, qa_db)

    mod.post_commit("x", dry_run=False)

    posts = [
        u for m, u, p in calls if m == "POST" and p is not None and "embeds" in p
    ]
    assert posts == [f"{mod.API}/channels/555000555000555549/messages"]


def test_unset_channel_setting_falls_back_to_hardcoded(mod, monkeypatch, qa_db) -> None:
    """No config table at all (the tmp DB default) → DEFAULT_QA_CHANNEL."""
    calls = wire(mod, monkeypatch, qa_db)

    mod.post_commit("x", dry_run=False)

    assert message_posts(mod, calls)


# ── merge commits: dk-ship's --no-ff merge expands to the branch side ────


SUBJECT_2 = "Widget: retune it"


def wire_merge(mod, monkeypatch, db, c2_body: str):
    """A --no-ff merge "m" of branch commits c1 (has a checklist) and c2."""
    calls = wire(mod, monkeypatch, db)

    def fake_git(*args: str) -> str:
        return {
            ("log", "-1", "--format=%P", "m"): "p1 p2",
            ("rev-list", "--reverse", "--no-merges", "p1..m"): "c1\nc2\n",
            ("log", "-1", "--format=%B", "m"): "Merge branch 'widget'\n",
            ("log", "-1", "--format=%P", "c1"): "p0",
            ("log", "-1", "--format=%B", "c1"): COMMIT_BODY,
            ("log", "-1", "--format=%s", "c1"): SUBJECT,
            ("rev-parse", "--short", "c1"): "c1c1c1c",
            ("log", "-1", "--format=%P", "c2"): "c1",
            ("log", "-1", "--format=%B", "c2"): c2_body,
            ("log", "-1", "--format=%s", "c2"): SUBJECT_2,
            ("rev-parse", "--short", "c2"): "c2c2c2c",
        }.get(args, "")

    monkeypatch.setattr(mod, "git", fake_git)
    return calls


def test_a_merge_posts_nothing_at_merge_time(mod, monkeypatch, qa_db) -> None:
    """The branch's card is written once, at teardown — not per merge.

    A branch ships as many times as the work needs (survivor-review landed
    ten), so a card per merge is what filled the queue with 442 cards a month.
    """
    calls = wire_merge(
        mod, monkeypatch, qa_db,
        c2_body=f"{SUBJECT_2}\n\nWhy.\n\nTesting:\n- [ ] spin it\n",
    )

    mod.post_commit("m", dry_run=False)

    assert rows(qa_db) == []
    assert message_posts(mod, calls) == []


def test_merged_commits_still_expands_a_merge(mod, monkeypatch, qa_db) -> None:
    """The expansion itself is unchanged — the branch card path uses it."""
    wire_merge(mod, monkeypatch, qa_db, c2_body=f"{SUBJECT_2}\n")

    assert mod.merged_commits("m") == ["c1", "c2"]


def test_merged_commits_passes_a_plain_commit_through(mod) -> None:
    git_returning(mod, None)  # every git call fails -> fall back to the sha
    assert mod.merged_commits("x") == ["x"]


def test_merged_commits_falls_back_when_rev_list_fails(mod) -> None:
    def fake_git(*args: str) -> str | None:
        return {("log", "-1", "--format=%P", "m"): "p1 p2"}.get(args)

    mod.git = fake_git
    assert mod.merged_commits("m") == ["m"]


# ── branch cards: one card per feature, written at session teardown ──────


@pytest.mark.parametrize(
    "subject, expected",
    [
        pytest.param("Merge branch 'survivor-review'", "survivor-review", id="single"),
        pytest.param('Merge branch "todo-triage"', "todo-triage", id="double"),
        pytest.param("Merge branch quest-review", "quest-review", id="bare"),
        pytest.param("Merge branch 'website-ux' into main", "website-ux", id="into"),
        pytest.param(
            "Merge branch 'huge-review' (2 passes, ~50 fixes)", "huge-review", id="paren"
        ),
        pytest.param(
            "Merge setup-quest-pinning: economy sources review",
            "setup-quest-pinning",
            id="colon-described",
        ),
        pytest.param("Merge fix/quote-spacing top to tail", "fix/quote-spacing", id="slash"),
        pytest.param("Economy: pay the host bounty", None, id="not-a-merge"),
        pytest.param("Merge", None, id="bare-word"),
    ],
)
def test_branch_from_merge_subject(mod, subject, expected) -> None:
    assert mod.branch_from_merge_subject(subject) == expected


BRANCH = "widget"
SUBJECT_3 = "Widget: polish it"


def wire_branch(mod, monkeypatch, db, *, merges: str, extra: dict | None = None):
    """Two merges of ``widget``: m1 landing c1, m2 landing c2 (and c3 if given)."""
    calls = wire(mod, monkeypatch, db)
    table = {
        ("log", "--first-parent", "--merges", f"-n{mod.MERGE_SCAN_DEPTH}",
         "--format=%H%x1f%s", "HEAD"): merges,
        ("log", "-1", "--format=%P", "m1"): "p0 b1",
        ("rev-list", "--reverse", "--no-merges", "p0..m1"): "c1\n",
        ("log", "-1", "--format=%P", "m2"): "m1 b2",
        ("rev-list", "--reverse", "--no-merges", "m1..m2"): "c2\n",
        ("log", "-1", "--format=%B", "c1"): COMMIT_BODY,
        ("log", "-1", "--format=%s", "c1"): SUBJECT,
        ("rev-parse", "--short", "c1"): "c1c1c1c",
        ("log", "-1", "--format=%B", "c2"): f"{SUBJECT_2}\n\nWhy.\n\nTesting:\n- [ ] spin it\n",
        ("log", "-1", "--format=%s", "c2"): SUBJECT_2,
        ("rev-parse", "--short", "c2"): "c2c2c2c",
    }
    table.update(extra or {})
    monkeypatch.setattr(mod, "git", lambda *args: table.get(args, ""))
    monkeypatch.setattr(mod, "rewrite_card", lambda *a, **k: None)  # raw by default
    return calls


TWO_MERGES = "m2\x1fMerge branch 'widget'\nm1\x1fMerge branch 'widget'\n"


def test_branch_card_is_one_card_for_every_merge(mod, monkeypatch, qa_db) -> None:
    """Two ships of one branch produce a single card keyed on the branch."""
    calls = wire_branch(mod, monkeypatch, qa_db, merges=TWO_MERGES)

    mod.post_branch_card(BRANCH, dry_run=False)

    (row,) = rows(qa_db)
    assert row["entry_key"] == BRANCH
    assert row["commit_subject"] == BRANCH
    assert row["commit_sha"] == "c2c2c2c"  # the latest thing it shipped
    # Both ships' checklists are on the one card, oldest first.
    assert "- [ ] check it" in row["body_md"]
    assert "- [ ] spin it" in row["body_md"]
    assert row["body_md"].index("check it") < row["body_md"].index("spin it")
    assert len(message_posts(mod, calls)) == 1


def test_branch_card_ignores_other_branches_merges(mod, monkeypatch, qa_db) -> None:
    merges = "m2\x1fMerge branch 'other-thing'\nm1\x1fMerge branch 'widget'\n"
    wire_branch(mod, monkeypatch, qa_db, merges=merges)

    mod.post_branch_card(BRANCH, dry_run=False)

    (row,) = rows(qa_db)
    assert "check it" in row["body_md"]
    assert "spin it" not in row["body_md"]


def test_branch_card_collapses_a_replayed_commit(mod, monkeypatch, qa_db) -> None:
    """A rebase replay lands the same work under a new sha — one item, not two."""
    merges = TWO_MERGES
    extra = {
        ("log", "-1", "--format=%B", "c2"): COMMIT_BODY,   # same subject+checklist
        ("log", "-1", "--format=%s", "c2"): SUBJECT,
    }
    wire_branch(mod, monkeypatch, qa_db, merges=merges, extra=extra)

    mod.post_branch_card(BRANCH, dry_run=False)

    (row,) = rows(qa_db)
    assert row["body_md"].count("check it") == 1


def test_branch_card_dedupes_a_repeated_step(mod, monkeypatch, qa_db) -> None:
    """Different commits, same literal step: the raw body carries it once."""
    extra = {
        ("log", "-1", "--format=%B", "c2"): (
            f"{SUBJECT_2}\n\nWhy.\n\nTesting:\n- [ ] check it\n- [ ] spin it\n"
        ),
    }
    wire_branch(mod, monkeypatch, qa_db, merges=TWO_MERGES, extra=extra)

    mod.post_branch_card(BRANCH, dry_run=False)

    (row,) = rows(qa_db)
    assert row["body_md"].count("check it") == 1
    assert row["body_md"].count("spin it") == 1


def test_branch_with_no_checklists_posts_nothing(mod, monkeypatch, qa_db) -> None:
    """A refactor, a docs pass or a dep bump earns no card, by design."""
    extra = {
        ("log", "-1", "--format=%B", "c1"): f"{SUBJECT}\n\nProse only.\n",
        ("log", "-1", "--format=%B", "c2"): f"{SUBJECT_2}\n\nProse only.\n",
    }
    calls = wire_branch(mod, monkeypatch, qa_db, merges=TWO_MERGES, extra=extra)

    mod.post_branch_card(BRANCH, dry_run=False)

    assert rows(qa_db) == []
    assert message_posts(mod, calls) == []


def test_never_merged_branch_posts_nothing(mod, monkeypatch, qa_db) -> None:
    calls = wire_branch(mod, monkeypatch, qa_db, merges="")

    mod.post_branch_card("never-shipped", dry_run=False)

    assert rows(qa_db) == []
    assert message_posts(mod, calls) == []


def test_teardown_rerun_does_not_repost(mod, monkeypatch, qa_db) -> None:
    """Teardown is re-runnable: the second run finds the card already posted."""
    calls = wire_branch(mod, monkeypatch, qa_db, merges=TWO_MERGES)

    mod.post_branch_card(BRANCH, dry_run=False)
    mod.post_branch_card(BRANCH, dry_run=False)

    assert len(rows(qa_db)) == 1
    assert len(message_posts(mod, calls)) == 1


def test_dry_run_posts_nothing_and_never_calls_the_rewrite(
    mod, monkeypatch, qa_db, capsys
) -> None:
    calls = wire_branch(mod, monkeypatch, qa_db, merges=TWO_MERGES)

    def explode(*_a, **_k):
        raise AssertionError("dry run must not call the API")

    monkeypatch.setattr(mod, "rewrite_card", explode)
    mod.post_branch_card(BRANCH, dry_run=True)

    assert rows(qa_db) == []
    assert message_posts(mod, calls) == []
    assert "2 checklist(s) -> 1 card" in capsys.readouterr().out


# ── the rewrite: best-effort, and the raw checklists when it isn't there ──


def test_a_second_card_carries_only_what_the_first_did_not(
    mod, monkeypatch, qa_db
) -> None:
    """The --keep escape hatch: card posted by hand, then the branch ships again.

    Without a bound the second card would repeat the first's steps on top of
    the new ones — and the same bound is what stops a reused branch name
    raking a previous incarnation's long-verified checklists into a new card.
    """
    wire_branch(mod, monkeypatch, qa_db, merges=TWO_MERGES)
    mod.post_branch_card(BRANCH, dry_run=False)

    three = "m3\x1fMerge branch 'widget'\n" + TWO_MERGES
    later = {
        ("log", "-1", "--format=%P", "m3"): "m2 b3",
        ("rev-list", "--reverse", "--no-merges", "m2..m3"): "c3\n",
        ("log", "-1", "--format=%B", "c3"): (
            f"{SUBJECT_3}\n\nWhy.\n\nTesting:\n- [ ] buff it\n"
        ),
        ("log", "-1", "--format=%s", "c3"): SUBJECT_3,
        ("rev-parse", "--short", "c3"): "c3c3c3c",
    }
    wire_branch(mod, monkeypatch, qa_db, merges=three, extra=later)
    mod.post_branch_card(BRANCH, dry_run=False)

    first, second = rows(qa_db)
    assert "check it" in first["body_md"] and "spin it" in first["body_md"]
    assert second["body_md"].count("buff it") == 1
    assert "check it" not in second["body_md"]
    assert "spin it" not in second["body_md"]
    assert second["commit_sha"] == "c3c3c3c"


def test_a_reship_of_already_carded_work_posts_nothing(
    mod, monkeypatch, qa_db
) -> None:
    """Same merges, run again: everything is covered, so no second card."""
    wire_branch(mod, monkeypatch, qa_db, merges=TWO_MERGES)
    mod.post_branch_card(BRANCH, dry_run=False)
    calls = wire_branch(mod, monkeypatch, qa_db, merges=TWO_MERGES)
    mod.post_branch_card(BRANCH, dry_run=False)

    assert len(rows(qa_db)) == 1
    assert message_posts(mod, calls) == []


def test_a_slashed_branch_matches_its_normalized_name(
    mod, monkeypatch, qa_db
) -> None:
    """teardown hands over the normalized name; the merge subject has the real one.

    ``normalize_name`` folds ``/`` and ``_`` to ``-``, so without matching on
    the folded form a branch like ``fix/quote-spacing`` would silently earn no
    card at all.
    """
    merges = "m1\x1fMerge branch 'fix/quote_spacing'\n"
    wire_branch(mod, monkeypatch, qa_db, merges=merges)

    mod.post_branch_card("fix-quote-spacing", dry_run=False)

    (row,) = rows(qa_db)
    assert row["entry_key"] == "fix-quote-spacing"
    assert "check it" in row["body_md"]


def test_rewrite_gives_up_when_thinking_ate_the_budget(mod, monkeypatch) -> None:
    """A max_tokens stop truncates the JSON — fall back rather than half-parse."""
    monkeypatch.setattr(mod, "env_value", lambda _k: "sk-test")

    class Resp:
        def read(self):
            return json.dumps(
                {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "{"}]}
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda *a, **k: Resp())
    assert mod.rewrite_card(BRANCH, [("a1", SUBJECT, "- [ ] x")]) is None


@pytest.mark.parametrize(
    "name, expected",
    [
        pytest.param("fix/quote_spacing", "fix-quote-spacing", id="slash-underscore"),
        pytest.param("Survivor-Review", "survivor-review", id="case"),
        pytest.param("  widget  ", "widget", id="whitespace"),
    ],
)
def test_branch_alias(mod, name, expected) -> None:
    assert mod.branch_alias(name) == expected


def test_rewrite_result_becomes_the_card(mod, monkeypatch, qa_db) -> None:
    wire_branch(mod, monkeypatch, qa_db, merges=TWO_MERGES)
    monkeypatch.setattr(
        mod, "rewrite_card", lambda *a, **k: ("Widget Polish", "- [ ] press it")
    )

    mod.post_branch_card(BRANCH, dry_run=False)

    (row,) = rows(qa_db)
    assert row["title"] == "Widget Polish"
    assert row["body_md"] == "- [ ] press it"


def test_rewrite_failure_falls_back_to_the_raw_checklists(
    mod, monkeypatch, qa_db
) -> None:
    """A dead API key or a network blip must not cost the card its content."""
    wire_branch(mod, monkeypatch, qa_db, merges=TWO_MERGES)

    mod.post_branch_card(BRANCH, dry_run=False)

    (row,) = rows(qa_db)
    assert row["title"] == "Widget"  # humanized branch name
    assert f"**{SUBJECT}**" in row["body_md"]
    assert "- [ ] check it" in row["body_md"]


def test_rewrite_needs_no_key_to_fail_quietly(mod, monkeypatch) -> None:
    monkeypatch.setattr(mod, "env_value", lambda _k: None)
    assert mod.rewrite_card(BRANCH, [("a1", SUBJECT, "- [ ] check it")]) is None


def rewrite_replying(mod, monkeypatch, text: str):
    """Point rewrite_card at a canned Messages API reply."""
    monkeypatch.setattr(mod, "env_value", lambda _k: "sk-test")

    class Resp:
        def read(self):
            return json.dumps({"content": [{"type": "text", "text": text}]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda *a, **k: Resp())


def test_rewrite_parses_the_reply(mod, monkeypatch) -> None:
    rewrite_replying(
        mod, monkeypatch,
        'Sure! {"title": "Widget Polish", "items": ["press it", "check it"]}',
    )

    title, body = mod.rewrite_card(BRANCH, [("a1", SUBJECT, "- [ ] x")])

    assert title == "Widget Polish"
    assert body == "- [ ] press it\n- [ ] check it"


def test_rewrite_caps_the_item_count(mod, monkeypatch) -> None:
    """The cap is the point of the card — a long one is what nobody finishes."""
    items = [f"step {n}" for n in range(20)]
    rewrite_replying(
        mod, monkeypatch, json.dumps({"title": "Widget", "items": items})
    )

    _title, body = mod.rewrite_card(BRANCH, [("a1", SUBJECT, "- [ ] x")])

    assert len(body.splitlines()) == mod.MAX_CARD_ITEMS


def test_rewrite_truncates_an_overlong_title(mod, monkeypatch) -> None:
    rewrite_replying(
        mod, monkeypatch, json.dumps({"title": "T" * 200, "items": ["press it"]})
    )

    title, _body = mod.rewrite_card(BRANCH, [("a1", SUBJECT, "- [ ] x")])

    assert len(title) == 60


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param("no json here at all", id="prose"),
        pytest.param('{"title": "Widget", "items": []}', id="no-items"),
        pytest.param('{"items": ["press it"]}', id="no-title"),
        pytest.param("{not json}", id="malformed"),
        pytest.param("[1, 2, 3]", id="not-an-object"),
    ],
)
def test_rewrite_rejects_a_useless_reply(mod, monkeypatch, reply) -> None:
    rewrite_replying(mod, monkeypatch, reply)
    assert mod.rewrite_card(BRANCH, [("a1", SUBJECT, "- [ ] x")]) is None


def test_rewrite_survives_a_dead_network(mod, monkeypatch) -> None:
    monkeypatch.setattr(mod, "env_value", lambda _k: "sk-test")

    def boom(*_a, **_k):
        raise OSError("no route to host")

    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
    assert mod.rewrite_card(BRANCH, [("a1", SUBJECT, "- [ ] x")]) is None


@pytest.mark.parametrize(
    "branch, expected",
    [
        pytest.param("survivor-review", "Survivor review", id="dashes"),
        pytest.param("fix/quote_spacing", "Fix quote spacing", id="slash-underscore"),
        pytest.param("widget", "Widget", id="one-word"),
    ],
)
def test_humanize_branch(mod, branch, expected) -> None:
    assert mod.humanize_branch(branch) == expected


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
    , encoding="utf-8")
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
    , encoding="utf-8")
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


def set_configured_channel(db: Path, channel_id: str, guild_id: int = GUILD_ID) -> None:
    """Set one guild's qa_channel_id; defaults to the guild the fake REST
    reports as owning DEFAULT_QA_CHANNEL (the install this hook posts for)."""
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS config ("
        "guild_id INTEGER NOT NULL DEFAULT 0, "
        "key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (guild_id, key))"
    )
    conn.execute(
        "INSERT INTO config (guild_id, key, value) VALUES (?, 'qa_channel_id', ?)",
        (guild_id, channel_id),
    )
    conn.commit()
    conn.close()


def test_every_chunk_fits_discords_limit(mod) -> None:
    """The real checklist docs, chunked -- a message over 2000 chars is rejected."""
    for name in mod.DOCS:
        for chunk in mod.plan(name):
            assert len(chunk) <= 2000, f"{name}: {len(chunk)} char chunk"
