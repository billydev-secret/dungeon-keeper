---
description: Ship the current feature — rebase on main, run scoped gate, merge, tear the session down
argument-hint: [--no-test] [--no-push] [--keep]
allowed-tools: Bash(git:*), Bash(python3:*), Bash(flock:*), Bash(tmux:*), Bash(setsid:*)
---
Ship this feature session into main, then delete the session. If ANY step fails, STOP
and report — never merge past a failed gate or an unresolved rebase, and never tear
down a session whose work did not land.

Args in `$ARGUMENTS`: `--no-test` skips the gate (docs-only ships), `--no-push` skips
the final GitHub push, `--keep` leaves the worktree and window alive after merging.

This session is a **worktree** of the prod checkout, not a clone — the branch already
lives in the same repository, so integration is a local merge with no push-into-origin
round trip. Resolve the prod checkout first:

    MAIN=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")

Steps:

1. `BRANCH=$(git rev-parse --abbrev-ref HEAD)`. If BRANCH is `main`, stop — nothing to ship.
2. **Commit any uncommitted work** so the ship starts from a clean tree:
   `git status --porcelain`. If it's non-empty, stage everything (`git add -A`) and
   commit it following the repo's commit conventions (CLAUDE.md → Commits:
   `Scope: summary` subject, prose body of why/edge-cases, a `Testing:` section of
   `- [ ]` lines **only** if the change is user-facing — something a member or admin
   can see in Discord or on the dashboard — written for a volunteer tester rather
   than a developer, no `Co-Authored-By`/`Claude-Session` trailers). The pre-commit hook runs the scoped
   gate — if that commit fails the hook, STOP and report; do not `--no-verify` past it.
   If the tree is already clean, skip this step.
3. **Rebase onto latest main:** `git fetch origin` then `git rebase origin/main`.
   If there are conflicts, help the user resolve them and `git rebase --continue`.
   Do NOT proceed until the rebase completes cleanly.
4. **Scoped regression** (skip only if `--no-test` was passed):
   `python scripts/gate.py --scoped`. If it fails, STOP — show the failures, do not merge.
5. **Integrate** — one ship at a time, since every session merges into the same prod
   checkout. Take the lock and run these under it:
   `flock "$MAIN/.git/dk-ship.lock" -c '<the commands below>'`

   a. Verify prod is on `main` with a clean tracked tree:
      `git -C "$MAIN" rev-parse --abbrev-ref HEAD` (must be `main`) and
      `git -C "$MAIN" status --porcelain -uno` (must be empty). If not, STOP.
   b. `git -C "$MAIN" merge --no-ff "$BRANCH"` — a merge commit. The merge itself
      posts **no** QA card: a branch ships as many times as the work needs, and the
      feature's single card is written at teardown in step 7 from everything the
      branch ever merged.
   c. Unless `--no-push`: `git -C "$MAIN" push`.

   If the lock is held, say so — a blocked ship looks identical to a hung one, and
   the user should know another session is mid-merge rather than assume a stall.
6. Report what merged and whether main was pushed.
7. **Tear the session down** (skip if `--keep` was passed, or if this checkout is not
   under `dk-sessions/` — a branch made directly in prod has no session to remove):

       cd "$MAIN" && setsid nohup python3 "$MAIN/scripts/dk_session.py" \
         teardown "$BRANCH" --window "$TMUX_PANE" --delay 5 >/dev/null 2>&1 &

   Detached and delayed on purpose: teardown removes the worktree, deletes the merged
   branch, and kills **the very window this command is running in**, so it has to
   outlive the shell that launched it and wait long enough for your report from step 6
   to reach the screen. Tell the user the window will close in a few seconds and that
   `--keep` is how to hold it open next time.

   Teardown is also where the feature's **QA card** is posted — one card built from
   every `Testing:` section the branch ever merged, rewritten into a tester-readable
   checklist. It runs after the window is killed, so nothing waits on it. A ship with
   `--keep` skips teardown and therefore posts no card; run
   `python3 scripts/post_testing_docs.py --branch "$BRANCH"` from the prod checkout
   when that session is finally done, or pass `--no-card` to teardown to suppress it.

   Never pass `--force` to teardown — it discards uncommitted work, and step 2 already
   guaranteed there is none.
