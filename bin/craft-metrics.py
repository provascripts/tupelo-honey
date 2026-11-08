#!/usr/bin/env python3
"""craft-metrics.py -- module depth, comment coverage and pass-through helpers, measured.

Usage:
  craft-metrics.py --report [PATH...]  minified columnar JSON for PATHs (default:
                                       every code file of the git repository, or
                                       under the current directory outside git)

The Stop gate (craft-gate.py) runs the same checks on what the session
changed and shows them as review signals, once per session: a number below
or above its threshold invites a look, it never decides. The sovereign test
is AGENTS.md section 1: lower the reader's cognitive load, and keep a helper
that draws a semantic boundary, owns a resource, removes duplication or is a
test seam.

Metrics (LOC = non-blank, non-comment lines; test files, and functions named
test_*, *Test or *Tests anywhere, get no signal; the thresholds are measured
on antirez's code, see the constants below):
  DMOD        implementation LOC / public functions of a file, a hint below 15
              for non-header files with >= 2 public functions (a single entry
              point is already as narrow as an interface gets). --report
              only: the Stop gate never shows it, since no count decides
              whether a module is shallow.
  RHYTHM      a function of >= 40 LOC whose longest run of code lines without
              a standalone comment line exceeds 58 and that is complex as well:
              >= 9 branches, or blocks nested >= 3 deep (profile()). Length
              alone never signals, since a flat handler reads top to bottom.
              Lines inside multi-line string literals and the cases of a flat
              switch, which is a table, neither extend nor break a run.
              The Stop gate asks only when the change makes the run longer (a
              fix inside an old long function changes nothing for its reader);
              --report lists them all and gives the mean density, LOC per
              Guide comment.
  HELPER      a pass-through: a module-level internal function whose body is
              one call handing its parameters on, in order and unchanged (no
              constant, expression, default value or reordering) to a callee
              not reached through a parameter, called exactly once besides
              its definition and never used as a value (a callback). A helper
              with logic of its own is never flagged: whether it draws a
              semantic boundary, owns a resource, removes duplication or is a
              test seam is judgment.
  NO-DOC      a function of >= 3 LOC without a Function comment (the comment
              block above it, a docstring, an Elixir @doc) that is either part
              of the file's exported API (exported_names(): public header,
              __all__, package index, crate pub) or leaves its caller something
              to release (owner()). `main`, @Override methods and Rust trait
              impls inherit their contract.
  NO-DESIGN   a module of >= 20 LOC that does not open with a comment block of
              >= 4 words (Design notes, a pointer to the module's Design
              comment, an Elixir @moduledoc) and holds something a reader must
              learn first (module_traits()): its own data structure, a binary
              layout, concurrency primitives or >= 2 public types.
  NO-TEACHER  layout-sensitive code (bitfields of >= 2 bits, packed structs,
              flexible array members, header-behind-pointer arithmetic,
              #[repr], pack/unpack,
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
  GRID        --report only: a comment block with >= 3 rulers of two or more
              columns (+--+--+ or box-drawing), a grid whose alignment breaks
              silently. A one-row strip has two rulers and passes.
  NO-TEST     Stop gate only, one per session: the session changed a struct,
              class or type (its masked code differs, so a new comment never
              counts) or added an exported function, and no test file changed.

Function extraction per language: Python by ast; brace languages (C family,
Java, C#, Kotlin, Swift, Scala, Groovy, Dart, Solidity, PHP, Go, Rust, Zig,
JS/TS, shell, Perl, R, PowerShell) by matching {}; Ruby, Crystal, Elixir,
Lua, Julia, Nim, CoffeeScript, OCaml and F# by indentation; the Lisp family
by matching parentheses. SQL, Haskell, Erlang, Fortran, VB and Tcl get the
comment checks only. The scripts check presence, never quality. See the
design notes in craftlib.py.
"""

import argparse
import ast
import bisect
import itertools
import json
import os
import re
import sys
from collections import Counter, namedtuple

import craftlib as cl

# Thresholds measured on antirez's code (rax, sds, redis src/; see bench/): the longest
# Guide-free run of his long functions is <= 58 lines in 90% of them, and three quarters
# of those he guides have >= 9 branches, as three quarters of his documented exported
# functions have >= 3 lines.
GUIDE_EVERY, LONG_FN, MIN_DMOD = 58, 40, 15
RHYTHM_BRANCHES, RHYTHM_NESTING = 9, 3
DOC_MIN_LOC, DESIGN_MIN_LOC, TEACHER_ABOVE, TEACHER_BELOW = 3, 20, 80, 5
MAX_FILES = 5000

# line: signature line; start: first line of the declaration (decorators,
# attributes, a return type on its own line); first..last: body lines,
# 1-based and inclusive; container: 'module', 'class' or 'func'; forward:
# the callee when the body only hands the parameters on (forwarded()), else
# ''; doc: has a Function comment or inherits one.
Func = namedtuple("Func", "name line start first last public forward container doc")

# A pass-through body in masked code: one call, maybe returned or awaited,
# to a plain or qualified name (g, self.g, r->g, ns::g), with no nested call.
PASS_THROUGH = re.compile(r"^(?:return\b\s*)?(?:await\s+)?(?P<callee>[A-Za-z_$][\w$]*(?:\s*(?:\.|::|->)\s*"
                          r"[A-Za-z_$][\w$]*)*)\s*\((?P<args>[^()]*)\)\s*;?$")
DIAGRAM = re.compile(r"\][\[(]|\]\s+\[|->|\d\s*\.\.\s*\d|\b[a-z]\s*[+-]\s*\d+\s+\w")
GRID_RULER = re.compile(r"\+(?:[-=/]+\+){2,}|[\u250c\u251c\u2514\u2554\u2560\u255a][\u2500\u2550]+[\u252c\u253c\u2534\u2566\u256c\u2569]")
GRID_RULERS = 3
STRING_ONLY = re.compile(r'"+\)?')
LAYOUT = {
    # Why these forms: on redis, a one-bit flag, an `extern` array declaration and size arithmetic
    # (`SIZE_MAX - sizeof(x)`) matched looser patterns, and none of them is a memory layout. A
    # bitfield is at least two bits wide; a flexible array is an indented member; `- sizeof` is
    # pointer arithmetic only after a cast to pointer or in the initializer of a pointer.
    "c": re.compile(r"^\s*(?:unsigned|signed|int|char|short|long|bool|_Bool|u?int\d+_t)\b[\w\s*]*\b\w+\s*:\s*"
                    r"(?:[2-9]|\d\d+)\s*;"
                    r"|__packed__|__attribute__\s*\(\(\s*packed|#\s*pragma\s+pack"
                    r"|^[ \t]+(?!extern\b|static\b)[\w\s*]+\b\w+\s*\[\s*\]\s*;"
                    r"|\*\s*\)[^;]*-\s*sizeof\s*\(|\*\s*\w+\s*=[^;=]*-\s*sizeof\s*\(|\[\s*-\s*\d+\s*\]"
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
TEST_NAME = re.compile(r"^test_|Tests?$")
HEADER_EXT = (".h", ".hh", ".hpp")
C_SOURCE = (".c", ".cc", ".cpp", ".cxx", ".m", ".mm", ".cu")
JS_EXT = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")

# Complexity profile, on masked code. A switch counts as one branch and its
# labels never do, so a switch used as a table stays one branch; .match( and
# select( are calls, not statements.
BRANCH = re.compile(r"(?<![\w$.])(?:if|elif|elsif|for|foreach|while|until|unless|catch|except|rescue|switch)\b"
                    r"|(?<![\w$.])(?:select|match)\b(?!\s*\()")
SWITCH_OPEN = re.compile(r"(?<![\w$.])(?:(?:switch|select)\b[^{};]*|(?:match|when)\b[^{};=]*)\{")
CONCURRENCY = re.compile(r"\b(?:pthread_\w+|std::(?:j?thread|(?:shared_|recursive_)?mutex|atomic|condition_variable)\b"
                         r"|_Atomic\b|atomic_\w+|__atomic_\w+|__sync_\w+|sync\.(?:Mutex|RWMutex|WaitGroup|Once|Cond)\b"
                         r"|threading\.\w+|multiprocessing\b|asyncio\.(?:Lock|Semaphore|Event|Condition|Queue)\b"
                         r"|(?:Mutex|RwLock|ReentrantLock|Semaphore|CountDownLatch|DispatchQueue|SharedArrayBuffer)\b"
                         r"|Atomic(?:Integer|Long|Boolean|Reference|Bool|Ptr|U?(?:8|16|32|64|size))\b|Atomics\.\w+"
                         r"|synchronized\b|go\s+func\b|chan\s+[\w*\[])")
TYPE_DEF = re.compile(r"^(?P<mods>(?:(?:export|default|pub(?:\([^)]*\))?|public|private|protected|internal|fileprivate|"
                      r"open|abstract|final|sealed|data|static|partial|readonly|declare|typedef)\s+)*)"
                      r"(?:class|struct|interface|enum|union|trait|record|protocol|object|type)\s+(?P<name>[A-Za-z_]\w*)")
# Ownership: the last name segment of a call that allocates or opens
# (zmalloc, strdup, listCreate, raxNew, fopen, socket), and of a call that
# takes or drops a lock. CALLED finds every call; ASSIGNED a call whose
# result is stored or returned, through a cast or an await.
OWNING_CALL = re.compile(r"(?i)(?:(?:m|c|re)?alloc|dup|new|create\w*|open|connect|accept|socket|mmap)$|^(?:new|create|open)")
LOCK_CALL = re.compile(r"(?:^|_)(?:lock|acquire)$|(?<=[a-z])(?:Lock|Acquire)$|^(?:Lock|Acquire)$")
UNLOCK_CALL = re.compile(r"(?i)(?:un_?lock|release)$|^(?:un_?lock|release)")
CALLED = re.compile(r"([A-Za-z_][\w$]*(?:\s*(?:\.|::|->)\s*[A-Za-z_][\w$]*)*)\s*\(")
ASSIGNED = re.compile(r"(?:(?P<var>[A-Za-z_]\w*)\s*:?=\s*|\breturn\s+)(?:\([^()]*\)\s*)?(?:await\s+)?"
                      r"(?P<callee>[A-Za-z_][\w$]*(?:\s*(?:\.|::|->)\s*[A-Za-z_][\w$]*)*)\s*\(")
# The NO-DOC signal per doc_due() reason: what the function is, and the question its comment answers.
DOC_QUESTIONS = {"exported": ("is part of the module's exported API", "what must a caller know that the signature "
                              "does not say (errors, ownership, side effects)?"),
                 "owner": ("leaves its caller memory, a resource or a lock to release", "who releases it, and when?")}
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


def _params(code, start, end):
    """Return the text inside the first balanced parentheses of code[start:end], or None if there are none."""
    i = code.find("(", start, end)
    if i < 0:
        return None
    depth = 0
    for j in range(i, end):
        depth += {"(": 1, ")": -1}.get(code[j], 0)
        if depth == 0:
            return code[i + 1:j]
    return None


def forwarded(params, body, name):
    """Return the callee when `body` only hands the parameters on, in order and unchanged; else ''.

    `params` is the masked text between the signature's parentheses (None
    when the signature has none), `body` the masked code of the body. Every
    argument must be the bare name of the parameter in its position: a
    constant, an expression, a reordering, a dropped parameter or a default
    value adds something, and so does a body with any other statement. A
    callee reached through a parameter (conn->type->writev(conn, ...))
    dispatches on it, which a direct call cannot replace.
    """
    m = PASS_THROUGH.match(body.strip()) if params is not None else None
    path = re.split(r"\s*(?:\.|::|->)\s*", m.group("callee")) if m else []
    if not m or path[-1] == name:
        return ""
    args = [a.strip() for a in m.group("args").split(",")] if m.group("args").strip() else []
    segments = [] if params.strip() in ("", "void") else params.split(",")
    if len(args) != len(segments) or (len(path) > 1 and path[0] in args):
        return ""
    for arg, segment in zip(args, segments):
        if "=" in segment or not cl.IDENT.fullmatch(arg) or arg not in cl.IDENT.findall(segment):
            return ""
    return re.sub(r"\s+", "", m.group("callee"))


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
                                is_public(family, ext, header, name, container),
                                forwarded(_params(code, name_off + len(name), open_off), code[open_off + 1:pos], name),
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
        head = lines[i].code
        body = "\n".join(line.code for line in lines[first - 1:last_body]) if last > i else ""
        params = _params(head, head.find(name) + len(name), len(head)) if name else None
        out.append(Func(name, i + 1, i + 1, first, last_body,
                        container != "func" and spec.public(lines[i].code, name), forwarded(params, body, name),
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
                        "", "func" if nested else "module",
                        documented(lines, line_no, first, above)))
    return out


def _py_forward(fn):
    """Return the callee when Python function `fn` only hands its parameters on, as forwarded() does; else ''.

    A docstring does not count as a statement; a decorator, a default value,
    *args, **kwargs or a keyword-only parameter adds something.
    """
    body = fn.body[1:] if ast.get_docstring(fn) is not None else fn.body
    a = fn.args
    if fn.decorator_list or a.defaults or a.kwonlyargs or a.vararg or a.kwarg or len(body) != 1 \
            or not isinstance(body[0], (ast.Return, ast.Expr)):
        return ""
    call = body[0].value.value if isinstance(body[0].value, ast.Await) else body[0].value
    if not isinstance(call, ast.Call) or call.keywords \
            or [x.id if isinstance(x, ast.Name) else None for x in call.args] != [p.arg for p in a.posonlyargs + a.args]:
        return ""
    parts, node = [], call.func  # self.io.g -> parts ['g', 'io'], node self
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name) or (parts[0] if parts else node.id) == fn.name \
            or (parts and node.id in [p.arg for p in a.posonlyargs + a.args]):
        return ""
    return ".".join([node.id] + parts[::-1])


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
                                public, _py_forward(child), container, bool(doc or above)))
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


def table_lines(lines, f):
    """Return the line numbers strictly inside the flat switches of the body of `f`.

    A switch (switch, select, match, when) is flat when none of its cases
    opens a block: it is a table, and a Guide comment per row would restate
    its labels. Found on masked code, so only brace languages have them.
    """
    body = lines[f.first - 1:f.last]
    code = "\n".join(line.code for line in body)
    starts, out = [0], set()
    for line in body[:-1]:
        starts.append(starts[-1] + len(line.code) + 1)
    for m in SWITCH_OPEN.finditer(code):
        depth, nested, j = 0, False, m.end() - 1
        for j in range(m.end() - 1, len(code)):
            depth += {"{": 1, "}": -1}.get(code[j], 0)
            nested = nested or depth > 1
            if depth == 0:
                break
        if depth == 0 and not nested:
            out.update(range(f.first + bisect.bisect_right(starts, m.end() - 1),
                             f.first + bisect.bisect_right(starts, j) - 1))
    return out


def body_stats(lines, f):
    """Return (LOC, longest run of code lines without a standalone comment) of the body of `f`.

    Why: a line inside a multi-line string literal (an embedded awk program,
    an SQL query, a template) counts as LOC but neither extends nor breaks a
    run, since no comment of the host language can be written there. The
    cases of a flat switch (table_lines) are neutral the same way.
    """
    loc = gap = run = 0
    table = table_lines(lines, f)
    for no, line in enumerate(lines[f.first - 1:f.last], f.first):
        code = line.code.strip()
        if code:
            loc += 1
            if code != '"' and no not in table:
                run += 1
                gap = max(gap, run)
        elif line.comment is not None:
            run = 0
    return loc, gap


def profile(lines, f, braces):
    """Return (branches, nesting), the complexity profile of the body of `f`.

    branches counts the decision keywords (BRANCH). nesting is the deepest
    block inside the body: {} pairs in brace languages, preprocessor lines
    skipped; elsewhere the indentation levels of the lines that do not
    continue an open bracket or a multi-line string.
    """
    branches = nesting = depth = 0
    levels = set()
    for line in lines[f.first - 1:f.last]:
        code = line.code.strip()
        if not code or code == '"' or (braces and code.startswith("#")):
            continue
        branches += len(BRANCH.findall(code))
        if braces:
            for ch in code:
                depth += {"{": 1, "}": -1}.get(ch, 0)
                nesting = max(nesting, depth)
        else:
            if depth <= 0:
                levels.add(len(line.raw) - len(line.raw.lstrip()))
            depth += sum(map(code.count, "([{")) - sum(map(code.count, ")]}"))
    return branches, nesting if braces else max(len(levels) - 1, 0)


def test_code(f, rel):
    """Return True for test code: any function of a test file, or one named test_* or *Test(s) anywhere."""
    return bool(TEST_PATH.search(rel) or TEST_NAME.search(f.name or ""))


def rhythm_due(f, rel, loc, gap, shape):
    """Return True if `f` gets the RHYTHM signal: long, complex (`shape` from profile()) and unsegmented."""
    return (loc >= LONG_FN and gap > GUIDE_EVERY and not test_code(f, rel)
            and (shape[0] >= RHYTHM_BRANCHES or shape[1] >= RHYTHM_NESTING))


def helper_candidate(f, rel):
    """Return True if `f` is an internal pass-through that may have a single caller (callers are counted later)."""
    return (bool(f.forward) and not f.public and f.container != "func"
            and SIMPLE_NAME.match(f.name) is not None and not f.name.startswith("__")
            and not TEST_PATH.search(rel))


def _last_name(callee):
    return re.split(r"\s*(?:\.|::|->)\s*", callee)[-1]


def owner(lines, f):
    """Return True if `f` leaves its caller something to release.

    That is: it returns what an allocating or opening call gave it (directly,
    or through the variable that call was stored in), or it takes a lock its
    body never releases.
    """
    body = "\n".join(line.code for line in lines[f.first - 1:f.last])
    calls = [_last_name(c) for c in CALLED.findall(body)]
    if any(LOCK_CALL.search(c) for c in calls) and not any(UNLOCK_CALL.search(c) for c in calls):
        return True
    for m in ASSIGNED.finditer(body):
        if OWNING_CALL.search(_last_name(m.group("callee"))) and (
                m.group("var") is None
                or re.search(r"\breturn\s+(?:\([^()]*\)\s*)?%s\b" % re.escape(m.group("var")), body)):
            return True
    return False


def _py_all(text):
    """Return the names a Python module lists in __all__, or None when it has no literal __all__."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    names = None
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] \
            if isinstance(node, (ast.AugAssign, ast.AnnAssign)) else []
        if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets) \
                and isinstance(node.value, (ast.List, ast.Tuple)):
            names = (names or set()) | {e.value for e in node.value.elts
                                        if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return names


def exported_names(path, text, lines, funcs, root=None):
    """Return the names of `funcs` that form the exported API of the file at `path`.

    Each language's own declaration of its interface:
      C family    what a header of the same stem declares (foo.c and foo.h, in
                  the same directory or in <root>/include); in a header, every
                  module-level function, static inline ones included.
      Python      __all__; without it, what the package's __init__.py imports
                  from the module (an __init__.py exports its public functions).
      JS/TS       what the directory's index file re-exports from the module
                  (an index file exports its exports).
      Rust        pub, but not pub(crate), pub(super) or pub(in ...).
      shell       nothing: a script has no importers.
      others      the visibility rule of is_public() (Go capitals, public...).
    Sibling files are read with cl.read_text, so a replay reads the commit's.
    """
    ext = os.path.splitext(path)[1].lower()
    folder, base = os.path.split(path)
    stem = os.path.splitext(base)[0]
    public = {f.name for f in funcs if f.public}
    if ext in HEADER_EXT:
        return {f.name for f in funcs if f.container == "module"}
    if ext in C_SOURCE:
        declared = set()
        for top in [folder] + ([os.path.join(root, "include")] if root else []):
            for header_ext in HEADER_EXT:
                header = cl.read_text(os.path.join(top, stem + header_ext))
                if header:
                    declared.update(_last_name(c) for c in CALLED.findall(
                        "\n".join(line.code for line in cl.scan(header, "c"))))
        return public & declared
    if ext == ".py":
        listed = _py_all(text)
        if listed is not None or base == "__init__.py":
            return public & listed if listed is not None else public
        imported = set()
        try:
            tree = ast.parse(cl.read_text(os.path.join(folder, "__init__.py")) or "")
        except (SyntaxError, ValueError):
            return imported
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] == stem:
                if any(alias.name == "*" for alias in node.names):
                    return public
                imported.update(alias.name for alias in node.names)
        return public & imported
    if ext in JS_EXT:
        if stem == "index":
            return public
        reexported = set()
        pattern = re.compile(r"export\s*(\*|\{([^}]*)\})\s*from\s*['\"]\./%s(?:\.\w+)?['\"]" % re.escape(stem))
        for index_ext in JS_EXT:
            for m in pattern.finditer(cl.read_text(os.path.join(folder, "index" + index_ext)) or ""):
                if m.group(1) == "*":
                    return public
                reexported.update(re.sub(r"^type\s+", "", part.strip()).split(" as ")[0].strip()
                                  for part in m.group(2).split(","))
        return public & reexported
    if BRACE.get(ext) in ("sh", "ps"):
        return set()
    if ext == ".rs":
        return {f.name for f in funcs if f.public
                and not any(re.search(r"\bpub\s*\(", line.code) for line in lines[f.start - 1:f.line])}
    return public


def doc_due(f, loc, rel, exported, lines):
    """Return why `f` needs the Function comment it lacks: 'exported', 'owner' (see owner()), or ''."""
    if f.doc or loc < DOC_MIN_LOC or f.name == "main" or f.container == "func" or test_code(f, rel):
        return ""
    return "exported" if f.name in exported else "owner" if owner(lines, f) else ""


def top_types(lines):
    """Yield (index in `lines`, TYPE_DEF match) for the type definitions starting at column 0 of a module."""
    for i, line in enumerate(lines):
        m = TYPE_DEF.match(line.code) if line.raw[:1] not in (" ", "\t") else None
        if m:
            yield i, m


def public_types(lines, family):
    """Count the top-level type definitions (class, struct, interface, enum...) a module exports."""
    count = 0
    for i, m in top_types(lines):
        if family == "c" and ("(" in lines[i].code or lines[i].code.rstrip().endswith(";")):
            continue
        mods, name = m.group("mods").split(), m.group("name")
        if name.startswith("_") or {"private", "internal", "fileprivate"} & set(mods) \
                or (family == "js" and "export" not in mods) or (family in ("rs", "zig") and "pub" not in mods) \
                or (family == "go" and not name[:1].isupper()):
            continue
        count += 1
    return count


def module_traits(lines, family):
    """Return what makes a module need a Design comment: its own data structure, a binary layout,
    concurrency primitives, or at least two public types; [] for none of them."""
    traits = ["its own data structure"] if cl.structs(lines, family) else []
    if family in LAYOUT and any(LAYOUT[family].search(line.code) for line in lines):
        traits.append("a binary layout")
    if any(CONCURRENCY.search(line.code) for line in lines):
        traits.append("concurrency primitives")
    types = public_types(lines, family)
    return traits + (["%d public types" % types] if types >= 2 else [])


def type_bodies(lines, family, braces):
    """Return {name: (line number, masked code)} for the top-level types of a scanned module.

    Comparing the masked code of two versions tells a changed type from one
    that only gained a comment. The body is the balanced {} pair, or without
    braces the lines indented below the definition; an alias or a forward
    declaration is its own line. C's anonymous `typedef struct {...} t` has
    no name for TYPE_DEF, so cl.structs names it by its alias.
    """
    def text(first, last):
        return "\n".join(line.code.strip() for line in lines[first:last + 1] if line.code.strip())

    out = {}
    for i, m in top_types(lines):
        last = i
        if braces:
            # 1: Find the opening brace: on this line, or below it before the next top-level statement.
            start = None
            for j in range(i, min(i + 4, len(lines))):
                code = lines[j].code
                if j > i and code.strip() and lines[j].raw[:1] not in (" ", "\t") and not code.strip().startswith("{"):
                    break
                if "{" in code:
                    start = j
                    break
                if ";" in code:
                    break
            # 2: Balance it on masked code, where a brace in a string is gone.
            depth = 0
            for j in range(start, len(lines)) if start is not None else ():
                depth += lines[j].code.count("{") - lines[j].code.count("}")
                if depth <= 0:
                    last = j
                    break
        else:
            for j in range(i + 1, len(lines)):
                code = lines[j].code.strip()
                if code and code != '"':
                    if lines[j].raw[:1] not in (" ", "\t"):
                        break
                    last = j
        out[m.group("name")] = (i + 1, text(i, last))
    for s in cl.structs(lines, family) if family == "c" else ():
        if not s.name and s.aliases:
            out[s.aliases[0]] = (s.line, text(s.first - 1, s.last - 1))
    return out


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
    """Count whole-word references to `names` in code, never in comments or strings: return (refs, calls).

    `calls` counts the references followed by '(' (calls, definitions,
    prototypes); the others use the function as a value, such as a callback
    in a method table, which no inlining can replace. Inside git the files
    come from `git grep` over the whole work tree; outside git only `files`
    are read. A forward declaration counts as a reference, so a prototyped C
    helper is never reported: a false negative is cheaper than a false alarm.
    """
    names = {n for n in names if SIMPLE_NAME.match(n)}
    if not names:
        return Counter(), Counter()
    if root:
        args = ["grep", "-l", "-I", "--untracked", "-w"]
        for name in sorted(names):
            args += ["-e", name]
        files = [os.path.join(root, rel) for rel in (cl.git(args, root) or "").splitlines() if cl.family_of(rel)]
    pattern = re.compile(r"(?<![\w$])(%s)(?![\w$])(\s*\()?" % "|".join(map(re.escape, sorted(names))))
    refs, calls = Counter(), Counter()
    for path in files:
        text = cl.read_text(path)
        if text is None or not any(n in text for n in names):
            continue
        code = "\n".join(line.code for line in cl.scan(text, cl.family_of(path)))
        for m in pattern.finditer(code):
            refs[m.group(1)] += 1
            calls[m.group(1)] += m.group(2) is not None
    return refs, calls


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


def grid_diagrams(lines):
    """Return (first ruler's line, rulers) for each comment block that draws a multi-column box grid."""
    out = []
    for is_comment, group in itertools.groupby(enumerate(lines, 1),
                                               key=lambda pair: pair[1].comment is not None and not pair[1].code.strip()):
        rows = [no for no, line in group if is_comment and GRID_RULER.search(line.comment)]
        if len(rows) >= GRID_RULERS:
            out.append((rows[0], len(rows)))
    return out


def report(paths):
    """Print module metrics and findings as minified columnar JSON; always exit 0."""
    root = cl.repo_root(os.getcwd())
    files = code_files(paths, root)
    search = assertion_search(root, files)
    rows, finds, helpers = [], [], []
    all_long_loc = all_guides = function_count = all_comments = all_code = 0

    # Per-file metrics: size, interface width, rhythm and Guide density, helper candidates.
    for path in files:
        text = cl.read_text(path)
        if text is None:
            continue
        rel = cl.shown(path)
        lines, funcs = functions(path, text)
        family, braces = cl.family_of(path), os.path.splitext(path)[1].lower() in BRACE
        exported = exported_names(path, text, lines, funcs, root)
        total = sum(1 for line in lines if line.code.strip())
        public = sum(f.public for f in funcs)
        long_fns = bad = long_loc = guides = 0
        function_count += len(funcs)
        for f in funcs:
            loc, gap = body_stats(lines, f)
            if loc >= LONG_FN and not test_code(f, rel):
                long_fns += 1
                long_loc += loc
                body = lines[f.first - 1:f.last]
                guides += sum(1 for i, line in enumerate(body) if line.comment is not None and not line.code.strip()
                              and not (i and body[i - 1].comment is not None and not body[i - 1].code.strip()))
                if rhythm_due(f, rel, loc, gap, profile(lines, f, braces)):
                    bad += 1
                    finds.append([rel, f.line, "RHYTHM", f.name, gap])
            if helper_candidate(f, rel):
                helpers.append((rel, f))
            if doc_due(f, loc, rel, exported, lines):
                finds.append([rel, f.line, "NO-DOC", f.name, loc])
        dmod = round(total / public, 1) if public else None
        if dmod is not None and public >= 2 and dmod < MIN_DMOD and not TEST_PATH.search(rel) \
                and not rel.endswith(HEADER_EXT):
            finds.append([rel, 1, "DMOD", "", dmod])
        all_long_loc += long_loc
        all_guides += guides

        # Comment coverage at module level: Design header, Teacher diagrams, layout assertions.
        everything = set(range(1, len(lines) + 1))
        if design_missing(lines, rel) and module_traits(lines, family):
            finds.append([rel, 1, "NO-DESIGN", "", total])
        layout = layout_unexplained(lines, family, everything)
        if layout:
            finds.append([rel, layout[0], "NO-TEACHER", "", layout[1]])
        for line_no, name, evidence in layout_unasserted(lines, family, everything, search):
            finds.append([rel, line_no, "NO-LAYOUT-ASSERT", name, evidence])
        for line_no, rulers in grid_diagrams(lines):
            finds.append([rel, line_no, "GRID", "", rulers])
        comments = sum(1 for line in lines if line.comment is not None)
        all_comments += comments
        all_code += total
        rows.append([rel, total, public, dmod, long_fns, bad, 0, round(long_loc / guides, 1) if guides else None,
                     round(comments / total, 2) if total else None])

    # Single-caller pass-throughs need repository-wide reference counts.
    refs, calls = reference_counts(root, {f.name for _, f in helpers}, files)
    per_file = Counter()
    for rel, f in helpers:
        if refs[f.name] == calls[f.name] == 2:
            per_file[rel] += 1
            finds.append([rel, f.line, "HELPER", f.name, f.forward])
    for row in rows:
        row[6] = per_file[row[0]]

    fn40 = sum(r[4] for r in rows)
    kinds = Counter(f[2] for f in finds)
    total = {"files": len(rows), "functions": function_count, "fn40": fn40,
             "rhythm_ok": round(1 - sum(r[5] for r in rows) / fn40, 3) if fn40 else 1.0,
             "guide_density": round(all_long_loc / all_guides, 1) if all_guides else None,
             "helpers1": sum(per_file.values()), "dmod_lt15": kinds["DMOD"], "no_doc": kinds["NO-DOC"],
             "no_design": kinds["NO-DESIGN"], "no_teacher": kinds["NO-TEACHER"],
             "no_layout_assert": kinds["NO-LAYOUT-ASSERT"], "grid": kinds["GRID"],
             "comment_ratio": round(all_comments / all_code, 2) if all_code else None}
    print(json.dumps({"c": ["file", "loc", "pub", "dmod", "fn40", "rhythm_bad", "helpers1", "density", "comment_ratio"], "r": rows,
                      "n": len(rows), "f": {"c": ["file", "line", "kind", "name", "value"], "r": finds,
                                            "n": len(finds)}, "total": total}, separators=(",", ":")))
    return 0


def gate_findings(root, changes):
    """Stop gate: return (key, message) review signals for the code files `changes` lists.

    RHYTHM for long and complex functions the change makes harder to read
    (new, or with a longer run); HELPER and NO-DOC for new functions; NO-DESIGN for new modules; NO-TEACHER and NO-LAYOUT-ASSERT
    for layout-sensitive code the change wrote; one NO-TEST when the change
    alters a type or adds an exported function and touches no test file (a
    file outside `changes` cannot count). Every message is a question
    the author may answer "none": a signal invites a look, it never decides.
    DMOD stays in --report: no count decides whether a module is shallow.
    Keys omit line numbers, which shift with every edit; craft-gate.py shows
    each key once per session.
    """
    findings, helpers, untested = [], [], []
    for ch in changes:
        family = cl.family_of(ch.rel)
        if not family:
            continue
        rel, added, new = ch.rel, ch.added, ch.new
        lines, funcs = functions(ch.path, ch.text)
        old_lines, old_funcs = functions(ch.path, ch.old) if not new else ([], [])
        old = {f.name for f in old_funcs}
        old_gaps = {f.name: body_stats(old_lines, f)[1] for f in old_funcs}
        braces, exported = os.path.splitext(ch.rel)[1].lower() in BRACE, None

        # RHYTHM for long and complex functions this change makes harder to read; HELPER and NO-DOC for new ones.
        # Why the old gap: on redis, 28 of 200 commits had no signal but RHYTHM for a one-line fix inside a function
        # that had been long for years, and the author of such a fix did not create the run.
        for f in funcs:
            loc, gap = body_stats(lines, f)
            if loc >= LONG_FN and gap > GUIDE_EVERY and gap > old_gaps.get(f.name, 0) \
                    and added.intersection(range(f.start, f.last + 2)):
                shape = profile(lines, f, braces)
                if rhythm_due(f, rel, loc, gap, shape):
                    findings.append(("metrics|%s|rhythm|%s" % (rel, f.name),
                                     "%s:%d: [RHYTHM] '%s' has %d LOC, %d branches, nesting %d and %d code lines in "
                                     "a row without a Guide comment: which question would a Guide comment answer "
                                     "here? If none, leave the code as it is."
                                     % (rel, f.line, f.name, loc, shape[0], shape[1], gap)))
            if f.name in old:
                continue
            if helper_candidate(f, rel):
                helpers.append((rel, f))
            exported = exported_names(ch.path, ch.text, lines, funcs, root) if exported is None else exported
            why = doc_due(f, loc, rel, exported, lines)
            if why:
                findings.append(("metrics|%s|doc|%s" % (rel, f.name), "%s:%d: [NO-DOC] '%s' %s and has no Function "
                                 "comment: %s" % ((rel, f.line, f.name) + DOC_QUESTIONS[why])))
            if f.name in exported and f.container != "func" and not test_code(f, rel):
                untested.append((rel, f.line, "function '%s' is new and exported" % f.name))

        # NO-TEST candidates among the types: compared by masked code, so a comment is no change.
        if not TEST_PATH.search(rel):
            before = type_bodies(old_lines, family, braces)
            for name, (line_no, body) in type_bodies(lines, family, braces).items():
                if name not in before or before[name][1] != body:
                    untested.append((rel, line_no, "type '%s' is %s" % (name, "changed" if name in before else "new")))

        # NO-DESIGN only for new modules that hold something a reader must learn first.
        traits = module_traits(lines, family) if new and design_missing(lines, rel) else []
        if traits:
            findings.append(("metrics|%s|design" % rel,
                             "%s:1: [NO-DESIGN] new module with %s and no Design comment at the top: what must a "
                             "reader know before changing it (problem, layout, invariants, limitations), or where "
                             "are the module's design notes?" % (rel, " and ".join(traits))))

        # NO-TEACHER and NO-LAYOUT-ASSERT for layout-sensitive code this change wrote.
        layout = layout_unexplained(lines, family, added)
        if layout:
            findings.append(("metrics|%s|teacher" % rel,
                             "%s:%d: [NO-TEACHER] layout-sensitive code (%s) without a Teacher diagram nearby: which "
                             "bytes go where? A [field:size] strip, a byte-offset table or a -> chain answers it."
                             % (rel, layout[0], layout[1])))
        for line_no, name, evidence in layout_unasserted(lines, family, added, assertion_search(root, [ch.path])):
            findings.append(("metrics|%s|layout|%s" % (rel, name),
                             "%s:%d: [NO-LAYOUT-ASSERT] struct '%s' has a physical layout (%s) but no size or "
                             "offset assertion: what stops the code drifting from its diagram? A "
                             "static_assert(sizeof/offsetof) or its language form (see craft-code) lets the "
                             "compiler check it." % (rel, line_no, name, evidence)))

    refs, calls = reference_counts(root, {f.name for _, f in helpers}, [])
    for rel, f in helpers:
        if refs[f.name] == calls[f.name] == 2:
            findings.append(("metrics|%s|helper|%s" % (rel, f.name),
                             "%s:%d: [HELPER] '%s' only hands its parameters to '%s' and has one caller: what does "
                             "the extra name tell the reader? If nothing, call '%s' there."
                             % (rel, f.line, f.name, f.forward, f.forward)))

    # One NO-TEST per session: the key names no file, so a later change cannot show it again.
    # Why the filter: the gate drops findings by key, which here names no file, and nobody tests generated code by hand.
    if untested:
        texts = {ch.rel: ch.text for ch in changes}
        skip = cl.generated(root, {rel for rel, _, _ in untested}, texts.get)
        untested = [item for item in untested if item[0] not in skip]
    if untested and not any(TEST_PATH.search(ch.rel) for ch in changes):
        rel, line_no, what = untested[0]
        more = " (and %d more)" % (len(untested) - 1) if len(untested) > 1 else ""
        findings.append(("metrics|session|no-test",
                         "%s:%d: [NO-TEST] %s%s and no test file changed in this session: which test would catch a "
                         "mistake here? If none is needed, leave it as it is." % (rel, line_no, what, more)))
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", action="store_true", required=True, help="print metrics as columnar JSON")
    parser.add_argument("paths", nargs="*", help="files or directories for --report")
    return report(parser.parse_args().paths)


if __name__ == "__main__":
    sys.exit(main())
