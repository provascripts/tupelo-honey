#!/usr/bin/env python3
"""craft-debt-scan.py -- zero-token ledger and linter for debt markers.

Usage (craft-debt-scan.sh, the stable command name, runs this file):
  craft-debt-scan.sh [--quiet] [PATH...]  scan PATHs (default .): print the ledger of
                                          tracked markers and every violation; exit 1
                                          if there is any violation
  craft-debt-scan.sh --hook               PostToolUse: judge only the lines the Edit,
                                          Write or MultiEdit on stdin wrote; findings
                                          on stderr with exit 2

Debt lives in two comment markers, each carrying its own exit condition on
line 1 (AGENTS.md, "Debt"), and a marker must open a comment:

  - "shortcut: <quantified ceiling>; <revisit trigger>"   deliberate simplification
  - "FIXME: <defect>; <trigger condition>"                 known defect

Findings, one line each ("path:line: [KIND] source line"):
  [TODO]        a bare TODO, XXX, HACK or lowercase todo: rewrite it as one of the
                two markers above.
  [NO-CEILING]  a shortcut without a number: name the ceiling ("~10k entries").
  [NO-TRIGGER]  a shortcut or FIXME without a trigger (';' before at least two
                words, '>', if, when, once...).
  [TRACKED]     a well-formed marker (ledger only: never with --quiet or --hook).

Markers are read from real comments through the shared lexer in craftlib.py,
so a "# TODO" inside a string literal is never one; files without a lexer
family (YAML, TOML, HTML, Dockerfile...) fall back to a line match, and prose
(*.md, *.mdc, *.rst, *.txt) is never scanned. The Stop gate (craft-gate.py)
runs the same check over every file the session changed.
"""

import argparse
import os
import sys

import craftlib as cl

HINT = "Fix: shortcut: <quantified ceiling>; <revisit trigger> | FIXME: <defect>; <trigger condition> | no bare TODO, XXX or HACK."
SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", "target", "out", ".venv", "venv",
             "__pycache__", ".tox", ".mypy_cache", ".pytest_cache", ".next", ".cache"}


def _finding(where, no, verdict, source):
    """Format one marker as a report line; an empty verdict is a tracked marker."""
    source = source if len(source) <= 120 else source[:117] + "..."
    return "%s:%d: [%s] %s" % (where, no, verdict or "TRACKED", source)


def scan_paths(paths, quiet):
    """Print the ledger of PATHs to stdout and return the exit code: 1 when a marker is a violation."""
    # Every file under the PATH arguments, vendored and generated trees skipped.
    files = []
    for top in paths:
        if os.path.isfile(top):
            files.append(top)
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            files += [os.path.join(dirpath, name) for name in sorted(filenames)]

    bad, tracked, out = 0, 0, []
    for path in files:
        text = cl.read_text(path) if cl.debt_scannable(path) else None
        if text is None:
            continue
        where = path.replace(os.sep, "/")
        for no, verdict, source in cl.debt_markers(path, text):
            bad, tracked = bad + bool(verdict), tracked + (not verdict)
            if verdict or not quiet:
                out.append(_finding(where, no, verdict, source))
    if bad:
        out.append(HINT)
    if bad or not quiet:
        out.append("craft-debt-scan: %d violation(s), %d tracked marker(s)." % (bad, tracked))
    if out:
        # Why: Windows consoles default to the ANSI code page, which cannot
        # encode arbitrary comment text.
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
        print("\n".join(out))
    return 1 if bad else 0


def from_hook(data):
    """Return the report lines for violations among the lines the Edit, Write or MultiEdit in `data` wrote."""
    got = cl.edit_lines(data)
    if not got:
        return []
    path, text, lines = got
    # Why: in a file the lexer misreads, a marker may sit in a heredoc or a
    # raw string; the Stop gate reports it as a review signal instead.
    if cl.inexact_reason(path, text):
        return []
    where = cl.shown(path)
    out = [_finding(where, no, verdict, source) for no, verdict, source in cl.debt_markers(path, text, lines)
           if verdict]
    return out + [HINT] if out else []


def gate_findings(root, changes):
    """Stop gate: return (key, message) for each violation on the lines `changes` added.

    Keys omit line numbers, which shift with every edit; see craft-gate.py.
    """
    out = []
    for ch in changes:
        for no, verdict, source in cl.debt_markers(ch.rel, ch.text, ch.added):
            if verdict:
                out.append(("debt|%s|%s|%s" % (ch.rel, verdict, source), _finding(ch.rel, no, verdict, source)))
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quiet", action="store_true", help="print violations only")
    parser.add_argument("--hook", action="store_true", help="PostToolUse mode: read the hook JSON on stdin")
    parser.add_argument("paths", nargs="*", help="files or directories to scan (default .)")
    args = parser.parse_args()
    if args.hook:
        return cl.report(from_hook(cl.hook_input()), hook=True)
    return scan_paths(args.paths or ["."], args.quiet)


if __name__ == "__main__":
    sys.exit(main())
