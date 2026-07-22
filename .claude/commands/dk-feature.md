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
5. Branch off it: `git checkout -b NAME --no-track origin/main`. The `--no-track` is
   important — without it the branch tracks origin/main and a stray `git push` would
   target main. If a branch NAME already exists, tell the user and stop rather than
   clobbering it.
6. Report: "Started feature **NAME** off main," then tell the user: *"Run `/rename NAME`
   to set the session title."* The terminal statusLine already shows NAME as the label,
   but the title shown in Remote Control / the web UI can only be changed by the user's
   own `/rename` — a command or hook can't set it mid-session (the built-in title is
   settable only at session startup/resume, never on demand).
