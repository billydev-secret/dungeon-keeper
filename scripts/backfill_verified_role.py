#!/usr/bin/env python3
"""Find (and optionally fix) members holding a grant role without its prerequisite.

Companion to the ``/grant`` prerequisite gate. ``required_role_id`` on a
``grant_roles`` row has been settable since migration 021 but was never
passed through by ``/grant`` (see docs/role_grant_spec.md), so members were
granted roles whose prerequisite they did not hold. The gate now blocks that
going forward; this script identifies — and with ``--apply``, remediates —
the members who came through while it was dead.

Reads **live Discord role state**, not ``role_events``: that table only
records role changes the bot observed, so a role gained during a downtime is
invisible to it and would produce a false positive here.

    python scripts/backfill_verified_role.py                  # report only
    python scripts/backfill_verified_role.py --grant denizen  # one grant
    python scripts/backfill_verified_role.py --apply          # add the prerequisite

Default is a dry run. ``--apply`` grants the *prerequisite* role to everyone
holding the grant without it — the deliberate reading that those members are
vouched for, rather than stripping the grant they already have. It never
removes anything.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

API = "https://discord.com/api/v10"
UA = "DiscordBot (https://github.com/local/dungeon-keeper, 1.0)"

#: Discord caps member pagination at 1000 per page.
PAGE = 1000


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


class ApiError(RuntimeError):
    """A request that failed for a reason retrying won't fix."""


def request(
    method: str,
    url: str,
    tok: str,
    payload: dict | None = None,
    *,
    reason: str | None = None,
):
    """Call the API, transparently obeying 429 retry_after.

    Raises :class:`ApiError` rather than exiting: callers decide whether one
    bad guild or one bad member should stop the whole run. A backfill that
    dies partway through with no record of what it already changed is worse
    than one that reports failures and keeps going.
    """
    for attempt in range(6):
        data = json.dumps(payload).encode() if payload is not None else None
        head = headers(tok)
        if reason:
            # The guild audit log is the only durable record of a role change
            # the bot didn't observe itself, so a bulk grant must explain
            # itself there.
            head["X-Audit-Log-Reason"] = reason
        req = urllib.request.Request(url, data=data, headers=head, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                body = resp.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                # Edge-level 429s (Cloudflare) come back as HTML, not JSON.
                try:
                    retry = float(json.loads(exc.read() or b"{}").get("retry_after", 1.0))
                except (ValueError, json.JSONDecodeError):
                    retry = 5.0
                time.sleep(retry + 0.3)
                continue
            if exc.code >= 500 and attempt < 5:
                time.sleep(2**attempt)
                continue
            raise ApiError(f"{method} {url} -> {exc.code}: {exc.read()[:300]!r}")
        except urllib.error.URLError as exc:
            if attempt < 5:
                time.sleep(2**attempt)
                continue
            raise ApiError(f"{method} {url} -> {exc}")
    raise ApiError(f"{method} {url}: gave up after repeated rate limits")


def fetch_members(guild_id: int, tok: str) -> list[dict]:
    """Every member in the guild, paginated by ascending user id.

    Needs the privileged GUILD_MEMBERS intent, which the bot already holds.
    """
    out: list[dict] = []
    after = "0"
    while True:
        page = request(
            "GET",
            f"{API}/guilds/{guild_id}/members?limit={PAGE}&after={after}",
            tok,
        )
        if not page:
            return out
        out.extend(page)
        if len(page) < PAGE:
            return out
        after = page[-1]["user"]["id"]


def grants_with_prerequisites(
    db: Path, only: str | None, guild_id: int | None
) -> tuple[list[dict], list[dict]]:
    """``grant_roles`` rows with a prerequisite: (selected, all_with_prereq).

    Grant *names* are seeded identically into every guild
    (``_DEFAULT_GRANT_ROLES``), so ``--grant denizen`` alone does not scope to
    one server — this database is shared with a second live guild run by
    someone else. ``--guild`` is the only real scope, hence both halves of the
    return: the caller reports what it skipped rather than silently narrowing.
    """
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT guild_id, grant_name, label, role_id, required_role_id "
                "FROM grant_roles WHERE required_role_id > 0 AND role_id > 0"
            ).fetchall()
        ]
    finally:
        conn.close()
    selected = [
        r
        for r in rows
        if (only is None or r["grant_name"] == only)
        and (guild_id is None or int(r["guild_id"]) == guild_id)
    ]
    return selected, rows


def role_name(guild_id: int, role_id: int, tok: str) -> str:
    for role in request("GET", f"{API}/guilds/{guild_id}/roles", tok):
        if str(role["id"]) == str(role_id):
            return str(role["name"])
    return str(role_id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="grant the prerequisite role (default: report only)",
    )
    parser.add_argument("--grant", help="limit to one grant_name (e.g. denizen)")
    parser.add_argument(
        "--guild",
        type=int,
        help="limit to one guild id — the only filter that actually scopes to "
        "a server, since grant names are seeded into every guild",
    )
    parser.add_argument(
        "--env",
        default="/home/ben/discord-bots/dungeon-keeper/.env",
        help="path to the .env holding DISCORD_TOKEN_PROD and DB_PATH_PROD",
    )
    args = parser.parse_args()

    env_file = Path(args.env)
    tok = env_value("DISCORD_TOKEN_PROD", env_file)
    if not tok:
        sys.exit(f"DISCORD_TOKEN_PROD not found in {env_file}")
    raw_db = env_value("DB_PATH_PROD", env_file) or "dungeonkeeper.db"
    db = Path(raw_db)
    if not db.is_absolute():
        db = env_file.parent / db
    if not db.exists():
        sys.exit(f"database not found: {db}")

    grants, all_with_prereq = grants_with_prerequisites(db, args.grant, args.guild)
    if not all_with_prereq:
        print("No grant roles have a prerequisite configured. Nothing to check.")
        return 0
    if not grants:
        # Distinguish "you typo'd / wrong guild" from "nothing to do".
        print("No grant matched those filters. Configured prerequisites:")
        for r in all_with_prereq:
            print(f"  guild {r['guild_id']}  {r['grant_name']} ({r['label']})")
        return 1
    skipped = len(all_with_prereq) - len(grants)
    if skipped:
        print(f"({skipped} configured prerequisite(s) in other guilds/grants not selected.)")

    total_missing = 0
    failures = 0
    for cfg in grants:
        guild_id = int(cfg["guild_id"])
        granted = str(cfg["role_id"])
        prereq = str(cfg["required_role_id"])
        try:
            members = fetch_members(guild_id, tok)
            prereq_name = role_name(guild_id, int(prereq), tok)
        except ApiError as exc:
            # A stale grant_roles row for a guild the bot has left shouldn't
            # abort every other guild's report.
            failures += 1
            print(f"\nguild {guild_id}: skipped — {exc}")
            continue

        missing = [
            m
            for m in members
            if granted in m.get("roles", [])
            and prereq not in m.get("roles", [])
            and not m["user"].get("bot")
        ]
        holders = sum(
            1 for m in members if granted in m.get("roles", []) and not m["user"].get("bot")
        )
        total_missing += len(missing)

        print(
            f"\n{cfg['label']} ({cfg['grant_name']}) requires @{prereq_name} in "
            f"guild {guild_id} — {len(missing)} of {holders} holders lack it "
            f"({len(members)} members total):"
        )
        if not missing:
            continue
        for m in missing:
            user = m["user"]
            name = m.get("nick") or user.get("global_name") or user["username"]
            print(f"  {name} ({user['id']})")

        if not args.apply:
            print("  (dry run — pass --apply to grant the prerequisite role)")
            continue

        for m in missing:
            try:
                request(
                    "PUT",
                    f"{API}/guilds/{guild_id}/members/{m['user']['id']}/roles/{prereq}",
                    tok,
                    reason=f"Prerequisite backfill for the {cfg['label']} grant",
                )
            except ApiError as exc:
                # Keep going: a half-applied run that reports which members
                # failed is recoverable; one that dies silently is not.
                failures += 1
                print(f"  FAILED {m['user']['id']}: {exc}")
                continue
            print(f"  granted @{prereq_name} to {m['user']['id']}")
            time.sleep(0.3)  # stay well inside the per-route bucket

    if not args.apply and total_missing:
        print(f"\nTotal: {total_missing}. Nothing was modified.")
    if failures:
        print(f"\n{failures} operation(s) failed — see FAILED/skipped lines above.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
