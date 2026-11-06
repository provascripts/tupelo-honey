#!/usr/bin/env python3
"""craft-metrics.py -- module depth, comment coverage and single-caller helpers, measured.

Usage:
  craft-metrics.py --report [PATH...]  minified columnar JSON for PATHs (default:
                                       every code file of the git repository, or
                                       under the current directory outside git)
  craft-metrics.py --gate              Stop hook: judge only what changed since HEAD

Metrics (LOC = non-blank, non-comment lines; test files are exempt from all
but RHYTHM):
  DMOD        implementation LOC / public functions of a file; target >= 15.
              Gated only for new non-header files with >= 2 public functions:
              a single entry point is already as narrow as an interface gets.
  RHYTHM      in a function of >= 40 LOC, the longest run of code lines without
              a standalone comment line must stay <= 25 (a Guide comment every
              15-25 LOC). Lines inside multi-line string literals never count
              toward a run.
  HELPER      a module-level internal function under 30 LOC referenced exactly
              once besides its definition: inline it under a Guide comment, or
              justify it with a "Why:" comment (semantic boundary, resource
              ownership, removed duplication, test seam).
  NO-DOC      a public function of >= 3 LOC without a Function comment (the
              comment block above it, a docstring, an Elixir @doc). `main`,
              @Override methods and Rust trait impls inherit their contract.
  NO-DESIGN   a module of >= 20 LOC that does not open with a comment block of
              >= 4 words: Design notes, or a pointer to the module's Design
              comment (Elixir @moduledoc counts).
  NO-TEACHER  layout-sensitive code (bitfields, packed structs, flexible array
              members, header-behind-pointer arithmetic, #[repr], pack/unpack,
              DataView, ByteBuffer, Erlang bit syntax...) with no
              diagram-bearing comment (a [..][..] strip, a -> chain, a 0..3
              byte-offset table) from 80 lines above to 5 below.
  NO-LAYOUT-ASSERT
              a struct that describes a physical memory layout ([field:size]
              cells or byte offsets in its diagram, or a packed, #[repr],
              extern, [StructLayout] or ctypes marker) with no compile-time or
              test assertion on its size or offsets anywhere in the repository,
              so the compiler cannot verify the diagram. Logical diagrams
              (trees, -> chains, state machines) never trigger it.

Function extraction per language: Python by ast; brace languages (C family,
Java, C#, Kotlin, Swift, Scala, Groovy, Dart, Solidity, PHP, Go, Rust, Zig,
JS/TS, shell, Perl, R, PowerShell) by matching {}; Ruby, Crystal, Elixir,
Lua, Julia, Nim, CoffeeScript, OCaml and F# by indentation; the Lisp family
by matching parentheses. SQL, Haskell, Erlang, Fortran, VB and Tcl get the
comment checks only. The scripts check presence, never quality.

--gate reports each finding once per session (stderr, exit 2) and stays quiet
while Claude is already continuing because of a Stop hook (stop_hook_active),
so it cannot loop. Outside git it exits 0: without a baseline it cannot tell
new code from legacy code. See the design notes in craftlib.py.
"""

import argparse
import ast
import bisect
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter, namedtuple

import craftlib as cl

GUIDE_EVERY, LONG_FN, SHALLOW_FN, MIN_DMOD = 25, 40, 30, 15
DOC_MIN_LOC, DESIGN_MIN_LOC, TEACHER_ABOVE, TEACHER_BELOW = 3, 20, 80, 5
MAX_FILES = 5000

# line: signature line; start: first line of the declaration (decorators,
# attributes, a return type on its own line); first..last: body lines,
# 1-based and inclusive; container: 'module', 'class' or 'func'; doc: has a
# Function comment or inherits one.
Func = namedtuple("Func", "name line start first last public why container doc")

WHY = re.compile(r"\bwhy\s*:", re.I)
DIAGRAM = re.compile(r"\][\[(]|\]\s+\[|->|\d\s*\.\.\s*\d|\b[a-z]\s*[+-]\s*\d+\s+\w")
STRING_ONLY = re.compile(r'"+\)?')
LAYOUT = {
    "c": re.compile(r"^\s*(?:unsigned|signed|int|char|short|long|bool|_Bool|u?int\d+_t)\b[\w\s*]*\b\w+\s*:\s*\d+\s*;"
                    r"|__packed__|__attribute__\s*\(\(\s*packed|#\s*pragma\s+pack"
                    r"|^\s*[\w\s*]+\b\w+\s*\[\s*\]\s*;|-\s*sizeof\s*\(|\[\s*-\s*\d+\s*\]"
                    r"|\bByteOrder\.\w+_ENDIAN\b"),
    "cs": re.compile(r"\[StructLayout\(|\bfixed\s+\w+\s+\w+\s*\[|\bMarshal\.(?:SizeOf|OffsetOf)\b|\bBinaryPrimitives\."),
    "kt": re.compile(r"\bMemoryLayout\s*<|\bwithUnsafe\w*Bytes\b|\bByteBuffer\b|\bByteOrder\.\w+_ENDIAN\b|\bByteData\b"),
    "rs": re.compile(r"#\[repr\("),
    "zig": re.compile(r"\b(?:packed|extern)\s+struct\b"),
    "go": re.compile(r"\bunsafe\.(?:Pointer|Offsetof|Sizeof)\b|\bbinary\.(?:Little|Big)Endian\b"),
    "py": re.compile(r"\bstruct\.(?:Struct|pack|unpack|pack_into|unpack_from)\b|\bctypes\.Structure\b"
                     r"|\b_fields_\s*=|\bnp\.dtype\s*\("),
    "js": re.compile(r"\bnew\s+DataView\b|\.(?:get|set)(?:Ui|I)nt(?:8|16|32)\b|\.(?:read|write)U?Int(?:8|16|32)(?:LE|BE)?\b"),
    "php": re.compile(r"\b(?:un)?pack\s*\("),
    "sh": re.compile(r"\.(?:un)?pack1?\s*\(|\b(?:un)?pack\s*\(\s*[\"']"),
    "lua": re.compile(r"\bstring\.(?:un)?pack\b"),
    "nim": re.compile(r"\{\.\s*packed\s*\.\}|\bcast\["),
    "jl": re.compile(r"\bunsafe_load\b|\breinterpret\s*\("),
    "erl": re.compile(r"<<[^>]*:\d+"),
    "hs": re.compile(r"\b(?:peek|poke)ByteOff\b|\bStorable\b"),
}
# Size or offset queries that, inside an assertion, let the compiler or the
# test suite check a layout diagram; {0} is the struct name.
ASSERT_QUERY = {
    "c": r"\b(?:sizeof|offsetof|alignof|_Alignof)\s*\(\s*(?:struct\s+|union\s+)?{0}\b",
    "cs": r"\b(?:sizeof|Marshal\.SizeOf|Marshal\.OffsetOf|Unsafe\.SizeOf)\s*(?:<\s*{0}\s*>|\(\s*(?:typeof\s*\(\s*)?{0}\b)",
    "rs": r"\b(?:size_of|align_of)\s*::\s*<\s*{0}\b|\boffset_of!\s*\(\s*{0}\b",
    "zig": r"@(?:sizeOf|offsetOf|bitSizeOf|bitOffsetOf|alignOf)\s*\(\s*{0}\b",
    "go": r"\bunsafe\.(?:Sizeof|Offsetof|Alignof)\s*\([^)]*\b{0}\b",
    "kt": r"\bMemoryLayout\s*<\s*{0}\s*>",
    "py": r"\bctypes\.(?:sizeof|alignment)\s*\(\s*{0}\b|\b{0}\.\w+\.offset\b",
}
ASSERTION = re.compile(r"assert|expect|require|check|verify|fatal|panic|comptime|const\s+_|[=!]=", re.I)

SIMPLE_NAME = re.compile(r"^[A-Za-z_]\w*$")
TEST_PATH = re.compile(r"(?:^|/)(?:tests?|__tests__|specs?)/|(?:^|/)test[_-][^/]*$|[_.-](?:test|spec)\.\w+$|Tests?\.java$")
HEADER_EXT = (".h", ".hh", ".hpp")
SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", "target", "out", ".venv", "venv",
             "__pycache__", ".tox", ".mypy_cache", ".pytest_cache", ".next", ".cache"}
CLASSY = re.compile(r"^\s*(?:(?:pub|export|default|public|private|protected|internal|abstract|final|static|sealed|"
                    r"unsafe|typedef|data|open|inner|fileprivate|partial|readonly|ref)(?:\([^)]*\))?\s+)*"
                    r"(?:class|struct|interface|impl|trait|enum|union|namespace|record|object|extension|protocol|"
                    r"contract|library|mixin)\b")
NOT_FUNC = set("if for while switch catch return sizeof else do try synchronized foreach using lock with new "
               "delete typeof defined alignof decltype static_assert assert elif when match function func fn "
               "constexpr".split())
ATTRIBUTES = {"__attribute__", "__attribute", "__declspec", "alignas", "_Alignas"}

# Brace languages: the signature recognizer per extension ("c" means
# c_signature, the identifier before the first top-level parenthesis).
BRACE = {}
for _key, _exts in {
    "c": ".c .h .cc .cpp .cxx .hh .hpp .java .cs .m .mm .cu .cuh .ino .glsl .hlsl .vert .frag "
         ".kt .kts .swift .scala .sc .groovy .gradle .dart .sol .php",
    "go": ".go", "rs": ".rs", "zig": ".zig", "js": ".js .jsx .mjs .cjs .ts .tsx",
    "sh": ".sh .bash .zsh .ksh", "pl": ".pl .pm", "r": ".r", "ps": ".ps1 .psm1",
}.items():
    for _ext in _exts.split():
        BRACE[_ext] = _key
SIGNATURES = {
    "go": [re.compile(r"\bfunc\s*(?:\([^()]*\)\s*)?(?P<name>[A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)?\(")],
    "rs": [re.compile(r"\bfn\s+(?P<name>[A-Za-z_]\w*)")],
    "zig": [re.compile(r"\bfn\s+(?P<name>[A-Za-z_]\w*)\s*\(")],
    "js": [
        re.compile(r"\bfunction\b\s*\*?\s*(?P<name>[A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\("),
        re.compile(r"(?P<name>[A-Za-z_$][\w$]*)\s*(?::[^=;{}]*)?=\s*(?:async\s+)?(?:function\b[^(]*\([^)]*\)|"
                   r"(?:\([^()]*\)|[A-Za-z_$][\w$]*)\s*(?::[^=;{}]*)?=>)\s*$"),
        re.compile(r"^\s*(?:@[\w.]+(?:\([^)]*\))?\s*)*(?:(?:export|default|public|private|protected|static|async|"
                   r"readonly|override|abstract|get|set)\s+)*\*?\s*(?P<name>#?[A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*"
                   r"\([^;]*\)\s*(?::\s*[^;{}=]+)?\s*$"),
    ],
    "sh": [re.compile(r"(?m)^[ \t]*(?:function[ \t]+)?(?P<name>[A-Za-z_][\w:.-]*)[ \t]*\([ \t]*\)\s*\Z"),
           re.compile(r"(?m)^[ \t]*function[ \t]+(?P<name>[A-Za-z_][\w:.-]*)\s*\Z")],
    "pl": [re.compile(r"\bsub\s+(?P<name>[A-Za-z_]\w*)\s*(?:\([^)]*\))?\s*\Z")],
    "r": [re.compile(r"(?P<name>[A-Za-z_.][\w.]*)\s*(?:<-|=)\s*function\s*\([^)]*\)\s*\Z")],
    "ps": [re.compile(r"\bfunction\s+(?P<name>[A-Za-z_][\w-]*)\s*(?:\([^)]*\))?\s*\Z")],
}

# Indentation languages: a body is every line indented deeper than its
# signature; `public` decides the export rule from the signature line.
IndentSpec = namedtuple("IndentSpec", "sig cls public")
INDENT = {
    "rb": IndentSpec(re.compile(r"^\s*(?:(?:private|protected|public)\s+)?def\s+(?:self\.)?(?P<name>[A-Za-z_]\w*[?!=]?)"),
                     re.compile(r"^\s*(?:class|module)\b"),
                     lambda head, name: not re.search(r"\b(?:private|protected)\b", head) and not name.startswith("_")),
    "ex": IndentSpec(re.compile(r"^\s*(?:defp?|defmacrop?)\s+(?P<name>[a-z_]\w*[?!]?)"),
                     re.compile(r"^\s*defmodule\b"),
                     lambda head, name: re.match(r"\s*def(?:macro)?p\b", head) is None),
    "jl": IndentSpec(re.compile(r"^\s*function\s+(?:[\w.]+\.)?(?P<name>[A-Za-z_]\w*!?)"),
                     re.compile(r"^\s*(?:module|(?:mutable\s+)?struct)\b"),
                     lambda head, name: not name.startswith("_")),
    "nim": IndentSpec(re.compile(r"^\s*(?:proc|func|method|iterator|converter|template|macro)\s+`?(?P<name>[A-Za-z_]\w*)"),
                      re.compile(r"^\s*type\b"),
                      lambda head, name: re.search(r"\b%s`?\s*\*" % re.escape(name), head) is not None),
    "lua": IndentSpec(re.compile(r"^\s*(?:local\s+)?(?:function\s+(?:\w+[.:])*(?P<name>[A-Za-z_]\w*)\s*\(|"
                                 r"(?:\w+[.:])*(?P<alt>[A-Za-z_]\w*)\s*=\s*function\s*\()"),
                      None,
                      lambda head, name: not head.lstrip().startswith("local")),
    "coffee": IndentSpec(re.compile(r"^\s*(?P<name>[A-Za-z_$][\w$]*)\s*[:=]\s*(?:\([^)]*\)\s*)?[-=]>"),
                         re.compile(r"^\s*class\b"),
                         lambda head, name: True),
    "ml": IndentSpec(re.compile(r"^\s*(?:let|and)\s+(?:rec\s+|inline\s+|private\s+)*(?P<name>[a-z_][\w']*)\s+(?=[a-z_(~?])[^=]*="),
                     re.compile(r"^\s*(?:module|type)\b"),
                     lambda head, name: "private" not in head.split()),
}
INDENT_EXT = {".rb": "rb", ".rake": "rb", ".gemspec": "rb", ".cr": "rb", ".ex": "ex", ".exs": "ex", ".jl": "jl",
              ".nim": "nim", ".lua": "lua", ".coffee": "coffee", ".ml": "ml", ".mli": "ml", ".fs": "ml",
              ".fsi": "ml", ".fsx": "ml"}
LISP_DEF = re.compile(r"\((?P<kw>defun|defmacro|defmethod|defgeneric|defn-?|define(?:-syntax)?)\s+(?:\^\S+\s+)*"
                      r"(?P<open>\(?)(?P<name>[^\s()\[\]{}\"^;]+)")


def c_signature(header):
    """Return (name, offset) of the function a C-family header declares, else None.

    The name is the identifier before the first top-level '(' that is not an
    attribute, an annotation, a keyword or a preprocessor line. A keyword or
    'new' right before the name means a type or an anonymous class, not a
    function. Kotlin fun, Swift func, Scala def, PHP and Solidity function
    headers have the same shape.
    """
    depth = 0
    for i, ch in enumerate(header):
        if ch == ")":
            depth -= 1
        elif ch == "(":
            depth += 1
            if depth != 1 or header[header.rfind("\n", 0, i) + 1:i].lstrip().startswith("#"):
                continue
            m = re.search(r"(@?)(~?[A-Za-z_]\w*)\s*$", header[:i])
            if not m or m.group(1) or m.group(2) in ATTRIBUTES:
                continue
            if m.group(2) in NOT_FUNC:
                return None
            before = header[:m.start()].rstrip()
            if re.search(r"(?:^|\W)(?:new|class|struct|interface|enum|record|union)$", before) or before.endswith("="):
                return None
            return m.group(2), m.start(2)
    return None


def signature(header, key):
    """Return (name, offset) if `header`, the code right before a '{', starts a function body."""
    if key == "c":
        return c_signature(header)
    for pattern in SIGNATURES[key]:
        m = pattern.search(header)
        if m and m.group("name") not in NOT_FUNC:
            return m.group("name"), m.start("name")
    return None


def comment_above(lines, start):
    """Return the text of the comment block right above line `start` (attributes skipped), or ''."""
    i = start - 2
    while i >= 0 and lines[i].code.strip().startswith(("@", "#[")):
        i -= 1
    texts = []
    while i >= 0 and lines[i].comment is not None and not lines[i].code.strip():
        texts.append(lines[i].comment)
        i -= 1
    return " ".join(reversed(texts)).strip()


def documented(lines, start, first, above):
    """Return True if a function has a Function comment in any of its languages' forms.

    That is a comment block above it, an Elixir @doc above it, a docstring
    literal right above it (Julia), or one opening its body (Lisp).
    """
    if above:
        return True
    i = start - 2
    while i >= 0 and lines[i].code.strip().startswith(("@", "#[")):
        if lines[i].code.strip().startswith("@doc"):
            return True
        i -= 1
    if i >= 0 and STRING_ONLY.fullmatch(lines[i].code.strip() or "x"):
        return True
    return 0 < first <= len(lines) and STRING_ONLY.fullmatch(lines[first - 1].code.strip() or "x") is not None


def is_public(family, ext, header, name, container):
    """Apply the export rule of a brace language to one function."""
    words = set(re.findall(r"[A-Za-z_]\w*", header))
    if container[0] == "func":
        return False
    if family == "go":
        return name[:1].isupper()
    if family in ("rs", "zig"):
        return "pub" in words or bool(re.search(r"\bimpl\b.*\bfor\b", container[1]))
    if family == "js":
        if container[0] == "class":
            return not (words & {"private", "protected"}) and not name.startswith("#")
        return "export" in words
    rules = {
        ".java": lambda: bool(words & {"public", "protected"}) or "interface" in container[1],
        ".cs": lambda: bool(words & {"public", "protected", "internal"}) or "interface" in container[1],
        ".swift": lambda: bool(words & {"public", "open"}),
        ".sol": lambda: not (words & {"internal", "private"}),
        ".dart": lambda: not name.startswith("_"),
        ".pl": lambda: not name.startswith("_"), ".pm": lambda: not name.startswith("_"),
        ".r": lambda: not name.startswith("."),
    }
    if ext in rules:
        return rules[ext]()
    if ext in (".kt", ".kts", ".scala", ".sc", ".groovy", ".gradle", ".php"):
        return not (words & {"private", "protected", "internal"})
    if BRACE.get(ext) in ("sh", "ps"):
        return True
    return container[0] == "class" or "static" not in words


def brace_funcs(lines, key, family, ext):
    """Return the functions of a brace-delimited file, found by matching {} in its masked code.

    A stack mirrors the open blocks; each '{' is classified by its header, the
    code since the previous ';', '{' or '}': a function, a class-like
    container, or a plain block. Headers with unbalanced parentheses are
    arguments (callbacks, lambdas), never named functions.
    """
    code = "\n".join(line.code for line in lines)
    starts = [0]
    for line in lines[:-1]:
        starts.append(starts[-1] + len(line.code) + 1)
    out, stack, last = [], [], 0
    for m in re.finditer(r"[{};]", code):
        ch, pos = m.group(), m.start()

        # Open a block: classify it by its header.
        if ch == "{":
            lo = max(last, pos - 500)
            header = code[lo:pos]
            sig = signature(header, key) if header.count("(") == header.count(")") else None
            container = next(((b[0], b[1]) for b in reversed(stack) if b[0] != "block"), ("module", ""))
            if sig:
                stack.append(("func", header, sig[0], lo + sig[1], lo, pos, container))
            else:
                stack.append(("class" if CLASSY.search(header) else "block", header))

        # Close a block: a closed function becomes a Func.
        elif ch == "}" and stack:
            block = stack.pop()
            if block[0] == "func":
                _, header, name, name_off, lo, open_off, container = block
                line_no = bisect.bisect_right(starts, name_off)
                open_line, close_line = bisect.bisect_right(starts, open_off), bisect.bisect_right(starts, pos)
                # The declaration starts at the top of the run of code lines that ends at the
                # signature: annotations, attributes, a return type on its own line. Why: stop at
                # blank, comment-only and preprocessor lines and at the header's first line, because
                # languages without ';' (shell, Go, Kotlin, Swift) let a header run back over the
                # previous statement, and the doc comment lies between the two.
                start, first_header_line = line_no, bisect.bisect_right(starts, lo)
                while start - 1 >= first_header_line and lines[start - 2].code.strip() \
                        and not lines[start - 2].code.strip().startswith("#"):
                    start -= 1
                first, last_body = open_line + 1, close_line - 1
                if last_body < first:
                    first = last_body = open_line
                above = comment_above(lines, start)
                inherited = "@Override" in header or (family == "rs" and re.search(r"\bimpl\b.*\bfor\b", container[1]))
                out.append(Func(name, line_no, start, first, last_body,
                                is_public(family, ext, header, name, container), bool(WHY.search(above)),
                                container[0], bool(inherited) or documented(lines, start, first, above)))
        last = pos + 1
    return out


def indent_funcs(lines, spec):
    """Return the functions of an indentation- or `end`-delimited file (Ruby, Elixir, Lua, Julia...).

    A body is every line after the signature indented deeper than it; the
    first code line at the same or a lower indentation ends it, which is
    exactly where the closing `end` of Ruby, Lua, Julia and Elixir sits.
    Comment-only lines and lines inside multi-line literals never end a body.
    """
    def extent(i):
        base, last = len(lines[i].raw) - len(lines[i].raw.lstrip()), i
        for j in range(i + 1, len(lines)):
            code = lines[j].code.strip()
            if not code or STRING_ONLY.fullmatch(code):
                continue
            if len(lines[j].raw) - len(lines[j].raw.lstrip()) <= base:
                break
            last = j
        return last

    classes = [(i, extent(i)) for i, line in enumerate(lines) if spec.cls and spec.cls.match(line.code)]
    found = []
    for i, line in enumerate(lines):
        m = spec.sig.match(line.code)
        if m:
            found.append((i, extent(i), m.group("name") or m.groupdict().get("alt")))
    out = []
    for i, last, name in found:
        container = "func" if any(a < i <= b for a, b, _ in found if a != i) else \
            ("class" if any(a < i <= b for a, b in classes) else "module")
        first, last_body = (i + 2, last + 1) if last > i else (i + 1, i + 1)
        above = comment_above(lines, i + 1)
        out.append(Func(name, i + 1, i + 1, first, last_body,
                        container != "func" and spec.public(lines[i].code, name), bool(WHY.search(above)),
                        container, documented(lines, i + 1, first, above)))
    return out


def lisp_funcs(lines):
    """Return the functions of a Lisp-family file: defun, defmacro, defn, (define (name ...)) forms."""
    code = "\n".join(line.code for line in lines)
    starts = [0]
    for line in lines[:-1]:
        starts.append(starts[-1] + len(line.code) + 1)
    spans = []
    for m in LISP_DEF.finditer(code):
        if m.group("kw").startswith("define") and not m.group("open"):
            continue
        depth, j = 0, m.start()
        while j < len(code):
            depth += {"(": 1, ")": -1, "[": 1, "]": -1}.get(code[j], 0)
            if depth == 0:
                break
            j += 1
        spans.append((m, bisect.bisect_right(starts, m.start()), bisect.bisect_right(starts, j)))
    out = []
    for m, line_no, close_line in spans:
        nested = any(a < line_no <= b for _, a, b in spans if a != line_no)
        first, last = (line_no + 1, close_line) if close_line > line_no else (line_no, line_no)
        above = comment_above(lines, line_no)
        out.append(Func(m.group("name"), line_no, line_no, first, last, not nested and m.group("kw") != "defn-",
                        bool(WHY.search(above)), "func" if nested else "module",
                        documented(lines, line_no, first, above)))
    return out


def py_funcs(text, lines):
    """Return the functions of a Python file via ast (exact boundaries); [] on syntax errors."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    out = []

    def walk(node, container):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min([d.lineno for d in child.decorator_list] + [child.lineno])
                doc = ast.get_docstring(child) or ""
                above = comment_above(lines, start)
                public = container != "func" and not child.name.startswith("_")
                out.append(Func(child.name, child.lineno, start, child.body[0].lineno, child.end_lineno,
                                public, bool(WHY.search(doc + " " + above)), container, bool(doc or above)))
                walk(child, "func")
            else:
                walk(child, "class" if isinstance(child, ast.ClassDef) else container)

    walk(tree, "module")
    return out


def functions(path, text):
    """Scan `text`, the content of `path`, and return (lines, functions) found the way its language needs."""
    family = cl.family_of(path)
    ext = os.path.splitext(path)[1].lower()
    lines = cl.scan(text, family)
    if family == "py":
        return lines, py_funcs(text, lines)
    if ext in BRACE:
        return lines, brace_funcs(lines, BRACE[ext], family, ext)
    if ext in INDENT_EXT:
        return lines, indent_funcs(lines, INDENT[INDENT_EXT[ext]])
    if family == "lisp":
        return lines, lisp_funcs(lines)
    return lines, []


def body_stats(lines, f):
    """Return (LOC, longest run of code lines without a standalone comment) of the body of `f`.

    Why: a line inside a multi-line string literal (an embedded awk program,
    an SQL query, a template) counts as LOC but neither extends nor breaks a
    run, since no comment of the host language can be written there.
    """
    loc = gap = run = 0
    for line in lines[f.first - 1:f.last]:
        code = line.code.strip()
        if code:
            loc += 1
            if code != '"':
                run += 1
                gap = max(gap, run)
        elif line.comment is not None:
            run = 0
    return loc, gap


def helper_candidate(f, loc, rel):
    """Return True if `f` could be a shallow single-caller helper (callers are counted later)."""
    return (not f.public and f.container != "func" and loc < SHALLOW_FN and not f.why
            and SIMPLE_NAME.match(f.name) is not None and not f.name.startswith("__")
            and not TEST_PATH.search(rel))


def doc_missing(f, loc, rel):
    """Return True if `f` is a public function that needs a Function comment and has none."""
    return f.public and not f.doc and loc >= DOC_MIN_LOC and f.name != "main" and not TEST_PATH.search(rel)


def design_missing(lines, rel):
    """Return True if a module of >= 20 LOC does not open with a comment block of >= 4 words.

    The shebang and the Python encoding cookie are not part of the block; a
    leading docstring literal (Julia, Elixir) counts, and so does an Elixir
    @moduledoc within the first three code lines. Test files and tiny
    modules are exempt.
    """
    if TEST_PATH.search(rel) or sum(1 for line in lines if line.code.strip()) < DESIGN_MIN_LOC:
        return False
    words = code_lines = 0
    for line in lines:
        code = line.code.strip()
        if code and not STRING_ONLY.fullmatch(code):
            if code.startswith("@moduledoc"):
                return False
            code_lines += 1
            if words or code_lines >= 3:
                break
            continue
        text = line.comment if line.comment is not None else (line.raw if code else "")
        if text and not text.startswith(("!", "-*-")):
            words += len(text.split())
    return words < 4


def layout_unexplained(lines, family, numbers):
    """Return (line, code) of the first layout-sensitive construct among line `numbers` that
    has no diagram-bearing comment from 80 lines above to 5 below, else None."""
    pattern = LAYOUT.get(family)
    for no in sorted(numbers) if pattern else ():
        if no > len(lines):
            break
        m = pattern.search(lines[no - 1].code)
        window = lines[max(0, no - 1 - TEACHER_ABOVE):no + TEACHER_BELOW]
        if m and not any(line.comment and DIAGRAM.search(line.comment) for line in window):
            return no, m.group().strip()[:40]
    return None


def layout_unasserted(lines, family, numbers, search):
    """Return (line, name, evidence) for each struct among line `numbers` that describes a
    physical layout but has no size or offset assertion; `search(names)` lists the files
    where such an assertion may live.

    Why: a [field:size] diagram rots silently, while static_assert(sizeof(...))
    or offsetof checks make the compiler or the test suite verify it on every
    build. Logical diagrams (trees, -> chains, state machines) carry no cells,
    offsets or layout markers, so they never trigger this rule.
    """
    template = ASSERT_QUERY.get(family)
    out = []
    for st in cl.structs(lines, family) if template else ():
        texts = [text for _, text in cl.diagram_window(lines, st)]
        sized = any(cl.CELL_SIZE.match(m.group("size").strip()) for t in texts for m in cl.CELL.finditer(t))
        evidence = ("layout marker" if st.packed else "[field:size] cells" if sized else
                    "byte offsets" if any(cl.OFFSETS.search(t) for t in texts) else None)
        names = [n for n in [st.name] + st.aliases if n]
        if not evidence or not names or not numbers.intersection(range(st.line, st.last + 1)):
            continue

        # Look for one code line that is an assertion and queries this struct's size or offsets.
        patterns = [re.compile(template.format(re.escape(n))) for n in names]
        asserted = False
        for path in search(names):
            text, path_family = cl.read_text(path), cl.family_of(path)
            if not text or not path_family or not any(n in text for n in names):
                continue
            if any(ASSERTION.search(line.code) and any(p.search(line.code) for p in patterns)
                   for line in cl.scan(text, path_family)):
                asserted = True
                break
        if not asserted:
            out.append((st.line, names[0], evidence))
    return out


def assertion_search(root, extra):
    """Return a search(names) function for layout_unasserted: git grep over the work tree, plus `extra` files."""
    def search(names):
        files = list(extra)
        if root:
            args = ["grep", "-l", "-I", "--untracked", "-w"]
            for name in names:
                args += ["-e", name]
            files += [os.path.join(root, rel) for rel in (cl.git(args, root) or "").splitlines()]
        return files
    return search


def reference_counts(root, names, files):
    """Count whole-word references to `names` in code, never in comments or strings.

    Inside git the files come from `git grep` over the whole work tree;
    outside git only `files` are read. A forward declaration counts as a
    reference, so a prototyped C helper is never reported: a false negative
    is cheaper than a false alarm in a blocking hook.
    """
    names = {n for n in names if SIMPLE_NAME.match(n)}
    if not names:
        return Counter()
    if root:
        args = ["grep", "-l", "-I", "--untracked", "-w"]
        for name in sorted(names):
            args += ["-e", name]
        files = [os.path.join(root, rel) for rel in (cl.git(args, root) or "").splitlines() if cl.family_of(rel)]
    pattern = re.compile(r"(?<![\w$])(?:%s)(?![\w$])" % "|".join(map(re.escape, sorted(names))))
    counts = Counter()
    for path in files:
        text = cl.read_text(path)
        if text is None or not any(n in text for n in names):
            continue
        code = "\n".join(line.code for line in cl.scan(text, cl.family_of(path)))
        counts.update(m.group() for m in pattern.finditer(code))
    return counts


def code_files(paths, root):
    """Expand PATH arguments (or the whole repository) into a list of code files."""
    if not paths and root:
        rels = (cl.git(["ls-files", "-co", "--exclude-standard"], root) or "").splitlines()
        found = [os.path.join(root, rel) for rel in rels if cl.family_of(rel)]
    else:
        found = []
        for top in paths or ["."]:
            if os.path.isfile(top):
                found.append(top)
                continue
            for dirpath, dirnames, filenames in os.walk(top):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                found += [os.path.join(dirpath, f) for f in filenames if cl.family_of(f)]
    # shortcut: --report reads at most 5000 code files; add a --limit option if a repository needs more.
    return [p for p in found if cl.family_of(p)][:MAX_FILES]


def report(paths):
    """Print module metrics and findings as minified columnar JSON; always exit 0."""
    root = cl.repo_root(os.getcwd())
    files = code_files(paths, root)
    search = assertion_search(root, files)
    rows, finds, helpers = [], [], []

    # Per-file metrics: size, interface width, rhythm, helper candidates.
    for path in files:
        text = cl.read_text(path)
        if text is None:
            continue
        rel = cl.shown(path)
        lines, funcs = functions(path, text)
        total = sum(1 for line in lines if line.code.strip())
        public = sum(f.public for f in funcs)
        long_fns = bad = 0
        for f in funcs:
            loc, gap = body_stats(lines, f)
            if loc >= LONG_FN:
                long_fns += 1
                if gap > GUIDE_EVERY:
                    bad += 1
                    finds.append([rel, f.line, "RHYTHM", f.name, gap])
            if helper_candidate(f, loc, rel):
                helpers.append((rel, f, loc))
            if doc_missing(f, loc, rel):
                finds.append([rel, f.line, "NO-DOC", f.name, loc])
        dmod = round(total / public, 1) if public else None
        if dmod is not None and public >= 2 and dmod < MIN_DMOD and not TEST_PATH.search(rel) \
                and not rel.endswith(HEADER_EXT):
            finds.append([rel, 1, "DMOD", "", dmod])

        # Comment coverage at module level: Design header, Teacher diagrams, layout assertions.
        family, everything = cl.family_of(path), set(range(1, len(lines) + 1))
        if design_missing(lines, rel):
            finds.append([rel, 1, "NO-DESIGN", "", total])
        layout = layout_unexplained(lines, family, everything)
        if layout:
            finds.append([rel, layout[0], "NO-TEACHER", "", layout[1]])
        for line_no, name, evidence in layout_unasserted(lines, family, everything, search):
            finds.append([rel, line_no, "NO-LAYOUT-ASSERT", name, evidence])
        rows.append([rel, total, public, dmod, long_fns, bad, 0])

    # Single-caller helpers need repository-wide reference counts.
    counts = reference_counts(root, {f.name for _, f, _ in helpers}, files)
    per_file = Counter()
    for rel, f, loc in helpers:
        if counts[f.name] == 2:
            per_file[rel] += 1
            finds.append([rel, f.line, "HELPER", f.name, loc])
    for row in rows:
        row[6] = per_file[row[0]]

    fn40 = sum(r[4] for r in rows)
    kinds = Counter(f[2] for f in finds)
    total = {"files": len(rows), "fn40": fn40,
             "rhythm_ok": round(1 - sum(r[5] for r in rows) / fn40, 3) if fn40 else 1.0,
             "helpers1": sum(per_file.values()), "dmod_lt15": kinds["DMOD"], "no_doc": kinds["NO-DOC"],
             "no_design": kinds["NO-DESIGN"], "no_teacher": kinds["NO-TEACHER"],
             "no_layout_assert": kinds["NO-LAYOUT-ASSERT"]}
    print(json.dumps({"c": ["file", "loc", "pub", "dmod", "fn40", "rhythm_bad", "helpers1"], "r": rows,
                      "n": len(rows), "f": {"c": ["file", "line", "kind", "name", "value"], "r": finds,
                                            "n": len(finds)}, "total": total}, separators=(",", ":")))
    return 0


def unseen(session, findings):
    """Drop findings already reported in this session and remember the new ones.

    Keys omit line numbers on purpose: edits shift lines, and the same
    violation must not come back as a new one.
    """
    if not session:
        return findings
    state = os.path.join(tempfile.gettempdir(),
                         "craft-gate-%s.txt" % hashlib.sha1(session.encode("utf-8")).hexdigest()[:16])
    try:
        with open(state, encoding="utf-8") as fh:
            seen = set(fh.read().splitlines())
    except OSError:
        seen = set()
    fresh = [f for f in findings if f[0] not in seen]
    try:
        with open(state, "a", encoding="utf-8") as fh:
            fh.writelines(key + "\n" for key, _ in fresh)
    except OSError:
        pass
    return fresh


def gate():
    """Stop hook: report new violations in code changed since HEAD; see the module docstring."""
    data = cl.hook_input()
    if data.get("stop_hook_active"):
        return 0
    root = cl.repo_root(data.get("cwd") or os.getcwd())
    if not root:
        return 0
    base = cl.resolve_base(root, "HEAD")
    findings, helpers = [], []
    for rel, new in cl.changed_files(root, base):
        path = os.path.join(root, rel)
        text = cl.read_text(path)
        if text is None:
            continue
        lines, funcs = functions(path, text)
        added = set(range(1, len(lines) + 1)) if new else cl.added_lines(root, base, rel)
        old = set() if new else {f.name for f in functions(path, cl.baseline_text(root, base, rel))[1]}

        # RHYTHM for touched long functions; HELPER and NO-DOC for new ones.
        for f in funcs:
            loc, gap = body_stats(lines, f)
            if loc >= LONG_FN and gap > GUIDE_EVERY and added.intersection(range(f.start, f.last + 2)):
                findings.append(("%s|rhythm|%s" % (rel, f.name),
                                 "%s:%d: [RHYTHM] '%s' has %d LOC and %d code lines in a row without a Guide "
                                 "comment (max %d): add one every 15-25 LOC." % (rel, f.line, f.name, loc, gap,
                                                                                 GUIDE_EVERY)))
            if f.name not in old and helper_candidate(f, loc, rel):
                helpers.append((rel, f, loc))
            if f.name not in old and doc_missing(f, loc, rel):
                findings.append(("%s|doc|%s" % (rel, f.name),
                                 "%s:%d: [NO-DOC] public function '%s' has no Function comment: state behavior, "
                                 "parameters, returns, errors, ownership and side effects above it."
                                 % (rel, f.line, f.name)))

        # DMOD and NO-DESIGN only for new modules; DMOD only with more than one entry point.
        if new and not TEST_PATH.search(rel) and not rel.endswith(HEADER_EXT):
            public = sum(f.public for f in funcs)
            total = sum(1 for line in lines if line.code.strip())
            if public >= 2 and total / public < MIN_DMOD:
                findings.append(("%s|dmod" % rel,
                                 "%s:1: [DMOD] D_mod %.1f < %d (%d LOC / %d public functions): merge shallow "
                                 "entry points or hide them behind a narrower interface."
                                 % (rel, total / public, MIN_DMOD, total, public)))
        if new and design_missing(lines, rel):
            findings.append(("%s|design" % rel,
                             "%s:1: [NO-DESIGN] new module without a Design comment at the top: problem, data "
                             "layout, invariants, known limitations, or a pointer to the module's Design comment."
                             % rel))

        # NO-TEACHER and NO-LAYOUT-ASSERT for layout-sensitive code this change wrote.
        family = cl.family_of(rel)
        layout = layout_unexplained(lines, family, added)
        if layout:
            findings.append(("%s|teacher" % rel,
                             "%s:%d: [NO-TEACHER] layout-sensitive code (%s) without a Teacher diagram nearby: add "
                             "a [field:size] strip, a byte-offset table or a -> chain that matches the code."
                             % (rel, layout[0], layout[1])))
        for line_no, name, evidence in layout_unasserted(lines, family, added, assertion_search(root, [path])):
            findings.append(("%s|layout|%s" % (rel, name),
                             "%s:%d: [NO-LAYOUT-ASSERT] struct '%s' has a physical layout (%s) but no size or "
                             "offset assertion: add static_assert(sizeof/offsetof) or its language form (see "
                             "craft-code) so the compiler checks the diagram." % (rel, line_no, name, evidence)))

    counts = reference_counts(root, {f.name for _, f, _ in helpers}, [])
    for rel, f, loc in helpers:
        if counts[f.name] == 2:
            findings.append(("%s|helper|%s" % (rel, f.name),
                             "%s:%d: [HELPER] '%s' (%d LOC) has a single caller: inline it under a Guide comment, "
                             "or justify it with a 'Why:' comment (boundary, ownership, duplication, test seam)."
                             % (rel, f.line, f.name, loc)))
    return cl.report([msg for _, msg in unseen(data.get("session_id"), findings)], hook=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--report", action="store_true", help="print metrics as columnar JSON")
    mode.add_argument("--gate", action="store_true", help="Stop hook mode: read the hook JSON on stdin")
    parser.add_argument("paths", nargs="*", help="files or directories for --report")
    args = parser.parse_args()
    return gate() if args.gate else report(args.paths)


if __name__ == "__main__":
    sys.exit(main())
