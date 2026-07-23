"""Remote pytest dispatch — config parsing, command building, and fallback.

The contract that matters: this must never block a commit. Every "can't
dispatch" path has to return None so gate.py runs locally instead. These
tests exercise that without touching the network.
"""

from __future__ import annotations

import pytest
from pathlib import Path

from scripts import remote_test as rt

FULL_ENV = {
    "REMOTE_TEST_HOST": "ben@bigbox",
    "REMOTE_TEST_DIR": "C:/dev/dungeon-keeper",
    "REMOTE_TEST_PYTHON": "C:/dev/dungeon-keeper/.venv/Scripts/python.exe",
}


# ── load_config ────────────────────────────────────────────────────────────────


def test_unset_host_means_local():
    assert rt.load_config({}) is None


def test_blank_host_means_local():
    assert rt.load_config({"REMOTE_TEST_HOST": "   "}) is None


@pytest.mark.parametrize("flag", ["1", "true", "yes"])
def test_gate_no_remote_forces_local(flag):
    assert rt.load_config({**FULL_ENV, "GATE_NO_REMOTE": flag}) is None


def test_gate_no_remote_ignores_unrecognised_values():
    """A typo'd opt-out must not silently keep dispatching... or silently stop."""
    cfg = rt.load_config({**FULL_ENV, "GATE_NO_REMOTE": "maybe"})
    assert cfg is not None and cfg.host == "ben@bigbox"


def test_host_without_dir_or_python_raises():
    """Half-configured is a mistake, not a reason to quietly run locally."""
    with pytest.raises(ValueError, match="refusing to guess"):
        rt.load_config({"REMOTE_TEST_HOST": "ben@bigbox"})

    with pytest.raises(ValueError, match="refusing to guess"):
        rt.load_config({"REMOTE_TEST_HOST": "ben@bigbox", "REMOTE_TEST_DIR": "/x"})


def test_defaults_applied():
    cfg = rt.load_config(FULL_ENV)
    assert cfg is not None
    assert cfg.jobs == rt.DEFAULT_JOBS
    assert cfg.cd_template == rt.DEFAULT_CD


def test_jobs_override():
    cfg = rt.load_config({**FULL_ENV, "REMOTE_TEST_JOBS": "16"})
    assert cfg is not None and cfg.jobs == 16


@pytest.mark.parametrize("bad", ["abc", "1.5", "0", "-4"])
def test_invalid_jobs_rejected(bad):
    with pytest.raises(ValueError):
        rt.load_config({**FULL_ENV, "REMOTE_TEST_JOBS": bad})


def test_cd_template_override_for_cmd_exe():
    cfg = rt.load_config({**FULL_ENV, "REMOTE_TEST_CD": "cd /d {dir} && {cmd}"})
    assert cfg is not None
    assert rt.remote_command(cfg, "echo hi")[-1] == "cd /d C:/dev/dungeon-keeper && echo hi"


# ── argument safety ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "arg",
    [
        "tests/test_a.py; rm -rf /",
        "tests/test a.py",
        "-k spam and eggs",
        'tests/"quoted".py',
        "tests/$(whoami).py",
        "tests/a.py|b",
        "",
    ],
)
def test_unsafe_arguments_rejected(arg):
    with pytest.raises(rt.UnsafeArgument):
        rt.check_args([arg])


@pytest.mark.parametrize(
    "arg",
    ["tests/test_ollama_client.py", "-x", "--maxfail=2", "-ktest_foo"],
)
def test_safe_arguments_accepted(arg):
    rt.check_args([arg])  # must not raise


def test_run_falls_back_locally_on_unsafe_argument(capsys):
    """An argument we can't quote means local, not a mangled remote run."""
    assert rt.run(["-k", "spam and eggs"], env=FULL_ENV) is None
    assert "running locally" in capsys.readouterr().err


# ── command construction ───────────────────────────────────────────────────────


def test_probe_command_is_non_interactive():
    cfg = rt.load_config(FULL_ENV)
    assert cfg is not None
    cmd = rt.probe_command(cfg, timeout=5)

    # BatchMode is what stops ssh hanging a commit hook on a password prompt.
    assert "BatchMode=yes" in cmd
    assert "ConnectTimeout=5" in cmd
    assert cmd[-2:] == ["ben@bigbox", "exit"]


def test_pytest_command_includes_targets_and_jobs():
    cfg = rt.load_config({**FULL_ENV, "REMOTE_TEST_JOBS": "8"})
    assert cfg is not None
    inner = rt.pytest_command(cfg, ["tests/test_a.py", "tests/test_b.py"])[-1]

    assert inner.startswith("cd C:/dev/dungeon-keeper && ")
    assert "scripts/remote_test.py --bootstrap --lock requirements-dev.lock -n 8 " in inner
    assert inner.endswith("tests/test_a.py tests/test_b.py")


def test_pytest_command_with_no_targets_has_no_trailing_space():
    cfg = rt.load_config(FULL_ENV)
    assert cfg is not None
    assert rt.pytest_command(cfg, [])[-1].endswith("--lock requirements-dev.lock -n 12")


def test_pytest_command_goes_through_bootstrap_not_pytest_directly():
    """The staleness check must not be bypassable by the happy path."""
    cfg = rt.load_config(FULL_ENV)
    assert cfg is not None
    inner = rt.pytest_command(cfg, [])[-1]
    assert "--bootstrap" in inner
    assert "-m pytest" not in inner


# ── remote-side bootstrap ──────────────────────────────────────────────────────


def _write_locks(root, main="a", dev="b"):
    (root / "requirements.lock").write_text(main, encoding="utf-8")
    (root / "requirements-dev.lock").write_text(dev, encoding="utf-8")


def test_lock_hash_is_stable_and_content_sensitive(tmp_path):
    _write_locks(tmp_path)
    first = rt.lock_hash(tmp_path)

    assert first == rt.lock_hash(tmp_path), "hash must be reproducible"

    (tmp_path / "requirements-dev.lock").write_text("changed", encoding="utf-8")
    assert rt.lock_hash(tmp_path) != first


def test_lock_hash_distinguishes_which_file_changed(tmp_path):
    """Swapping content between the two locks must not collide."""
    _write_locks(tmp_path, main="x", dev="y")
    one = rt.lock_hash(tmp_path)
    _write_locks(tmp_path, main="y", dev="x")
    assert rt.lock_hash(tmp_path) != one


def test_lock_hash_tolerates_a_missing_lock(tmp_path):
    (tmp_path / "requirements.lock").write_text("only-one", encoding="utf-8")
    assert rt.lock_hash(tmp_path)  # does not raise


def test_needs_install_when_no_stamp(tmp_path):
    _write_locks(tmp_path)
    assert rt.needs_install(tmp_path, rt.lock_hash(tmp_path)) is True


def test_needs_install_false_after_stamp_written(tmp_path):
    _write_locks(tmp_path)
    digest = rt.lock_hash(tmp_path)
    rt.write_stamp(tmp_path, digest)
    assert rt.needs_install(tmp_path, digest) is False


def test_needs_install_true_after_lock_changes(tmp_path):
    _write_locks(tmp_path)
    rt.write_stamp(tmp_path, rt.lock_hash(tmp_path))

    (tmp_path / "requirements-dev.lock").write_text("bumped", encoding="utf-8")
    assert rt.needs_install(tmp_path, rt.lock_hash(tmp_path)) is True


def test_read_stamp_survives_a_corrupt_file(tmp_path):
    (tmp_path / rt.STAMP_FILE).write_bytes(b"\xff\xfe\x00garbage")
    assert rt.read_stamp(tmp_path) == ""  # treated as "needs install"


def test_bootstrap_skips_install_when_stamp_current(tmp_path, monkeypatch):
    _write_locks(tmp_path)
    rt.write_stamp(tmp_path, rt.lock_hash(tmp_path))

    calls = []
    monkeypatch.setattr(rt, "install_deps", lambda py, root, lock: calls.append("install") or 0)
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())

    assert rt.bootstrap(["-q"], root=tmp_path, python="py") == 0
    assert calls == []


def test_bootstrap_installs_then_stamps_when_stale(tmp_path, monkeypatch):
    _write_locks(tmp_path)

    monkeypatch.setattr(rt, "install_deps", lambda py, root, lock: 0)
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())

    assert rt.bootstrap(["-q"], root=tmp_path, python="py") == 0
    assert rt.read_stamp(tmp_path) == rt.lock_hash(tmp_path)


def test_bootstrap_returns_sentinel_when_install_fails(tmp_path, monkeypatch):
    _write_locks(tmp_path)
    monkeypatch.setattr(rt, "install_deps", lambda py, root, lock: 1)

    ran = []
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: ran.append(a) or None)

    assert rt.bootstrap(["-q"], root=tmp_path, python="py") == rt.BOOTSTRAP_FAILED
    assert ran == [], "pytest must not run against a half-installed venv"
    assert rt.read_stamp(tmp_path) == "", "a failed install must not be stamped"


def test_bootstrap_sentinel_cannot_collide_with_pytest_exit_codes():
    assert rt.BOOTSTRAP_FAILED > 5


def test_bootstrap_propagates_test_failure(tmp_path, monkeypatch):
    _write_locks(tmp_path)
    rt.write_stamp(tmp_path, rt.lock_hash(tmp_path))
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 1})())

    assert rt.bootstrap(["-q"], root=tmp_path, python="py") == 1


def test_run_falls_back_locally_on_bootstrap_sentinel(monkeypatch, capsys):
    """A broken remote venv is an environment problem, not a red suite."""
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: True)
    monkeypatch.setattr(
        rt.subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": rt.BOOTSTRAP_FAILED})(),
    )

    assert rt.run(["tests/test_a.py"], env=FULL_ENV) is None
    assert "remote setup failed" in capsys.readouterr().err


def test_locks_are_synced_so_the_remote_can_detect_staleness():
    for name in rt.LOCK_FILES:
        assert name in rt.SYNC_PATHS


def test_readme_is_synced_so_its_doc_test_cant_go_stale():
    # test_games_help_logic.py checks README.md's party-game count against
    # the code — without shipping it every run, a remote's only copy is
    # whatever its initial `git clone` had, drifting silently forever after.
    assert "README.md" in rt.SYNC_PATHS


# ── .env fallback ──────────────────────────────────────────────────────────────


def _write_env(root, body):
    (root / ".env").write_text(body, encoding="utf-8")


def test_env_file_values_reads_only_our_keys(tmp_path):
    _write_env(tmp_path, "\n".join([
        "DISCORD_TOKEN_PROD=supersecret",
        "REMOTE_TEST_HOST=ben@box",
        "REMOTE_TEST_DIR=C:/dev/dk",
        "GATE_NO_REMOTE=0",
        "SESSION_SECRET=alsosecret",
    ]))
    values = rt.env_file_values(tmp_path)

    assert values == {
        "REMOTE_TEST_HOST": "ben@box",
        "REMOTE_TEST_DIR": "C:/dev/dk",
        "GATE_NO_REMOTE": "0",
    }
    # Secrets in .env must not be hoovered up by a dev-tooling helper.
    assert "DISCORD_TOKEN_PROD" not in values
    assert "SESSION_SECRET" not in values


def test_env_file_values_strips_comments_and_quotes(tmp_path):
    _write_env(tmp_path, '\n'.join([
        "# a comment",
        "",
        'REMOTE_TEST_HOST="ben@box"   # trailing note',
        "REMOTE_TEST_DIR='C:/dev/dk'",
    ]))
    values = rt.env_file_values(tmp_path)
    assert values["REMOTE_TEST_HOST"] == "ben@box"
    assert values["REMOTE_TEST_DIR"] == "C:/dev/dk"


def test_env_file_values_missing_file_is_empty(tmp_path):
    assert rt.env_file_values(tmp_path) == {}


def test_env_path_prefers_the_local_checkout(tmp_path):
    _write_env(tmp_path, "REMOTE_TEST_HOST=ben@box")
    assert rt.env_path(tmp_path) == tmp_path / ".env"


def test_env_path_falls_back_to_the_main_checkout_from_a_worktree(tmp_path, monkeypatch):
    """Edits happen in worktrees, which have no .env of their own."""
    main = tmp_path / "main"
    (main / ".git").mkdir(parents=True)
    _write_env(main, "REMOTE_TEST_HOST=ben@box")

    worktree = tmp_path / "wt"
    worktree.mkdir()

    def fake_git(cmd, **kwargs):
        assert cmd[:2] == ["git", "rev-parse"]
        return type("R", (), {"returncode": 0, "stdout": str(main / ".git")})()

    monkeypatch.setattr(rt.subprocess, "run", fake_git)
    assert rt.env_path(worktree) == main / ".env"


def test_env_path_is_none_when_git_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(
        rt.subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": 128, "stdout": ""})(),
    )
    assert rt.env_path(tmp_path) is None


def test_env_path_survives_git_being_absent(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("no git")

    monkeypatch.setattr(rt.subprocess, "run", boom)
    assert rt.env_path(tmp_path) is None


def test_env_file_values_ignores_malformed_lines(tmp_path):
    _write_env(tmp_path, "REMOTE_TEST_HOST\nnot a setting\nREMOTE_TEST_DIR=C:/x")
    assert rt.env_file_values(tmp_path) == {"REMOTE_TEST_DIR": "C:/x"}


def test_real_environment_overrides_env_file(monkeypatch, tmp_path):
    """GATE_NO_REMOTE=1 on the command line must beat .env without editing it."""
    monkeypatch.setattr(rt, "env_file_values", lambda root=None: dict(FULL_ENV))
    monkeypatch.setenv("GATE_NO_REMOTE", "1")
    assert rt.load_config() is None


def test_env_file_supplies_config_when_shell_has_none(monkeypatch):
    monkeypatch.setattr(rt, "env_file_values", lambda root=None: dict(FULL_ENV))
    for key in FULL_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("GATE_NO_REMOTE", raising=False)

    cfg = rt.load_config()
    assert cfg is not None and cfg.host == "ben@bigbox"


# ── per-host lock file ─────────────────────────────────────────────────────────


def test_lock_defaults_to_the_dev_lock():
    cfg = rt.load_config(FULL_ENV)
    assert cfg is not None and cfg.lock == rt.DEFAULT_LOCK


def test_lock_override_reaches_the_remote_command():
    cfg = rt.load_config({**FULL_ENV, "REMOTE_TEST_LOCK": "requirements-win.lock"})
    assert cfg is not None and cfg.lock == "requirements-win.lock"
    assert "--lock requirements-win.lock" in rt.pytest_command(cfg, [])[-1]


def test_lock_with_unsafe_characters_is_rejected():
    cfg = rt.load_config({**FULL_ENV, "REMOTE_TEST_LOCK": "a lock.txt; rm -rf /"})
    assert cfg is not None
    with pytest.raises(rt.UnsafeArgument):
        rt.pytest_command(cfg, [])


def test_install_deps_uses_the_configured_lock(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(rt.subprocess, "run", fake_run)
    rt.install_deps("py", Path("/tmp"), "requirements-win.lock")
    assert seen["cmd"][-1] == "requirements-win.lock"


def test_bootstrap_passes_lock_through_to_install(tmp_path, monkeypatch):
    _write_locks(tmp_path)
    seen = {}
    monkeypatch.setattr(rt, "install_deps",
                        lambda py, root, lock: seen.setdefault("lock", lock) or 0)
    monkeypatch.setattr(rt.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0})())

    rt.bootstrap(["-q"], root=tmp_path, python="py", lock="requirements-win.lock")
    assert seen["lock"] == "requirements-win.lock"


def test_tar_command_excludes_caches_and_never_ships_secrets():
    cmd = rt.tar_command()
    assert "--exclude=__pycache__" in cmd

    # The suite needs no .env, database, or model weights — so none are synced.
    joined = " ".join(cmd)
    for secret in (".env", "dungeonkeeper.db", "models", "backups", ".git"):
        assert secret not in rt.SYNC_PATHS
    assert "src" in cmd and "tests" in cmd
    assert "dungeonkeeper.db" not in joined


# ── fallback wiring ────────────────────────────────────────────────────────────


def test_run_returns_none_when_unconfigured():
    assert rt.run(["tests/test_a.py"], env={}) is None


def test_run_returns_none_on_bad_config_instead_of_raising(capsys):
    """gate.py must not crash because REMOTE_TEST_* is half-set."""
    assert rt.run(["tests/test_a.py"], env={"REMOTE_TEST_HOST": "h"}) is None
    assert "refusing to guess" in capsys.readouterr().err


def test_run_falls_back_when_host_unreachable(monkeypatch, capsys):
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: False)
    assert rt.run(["tests/test_a.py"], env=FULL_ENV) is None
    assert "unreachable" in capsys.readouterr().out


def test_run_falls_back_when_sync_fails(monkeypatch, capsys):
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: False)
    assert rt.run(["tests/test_a.py"], env=FULL_ENV) is None
    assert "sync failed" in capsys.readouterr().err


def test_run_returns_remote_exit_code_on_success(monkeypatch):
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: True)

    class _Completed:
        returncode = 0

    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _Completed())
    assert rt.run(["tests/test_a.py"], env=FULL_ENV) == 0


def test_run_propagates_remote_failure(monkeypatch):
    """A remote failure must fail the gate, not silently retry locally."""
    monkeypatch.setattr(rt, "is_available", lambda cfg, timeout=3: True)
    monkeypatch.setattr(rt, "sync", lambda cfg: True)

    class _Failed:
        returncode = 1

    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _Failed())
    assert rt.run(["tests/test_a.py"], env=FULL_ENV) == 1


def test_is_available_is_false_without_ssh(monkeypatch):
    monkeypatch.setattr(rt.shutil, "which", lambda name: None)
    cfg = rt.load_config(FULL_ENV)
    assert cfg is not None
    assert rt.is_available(cfg) is False
