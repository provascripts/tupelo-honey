"""craftlib -- shared lexer, struct parser, debt markers and git plumbing for the tupelo-honey scripts.

Design notes for bin/ (craft-debt-scan.py, craft-comment-sync.py,
craft-metrics.py, craft-gate.py and this module):

  The scripts run as Claude Code hooks, so they must be fast,
  dependency-free (Python 3.8+ stdlib) and silent: they print nothing and
  exit 0 unless they found a violation. Findings go to stderr with exit code
  2, the only code Claude Code feeds back to the model; run by hand, the same
  findings go to stdout with exit code 1. Every finding is one line,
  "path:line: [KIND] detail".

  The hooks work on two levels, so that a change spread over several edits
  is never interrupted half-way:

    PostToolUse   judges the lines one Edit, Write or MultiEdit wrote, and
                  only what those lines decide alone: debt markers, Backup
                  and Trivial comments (craft-debt-scan.py --hook,
                  craft-comment-sync.py --hook).
    Stop          judges every file the session changed, Bash edits included
                  (craft-gate.py): the same checks, [field:size] diagrams and
                  orphaned symbols (looked up in the whole repository) are hard
                  findings that block each Stop until fixed, at most 3 times
                  each; the craft-metrics review signals, orphans named only
                  by prose documents and the hard findings of a file the
                  lexer misreads (inexact_reason) are shown once per session
                  and never block: when nothing else does, Claude stops, the
                  user reads them at once and Claude gets them with the next
                  prompt (UserPromptSubmit, craft-gate.py --prompt).

  craft-gate.py --staged runs the Stop checks on the files staged for
  commit, so a git pre-commit hook can apply the same rules outside Claude
  Code.

  Parsing is lexical, not syntactic. scan() turns a file into one
  Line(code, comment, kind, raw, parts) per source line. _SPEC lists, per
  language family, the tokens that open a comment or a literal and how each
  one ends; one regex search jumps from opener to opener:

      'x = f("//"); // why: f is idempotent'
          -> Line(code='x = f(""); ', comment='why: f is idempotent', kind='line',
                  raw=<the line>, parts=('why: f is idempotent',))

  String and regex literals collapse to "" so that braces, quotes and comment
  markers inside them can never be mistaken for structure. Everything above
  this layer (brace matching, identifiers, comment checks, debt markers,
  struct fields) reads that output.

  Languages (FAMILY maps the extensions; any other extension is skipped,
  never guessed): C, C++, Objective-C, CUDA, GLSL/HLSL, Java, C#, Kotlin,
  Swift, Scala, Groovy, Dart, Solidity, PHP, Go, Rust, Zig, JavaScript,
  TypeScript, Python, shell, Ruby, Perl, R, Elixir, Crystal, CoffeeScript,
  Tcl, Julia, Nim, PowerShell, Lua, SQL, Haskell, Elm, PureScript, Idris,
  Common Lisp, Scheme, Racket, Clojure, Emacs Lisp, Fennel, OCaml, F#,
  Erlang, Fortran, Visual Basic.

  Known limitations (deliberate sacrifices):
  - Nested block comments (Rust, Swift, Kotlin, Scala, Dart, Haskell, OCaml,
    F#, Julia, Nim, Lisp), C++ raw strings R"(...)", heredocs (shell, Ruby,
    Crystal, Perl, PHP) and Swift raw strings #"..."# are not lexed.
    inexact_reason() spots them on scan()'s output, and the Stop gate turns
    the hard findings of such a file into review signals that name the
    construct. A real parser (tree-sitter) would read them, but it is not
    stdlib: it could only ever be an optional backend.
  - A JS regex literal is told apart from a division by the previous code
    character on the same line only; a shell '#' opens a comment only at the
    start of a word.
  - Debt markers in files without a lexer family (YAML, TOML, HTML,
    Dockerfile, Makefile...) are found by a line match on the usual comment
    openers, which cannot tell a quoted "# TODO" from a real comment.
  - Struct fields are parsed for C, C++, C#, Rust, Zig, Go, Swift and Python
    ctypes only; widths are known only for fixed-width types, never for int,
    long, size_t or pointers, whose size depends on the platform (LP64 vs
    LLP64). A struct with a member the parser does not understand (a macro,
    an anonymous union, an #if, a base class, a virtual method) is inexact:
    only the order and widths of the fields it did read are checked.
"""

import json
import os
import re
import subprocess
import sys
import time
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
# `parts` holds each comment of the line on its own ('a' and 'b' for
# `/* a */ x; // b`), where `comment` joins them: a debt marker must open one.
Line = namedtuple("Line", "code comment kind raw parts", defaults=((),))

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
    """Split source text into one Line(code, comment, kind, raw, parts) per source line.

    `code` keeps everything outside comments, with each string or regex
    literal reduced to "" (a multi-line literal leaves a lone " on every line
    it crosses, so those lines still count as code). `comment` is the comment
    text without delimiters or leading '*'; a line comment also loses the
    repeated opener characters of ';;', '%%' or '##'. Never raises on
    malformed input: an unterminated literal or comment simply runs to the
    end of its line or of the file. Line endings are normalized, so `\\r\\n`
    files work too.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    n = text.count("\n") + 1
    code = [[] for _ in range(n)]
    com = [None] * n
    kind = [None] * n
    parts = {}
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
            parts.setdefault(ln, []).append(part)

    # Where a closer starts and ends; both are len(text) when it is missing.
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
            body = body[1:] if doc and family != "hs" else body
            # Why: ';;' (Lisp), '%%' (Erlang) and '##' banners repeat the opener
            # as decoration; the comment text, and any debt marker, starts after.
            if not tok[-1].isalnum():
                body = body.lstrip(tok[-1])
            put_comment(body, "doc" if doc else "line")
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
    return [Line("".join(code[i]), com[i], kind[i], raws[i], tuple(parts.get(i, ()))) for i in range(n)]


def code_identifiers(lines):
    """Return the set of identifiers that appear in code (never in comments or strings)."""
    out = set()
    for line in lines:
        out.update(IDENT.findall(line.code))
    return out


# --------------------------------------------------------------------------
# Inexact files. scan() misreads each construct below from its first line on
# ("Known limitations" in the design notes): code may come out as comment,
# or the reverse. inexact_reason() spots them afterwards on scan()'s own
# output, so scan() stays one pass without lookahead, and the Stop gate
# turns a hard finding in such a file into a review signal naming why.
# --------------------------------------------------------------------------

# Nested comments: scan() ends "/* a /* b */ c */" at the first closer and
# reads " c */" as code. The opener is the family's own block opener.
_NESTING = frozenset(".rs .swift .kt .kts .scala .sc .dart .hs .elm .purs .idr .ml .mli .fs .fsi .fsx .jl .nim "
                     ".lisp .lsp .scm .ss .rkt".split())
_BLOCK_OPENER = {f: re.compile(e[0]) for f, spec in _SPEC.items() for e in spec if e[1] == "block"}
# Raw strings, as their opener shows in Line.code: scan() takes the first
# quote for a plain string, which the next inner quote or backslash breaks.
_RAW_STRINGS = (
    (frozenset(".c .h .cc .cpp .cxx .hh .hpp .cu .cuh .ino .mm".split()), re.compile(r'(?<!\w)(?:u8|[uUL])?R""'),
     "C++ raw string"),
    (frozenset([".swift"]), re.compile(r'#""'), "Swift raw string"),
)
# Heredocs: (extensions, opener on Line.raw capturing the closing tag,
# whether the closing line may go on after the tag, as PHP's "EOT;" does).
# An opener counts only when its line's code has '<<' (a comment or string
# that mentions one does not) and a later line closes it, so a shift such
# as $((a << b)) never counts. Ruby and Perl allow no space before a bare
# tag: "class << self" is no heredoc.
_HEREDOCS = (
    (frozenset(".sh .bash .zsh .ksh".split()), re.compile(r"(?<!<)<<-?[ \t]*[\\\"']?(?P<tag>[A-Za-z_]\w*)"), False),
    (frozenset(".rb .rake .gemspec .cr".split()), re.compile(r"(?<![<\w])<<[-~]?[\"'`]?(?P<tag>[A-Za-z_]\w*)"), False),
    (frozenset(".pl .pm".split()), re.compile(r"(?<![<\w])<<~?(?:[ \t]*[\"'`]|\\)?(?P<tag>[A-Za-z_]\w*)"), False),
    (frozenset([".php"]), re.compile(r"<<<[ \t]*[\"']?(?P<tag>[A-Za-z_]\w*)[\"']?[ \t]*$"), True),
)


def inexact_reason(path, text, lines=None):
    """Return why scan() misreads `text`, such as "heredoc at line 3", or None when it reads it exactly.

    `path` picks the language by its extension; pass scan()'s output for
    `text` as `lines` when you already have it. The reason names the first
    misread construct (see the table comments above) by its 1-based line.
    """
    ext = os.path.splitext(path)[1].lower()
    family = FAMILY.get(ext)
    if family is None:
        return None
    lines = scan(text, family) if lines is None else lines
    nested = _BLOCK_OPENER.get(family) if ext in _NESTING else None
    raw = [(pattern, name) for exts, pattern, name in _RAW_STRINGS if ext in exts]
    heredoc = next(((opener, loose) for exts, opener, loose in _HEREDOCS if ext in exts), None)
    closers = {}
    if heredoc:
        # Last index of each possible closing line: the stripped line, or its first word when loose.
        for i, line in enumerate(lines):
            word = IDENT.match(line.raw.strip()) if heredoc[1] else None
            closers[word.group() if word else line.raw.strip()] = i

    for i, line in enumerate(lines):
        # A block opener in a block comment nests; a line doc (///, -- |, ##) only mentions it.
        if nested and line.comment and nested.search(line.comment) and (line.kind in ("block", "multi") or (
                line.kind == "doc" and not line.raw.lstrip().startswith(("//", "--", "##")))):
            return "nested block comment at line %d" % (i + 1)
        for pattern, name in raw:
            if pattern.search(line.code):
                return "%s at line %d" % (name, i + 1)
        if heredoc and "<<" in line.code:
            if any(closers.get(m.group("tag"), -1) > i for m in heredoc[0].finditer(line.raw)):
                return "heredoc at line %d" % (i + 1)
    return None


# --------------------------------------------------------------------------
# Debt markers (AGENTS.md, "Debt"). A marker opens a comment and carries its
# own exit condition:
#
#     "shortcut: <quantified ceiling>; <revisit trigger>"   deliberate simplification
#     "FIXME: <defect>; <trigger condition>"                 known defect
#
# The ceiling needs at least one digit ("~10k entries", "<1ms", "n > 5000"),
# the trigger a ';' followed by at least two words, a '>' or one of the
# words in TRIGGER. 'honey:' is the legacy spelling of 'shortcut:'. A bare
# marker is always a violation: TODO, XXX, HACK and a lowercase 'todo:'
# ('fixme:' is a FIXME). In a
# file with a lexer family only real comments count, never a string literal;
# other text files fall back to a line match on #, //, --, /* and a leading
# * (<!-- in markup).
# --------------------------------------------------------------------------

_TAGS = r"shortcut:|honey:|(?:TODO|XXX|HACK|FIXME)(?!\w)|todo:|fixme:"
MARKER = re.compile(r"(?P<tag>" + _TAGS + r")(?P<rest>.*)", re.S)
# Why two words after a ';': "; later" or "; sigh" names no condition anyone
# could ever check, and the trigger exists to say when the debt falls due.
TRIGGER = re.compile(r";\s*[\w'-]+\s+\w|>|\b(?:if|when|whenever|once|unless|until|after|upon|triggers?|over|above|past|exceeds|"
                     r"beyond)\b", re.I)
PROSE_EXT = (".md", ".mdc", ".markdown", ".rst", ".txt")
MARKUP_EXT = (".html", ".htm", ".xhtml", ".xml", ".vue", ".svelte")
_MARK_AHEAD = r"(?=" + _TAGS + r")"
_FALLBACK = re.compile(r"(?:(?:^|(?<=\s))(?:#|//|--|/\*)|^\s*\*)\s*" + _MARK_AHEAD)
_FALLBACK_MARKUP = re.compile(r"<!--\s*" + _MARK_AHEAD)


def debt_verdict(tag, rest):
    """Return '' for a well-formed marker, else its violation: TODO, NO-CEILING or NO-TRIGGER."""
    if tag.upper().startswith(("TODO", "XXX", "HACK")):
        return "TODO"
    trigger = TRIGGER.search(rest) is not None
    if tag.upper().startswith("FIXME"):
        return "" if trigger else "NO-TRIGGER"
    if not re.search(r"\d", rest):
        return "NO-CEILING"
    return "" if trigger else "NO-TRIGGER"


def debt_scannable(path):
    """Return True unless `path` is prose (it talks about markers) or minified JavaScript."""
    low = path.lower()
    return not low.endswith(PROSE_EXT) and not low.endswith(".min.js")


def debt_markers(path, text, numbers=None):
    """Return (line, verdict, source line) for every debt marker that opens a comment in `text`.

    `verdict` is '' for a well-formed marker, else TODO, NO-CEILING or
    NO-TRIGGER. `numbers`, when given, limits the result to those 1-based
    lines. Prose, minified JavaScript and text containing a NUL byte (binary)
    yield []. One marker per line: the first comment that opens with one.
    """
    if not debt_scannable(path) or "\0" in text[:8192]:
        return []
    family = family_of(path)
    out = []
    if family:
        for no, line in enumerate(scan(text, family), 1):
            if numbers is not None and no not in numbers:
                continue
            for part in line.parts:
                m = MARKER.match(part)
                if m:
                    out.append((no, debt_verdict(m.group("tag"), m.group("rest")), line.raw.strip()))
                    break
        return out
    opener = _FALLBACK_MARKUP if path.lower().endswith(MARKUP_EXT) else _FALLBACK
    for no, raw in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
        if numbers is not None and no not in numbers:
            continue
        m = opener.search(raw)
        mark = MARKER.match(raw, m.end()) if m else None
        if mark:
            out.append((no, debt_verdict(mark.group("tag"), mark.group("rest")), raw.strip()))
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
# #[repr], extern struct, [StructLayout], ctypes); exact: every member was
# understood and no #if selects members, so a cell naming no field, a field
# missing between two cells and a header sum can be judged (when False only
# the order and widths of the fields found are).
Struct = namedtuple("Struct", "name aliases line first last fields packed exact")

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
                         r"struct|union|enum|mutable)\b|\bpub\([^)]*\)")
# Up to three levels of nested parentheses: __attribute__((aligned(8))).
_PAREN = r"\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*\)"
_C_ATTR = re.compile(r"\b(?:__attribute__|__declspec|alignas|_Alignas|__aligned)\s*" + _PAREN
                     + r"|\b__packed\b|\[\[.*?\]\]")
_STRUCT_OPEN = {
    "c": re.compile(r"\b(?:struct|union)\b(?P<head>[^;{}()=]*(?:(?:__attribute__|__declspec|alignas|_Alignas|__aligned)"
                    r"\s*" + _PAREN + r"[^;{}()=]*)*)\{"),
    "cs": re.compile(r"\bstruct\s+(?P<head>\w+)[^;{}=]*\{"),
    "rs": re.compile(r"\bstruct\s+(?P<head>\w+)(?:<[^>{]*>)?\s*(?:where[^{]*)?\{"),
    "zig": re.compile(r"\bconst\s+(?P<head>\w+)\s*=\s*(?P<mod>(?:packed|extern)\s+)?struct\b[^{=]*\{"),
    "go": re.compile(r"\btype\s+(?P<head>\w+)\s+struct\s*\{"),
    "kt": re.compile(r"\bstruct\s+(?P<head>\w+)[^{=]*\{"),
}
_PY_STRUCT = re.compile(r"^\s*class\s+(?P<head>\w+)\s*\([^)]*\b(?:Structure|Union|LittleEndianStructure|"
                        r"BigEndianStructure)\b[^)]*\)\s*:")
_PY_FIELD = re.compile(r"\(\s*[\"'](?P<name>\w+)[\"']\s*,\s*(?P<type>(?:[^,()]|\([^()]*\))+?)"
                       r"(?:\s*,\s*(?P<bits>\d+))?\s*\)")
_MARKER_ABOVE = re.compile(r"#\[repr\(|\[StructLayout\(|#\s*pragma\s+pack|@frozen\b|__attribute__\s*\(\(\s*_*packed")
_C_DIRECTIVE = re.compile(r"(?m)^[ \t]*#.*$")
_C_CONDITIONAL = re.compile(r"(?m)^[ \t]*#\s*(?:if|ifdef|ifndef|elif|else)\b")
_C_BODY = re.compile(r"\)\s*(?:(?:const|override|final|noexcept|volatile|mutable)\b\s*|&&?\s*|->\s*[\w:<>*&, ]+?\s*)*\{")
_C_METHOD_END = re.compile(r"\)\s*(?:(?:const|override|final|noexcept|volatile|mutable)\b\s*|&&?\s*)*$")
_C_MACRO_FIELD = re.compile(r"^[A-Z_][A-Z0-9_]*\s*" + _PAREN + r"\s*\**\s*(?P<name>[A-Za-z_]\w*)\s*(?:\[\s*\w*\s*\])?$")
_C_NO_STORAGE = re.compile(r"^(?:static|typedef|using|friend|template|enum|namespace|class|struct|union)\b(?![^{]*\{)"
                           r"(?:\s+\w+)?$|^(?:static|typedef|using|friend|template|namespace)\b")


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


def _drop_bodies(body):
    """Replace each inline function body of a C++/C# struct by ';', so the member after it stands alone.

    Why: brace matching is a concern of its own (a semantic boundary), and
    keeping it apart leaves _members a plain pipeline: directives out,
    bodies out, split, parse each member.
    """
    out, pos = [], 0
    while True:
        m = _C_BODY.search(body, pos)
        if not m:
            out.append(body[pos:])
            return "".join(out)
        depth, j = 0, m.end() - 1
        while j < len(body):
            depth += {"{": 1, "}": -1}.get(body[j], 0)
            if depth == 0:
                break
            j += 1
        out.append(body[pos:m.end() - 1] + ";")
        pos = j + 1


def _c_fields(statement, family, packed):
    """Return (fields, understood) for one C, C++ or C# member statement.

    Members without storage (methods, static members, typedefs, nested type
    definitions without a declarator) give ([], True). Why `understood`: C
    declarators (bitfields, arrays, function pointers, several names sharing
    one type) and C++/C# members form a small grammar of their own, and a
    statement this function does not positively recognize must make the
    struct inexact instead of silently losing a field, which would turn every
    cell naming it into a false DIAGRAM-FIELD.
    """
    s = _C_ATTR.sub(" ", statement)
    s = re.sub(r"\b(?:public|private|protected|internal)\s*:", " ", s)
    if family == "cs":
        s = re.sub(r"^\s*(?:\[[^\[\]]*\]\s*)+", "", s)
        s = re.sub(r"^(?:(?:public|private|protected|internal|readonly|unsafe|volatile|new)\s+)+", "", s.strip())
        if re.match(r"(?:const|static)\b", s):
            return [], True
    s = " ".join(s.split())
    if not s or "=>" in s or _C_NO_STORAGE.match(s):
        return [], True
    if "virtual" in s.split():
        return [], False

    # Nested type definitions: a declarator after '}' is a field; an anonymous
    # struct or union adds its own members to this layout, which is not read.
    if re.match(r"(?:struct|union|enum|class)\b[^{]*\{", s):
        tail = re.search(r"\}\s*\**\s*([A-Za-z_]\w*)\s*(?:\[\s*\w*\s*\])?$", s)
        if tail:
            return [Field(tail.group(1), None, False)], True
        return [], bool(re.match(r"(?:struct|union|enum|class)\s+\w+", s)) or s.startswith("enum")

    # Declarators, with default initializers dropped at depth 0: int x = 5, y{6}.
    parts = [re.sub(r"(?<=[\w\]])\s*\{[^{}]*\}$", "", (_split_members(p, "=") or [p])[0])
             for p in _split_members(s, ",")]
    if not parts:
        return [], True

    # Parentheses: a function pointer is a field; a method has no storage; a
    # macro (or a std::function<...> member) may hide one, so it is not read.
    if any("(" in p for p in parts):
        fp = re.match(r"^[^(]*\(\s*\*\s*(\w+)\s*\)", parts[0])
        macro = _C_MACRO_FIELD.match(parts[0])
        if fp:
            return [Field(fp.group(1), None, False)], True
        if macro:
            return [Field(macro.group("name"), None, False)], False
        is_macro_call = re.match(r"[A-Z_][A-Z0-9_]*\s*\(", parts[0]) is not None
        return [], bool(_C_METHOD_END.search(parts[-1])) and not is_macro_call
    if any("{" in p for p in parts):
        return [], False

    # Plain declarators sharing one type: type name[N] : bits, *name2, ...
    head = re.match(r"^(?P<type>.*?)\b(?P<name>[A-Za-z_]\w*)\s*(?:\[\s*(?P<arr>\w*)\s*\])?\s*(?::\s*(?P<bits>\d+))?\s*$",
                    parts[0])
    if not head or not head.group("type").strip():
        return [], False
    out = []
    for i, part in enumerate(parts):
        m = head if i == 0 else re.match(r"^\s*\**\s*(?P<name>[A-Za-z_]\w*)\s*(?:\[\s*(?P<arr>\w*)\s*\])?"
                                         r"\s*(?::\s*(?P<bits>\d+))?\s*$", part)
        if not m:
            return out, False
        if m.group("bits"):
            out.append(Field(m.group("name"), int(m.group("bits")), True))
            continue
        bits = type_bits(head.group("type") + ("*" if "*" in part else ""), family, packed)
        arr = m.group("arr")
        if arr is not None:
            bits = bits * int(arr) if bits and arr.isdigit() else None
        out.append(Field(m.group("name"), bits, False))
    return out, True


def _members(body, family, packed):
    """Return (fields, exact) for the masked body of a brace-delimited struct."""
    fields, exact = [], True

    # C family: preprocessor lines leave the body; an #if makes the layout
    # depend on the build, so the struct cannot be exact.
    if family in ("c", "cs"):
        exact = not _C_CONDITIONAL.search(body)
        body = _drop_bodies(_C_DIRECTIVE.sub("", body))
        if family == "cs":
            body = re.sub(r"\{\s*(?:\w+\s+)?(?:get|set|init)\b[^{}]*\}", ";", body)
        for stmt in _split_members(body, ";"):
            got, understood = _c_fields(stmt, family, packed)
            fields += got
            exact = exact and understood
        return fields, exact

    # Rust and Zig: name: type, one per line or comma; attributes and
    # methods carry no storage.
    if family in ("rs", "zig"):
        for stmt in _split_members(body, ",\n"):
            if re.match(r"#!?\[[^\]]*\]$", stmt) or re.match(r"(?:pub(?:\([^)]*\))?\s+)?(?:fn|const|var|comptime)\b",
                                                              stmt):
                continue
            f = re.match(r"^(?:#\[[^\]]*\]\s*)*(?:pub(?:\([^)]*\))?\s+)?(?P<name>[A-Za-z_]\w*)\s*:\s*(?P<type>[^=]+?)"
                         r"\s*(?:=.*)?$", stmt)
            if f:
                fields.append(Field(f.group("name"), type_bits(f.group("type"), family, packed), False))
            else:
                exact = False
        return fields, exact

    # Go: names type `tag`, or an embedded type whose name is the field's.
    if family == "go":
        for stmt in _split_members(body, ";\n"):
            f = re.match(r"^(?P<names>[A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s+(?P<type>[^\s\"]+)", stmt)
            embedded = re.match(r"^\*?(?:[A-Za-z_]\w*\.)?(?P<name>[A-Za-z_]\w*)(?:\s*\"\")?$", stmt)
            if f:
                for fname in f.group("names").split(","):
                    fields.append(Field(fname.strip(), type_bits(f.group("type"), family, packed), False))
            elif embedded:
                fields.append(Field(embedded.group("name"), None, False))
            else:
                exact = False
        return fields, exact

    # Swift: stored properties are var/let; everything else has no storage.
    for stmt in _split_members(body, ";\n"):
        f = re.match(r"^(?:@\w+(?:\([^)]*\))?\s+)*(?:\w+\s+)*(?:var|let)\s+(?P<name>\w+)\s*:\s*(?P<type>[\w.]+)\s*$", stmt)
        if f:
            fields.append(Field(f.group("name"), type_bits(f.group("type"), family, packed), False))
        elif re.match(r"^(?:@\w+(?:\([^)]*\))?\s+)*(?:\w+\s+)*(?:var|let)\b", stmt):
            exact = False
    return fields, exact


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

        # Name, typedef alias, base classes and layout markers.
        inherits = False
        if family == "c":
            head = re.split(r"(?<!:):(?!:)", _C_ATTR.sub(" ", m.group("head")), maxsplit=1)
            inherits = len(head) > 1
            words = [w for w in re.findall(r"[A-Za-z_]\w*", head[0])
                     if w not in ("__attribute__", "__packed__", "packed", "aligned", "__aligned__", "final")]
            name = words[-1] if words else ""
            alias = re.match(r"\s*\**\s*([A-Za-z_]\w*)\s*[;,]", code[j + 1:j + 200])
            aliases = [alias.group(1)] if alias else []
        else:
            name, aliases = m.group("head"), []
        line_no = line_of(m.start())
        above = "\n".join(line.code for line in lines[max(0, line_no - 4):line_no])
        packed = ("packed" in m.group(0) or bool(m.groupdict().get("mod")) or bool(_MARKER_ABOVE.search(above)))
        fields, exact = _members(body, family, packed)
        out.append(Struct(name, aliases, line_no, line_of(open_at), line_of(j), fields, packed,
                          exact and not inherits))
    return out


def _py_structs(lines):
    """Return the ctypes Structures of a Python file, with fields read from the raw _fields_ list.

    Why: these structs are classes delimited by indentation with fields in a
    list of tuples, so they share nothing with the brace walk in structs().
    A _fields_ that is not one literal list, or an _anonymous_ member, makes
    the struct inexact.
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
            t = re.sub(r"^ctypes\.", "", " ".join(f.group("type").split()))
            arr = re.fullmatch(r"(?:ctypes\.)?(c_\w+)\s*\*\s*(\d+)", t)
            if f.group("bits"):
                fields.append(Field(f.group("name"), int(f.group("bits")), True))
            elif arr:
                bits = type_bits(arr.group(1), "py")
                fields.append(Field(f.group("name"), bits * int(arr.group(2)) if bits else None, False))
            else:
                fields.append(Field(f.group("name"), type_bits(t, "py"), False))
        exact = re.search(r"\b_fields_\s*=\s*\[", text) is not None \
            and not re.search(r"\b_fields_\s*(?:\+=|=\s*\[[^\]]*\]\s*\+)|\b_anonymous_\b", text)
        out.append(Struct(m.group("head"), [], i + 1, i + 2, last + 1, fields, True, exact))
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


# Why a retry: on Windows another process (an antivirus, an indexer) can hold a
# file under .git for a moment, and git then fails with one of these messages.
# Anything else fails at once: a retry could only repeat it.
GIT_TRANSIENT = re.compile(r"unable to|failed to|permission denied|cannot lock|could not (?:open|read|lock)", re.I)
GIT_ATTEMPTS = 3
_git_failures = 0


def git(args, cwd):
    """Run git in `cwd` and return stdout, or None if git is missing or exits non-zero.

    Exit 1 with nothing on stderr is git's plain "no" (rev-parse -q --verify
    of a missing ref, grep without a match), and so is "not a git repository".
    Any other non-zero exit, a git that cannot start and a timeout are
    failures: a transient one is retried, up to GIT_ATTEMPTS runs in all, and
    one that lasts is counted by git_failures(), since None alone cannot tell
    "no" from "broken".
    """
    global _git_failures
    for attempt in range(1, GIT_ATTEMPTS + 1):
        try:
            r = subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=cwd,
                               capture_output=True, encoding="utf-8", errors="replace", timeout=30)
        except (OSError, subprocess.SubprocessError):
            break
        if r.returncode == 0:
            return r.stdout
        if (r.returncode == 1 and not r.stderr.strip()) or "not a git repository" in r.stderr:
            return None
        if attempt == GIT_ATTEMPTS or not GIT_TRANSIENT.search(r.stderr):
            break
        time.sleep(0.05 * attempt)
    _git_failures += 1
    return None


def git_failures():
    """Return how many git commands of this process failed for good; see git()."""
    return _git_failures


def repo_root(cwd):
    """Return the top directory of the git work tree containing `cwd`, or None."""
    out = git(["rev-parse", "--show-toplevel"], cwd or ".")
    return out.strip() if out else None


def resolve_base(root, base):
    """Return `base` if it names a commit, else the empty tree (repository without commits)."""
    return base if git(["rev-parse", "--verify", "-q", base + "^{commit}"], root) else EMPTY_TREE


# One file the session changed, as the Stop gate hands it to each check: rel
# (path from the repository root, forward slashes), path (absolute), text
# (current content), old (content at the baseline, '' when new), new (no
# baseline version) and added (1-based line numbers new or changed since it).
Change = namedtuple("Change", "rel path text old new added")


# A file a tool writes: nobody can fix a finding in it by hand, because the
# next run of the tool undoes the fix. These are the header conventions of Go
# ("Code generated ... DO NOT EDIT.") and of Meta's tools (@generated); both
# live in a comment near the top, so strings and later lines do not count.
_GENERATED_HEADER = re.compile(r"(?m)^\s*(?:#|//+|/\*+|\*|--|<!--|;+)\s*(?:Code generated .*DO NOT EDIT\.|.*@generated\b)")
GENERATED_HEAD_LINES = 10
_ATTR_BATCH = 100


def generated(root, rels, text_of):
    """Return the subset of `rels` that a tool writes.

    A file is generated when its name holds `.gen.`, when a comment in its
    first GENERATED_HEAD_LINES lines carries a generated-code header, or when
    .gitattributes marks it linguist-generated (git answers that one, since it
    knows the precedence rules). `text_of(rel)` returns its text or None.
    """
    out, rest = set(), []
    for rel in rels:
        named = ".gen." in os.path.basename(rel).lower()
        if named or _GENERATED_HEADER.search("\n".join((text_of(rel) or "").split("\n")[:GENERATED_HEAD_LINES])):
            out.add(rel)
        else:
            rest.append(rel)
    for i in range(0, len(rest), _ATTR_BATCH):
        for line in (git(["check-attr", "linguist-generated", "--", *rest[i:i + _ATTR_BATCH]], root) or "").splitlines():
            path, _, value = line.rpartition(": linguist-generated: ")
            if value in ("set", "true"):
                out.add(path)
    return out


def tracked_changes(root, base, cached=False):
    """Return (relative path, is_new) for tracked files added, modified or renamed since `base`.

    is_new is True when the path has no version in `base`. `cached` compares
    the index instead of the work tree.

    Why --name-status and not one `cat-file -e` per file: that probe answers
    exit 128 both for a missing path and for a git that broke, and a wrong
    "new" makes every old line of the file count as written by the session.
    """
    out = []
    args = ["diff", "--name-status", "--diff-filter=AMR"] + (["--cached"] if cached else []) + [base, "--"]
    for line in (git(args, root) or "").splitlines():
        fields = line.split("\t")
        out.append((fields[-1], fields[0] != "M"))
    return out


def changed_files(root, base, keep=family_of):
    """List (relative path, is_new) for files that differ from `base` or are untracked.

    Only paths for which keep(path) is true are listed (default: code files).
    is_new is True when the file has no version in `base`, so every one of
    its lines counts as added. Deleted files are skipped.
    """
    out = [(rel, new) for rel, new in tracked_changes(root, base)
           if keep(rel) and os.path.isfile(os.path.join(root, rel))]
    untracked = git(["ls-files", "--others", "--exclude-standard"], root) or ""
    for rel in untracked.splitlines():
        if keep(rel) and os.path.isfile(os.path.join(root, rel)):
            out.append((rel, True))
    return out


def baseline_text(root, base, rel):
    """Return the content of `rel` at `base`, or '' when it does not exist there."""
    if base == EMPTY_TREE:
        return ""
    return git(["show", "%s:%s" % (base, rel)], root) or ""


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)


def added_lines(root, base, rel, cached=False):
    """Return the 1-based line numbers of `rel` that are new or changed since `base` (in the index when `cached`)."""
    out = git(["diff", "-U0", "--no-color", "--no-ext-diff"] + (["--cached"] if cached else []) + [base, "--", rel],
              root) or ""
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


def edit_lines(data):
    """Return (path, text, lines) for the Edit, MultiEdit or Write described by hook payload `data`.

    `lines` holds the 1-based line numbers this call wrote: every line for a
    Write; each occurrence of each new_string (at most 50 per edit) for Edit
    and MultiEdit. Returns None when the payload names no readable file.
    Why judge only these lines: the PostToolUse level must stay silent about
    everything the edit did not write (see the design notes above).
    """
    tool_input = data.get("tool_input") or {}
    if not tool_input.get("file_path"):
        return None
    path = os.path.abspath(tool_input["file_path"])
    text = read_text(path)
    if text is None:
        return None
    text = text.replace("\r\n", "\n")
    edits = [] if "content" in tool_input else (tool_input.get("edits") or [tool_input])
    lines = set(range(1, text.count("\n") + 2)) if not edits else set()
    for edit in edits:
        new = (edit.get("new_string") or "").replace("\r\n", "\n")
        start, hits = (text.find(new), 0) if new.strip() else (-1, 0)
        while start >= 0 and hits < 50:
            first = text.count("\n", 0, start) + 1
            lines.update(range(first, first + new.count("\n") + 1))
            start, hits = text.find(new, start + len(new)), hits + 1
    return path, text, lines


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
