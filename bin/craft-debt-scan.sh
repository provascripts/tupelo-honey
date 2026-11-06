#!/usr/bin/env bash
# craft-debt-scan.sh -- stable command name of the debt-marker ledger and linter.
#
# Why a shim: hooks, skills, agents and users have called this name since
# v0.0.1, while the scanner moved to craft-debt-scan.py in v0.0.2 so that it
# reads markers from real comments only, through the shared lexer in
# craftlib.py. Usage and finding kinds: craft-debt-scan.sh --help.
here=$(dirname -- "$0")
py=$(command -v python3 || command -v python) || { echo "craft-debt-scan: python3 not found" >&2; exit 1; }
exec "$py" "$here/craft-debt-scan.py" "$@"
