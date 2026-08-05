#!/usr/bin/env python3
"""tmux-native feature sessions: worktree + branch + window + claude, in one step.

Replaces the old hand-provisioned clone flow (`dk-sessions/work1`, `work2`, …
each a full `git clone` whose origin was the prod checkout). A worktree shares
one object store and one hooks dir with prod, so a session costs a checkout
instead of a clone, and `/dk-ship` merges a branch that already lives in the
same repo — no push-into-MAINREPO round trip.

    dk_session.py new [MODEL] NAME...   worktree off origin/main + tmux window
    dk_session.py list                  every session: branch, dirty, window
    dk_session.py snapshot              record which sessions are live right now
    dk_session.py restore               rebuild them after a reboot
    dk_session.py teardown NAME         remove worktree, drop branch, kill window

`new` names the branch, the worktree directory, and the tmux window the same
thing, so `tmux select-window -t <name>` is the only address you need.

`teardown` is what `/dk-ship` calls, and it runs *inside the window it kills* —
hence `--delay`, which lets the caller detach it (`setsid nohup … &`) and still
get its final report on screen before the pane disappears.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path, PurePath, PurePosixPath

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


def link_venv(main_repo: Path, path: Path) -> str | None:
    """Point the session's .venv at prod's, so tooling works on first command.

    A fresh worktree has no .venv, and everything a session actually runs —
    scripts/gate.py, pytest, the Playwright browser suite — resolves its
    interpreter from one. Without this every session starts by hitting
    ModuleNotFoundError and hand-linking it back (see docs/dev_sessions.md).

    A relative symlink, so it survives the whole dk-sessions tree being moved.
    Returns a message for the caller to print, or None if there was nothing
    to do. Never fatal: a missing prod .venv just means no link.
    """
    src = main_repo / ".venv"
    dst = path / ".venv"
    if dst.exists() or dst.is_symlink():
        return None
    if not src.is_dir():
        return "venv:     skipped (no .venv in the prod checkout)"
    try:
        dst.symlink_to(os.path.relpath(src, path), target_is_directory=True)
    except OSError as exc:
        return f"venv:     skipped ({exc})"
    return f"venv:     {dst} -> {os.path.relpath(src, path)}"


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


PRECOMMIT_CACHE = Path.home() / ".cache" / "pre-commit"


def newest_precommit_patch(cache_dir: Path = PRECOMMIT_CACHE) -> Path | None:
    """The most recent patch pre-commit stashed, or None.

    pre-commit stashes *unstaged* changes while hooks run and restores them
    afterwards. If the hook is killed first — a commit that outruns a command
    timeout is the usual way — the restore never happens and that work is gone
    from the worktree. It is not lost, though: the stash is written here as a
    patch file, newest last by mtime.
    """
    if not cache_dir.is_dir():
        return None
    patches = [p for p in cache_dir.glob("patch*") if p.is_file()]
    if not patches:
        return None
    return max(patches, key=lambda p: p.stat().st_mtime)


def patch_files(text: str) -> list[str]:
    """Repo-relative paths a git patch touches, for showing before applying."""
    out: list[str] = []
    for line in text.splitlines():
        if line.startswith("diff --git a/"):
            path = line.split(" b/", 1)[-1].strip()
            if path and path not in out:
                out.append(path)
    return out


def cmd_recover(args: argparse.Namespace) -> int:
    patch = newest_precommit_patch()
    if patch is None:
        print("no pre-commit stash found")
        return 0
    text = patch.read_text(encoding="utf-8", errors="replace")
    files = patch_files(text)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(patch.stat().st_mtime))
    print(f"{patch}  ({stamp})")
    for f in files:
        print(f"  {f}")
    if not args.apply:
        print("\ndry run — pass --apply to restore these into the working tree")
        return 0
    res = run(["git", "apply", str(patch)], check=False)
    if res.returncode != 0:
        print(res.stderr.strip() or "git apply failed", file=sys.stderr)
        print("the patch may already be applied, or the tree has moved on",
              file=sys.stderr)
        return 1
    print(f"\nrestored {len(files)} file(s)")
    return 0


def agent_tmp_name(worktree: PurePath) -> str:
    """The scratch-dir name a Claude Code session uses for *worktree*.

    The agent mangles a session's cwd into a single directory name by turning
    every ``/`` into ``-``, so /home/ben/x becomes ``-home-ben-x``.

    Reads the path through ``as_posix()`` rather than ``str()``, and takes a
    ``PurePath`` so the property is stated in the signature: the suite also runs
    on the Windows remote runner, where ``str(Path("/home/x"))`` is
    ``\\home\\x`` and the mangling would silently produce a different name.
    A test feeds both path flavours and asserts one answer.
    """
    return worktree.as_posix().replace("/", "-")


def agent_tmp_dirs(worktree: Path, tmp_root: Path = Path("/tmp")) -> list[Path]:
    """Existing agent scratch dirs belonging to *worktree*.

    Teardown removed the worktree, the branch and the window but left these,
    so every session ever run leaked its scratch space — five dead sessions had
    accumulated 3.7 GB before this was noticed, on a 5.8 GB tmpfs.

    Deliberately narrow: only ``/tmp/claude-*/<mangled>`` is ever considered,
    and only when the mangled name actually contains the worktree's own
    directory name. This runs `rm -rf` under /tmp, so it refuses to work from a
    name it did not derive from a real worktree path.
    """
    name = agent_tmp_name(worktree)
    if not worktree.name or len(worktree.name) < 2 or worktree.name not in name:
        return []
    return [
        d for parent in sorted(tmp_root.glob("claude-*"))
        if (d := parent / name).is_dir()
    ]


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


# ── surviving a reboot ───────────────────────────────────────────────────
#
# A host reboot kills the tmux server and every session in it, and leaves no
# record of what was running: the worktrees survive, the transcripts survive,
# but the mapping between them does not, so recovery means grepping transcript
# files for their `cwd`. It does not have to. Claude Code already keeps a live
# registry at ~/.claude/sessions/<pid>.json holding a session's id, cwd and
# name — everything a restore needs.
#
# The catch, and the reason `snapshot` exists at all: that registry is deleted
# on SIGTERM, which is exactly what a reboot sends. The data is on disk right
# up to the moment it becomes useful and is then erased. So take a copy while
# the machine is still up, somewhere that isn't tmpfs.

CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"
RESTORE_DIR = Path.home() / ".claude" / "dk-restore"
MANIFEST_PATH = RESTORE_DIR / "manifest.json"
EMPTY_SINCE_PATH = RESTORE_DIR / "empty-since"
TMUX_SESSION = "dk"

# How long the machine must show *no* live sessions before an empty snapshot is
# allowed to replace a populated manifest. Shutdown SIGTERMs every claude, each
# of which unlinks its own registry file, so a snapshot landing in that window
# sees an empty machine and would otherwise erase the very thing it exists to
# preserve — seconds before the reboot it is meant to survive. Emptiness that
# *persists* means the sessions really were closed; emptiness that appears and
# is immediately followed by a reboot means they were killed.
EMPTY_GRACE_SECONDS = 300

# Resuming trips Claude Code's "this session is old and large" dialog — it
# fires past 70 minutes and 100k tokens, thresholds every post-reboot resume
# clears. Left alone it parks each restored session on a menu (the exact state
# session_state() calls WAITING). Suppressed, the session resumes *full*, which
# is the expensive option. Picking "Resume from summary" in that dialog does
# nothing more than run /compact, so suppressing the dialog and sending
# /compact reproduces that choice exactly — unattended, and with no pane left
# blocked on a prompt nobody is sitting in front of.
RESUME_DIALOG_OFF = "CLAUDE_CODE_RESUME_THRESHOLD_MINUTES=999999"
RESUME_SUMMARY_PROMPT = "/compact"

_TRUST_PROMPT_MARK = "do you trust the files in this folder"


def proc_start_ticks(pid: int) -> str | None:
    """Field 22 of /proc/<pid>/stat — when the process started, or None.

    Paired with the `procStart` the registry file recorded, this is what tells
    a live session from a dead one whose pid has been reused. The comm field
    can contain spaces and parentheses, so the split starts after its last ')'.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    tail = stat[stat.rfind(")") + 2:].split(" ")
    return tail[19] if len(tail) > 19 else None


def session_is_live(entry: dict, observed: str | None) -> bool:
    """Is the pid in *entry* still the process that wrote it?

    A registry file with no procStart is trusted on the pid alone — that is the
    format's own fallback, and it is better to offer a dead session for restore
    than to silently drop a live one.
    """
    if observed is None:
        return False
    recorded = entry.get("procStart")
    return recorded is None or str(recorded) == str(observed)


def read_live_sessions(sessions_dir: Path = CLAUDE_SESSIONS_DIR,
                       proc_start=proc_start_ticks) -> list[dict[str, str]]:
    """Every Claude Code session running right now, as {session_id, cwd, name}."""
    out: list[dict[str, str]] = []
    if not sessions_dir.is_dir():
        return out
    for f in sorted(sessions_dir.glob("*.json")):
        try:
            entry = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # a half-written file is not worth failing a snapshot over
        if not isinstance(entry, dict):
            continue
        pid, cwd, sid = entry.get("pid"), entry.get("cwd"), entry.get("sessionId")
        if not isinstance(pid, int) or not cwd or not sid:
            continue
        if not session_is_live(entry, proc_start(pid)):
            continue
        out.append({"session_id": sid, "cwd": cwd, "name": entry.get("name") or ""})
    return out


def build_manifest(sessions: list[dict[str, str]], taken_at: float) -> dict:
    return {"version": 1, "taken_at": taken_at, "sessions": sessions}


def should_replace_manifest(old_count: int, new_count: int, empty_for: float,
                            grace: float = EMPTY_GRACE_SECONDS) -> bool:
    """May a snapshot of *new_count* sessions overwrite one of *old_count*?

    Anything non-empty is written. An empty snapshot only replaces a populated
    manifest once the machine has been empty for *grace* seconds — see
    EMPTY_GRACE_SECONDS for why that distinction is the whole point.
    """
    if new_count > 0 or old_count == 0:
        return True
    return empty_for >= grace


def load_manifest(path: Path = MANIFEST_PATH) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_manifest(manifest: dict, path: Path = MANIFEST_PATH) -> None:
    """Atomically, so a snapshot interrupted mid-write leaves the old one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    tmp.replace(path)


def window_name(cwd: str, main_repo: str) -> str:
    """The tmux window a recorded cwd belongs in.

    The prod checkout is the one session that is not a worktree; it keeps the
    window name `main` it already has, and every worktree is named after its
    directory — the convention `teardown --window` relies on.
    """
    cwd_p, main_p = PurePosixPath(cwd), PurePosixPath(main_repo)
    return "main" if cwd_p == main_p else cwd_p.name


def elide(name: str, limit: int = 44) -> str:
    """Shorten a window name for a table, keeping both ends recognizable."""
    if len(name) <= limit:
        return name
    head = (limit - 1) // 2
    tail = limit - 1 - head
    return f"{name[:head]}…{name[len(name) - tail:]}"


def in_scope(cwd: str, main_repo: str) -> bool:
    """Is this session one of *ours* — the prod checkout or a dk worktree?

    The snapshot records every Claude Code session on the machine, because a
    durable record of what was running is worth having regardless. Restore is
    narrower: rebuilding the `dk` tmux session should not invent windows for
    unrelated projects that happen to have been open.
    """
    # Built with PurePosixPath rather than sessions_dir()'s Path, so the answer
    # is the same on the Windows remote runner — see agent_tmp_name.
    cwd_p, main_p = PurePosixPath(cwd), PurePosixPath(main_repo)
    if cwd_p == main_p:
        return True
    # A *direct* child of dk-sessions only. A session started in some
    # subdirectory of a worktree would otherwise earn its own window, named
    # after that subdirectory ("src"), which is not a session anyone can find.
    return cwd_p.parent == main_p.parent / SESSIONS_DIRNAME


def restore_plan(sessions: list[dict[str, str]], main_repo: str,
                 exists, dirty_count, max_resume: int | None = None) -> list[dict]:
    """Decide, per recorded session, whether it comes back live or as a shell.

    A tree with uncommitted work comes back as a resumed session; a clean one
    comes back as a shell holding the command to resume it by hand. That split
    is deliberate — respawning every session costs real usage for workers you
    would have abandoned, and a clean tree has nothing in flight to lose.

    Sessions outside this repo (`foreign`) and worktrees torn down since the
    snapshot (`gone`) are reported rather than skipped, so a restore never
    silently drops something it was asked about. Prod sorts first so it keeps
    window 1.
    """
    plan: list[dict] = []
    seen: set[str] = set()
    for s in sessions:
        cwd = s.get("cwd", "")
        if not cwd or cwd in seen:
            continue
        seen.add(cwd)
        entry = dict(s, window=window_name(cwd, main_repo), dirty=0, capped=False)
        if not in_scope(cwd, main_repo):
            entry["mode"] = "foreign"
        elif not exists(cwd):
            entry["mode"] = "gone"
        else:
            entry["dirty"] = dirty_count(cwd)
            entry["mode"] = "resume" if entry["dirty"] else "shell"
        plan.append(entry)

    plan.sort(key=lambda e: (e["window"] != "main", e["window"]))

    if max_resume is not None:
        live = 0
        for entry in plan:
            if entry["mode"] != "resume":
                continue
            live += 1
            if live > max_resume:
                entry["mode"] = "shell"
                entry["capped"] = True
    return plan


def resume_claude_command(session_id: str, name: str,
                          permission_mode: str | None = DEFAULT_PERMISSION_MODE,
                          summary: bool = True) -> str:
    """The shell line that brings one session back in its window."""
    cmd = (f"claude --resume {shlex.quote(session_id)} "
           f"--remote-control {shlex.quote(name)}")
    if permission_mode:
        cmd += f" --permission-mode {shlex.quote(permission_mode)}"
    if summary:
        cmd += f" {shlex.quote(RESUME_SUMMARY_PROMPT)}"
    # The dialog is suppressed either way: unanswered it blocks the session,
    # and when resuming from summary /compact is what answers it anyway.
    return f"{RESUME_DIALOG_OFF} {cmd}; exec $SHELL"


def shell_window_command(session_id: str, name: str) -> str:
    """A clean tree's window: a shell, with the resume line ready to paste."""
    resume = resume_claude_command(session_id, name)
    resume = resume[: -len("; exec $SHELL")]
    msg = (f"clean tree — prior session {session_id}\n"
           f"resume it with:\n  {resume}")
    return f"printf '%s\\n' {shlex.quote(msg)}; exec $SHELL"


def window_command(entry: dict) -> str:
    if entry["mode"] == "resume":
        return resume_claude_command(entry["session_id"], entry["window"])
    return shell_window_command(entry["session_id"], entry["window"])


def restore_new_session_args(window: str, path: str, command: str) -> list[str]:
    return ["tmux", "new-session", "-d", "-s", TMUX_SESSION,
            "-n", window, "-c", str(path), command]


def restore_new_window_args(window: str, path: str, command: str) -> list[str]:
    return ["tmux", "new-window", "-d", "-t", f"{TMUX_SESSION}:",
            "-n", window, "-c", str(path), command]


def wait_for_network(host: str = "api.anthropic.com", port: int = 443,
                     timeout: float = 120.0, interval: float = 2.0,
                     probe=None, now=time.monotonic, sleep=time.sleep) -> bool:
    """Block until *host* answers, or *timeout* elapses.

    A restore driven by a systemd **user** unit cannot order itself after the
    system's network-online.target, and a claude that starts before DNS works
    fails its first call rather than retrying. So poll instead of ordering.
    """
    if probe is None:
        def probe() -> bool:
            try:
                socket.create_connection((host, port), timeout=5).close()
                return True
            except OSError:
                return False
    deadline = now() + timeout
    while True:
        if probe():
            return True
        if now() >= deadline:
            return False
        sleep(interval)


def dirty_file_count(path: Path) -> int:
    """Uncommitted files in a worktree, untracked ones included.

    Deliberately *not* `-uno` (which `list` uses): a session whose only work so
    far is a new file has work worth restoring, and reporting it clean would
    quietly drop exactly the session most worth bringing back.
    """
    res = run(["git", "-C", str(path), "status", "--porcelain"], check=False)
    if res.returncode != 0:
        return 0
    return len([ln for ln in res.stdout.splitlines() if ln.strip()])


def tmux_session_exists(session: str = TMUX_SESSION) -> bool:
    return run(["tmux", "has-session", "-t", session], check=False).returncode == 0


def clear_trust_prompt(window: str, attempts: int = 10, delay: float = 1.5) -> bool:
    """Answer the "do you trust this folder?" dialog in a restored window.

    A session started in a directory Claude Code has not been trusted in blocks
    on this before it does anything else, which unattended means a restore that
    silently restores nothing. Narrow on purpose: this answers that one dialog
    and no other — every path involved came out of our own manifest.
    """
    for _ in range(attempts):
        pane = run(["tmux", "capture-pane", "-p", "-S", "-20", "-t", window],
                   check=False)
        if pane.returncode != 0:
            return False
        if _TRUST_PROMPT_MARK in pane.stdout.lower():
            run(["tmux", "send-keys", "-t", window, "1", "Enter"], check=False)
            return True
        time.sleep(delay)
    return False


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

    venv_msg = link_venv(main_repo, path)
    if venv_msg:
        print(venv_msg)

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


def orphan_scratch_dirs(sessions_root: Path,
                        tmp_root: Path = Path("/tmp")) -> list[Path]:
    """Agent scratch dirs under /tmp whose session worktree no longer exists.

    Teardown now cleans up after itself, but every session torn down before
    that fix left its scratch behind. Matches only names mangled from
    *sessions_root*, so nothing outside dk-sessions/ is ever a candidate.
    """
    prefix = sessions_root.as_posix().replace("/", "-") + "-"
    out: list[Path] = []
    for parent in sorted(tmp_root.glob("claude-*")):
        if not parent.is_dir():
            continue
        for d in sorted(parent.iterdir()):
            if not d.is_dir() or not d.name.startswith(prefix):
                continue
            session = d.name[len(prefix):]
            if session and not (sessions_root / session).exists():
                out.append(d)
    return out


def cmd_sweep(args: argparse.Namespace) -> int:
    main_repo = find_main_repo()
    orphans = orphan_scratch_dirs(sessions_dir(main_repo))
    if not orphans:
        print("no orphaned scratch dirs")
        return 0
    total = 0
    for d in orphans:
        raw = run(["du", "-sb", str(d)], check=False).stdout.split("\t")[0]
        total += int(raw) if raw.isdigit() else 0
        human = run(["du", "-sh", str(d)], check=False).stdout.split("\t")[0]
        print(f"  {human:>6}  {d.name}")
        if args.apply:
            shutil.rmtree(d, ignore_errors=True)
    print()
    verb = "removed" if args.apply else "would remove"
    print(f"{verb} {len(orphans)} dir(s), {total / 1e9:.2f} GB")
    if not args.apply:
        print("dry run — pass --apply to delete")
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    live = read_live_sessions()
    old = load_manifest()
    old_count = len(old.get("sessions", []))
    now = time.time()

    RESTORE_DIR.mkdir(parents=True, exist_ok=True)
    empty_for = 0.0
    if live:
        EMPTY_SINCE_PATH.unlink(missing_ok=True)
    else:
        if not EMPTY_SINCE_PATH.exists():
            EMPTY_SINCE_PATH.write_text(str(now), encoding="utf-8")
        try:
            empty_for = now - float(EMPTY_SINCE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            empty_for = 0.0

    if not should_replace_manifest(old_count, len(live), empty_for):
        if args.verbose:
            print(f"kept manifest ({old_count} session(s)); machine has been "
                  f"empty {empty_for:.0f}s of {EMPTY_GRACE_SECONDS:.0f}s")
        return 0

    write_manifest(build_manifest(live, now))
    if args.verbose:
        print(f"snapshot: {len(live)} live session(s) -> {MANIFEST_PATH}")
        for s in live:
            print(f"  {s['session_id'][:8]}  {s['cwd']}")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    main_repo = find_main_repo()
    manifest = load_manifest()
    sessions = manifest.get("sessions", [])
    if not sessions:
        print(f"no manifest at {MANIFEST_PATH} — nothing to restore")
        return 0

    taken = manifest.get("taken_at")
    stamp = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(taken))
             if isinstance(taken, (int, float)) else "?")
    print(f"manifest: {len(sessions)} session(s), taken {stamp}")

    plan = restore_plan(
        sessions, str(main_repo),
        exists=lambda p: Path(p).is_dir(),
        dirty_count=lambda p: dirty_file_count(Path(p)),
        max_resume=args.max,
    )

    # Feature names come from free prose, so a window name can run to 130
    # characters; elided, the table stays readable in an 80-column pane.
    labels = {e["session_id"]: elide(e["window"]) for e in plan}
    width = max([len("WINDOW"), *(len(v) for v in labels.values())])
    print(f"\n{'WINDOW'.ljust(width)}  MODE     DIRTY  SESSION")
    for e in plan:
        note = " (capped)" if e["capped"] else ""
        print(f"{labels[e['session_id']].ljust(width)}  {e['mode'].ljust(7)}  "
              f"{str(e['dirty']).rjust(5)}  {e['session_id'][:8]}{note}")

    tally = {mode: sum(1 for e in plan if e["mode"] == mode)
             for mode in ("resume", "shell", "gone", "foreign")}
    print(f"\n{tally['resume']} to resume, {tally['shell']} as shells, "
          f"{tally['gone']} worktree(s) gone, "
          f"{tally['foreign']} outside this repo (left alone)")

    # Checked here rather than up front: a dry run is always safe to look at,
    # and "what would have come back?" is a fair question to ask of a machine
    # whose sessions are already running.
    if tmux_session_exists() and not args.force:
        print(f"\ntmux session {TMUX_SESSION!r} already exists — refusing to "
              "restore on top of it (pass --force if that is what you want)",
              file=sys.stderr)
        return 1 if args.apply else 0

    if not args.apply:
        print("\ndry run — pass --apply to rebuild the tmux session")
        return 0

    if args.wait_online and tally["resume"]:
        if not wait_for_network(timeout=args.wait_online):
            print("warning: network never came up; resuming anyway",
                  file=sys.stderr)

    started: list[str] = []
    for e in plan:
        if e["mode"] in ("gone", "foreign"):
            continue
        cmd = window_command(e)
        first = not started and not tmux_session_exists()
        make = restore_new_session_args if first else restore_new_window_args
        res = run(make(e["window"], e["cwd"], cmd), check=False)
        if res.returncode != 0:
            print(f"window {e['window']} failed: {res.stderr.strip()}",
                  file=sys.stderr)
            continue
        started.append(e["window"])

    for e in plan:
        if e["mode"] == "resume" and e["window"] in started:
            clear_trust_prompt(e["window"])

    print(f"\nrestored {len(started)} window(s) — attach with: "
          f"tmux attach -t {TMUX_SESSION}")
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

    # The agent's scratch dir outlives the worktree unless we remove it, and
    # it is the single biggest consumer of a small tmpfs — see agent_tmp_dirs.
    for scratch in agent_tmp_dirs(path):
        size = run(["du", "-sh", str(scratch)], check=False).stdout.split("\t")[0]
        shutil.rmtree(scratch, ignore_errors=True)
        if not scratch.exists():
            print(f"removed scratch {scratch} ({size or '?'})")

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

    p_sweep = sub.add_parser(
        "sweep", help="remove agent scratch dirs left by already-gone sessions",
    )
    p_sweep.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    p_sweep.set_defaults(func=cmd_sweep)

    p_snap = sub.add_parser(
        "snapshot", help="record which Claude Code sessions are live right now",
    )
    p_snap.add_argument("--verbose", action="store_true", help="print what was recorded")
    p_snap.set_defaults(func=cmd_snapshot)

    p_res = sub.add_parser(
        "restore", help="rebuild the tmux session from the last snapshot",
    )
    p_res.add_argument("--apply", action="store_true",
                       help="actually rebuild (default: dry run)")
    p_res.add_argument("--force", action="store_true",
                       help="restore even though a tmux session already exists")
    p_res.add_argument("--max", type=int, default=None, metavar="N",
                       help="resume at most N sessions; the rest come back as shells")
    p_res.add_argument("--wait-online", type=float, default=0.0, metavar="SECONDS",
                       help="wait up to SECONDS for the network before resuming")
    p_res.set_defaults(func=cmd_restore)

    p_rec = sub.add_parser(
        "recover", help="restore work pre-commit stashed and never gave back",
    )
    p_rec.add_argument("--apply", action="store_true", help="actually restore (default: dry run)")
    p_rec.set_defaults(func=cmd_recover)

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
