"""Dispatch pytest to a faster machine over SSH, falling back to local silently.

The prod box is a 2-core VM; a spare desktop runs the suite several times
faster. This ships the source over and runs pytest there, but **never blocks
work when that machine is off** — if the probe fails, the caller runs locally
as if this module didn't exist.

Transport-agnostic on purpose: the remote may be native Windows (OpenSSH
Server), WSL2, or plain Linux. Everything goes through one `ssh host "<cmd>"`
invocation and a tar pipe, so there is no rsync dependency (rsync is not
present on native Windows) and no assumption about the remote shell beyond
`cd X && Y`, which cmd.exe, PowerShell, and bash all accept.

Configuration comes from the process environment, falling back to the
checkout's `.env` (which gate.py does not otherwise load). Absent
`REMOTE_TEST_HOST`, this module is inert.

    REMOTE_TEST_HOST      user@host of the test runner. Unset ⇒ always local.
    REMOTE_TEST_DIR       Path to the repo checkout on that host.
    REMOTE_TEST_PYTHON    Python to run there (e.g. .venv/Scripts/python.exe).
    REMOTE_TEST_JOBS      xdist workers (default 12 — leaves a desktop usable).
    REMOTE_TEST_LOCK      Lock file the remote installs from, when it needs its
                          own (default requirements-dev.lock).
    REMOTE_TEST_CD        Override the `cd` template if the remote shell needs
                          it (e.g. "cd /d {dir} && {cmd}" for a cmd.exe remote
                          on a different drive letter).
    REMOTE_TEST_WORKSPACE Sub-directory of REMOTE_TEST_DIR to sync into (default
                          derived per checkout, so parallel checkouts don't
                          collide). Set to "off" for the legacy layout: sync
                          straight over REMOTE_TEST_DIR, and don't prune.
    GATE_NO_REMOTE=1      Force local for one run.

The real environment takes precedence over `.env`, so a one-off
`GATE_NO_REMOTE=1 git commit ...` works without editing the file.

The remote needs only a git clone — the suite reads no .env, no database, and
no model files, so nothing secret is ever synced.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]

# Compiled pins. Shipped so the remote can detect when its venv is stale, and
# reinstall from the same file this side is using.
LOCK_FILES = ("requirements.lock", "requirements-dev.lock")

# Only these are shipped. Everything else (the DB, models/, backups/, .venv,
# .git) is either irrelevant to the suite or far too large to sync per run.
# README.md is included even though it isn't code: test_games_help_logic.py
# asserts it stays in sync with the actual party-game list, and a remote whose
# only copy dates back to its initial `git clone` will drift the moment either
# side changes — silently, since nothing else re-syncs it.
SYNC_PATHS = ("src", "tests", "scripts", "pyproject.toml", "README.md", *LOCK_FILES)

# Records which lock hash the remote venv was last installed from. Kept beside
# the venv rather than in a workspace: workspaces come and go per checkout, and
# re-running a multi-GB pip install for each one would defeat the point.
STAMP_FILE = ".remote-test-stamp"

# Shipped inside each workspace; lists every file the sync sent, so the remote
# can prune whatever it still has that we did not.
MANIFEST_FILE = ".remote-manifest"

# Exit code meaning "the remote could not prepare itself — run locally
# instead". Chosen outside pytest's 0-5 range so it can never collide with a
# genuine test result, which must keep failing the gate.
BOOTSTRAP_FAILED = 97

# Characters that would change meaning inside the single remote command string.
# Rather than guess at cmd.exe vs POSIX quoting, refuse to build a command we
# cannot faithfully represent — a loud failure beats a silently wrong test run.
_UNSAFE = set(' \t\n"\'\\|&;<>()$`')

DEFAULT_JOBS = 12
DEFAULT_CD = "cd {dir} && {cmd}"

# Which compiled pins the remote installs from. Overridable because a host may
# need its own: the Windows runner requires onnxruntime 1.27, while the Linux
# lock pins 1.26 (no cp314 Windows wheel for it).
DEFAULT_LOCK = "requirements-dev.lock"


@dataclass(frozen=True)
class RemoteConfig:
    host: str
    directory: str
    python: str
    jobs: int = DEFAULT_JOBS
    cd_template: str = DEFAULT_CD
    lock: str = DEFAULT_LOCK
    # Sub-directory of `directory` this checkout syncs into. Empty means the
    # legacy behaviour of syncing straight over the remote checkout.
    workspace: str = ""

    @property
    def run_dir(self) -> str:
        """Where pytest runs on the remote."""
        return f"{self.directory}/{self.workspace}" if self.workspace else self.directory


def workspace_slug(root: Path = ROOT) -> str:
    """A stable, filesystem-safe directory name for one local checkout.

    Several session checkouts share one test host, and they must not extract
    over each other: two concurrent runs sharing a directory interleave their
    writes and both report nonsense. Keyed on the absolute path so the same
    checkout reuses its workspace (and so its venv-independent state survives),
    but two checkouts — even two with the same basename — never collide.
    """
    resolved = root.resolve()
    digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:10]
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in resolved.name)[:24]
    return f"ws-{safe or 'checkout'}-{digest}"


def _git(root: Path, *args: str) -> str | None:
    """Run a read-only git command in root, or None if it can't be run."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def env_path(root: Path = ROOT) -> Path | None:
    """Locate the .env holding this config, or None.

    Prefers the current checkout. Two fallbacks, because .env is gitignored and
    so never travels with the code:

    1. **Worktrees** (`git rev-parse --git-common-dir`) — the documented edit
       workflow, and a worktree has no .env of its own.
    2. **Local clones** (`git config remote.origin.url`) — session checkouts are
       cloned from the main checkout rather than added as worktrees, and for
       those `--git-common-dir` resolves back to the clone itself, so step 1
       finds nothing. Only a filesystem path is followed; a real URL is ignored.

    Without these, dispatch silently never fires and every run falls back to the
    slow local box — the failure mode is a quiet 10x slowdown, not an error.
    """
    local = root / ".env"
    if local.exists():
        return local

    common_dir = _git(root, "rev-parse", "--git-common-dir")
    if common_dir is None:
        return None
    # --git-common-dir points at the main checkout's .git; its parent is the
    # working tree that owns the .env. In a plain clone it is this checkout's
    # own .git, so this resolves back to `local` and finds nothing.
    candidate = (root / common_dir).resolve().parent / ".env"
    if candidate.exists():
        return candidate

    origin = _git(root, "config", "--get", "remote.origin.url")
    if not origin:
        return None
    # Filesystem paths only: "user@host:repo" and "https://…" are remote and
    # have no .env we could read.
    if "://" in origin or (":" in origin and not Path(origin).is_absolute()):
        return None
    candidate = Path(origin).expanduser()
    if not candidate.is_absolute():
        candidate = (root / candidate).resolve()
    # origin may point at the bare .git or at the working tree above it.
    for base in (candidate, candidate.parent):
        if base.name == ".git":
            continue
        env = base / ".env"
        if env.exists():
            return env
    return None


def env_file_values(root: Path = ROOT) -> dict[str, str]:
    """Read REMOTE_TEST_* / GATE_NO_REMOTE settings out of the checkout's .env.

    gate.py runs from a plain shell — often the pre-commit hook's, under a bare
    system Python — so it never sees the bot's dotenv-loaded config. Parsing the
    file directly (same approach as scripts/post_testing_docs.py) keeps this
    config beside every other setting instead of in a shell profile, and .env is
    gitignored so host-specific paths never reach the repo.

    Deliberately no python-dotenv import: it may not exist in the interpreter
    running the hook.
    """
    path = env_path(root)
    if path is None:
        return {}

    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        if not (key.startswith("REMOTE_TEST_") or key == "GATE_NO_REMOTE"):
            continue

        # A quoted value keeps everything inside the quotes (a '#' there is
        # data); an unquoted one is truncated at an inline comment.
        value = raw.strip()
        if value[:1] in ("'", '"'):
            closing = value.find(value[0], 1)
            value = value[1:closing] if closing > 0 else value[1:]
        else:
            value = value.split("#")[0]
        values[key] = value.strip()
    return values


class UnsafeArgument(ValueError):
    """A pytest argument cannot be safely embedded in the remote command."""


def load_config(env: Mapping[str, str] | None = None) -> RemoteConfig | None:
    """Build a config from the environment, or None when remote is disabled.

    Returns None (meaning "run locally") when the host is unset or when
    GATE_NO_REMOTE is truthy. A host set without DIR/PYTHON is a
    misconfiguration and raises, because silently running locally there would
    hide the fact that dispatch never happened.
    """
    # Real environment wins over .env, so `GATE_NO_REMOTE=1 git commit ...`
    # still forces a local run without editing the file.
    if env is None:
        env = {**env_file_values(), **os.environ}

    if env.get("GATE_NO_REMOTE", "").strip() in ("1", "true", "yes"):
        return None

    host = env.get("REMOTE_TEST_HOST", "").strip()
    if not host:
        return None

    directory = env.get("REMOTE_TEST_DIR", "").strip()
    python = env.get("REMOTE_TEST_PYTHON", "").strip()
    if not directory or not python:
        raise ValueError(
            "REMOTE_TEST_HOST is set but REMOTE_TEST_DIR and/or "
            "REMOTE_TEST_PYTHON are missing — refusing to guess."
        )

    raw_jobs = env.get("REMOTE_TEST_JOBS", "").strip()
    try:
        jobs = int(raw_jobs) if raw_jobs else DEFAULT_JOBS
    except ValueError:
        raise ValueError(f"REMOTE_TEST_JOBS must be an integer, got {raw_jobs!r}") from None
    if jobs < 1:
        raise ValueError(f"REMOTE_TEST_JOBS must be >= 1, got {jobs}")

    workspace = env.get("REMOTE_TEST_WORKSPACE", "").strip()
    if workspace.lower() in ("0", "off", "false", "none"):
        workspace = ""          # opt back into syncing over the checkout itself
    elif not workspace:
        workspace = workspace_slug()
    if workspace:               # empty is legal (legacy layout); "" fails check_args
        check_args([workspace])

    return RemoteConfig(
        host=host,
        directory=directory,
        python=python,
        jobs=jobs,
        workspace=workspace,
        cd_template=env.get("REMOTE_TEST_CD", "").strip() or DEFAULT_CD,
        lock=env.get("REMOTE_TEST_LOCK", "").strip() or DEFAULT_LOCK,
    )


def check_args(args: Sequence[str]) -> None:
    """Raise if any pytest argument would need shell quoting on the remote."""
    for arg in args:
        if not arg or _UNSAFE & set(arg):
            raise UnsafeArgument(
                f"pytest argument {arg!r} contains characters that cannot be "
                "safely passed to the remote shell; re-run with GATE_NO_REMOTE=1"
            )


def probe_command(cfg: RemoteConfig, timeout: int = 3) -> list[str]:
    """An ssh invocation that succeeds iff the host is reachable non-interactively.

    BatchMode stops ssh prompting for a password/passphrase, which would
    otherwise hang a commit hook forever waiting on stdin.
    """
    return [
        "ssh",
        "-o", f"ConnectTimeout={timeout}",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        cfg.host,
        "exit",
    ]


def remote_command(cfg: RemoteConfig, command: str, *, base: bool = False) -> list[str]:
    """Wrap a command string so it runs on the remote.

    `base=True` runs in the configured directory (where the venv lives and
    where workspaces are extracted); otherwise it runs in this checkout's
    workspace, which is where the code under test actually is.
    """
    directory = cfg.directory if base else cfg.run_dir
    return ["ssh", "-o", "BatchMode=yes", cfg.host,
            cfg.cd_template.format(dir=directory, cmd=command)]


def tar_command(paths: Sequence[str] = SYNC_PATHS, prefix: str = "") -> list[str]:
    """Local half of the sync: stream a gzipped tar of the source to stdout.

    `prefix` re-roots every member under that directory, so the remote's plain
    `tar -xzf -` creates the workspace itself. That avoids needing a portable
    mkdir — cmd.exe, PowerShell and bash disagree about `mkdir -p`, and the
    whole transport deliberately assumes nothing beyond `cd X && Y`.
    """
    transform = [f"--transform=s,^,{prefix}/,"] if prefix else []
    return ["tar", "-czf", "-", "--exclude=__pycache__", "--exclude=*.pyc",
            *transform, "-C", str(ROOT), *paths]


def pytest_command(cfg: RemoteConfig, args: Sequence[str]) -> list[str]:
    """Full ssh argv that runs pytest on the remote, via the bootstrap hook.

    Invoking this module rather than pytest directly keeps the staleness check
    on the far side, where it is plain Python — reading a file over SSH would
    otherwise mean a shell one-liner, and `cat` vs `type` differ between bash
    and cmd.exe. Nothing here needs quoting beyond the args already validated
    by check_args.
    """
    check_args(args)
    check_args([cfg.lock])
    inner = (
        f"{cfg.python} scripts/remote_test.py --bootstrap --lock {cfg.lock} "
        f"-n {cfg.jobs} " + " ".join(args)
    )
    # Runs in the workspace, whose freshly synced scripts/ is the copy that
    # matches the code under test.
    return remote_command(cfg, inner.strip())


# ── remote side (runs on the test host, under --bootstrap) ─────────────────────


def lock_hash(root: Path = ROOT) -> str:
    """Digest of the compiled pins, used to detect a stale remote venv."""
    digest = hashlib.sha256()
    for name in LOCK_FILES:  # fixed order — the hash must be reproducible
        path = root / name
        digest.update(name.encode())
        digest.update(path.read_bytes() if path.exists() else b"")
    return digest.hexdigest()


def stamp_dir(python: str | None = None) -> Path:
    """Where the venv-freshness stamp lives: beside the venv, not in a workspace.

    Workspaces are per-checkout and disposable; the venv is shared and costs
    gigabytes to rebuild. Keying the stamp to the interpreter means every
    workspace using that venv agrees about whether it is current.
    """
    # Deliberately not resolve()d: this always runs on the remote against its
    # own native path, and resolving a Windows path from a Linux test would
    # silently graft the cwd onto the front of it.
    exe = Path(python or sys.executable)
    parent = exe.parent
    # .../.venv/bin/python or ...\.venv\Scripts\python.exe → .../.venv. A system
    # python (/usr/bin/python3) also has a "bin" parent, so only strip when the
    # grandparent actually looks like a venv — otherwise keep the bin dir.
    if parent.name in ("bin", "Scripts") and "venv" in parent.parent.name.lower():
        return parent.parent
    return parent


def read_stamp(root: Path) -> str:
    try:
        return (root / STAMP_FILE).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def write_stamp(root: Path, digest: str) -> None:
    try:
        (root / STAMP_FILE).write_text(digest, encoding="utf-8")
    except OSError:
        # A read-only venv directory just means we re-check next run.
        pass


def prune_to_manifest(root: Path) -> list[str]:
    """Delete files under the synced roots that this sync did not ship.

    tar only adds and overwrites, so without this a file deleted from the
    branch — or left behind by a different branch tested here earlier — keeps
    running as a phantom test. Returns what was removed, for reporting.
    """
    manifest = root / MANIFEST_FILE
    try:
        shipped = {
            line.strip() for line in
            manifest.read_text(encoding="utf-8").splitlines() if line.strip()
        }
    except OSError:
        return []  # Older sender: leave the tree alone rather than guess.

    removed: list[str] = []
    for entry in SYNC_PATHS:
        target = root / entry
        if not target.is_dir():
            continue
        for path in sorted(target.rglob("*"), reverse=True):
            if path.is_dir():
                continue
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            rel = path.relative_to(root).as_posix()
            if rel not in shipped:
                try:
                    path.unlink()
                    removed.append(rel)
                except OSError:
                    pass
    return removed


def needs_install(root: Path, expected: str) -> bool:
    """True when the remote venv was built from different pins than we shipped."""
    return read_stamp(root) != expected


def install_deps(python: str, root: Path, lock: str = DEFAULT_LOCK) -> int:
    return subprocess.run(
        [python, "-m", "pip", "install", "-r", lock], cwd=root
    ).returncode


def bootstrap(
    args: Sequence[str],
    root: Path | None = None,
    python: str | None = None,
    lock: str = DEFAULT_LOCK,
) -> int:
    """Entry point executed **on the remote**: sync deps, then run pytest.

    Returns BOOTSTRAP_FAILED if the venv could not be brought up to date, so
    the calling side falls back to a local run rather than reporting a
    dependency problem as a test failure.
    """
    root = Path.cwd() if root is None else root
    python = sys.executable if python is None else python

    removed = prune_to_manifest(root)
    if removed:
        shown = ", ".join(removed[:5]) + (f" (+{len(removed) - 5} more)" if len(removed) > 5 else "")
        print(f"remote-test: pruned {len(removed)} stale file(s): {shown}", flush=True)

    stamps = stamp_dir(python)
    expected = lock_hash(root)
    if needs_install(stamps, expected):
        print("remote-test: dependency lock changed — reinstalling remote venv", flush=True)
        if install_deps(python, root, lock) != 0:
            print("remote-test: remote pip install failed", file=sys.stderr, flush=True)
            return BOOTSTRAP_FAILED
        write_stamp(stamps, expected)

    return subprocess.run([python, "-m", "pytest", *args], cwd=root).returncode


def is_available(cfg: RemoteConfig, timeout: int = 3) -> bool:
    """True if the remote answers promptly. Any failure means 'run locally'."""
    if shutil.which("ssh") is None:
        return False
    try:
        result = subprocess.run(
            probe_command(cfg, timeout),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout + 2,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def manifest_lines(paths: Sequence[str] = SYNC_PATHS, root: Path = ROOT) -> list[str]:
    """Every file the sync ships, as forward-slash paths relative to the root.

    Shipped alongside the tree so the remote can delete anything it still has
    that we did not send. tar only ever adds and overwrites, so without this a
    file removed from a branch — or belonging to a branch tested here earlier —
    survives indefinitely and runs as a phantom test.
    """
    out: list[str] = []
    for entry in paths:
        target = root / entry
        if target.is_file():
            out.append(entry)
            continue
        for path in sorted(target.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            out.append(path.relative_to(root).as_posix())
    return out


def sync(cfg: RemoteConfig) -> bool:
    """Ship the source tree into this checkout's workspace.

    Returns False if the transfer failed, which the caller treats as "run
    locally" rather than reporting a broken transfer as a test failure.
    """
    # Extract at the base: the archive carries the workspace directory as its
    # own prefix, so tar creates it and no remote mkdir is needed.
    extract = remote_command(cfg, "tar -xzf -", base=bool(cfg.workspace))

    with TemporaryDirectory() as staging:
        manifest = Path(staging) / MANIFEST_FILE
        manifest.write_text("\n".join(manifest_lines()) + "\n", encoding="utf-8")
        argv = tar_command(prefix=cfg.workspace)
        # A second -C switches directory for the members that follow, so the
        # manifest joins the stream without ever being written into the repo.
        argv += ["-C", staging, MANIFEST_FILE]

        tar = subprocess.Popen(argv, stdout=subprocess.PIPE)
        assert tar.stdout is not None
        try:
            pushed = subprocess.run(extract, stdin=tar.stdout)
        finally:
            tar.stdout.close()
            tar.wait()
    return pushed.returncode == 0 and tar.returncode == 0


def run(args: Sequence[str], env: Mapping[str, str] | None = None) -> int | None:
    """Run pytest remotely.

    Returns the remote exit code, or **None** when the caller should fall back
    to running locally (not configured, host down, sync failed, or an argument
    that could not be safely quoted).
    """
    try:
        cfg = load_config(env)
    except ValueError as exc:
        print(f"remote-test: {exc}", file=sys.stderr)
        return None

    if cfg is None:
        return None

    try:
        check_args(args)
    except UnsafeArgument as exc:
        print(f"remote-test: {exc} — running locally.", file=sys.stderr)
        return None

    if not is_available(cfg):
        print(f"remote-test: {cfg.host} unreachable — running locally.", flush=True)
        return None

    print(f"remote-test: dispatching to {cfg.host} (-n {cfg.jobs})", flush=True)
    if not sync(cfg):
        print("remote-test: sync failed — running locally.", file=sys.stderr)
        return None

    code = subprocess.run(pytest_command(cfg, args)).returncode
    if code == BOOTSTRAP_FAILED:
        # The remote couldn't ready its venv. That's an environment problem,
        # not a test result — don't let it read as a red suite.
        print("remote-test: remote setup failed — running locally.", file=sys.stderr)
        return None
    return code


if __name__ == "__main__":
    # Only ever invoked on the remote, by pytest_command() above.
    if len(sys.argv) > 1 and sys.argv[1] == "--bootstrap":
        rest = sys.argv[2:]
        lock = DEFAULT_LOCK
        if len(rest) >= 2 and rest[0] == "--lock":
            lock, rest = rest[1], rest[2:]
        sys.exit(bootstrap(rest, lock=lock))
    print(__doc__)
    sys.exit(0)
