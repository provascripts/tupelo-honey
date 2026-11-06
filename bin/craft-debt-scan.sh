#!/usr/bin/env bash
# craft-debt-scan.sh -- zero-token ledger and linter for debt markers.
#
# Debt lives in exactly two comment markers, each carrying its own exit
# condition on line 1 (AGENTS.md, "Debt"):
#
#   shortcut: <quantified ceiling>; <revisit trigger>   deliberate simplification
#   FIXME: <defect>; <trigger condition>                 known defect
#
#   ceiling = at least one digit ("~10k entries", "<1ms", "n > 5000")
#   trigger = ';' or '>' or a word: if when whenever once unless until after
#             upon trigger over above past exceeds beyond
#
# 'honey:' is the legacy spelling of 'shortcut:' and obeys the same rule. A
# bare TODO is always a violation. Markers count only inside comments, with
# the openers each language really uses (an opener is ignored elsewhere, so a
# '%' in Python or a ';' in C never looks like a comment):
#
#   #  //  --  /*  *     every other file (C family, Go, Rust, JS/TS, Python,
#                        shell, Ruby, Perl, R, Lua, SQL, Julia, Nim, YAML...)
#   ;                    Lisp, Scheme, Racket, Clojure, Emacs Lisp, Fennel
#   %                    Erlang
#   !                    Fortran
#   '  REM               Visual Basic
#   --  {-               Haskell, Elm, PureScript, Idris
#   (*  //  *            OCaml, F#
#   <!--                 HTML, XML, Vue, Svelte
#
# Prose files (*.md, *.mdc, *.rst, *.txt) are never scanned: they talk about
# markers.
#
# Usage: craft-debt-scan.sh [--quiet] [--hook] [PATH...]
#   (default)  scan PATHs (default .): print the ledger of tracked markers and
#              every violation; exit 1 if there is any violation.
#   --quiet    print violations only.
#   --hook     PostToolUse mode: read the hook JSON on stdin and scan only the
#              lines of the edited file that differ from git HEAD (the whole
#              file when untracked or outside git). Violations go to stderr
#              with exit 2, the only code Claude Code feeds back to the model.
set -u

QUIET=0
HOOK=0
paths=()
for arg in "$@"; do
  case "$arg" in
    --quiet) QUIET=1 ;;
    --hook) HOOK=1; QUIET=1 ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
    *) paths+=("$arg") ;;
  esac
done
[ ${#paths[@]} -eq 0 ] && paths=(.)

MARK="((^|[[:space:]])(#|//|--|/\\*|;|%|!|'|<!--|\\{-|\\(\\*|[Rr][Ee][Mm])|^[[:space:]]*\\*)[[:space:]]*(shortcut:|honey:|TODO([^[:alnum:]_]|\$)|FIXME([^[:alnum:]_]|\$))"
SKIP_DIRS=(.git node_modules vendor dist build target out .venv venv __pycache__ .tox .mypy_cache .pytest_cache .next .cache)
SKIP_FILES=('*.md' '*.mdc' '*.markdown' '*.rst' '*.txt' '*.min.js' "$(basename -- "$0")")

# Read "path:line:text" records on stdin; print the ledger and violations.
# Exit status 1 when at least one record is a violation.
classify() {
  awk -v quiet="$QUIET" '
    # Return 1 if comment opener `pre` opens a comment in the language of `path`.
    function allowed(path, pre,   ext, p) {
      ext = tolower(path)
      sub(/:[0-9]+$/, "", ext)
      if (!sub(/^.*\./, "", ext)) ext = ""
      p = tolower(pre)
      if (ext ~ /^(lisp|lsp|el|scm|ss|rkt|clj|cljs|cljc|fnl)$/) return p == ";"
      if (ext ~ /^(erl|hrl)$/) return p == "%"
      if (ext ~ /^(f|for|f90|f95|f03|f08)$/) return p == "!"
      if (ext ~ /^(vb|bas|vbs)$/) return p == "\047" || p == "rem"
      if (ext ~ /^(hs|elm|purs|idr)$/) return p == "--" || p == "{-"
      if (ext ~ /^(ml|mli|fs|fsi|fsx)$/) return p == "(*" || p == "//" || p == "*"
      if (ext ~ /^(html|htm|xhtml|xml|vue|svelte)$/) return p == "<!--"
      return p == "#" || p == "//" || p == "--" || p == "/*" || p == "*"
    }
    BEGIN { bad = 0; ok = 0 }
    {
      # Split "path:line:text" and find the marker with the opener before it.
      if (!match($0, /:[0-9]+:/)) next
      where = substr($0, 1, RSTART + RLENGTH - 2)
      text = substr($0, RSTART + RLENGTH)
      if (!match(text, /((^|[[:space:]])(#|\/\/|--|\/\*|;|%|!|\047|<!--|\{-|\(\*|[Rr][Ee][Mm])|^[[:space:]]*\*)[[:space:]]*(shortcut:|honey:|TODO|FIXME)/)) next
      marker = substr(text, RSTART, RLENGTH)
      rest = substr(text, RSTART + RLENGTH)
      if (marker ~ /(TODO|FIXME)$/ && rest ~ /^[A-Za-z0-9_]/) next
      pre = marker
      sub(/[[:space:]]*(shortcut:|honey:|TODO|FIXME)$/, "", pre)
      sub(/^[[:space:]]+/, "", pre)
      if (!allowed(where, pre)) next

      # Judge the marker: TODO never passes; FIXME needs a trigger; shortcut both.
      low = tolower(rest)
      trigger = (low ~ /[;>]/ || low ~ /(^|[^a-z])(if|when|whenever|once|unless|until|after|upon|trigger|triggers|over|above|past|exceeds|beyond)([^a-z]|$)/)
      if (marker ~ /TODO$/) tag = "TODO"
      else if (marker ~ /FIXME$/) tag = trigger ? "" : "NO-TRIGGER"
      else tag = (rest !~ /[0-9]/) ? "NO-CEILING" : (trigger ? "" : "NO-TRIGGER")

      body = text
      sub(/^[[:space:]]+/, "", body)
      sub(/\r$/, "", body)
      if (length(body) > 120) body = substr(body, 1, 117) "..."
      if (tag != "") { printf "[%s] %s: %s\n", tag, where, body; bad++ }
      else { ok++; if (!quiet) printf "[TRACKED] %s: %s\n", where, body }
    }
    END {
      if (bad) print "Fix: shortcut: <quantified ceiling>; <revisit trigger> | FIXME: <defect>; <trigger condition> | no bare TODO."
      if (bad || !quiet) printf "craft-debt-scan: %d violation(s), %d tracked marker(s).\n", bad, ok
      exit (bad > 0)
    }'
}

# Print "path:line:text" for every marker candidate under the scanned paths.
scan_paths() {
  local args=(-rnHIE "$MARK") d f
  for d in "${SKIP_DIRS[@]}"; do args+=(--exclude-dir="$d"); done
  for f in "${SKIP_FILES[@]}"; do args+=(--exclude="$f"); done
  grep "${args[@]}" -- "${paths[@]}" 2>/dev/null
}

# Print "path:line:text" for the lines of the hook's edited file worth scanning.
hook_lines() {
  local file dir base
  file=$(sed -n 's/.*"file_path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)
  file=${file//\\\\/\/}
  [ -n "$file" ] && [ -f "$file" ] || return 0
  case "$file" in *.md|*.mdc|*.markdown|*.rst|*.txt) return 0 ;; esac
  # Why: this script documents the marker formats in its own header; the
  # repository scan skips it by name, and so must the hook.
  [ "$(basename -- "$file")" = "$(basename -- "$0")" ] && return 0
  dir=$(dirname -- "$file")
  base=$(basename -- "$file")
  if git -C "$dir" rev-parse -q --verify HEAD >/dev/null 2>&1 &&
     git -C "$dir" ls-files --error-unmatch -- "$base" >/dev/null 2>&1; then
    git -C "$dir" diff -U0 --no-color --no-ext-diff HEAD -- "$base" 2>/dev/null |
      awk -v p="$file" '
        /^@@/ { n = $3; sub(/^\+/, "", n); sub(/,.*/, "", n); n += 0; next }
        /^\+\+\+/ { next }
        /^\+/ { print p ":" n ":" substr($0, 2); n++ }'
  else
    grep -nHIE "$MARK" -- "$file" 2>/dev/null
  fi
}

if [ "$HOOK" -eq 1 ]; then
  out=$(hook_lines | classify) && exit 0
  printf '%s\n' "$out" >&2
  exit 2
fi
scan_paths | classify
