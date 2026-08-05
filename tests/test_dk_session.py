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


# ── surviving a reboot: snapshot ─────────────────────────────────────────

def _registry(tmp_path, pid, sid="s-1", cwd="/w", proc_start="1234", **extra):
    """Write one Claude Code registry file, the shape ~/.claude/sessions holds."""
    entry = {"pid": pid, "sessionId": sid, "cwd": cwd,
             "procStart": proc_start, "name": "n", **extra}
    (tmp_path / f"{pid}.json").write_text(__import__("json").dumps(entry))
    return entry


@pytest.mark.parametrize(
    ("recorded", "observed", "live"),
    [
        ("1234", "1234", True),    # same process
        ("1234", "9999", False),   # pid reused by something else
        ("1234", None, False),     # process is gone
        (None, "1234", True),      # no identity recorded — trust the pid
        (None, None, False),
    ],
)
def test_session_is_live(recorded, observed, live):
    assert dk_session.session_is_live({"procStart": recorded}, observed) is live


def test_read_live_sessions_keeps_only_running_processes(tmp_path):
    _registry(tmp_path, 100, sid="alive", cwd="/w/a", proc_start="11")
    _registry(tmp_path, 101, sid="dead", cwd="/w/b", proc_start="22")
    starts = {100: "11", 101: None}

    got = dk_session.read_live_sessions(tmp_path, proc_start=starts.get)

    assert [s["session_id"] for s in got] == ["alive"]
    assert got[0]["cwd"] == "/w/a"


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",                                  # half-written file
        '{"pid": 1}',                                 # no sessionId or cwd
        '{"sessionId": "x", "cwd": "/w"}',            # no pid
        '["a list"]',                                 # wrong shape entirely
    ],
)
def test_read_live_sessions_ignores_unusable_files(tmp_path, payload):
    """A snapshot runs every minute; one bad file must not break it."""
    (tmp_path / "1.json").write_text(payload)
    assert dk_session.read_live_sessions(tmp_path, proc_start=lambda p: "1") == []


def test_read_live_sessions_without_a_registry_dir(tmp_path):
    assert dk_session.read_live_sessions(tmp_path / "nope") == []


@pytest.mark.parametrize(
    ("old", "new", "empty_for", "replace"),
    [
        (3, 5, 0, True),        # normal snapshot
        (0, 0, 0, True),        # nothing to lose
        (3, 3, 0, True),        # still populated
        # THE case this whole guard exists for: shutdown SIGTERMs every claude,
        # each unlinks its registry file, and the last snapshot before the
        # reboot sees an empty machine. Overwriting here erases the manifest
        # seconds before the reboot it is meant to survive.
        (3, 0, 0, False),
        (3, 0, 60, False),
        (3, 0, dk_session.EMPTY_GRACE_SECONDS - 1, False),
        # Emptiness that persists means the sessions really were closed.
        (3, 0, dk_session.EMPTY_GRACE_SECONDS, True),
        (3, 0, 86400, True),
    ],
)
def test_should_replace_manifest(old, new, empty_for, replace):
    assert dk_session.should_replace_manifest(old, new, empty_for) is replace


def test_manifest_round_trips(tmp_path):
    path = tmp_path / "manifest.json"
    manifest = dk_session.build_manifest([{"session_id": "a", "cwd": "/w"}], 12.5)

    dk_session.write_manifest(manifest, path)

    assert dk_session.load_manifest(path) == manifest
    assert not path.with_suffix(".json.tmp").exists()  # atomic write cleaned up


def test_load_manifest_survives_a_corrupt_file(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text("{truncated")
    assert dk_session.load_manifest(path) == {}


def test_load_manifest_without_a_file(tmp_path):
    assert dk_session.load_manifest(tmp_path / "missing.json") == {}


# ── surviving a reboot: the restore plan ─────────────────────────────────

PROD = "/home/ben/discord-bots/dungeon-keeper"
SESSIONS = "/home/ben/discord-bots/dk-sessions"


def test_window_name_keeps_prod_as_main():
    assert dk_session.window_name(PROD, PROD) == "main"


def test_window_name_of_a_worktree_is_its_directory():
    assert dk_session.window_name(f"{SESSIONS}/casino-derby", PROD) == "casino-derby"


def _plan(sessions, dirty, exists=None, **kw):
    return dk_session.restore_plan(
        sessions, PROD,
        exists=exists or (lambda p: True),
        dirty_count=lambda p: dirty.get(p, 0),
        **kw,
    )


def test_dirty_trees_resume_and_clean_trees_come_back_as_shells():
    """The split the whole control turns on: respawning everything bills for
    workers you would have abandoned; a clean tree has nothing in flight."""
    plan = _plan(
        [{"session_id": "a", "cwd": f"{SESSIONS}/dirty"},
         {"session_id": "b", "cwd": f"{SESSIONS}/clean"}],
        dirty={f"{SESSIONS}/dirty": 7},
    )
    modes = {e["window"]: e["mode"] for e in plan}
    assert modes == {"dirty": "resume", "clean": "shell"}
    assert [e["dirty"] for e in plan if e["window"] == "dirty"] == [7]


def test_a_torn_down_worktree_is_reported_not_dropped():
    """A restore must never silently lose something it was asked about."""
    plan = _plan(
        [{"session_id": "a", "cwd": f"{SESSIONS}/gone"}],
        dirty={}, exists=lambda p: False,
    )
    assert [e["mode"] for e in plan] == ["gone"]


def test_prod_sorts_first_so_it_keeps_window_one():
    plan = _plan(
        [{"session_id": "a", "cwd": f"{SESSIONS}/zzz"},
         {"session_id": "b", "cwd": PROD}],
        dirty={},
    )
    assert plan[0]["window"] == "main"


def test_one_window_per_directory():
    """Two sessions can share a cwd; two windows must not share a name."""
    plan = _plan(
        [{"session_id": "a", "cwd": f"{SESSIONS}/feat"},
         {"session_id": "b", "cwd": f"{SESSIONS}/feat"}],
        dirty={},
    )
    assert [e["session_id"] for e in plan] == ["a"]


def test_max_resume_downgrades_the_excess_to_shells():
    plan = _plan(
        [{"session_id": s, "cwd": f"{SESSIONS}/w{s}"} for s in "abcd"],
        dirty={f"{SESSIONS}/w{s}": 3 for s in "abcd"},
        max_resume=2,
    )
    assert [e["mode"] for e in plan] == ["resume", "resume", "shell", "shell"]
    assert [e["capped"] for e in plan] == [False, False, True, True]


def test_no_cap_resumes_every_dirty_tree():
    plan = _plan(
        [{"session_id": s, "cwd": f"{SESSIONS}/w{s}"} for s in "abcd"],
        dirty={f"{SESSIONS}/w{s}": 1 for s in "abcd"},
    )
    assert all(e["mode"] == "resume" for e in plan)


# ── surviving a reboot: the commands a window runs ───────────────────────

def test_resume_command_resumes_the_recorded_session():
    cmd = dk_session.resume_claude_command("abc-123", "casino-derby")
    assert "--resume abc-123" in cmd
    assert "--remote-control casino-derby" in cmd
    assert cmd.endswith("; exec $SHELL")


def test_resume_command_asks_for_a_summary_not_a_full_replay():
    """Picking "Resume from summary" in Claude Code's dialog just runs
    /compact; unattended, sending it is how that choice gets made."""
    cmd = dk_session.resume_claude_command("abc-123", "x")
    assert shlex.split(cmd[: -len("; exec $SHELL")])[-1] == "/compact"


def test_resume_command_suppresses_the_blocking_dialog():
    """Unanswered, that dialog parks the restored session on a menu — the
    exact state session_state() reports as WAITING."""
    for summary in (True, False):
        cmd = dk_session.resume_claude_command("a", "x", summary=summary)
        assert dk_session.RESUME_DIALOG_OFF in cmd


def test_full_resume_omits_the_compact_prompt():
    cmd = dk_session.resume_claude_command("a", "x", summary=False)
    assert "/compact" not in cmd


def test_shell_window_never_starts_a_billing_session():
    """A clean tree's window is a shell — the resume line is text to read."""
    cmd = dk_session.shell_window_command("abc-123", "x")
    assert cmd.startswith("printf ")
    assert cmd.endswith("; exec $SHELL")
    assert shlex.split(cmd[: -len("; exec $SHELL")])[-1].count("abc-123") == 2


def test_window_command_dispatches_on_mode():
    resume = dk_session.window_command(
        {"mode": "resume", "session_id": "a", "window": "w"})
    shell = dk_session.window_command(
        {"mode": "shell", "session_id": "a", "window": "w"})
    assert resume.startswith(dk_session.RESUME_DIALOG_OFF)
    assert shell.startswith("printf ")


def test_restore_targets_the_dk_session():
    first = dk_session.restore_new_session_args("main", "/w", "cmd")
    later = dk_session.restore_new_window_args("feat", "/w", "cmd")
    assert first[:5] == ["tmux", "new-session", "-d", "-s", dk_session.TMUX_SESSION]
    assert later[:3] == ["tmux", "new-window", "-d"]
    assert later[later.index("-t") + 1] == f"{dk_session.TMUX_SESSION}:"
    for args in (first, later):
        assert args[args.index("-c") + 1] == "/w"
        assert args[-1] == "cmd"


# ── surviving a reboot: waiting for the network ──────────────────────────

def test_wait_for_network_returns_once_the_host_answers():
    answers = iter([False, False, True])
    slept = []
    assert dk_session.wait_for_network(
        probe=lambda: next(answers), now=lambda: 0.0, sleep=slept.append) is True
    assert slept == [2.0, 2.0]


def test_wait_for_network_gives_up_at_the_deadline():
    clock = iter([0.0, 10.0, 20.0])
    assert dk_session.wait_for_network(
        probe=lambda: False, timeout=15.0,
        now=lambda: next(clock), sleep=lambda s: None) is False


# ── table formatting ─────────────────────────────────────────────────────

def test_elide_leaves_short_names_alone():
    assert dk_session.elide("marqo-nsfw-swap") == "marqo-nsfw-swap"


def test_elide_keeps_both_ends_of_a_long_name():
    """Feature names are free prose and run to 130 characters."""
    name = "i-m-trying-to-think-of-more-quests-and-community-quests-in-the-casino"
    out = dk_session.elide(name, limit=20)
    assert len(out) == 20
    assert out.startswith("i-m-tryin")
    assert out.endswith("the-casino")


def test_sessions_outside_this_repo_are_left_alone():
    """The snapshot records every session on the machine; restore rebuilds
    only the dk tmux session, not windows for unrelated projects."""
    plan = _plan(
        [{"session_id": "a", "cwd": "/home/ben"},
         {"session_id": "b", "cwd": "/home/ben/scarecrow"},
         {"session_id": "c", "cwd": PROD},
         {"session_id": "d", "cwd": f"{SESSIONS}/feat"}],
        dirty={},
    )
    modes = {e["session_id"]: e["mode"] for e in plan}
    assert modes == {"a": "foreign", "b": "foreign", "c": "shell", "d": "shell"}


@pytest.mark.parametrize(
    ("cwd", "ours"),
    [
        (PROD, True),
        (f"{SESSIONS}/casino-derby", True),
        (f"{SESSIONS}/casino-derby/src", False),   # a subdir is not a session
        ("/home/ben", False),
        ("/home/ben/scarecrow", False),
        ("/home/ben/discord-bots/other-repo", False),
    ],
)
def test_in_scope(cwd, ours):
    assert dk_session.in_scope(cwd, PROD) is ours
