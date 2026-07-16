#!/usr/bin/env python3
"""Post the testing checklists into their dev channels, chunked to fit Discord.

Chunks at ``###`` entry boundaries so a test entry is never split mid-checklist;
entries over the message limit are split again at line boundaries and the
heading is repeated with a ``(cont.)`` marker.

    python scripts/post_testing_docs.py --dry-run          # show the plan
    python scripts/post_testing_docs.py --only testing-queue
    python scripts/post_testing_docs.py --purge --yes      # replace channel contents
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
API = "https://discord.com/api/v10"
UA = "DiscordBot (https://github.com/local/dungeon-keeper, 1.0)"

# Discord's hard cap is 2000; leave room for the "(cont.)" heading we re-add.
LIMIT = 1900

DOCS = {
    "testing-queue": ("docs/TESTING_QUEUE.md", "1527184897775763549"),
    "admin-tests": ("docs/testing/admin_testing_checklist.md", "1527185973065154711"),
    "moderator-tests": ("docs/testing/mod_testing_checklist.md", "1527186000772862112"),
    "user-tests": ("docs/testing/user_testing_checklist.md", "1527186042363449405"),
}


def token() -> str:
    for line in (REPO / ".env").read_text().splitlines():
        if line.startswith("DISCORD_TOKEN_PROD="):
            return line.split("=", 1)[1].strip().strip("\"'").split("#")[0].strip()
    sys.exit("DISCORD_TOKEN_PROD not found in .env")


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


def heading_level(block: str) -> int:
    """Markdown heading depth of a block's first line (0 if it isn't a heading)."""
    first = block.splitlines()[0]
    return len(first) - len(first.lstrip("#")) if first.startswith("#") else 0


def split_entries(text: str) -> list[str]:
    """Split a doc into blocks at each ``##``/``###`` heading.

    The queue is organised as ``## Pending`` + one ``###`` per change, while the
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
    text = (REPO / path).read_text()
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


def state_path() -> Path:
    """Ledger of entry keys already sent, shared by every worktree.

    Lives in the common git dir (not the tree) so it is never committed and so a
    commit made in any worktree sees the same history.
    """
    common = (git("rev-parse", "--git-common-dir") or ".git").strip()
    return (REPO / common).resolve() / "testing_queue_posted.json"


def load_state() -> set[str]:
    p = state_path()
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text()))
    except json.JSONDecodeError, OSError:
        return set()


def save_state(keys: set[str]) -> None:
    state_path().write_text(json.dumps(sorted(keys), indent=0))


def new_entries(sha: str) -> list[str]:
    """Return queue entries that ``sha`` adds relative to its parent.

    Entries are keyed by heading line rather than by body, so fixing a typo in an
    existing entry doesn't re-post it. Headings repeat across the file (many read
    "(this commit)"), so matching is count-aware: a heading already present once
    stays matched once, and only genuinely extra occurrences count as new.
    """
    from collections import Counter

    path = DOCS["testing-queue"][0]
    after = git("show", f"{sha}:{path}")
    if after is None:
        return []  # file didn't exist at this commit
    parent = git("rev-parse", "--verify", f"{sha}^")
    before = git("show", f"{sha}^:{path}") if parent else None
    if before is None:
        return []  # root commit, or file newly added -- treat the dump as baseline

    seen = Counter(entry_key(b) for b in split_entries(before))
    already = load_state()
    fresh: list[str] = []
    for block in pending_entries(after):
        key = entry_key(block)
        if seen[key] > 0:
            seen[key] -= 1
        elif key not in already:
            # Amend/rebase/cherry-pick all re-present the same addition against
            # the same parent, so the diff alone would post it again.
            fresh.append(block)
    return fresh


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
    """Post the entries a single commit adds. Used by the post-commit hook."""
    entries = new_entries(sha)
    if not entries:
        if dry_run:
            print(f"{sha[:8]}: no new TESTING_QUEUE entries")
        return

    channel = DOCS["testing-queue"][1]
    footer = stamp(sha)
    chunks: list[str] = []
    for entry in entries:
        parts = pack(entry)
        parts[-1] = f"{parts[-1]}\n{footer}"
        chunks.extend(parts)

    if dry_run:
        print(f"{sha[:8]}: {len(entries)} new entry(s) -> {len(chunks)} message(s)")
        for entry in entries:
            print(f"  - {heading_of(entry)[4:]}")
        return

    tok = token()
    for chunk in chunks:
        request(
            "POST",
            f"{API}/channels/{channel}/messages",
            tok,
            {
                "content": chunk,
                "allowed_mentions": {"parse": []},
            },
        )
        time.sleep(1.1)

    save_state(load_state() | {entry_key(e) for e in entries})
    for entry in entries:
        print(f"post-commit: posted to #testing-queue -- {heading_of(entry)[4:]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", choices=sorted(DOCS), action="append")
    ap.add_argument(
        "--purge", action="store_true", help="delete the bot's existing messages first"
    )
    ap.add_argument("--yes", action="store_true", help="required to actually post")
    ap.add_argument(
        "--commit", metavar="SHA", help="post only the entries this commit adds"
    )
    ap.add_argument(
        "--seed-state",
        action="store_true",
        help="mark every entry currently in the queue as already posted (baseline)",
    )
    args = ap.parse_args()

    targets = args.only or list(DOCS)

    if args.seed_state:
        text = (REPO / DOCS["testing-queue"][0]).read_text()
        keys = {
            entry_key(b)
            for b in split_entries(text)
            if heading_of(b).startswith("### ")
        }
        save_state(keys)
        print(f"seeded {len(keys)} entry key(s) as already posted -> {state_path()}")
        return

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
        chunks = plan(name)
        if args.purge:
            gone = purge(channel, tok, me)
            print(f"#{name}: purged {gone} old message(s)")
        print(f"#{name}: posting {len(chunks)} message(s) from {path}")
        for i, chunk in enumerate(chunks, 1):
            request(
                "POST",
                f"{API}/channels/{channel}/messages",
                tok,
                {
                    "content": chunk,
                    "allowed_mentions": {"parse": []},
                },
            )
            print(f"  [{i}/{len(chunks)}]", end="\r", flush=True)
            time.sleep(1.1)
        print(f"  done: {len(chunks)} posted        ")


if __name__ == "__main__":
    main()
