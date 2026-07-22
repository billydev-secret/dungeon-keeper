#!/usr/bin/env python3
"""Post QA cards for commits, and mirror the role testing checklists.

Two independent sources feed the same **QA card** renderer (one embed with
Pass / Fail / Blocked buttons, backed by a ``qa_tests`` row in the
production DB):

1. **Per-commit cards** — the post-commit hook calls ``--commit <sha>``.
   If that commit's message body has a ``Testing:`` section, its checklist
   lines become one card titled with the commit subject. No section, no
   card — most commits don't change live-testable behavior.
2. **Role checklists** — ``###`` blocks in the admin/moderator/user
   testing-checklist docs post as cards too (one per feature, keyed with a
   per-doc prefix like "admin-tests: …" so same-named features can't
   collide), via the ``--only`` full-dump path. Blocks without a ``###``
   heading (doc intros, section headers) still post as plain text, chunked
   at heading boundaries (oversized blocks re-split at line boundaries with
   a ``(cont.)`` heading).

The stage-1 cog dispatches buttons purely on ``custom_id``, so cards posted
here over raw REST come alive after the next bot restart.

Runs under the bare system python3 (the post-commit hook has no venv): only
the stdlib plus ``bot_modules.qa.cards``, which is stdlib-pure by design.
If the prod DB is unreachable or predates migration 077, cards degrade to
plain-text messages, and every hook path still exits 0.

    python scripts/post_testing_docs.py --commit <sha> --dry-run   # one commit's card
    python scripts/post_testing_docs.py --only admin-tests         # dump a checklist
    python scripts/post_testing_docs.py --purge --yes      # replace channel contents
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The card renderer is stdlib-pure on purpose (see its docstring), so this
# import works without the project venv -- exactly what the git hook needs.
sys.path.insert(0, str(REPO / "src"))
from bot_modules.qa.cards import build_card_embed, build_card_components

API = "https://discord.com/api/v10"
UA = "DiscordBot (https://github.com/local/dungeon-keeper, 1.0)"

# Discord's hard cap is 2000; leave room for the "(cont.)" heading we re-add.
LIMIT = 1900

DOCS = {
    "admin-tests": ("docs/testing/admin_testing_checklist.md", "1527185973065154711"),
    "moderator-tests": ("docs/testing/mod_testing_checklist.md", "1527186000772862112"),
    "user-tests": ("docs/testing/user_testing_checklist.md", "1527186042363449405"),
}

# Per-commit QA cards have no doc of their own, so their channel isn't in
# DOCS — this is the fallback #testing-queue channel qa_card_channel() uses
# absent a dashboard-configured override.
DEFAULT_QA_CHANNEL = "1527184897775763549"


def env_value(key: str) -> str | None:
    """Read one ``KEY=value`` line from the checkout's .env."""
    env_file = REPO / ".env"
    if not env_file.exists():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip("\"'").split("#")[0].strip()
    return None


def token() -> str:
    tok = env_value("DISCORD_TOKEN_PROD")
    if not tok:
        sys.exit("DISCORD_TOKEN_PROD not found in .env")
    return tok


def db_path() -> Path | None:
    """The production SQLite file, resolved the way the bot's config does.

    ``bot_modules.core.config.load_config`` reads ``DB_PATH_PROD`` from the
    environment (populated from .env); the service runs with the repo root
    as its working directory, so a relative value resolves against REPO.
    """
    raw = env_value("DB_PATH_PROD")
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_absolute() else REPO / p


def headers(tok: str) -> dict[str, str]:
    return {
        "Authorization": f"Bot {tok}",
        "User-Agent": UA,
        "Content-Type": "application/json",
    }


def request(method: str, url: str, tok: str, payload: dict | None = None) -> dict:
    """Call the API, transparently obeying 429 retry_after."""
    for attempt in range(6):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            url, data=data, headers=headers(tok), method=method
        )
        try:
            with urllib.request.urlopen(req) as resp:
                body = resp.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                retry = json.loads(exc.read() or b"{}").get("retry_after", 1.0)
                time.sleep(float(retry) + 0.3)
                continue
            if exc.code >= 500 and attempt < 5:
                time.sleep(2**attempt)
                continue
            raise SystemExit(f"{method} {url} -> {exc.code}: {exc.read()[:300]!r}")
    raise SystemExit(f"{method} {url}: gave up after repeated rate limits")


def post_message(channel: str, content: str, tok: str) -> str:
    """Post one message, returning its id."""
    resp = request(
        "POST",
        f"{API}/channels/{channel}/messages",
        tok,
        {"content": content, "allowed_mentions": {"parse": []}},
    )
    return resp.get("id", "")


# ── QA cards: qa_tests rows + embed-with-buttons posting ────────────────────


def qa_connect() -> sqlite3.Connection | None:
    """Open the prod DB for card rows; None means degrade to plain text."""
    path = db_path()
    if path is None or not path.exists():
        print(
            "post-commit: WARNING prod DB not found (DB_PATH_PROD in .env) "
            "-- posting plain text"
        )
        return None
    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.execute("SELECT 1 FROM qa_tests LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        print(
            "post-commit: WARNING qa_tests table missing -- apply migration "
            "077_qa_tracker.sql (restart the bot); posting plain text"
        )
        conn.close()
        return None
    return conn


def qa_card_channel(conn: sqlite3.Connection | None) -> str:
    """The channel cards post to: the dashboard's qa_channel_id when set.

    The knob lives in the config KV (written by the stage-3 panel) and must
    be honored here or it's a dead setting. Falls back to the hardcoded
    #testing-queue id when unset, zero, unreadable, or pre-077 — the same
    degraded installs that fall back to plain text.
    """
    default = DEFAULT_QA_CHANNEL
    if conn is None:
        return default
    try:
        row = conn.execute(
            "SELECT value FROM config WHERE key = 'qa_channel_id' "
            "AND CAST(value AS INTEGER) > 0 LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return default
    return str(row[0]) if row else default


_GUILD_IDS: dict[str, int] = {}


def channel_guild_id(channel: str, tok: str) -> int:
    """The guild owning a channel — one REST lookup, cached per run."""
    if channel not in _GUILD_IDS:
        info = request("GET", f"{API}/channels/{channel}", tok)
        _GUILD_IDS[channel] = int(info.get("guild_id") or 0)
    return _GUILD_IDS[channel]


def insert_qa_test(
    conn: sqlite3.Connection,
    guild_id: int,
    key: str,
    title: str,
    body_md: str,
    commit_sha: str | None,
    commit_subject: str | None,
) -> int:
    """Insert a qa_tests row, returning its id (existing or new).

    Raw SQL mirroring ``qa_service.create_test`` — same ON CONFLICT
    idempotency on (guild_id, entry_key, commit_sha), same UTC-ISO
    timestamps. Importing the service itself would drag in the economy
    module chain, too heavy for a bare-python3 git hook.
    """
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """
        INSERT INTO qa_tests
            (guild_id, entry_key, title, body_md, commit_sha, commit_subject,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, entry_key, commit_sha) DO NOTHING
        """,
        (guild_id, key, title, body_md, commit_sha, commit_subject, now, now),
    )
    if (cur.rowcount or 0) > 0:
        conn.commit()
        return int(cur.lastrowid or 0)
    row = conn.execute(
        """
        SELECT id FROM qa_tests
        WHERE guild_id = ? AND entry_key = ? AND commit_sha IS ?
        """,
        (guild_id, key, commit_sha),
    ).fetchone()
    return int(row[0])


def set_qa_test_message(
    conn: sqlite3.Connection, test_id: int, channel_id: int, message_id: int
) -> None:
    """Store the posted card's location back on its row (mirrors the service)."""
    conn.execute(
        "UPDATE qa_tests SET channel_id = ?, message_id = ?, updated_at = ? "
        "WHERE id = ?",
        (channel_id, message_id, datetime.now(timezone.utc).isoformat(), test_id),
    )
    conn.commit()


def card_fields(block: str) -> tuple[str, str]:
    """Card title (heading text, trailing parenthetical kept) + body."""
    lines = block.splitlines()
    title = lines[0].removeprefix("### ").strip()
    body = "\n".join(lines[1:]).strip("\n")
    return title, body


def post_card(
    channel: str,
    tok: str,
    test_id: int,
    title: str,
    body_md: str,
    commit_sha: str | None,
    commit_subject: str | None,
) -> str:
    """Post one verdict card (embed + buttons), returning the message id.

    The sha/subject land in the embed footer via the shared renderer, so a
    card carries no ``-#`` stamp line the way text entries did. Entries stay
    far under the 4096-char embed cap (and the renderer truncates anyway),
    so a card is always exactly one message — no pack() chunking.
    """
    test = {
        "title": title,
        "body_md": body_md,
        "status": "pending",
        "commit_sha": commit_sha,
        "commit_subject": commit_subject,
    }
    resp = request(
        "POST",
        f"{API}/channels/{channel}/messages",
        tok,
        {
            "embeds": [build_card_embed(test, [])],
            "components": build_card_components(test_id),
            "allowed_mentions": {"parse": []},
        },
    )
    return resp.get("id", "")


def post_entry_card(
    conn: sqlite3.Connection,
    guild_id: int,
    channel: str,
    tok: str,
    block: str,
    commit_sha: str | None,
    commit_subject: str | None,
    key_prefix: str = "",
) -> None:
    """Create/reuse the DB row for one entry and post its card.

    ``key_prefix`` namespaces checklist features per doc ("admin-tests: …")
    so a feature named identically in two checklists can't collide on the
    (guild_id, entry_key, commit_sha) unique index. Queue entries stay
    unprefixed — their keys predate the prefix and must keep matching.
    """
    title, body = card_fields(block)
    test_id = insert_qa_test(
        conn, guild_id, key_prefix + entry_key(block), title, body,
        commit_sha, commit_subject,
    )
    mid = post_card(channel, tok, test_id, title, body, commit_sha, commit_subject)
    time.sleep(1.1)
    if mid:
        set_qa_test_message(conn, test_id, int(channel), int(mid))


def heading_level(block: str) -> int:
    """Markdown heading depth of a block's first line (0 if it isn't a heading)."""
    first = block.splitlines()[0]
    return len(first) - len(first.lstrip("#")) if first.startswith("#") else 0


def split_entries(text: str) -> list[str]:
    """Split a doc into blocks at each ``##``/``###`` heading.

    The queue is organized as ``## Pending`` + one ``###`` per change, while the
    role checklists use ``##`` per section -- splitting on both keeps a section
    or entry whole and gives every block a heading of its own.
    """
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if (line.startswith("## ") or line.startswith("### ")) and current:
            blocks.append("\n".join(current).strip("\n"))
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current).strip("\n"))

    # Two same-level headings in a row (the queue has a pair) leave the first as
    # a stray bodyless message, so fold it into the next. A *parent* heading like
    # "## Pending" followed by its own "### entry" must NOT fold -- that would
    # swallow the entry into the wrapper and hide it from the entry scan.
    merged: list[str] = []
    for block in (b for b in blocks if b.strip()):
        if (
            merged
            and len(merged[-1].splitlines()) == 1
            and merged[-1].startswith("#")
            and heading_level(block) <= heading_level(merged[-1])
        ):
            merged[-1] = f"{merged[-1]}\n{block}"
        else:
            merged.append(block)
    return merged


def pack(block: str) -> list[str]:
    """Fit one entry into <=LIMIT chunks, splitting only at line boundaries."""
    if len(block) <= LIMIT:
        return [block]

    lines = block.splitlines()
    heading = lines[0] if lines[0].startswith("#") else ""
    chunks: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            chunks.append("\n".join(buf).strip("\n"))
            buf.clear()

    for line in lines:
        # A single line longer than the limit is pathological; hard-wrap it.
        while len(line) > LIMIT:
            flush()
            chunks.append(line[:LIMIT])
            line = line[LIMIT:]
        candidate = len("\n".join(buf + [line]))
        if candidate > LIMIT:
            flush()
            if heading and chunks:
                buf.append(f"{heading} *(cont.)*")
        buf.append(line)
    flush()
    return chunks


def plan(name: str) -> list[str]:
    path, _ = DOCS[name]
    text = (REPO / path).read_text(encoding="utf-8")
    chunks: list[str] = []
    for block in split_entries(text):
        chunks.extend(pack(block))
    return chunks


def git(*args: str) -> str | None:
    """Run a git command, returning None if it fails (e.g. path not in that commit)."""
    import subprocess

    proc = subprocess.run(
        ["git", "-C", str(REPO), *args], capture_output=True, text=True
    )
    return proc.stdout if proc.returncode == 0 else None


def heading_of(block: str) -> str:
    return block.splitlines()[0].strip()


def entry_key(block: str) -> str:
    """Identity of an entry, ignoring its trailing commit marker.

    Entries land as "... (this commit)" and a later commit rewrites that to the
    real sha, so keying on the raw heading would read every such rewrite as a
    brand-new entry. Dropping the trailing parenthetical keeps identity stable
    across that rename.
    """
    head = heading_of(block).removeprefix("### ")
    return re.sub(r"\s*\([^()]*\)\s*$", "", head).strip().casefold()


def pending_entries(text: str) -> list[str]:
    """The ``###`` entries above the "## Done" heading.

    Verified work is moved down to Done, and the doc asks for a date on it when
    that happens. A date outside the trailing parenthetical changes the heading,
    which would otherwise read as a brand-new entry and re-post a test that was
    just signed off -- so Done is never scanned at all.
    """
    out: list[str] = []
    for block in split_entries(text):
        head = heading_of(block)
        if head.startswith("## ") and head[3:].strip().casefold().startswith("done"):
            break
        if head.startswith("### "):
            out.append(block)
    return out


def testing_checklist(sha: str) -> str | None:
    """The ``Testing:`` checklist block from a commit's message body.

    ``None`` if the commit carries no such section (not every commit changes
    live-testable behavior) or the section is present but empty.
    """
    body = git("log", "-1", "--format=%B", sha)
    if body is None:
        return None
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if line.strip().casefold() == "testing:":
            checklist = "\n".join(lines[i + 1 :]).strip("\n")
            return checklist or None
    return None


def stamp(sha: str) -> str:
    """A footer resolving the doc's ambiguous '(this commit)' to a real commit."""
    short = (git("rev-parse", "--short", sha) or sha[:8]).strip()
    subject = (git("log", "-1", "--format=%s", sha) or "").strip()
    return f"-# `{short}` · {subject}" if subject else f"-# `{short}`"


def purge(channel: str, tok: str, me: str) -> int:
    """Delete this bot's own messages in the channel (newest 1000)."""
    removed = 0
    while True:
        batch = request("GET", f"{API}/channels/{channel}/messages?limit=100", tok)
        mine = [m["id"] for m in batch if m["author"]["id"] == me]
        if not mine:
            return removed
        for mid in mine:
            request("DELETE", f"{API}/channels/{channel}/messages/{mid}", tok)
            removed += 1
            time.sleep(0.35)
        if len(batch) < 100:
            return removed


def post_commit(sha: str, *, dry_run: bool) -> None:
    """Post the QA card for one commit's ``Testing:`` section, if it has one.

    Used by the post-commit hook. Any DB or REST failure prints a warning
    and returns normally — the hook must never break a commit. No state
    ledger is needed: ``insert_qa_test``'s ``ON CONFLICT DO NOTHING`` on
    ``(guild_id, entry_key, commit_sha)`` already makes a hook re-run for
    the same sha (a retried commit, a rebase replay) reuse the existing row
    instead of duplicating it.
    """
    checklist = testing_checklist(sha)
    if not checklist:
        if dry_run:
            print(f"{sha[:8]}: no Testing: section")
        return

    short = (git("rev-parse", "--short", sha) or sha[:8]).strip()
    subject = (git("log", "-1", "--format=%s", sha) or "").strip() or "(no subject)"

    if dry_run:
        print(f"{sha[:8]}: Testing section -> 1 card")
        print(f"  - {subject}")
        return

    try:
        tok = token()
        conn = qa_connect()
        channel = qa_card_channel(conn)
        guild_id = channel_guild_id(channel, tok) if conn is not None else 0
        if conn is not None and not guild_id:
            print(
                "post-commit: WARNING channel has no guild -- posting plain text"
            )
            conn.close()
            conn = None
        if conn is not None:
            key = subject.casefold()
            test_id = insert_qa_test(
                conn, guild_id, key, subject, checklist, short, subject
            )
            mid = post_card(channel, tok, test_id, subject, checklist, short, subject)
            if mid:
                set_qa_test_message(conn, test_id, int(channel), int(mid))
            conn.close()
        else:
            # Degraded path (no DB / pre-077 schema): the old plain-text
            # message(s) with the sha stamp.
            parts = pack(f"### {subject}\n\n{checklist}")
            parts[-1] = f"{parts[-1]}\n{stamp(sha)}"
            for part in parts:
                post_message(channel, part, tok)
                time.sleep(1.1)
    except (Exception, SystemExit) as exc:  # containment: the hook exits 0
        print(f"post-commit: WARNING could not post, will retry next commit -- {exc}")
        return

    print(f"post-commit: posted QA card -- {subject}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", choices=sorted(DOCS), action="append")
    ap.add_argument(
        "--purge", action="store_true", help="delete the bot's existing messages first"
    )
    ap.add_argument("--yes", action="store_true", help="required to actually post")
    ap.add_argument(
        "--commit", metavar="SHA", help="post this commit's Testing: section, if any"
    )
    args = ap.parse_args()

    targets = args.only or list(DOCS)

    if args.commit:
        post_commit(args.commit, dry_run=args.dry_run)
        return

    if args.dry_run:
        grand = 0
        for name in targets:
            chunks = plan(name)
            grand += len(chunks)
            longest = max(len(c) for c in chunks)
            print(
                f"#{name:<16} {len(chunks):>3} messages  (longest {longest} chars)  <- {DOCS[name][0]}"
            )
            over = [c for c in chunks if len(c) > 2000]
            if over:
                print(f"  !! {len(over)} chunk(s) STILL over 2000")
        print(f"\ntotal: {grand} messages across {len(targets)} channel(s)")
        return

    if not args.yes:
        sys.exit("refusing to post without --yes (try --dry-run first)")

    tok = token()
    me = request("GET", f"{API}/users/@me", tok)["id"]

    for name in targets:
        path, channel = DOCS[name]
        text = (REPO / path).read_text(encoding="utf-8")
        # Every ``###`` block above "## Done" becomes a QA card — each
        # checklist feature, since the checklists were regrouped into
        # per-feature ``###`` blocks. A doc with no ``###`` headings simply
        # posts as plain text, one message per ``##`` section (which is also
        # the pre-077 degraded behavior).
        pending = {entry_key(b) for b in pending_entries(text)}
        conn = qa_connect() if pending else None
        # A checklist's cards belong in that checklist's own dev channel, not
        # the dashboard-configurable qa_channel_id (that only applies to
        # per-commit cards from post_commit()).
        card_channel = channel
        key_prefix = f"{name}: "
        guild_id = channel_guild_id(card_channel, tok) if conn is not None else 0
        if conn is not None and not guild_id:
            conn.close()
            conn = None
        # Rows are keyed on the dump's HEAD so a re-run reuses them instead
        # of duplicating; the subject is per-entry history we no longer have,
        # so full-dump cards carry a sha-only footer.
        head_sha = (git("rev-parse", "--short", "HEAD") or "").strip() or None

        if args.purge:
            gone = purge(channel, tok, me)
            print(f"#{name}: purged {gone} old message(s)")
        blocks = split_entries(text)
        print(f"#{name}: posting {len(blocks)} block(s) from {path}")
        sent = 0
        for block in blocks:
            as_card = (
                conn is not None
                and heading_of(block).startswith("### ")
                and entry_key(block) in pending
            )
            if as_card:
                post_entry_card(
                    conn, guild_id, card_channel, tok, block, head_sha, None,
                    key_prefix=key_prefix,
                )
                sent += 1
            else:
                for part in pack(block):
                    post_message(channel, part, tok)
                    time.sleep(1.1)
                    sent += 1
            print(f"  [{sent} sent]", end="\r", flush=True)
        if conn is not None:
            conn.close()
        print(f"  done: {sent} posted        ")


if __name__ == "__main__":
    main()
