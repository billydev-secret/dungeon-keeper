"""Rules Watch backtest runner.

Replays archived messages through the guard model and prints flagged ones to
stdout.  Intended as a one-off calibration tool — run before tuning weights,
read the output by eye, then decide whether the raw model is surfacing sensible
candidates.

Usage examples:

  # Raw guard model only, last 30 days, random sample of 500 messages
  python -m scripts.rules_watch_backtest --sample 500

  # Full pipeline (guard + scorer), specific channel, specific date range
  python -m scripts.rules_watch_backtest --scorer --channel 123456789 \\
      --since 2026-01-01 --until 2026-04-01

  # Raw model, a single channel, print everything (flagged and ok)
  python -m scripts.rules_watch_backtest --channel 123456789 --verbose

Options:
  --db PATH           Path to the SQLite database (default: auto-detect)
  --model PATH        Path to GGUF model file (default: read from DB config)
  --since DATE        Start date (YYYY-MM-DD, inclusive)
  --until DATE        End date (YYYY-MM-DD, inclusive)
  --channel ID        Limit to one channel
  --sample N          Randomly sample N messages (default: all in range)
  --scorer            Also apply the priority scorer and show tier
  --verbose           Print ok results too, not just flags
  --ctx-window N      Messages of context around each candidate (default: 8)
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _load_model(model_path: str):
    """Load a llama-cpp model synchronously. Returns the Llama instance."""
    try:
        from llama_cpp import Llama
    except ImportError:
        print("ERROR: llama-cpp-python is not installed.", file=sys.stderr)
        sys.exit(1)

    print(f"Loading model: {model_path}", file=sys.stderr)
    t0 = time.monotonic()
    model = Llama(
        model_path=model_path,
        n_ctx=4096,
        n_gpu_layers=int(__import__("os").getenv("LLAMA_N_GPU_LAYERS", "0")),
        n_threads=int(__import__("os").getenv("LLAMA_N_THREADS", "4")),
        verbose=False,
    )
    print(f"Model ready in {time.monotonic() - t0:.1f}s", file=sys.stderr)
    return model


def _resolve_model_path(db_path: Path, explicit: str | None) -> str:
    if explicit:
        return explicit
    try:
        from bot_modules.services.ollama_client import get_config
        model_path, _, _ = get_config(db_path)
        if model_path:
            return model_path
    except Exception:
        pass
    # Fallback to default location
    default = PROJECT_ROOT / "models" / "Llama-3.2-3B-Instruct-Q4_K_M.gguf"
    if default.exists():
        return str(default)
    print("ERROR: Could not find model. Use --model PATH.", file=sys.stderr)
    sys.exit(1)


def _find_db() -> Path:
    candidates = [
        PROJECT_ROOT / "dungeonkeeper.db",
        PROJECT_ROOT / "data" / "dungeonkeeper.db",
    ]
    for p in candidates:
        if p.exists():
            return p
    print("ERROR: Could not find database. Use --db PATH.", file=sys.stderr)
    sys.exit(1)


def _build_window(conn: sqlite3.Connection, guild_id: int, channel_id: int,
                  anchor_ts: float, window_size: int) -> list[str]:
    """Fetch window_size messages around anchor_ts and format them for the prompt."""
    half = window_size // 2
    before = conn.execute(
        """
        SELECT m.message_id, m.author_id, m.content, m.reply_to_id, m.ts,
               ku.display_name
        FROM messages m
        LEFT JOIN known_users ku ON ku.user_id = m.author_id AND ku.guild_id = m.guild_id
        WHERE m.guild_id = ? AND m.channel_id = ? AND m.ts <= ? AND m.content != ''
        ORDER BY m.ts DESC LIMIT ?
        """,
        (guild_id, channel_id, anchor_ts, half + 1),
    ).fetchall()
    after = conn.execute(
        """
        SELECT m.message_id, m.author_id, m.content, m.reply_to_id, m.ts,
               ku.display_name
        FROM messages m
        LEFT JOIN known_users ku ON ku.user_id = m.author_id AND ku.guild_id = m.guild_id
        WHERE m.guild_id = ? AND m.channel_id = ? AND m.ts > ? AND m.content != ''
        ORDER BY m.ts ASC LIMIT ?
        """,
        (guild_id, channel_id, anchor_ts, half),
    ).fetchall()

    rows = list(reversed(before)) + list(after)
    id_to_author = {r["message_id"]: r["author_id"] for r in rows}
    lines = []
    for r in rows:
        ts = datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%H:%M")
        name = r["display_name"] or f"User {r['author_id']}"
        text = (r["content"] or "").replace("\n", " ")[:400]
        reply_note = ""
        if r["reply_to_id"] and r["reply_to_id"] in id_to_author:
            reply_note = f" [↩ {id_to_author[r['reply_to_id']]}]"
        lines.append(f"[{ts}] {name}{reply_note}: {text}")
    return lines


def _guard_check(model, system_prompt: str, window_lines: list[str]) -> dict:
    """Run the guard model and parse the JSON result."""
    user_content = "\n".join(window_lines)
    result = model.create_chat_completion(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        max_tokens=256,
        temperature=0.0,
    )
    raw = result["choices"][0]["message"]["content"].strip()
    try:
        data = json.loads(raw)
        verdict = str(data.get("verdict", "ok")).lower()
        if verdict not in ("flag", "ok"):
            verdict = "ok"
        return {
            "verdict": verdict,
            "rule": str(data["rule"]) if data.get("rule") else None,
            "reason": str(data.get("reason") or ""),
            "confidence": float(data.get("confidence", 0.0)),
            "raw": raw,
        }
    except Exception:
        return {"verdict": "ok", "rule": None, "reason": "", "confidence": 0.0, "raw": raw}


def _print_flag(row, guard, window_lines, scorer_result=None):
    ts = datetime.fromtimestamp(row["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    author = row["display_name"] or f"User {row['author_id']}"
    print(f"\n{'─' * 72}")
    print(f"FLAG  msg={row['message_id']}  ch={row['channel_id']}  ts={ts}")
    print(f"  Author : {author}")
    print(f"  Rule   : {guard['rule'] or '?'}  conf={guard['confidence']:.0%}")
    print(f"  Reason : {guard['reason']}")
    if scorer_result:
        print(f"  Score  : {scorer_result.score:.1f}  tier={scorer_result.tier}")
        print(f"  Why    : {scorer_result.reason}")
    print("  Window :")
    for line in window_lines:
        print(f"    {line}")


def _run_seeds(model, system_prompt: str, seeds_path: Path) -> None:
    """Run each seed scenario and report recall."""
    seeds = json.loads(seeds_path.read_text(encoding="utf-8"))
    expected_flags = [s for s in seeds if s.get("rule") is not None]
    expected_ok    = [s for s in seeds if s.get("rule") is None]

    print(f"\n{'═' * 72}")
    print(f"SEED SCENARIOS  ({len(seeds)} total — "
          f"{len(expected_flags)} should flag, {len(expected_ok)} should be ok)")
    print(f"{'═' * 72}")

    caught = 0
    false_pos = 0

    for seed in seeds:
        is_violation = seed.get("rule") is not None
        guard = _guard_check(model, system_prompt, seed["window"])
        flagged = guard["verdict"] == "flag"

        if is_violation and flagged:
            caught += 1
            status = "✅ CAUGHT"
        elif is_violation and not flagged:
            status = "❌ MISSED"
        elif not is_violation and flagged:
            false_pos += 1
            status = "⚠️  FALSE POS"
        else:
            status = "✓  OK"

        label = f"[Rule {seed['rule']}]" if seed.get("rule") else "[control]"
        print(f"\n{status}  {seed['id']}  {label}  {seed['severity']}")
        print(f"  Desc   : {seed['description']}")
        print(f"  Guard  : verdict={guard['verdict']}  rule={guard['rule'] or '?'}  "
              f"conf={guard['confidence']:.0%}")
        if guard["reason"]:
            print(f"  Reason : {guard['reason']}")
        if status in ("❌ MISSED", "⚠️  FALSE POS"):
            print("  Window :")
            for line in seed["window"]:
                print(f"    {line}")

    print(f"\n{'─' * 72}")
    recall = caught / len(expected_flags) if expected_flags else 0.0
    fp_rate = false_pos / len(expected_ok) if expected_ok else 0.0
    print(f"Seed recall     : {caught}/{len(expected_flags)} = {recall:.0%}  "
          f"(violations caught)")
    print(f"Seed FP rate    : {false_pos}/{len(expected_ok)} = {fp_rate:.0%}  "
          f"(controls falsely flagged)")
    print(f"{'─' * 72}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Replay archived messages through the Rules Watch guard model."
    )
    parser.add_argument("--db", help="Path to dungeonkeeper.db")
    parser.add_argument("--model", help="Path to GGUF model file")
    parser.add_argument("--since", help="Start date YYYY-MM-DD (inclusive)")
    parser.add_argument("--until", help="End date YYYY-MM-DD (inclusive)")
    parser.add_argument("--channel", type=int, help="Limit to one channel ID")
    parser.add_argument("--guild", type=int, help="Guild ID (default: read from config)")
    parser.add_argument("--sample", type=int, help="Randomly sample N messages")
    parser.add_argument("--scorer", action="store_true", help="Also run the priority scorer")
    parser.add_argument("--verbose", action="store_true", help="Print ok results too")
    parser.add_argument("--ctx-window", type=int, default=8, dest="ctx_window",
                        help="Context window size (default: 8)")
    parser.add_argument("--seeds", metavar="PATH",
                        help="JSON file of seed violation scenarios to test recall "
                             "(default: scripts/rules_watch_seeds.json if it exists)")
    parser.add_argument("--seeds-only", action="store_true",
                        help="Only run seed scenarios, skip real-message evaluation")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else _find_db()

    # For --seeds-only we still need the model, but don't need the DB to exist/be migrated
    model_path = _resolve_model_path(db_path, args.model)

    # Parse date range
    since_ts: float = 0.0
    until_ts: float = time.time()
    if args.since:
        since_ts = datetime.strptime(args.since, "%Y-%m-%d").replace(
            tzinfo=timezone.utc).timestamp()
    if args.until:
        until_ts = datetime.strptime(args.until, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc).timestamp()

    conn = _open_db(db_path)

    # Read guild_id from config if not specified
    guild_id = args.guild or 0
    if not guild_id and not args.seeds_only:
        try:
            row = conn.execute(
                "SELECT value FROM config WHERE key = 'guild_id' ORDER BY guild_id LIMIT 1"
            ).fetchone()
            guild_id = int(row["value"]) if row else 0
        except Exception:
            pass

    # Load guard model system prompt + model (needed for both seeds and real messages)
    from bot_modules.services.ai_moderation_service import _RULES_WATCH_SYSTEM as GUARD_PROMPT

    scorer_module = None
    if args.scorer:
        from bot_modules.rules_watch import scorer as scorer_module

    model = _load_model(model_path)

    # Resolve and run seeds first
    seeds_path: Path | None = None
    if args.seeds:
        seeds_path = Path(args.seeds)
    else:
        default_seeds = Path(__file__).parent / "rules_watch_seeds.json"
        if default_seeds.exists():
            seeds_path = default_seeds

    if seeds_path:
        _run_seeds(model, GUARD_PROMPT, seeds_path)

    if args.seeds_only:
        conn.close()
        return

    # Fetch candidate messages
    where_parts = [
        "m.ts >= ?", "m.ts <= ?",
        "m.content != ''", "m.content IS NOT NULL",
        "(SELECT is_bot FROM known_users ku WHERE ku.user_id = m.author_id AND ku.guild_id = m.guild_id LIMIT 1) != 1",
    ]
    params: list = [since_ts, until_ts]

    if guild_id:
        where_parts.append("m.guild_id = ?")
        params.append(guild_id)
    if args.channel:
        where_parts.append("m.channel_id = ?")
        params.append(args.channel)

    where_clause = " AND ".join(where_parts)
    rows = conn.execute(
        f"""
        SELECT m.message_id, m.author_id, m.channel_id, m.guild_id,
               m.content, m.ts, ku.display_name
        FROM messages m
        LEFT JOIN known_users ku ON ku.user_id = m.author_id AND ku.guild_id = m.guild_id
        WHERE {where_clause}
        ORDER BY m.ts ASC
        """,
        params,
    ).fetchall()

    if not rows:
        print("No messages found in the given range.", file=sys.stderr)
        sys.exit(0)

    if args.sample and args.sample < len(rows):
        rows = random.sample(rows, args.sample)
        rows.sort(key=lambda r: r["ts"])

    print(f"Evaluating {len(rows)} messages…", file=sys.stderr)

    flagged = 0
    ok = 0
    errors = 0

    for i, row in enumerate(rows, 1):
        if i % 50 == 0:
            print(f"  {i}/{len(rows)} evaluated — {flagged} flagged so far…",
                  file=sys.stderr)

        try:
            window_lines = _build_window(
                conn, row["guild_id"], row["channel_id"],
                row["ts"], args.ctx_window,
            )
            if not window_lines:
                continue

            guard = _guard_check(model, GUARD_PROMPT, window_lines)

            scorer_result = None
            if args.scorer and guard["verdict"] == "flag" and scorer_module:
                from bot_modules.rules_watch.scorer import Signals, compute_priority
                # Minimal signals — no target identification in backtest mode
                sigs = Signals(
                    guard_verdict=guard["verdict"],
                    guard_confidence=guard["confidence"],
                )
                scorer_result = compute_priority(sigs)

            if guard["verdict"] == "flag":
                flagged += 1
                _print_flag(row, guard, window_lines, scorer_result)
            else:
                ok += 1
                if args.verbose:
                    ts = datetime.fromtimestamp(row["ts"], tz=timezone.utc).strftime("%H:%M")
                    name = row["display_name"] or f"User {row['author_id']}"
                    print(f"ok  [{ts}] {name}: {(row['content'] or '')[:80]}")

        except Exception as exc:
            errors += 1
            print(f"ERROR on msg {row['message_id']}: {exc}", file=sys.stderr)

    print(f"\n{'═' * 72}", file=sys.stderr)
    print(f"Done.  Flagged: {flagged}  OK: {ok}  Errors: {errors}  "
          f"Flag rate: {flagged / max(flagged + ok, 1):.1%}", file=sys.stderr)

    conn.close()


if __name__ == "__main__":
    main()
