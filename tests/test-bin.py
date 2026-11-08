#!/usr/bin/env python3
"""Runnable tests for tupelo-honey: bin/ scripts, hook wiring, plugin layout and mirrors.

Run: python3 tests/test-bin.py   (needs git and bash on PATH; on Windows run it
from Git Bash, so that `bash` is Git's and not the WSL launcher).

The randomized tests compare the scripts against oracles built by their own
generators: the generator knows which line is a violation, which token is
code and which is comment. They are seeded, report the seed on failure, and
replay with CRAFT_SEED=<seed>.
"""

import collections
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import random
import re
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import unittest.mock
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
sys.dont_write_bytecode = True
sys.path.insert(0, BIN)
import craftlib as cl  # noqa: E402

SEED = int(os.environ.get("CRAFT_SEED") or random.SystemRandom().randrange(1 << 30))
BASH = os.environ.get("CRAFT_BASH") or shutil.which("bash")
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
# Why: craft-debt-scan reads comments only since v0.0.2, but grep-based
# scanners (and any older installed copy of this plugin) would still count
# markers written literally in this file, so the fixtures assemble them.
TODO, FIXME, SHORTCUT, HONEY = "TO" + "DO", "FIX" + "ME", "short" + "cut:", "hon" + "ey:"
XXX, HACK = "X" + "XX", "HA" + "CK"
FIXTURES = os.path.join(ROOT, "tests", "fixtures")


def load_script(name):
    """Import bin/<name>.py as a module (the hyphen rules out a plain import)."""
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), os.path.join(BIN, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


metrics = load_script("craft-metrics")
sync = load_script("craft-comment-sync")
gatekeeper = load_script("craft-gate")


# Why: on Windows an antivirus or an indexer can hold a file git is writing
# under .git for a moment. Fixture commits failed about one suite run in twenty
# with these messages, never the same test twice. Only these lines are retried,
# so a real git failure still surfaces on its first attempt.
GIT_TRANSIENT = re.compile(r"unable to (?:write|create)|failed to write", re.I)
GIT_ATTEMPTS = 5


def run(args, cwd=None, stdin=None):
    """Run a command; return (exit code, stdout, stderr) as text.

    A git command failing with GIT_TRANSIENT is run again, up to GIT_ATTEMPTS
    times in all, after a short pause; the last attempt's result is returned.
    """
    for attempt in range(1, GIT_ATTEMPTS + 1):
        r = subprocess.run(args, cwd=cwd, input=stdin, capture_output=True, encoding="utf-8",
                           errors="replace", env=ENV)
        if r.returncode == 0 or args[0] != "git" or attempt == GIT_ATTEMPTS or not GIT_TRANSIENT.search(r.stderr):
            return r.returncode, r.stdout, r.stderr
        time.sleep(0.05 * attempt)


def posix(path):
    """Spell a path so that Git Bash accepts it as a script argument on Windows too."""
    return path.replace("\\", "/")


def debt(*args, cwd=None, stdin=None):
    return run([BASH, posix(os.path.join(BIN, "craft-debt-scan.sh")), *args], cwd=cwd, stdin=stdin)


def script(name, *args, cwd=None, stdin=None):
    return run([sys.executable, os.path.join(BIN, name), *args], cwd=cwd, stdin=stdin)


def edit_payload(path, **tool_input):
    """Hook JSON for an Edit of `path`, as Claude Code sends it on stdin."""
    return json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Edit",
                       "tool_input": dict(file_path=path, **tool_input)})


def read(rel):
    return read_file(os.path.join(ROOT, rel))


def read_file(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def c_function(name, loc, guide_every=0, static=False):
    """C function whose body has exactly `loc` code lines, with a Guide comment every `guide_every`."""
    body = []
    for i in range(loc - 1):
        if guide_every and i % guide_every == 0:
            body.append("    /* %d: stage %d of %s. */" % (i // guide_every + 1, i, name))
        body.append("    x += %d;" % i)
    body.append("    return x;")
    return "%sint %s(int x) {\n%s\n}\n" % ("static " if static else "", name, "\n".join(body))


def c_nested(name, loc=70):
    """C function with exactly `loc` code lines and no comment: three nested blocks, then a flat tail."""
    head = ["    if (x > 0) {", "        for (int i = 0; i < x; i++) {", "            if (i % 2) {",
            "                x -= 1;", "            }", "        }", "    }"]
    body = head + ["    x += %d;" % i for i in range(loc - len(head) - 1)] + ["    return x;"]
    return "int %s(int x) {\n%s\n}\n" % (name, "\n".join(body))


class Repo:
    """A throwaway directory, optionally a git repository, with file and commit helpers."""

    def __init__(self, git=True):
        self.path = tempfile.mkdtemp(prefix="craft-test-")
        if git:
            run(["git", "init", "-q"], cwd=self.path)
            run(["git", "config", "core.autocrlf", "false"], cwd=self.path)

    def write(self, rel, text):
        path = os.path.join(self.path, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(textwrap.dedent(text).lstrip("\n"))
        return path

    def commit(self):
        code, out, err = run(["git", "add", "-A"], cwd=self.path)
        assert code == 0, out + err
        code, out, err = run(["git", "-c", "user.email=test@example.com", "-c", "user.name=test",
                              "-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture"], cwd=self.path)
        assert code == 0, out + err

    def close(self):
        def retry(func, path, _exc):
            os.chmod(path, stat.S_IWRITE)
            func(path)
        if sys.version_info >= (3, 12):
            shutil.rmtree(self.path, onexc=retry)
        else:
            shutil.rmtree(self.path, onerror=retry)


# Per extension, for the generated marker files: the line comment opener, the
# block comment delimiters (None when the language has none) and the quote of
# a string literal, which must hide every marker written inside it.
DEBT_SYNTAX = {".py": ("#", None, '"'), ".c": ("//", ("/*", "*/"), '"'), ".sh": ("#", None, '"'),
               ".lua": ("--", ("--[[", "]]"), '"'), ".sql": ("--", ("/*", "*/"), "'"),
               ".lisp": (";", ("#|", "|#"), '"'), ".erl": ("%", None, '"'), ".hs": ("--", ("{-", "-}"), '"'),
               ".rs": ("//", ("/*", "*/"), '"')}


class RunRetryTest(unittest.TestCase):
    def test_only_transient_git_failures_are_retried(self):
        transient, real = "fatal: unable to write new index file", "fatal: not a git repository"
        # (command, stderr, failures before success, spawns expected, exit code expected)
        cases = [(["git", "add"], transient, k, k + 1, 0) for k in range(GIT_ATTEMPTS)]
        cases += [(["git", "add"], transient, GIT_ATTEMPTS, GIT_ATTEMPTS, 1), (["git", "add"], real, 3, 1, 1),
                  (["bash", "x"], transient, 3, 1, 1)]
        for args, err, failures, spawns, code in cases:
            results = [subprocess.CompletedProcess(args, 1, "", err)] * failures + [subprocess.CompletedProcess(args, 0, "", "")]
            with unittest.mock.patch("subprocess.run", side_effect=results) as spawn, unittest.mock.patch("time.sleep"):
                self.assertEqual(run(args)[0], code, (args, err, failures))
            self.assertEqual(spawn.call_count, spawns, (args, err, failures))


class DebtScanTest(unittest.TestCase):
    def setUp(self):
        self.repo = Repo(git=False)

    def tearDown(self):
        self.repo.close()

    @staticmethod
    def found(out):
        """Return sorted (kind, file, line) triples from "./file:line: [KIND] ..." report lines."""
        return sorted((kind, name, no) for name, no, kind in re.findall(r"^\./(\S+?):(\d+): \[([A-Z-]+)\]", out, re.M))

    def test_ledger_and_violations(self):
        self.repo.write("ok.py", f"""
            x = 1  # {SHORTCUT} O(N) scan, fine under ~10k entries; add an index past that.
            # {FIXME}: empty keys not handled; callers must reject them until fixed.
            y = "{TODO}"
            """)
        self.repo.write("bad.c", f"""
            // {TODO}: optimize
            /* {SHORTCUT} global lock; split it later */
            // {SHORTCUT} linear scan under 500 items
            /*
             * {FIXME} broken
             */
            // {HONEY} O(n^2) under 1k items; switch to a hash if n > 5000
            int {TODO}S = 1; // {TODO}LIST stays a word
            """)
        self.repo.write("notes.md", f"# {TODO} prose is never scanned\n")
        code, out, _ = debt(cwd=self.repo.path)
        self.assertEqual(code, 1, out)
        self.assertEqual(self.found(out), sorted([
            ("TODO", "bad.c", "1"), ("NO-CEILING", "bad.c", "2"), ("NO-TRIGGER", "bad.c", "3"),
            ("NO-TRIGGER", "bad.c", "5"), ("TRACKED", "bad.c", "7"), ("TRACKED", "ok.py", "1"),
            ("TRACKED", "ok.py", "2")]))
        self.assertIn("4 violation(s), 3 tracked marker(s)", out)

    def test_quiet_is_silent_when_clean(self):
        self.repo.write("ok.py", f"# {SHORTCUT} fixed 4 KiB buffer; grow it if a frame exceeds 4096 bytes\n")
        self.assertEqual(debt("--quiet", cwd=self.repo.path), (0, "", ""))

    def test_markers_in_literals_are_never_debt(self):
        # Regression (v0.0.1): the grep-based scanner reported every one of these
        # literals as a violation; only the comments may count.
        self.repo.write("lit.py", f'''
            message = "literal text # {TODO}: keep this string unchanged"
            banner = """multi-line
             # {FIXME} inside a triple-quoted literal
            """
            raw = r"C:\\path # {TODO}: raw"
            x = 1  # {TODO}: the real one
            ''')
        self.repo.write("lit.js", f'''
            const a = "text // {FIXME} broken";
            const b = `line
            // {FIXME} in a template`;
            const c = "x /* {SHORTCUT} no ceiling */"; const r = /a \\/\\/ {TODO}/g;
            /* a */ go(); // {FIXME} the second comment of this line
            ''')
        self.repo.write("lit.rs", f'let s = r#"x // {TODO}"#;\nlet t = "x"; // {TODO}: real\n')
        self.repo.write("lit.go", f'var s = `x // {TODO}`\n// {TODO}: real\n')
        code, out, _ = debt("--quiet", cwd=self.repo.path)
        self.assertEqual(self.found(out), sorted([("TODO", "lit.py", "6"), ("NO-TRIGGER", "lit.js", "5"),
                                                  ("TODO", "lit.rs", "2"), ("TODO", "lit.go", "2")]))
        self.assertEqual(code, 1)

    def test_hook_judges_only_the_lines_this_edit_wrote(self):
        path = self.repo.write("a.c", f"// {TODO}: legacy marker, not this edit's business\nint a;\n"
                                      f"// {FIXME} breaks things\nint b;\n")
        code, out, err = debt("--hook", stdin=edit_payload(path, old_string="int b;",
                                                           new_string=f"// {FIXME} breaks things\nint b;"))
        self.assertEqual((code, out), (2, ""), err)
        self.assertRegex(err, r"a\.c:3: \[NO-TRIGGER\]")
        self.assertNotIn("a.c:1:", err)
        # A Write wrote every line of the file.
        write = json.dumps({"tool_name": "Write", "tool_input": {"file_path": path, "content": "x"}})
        code, _, err = debt("--hook", stdin=write)
        self.assertEqual(code, 2)
        self.assertRegex(err, r"a\.c:1: \[TODO\]")

    def test_comment_openers_follow_the_language(self):
        for name, text in {"a.lisp": f"; {TODO}: lisp", "a.erl": f"% {FIXME} broken", "a.f90": f"! {TODO} fortran",
                           "a.vb": f"' {TODO} vb\nREM {FIXME} oops", "a.hs": f"-- {TODO} hs\n{{- {FIXME} nope -}}",
                           "a.ml": f"(* {TODO} ocaml *)", "a.html": f"<!-- {TODO} html -->", "a.lua": f"-- {TODO} lua",
                           "b.py": f"x = '%s' % {TODO}\ny = 1 ; {TODO}", "b.c": f"int x = a ! {TODO};\nfor (;;) ; {TODO}",
                           "c.lisp": f";; {TODO} doubled opener", "c.erl": f"%% {FIXME} doubled opener",
                           "c.py": f"## {TODO} banner", "c.yml": f"key: 1  # {TODO} no lexer: line match"}.items():
            self.repo.write(name, text + "\n")
        code, out, _ = debt("--quiet", cwd=self.repo.path)
        found = sorted(re.findall(r"^\./(\S+?):(\d+): \[[A-Z-]+\]", out, re.M))
        self.assertEqual(found, [("a.erl", "1"), ("a.f90", "1"), ("a.hs", "1"), ("a.hs", "2"), ("a.html", "1"),
                                 ("a.lisp", "1"), ("a.lua", "1"), ("a.ml", "1"), ("a.vb", "1"), ("a.vb", "2"),
                                 ("c.erl", "1"), ("c.lisp", "1"), ("c.py", "1"), ("c.yml", "1")])
        self.assertEqual(code, 1)

    def test_hook_ignores_prose_missing_files_and_empty_input(self):
        path = self.repo.write("notes.md", f"# {TODO}\n")
        self.assertEqual(debt("--hook", stdin=edit_payload(path))[0], 0)
        self.assertEqual(debt("--hook", stdin=edit_payload(path + ".gone.c"))[0], 0)
        self.assertEqual(debt("--hook", stdin="")[0], 0)

    def test_marker_spellings_and_triggers(self):
        cases = (("%s broken" % XXX, "TODO"), ("%s: around the parser" % HACK, "TODO"), (TODO.lower() + ": later", "TODO"),
                 (FIXME.lower() + ": slow", "NO-TRIGGER"), (FIXME.lower() + ": slow if n > 5", ""),
                 (FIXME + ": slow; later", "NO-TRIGGER"), (FIXME + ": slow; fix it", ""), (FIXME + ": slow;", "NO-TRIGGER"),
                 (SHORTCUT + " under 5 items; later", "NO-TRIGGER"), (SHORTCUT + " under 5 items; add an index", ""),
                 (HONEY + " under 5 items; add an index", ""),
                 # Only these spellings are markers: longer words and other capitalizations are prose.
                 (XXX + "L size", None), (HACK + "S are fun", None), (TODO.lower() + "list", None), (TODO.capitalize() + ": x", None))
        for body, verdict in cases:
            found = [v for _, v, _ in cl.debt_markers("a.py", "# %s\n" % body)]
            self.assertEqual(found, [] if verdict is None else [verdict], body)

    def test_random_markers_match_oracle(self):
        rng = random.Random(SEED)
        words = ["scan", "cache", "lookup", "table", "linear", "global", "lock", "buffer", "small", "path"]
        triggers = ["; switch it", "if it grows", "when it grows", "once it grows", "unless sorted", "until sorted",
                    "after the port", "upon reload", "over budget", "above budget", "past budget",
                    "exceeds budget", "beyond budget", "n > limit"]
        expected = set()
        for ext, (line_c, block, quote) in DEBT_SYNTAX.items():
            lines = []
            for no in range(1, 121):
                kind = rng.choice(["shortcut", "honey", "fixme", "lower-fixme", "todo", "lower-todo", "xxx", "hack", "plain"])
                ceiling, trigger = rng.random() < 0.5, rng.random() < 0.5
                text = " ".join(rng.choice(words) for _ in range(rng.randint(1, 4)))
                text += (" under %d items" % rng.randint(1, 99999)) if ceiling else ""
                # One word after a ';' names no condition, so it is no trigger.
                text += (" " + rng.choice(triggers)) if trigger else ("; later" if rng.random() < 0.3 else "")
                marker = {"shortcut": SHORTCUT, "honey": HONEY, "fixme": FIXME + ":", "lower-fixme": FIXME.lower() + ":",
                          "todo": TODO + ":", "lower-todo": TODO.lower() + ":", "xxx": XXX, "hack": HACK + ":",
                          "plain": ""}[kind]
                body = ("%s %s" % (marker, text)).strip()

                # A marker counts only where it opens a comment: never inside a
                # string literal, never in the middle of a comment.
                form = rng.choice(["line", "trailing", "block", "string", "middle"])
                form = "line" if form == "block" and not block else form
                lines.append({"line": "%s %s" % (line_c, body),
                              "trailing": "x = 1  %s %s" % (line_c, body),
                              "block": "%s %s %s" % (block[0], body, block[1]) if block else "",
                              "string": "s = %s%s %s%s" % (quote, line_c, body, quote),
                              "middle": "%s note: %s" % (line_c, body)}[form])
                bad = kind in ("todo", "lower-todo", "xxx", "hack") or (kind in ("fixme", "lower-fixme") and not trigger) \
                    or (kind in ("shortcut", "honey") and not (ceiling and trigger))
                if bad and form in ("line", "trailing", "block"):
                    expected.add(("gen" + ext, str(no)))
            self.repo.write("gen" + ext, "\n".join(lines) + "\n")
        code, out, _ = debt("--quiet", cwd=self.repo.path)
        found = set(re.findall(r"^\./(gen\.\w+):(\d+): \[[A-Z-]+\]", out, re.M))
        self.assertEqual(found, expected, "seed %d" % SEED)
        self.assertEqual(code, 1 if expected else 0)


# Generated sources for the lexer oracle: per family, its line comment opener
# and its block comment delimiters (None when the language has none).
_C_SYNTAX = ("//", ("/*", "*/"))
SYNTAX = {"c": _C_SYNTAX, "cs": _C_SYNTAX, "kt": _C_SYNTAX, "php": _C_SYNTAX, "go": _C_SYNTAX, "rs": _C_SYNTAX,
          "js": _C_SYNTAX, "zig": ("//", None), "py": ("#", None), "sh": ("#", None), "jl": ("#", ("#=", "=#")),
          "nim": ("#", ("#[", "]#")), "ps": ("#", ("<#", "#>")), "lua": ("--", ("--[[", "]]")),
          "sql": ("--", ("/*", "*/")), "hs": ("--", ("{-", "-}")), "lisp": (";", ("#|", "|#")),
          "ml": ("//", ("(*", "*)")), "erl": ("%", None), "f90": ("!", None), "vb": ("'", None)}
# Per-language traps, as (template, hides string tokens, has a comment):
# char literals, verbatim and raw strings, lifetimes and primes, regex and
# long-bracket literals, doubled-quote escapes, word-start '#', docstrings.
SPECIALS = {
    "c": ("{c} = '\"'; // {k}", False, True),
    "cs": ('{c} = @"C:\\{s} // {s}\n{s}"; // {k}', True, True),
    "kt": ('{c} = """{s} // {s}\n{s}""" // {k}', True, True),
    "php": ("{c} = '#{s} // {s}'; # {k}", True, True),
    "go": ("{c} := `{s} // {s}\n{s}` // {k}", True, True),
    "rs": ("fn {c}<'a>(x: &'a str) -> &'a str {{ x }} // {k}", False, True),
    "js": ("{c} = /{s}\\/\\/{s}/g; {c} = `{s} // {s}\n{s}` // {k}", True, True),
    "zig": ("{c} = \\\\{s} // {s}", True, False),
    "py": ('{c} = """{s} # {s}\n{s}"""  # {k}\n"""{k}\n{k}"""', True, True),
    "sh": ("echo ${{#{c}}} # {k}", False, True),
    "jl": ('{c} = "{s} # {s}" #= {k} =#', True, True),
    "nim": ('{c} = "{s} # {s}" #[ {k} ]#', True, True),
    "ps": ('{c} = "{s} # {s}" <# {k} #>', True, True),
    "lua": ("{c} = [[{s} -- {s}]] --[[ {k} ]]", True, True),
    "sql": ("{c} = 'it''s {s} -- {s}' -- {k}", True, True),
    "hs": ("{c} x' = x' --> y -- {k}", False, True),
    "lisp": ('({c} #\\" "{s} ; {s}") ; {k}', True, True),
    "ml": ("let {c} = '\"' (* {k} *)", False, True),
    "erl": ("{c} = '{s} % {s}' % {k}", True, True),
    "f90": ("{c} = 'it''s {s} ! {s}' ! {k}", True, True),
    "vb": ('{c} = "say ""{s}"" \' {s}" \' {k}', True, True),
}


def generate(rng, family):
    """Return (source, code_ids, comment_ids, hidden_ids) for one random source file.

    c<N> tokens are code, k<N> tokens live only in comments, s<N> tokens live
    only inside string or regex literals and must stay invisible to both.
    """
    line_c, block = SYNTAX[family]
    code_ids, comment_ids, hidden, out = set(), set(), set(), []
    for n in range(rng.randint(40, 80)):
        c, k, s = "c%d" % n, "k%d" % n, "s%d" % n
        pick = rng.choice(["code", "line", "block", "string", "mixed", "special"])

        # Plain code and plain comments, with quotes and openers inside comments.
        if pick == "code":
            out.append("%s = %s + 1" % (c, c))
            code_ids.add(c)
        elif pick == "line" or (pick == "block" and not block):
            out.append("%s %s it's \"quoted\" /* not a block" % (line_c, k))
            comment_ids.add(k)
        elif pick == "block":
            out.append("%s %s\n * \"%s\" spans 'lines'\n %s %s = 1" % (block[0], k, k, block[1], c))
            comment_ids.add(k)
            code_ids.add(c)

        # Literals hiding comment openers, alone or followed by a real comment.
        elif pick == "string":
            out.append('%s = "%s %s %s /* %s"' % (c, s, line_c, s, s))
            code_ids.add(c)
            hidden.add(s)
        elif pick == "mixed":
            out.append('%s = f("%s %s %s") %s %s' % (c, s, line_c, s, line_c, k))
            code_ids.add(c)
            hidden.add(s)
            comment_ids.add(k)

        # Per-language traps (SPECIALS).
        else:
            template, hides, comments = SPECIALS[family]
            out.append(template.format(c=c, k=k, s=s))
            code_ids.add(c)
            if hides:
                hidden.add(s)
            if comments:
                comment_ids.add(k)
    return "\n".join(out) + "\n", code_ids, comment_ids, hidden


class LexerTest(unittest.TestCase):
    def test_scan_against_generated_oracle(self):
        rng = random.Random(SEED)
        for family in SYNTAX:
            for _ in range(20):
                src, code_ids, comment_ids, hidden = generate(rng, family)
                lines = cl.scan(src, family)
                ids = cl.code_identifiers(lines)
                comments = " ".join(line.comment or "" for line in lines)
                with self.subTest(family=family, seed=SEED):
                    self.assertEqual(len(lines), src.count("\n") + 1)
                    self.assertLessEqual(code_ids, ids)
                    self.assertFalse(ids & (comment_ids | hidden))
                    self.assertEqual(set(re.findall(r"\bk\d+\b", comments)), comment_ids)
                    self.assertFalse(set(re.findall(r"\b[cs]\d+\b", comments)))


# Teacher diagrams drawn with [field:size] cells, each with the findings it
# must produce as (line, kind): renamed, swapped and resized cells, a sized
# header over raxNode-style bitfields, repeated strips, a free-form diagram
# that must never be checked, and the Rust, Go, Zig and ctypes forms.
SDS = """/* sdshdr8 layout, header right before the string:
 *   %s
 */
struct __attribute__ ((__packed__)) sdshdr8 {
    uint8_t len;
    uint8_t alloc;
    unsigned char flags;
    char buf[];
};
"""
RAX = """typedef struct raxNode {
    uint32_t iskey:1;
    uint32_t isnull:1;
    uint32_t iscompr:1;
    uint32_t size:%d;
    /* [header:32b][data:size bytes] */
    unsigned char data[];
} raxNode;
"""
DIAGRAM_FIXTURES = {
    "sds_ok.c": (SDS % "[len:1B][alloc:1B][flags:1B][buf: len bytes]", []),
    "sds_renamed.c": (SDS % "[len:1B][free:1B][flags:1B][buf: len bytes]", [(2, "DIAGRAM-FIELD")]),
    "sds_swapped.c": (SDS % "[alloc:1B][len:1B][flags:1B][buf: len bytes]", [(2, "DIAGRAM-ORDER")]),
    "sds_resized.c": (SDS % "[len:4B][alloc:1B][flags:1B][buf: len bytes]", [(2, "DIAGRAM-SIZE")]),
    "sds_gap.c": (SDS.replace("    uint8_t alloc;\n", "    uint8_t alloc;\n    uint8_t extra;\n")
                  % "[len:1B][alloc:1B][flags:1B][buf: len bytes]", [(2, "DIAGRAM-FIELD")]),
    "sds_strips.c": (SDS.replace(" *   %s\n", " *   [len:1B][alloc:1B]\n *   [len:1B][alloc:1B][flags:1B]\n"), []),
    "rax_ok.c": (RAX % 29, []),
    "rax_bad.c": (RAX % 28, [(6, "DIAGRAM-SIZE")]),
    "rax_freeform.c": (RAX.replace("[header:32b][data:size bytes]", "[header iscompr=0][abc][a-ptr](value-ptr?)") % 29,
                       []),
    "hdr_ok.rs": ("/// [len:1B][kind:1B][crc:4B]\n#[repr(C)]\nstruct Hdr {\n    len: u8,\n    kind: u8,\n"
                  "    crc: u32,\n}\n", []),
    "hdr_bad.rs": ("/// [len:1B][kind:1B][crc:4B]\n#[repr(C)]\nstruct Hdr {\n    len: u8,\n    kind: u8,\n"
                   "    crc: u16,\n}\n", [(1, "DIAGRAM-SIZE")]),
    "hdr_ok.go": ("// [Len:1B][Flags:1B][Crc:4B]\ntype Hdr struct {\n\tLen   uint8\n\tFlags uint8\n\tCrc   uint32\n}\n",
                  []),
    "node_bad.zig": ("// [iskey:1b][size:30b]\nconst Node = packed struct {\n    iskey: u1,\n    size: u31,\n};\n",
                     [(1, "DIAGRAM-SIZE")]),
    "hdr_bad.py": ('class Hdr(ctypes.Structure):\n    """Layout: [len:1B][flags:4][rest:5]"""\n    _fields_ = [("len", '
                   'ctypes.c_uint8), ("flags", ctypes.c_uint8, 3), ("rest", ctypes.c_uint8, 5)]\n',
                   [(2, "DIAGRAM-SIZE")]),
}
# Physical layouts with and without a size/offset assertion, and a logical
# diagram that must never demand one.
LAYOUT_FIXTURES = {
    "packed.c": SDS % "[len:1B][alloc:1B][flags:1B][buf: len bytes]",
    "packed_ok.c": SDS.replace("sdshdr8", "sdshdr16") % "[len:1B][alloc:1B][flags:1B][buf]"
                   + '_Static_assert(sizeof(struct sdshdr16) == 3, "matches the diagram");\n',
    "tree.c": "/* Tree: (f) -> (o) -> [t b] */\nstruct node {\n    struct node *child;\n    int key;\n};\n",
    "cells.c": "/* [len:1B][kind:1B] */\nstruct frame {\n    uint8_t len;\n    uint8_t kind;\n};\n",
    "split.h": "struct __attribute__((packed)) split {\n    uint8_t a;\n    uint8_t b;\n};\n",
    "tests/test_split.c": '#include "split.h"\nvoid t(void) { assert(sizeof(struct split) == 2); }\n',
    "repr.rs": "#[repr(C)]\nstruct Hdr {\n    len: u8,\n}\n",
    "repr_ok.rs": "#[repr(C)]\nstruct Frame {\n    len: u8,\n    kind: u8,\n}\n"
                  "const _: () = assert!(std::mem::size_of::<Frame>() == 2);\n",
    "hdr.py": 'class Hdr(ctypes.Structure):\n    _fields_ = [("len", ctypes.c_uint8)]\n',
    "hdr_ok.py": 'class Pkt(ctypes.Structure):\n    _fields_ = [("len", ctypes.c_uint8)]\n\n\n'
                 'assert ctypes.sizeof(Pkt) == 1\n',
}
# The finding each mutation of a tests/fixtures file must produce (swap the
# first two named cells of its first strip, rename the first one, grow the
# first one's size): exact structs catch all three; an inexact one (a macro
# field, an #ifdef) cannot tell a renamed cell from a field it did not read;
# languages without a memory layout are never checked.
_ALL = ("DIAGRAM-ORDER", "DIAGRAM-FIELD", "DIAGRAM-SIZE")
FIXTURE_MUTATIONS = {
    "c-packed-bitfields.c": _ALL, "c-macros-ifdef.c": ("DIAGRAM-ORDER", None, "DIAGRAM-SIZE"),
    "cpp-members.cpp": _ALL, "csharp-layout.cs": _ALL, "rust-repr-lifetimes.rs": _ALL, "go-struct-tags.go": _ALL,
    "zig-packed.zig": _ALL, "python-ctypes.py": _ALL, "python-dataclass.py": (None, None, None),
    "typescript-interface.ts": (None, None, None), "literals.py": (None, None, None),
    "antirez-style.c": (None, None, None),
}
# Review signals a fixture is expected to raise; every other fixture raises none.
# antirez-style.c line 33 explains a return in words the code lacks ("Return
# failure on truncated path info."): 60% overlap, a signal, never a block.
FIXTURE_SIGNALS = {"antirez-style.c": [("33", "TRIVIAL")]}


def mutate_strip(text, how):
    """Return `text` with its first strip of >= 2 named cells mutated, or None when it has none."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        cells = [m for m in cl.CELL.finditer(line) if not cl.SYNTHETIC.match(m.group("name"))]
        if len(cells) >= 2:
            break
    else:
        return None
    a, b = cells[0], cells[1]
    if how == "swap":
        line = line[:a.start()] + b.group() + line[a.end():b.start()] + a.group() + line[b.end():]
    elif how == "rename":
        line = line[:a.start("name")] + "ghost" + line[a.end("name"):]
    else:
        num = re.match(r"\d+", a.group("size").strip()).group()
        line = line[:a.start("size")] + a.group("size").replace(num, str(int(num) + 1), 1) + line[a.end("size"):]
    lines[i] = line
    return "\n".join(lines)


class CommentSyncTest(unittest.TestCase):
    def test_trivial_spares_a_value_the_code_does_not_show(self):
        # Every word of the comment is in the code, but the number is not.
        self.assertIsNone(sync.trivial("no_value = 1", "dict *dv = dictCreate(&BenchmarkDictTypeNoValue);"))
        self.assertIsNotNone(sync.trivial("count = 1", "count = 1;"))

    def setUp(self):
        self.repo = Repo()

    def tearDown(self):
        self.repo.close()

    def check_diff(self):
        return script("craft-comment-sync.py", "--check-diff", cwd=self.repo.path)

    def gate_tiers(self, *rels):
        """Run the gate checks of this script on `rels` as one session; return ({hard kinds}, {soft kinds}) by file."""
        changes = gatekeeper.session_changes(self.repo.path, {})
        self.assertEqual(sorted(ch.rel for ch in changes), sorted(rels))
        hard, soft = sync.gate_findings(self.repo.path, changes)
        return [m for _, m in hard], [m for _, m in soft]

    def test_orphan_follows_a_rename_into_files_the_session_never_touched(self):
        # The comment that went stale is in net.c, which the rename never edits.
        self.repo.write("codec.c", "/* codec.c -- frame decoding. */\nint parse_frame(const char *p) { return p[0]; }\n")
        self.repo.write("net.c", "/* Calls parse_frame() once per packet */\n"
                                 "int net_read(const char *p, int (*decode)(const char *)) { return decode(p); }\n")
        self.repo.write("DESIGN.md", "Frames go through `parse_frame` first.\n")
        self.repo.write("CHANGELOG.md", "- parse_frame() added.\n")
        self.repo.commit()
        self.repo.write("codec.c", "/* codec.c -- frame decoding. */\nint decode_frame(const char *p) { return p[0]; }\n")
        hard, soft = self.gate_tiers("codec.c")
        self.assertEqual(hard, ["net.c:1: [ORPHAN] 'parse_frame' is gone from the code but this comment still names it"])
        self.assertEqual(soft, ["DESIGN.md:1: [ORPHAN] 'parse_frame' is gone from the code: is what this document "
                                "says about it still true?"])
        code, out, _ = self.check_diff()
        self.assertEqual((code, re.findall(r"^(\S+?):\d+: \[ORPHAN\]", out, re.M)), (1, ["net.c", "DESIGN.md"]))

    def test_orphan_ignores_a_symbol_moved_to_another_file(self):
        self.repo.write("io.c", "/* io.c -- low-level I/O helpers. */\nint read_all(int fd) { return fd; }\n"
                                "int io_close(int fd) { return fd; }\n")
        self.repo.write("file.c", "/* file.c -- file helpers. */\nint file_open(int fd) { return fd; }\n")
        self.repo.commit()
        # Moved within the session, or into a file the session never touched: alive either way.
        self.repo.write("io.c", "/* io.c -- low-level I/O helpers. */\n/* read_all() moved to file.c */\n"
                                "int io_close(int fd) { return fd; }\n")
        self.repo.write("file.c", "/* file.c -- file helpers. */\nint file_open(int fd) { return fd; }\n"
                                  "int read_all(int fd) { return fd; }\n")
        self.assertEqual(self.gate_tiers("file.c", "io.c"), ([], []))
        self.repo.commit()
        self.repo.write("file.c", "/* file.c -- file helpers. */\nint file_open(int fd) { return fd; }\n"
                                  "/* read_all() lives in io.c again. */\n")
        self.repo.write("io.c", "/* io.c -- low-level I/O helpers. */\nint read_all(int fd) { return fd; }\n"
                                "int io_close(int fd) { return fd; }\n")
        self.repo.commit()
        self.repo.write("lib.c", "int helper(int fd) { return read_all(fd); }\n")
        self.repo.commit()
        self.repo.write("io.c", "/* io.c -- low-level I/O helpers. */\nint io_close(int fd) { return fd; }\n")
        hard, _ = self.gate_tiers("io.c")
        self.assertEqual(hard, [], "read_all is still called in lib.c: alive")
        self.assertEqual(self.check_diff()[0], 0)

    def test_orphan_after_rename(self):
        self.repo.write("rax.c", """
            /* raxLowWalk() stops at the first mismatch; see raxSeekGreatest too. */
            static int raxLowWalk(int x) { return x; }
            int raxSeekGreatest(int y) { return raxLowWalk(y); }
            """)
        self.repo.commit()
        self.repo.write("rax.c", """
            /* raxLowWalk() stops at the first mismatch; see raxSeekGreatest too. */
            static int raxWalk(int x) { return x; }
            int raxSeekGreatest(int y) { return raxWalk(y); }
            """)
        code, out, _ = self.check_diff()
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(),
                         ["rax.c:1: [ORPHAN] 'raxLowWalk' is gone from the code but this comment still names it"])

    def test_backup_and_trivial_judge_only_new_lines(self):
        legacy = """
            int legacy(int count) {
                // count++;
                return count;
            }
            """
        self.repo.write("m.c", legacy)
        self.repo.commit()
        self.repo.write("m.c", legacy + """
            int other(int count, int *node, struct client *c) {
                // free(node);

                // increment count
                count++;

                count++; /* increment count */
                /* 1: Save next pointer. */
                int next = node[1];
                // Why: count before free, the callback reads it.
                count++;
                // Free the query buffer.
                free(c->query_buf);
                free(c->pending_query_buf);
                c->query_buf = NULL;

                // Free the query buffer.
                free(c->query_buf);
                /* Return 0 on OOM. */
                return 0;
            }
            """)
        code, out, _ = self.check_diff()
        self.assertEqual(code, 1, out)
        # Lines 1-4 are the committed legacy code (its "// count++;" is not this
        # change's business), line 5 is blank, the new function starts at 6.
        # Regression (v0.0.1): line 17 titles three statements, a Guide comment,
        # but was compared with its first statement alone and reported Trivial;
        # the same words over the single statement at line 23 are Trivial.
        self.assertEqual(re.findall(r"^m\.c:(\d+): \[([A-Z]+)\]", out, re.M),
                         [("7", "BACKUP"), ("9", "TRIVIAL"), ("12", "TRIVIAL"), ("22", "TRIVIAL")])

    def test_backup_needs_a_code_majority_in_a_block(self):
        self.repo.write("b.py", '''
            # Markers carry their exit condition:
            # ceiling = at least one digit
            # trigger = a semicolon or one of the trigger words
            # so the ledger can say when to revisit them.
            X = 1

            # old implementation:
            # total = compute(x)
            # emit(total)
            Y = 2
            ''')
        code, out, _ = self.check_diff()
        self.assertEqual(re.findall(r"^b\.py:(\d+): \[([A-Z]+)\]", out, re.M), [("8", "BACKUP"), ("9", "BACKUP")])
        self.assertEqual(code, 1)

    def test_python_docstring_orphan_and_backup(self):
        self.repo.write("tool.py", '''
            def _old_helper(x):
                return x * 2


            def run(x):
                """Double x through _old_helper and log it."""
                return _old_helper(x)
            ''')
        self.repo.commit()
        self.repo.write("tool.py", '''
            def _double(x):
                return x * 2


            def run(x):
                """Double x through _old_helper and log it."""
                # print(x)
                return _double(x)
            ''')
        code, out, _ = self.check_diff()
        self.assertEqual(code, 1)
        self.assertEqual(re.findall(r"^tool\.py:(\d+): \[([A-Z]+)\]", out, re.M), [("6", "ORPHAN"), ("7", "BACKUP")])

    def test_hook_leaves_cross_edit_checks_to_stop(self):
        # A rename half-way through: the comment still names parse_header and the
        # diagram still names len. Those may be the next edits of the same change,
        # so PostToolUse reports only what this edit decided alone (the Backup).
        path = self.repo.write("codec.c", """
            /* parse_header() reads the 5-byte frame header. */
            int read_header(int x) { return x; }
            // free(buf);
            /* [len:1B][kind:1B] */
            struct frame {
                uint8_t size;
                uint8_t kind;
            };
            """)
        data = edit_payload(path, edits=[
            {"old_string": "int parse_header(int x) { return x; }",
             "new_string": "int read_header(int x) { return x; }\n// free(buf);"},
            {"old_string": "    uint8_t len;", "new_string": "    uint8_t size;"}])
        code, out, err = script("craft-comment-sync.py", "--hook", stdin=data)
        self.assertEqual((code, out), (2, ""), err)
        self.assertIn("codec.c:3: [BACKUP] commented-out code", err)
        self.assertNotIn("ORPHAN", err)
        self.assertNotIn("DIAGRAM", err)

    def test_vague_comments(self):
        self.repo.write("v.c", """
            int f(int x) {
                // Process the data
                x += 1;
                x += 2;
                x += 3;
                x += 4;
                // Handle edge cases
                x += 5;
                // 1: Process data
                x += 6;
                // Retry because the socket may be reset
                x += 7;
                // Handle EAGAIN
                x += 8;
                return x; // update state if needed
            }
            """)
        code, out, _ = self.check_diff()
        self.assertEqual(re.findall(r"^v\.c:(\d+): \[([A-Z]+)\]", out, re.M),
                         [("2", "VAGUE"), ("7", "VAGUE"), ("15", "VAGUE")])
        self.assertEqual(code, 1)
        # Vagueness is a judgment, never proof: a review signal at Stop, and
        # PostToolUse, which reports only hard findings, stays silent.
        hard, soft = self.gate_tiers("v.c")
        self.assertEqual((hard, [m.split(": [")[1][:5] for m in soft]), ([], ["VAGUE"] * 3))
        path = os.path.join(self.repo.path, "v.c")
        self.assertEqual(script("craft-comment-sync.py", "--hook", stdin=edit_payload(path, new_string="x"))[0], 0)

    def test_operators_are_specific(self):
        # rax.c titles its comparison cases "Handle ==" and "Handle >, >=, <,
        # <=": an operator names the case exactly, so neither is Vague.
        text = ("int cmp(int lt, int gt, int c) {\n    /* Handle == */\n    if (lt == 0 && gt == 0) return c == 0;\n\n"
                "    /* Handle >, >=, <, <= */\n    if (c == 0) return 1;\n    return lt;\n}\n")
        self.assertEqual(sync.comment_findings(cl.scan(text, "c"), "c", set(range(1, 9)), "cmp.c"), ([], []))

    def test_trivial_tiers_against_generated_oracle(self):
        # Oracle: TRIVIAL is hard when >= 80% of the comment's content words
        # (at most 6) appear in the one statement it describes and at most
        # one is missing; a review signal from 60%; nothing below. A comment
        # on a declaration (list element, macro, field, enum member,
        # prototype) documents a name, never restates a step: never TRIVIAL.
        rng = random.Random(SEED)
        vocab = "alpha bravo delta kilo lima metro oscar quebec romeo sierra tango victor yankee zulu".split()
        decls = [("static int table[] = {{\n    {a}_{b},{tail}\n    0\n}};\n", 2),
                 ("#define {A}_{B} 16{tail}\n", 1),
                 ("struct rec {{\n    uint32_t {a}_{b};{tail}\n}};\n", 2),
                 ("enum mode {{\n    {A}_{B} = 4,{tail}\n    {C}_{D}\n}};\n", 2),
                 ("enum mode {{\n    {A}_{B} = 4,\n    {C}_{D}{tail}\n}};\n", 3),
                 ("{head}int {a}_{b}(int {c});\n", 1)]
        seen_tiers = collections.Counter()
        for trial in range(300):
            a, b, c, d, *others = rng.sample(vocab, len(vocab))
            n_in, n_out = rng.randint(1, 4), rng.randint(0, 2)
            said = rng.sample([a, b, c, d], n_in) + others[:n_out]
            rng.shuffle(said)
            comment = "/* %s. */" % " ".join(said).capitalize()
            names = dict(a=a, b=b, c=c, d=d, A=a.upper(), B=b.upper(), C=c.upper(), D=d.upper())
            if rng.random() < 0.6:
                trailing = rng.random() < 0.5
                stmt = "    %s_%s = %s_%s + 1;" % (a, b, c, d)
                body = stmt + " " + comment if trailing else "    " + comment + "\n" + stmt
                text, no = "void g(void) {\n" + body + "\n}\n", 2
                ratio = n_in / (n_in + n_out)
                want = "hard" if ratio >= 0.8 and n_out <= 1 else "soft" if ratio >= 0.6 else None
            else:
                template, no = rng.choice(decls)
                head = comment + "\n" if "{head}" in template else ""
                text = template.format(tail=" " + comment if not head else "", head=head, **names)
                no = 1 if head else no
                want = None
            hard, soft = sync.comment_findings(cl.scan(text, "c"), "c", set(range(1, text.count("\n") + 1)), "g.c")
            got = [("hard", m) for _, m in hard] + [("soft", m) for _, m in soft]
            with self.subTest(trial=trial, text=text, seed=SEED):
                self.assertEqual([(tier, m.split(": [")[1].split("]")[0]) for tier, m in got],
                                 [(want, "TRIVIAL")] if want else [])
                if want:
                    self.assertTrue(got[0][1].startswith("g.c:%d: " % no), got)
            seen_tiers[want] += 1
        self.assertTrue(all(seen_tiers[t] for t in ("hard", "soft", None)), seen_tiers)

    def test_diagram_cells_against_their_struct(self):
        for name, (text, expected) in DIAGRAM_FIXTURES.items():
            self.repo.write(name, text)
        code, out, _ = self.check_diff()
        found = sorted(re.findall(r"^(\S+?):(\d+): \[(DIAGRAM-[A-Z]+)\]", out, re.M))
        wanted = sorted((name, str(line), kind) for name, (_, hits) in DIAGRAM_FIXTURES.items() for line, kind in hits)
        self.assertEqual(found, wanted)

    def test_diagram_cells_against_generated_oracle(self):
        rng = random.Random(SEED)
        types = [("uint8_t", 8), ("uint16_t", 16), ("uint32_t", 32), ("uint64_t", 64), ("char", 8)]
        for trial in range(80):
            fields = [("f%d_%d" % (trial, i),) + rng.choice(types) for i in range(rng.randint(2, 6))]
            cells = ["[%s:%dB]" % (fname, bits // 8) for fname, _, bits in fields]
            mutation, i = rng.choice(["none", "rename", "swap", "resize", "insert"]), rng.randrange(len(fields) - 1)
            if mutation == "rename":
                cells[i] = "[ghost%d:%s" % (i, cells[i].split(":", 1)[1])
            elif mutation == "swap":
                cells[i], cells[i + 1] = cells[i + 1], cells[i]
            elif mutation == "resize":
                cells[i] = "[%s:%dB]" % (fields[i][0], fields[i][2] // 4)
            elif mutation == "insert":
                fields.insert(i + 1, ("added%d_%d" % (trial, i), "uint8_t", 8))
            text = "/* Layout:\n *   %s\n */\nstruct s%d {\n%s};\n" % (
                "".join(cells), trial, "".join("    %s %s;\n" % (t, fname) for fname, t, _ in fields))
            found = {key.split("|")[2]
                     for key, _ in sync.diagram_findings(cl.scan(text, "c"), "c", set(range(1, 99)), "x.c")}
            expected = {"none": set(), "rename": {"DIAGRAM-FIELD"}, "swap": {"DIAGRAM-ORDER"},
                        "resize": {"DIAGRAM-SIZE"}, "insert": {"DIAGRAM-FIELD"}}[mutation]
            self.assertEqual(found, expected, "seed %d trial %d: %s" % (SEED, trial, text))

    def test_polyglot_fixtures_are_clean_and_their_mutations_are_caught(self):
        # Hipp's rule applied to the checker itself: every fixture is clean as
        # written, and each derived mutation must produce exactly its expected
        # finding, or none where the parser cannot be certain.
        self.assertEqual(sorted(os.listdir(FIXTURES)), sorted(FIXTURE_MUTATIONS))
        for name, expected in sorted(FIXTURE_MUTATIONS.items()):
            text, family = read("tests/fixtures/" + name), cl.family_of(name)
            everything = set(range(1, text.count("\n") + 2))
            with self.subTest(fixture=name):
                hard, soft = sync.audit(name, family, text, everything)
                self.assertEqual(hard, [])
                self.assertEqual([tuple(re.match(r"[^:]+:(\d+): \[([A-Z-]+)\]", m).groups()) for _, m in soft],
                                 FIXTURE_SIGNALS.get(name, []))
                self.assertEqual(cl.debt_markers(name, text), [])
                for how, kind in zip(("swap", "rename", "resize"), expected):
                    mutated = mutate_strip(text, how)
                    found = {key.split("|")[2] for key, _ in
                             sync.diagram_findings(cl.scan(mutated, family), family, everything, name)} if mutated else set()
                    self.assertEqual(found, {kind} if kind else set(), how)

    def test_quiet_outside_scope(self):
        path = self.repo.write("notes.md", "// free(buf);\n")
        self.assertEqual(script("craft-comment-sync.py", "--hook", stdin=edit_payload(path, new_string="x"))[0], 0)
        plain = Repo(git=False)
        try:
            plain.write("a.c", "// free(buf);\n")
            self.assertEqual(script("craft-comment-sync.py", "--check-diff", cwd=plain.path), (0, "", ""))
        finally:
            plain.close()


# Source fixtures for the function extractor and the (name, public) pairs it
# must find: one file per language, each with the constructs that fool naive
# brace or regex parsers.
EXTRACTOR_FIXTURES = {
            "srv.go": ("""
                package srv

                // Handle serves one request.
                func (s *Server) Handle(w int) error {
                \tgo func() {
                \t\ts.n++
                \t}()
                \treturn helper(w)
                }

                func helper(w int) error {
                \tif w > 0 {
                \t\treturn nil
                \t}
                \treturn nil
                }
                """, {("Handle", True), ("helper", False)}),
            "store.rs": ("""
                pub struct Store { n: u8 }

                impl Store {
                    pub fn get(&self) -> u8 { self.n }
                    fn bump<'a>(&mut self, tag: &'a str) -> &'a str { self.n += 1; tag } // lifetimes
                }

                impl Drop for Store {
                    fn drop(&mut self) {}
                }

                fn private_helper(x: u8) -> u8 {
                    let f = |y: u8| { y + x };
                    f(1)
                }
                """, {("get", True), ("bump", False), ("drop", True), ("private_helper", False)}),
            "widget.ts": ("""
                export function api(x: number): string {
                  return String(x);
                }

                const local = (a: number): number => {
                  return a + 1;
                };

                class Widget {
                  private hidden(): void {}
                  public shown(): number { return 1; }
                  async load(id: string): Promise<void> {
                    if (id) {
                      describe("x", () => {
                        return;
                      });
                    }
                  }
                }
                """, {("api", True), ("local", False), ("hidden", False), ("shown", True), ("load", True)}),
            "Account.java": ("""
                public class Account {
                    public int balance() { return 1; }
                    private void audit(int x) { }
                    void touch() {
                        Runnable r = new Runnable() {
                            public void run() { }
                        };
                    }
                }
                """, {("balance", True), ("audit", False), ("touch", False), ("run", False)}),
            "tool.py": ("""
                import functools


                def public_api(x):
                    return _helper(x)


                @functools.lru_cache(maxsize=None)
                def _helper(x):
                    def inner(y):
                        return y
                    return inner(x)


                class Box:
                    def __init__(self):
                        self.v = 1

                    def get(self):
                        return self.v
                """, {("public_api", True), ("_helper", False), ("inner", False), ("__init__", False),
                      ("get", True)}),
            "main.zig": ("""
                const std = @import("std");

                pub fn main() !void {
                    std.debug.print("hi\\n", .{});
                }

                fn add(a: i32, b: i32) i32 {
                    return a + b;
                }
                """, {("main", True), ("add", False)}),
            "node.c": ("""
                #include <stdio.h>
                #define MAX(a, b) ((a) > (b) ? (a) : (b))

                static int
                helper (int x)
                {
                    return MAX(x, 0);
                }

                __attribute__((unused)) static void quiet(void) {
                }

                struct node *new_node(int v) {
                    struct node *n = 0;
                    for (int i = 0; i < v; i++) {
                        if (i) { n = 0; }
                    }
                    return n;
                }

                int main(void) {
                    return helper(1);
                }
                """, {("helper", False), ("quiet", False), ("new_node", True), ("main", True)}),
}
# Languages added with the multi-language extractor: one fixture each, the
# trap of each language noted in the source itself.
EXTRACTOR_FIXTURES.update({
    "lib.rb": ("""
        module Store
          class Cache
            def get(key)
              @data[key]
            end

            def _evict
              @data.clear
            end

            private def secret
              1
            end
          end
        end

        def helper(x)
          [1, 2].each do |i|
            puts i
          end
          x
        end
        """, {("get", True), ("_evict", False), ("secret", False), ("helper", True)}),
    "cart.ex": ("""
        defmodule Shop.Cart do
          @moduledoc "Cart operations."

          @doc "Adds an item."
          def add(cart, item) do
            [item | cart]
          end

          defp total(cart) do
            Enum.sum(cart)
          end
        end
        """, {("add", True), ("total", False)}),
    "mod.lua": ("""
        local M = {}

        function M.open(path)
          return io.open(path)
        end

        local function parse(line)
          return line
        end

        M.close = function(f)
          f:close()
        end

        return M
        """, {("open", True), ("parse", False), ("close", True)}),
    "geo.jl": ('''
        module Geo

        """
            area(r)

        Area of a circle of radius `r`.
        """
        function area(r)
            return pi * r^2
        end

        function _scale(x)
            x * 2
        end

        end
        ''', {("area", True), ("_scale", False)}),
    "io.nim": ("""
        proc open*(path: string): File =
          result = open(path)

        proc parse(line: string): int =
          result = line.len
        """, {("open", True), ("parse", False)}),
    "Repo.kt": ("""
        class Repo(val db: Db) {
            fun find(id: Int): User? {
                return db.get(id)
            }

            private fun cache(u: User) {
                when (u.kind) {
                    else -> {}
                }
            }
        }

        internal fun helper(x: Int): Int {
            return x
        }
        """, {("find", True), ("cache", False), ("helper", False)}),
    "Point.swift": ("""
        public struct Point {
            public func norm() -> Double {
                return 0
            }
            func scaled(by k: Double) -> Point {
                guard k > 0 else { return self }
                return self
            }
        }
        """, {("norm", True), ("scaled", False)}),
    "Service.cs": ("""
        namespace App {
            public class Service {
                public int Run(int x) {
                    if (x > 0) { return x; }
                    return 0;
                }
                private void Log(string m) {
                }
                public int Count { get; set; }
            }
        }
        """, {("Run", True), ("Log", False)}),
    "cart.php": ("""
        <?php
        class Cart {
            public function add($item) {
                $this->items[] = $item;
            }
            private function total() {
                return array_sum($this->items);
            }
        }
        function helper($x) {
            $f = function($y) use ($x) { return $y; };
            return $f($x);
        }
        """, {("add", True), ("total", False), ("helper", True)}),
    "tool.sh": ("""
        #!/bin/sh
        # Print a greeting.
        greet() {
          echo "hi $1"
        }

        function cleanup {
          rm -f "$TMP"
        }
        """, {("greet", True), ("cleanup", True)}),
    "Tool.pm": ("""
        package Tool;
        sub run {
            my ($x) = @_;
            return _helper($x);
        }
        sub _helper {
            return $_[0];
        }
        1;
        """, {("run", True), ("_helper", False)}),
    "stats.r": ("""
        summarize <- function(df) {
          if (nrow(df) == 0) {
            return(NULL)
          }
          df
        }
        .internal = function(x) {
          x
        }
        """, {("summarize", True), (".internal", False)}),
    "thing.ps1": ("""
        function Get-Thing {
            param($Name)
            Write-Output $Name
        }
        """, {("Get-Thing", True)}),
    "core.clj": ("""
        (ns app.core)

        (defn handler
          "Handle one request."
          [req]
          (let [x 1]
            (str req x)))

        (defn- secret [x]
          (* x 2))
        """, {("handler", True), ("secret", False)}),
    "math.scm": ("""
        (define (square x)
          (* x x))

        (define pi 3.14)
        """, {("square", True)}),
    "math.ml": ("""
        let square x =
          x * x

        let pi = 3.14

        let rec fact n =
          if n = 0 then 1 else n * fact (n - 1)
        """, {("square", True), ("fact", True)}),
    "Lib.fs": ("""
        module Lib

        let private helper x =
            x + 1

        let api (x: int) =
            helper x
        """, {("helper", False), ("api", True)}),
    "box.dart": ("""
        class Box {
          int value() {
            return 1;
          }
          void _reset() {
          }
        }
        int main() {
          return Box().value();
        }
        """, {("value", True), ("_reset", False), ("main", True)}),
    "App.scala": ("""
        object App {
          def run(args: Array[String]): Unit = {
            println(args.length)
          }
          private def helper(x: Int): Int = {
            x
          }
        }
        """, {("run", True), ("helper", False)}),
    "cart.coffee": ("""
        class Cart
          add: (item) ->
            @items.push item

        square = (x) -> x * x
        """, {("add", True), ("square", True)}),
})


class GateTest(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()
        self.sessions = []

    def tearDown(self):
        self.repo.close()
        for session in self.sessions:
            for kind in ("snap", "gate"):
                state = gatekeeper._state_path(kind, session)
                if os.path.exists(state):
                    os.remove(state)

    def hook(self, *args, session=None, active=False, cwd=None):
        """Run craft-gate.py with a hook payload; a fresh session unless one is given."""
        session = session or str(uuid.uuid4())
        self.sessions.append(session)
        event = {"--snapshot": "SessionStart", "--prompt": "UserPromptSubmit"}.get(args[0] if args else "", "Stop")
        data = json.dumps({"hook_event_name": event, "session_id": session, "stop_hook_active": active,
                           "cwd": cwd or self.repo.path, "prompt": "go on"})
        return script("craft-gate.py", *args, cwd=cwd or self.repo.path, stdin=data)

    def gate(self, session=None, active=False, cwd=None):
        return self.hook(session=session, active=active, cwd=cwd)

    def signals(self, session=None):
        """Run a Stop that must only signal: return the (file, kind) pairs of its systemMessage."""
        code, out, err = self.gate(session)
        self.assertEqual((code, err), (0, ""), out)
        return sorted(re.findall(r"([\w./-]+):\d+: \[([A-Z-]+)\]", json.loads(out)["systemMessage"]))

    def test_inexact_files_turn_hard_findings_into_signals(self):
        # One generated file per construct scan() cannot read exactly: the
        # file is reported inexact, and a bare marker written after the
        # construct is a review signal that names the reason, never a block.
        # Without the construct, the same marker blocks as before.
        rng = random.Random(SEED)

        def word():
            return "".join(rng.choice("abcdefgh") for _ in range(rng.randint(3, 7)))

        tag = rng.choice(["EOF", "END", "TXT"]) + str(rng.randint(0, 99))
        delim = rng.choice(["", "x", "sql", "json"])
        quoted = rng.choice(["%s", "'%s'", '"%s"'])
        cases = {  # file: (construct, line comment template)
            "nested.rs": ("/* %s /* %s */ %s */\nfn main() {}\n" % (word(), word(), word()), "// %s"),
            "nested.swift": ("/* %s /* %s */ %s */\nlet x = 1\n" % (word(), word(), word()), "// %s"),
            "nested.kt": ("/* %s /* %s */ %s */\nval x = 1\n" % (word(), word(), word()), "// %s"),
            "nested.hs": ("{- %s {- %s -} %s -}\nmain = pure ()\n" % (word(), word(), word()), "-- %s"),
            "nested.ml": ("(* %s (* %s *) %s *)\nlet () = ()\n" % (word(), word(), word()), "(* %s *)"),
            "raw.cpp": ('const char *s = R"%s(a "%s" \\ b)%s";\n' % (delim, word(), delim), "// %s"),
            "raw.swift": ('let s = #"a "%s" b"#\n' % word(), "// %s"),
            "here.sh": ("cat <<%s\n# %s\n%s\n" % (quoted % tag, word(), tag), "# %s"),
            "here.rb": ("s = <<~%s\n  # %s\n%s\n" % (tag, word(), tag), "# %s"),
            "here.pl": ('print <<"%s";\n# %s\n%s\n' % (tag, word(), tag), "# %s"),
            "here.php": ("<?php\n$s = <<<%s\n# %s\n%s;\n" % (tag, word(), tag), "// %s"),
        }
        # Write each construct with a bare marker after it, and the marker alone in plain_<name>.
        self.repo.write("README", "fixture\n")
        self.repo.commit()
        markers, paths = {}, {}
        for name, (construct, comment) in cases.items():
            markers[name] = comment % ("%s %s" % (TODO, word())) + "\n"
            paths[name] = self.repo.write(name, construct + markers[name])
            paths["plain_" + name] = self.repo.write("plain_" + name, markers[name])
        changes = gatekeeper.session_changes(self.repo.path, {})
        hard, soft = gatekeeper.collect(self.repo.path, changes)
        debt_scan = gatekeeper._load("craft-debt-scan")

        def written(name):  # PostToolUse payload of a Write of the whole file
            return {"tool_name": "Write", "tool_input": {"file_path": paths[name], "content": "x"}}

        # Judge both files of each pair at Stop and after a Write.
        for name, (construct, _) in cases.items():
            with self.subTest(name=name, seed=SEED):
                reason = cl.inexact_reason(name, construct)
                self.assertTrue(reason)
                self.assertIsNone(cl.inexact_reason("plain_" + name, markers[name]))
                self.assertEqual([m for k, m in hard if k.split("|")[1] == name], [])
                signals = [m for k, m in soft if k.split("|")[1] == name and "[%s]" % TODO in m]
                self.assertEqual(len(signals), 1, soft)
                self.assertIn("inexact file: %s" % reason, signals[0])
                self.assertEqual(len([k for k, _ in hard if k.split("|")[1] == "plain_" + name]), 1, hard)
                self.assertEqual((debt_scan.from_hook(written(name)), sync.from_hook(written(name))), ([], []))
                self.assertTrue(debt_scan.from_hook(written("plain_" + name)))

    def test_review_signals_are_shown_once_per_session(self):
        self.repo.write("core.c", "int base(int x) { return x; }\n")
        self.repo.commit()
        self.repo.write("core.c", "int twice(int x) { return 2 * x; }\nstatic int helper(int x) { return twice(x); }\n"
                        "int base(int x) { return helper(x); }\n" + c_nested("pipeline"))
        # Signals alone never cost a turn: Claude stops, the user reads them at once.
        session = str(uuid.uuid4())
        code, out, err = self.gate(session)
        self.assertEqual((code, err), (0, ""))
        note = json.loads(out)["systemMessage"]
        self.assertIn("[RHYTHM] 'pipeline' has 70 LOC", note)
        self.assertIn("[HELPER] 'helper' only hands its parameters to 'twice' and has one caller", note)
        # Claude gets them once, with the next prompt; later Stops and prompts stay quiet.
        code, out, err = self.hook("--prompt", session=session)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Review signals, shown once", out)
        self.assertIn("[RHYTHM] 'pipeline' has 70 LOC", out)
        self.assertEqual(self.hook("--prompt", session=session), (0, "", ""))
        self.assertEqual(self.gate(session, active=True), (0, "", ""))
        self.assertEqual(self.gate(session), (0, "", ""))
        self.assertEqual(self.hook("--prompt", session=session), (0, "", ""))
        # Another session shows them again.
        self.assertIn(("core.c", "RHYTHM"), self.signals())

    def test_hard_findings_block_every_stop_then_are_released(self):
        self.repo.write("lib.c", "/* old_parse() is kept for the CLI. */\nint old_parse(int x) { return x; }\n")
        self.repo.commit()
        self.repo.write("lib.c", f"/* old_parse() is kept for the CLI. */\nint new_parse(int x) {{ return x; }}\n"
                                 f"// {TODO}: tidy up\n")
        session = str(uuid.uuid4())
        for attempt in range(gatekeeper.MAX_TRIES):
            code, out, err = self.gate(session, active=attempt > 0)
            self.assertEqual((code, out), (2, ""), err)
            self.assertIn("lib.c:1: [ORPHAN] 'old_parse'", err)
            self.assertIn("lib.c:3: [TODO]", err)
        code, out, err = self.gate(session, active=True)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("2 unresolved finding(s)", json.loads(out)["systemMessage"])
        self.assertEqual(self.gate(session), (0, "", ""))
        # A new violation is a new finding: it blocks again.
        with open(os.path.join(self.repo.path, "lib.c"), "a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"// {FIXME} breaks on empty input\n")
        code, _, err = self.gate(session)
        self.assertEqual(code, 2, err)
        self.assertIn("lib.c:4: [NO-TRIGGER]", err)
        self.assertNotIn("ORPHAN", err)

    def test_decide_caps_blocks_per_turn_and_per_finding(self):
        decide, tries = gatekeeper.decide, gatekeeper.MAX_TRIES
        # A different hard finding at every Stop of one turn: the turn cap lets Claude stop.
        state = {}
        for i in range(tries):
            block, _, release, state = decide(state, i > 0, [("k%d" % i, "m%d" % i)], [])
            self.assertEqual((block, release), ([("k%d" % i, "m%d" % i)], []))
        block, _, release, state = decide(state, True, [("k9", "m9")], [])
        self.assertEqual((block, release), ([], ["m9"]))
        # A new turn starts a new streak; the released finding still has tries left.
        block, _, release, state = decide(state, False, [("k9", "m9")], [])
        self.assertEqual((block, release), ([("k9", "m9")], []))
        # A finding that disappears forgets its tries; soft findings show once and never start a streak.
        _, show, _, state = decide(state, True, [], [("s", "signal")])
        self.assertEqual((state["tries"], show, state["streak"]), ({}, [("s", "signal")], 0))
        self.assertEqual(decide(state, True, [], [("s", "signal")])[1], [])
        # Without a session there is no state: everything is reported.
        self.assertEqual(decide({}, False, [("k", "m"), ("k", "m2")], [])[0], [("k", "m"), ("k", "m2")])

    def test_gate_sees_bash_edits_and_skips_files_dirty_before_the_session(self):
        self.repo.write("README", "fixture\n")
        self.repo.commit()
        wip = self.repo.write("wip.py", f"# {TODO}: the user's own work in progress\nX = 1\n")
        session = str(uuid.uuid4())
        self.assertEqual(self.hook("--snapshot", session=session), (0, "", ""))
        self.assertEqual(self.gate(session), (0, "", ""))
        # A generator run through Bash: no PostToolUse hook ever saw this file.
        self.repo.write("gen.py", f"# {TODO}: generated\nY = 2\n")
        code, _, err = self.gate(session)
        self.assertEqual(code, 2, err)
        self.assertIn("gen.py:1: [TODO]", err)
        self.assertNotIn("wip.py", err)
        # Once the session touches the dirty file, it is the session's business.
        with open(wip, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("Z = 3\n")
        code, _, err = self.gate(session, active=True)
        self.assertIn("wip.py:1: [TODO]", err)

    def test_one_stop_runs_every_scanner(self):
        self.repo.write("README", "fixture\n")
        self.repo.commit()
        self.repo.write("frame.c", "/* frame.c -- one length-prefixed frame header and its parser. */\n"
                                   "#include <stdint.h>\n\n/* Layout: [kind:1B][len:1B] */\nstruct frame {\n"
                                   "    uint8_t len;\n    uint8_t kind;\n};\n_Static_assert(sizeof(struct frame) == 2,"
                                   " \"two bytes\");\n// " + TODO + ": validate kind\n\n/* Parse a frame. */\n"
                                   + c_nested("frame_parse"))
        code, _, err = self.gate()
        self.assertEqual(code, 2, err)
        self.assertEqual(sorted(re.findall(r"^frame\.c:\d+: \[([A-Z-]+)\]", err, re.M)),
                         ["DIAGRAM-ORDER", "NO-TEST", "RHYTHM", "TODO"])

    def test_polyglot_fixtures_pass_the_whole_gate(self):
        self.repo.write("README", "fixture\n")
        self.repo.commit()
        for name in os.listdir(FIXTURES):
            self.repo.write("tests/fixtures/" + name, read("tests/fixtures/" + name))
        code, out, err = self.gate()
        self.assertNotIn("fix before finishing", err)
        self.assertEqual(re.findall(r"tests/fixtures/([^:]+):(\d+): \[([A-Z-]+)\]", out + err),
                         [(name, no, kind) for name, hits in sorted(FIXTURE_SIGNALS.items()) for no, kind in hits])

    def test_gate_accepts_guides_and_helpers_with_their_own_logic(self):
        self.repo.write("core.c", "int base(int x) { return x; }\n")
        self.repo.commit()
        pipeline = "/* Run the three stages on x; never fails. */\n" + c_function("pipeline", 60, guide_every=20)
        self.repo.write("core.c", c_function("helper", 5, static=True) + "int base(int x) { return helper(x); }\n"
                        + pipeline)
        self.assertEqual(self.gate(), (0, "", ""))

    def test_gate_flags_pass_through_helpers_only(self):
        # The cases of the v0.0.4 review: a pass-through with one caller is
        # flagged; a short helper with logic of its own, a helper that adds an
        # argument and a public function that adds one (raxInsert) are not.
        # Neither are a callback in a method table (redis connSocketListen)
        # nor a dispatch through a parameter (redis connWritev).
        self.repo.write("README", "fixture\n")
        self.repo.commit()
        self.repo.write("calc.c", "/* calc.c -- helpers that exercise the HELPER review signal. */\n"
                        "static int sub(int a, int b) { return a - b; }\n"
                        "static int f(int a, int b) { return sub(a, b); }\n"
                        "static int twice(int a) {\n    int t = a + a;\n    return t;\n}\n"
                        "static int ins(int *r, int s) { return gen(r, s, 1); }\n"
                        "static int on_listen(int *r) { return listen_to(r); }\n"
                        "static struct ops table = { .listen = on_listen };\n"
                        "static int write_to(conn *c, int n) { return c->type->write(c, n); }\n"
                        "/* Insert s into r, keeping the old value. */\n"
                        "int raxInsert(int *r, int s) { return raxGenericInsert(r, s, 1); }\n"
                        "/* Run every helper once. */\n"
                        "int api(int *r, int a) { return f(twice(a), ins(r, a)) + raxInsert(r, a) + write_to(r, a); }\n")
        _, soft = gatekeeper.collect(self.repo.path, gatekeeper.session_changes(self.repo.path, {}))
        self.assertEqual([k for k, _ in soft if "|helper|" in k], ["metrics|calc.c|helper|f"])

    def test_gate_requires_function_design_and_teacher_comments(self):
        self.repo.write("README", "fixture\n")
        self.repo.commit()
        undocumented = "\n".join(["#include <stdint.h>", "", "struct hdr {", "    uint8_t len;", "    uint8_t flags:3;",
                                  "    char buf[];", "};", ""]) + c_function("hdr_parse", 20)
        self.repo.write("hdr.c", undocumented)
        self.repo.write("hdr.h", "int hdr_parse(int x);\n")
        self.repo.write("tool.py", '"""Tool module: one public entry point."""\n\n\ndef api(x):\n'
                        '    """Return x doubled, plus one."""\n    y = x * 2\n    y += 1\n    return y\n')
        code, out, err = self.gate()
        self.assertEqual((code, err), (0, ""))
        note = json.loads(out)["systemMessage"]
        self.assertEqual(sorted(re.findall(r"([\w./-]+):\d+: \[([A-Z-]+)\]", note)),
                         [("hdr.c", "NO-DESIGN"), ("hdr.c", "NO-DOC"), ("hdr.c", "NO-TEACHER"), ("hdr.c", "NO-TEST")])
        self.assertIn("hdr.c:5: [NO-TEACHER] layout-sensitive code (uint8_t flags:3;)", note)

    def test_rhythm_asks_only_when_the_run_grew(self):
        # An old long function is asked about when the change lengthens its
        # longest run; a fix inside it, the same length, changes nothing.
        source = c_nested("pipeline")
        self.repo.write("core.c", source)
        self.repo.commit()
        self.repo.write("core.c", source.replace("x += 30;", "x += 31;"))
        self.assertEqual(self.gate(), (0, "", ""))
        self.repo.write("core.c", source.replace("x += 30;", "x += 30;\n    x += 300;"))
        self.assertIn(("core.c", "RHYTHM"), self.signals())

    def test_no_test_follows_the_interface(self):
        # One session per case on a fixed base, with names drawn per seed:
        # NO-TEST is due for a new or changed type and a new exported function,
        # and never when a test file changed or only a comment did.
        rng = random.Random(SEED)
        name = "".join(rng.choice("abcdefgh") for _ in range(6))
        net = "struct %s {\n    int len;\n};\n\nint %s_parse(int x) {\n    return x;\n}\n" % (name, name)
        go = "package alias\n\ntype Size int\n\nfunc F() int {\n\treturn 1\n}\n"
        base = {"net.c": net, "net.h": "int %s_parse(int x);\nint %s_new(void);\n" % (name, name),
                "anon.c": "typedef struct {\n    int a;\n} anon_t;\n", "model.py": "class Model:\n    a = 1\n",
                "api.ts": "export interface Shape {\n  a: number;\n}\n", "alias.go": go}
        extra = net + "struct extra {\n    int y;\n};\n"
        probe = "int probe(void) {\n    return 0;\n}\n"
        cases = [
            ({"net.c": extra}, "type 'extra' is new"),
            ({"net.c": extra, "tests/test_net.c": probe}, None),
            ({"net.c": extra, "src/net_test.go": "package net\n"}, None),
            ({"net.c": net.replace("int len;", "long len;")}, "type '%s' is changed" % name),
            ({"net.c": net.replace("int len;", "int len; /* Bytes after the header. */")}, None),
            ({"net.c": net + "int %s_new(void) {\n    return 1;\n}\n" % name}, "function '%s_new' is new and exported" % name),
            ({"net.c": net + "static int helper(void) {\n    return 1;\n}\n"}, None),
            ({"net.c": net + "int undeclared(void) {\n    return 1;\n}\n"}, None),
            ({"anon.c": "typedef struct {\n    int b;\n} anon_t;\n"}, "type 'anon_t' is changed"),
            ({"model.py": "class Model:\n    a = 2\n"}, "type 'Model' is changed"),
            ({"model.py": "class Model:\n    # Starts at one, the first valid frame number.\n    a = 1\n"}, None),
            ({"api.ts": "export interface Shape {\n  a: string;\n}\n"}, "type 'Shape' is changed"),
            ({"alias.go": go.replace("return 1", "return 2")}, None),
            ({"tests/helper.c": "struct probe {\n    int a;\n};\n"}, None),
        ]
        for files, expected in cases:
            repo = Repo()
            try:
                for rel, text in base.items():
                    repo.write(rel, text)
                repo.commit()
                for rel, text in files.items():
                    repo.write(rel, text)
                code, out, err = self.gate(cwd=repo.path)
                with self.subTest(files=sorted(files), expected=expected, seed=SEED):
                    self.assertEqual(code, 0, err)
                    note = json.loads(out)["systemMessage"] if out else ""
                    self.assertEqual(re.findall(r"\[NO-TEST\] (.*?) and no test file changed", note),
                                     [expected] if expected else [])
            finally:
                repo.close()

        # Once per session: a further change never shows it again, however the first file changes.
        repo, session = Repo(), str(uuid.uuid4())
        try:
            for rel, text in base.items():
                repo.write(rel, text)
            repo.commit()
            repo.write("net.c", extra)
            self.assertIn("[NO-TEST]", self.gate(session, cwd=repo.path)[1])
            repo.write("anon.c", "typedef struct {\n    int c;\n} anon_t;\n")
            self.assertNotIn("[NO-TEST]", self.gate(session, cwd=repo.path)[1])
        finally:
            repo.close()

    def test_gate_accepts_documented_module(self):
        self.repo.write("README", "fixture\n")
        self.repo.commit()
        documented = "\n".join([
            "/* hdr.c -- length-prefixed frame header, one allocation per frame.",
            " *",
            " * Layout, header right before the payload:",
            " *   [len:1B][flags:3b][buf: len bytes]",
            " */",
            "#include <stdint.h>", "", "struct hdr {", "    uint8_t len;", "    uint8_t flags:3;", "    char buf[];",
            "};", "", "/* Parse one frame header; returns its payload length. */"]) + "\n" + c_function("hdr_parse", 20)
        self.repo.write("hdr.c", documented + "int main(void) {\n    int x = 0;\n    x += 1;\n    return hdr_parse(x);\n}\n")
        self.repo.write("tests/hdr_test.c", "int probe(void) {\n    return 0;\n}\n")
        self.assertEqual(self.gate(), (0, "", ""))

    def test_dmod_is_reported_never_gated(self):
        self.repo.write("README", "fixture\n")
        self.repo.commit()
        self.repo.write("tiny.c", "int a(void) { return 1; }\nint b(void) { return 2; }\nint c(void) { return 3; }\n")
        self.assertEqual(self.gate(), (0, "", ""))
        code, out, _ = script("craft-metrics.py", "--report", "tiny.c", cwd=self.repo.path)
        self.assertEqual(code, 0)
        self.assertIn(["tiny.c", 1, "DMOD", "", 1.0], json.loads(out)["f"]["r"])

    def test_gate_layout_assertions(self):
        self.repo.write("README", "fixture\n")
        self.repo.commit()
        for name, text in LAYOUT_FIXTURES.items():
            self.repo.write(name, text)
        code, out, err = self.gate()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(sorted(re.findall(r"([\w./-]+):\d+: \[NO-LAYOUT-ASSERT\] struct '(\w+)'",
                                           json.loads(out)["systemMessage"])),
                         [("cells.c", "frame"), ("hdr.py", "Hdr"), ("packed.c", "sdshdr8"), ("repr.rs", "Hdr")])

    def test_generated_files_leave_the_gate(self):
        self.repo.write("README", "fixture\n")
        self.repo.commit()
        debt, late = f"// {TODO}: tidy up\nexport const x = 1;\n", "// filler\n" * 10
        generated = {
            "src/utils.gen.ts": debt,
            "go/api.pb.go": "// Code generated by protoc-gen-go. DO NOT EDIT.\n\n" + debt,
            "src/meta.ts": "/* @generated by relay */\n" + debt,
            "src/doc.ts": "/**\n * @generated\n */\n" + debt,
            "src/attr.ts": debt,
        }
        judged = {
            "src/plain.ts": debt,
            "src/late.ts": late + "// @generated\n" + debt,
            "src/string.ts": 'export const marker = "@generated";\n' + debt,
        }
        self.repo.write(".gitattributes", "src/attr.ts linguist-generated\n")
        for rel, text in {**generated, **judged}.items():
            self.repo.write(rel, text)
        code, _, err = self.gate()
        self.assertEqual(code, 2, err)
        self.assertEqual(sorted(set(re.findall(r"^(\S+):\d+: \[%s\]" % TODO, err, re.M))), sorted(judged))

    def test_a_generated_file_is_not_reported_but_still_uses_a_name(self):
        self.repo.write("lib.ts", "export function oldName() { return 1; }\n")
        self.repo.write("api.gen.ts", "// oldName() is wrapped here.\nexport const x = 1;\n")
        self.repo.write("hand.ts", "// oldName() is documented here.\nexport const y = 2;\n")
        self.repo.commit()
        self.repo.write("lib.ts", "export function newName() { return 1; }\n")
        code, _, err = self.gate()
        self.assertEqual(code, 2, err)
        self.assertIn("hand.ts:1: [ORPHAN] 'oldName'", err)
        self.assertNotIn("api.gen.ts", err)
        # Code in a generated file keeps the name alive: the hand-written comment is still true.
        self.repo.write("use.gen.ts", "import { oldName } from './lib';\nexport const z = oldName;\n")
        self.assertNotIn("ORPHAN", self.gate()[2])

    def test_gate_is_silent_outside_git(self):
        plain = Repo(git=False)
        try:
            plain.write("big.c", c_function("pipeline", 80) + "// " + TODO + ": no baseline, no judgment\n")
            self.assertEqual(self.gate(cwd=plain.path), (0, "", ""))
            self.assertEqual(self.hook("--snapshot", cwd=plain.path), (0, "", ""))
            self.assertEqual(script("craft-gate.py", "--staged", cwd=plain.path), (0, "", ""))
        finally:
            plain.close()

    def test_staged_gate_judges_the_index(self):
        # git pre-commit: the staged bytes decide, never the working tree.
        self.repo.write("README", "fixture\n")
        self.repo.commit()

        def staged(*rels):
            run(["git", "add", *rels], cwd=self.repo.path)
            return script("craft-gate.py", "--staged", cwd=self.repo.path)

        marker = "# %s: staged debt\n" % TODO
        self.repo.write("debt.py", "x = 1\n" + marker)
        code, out, err = staged("debt.py")
        self.assertEqual((code, out), (1, ""), err)
        self.assertIn("fix before committing", err)
        self.assertIn("debt.py:2: [%s]" % TODO, err)
        # A fix left out of the index does not count; once staged it does, and
        # a marker that was never staged cannot block the commit.
        self.repo.write("debt.py", "x = 1\n")
        self.assertEqual(script("craft-gate.py", "--staged", cwd=self.repo.path)[0], 1)
        self.repo.write("loose.py", marker)
        self.assertEqual(staged("debt.py"), (0, "", ""))
        # Review signals alone print on stderr and let the commit through.
        self.repo.write("core.c", "int twice(int x) { return 2 * x; }\nstatic int helper(int x) { return twice(x); }\n"
                        "int base(int x) { return helper(x); }\n")
        code, out, err = staged("core.c")
        self.assertEqual((code, out), (0, ""), err)
        self.assertIn("[HELPER] 'helper' only hands its parameters to 'twice'", err)
        self.assertNotIn("fix before committing", err)


class GitFailureTest(unittest.TestCase):
    """Fault injection for the hooks: a git command that fails costs a check, never a false block.

    Why in process: on Windows `git` resolves to git.exe whatever stands first
    on PATH, so a shim script cannot stand in for it. craftlib runs git only
    through subprocess.run, so that call is the seam. Every test fails the
    k-th command for k = 1..N, N counted on a clean run.
    """

    TRANSIENT = "fatal: unable to write new index file\n"
    BREAKS = (lambda args: subprocess.CompletedProcess(args, 128, "", GitFailureTest.TRANSIENT),
              lambda args: subprocess.CompletedProcess(args, 1, "", "error: object file is empty\n"),
              lambda args: (_ for _ in ()).throw(OSError("git vanished")),
              lambda args: (_ for _ in ()).throw(subprocess.TimeoutExpired(args, 30)))

    def setUp(self):
        # Old debt in a tracked file, and debt in a file dirty before the session:
        # neither is the session's, so a healthy gate says nothing about them.
        self.repo = Repo()
        self.addCleanup(self.repo.close)
        self.committed = f"# {TODO}: debt from before the session\nX = 1\n"
        self.repo.write("old.py", self.committed)
        self.repo.commit()
        self.repo.write("wip.py", f"# {TODO}: the user's work in progress\nW = 1\n")
        patcher = unittest.mock.patch.object(cl.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def session(self):
        """Return the hook payload of a new session, whose state files are removed after the test."""
        data = {"session_id": str(uuid.uuid4()), "cwd": self.repo.path}
        for kind in ("snap", "gate"):
            path = gatekeeper._state_path(kind, data["session_id"])
            self.addCleanup(lambda p=path: os.path.exists(p) and os.remove(p))
        return data

    def edit(self, extra="Y = 2\n"):
        """Add `extra` to the committed file, as the session would."""
        self.repo.write("old.py", self.committed + extra)

    def hook(self, function, *args, first=0, last=0, broken=None):
        """Call `function` with git commands number first..last broken; return (code, stdout, stderr, git calls)."""
        real, calls = subprocess.run, [0]

        def fake(args, **kwargs):
            calls[0] += 1
            return broken(args) if first <= calls[0] <= last else real(args, **kwargs)

        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(cl.subprocess, "run", fake), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = function(*args)
        return code, out.getvalue(), err.getvalue(), calls[0]

    def test_stop_passes_when_git_fails(self):
        data = self.session()
        self.assertEqual(self.hook(gatekeeper.snapshot, data)[:3], (0, "", ""))
        self.edit()
        code, out, err, total = self.hook(gatekeeper.gate, data)
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertGreater(total, 3)
        for k in range(1, total + 1):
            with self.subTest(k=k):
                broken = self.BREAKS[k % len(self.BREAKS)]
                code, out, err, calls = self.hook(gatekeeper.gate, data, first=k, last=sys.maxsize, broken=broken)
                self.assertGreaterEqual(calls, k, "the fault never fired")
                self.assertEqual((code, out, err), (0, "", ""))

    def test_a_failed_snapshot_never_blocks_the_session(self):
        self.repo.write("old.py", self.committed)
        _, _, _, total = self.hook(gatekeeper.snapshot, self.session())
        self.assertGreater(total, 1)
        for k in range(1, total + 1):
            with self.subTest(k=k):
                data = self.session()
                self.repo.write("old.py", self.committed)
                broken = self.BREAKS[k % len(self.BREAKS)]
                code, out, err, calls = self.hook(gatekeeper.snapshot, data, first=k, last=sys.maxsize, broken=broken)
                self.assertGreaterEqual(calls, k, "the fault never fired")
                self.assertEqual((code, out, err), (0, "", ""))
                self.edit()
                self.assertEqual(self.hook(gatekeeper.gate, data)[:3], (0, "", ""))

    def test_one_transient_failure_is_retried_and_changes_nothing(self):
        data = {"cwd": self.repo.path}
        self.edit(f"# {TODO}: written in this session\n")
        clean = self.hook(gatekeeper.gate, data)
        self.assertEqual(clean[0], 2, clean[2])
        self.assertIn("[%s]" % TODO, clean[2])
        for k in range(1, clean[3] + 1):
            with self.subTest(k=k):
                self.assertEqual(self.hook(gatekeeper.gate, data, first=k, last=k, broken=self.BREAKS[0])[:3], clean[:3])

    def test_staged_passes_when_git_fails(self):
        self.edit()
        run(["git", "add", "old.py"], cwd=self.repo.path)
        here = os.getcwd()
        os.chdir(self.repo.path)
        self.addCleanup(os.chdir, here)
        code, out, err, total = self.hook(gatekeeper.staged)
        self.assertEqual((code, out, err), (0, "", ""))
        for k in range(1, total + 1):
            with self.subTest(k=k):
                code, out, err, calls = self.hook(gatekeeper.staged, first=k, last=sys.maxsize, broken=self.BREAKS[0])
                self.assertGreaterEqual(calls, k, "the fault never fired")
                self.assertEqual((code, out, err), (0, "", ""))

    def test_a_plain_no_from_git_is_not_a_failure(self):
        # A repository with no commits answers exit 1, quietly: every file is simply new.
        empty = Repo()
        self.addCleanup(empty.close)
        empty.write("a.py", f"# {TODO}: first file\n")
        before = cl.git_failures()
        code, _, err, _ = self.hook(gatekeeper.gate, {"cwd": empty.path})
        self.assertEqual(code, 2, err)
        self.assertEqual(cl.git_failures(), before)

    def test_tracked_changes_tell_new_files_without_probing(self):
        self.repo.write("gone.py", "G = 1\n" * 40)
        self.repo.commit()
        run(["git", "mv", "gone.py", "moved.py"], cwd=self.repo.path)
        self.repo.write("added.py", "A = 1\n")
        self.edit()
        run(["git", "add", "added.py"], cwd=self.repo.path)
        got = dict(cl.tracked_changes(self.repo.path, "HEAD"))
        self.assertEqual(got, {"added.py": True, "moved.py": True, "old.py": False})

    def test_edit_hooks_never_call_git(self):
        path = self.repo.write("new.py", f"# {TODO}: written by this edit\n")
        data = {"tool_name": "Write", "tool_input": {"file_path": path, "content": "x"}}
        debt_scan, broken = gatekeeper._load("craft-debt-scan"), self.BREAKS[0]
        for from_hook, want in ((debt_scan.from_hook, True), (sync.from_hook, False)):
            found = []
            code, _, _, calls = self.hook(lambda: found.append(from_hook(data)), first=1, last=sys.maxsize, broken=broken)
            self.assertEqual((code, calls, bool(found[0])), (None, 0, want))


class StatePurgeTest(unittest.TestCase):
    def test_session_start_purges_state_older_than_a_week(self):
        folder, repo = tempfile.mkdtemp(prefix="craft-state-"), Repo()
        saved, tempfile.tempdir = tempfile.tempdir, folder
        try:
            week, now = 7 * 86400, time.time()
            ages = {"craft-snap-old.json": week + 60, "craft-gate-old.json": week + 60, "craft-snap-fresh.json": week - 60,
                    "craft-gate-fresh.json": 0, "craft-other-old.json": week * 2, "notes.json": week * 2}
            for name, age in ages.items():
                path = os.path.join(folder, name)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("{}")
                os.utime(path, (now - age, now - age))
            gatekeeper.snapshot({"session_id": "purge-test", "cwd": repo.path})
            left = set(os.listdir(folder))
            self.assertFalse({"craft-snap-old.json", "craft-gate-old.json"} & left)
            self.assertLessEqual({"craft-snap-fresh.json", "craft-gate-fresh.json", "craft-other-old.json", "notes.json"}, left)
            # The purge runs even when SessionStart has no session to snapshot.
            os.utime(os.path.join(folder, "craft-snap-fresh.json"), (now - week * 2, now - week * 2))
            gatekeeper.snapshot({})
            self.assertNotIn("craft-snap-fresh.json", os.listdir(folder))
        finally:
            tempfile.tempdir = saved
            repo.close()
            shutil.rmtree(folder)


class GitHookTest(unittest.TestCase):
    """scripts/install-git-hook.sh: the Stop gate's checks as a real pre-commit hook, and as a CI step."""

    def setUp(self):
        self.repo = Repo()
        self.repo.write("README", "fixture\n")
        self.repo.commit()
        self.hook = os.path.join(self.repo.path, ".git", "hooks", "pre-commit")

    def tearDown(self):
        self.repo.close()

    def install(self):
        return run([BASH, posix(os.path.join(ROOT, "scripts", "install-git-hook.sh")), posix(self.repo.path)])

    def head(self):
        return run(["git", "rev-parse", "HEAD"], cwd=self.repo.path)[1]

    def commit(self, rel, text, *flags):
        """Write and stage `rel`, then run a real git commit; return (exit code, stderr)."""
        self.repo.write(rel, text)
        run(["git", "add", rel], cwd=self.repo.path)
        code, _, err = run(["git", "-c", "user.email=test@example.com", "-c", "user.name=test", "-c", "commit.gpgsign=false",
                            "commit", "-q", "-m", "change", *flags], cwd=self.repo.path)
        return code, err

    def test_bare_marker_blocks_the_commit(self):
        self.assertEqual(self.install()[0], 0)
        before = self.head()
        code, err = self.commit("debt.py", "x = 1\n# %s: staged debt\n" % TODO)
        self.assertEqual(code, 1, err)
        self.assertIn("fix before committing", err)
        self.assertEqual(self.head(), before)

    def test_review_signals_let_the_commit_pass(self):
        self.assertEqual(self.install()[0], 0)
        before = self.head()
        code, err = self.commit("core.c", "int twice(int x) { return 2 * x; }\nstatic int helper(int x) { return twice(x); }\n"
                                "int base(int x) { return helper(x); }\n")
        self.assertEqual(code, 0, err)
        self.assertIn("[HELPER]", err)
        self.assertNotEqual(self.head(), before)

    def test_a_foreign_hook_stays_and_ours_refreshes(self):
        # A hook someone else wrote may hold their own checks: refuse, never overwrite.
        foreign = "#!/bin/sh\nexit 0\n"
        with open(self.hook, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(foreign)
        code, _, err = self.install()
        self.assertEqual(code, 1, err)
        self.assertIn("craft-gate.py", err)
        self.assertEqual(read_file(self.hook), foreign)
        # Ours may be written over: running the installer twice changes nothing.
        os.remove(self.hook)
        self.assertEqual(self.install()[0], 0)
        first = read_file(self.hook)
        self.assertEqual(self.install()[0], 0)
        self.assertEqual(read_file(self.hook), first)

    def test_hooks_path_is_followed(self):
        # Why: with core.hooksPath set, git never reads .git/hooks.
        run(["git", "config", "core.hooksPath", "myhooks"], cwd=self.repo.path)
        self.assertEqual(self.install()[0], 0)
        self.assertFalse(os.path.exists(self.hook))
        self.assertTrue(os.path.isfile(os.path.join(self.repo.path, "myhooks", "pre-commit")))
        self.assertEqual(self.commit("debt.py", "x = 1\n# %s: staged debt\n" % TODO)[0], 1)

    def test_ci_step_judges_the_whole_branch(self):
        # The README's CI recipe: staging the branch's diff reuses the same gate.
        self.assertEqual(self.commit("debt.py", "x = 1\n# %s: staged debt\n" % TODO)[0], 0)
        self.assertEqual(run(["git", "reset", "--soft", "HEAD~1"], cwd=self.repo.path)[0], 0)
        code, _, err = script("craft-gate.py", "--staged", cwd=self.repo.path)
        self.assertEqual(code, 1, err)
        self.assertIn("debt.py:2: [%s]" % TODO, err)


class MetricsTest(unittest.TestCase):
    def test_report_json(self):
        plain = Repo(git=False)
        try:
            plain.write("mod.c", "struct pair { int a; int b; };\n" + c_function("twice", 5, static=True)
                        + "static int helper(int x) { return twice(x); }\n"
                        + "int api_one(int x) { return helper(x); }\n" + c_nested("api_two"))
            plain.write("mod.h", "int api_one(int x);\nint api_two(int x);\n")
            code, out, _ = script("craft-metrics.py", "--report", "mod.c", cwd=plain.path)
            self.assertEqual(code, 0)
            data = json.loads(out)
            self.assertEqual((data["n"], data["f"]["n"]), (len(data["r"]), len(data["f"]["r"])))
            row = dict(zip(data["c"], data["r"][0]))
            self.assertEqual((row["file"], row["pub"], row["fn40"], row["rhythm_bad"], row["helpers1"]),
                             ("mod.c", 2, 1, 1, 1))
            self.assertEqual(sorted((f[2], f[3]) for f in data["f"]["r"]),
                             [("HELPER", "helper"), ("NO-DESIGN", ""), ("NO-DOC", "api_two"), ("RHYTHM", "api_two")])
            self.assertEqual((data["total"]["no_doc"], data["total"]["no_design"], data["total"]["no_teacher"]),
                             (1, 1, 0))
            # Mean density, LOC per Guide comment; a file without Guide comments has none.
            self.assertIsNone(row["density"])
            guided = c_function("api_three", 80, guide_every=40)
            plain.write("guided.c", guided)
            data = json.loads(script("craft-metrics.py", "--report", "guided.c", cwd=plain.path)[1])
            row = dict(zip(data["c"], data["r"][0]))
            guides = sum(1 for line in guided.splitlines() if line.strip().startswith("/*"))
            self.assertEqual(row["density"], round(80 / guides, 1))
            self.assertEqual((data["total"]["functions"], data["total"]["guide_density"]), (1, row["density"]))
        finally:
            plain.close()

    def test_report_comment_ratio_and_grid_lint(self):
        plain = Repo(git=False)
        try:
            grid = "/* Table:\n * +---+---+\n * | a | b |\n * +---+---+\n * | c | d |\n * +---+---+\n */\n"
            strip = "/* Frame:\n * +---+---+\n * | a | b |\n * +---+---+\n */\n"
            plain.write("grid.c", grid + "int a(int x) { return x; }\nint b(int x) { return x; } // Trailing.\n")
            plain.write("strip.c", strip + "int c(int x) { return x; }\n")
            data = json.loads(script("craft-metrics.py", "--report", "grid.c", "strip.c", cwd=plain.path)[1])
            rows = {r[0]: dict(zip(data["c"], r)) for r in data["r"]}
            # grid.c: 7 comment-only lines plus one trailing comment, over 2 lines of code.
            self.assertEqual(rows["grid.c"]["comment_ratio"], 4.0)
            self.assertEqual(rows["strip.c"]["comment_ratio"], 5.0)
            self.assertEqual(data["total"]["comment_ratio"], round(13 / 3, 2))
            # A one-row strip is a shape the standard allows; three rulers make a grid.
            grids = [(f[0], f[1], f[4]) for f in data["f"]["r"] if f[2] == "GRID"]
            self.assertEqual(grids, [("grid.c", 2, 3)])
            self.assertEqual(data["total"]["grid"], 1)
        finally:
            plain.close()

    def test_grid_lint_stays_out_of_the_gate(self):
        repo = Repo()
        try:
            repo.write("README", "fixture\n")
            repo.commit()
            repo.write("grid.c", "/* Table:\n * +---+---+\n * | a | b |\n * +---+---+\n * | c | d |\n * +---+---+\n */\nint a;\n")
            run(["git", "add", "grid.c"], cwd=repo.path)
            code, _, err = script("craft-gate.py", "--staged", cwd=repo.path)
            self.assertEqual(code, 0, err)
            self.assertNotIn("GRID", err)
        finally:
            repo.close()

    def test_pass_through_against_generated_oracle(self):
        # A generated private helper per trial: only the "pass" variant, a body
        # that hands every parameter on in order and unchanged, is a pass-through.
        rng = random.Random(SEED)
        templates = {  # ext: (signature, call statement, extra statement, end, parameter form, no parameters)
            ".c": ("static int {name}({params}) {{", "    return {call};", "    int t = 0;", "}", "int {}", "void"),
            ".go": ("func {name}({params}) int {{", "\treturn {call}", "\tt := 0", "}", "{} int", ""),
            ".rs": ("fn {name}({params}) -> i32 {{", "    {call}", "    let t = 0;", "}", "{}: i32", ""),
            ".py": ("def _{name}({params}):", "    return {call}", "    t = 0", "", "{}", ""),
            ".js": ("function {name}({params}) {{", "  return {call};", "  let t = 0;", "}", "{}", ""),
            ".rb": ("def {name}({params})", "  {call}", "  t = 0", "end", "{}", ""),
        }
        for trial in range(300):
            ext = rng.choice(sorted(templates))
            sig, call, extra, end, form, empty = templates[ext]
            params = rng.sample(["alpha", "beta", "gamma", "delta"], rng.randint(0, 3))
            variants = ["pass", "extra", "logic"] + (["drop", "expr"] if params else []) \
                + (["reorder"] if len(params) >= 2 else [])
            variant, args = rng.choice(variants), list(params)
            if variant == "extra":
                args.append(str(rng.randint(0, 9)))
            elif variant == "drop":
                args.pop(rng.randrange(len(args)))
            elif variant == "expr":
                args[0] += " + 1"
            elif variant == "reorder":
                args.reverse()
            body = [extra] if variant == "logic" else []
            source = "\n".join([sig.format(name="h%d" % trial, params=", ".join(form.format(p) for p in params) or empty)]
                               + body + [call.format(call="g%d(%s)" % (trial, ", ".join(args))), end]) + "\n"
            _, funcs = metrics.functions("helper" + ext, source)
            with self.subTest(trial=trial, seed=SEED, source=source):
                self.assertEqual([f.forward for f in funcs], ["g%d" % trial if variant == "pass" else ""])

    def test_extractor_across_languages(self):
        for name, (source, expected) in EXTRACTOR_FIXTURES.items():
            _, funcs = metrics.functions(name, textwrap.dedent(source).lstrip("\n"))
            with self.subTest(file=name):
                self.assertEqual({(f.name, f.public) for f in funcs}, expected)
        _, funcs = metrics.functions("node.c", textwrap.dedent(EXTRACTOR_FIXTURES["node.c"][0]).lstrip("\n"))
        helper = next(f for f in funcs if f.name == "helper")
        self.assertEqual((helper.start, helper.line, helper.first, helper.last), (4, 5, 7, 7))

    def test_body_stats_against_generated_oracle(self):
        rng = random.Random(SEED)
        for family, guide, trailing, head, indent in (("c", "/* guide */", " // trailing", "int f(int x) {", "    "),
                                                      ("py", "# guide", "  # trailing", "def f(x):", "    ")):
            for _ in range(100):
                loc, run_len, gap, body = rng.randint(1, 120), 0, 0, []
                for _ in range(loc):
                    if rng.random() < 0.08:
                        body.append(indent + guide)
                        run_len = 0
                    if rng.random() < 0.1:
                        body.append("")
                    body.append(indent + "x += 1" + (";" if family == "c" else "")
                                + (trailing if rng.random() < 0.2 else ""))
                    run_len += 1
                    gap = max(gap, run_len)
                text = head + "\n" + "\n".join(body) + ("\n}\n" if family == "c" else "\n")
                lines, funcs = metrics.functions("f." + family, text)
                with self.subTest(family=family, seed=SEED):
                    self.assertEqual(len(funcs), 1)
                    self.assertEqual(metrics.body_stats(lines, funcs[0]), (loc, gap))

    def test_flat_switch_does_not_lengthen_a_run(self):
        # The cases of a flat switch are a table: neutral for the run. One
        # block opened inside any case makes it ordinary code again.
        rng = random.Random(SEED)
        for _ in range(100):
            before, after, arms, nested = rng.randint(1, 20), rng.randint(1, 20), [], rng.random() < 0.4
            for i in range(rng.randint(3, 40)):
                arms += ["    case %d:" % i, "        x = %d;" % i, "        break;"]
            if nested:
                arms.insert(rng.randrange(1, len(arms)), "        { x += 1; }")
            body = ["    x += 1;"] * before + ["    switch (x) {"] + arms + ["    }"] + ["    x += 2;"] * after
            lines, funcs = metrics.functions("f.c", "int f(int x) {\n" + "\n".join(body) + "\n    return x;\n}\n")
            loc = len(body) + 1
            with self.subTest(seed=SEED, nested=nested):
                self.assertEqual(metrics.body_stats(lines, funcs[0]), (loc, loc if nested else loc - len(arms)))

    def test_profile_against_generated_oracle(self):
        # A body of known shape, with decoys the profile must not count: a
        # keyword in a string, a preprocessor #if, a case label, a method
        # named match, and in Python a bracket continued on a deeper line.
        rng = random.Random(SEED)
        for trial in range(200):
            depth, ifs = rng.randint(0, 6), rng.randint(0, 12)
            for ext, opener, closer, step, tail, decoys in (
                    (".c", "if (x) {", "}", "    ", "    x += 1;",
                     ['    char *s = "if (x) { while";', "#if FEATURE", "#endif", "    switch (x) {", "    case 1:",
                      "        x = 2;", "    }"]),
                    (".py", "if x:", None, "    ", "    x += 1",
                     ['    s = "if x: while"', "    y = max(x,", "                1)", "    z = s.match(x)"])):
                body, indent = [], "    "
                for _ in range(depth):
                    body.append(indent + opener)
                    indent += step
                body.append(indent + "x -= 1" + (";" if ext == ".c" else ""))
                for d in reversed(range(depth)):
                    if closer:
                        body.append("    " + step * d + closer)
                for i in range(ifs):
                    body += ["    " + opener, "    " + step + "x -= %d" % i + (";" if ext == ".c" else "")]
                    if closer:
                        body.append("    " + closer)
                body += decoys + [tail]
                head = "int f(int x) {" if ext == ".c" else "def f(x):"
                text = head + "\n" + "\n".join(body) + ("\n}\n" if ext == ".c" else "\n")
                lines, funcs = metrics.functions("f" + ext, text)
                with self.subTest(trial=trial, ext=ext, seed=SEED, text=text):
                    self.assertEqual(len(funcs), 1)
                    # The C decoy switch counts once, its case label never.
                    self.assertEqual(metrics.profile(lines, funcs[0], ext == ".c"),
                                     (depth + ifs + (ext == ".c"), max(depth, 1 if ifs or ext == ".c" else 0)))

    def test_rhythm_follows_complexity_not_size(self):
        # Same length, different shape: a flat handler or table gets no signal,
        # a nested or branchy function does, and test code is exempt.
        flat = c_function("handler", 70)
        table = "int table(int x) {\n    switch (x) {\n" \
            + "".join("    case %d: return %d;\n" % (i, i * 2) for i in range(70)) + "    }\n    return 0;\n}\n"
        branchy = "int branchy(int x) {\n" + "".join(
            "    if (x > %d) x -= 1;\n    x += %d;\n" % (i, i) for i in range(35)) + "    return x;\n}\n"
        cases = {("mod.c", flat): False, ("mod.c", table): False, ("mod.c", c_nested("sixty", 60)): True,
                 ("mod.c", branchy): True, ("mod.c", c_nested("fuzzTest")): False,
                 ("mod.c", c_nested("test_parse")): False, ("tests/mod.c", c_nested("parse")): False}
        for (rel, source), expected in cases.items():
            lines, funcs = metrics.functions(rel, source)
            loc, gap = metrics.body_stats(lines, funcs[0])
            with self.subTest(name=funcs[0].name, rel=rel):
                self.assertEqual(metrics.rhythm_due(funcs[0], rel, loc, gap, metrics.profile(lines, funcs[0], True)),
                                 expected)
        # 60 lines, 6 branches and nesting 3, named by the review as the case that must signal.
        lines, funcs = metrics.functions("mod.c", c_nested("sixty", 60).replace(
            "    x += 0;\n    x += 1;\n    x += 2;\n", "    if (x) x--;\n    if (x) x--;\n    if (x) x--;\n"))
        self.assertEqual(metrics.profile(lines, funcs[0], True), (6, 3))
        self.assertTrue(metrics.rhythm_due(funcs[0], "mod.c", *metrics.body_stats(lines, funcs[0]),
                                           metrics.profile(lines, funcs[0], True)))

    def test_exported_names_per_language(self):
        # Each language declares its interface its own way; the file list is
        # (path, source) and the last element the names it must export.
        function = "int %s(void) { return 1; }\n"
        groups = [
            ([("a.c", function % "api" + function % "internal" + "static " + function % "hidden"),
              ("a.h", "int api(void);\n")], "a.c", {"api"}),
            ([("src/b.c", function % "api" + function % "internal"), ("include/b.h", "int api(void);\n")],
             "src/b.c", {"api"}),
            ([("c.c", function % "api")], "c.c", set()),
            ([("d.h", "static inline " + function % "inline_one" + function % "plain_one")], "d.h",
             {"inline_one", "plain_one"}),
            ([("m.py", '__all__ = ["a"]\n\n\ndef a():\n    pass\n\n\ndef b():\n    pass\n')], "m.py", {"a"}),
            ([("pkg/__init__.py", "from .mod import a\n"), ("pkg/mod.py", "def a():\n    pass\n\n\ndef b():\n    pass\n")],
             "pkg/mod.py", {"a"}),
            ([("star/__init__.py", "from .mod import *\n"), ("star/mod.py", "def a():\n    pass\n\n\ndef _b():\n    pass\n")],
             "star/mod.py", {"a"}),
            ([("top/__init__.py", "def x():\n    pass\n\n\ndef _y():\n    pass\n")], "top/__init__.py", {"x"}),
            ([("solo.py", "def a():\n    pass\n")], "solo.py", set()),
            ([("lib/index.ts", 'export { a, b as c } from "./x";\n'),
              ("lib/x.ts", "export function a() {\n  return 1;\n}\n\nexport function b() {\n  return 2;\n}\n\n"
                           "export function d() {\n  return 3;\n}\n")], "lib/x.ts", {"a", "b"}),
            ([("all/index.js", "export * from './y.js';\n"), ("all/y.js", "export function a() {\n  return 1;\n}\n")],
             "all/y.js", {"a"}),
            ([("ind/index.ts", "export function c() {\n  return 1;\n}\n\nfunction e() {\n  return 2;\n}\n")],
             "ind/index.ts", {"c"}),
            ([("alone.ts", "export function a() {\n  return 1;\n}\n")], "alone.ts", set()),
            ([("r.rs", "pub fn a() {}\npub(crate) fn b() {}\nfn c() {}\n")], "r.rs", {"a"}),
            ([("g.go", "package g\n\nfunc A() {}\nfunc b() {}\n")], "g.go", {"A"}),
            ([("s.sh", "f() {\n  echo hi\n}\n")], "s.sh", set()),
        ]
        plain = Repo(git=False)
        try:
            for files, target, expected in groups:
                for rel, text in files:
                    plain.write(rel, text)
                path, text = os.path.join(plain.path, target), read_file(os.path.join(plain.path, target))
                lines, funcs = metrics.functions(path, text)
                with self.subTest(target=target):
                    self.assertEqual(metrics.exported_names(path, text, lines, funcs, plain.path), expected)
        finally:
            plain.close()

    def test_owner_and_doc_due(self):
        # owner(): what the caller must release, on generated bodies. Stored
        # results count only when returned; a released lock is no hand-over.
        owning = [("c", "    char *p = malloc(8);\n    p[0] = 0;\n    return p;"),
                  ("c", "    return zmalloc(8);"),
                  ("c", "    robj *o = (robj *)createObject(x);\n    o->x = 1;\n    return o;"),
                  ("c", "    pthread_mutex_lock(&m);\n    x += 1;\n    return x;"),
                  ("py", "    return open(x)"),
                  ("py", "    conn = socket.create_connection(x)\n    return conn")]
        plain_bodies = [("c", "    char *p = malloc(8);\n    free(p);\n    return x;"),
                        ("c", "    return zmalloc_size(x);"),
                        ("c", "    pthread_mutex_lock(&m);\n    x += 1;\n    pthread_mutex_unlock(&m);\n    return x;"),
                        ("c", "    blockClient(c);\n    x += 1;\n    return x;"),
                        ("py", "    with open(x) as f:\n        return f.read()")]
        for bodies, expected in ((owning, True), (plain_bodies, False)):
            for family, body in bodies:
                source = ("static char *f(int x) {\n%s\n}\n" if family == "c" else "def _f(x):\n%s\n") % body
                lines, funcs = metrics.functions("f." + family, source)
                with self.subTest(body=body):
                    self.assertEqual(metrics.owner(lines, funcs[0]), expected)
        source = "static char *f(int x) {\n    char *p = malloc(8);\n    p[0] = x;\n    return p;\n}\n"
        lines, funcs = metrics.functions("mod.c", source)
        self.assertEqual(metrics.doc_due(funcs[0], 3, "mod.c", set(), lines), "owner")
        self.assertEqual(metrics.doc_due(funcs[0], 3, "tests/mod.c", set(), lines), "")
        lines, funcs = metrics.functions("mod.c", "/* Return a new buffer. */\n" + source)
        self.assertEqual(metrics.doc_due(funcs[0], 3, "mod.c", set(), lines), "")
        lines, funcs = metrics.functions("mod.c", "int api(int x) {\n    x += 1;\n    x += 2;\n    return x;\n}\n")
        self.assertEqual(metrics.doc_due(funcs[0], 3, "mod.c", {"api"}, lines), "exported")
        self.assertEqual(metrics.doc_due(funcs[0], 3, "mod.c", set(), lines), "")

    def test_module_traits(self):
        # NO-DESIGN needs something a reader must learn first; size alone is not it.
        cases = [("a.c", "struct pair {\n    int a;\n};\n", ["its own data structure"]),
                 ("b.c", "int f(void) {\n    return 1;\n}\n", []),
                 ("c.c", "void f(void) {\n    pthread_mutex_lock(&m);\n}\n", ["concurrency primitives"]),
                 ("d.py", "class A:\n    pass\n\n\nclass B:\n    pass\n", ["2 public types"]),
                 ("e.py", "class A:\n    pass\n\n\nclass _B:\n    pass\n", []),
                 ("f.py", "import struct\n\nHDR = struct.Struct('<I')\n", ["a binary layout"]),
                 ("g.ts", "export interface A {}\nexport type B = number;\ninterface C {}\n", ["2 public types"]),
                 ("h.ts", "interface C {}\ntype D = number;\n", [])]
        for name, source, expected in cases:
            lines = cl.scan(source, cl.family_of(name))
            with self.subTest(file=name):
                self.assertEqual(metrics.module_traits(lines, cl.family_of(name)), expected)

    def test_layout_patterns_skip_declarations_and_size_arithmetic(self):
        # Each line is masked code as the scanner would produce it.
        for line, expected in (("extern struct cmd table[];", False), ("    unsigned flag:1;", False),
                               ("    if (n > SIZE_MAX - sizeof(x)) return;", False),
                               ("    tail->size = usable - sizeof(block);", False),
                               ("    uint8_t flags:3;", True), ("    char buf[];", True),
                               ("    char *p = s - sizeof(hdr);", True), ("    h = (hdr *)s - sizeof(hdr);", True),
                               ("    s[-1] = 1;", True), ("struct __attribute__((__packed__)) x {", True)):
            with self.subTest(line=line):
                self.assertEqual(bool(metrics.LAYOUT["c"].search(line)), expected)


class ReplayTest(unittest.TestCase):
    """scripts/replay-history.py against a generated history whose debt findings the generator knows."""

    REPLAY = os.path.join(ROOT, "scripts", "replay-history.py")

    def test_no_test_sees_tests_outside_the_prefix(self):
        # redis keeps code in src/ and tests in tests/: under --path src/ the
        # checks cannot see the test files, so the replay must look for them.
        repo = Repo()
        try:
            repo.write("README", "fixture\n")
            repo.commit()
            repo.write("src/a.c", "struct pair {\n    int a;\n};\n")
            repo.commit()
            repo.write("src/a.c", "struct pair {\n    int b;\n};\n")
            repo.write("tests/a.tcl", "test a\n")
            repo.commit()
            out = os.path.join(repo.path, "replay.jsonl")
            code, _, err = script(self.REPLAY, repo.path, "--commits", "2", "--jobs", "1", "--path", "src/", "--out", out)
            self.assertEqual(code, 0, err)
            with open(out, encoding="utf-8") as fh:
                self.assertEqual([json.loads(line)["soft"].get("NO-TEST", 0) for line in fh], [1, 0])
        finally:
            repo.close()
    # Per generated commit kind: the line it appends to its module, and the hard debt kind it must produce.
    LINES = {"todo": ("// %s: tidy up" % TODO, "TODO"), "fixme": ("// %s breaks on empty input" % FIXME, "NO-TRIGGER"),
             "clean": ("/* %s linear scan, fine under 100 entries; index past that. */" % SHORTCUT, None)}

    def test_replay_matches_generated_history(self):
        rng, repo, out = random.Random(SEED), Repo(), None
        try:
            # 1. A history of single-file commits, plus one merged branch whose merge commit is skipped.
            repo.write("README", "fixture\n")
            repo.commit()
            expected, modules = {}, []
            head = (lambda: run(["git", "rev-parse", "HEAD"], cwd=repo.path)[1].strip())
            for i in range(rng.randint(5, 9)):
                kind = rng.choice(sorted(self.LINES))
                if modules and rng.random() < 0.4:
                    rel = rng.choice(modules)
                    with open(os.path.join(repo.path, rel), "a", encoding="utf-8", newline="\n") as fh:
                        fh.write(self.LINES[kind][0] + "\n")
                else:
                    rel = "m%d.c" % i
                    modules.append(rel)
                    repo.write(rel, "int f%d(int x) { return x + %d; }\n%s\n" % (i, i, self.LINES[kind][0]))
                repo.commit()
                expected[head()] = {self.LINES[kind][1]: 1} if self.LINES[kind][1] else {}
            run(["git", "checkout", "-q", "-b", "side"], cwd=repo.path)
            repo.write("side.c", "int side(int x) { return x; }\n" + self.LINES["todo"][0] + "\n")
            repo.commit()
            expected[head()] = {"TODO": 1}
            run(["git", "checkout", "-q", "-"], cwd=repo.path)
            code, _, err = run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "merge", "-q",
                                "--no-ff", "-m", "merge side", "side"], cwd=repo.path)
            self.assertEqual(code, 0, err)

            # 2. The replay judges every non-merge commit but the root, in rev-list order; a second run resumes.
            out = os.path.join(repo.path, "replay.jsonl")
            for _ in range(2):
                code, stdout, err = script(self.REPLAY, repo.path, "--commits", str(len(expected)), "--jobs", "2",
                                           "--out", out)
                self.assertEqual(code, 0, err)
            with open(out, encoding="utf-8") as fh:
                records = [json.loads(line) for line in fh]
            shas = run(["git", "rev-list", "--no-merges", "--reverse", "-n", str(len(expected)), "HEAD"],
                       cwd=repo.path)[1].split()
            with self.subTest(seed=SEED):
                self.assertEqual([r["sha"] for r in records], shas)
                self.assertEqual({r["sha"]: r["hard"] for r in records}, expected)
                summary = json.loads(stdout.splitlines()[-1])
                self.assertEqual((summary["commits"], summary["commits_hard_debt"], summary["commits_hard_heuristic"]),
                                 (len(expected), sum(1 for e in expected.values() if e), 0))

            # 3. --static judges HEAD as new files: every marker still in the tree, once.
            code, stdout, err = script(self.REPLAY, repo.path, "--static")
            self.assertEqual(code, 0, err)
            markers = collections.Counter(k for e in expected.values() for k in e)
            with self.subTest(seed=SEED):
                self.assertEqual(json.loads(stdout.splitlines()[-1])["hard"], dict(markers))

            # 4. --sample prints each drawn finding with its line marked.
            code, stdout, err = script(self.REPLAY, repo.path, "--sample", "2", "--out", out)
            self.assertEqual(code, 0, err)
            self.assertEqual(stdout.count("\n=== ") + stdout.startswith("=== "), min(2, sum(markers.values())))
            self.assertRegex(stdout, r"(?m)^>\s+\d+  ")
        finally:
            repo.close()


def c_compiler():
    """Return the C compiler as an argument list: $CRAFT_CC (it may carry arguments), else cc, gcc or clang; None."""
    if os.environ.get("CRAFT_CC"):
        return shlex.split(os.environ["CRAFT_CC"])
    found = next((shutil.which(name) for name in ("cc", "gcc", "clang") if shutil.which(name)), None)
    return [found] if found else None


# A map with the subset of rax's API the skill's driver calls, behind rax's
# allocator macros. Each new key costs exactly three allocations (entry, key
# copy, index), all made before the map changes. The BUG_* switches are the
# faults the driver must catch: a leak on the error path, a size bumped before
# the last allocation, and an allocator that bypasses the harness.
MINI_RAX_H = """\
#include <stddef.h>
#include <stdint.h>
typedef struct raxEntry { unsigned char *key; size_t len; void *data; } raxEntry;
typedef struct rax { raxEntry **items; uint64_t numele; } rax;
typedef struct raxIterator { rax *rt; uint64_t pos; unsigned char *key; size_t key_len; void *data; } raxIterator;
extern void *raxNotFound;
rax *raxNew(void);
int raxInsert(rax *t, unsigned char *s, size_t len, void *data, void **old);
void *raxFind(rax *t, unsigned char *s, size_t len);
uint64_t raxSize(rax *t);
void raxFree(rax *t);
void raxStart(raxIterator *it, rax *rt);
int raxSeek(raxIterator *it, const char *op, unsigned char *ele, size_t len);
int raxNext(raxIterator *it);
void raxStop(raxIterator *it);
"""
MINI_RAX_C = """\
#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include "rax.h"
#include "rax_malloc.h"
#ifdef BUG_UNWIRED
#undef rax_malloc
#undef rax_realloc
#undef rax_free
#define rax_malloc malloc
#define rax_realloc realloc
#define rax_free free
#endif

void *raxNotFound = (void *)"rax-not-found-pointer";

rax *raxNew(void) {
    rax *t = rax_malloc(sizeof(*t));
    if (t != NULL) { t->items = NULL; t->numele = 0; }
    return t;
}

static uint64_t lookup(rax *t, unsigned char *s, size_t len, int *found) {
    uint64_t i;
    for (i = 0; i < t->numele; i++) {
        raxEntry *e = t->items[i];
        int cmp = memcmp(e->key, s, e->len < len ? e->len : len);
        if (cmp == 0) cmp = (e->len > len) - (e->len < len);
        if (cmp >= 0) { *found = cmp == 0; return i; }
    }
    *found = 0;
    return i;
}

int raxInsert(rax *t, unsigned char *s, size_t len, void *data, void **old) {
    int found;
    uint64_t pos = lookup(t, s, len, &found);
    raxEntry *e, **items = NULL;
    unsigned char *key = NULL;
    errno = 0;
    if (found) {
        if (old != NULL) *old = t->items[pos]->data;
        t->items[pos]->data = data;
        return 0;
    }
    testcase(pos == t->numele);
    e = rax_malloc(sizeof(*e));
    if (e != NULL) key = rax_malloc(len ? len : 1);
#ifdef BUG_CORRUPT
    if (key != NULL) t->numele++;
#endif
    if (key != NULL) items = rax_realloc(t->items, (size_t)(t->numele + 1) * sizeof(*items));
    if (items == NULL) {
#ifndef BUG_LEAK
        rax_free(key);
#endif
        rax_free(e);
        errno = ENOMEM;
        return 0;
    }
#ifdef BUG_CORRUPT
    t->numele--;
#endif
    memcpy(key, s, len);
    e->key = key; e->len = len; e->data = data;
    memmove(items + pos + 1, items + pos, (size_t)(t->numele - pos) * sizeof(*items));
    items[pos] = e;
    t->items = items;
    t->numele++;
    return 1;
}

void *raxFind(rax *t, unsigned char *s, size_t len) {
    int found;
    uint64_t pos = lookup(t, s, len, &found);
    return found ? t->items[pos]->data : raxNotFound;
}

uint64_t raxSize(rax *t) { return t->numele; }

void raxFree(rax *t) {
    uint64_t i;
    for (i = 0; i < t->numele; i++) {
        rax_free(t->items[i]->key);
        rax_free(t->items[i]);
    }
    rax_free(t->items);
    rax_free(t);
}

void raxStart(raxIterator *it, rax *rt) { it->rt = rt; it->pos = 0; it->key = NULL; it->key_len = 0; }
int raxSeek(raxIterator *it, const char *op, unsigned char *ele, size_t len) {
    (void)op; (void)ele; (void)len;
    it->pos = 0;
    return 1;
}
int raxNext(raxIterator *it) {
    raxEntry *e;
    if (it->pos >= it->rt->numele) return 0;
    e = it->rt->items[it->pos++];
    it->key = e->key; it->key_len = e->len; it->data = e->data;
    return 1;
}
void raxStop(raxIterator *it) { (void)it; }
"""


class OomHarnessTest(unittest.TestCase):
    """The C harness and driver of skills/craft-verify, compiled as a reader would use them."""

    def blocks(self):
        """Return (harness, driver): the C blocks of craft-verify that define them, wherever the skill keeps them."""
        text = ""
        for base, _, names in os.walk(os.path.join(ROOT, "skills", "craft-verify")):
            text += "".join(read(os.path.relpath(os.path.join(base, n), ROOT)) for n in sorted(names) if n.endswith(".md"))
        blocks = re.findall(r"```c\n(.*?)```", text, re.S)
        return (next(b for b in blocks if "CRAFT_OOM_HARNESS_H" in b), next(b for b in blocks if "test_insert_oom" in b))

    def build(self, module_files, defines=(), werror=True):
        """Compile the driver (with CRAFT_OOM_IMPL) and `module_files` as separate translation units; return the run."""
        cc = c_compiler()
        if cc is None:
            self.skipTest("no C compiler (cc, gcc, clang or $CRAFT_CC)")
        harness, driver = self.blocks()
        tmp = tempfile.mkdtemp(prefix="craft-oom-")
        try:
            files = dict(module_files, **{
                "craft_oom_harness.h": harness,
                "rax_malloc.h": '#include "craft_oom_harness.h"\n#define rax_malloc craft_malloc\n'
                                '#define rax_realloc craft_realloc\n#define rax_free craft_free\n',
                "test.c": '#define CRAFT_OOM_IMPL\n#include <assert.h>\n#include <errno.h>\n#include <stdio.h>\n'
                          '#include <string.h>\n#include "rax.h"\n#include "craft_oom_harness.h"\n' + driver +
                          '\nint main(void) {\n    test_insert_oom();\n'
                          '    printf("faults=%ld live=%ld\\n", craft_faults, craft_live);\n    return 0;\n}\n'})
            for name, text in files.items():
                with open(os.path.join(tmp, name), "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(text)
            exe = os.path.join(tmp, "oom.exe" if os.name == "nt" else "oom")
            sources = ["test.c"] + [n for n in module_files if n.endswith(".c")]
            flags = ["-std=c99", "-O0", "-Wall", "-Wextra"] + (["-Werror"] if werror else [])
            code, _, err = run(cc + flags + ["-D" + d for d in defines] + ["-o", exe] + sources + ["-lm"], cwd=tmp)
            self.assertEqual(code, 0, err)
            return run([exe], cwd=tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_skill_harness_injects_across_translation_units(self):
        mini = {"rax.h": MINI_RAX_H, "rax.c": MINI_RAX_C}
        for defines in ((), ("CRAFT_COVERAGE",)):
            code, out, err = self.build(mini, defines)
            self.assertEqual((code, out.strip()), (0, "faults=3 live=0"), err)
        # Each fault class must stop the driver: a leak, a corrupted size, an allocator the harness never sees.
        for bug, symptom in (("BUG_LEAK", "craft_live == base"), ("BUG_CORRUPT", "raxSize(t) == n"),
                             ("BUG_UNWIRED", "craft_faults > 0")):
            code, out, err = self.build(mini, (bug,))
            with self.subTest(bug=bug):
                self.assertNotEqual(code, 0, out)
                self.assertIn(symptom, err)

    @unittest.skipUnless(os.environ.get("CRAFT_CORPUS"), "set CRAFT_CORPUS to a folder holding a clone of antirez/rax")
    def test_skill_harness_against_real_rax(self):
        rax_dir = os.path.join(os.environ["CRAFT_CORPUS"], "rax")
        module = {}
        for name in ("rax.c", "rax.h"):
            with open(os.path.join(rax_dir, name), encoding="utf-8") as fh:
                module[name] = fh.read()
        code, out, err = self.build(module, werror=False)
        self.assertEqual(code, 0, err)
        faults, live = map(int, re.match(r"faults=(\d+) live=(-?\d+)", out).groups())
        self.assertGreater(faults, 0)
        self.assertEqual(live, 0)


class CorpusTest(unittest.TestCase):
    """Opt-in checks on real code: CRAFT_CORPUS names a folder with clones of rax, sds, redis and app."""

    def setUp(self):
        self.corpus = os.environ.get("CRAFT_CORPUS")
        if not self.corpus:
            self.skipTest("set CRAFT_CORPUS to a folder holding clones of antirez/rax and antirez/sds")
        self.repo = Repo()

    def tearDown(self):
        self.repo.close()

    def test_quoted_excerpts_are_verbatim(self):
        # The skills present these as antirez's own code: an excerpt that
        # drifted would teach a style he never wrote. Each line must be a line
        # of the upstream file; `[...]` marks what was trimmed.
        excerpts = (("skills/craft-code/SKILL.md", "rax/rax.c", "3a:"),
                    ("skills/craft-code/references/rax-excerpts-c.md", "rax/rax.c", ""),
                    ("skills/craft-design/SKILL.md", "sds/sds.h", "sdshdr8 {"),
                    ("skills/craft-design/references/header-c.md", "rax/rax.h", ""),
                    ("skills/craft-verify/SKILL.md", "rax/rax-test.c", "fuzzTest"))
        for rel, source, marker in excerpts:
            with open(os.path.join(self.corpus, source), encoding="utf-8", errors="replace") as fh:
                upstream = {line.rstrip() for line in fh}
            blocks = [b for b in re.findall(r"```c\n(.*?)```", read(rel), re.S) if marker in b]
            self.assertTrue(blocks, rel)
            for line in "".join(blocks).splitlines():
                if line.strip() and "[...]" not in line:
                    self.assertIn(line.rstrip(), upstream, rel)

    def test_encoding_defaults_match_redis(self):
        # The reference tells readers to treat these as upstream's numbers:
        # they must be upstream's numbers.
        with open(os.path.join(self.corpus, "redis", "redis.conf"), encoding="utf-8", errors="replace") as fh:
            conf = dict(re.findall(r"^([a-z-]+) (-?\d+)$", fh.read(), re.M))
        rows = re.findall(r"^\|[^|]+\| `([a-z-]+)` \| (-?\d+)", read("skills/craft-design/references/encodings.md"), re.M)
        self.assertEqual(len(rows), 11)
        for name, value in rows:
            self.assertEqual(conf.get(name), value, name)
        with open(os.path.join(self.corpus, "redis", "src", "object.c"), encoding="utf-8", errors="replace") as fh:
            self.assertIn("#define OBJ_ENCODING_EMBSTR_SIZE_LIMIT 44", fh.read())

    def test_antirez_code_has_no_hard_findings(self):
        # rax and sds, written by the author the standard is modeled on, must
        # never block a session that adds them: whatever the gate would hold
        # against them is a false positive of the gate.
        self.repo.write("README", "corpus\n")
        self.repo.commit()
        for module, names in (("rax", ("rax.c", "rax.h")), ("sds", ("sds.c", "sds.h"))):
            for name in names:
                shutil.copyfile(os.path.join(self.corpus, module, name), os.path.join(self.repo.path, name))
        changes = gatekeeper.session_changes(self.repo.path, {})
        self.assertEqual(len(changes), 4)
        hard, _ = gatekeeper.collect(self.repo.path, changes)
        self.assertEqual([m for _, m in hard], [])

    def test_review_signals_stay_rare(self):
        # A signal a reference author earns everywhere teaches nothing. Function
        # signals are counted against functions, module signals against modules,
        # RHYTHM against long functions; rax, sds and redis/src are one author's body.
        totals = collections.defaultdict(collections.Counter)
        for repo, paths, group in (("rax", [], "antirez"), ("sds", [], "antirez"), ("redis", ["src"], "antirez"),
                                   ("app", [], "app")):
            code, out, err = script("craft-metrics.py", "--report", *paths, cwd=os.path.join(self.corpus, repo))
            self.assertEqual(code, 0, err)
            data = json.loads(out)
            finds = data["f"]["r"]
            totals[group].update(functions=data["total"]["functions"], modules=data["n"], long=data["total"]["fn40"],
                                 rhythm=sum(f[2] == "RHYTHM" for f in finds),
                                 signalled_functions=len({(f[0], f[3]) for f in finds
                                                          if f[2] in ("RHYTHM", "NO-DOC", "HELPER")}),
                                 signalled_modules=len({f[0] for f in finds
                                                        if f[2] in ("NO-DESIGN", "NO-TEACHER", "NO-LAYOUT-ASSERT")}))
        for group, limit in (("antirez", 0.2), ("app", 0.1)):
            with self.subTest(group=group, totals=dict(totals[group])):
                self.assertLessEqual(totals[group]["signalled_functions"] / totals[group]["functions"], limit)
                self.assertLessEqual(totals[group]["signalled_modules"] / totals[group]["modules"], limit)
        everywhere = totals["antirez"] + totals["app"]
        self.assertLessEqual(everywhere["rhythm"] / everywhere["long"], 0.2)


class ModeTest(unittest.TestCase):
    def test_set_get_session_roundtrip(self):
        home = tempfile.mkdtemp(prefix="craft-mode-")
        mode = posix(os.path.join(BIN, "craft-mode.sh"))
        env = dict(ENV, CLAUDE_CONFIG_DIR=posix(home))
        try:
            def call(*args):
                r = subprocess.run([BASH, mode, *args], capture_output=True, encoding="utf-8", env=env)
                return r.returncode, r.stdout.strip(), r.stderr
            self.assertEqual(call("get"), (0, "full", ""))
            self.assertEqual(call("set", "ULTRA please")[1], "ultra")
            self.assertEqual(call("get")[1], "ultra")
            self.assertIn("mode: ultra.", call("session")[1])
            self.assertIn("never applies to code comments", call("session")[1])
            self.assertEqual(call("set", "bogus")[1], "full")
            self.assertEqual(call("set", "")[1], "full")
            self.assertEqual(call("set", "off")[1], "off")
            self.assertIn("Chat compression is OFF", call("session")[1])
            self.assertEqual(call("nonsense")[0], 1)
            self.assertEqual(call("init")[0], 1)
        finally:
            shutil.rmtree(home)

    def test_command_session_vs_default(self):
        home = tempfile.mkdtemp(prefix="craft-mode-")
        mode = posix(os.path.join(BIN, "craft-mode.sh"))
        env = dict(ENV, CLAUDE_CONFIG_DIR=posix(home))
        try:
            def call(*args):
                r = subprocess.run([BASH, mode, *args], capture_output=True, encoding="utf-8", env=env)
                self.assertEqual((r.returncode, r.stderr), (0, ""), args)
                return r.stdout.strip()
            self.assertEqual(call("command", ""), "mode=full scope=session default=full")
            # Session-only: printed but never persisted.
            self.assertEqual(call("command", "Ultra"), "mode=ultra scope=session default=full")
            self.assertEqual(call("get"), "full")
            self.assertEqual(call("command", "default LITE"), "mode=lite scope=default default=lite")
            self.assertEqual(call("get"), "lite")
            self.assertIn("mode: lite.", call("session"))
            self.assertEqual(call("command", "  default  "), "mode=lite scope=show default=lite")
            self.assertEqual(call("command", "off"), "mode=off scope=session default=lite")
            self.assertEqual(call("command", ""), "mode=lite scope=session default=lite")
            # Bad input: one error line, exit 0, default untouched. "*" checks globbing is off.
            for bad in ("bogus", "default bogus", "full lite", "default full x", "*", "init"):
                self.assertTrue(call("command", bad).startswith("error="), bad)
            self.assertEqual(call("get"), "lite")
        finally:
            shutil.rmtree(home)

    def test_session_directive_is_static_and_carries_the_core(self):
        home = tempfile.mkdtemp(prefix="craft-mode-")
        mode = posix(os.path.join(BIN, "craft-mode.sh"))
        env = dict(ENV, CLAUDE_CONFIG_DIR=posix(home))
        try:
            for m in ("full", "lite", "ultra", "off"):
                subprocess.run([BASH, mode, "set", m], capture_output=True, env=env, check=True)
                runs = [subprocess.run([BASH, mode, "session"], capture_output=True, env=env).stdout for _ in range(2)]
                with self.subTest(mode=m):
                    # The same bytes in every session (prompt cache), within budget.
                    self.assertEqual(runs[0], runs[1])
                    text = runs[0].decode("utf-8")
                    self.assertLessEqual(len(text), 1200)
                    for core in ("Precedence: an explicit user request", "honey-review", "hive-reviewer",
                                 "a redundant comment means Trivial or Backup only", "Locate before reading",
                                 "never from reasoning", "load the craft-standard skill once"):
                        self.assertIn(core, text)
        finally:
            shutil.rmtree(home)

    def test_the_standard_reaches_a_session_with_no_step_in_the_repository(self):
        # Plug and play: installing the plugin is the whole setup. The directive
        # has a session load the standard as a skill, and neither the hook nor
        # /tupelo writes anything into the user's repository.
        home = tempfile.mkdtemp(prefix="craft-mode-")
        mode = posix(os.path.join(BIN, "craft-mode.sh"))
        env = dict(ENV, CLAUDE_CONFIG_DIR=posix(home))
        repo = Repo()
        try:
            def call(*args):
                return subprocess.run([BASH, mode, *args], cwd=repo.path, capture_output=True, encoding="utf-8", env=env).stdout
            self.assertIn("load the craft-standard skill once", call("session"))
            call("command", "")
            self.assertEqual(os.listdir(repo.path), [".git"])
        finally:
            repo.close()
            shutil.rmtree(home)

    def test_the_standard_is_a_skill_a_session_can_load_unprompted(self):
        # Why a skill and not a Read of the plugin's AGENTS.md: in a live session
        # reading a file outside the project asks for permission, while loading
        # a plugin skill does not.
        text = read("skills/craft-standard/SKILL.md")
        self.assertTrue(text.endswith("\n---\n\n" + read("AGENTS.md")))
        # The model must be able to invoke it; a user never needs to.
        self.assertNotIn("disable-model-invocation", text)
        self.assertIn("\nuser-invocable: false\n", text)
        # Why: a skill body runs !`...` as a shell command when it is loaded. In
        # a live session the "!" had to start a word: `//!` and `proptest!` in
        # the other skills arrived as plain text.
        self.assertNotRegex(text, r"(?m)(?:^|\s)!`")


class LayoutTest(unittest.TestCase):
    COMPONENTS = (".claude-plugin", ".cursor", ".agents", "skills", "agents", "hooks", "bin", "scripts", "tests")

    def test_manifests_in_claude_plugin_and_components_at_root(self):
        self.assertEqual(sorted(os.listdir(os.path.join(ROOT, ".claude-plugin"))), ["icon.png", "marketplace.json", "plugin.json"])
        plugin = json.loads(read(".claude-plugin/plugin.json"))
        self.assertEqual(plugin["name"], "tupelo-honey")
        # The repository is its own one-plugin marketplace (README: /plugin
        # install tupelo-honey@provascripts): the entry must point at this
        # root and carry the same name and version as plugin.json.
        market = json.loads(read(".claude-plugin/marketplace.json"))
        self.assertEqual(market["name"], "provascripts")
        self.assertEqual([(e["name"], e["source"], e["version"]) for e in market["plugins"]],
                         [(plugin["name"], "./", plugin["version"])])
        self.assertEqual(market["metadata"]["version"], plugin["version"])
        for name in ("skills", "agents", "hooks", "bin"):
            self.assertTrue(os.path.isdir(os.path.join(ROOT, name)), name)
        # Why no commands/: Claude Code runs a plugin command only with the
        # plugin prefix (/tupelo-honey:tupelo), a plugin skill by its bare
        # name too (/tupelo), so the user-facing commands are skills.
        self.assertFalse(os.path.exists(os.path.join(ROOT, "commands")))

    def test_kebab_case_names(self):
        conventional = {"AGENTS.md", "CLAUDE.md", "SKILL.md"}
        kebab = re.compile(r"^\.?[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[a-z0-9]+)*$")
        for top in self.COMPONENTS:
            self.assertRegex(top, kebab)
            for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, top)):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                for name in dirnames + filenames:
                    if name not in conventional and not name.endswith(".pyc"):
                        self.assertRegex(name, kebab, os.path.join(dirpath, name))

    def test_frontmatter(self):
        for name in os.listdir(os.path.join(ROOT, "skills")):
            self.assertRegex(read("skills/%s/SKILL.md" % name), r"\A---\nname: %s\ndescription: " % re.escape(name))
        for name in os.listdir(os.path.join(ROOT, "agents")):
            text = read("agents/" + name)
            self.assertRegex(text, r"\A---\nname: %s\ndescription: " % re.escape(name[:-3]))
            self.assertRegex(text, r"\ntools: [A-Z][\w, ]+\nmodel: \w+\n---\n")
        for name in ("tupelo", "honey"):
            self.assertRegex(read("skills/%s/SKILL.md" % name),
                             r"\A---\nname: %s\ndescription: .+\nargument-hint: .+\n" % name)
        self.assertRegex(read("skills/honey/SKILL.md"), r"\A---\nname: honey\ndescription: Alias of /tupelo\. ")

    def test_every_skill_keeps_its_budget(self):
        # Why a budget: a SKILL.md enters whole into the context of the session
        # that triggers it, so what only one language needs is a reference file
        # read on demand. The generated craft-standard is AGENTS.md, which a
        # session loads on purpose and which has its own budget.
        for name in os.listdir(os.path.join(ROOT, "skills")):
            if name != "craft-standard":
                size = os.path.getsize(os.path.join(ROOT, "skills", name, "SKILL.md"))
                self.assertLessEqual(size, 6000, name)

    def test_translation_tables_have_a_reference_per_language(self):
        suffix = {"C": "c", "C++": "cpp", "Zig": "zig", "Rust": "rust", "Go": "go", "Python": "python",
                  "TypeScript": "typescript", "Java": "java"}
        tables = 0
        for name in sorted(os.listdir(os.path.join(ROOT, "skills"))):
            m = re.search(r"^## Translation table\n\|[^|]*\|(.*)\|$", read("skills/%s/SKILL.md" % name), re.M)
            if m:
                tables += 1
                languages = [lang.strip() for column in m.group(1).split("|") for lang in column.split("/")]
                self.assertEqual(sorted(languages), sorted(suffix), name)
                files = os.listdir(os.path.join(ROOT, "skills", name, "references"))
                for lang in languages:
                    self.assertTrue([f for f in files if f.endswith("-%s.md" % suffix[lang])], "%s: %s" % (name, lang))
        self.assertEqual(tables, 3)

    def test_skills_point_at_every_reference(self):
        # A reference no SKILL.md names is never read: the skill is the only
        # road to it, by its own name or by its `<topic>-<lang>.md` pattern.
        found = 0
        for name in sorted(os.listdir(os.path.join(ROOT, "skills"))):
            folder = os.path.join(ROOT, "skills", name, "references")
            if os.path.isdir(folder):
                text = read("skills/%s/SKILL.md" % name)
                self.assertIn("Read only the file for the language in use", text, name)
                for ref in os.listdir(folder):
                    found += 1
                    pattern = re.sub(r"-[a-z]+\.md$", "-<lang>.md", ref)
                    self.assertTrue(ref in text or pattern in text, "%s: %s" % (name, ref))
                    lang = re.search(r"-([a-z]+)\.md$", ref)
                    self.assertTrue(ref in text or lang and lang.group(1) in text, "%s: %s" % (name, ref))
        self.assertGreater(found, 30)

    def test_encodings_reference_examples_run(self):
        # The examples teach exact byte widths and limits: they are executed
        # in order, sharing one namespace, so a wrong number fails here.
        text = read("skills/craft-design/references/encodings.md")
        blocks = re.findall(r"```python\n(.*?)```", text, re.S)
        self.assertGreaterEqual(len(blocks), 7)
        namespace = {}
        for block in blocks:
            exec(compile(block, "encodings.md", "exec"), namespace)  # pylint: disable=exec-used
        self.assertEqual([int(n) for n in re.findall(r"^(\d+)\. \*\*", text, re.M)], list(range(1, 11)))
        self.assertIn("references/encodings.md", read("skills/craft-design/SKILL.md"))
        self.assertIn("references/encodings.md", read("skills/craft-verify/SKILL.md"))

    def test_design_check_counts_the_same_thing_everywhere(self):
        # AGENTS.md and craft-design used to count different things: an
        # exhaustive enumeration like rax's insertion cases must pass both.
        for rel in ("AGENTS.md", "skills/craft-design/SKILL.md"):
            text = read(rel)
            self.assertIn("exceptions to the invariant", text, rel)
            self.assertIn("cases of the interface", text, rel)
            self.assertIn("exhaustive enumeration of algorithmic cases is not", text, rel)

    def test_changelog_opens_with_the_plugin_version(self):
        version = json.loads(read(".claude-plugin/plugin.json"))["version"]
        self.assertEqual(re.search(r"^## (\S+)", read("CHANGELOG.md"), re.M).group(1), version)

    def test_icon_is_a_square_png_the_directory_accepts(self):
        # Every PNG opens with its IHDR chunk, big endian, so 24 bytes hold the size:
        #   [signature:8B][chunk length:4B][chunk type:4B][width:4B][height:4B]
        path = os.path.join(ROOT, ".claude-plugin", "icon.png")
        with open(path, "rb") as fh:
            head = fh.read(24)
        self.assertEqual(head[:8], b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", head[16:24])
        self.assertEqual(width, height)
        self.assertTrue(512 <= width <= 2048, width)
        self.assertLess(os.path.getsize(path), 2 * 1024 * 1024)

    def test_hooks_point_at_existing_scripts(self):
        hooks = json.loads(read("hooks/hooks.json"))["hooks"]
        self.assertEqual(sorted(hooks), ["PostToolUse", "SessionStart", "Stop", "UserPromptSubmit"])
        self.assertEqual(hooks["PostToolUse"][0]["matcher"], "Edit|Write|MultiEdit")
        for groups in hooks.values():
            for hook in (h for group in groups for h in group["hooks"]):
                m = re.search(r'"\$\{CLAUDE_PLUGIN_ROOT\}/(bin/[\w.-]+)"', hook["command"])
                self.assertTrue(m and os.path.isfile(os.path.join(ROOT, m.group(1))), hook["command"])
        # One Stop gate runs every scanner; SessionStart takes its snapshot.
        self.assertEqual([h["command"].split("/bin/")[1] for h in hooks["Stop"][0]["hooks"]], ['craft-gate.py"'])
        self.assertTrue(any("craft-gate.py\" --snapshot" in h["command"] for h in hooks["SessionStart"][0]["hooks"]))
        # The next prompt hands Claude the review signals a Stop queued.
        self.assertEqual([h["command"].split("/bin/")[1] for h in hooks["UserPromptSubmit"][0]["hooks"]],
                         ['craft-gate.py" --prompt'])

    def test_scripts_run_by_bare_name(self):
        # Claude Code puts the plugin's bin/ on the Bash tool's PATH, so a
        # script runs by its bare name only if it is executable and its first
        # line is a shebang ending in LF (`env python3\r` names no program).
        # The git index carries the mode to every clone, Windows checkouts
        # included, which have no exec bit to look at; craftlib.py is a
        # module, imported and never run.
        out = subprocess.run(["git", "ls-files", "-s", "--", "bin"], cwd=ROOT, capture_output=True, text=True)
        modes = {}
        for line in out.stdout.splitlines() if out.returncode == 0 else []:
            meta, rel = line.split("\t", 1)
            modes[rel] = meta.split()[0] == "100755"
        if not modes:
            if os.name != "posix":
                self.skipTest("no git index and no exec bits on this filesystem")
            bin_dir = os.path.join(ROOT, "bin")
            modes = {"bin/" + n: os.access(os.path.join(bin_dir, n), os.X_OK) for n in os.listdir(bin_dir)
                     if os.path.isfile(os.path.join(bin_dir, n))}
        scripts = [rel for rel in modes if re.fullmatch(r"bin/craft-[\w-]+\.(?:sh|py)", rel)]
        self.assertGreaterEqual(len(scripts), 6)
        for rel, executable in sorted(modes.items()):
            with open(os.path.join(ROOT, rel), "rb") as fh:
                first = fh.readline()
            if rel in scripts:
                shebang = b"#!/usr/bin/env bash\n" if rel.endswith(".sh") else b"#!/usr/bin/env python3\n"
                self.assertEqual((first, executable), (shebang, True), rel)
            else:
                self.assertEqual((first.startswith(b"#!"), executable), (False, False), rel + " is a module")

    def test_skills_and_agents_call_scripts_by_name(self):
        # The model's Bash tool has no CLAUDE_PLUGIN_ROOT in its environment;
        # Claude Code substitutes the variable only in the Markdown it loads.
        # A bare name works everywhere bin/ is on the PATH, so every command
        # the model is told to run starts with one; the path survives only in
        # prose, as the fallback when the name is not found, and in the
        # /tupelo `!` line, which Claude Code itself runs after substituting.
        sources = [os.path.join("agents", n) for n in sorted(os.listdir(os.path.join(ROOT, "agents")))]
        for skill in sorted(os.listdir(os.path.join(ROOT, "skills"))):
            for base, _, names in os.walk(os.path.join(ROOT, "skills", skill)):
                sources += [os.path.relpath(os.path.join(base, n), ROOT) for n in sorted(names) if n.endswith(".md")]
        scanners = {"craft-debt-scan.sh", "craft-comment-sync.py", "craft-metrics.py"}
        for rel in sources:
            text = read(rel)
            cited = set()
            for block in re.findall(r"^\s*```[\w-]*\n(.*?)^\s*```", text, re.S | re.M):
                for line in block.splitlines():
                    if re.search(r"\bcraft-[\w-]+\.(?:sh|py)\b", line):
                        m = re.match(r"\s*(craft-[\w-]+\.(?:sh|py))\s", line)
                        self.assertTrue(m, "%s runs a script by path: %s" % (rel, line.strip()))
                        self.assertTrue(os.path.isfile(os.path.join(ROOT, "bin", m.group(1))), m.group(1))
                        cited.add(m.group(1))
            for line in text.splitlines():
                if "${CLAUDE_PLUGIN_ROOT}/bin/" in line:
                    self.assertTrue(line.startswith("Mode: !`") or "not found" in line, "%s: %s" % (rel, line))
            if os.path.basename(rel) == "hive-craft-reviewer.md" or rel.replace(os.sep, "/") == "skills/craft-review/SKILL.md":
                self.assertEqual(cited & scanners, scanners, rel)

    def test_agents_md_budget_and_claude_md_import(self):
        # AGENTS.md is what users receive (the craft-standard skill, the mirrors): it must
        # not carry this repository's own maintenance rules, which live in
        # CONTRIBUTING.md; the repository's CLAUDE.md imports both.
        self.assertLess(len(read("AGENTS.md").splitlines()), 200)
        self.assertNotIn("This Repository", read("AGENTS.md"))
        self.assertIn("Generated mirrors, never edited by hand", read("CONTRIBUTING.md"))
        self.assertEqual(read("CLAUDE.md").split(), ["@AGENTS.md", "@CONTRIBUTING.md"])

    def test_agents_md_keeps_the_nine_kinds_and_verbatim_blocks(self):
        # A guard against lossy rewrites (honey-compress or a hand edit): the
        # nine kinds stay distinct and in antirez's order, code blocks intact.
        text = read("AGENTS.md")
        self.assertEqual(re.findall(r"^\d\. \*\*(\w+)\*\*", text, re.M),
                         ["Function", "Design", "Why", "Teacher", "Checklist", "Guide", "Trivial", "Debt", "Backup"])
        self.assertEqual(text.count("```") % 2, 0)
        self.assertIn('   Node "ANNIBALE" -> "SCO" -> [], inserting "ANNIENTARE".\n'
                      '   $SPLITPOS = 4 (first mismatch: \'B\' in node, \'E\' in key).\n', text)

    def test_mirrors_in_sync(self):
        code, _, err = run([BASH, posix(os.path.join(ROOT, "scripts", "sync-mirrors.sh")), "--check"])
        self.assertEqual(code, 0, err)
        # Codex and Gemini CLI load AGENTS.md by themselves: a skill would be a second copy.
        self.assertFalse(os.path.exists(os.path.join(ROOT, ".agents", "skills", "craft-standard")))
        rule = read(".cursor/rules/systems-craft.mdc")
        self.assertIn("\nalwaysApply: true\n", rule)
        # Cursor runs no Claude Code hooks: the rule points at the git gate instead.
        for absent in ("This Repository", "PostToolUse", "### Claude Code hooks", "Zero-token hooks"):
            self.assertNotIn(absent, rule)
        self.assertIn("craft-gate.py --staged", rule)

    def test_repository_passes_its_own_scanners(self):
        self.assertEqual(debt("--quiet", cwd=ROOT), (0, "", ""))
        code, out, _ = script("craft-metrics.py", "--report", "bin", "tests", cwd=ROOT)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["f"]["r"], [])


if __name__ == "__main__":
    print("seed %d (replay with CRAFT_SEED=%d)" % (SEED, SEED), file=sys.stderr)
    unittest.main(verbosity=1)
