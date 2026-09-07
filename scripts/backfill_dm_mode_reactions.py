#!/usr/bin/env python3
"""Grant the DM mode a member picked on MEE6's reaction-role message.

MEE6 runs a reaction-role panel in rabbithole (`꒰-🐰-꒱roles`) bound to the
*same three role IDs* as DK's ``dm_mode_roles`` row for that guild, and every
binding was set to MEE6's **Reverse** mode: reacting *removes* the role and
un-reacting gives it back. The embed above it reads "React to this message to
get your roles!", so members clicked the emoji for the mode they wanted and
got nothing — under Reverse a reaction can never grant to someone who holds no
role, which is why ~76 of them never appear in ``role_events`` at all.

Their reaction is a genuine opt-in: they clicked the emoji labelled with the
mode they wanted, under an embed telling them that would grant it. This script
honours that click.

Switching that panel to **button roles** on 2026-08-19 cleared every reaction
off the message, destroying the live record of who had picked what. The
snapshot taken beforehand (``/home/ben/dk-backfills/``) is the only surviving
copy — pass it with ``--from-snapshot``. Role state is still read live.

Reads **live Discord state** (members + reactions), not ``role_events``: that
table only records what the bot observed while online, and it structurally
cannot see a grant that never happened.

    python scripts/backfill_dm_mode_reactions.py                 # report only
    python scripts/backfill_dm_mode_reactions.py --mode closed   # one mode
    python scripts/backfill_dm_mode_reactions.py --apply         # grant

Default is a dry run. ``--apply`` **only ever adds** a role, and only to
members who currently hold **no** DM-mode role at all. Members already holding
a mode are always skipped, even when their reaction disagrees with it — a
disagreement means two conflicting signals and needs a human, not a script.

Fix MEE6's Reverse setting *before* running with --apply. Backfilling while
the bindings are still reversed leaves every one of these members one click
away from being stripped again.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

API = "https://discord.com/api/v10"
UA = "DiscordBot (https://github.com/local/dungeon-keeper, 1.0)"

#: Discord caps member pagination at 1000 per page.
PAGE = 1000

GUILD_ID = 1476525656115515484
CHANNEL_ID = 1525028461561905233
MESSAGE_ID = 1525051023830421525

#: Custom-emoji id → mode, read off the MEE6 embed's own legend:
#:   :t__stopit: DM CLOSED / :e_hearteyeseyes: DM OPEN / :e_whistle: ASK TO DM
EMOJI_MODE = {
    "t__stopit:1478024255777149000": "closed",
    "e_hearteyeseyes:1480550378239033587": "open",
    "e_whistle:1477686941011935283": "ask",
}

MEE6_ID = "159985870458322944"

REASON = "DM mode backfill: honouring the member's reaction (MEE6 Reverse-mode misfire)"


def env_value(key: str, env_file: Path) -> str | None:
    """Read one ``KEY=value`` line from an .env file."""
    if not env_file.exists():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip("\"'").split("#")[0].strip()
    return None


def headers(tok: str) -> dict[str, str]:
    return {
        "Authorization": f"Bot {tok}",
        "User-Agent": UA,
        "Content-Type": "application/json",
    }


def api(tok: str, method: str, path: str, *, reason: str | None = None) -> object:
    """One Discord API call, retrying through 429s."""
    hdrs = headers(tok)
    if reason:
        hdrs["X-Audit-Log-Reason"] = urllib.parse.quote(reason, safe="")
    for _ in range(8):
        req = urllib.request.Request(f"{API}{path}", method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req) as resp:
                body = resp.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                time.sleep(float(json.load(exc).get("retry_after", 1)) + 0.25)
                continue
            raise
    raise RuntimeError(f"gave up after repeated 429s on {method} {path}")


def mode_role_ids(db: Path, guild_id: int) -> dict[str, int]:
    """The guild's configured mode→role-id map, straight from ``dm_mode_roles``."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT open_role_id, ask_role_id, closed_role_id "
            "FROM dm_mode_roles WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    finally:
        con.close()
    if not row or not all(row):
        sys.exit(f"guild {guild_id} has no complete dm_mode_roles row — nothing to map")
    return {"open": int(row[0]), "ask": int(row[1]), "closed": int(row[2])}


def reactors(tok: str, channel_id: int, message_id: int) -> dict[str, set[str]]:
    """user_id → set of modes they reacted for (MEE6's own seed reaction skipped)."""
    out: dict[str, set[str]] = {}
    for key, mode in EMOJI_MODE.items():
        after = ""
        while True:
            qs = f"?limit=100{f'&after={after}' if after else ''}"
            page = api(
                tok, "GET",
                f"/channels/{channel_id}/messages/{message_id}"
                f"/reactions/{urllib.parse.quote(key)}{qs}",
            )
            assert isinstance(page, list)
            for user in page:
                if user["id"] != MEE6_ID:
                    out.setdefault(user["id"], set()).add(mode)
            if len(page) < 100:
                break
            after = page[-1]["id"]
    return out


def members(tok: str, guild_id: int, role_ids: dict[str, int]) -> dict[str, dict]:
    """user_id → {name, modes} for every current member, from live role state."""
    by_id = {rid: mode for mode, rid in role_ids.items()}
    out: dict[str, dict] = {}
    after = "0"
    while True:
        page = api(tok, "GET", f"/guilds/{guild_id}/members?limit={PAGE}&after={after}")
        assert isinstance(page, list)
        for mem in page:
            out[mem["user"]["id"]] = {
                "name": mem["user"]["username"],
                "modes": {by_id[int(r)] for r in mem["roles"] if int(r) in by_id},
            }
        if len(page) < PAGE:
            break
        after = page[-1]["user"]["id"]
        time.sleep(0.3)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="actually grant the roles (default is a dry run)",
    )
    parser.add_argument(
        "--mode", choices=sorted(EMOJI_MODE.values()),
        help="limit to members who reacted for one mode (e.g. closed)",
    )
    parser.add_argument(
        "--from-snapshot", metavar="PATH",
        help="read the reaction record from a JSON snapshot instead of live "
             "Discord (the reactions were cleared when the panel moved to "
             "buttons on 2026-08-19 — the snapshot is now the only record)",
    )
    parser.add_argument("--guild", type=int, default=GUILD_ID)
    parser.add_argument("--channel", type=int, default=CHANNEL_ID)
    parser.add_argument("--message", type=int, default=MESSAGE_ID)
    args = parser.parse_args()

    env = REPO / ".env"
    tok = env_value("DISCORD_TOKEN_PROD", env)
    db = REPO / (env_value("DB_PATH_PROD", env) or "dungeonkeeper.db")
    if not tok:
        sys.exit("DISCORD_TOKEN_PROD not found in .env")

    role_ids = mode_role_ids(db, args.guild)
    if args.from_snapshot:
        snap = json.loads(Path(args.from_snapshot).read_text(encoding="utf-8"))
        # The snapshot stores modes upper-cased; this script works in the
        # lower-case vocabulary of ``dm_mode_roles`` / ``resolve_mode``.
        react = {uid: {m.lower() for m in modes} for uid, modes in snap["react"].items()}
        print(f"intent read from snapshot: {args.from_snapshot} ({len(react)} members)")
    else:
        react = reactors(tok, args.channel, args.message)
    mem = members(tok, args.guild, role_ids)

    todo: list[tuple[str, str, str]] = []   # (user_id, name, mode)
    held = ambiguous = crossed = 0
    for uid, info in sorted(mem.items(), key=lambda kv: kv[1]["name"].lower()):
        modes = react.get(uid)
        if not modes:
            continue
        if info["modes"]:
            held += 1
            if not (modes & info["modes"]):
                crossed += 1
            continue
        if len(modes) > 1:
            ambiguous += 1
            print(f"  ambiguous, skipped: {info['name']} reacted {sorted(modes)}")
            continue
        mode = next(iter(modes))
        if args.mode and mode != args.mode:
            continue
        todo.append((uid, info["name"], mode))

    print(f"\n{len(mem)} members, {len(react)} reactors")
    print(f"  already hold a mode (skipped): {held}"
          f"  — of which reaction disagrees: {crossed}")
    if ambiguous:
        print(f"  multiple reactions (skipped): {ambiguous}")
    print(f"  to grant: {len(todo)}")
    for mode in sorted(EMOJI_MODE.values()):
        n = sum(1 for _, _, m in todo if m == mode)
        if n:
            print(f"      {mode.upper():<7} {n}")
    print()
    for _, name, mode in todo:
        print(f"    {name:<28} → {mode.upper()}")

    if not args.apply:
        print("\n(dry run — pass --apply to grant these roles)")
        return 0

    granted = failed = 0
    for uid, name, mode in todo:
        try:
            api(tok, "PUT",
                f"/guilds/{args.guild}/members/{uid}/roles/{role_ids[mode]}",
                reason=REASON)
        except urllib.error.HTTPError as exc:
            print(f"  FAILED {name}: {exc.code} {exc.read()[:200]!r}")
            failed += 1
            continue
        granted += 1
        con = sqlite3.connect(db, timeout=30)
        try:
            con.execute(
                "INSERT INTO dm_audit_log "
                "(guild_id, actor_id, user_a_id, user_b_id, action, timestamp, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (args.guild, None, int(uid), None, "mode_set", time.time(),
                 f"mode={mode} (reaction backfill)"),
            )
            con.commit()
        finally:
            con.close()
        time.sleep(0.4)

    print(f"\ngranted {granted}, failed {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
