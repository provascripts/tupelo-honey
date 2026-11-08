#!/usr/bin/env bash
# craft-mode.sh -- persist the tupelo-honey mode and re-inject it at session start.
#
# Usage:
#   craft-mode.sh set [lite|full|ultra|off]   persist the first word (empty or unknown -> full), print it
#   craft-mode.sh get                          print the persisted mode (full when unset)
#   craft-mode.sh session                      SessionStart hook: print the always-on directive
#   craft-mode.sh command "<args>"             /tupelo: parse "[default] [mode]", print one
#                                              "mode=<m> scope=<s> default=<d>" line
#
# /tupelo argument grammar (its alias /honey is the same command), as handled by `command`:
#   ""               scope=session, mode = the default    (back to the default)
#   "<mode>"         scope=session, nothing written        (this session only)
#   "default <mode>" scope=default, <mode> persisted       (now and new sessions)
#   "default"        scope=show, nothing written           (print the default)
#   anything else    one "error=..." line, nothing written
#
# The persisted mode is the default: one global file,
# ${CLAUDE_CONFIG_DIR:-~/.claude}/.tupelo-honey-mode.
# Why global: it is read only at SessionStart (startup, resume, clear,
# compact), so switching never flips a session that is already running.
# Why not ${CLAUDE_PLUGIN_DATA}: /tupelo runs this script through the Bash
# tool, whose environment lacks that variable, so the writer and the hook
# could disagree on the path.
set -u

STATE="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.tupelo-honey-mode"

# Print the persisted mode; full when the state file is missing or holds anything else.
read_mode() {
  local m=
  [ -f "$STATE" ] && m=$(tr -d '[:space:]' < "$STATE")
  case "$m" in lite|full|ultra|off) printf '%s' "$m" ;; *) printf 'full' ;; esac
}

case "${1:-}" in
  set)
    m=$(printf '%s' "${2:-}" | awk '{ print tolower($1) }')
    case "$m" in lite|full|ultra|off) ;; *) m=full ;; esac
    mkdir -p "$(dirname -- "$STATE")" && printf '%s\n' "$m" > "$STATE" || exit 1
    printf '%s\n' "$m"
    ;;
  get)
    read_mode
    echo
    ;;
  command)
    # Why exit 0 on bad input: /tupelo runs this through a "!" command, and a
    # non-zero exit aborts the slash command before the model can show the
    # user the usage line.
    # shortcut: session mode survives 0 /clear or compactions; add a per-session state file if it must outlive them.
    # The session-only mode lives in the conversation, not on disk, and the
    # SessionStart hook re-injects the default after /clear or compaction.
    set -f
    # shellcheck disable=SC2046  # word splitting is the parser here
    set -- $(printf '%s' "${2:-}" | tr '[:upper:]' '[:lower:]')
    set +f
    def=$(read_mode)
    scope=session
    if [ "${1:-}" = default ]; then scope=default; shift; fi
    m=${1:-}
    usage='usage: /tupelo [lite|full|ultra|off] | /tupelo default [lite|full|ultra|off]'
    if [ $# -gt 1 ]; then
      printf 'error=too many words; %s\n' "$usage"
      exit 0
    fi
    case "$scope:$m" in
      default:) printf 'mode=%s scope=show default=%s\n' "$def" "$def"; exit 0 ;;
      session:) m=$def ;;
      *:lite|*:full|*:ultra|*:off) ;;
      *) printf 'error=unknown mode "%s"; %s\n' "$m" "$usage"; exit 0 ;;
    esac
    if [ "$scope" = default ]; then
      mkdir -p "$(dirname -- "$STATE")" && printf '%s\n' "$m" > "$STATE" || exit 1
      def=$m
    fi
    printf 'mode=%s scope=%s default=%s\n' "$m" "$scope" "$def"
    ;;
  session)
    m=$(read_mode)
    case "$m" in
      off)   chat="Chat compression is OFF: write normal prose." ;;
      lite)  chat="Chat: lite, full sentences allowed for explanations and trade-offs, still no filler." ;;
      ultra) chat="Chat: ultra, answer-only, no recap; read-only files >= 6000 chars: outline first (honey-px if installed), never a file you will Edit. Step up to lite when brevity would drop correctness." ;;
      *)     chat="Chat: full, answer first, fragments, no filler, never abbreviate words. Step up to lite when brevity would drop correctness." ;;
    esac
    # Why static text: the directive enters every session's context, and the
    # same bytes per mode let the prompt cache keep it. Keep it under 1200
    # characters per mode; a test checks both.
    # Why it names a skill instead of carrying the standard: AGENTS.md is about
    # twice the 10,000 characters Claude Code accepts from a hook, and sessions
    # that change no code should not pay for it. Why not a copy in the
    # repository: setup must be nothing beyond installing the plugin, and
    # writing into every repository would dirty the user's git status.
    # sync-mirrors.sh says why it is a skill and not a file read by path.
    printf 'tupelo-honey mode: %s. %s Precedence: an explicit user request, then these rules, then honey, then repo conventions, which win on form. Code, in every mode: Lever 1 ladder (YAGNI, stdlib first) and deep modules. Chat compression, from this plugin or from honey, never applies to code comments or docs: write Function, Design, Why, Teacher (exact ASCII diagrams), Checklist and Guide comments in full for a human reader; never Trivial or Backup comments; debt only as "shortcut: <ceiling>; <trigger>" or "FIXME: <defect>; <trigger>". For honey-review, craft-review, hive-reviewer and hive-craft-reviewer, a redundant comment means Trivial or Backup only. Locate before reading: Grep or Glob, then Read with offset and limit. Counts, sorts, dedups and measurements come from a command, never from reasoning. Load the craft-design, craft-code, craft-review or craft-verify skill when the task matches, and fix what the craft hooks report. Before the first code change, load the craft-standard skill once.\n' "$m" "$chat"
    ;;
  *)
    echo "usage: craft-mode.sh set [lite|full|ultra|off] | get | session | command \"[default] [mode]\"" >&2
    exit 1
    ;;
esac
