#!/bin/sh
# Install the repo's post-commit hook.
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
cp "$top/scripts/hooks/post-commit" "$hooks/post-commit"
chmod +x "$hooks/post-commit"

echo "installed: $hooks/post-commit"
echo "seed the baseline once with: python3 scripts/post_testing_docs.py --seed-state"
