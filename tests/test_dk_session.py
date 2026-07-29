"""dk_session.py's argument parsing and path/command construction (pure logic).

The launcher turns free prose (`/dk-feature opus documentation review`) into a
branch name, a directory, a tmux window name, and a `claude --model` line that
all have to agree — so each transform gets a case. The subprocess plumbing
(git worktree add, tmux new-window) is glue and stays untested here.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import dk_session  # noqa: E402


# ── feature name normalization ───────────────────────────────────────────

@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (["documentation", "review"], "documentation-review"),
        (["Documentation", "Review"], "documentation-review"),
        (["casino_derby"], "casino-derby"),
        (["Pen Pals", "bank"], "pen-pals-bank"),
        (["economy/sinks"], "economy-sinks"),
        (["  spaced   out  "], "spaced-out"),
        (["--weird--name--"], "weird-name"),
        (["quest.digest"], "quest.digest"),
    ],
)
def test_normalize_name(tokens, expected):
    assert dk_session.normalize_name(tokens) == expected


@pytest.mark.parametrize("tokens", [[], ["---"], ["!!!"]])
def test_normalize_name_rejects_empty_result(tokens):
    """`new` treats an empty normalization as a usage error, so it must be falsy."""
    assert dk_session.normalize_name(tokens) == ""


# ── model detection ──────────────────────────────────────────────────────

@pytest.mark.parametrize("alias", ["opus", "sonnet", "haiku", "fable"])
def test_resolve_model_accepts_aliases(alias):
    assert dk_session.resolve_model(alias) == alias


def test_resolve_model_is_case_insensitive():
    assert dk_session.resolve_model("Opus") == "opus"


def test_resolve_model_passes_through_full_ids():
    assert dk_session.resolve_model("claude-opus-5") == "claude-opus-5"


@pytest.mark.parametrize("token", [None, "", "documentation", "review", "billy"])
def test_resolve_model_rejects_non_models(token):
    assert dk_session.resolve_model(token) is None


@pytest.mark.parametrize(
    ("tokens", "model", "rest"),
    [
        (["opus", "documentation", "review"], "opus", ["documentation", "review"]),
        (["sonnet", "casino"], "sonnet", ["casino"]),
        (["claude-opus-5", "casino"], "claude-opus-5", ["casino"]),
        (["documentation", "review"], None, ["documentation", "review"]),
        # A lone token is always the feature name — never a model with no name.
        (["opus"], None, ["opus"]),
    ],
)
def test_split_model(tokens, model, rest):
    assert dk_session.split_model(tokens) == (model, rest)


# ── paths and commands ───────────────────────────────────────────────────

def test_sessions_dir_is_a_sibling_of_the_prod_checkout():
    """Worktrees must not nest inside prod — that tree is the running bot."""
    main = Path("/home/ben/discord-bots/dungeon-keeper")
    assert dk_session.sessions_dir(main) == Path("/home/ben/discord-bots/dk-sessions")


def test_worktree_path_uses_the_branch_name():
    main = Path("/home/ben/discord-bots/dungeon-keeper")
    assert dk_session.worktree_path(main, "casino-derby") == Path(
        "/home/ben/discord-bots/dk-sessions/casino-derby"
    )


def test_claude_command_includes_the_model():
    assert dk_session.claude_command("opus").startswith("claude --model opus")


def test_claude_command_omits_model_when_unset():
    assert dk_session.claude_command(None).startswith("claude --remote-control")


def test_claude_command_keeps_the_window_alive():
    """Quitting claude must not close the window and orphan the worktree."""
    assert dk_session.claude_command("opus").endswith("; exec $SHELL")


def test_remote_control_is_on_by_default():
    assert "--remote-control" in dk_session.claude_command("opus", "casino-derby")


def test_remote_control_session_is_named_after_the_feature():
    """The name is only settable at launch — nothing can rename it later."""
    cmd = dk_session.claude_command("opus", "casino-derby")
    assert "--remote-control casino-derby" in cmd


def test_remote_control_can_be_opted_out():
    cmd = dk_session.claude_command("opus", "casino-derby", remote=False)
    assert "--remote-control" not in cmd
    assert "casino-derby" not in cmd  # the name only ever reached the remote flag
    assert cmd.startswith("claude --model opus ")


def test_auto_permission_mode_is_the_default():
    """A worker driven from a phone must not stall on a permission prompt."""
    assert "--permission-mode auto" in dk_session.claude_command("opus", "casino-derby")


@pytest.mark.parametrize("mode", ["manual", "plan", "acceptEdits", "bypassPermissions"])
def test_permission_mode_is_overridable(mode):
    cmd = dk_session.claude_command("opus", "x", permission_mode=mode)
    assert f"--permission-mode {mode}" in cmd
    assert "--permission-mode auto" not in cmd


def test_permission_mode_can_be_omitted_entirely():
    cmd = dk_session.claude_command("opus", "x", permission_mode=None)
    assert "--permission-mode" not in cmd


def test_default_permission_mode_is_not_bypass():
    """auto still runs calls past the classifier; bypass would not."""
    assert dk_session.DEFAULT_PERMISSION_MODE == "auto"
    assert dk_session.DEFAULT_PERMISSION_MODE in dk_session.PERMISSION_MODES


def test_no_brief_leaves_the_worker_idle():
    """Without a briefing the command ends at its flags — no stray positional."""
    cmd = dk_session.claude_command("opus", "casino-derby")
    assert cmd.endswith("--permission-mode auto; exec $SHELL")


def test_brief_is_appended_as_the_opening_prompt():
    cmd = dk_session.claude_command("opus", "casino-derby", brief="fix the parser")
    assert "'fix the parser'" in cmd
    # Must come after every flag, or claude reads it as a flag's value.
    assert cmd.index("fix the parser") > cmd.index("--permission-mode")


@pytest.mark.parametrize(
    "brief",
    [
        "it's a quote",                      # apostrophe
        'say "hello"',                       # double quotes
        "line one\nline two",                # newlines — briefings are multi-line
        "back\\slash and $VAR and `cmd`",    # shell metacharacters
    ],
)
def test_brief_survives_shell_quoting(brief):
    """A briefing is prose; it must not be able to break out into the shell."""
    cmd = dk_session.claude_command("opus", "x", brief=brief)
    tail = cmd[: -len("; exec $SHELL")]
    assert shlex.split(tail)[-1] == brief


def test_new_window_args_carries_the_brief():
    args = dk_session.new_window_args(
        "casino-derby", Path("/tmp/wt"), "opus", brief="ship the fix"
    )
    assert "'ship the fix'" in args[-1]


def test_new_window_args_names_window_after_branch():
    # Compare against str(path), not a literal: the suite also runs on the
    # Windows remote runner, where str(Path("/tmp/wt")) is "\\tmp\\wt".
    wt = Path("/tmp/wt")
    args = dk_session.new_window_args("casino-derby", wt, "opus")
    assert args[:3] == ["tmux", "new-window", "-d"]
    assert "-n" in args and args[args.index("-n") + 1] == "casino-derby"
    assert args[args.index("-c") + 1] == str(wt)


def test_new_window_passes_the_name_through_to_remote_control():
    """One name for the branch, the directory, the window, and Remote Control."""
    args = dk_session.new_window_args("casino-derby", Path("/tmp/wt"), "opus")
    assert "--remote-control casino-derby" in args[-1]


# ── base ref ─────────────────────────────────────────────────────────────

def test_sessions_branch_off_local_main_not_origin():
    """Regression: a session based on origin/main starts on stale code.

    The clone-per-session flow used origin/main safely because each clone's
    origin *was* the prod checkout. In a worktree origin is GitHub, so prod's
    main leads it by every unpushed commit — the first session cut after this
    change was 12 commits behind because of exactly that.
    """
    args = dk_session.worktree_add_args(
        Path("/home/ben/discord-bots/dungeon-keeper"),
        "casino-derby",
        Path("/home/ben/discord-bots/dk-sessions/casino-derby"),
    )
    assert args[-1] == "main"
    assert "origin/main" not in args


def test_worktree_add_does_not_track_its_base():
    """Without --no-track a stray `git push` from a session targets main."""
    args = dk_session.worktree_add_args(Path("/repo"), "feat", Path("/wt/feat"))
    assert "--no-track" in args
    assert args[args.index("-b") + 1] == "feat"


@pytest.mark.parametrize("behind", [1, 12, 300])
def test_staleness_warning_when_prod_trails_origin(behind):
    note = dk_session.staleness_warning(behind)
    assert note is not None
    assert str(behind) in note


def test_no_staleness_warning_when_prod_is_current():
    assert dk_session.staleness_warning(0) is None


# ── worker state ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "pane",
    [
        "Enter to select · ↑/↓ to navigate · n to add notes · Esc to cancel",
        "Do you want to proceed?",
        "❯ 1. Yes, I trust this folder",
    ],
)
def test_a_worker_showing_a_dialog_is_waiting(pane):
    """A blocked worker must never read as 'working' — that's how it goes unseen."""
    assert dk_session.session_state(pane) == "waiting"


def test_a_running_worker_is_working():
    pane = "⏵⏵ auto mode on (shift+tab to cycle) · esc to interrupt · ← for agents"
    assert dk_session.session_state(pane) == "working"


def test_a_worker_at_an_empty_prompt_is_idle():
    pane = "⏸ manual mode on · install gh for PR status · ← for agents"
    assert dk_session.session_state(pane) == "idle"


def test_a_question_during_a_run_reads_as_waiting():
    """Both markers present — the dialog wins, because a human is blocking it."""
    pane = (
        "● Running the backfill…\n"
        "esc to interrupt\n"
        "Do you want to proceed?\n"
    )
    assert dk_session.session_state(pane) == "waiting"


def test_state_detection_is_case_insensitive():
    assert dk_session.session_state("ENTER TO SELECT") == "waiting"


# ── worktree listing ─────────────────────────────────────────────────────

def test_parse_worktrees_reads_paths_and_branches():
    porcelain = (
        "worktree /home/ben/discord-bots/dungeon-keeper\n"
        "HEAD abc123\n"
        "branch refs/heads/main\n"
        "\n"
        "worktree /home/ben/discord-bots/dk-sessions/casino-derby\n"
        "HEAD def456\n"
        "branch refs/heads/casino-derby\n"
    )
    assert dk_session.parse_worktrees(porcelain) == [
        {"path": "/home/ben/discord-bots/dungeon-keeper", "branch": "main"},
        {
            "path": "/home/ben/discord-bots/dk-sessions/casino-derby",
            "branch": "casino-derby",
        },
    ]


def test_parse_worktrees_marks_detached_heads():
    porcelain = "worktree /tmp/wt\nHEAD abc123\ndetached\n"
    assert dk_session.parse_worktrees(porcelain) == [
        {"path": "/tmp/wt", "branch": "(detached)"}
    ]


def test_parse_worktrees_handles_empty_input():
    assert dk_session.parse_worktrees("") == []


# ── agent scratch cleanup ────────────────────────────────────────────────

def test_agent_tmp_name_mangles_slashes():
    """The agent names a session's scratch dir after its cwd, / -> -."""
    assert dk_session.agent_tmp_name(
        Path("/home/ben/discord-bots/dk-sessions/casino-derby")
    ) == "-home-ben-discord-bots-dk-sessions-casino-derby"


@pytest.mark.skipif(os.name != "posix", reason=
    "mangles an absolute path into a single filename; a Windows drive letter\n     makes that name illegal. The code path is /tmp/claude-<uid>, POSIX-only.")
def test_agent_tmp_dirs_finds_the_matching_scratch(tmp_path):
    wt = Path("/home/ben/discord-bots/dk-sessions/casino-derby")
    scratch = tmp_path / "claude-1000" / dk_session.agent_tmp_name(wt)
    scratch.mkdir(parents=True)
    assert dk_session.agent_tmp_dirs(wt, tmp_root=tmp_path) == [scratch]


@pytest.mark.skipif(os.name != "posix", reason=
    "mangles an absolute path into a single filename; a Windows drive letter\n     makes that name illegal. The code path is /tmp/claude-<uid>, POSIX-only.")
def test_agent_tmp_dirs_ignores_unrelated_dirs(tmp_path):
    wt = Path("/home/ben/discord-bots/dk-sessions/casino-derby")
    (tmp_path / "claude-1000" / "-home-ben-somewhere-else").mkdir(parents=True)
    (tmp_path / "not-claude" / dk_session.agent_tmp_name(wt)).mkdir(parents=True)
    assert dk_session.agent_tmp_dirs(wt, tmp_root=tmp_path) == []


@pytest.mark.parametrize("wt", [Path("/"), Path("/a")])
def test_agent_tmp_dirs_refuses_degenerate_paths(wt, tmp_path):
    """This drives rm -rf under /tmp — a one-char or empty name must not match."""
    assert dk_session.agent_tmp_dirs(wt, tmp_root=tmp_path) == []


@pytest.mark.skipif(os.name != "posix", reason=
    "mangles an absolute path into a single filename; a Windows drive letter\n     makes that name illegal. The code path is /tmp/claude-<uid>, POSIX-only.")
def test_orphan_scratch_dirs_only_lists_dead_sessions(tmp_path):
    sessions = tmp_path / "dk-sessions"
    (sessions / "alive").mkdir(parents=True)
    pre = sessions.as_posix().replace("/", "-") + "-"
    claude = tmp_path / "claude-1000"
    (claude / f"{pre}alive").mkdir(parents=True)
    (claude / f"{pre}dead").mkdir(parents=True)
    (claude / "-home-ben-unrelated").mkdir(parents=True)
    found = dk_session.orphan_scratch_dirs(sessions, tmp_root=tmp_path)
    assert found == [claude / f"{pre}dead"]


# ── platform independence ────────────────────────────────────────────────
#
# The suite dispatches to a Windows remote runner, which has twice caught a
# POSIX assumption in this module — first a tmux -c literal, then the scratch
# mangling. Both failed only on the remote, minutes after passing locally.
# Feeding both flavours here makes the next one fail on this machine instead.

@pytest.mark.parametrize("flavour", [PurePosixPath, PureWindowsPath])
def test_agent_tmp_name_is_platform_independent(flavour):
    assert dk_session.agent_tmp_name(
        flavour("/home/ben/discord-bots/dk-sessions/casino-derby")
    ) == "-home-ben-discord-bots-dk-sessions-casino-derby"


@pytest.mark.parametrize("flavour", [PurePosixPath, PureWindowsPath])
def test_claude_command_is_platform_independent(flavour):
    """new_window_args embeds a path; it must read the same on either host."""
    args = dk_session.new_window_args("x", flavour("/tmp/wt"), "opus")
    assert args[args.index("-c") + 1] == str(flavour("/tmp/wt"))


# ── recovering pre-commit's stash ────────────────────────────────────────

def test_newest_precommit_patch_picks_the_latest(tmp_path):
    old, new = tmp_path / "patch100", tmp_path / "patch200"
    old.write_text("a", encoding="utf-8")
    new.write_text("b", encoding="utf-8")
    os.utime(old, (1, 1))
    os.utime(new, (2, 2))
    assert dk_session.newest_precommit_patch(tmp_path) == new


def test_newest_precommit_patch_handles_no_cache(tmp_path):
    assert dk_session.newest_precommit_patch(tmp_path / "nope") is None
    assert dk_session.newest_precommit_patch(tmp_path) is None


def test_patch_files_lists_what_would_be_restored():
    patch = (
        "diff --git a/scripts/gate.py b/scripts/gate.py\n"
        "index 1..2 100644\n"
        "--- a/scripts/gate.py\n"
        "+++ b/scripts/gate.py\n"
        "@@ -1 +1 @@\n"
        "-x\n+y\n"
        "diff --git a/tests/test_gate_scope.py b/tests/test_gate_scope.py\n"
    )
    assert dk_session.patch_files(patch) == [
        "scripts/gate.py", "tests/test_gate_scope.py",
    ]


def test_patch_files_on_an_empty_patch():
    assert dk_session.patch_files("") == []


# ── venv symlink into a fresh worktree ────────────────────────────────────────


def test_link_venv_points_the_session_at_prod(tmp_path):
    main = tmp_path / "prod"
    (main / ".venv" / "bin").mkdir(parents=True)
    session = tmp_path / "dk-sessions" / "feat"
    session.mkdir(parents=True)

    msg = dk_session.link_venv(main, session)

    link = session / ".venv"
    assert link.is_symlink()
    assert link.resolve() == (main / ".venv").resolve()
    assert msg is not None and ".venv" in msg


def test_link_venv_is_relative_so_the_tree_can_move(tmp_path):
    """An absolute link breaks the moment dk-sessions/ is relocated."""
    main = tmp_path / "prod"
    (main / ".venv").mkdir(parents=True)
    session = tmp_path / "dk-sessions" / "feat"
    session.mkdir(parents=True)

    dk_session.link_venv(main, session)
    target = os.readlink(session / ".venv")

    assert not os.path.isabs(target)
    moved = tmp_path / "moved"
    (tmp_path / "dk-sessions").rename(moved)
    assert (moved / "feat" / ".venv").resolve() == (main / ".venv").resolve()


def test_link_venv_leaves_an_existing_venv_alone(tmp_path):
    """A session that already has a real .venv must not have it replaced."""
    main = tmp_path / "prod"
    (main / ".venv").mkdir(parents=True)
    session = tmp_path / "dk-sessions" / "feat"
    (session / ".venv").mkdir(parents=True)

    assert dk_session.link_venv(main, session) is None
    assert not (session / ".venv").is_symlink()


def test_link_venv_without_a_prod_venv_is_not_fatal(tmp_path):
    main = tmp_path / "prod"
    main.mkdir()
    session = tmp_path / "dk-sessions" / "feat"
    session.mkdir(parents=True)

    msg = dk_session.link_venv(main, session)

    assert msg is not None and "skipped" in msg
    assert not (session / ".venv").exists()
