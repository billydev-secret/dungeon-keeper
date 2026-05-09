#!/usr/bin/env python3
"""Build a bigram Markov chain from the messages table and write it to JSON.

Usage:
    python scripts/build_markov.py --db path/to/dk.db --out fixtures/markov_chain.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

MIN_CORPUS = 100


def _load_messages(db_path: str) -> list[str]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        # First, check if the source column exists
        cursor = con.execute("PRAGMA table_info(messages)")
        columns = {row[1] for row in cursor.fetchall()}

        if "source" in columns:
            query = """
            SELECT m.content
            FROM messages m
            LEFT JOIN known_users ku
                ON m.author_id = ku.user_id AND m.guild_id = ku.guild_id
            WHERE m.content IS NOT NULL
              AND m.source IS NULL
              AND (ku.is_bot IS NULL OR ku.is_bot = 0)
            """
        else:
            query = """
            SELECT m.content
            FROM messages m
            LEFT JOIN known_users ku
                ON m.author_id = ku.user_id AND m.guild_id = ku.guild_id
            WHERE m.content IS NOT NULL
              AND (ku.is_bot IS NULL OR ku.is_bot = 0)
            """

        rows = con.execute(query).fetchall()
    finally:
        con.close()
    return [r["content"] for r in rows]


def _build_chain(messages: list[str]) -> dict[str, list[str]]:
    chain: dict[str, list[str]] = defaultdict(list)
    for msg in messages:
        words = msg.split()
        if len(words) < 3:
            continue
        for i in range(len(words) - 2):
            key = f"{words[i]} {words[i + 1]}"
            chain[key].append(words[i + 2])
    return dict(chain)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build bigram Markov chain from messages DB")
    parser.add_argument("--db", required=True, help="Path to SQLite DB with messages table")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    messages = _load_messages(args.db)
    valid = [m for m in messages if len(m.split()) >= 3]
    print(f"Loaded {len(messages)} messages, {len(valid)} usable (>=3 words)")

    if len(valid) < MIN_CORPUS:
        raise SystemExit(
            f"Only {len(valid)} usable messages -- need at least {MIN_CORPUS}. "
            "Point --db at a richer database."
        )

    chain = _build_chain(valid)
    out_data = {
        "version": 1,
        "corpus_size": len(valid),
        "chain": chain,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(chain)} bigram states to {out_path}")


if __name__ == "__main__":
    main()
