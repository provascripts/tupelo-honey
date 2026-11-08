#!/usr/bin/env bash
# sync-mirrors.sh -- regenerate the cross-agent mirrors from their single sources.
#
#   skills/tupelo/      -> skills/honey/                     /honey, the alias of /tupelo
#   AGENTS.md           -> skills/craft-standard/            the full standard as a skill, for Claude Code sessions
#   AGENTS.md           -> .cursor/rules/systems-craft.mdc   Cursor rule, alwaysApply, hooks swapped for the git gate
#   skills/             -> .agents/skills/                   Agent Skills layout (Codex, Gemini CLI, Copilot, ...), without craft-standard
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

# /honey alias: the /tupelo skill, renamed, its description marked as an alias.
# Why a skill and not commands/: Claude Code runs a plugin skill by its bare
# name (/honey), a plugin command only with the prefix (/tupelo-honey:honey).
# Why keep /honey: it is the name users typed first; /tupelo exists because
# the Honey plugin has its own /honey skill, and when both plugins are
# installed the bare name runs whichever plugin loaded first.
mkdir -p "$out/skills/honey"
sed -e '2s|^name: tupelo$|name: honey|' -e '3s|^description: |description: Alias of /tupelo. |'   skills/tupelo/SKILL.md > "$out/skills/honey/SKILL.md"

# The full standard as a skill: frontmatter, then AGENTS.md byte for byte.
# Why a skill: installing the plugin must be the whole setup. A hook can add
# only 10,000 characters to a session and this file is about twice that, and
# reading it by path asks for permission in every session, because it lies
# outside the project, while loading a plugin skill does not.
# Why user-invocable is false: the session directive loads it, so it has no
# place in the user's / menu.
mkdir -p "$out/skills/craft-standard"
{
  printf -- '---\n'
  printf 'name: craft-standard\n'
  printf 'description: The full tupelo-honey coding standard, with the levers, where knowledge lives, the 9 comment kinds and the pre-finish checklist. Load it once per session, before the first code change.\n'
  printf 'user-invocable: false\n'
  printf '# Generated from AGENTS.md by scripts/sync-mirrors.sh; edit AGENTS.md, not this file.\n'
  printf -- '---\n\n'
  cat AGENTS.md
} > "$out/skills/craft-standard/SKILL.md"

# Cursor rule: Cursor's frontmatter, then AGENTS.md with its "### Claude Code
# hooks" subsection (up to the next "## " heading) replaced by the gate that
# works in any editor: the same checks as a git pre-commit hook.
# Why: Cursor runs no Claude Code hooks, so that subsection would describe
# checks that never run there.
mkdir -p "$out/.cursor/rules"
{
  printf -- '---\n'
  printf 'description: Systems Craft + Honey always-on core. Generated from AGENTS.md by scripts/sync-mirrors.sh; edit AGENTS.md, not this file.\n'
  printf 'globs:\n'
  printf 'alwaysApply: true\n'
  printf -- '---\n\n'
  awk '
    /^### Claude Code hooks$/ {
      skip = 1
      print "### Checks outside Claude Code"
      print "- Hooks and the Stop gate run only in Claude Code. Here, run the same checks before each commit: `craft-gate.py --staged` as a git pre-commit hook (`bash scripts/install-git-hook.sh` from a clone of tupelo-honey) or as a CI step. Hard findings block the commit; review signals are printed and let it pass."
      print ""
      next
    }
    skip && /^## / { skip = 0 }
    !skip
  ' AGENTS.md
} > "$out/.cursor/rules/systems-craft.mdc"

# Agent Skills mirror: an exact copy of skills/, without craft-standard.
# Why without: Codex and Gemini CLI load AGENTS.md by themselves, so the skill
# would give them the same text twice.
rm -rf "$out/.agents/skills"
mkdir -p "$out/.agents"
cp -R skills "$out/.agents/skills"
rm -rf "$out/.agents/skills/craft-standard"

if [ "$CHECK" -eq 1 ]; then
  if ! diff -q "$out/.cursor/rules/systems-craft.mdc" .cursor/rules/systems-craft.mdc >/dev/null 2>&1 ||
     ! diff -r -q "$out/.agents/skills" .agents/skills >/dev/null 2>&1 ||
     ! diff -q "$out/skills/craft-standard/SKILL.md" skills/craft-standard/SKILL.md >/dev/null 2>&1 ||
     ! diff -q "$out/skills/honey/SKILL.md" skills/honey/SKILL.md >/dev/null 2>&1; then
    echo "mirrors are out of date: run bash scripts/sync-mirrors.sh" >&2
    exit 1
  fi
fi
