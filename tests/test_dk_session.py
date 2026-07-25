"""dk_session.py's argument parsing and path/command construction (pure logic).

The launcher turns free prose (`/dk-feature opus documentation review`) into a
branch name, a directory, a tmux window name, and a `claude --model` line that
all have to agree — so each transform gets a case. The subprocess plumbing
(git worktree add, tmux new-window) is glue and stays untested here.
"""

from __future__ import annotations

import sys
from pathlib import Path

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
    assert dk_session.claude_command(None).startswith("claude;")


def test_claude_command_keeps_the_window_alive():
    """Quitting claude must not close the window and orphan the worktree."""
    assert dk_session.claude_command("opus").endswith("; exec $SHELL")


def test_new_window_args_names_window_after_branch():
    args = dk_session.new_window_args("casino-derby", Path("/tmp/wt"), "opus")
    assert args[:3] == ["tmux", "new-window", "-d"]
    assert "-n" in args and args[args.index("-n") + 1] == "casino-derby"
    assert args[args.index("-c") + 1] == "/tmp/wt"


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
