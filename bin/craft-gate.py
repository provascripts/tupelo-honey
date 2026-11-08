#!/usr/bin/env python3
"""craft-gate.py -- the Stop gate: every check, on every file the session changed.

Usage (Claude Code runs the first three as hooks, see hooks/hooks.json):
  craft-gate.py --snapshot   SessionStart: remember HEAD and the files already dirty
  craft-gate.py              Stop: judge the files the session changed
  craft-gate.py --prompt     UserPromptSubmit: hand Claude the signals the last Stop queued
  craft-gate.py --staged     git pre-commit: judge the staged files; exit 1 on hard findings

Scope. A file is judged when it differs from the commit HEAD named at
SessionStart (HEAD itself when there is no snapshot) and its bytes changed
since the snapshot. Edits made through Bash, generators or sed count like
Edit and Write; work left uncommitted before the session and not touched
since never blocks it. Outside git there is no baseline: the gate is silent.

Two tiers (craftlib.py's design notes explain why PostToolUse runs only the
checks that one edit decides alone):

  hard   craft-debt-scan and craft-comment-sync findings (debt markers,
         ORPHAN in a comment, BACKUP, TRIVIAL from 80%, DIAGRAM-*): each
         one blocks every Stop while it exists, stop_hook_active or not.
  soft   craft-metrics review signals (RHYTHM, HELPER, NO-DOC, NO-DESIGN,
         NO-TEACHER, NO-LAYOUT-ASSERT, NO-TEST), TRIVIAL from 60%, VAGUE,
         ORPHAN in a prose document, and every hard finding of a file the
         lexer misreads (collect()): shown once per session and never
         again; the judgment stays with AGENTS.md section 1. They never
         cost a turn: with hard findings due they ride along on stderr;
         otherwise Claude stops, the user reads them at once in a
         systemMessage and Claude gets them with the next prompt.

Escape hatches, so that a finding Claude cannot or should not fix never
traps it in a loop:
  - a hard finding blocks at most MAX_TRIES consecutive Stops; then it is
    released for the session and the user is told (systemMessage);
  - within one turn (a chain of stop_hook_active Stops) at most MAX_TRIES
    Stops in a row are blocked, whatever the findings.
Claude Code also caps consecutive Stop continuations on its own (8).

A git command that fails for good (craftlib.git, after its retries) leaves
partial answers, and a block built on them would be false: a missing baseline
makes every old line look written by the session. So that Stop, or that
commit, passes in silence and the next Stop judges again. A snapshot git could
not take is stored as broken and every Stop of that session passes, since
nothing can say any more what the session changed.

State: one JSON file per session and kind (snap, gate) in the temporary
directory; the gate file also queues the signals for the next prompt.
SessionStart deletes the files older than STATE_TTL seconds (7 days).
Finding keys omit line numbers, which shift with every edit.
"""

import argparse
import glob
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import time

import craftlib as cl

MAX_TRIES = 3
STATE_TTL = 7 * 86400
MAX_FILES = 5000
SHOWN_PER_TIER = 20
SIGNALS = "Review signals, shown once: act where one reveals a real problem; the judgment is yours (AGENTS.md section 1):"
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


def _purge_states():
    """Delete the state files of sessions untouched for over STATE_TTL seconds."""
    now = time.time()
    for kind in ("snap", "gate"):
        for path in glob.glob(os.path.join(glob.escape(tempfile.gettempdir()), "craft-%s-*.json" % kind)):
            # Why the try: two sessions starting together race for the same file.
            try:
                if now - os.path.getmtime(path) > STATE_TTL:
                    os.remove(path)
            except OSError:
                pass


def snapshot(data):
    """SessionStart: record HEAD and the digest of every dirty or untracked file; keep an existing snapshot.

    Why keep it: resume and compaction fire SessionStart again for the same
    session, and what the session changed before must still count as its own.
    Also purges the state files of sessions older than STATE_TTL.
    """
    _purge_states()
    failures = cl.git_failures()
    session, root = data.get("session_id"), cl.repo_root(data.get("cwd") or os.getcwd())
    if not session or not (root or cl.git_failures() != failures):
        return 0
    path = _state_path("snap", session)
    if os.path.exists(path):
        return 0
    base, listed = cl.EMPTY_TREE, ""
    if root:
        head = cl.git(["rev-parse", "-q", "--verify", "HEAD^{commit}"], root)
        base = head.strip() if head else cl.EMPTY_TREE
        listed = (cl.git(["diff", "--name-only", base, "--"], root) or "") \
            + (cl.git(["ls-files", "--others", "--exclude-standard"], root) or "")
    if cl.git_failures() != failures:
        _save_state(path, {"broken": True})
        return 0
    dirty ={rel: _digest(os.path.join(root, rel)) for rel in listed.splitlines()[:MAX_FILES]
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
    new state): the hard findings that block this Stop, the soft findings
    not shown yet in this session, and the messages of hard findings given
    up on at this Stop, for the user. Only hard findings block, so only they
    count toward the per-turn streak.
    """
    streak = state.get("streak", 0) if active else 0
    hard_keys = list(dict.fromkeys(k for k, _ in hard))
    tries = {k: n for k, n in (state.get("tries") or {}).items() if k in hard_keys}
    shown = set(state.get("shown") or [])
    released = [k for k in state.get("released") or [] if k in hard_keys]
    due = [k for k in hard_keys if tries.get(k, 0) < MAX_TRIES] if streak < MAX_TRIES else []
    fresh = {k for k, _ in soft if k not in shown}
    for k in due:
        tries[k] = tries.get(k, 0) + 1
    release = []
    if not due:
        unresolved = [k for k in hard_keys if k not in released]
        release = [m for k, m in hard if k in unresolved]
        released += unresolved
    new_state = {"streak": streak + 1 if due else 0, "tries": tries, "shown": sorted(shown | fresh),
                 "released": released, "pending": state.get("pending") or []}
    return [(k, m) for k, m in hard if k in due], [(k, m) for k, m in soft if k in fresh], release, new_state


def _capped(messages):
    """Return at most SHOWN_PER_TIER messages, plus a count of the rest."""
    extra = len(messages) - SHOWN_PER_TIER
    return messages[:SHOWN_PER_TIER] + (["... %d more" % extra] if extra > 0 else [])


def collect(root, changes):
    """Return (hard, soft) lists of (key, message): every check of the three scanners on `changes`, tiered.

    Findings in generated files (cl.generated) are dropped.
    A hard finding in a file the lexer misreads (cl.inexact_reason) becomes a
    soft one whose message names the reason: there the finding may be an
    artifact of code read as comment. scripts/replay-history.py calls this
    too, to judge a commit as a session.
    """
    sync_hard, sync_soft = _load("craft-comment-sync").gate_findings(root, changes)
    hard = _load("craft-debt-scan").gate_findings(root, changes) + sync_hard
    soft = sync_soft + _load("craft-metrics").gate_findings(root, changes)

    # A finding's file is the second key field; an ORPHAN may place it outside
    # `changes`, and then it is read from disk.
    texts = {ch.rel: ch.text for ch in changes}

    def text_of(rel):
        """Return the text of `rel`, read at most once per call of collect()."""
        if rel not in texts:
            texts[rel] = cl.read_text(os.path.join(root, rel))
        return texts[rel]

    # Why on the findings and not on `changes`: orphan_findings() must still read
    # a generated file as code that uses a name, or every name only it uses would
    # look gone and the hand-written comments citing it would be false ORPHANs.
    skip = cl.generated(root, {key.split("|")[1] for key, _ in hard + soft}, text_of)
    hard = [f for f in hard if f[0].split("|")[1] not in skip]
    soft = [f for f in soft if f[0].split("|")[1] not in skip]

    # Split the hard findings by whether the lexer reads their file exactly.
    reasons, exact = {}, []
    for key, message in hard:
        rel = key.split("|")[1]
        if rel not in reasons:
            text = text_of(rel)
            reasons[rel] = text is not None and cl.inexact_reason(rel, text)
        if reasons[rel]:
            soft.append((key, "%s (inexact file: %s; read it before acting)" % (message, reasons[rel])))
        else:
            exact.append((key, message))
    return exact, soft


def gate(data):
    """Stop: block on what the session must still fix; pass the review signals on without costing a turn.

    Returns the exit code. Hard findings due go to stderr with exit 2, so
    Claude continues, together with the fresh review signals. Otherwise
    Claude stops (exit 0): the user gets the fresh signals and any release
    note at once in a systemMessage, and the signals wait in the session
    state until the next UserPromptSubmit hands them to Claude (prompt()).
    """
    root = cl.repo_root(data.get("cwd") or os.getcwd())
    if not root:
        return 0
    session = data.get("session_id")
    snap = _load_state(_state_path("snap", session)) if session else {}
    if snap.get("broken"):
        return 0
    failures = cl.git_failures()
    changes = session_changes(root, snap)
    hard, soft = collect(root, changes) if changes else ([], [])
    if cl.git_failures() != failures:
        return 0
    path = _state_path("gate", session) if session else None
    if path:
        block, show, release, state = decide(_load_state(path), bool(data.get("stop_hook_active")), hard, soft)
    else:
        block, show, release, state = hard, soft, [], {}

    # 1: Hard findings due: Claude continues with them, and the fresh signals, on stderr.
    if block:
        out = ["tupelo-honey Stop gate: fix before finishing (checked again at every Stop, at most %d times each):"
               % MAX_TRIES] + _capped([m for _, m in block])
        if any(k.startswith("debt|") for k, _ in block):
            out.append(_load("craft-debt-scan").HINT)
        if show:
            out += [SIGNALS] + _capped([m for _, m in show])
        if path:
            _save_state(path, state)
        return cl.report(out, hook=True, limit=len(out))

    # 2: Nothing blocks: the user hears at once, Claude with the next prompt.
    notes = []
    if show:
        notes.append("tupelo-honey review signals, for Claude with your next message: "
                     + " | ".join(_capped([m for _, m in show])))
        state["pending"] = list(dict.fromkeys((state.get("pending") or []) + [m for _, m in show]))
    if release:
        notes.append("tupelo-honey: Claude stopped with %d unresolved finding(s) after %d attempts: %s" % (
            len(release), MAX_TRIES, " | ".join(release[:3]) + (" | ..." if len(release) > 3 else "")))
    if path:
        _save_state(path, state)
    if notes:
        print(json.dumps({"systemMessage": " ".join(notes)}))
    return 0


def prompt(data):
    """UserPromptSubmit: hand Claude, once, the review signals the last Stop queued; return 0.

    Claude Code adds the plain stdout of a UserPromptSubmit hook that exits 0
    to Claude's context next to the prompt; nothing here can block it.
    """
    session = data.get("session_id")
    path = _state_path("gate", session) if session else None
    state = _load_state(path) if path else {}
    pending = state.pop("pending", None)
    if not pending:
        return 0
    _save_state(path, state)
    lines = ["tupelo-honey, from the last Stop. " + SIGNALS] + _capped(pending)
    # Why: report() writes to stdout outside hook mode and returns 1; this hook must exit 0.
    cl.report(lines, hook=False, limit=len(lines))
    return 0


def staged_changes(root):
    """Return the cl.Change list of the files staged for commit, read from the index."""
    head = cl.git(["rev-parse", "-q", "--verify", "HEAD^{commit}"], root)
    base = head.strip() if head else cl.EMPTY_TREE
    out = []
    for rel, new in cl.tracked_changes(root, base, cached=True)[:MAX_FILES]:
        text = cl.git(["show", ":" + rel], root) if cl.debt_scannable(rel) else None
        if text is None or len(text) > cl.MAX_BYTES:
            continue
        old = "" if new or not cl.family_of(rel) else cl.baseline_text(root, base, rel)
        added = set(range(1, text.count("\n") + 2)) if new else cl.added_lines(root, base, rel, cached=True)
        out.append(cl.Change(rel, os.path.join(root, rel), text, old, new, added))
    return out


def staged():
    """git pre-commit: judge the staged files with every check; return 1 on hard findings, else 0.

    Findings go to stderr, which git shows; exit 1 aborts the commit. There
    is no session state: every commit is judged afresh.
    """
    root = cl.repo_root(os.getcwd())
    failures = cl.git_failures()
    changes = staged_changes(root) if root else []
    hard, soft = collect(root, changes) if changes else ([], [])
    if cl.git_failures() != failures:
        return 0
    out = []
    if hard:
        out += ["tupelo-honey: fix before committing:"] + _capped([m for _, m in hard])
        if any(k.startswith("debt|") for k, _ in hard):
            out.append(_load("craft-debt-scan").HINT)
    if soft:
        out += ["tupelo-honey: " + SIGNALS] + _capped([m for _, m in soft])
    if out:
        cl.report(out, hook=True, limit=len(out))
    return 1 if hard else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--snapshot", action="store_true", help="SessionStart: record the files already dirty")
    mode.add_argument("--prompt", action="store_true", help="UserPromptSubmit: hand Claude the queued review signals")
    mode.add_argument("--staged", action="store_true", help="git pre-commit: judge the staged files")
    args = parser.parse_args()
    if args.staged:
        return staged()
    data = cl.hook_input()
    return snapshot(data) if args.snapshot else prompt(data) if args.prompt else gate(data)


if __name__ == "__main__":
    sys.exit(main())
