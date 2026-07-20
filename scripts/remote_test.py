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
from typing import Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]

# Compiled pins. Shipped so the remote can detect when its venv is stale, and
# reinstall from the same file this side is using.
LOCK_FILES = ("requirements.lock", "requirements-dev.lock")

# Only these are shipped. Everything else (the DB, models/, backups/, .venv,
# .git) is either irrelevant to the suite or far too large to sync per run.
SYNC_PATHS = ("src", "tests", "scripts", "pyproject.toml", *LOCK_FILES)

# Records which lock hash the remote venv was last installed from.
STAMP_FILE = ".remote-test-stamp"

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


def env_path(root: Path = ROOT) -> Path | None:
    """Locate the .env holding this config, or None.

    Prefers the current checkout. Project convention is to do edits in a git
    worktree, and worktrees have no .env of their own — so fall back to the
    main checkout, found via `git rev-parse --git-common-dir`. Without this,
    dispatch would silently never fire for the majority of commits.
    """
    local = root / ".env"
    if local.exists():
        return local

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=root, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None

    # --git-common-dir points at the main checkout's .git; its parent is the
    # working tree that owns the .env.
    candidate = (root / result.stdout.strip()).resolve().parent / ".env"
    return candidate if candidate.exists() else None


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

    return RemoteConfig(
        host=host,
        directory=directory,
        python=python,
        jobs=jobs,
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


def remote_command(cfg: RemoteConfig, command: str) -> list[str]:
    """Wrap a command string so it runs inside the remote repo directory."""
    return ["ssh", "-o", "BatchMode=yes", cfg.host,
            cfg.cd_template.format(dir=cfg.directory, cmd=command)]


def tar_command(paths: Sequence[str] = SYNC_PATHS) -> list[str]:
    """Local half of the sync: stream a gzipped tar of the source to stdout."""
    return ["tar", "-czf", "-", "--exclude=__pycache__", "--exclude=*.pyc",
            "-C", str(ROOT), *paths]


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


def read_stamp(root: Path) -> str:
    try:
        return (root / STAMP_FILE).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def write_stamp(root: Path, digest: str) -> None:
    (root / STAMP_FILE).write_text(digest, encoding="utf-8")


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

    expected = lock_hash(root)
    if needs_install(root, expected):
        print("remote-test: dependency lock changed — reinstalling remote venv", flush=True)
        if install_deps(python, root, lock) != 0:
            print("remote-test: remote pip install failed", file=sys.stderr, flush=True)
            return BOOTSTRAP_FAILED
        write_stamp(root, expected)

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


def sync(cfg: RemoteConfig) -> bool:
    """Ship the source tree. Returns False if the transfer failed."""
    extract = remote_command(cfg, "tar -xzf -")
    tar = subprocess.Popen(tar_command(), stdout=subprocess.PIPE)
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
