#!/usr/bin/env python3
"""craft-comment-sync.py -- catch comments that drifted away from the code.

Usage:
  craft-comment-sync.py [--check-diff] [--base REF]  audit code files changed since REF (default HEAD)
  craft-comment-sync.py --hook                       PostToolUse: the comments one Edit/Write/MultiEdit wrote

Findings, one line each ("path:line: [KIND] detail"):
  [ORPHAN]  an identifier the change removed from the code of a file is used by
            no code anywhere in the repository, yet a comment still names it:
            a stale Function, Design, Teacher or Checklist comment, in any
            file. A prose document (DESIGN.md, README) that names it is a
            review signal; history files (CHANGELOG, NEWS) are left alone.
            Distinctive names (snake_case, camelCase, digits, ALL_CAPS) match
            as whole words; plain words only as `name` or name(.
  [BACKUP]  a new comment is commented-out code. Inside a block of comment
            lines it takes a majority of code-shaped lines, so prose with one
            code-like line is never Backup.
  [TRIVIAL] a new one-line comment restates the statement it describes: the
            single statement under a standalone comment (a blank line, a
            comment, a closing brace or a dedent ends its block), or the same
            line for a trailing comment. Hard when >= 80% of its content words
            (at most 6; abbreviations such as buf and buffer count as one) are
            in that statement and at most one is missing; from 60% a review
            signal, since the missing words may be the point. A comment over
            two or more statements titles a block (a Guide comment), and one
            on a declaration (field, enum member, macro, initializer slot,
            prototype, file-scope variable) documents a name: neither is ever
            Trivial.
  [VAGUE]   review signal: a new one-line comment of <= 8 words made only of
            generic words ("Process the data", "Handle edge cases", "Update
            state if needed"): no domain term, symbol, operator, number or
            causal connective. Numbered Guide comments (1:, 3a.) are exempt.
  [DIAGRAM-FIELD] [DIAGRAM-ORDER] [DIAGRAM-SIZE]
            a Teacher diagram drawn with [field:size] cells (see craftlib.py)
            disagrees with the struct it documents: a cell names no field, a
            field declared between two adjacent cells has no cell, two fields
            appear out of declaration order, or a width differs from a
            fixed-width type or bitfield. Synthetic cells (header, hdr, pad,
            padding, payload) need no field; a sized header must equal the
            fields it summarizes. A struct with members the parser cannot read
            is checked for order and widths only. Free-form diagrams without
            cells are never checked.

--hook reports only BACKUP and hard TRIVIAL findings, on the lines the call
wrote: an orphan, or a diagram that disagrees with its struct, may be the
middle of a change spread over several edits, and review signals are the
Stop gate's to show once, so all of those wait for craft-gate.py, which runs
every check here over what the session changed.
--check-diff judges BACKUP, TRIVIAL and VAGUE only on lines changed since REF,
and a diagram only when the change touches it or its struct, so legacy
comments never block new work. Exit codes: 0 clean, 1 findings on stdout, 2
findings on stderr in --hook mode. Outside a git repository --check-diff has
no baseline and exits 0. See the design notes in craftlib.py.
"""

import argparse
import os
import re
import sys
from collections import Counter

import craftlib as cl

STOP = set("a an the to of and or in on for with by is are be this that it its from at as into then we our".split())
KEYWORDS = set("""
    if else for while return int char void static const struct def class self this true false null none
    let var func fn pub mut impl use import from as new delete sizeof unsigned long short double float bool
    string async await yield try catch finally throw throws public private protected interface type enum
    switch case break continue default goto do in of is not and or lambda pass raise with global nonlocal
    assert del package extends implements super export typeof instanceof defer go chan map range select
    match where loop crate mod trait unsafe extern inline volatile register union typedef auto template
    typename namespace using virtual override final operator friend explicit mutable constexpr nullptr
    noexcept decltype""".split())
NOT_DISTINCTIVE = {"NULL", "TRUE", "FALSE", "TODO", "FIXME", "NOTE", "XXX", "HACK"}

# ORPHAN asks git grep about at most MAX_NAMES names per call: one call is
# far cheaper than one per name, and 200 names stay well below the 32k
# characters of a Windows command line.
MAX_NAMES = 200
# Prose files whose job is to name what was removed: never ORPHAN.
HISTORY = re.compile(r"^(?:changelog|changes|history|news|releases?)\b", re.I)
# The line number of a report line "path:line: [KIND] ...", whatever the path holds.
LINE_NO = re.compile(r":(\d+): \[")

# A comment carrying rationale, a step number or an example is never Trivial
# or Backup, whatever its words: it answers a question the code cannot.
RATIONALE = re.compile(r"\b(?:why|because|since|otherwise|so that|avoid|must|never|don't|do not|note|warning|"
                       r"careful|workaround|order|invariant|assum\w*|guarantee\w*|unsafe|bug|regression|"
                       r"fixme|shortcut|checklist|step)\b", re.I)
STEP = re.compile(r"^(?:\d+[a-z]?[.:)]|[a-z][.)]\s|step\b)", re.I)
EXAMPLE = re.compile(r"\b(?:e\.g\.|i\.e\.|example|usage)\b", re.I)
# Words that carry no information on their own: a short comment made only of
# these says nothing the code does not ("Process the data").
GENERIC = set("""process handle manage perform execute run do check update set get compute calculate data
    value item logic state operation case edge info information properly correctly stuff thing code function
    method variable result input output main helper misc various work needed necessary accordingly
    appropriately here some all if later now etc new old""".split())
CAUSAL = re.compile(r"\b(?:due|prevent|before|after|unless|until|instead|only|cannot|can't|would|could)\b", re.I)
# A symbol, a number or an operator names something exactly: "Handle >=" titles
# one comparison case, it is not "Handle the cases".
SPECIFIC = re.compile(r"[a-z][A-Z]|\w_\w|\b[A-Z]{3,}\b|\d|`[^`]+`|==|!=|<=|>=|&&|\|\||<<|>>|->|=>")
# Abbreviations and their words count as one word, so "Free the query
# buffer." restates free(c->query_buf) exactly.
ABBREV = {"buf": "buffer", "len": "length", "ptr": "pointer", "idx": "index", "cnt": "count", "num": "number",
          "str": "string", "msg": "message", "err": "error", "cfg": "config", "configuration": "config",
          "arg": "argument", "func": "function", "fn": "function", "alloc": "allocate", "allocation": "allocate",
          "init": "initialize", "initialization": "initialize", "tmp": "temporary", "prev": "previous",
          "cur": "current", "curr": "current", "dst": "destination", "src": "source", "pos": "position",
          "val": "value", "elem": "element", "obj": "object"}
# TRIVIAL tiers. All or all but one of a comment's words in its statement is
# a restatement (hard); from 60% it may be one (a review signal): "Return OOM
# on incomplete stack info." over `if (it->stack.oom) return 0;` shares 3 of
# 5 words, and the two it adds explain the condition.
TRIVIAL_HARD, TRIVIAL_SIGNAL = 0.8, 0.6
# A comment on a declaration documents a name, the Function comment of data:
# `int backup_state; /* BACKUP_STATE_* */` says which values the field takes,
# `dictSdsKeyCompare, /* key compare */` which slot an initializer fills. So
# TRIVIAL judges statements only. Lexical, one line of code at a time:
DECLARATION = [re.compile(p) for p in (
    r",$",                                                 # list element: initializer slot, enum member, argument
    r"^#\s*define\b",                                      # macro
    r"^(?!(?:return|else|case|goto|throw|delete|new|break|continue|yield|await|typeof|sizeof)\b)"
    r"(?:[A-Za-z_][\w:<>]*[\s*&]+)+[*&]*[A-Za-z_]\w*(?:\s*\[[^\]]*\])*(?:\s*:\s*\d+)?\s*;$",   # field: type name;
    r"^(?!(?:return|else|case|goto|throw|delete|new|yield|await)\b)"
    r"(?:[A-Za-z_][\w:<>]*[\s*&]+)+[*&]*[A-Za-z_][\w:]*\s*\((?:[^()]|\([^()]*\))*\)\s*(?:const\s*)?;$",  # prototype
)]
FAMILY_DECLARATION = {
    "py": re.compile(r"^(?!(?:else|try|finally|except|lambda)\b)[A-Za-z_]\w*\s*:\s*[^=\s][^=]*(?:=.*)?$"),  # x: int
    "js": re.compile(r"^(?:readonly\s+)?[A-Za-z_$][\w$]*\??\s*:\s*[^=;]+;?$"),                    # name?: Type;
    "go": re.compile(r"^(?:(?:var|const|type)\s+\w+.*|(?!(?:return|go|defer|goto|break|continue|fallthrough|case|"
                     r"default|select|package|import)\b)[A-Za-z_]\w*\s+[\w.*\[\]]+(?:\s+`[^`]*`)?)$"),  # Name string
}
# Where every statement ends in ';', a line without one that is a bare name,
# possibly with a value (an enum's last member), declares it too; at column 0
# a storage class opens a file-scope declaration, whatever initializes it.
SEMICOLON_FAMILIES = ("c", "cs", "php")
FILE_SCOPE = re.compile(r"^(?:static|extern|const|__thread|thread_local|_Thread_local|typedef)\b")
BARE_NAME = re.compile(r"[A-Za-z_]\w*(?:\s*=\s*[^;{}]+)?")
PROSE = re.compile(r"^[A-Z][a-z']*\s+\S+\s+\S+.*[.!?]$")
VERBS = ((re.compile(r"\+\+|\+="), "increment add increase"),
         (re.compile(r"--|-="), "decrement subtract decrease"),
         (re.compile(r"(?<![=!<>+\-*/%&|^])=(?!=)"), "set assign store"))
# A line that only closes a block ends the statements a comment describes.
CLOSER = re.compile(r"^(?:[)\]}]+[;,]?|end|fi|done|esac)$")

_C_CODE = [re.compile(p) for p in (
    r"^[\w.$>\-\[\]*&:!]+\s*\(.*\)\s*;$",                                 # call;
    r"^[\w.$>\-\[\]*&:]+\s*(?:[-+*/%&|^]|<<|>>)?=(?!=)\s*\S.*;$",         # assignment;
    r"^(?:[\w.$>\-\[\]]+(?:\+\+|--)|(?:\+\+|--)[\w.$>\-\[\]]+);$",        # i++;
    r"^(?:return|break|continue|goto|throw|delete)\b.*;$",                 # jump;
    r"^(?:if|for|while|switch)\s*\(.*\)\s*\{?$",                           # control header
    r"^\}?\s*else\b(?:\s+if\s*\(.*\))?\s*\{?$",                            # else, else if
    r"^[{}];?$",                                                           # lone brace
    r"^#\s*(?:include|define|undef|ifdef|ifndef|endif|pragma)\b",          # preprocessor
    r"^(?:let|const|var|auto|int|char|long|unsigned|size_t|float|double|bool|void|static|struct)\b[^.]*[;{]$",
)]
_C_CALL_NO_SEMI = re.compile(r"^[\w.$]+\s*\(.*\)$")   # js, go, rs, zig: ';' is optional
_C_FAMILIES = ("c", "cs", "kt", "php", "go", "rs", "zig", "js")
# Hash, dash, semicolon and percent languages: Ruby, shell, Lua, SQL, Lisp...
_OTHER_CODE = [re.compile(p) for p in (
    r"^[\w.:$@]+\s*\(.*\)\s*;?$",                                          # call
    r"^(?:local\s+)?[\w.$@\[\]]+\s*(?:=|<-|:=)(?!=)\s*\S",                 # assignment
    r"^\(\s*[\w*+/<>=!?-]+(?:\s.*)?\)$",                                   # Lisp form
    r"^(?i:select|insert|update|delete|create|drop|alter)\b.*;$",          # SQL statement
    r"^(?:def|defp|fn|function|proc|sub|local\s+function)\s+[\w.:]+.*$",    # definition
    r"^(?:end|done|fi|esac)$",                                             # block closer
)]
_PY_CODE = [re.compile(p) for p in (
    r"^(?:def|class)\s+\w+.*:$",
    r"^(?:if|elif|while|for|with|try|except|finally|else)\b[^.]*:$",
    r"^(?:import\s+[\w.]+(?:\s+as\s+\w+)?|from\s+[\w.]+\s+import\s+[\w., ()*]+)$",
    r"^(?:return|yield|raise|del|assert)\b.*[()\[\].=]",
    r"^(?:pass|break|continue|return)$",
    r"^[\w.\[\]'\"]+\s*(?:[-+*/%&|^]|//|\*\*)?=(?!=)\s*[^\s=]",
    r"^[\w.]+\(.*\)$",
)]


def words(text):
    """Return the lowercase content words of `text`: camelCase and snake_case split, plurals and abbreviations folded.

    Bare numbers are dropped: "Return 0 on OOM" above `return 0;` carries one
    real word the code lacks (OOM), and literals must not outvote it.
    """
    out = set()
    for tok in re.findall(r"[A-Za-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)):
        w = tok.lower()
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        w = ABBREV.get(w, w)
        if w not in STOP and not w.isdigit():
            out.add(w)
    return out


def trivial(comment, code):
    """Return (share, missing) when >= TRIVIAL_SIGNAL of the comment's words are in `code`, else None.

    A number the code does not show is information, whatever the words: redis
    wrote `dictCreate(&BenchmarkDictTypeNoValue);  /* no_value = 1 */`.

    `share` is the fraction of the comment's content words (at most 6) that
    `code` already holds, `missing` the number of the others.
    """
    if set(re.findall(r"\b\d+\b", comment)) - set(re.findall(r"\b\d+\b", code)):
        return None
    if RATIONALE.search(comment) or STEP.match(comment) or EXAMPLE.search(comment):
        return None
    said = words(comment)
    if not said or len(said) > 6:
        return None
    seen = words(code)
    for pattern, verbs in VERBS:
        if pattern.search(code):
            seen.update(verbs.split())
    hit = len(said & seen)
    return (hit / len(said), len(said) - hit) if hit / len(said) >= TRIVIAL_SIGNAL else None


def declaration(line, family):
    """Return True when the code of `line` declares a name instead of running a step (see DECLARATION)."""
    code = line.code.strip()
    if any(p.search(code) for p in DECLARATION):
        return True
    if family in FAMILY_DECLARATION:
        return FAMILY_DECLARATION[family].match(code) is not None
    if family not in SEMICOLON_FAMILIES:
        return False
    at_file_scope = not line.raw[:1].isspace() and FILE_SCOPE.match(code) is not None
    return at_file_scope or BARE_NAME.fullmatch(code) is not None


def backup(comment, family):
    """Return True when a one-line comment reads as commented-out code rather than prose."""
    if PROSE.match(comment) or EXAMPLE.search(comment):
        return False
    if family == "py":
        return any(p.search(comment) for p in _PY_CODE)
    if family not in _C_FAMILIES:
        return any(p.search(comment) for p in _OTHER_CODE)
    return any(p.search(comment) for p in _C_CODE) or (family != "c" and bool(_C_CALL_NO_SEMI.match(comment)))


def block_is_code(lines, no, family):
    """Return True when most lines of the comment block holding line `no` read as code.

    Why: commented-out code comes in uniform blocks, while a prose block may
    hold one line shaped like an assignment ("ceiling = at least one digit").
    The block is the run of comment-only lines of the same kind around `no`.
    """
    def same(i):
        return 0 <= i < len(lines) and lines[i].comment is not None and not lines[i].code.strip() \
            and lines[i].kind == lines[no - 1].kind
    lo = hi = no - 1
    while same(lo - 1):
        lo -= 1
    while same(hi + 1):
        hi += 1
    run = [lines[i].comment.strip() for i in range(lo, hi + 1) if lines[i].comment.strip()]
    return sum(backup(c, family) for c in run) * 2 > len(run)


def vague(comment):
    """Return True when a one-line comment of <= 8 words is made only of generic words."""
    if len(comment.split()) > 8 or STEP.match(comment) or SPECIFIC.search(comment) \
            or RATIONALE.search(comment) or CAUSAL.search(comment):
        return False
    said = words(comment)
    return bool(said) and said <= GENERIC


def described_code(lines, no):
    """Return the Line of the single statement a standalone comment at 1-based line `no` describes, or None.

    None when the comment belongs to a multi-line comment block or heads two
    or more statements: then it titles a block (a Guide comment) or teaches
    (a Teacher comment) instead of restating one statement. The block ends at
    a blank line, a comment, a line that only closes a block, or a line
    indented less than the block's first line.
    """
    prev = lines[no - 2] if no >= 2 else None
    if prev is not None and prev.comment is not None and not prev.code.strip():
        return None
    block, indent = [], None
    for line in lines[no:]:
        code = line.code.strip()
        depth = len(line.raw) - len(line.raw.lstrip())
        if not code or CLOSER.match(code) or (indent is not None and depth < indent):
            break
        indent = depth if indent is None else indent
        block.append(line)
        if len(block) > 1:
            return None
    return block[0] if block else None


def cell_bits(size, bitfield):
    """Return the width in bits a diagram cell states (1B, 8b, 4 bytes, 29), or None.

    A bare number is bits for a bitfield and bytes for anything else, the way
    raxNode writes size:29 and sds writes len:1.
    """
    m = cl.CELL_SIZE.match(size)
    if not m:
        return None
    num, unit = int(m.group("num")), m.group("unit") or ""
    if unit.startswith(("B", "byte")):
        return num * 8
    return num if unit or bitfield else num * 8


def diagram_findings(lines, family, added, where):
    """Return (key, message) for [field:size] cells that disagree with their struct; see the module docstring.

    A struct is checked only when this change touched it or its diagram, it
    has parsed fields, and its diagram has at least two cells: a lone
    "[note: see below]" in prose is not a layout. An inexact struct (see
    craftlib.Struct) skips every check that needs its complete field list.
    """
    out = []
    for st in cl.structs(lines, family):
        window = cl.diagram_window(lines, st)
        touched = added & (set(range(st.line, st.last + 1)) | {no for no, _ in window})
        cells = [(no, m.group("name"), m.group("size").strip(), m.start(), m.end(), text)
                 for no, text in window for m in cl.CELL.finditer(text)]
        if not st.fields or not touched or len(cells) < 2:
            continue
        counts = Counter(f.name.lower() for f in st.fields)
        index = {f.name.lower(): (i, f) for i, f in enumerate(st.fields) if counts[f.name.lower()] == 1}
        title = st.name or (st.aliases[0] if st.aliases else "struct")

        def emit(no, kind, subject, text):
            out.append(("sync|%s|%s|%s|%s" % (where, kind, title, subject), "%s:%d: [%s] %s" % (where, no, kind, text)))

        # Every real cell names a field and agrees with its width.
        named = []
        for no, name, size, _, _, _ in cells:
            if cl.SYNTHETIC.match(name):
                continue
            hit = index.get(name.lower())
            if hit is None:
                if st.exact and name.lower() not in counts:
                    emit(no, "DIAGRAM-FIELD", name, "cell '%s' names no field of '%s' (fields: %s)"
                         % (name, title, ", ".join(f.name for f in st.fields)))
                continue
            named.append((no, name, hit[0]))
            bits = cell_bits(size, hit[1].bitfield)
            if bits is not None and hit[1].bits is not None and bits != hit[1].bits:
                emit(no, "DIAGRAM-SIZE", name, "cell '%s:%s' says %d bits, the code declares %d"
                     % (name, size, bits, hit[1].bits))

        # Order: within each strip (a repeated field starts a new one) indices only grow.
        strip, prev = set(), None
        for no, name, idx in named:
            if idx in strip:
                strip, prev = set(), None
            if prev is not None and idx < prev[2]:
                emit(no, "DIAGRAM-ORDER", name, "cell '%s' comes after '%s' but '%s' declares it first"
                     % (name, prev[1], title))
            strip.add(idx)
            prev = (no, name, idx)
        if not st.exact:
            continue

        # Gaps: two adjacent cells on one line, with nothing between them, name
        # fields i < j; a field declared between them that no cell names is missing.
        drawn = {name.lower() for _, name, _, _, _, _ in cells}
        for a, b in zip(cells, cells[1:]):
            first, second = index.get(a[1].lower()), index.get(b[1].lower())
            if a[0] != b[0] or a[5][a[4]:b[3]].strip() or not first or not second or second[0] <= first[0] + 1:
                continue
            missing = [f.name for f in st.fields[first[0] + 1:second[0]] if f.name.lower() not in drawn]
            if missing:
                emit(a[0], "DIAGRAM-FIELD", ",".join(missing), "'%s' declares %s between '%s' and '%s', but no "
                     "cell draws %s" % (title, ", ".join(missing), a[1], b[1], "them" if len(missing) > 1 else "it"))

        # A sized header covers exactly the leading fields that no cell names.
        named_idx = {idx for _, _, idx in named}
        covered = [f for i, f in enumerate(st.fields[:min(named_idx, default=len(st.fields))]) if i not in named_idx]
        for no, name, size, _, _, _ in cells:
            if not re.match(r"(?i)h(?:ea)?d(?:e)?r", name) or not covered or any(f.bits is None for f in covered):
                continue
            bits, total = cell_bits(size, all(f.bitfield for f in covered)), sum(f.bits for f in covered)
            if bits is not None and bits != total:
                emit(no, "DIAGRAM-SIZE", name, "cell '%s:%s' says %d bits, the fields it summarizes (%s) add up to %d"
                     % (name, size, bits, ", ".join(f.name for f in covered), total))
    return out


def mentions(comment, name):
    """Return True if `comment` names identifier `name` (see the ORPHAN rule in the module docstring).

    Why: names unlikely to be plain English (snake_case, camelCase, digits,
    ALL_CAPS) match as whole words; a plain word such as `count` matches only
    as `count` or count(, or every English sentence would look stale.
    """
    core = name.strip("_$")
    distinctive = len(core) >= 3 and name not in NOT_DISTINCTIVE and (
        "_" in core or any(ch.isdigit() for ch in core) or re.search(r"[a-z][A-Z]", core) is not None
        or (core.isupper() and len(core) >= 4))
    if distinctive:
        return re.search(r"(?<![\w$])%s(?![\w$])" % re.escape(name), comment) is not None
    return ("`%s`" % name) in comment or re.search(r"(?<![\w$.])%s\(" % re.escape(name), comment) is not None


def clip(text, width=60):
    """Shorten `text` for a one-line report."""
    return text if len(text) <= width else text[:width - 3] + "..."


def comment_findings(lines, family, added, where):
    """Return (hard, soft) lists of (key, message) for BACKUP, TRIVIAL and VAGUE among the comment lines in `added`.

    These judge one comment against its own code, so they are safe after any
    single edit (the PostToolUse level of craftlib's design notes). Hard:
    BACKUP and TRIVIAL at TRIVIAL_HARD; soft: TRIVIAL below it and VAGUE.
    """
    hard, soft = [], []
    for no in sorted(added):
        if no > len(lines):
            continue
        line = lines[no - 1]
        said = (line.comment or "").strip()
        if not said or line.kind not in ("line", "block", "doc"):
            continue
        in_block = any(0 <= j < len(lines) and lines[j].comment is not None and not lines[j].code.strip()
                       for j in (no - 2, no))
        if line.kind == "doc" and in_block and not line.code.strip():
            continue
        if line.code.strip():
            target = line
        elif line.kind != "doc" and backup(said, family) and (not in_block or block_is_code(lines, no, family)):
            hard.append(("sync|%s|BACKUP|%s" % (where, said),
                         "%s:%d: [BACKUP] commented-out code, git is the backup: '%s'" % (where, no, clip(said))))
            continue
        else:
            target = described_code(lines, no)
        # Why: a doc comment is a Function comment, never deletable as Trivial; it can only be Vague.
        stated = trivial(said, target.code) if target and line.kind != "doc" and not declaration(target, family) \
            else None
        if stated and stated[0] >= TRIVIAL_HARD and stated[1] <= 1:
            hard.append(("sync|%s|TRIVIAL|%s" % (where, said),
                         "%s:%d: [TRIVIAL] restates its code (%d%% word overlap), delete or say why: '%s'"
                         % (where, no, round(stated[0] * 100), clip(said))))
        elif stated:
            soft.append(("sync|%s|TRIVIAL|%s" % (where, said),
                         "%s:%d: [TRIVIAL] %d%% of its words are in the code: what does it add? If nothing, "
                         "delete it: '%s'" % (where, no, round(stated[0] * 100), clip(said))))
        elif (line.code.strip() or not in_block) and vague(said):
            soft.append(("sync|%s|VAGUE|%s" % (where, said),
                         "%s:%d: [VAGUE] which domain fact, limit or reason does it name? Name one, or delete "
                         "it: '%s'" % (where, no, clip(said))))
    return hard, soft


def audit(where, family, text, added):
    """Return (hard, soft) lists of (key, message) for the checks of this script that read one file alone.

    `added` holds the 1-based line numbers the change wrote. ORPHAN needs
    the whole repository: see orphan_findings(). Keys omit line numbers,
    which shift with every edit; see craft-gate.py.
    """
    lines = cl.scan(text, family)
    hard, soft = comment_findings(lines, family, added, where)
    return hard + diagram_findings(lines, family, added, where), soft


def orphan_findings(root, changes, show=None):
    """Return (hard, soft) lists of ORPHAN (key, message) for the session in `changes`.

    `changes` are craftlib Change records; `show` maps a repository-relative
    path to the one printed (default: unchanged). The steps:

    1. Candidates: identifiers that left the code of a changed file and are
       in the code of no changed file (a move between two changed files is
       not a removal).
    2. One `git grep -nozIwF` per MAX_NAMES candidates lists every line of
       the repository, tracked or untracked, that holds one as a word.
    3. The lexer splits those lines: a candidate in code anywhere is alive
       (it moved, or was only renamed locally); a gone one is cited by each
       comment that names it (hard) and by each line of a prose document
       that names it (soft). History files are skipped.

    Outside git, git grep finds nothing and only step 1 runs: no findings.
    """
    show = show or (lambda rel: rel)

    # 1. Names that left the code of a changed file and stay in none.
    removed, present = set(), set()
    for ch in changes:
        family = cl.family_of(ch.rel)
        if family:
            now = cl.code_identifiers(cl.scan(ch.text, family))
            present |= now
            if ch.old:
                removed |= cl.code_identifiers(cl.scan(ch.old, family)) - now
    names = sorted(s for s in removed - present if len(s.strip("_$")) >= 3 and s.lower() not in KEYWORDS)

    # 2. Every line that holds one of them as a word, grouped by name and file.
    hits = {}
    for i in range(0, len(names), MAX_NAMES):
        args = ["grep", "-nozIwF", "--untracked"]
        for name in names[i:i + MAX_NAMES]:
            args += ["-e", name]
        for record in (cl.git(args, root) or "").splitlines():
            parts = record.split("\0")
            if len(parts) == 3 and parts[1].isdigit():
                hits.setdefault(parts[2], {}).setdefault(parts[0], []).append(int(parts[1]))

    # 3. Code keeps a name alive; comments and documents that still name a gone one are reported.
    texts = {}

    def lines_of(rel):
        """Return (scanned lines or None, raw lines) of `rel`, read once per call of orphan_findings."""
        if rel not in texts:
            text = cl.read_text(os.path.join(root, rel)) or ""
            family = cl.family_of(rel)
            texts[rel] = (cl.scan(text, family) if family else None, text.replace("\r\n", "\n").split("\n"))
        return texts[rel]

    # Walk the hits of each name, file by file, until one in code proves it alive.
    hard, soft = [], []
    for name in names:
        alive, cited = False, []
        for rel, numbers in sorted(hits.get(name, {}).items()):
            prose = rel.lower().endswith(cl.PROSE_EXT)
            if prose and HISTORY.match(os.path.basename(rel)):
                continue
            scanned, raw = lines_of(rel)
            for no in numbers:
                if scanned is not None and no <= len(scanned):
                    line = scanned[no - 1]
                    if name in cl.code_identifiers([line]):
                        alive = True
                        break
                    if line.comment and mentions(line.comment, name):
                        cited.append((rel, no, True))
                elif prose and no <= len(raw) and mentions(raw[no - 1], name):
                    cited.append((rel, no, False))
            if alive:
                break
        # A gone name: every comment still citing it must be fixed, a document is only flagged.
        for rel, no, in_code in [] if alive else cited:
            message = ("%s:%d: [ORPHAN] '%s' is gone from the code but this comment still names it" if in_code else
                       "%s:%d: [ORPHAN] '%s' is gone from the code: is what this document says about it still true?")
            (hard if in_code else soft).append(("sync|%s|ORPHAN|%s" % (rel, name), message % (show(rel), no, name)))
    order = (lambda item: (item[1].split(":", 1)[0], int(item[1].split(":", 2)[1])))
    return sorted(hard, key=order), sorted(soft, key=order)


def from_hook(data):
    """Return the report lines for BACKUP, TRIVIAL and VAGUE among the lines the call in `data` wrote."""
    got = cl.edit_lines(data)
    family = cl.family_of(got[0]) if got else None
    if not family:
        return []
    path, text, added = got
    lines = cl.scan(text, family)
    # Why: in a file the lexer misreads, code may come out as a comment; the
    # Stop gate reports such findings as review signals instead.
    if cl.inexact_reason(path, text, lines):
        return []
    hard, _ = comment_findings(lines, family, added, cl.shown(path))
    return [msg for _, msg in hard]


def from_git(base):
    """Return the report lines for every code file that changed since `base` or is untracked; [] outside git.

    The CLI prints hard findings and review signals alike: whoever runs it
    asked for the whole picture.
    """
    root = cl.repo_root(os.getcwd())
    if not root:
        return []
    base = cl.resolve_base(root, base)
    out, changes = [], []
    for rel, new in cl.changed_files(root, base):
        path = os.path.join(root, rel)
        text = cl.read_text(path)
        family = cl.family_of(rel)
        if text is None or not family:
            continue
        if new:
            added, old = set(range(1, text.count("\n") + 2)), ""
        else:
            added, old = cl.added_lines(root, base, rel), cl.baseline_text(root, base, rel)
        changes.append(cl.Change(rel, path, text, old, new, added))
        hard, soft = audit(cl.shown(path), family, text, added)
        out += sorted((msg for _, msg in hard + soft), key=lambda msg: int(LINE_NO.search(msg).group(1)))
    hard, soft = orphan_findings(root, changes, lambda rel: cl.shown(os.path.join(root, rel)))
    return [msg for _, msg in hard + soft] + out


def gate_findings(root, changes):
    """Stop gate: return (hard, soft) lists of (key, message) for every check of this script on `changes`."""
    hard, soft = orphan_findings(root, changes)
    for ch in changes:
        family = cl.family_of(ch.rel)
        if family:
            file_hard, file_soft = audit(ch.rel, family, ch.text, ch.added)
            hard, soft = hard + file_hard, soft + file_soft
    return hard, soft


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check-diff", action="store_true", help="audit files changed since --base (default)")
    parser.add_argument("--base", default="HEAD", help="git revision to diff against (default HEAD)")
    parser.add_argument("--hook", action="store_true", help="PostToolUse mode: read the hook JSON on stdin")
    args = parser.parse_args()
    found = from_hook(cl.hook_input()) if args.hook else from_git(args.base)
    return cl.report(found, args.hook)


if __name__ == "__main__":
    sys.exit(main())
