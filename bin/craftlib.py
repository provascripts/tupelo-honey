"""craftlib -- shared lexer, struct parser and git plumbing for the tupelo-honey scripts.

Design notes for bin/ (craft-comment-sync.py, craft-metrics.py and this module):

  The scripts run as Claude Code hooks after every edit and at every Stop, so
  they must be fast, dependency-free (Python 3.8+ stdlib) and silent: they
  print nothing and exit 0 unless they found a violation. Findings go to
  stderr with exit code 2, the only code Claude Code feeds back to the model;
  run by hand, the same findings go to stdout with exit code 1.

  Parsing is lexical, not syntactic. scan() turns a file into one
  Line(code, comment, kind, raw) per source line. _SPEC lists, per language
  family, the tokens that open a comment or a literal and how each one ends;
  one regex search jumps from opener to opener:

      'x = f("//"); // why: f is idempotent'
          -> Line(code='x = f(""); ', comment='why: f is idempotent', kind='line', raw=<the line>)

  String and regex literals collapse to "" so that braces, quotes and comment
  markers inside them can never be mistaken for structure. Everything above
  this layer (brace matching, identifiers, comment checks, struct fields)
  reads that output.

  Languages (FAMILY maps the extensions; any other extension is skipped,
  never guessed): C, C++, Objective-C, CUDA, GLSL/HLSL, Java, C#, Kotlin,
  Swift, Scala, Groovy, Dart, Solidity, PHP, Go, Rust, Zig, JavaScript,
  TypeScript, Python, shell, Ruby, Perl, R, Elixir, Crystal, CoffeeScript,
  Tcl, Julia, Nim, PowerShell, Lua, SQL, Haskell, Elm, PureScript, Idris,
  Common Lisp, Scheme, Racket, Clojure, Emacs Lisp, Fennel, OCaml, F#,
  Erlang, Fortran, Visual Basic.

  Known limitations (deliberate sacrifices):
  - Nested block comments (Rust, Swift, Kotlin, Scala, Haskell, OCaml), C++
    raw strings R"(...)", heredocs and Swift raw strings #"..."# are not
    recognized.
  - A JS regex literal is told apart from a division by the previous code
    character on the same line only; a shell '#' opens a comment only at the
    start of a word.
  - Struct fields are parsed for C, C++, C#, Rust, Zig, Go, Swift and Python
    ctypes only; widths are known only for fixed-width types, never for int,
    long, size_t or pointers, whose size depends on the platform (LP64 vs
    LLP64).
"""

import json
import os
import re
import subprocess
import sys
from collections import namedtuple

FAMILY = {}
for _family, _exts in {
    "c": ".c .h .cc .cpp .cxx .hh .hpp .java .m .mm .cu .cuh .ino .glsl .hlsl .vert .frag",
    "cs": ".cs",
    "kt": ".kt .kts .swift .scala .sc .groovy .gradle .dart .sol",
    "php": ".php",
    "go": ".go", "rs": ".rs", "zig": ".zig",
    "js": ".js .jsx .mjs .cjs .ts .tsx",
    "py": ".py .pyi .pyw",
    "sh": ".sh .bash .zsh .ksh .rb .rake .gemspec .pl .pm .r .ex .exs .cr .coffee .tcl",
    "jl": ".jl", "nim": ".nim", "ps": ".ps1 .psm1",
    "lua": ".lua", "sql": ".sql",
    "hs": ".hs .elm .purs .idr",
    "lisp": ".lisp .lsp .el .scm .ss .rkt .clj .cljs .cljc .fnl",
    "ml": ".ml .mli .fs .fsi .fsx",
    "erl": ".erl .hrl", "f90": ".f .for .f90 .f95 .f03 .f08", "vb": ".vb .bas .vbs",
}.items():
    for _ext in _exts.split():
        FAMILY[_ext] = _family
MAX_BYTES = 1 << 20
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
IDENT = re.compile(r"[A-Za-z_$][\w$]*")

# A comment touching a line sets `comment` (possibly "") and `kind`: 'line'
# (//, #, --, ;...), 'block' (/* */ on one line), 'multi' (a block comment
# across lines) or 'doc' (///, //!, /** */, docstrings...). Both are None
# otherwise. `raw` is the original line, for indentation and raw literals.
Line = namedtuple("Line", "code comment kind raw")

# Openers per family, tried left to right at each position: longer tokens
# first. Handlers: line (comment to end of line), block (comment up to the
# third element), str (string, backslash escapes, one line), mstr (same, may
# span lines), doubled (quote escaped by doubling it), triple (triple-quoted
# string; a docstring in Python), verbatim (C# @"..."), goraw (Go `...`),
# template (JS `...`), rsraw (Rust r#"..."#), zigline (Zig \\ line string),
# long (Lua [==[ ]==], a comment when it starts with --), char (one character
# or one escape, else plain code: Rust lifetimes, Haskell primes), slash (JS
# regex literal or division), code (plain code, e.g. Lisp #\" characters).
_C = [("//", "line"), (r"/\*", "block", "*/")]
_SPEC = {
    "c": _C + [('"""', "triple"), ('"', "str"), ("'", "char")],
    "cs": _C + [('@"', "verbatim"), ('"""', "triple"), ('"', "str"), ("'", "char")],
    "kt": _C + [('"""', "triple"), ("'''", "triple"), ('"', "str"), ("'", "str")],
    "php": [("//", "line"), (r"#(?!\[)", "line"), (r"/\*", "block", "*/"), ('"', "mstr"), ("'", "mstr")],
    "go": _C + [('"', "str"), ("`", "goraw"), ("'", "char")],
    "rs": _C + [(r'(?<!\w)b?r#*"', "rsraw"), ('"', "mstr"), ("'", "char")],
    "zig": [("//", "line"), (r"\\\\", "zigline"), ('"', "str"), ("'", "char")],
    "js": _C + [('"', "str"), ("'", "str"), ("`", "template"), ("/", "slash")],
    "py": [("#", "line"), ('"""', "triple"), ("'''", "triple"), ('"', "str"), ("'", "str")],
    "sh": [(r"(?:^|(?<=[\s;|&(]))#", "line"), ('"', "mstr"), ("'", "mstr")],
    "jl": [("#=", "block", "=#"), ("#", "line"), ('"""', "triple"), ('"', "mstr"), ("'", "char")],
    "nim": [(r"#\[", "block", "]#"), ("#", "line"), ('"""', "triple"), ('"', "str"), ("'", "char")],
    "ps": [("<#", "block", "#>"), ("#", "line"), ('"', "mstr"), ("'", "mstr")],
    "lua": [(r"--\[=*\[", "long"), ("--", "line"), (r"\[=*\[", "long"), ('"', "str"), ("'", "str")],
    "sql": [("--", "line"), (r"/\*", "block", "*/"), ("'", "doubled"), ('"', "doubled")],
    "hs": [(r"\{-", "block", "-}"), (r"--(?![!#$%&*+./<=>?@\\^|~:])", "line"), ('"', "str"), ("'", "char")],
    "lisp": [(r"#\|", "block", "|#"), (";", "line"), (r"#\\.", "code"), ('"', "mstr")],
    "ml": [(r"\(\*(?!\))", "block", "*)"), ("//", "line"), ('"', "mstr"), ("'", "char")],
    "erl": [("%", "line"), ('"', "mstr"), ("'", "str")],
    "f90": [("!", "line"), ('"', "doubled"), ("'", "doubled")],
    "vb": [("'", "line"), (r"(?:^|(?<=\s))[Rr][Ee][Mm]\b", "line"), ('"', "doubled")],
}
_OPENERS = {f: re.compile("|".join("(%s)" % e[0] for e in spec), re.M) for f, spec in _SPEC.items()}
_STR = {q: re.compile(r"(?:\\.|[^%s\\\n])*%s" % (q, q)) for q in "\"'"}
_MSTR = {q: re.compile(r"(?:\\.|[^%s\\])*%s" % (q, q), re.S) for q in "\"'"}
_DOUBLED = {q: re.compile(r"(?:[^%s]|%s%s)*%s" % (q, q, q, q), re.S) for q in "\"'"}
_TRIPLE = {q * 3: re.compile(r"(?:\\.|[^\\])*?%s" % (q * 3), re.S) for q in "\"'"}
_TEMPLATE = re.compile(r"(?:\\.|[^`\\])*`", re.S)
# One character or one escape: anything longer is a Rust lifetime, an OCaml
# type variable or a Haskell prime, which are plain code.
_CHAR = re.compile(r"'(?:\\[^\n]{1,10}?|[^'\\\n])'")
_REGEX_LIT = re.compile(r"/(?![*/])(?:\\.|\[(?:\\.|[^\]\\\n])*\]|[^/\\\n\[])+/[A-Za-z]*")
_REGEX_PREV = re.compile(r"(?:^|[(,=:\[!&|?{};+\-*%<>~^]|\b(?:return|typeof|case|do|else|in|of|yield|await|void|delete|throw|new))$")


def family_of(path):
    """Return the lexer family for `path` from its extension, or None."""
    return FAMILY.get(os.path.splitext(path)[1].lower())


def scan(text, family):
    """Split source text into one Line(code, comment, kind, raw) per source line.

    `code` keeps everything outside comments, with each string or regex
    literal reduced to "" (a multi-line literal leaves a lone " on every line
    it crosses, so those lines still count as code). `comment` is the comment
    text without delimiters or leading '*'. Never raises on malformed input:
    an unterminated literal or comment simply runs to the end of its line or
    of the file. Line endings are normalized, so `\\r\\n` files work too.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    n = text.count("\n") + 1
    code = [[] for _ in range(n)]
    com = [None] * n
    kind = [None] * n
    spec, opener = _SPEC[family], _OPENERS[family]
    ln = 0

    # Emitters: each appends a chunk to the per-line buffers and advances the
    # current line `ln` past every newline the chunk contains.
    def put_code(s):
        nonlocal ln
        for i, part in enumerate(s.split("\n")):
            if i:
                ln += 1
            code[ln].append(part)

    def put_literal(s):
        nonlocal ln
        code[ln].append('""')
        for _ in range(s.count("\n")):
            ln += 1
            code[ln].append('"')

    def put_comment(s, k):
        nonlocal ln
        for i, part in enumerate(s.split("\n")):
            if i:
                ln += 1
            part = part.strip()
            if k in ("multi", "doc"):
                part = part.lstrip("*").strip()
            com[ln] = part if com[ln] is None else (com[ln] + " " + part).strip()
            kind[ln] = kind[ln] or k

    def until(close, start):
        j = text.find(close, start)
        return (len(text), len(text)) if j < 0 else (j, j + len(close))

    # Jump from opener to opener: everything in between is plain code.
    pos = 0
    while True:
        m = opener.search(text, pos)
        if not m:
            put_code(text[pos:])
            break
        i, tok = m.start(), m.group()
        entry = spec[m.lastindex - 1]
        how = entry[1]
        put_code(text[pos:i])

        # Comments.
        if how == "line":
            end = until("\n", m.end())[0]
            body = text[m.end():end]
            doc = (tok == "//" and body[:1] in ("/", "!")) or (family == "nim" and body[:1] == "#") \
                or (family == "lua" and body[:1] == "-") or (family == "hs" and body.lstrip()[:1] in ("|", "^"))
            put_comment(body[1:] if doc and family != "hs" else body, "doc" if doc else "line")
        elif how == "block":
            inner_end, end = until(entry[2], m.end())
            inner = text[m.end():inner_end]
            doc = (tok == "/*" and inner.startswith("*") and not inner.startswith("*/")) \
                or (tok == "{-" and inner.startswith("|")) or (tok.startswith("(*") and inner.startswith("*"))
            put_comment(inner, "doc" if doc else ("multi" if "\n" in inner else "block"))
        elif how == "long":
            inner_end, end = until("]" + "=" * tok.count("=") + "]", m.end())
            if tok.startswith("--"):
                inner = text[m.end():inner_end]
                put_comment(inner, "multi" if "\n" in inner else "block")
            else:
                put_literal(text[i:end])

        # Literals whose opener is ambiguous: JS regex vs division, char vs lifetime.
        elif how == "slash":
            lit = _REGEX_LIT.match(text, i) if _REGEX_PREV.search("".join(code[ln]).rstrip()) else None
            end = lit.end() if lit else i + 1
            (put_literal if lit else put_code)(text[i:end])
        elif how == "char":
            lit = _CHAR.match(text, i)
            end = lit.end() if lit else i + 1
            (put_literal if lit else put_code)(text[i:end])
        elif how == "code":
            end = m.end()
            put_code(tok)

        # Plain literals.
        elif how == "triple":
            body = _TRIPLE[tok].match(text, m.end())
            end = body.end() if body else len(text)
            prefix = "".join(code[ln]).strip()
            # A triple-quoted string opening a Python statement is a docstring.
            if family == "py" and prefix in ("", "r", "R", "u", "U"):
                code[ln] = []
                put_comment(text[m.end():end - 3 if body else end], "doc")
            else:
                put_literal(text[i:end])
        else:
            quote = tok[-1]
            pattern = {"str": _STR.get(quote), "mstr": _MSTR.get(quote), "doubled": _DOUBLED.get(quote),
                       "verbatim": _DOUBLED['"'], "template": _TEMPLATE}.get(how)
            if how == "goraw":
                end = until("`", m.end())[1]
            elif how == "zigline":
                end = until("\n", m.end())[0]
            elif how == "rsraw":
                end = until('"' + "#" * tok.count("#"), m.end())[1]
            else:
                body = pattern.match(text, m.end())
                end = body.end() if body else until("\n", m.end())[0]
            put_literal(text[i:end])
        pos = end

    raws = text.split("\n")
    return [Line("".join(code[i]), com[i], kind[i], raws[i]) for i in range(n)]


def code_identifiers(lines):
    """Return the set of identifiers that appear in code (never in comments or strings)."""
    out = set()
    for line in lines:
        out.update(IDENT.findall(line.code))
    return out


# --------------------------------------------------------------------------
# Structs and their layout diagrams.
#
# A Teacher diagram describes a physical layout with cells named after the
# fields, in declaration order, each with its width:
#
#     [len:1B][alloc:1B][flags:1B][buf: len bytes]
#
# B/byte(s) are bytes, b/bit(s) bits; a bare number is bits for a bitfield,
# bytes otherwise. header, hdr, pad, padding and payload are synthetic cells:
# they need no matching field, and a sized header must equal the summed
# width of the leading fields no cell names (raxNode's [header:32b] covers
# iskey:1, isnull:1, iscompr:1 and size:29).
# --------------------------------------------------------------------------

Field = namedtuple("Field", "name bits bitfield")
# line: the definition line; first..last: the lines from '{' to '}' (the
# indented body for Python); packed: a layout marker applies (packed,
# #[repr], extern struct, [StructLayout], ctypes).
Struct = namedtuple("Struct", "name aliases line first last fields packed")

CELL = re.compile(r"\[\s*(?P<name>[A-Za-z_][\w.-]*)\s*:\s*(?P<size>[^\]\n]*)\]")
CELL_SIZE = re.compile(r"^(?P<num>\d+)\s*(?P<unit>B|bytes?|b|bits?)?(?!\w)")
OFFSETS = re.compile(r"\b\d+\s*\.\.\s*\d+\b")
SYNTHETIC = re.compile(r"^(?:header|hdr|pad|padding|payload)\d*$", re.I)

_BITS = {
    "c": {"char": 8, "signed char": 8, "unsigned char": 8, "bool": 8, "_Bool": 8, "int8_t": 8, "uint8_t": 8,
          "int16_t": 16, "uint16_t": 16, "int32_t": 32, "uint32_t": 32, "int64_t": 64, "uint64_t": 64,
          "float": 32, "double": 64},
    "cs": {"byte": 8, "sbyte": 8, "bool": 8, "short": 16, "ushort": 16, "char": 16, "int": 32, "uint": 32,
           "float": 32, "long": 64, "ulong": 64, "double": 64},
    "rs": {"u8": 8, "i8": 8, "bool": 8, "u16": 16, "i16": 16, "u32": 32, "i32": 32, "f32": 32, "char": 32,
           "u64": 64, "i64": 64, "f64": 64, "u128": 128, "i128": 128},
    "go": {"uint8": 8, "int8": 8, "byte": 8, "bool": 8, "uint16": 16, "int16": 16, "uint32": 32, "int32": 32,
           "float32": 32, "rune": 32, "uint64": 64, "int64": 64, "float64": 64},
    "kt": {"UInt8": 8, "Int8": 8, "Bool": 8, "UInt16": 16, "Int16": 16, "UInt32": 32, "Int32": 32, "Float": 32,
           "UInt64": 64, "Int64": 64, "Double": 64},
    "py": {"c_uint8": 8, "c_int8": 8, "c_ubyte": 8, "c_byte": 8, "c_char": 8, "c_bool": 8, "c_uint16": 16,
           "c_int16": 16, "c_ushort": 16, "c_short": 16, "c_uint32": 32, "c_int32": 32, "c_float": 32,
           "c_uint64": 64, "c_int64": 64, "c_double": 64, "c_ulonglong": 64, "c_longlong": 64},
}
_QUALIFIERS = re.compile(r"\b(?:const|volatile|static|public|private|internal|protected|readonly|fixed|mut|pub|"
                         r"struct|union|enum)\b|\bpub\([^)]*\)")
_STRUCT_OPEN = {
    "c": re.compile(r"\b(?:struct|union)\b(?P<head>[^;{}()=]*(?:\(\([^)]*\)\)[^;{}()=]*)*)\{"),
    "cs": re.compile(r"\bstruct\s+(?P<head>\w+)[^;{}=]*\{"),
    "rs": re.compile(r"\bstruct\s+(?P<head>\w+)(?:<[^>{]*>)?\s*(?:where[^{]*)?\{"),
    "zig": re.compile(r"\bconst\s+(?P<head>\w+)\s*=\s*(?P<mod>(?:packed|extern)\s+)?struct\b[^{=]*\{"),
    "go": re.compile(r"\btype\s+(?P<head>\w+)\s+struct\s*\{"),
    "kt": re.compile(r"\bstruct\s+(?P<head>\w+)[^{=]*\{"),
}
_PY_STRUCT = re.compile(r"^\s*class\s+(?P<head>\w+)\s*\([^)]*\b(?:Structure|Union|LittleEndianStructure|"
                        r"BigEndianStructure)\b[^)]*\)\s*:")
_PY_FIELD = re.compile(r"\(\s*[\"'](?P<name>\w+)[\"']\s*,\s*(?:ctypes\.)?(?P<type>c_\w+)(?:\s*\*\s*(?P<arr>\d+))?"
                       r"(?:\s*,\s*(?P<bits>\d+))?\s*\)")
_MARKER_ABOVE = re.compile(r"#\[repr\(|\[StructLayout\(|#\s*pragma\s+pack|@frozen\b|__attribute__\s*\(\(\s*_*packed")


def type_bits(type_text, family, packed=False):
    """Return the width in bits of a field type, or None when it depends on the platform."""
    t = " ".join(_QUALIFIERS.sub(" ", type_text).split())
    if not t or "*" in t or "&" in t:
        return None
    arr = re.fullmatch(r"\[\s*(\d+)\s*\]\s*(\w+)", t) or re.fullmatch(r"\[\s*(\w+)\s*;\s*(\d+)\s*\]", t)
    if arr:
        count, elem = (arr.group(1), arr.group(2)) if t.startswith("[") and t[1:].lstrip()[:1].isdigit() \
            else (arr.group(2), arr.group(1))
        inner = type_bits(elem, family, packed)
        return inner * int(count) if inner else None
    if family == "zig":
        m = re.fullmatch(r"[ui](\d+)|f(16|32|64|80|128)", t)
        if m:
            return int(m.group(1) or m.group(2))
        return (1 if packed else 8) if t == "bool" else None
    return _BITS.get(family, {}).get(t)


def _c_fields(statement, family, packed):
    """Return the Fields declared by one C, C++ or C# struct member statement.

    Why: C declarators (bitfields, arrays, function pointers, several names
    sharing one type) are a small grammar of their own; kept apart from
    structs() so that the brace walk there stays readable.
    """
    if re.search(r"\(\s*\*\s*(\w+)\s*\)", statement):
        return [Field(re.search(r"\(\s*\*\s*(\w+)\s*\)", statement).group(1), None, False)]
    if "(" in statement or "{" in statement and not re.search(r"}\s*\w+\s*$", statement):
        return []
    if "{" in statement:
        return [Field(re.search(r"(\w+)\s*$", statement).group(1), None, False)]
    parts = statement.split(",")
    head = re.match(r"^(?P<type>.*?)\b(?P<name>[A-Za-z_]\w*)\s*(?:\[\s*(?P<arr>\w*)\s*\])?\s*(?::\s*(?P<bits>\d+))?\s*$",
                    parts[0].strip())
    if not head or not head.group("type").strip():
        return []
    out = []
    for i, part in enumerate(parts):
        m = head if i == 0 else re.match(r"^\s*\**\s*(?P<name>[A-Za-z_]\w*)\s*(?:\[\s*(?P<arr>\w*)\s*\])?"
                                         r"\s*(?::\s*(?P<bits>\d+))?\s*$", part)
        if not m:
            continue
        if m.group("bits"):
            out.append(Field(m.group("name"), int(m.group("bits")), True))
            continue
        bits = type_bits(head.group("type") + ("*" if "*" in part else ""), family, packed)
        arr = m.group("arr")
        if arr is not None:
            bits = bits * int(arr) if bits and arr.isdigit() else None
        out.append(Field(m.group("name"), bits, False))
    return out


def _split_members(body, separators):
    """Split a struct body into member statements at depth 0, keeping nested blocks whole."""
    out, depth, cur = [], 0, []
    for ch in body:
        if ch in "{(<[":
            depth += 1
        elif ch in "})>]":
            depth = max(0, depth - 1)
        if ch in separators and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur).strip())
    return [s for s in out if s]


def structs(lines, family):
    """Return the Structs defined in a scanned file (empty for families without struct parsing)."""
    if family == "py":
        return _py_structs(lines)
    pattern = _STRUCT_OPEN.get(family)
    if pattern is None:
        return []
    code = "\n".join(line.code for line in lines)
    starts = [0]
    for line in lines[:-1]:
        starts.append(starts[-1] + len(line.code) + 1)

    def line_of(off):
        n = 0
        lo, hi = 0, len(starts)
        while lo < hi:
            mid = (lo + hi) // 2
            if starts[mid] <= off:
                n, lo = mid, mid + 1
            else:
                hi = mid
        return n + 1

    out = []
    for m in pattern.finditer(code):
        # Match the body braces.
        open_at, depth, j = m.end() - 1, 0, m.end() - 1
        while j < len(code):
            depth += {"{": 1, "}": -1}.get(code[j], 0)
            if depth == 0:
                break
            j += 1
        body = code[open_at + 1:j]

        # Name, typedef alias and layout markers.
        if family == "c":
            words = [w for w in re.findall(r"[A-Za-z_]\w*", m.group("head"))
                     if w not in ("__attribute__", "__packed__", "packed", "aligned", "__aligned__")]
            name = words[-1] if words else ""
            alias = re.match(r"\s*\**\s*([A-Za-z_]\w*)\s*[;,]", code[j + 1:j + 200])
            aliases = [alias.group(1)] if alias else []
        else:
            name, aliases = m.group("head"), []
        line_no = line_of(m.start())
        above = "\n".join(line.code for line in lines[max(0, line_no - 4):line_no])
        packed = ("packed" in m.group(0) or bool(m.groupdict().get("mod")) or bool(_MARKER_ABOVE.search(above)))

        # Fields, one member statement at a time.
        fields = []
        if family in ("c", "cs"):
            for stmt in _split_members(body, ";"):
                fields += _c_fields(stmt, family, packed)
        elif family in ("rs", "zig"):
            for stmt in _split_members(body, ",\n"):
                f = re.match(r"^(?:#\[[^\]]*\]\s*)*(?:pub(?:\([^)]*\))?\s+)?(?P<name>[A-Za-z_]\w*)\s*:\s*(?P<type>[^=]+?)"
                             r"\s*(?:=.*)?$", stmt)
                if f and not re.match(r"(?:pub\s+)?(?:fn|const|var)\b", stmt):
                    fields.append(Field(f.group("name"), type_bits(f.group("type"), family, packed), False))
        elif family == "go":
            for stmt in _split_members(body, ";\n"):
                f = re.match(r"^(?P<names>[A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s+(?P<type>[^\s\"]+)", stmt)
                for fname in (f.group("names").split(",") if f else []):
                    fields.append(Field(fname.strip(), type_bits(f.group("type"), family, packed), False))
        else:
            for stmt in _split_members(body, ";\n"):
                f = re.match(r"^(?:\w+\s+)*(?:var|let)\s+(?P<name>\w+)\s*:\s*(?P<type>[\w.]+)\s*$", stmt)
                if f:
                    fields.append(Field(f.group("name"), type_bits(f.group("type"), family, packed), False))
        out.append(Struct(name, aliases, line_no, line_of(open_at), line_of(j), fields, packed))
    return out


def _py_structs(lines):
    """Return the ctypes Structures of a Python file, with fields read from the raw _fields_ list.

    Why: these structs are classes delimited by indentation with fields in a
    list of tuples, so they share nothing with the brace walk in structs().
    """
    out = []
    for i, line in enumerate(lines):
        m = _PY_STRUCT.match(line.raw)
        if not m:
            continue
        indent, last = len(line.raw) - len(line.raw.lstrip()), i
        for j in range(i + 1, len(lines)):
            raw = lines[j].raw
            if raw.strip():
                if len(raw) - len(raw.lstrip()) <= indent:
                    break
                last = j
        text = "\n".join(l.raw for l in lines[i + 1:last + 1])
        fields = []
        for f in _PY_FIELD.finditer(text):
            if f.group("bits"):
                fields.append(Field(f.group("name"), int(f.group("bits")), True))
            else:
                bits = type_bits(f.group("type"), "py")
                fields.append(Field(f.group("name"), bits * int(f.group("arr")) if bits and f.group("arr") else
                                    (None if f.group("arr") else bits), False))
        out.append(Struct(m.group("head"), [], i + 1, i + 2, last + 1, fields, True))
    return out


def diagram_window(lines, st):
    """Return (line number, comment) pairs that can document struct `st`'s layout.

    That is the contiguous comment block right above the definition
    (attribute lines skipped) plus every comment inside the struct body,
    where raxNode keeps its own layout diagram.
    """
    window, i = [], st.line - 2
    while i >= 0 and re.match(r"\s*(?:#\[|\[\w|@\w|#\s*pragma|__attribute__)", lines[i].code):
        i -= 1
    while i >= 0 and lines[i].comment is not None and not lines[i].code.strip():
        window.append((i + 1, lines[i].comment))
        i -= 1
    window.reverse()
    window += [(no, lines[no - 1].comment) for no in range(st.line, st.last + 1) if lines[no - 1].comment]
    return window


def read_text(path):
    """Return the file as text (UTF-8, undecodable bytes replaced), or None if unreadable or > 1 MiB."""
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return None
        with open(path, encoding="utf-8", errors="replace", newline="") as fh:
            return fh.read()
    except OSError:
        return None


def git(args, cwd):
    """Run git in `cwd` and return stdout, or None if git is missing or exits non-zero."""
    try:
        r = subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=cwd,
                           capture_output=True, encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def repo_root(cwd):
    """Return the top directory of the git work tree containing `cwd`, or None."""
    out = git(["rev-parse", "--show-toplevel"], cwd or ".")
    return out.strip() if out else None


def resolve_base(root, base):
    """Return `base` if it names a commit, else the empty tree (repository without commits)."""
    return base if git(["rev-parse", "--verify", "-q", base + "^{commit}"], root) else EMPTY_TREE


def changed_files(root, base):
    """List (relative path, is_new) for code files that differ from `base` or are untracked.

    is_new is True when the file has no version in `base`, so every one of
    its lines counts as added. Deleted files are skipped.
    """
    out = []
    tracked = git(["diff", "--name-only", "--diff-filter=AMR", base, "--"], root) or ""
    for rel in tracked.splitlines():
        if family_of(rel) and os.path.isfile(os.path.join(root, rel)):
            new = base == EMPTY_TREE or git(["cat-file", "-e", "%s:%s" % (base, rel)], root) is None
            out.append((rel, new))
    untracked = git(["ls-files", "--others", "--exclude-standard"], root) or ""
    for rel in untracked.splitlines():
        if family_of(rel) and os.path.isfile(os.path.join(root, rel)):
            out.append((rel, True))
    return out


def baseline_text(root, base, rel):
    """Return the content of `rel` at `base`, or '' when it does not exist there."""
    if base == EMPTY_TREE:
        return ""
    return git(["show", "%s:%s" % (base, rel)], root) or ""


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)


def added_lines(root, base, rel):
    """Return the 1-based line numbers of `rel` that are new or changed since `base`."""
    out = git(["diff", "-U0", "--no-color", "--no-ext-diff", base, "--", rel], root) or ""
    lines = set()
    for m in _HUNK.finditer(out):
        start = int(m.group(1))
        lines.update(range(start, start + int(m.group(2) or 1)))
    return lines


def hook_input():
    """Parse the hook JSON on stdin; {} when stdin is a terminal, empty or not a JSON object."""
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def shown(path):
    """Return `path` relative to the working directory when possible, with forward slashes."""
    try:
        path = os.path.relpath(path)
    except ValueError:
        pass
    return path.replace(os.sep, "/")


def report(lines, hook, limit=20):
    """Print findings and return the exit code: 0 clean, 2 in hook mode, 1 otherwise.

    Hook findings go to stderr because Claude Code shows the model only the
    stderr of a hook that exits with code 2.
    """
    if not lines:
        return 0
    lines = list(lines[:limit]) + (["... %d more" % (len(lines) - limit)] if len(lines) > limit else [])
    stream = sys.stderr if hook else sys.stdout
    # Why: Windows pipes default to the ANSI code page, which cannot encode
    # arbitrary comment text; fall back silently on streams without reconfigure.
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    stream.write("\n".join(lines) + "\n")
    return 2 if hook else 1
