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
| Remote Control session | `documentation-review` |
| attach with | `tmux select-window -t documentation-review` |

## Remote Control is on by default

Every session launches with `claude --remote-control <name>`, so you can pick it up
from claude.ai or your phone without attaching to tmux at all. The session prints its
URL on startup.

The name has to be passed at launch. A session's Remote Control title is fixed at
startup — no hook and no slash command can set it afterwards, which is why the older
version of `/dk-feature` told you to run `/rename` by hand. Spawning is the one moment
the name can be applied, so that is where it happens, and it reuses the same feature
name as everything else.

Pass `--no-remote-control` to `dk_session.py new` for a local-only session.

## Auto mode is the default

Workers launch with `--permission-mode auto`. A session you drive from a phone should
not stall on a permission prompt with nobody sitting in front of it, and the whole
point of spinning several up is that you are not watching any one of them.

Auto is deliberately not `bypassPermissions`. Auto still runs every tool call past the
permission classifier, so genuinely destructive things — an `rm -rf` of a directory
tree, a force push — are refused rather than waved through. That safety net is real:
during the clone cleanup it blocked an `rm -rf` of 971 MB of session directories and
made a human approve it.

Override per session with `--permission-mode` (`manual`, `plan`, `acceptEdits`,
`bypassPermissions`, `dontAsk`). Inside a running session, `shift+tab` cycles modes —
which is how to change an already-running worker without restarting it.

**Auto mode is model-gated.** Opus sessions come up `⏵⏵ auto mode on`; a haiku session
launched with the same flag reports `auto mode unavailable for this model` and falls
back to manual. The launcher passes `--permission-mode` regardless and Claude Code
degrades gracefully, so nothing breaks — but a cheap-model worker spun up to run
unattended may quietly be waiting on a prompt. Check the status line before walking
away from one.

**`/dk-feature` does not move the session you typed it in.** It launches a worker
beside you and leaves your tree alone — that is what makes several at once possible.

Worktrees are siblings of the prod checkout, never inside it: prod is the running bot,
and nesting working trees under it invites a stray glob into production.

The branch is created with `--no-track`, so a stray `git push` from a session cannot
target `main`.

## What a session branches off — and why it isn't `origin/main`

Sessions branch off **local `main`**, the branch in the prod checkout.

This is the one place converting from clones to worktrees changed a meaning. The old
flow branched off `origin/main`, which was safe *because each clone's `origin` was the
prod checkout* — `origin/main` and prod's `main` were the same commit. In a worktree,
`origin` is GitHub. Prod's `main` runs ahead of it by every commit `/dk-ship` has
merged but nobody has pushed, so `origin/main` there means "main as of the last push",
not "current main".

Carrying the literal `origin/main` across is exactly the bug the first session hit: it
was cut 12 commits behind, missing everything merged that afternoon. Basing on local
`main` is correct because local `main` is what `/dk-ship` merges into — the integration
point *is* the prod checkout.

The remote still matters in one direction: `new` fetches, and if prod's `main` is
*behind* `origin/main` — someone pushed from elsewhere — it warns you to pull before
starting real work. A failed fetch (offline, dead SSH agent) is non-fatal; the session
is created anyway and the warning says the staleness check couldn't run.

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

## The clone migration (2026-07-25)

`dk-sessions/` previously held eleven hand-made clones (`work1`–`work4`, `gambling`,
`stats`, `todo`, `fable`, `sonnet-work`, `valdiation`) totalling 971 MB. They were
audited, their unmerged work salvaged into `main`, and the directories removed.

Three of them held commits that were not in `main`, and **two were on branches nobody
had checked out** — `gambling` sat on `website-cleanup` while its unmerged commit was
on `website-ux`, and `work4` looked idle on `main` but was one commit ahead of it. If
you ever sweep session directories again, audit *every* `refs/heads/` ref, not just
`HEAD`:

    for b in $(git -C <dir> for-each-ref --format='%(refname:short)' refs/heads/); do
      git -C <dir> log --oneline origin/main.."$b"
    done

And compare against the *prod* main, not the clone's own `main` — the stale local ref
made three already-merged branches look 75–154 commits ahead.
