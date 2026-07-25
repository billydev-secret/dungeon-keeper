---
description: Start a feature — worktree, branch, tmux window, and a claude running in it
argument-hint: [opus|sonnet|haiku|fable] <feature name>
allowed-tools: Bash(python3:*), Bash(git:*), Bash(tmux:*)
---
Spawn a new feature session from `$ARGUMENTS`: a git worktree off prod's `main`, a
branch, a tmux window, and a Remote Control-enabled `claude` running inside it — one
command, one name for all five.

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
   clobbers if that worktree or branch already exists. It branches off **local**
   `main` — the branch `/dk-ship` merges into, and which runs ahead of `origin/main`
   by every commit not yet pushed — with `--no-track`, so a stray `git push` from the
   new session can never target main.
3. If it exits non-zero, show its stderr verbatim and stop — do not retry with a
   different name unless the user asks.
4. On success, report the branch, the model, and the attach line it printed
   (`tmux select-window -t NAME`). Relay any `warning:` lines verbatim — a warning
   that prod trails `origin/main` means the session just started on stale code.
   Mention that it ships with `/dk-ship` from inside its own window.
5. The worker starts with Remote Control enabled and named after the feature, so it
   can be driven from claude.ai or a phone without attaching to tmux. It prints its
   own session URL on startup; `tmux capture-pane -p -t NAME` will show it if the
   user wants the link without switching windows.

Notes:

- Worktrees live in `../dk-sessions/<name>`, siblings of this checkout — never inside
  it, because this checkout is production and runs the live bot.
- A worktree shares one object store and one hooks dir with prod, so the pre-commit
  gate is already installed in every new session; there is nothing to set up.
- `python3 scripts/dk_session.py list` shows every session, its branch, whether its
  tree is dirty, and whether its window is still live.
