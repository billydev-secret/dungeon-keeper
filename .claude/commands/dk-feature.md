---
description: Start a feature — worktree, branch, tmux window, and a claude running in it
argument-hint: [opus|sonnet|haiku|fable] <feature name>
allowed-tools: Bash(python3:*), Bash(git:*), Bash(tmux:*)
---
Spawn a new feature session from `$ARGUMENTS`: a git worktree off `origin/main`, a
branch, a tmux window, and a `claude` running inside it — one command, one name for
all four.

**This does not move the session you typed it in.** The old flow put you on a new
branch in your own checkout; this one launches a *worker beside you* and leaves your
tree alone. That's the point: you can start several and attach to whichever you want.

Argument shape — an optional leading model alias, then the feature name as prose:

    /dk-feature opus documentation review     → model opus,  branch documentation-review
    /dk-feature sonnet casino derby           → model sonnet, branch casino-derby
    /dk-feature quest digest redesign         → default model, branch quest-digest-redesign

Do exactly this, stopping with a clear message on any problem:

1. If `$ARGUMENTS` is empty, ask the user for a feature name and stop.
2. Run it — the launcher does the parsing, normalization, and every guard:

       python3 scripts/dk_session.py new $ARGUMENTS

   It splits the model alias off the front, normalizes the rest into a name that is
   legal as a branch *and* a directory *and* a tmux window, then refuses rather than
   clobbers if that worktree or branch already exists. It branches with `--no-track`
   so a stray `git push` from the new session can never target main.
3. If it exits non-zero, show its stderr verbatim and stop — do not retry with a
   different name unless the user asks.
4. On success, report the branch, the model, and the attach line it printed
   (`tmux select-window -t NAME`). Mention that the worker starts on a fresh
   `origin/main` and ships with `/dk-ship` from inside its own window.

Notes:

- Worktrees live in `../dk-sessions/<name>`, siblings of this checkout — never inside
  it, because this checkout is production and runs the live bot.
- A worktree shares one object store and one hooks dir with prod, so the pre-commit
  gate is already installed in every new session; there is nothing to set up.
- `python3 scripts/dk_session.py list` shows every session, its branch, whether its
  tree is dirty, and whether its window is still live.
