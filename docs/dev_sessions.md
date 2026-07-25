# Feature sessions (tmux + worktrees)

How parallel work happens on this box: one tmux window per feature, each running its
own `claude` in its own git worktree, all merging into the prod checkout.

`/dk-feature` creates a session. `/dk-ship` merges it and deletes it. Those two
commands and `scripts/dk_session.py` are the whole system.

## Why worktrees and not clones

The earlier flow used full `git clone`s under `dk-sessions/`, each with `origin`
pointing at the prod checkout. That worked, but it meant a separate object store per
session, a separate hooks install per session, and a two-step integration: push the
branch into the prod repo, *then* merge it there.

A worktree shares one `.git` with prod. So:

- a session costs a checkout, not a clone;
- the pre-commit gate is already installed in every new session — `scripts/hooks/install.sh`
  writes into the common dir, which every worktree shares;
- `/dk-ship` merges a branch that already lives in the repo — no push round trip;
- `git worktree list` is a real inventory of active sessions.

The one rule worktrees add: **a branch can only be checked out in one worktree at a
time.** Prod holds `main`, so no session may be on `main` — which is exactly what you
want anyway.

## Starting a session

    /dk-feature opus documentation review     → branch documentation-review, model opus
    /dk-feature sonnet casino derby           → branch casino-derby, model sonnet
    /dk-feature quest digest redesign         → branch quest-digest-redesign, default model

An optional leading model alias (`opus`, `sonnet`, `haiku`, `fable`, or a full model
id) is peeled off the front; the rest is the feature name as prose. A lone token is
always the name, never a model — `/dk-feature opus` makes a branch called `opus`.

The name is normalized once and used for all four things, so there is only ever one
address to remember:

| | |
|---|---|
| branch | `documentation-review` |
| worktree | `../dk-sessions/documentation-review` |
| tmux window | `documentation-review` |
| attach with | `tmux select-window -t documentation-review` |

**`/dk-feature` does not move the session you typed it in.** It launches a worker
beside you and leaves your tree alone — that is what makes several at once possible.

Worktrees are siblings of the prod checkout, never inside it: prod is the running bot,
and nesting working trees under it invites a stray glob into production.

The branch is created with `--no-track`, so a stray `git push` from a session cannot
target `main`.

If `git fetch` fails (offline, dead SSH agent) the session is still created, off the
last-known `origin/main`, with a warning. `/dk-ship` re-fetches and rebases before it
merges anything, so staleness is caught at ship time rather than blocking you from
starting work at all.

## Seeing what's running

    python scripts/dk_session.py list

    SESSION                BRANCH                 TREE   WINDOW
    (prod)                 main                   dirty  -
    documentation-review   documentation-review   clean  live

`WINDOW: live` means a tmux window by that name still exists. A session with no live
window is an abandoned worktree — either attach a new `claude` to it or tear it down.

## Shipping

`/dk-ship`, run from inside the session's own window:

1. commits anything uncommitted (pre-commit gate runs);
2. `git fetch` + `git rebase origin/main`;
3. `python scripts/gate.py --scoped` (skip with `--no-test` for docs-only ships);
4. under `flock .git/dk-ship.lock`, verifies prod is on a clean `main` and
   `git merge --no-ff` the branch, then pushes (skip with `--no-push`);
5. tears the session down: removes the worktree, deletes the merged branch, kills the
   window.

The lock matters because every session merges into the same prod checkout. If it's
held, the ship waits — `/dk-ship` says so explicitly, because a blocked ship and a
hung one look identical from the outside.

Teardown is detached and delayed (`setsid nohup … --delay 5`) because it kills the
very window it runs in; without the delay the ship report dies with the pane. Pass
`--keep` to merge but hold the session open.

Teardown deletes the branch with `git branch -d`, so unmerged work is never silently
dropped — it says "kept branch — not merged into main" and leaves it.

## Cleaning up by hand

    python scripts/dk_session.py teardown <name>

Refuses to remove a worktree with uncommitted changes. `--force` overrides that and
discards the work; `/dk-ship` never passes it.

## Note on `dk-sessions/`

The directory also holds leftover **clones** from the pre-worktree flow (`work1`–`work4`,
`gambling`, `stats`, `todo`, `fable`, `sonnet-work`, `valdiation`). They are not worktrees
and `dk_session.py list` does not show them. Some still hold unmerged feature branches,
so they are left alone rather than swept — check each with `git -C <dir> log origin/main..HEAD`
before deleting it.
