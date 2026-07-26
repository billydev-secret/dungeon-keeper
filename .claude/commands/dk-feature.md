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
2. **Brief the worker.** If this conversation already holds context the new session
   would otherwise have to re-derive — what the bug actually is, which commits
   touched the area, a sibling session working the same file, a wrong assumption to
   avoid — write it to a file and pass `--brief-file`. It becomes the worker's
   opening prompt, so it starts informed instead of starting cold:

       python3 scripts/dk_session.py new $ARGUMENTS --brief-file <path>

   Keep it to what a competent colleague couldn't get from the repo in five minutes.
   Don't restate CLAUDE.md — the worker loads it. Don't invent findings you haven't
   verified. If you're unsure whether something is true, say so in the briefing or
   leave it out; a confident wrong steer is worse than no briefing. End with what you
   want done first, and say so plainly if it's "investigate, don't edit yet".

   With no such context, spawn it bare — the launcher does the parsing,
   normalization, and every guard:

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
6. It also starts in **auto** permission mode, so it doesn't stall on prompts with
   nobody watching. Auto still checks every call against the permission classifier —
   it is not `bypassPermissions`. Override at spawn with `--permission-mode`, or
   `shift+tab` inside a running session to cycle modes without restarting it.

Notes:

- Worktrees live in `../dk-sessions/<name>`, siblings of this checkout — never inside
  it, because this checkout is production and runs the live bot.
- A worktree shares one object store and one hooks dir with prod, so the pre-commit
  gate is already installed in every new session; there is nothing to set up.
- `python3 scripts/dk_session.py list` shows every session, its branch, whether its
  tree is dirty, and whether its window is still live.
