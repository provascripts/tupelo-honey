#!/usr/bin/env bash
# sync-mirrors.sh -- regenerate the cross-agent mirrors from their single sources.
#
#   AGENTS.md           -> .cursor/rules/systems-craft.mdc   Cursor rule, alwaysApply
#   skills/             -> .agents/skills/                   Agent Skills layout (Codex, Gemini CLI, Copilot, ...)
#   commands/tupelo.md  -> commands/honey.md                 /honey, the alias of /tupelo
#
# Usage: sync-mirrors.sh [--check]
#   --check  build the mirrors in a temporary directory and exit 1 if the
#            ones in the repository differ, without touching them (the tests
#            run this to catch drift).
# Why copies and not symlinks: Windows checkouts turn symlinks into plain
# text files unless Developer Mode and core.symlinks are both enabled.
set -eu
cd "$(dirname -- "$0")/.."

CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1
out=.
if [ "$CHECK" -eq 1 ]; then
  out=$(mktemp -d)
  trap 'rm -rf "$out"' EXIT
fi

# Cursor rule: Cursor's frontmatter, then AGENTS.md unchanged.
mkdir -p "$out/.cursor/rules"
{
  printf -- '---\n'
  printf 'description: Systems Craft + Honey always-on core. Generated from AGENTS.md by scripts/sync-mirrors.sh; edit AGENTS.md, not this file.\n'
  printf 'globs:\n'
  printf 'alwaysApply: true\n'
  printf -- '---\n\n'
  cat AGENTS.md
} > "$out/.cursor/rules/systems-craft.mdc"

# Agent Skills mirror: an exact copy of skills/.
rm -rf "$out/.agents/skills"
mkdir -p "$out/.agents"
cp -R skills "$out/.agents/skills"

# /honey alias: the /tupelo command, its description marked as an alias.
# Why keep it: /honey is the name users typed before v0.0.2; /tupelo exists
# because the Honey plugin owns /honey when both are installed.
mkdir -p "$out/commands"
sed '2s|^description: |description: Alias of /tupelo. |' commands/tupelo.md > "$out/commands/honey.md"

if [ "$CHECK" -eq 1 ]; then
  if ! diff -q "$out/.cursor/rules/systems-craft.mdc" .cursor/rules/systems-craft.mdc >/dev/null 2>&1 ||
     ! diff -r -q "$out/.agents/skills" .agents/skills >/dev/null 2>&1 ||
     ! diff -q "$out/commands/honey.md" commands/honey.md >/dev/null 2>&1; then
    echo "mirrors are out of date: run bash scripts/sync-mirrors.sh" >&2
    exit 1
  fi
fi
