#!/usr/bin/env python3
"""craft-gate.py -- the Stop gate: every check, on every file the session changed.

Usage (Claude Code runs both as hooks, see hooks/hooks.json):
  craft-gate.py --snapshot   SessionStart: remember HEAD and the files already dirty
  craft-gate.py              Stop: judge the files the session changed

Scope. A file is judged when it differs from the commit HEAD named at
SessionStart (HEAD itself when there is no snapshot) and its bytes changed
since the snapshot. Edits made through Bash, generators or sed count like
Edit and Write; work left uncommitted before the session and not touched
since never blocks it. Outside git there is no baseline: the gate is silent.

Two tiers (craftlib.py's design notes explain why PostToolUse runs only the
checks that one edit decides alone):

  hard   craft-debt-scan and craft-comment-sync findings (debt markers,
         ORPHAN, BACKUP, TRIVIAL, VAGUE, DIAGRAM-*): objective, so each one
         blocks every Stop while it exists, stop_hook_active or not.
  soft   craft-metrics review signals (RHYTHM, HELPER, DMOD, NO-DOC,
         NO-DESIGN, NO-TEACHER, NO-LAYOUT-ASSERT): shown once per session
         and never again; the judgment stays with AGENTS.md section 1.

Escape hatches, so that a finding Claude cannot or should not fix never
traps it in a loop:
  - a hard finding blocks at most MAX_TRIES consecutive Stops; then it is
    released for the session and the user is told (systemMessage);
  - within one turn (a chain of stop_hook_active Stops) at most MAX_TRIES
    Stops in a row are blocked, whatever the findings.
Claude Code also caps consecutive Stop continuations on its own (8).

State: one JSON file per session and kind (snap, gate) in the temporary
directory. Finding keys omit line numbers, which shift with every edit.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import tempfile

import craftlib as cl

MAX_TRIES = 3
MAX_FILES = 5000
SHOWN_PER_TIER = 20
HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name):
    """Import bin/<name>.py as a module (the hyphen in the file name rules out a plain import)."""
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), os.path.join(HERE, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _state_path(kind, session):
    """Return the path of the `kind` state file ('snap' or 'gate') of one session."""
    digest_ = hashlib.sha1(session.encode("utf-8")).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), "craft-%s-%s.json" % (kind, digest_))


def _load_state(path):
    """Return the JSON object stored at `path`, or {} when it is missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(path, data):
    """Write `data` to `path` as JSON; a failure only costs the deduplication, so it is ignored."""
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError:
        pass


def _digest(path):
    """Return the SHA-1 of a file's bytes, or '' when it cannot be read or exceeds cl.MAX_BYTES.

    Why the size cap: the checks never read files over cl.MAX_BYTES, so
    hashing a multi-gigabyte untracked dump at SessionStart would buy nothing.
    """
    try:
        if os.path.getsize(path) > cl.MAX_BYTES:
            return ""
        with open(path, "rb") as fh:
            return hashlib.sha1(fh.read()).hexdigest()
    except OSError:
        return ""


def snapshot(data):
    """SessionStart: record HEAD and the digest of every dirty or untracked file; keep an existing snapshot.

    Why keep it: resume and compaction fire SessionStart again for the same
    session, and what the session changed before must still count as its own.
    """
    session, root = data.get("session_id"), cl.repo_root(data.get("cwd") or os.getcwd())
    if not session or not root:
        return 0
    path = _state_path("snap", session)
    if os.path.exists(path):
        return 0
    head = cl.git(["rev-parse", "-q", "--verify", "HEAD^{commit}"], root)
    base = head.strip() if head else cl.EMPTY_TREE
    listed = (cl.git(["diff", "--name-only", base, "--"], root) or "") \
        + (cl.git(["ls-files", "--others", "--exclude-standard"], root) or "")
    dirty = {rel: _digest(os.path.join(root, rel)) for rel in listed.splitlines()[:MAX_FILES]
             if os.path.isfile(os.path.join(root, rel))}
    _save_state(path, {"root": os.path.normcase(root), "base": base, "dirty": dirty})
    return 0


def session_changes(root, snap):
    """Return the cl.Change list of files the session changed (scope rules in the module docstring)."""
    base = snap.get("base") if snap.get("root") == os.path.normcase(root) else None
    if base != cl.EMPTY_TREE and not (base and cl.git(["rev-parse", "-q", "--verify", base + "^{commit}"], root)):
        base, snap = cl.resolve_base(root, "HEAD"), {}
    dirty = snap.get("dirty") or {}
    out = []
    for rel, new in cl.changed_files(root, base, keep=cl.debt_scannable)[:MAX_FILES]:
        path = os.path.join(root, rel)
        if rel in dirty and dirty[rel] == _digest(path):
            continue
        text = cl.read_text(path)
        if text is None:
            continue
        old = "" if new or not cl.family_of(rel) else cl.baseline_text(root, base, rel)
        added = set(range(1, text.count("\n") + 2)) if new else cl.added_lines(root, base, rel)
        out.append(cl.Change(rel, path, text, old, new, added))
    return out


def decide(state, active, hard, soft):
    """Apply the two tiers and both escape hatches to one Stop.

    `hard` and `soft` are (key, message) lists; several messages may share a
    key (one orphan named by three comments). Returns (block, show, release,
    new state): the hard and soft findings to report now, and the messages of
    hard findings given up on at this Stop, for the user.
    """
    streak = state.get("streak", 0) if active else 0
    hard_keys = list(dict.fromkeys(k for k, _ in hard))
    tries = {k: n for k, n in (state.get("tries") or {}).items() if k in hard_keys}
    shown = set(state.get("shown") or [])
    released = [k for k in state.get("released") or [] if k in hard_keys]
    due = [k for k in hard_keys if tries.get(k, 0) < MAX_TRIES]
    fresh = {k for k, _ in soft if k not in shown}
    if streak >= MAX_TRIES:
        due, fresh = [], set()
    for k in due:
        tries[k] = tries.get(k, 0) + 1
    blocking = bool(due or fresh)
    release = []
    if not blocking:
        unresolved = [k for k in hard_keys if k not in released]
        release = [m for k, m in hard if k in unresolved]
        released += unresolved
    new_state = {"streak": streak + 1 if blocking else 0, "tries": tries, "shown": sorted(shown | fresh),
                 "released": released}
    return [(k, m) for k, m in hard if k in due], [(k, m) for k, m in soft if k in fresh], release, new_state


def _capped(messages):
    """Return at most SHOWN_PER_TIER messages, plus a count of the rest."""
    extra = len(messages) - SHOWN_PER_TIER
    return messages[:SHOWN_PER_TIER] + (["... %d more" % extra] if extra > 0 else [])


def gate(data):
    """Stop: report what the session must still fix and the review signals; return the exit code."""
    root = cl.repo_root(data.get("cwd") or os.getcwd())
    if not root:
        return 0
    session = data.get("session_id")
    changes = session_changes(root, _load_state(_state_path("snap", session)) if session else {})
    hard, soft, hint = [], [], None
    if changes:
        debt, sync, metrics = _load("craft-debt-scan"), _load("craft-comment-sync"), _load("craft-metrics")
        hard = debt.gate_findings(root, changes) + sync.gate_findings(root, changes)
        soft = metrics.gate_findings(root, changes)
        hint = debt.HINT
    if session:
        path = _state_path("gate", session)
        block, show, release, state = decide(_load_state(path), bool(data.get("stop_hook_active")), hard, soft)
        _save_state(path, state)
    else:
        block, show, release = hard, soft, []

    # Report: hard findings first, then the signals; release notes go to the user only.
    out = []
    if block:
        out.append("tupelo-honey Stop gate: fix before finishing (checked again at every Stop, at most %d times "
                   "each):" % MAX_TRIES)
        out += _capped([m for _, m in block])
        if any(k.startswith("debt|") for k, _ in block):
            out.append(hint)
    if show:
        out.append("Review signals, shown once: act where one reveals a real problem; the judgment is yours "
                   "(AGENTS.md section 1):")
        out += _capped([m for _, m in show])
    if out:
        return cl.report(out, hook=True, limit=len(out))
    if release:
        note = "tupelo-honey: Claude stopped with %d unresolved finding(s) after %d attempts: %s" % (
            len(release), MAX_TRIES, " | ".join(release[:3]) + (" | ..." if len(release) > 3 else ""))
        print(json.dumps({"systemMessage": note}))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot", action="store_true", help="SessionStart mode: record the files already dirty")
    args = parser.parse_args()
    data = cl.hook_input()
    return snapshot(data) if args.snapshot else gate(data)


if __name__ == "__main__":
    sys.exit(main())
