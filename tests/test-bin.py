#!/usr/bin/env python3
"""Runnable tests for tupelo-honey: bin/ scripts, hook wiring, plugin layout and mirrors.

Run: python3 tests/test-bin.py   (needs git and bash on PATH; on Windows run it
from Git Bash, so that `bash` is Git's and not the WSL launcher).

The randomized tests compare the scripts against oracles built by their own
generators: the generator knows which line is a violation, which token is
code and which is comment. They are seeded, report the seed on failure, and
replay with CRAFT_SEED=<seed>.
"""

import hashlib
import importlib.util
import json
import os
import random
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
sys.dont_write_bytecode = True
sys.path.insert(0, BIN)
import craftlib as cl  # noqa: E402

SEED = int(os.environ.get("CRAFT_SEED") or random.SystemRandom().randrange(1 << 30))
BASH = os.environ.get("CRAFT_BASH") or shutil.which("bash")
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
# Why: written literally, these markers would make craft-debt-scan.sh flag
# this very file when it scans the repository, so the fixtures assemble them.
TODO, FIXME, SHORTCUT, HONEY = "TO" + "DO", "FIX" + "ME", "short" + "cut:", "hon" + "ey:"


def load_script(name):
    """Import bin/<name>.py as a module (the hyphen rules out a plain import)."""
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), os.path.join(BIN, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


metrics = load_script("craft-metrics")
sync = load_script("craft-comment-sync")


def run(args, cwd=None, stdin=None):
    """Run a command; return (exit code, stdout, stderr) as text."""
    r = subprocess.run(args, cwd=cwd, input=stdin, capture_output=True, encoding="utf-8",
                       errors="replace", env=ENV)
    return r.returncode, r.stdout, r.stderr


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
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
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
        run(["git", "add", "-A"], cwd=self.path)
        code, _, err = run(["git", "-c", "user.email=test@example.com", "-c", "user.name=test",
                            "-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture"], cwd=self.path)
        assert code == 0, err

    def close(self):
        def retry(func, path, _exc):
            os.chmod(path, stat.S_IWRITE)
            func(path)
        if sys.version_info >= (3, 12):
            shutil.rmtree(self.path, onexc=retry)
        else:
            shutil.rmtree(self.path, onerror=retry)


class DebtScanTest(unittest.TestCase):
    def setUp(self):
        self.repo = Repo(git=False)

    def tearDown(self):
        self.repo.close()

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
                * {FIXME} broken
            // {HONEY} O(n^2) under 1k items; switch to a hash if n > 5000
            int {TODO}S = 1; // {TODO}LIST stays a word
            """)
        self.repo.write("notes.md", f"# {TODO} prose is never scanned\n")
        code, out, _ = debt(cwd=self.repo.path)
        self.assertEqual(code, 1, out)
        found = sorted(re.findall(r"^\[([A-Z-]+)\] \./(\S+?):(\d+):", out, re.M))
        self.assertEqual(found, sorted([
            ("TODO", "bad.c", "1"), ("NO-CEILING", "bad.c", "2"), ("NO-TRIGGER", "bad.c", "3"),
            ("NO-TRIGGER", "bad.c", "4"), ("TRACKED", "bad.c", "5"), ("TRACKED", "ok.py", "1"),
            ("TRACKED", "ok.py", "2")]))
        self.assertIn("4 violation(s), 3 tracked marker(s)", out)

    def test_quiet_is_silent_when_clean(self):
        self.repo.write("ok.py", f"# {SHORTCUT} fixed 4 KiB buffer; grow it if a frame exceeds 4096 bytes\n")
        self.assertEqual(debt("--quiet", cwd=self.repo.path), (0, "", ""))

    def test_hook_reports_only_lines_changed_since_head(self):
        repo = Repo()
        try:
            path = repo.write("a.c", f"// {TODO}: legacy marker, not this edit's business\nint a;\n")
            repo.commit()
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(f"// {FIXME} breaks things\nint b;\n")
            code, out, err = debt("--hook", stdin=edit_payload(path, old_string="", new_string="x"))
            self.assertEqual((code, out), (2, ""), err)
            self.assertRegex(err, r"\[NO-TRIGGER\] \S*a\.c:3:")
            self.assertNotRegex(err, r"a\.c:1:")
        finally:
            repo.close()

    def test_hook_scans_untracked_files_whole(self):
        path = self.repo.write("new.c", f"int x;\n// {TODO}: later\n")
        code, _, err = debt("--hook", stdin=edit_payload(path, old_string="", new_string="int x;"))
        self.assertEqual(code, 2)
        self.assertRegex(err, r"\[TODO\] \S*new\.c:2:")

    def test_comment_openers_follow_the_language(self):
        for name, text in {"a.lisp": f"; {TODO}: lisp", "a.erl": f"% {FIXME} broken", "a.f90": f"! {TODO} fortran",
                           "a.vb": f"' {TODO} vb\nREM {FIXME} oops", "a.hs": f"-- {TODO} hs\n{{- {FIXME} nope -}}",
                           "a.ml": f"(* {TODO} ocaml *)", "a.html": f"<!-- {TODO} html -->", "a.lua": f"-- {TODO} lua",
                           "b.py": f"x = '%s' % {TODO}\ny = 1 ; {TODO}", "b.c": f"int x = a ! {TODO};\nfor (;;) ; {TODO}"
                           }.items():
            self.repo.write(name, text + "\n")
        code, out, _ = debt("--quiet", cwd=self.repo.path)
        found = sorted(re.findall(r"^\[[A-Z-]+\] \./(\S+?):(\d+):", out, re.M))
        self.assertEqual(found, [("a.erl", "1"), ("a.f90", "1"), ("a.hs", "1"), ("a.hs", "2"), ("a.html", "1"),
                                 ("a.lisp", "1"), ("a.lua", "1"), ("a.ml", "1"), ("a.vb", "1"), ("a.vb", "2")])
        self.assertEqual(code, 1)

    def test_hook_skips_the_scanner_itself(self):
        path = self.repo.write("craft-debt-scan.sh", f"#   {SHORTCUT} <quantified ceiling>; <revisit trigger>\n")
        self.assertEqual(debt("--hook", stdin=edit_payload(path, old_string="", new_string="x")), (0, "", ""))

    def test_hook_ignores_prose_missing_files_and_empty_input(self):
        path = self.repo.write("notes.md", f"# {TODO}\n")
        self.assertEqual(debt("--hook", stdin=edit_payload(path))[0], 0)
        self.assertEqual(debt("--hook", stdin=edit_payload(path + ".gone.c"))[0], 0)
        self.assertEqual(debt("--hook", stdin="")[0], 0)

    def test_random_markers_match_oracle(self):
        rng = random.Random(SEED)
        words = ["scan", "cache", "lookup", "table", "linear", "global", "lock", "buffer", "small", "path"]
        triggers = ["; switch", "if it grows", "when it grows", "once it grows", "unless sorted", "until sorted",
                    "after the port", "upon reload", "over budget", "above budget", "past budget",
                    "exceeds budget", "beyond budget", "n > limit"]
        lines, expected = [], set()
        for no in range(1, 301):
            kind = rng.choice(["shortcut", "honey", "fixme", "todo", "plain"])
            ceiling, trigger = rng.random() < 0.5, rng.random() < 0.5
            text = " ".join(rng.choice(words) for _ in range(rng.randint(1, 4)))
            text += (" under %d items" % rng.randint(1, 99999)) if ceiling else ""
            text += (" " + rng.choice(triggers)) if trigger else ""
            prefix = rng.choice(["#", "//", "--", "/*", " *"])
            lead = "x = 1  " if prefix != " *" and rng.random() < 0.3 else ""
            marker = {"shortcut": SHORTCUT, "honey": HONEY, "fixme": FIXME + ":", "todo": TODO + ":", "plain": ""}[kind]
            lines.append("%s%s %s %s" % (lead, prefix, marker, text))
            if (kind == "todo" or (kind == "fixme" and not trigger)
                    or (kind in ("shortcut", "honey") and not (ceiling and trigger))):
                expected.add(no)
        self.repo.write("gen.c", "\n".join(lines) + "\n")
        code, out, _ = debt("--quiet", cwd=self.repo.path)
        found = {int(n) for n in re.findall(r"^\[[A-Z-]+\] \./gen\.c:(\d+):", out, re.M)}
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


class CommentSyncTest(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()

    def tearDown(self):
        self.repo.close()

    def check_diff(self):
        return script("craft-comment-sync.py", "--check-diff", cwd=self.repo.path)

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
            int other(int count, int *node) {
                // free(node);

                // increment count
                count++;
                count++; /* increment count */
                /* 1: Save next pointer. */
                int next = node[1];
                // Why: count before free, the callback reads it.
                count++;
                // Free the query buffer.
                free(node);
                node = 0;
                count = 0;
                next = 0;
                /* Return 0 on OOM. */
                return 0;
            }
            """)
        code, out, _ = self.check_diff()
        self.assertEqual(code, 1, out)
        # Lines 1-4 are the committed legacy code (its "// count++;" is not this
        # change's business), line 5 is blank, the new function starts at 6.
        self.assertEqual(re.findall(r"^m\.c:(\d+): \[([A-Z]+)\]", out, re.M),
                         [("7", "BACKUP"), ("9", "TRIVIAL"), ("11", "TRIVIAL")])

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

    def test_hook_audits_the_edit_without_git(self):
        plain = Repo(git=False)
        try:
            path = plain.write("codec.c", """
                /* parse_header() reads the 5-byte frame header. */
                int read_header(int x) { return x; }
                // free(buf);
                """)
            data = edit_payload(path, old_string="int parse_header(int x) { return x; }",
                                new_string="int read_header(int x) { return x; }\n// free(buf);")
            code, out, err = script("craft-comment-sync.py", "--hook", stdin=data)
            self.assertEqual((code, out), (2, ""), err)
            self.assertIn("[ORPHAN] 'parse_header'", err)
            self.assertIn("[BACKUP] commented-out code", err)
        finally:
            plain.close()

    def test_hook_lexes_the_old_side_in_context(self):
        # Regression: an old_string cut from a docstring was lexed as code and
        # every ALL_CAPS word in it became a false ORPHAN.
        plain = Repo(git=False)
        try:
            path = plain.write("doc.py", '"""Findings:\n  [BACKUP] commented-out code.\n"""\nX = 1\n')
            data = edit_payload(path, old_string="  [BACKUP] commented-out code.\n  [TRIVIAL] restates.",
                                new_string="  [BACKUP] commented-out code.")
            self.assertEqual(script("craft-comment-sync.py", "--hook", stdin=data), (0, "", ""))
        finally:
            plain.close()

    def test_hook_uses_original_file_for_deletions(self):
        plain = Repo(git=False)
        try:
            path = plain.write("lib.py", "# old_parse() is kept for the CLI.\nX = 1\n")
            data = json.loads(edit_payload(path, old_string="def old_parse():\n    return 1\n", new_string=""))
            data["tool_response"] = {"originalFile": "# old_parse() is kept for the CLI.\nX = 1\n"
                                                     "def old_parse():\n    return 1\n"}
            code, _, err = script("craft-comment-sync.py", "--hook", stdin=json.dumps(data))
            self.assertEqual(code, 2, err)
            self.assertIn("lib.py:1: [ORPHAN] 'old_parse'", err)
        finally:
            plain.close()

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
            mutation, i = rng.choice(["none", "rename", "swap", "resize"]), rng.randrange(len(fields) - 1)
            if mutation == "rename":
                cells[i] = "[ghost%d:%s" % (i, cells[i].split(":", 1)[1])
            elif mutation == "swap":
                cells[i], cells[i + 1] = cells[i + 1], cells[i]
            elif mutation == "resize":
                cells[i] = "[%s:%dB]" % (fields[i][0], fields[i][2] // 4)
            text = "/* Layout:\n *   %s\n */\nstruct s%d {\n%s};\n" % (
                "".join(cells), trial, "".join("    %s %s;\n" % (t, fname) for fname, t, _ in fields))
            found = {re.search(r": \[([A-Z-]+)\]", f).group(1)
                     for f in sync.diagram_findings(cl.scan(text, "c"), "c", set(range(1, 99)), "x.c")}
            expected = {"none": set(), "rename": {"DIAGRAM-FIELD"}, "swap": {"DIAGRAM-ORDER"},
                        "resize": {"DIAGRAM-SIZE"}}[mutation]
            self.assertEqual(found, expected, "seed %d trial %d: %s" % (SEED, trial, text))

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


class MetricsTest(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()
        self.sessions = []

    def tearDown(self):
        self.repo.close()
        for session in self.sessions:
            state = os.path.join(tempfile.gettempdir(),
                                 "craft-gate-%s.txt" % hashlib.sha1(session.encode()).hexdigest()[:16])
            if os.path.exists(state):
                os.remove(state)

    def gate(self, session=None, active=False, cwd=None):
        session = session or str(uuid.uuid4())
        self.sessions.append(session)
        data = json.dumps({"hook_event_name": "Stop", "session_id": session, "stop_hook_active": active,
                           "cwd": cwd or self.repo.path})
        return script("craft-metrics.py", "--gate", cwd=cwd or self.repo.path, stdin=data)

    def test_gate_reports_new_violations_once_per_session(self):
        self.repo.write("core.c", "int base(int x) { return x; }\n")
        self.repo.commit()
        self.repo.write("core.c", c_function("helper", 5, static=True)
                        + "int base(int x) { return helper(x); }\n" + c_function("pipeline", 45))
        session = str(uuid.uuid4())
        code, out, err = self.gate(session)
        self.assertEqual((code, out), (2, ""), err)
        self.assertIn("[RHYTHM] 'pipeline' has 45 LOC", err)
        self.assertIn("[HELPER] 'helper' (5 LOC) has a single caller", err)
        self.assertEqual(self.gate(session), (0, "", ""))
        self.assertEqual(self.gate()[0], 2)
        self.assertEqual(self.gate(active=True), (0, "", ""))

    def test_gate_accepts_guides_and_why(self):
        self.repo.write("core.c", "int base(int x) { return x; }\n")
        self.repo.commit()
        helper = "/* Why: kept apart so the OOM harness can fail it directly. */\n" + c_function("helper", 5, static=True)
        pipeline = "/* Run the three stages on x; never fails. */\n" + c_function("pipeline", 60, guide_every=20)
        self.repo.write("core.c", helper + "int base(int x) { return helper(x); }\n" + pipeline)
        self.assertEqual(self.gate(), (0, "", ""))

    def test_gate_requires_function_design_and_teacher_comments(self):
        self.repo.write("README", "fixture\n")
        self.repo.commit()
        undocumented = "\n".join(["#include <stdint.h>", "", "struct hdr {", "    uint8_t len;", "    uint8_t flags:3;",
                                  "    char buf[];", "};", ""]) + c_function("hdr_parse", 20)
        self.repo.write("hdr.c", undocumented)
        self.repo.write("tool.py", '"""Tool module: one public entry point."""\n\n\ndef api(x):\n'
                        '    """Return x doubled, plus one."""\n    y = x * 2\n    y += 1\n    return y\n')
        code, _, err = self.gate()
        self.assertEqual(code, 2, err)
        self.assertEqual(sorted(re.findall(r"^(\S+?):\d+: \[([A-Z-]+)\]", err, re.M)),
                         [("hdr.c", "NO-DESIGN"), ("hdr.c", "NO-DOC"), ("hdr.c", "NO-TEACHER")])
        self.assertIn("hdr.c:5: [NO-TEACHER] layout-sensitive code (uint8_t flags:3;)", err)

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
        self.assertEqual(self.gate(), (0, "", ""))

    def test_gate_dmod_only_for_new_modules(self):
        self.repo.write("README", "fixture\n")
        self.repo.commit()
        self.repo.write("tiny.c", "int a(void) { return 1; }\nint b(void) { return 2; }\nint c(void) { return 3; }\n")
        code, _, err = self.gate()
        self.assertEqual(code, 2)
        self.assertIn("tiny.c:1: [DMOD] D_mod 1.0 < 15 (3 LOC / 3 public functions)", err)

    def test_gate_layout_assertions(self):
        self.repo.write("README", "fixture\n")
        self.repo.commit()
        for name, text in LAYOUT_FIXTURES.items():
            self.repo.write(name, text)
        code, _, err = self.gate()
        self.assertEqual(code, 2, err)
        self.assertEqual(sorted(re.findall(r"^(\S+?):\d+: \[NO-LAYOUT-ASSERT\] struct '(\w+)'", err, re.M)),
                         [("cells.c", "frame"), ("hdr.py", "Hdr"), ("packed.c", "sdshdr8"), ("repr.rs", "Hdr")])

    def test_gate_is_silent_outside_git(self):
        plain = Repo(git=False)
        try:
            plain.write("big.c", c_function("pipeline", 80))
            self.assertEqual(self.gate(cwd=plain.path), (0, "", ""))
        finally:
            plain.close()

    def test_report_json(self):
        plain = Repo(git=False)
        try:
            plain.write("mod.c", c_function("helper", 5, static=True)
                        + "int api_one(int x) { return helper(x); }\n" + c_function("api_two", 45))
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
        finally:
            plain.close()

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
            for bad in ("bogus", "default bogus", "full lite", "default full x", "*"):
                self.assertTrue(call("command", bad).startswith("error="), bad)
            self.assertEqual(call("get"), "lite")
        finally:
            shutil.rmtree(home)


class LayoutTest(unittest.TestCase):
    COMPONENTS = (".claude-plugin", ".cursor", ".agents", "skills", "agents", "commands", "hooks", "bin",
                  "scripts", "tests")

    def test_manifest_alone_in_claude_plugin_and_components_at_root(self):
        self.assertEqual(os.listdir(os.path.join(ROOT, ".claude-plugin")), ["plugin.json"])
        self.assertEqual(json.loads(read(".claude-plugin/plugin.json"))["name"], "tupelo-honey")
        for name in ("skills", "agents", "commands", "hooks", "bin"):
            self.assertTrue(os.path.isdir(os.path.join(ROOT, name)), name)

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
        self.assertRegex(read("commands/honey.md"), r"\A---\ndescription: .+\nargument-hint: ")

    def test_hooks_point_at_existing_scripts(self):
        hooks = json.loads(read("hooks/hooks.json"))["hooks"]
        self.assertEqual(sorted(hooks), ["PostToolUse", "SessionStart", "Stop"])
        self.assertEqual(hooks["PostToolUse"][0]["matcher"], "Edit|Write|MultiEdit")
        for groups in hooks.values():
            for hook in (h for group in groups for h in group["hooks"]):
                m = re.search(r'"\$\{CLAUDE_PLUGIN_ROOT\}/(bin/[\w.-]+)"', hook["command"])
                self.assertTrue(m and os.path.isfile(os.path.join(ROOT, m.group(1))), hook["command"])

    def test_agents_md_budget_and_claude_md_import(self):
        self.assertLess(len(read("AGENTS.md").splitlines()), 200)
        self.assertEqual(read("CLAUDE.md").strip(), "@AGENTS.md")

    def test_mirrors_in_sync(self):
        code, _, err = run([BASH, posix(os.path.join(ROOT, "scripts", "sync-mirrors.sh")), "--check"])
        self.assertEqual(code, 0, err)
        self.assertIn("\nalwaysApply: true\n", read(".cursor/rules/systems-craft.mdc"))

    def test_repository_passes_its_own_scanners(self):
        self.assertEqual(debt("--quiet", cwd=ROOT), (0, "", ""))
        code, out, _ = script("craft-metrics.py", "--report", "bin", "tests", cwd=ROOT)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["f"]["r"], [])


if __name__ == "__main__":
    print("seed %d (replay with CRAFT_SEED=%d)" % (SEED, SEED), file=sys.stderr)
    unittest.main(verbosity=1)
