"""Prod-state fixes from the 2026-09-02 games deep review (package P0).

Four one-off writes the review found that no code change can make on its own.
Each is idempotent; the script writes nothing without ``--apply``.

    python -m scripts.games_review_p0            # dry run, prints every change
    python -m scripts.games_review_p0 --apply    # applies all four
    python -m scripts.games_review_p0 --only survivor,mahjong,tags,wyr --apply

The four steps (register ids in docs/reviews/2026-09-02-games-deep-review-findings.md):

* **survivor** (survivor-172): season 3's ``last_slate_week`` and
  ``last_lastcall_week`` were set to 1 by the loop on the first Wednesday and
  Saturday after the season was created, weeks before Week 1 kicks off, so the
  Week-1 slate ping and last-call nudge can never fire. Reset both to 0 on
  every *enrolling* or *active* season whose week-1 games are still in the
  future. The code guard that stops this recurring ships in the same review.
* **mahjong** (mahjong-142, -143): ``mahjong_duel_wall_trim`` is the old
  checklist value 60 on top of a rank-5 short deck, which deals a 17-tile
  Quick Duel; the plan and the dashboard hint both say 0. ``mahjong_fill_bots``
  is on with no two-human floor. Set trim to 0 and fill bots to 0 wherever
  either is set.
* **tags** (trivia-tail-81 / safety-sweep-1): 68 bank rows carry the tag
  ``Nsfw``; the bank filter matches ``nsfw`` exactly, so they serve in
  SFW channels. Lowercase every tag in ``games_question_bank.tags``.
* **wyr** (vote-games-49): all 23 Would-You-Rather bank rows are prose
  ("Would you rather A, or B?") and the parser needs ``A | B``. Rewrite each
  row that has no ``|`` by stripping the prefix and splitting on the last
  ", or " / " or "; rows that do not split cleanly are listed, not touched.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bot_modules.core.db_utils import open_db_immediate  # noqa: E402
from bot_modules.survivor.logic import week_first_kickoff  # noqa: E402

DB_PATH = PROJECT_ROOT / "dungeonkeeper.db"

STEPS = ("survivor", "mahjong", "tags", "wyr")

_WYR_PREFIX = re.compile(r"^\s*would you rather\s+", re.IGNORECASE)
_WYR_SPLIT = re.compile(r",?\s+or\s+", re.IGNORECASE)


def split_wyr(text: str) -> tuple[str, str] | None:
    """``"Would you rather A, or B?"`` -> ``("A", "B")``; ``None`` if it does not
    split into exactly two non-empty halves on the LAST ", or " / " or "."""
    if "|" in text:
        return None
    body = _WYR_PREFIX.sub("", text.strip()).rstrip("?").strip()
    matches = list(_WYR_SPLIT.finditer(body))
    if not matches:
        return None
    m = matches[-1]
    a, b = body[: m.start()].strip(), body[m.end():].strip()
    if not a or not b:
        return None
    return a, b


def step_survivor(conn: sqlite3.Connection, apply: bool) -> int:
    now = time.time()
    rows = conn.execute(
        "SELECT id, guild_id, name, status, season_year, config FROM survivor_seasons "
        "WHERE status IN ('enrolling', 'active')"
    ).fetchall()
    changed = 0
    for r in rows:
        cfg = json.loads(r["config"] or "{}")
        marks = {k: int(cfg.get(k) or 0) for k in ("last_slate_week", "last_lastcall_week")}
        if not any(marks.values()):
            continue
        first_kick = week_first_kickoff(conn, int(r["season_year"]), 1)
        if first_kick is None or first_kick <= now:
            print(f"  survivor: season {r['id']} {r['name']!r} — week 1 already kicked off or "
                  f"not ingested; leaving {marks}")
            continue
        print(f"  survivor: season {r['id']} {r['name']!r} ({r['status']}): {marks} -> 0/0 "
              f"(week-1 kickoff in {(float(first_kick) - now) / 86400:.1f} days)")
        if apply:
            cfg["last_slate_week"] = 0
            cfg["last_lastcall_week"] = 0
            conn.execute(
                "UPDATE survivor_seasons SET config = ? WHERE id = ?",
                (json.dumps(cfg), r["id"]),
            )
        changed += 1
    if not changed:
        print("  survivor: nothing to reset")
    return changed


def step_mahjong(conn: sqlite3.Connection, apply: bool) -> int:
    changed = 0
    for key, wanted in (("mahjong_duel_wall_trim", "0"), ("mahjong_fill_bots", "0")):
        for r in conn.execute(
            "SELECT guild_id, value FROM config WHERE key = ? AND value != ?", (key, wanted)
        ).fetchall():
            print(f"  mahjong: guild {r['guild_id']} {key} {r['value']!r} -> {wanted!r}")
            if apply:
                conn.execute(
                    "UPDATE config SET value = ? WHERE guild_id = ? AND key = ?",
                    (wanted, r["guild_id"], key),
                )
            changed += 1
    if not changed:
        print("  mahjong: both dials already at the reviewed values")
    return changed


def step_tags(conn: sqlite3.Connection, apply: bool) -> int:
    changed = 0
    for r in conn.execute(
        "SELECT question_id, game_type, tags FROM games_question_bank WHERE tags != '[]'"
    ).fetchall():
        try:
            tags = json.loads(r["tags"] or "[]")
        except json.JSONDecodeError:
            print(f"  tags: row {r['question_id']} has unparseable tags {r['tags']!r}; skipped")
            continue
        lowered: list[str] = []
        for t in tags:
            t = str(t).strip().lower()
            if t and t not in lowered:
                lowered.append(t)
        if lowered == tags:
            continue
        print(f"  tags: row {r['question_id']} ({r['game_type']}) {tags} -> {lowered}")
        if apply:
            conn.execute(
                "UPDATE games_question_bank SET tags = ? WHERE question_id = ?",
                (json.dumps(lowered), r["question_id"]),
            )
        changed += 1
    if not changed:
        print("  tags: every tag already lowercase")
    return changed


def step_wyr(conn: sqlite3.Connection, apply: bool) -> int:
    changed = 0
    for r in conn.execute(
        "SELECT question_id, question_text FROM games_question_bank "
        "WHERE game_type = 'wyr' AND instr(question_text, '|') = 0 ORDER BY question_id"
    ).fetchall():
        parts = split_wyr(r["question_text"])
        if parts is None:
            print(f"  wyr: row {r['question_id']} does not split cleanly, left as is: "
                  f"{r['question_text']!r}")
            continue
        new_text = f"{parts[0]} | {parts[1]}"
        print(f"  wyr: row {r['question_id']}: {r['question_text']!r} -> {new_text!r}")
        if apply:
            conn.execute(
                "UPDATE games_question_bank SET question_text = ? WHERE question_id = ?",
                (new_text, r["question_id"]),
            )
        changed += 1
    if not changed:
        print("  wyr: every row already carries a '|'")
    return changed


STEP_FNS = {
    "survivor": step_survivor,
    "mahjong": step_mahjong,
    "tags": step_tags,
    "wyr": step_wyr,
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--only", default=",".join(STEPS),
                    help="comma-separated subset of: " + ", ".join(STEPS))
    args = ap.parse_args(argv)
    steps = [s.strip() for s in args.only.split(",") if s.strip()]
    bad = [s for s in steps if s not in STEP_FNS]
    if bad:
        ap.error(f"unknown step(s): {', '.join(bad)}")

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"{mode} against {args.db}")
    total = run_steps(args.db, steps, apply=args.apply)
    print(f"{total} change(s) {'applied' if args.apply else 'would be applied'}")
    return 0


def run_steps(db: Path, steps: list[str], *, apply: bool) -> int:
    """Run the named steps; a dry run opens the database read-only so it
    cannot write even by accident."""
    total = 0
    if apply:
        with open_db_immediate(db) as conn:
            conn.row_factory = sqlite3.Row
            for s in steps:
                print(f"[{s}]")
                total += STEP_FNS[s](conn, True)
        return total
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        for s in steps:
            print(f"[{s}]")
            total += STEP_FNS[s](conn, False)
    finally:
        conn.close()
    return total


if __name__ == "__main__":
    sys.exit(main())
