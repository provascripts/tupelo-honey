#!/usr/bin/env bash
# install-git-hook.sh -- run the tupelo-honey gate before every commit of a repository.
#
# Usage: install-git-hook.sh [REPOSITORY]     (default: the current directory)
#
# The hook calls bin/craft-gate.py --staged from this clone by absolute path,
# so the clone has to stay where it is. Hard findings abort the commit (exit
# 1); review signals print on stderr and let it pass; git commit --no-verify
# skips the hook.
#
# A pre-commit hook that is not ours is never overwritten: the script exits 1
# and prints the one line to add to it. Running it again refreshes our hook.
set -eu

plugin=$(cd "$(dirname -- "$0")/.." && pwd)
cd -- "${1:-.}"

# Why --git-path and not .git/hooks: it follows core.hooksPath and linked
# worktrees, where .git/hooks is not the directory git reads.
hooks=$(git rev-parse --git-path hooks)
hook=$hooks/pre-commit
marker='# tupelo-honey gate, written by scripts/install-git-hook.sh.'
gate="python3 $(printf %q "$plugin/bin/craft-gate.py") --staged"

if [ -e "$hook" ] && ! grep -qF "$marker" "$hook"; then
  printf '%s is not ours and stays as it is. Add this line to it:\n  %s\n' "$hook" "$gate" >&2
  exit 1
fi

mkdir -p -- "$hooks"
printf '#!/bin/sh\n%s\nexec %s\n' "$marker" "$gate" > "$hook"
chmod +x "$hook"
printf 'installed %s\n' "$hook"
