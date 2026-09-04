#!/usr/bin/env python3
"""Config keys in the database that no code reads, and vice versa.

Two failure modes, both silent, both seen in this repo:

* **A dead key.** A branch shipped a config key, its reader never merged (or
  was later deleted), and the row sits in prod looking like live configuration.
  ``econ_manager_role_id`` is set in one guild and read nowhere in ``src/``.
  Nothing breaks; the dial simply does nothing, and the next person to read the
  config believes it does something.
The reverse direction — a key the code reads that no guild has set — is
deliberately *not* reported: nothing distinguishes "unset and therefore broken"
from "unset and correctly running on its default", and the draft that tried
printed 467 lines beside the 11 that mattered.

Read-only. Point it at the live database; it never writes.

    python scripts/config_key_audit.py [--db dungeonkeeper.db] [--json]
"""

from __future__ import annotations

import argparse
import ast
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

#: A key is "read" if the source can plausibly produce it. Two mechanisms, and
#: missing the second is what makes a naive scan useless — it reports
#: ``casino_min_bet`` as dead because that string appears nowhere:
#:
#:   1. a literal (``get_config_value(conn, "econ_theme_channel_id", ...)``);
#:   2. a prefix constant plus a settings-dataclass field name
#:      (``CASINO_PREFIX = "casino_"`` + ``fields(CasinoSettings)``).
#:
#: The prefix side is a cross-product, so it over-matches — a key is called
#: alive if *any* prefix pairs with *any* field. That bias is deliberate: this
#: tool is only worth running if its output is short enough to read, so it must
#: err toward silence rather than toward a list nobody finishes.
def _source_files() -> list[Path]:
    return sorted(SRC.rglob("*.py")) + sorted((SRC / "web_server" / "static").rglob("*.js"))


def _source_text(files: list[Path]) -> str:
    return "\n".join(f.read_text(encoding="utf-8", errors="ignore") for f in files)


def _prefixes_and_fields(py_files: list[Path]) -> tuple[set[str], set[str]]:
    """Prefix constants and settings-dataclass field names, via AST.

    A regex cannot do this: `@dataclass` is followed immediately by the `class`
    line, so any "block until the next class" pattern captures nothing.
    """
    prefixes: set[str] = set()
    fields: set[str] = set()
    for path in py_files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id.endswith("PREFIX")
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                prefixes.add(node.value.value)
            if isinstance(node, ast.ClassDef) and any(
                (isinstance(d, ast.Name) and d.id == "dataclass")
                or (isinstance(d, ast.Attribute) and d.attr == "dataclass")
                or (isinstance(d, ast.Call) and getattr(d.func, "id", getattr(d.func, "attr", "")) == "dataclass")
                for d in node.decorator_list
            ):
                for stmt in node.body:
                    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                        fields.add(stmt.target.id)
    return prefixes, fields


def _reachable(stored: set[str], text: str, py_files: list[Path]) -> set[str]:
    prefixes, fields = _prefixes_and_fields(py_files)
    generated = {p + f for p in prefixes for f in fields}
    return {k for k in stored if k in text or k in generated}


def stored_keys(db: Path) -> dict[str, list[int]]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT key, guild_id FROM config").fetchall()
    finally:
        conn.close()
    out: dict[str, list[int]] = {}
    for key, guild_id in rows:
        out.setdefault(str(key), []).append(int(guild_id))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(ROOT / "dungeonkeeper.db"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"no such database: {db}", file=sys.stderr)
        return 2

    files = _source_files()
    text = _source_text(files)
    stored = stored_keys(db)
    alive = _reachable(set(stored), text, [f for f in files if f.suffix == ".py"])
    dead = {k: gs for k, gs in sorted(stored.items()) if k not in alive}

    if args.json:
        print(json.dumps({"dead": dead}, indent=2))
        return 0

    print(f"config keys stored: {len(stored)}   dead: {len(dead)}")
    if dead:
        print("\nDEAD — stored in the database, read nowhere in src/:")
        for key, guilds in dead.items():
            print(f"  {key:42} guild(s): {', '.join(str(g) for g in guilds)}")
        print("\n  Each is a dial that does nothing. Either the reader never "
              "merged, or it was deleted and the row outlived it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
