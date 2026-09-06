#!/usr/bin/env python3
"""CI watch — DMs the owner when CI on main turns red, and again when it recovers.

Main was red for 25 days (2026-08-11 → 2026-09-05) and nobody knew, because
nothing was watching. That is not a cosmetic problem: while the `test` job was
failing, every merge went in with the full suite never having passed on it, and
three embed-contract violations shipped that a green baseline would have caught.
A red main is a disabled gate, so it is worth a page.

Deliberately stdlib-only and run with the system python3, for the same reason
scripts/watchdog.py is: a broken venv or a half-finished deploy must not be able
to silence the alarm. The Discord DM path is watchdog.py's, imported rather than
copied — one bot token, one `send_dm`, one place to fix.

Reads the repo .env the way watchdog.py does (see its docstring for the token
and user-id keys). Extra key, both optional:

    CI_WATCH_WORKFLOW   workflow name to watch (default "Test Suite")
    CI_WATCH_BRANCH     branch to watch (default "main")

Alert logic mirrors the watchdog's: state is remembered between runs, and a DM
goes out only when the conclusion *changes*. A run that is still in progress is
ignored entirely — the question is what the last finished run concluded, not
what the current one might. So a permanently red main pages once, not hourly.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GH = "/usr/bin/gh"
#: Not in the repo: an untracked file in a worktree aborts `dk_session.py
#: teardown` (and takes the branch delete and QA card down with it).
STATE = Path.home() / ".cache" / "dk-ci-watch.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ci-watch")


def _load_watchdog():
    """Import watchdog.py for its .env parser and Discord DM sender.

    It guards its own entry point behind ``__name__``, so importing runs no
    polling loop.
    """
    spec = importlib.util.spec_from_file_location("dk_watchdog", ROOT / "scripts" / "watchdog.py")
    if spec is None or spec.loader is None:  # pragma: no cover - packaging accident
        raise RuntimeError("cannot load scripts/watchdog.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def latest_finished_run(workflow: str, branch: str) -> dict | None:
    """The most recent *completed* run of ``workflow`` on ``branch``.

    Asks for a handful and filters client-side: `gh run list` has no "completed
    only" filter, and a run kicked off a minute ago would otherwise mask the
    verdict of the one before it.
    """
    out = subprocess.run(
        [GH, "run", "list", "--workflow", workflow, "--branch", branch, "--limit", "10",
         "--json", "databaseId,status,conclusion,displayTitle,headSha,url,createdAt"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    if out.returncode != 0:
        raise RuntimeError(f"gh run list failed: {out.stderr.strip()[:400]}")
    for run in json.loads(out.stdout or "[]"):
        if run.get("status") == "completed":
            return run
    return None


def failing_jobs(run_id: int) -> list[str]:
    """Names of the jobs that failed, for a DM that says what broke."""
    out = subprocess.run(
        [GH, "run", "view", str(run_id), "--json", "jobs"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    if out.returncode != 0:
        return []
    jobs = json.loads(out.stdout or "{}").get("jobs", [])
    return [j["name"] for j in jobs if j.get("conclusion") not in ("success", "skipped", None)]


def decide(previous: str | None, conclusion: str) -> str | None:
    """What to say about ``conclusion``, given what was last said.

    Returns ``"broke"``, ``"fixed"``, or None for "say nothing". Pure, so the
    one piece of judgement here is testable without a network or a bot token.

    A first sighting is only worth a DM when it is bad news: arriving to a red
    main is the thing this exists to tell you, while arriving to a green one is
    just Tuesday. After that only transitions speak, which is what keeps a
    long-running red from paging every hour.
    """
    good = conclusion == "success"
    if previous is None:
        return None if good else "broke"
    if previous == conclusion:
        return None
    return "fixed" if good else "broke"


def compose(kind: str, run: dict, jobs: list[str]) -> str:
    title = (run.get("displayTitle") or "").strip()[:120]
    sha = (run.get("headSha") or "")[:8]
    if kind == "fixed":
        return (f"✅ CI on main is green again — {run.get('conclusion')} on `{sha}`\n"
                f"{title}\n{run.get('url')}")
    broke = f" ({', '.join(jobs)})" if jobs else ""
    return (f"🔴 CI on main is {run.get('conclusion')}{broke} — `{sha}`\n"
            f"{title}\n{run.get('url')}\n"
            f"Merges from here land without the full suite ever passing on them.")


def read_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="say what would be sent, send nothing, record nothing")
    ap.add_argument("--test-dm", action="store_true",
                    help="send one DM right now to prove the path works, then exit")
    args = ap.parse_args()

    wd = _load_watchdog()
    env = wd.parse_env(ROOT / ".env")
    token_key = "DISCORD_TOKEN_DEV" if env.get("BOT_ENV", "prod") == "dev" else "DISCORD_TOKEN_PROD"
    token = env.get(token_key) or env.get("DISCORD_TOKEN")
    user_id = env.get("WATCHDOG_USER_ID") or env.get("SUPPORT_USER_ID")
    if not token or not user_id:
        log.error("no bot token or no DM target in .env (%s / WATCHDOG_USER_ID)", token_key)
        return 2

    if args.test_dm:
        ok = wd.send_dm(token, user_id, "🔔 CI watch is wired up. This is the test DM.")
        log.info("test DM %s", "sent" if ok else "FAILED")
        return 0 if ok else 1

    workflow = env.get("CI_WATCH_WORKFLOW", "Test Suite")
    branch = env.get("CI_WATCH_BRANCH", "main")
    run = latest_finished_run(workflow, branch)
    if run is None:
        log.info("no completed %s run on %s yet", workflow, branch)
        return 0

    conclusion = run.get("conclusion") or "unknown"
    state = read_state()
    kind = decide(state.get("conclusion"), conclusion)
    log.info("%s on %s: %s (%s) -> %s", workflow, branch, conclusion,
             str(run.get("headSha"))[:8], kind or "no change")
    if kind is None:
        if not args.dry_run:
            write_state({"conclusion": conclusion, "run_id": run.get("databaseId")})
        return 0

    jobs = failing_jobs(int(run["databaseId"])) if kind == "broke" else []
    message = compose(kind, run, jobs)
    if args.dry_run:
        print(message)
        return 0
    if wd.send_dm(token, user_id, message):
        write_state({"conclusion": conclusion, "run_id": run.get("databaseId")})
        return 0
    log.error("DM failed; leaving state alone so the next run retries")
    return 1


if __name__ == "__main__":
    sys.exit(main())
