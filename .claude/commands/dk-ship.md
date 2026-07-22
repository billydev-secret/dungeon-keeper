---
description: Ship the current feature — rebase on main, run scoped gate, merge locally to main
argument-hint: [--no-test] [--no-push]
allowed-tools: Bash(git:*), Bash(python:*), Bash(flock:*)
---
Ship the current feature branch into local main. If ANY step fails, STOP and report —
never merge past a failed gate or an unresolved rebase. Args in `$ARGUMENTS`:
`--no-test` skips the gate (docs-only ships), `--no-push` skips the final GitHub push.

This session's checkout is an independent clone; its `origin` is the local main repo.
Resolve it first: `MAINREPO=$(git remote get-url origin)`. Integration is a LOCAL merge
into MAINREPO, which then pushes to GitHub.

Steps:

1. `BRANCH=$(git rev-parse --abbrev-ref HEAD)`. If BRANCH is `main`, stop — nothing to ship.
2. `git status --porcelain` — if anything is uncommitted, STOP and ask the user to commit first.
3. **Rebase onto latest main:** `git fetch origin` then `git rebase origin/main`.
   If there are conflicts, help the user resolve them and `git rebase --continue`.
   Do NOT proceed until the rebase completes cleanly.
4. **Scoped regression** (skip only if `--no-test` was passed): `python scripts/gate.py --scoped`.
   If it fails, STOP — show the failures and do not merge.
5. **Integrate locally** (one ship at a time — take the lock:
   `flock "$MAINREPO/.git/dk-ship.lock" -c '<the commands below>'`, or run them under flock):
   a. Push the feature into the main repo: `git push origin HEAD:refs/heads/$BRANCH`.
   b. Verify MAINREPO is on `main` with a clean tracked tree:
      `git -C "$MAINREPO" rev-parse --abbrev-ref HEAD` (must be `main`) and
      `git -C "$MAINREPO" status --porcelain -uno` (must be empty). If not, STOP.
   c. `git -C "$MAINREPO" merge --no-ff "$BRANCH"` — merge commit so the QA/Testing card hook fires.
   d. `git -C "$MAINREPO" branch -d "$BRANCH"` — drop the integrated branch from the main repo.
   e. Unless `--no-push`: `git -C "$MAINREPO" push`.
6. Report what merged and whether main was pushed. This session stays on BRANCH (now merged
   into main); start the next feature with `/dk-feature`.
