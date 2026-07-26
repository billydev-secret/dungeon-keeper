#!/usr/bin/env python3
"""tmux-native feature sessions: worktree + branch + window + claude, in one step.

Replaces the old hand-provisioned clone flow (`dk-sessions/work1`, `work2`, …
each a full `git clone` whose origin was the prod checkout). A worktree shares
one object store and one hooks dir with prod, so a session costs a checkout
instead of a clone, and `/dk-ship` merges a branch that already lives in the
same repo — no push-into-MAINREPO round trip.

    dk_session.py new [MODEL] NAME...   worktree off origin/main + tmux window
    dk_session.py list                  every session: branch, dirty, window
    dk_session.py teardown NAME         remove worktree, drop branch, kill window

`new` names the branch, the worktree directory, and the tmux window the same
thing, so `tmux select-window -t <name>` is the only address you need.

`teardown` is what `/dk-ship` calls, and it runs *inside the window it kills* —
hence `--delay`, which lets the caller detach it (`setsid nohup … &`) and still
get its final report on screen before the pane disappears.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

SESSIONS_DIRNAME = "dk-sessions"

# Friendly aliases only. A full model id (claude-opus-5, …) passes through
# untouched so the launcher never becomes the thing that gates model choice.
MODEL_ALIASES = ("opus", "sonnet", "haiku", "fable")

# Workers default to auto: a session you drive from a phone shouldn't stall on a
# permission prompt nobody is sitting in front of. Auto still runs every call
# past the permission classifier, so genuinely destructive commands (rm -rf, a
# force push) are refused rather than waved through — that's why the default is
# auto and not bypassPermissions.
PERMISSION_MODES = (
    "acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan",
)
DEFAULT_PERMISSION_MODE = "auto"


# ── pure helpers (unit-tested in tests/test_dk_session.py) ───────────────

def normalize_name(tokens: list[str]) -> str:
    """`['Documentation', 'Review']` → `documentation-review`.

    The feature name arrives as free prose (`/dk-feature opus documentation
    review`), so collapse whitespace/underscores to hyphens and drop anything
    that isn't safe in a branch name, a directory name, *and* a tmux window
    name at once.
    """
    raw = "-".join(str(t) for t in tokens).lower()
    raw = re.sub(r"[\s_]+", "-", raw)
    raw = re.sub(r"[^a-z0-9.-]+", "-", raw)
    raw = re.sub(r"-{2,}", "-", raw).strip("-.")
    return raw


def resolve_model(token: str | None) -> str | None:
    """Map a leading token to a `--model` value, or None if it isn't one."""
    if not token:
        return None
    low = token.lower()
    if low in MODEL_ALIASES:
        return low
    if low.startswith("claude-"):
        return low
    return None


def split_model(tokens: list[str]) -> tuple[str | None, list[str]]:
    """Peel a leading model alias off the argument list.

    `['opus', 'documentation', 'review']` → `('opus', ['documentation',
    'review'])`. A feature genuinely named "opus something" is why this only
    ever looks at position 0 and only when more tokens follow it.
    """
    if len(tokens) >= 2:
        model = resolve_model(tokens[0])
        if model:
            return model, list(tokens[1:])
    return None, list(tokens)


def sessions_dir(main_repo: Path) -> Path:
    """Sibling of the prod checkout — keeps worktrees out of the prod tree."""
    return Path(main_repo).parent / SESSIONS_DIRNAME


def worktree_path(main_repo: Path, name: str) -> Path:
    return sessions_dir(main_repo) / name


def claude_command(model: str | None, name: str | None = None,
                   remote: bool = True,
                   permission_mode: str | None = DEFAULT_PERMISSION_MODE,
                   brief: str | None = None) -> str:
    """Shell line for the window: run claude, then keep the window alive.

    Remote Control is on by default and named after the feature. The name has
    to be passed at launch: a session's Remote Control title is fixed at
    startup, so nothing — not a hook, not a slash command — can set it later.
    Spawning is the only moment it can be named, so this is where it happens.

    `exec $SHELL` matters — without it, quitting claude closes the window and
    the worktree is orphaned with no visible trace that it still exists.
    """
    cmd = "claude"
    if model:
        cmd += f" --model {shlex.quote(model)}"
    if remote:
        cmd += " --remote-control"
        if name:
            cmd += f" {shlex.quote(name)}"
    if permission_mode:
        cmd += f" --permission-mode {shlex.quote(permission_mode)}"
    if brief:
        # Trailing positional: claude treats it as the session's first prompt,
        # so the worker starts with its context already in hand instead of
        # waiting to be told what it is. shlex.quote survives newlines and
        # quotes, which a hand-written briefing will contain.
        cmd += f" {shlex.quote(brief)}"
    return f"{cmd}; exec $SHELL"


def new_window_args(name: str, path: Path, model: str | None,
                    remote: bool = True,
                    permission_mode: str | None = DEFAULT_PERMISSION_MODE,
                    brief: str | None = None) -> list[str]:
    return [
        "tmux", "new-window",
        "-d",
        "-n", name,
        "-c", str(path),
        claude_command(model, name, remote, permission_mode, brief),
    ]


# Footer/dialog markers Claude Code paints in a pane. Checked against the last
# lines of `tmux capture-pane`, most-specific first: a worker showing a choice
# dialog is blocked on a human, which a bare "is it running" check reads as
# working — the exact confusion that makes a stalled worker invisible.
_WAITING_MARKS = (
    "enter to select",       # AskUserQuestion / plan approval / any choice list
    "do you want to",        # permission prompt
    "1. yes",                # trust prompt and other numbered confirms
)
_WORKING_MARK = "esc to interrupt"


def session_state(pane: str) -> str:
    """'waiting' | 'working' | 'idle' for a worker, from its pane text.

    'waiting' means blocked on *you* — a question, a plan, a permission prompt.
    'idle' means sitting at an empty prompt with nothing running, which needs
    you too but isn't blocking anything. Order matters: the dialog markers are
    checked before the running marker, since a pane can hold both when a
    question interrupts a run.
    """
    tail = pane.lower()
    if any(m in tail for m in _WAITING_MARKS):
        return "waiting"
    if _WORKING_MARK in tail:
        return "working"
    return "idle"


def worktree_add_args(main_repo: Path, name: str, path: Path) -> list[str]:
    """The `git worktree add` invocation for a new session.

    Bases on *local* main, never origin/main — see the note in cmd_new. Kept
    separate from the subprocess call so the base ref is pinned by a test.
    """
    return [
        "git", "-C", str(main_repo), "worktree", "add",
        "-b", name, "--no-track", str(path), "main",
    ]


def staleness_warning(behind: int) -> str | None:
    """Warn when prod's main trails origin/main, i.e. someone else pushed.

    Sessions branch off *local* main because that is what /dk-ship merges into.
    That makes prod's main the integration point, and a prod that has fallen
    behind the remote silently bases every new session on old code.
    """
    if behind > 0:
        return (
            f"prod main is {behind} commit(s) behind origin/main — "
            "`git pull` in the prod checkout before starting real work"
        )
    return None


def parse_worktrees(porcelain: str) -> list[dict[str, str]]:
    """Parse `git worktree list --porcelain` into dicts of path/branch."""
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in porcelain.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current = {"path": value, "branch": ""}
        elif key == "branch":
            current["branch"] = value.removeprefix("refs/heads/")
        elif key == "detached":
            current["branch"] = "(detached)"
    if current:
        entries.append(current)
    return entries


# ── process plumbing ─────────────────────────────────────────────────────

def run(args: list[str], cwd: Path | None = None, check: bool = True):
    """Run a command, surfacing failures as a one-line error, not a traceback.

    Everything here shells out to git or tmux; when one of those fails the
    useful information is its stderr, and a Python stack on top of it just
    buries the message the user needs to act on.
    """
    res = subprocess.run(
        args, cwd=str(cwd) if cwd else None,
        capture_output=True, text=True,
    )
    if check and res.returncode != 0:
        detail = (res.stderr or res.stdout).strip().splitlines()
        die(f"{' '.join(args[:3])} failed: {detail[0] if detail else res.returncode}")
    return res


def git_out(main_repo: Path, *args: str) -> str:
    return run(["git", "-C", str(main_repo), *args]).stdout.strip()


def find_main_repo() -> Path:
    """The main worktree, even when called from inside a session worktree."""
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return Path(common).parent


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def tmux_windows() -> dict[str, str]:
    """window name → window id, empty when tmux isn't running."""
    res = run(["tmux", "list-windows", "-a", "-F", "#{window_name}\t#{window_id}"],
              check=False)
    if res.returncode != 0:
        return {}
    out = {}
    for line in res.stdout.splitlines():
        name, _, wid = line.partition("\t")
        if name:
            out[name] = wid
    return out


# ── commands ─────────────────────────────────────────────────────────────

def cmd_new(args: argparse.Namespace) -> int:
    main_repo = find_main_repo()
    model, name_tokens = split_model(args.tokens)
    if args.model:  # explicit --model wins over a positional alias
        model = resolve_model(args.model) or args.model
    name = normalize_name(name_tokens)
    if not name:
        die("no feature name given — try: dk_session.py new opus documentation review")

    if not os.environ.get("TMUX") and not args.no_window:
        die("not inside tmux — start one first, or pass --no-window")

    path = worktree_path(main_repo, name)
    if path.exists():
        die(f"{path} already exists — pick another name or tear it down first")

    branches = git_out(main_repo, "branch", "--list", name)
    if branches.strip():
        die(f"branch {name!r} already exists — pick another name")

    # Fetch only to judge staleness — non-fatal, since being offline or having
    # a dead SSH agent shouldn't stop you starting work.
    fetched = run(["git", "-C", str(main_repo), "fetch", "origin"], check=False)
    if fetched.returncode == 0:
        behind = run(
            ["git", "-C", str(main_repo), "rev-list", "--count", "main..origin/main"],
            check=False,
        )
        if behind.returncode == 0 and behind.stdout.strip().isdigit():
            note = staleness_warning(int(behind.stdout.strip()))
            if note:
                print(f"warning: {note}", file=sys.stderr)
    else:
        first = (fetched.stderr or "").strip().splitlines()
        print(f"warning: fetch failed ({first[0] if first else '?'})", file=sys.stderr)
        print("warning: cannot tell whether prod main is behind origin", file=sys.stderr)

    sessions_dir(main_repo).mkdir(parents=True, exist_ok=True)
    # Base off *local* main, never origin/main. The old clone-per-session flow
    # used origin/main because each clone's origin *was* the prod checkout, so
    # the two were one ref. In a worktree origin is GitHub, and prod's main runs
    # ahead of it by every commit not yet pushed — branching off origin/main
    # there silently starts the session on stale code.
    #
    # --no-track: without it the new branch tracks main and a stray `git push`
    # from the session would target main directly.
    run(worktree_add_args(main_repo, name, path))
    print(f"worktree: {path}  (branch {name} off main)")

    if args.no_window:
        print("window: skipped (--no-window)")
        return 0

    brief = args.brief
    if args.brief_file:
        try:
            brief = Path(args.brief_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            die(f"could not read --brief-file: {exc}")

    run(new_window_args(
        name, path, model,
        remote=not args.no_remote_control,
        permission_mode=args.permission_mode,
        brief=brief,
    ))
    label = model or "default model"
    remote = "remote control off" if args.no_remote_control else f"remote: {name}"
    print(f"window:   {name}  ({label}, {remote}, {args.permission_mode} mode)")
    if brief:
        print(f"briefed:  {len(brief)} chars delivered as the opening prompt")
    print(f"attach:   tmux select-window -t {name}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    main_repo = find_main_repo()
    trees = parse_worktrees(git_out(main_repo, "worktree", "list", "--porcelain"))
    windows = tmux_windows()

    rows = []
    for wt in trees:
        path = Path(wt["path"])
        is_main = path == main_repo
        dirty = run(["git", "-C", str(path), "status", "--porcelain", "-uno"],
                    check=False).stdout.strip()
        live = path.name in windows
        state = "-"
        if live:
            pane = run(["tmux", "capture-pane", "-p", "-S", "-12",
                        "-t", path.name], check=False)
            state = session_state(pane.stdout) if pane.returncode == 0 else "?"
        rows.append((
            "(prod)" if is_main else path.name,
            wt["branch"] or "?",
            "dirty" if dirty else "clean",
            "live" if live else "-",
            state.upper() if state == "waiting" else state,
        ))

    width = max([len("SESSION"), *(len(r[0]) for r in rows)])
    bwidth = max([len("BRANCH"), *(len(r[1]) for r in rows)])
    print(f"{'SESSION'.ljust(width)}  {'BRANCH'.ljust(bwidth)}  TREE   WINDOW  STATE")
    for name, branch, dirty, live, state in rows:
        print(f"{name.ljust(width)}  {branch.ljust(bwidth)}  "
              f"{dirty.ljust(5)}  {live.ljust(6)}  {state}")
    waiting = [r[0] for r in rows if r[4] == "WAITING"]
    if waiting:
        print(f"\n{len(waiting)} waiting on you: {', '.join(waiting)}")
    return 0


def cmd_teardown(args: argparse.Namespace) -> int:
    main_repo = find_main_repo()
    name = normalize_name([args.name])
    path = worktree_path(main_repo, name)

    if args.delay:
        # Called detached from inside the doomed window: give the caller time
        # to print its ship report before the pane vanishes underneath it.
        time.sleep(args.delay)

    if path.exists():
        remove = ["git", "-C", str(main_repo), "worktree", "remove", str(path)]
        if args.force:
            remove.append("--force")
        res = run(remove, check=False)
        if res.returncode != 0:
            print(f"worktree remove failed: {res.stderr.strip()}", file=sys.stderr)
            return 1
        print(f"removed worktree {path}")
    run(["git", "-C", str(main_repo), "worktree", "prune"], check=False)

    # Only after the worktree is gone: git refuses to delete a checked-out branch.
    res = run(["git", "-C", str(main_repo), "branch", "-d", name], check=False)
    if res.returncode == 0:
        print(f"deleted branch {name}")
    elif "not fully merged" in res.stderr:
        print(f"kept branch {name} — not merged into main", file=sys.stderr)

    window = args.window or name
    run(["tmux", "kill-window", "-t", window], check=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dk_session.py", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_new = sub.add_parser("new", help="worktree + branch + tmux window running claude")
    p_new.add_argument("tokens", nargs="+", help="[model] feature name words")
    p_new.add_argument("--model", help="model override (opus/sonnet/haiku/fable or full id)")
    p_new.add_argument("--no-window", action="store_true", help="worktree only, no tmux")
    p_new.add_argument(
        "--no-remote-control", action="store_true",
        help="local-only session (Remote Control is on by default)",
    )
    p_new.add_argument(
        "--permission-mode", choices=PERMISSION_MODES,
        default=DEFAULT_PERMISSION_MODE,
        help=f"worker permission mode (default: {DEFAULT_PERMISSION_MODE})",
    )
    brief_group = p_new.add_mutually_exclusive_group()
    brief_group.add_argument(
        "--brief", help="opening prompt handed to the worker at launch",
    )
    brief_group.add_argument(
        "--brief-file", help="read the opening prompt from a file (better for long briefings)",
    )
    p_new.set_defaults(func=cmd_new)

    p_list = sub.add_parser("list", help="every session worktree and its window")
    p_list.set_defaults(func=cmd_list)

    p_down = sub.add_parser("teardown", help="remove worktree, drop branch, kill window")
    p_down.add_argument("name")
    p_down.add_argument("--window", help="tmux target to kill (default: the name)")
    p_down.add_argument("--delay", type=float, default=0.0, help="seconds to wait first")
    p_down.add_argument("--force", action="store_true", help="discard uncommitted work")
    p_down.set_defaults(func=cmd_teardown)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
