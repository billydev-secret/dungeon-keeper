---
description: Ship the current feature — review, rebase on main, run scoped gate, merge, tear the session down
argument-hint: [--no-review] [--review LEVEL] [--no-test] [--no-push] [--keep]
allowed-tools: Bash(git:*), Bash(python3:*), Bash(flock:*), Bash(tmux:*), Bash(setsid:*), Skill, Read, Edit, Write, Grep, Glob
---
Ship this feature session into main, then delete the session. If ANY step fails, STOP
and report — never merge past a failed gate or an unresolved rebase, and never tear
down a session whose work did not land.

Args in `$ARGUMENTS`: `--no-review` skips the code review, `--review LEVEL` sets its
effort level (default `high`), `--no-test` skips the gate (docs-only ships),
`--no-push` skips the final GitHub push, `--keep` leaves the worktree and window alive
after merging.

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
4. **Code review, with fixes applied** (default; skip only if `--no-review` was
   passed). Invoke the `code-review` skill through the Skill tool, with args
   `<level> --fix main...HEAD`, where `<level>` is whatever `--review` named and
   `high` otherwise.

   The explicit `main...HEAD` target matters: step 2 left the tree clean, so "the
   current diff" is empty and a bare review would report a clean branch it never
   read. The target is the whole feature — every commit the merge in step 6 will
   land — reviewed against the main it is about to land on, which is exactly what
   the rebase in step 3 just made current.

   `ultra` is **not** available here: it launches a billed cloud review and is
   user-triggered only. If the user asked for it, stop and tell them to run
   `/code-review ultra` themselves, then re-run `/dk-ship`.

   Then handle what the review did:

   a. If it applied fixes, the tree is now dirty. Commit them **as their own
      commit** — `Review: <what was fixed>` — following the same commit conventions
      as step 2. A separate commit keeps the fixes legible in `git log` and in the
      merge diff instead of folding them invisibly into the feature's own history,
      and it is the only record that a machine touched the code after you last read
      it. The pre-commit hook gates that commit; if it fails, STOP and report.
   b. If a fix changed something a member or admin can see, that commit carries its
      own `Testing:` lines — teardown gathers every one of them into the feature's
      single QA card, so a behavior change introduced by the review still reaches
      the tester.
   c. If the review reported findings it did **not** fix — anything it flagged and
      left alone, or judged out of scope — report them and **ask before continuing**.
      Do not merge past an unfixed high-confidence finding on your own judgement;
      the user may well say ship it anyway, but that is their call, not yours.
   d. If nothing was found, say so in one line and move on. A clean review is not
      worth a paragraph.
5. **Scoped regression** (skip only if `--no-test` was passed):
   `python scripts/gate.py --scoped`. If it fails, STOP — show the failures, do not merge.
   In a session worktree this never fans out to the whole suite: a shared-file edit
   (`core/`, `models/`, an edited migration, deps, `gate.py`) prints the paths whose
   full run was **deferred** instead of running it. That run is paid on main — see
   step 7 — so a ship is fast and the coverage still happens, once, on the tree that
   actually matters.
6. **Integrate** — one ship at a time, since every session merges into the same prod
   checkout. Take the lock and run these under it:
   `flock "$MAIN/.git/dk-ship.lock" -c '<the commands below>'`

   a. Verify prod is on `main` with a clean tracked tree:
      `git -C "$MAIN" rev-parse --abbrev-ref HEAD` (must be `main`) and
      `git -C "$MAIN" status --porcelain -uno` (must be empty). If not, STOP.
   b. `git -C "$MAIN" merge --no-ff "$BRANCH"` — a merge commit. The merge itself
      posts **no** QA card: a branch ships as many times as the work needs, and the
      feature's single card is written at teardown in step 8 from everything the
      branch ever merged.
   c. Unless `--no-push`: `git -C "$MAIN" push`.

   If the lock is held, say so — a blocked ship looks identical to a hung one, and
   the user should know another session is mid-merge rather than assume a stall.
7. Report what merged and whether main was pushed. If step 5 printed **deferred**
   full-run paths, say so and tell the user main needs `python scripts/gate.py`
   (~10 min, from the prod checkout) once their current batch of ships is done —
   one run covers every branch merged since the last one, so don't run it per ship.
   Offer to start it; don't block the teardown on it.
8. **Tear the session down** (skip if `--keep` was passed, or if this checkout is not
   under `dk-sessions/` — a branch made directly in prod has no session to remove):

       cd "$MAIN" && setsid nohup python3 "$MAIN/scripts/dk_session.py" \
         teardown "$BRANCH" --window "$TMUX_PANE" --delay 5 >/dev/null 2>&1 &

   Detached and delayed on purpose: teardown removes the worktree, deletes the merged
   branch, and kills **the very window this command is running in**, so it has to
   outlive the shell that launched it and wait long enough for your report from step 7
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
