---
description: Start a feature — branch off fresh local main and label the session
argument-hint: <feature-name>
allowed-tools: Bash(git:*)
---
Start a new feature branch named `$ARGUMENTS` in THIS session's checkout.

Do exactly this, stopping with a clear message on any problem:

1. If `$ARGUMENTS` is empty, ask the user for a feature name and stop.
2. Normalize it: lowercase, spaces and underscores → hyphens. Call the result NAME.
3. Check for unfinished work: `git status --porcelain -uno`. If there are **tracked**
   changes, STOP and tell the user to commit or discard them first (untracked files
   are fine — they carry over harmlessly).
4. Get the latest main: `git fetch origin`.
5. Branch off it: `git checkout -b NAME origin/main`. If a branch NAME already exists,
   tell the user and stop rather than clobbering it.
6. Report: "Started feature **NAME** off main." The statusLine now shows NAME as the
   session's label. (Note: the built-in session title can't be set programmatically —
   the statusLine *is* the session name, and it tracks this branch.)
