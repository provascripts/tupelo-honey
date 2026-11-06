#!/usr/bin/env python3
"""craft-comment-sync.py -- catch comments that drifted away from the code.

Usage:
  craft-comment-sync.py [--check-diff] [--base REF]  audit code files changed since REF (default HEAD)
  craft-comment-sync.py --hook                       PostToolUse: audit the Edit/Write/MultiEdit on stdin

Findings, one line each ("path:line: [KIND] detail"):
  [ORPHAN]  an identifier removed from the code is still named by a comment of
            the same file: a stale Function, Design, Teacher or Checklist
            comment. Distinctive names (snake_case, camelCase, digits,
            ALL_CAPS) match as whole words; plain words only as `name` or name(.
  [BACKUP]  a new one-line comment is commented-out code.
  [TRIVIAL] a new one-line comment restates its code: >= 60% of its content
            words (at most 6) already appear in the code it describes. That is
            the next line for a standalone comment heading <= 3 code lines,
            the same line for a trailing comment.
  [VAGUE]   a new one-line comment of <= 8 words made only of generic words
            ("Process the data", "Handle edge cases", "Update state if
            needed"): no domain term, symbol, number or causal connective.
            Numbered Guide comments (1:, 3a.) are exempt.
  [DIAGRAM-FIELD] [DIAGRAM-ORDER] [DIAGRAM-SIZE]
            a Teacher diagram drawn with [field:size] cells (see craftlib.py)
            disagrees with the struct it documents: a cell names no field, two
            fields appear out of declaration order, or a width differs from a
            fixed-width type or bitfield. Synthetic cells (header, hdr, pad,
            padding, payload) need no field; a sized header must equal the
            fields it summarizes. Free-form diagrams without cells are never
            checked.

Only lines written by the edit (--hook) or since REF (--check-diff) are judged
for BACKUP, TRIVIAL and VAGUE, and a diagram only when the change touches it
or its struct, so legacy comments never block new work. Exit codes:
0 clean, 1 findings on stdout, 2 findings on stderr in --hook mode. Outside a
git repository --check-diff has no baseline and exits 0. See the design notes
in craftlib.py.
"""

import argparse
import os
import re
import sys

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
SPECIFIC = re.compile(r"[a-z][A-Z]|\w_\w|\b[A-Z]{3,}\b|\d|`[^`]+`")
PROSE = re.compile(r"^[A-Z][a-z']*\s+\S+\s+\S+.*[.!?]$")
VERBS = ((re.compile(r"\+\+|\+="), "increment add increase"),
         (re.compile(r"--|-="), "decrement subtract decrease"),
         (re.compile(r"(?<![=!<>+\-*/%&|^])=(?!=)"), "set assign store"))

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
    """Return the lowercase content words of `text`: camelCase and snake_case split, plurals folded.

    Bare numbers are dropped: "Return 0 on OOM" above `return 0;` carries one
    real word the code lacks (OOM), and literals must not outvote it.
    """
    out = set()
    for tok in re.findall(r"[A-Za-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)):
        w = tok.lower()
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        if w not in STOP and not w.isdigit():
            out.add(w)
    return out


def trivial(comment, code):
    """Return the share of the comment's words already present in `code` when >= 0.6, else None."""
    if RATIONALE.search(comment) or STEP.match(comment) or EXAMPLE.search(comment):
        return None
    said = words(comment)
    if not said or len(said) > 6:
        return None
    seen = words(code)
    for pattern, verbs in VERBS:
        if pattern.search(code):
            seen.update(verbs.split())
    ratio = len(said & seen) / len(said)
    return ratio if ratio >= 0.6 else None


def backup(comment, family):
    """Return True when a one-line comment reads as commented-out code rather than prose."""
    if PROSE.match(comment) or EXAMPLE.search(comment):
        return False
    if family == "py":
        return any(p.search(comment) for p in _PY_CODE)
    if family not in _C_FAMILIES:
        return any(p.search(comment) for p in _OTHER_CODE)
    return any(p.search(comment) for p in _C_CODE) or (family != "c" and bool(_C_CALL_NO_SEMI.match(comment)))


def vague(comment):
    """Return True when a one-line comment of <= 8 words is made only of generic words."""
    if len(comment.split()) > 8 or STEP.match(comment) or SPECIFIC.search(comment) \
            or RATIONALE.search(comment) or CAUSAL.search(comment):
        return False
    said = words(comment)
    return bool(said) and said <= GENERIC


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
    """Check [field:size] cells against the struct they document; see the module docstring.

    A struct is checked only when this change touched it or its diagram, it
    has parsed fields, and its diagram has at least two cells: a lone
    "[note: see below]" in prose is not a layout.
    """
    out = []
    for st in cl.structs(lines, family):
        window = cl.diagram_window(lines, st)
        touched = added & (set(range(st.line, st.last + 1)) | {no for no, _ in window})
        cells = [(no, m.group("name"), m.group("size").strip()) for no, text in window for m in cl.CELL.finditer(text)]
        if not st.fields or not touched or len(cells) < 2:
            continue
        index = {f.name.lower(): (i, f) for i, f in enumerate(st.fields)}
        title = st.name or (st.aliases[0] if st.aliases else "struct")

        # Every real cell names a field and agrees with its width.
        named = []
        for no, name, size in cells:
            if cl.SYNTHETIC.match(name):
                continue
            hit = index.get(name.lower())
            if hit is None:
                out.append("%s:%d: [DIAGRAM-FIELD] cell '%s' names no field of '%s' (fields: %s)"
                           % (where, no, name, title, ", ".join(f.name for f in st.fields)))
                continue
            named.append((no, name, hit[0]))
            bits = cell_bits(size, hit[1].bitfield)
            if bits is not None and hit[1].bits is not None and bits != hit[1].bits:
                out.append("%s:%d: [DIAGRAM-SIZE] cell '%s:%s' says %d bits, the code declares %d"
                           % (where, no, name, size, bits, hit[1].bits))

        # Order: within each strip (a repeated field starts a new one) indices only grow.
        strip, prev = set(), None
        for no, name, idx in named:
            if idx in strip:
                strip, prev = set(), None
            if prev is not None and idx < prev[2]:
                out.append("%s:%d: [DIAGRAM-ORDER] cell '%s' comes after '%s' but '%s' declares it first"
                           % (where, no, name, prev[1], title))
            strip.add(idx)
            prev = (no, name, idx)

        # A sized header covers exactly the leading fields that no cell names.
        named_idx = {idx for _, _, idx in named}
        covered = [f for i, f in enumerate(st.fields[:min(named_idx, default=len(st.fields))]) if i not in named_idx]
        for no, name, size in cells:
            if not re.match(r"(?i)h(?:ea)?d(?:e)?r", name) or not covered or any(f.bits is None for f in covered):
                continue
            bits, total = cell_bits(size, all(f.bitfield for f in covered)), sum(f.bits for f in covered)
            if bits is not None and bits != total:
                out.append("%s:%d: [DIAGRAM-SIZE] cell '%s:%s' says %d bits, the fields it summarizes (%s) add up to %d"
                           % (where, no, name, size, bits, ", ".join(f.name for f in covered), total))
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


def described_code(lines, no):
    """Return the code a standalone comment at 1-based line `no` describes, or None.

    None when the comment belongs to a multi-line comment block or heads more
    than 3 code lines: then it is a Guide or Teacher comment, not a restatement.
    """
    prev = lines[no - 2] if no >= 2 else None
    if prev is not None and prev.comment is not None and not prev.code.strip():
        return None
    end = no
    while end < len(lines) and lines[end].code.strip():
        end += 1
    if end == no or end - no > 3:
        return None
    return lines[no].code


def clip(text, width=60):
    """Shorten `text` for a one-line report."""
    return text if len(text) <= width else text[:width - 3] + "..."


def audit(path, family, text, added, removed):
    """Return the findings for one file.

    `added` holds the 1-based line numbers the change wrote; `removed` the
    identifiers that were in the old code (those still in the code are
    filtered here, so callers may pass the whole old identifier set).
    """
    lines = cl.scan(text, family)
    where = cl.shown(path)
    out = []

    # ORPHAN: identifiers gone from the code but still named by any comment.
    gone = sorted(s for s in removed - cl.code_identifiers(lines)
                  if len(s.strip("_$")) >= 3 and s.lower() not in KEYWORDS)
    if gone:
        for no, line in enumerate(lines, 1):
            for sym in gone:
                if line.comment and mentions(line.comment, sym):
                    out.append("%s:%d: [ORPHAN] '%s' is gone from the code but this comment still names it"
                               % (where, no, sym))

    # BACKUP, TRIVIAL and VAGUE: only one-line comments this change wrote.
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
            target = line.code
        elif line.kind != "doc" and backup(said, family):
            out.append("%s:%d: [BACKUP] commented-out code, git is the backup: '%s'" % (where, no, clip(said)))
            continue
        else:
            target = described_code(lines, no)
        # Why: a doc comment is a Function comment, never deletable as Trivial; it can only be Vague.
        ratio = trivial(said, target) if target and line.kind != "doc" else None
        if ratio:
            out.append("%s:%d: [TRIVIAL] restates its code (%d%% word overlap), delete or say why: '%s'"
                       % (where, no, round(ratio * 100), clip(said)))
        elif (line.code.strip() or not in_block) and vague(said):
            out.append("%s:%d: [VAGUE] says nothing the code does not: name the domain fact, limit or reason: '%s'"
                       % (where, no, clip(said)))

    # Layout diagrams drawn with [field:size] cells, against their struct.
    return out + diagram_findings(lines, family, added, where)


def from_hook(data):
    """Audit the file written by the Edit, MultiEdit or Write call described by hook payload `data`.

    Why: judging only the text this call wrote (new_string, or the whole
    content of a Write) keeps the hook stateless and silent about legacy
    comments elsewhere in the file.
    """
    tool_input = data.get("tool_input") or {}
    path = os.path.abspath(tool_input.get("file_path") or "")
    family = cl.family_of(path)
    text = cl.read_text(path) if family else None
    if text is None:
        return []
    text = text.replace("\r\n", "\n")

    edits = [] if "content" in tool_input else (tool_input.get("edits") or [tool_input])

    # Lines this call wrote: all of them for a Write; each new_string's lines
    # for Edit and MultiEdit (at most 50 occurrences per edit).
    added = set(range(1, text.count("\n") + 2)) if not edits else set()
    for edit in edits:
        new = (edit.get("new_string") or "").replace("\r\n", "\n")
        start, hits = (text.find(new), 0) if new.strip() else (-1, 0)
        while start >= 0 and hits < 50:
            first = text.count("\n", 0, start) + 1
            added.update(range(first, first + new.count("\n") + 1))
            start, hits = text.find(new, start + len(new)), hits + 1

    # The file as it was before this call, lexed whole. Why: an old_string
    # lexed on its own turns a piece of docstring or comment into "code" and
    # every word in it into a false ORPHAN; in context it stays a comment.
    response = data.get("tool_response")
    old = response.get("originalFile") if isinstance(response, dict) else None
    if not isinstance(old, str) and edits:
        old = text
        for edit in reversed(edits):
            new = (edit.get("new_string") or "").replace("\r\n", "\n")
            if not new or new not in old:
                old = None
                break
            old = old.replace(new, (edit.get("old_string") or "").replace("\r\n", "\n"),
                              -1 if edit.get("replace_all") else 1)
    if not isinstance(old, str):
        root = cl.repo_root(os.path.dirname(path))
        old = (cl.git(["show", "HEAD:" + os.path.relpath(path, root).replace(os.sep, "/")], root) if root else "") \
            or ""
    return audit(path, family, text, added, cl.code_identifiers(cl.scan(old.replace("\r\n", "\n"), family)))


def from_git(base):
    """Audit every code file that changed since `base` or is untracked; [] outside git."""
    root = cl.repo_root(os.getcwd())
    if not root:
        return []
    base = cl.resolve_base(root, base)
    out = []
    for rel, new in cl.changed_files(root, base):
        path = os.path.join(root, rel)
        text = cl.read_text(path)
        if text is None:
            continue
        family = cl.family_of(rel)
        if new:
            added, old = set(range(1, text.count("\n") + 2)), ""
        else:
            added, old = cl.added_lines(root, base, rel), cl.baseline_text(root, base, rel)
        removed = cl.code_identifiers(cl.scan(old, family)) if old else set()
        out += audit(path, family, text, added, removed)
    return out


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
