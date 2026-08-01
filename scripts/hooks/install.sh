#!/bin/sh
# Install the repo's QA-card hooks (post-commit + post-merge).
#
# Two hooks because git splits the trigger: `git commit` fires post-commit,
# `git merge` fires post-merge — a --no-ff ship lands via the latter, and
# without it the merged commits' Testing: cards silently never post.
#
# Deliberately copies into $(git rev-parse --git-common-dir)/hooks rather than
# setting core.hooksPath: this repo already relies on the pre-commit framework's
# hook living there, and pointing hooksPath elsewhere would silently disable it.
# The common dir is shared by every worktree, so one install covers them all.
set -e

top=$(git rev-parse --show-toplevel)
hooks="$(cd "$top" && git rev-parse --git-common-dir)/hooks"
case "$hooks" in /*) ;; *) hooks="$top/$hooks" ;; esac

mkdir -p "$hooks"
for hook in post-commit post-merge; do
    cp "$top/scripts/hooks/$hook" "$hooks/$hook"
    chmod +x "$hooks/$hook"
    echo "installed: $hooks/$hook"
done
